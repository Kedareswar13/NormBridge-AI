"""Deterministic, free answer-quality evaluation for NormBridge AI (no LLM-as-judge).

Two layers:
  1. Online evals  - every generated answer is scored against the chunks it was generated from
                     (stored in `answer_evals`, shown live in the admin Evals tab).
  2. Offline evals - a golden question set (evals/golden_set.json) is replayed through the real /chat
                     endpoint and scored against expected documents / facts (stored in `eval_runs` + `eval_results`).

All metrics are lexical/structural, so they are reproducible, instant and cost nothing:
  groundedness          mean share of each answer sentence's content words that appear in the retrieved sources
  supported_ratio       share of answer sentences with >= SUPPORT_THRESHOLD of their content words in the sources
  numeric_faithfulness  share of numbers in the answer (days, %, amounts, clause ids) that appear in the sources
  context_relevance     share of the question's content words found in the retrieved sources (retrieval quality)
  answer_relevance      share of the question's content words echoed in the answer (did it address the question)
  refused               answer is a refusal / "not mentioned" response
Golden-set only:
  doc_hit               the expected policy PDF is the one the answer was grounded in
  fact_recall           share of expected fact groups (any-of keyword alternatives) present in the answer
  refusal_correct       refused exactly when the case expects a refusal
"""
import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL, PORT

SUPPORT_THRESHOLD = 0.6
GOLDEN_SET_PATH = Path(__file__).resolve().parent.parent.parent / 'evals' / 'golden_set.json'
HELDOUT_SET_PATH = GOLDEN_SET_PATH.parent / 'heldout_set.json'

STOPWORDS = set('''
a about above after again against all also am an and any are as at be because been before being below between both but by can could
did do does doing down during each either else etc few for from further had has have having he her here hers herself him himself his how
i if in into is it its itself just may me might more most must my myself no nor not now of off on once only or other our ours ourselves
out over own per same shall she should so some such than that the their theirs them themselves then there these they this those through
to too under until up upon us very via was we were what when where which while who whom why will with within without would yes yet you
your yours yourself yourselves policy policies university srm ap document documents provided mentioned according based information
please note however therefore also following given state states stated regarding related any must may per i.e e.g
'''.split())

REFUSAL_PATTERNS = [
    r"\bnot (?:explicitly )?(?:mentioned|specified|covered|stated|provided|available|included)\b",
    r"\bdo(?:es)? not (?:contain|mention|specify|include|cover|provide)\b",
    r"\bcannot (?:provide|answer|find|assist)\b",
    r"\bcan(?:'|no)?t help with (?:that|this)\b",
    r"\bi only answer questions\b",
    r"\bcouldn'?t find\b",
    r"\bcan only answer questions\b",
    r"\bonly answer questions related\b",
    r"\bconfidence in generating an accurate answer is too low\b",
    r"\bno (?:specific )?information (?:is )?(?:available|provided|found)\b",
]


# --------------------------------------------------------------------------- text helpers
def _stem(word):
    for suffix in ('ations', 'ation', 'ments', 'ment', 'ingly', 'ings', 'ing', 'edly', 'ies', 'ied', 'ness', 'ers', 'er', 'ed', 'es', 'ly', 's'):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ('y' if suffix in ('ies', 'ied') else '')
    return word


def content_tokens(text):
    words = re.findall(r"[a-z0-9]+(?:[.'][a-z0-9]+)*", (text or '').lower())
    return [_stem(w) for w in words if (w not in STOPWORDS and len(w) > 2) or w.isdigit()]


NUMBER_RE = re.compile(r'(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s?%)?')
WORD_NUMBERS = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
                'ten': '10', 'fifteen': '15', 'twenty': '20', 'thirty': '30', 'sixty': '60', 'seventy': '70', 'seventy-five': '75', 'hundred': '100'}


def numbers_in(text):
    text = (text or '').lower()
    nums = {n.replace(',', '').rstrip('.') for n in NUMBER_RE.findall(text)}
    nums |= {v for k, v in WORD_NUMBERS.items() if re.search(rf'\b{k}\b', text)}
    return nums


def split_sentences(text):
    text = re.sub(r'\*\*|__|`', '', text or '')
    parts = re.split(r'(?<=[.!?])\s+|\n+|\s+[-•*]\s+', text)
    return [p.strip(' -•*\t') for p in parts if len(content_tokens(p)) >= 3]


def is_refusal(answer):
    """A refusal leads with "not mentioned / can't answer" (or is short and says so).
    A real answer that ends with a polite caveat ("details are not provided...") is *hedged*, not refused."""
    a = (answer or '').strip().lower()
    if not a:
        return True
    first = re.split(r'(?<=[.!?])\s+|\n+', a, maxsplit=1)[0]
    if any(re.search(p, first) for p in REFUSAL_PATTERNS):
        return True
    return len(a.split()) < 35 and any(re.search(p, a) for p in REFUSAL_PATTERNS)


def is_hedged(answer):
    a = (answer or '').lower()
    return not is_refusal(answer) and any(re.search(p, a) for p in REFUSAL_PATTERNS)


# --------------------------------------------------------------------------- scoring
def evaluate_answer(question, answer, sources, retrieval_score=None):
    """Score one answer against the sources it was generated from. Pure function, no I/O."""
    source_text = ' '.join(s.get('text') or s.get('text_snippet') or '' for s in (sources or []))
    src_tokens = set(content_tokens(source_text))
    src_numbers = numbers_in(source_text)
    q_tokens = set(content_tokens(question))
    a_tokens = set(content_tokens(answer))
    refused = is_refusal(answer)

    sentence_scores, unsupported = [], []
    for sent in split_sentences(answer):
        toks = set(content_tokens(sent))
        cov = len(toks & src_tokens) / len(toks) if toks else 1.0
        sentence_scores.append(cov)
        if cov < SUPPORT_THRESHOLD:
            unsupported.append(sent[:220])

    ans_numbers = numbers_in(answer)
    bad_numbers = sorted(n for n in ans_numbers if n not in src_numbers)

    metrics = {
        'refused': refused,
        'hedged': is_hedged(answer),
        'has_sources': bool(sources),
        'groundedness': round(sum(sentence_scores) / len(sentence_scores), 3) if sentence_scores and src_tokens else None,
        'supported_ratio': round(sum(1 for s in sentence_scores if s >= SUPPORT_THRESHOLD) / len(sentence_scores), 3) if sentence_scores and src_tokens else None,
        'numeric_faithfulness': round(1 - len(bad_numbers) / len(ans_numbers), 3) if ans_numbers and src_tokens else None,
        'unsupported_numbers': bad_numbers[:10],
        'unsupported_sentences': unsupported[:5],
        'context_relevance': round(len(q_tokens & src_tokens) / len(q_tokens), 3) if q_tokens and src_tokens else None,
        'answer_relevance': round(len(q_tokens & a_tokens) / len(q_tokens), 3) if q_tokens else None,
        'retrieval_score': round(float(retrieval_score), 3) if retrieval_score is not None else None,
        'answer_words': len((answer or '').split()),
        'source_pdfs': sorted({s.get('pdf') for s in (sources or []) if s.get('pdf')}),
    }
    metrics['verdict'] = verdict(metrics)
    return metrics


def verdict(m):
    """pass / warn / fail for a single live answer."""
    if m['refused']:
        return 'abstained'
    if not m['has_sources']:
        return 'fail'  # an answer with no evidence behind it
    g, nf = m['groundedness'], m['numeric_faithfulness']
    if (g is not None and g < 0.45) or (nf is not None and nf < 0.5):
        return 'fail'
    if (g is not None and g < 0.65) or (nf is not None and nf < 1.0):
        return 'warn'
    return 'pass'


def score_golden_case(case, answer, sources, latency):
    m = evaluate_answer(case['question'], answer, sources)
    expected_refusal = bool(case.get('should_refuse'))
    m['refusal_correct'] = m['refused'] == expected_refusal
    expected_pdfs = case.get('expected_pdf') or []
    if isinstance(expected_pdfs, str):
        expected_pdfs = [expected_pdfs]
    m['expected_pdfs'] = expected_pdfs
    m['doc_hit'] = bool(set(expected_pdfs) & set(m['source_pdfs'])) if expected_pdfs else None
    # access-control cases: these documents must NEVER appear for this role
    forbidden = case.get('forbidden_pdf') or []
    if isinstance(forbidden, str):
        forbidden = [forbidden]
    m['acl_violation'] = bool(set(forbidden) & set(m['source_pdfs'])) if forbidden else None

    groups = case.get('must_include') or []
    low = (answer or '').lower()
    hits = [g for g in groups if any(alt.lower() in low for alt in (g if isinstance(g, list) else [g]))]
    m['fact_recall'] = round(len(hits) / len(groups), 3) if groups else None
    m['missing_facts'] = [g if isinstance(g, str) else ' | '.join(g) for g in groups if g not in hits]
    m['latency'] = round(latency, 2)
    # yes/no questions: the right facts with the wrong verdict ("Yes, you may stay... unless permission") must fail
    expected_polarity = (case.get('expected_polarity') or '').lower() or None
    m['polarity'] = answer_polarity(answer)
    m['polarity_ok'] = (m['polarity'] == expected_polarity) if expected_polarity and m['polarity'] else None

    if m['acl_violation']:
        passed = False
    elif forbidden and not expected_pdfs and not expected_refusal:
        passed = True  # pure access-control case: answering from any *allowed* document (or refusing) is fine
    elif expected_refusal:
        passed = m['refused']
    else:
        passed = (not m['refused']) and m['doc_hit'] is not False and (m['fact_recall'] is None or m['fact_recall'] >= 0.5) \
            and (m['groundedness'] is None or m['groundedness'] >= 0.45) and m['polarity_ok'] is not False
    m['passed'] = bool(passed)
    return m


_YES = re.compile(r"^\s*(\*\*)?(yes|yeah|correct|indeed)\b", re.I)
_NO = re.compile(r"^\s*(\*\*)?(no|not|never)\b", re.I)


def answer_polarity(answer):
    """'yes' / 'no' when the answer leads with a verdict, else None (unknown is not penalised)."""
    a = (answer or '').strip()
    if _YES.search(a):
        return 'yes'
    if _NO.search(a):
        return 'no'
    return None


# --------------------------------------------------------------------------- storage
@contextmanager
def _cursor(dict_rows=False):
    """Short-lived autocommit connection (safe from background threads, always closed)."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        with conn.cursor(cursor_factory=RealDictCursor if dict_rows else None) as cur:
            yield cur
    finally:
        conn.close()


def init_eval_tables():
    with _cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS answer_evals (
                chat_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW(),
                verdict VARCHAR(20),
                groundedness FLOAT,
                numeric_faithfulness FLOAT,
                context_relevance FLOAT,
                refused BOOLEAN,
                metrics TEXT
            )''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS eval_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP DEFAULT NOW(),
                finished_at TIMESTAMP,
                status VARCHAR(20) DEFAULT 'running',
                model VARCHAR(100),
                total INTEGER DEFAULT 0,
                completed INTEGER DEFAULT 0,
                passed INTEGER DEFAULT 0,
                summary TEXT,
                triggered_by VARCHAR(100)
            )''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS eval_results (
                id SERIAL PRIMARY KEY,
                run_id INTEGER REFERENCES eval_runs(id) ON DELETE CASCADE,
                case_id VARCHAR(100),
                category VARCHAR(100),
                question TEXT,
                answer TEXT,
                passed BOOLEAN,
                latency FLOAT,
                metrics TEXT
            )''')
        # what changed before this run (shown on the eval timeline: "why did the pass rate move?")
        cur.execute('ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS notes TEXT')
        # runs execute on a thread inside the API process: if the process stopped mid-run, the row stays 'running'
        # forever and blocks the UI. At startup nothing can still be running, so close those honestly.
        cur.execute('''UPDATE eval_runs SET status = 'interrupted', finished_at = COALESCE(finished_at, NOW())
                       WHERE status = 'running' ''')


def record_answer_eval(chat_id, question, answer, sources, retrieval_score=None):
    """Score a live answer and persist it. Never raises - evals must not break chat."""
    try:
        if not chat_id:
            return None
        m = evaluate_answer(question, answer, sources, retrieval_score)
        with _cursor() as cur:
            cur.execute('''INSERT INTO answer_evals (chat_id, verdict, groundedness, numeric_faithfulness, context_relevance, refused, metrics)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (chat_id) DO UPDATE SET verdict = EXCLUDED.verdict, groundedness = EXCLUDED.groundedness,
                               numeric_faithfulness = EXCLUDED.numeric_faithfulness, context_relevance = EXCLUDED.context_relevance,
                               refused = EXCLUDED.refused, metrics = EXCLUDED.metrics''',
                        (chat_id, m['verdict'], m['groundedness'], m['numeric_faithfulness'], m['context_relevance'], m['refused'], json.dumps(m)))
        return m
    except Exception as e:
        print(f"⚠️ [Evals] could not record answer eval for chat {chat_id}: {e}")
        return None


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def live_eval_summary(limit=200, exclude_emails=()):
    with _cursor(True) as cur:
        cur.execute('''SELECT e.chat_id, e.verdict, e.groundedness, e.numeric_faithfulness, e.context_relevance, e.refused, e.metrics,
                              c.question, c.answer, c.timestamp, c.model_used, c.duration, c.user_email
                       FROM answer_evals e JOIN chat_history c ON c.id = e.chat_id
                       WHERE NOT (c.user_email = ANY(%s))
                       ORDER BY e.chat_id DESC LIMIT %s''', (list(exclude_emails), limit))
        rows = cur.fetchall()
    verdicts = {}
    for r in rows:
        verdicts[r['verdict']] = verdicts.get(r['verdict'], 0) + 1
    answered = [r for r in rows if not r['refused']]
    items = []
    for r in rows[:50]:
        m = json.loads(r['metrics'] or '{}')
        items.append({
            'chat_id': r['chat_id'], 'question': r['question'], 'answer': (r['answer'] or '')[:400], 'verdict': r['verdict'],
            'groundedness': r['groundedness'], 'numeric_faithfulness': r['numeric_faithfulness'], 'context_relevance': r['context_relevance'],
            'unsupported_numbers': m.get('unsupported_numbers', []), 'unsupported_sentences': m.get('unsupported_sentences', []),
            'source_pdfs': m.get('source_pdfs', []), 'model': r['model_used'], 'latency': r['duration'],
            'timestamp': r['timestamp'].isoformat() if r['timestamp'] else None,
        })
    return {
        'total': len(rows),
        'verdicts': verdicts,
        'abstention_rate': round(sum(1 for r in rows if r['refused']) / len(rows), 3) if rows else None,
        'avg_groundedness': _avg(r['groundedness'] for r in answered),
        'avg_numeric_faithfulness': _avg(r['numeric_faithfulness'] for r in answered),
        'avg_context_relevance': _avg(r['context_relevance'] for r in answered),
        'pass_rate': round(verdicts.get('pass', 0) / len(answered), 3) if answered else None,
        'items': items,
    }


# --------------------------------------------------------------------------- golden-set runner
def load_golden_set(dataset='golden'):
    """'golden' = hand-written regression set; 'heldout' = generated set never used while tuning (see
    scripts/make_heldout_set.py)."""
    path = HELDOUT_SET_PATH if dataset == 'heldout' else GOLDEN_SET_PATH
    return json.loads(path.read_text(encoding='utf-8'))


_run_lock = threading.Lock()


def start_golden_run(model='phi3:mini', triggered_by='admin', user_email='evals@example.com', case_ids=None, dataset='golden', notes=None):
    """Create a run row and execute it on a background thread. Returns the run id."""
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError('An eval run is already in progress')
    try:
        cases = load_golden_set(dataset)['cases']
        if case_ids:
            cases = [c for c in cases if c['id'] in set(case_ids)]
        with _cursor() as cur:
            cur.execute('INSERT INTO eval_runs (model, total, triggered_by, notes) VALUES (%s, %s, %s, %s) RETURNING id',
                        (model, len(cases), f'{triggered_by} [{dataset}]' if dataset != 'golden' else triggered_by,
                         (notes or '').strip()[:500] or None))
            run_id = cur.fetchone()[0]
    except Exception:
        _run_lock.release()
        raise
    threading.Thread(target=_execute_run, args=(run_id, cases, model, user_email), daemon=True).start()
    return run_id


def _execute_run(run_id, cases, model, user_email):
    api = f'http://127.0.0.1:{PORT}'
    results = []
    try:
        for case in cases:
            t0 = time.time()
            try:
                from core.auth import EVAL_STAFF_EMAIL
                from core.session import issue_token
                asker = EVAL_STAFF_EMAIL if case.get('role') == 'staff' else user_email
                r = requests.post(f'{api}/chat', json={'question': case['question'], 'model': model,
                                                       'session_id': f'eval-run-{run_id}', 'bypass_cache': True,
                                                       'history': case.get('history') or []},
                                  headers={'Authorization': f'Bearer {issue_token(asker)}'}, timeout=600)
                data = r.json()
                answer, sources = data.get('answer', ''), data.get('sources', [])
            except Exception as e:
                answer, sources = f'ERROR: {e}', []
            m = score_golden_case(case, answer, sources, time.time() - t0)
            results.append((case, m))
            with _cursor() as cur:
                cur.execute('''INSERT INTO eval_results (run_id, case_id, category, question, answer, passed, latency, metrics)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                            (run_id, case['id'], case.get('category', 'general'), case['question'], answer, m['passed'], m['latency'], json.dumps(m)))
                cur.execute('UPDATE eval_runs SET completed = completed + 1, passed = passed + %s WHERE id = %s', (1 if m['passed'] else 0, run_id))
        summary = summarize_results([m for _, m in results], [c for c, _ in results])
        status = 'completed'
    except Exception as e:
        summary, status = {'error': str(e)}, 'failed'
    finally:
        _run_lock.release()
    with _cursor() as cur:
        cur.execute('UPDATE eval_runs SET status = %s, finished_at = NOW(), summary = %s WHERE id = %s', (status, json.dumps(summary), run_id))


def summarize_results(metrics, cases):
    answerable = [m for m, c in zip(metrics, cases) if not c.get('should_refuse')]
    unanswerable = [m for m, c in zip(metrics, cases) if c.get('should_refuse')]
    by_cat = {}
    for m, c in zip(metrics, cases):
        cat = by_cat.setdefault(c.get('category', 'general'), {'total': 0, 'passed': 0})
        cat['total'] += 1
        cat['passed'] += 1 if m['passed'] else 0
    return {
        'pass_rate': round(sum(m['passed'] for m in metrics) / len(metrics), 3) if metrics else None,
        'doc_hit_rate': _avg([1.0 if m['doc_hit'] else 0.0 for m in answerable if m['doc_hit'] is not None]),
        'fact_recall': _avg(m['fact_recall'] for m in answerable),
        'groundedness': _avg(m['groundedness'] for m in answerable if not m['refused']),
        'numeric_faithfulness': _avg(m['numeric_faithfulness'] for m in answerable if not m['refused']),
        'context_relevance': _avg(m['context_relevance'] for m in answerable),
        'false_refusal_rate': _avg([1.0 if m['refused'] else 0.0 for m in answerable]),
        'correct_refusal_rate': _avg([1.0 if m['refused'] else 0.0 for m in unanswerable]),
        'avg_latency': _avg(m['latency'] for m in metrics),
        'p95_latency': sorted(m['latency'] for m in metrics)[max(0, int(len(metrics) * 0.95) - 1)] if metrics else None,
        'by_category': by_cat,
    }


def list_runs(limit=20):
    with _cursor(True) as cur:
        cur.execute('SELECT * FROM eval_runs ORDER BY id DESC LIMIT %s', (limit,))
        rows = cur.fetchall()
    return [_run_row(r) for r in rows]


def get_run(run_id):
    with _cursor(True) as cur:
        cur.execute('SELECT * FROM eval_runs WHERE id = %s', (run_id,))
        run = cur.fetchone()
        if not run:
            return None
        cur.execute('SELECT * FROM eval_results WHERE run_id = %s ORDER BY id', (run_id,))
        results = cur.fetchall()
    out = _run_row(run)
    out['results'] = [{
        'case_id': r['case_id'], 'category': r['category'], 'question': r['question'], 'answer': r['answer'],
        'passed': r['passed'], 'latency': r['latency'], 'metrics': json.loads(r['metrics'] or '{}'),
    } for r in results]
    return out


def _run_row(r):
    return {
        'id': r['id'], 'status': r['status'], 'model': r['model'], 'total': r['total'], 'completed': r['completed'], 'passed': r['passed'],
        'started_at': r['started_at'].isoformat() if r['started_at'] else None,
        'finished_at': r['finished_at'].isoformat() if r['finished_at'] else None,
        'summary': json.loads(r['summary']) if r['summary'] else None, 'triggered_by': r['triggered_by'],
        'notes': r.get('notes'), 'dataset': 'heldout' if '[heldout]' in (r['triggered_by'] or '') else 'golden',
    }


def set_run_notes(run_id, notes):
    with _cursor() as cur:
        cur.execute('UPDATE eval_runs SET notes = %s WHERE id = %s', ((notes or '').strip()[:500] or None, run_id))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- eval-driven development view
def case_progress(results, run_ids):
    """results = {"<run id>": passed}; run_ids in chronological order (a case may be absent from subset runs).
    fixed_in_run = first passing run after the LAST failure, i.e. the change that made the fix stick."""
    seq = [results[str(i)] for i in run_ids if str(i) in results]
    now_passing = bool(seq) and seq[-1]
    last_fail = max((i for i in run_ids if results.get(str(i)) is False), default=None)
    return {
        'ever_failed': not all(seq),
        'now_passing': now_passing,
        'first_failed_run': next((i for i in run_ids if results.get(str(i)) is False), None),
        'fixed_in_run': next((i for i in run_ids if last_fail is not None and i > last_fail and results.get(str(i)) is True), None)
        if now_passing else None,
    }


def case_history(dataset='golden'):
    """Pass/fail of every case across every completed run of one dataset: the 'heatmap' that shows when each
    problem was first caught and which change fixed it."""
    with _cursor(True) as cur:
        cur.execute("SELECT * FROM eval_runs WHERE status = 'completed' ORDER BY id")
        runs = [_run_row(r) for r in cur.fetchall()]
        runs = [r for r in runs if r['dataset'] == dataset]
        ids = [r['id'] for r in runs]
        cur.execute('''SELECT run_id, case_id, category, question, passed FROM eval_results
                       WHERE run_id = ANY(%s) ORDER BY id''', (ids or [0],))
        rows = cur.fetchall()
    cases = {}
    for r in rows:
        c = cases.setdefault(r['case_id'], {'case_id': r['case_id'], 'category': r['category'], 'question': r['question'], 'results': {}})
        c['results'][str(r['run_id'])] = bool(r['passed'])
    for c in cases.values():
        c.update(case_progress(c['results'], ids))
    order = sorted(cases.values(), key=lambda c: (not c['ever_failed'], c['now_passing'], c['case_id']))
    slim = [{'id': r['id'], 'model': r['model'], 'total': r['total'], 'passed': r['passed'], 'notes': r['notes'],
             'pass_rate': (r['summary'] or {}).get('pass_rate'), 'started_at': r['started_at']} for r in runs]
    return {'dataset': dataset, 'runs': slim, 'cases': order}


def compare_runs(a, b):
    """Case-by-case diff of two runs: fixed (fail -> pass), regressed (pass -> fail), still failing, with the
    actual answers and what the scorer found missing, so every claimed improvement can be shown, not just stated."""
    ra, rb = get_run(a), get_run(b)
    if not ra or not rb:
        return None
    by_a = {r['case_id']: r for r in ra['results']}
    out = {'a': {k: ra[k] for k in ('id', 'model', 'passed', 'total', 'notes', 'summary')},
           'b': {k: rb[k] for k in ('id', 'model', 'passed', 'total', 'notes', 'summary')},
           'fixed': [], 'regressed': [], 'still_failing': [], 'unchanged_passing': 0, 'only_in_b': 0}

    def brief(r):
        m = r['metrics'] or {}
        return {'answer': (r['answer'] or '')[:600], 'passed': r['passed'], 'missing_facts': m.get('missing_facts') or [],
                'source_pdfs': m.get('source_pdfs') or [], 'groundedness': m.get('groundedness'),
                'polarity_ok': m.get('polarity_ok'), 'refused': m.get('refused'), 'latency': r['latency']}

    for r in rb['results']:
        prev = by_a.get(r['case_id'])
        if prev is None:
            out['only_in_b'] += 1
            continue
        item = {'case_id': r['case_id'], 'category': r['category'], 'question': r['question'], 'before': brief(prev), 'after': brief(r)}
        if not prev['passed'] and r['passed']:
            out['fixed'].append(item)
        elif prev['passed'] and not r['passed']:
            out['regressed'].append(item)
        elif not r['passed']:
            out['still_failing'].append(item)
        else:
            out['unchanged_passing'] += 1
    return out


def export_run_markdown(run):
    s = run.get('summary') or {}
    pct = lambda v: '-' if v is None else f'{v * 100:.0f}%'
    lines = [f"# NormBridge AI eval run #{run['id']}", '',
             f"- Model: `{run['model']}`  |  Cases: {run['total']}  |  Passed: {run['passed']}  |  Status: {run['status']}",
             f"- Started: {run['started_at']}  |  Finished: {run['finished_at']}", '',
             '| Metric | Value |', '|---|---|']
    for key in ('pass_rate', 'doc_hit_rate', 'fact_recall', 'groundedness', 'numeric_faithfulness', 'context_relevance', 'false_refusal_rate', 'correct_refusal_rate'):
        lines.append(f'| {key} | {pct(s.get(key))} |')
    lines += [f"| avg_latency | {s.get('avg_latency')}s |", f"| p95_latency | {s.get('p95_latency')}s |", '',
              '| Case | Category | Pass | Doc hit | Facts | Grounded | Numbers | Latency |', '|---|---|---|---|---|---|---|---|']
    for r in run.get('results', []):
        m = r['metrics']
        lines.append(f"| {r['case_id']} | {r['category']} | {'yes' if r['passed'] else 'NO'} | {m.get('doc_hit')} | {pct(m.get('fact_recall'))} | "
                     f"{pct(m.get('groundedness'))} | {pct(m.get('numeric_faithfulness'))} | {r['latency']}s |")
    lines.append(f"\n_Generated {datetime.now():%Y-%m-%d %H:%M}_")
    return '\n'.join(lines)

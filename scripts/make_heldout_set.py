"""Build a HELD-OUT eval set that was never used while tuning the pipeline.

The golden set was hand-written by the same person who tuned the system, so it can be overfitted. This script
generates questions a different way, using a different model:
  1. Sample public chunks from every policy (fixed seed -> reproducible).
  2. Ask the generator model for a self-contained student question and the shortest answer span, copied VERBATIM
     from the chunk.
  3. Keep the case only if the span really occurs in the chunk and the question doesn't leak it. The expected
     facts are the span's numbers/durations, or its rarest content words, so the scorer stays deterministic.

Usage:
  backend\\.venv\\Scripts\\python scripts\\make_heldout_set.py              # -> evals/heldout_set.json
  backend\\.venv\\Scripts\\python scripts\\run_evals.py --set heldout       # evaluate once; don't tune on it
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

import psycopg2
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_evals import load_env  # noqa: E402

GEN_SYSTEM = (
    "You write exam-style questions that a university student might ask about a policy.\n"
    "Given ONE policy excerpt, write a question whose answer is a specific fact stated in the excerpt "
    "(a number, duration, amount, deadline, authority, or a clear rule).\n"
    "Rules: the question must make sense on its own (name the topic, e.g. 'hostel', 'library', 'PhD'), "
    "must not copy the answer, and must not mention 'excerpt' or 'document'.\n"
    "answer_span = the SHORTEST phrase (3-15 words) copied EXACTLY, character for character, from the excerpt that answers it.\n"
    'Reply with JSON only: {"question": "...", "answer_span": "..."}'
)
STOP = set('''a an the of to in on for and or is are was be by with as at from that this it its can do does i my me you
your we what which who how when where why will would should could may must not no if than then there their they them shall
any all per such other under within after before upon also only each every into been being has have had'''.split())
NUM_RE = re.compile(r'(?:rs\.?\s*|inr\s*)?\d[\d,]*(?:\.\d+)?\s*(?:%|percent|days?|weeks?|months?|years?|hours?|working days?)?', re.I)


def norm(s):
    return re.sub(r'\s+', ' ', (s or '').replace('\u2019', "'")).strip().lower()


WORD_NUM = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8',
            'nine': '9', 'ten': '10', 'twelve': '12', 'fifteen': '15', 'twenty': '20', 'thirty': '30', 'sixty': '60',
            'seventy': '70', 'hundred': '100'}


def facts_from_span(span, question):
    """Deterministic expected facts derived from the verbatim answer span (most specific signal first)."""
    s = span.lower()
    times = list(dict.fromkeys(re.findall(r'\b\d{1,2}[:.]\d{2}\b', s)))
    if times:
        out = []
        for t in times[:2]:
            alts = {t, t.replace('.', ':')}
            h, mnt = re.split(r'[:.]', t)
            if mnt == '00':  # "10:00 PM" is also written "10 PM"
                alts |= {f'{int(h)} pm', f'{int(h)} am', f'{int(h)}pm', f'{int(h)}am'}
            out.append(sorted(alts))
        return out
    nums = []
    for m in NUM_RE.finditer(span):
        tok = m.group(0).strip()
        digits = re.sub(r'[^\d.]', '', tok).strip('.')
        has_unit = bool(re.search(r'[a-z%]', tok.lower()))
        if digits and (len(digits.replace('.', '')) >= 2 or '.' in digits or has_unit):
            nums.append(sorted({tok.lower(), digits}, key=len, reverse=True))
    if nums:
        return nums[:2]
    words = [w for w in re.findall(r'[a-z]+', s) if w in WORD_NUM]
    if words:
        return [[w, WORD_NUM[w]] for w in list(dict.fromkeys(words))[:2]]
    if re.search(r'\d', s) and len(span.split()) <= 4:
        return [sorted({s.strip(), re.sub(r'\s*to\s*', '-', s).strip()})]
    q = set(re.findall(r'[a-z]+', question.lower()))
    words = [w for w in re.findall(r'[a-z]+', s) if w not in STOP and len(w) >= 5 and w not in q]
    words = sorted(dict.fromkeys(words), key=len, reverse=True)[:2]
    return [[w[:max(5, len(w) - 2)]] for w in words]   # stem-ish prefix tolerates inflection


def main():
    env = load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='qwen2.5:3b')
    ap.add_argument('--per-policy', type=int, default=2)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--out', default=str(ROOT / 'evals' / 'heldout_set.json'))
    args = ap.parse_args()
    ollama = env.get('OLLAMA_HOST', 'http://127.0.0.1:11434').replace('://localhost', '://127.0.0.1').rstrip('/')

    conn = psycopg2.connect(host=env.get('DB_HOST', '127.0.0.1').replace('localhost', '127.0.0.1'), port=env.get('DB_PORT', '5432'),
                            dbname=env.get('DB_NAME'), user=env.get('DB_USER'), password=env.get('DB_PASSWORD'))
    cur = conn.cursor()
    cur.execute('''SELECT p.name, e.chunk_index, e.text FROM embeddings e JOIN policies p ON p.id = e.policy_id
                   WHERE COALESCE(p.audience, 'public') = 'public' ORDER BY p.name, e.chunk_index''')
    by_pdf = {}
    for name, idx, text in cur.fetchall():
        if len(text.split()) >= 60:
            by_pdf.setdefault(name, []).append((idx, text))

    rng = random.Random(args.seed)
    cases, rejected = [], 0
    for pdf, chunks in sorted(by_pdf.items()):
        picks = rng.sample(chunks, min(args.per_policy + 2, len(chunks)))   # spare picks in case some are rejected
        kept = 0
        for idx, text in picks:
            if kept >= args.per_policy:
                break
            try:
                r = requests.post(f'{ollama}/api/chat', timeout=300, json={
                    'model': args.model, 'stream': False, 'format': 'json', 'keep_alive': '30m',
                    'options': {'temperature': 0.3, 'num_predict': 160, 'seed': args.seed},
                    'messages': [{'role': 'system', 'content': GEN_SYSTEM},
                                 {'role': 'user', 'content': f'Policy: {pdf}\nExcerpt:\n{text[:2500]}'}]})
                out = json.loads(r.json()['message']['content'])
                q, span = str(out.get('question', '')).strip(), str(out.get('answer_span', '')).strip().strip('."')
            except Exception:
                rejected += 1
                continue
            ok = (q.endswith('?') and 15 <= len(q) <= 200 and 2 <= len(span.split()) <= 18
                  and norm(span) in norm(text) and norm(span) not in norm(q))
            facts = facts_from_span(span, q) if ok else []
            if not ok or not facts:
                rejected += 1
                continue
            kept += 1
            cases.append({'id': f'ho-{len(cases) + 1:02d}', 'category': 'heldout', 'question': q, 'expected_pdf': pdf,
                          'must_include': facts, 'answer_span': span, 'source_chunk': idx})
            print(f'+ {pdf[:34]:34s} {q[:80]}  ->  {span[:50]}')
    out_cases = []
    for c in cases:
        # the same sentence often appears in several circulars: every public document containing the span verbatim
        # is a correct source
        holders = sorted({pdf for pdf, chs in by_pdf.items() if any(norm(c['answer_span']) in norm(t) for _, t in chs)})
        # only distinctive spans (4+ words): "six months" occurs in many unrelated policies
        c['expected_pdf'] = holders if len(holders) > 1 and len(c['answer_span'].split()) >= 4 else c['expected_pdf']
        out_cases.append(c)
    cases = out_cases
    Path(args.out).write_text(json.dumps({'description': 'Held-out set: generated by ' + args.model +
                                          ' with verbatim answer spans; never used for tuning.',
                                          'seed': args.seed, 'cases': cases}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'\n{len(cases)} cases written to {args.out} ({rejected} candidates rejected by the verbatim/leak checks)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

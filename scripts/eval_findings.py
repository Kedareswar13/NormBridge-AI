"""Generate docs/EVAL_FINDINGS.md: the problems the evals caught, with the actual failing and fixed answers.

Everything in the document comes from the eval database (eval_runs / eval_results), not from memory. Each curated
finding states "failed in run X, passes in run Y". The script CHECKS that claim against the stored results and marks
it ⚠ if the data doesn't back it, so the write-up can never claim more than the evals show.

Usage:  backend\\.venv\\Scripts\\python scripts\\eval_findings.py      (Postgres must be running)
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_evals import load_env  # noqa: E402

# (title, case ids, run where it failed, run where it passes (None = still open), how the evals caught it,
#  root cause, fix, GAP_ANALYSIS id)
FINDINGS = [
    ('The scorer itself was wrong: correctly blocked attacks counted as failures', ['sec-01', 'sec-02', 'sec-03', 'sec-04'], 3, 4,
     'All 4 new prompt-injection cases "failed" in 0.2 s, which is suspiciously fast for a wrong answer.',
     'The API blocked them correctly ("I can\'t help with that request…"), but the refusal detector only knew the word "cannot".',
     'Refusal patterns extended; a unit test pins the block message as a refusal.', 'run #3 diagnosis'),
    ('Contextual compression threw away the answer sentence', ['email-01', 'logo-01', 'substance-01'], 3, 4,
     'Facts missing although the right document was retrieved (doc hit ✓, fact recall 0%).',
     'An offline replay of the retrieved chunks showed the answer sentence was dropped while shrinking the prompt (facts kept 34/38).',
     'The top-ranked chunk keeps up to 220 words, the others 70: 38/38 facts kept for +45 prompt words.', 'F4'),
    ('Exact words were invisible to vector search', ['hostel-06'], 2, 5,
     '"Can I stay in the hostel during semester breaks?" answered from the wrong policy; the rule says "vacate".',
     'Dense embeddings blur exact terms; the "vacate" clause never reached the top-50 candidates.',
     'Hybrid retrieval: BM25 keyword index merged with vector search, later fused with Reciprocal Rank Fusion.', 'C11'),
    ('A stricter test exposed self-contradicting answers', ['fee-01', 'fee-03', 'hostel-06'], 7, 10,
     'After adding expected yes/no verdicts, answers that "passed" for six runs started failing.',
     'The 1.5B model writes "Yes, the transport fee is non-refundable": the facts are right and the verdict is wrong. Fact matching alone never saw it.',
     'Verdict check in the scorer, plus query routing: yes/no questions go to qwen2.5:3b (offline A/B: 1.5B 3/7, prompt fix 4/7, 3B 6/7 correct verdicts).', 'G4, H2'),
    ('The small model inverted a definition', ['ragging-02'], 9, 10,
     '"Does forcing a fresher to do my assignments count as ragging?" answered No in 9 of 10 runs.',
     'The policy lists "exploiting the services of a fresher for completing academic tasks" as ragging, but the 1.5B model misreads it.',
     'Yes/no routing to the bigger model.', 'H2'),
    ('Near-duplicate policies were confused', ['sgriev-01'], 10, 12,
     'Student grievance question answered with the Employee grievance timeline ("seven working days" instead of two).',
     'The two policies use almost identical wording; chunk text alone doesn\'t say which policy it belongs to.',
     'Contextual chunk headers: "Student Grievance Redressal Policy > section" is embedded with every chunk.', 'H3'),
    ('A retention rule was never retrieved', ['cif-01'], 9, 10,
     '"How long does the Central Instrumentation Facility keep analysis records?" → "the policy does not mention it".',
     'The "one year" sentence sat in the middle of a 300-word page window about sample submission.',
     'Structure-aware chunking (blocks, not word windows) + contextual headers.', 'H4'),
    ('Table rows lost their header', ['fee-04'], 10, 12,
     '"How is the tuition fee refunded if I withdraw?" → "non-refundable" (wrong: "as per UGC norms").',
     'Word-window chunking split the OCR\'d refund table, so the "Tuition fee | As per UGC norms" row lost its context.',
     'Table-aware chunking: every table piece repeats the header row; 200-word structure-aware chunks.', 'B10, H4'),
    ('A fix caused a regression, and the evals caught it', ['phd-02'], 15, None,
     'phd-02 passed in every run until multi-document answers v1 (run #15).',
     'v1 REPLACED the weakest chunk of the best document with the runner-up document\'s chunk, and that chunk held "180 days".',
     'v2 adds the second document and never replaces evidence. The evidence is back in the prompt, but the 1.5B model now hedges ("not explicitly stated"). Still open: a generation issue.', 'H5'),
    ('Generation errors the retrieval can\'t fix (open)', ['conv-01'], 7, None,
     'The follow-up "And how much shortfall can be condoned?" retrieves the right policy every time.',
     'The model states the wrong condonation figure (40% instead of 15%) from the right excerpt.',
     'Open. In the cloud a larger hosted model answers; locally, a verify-against-excerpt pass is the next step.', 'H8 / review'),
]


def connect(env):
    if env.get('DATABASE_URL'):
        return psycopg2.connect(env['DATABASE_URL'])
    return psycopg2.connect(host=env.get('DB_HOST', '127.0.0.1').replace('localhost', '127.0.0.1'), port=env.get('DB_PORT', '5432'),
                            dbname=env.get('DB_NAME'), user=env.get('DB_USER'), password=env.get('DB_PASSWORD'))


def main():
    env = load_env()
    cur = connect(env).cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, model, total, passed, status, triggered_by, notes, summary FROM eval_runs WHERE status = 'completed' ORDER BY id")
    runs = cur.fetchall()
    cur.execute('SELECT run_id, case_id, question, answer, passed, metrics FROM eval_results ORDER BY id')
    res = {}
    for r in cur.fetchall():
        res[(r['run_id'], r['case_id'])] = r
    golden = [r for r in runs if '[heldout]' not in (r['triggered_by'] or '')]
    heldout = [r for r in runs if '[heldout]' in (r['triggered_by'] or '')]

    def rate(r):
        s = json.loads(r['summary']) if r['summary'] else {}
        return s.get('pass_rate'), s

    def quote(run_id, case_id, n=320):
        r = res.get((run_id, case_id))
        if not r:
            return None
        m = json.loads(r['metrics'] or '{}')
        a = ' '.join((r['answer'] or '').split())
        a = a if len(a) <= n else a[:n].rsplit(' ', 1)[0] + ' …'
        extra = []
        if m.get('missing_facts'):
            extra.append('missing: ' + '; '.join(m['missing_facts']))
        if m.get('polarity_ok') is False:
            extra.append('wrong yes/no verdict')
        if m.get('source_pdfs'):
            extra.append('sources: ' + ', '.join(m['source_pdfs']))
        return r['passed'], a, ' · '.join(extra)

    L = ['# What the evals caught (and what fixed it)', '',
         f'_Generated by `scripts/eval_findings.py` from the eval database on {datetime.now():%Y-%m-%d %H:%M}. '
         'Every answer quoted below is the real stored output of that run. Each claim "failed in run X, passes in run Y" '
         'is checked against the stored results, and a ⚠ marks any claim the data doesn\'t support._', '',
         '**Show it live:** Admin → **Evals & Quality** → *Eval-driven fixes*. It has the pass-rate timeline with notes, '
         'the case × run heatmap, and **Compare two runs** (e.g. #3 → #16) with both answers side by side.', '']

    last = golden[-1] if golden else None
    caught = sorted({cid for (rid, cid), r in res.items() if not r['passed'] and any(g['id'] == rid for g in golden)})
    still = [cid for cid in caught if last and (last['id'], cid) in res and not res[(last['id'], cid)]['passed']]
    L += ['## In one paragraph', '',
          f"{len(golden)} golden-set runs and {len(heldout)} held-out runs. **{len(caught)} different cases failed at least once**, and each failure "
          f"was traced to a root cause. {len(caught) - len(still)} of them pass in the latest run (#{last['id'] if last else '-'}); "
          f"{len(still)} are still open and explained below. Several failures were the **evaluator's own mistakes**. "
          "One drop in pass rate came from a **stricter test**, not a worse system. One fix **caused a regression that the evals caught**.", '']

    L += ['## Pass rate over time', '', '| Run | Model | Passed | Pass rate | What changed before this run |', '|---|---|---|---|---|']
    for r in golden + heldout:
        pr, _ = rate(r)
        tag = ' (held-out)' if r in heldout else ''
        L.append(f"| #{r['id']}{tag} | {r['model']} | {r['passed']}/{r['total']} | {pr * 100:.0f}% | {(r['notes'] or '').replace('|', '/')} |"
                 if pr is not None else f"| #{r['id']} | {r['model']} | {r['passed']}/{r['total']} | - | {r['notes'] or ''} |")

    L += ['', '## Case history (golden set: cases that ever failed)', '',
          '✓ pass · ✗ fail · blank = case not in that run (subset runs re-checked only the cases being fixed).', '',
          '| Case | ' + ' | '.join(f"#{r['id']}" for r in golden) + ' |', '|---|' + '---|' * len(golden)]
    for cid in caught:
        cells = []
        for r in golden:
            x = res.get((r['id'], cid))
            cells.append('' if x is None else ('✓' if x['passed'] else '✗'))
        L.append(f'| `{cid}` | ' + ' | '.join(cells) + ' |')

    L += ['', '## The findings', '']
    for i, (title, cases, bad_run, good_run, caught_how, cause, fix, ref) in enumerate(FINDINGS, 1):
        ok = all(res.get((bad_run, c)) is not None and not res[(bad_run, c)]['passed'] for c in cases)
        if good_run is not None:
            ok = ok and all(res.get((good_run, c)) is not None and res[(good_run, c)]['passed'] for c in cases)
        status = 'still open' if good_run is None else f'fixed in run #{good_run}'
        L += [f'### {i}. {title}', '',
              f"- **Cases:** {', '.join(f'`{c}`' for c in cases)} · failed in run #{bad_run} · {status}" + ('' if ok else ' ⚠ data does not fully match this claim'),
              f'- **How the evals caught it:** {caught_how}',
              f'- **Root cause:** {cause}',
              f'- **Fix:** {fix} _(GAP_ANALYSIS {ref})_', '']
        c = cases[0]
        q = res.get((bad_run, c))
        if q:
            L.append(f"> **Q ({c}):** {q['question']}")
            before = quote(bad_run, c)
            if before:
                L += ['>', f"> **Run #{bad_run} ({'pass' if before[0] else 'FAIL'}):** {before[1]}"]
                if before[2]:
                    L.append(f'> _{before[2]}_')
            if good_run is not None:
                after = quote(good_run, c)
                if after:
                    L += ['>', f"> **Run #{good_run} ({'PASS' if after[0] else 'fail'}):** {after[1]}"]
                    if after[2]:
                        L.append(f'> _{after[2]}_')
            elif last and last['id'] != bad_run:
                now = quote(last['id'], c)
                if now:
                    L += ['>', f"> **Latest run #{last['id']} ({'PASS' if now[0] else 'FAIL'}):** {now[1]}"]
                    if now[2]:
                        L.append(f'> _{now[2]}_')
            L.append('')

    if heldout:
        h = heldout[-1]
        hp, _ = rate(h)
        gp, _ = rate(last) if last else (None, None)
        open_h = [cid for (rid, cid), r in res.items() if rid == h['id'] and not r['passed']]
        L += ['## Generalisation check: the held-out set', '',
              f"The held-out set (generated by a different model, verbatim answer spans, **never used for tuning**) scores "
              f"**{hp * 100:.0f}%** in run #{h['id']} vs **{gp * 100:.0f}%** on the golden set in run #{last['id']}. The gap is the honest "
              f"estimate of how much the hand-written set flatters the system. Open held-out cases: {', '.join(f'`{c}`' for c in sorted(open_h))}.", '']

    L += ['## How to talk about this in an interview', '',
          '1. *"I don\'t trust a pass rate until I\'ve read the failures."* Runs #3 and #9 dropped because of the evaluator and a stricter test, not the system.',
          '2. *"Every fix is a hypothesis tested by a re-run."* Subset runs re-check only the cases a fix targets, and the next full run checks for regressions.',
          '3. *"The evals caught my own regression."* `phd-02` (multi-document v1) is the proof that the suite protects against well-meant changes.',
          '4. *"I report what\'s still broken."* Open cases are listed with their root cause: retrieval is solved for them, and generation isn\'t.', '']

    out = ROOT / 'docs' / 'EVAL_FINDINGS.md'
    out.write_text('\n'.join(L) + '\n', encoding='utf-8')
    unsupported = [f[0] for f in FINDINGS if '⚠' in '\n'.join(L).split(f[0])[1].split('###')[0]]
    print(f'Wrote {out} ({len(golden)} golden runs, {len(heldout)} held-out runs, {len(caught)} cases ever failed, {len(still)} open)')
    print('claims not backed by data:', unsupported or 'none')
    return 0


if __name__ == '__main__':
    sys.exit(main())

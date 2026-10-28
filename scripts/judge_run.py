"""LLM-as-judge for a finished golden-set run, plus agreement with the deterministic scorer.

The deterministic evals are free and reproducible but lexical: they can't judge paraphrases or catch a
"right words, wrong verdict" answer unless a case encodes it. This script asks a *different, larger* local model
to grade each answer against the case's reference facts, then measures how often the judge agrees with the
deterministic pass/fail (raw agreement and Cohen's kappa). Disagreements are the interesting part: each one is
either an evaluator bug or a judge mistake, and both are worth reading.

Usage:
  backend\\.venv\\Scripts\\python scripts\\judge_run.py                 # latest run, judge = qwen2.5:3b
  backend\\.venv\\Scripts\\python scripts\\judge_run.py --run 9 --judge qwen2.5:3b --limit 20
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_evals import load_env  # noqa: E402

JUDGE_SYSTEM = (
    "You are a strict grader for a university-policy assistant. Grade the ANSWER to the QUESTION using only the "
    "REFERENCE (key facts a correct answer must convey, and the expected verdict for yes/no questions).\n"
    "correct = conveys the reference facts and the right verdict, with nothing contradicting them.\n"
    "partial = some reference facts are missing or vague, but nothing is wrong.\n"
    "incorrect = a wrong fact or wrong verdict, contradicts itself, or answers a different question.\n"
    "For must-refuse cases, correct = the assistant declines or says the policies don't cover it.\n"
    'Reply with JSON only: {"grade": "correct|partial|incorrect", "reason": "<one sentence>"}'
)


def reference_for(case):
    if case.get('should_refuse'):
        return 'This question is out of scope or must be refused; a correct answer declines.'
    if case.get('forbidden_pdf') and not case.get('expected_pdf'):
        return 'Access-control case: any answer that does not reveal the restricted document is acceptable.'
    facts = ['one of: ' + ' / '.join(g) if isinstance(g, list) else g for g in case.get('must_include') or []]
    ref = 'Key facts: ' + ('; '.join(facts) if facts else '(none listed; judge plausibility and consistency)')
    if case.get('expected_polarity'):
        ref += f". Expected verdict: {case['expected_polarity'].upper()}"
    return ref


def judge(ollama, model, question, answer, reference):
    user = f"QUESTION: {question}\nREFERENCE: {reference}\nANSWER: {answer}"
    r = requests.post(f'{ollama}/api/chat', timeout=300, json={
        'model': model, 'stream': False, 'format': 'json', 'keep_alive': '30m',
        'options': {'temperature': 0, 'num_predict': 120},
        'messages': [{'role': 'system', 'content': JUDGE_SYSTEM}, {'role': 'user', 'content': user}]})
    r.raise_for_status()
    try:
        out = json.loads(r.json()['message']['content'])
        grade = str(out.get('grade', '')).lower().strip()
        return (grade if grade in ('correct', 'partial', 'incorrect') else 'incorrect'), str(out.get('reason', ''))[:240]
    except (ValueError, KeyError):
        return 'incorrect', 'unparseable judge output'


def cohen_kappa(a, b):
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


def main():
    env = load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument('--api', default=f"http://127.0.0.1:{env.get('PORT', '5001')}")
    ap.add_argument('--run', type=int, default=None, help='run id (default: latest completed)')
    ap.add_argument('--judge', default='qwen2.5:3b', help='judge model; use a different/bigger model than the answer model')
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()
    auth = (env.get('ADMIN_USERNAME', 'admin'), env.get('ADMIN_PASSWORD', ''))
    ollama = env.get('OLLAMA_HOST', 'http://127.0.0.1:11434').replace('://localhost', '://127.0.0.1').rstrip('/')

    run_id = args.run
    if run_id is None:
        runs = requests.get(f'{args.api}/admin/evals/runs', auth=auth, timeout=30).json()
        runs = runs.get('runs', runs) if isinstance(runs, dict) else runs
        run_id = next(r['id'] for r in runs if r.get('status') == 'completed')
    run = requests.get(f'{args.api}/admin/evals/runs/{run_id}', auth=auth, timeout=30).json()
    golden = json.loads((ROOT / 'evals' / 'golden_set.json').read_text(encoding='utf-8'))
    cases = {c['id']: c for c in (golden['cases'] if isinstance(golden, dict) else golden)}
    results = run['results'][: args.limit] if args.limit else run['results']
    print(f'Judging run #{run_id} ({len(results)} answers, answer model {run.get("model")}) with {args.judge}')

    rows, det, jud = [], [], []
    t0 = time.time()
    for i, res in enumerate(results, 1):
        case = cases.get(res['case_id'], {'question': res['question']})
        grade, reason = judge(ollama, args.judge, res['question'], res['answer'], reference_for(case))
        rows.append((res, grade, reason))
        det.append(1 if res['passed'] else 0)
        jud.append(1 if grade == 'correct' else 0)
        flag = '' if bool(res['passed']) == (grade == 'correct') else '  <- disagreement'
        print(f"  [{i}/{len(results)}] {res['case_id']}: scorer={'PASS' if res['passed'] else 'FAIL'} judge={grade}{flag}")

    n = len(rows)
    agree = sum(x == y for x, y in zip(det, jud))
    kappa = cohen_kappa(det, jud)
    counts = {g: sum(1 for _, gr, _ in rows if gr == g) for g in ('correct', 'partial', 'incorrect')}
    lines = [f'# LLM-as-judge report: run #{run_id}', '',
             f'- Answer model: `{run.get("model")}` | Judge model: `{args.judge}` | Answers judged: {n} | Time: {time.time() - t0:.0f}s',
             f'- Judge grades: correct {counts["correct"]}, partial {counts["partial"]}, incorrect {counts["incorrect"]} '
             f'(judge accuracy {counts["correct"] / n:.0%})' if n else '- no answers',
             f'- Deterministic pass rate: {sum(det) / n:.0%}' if n else '',
             f'- **Agreement with the deterministic scorer: {agree}/{n} = {agree / n:.0%}, Cohen\'s kappa = {kappa}**' if n else '',
             '', 'Kappa corrects raw agreement for chance: < 0.4 weak, 0.4-0.6 moderate, 0.6-0.8 substantial, > 0.8 near-perfect.',
             '', '## Disagreements (read these: each is an evaluator gap or a judge mistake)', '',
             '| Case | Scorer | Judge | Judge reason | Answer |', '|---|---|---|---|---|']
    for res, grade, reason in rows:
        if bool(res['passed']) != (grade == 'correct'):
            ans = (res['answer'] or '').replace('\n', ' ').replace('|', '/')[:160]
            lines.append(f"| {res['case_id']} | {'pass' if res['passed'] else 'fail'} | {grade} | {reason.replace('|', '/')} | {ans} |")
    out = ROOT / 'evals' / 'reports' / f'judge-run-{run_id}-{datetime.now():%Y%m%d-%H%M}.md'
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"\nAgreement {agree}/{n} ({agree / max(n, 1):.0%}), kappa={kappa}, judge accuracy {counts['correct'] / max(n, 1):.0%}")
    print(f'Report: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

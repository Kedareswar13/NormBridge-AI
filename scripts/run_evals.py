"""Run the golden-set evals against a running NormBridge AI API and write a Markdown report.

Usage:
  backend\\.venv\\Scripts\\python scripts\\run_evals.py                 # all cases
  backend\\.venv\\Scripts\\python scripts\\run_evals.py --cases att-01 fee-01
  backend\\.venv\\Scripts\\python scripts\\run_evals.py --min-pass-rate 0.8   # exit 1 below threshold (CI gate)
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    """Settings from the repo .env, overridden by real environment variables (so the same script can evaluate a
    deployed API: set ADMIN_USERNAME / ADMIN_PASSWORD of the deployment and pass --api https://...)."""
    env = {}
    p = ROOT / '.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    import os
    for key in ('ADMIN_USERNAME', 'ADMIN_PASSWORD', 'PORT', 'OLLAMA_HOST'):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def main():
    env = load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument('--api', default=f"http://127.0.0.1:{env.get('PORT', '5001')}")
    ap.add_argument('--model', default=None, help='answer model (default: the server default)')
    ap.add_argument('--cases', nargs='*')
    ap.add_argument('--set', dest='dataset', choices=['golden', 'heldout'], default='golden',
                    help='golden = hand-written regression set; heldout = generated set never used for tuning')
    ap.add_argument('--min-pass-rate', type=float, default=None)
    ap.add_argument('--note', default=None, help='what changed before this run (shown on the Admin eval timeline)')
    args = ap.parse_args()
    auth = (env.get('ADMIN_USERNAME', 'admin'), env.get('ADMIN_PASSWORD', ''))

    r = requests.post(f'{args.api}/admin/evals/runs', auth=auth, json={'model': args.model, 'case_ids': args.cases, 'dataset': args.dataset,
                                                                       'notes': args.note}, timeout=30)
    if r.status_code not in (200, 202):
        print(f'Could not start run: {r.status_code} {r.text}')
        return 2
    run_id = r.json()['run_id']
    print(f'Started eval run #{run_id}')

    last = -1
    while True:
        run = requests.get(f'{args.api}/admin/evals/runs/{run_id}', auth=auth, timeout=30).json()
        if run['completed'] != last:
            last = run['completed']
            if run.get('results'):
                res = run['results'][-1]
                print(f"  [{run['completed']}/{run['total']}] {'PASS' if res['passed'] else 'FAIL'} {res['case_id']} ({res['latency']}s)")
        if run['status'] != 'running':
            break
        time.sleep(3)

    md = requests.get(f'{args.api}/admin/evals/runs/{run_id}', params={'format': 'md'}, auth=auth, timeout=30).text
    out_dir = ROOT / 'evals' / 'reports'
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{'heldout-' if args.dataset == 'heldout' else ''}run-{run_id}-{datetime.now():%Y%m%d-%H%M}.md"
    out.write_text(md, encoding='utf-8')

    s = run.get('summary') or {}
    print(f"\nRun #{run_id} {run['status']}: {run['passed']}/{run['total']} passed | pass_rate={s.get('pass_rate')} doc_hit={s.get('doc_hit_rate')} "
          f"facts={s.get('fact_recall')} grounded={s.get('groundedness')} numbers={s.get('numeric_faithfulness')}")
    print(f'Report: {out}')
    if args.min_pass_rate is not None and (s.get('pass_rate') or 0) < args.min_pass_rate:
        print(f'FAILED quality gate: pass_rate < {args.min_pass_rate}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

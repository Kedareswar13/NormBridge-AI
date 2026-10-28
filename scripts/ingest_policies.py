"""Upload every PDF in data/policies/ to the running NormBridge AI API (uses manifest.json for categories).

Already-ingested files are skipped unless --force is given.
Usage:  backend\\.venv\\Scripts\\python scripts\\ingest_policies.py [--api http://127.0.0.1:5001] [--force]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = ROOT / 'data' / 'policies'


def load_env():
    """Settings from the repo .env file, overridden by real environment variables (e.g. Space secrets)."""
    env = {}
    p = ROOT / '.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    for key in ('ADMIN_USERNAME', 'ADMIN_PASSWORD', 'PORT'):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def needs_ocr(pdf):
    """True if the PDF has image-only pages and no cached extraction yet."""
    import hashlib
    import PyPDF2
    if (ROOT / 'data' / 'ocr_cache' / (hashlib.sha256(pdf.read_bytes()).hexdigest() + '.json')).exists():
        return False
    try:
        return any(len((p.extract_text() or '').split()) < 20 for p in PyPDF2.PdfReader(str(pdf)).pages)
    except Exception:
        return True


def main():
    env = load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument('--api', default=f"http://127.0.0.1:{env.get('PORT', '5001')}")
    ap.add_argument('--force', action='store_true', help='re-upload policies that already exist')
    ap.add_argument('--skip-uncached-scans', action='store_true',
                    help='skip scanned PDFs whose OCR result is not in data/ocr_cache yet (avoids slow OCR inside the API)')
    args = ap.parse_args()

    auth = (env.get('ADMIN_USERNAME', 'admin'), env.get('ADMIN_PASSWORD', ''))
    manifest = json.loads((POLICY_DIR / 'manifest.json').read_text(encoding='utf-8')) if (POLICY_DIR / 'manifest.json').exists() else {}

    try:
        data = requests.get(f'{args.api}/policies', auth=auth, timeout=30).json()  # admin view: includes staff-only
        rows = data if isinstance(data, list) else data.get('policies', [])
        existing = {p.get('filename') or p.get('name') for p in rows}
    except Exception:
        existing = set()

    failures = 0
    for pdf in sorted(POLICY_DIR.glob('*.pdf')):
        if pdf.name in existing and not args.force:
            print(f'= {pdf.name} already ingested')
            continue
        if args.skip_uncached_scans and needs_ocr(pdf):
            print(f'~ {pdf.name} skipped (scanned, OCR not cached yet - run scripts/ocr_prefetch.py)')
            continue
        category = manifest.get(pdf.name, {}).get('category', 'General')
        audience = manifest.get(pdf.name, {}).get('audience', 'public')
        t0 = time.time()
        with pdf.open('rb') as fh:
            r = requests.post(f'{args.api}/upload', auth=auth, files={'file': (pdf.name, fh, 'application/pdf')},
                              data={'category_name': category, 'version_name': 'v1.0', 'audience': audience}, timeout=3600)
        try:
            res = r.json()['results'][0]
        except Exception:
            res = {'status': 'error', 'error': r.text[:200]}
        ok = res.get('status') == 'success'
        failures += 0 if ok else 1
        detail = f"{res.get('pages')} pages, {res.get('chunks')} chunks, {res.get('extraction_method')}" if ok else res.get('error')
        print(f"{'+' if ok else '!'} {pdf.name} [{category} | {audience}] {detail} ({time.time() - t0:.0f}s)")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

"""Download the official SRM University-AP policy PDFs into data/policies/.

Re-runnable: files that already exist are skipped.
The list below is the curated, student/staff-facing policy set published on
https://srmap.edu.in (mandatory-disclosure page + the university media library).

Usage:  python scripts/fetch_policies.py            (from the repo root)
        python scripts/fetch_policies.py --check    (only report which files are missing)
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / 'data' / 'policies'
MANIFEST = DEST / 'manifest.json'
BASE = 'https://www.srmap.edu.in/wp-content/uploads/'

# (local file name, path under /wp-content/uploads/, category shown in the app)
# NOTE: the chat pipeline boosts the most recently uploaded document *within a category*, so a category
# must only group versions of the same policy (e.g. the two fee-refund circulars). Keep the others distinct.
POLICIES = [
    ('Anti-Ragging-Policy.pdf', '2025/05/Anti-Ragging-Policy.pdf', 'Anti-Ragging'),
    ('Student-Code-of-Conduct.pdf', '2026/09/Student-Code-of-Conduct-of-SRM-UNIVERSITY-AP.pdf', 'Code of Conduct'),
    ('Students-Attendance-Policy.pdf', '2026/07/Students-Attendance-Policy.pdf', 'Attendance'),
    ('Plagiarism-Policy.pdf', '2025/08/Plagiarism-Policy-of-SRM-University-AP.pdf', 'Plagiarism'),
    ('Library-Policy.pdf', '2026/09/19.-Library-Policy-of-SRM-University-AP.pdf', 'Library'),
    ('University-Hostel-Policy.pdf', '2026/09/University-Hostel-Policy-31.08.2026.pdf', 'Hostel Policy'),
    ('Hostel-Rules-and-Regulations.pdf', '2026/07/1st-June-Revised-Hostel-Rules-and-Regulations-1.pdf', 'Hostel Rules'),
    ('Hostel-Refund-Policy.pdf', '2025/08/Hostel-Refund-Policy.pdf', 'Fees & Refunds'),
    ('Fee-Refund-Policy.pdf', '2025/05/Refund-Policy-of-Fee.pdf', 'Fees & Refunds'),
    ('Transport-Refund-Policy.pdf', '2025/06/Transport-Refund-Policy.pdf', 'Transport Refund'),
    ('Student-Grievance-Redressal-Policy.pdf', '2025/05/grievance-redressal-of-students-policy.pdf', 'Student Grievance'),
    ('Employee-Grievance-Redressal-Policy.pdf', '2025/05/grievance-redressal-of-employees-policy.pdf', 'Employee Grievance'),
    ('PhD-Regulations.pdf', '2025/05/PhD-Regulations-Policy.pdf', 'PhD Regulations'),
    ('PhD-Fellowship-Leave-Policy.pdf', '2025/05/PhD-Fellowship-Leave-Policy.pdf', 'PhD Leave'),
    ('Central-Instrumentation-Facility-Policy.pdf', '2025/06/Central-Instrumentation-Facility-CIF-Policy-of-SRM-University-ap.pdf', 'Research Facilities'),
]

# Who may retrieve each policy (security layer 2). Anything not listed is 'public'.
# Levels: public < student < staff < admin. Staff-only documents are never retrieved for student questions.
AUDIENCE = {
    'Employee-Grievance-Redressal-Policy.pdf': 'staff',
}

# SRM AP policies that ship with the repository (kept as-is).
BUNDLED = [
    ('Email-Policy.pdf', 'Email'),
    ('Sexual-Harassment-Policy.pdf', 'Sexual Harassment'),
    ('Substance-Abuse-Policy.pdf', 'Substance Abuse'),
    ('Use-of-Logo-and-Name-Policy.pdf', 'Branding'),
    ('Policy.pdf', 'Internship'),
]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url, target, attempts=4):
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (NormBridge AI policy fetcher)'})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if not data.startswith(b'%PDF'):
                raise ValueError('response is not a PDF')
            target.write_bytes(data)
            return
        except Exception as e:  # flaky networks: retry with backoff
            if i == attempts:
                raise
            print(f'   retry {i}/{attempts - 1} after error: {e}')
            time.sleep(3 * i)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)

    old = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {}
    manifest, missing = {}, []
    for name, path, category in POLICIES:
        target = DEST / name
        url = BASE + path
        if not target.exists():
            if args.check:
                missing.append(name)
                continue
            print(f'-> downloading {name}')
            try:
                download(url, target)
            except Exception as e:
                print(f'   FAILED: {e}')
                missing.append(name)
                continue
        manifest[name] = {'category': category, 'audience': AUDIENCE.get(name, 'public'), 'source_url': url, 'sha256': sha256(target), 'bytes': target.stat().st_size,
                          'fetched': old.get(name, {}).get('fetched') or time.strftime('%Y-%m-%d')}
    for name, category in BUNDLED:
        target = DEST / name
        if target.exists():
            manifest[name] = {'category': category, 'audience': AUDIENCE.get(name, 'public'), 'source_url': 'bundled with the repository', 'sha256': sha256(target), 'bytes': target.stat().st_size}
        else:
            missing.append(name)

    if not args.check:
        MANIFEST.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'\n{len(manifest)} policies in {DEST}' + (f' | missing: {missing}' if missing else ''))
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())

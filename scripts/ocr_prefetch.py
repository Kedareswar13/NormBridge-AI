"""Pre-compute text extraction (incl. OCR of scanned pages) for every PDF in data/policies/.

Results are cached in data/ocr_cache/<sha256>.json, so uploads become instant and the
expensive OCR never has to run again on another machine that gets a copy of this folder.

Usage:  backend\\.venv\\Scripts\\python scripts\\ocr_prefetch.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))

from services.ocr_service import extract_text_hybrid, _cache_path  # noqa: E402

pdfs = sorted((ROOT / 'data' / 'policies').glob('*.pdf'), key=lambda p: p.stat().st_size)
start = time.time()
for i, pdf in enumerate(pdfs, 1):
    data = pdf.read_bytes()
    if os.path.exists(_cache_path(data)):
        print(f'[{i}/{len(pdfs)}] {pdf.name}: cached', flush=True)
        continue
    t0 = time.time()
    pages = extract_text_hybrid(data)
    methods = sorted({p['method'] for p in pages})
    print(f'[{i}/{len(pdfs)}] {pdf.name}: {len(pages)} pages via {methods} in {time.time() - t0:.0f}s', flush=True)
print(f'done in {(time.time() - start) / 60:.1f} min')

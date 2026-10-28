import os
import re
import io
import json
import base64
import hashlib
import shutil
import tempfile
import time
import PyPDF2
import requests

from config import (
    OCR_ENABLE_GLM, OCR_ENABLE_TESSERACT, OCR_ENABLE_EASYOCR,
    TESSERACT_CMD, POPPLER_PATH, GLM_OCR_MODEL_PATH as _GLM_PATH_OVERRIDE,
    OLLAMA_HOST, OLLAMA_OCR_MODEL, OCR_CACHE_DIR, OCR_MIN_WORDS_PER_PAGE, OCR_DPI, OCR_PAGE_TIMEOUT,
)

try:
    from rapidocr_onnxruntime import RapidOCR
    RAPIDOCR_AVAILABLE = True
except ImportError:
    RAPIDOCR_AVAILABLE = False
_rapidocr_engine = None

try:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # older PyMuPDF releases
    from PIL import Image
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

try:
    from pdf2image import convert_from_path, convert_from_bytes
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    print("⚠️ pdf2image not installed. Install with: pip install pdf2image")

try:
    import pytesseract
    from PIL import Image, ImageEnhance, ImageFilter
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    # The Python wrapper is useless without the tesseract binary, so require both.
    TESSERACT_AVAILABLE = OCR_ENABLE_TESSERACT and bool(TESSERACT_CMD and os.path.exists(TESSERACT_CMD) or shutil.which('tesseract'))
except ImportError:
    TESSERACT_AVAILABLE = False
if not TESSERACT_AVAILABLE:
    print("⚠️ Tesseract OCR not available (optional - only needed for scanned PDFs)")

try:
    if not OCR_ENABLE_EASYOCR:
        raise ImportError
    import easyocr
    EASYOCR_AVAILABLE = True
    easyocr_reader = None
except ImportError:
    EASYOCR_AVAILABLE = False
    easyocr_reader = None
    print("⚠️ EasyOCR not installed. Optional: pip install easyocr")

try:
    if not OCR_ENABLE_GLM:
        raise ImportError
    from mlx_vlm import load, generate
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config
    MLX_AVAILABLE = True
    print("✅ MLX-VLM available - GLM-OCR will run fast on Mac")
except ImportError:
    MLX_AVAILABLE = False
    print("ℹ️ GLM-OCR via MLX is Mac-only - will use GLM-OCR through Ollama (if pulled), then Tesseract/RapidOCR")

_glm_ocr_model = None
_glm_ocr_processor = None
_glm_ocr_config = None
GLM_OCR_MODEL_PATH = _GLM_PATH_OVERRIDE or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'models',
    'GLM-OCR-8bit'
)

def get_glm_ocr():
    global _glm_ocr_model, _glm_ocr_processor, _glm_ocr_config
    
    if _glm_ocr_model is None and MLX_AVAILABLE:
        try:
            print(f"🔄 Loading GLM-OCR model from {GLM_OCR_MODEL_PATH}...")
            _glm_ocr_model, _glm_ocr_processor = load(GLM_OCR_MODEL_PATH)
            _glm_ocr_config = load_config(GLM_OCR_MODEL_PATH)
            print("✅ GLM-OCR model loaded successfully!")
        except Exception as e:
            print(f"⚠️ Failed to load GLM-OCR model: {e}")
            _glm_ocr_model = False
            
    return _glm_ocr_model, _glm_ocr_processor, _glm_ocr_config

def clean_extracted_text(text):
    if not text:
        return ""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r' +', ' ', text)
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def convert_pdf_to_images(pdf_source, dpi=200):
    # PyMuPDF renders pages without the external poppler binary, so prefer it on every OS.
    if PYMUPDF_AVAILABLE:
        try:
            doc = fitz.open(stream=bytes(pdf_source), filetype='pdf') if isinstance(pdf_source, (bytes, bytearray)) else fitz.open(str(pdf_source))
            images = []
            for page in doc:
                pix = page.get_pixmap(dpi=dpi)
                images.append(Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB'))
            doc.close()
            print(f"  ✅ Rendered {len(images)} pages to images (PyMuPDF)")
            return images
        except Exception as e:
            print(f"  ⚠️ PyMuPDF rendering failed, trying pdf2image: {e}")

    if not PDF2IMAGE_AVAILABLE:
        print("⚠️ pdf2image not available")
        return []
    
    try:
        if isinstance(pdf_source, (bytes, bytearray)):
            images = convert_from_bytes(pdf_source, dpi=dpi, poppler_path=POPPLER_PATH)
        else:
            images = convert_from_path(str(pdf_source), dpi=dpi, poppler_path=POPPLER_PATH)
            
        print(f"  ✅ Converted {len(images)} pages to images")
        return images
    except Exception as e:
        print(f"  ⚠️ PDF to image conversion failed: {e}")
        return []

def extract_text_with_glmocr_from_image(image, task="Text Recognition:"):
    model, processor, config = get_glm_ocr()
    if model is None or processor is None:
        return ""
    
    try:
        if not isinstance(image, str):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                image.save(tmp.name)
                tmp_path = tmp.name
        else:
            tmp_path = image

        formatted_prompt = apply_chat_template(
            processor,
            config,
            task,
            num_images=1
        )
        
        result = generate(
            model,
            processor,
            formatted_prompt,
            image=[tmp_path],
            max_tokens=2048,
            temperature=0.0,
            verbose=False
        )
        
        if not isinstance(image, str) and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except:
                pass

        extracted_text = ""
        if hasattr(result, 'text'):
            extracted_text = result.text
        elif isinstance(result, dict) and 'text' in result:
            extracted_text = result['text']
        elif isinstance(result, str):
            extracted_text = result
        else:
            extracted_text = str(result)
        
        return extracted_text.strip()
    except Exception as e:
        print(f"    ⚠️ GLM-OCR error: {e}")
        return ""

def extract_text_with_glmocr(pdf_source):
    print(f"\n  🚀 GLM-OCR: Processing pages...")
    start_time = time.time()
    
    images = convert_pdf_to_images(pdf_source, dpi=200)
    if not images:
        return []
    
    text_by_page = []
    for page_num, image in enumerate(images):
        page_start = time.time()
        text = extract_text_with_glmocr_from_image(image)
        page_time = time.time() - page_start
        
        if text and len(text.strip()) > 10:
            text = clean_extracted_text(text)
            text_by_page.append({
                'page': page_num,
                'text': text,
                'method': 'glm-ocr'
            })
            print(f"  Page {page_num + 1}/{len(images)}: Extracted {len(text.split())} words in {page_time:.1f}s")
    
    total_time = time.time() - start_time
    print(f"  ✅ GLM-OCR completed {len(text_by_page)}/{len(images)} pages in {total_time:.1f}s")
    return text_by_page

OLLAMA_OCR_AVAILABLE = None  # resolved lazily: True/False once we've asked Ollama


def ollama_ocr_available():
    """True when the Ollama server is reachable and the GLM-OCR model is pulled."""
    global OLLAMA_OCR_AVAILABLE
    if OLLAMA_OCR_AVAILABLE is None:
        if not OCR_ENABLE_GLM:
            OLLAMA_OCR_AVAILABLE = False
        else:
            try:
                tags = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3).json().get('models', [])
                names = {m.get('name', '') for m in tags}
                OLLAMA_OCR_AVAILABLE = any(n == OLLAMA_OCR_MODEL or n.split(':')[0] == OLLAMA_OCR_MODEL.split(':')[0] for n in names)
            except Exception:
                return False  # don't cache: Ollama may simply not be started yet
    return OLLAMA_OCR_AVAILABLE


def extract_text_with_ollama_glmocr(image):
    """GLM-OCR served by Ollama - same model as the MLX path, but runs on Windows/Linux/Mac."""
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    payload = {
        'model': OLLAMA_OCR_MODEL,
        'prompt': 'Text Recognition:',
        'images': [base64.b64encode(buf.getvalue()).decode()],
        'stream': False,
        'options': {'temperature': 0, 'num_ctx': 16384, 'num_predict': 2048},  # default 4096 ctx truncates page images; cap runaway loops
    }
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/generate", json=payload, timeout=OCR_PAGE_TIMEOUT)
        r.raise_for_status()
        return (r.json().get('response') or '').strip()
    except Exception as e:
        print(f"    ⚠️ Ollama GLM-OCR error: {e}")
        return ""


def _ocr_image(image):
    """Run the best available OCR engine on one page image. Returns (text, method)."""
    if MLX_AVAILABLE and os.path.exists(GLM_OCR_MODEL_PATH):
        text = extract_text_with_glmocr_from_image(image)
        if text and len(text.split()) > 3:
            return text, 'glm-ocr'
    if ollama_ocr_available():
        text = extract_text_with_ollama_glmocr(image)
        if text and len(text.split()) > 3:
            return text, 'glm-ocr (ollama)'
    if TESSERACT_AVAILABLE:
        try:
            text = pytesseract.image_to_string(image, config='--psm 6').strip()
            if text:
                return text, 'tesseract'
        except Exception as e:
            print(f"    ⚠️ Tesseract error: {e}")
    if EASYOCR_AVAILABLE:
        global easyocr_reader
        try:
            import numpy as np
            if easyocr_reader is None:
                easyocr_reader = easyocr.Reader(['en'], gpu=False)
            text = '\n'.join(easyocr_reader.readtext(np.array(image), detail=0, paragraph=True))
            if text:
                return text, 'easyocr'
        except Exception as e:
            print(f"    ⚠️ EasyOCR error: {e}")
    if RAPIDOCR_AVAILABLE:
        global _rapidocr_engine
        try:
            import numpy as np
            if _rapidocr_engine is None:
                _rapidocr_engine = RapidOCR()
            result, _ = _rapidocr_engine(np.array(image))
            text = '\n'.join(r[1] for r in (result or []))
            if text:
                return text, 'rapidocr'
        except Exception as e:
            print(f"    ⚠️ RapidOCR error: {e}")
    return '', ''


def _cache_path(pdf_bytes):
    if not OCR_CACHE_DIR:
        return None
    return os.path.join(OCR_CACHE_DIR, hashlib.sha256(pdf_bytes).hexdigest() + '.json')


def extract_text_hybrid(pdf_source):
    """Extract text page by page.

    1. Use the PDF's own text layer (fast, exact) for every page that has one.
    2. OCR only the pages that are scanned images: GLM-OCR (MLX on Mac, or Ollama anywhere) -> Tesseract -> EasyOCR.
    3. Cache the result by file hash so a PDF is only ever OCR'd once (the cache travels with the project folder).
    Returns [{'page': int, 'text': str, 'method': str}, ...] for every caller.
    """
    pdf_bytes = bytes(pdf_source) if isinstance(pdf_source, (bytes, bytearray)) else open(pdf_source, 'rb').read()

    cache_file = _cache_path(pdf_bytes)
    if cache_file and os.path.exists(cache_file):
        with open(cache_file, encoding='utf-8') as fh:
            cached = json.load(fh)
        print(f"  ♻️ Using cached extraction ({len(cached)} pages) from {os.path.basename(cache_file)}")
        return cached

    start = time.time()
    pages_text = []
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            try:
                pages_text.append(page.extract_text() or '')
            except Exception:
                pages_text.append('')
    except Exception as e:
        print(f"  ⚠️ PyPDF2 could not read the PDF: {e}")

    needs_ocr = [i for i, t in enumerate(pages_text) if len(t.split()) < OCR_MIN_WORDS_PER_PAGE] if pages_text else None
    print(f"  📖 Text layer: {len(pages_text) - len(needs_ocr or [])}/{len(pages_text)} pages" + (f", {len(needs_ocr)} need OCR" if needs_ocr else ''))

    results = {}
    for i, t in enumerate(pages_text):
        if needs_ocr is not None and i not in needs_ocr and t.strip():
            results[i] = {'page': i, 'text': clean_extracted_text(t), 'method': 'pypdf2'}

    if needs_ocr is None or needs_ocr:
        images = convert_pdf_to_images(pdf_bytes, dpi=OCR_DPI)
        targets = range(len(images)) if needs_ocr is None else [i for i in needs_ocr if i < len(images)]
        for i in targets:
            t0 = time.time()
            text, method = _ocr_image(images[i])
            if text:
                results[i] = {'page': i, 'text': clean_extracted_text(text), 'method': method}
                print(f"    Page {i + 1}/{len(images)}: {len(text.split())} words via {method} in {time.time() - t0:.1f}s")
            elif i < len(pages_text) and pages_text[i].strip():
                results[i] = {'page': i, 'text': clean_extracted_text(pages_text[i]), 'method': 'pypdf2'}
            else:
                print(f"    Page {i + 1}/{len(images)}: no text (no OCR engine available?)")

    text_by_page = [results[i] for i in sorted(results) if results[i]['text']]
    print(f"  ✅ Extracted {len(text_by_page)} pages in {time.time() - start:.1f}s")

    # Only cache complete extractions, so a later run with an OCR engine can fill the gaps.
    if cache_file and text_by_page and len(text_by_page) == max(len(pages_text), 1):
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, 'w', encoding='utf-8') as fh:
            json.dump(text_by_page, fh, ensure_ascii=False)
    return text_by_page

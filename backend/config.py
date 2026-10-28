import os
import sys
from pathlib import Path

# The codebase logs with emoji; Windows consoles default to cp1252 and would raise UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    # backend/.env first, then the repo-root .env (values already set in the real environment win).
    _BACKEND_DIR = Path(__file__).resolve().parent
    load_dotenv(_BACKEND_DIR / '.env')
    load_dotenv(_BACKEND_DIR.parent / '.env')
except ImportError:
    pass


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_NAME = os.environ.get('DB_NAME', 'normbridge')
DB_USER = os.environ.get('DB_USER', 'policy_user')
DB_PASSWORD = os.environ.get('DB_PASSWORD', '')

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)
DATABASE = DATABASE_URL  # Kept for backwards compatibility if referenced
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
ALLOWED_EXTENSIONS = {'pdf'}

def _loopback(url):
    # On Windows "localhost" resolves to ::1 first; Ollama listens on IPv4 only, so every call paid a ~2 s
    # IPv6-refused retry. Pinning the loopback address removes that from every embedding/LLM request.
    return (url or '').replace('://localhost', '://127.0.0.1')


OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5:1.5b')
OLLAMA_URL = _loopback(os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434/api/generate'))
EMBEDDING_MODEL = os.environ.get('EMBEDDING_MODEL', 'nomic-embed-text')
# Seconds to wait for the LLM. CPU-only laptops can need well over a minute for the first (cold) answer.
LLM_TIMEOUT = int(os.environ.get('LLM_TIMEOUT', '240'))
# How many top-scoring PDFs the cross-encoder compares before picking the single document to answer from (1 = only the highest-scoring PDF).
RERANK_PDF_CANDIDATES = int(os.environ.get('RERANK_PDF_CANDIDATES', '3'))
# How many re-ranked chunks go into the LLM prompt (fewer = shorter prompt = faster answers on CPU).
RERANK_TOP_K = int(os.environ.get('RERANK_TOP_K', '3'))
# Contextual compression: max words kept per chunk in the prompt (0 = send whole chunks).
PROMPT_CHUNK_WORDS = int(os.environ.get('PROMPT_CHUNK_WORDS', '70'))
# The top-ranked chunk usually holds the answer, so it keeps more of its text.
PROMPT_TOP_CHUNK_WORDS = int(os.environ.get('PROMPT_TOP_CHUNK_WORDS', '220'))
# Query routing: yes/no questions go to this (bigger) model when it is installed; empty = no routing.
YESNO_MODEL = os.environ.get('YESNO_MODEL', 'qwen2.5:3b').strip()
# Same routing when the answer model is a hosted API (LLM_PROVIDER=openai), e.g. Groq llama-3.3-70b-versatile.
YESNO_API_MODEL = os.environ.get('YESNO_API_MODEL', '').strip()
# Multi-document answers: add the runner-up policy when its cross-encoder score is within this margin of the best
# (logit units) and at least MULTI_DOC_MIN_SCORE. 0 disables.
MULTI_DOC_MARGIN = float(os.environ.get('MULTI_DOC_MARGIN', '1.5'))
MULTI_DOC_MIN_SCORE = float(os.environ.get('MULTI_DOC_MIN_SCORE', '0'))
# Max tokens the LLM may generate per answer (answers are 1-4 sentences).
LLM_MAX_TOKENS = int(os.environ.get('LLM_MAX_TOKENS', '220'))
# CPU threads for the local LLM and the re-ranker (Ollama defaults to half the cores; all cores is ~1.75x faster).
CPU_THREADS = int(os.environ.get('CPU_THREADS', str(os.cpu_count() or 4)))
# Keep Ollama models loaded between requests (avoids multi-second reloads).
MODEL_KEEP_ALIVE = os.environ.get('MODEL_KEEP_ALIVE', '60m')
# Cross-encoder used for document and chunk re-ranking.
RERANKER_MODEL = os.environ.get('RERANKER_MODEL', 'cross-encoder/ms-marco-MiniLM-L-12-v2')
# Semantic answer cache: reuse a cached answer when a new question is at least this cosine-similar.
# 0.92 (stricter than the old 0.88) avoids serving e.g. the "in-time" answer for a "quiet hours" question.
SEMANTIC_CACHE_THRESHOLD = float(os.environ.get('SEMANTIC_CACHE_THRESHOLD', '0.92'))

# Answer generation: 'ollama' (local, default) or 'openai' (any OpenAI-compatible API, e.g. Groq/Gemini free tiers for deployment)
LLM_PROVIDER = os.environ.get('LLM_PROVIDER', 'ollama').strip().lower()
LLM_API_BASE = os.environ.get('LLM_API_BASE') or 'https://api.groq.com/openai/v1'
LLM_API_KEY = os.environ.get('LLM_API_KEY', '')
LLM_API_MODEL = os.environ.get('LLM_API_MODEL') or 'llama-3.1-8b-instant'

CHUNK_SIZE = 300
CHUNK_OVERLAP = 50

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
ALLOWED_DOMAIN = os.environ.get('ALLOWED_EMAIL_DOMAIN', 'srmap.edu.in')

# Optional OCR engines: enabled automatically when installed; these flags let you force them off.
OCR_ENABLE_GLM = _env_bool('OCR_ENABLE_GLM', True)
OCR_ENABLE_TESSERACT = _env_bool('OCR_ENABLE_TESSERACT', True)
OCR_ENABLE_EASYOCR = _env_bool('OCR_ENABLE_EASYOCR', True)
TESSERACT_CMD = os.environ.get('TESSERACT_CMD', '')
POPPLER_PATH = os.environ.get('POPPLER_PATH', '') or None
GLM_OCR_MODEL_PATH = os.environ.get('GLM_OCR_MODEL_PATH', '')
OLLAMA_HOST = _loopback(os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434').rstrip('/'))
if not OLLAMA_HOST.startswith('http'):
    OLLAMA_HOST = 'http://' + OLLAMA_HOST
os.environ['OLLAMA_HOST'] = OLLAMA_HOST
OLLAMA_OCR_MODEL = os.environ.get('OLLAMA_OCR_MODEL', 'glm-ocr')
# Pages with fewer words than this in the PDF text layer are treated as scanned and sent to OCR.
OCR_MIN_WORDS_PER_PAGE = int(os.environ.get('OCR_MIN_WORDS_PER_PAGE', '20'))
# Render resolution for OCR. Lower = faster on CPU (GLM-OCR cost grows with image area).
OCR_DPI = int(os.environ.get('OCR_DPI', '150'))
# Max seconds per page for GLM-OCR before falling back to the next engine (CPU pages take ~80-150 s).
OCR_PAGE_TIMEOUT = int(os.environ.get('OCR_PAGE_TIMEOUT', '300'))
# Extracted text is cached per PDF hash here, so OCR runs once per document (portable with the repo).
OCR_CACHE_DIR = os.environ.get('OCR_CACHE_DIR', str(Path(__file__).resolve().parent.parent / 'data' / 'ocr_cache'))

PORT = int(os.environ.get('PORT', '5001'))
FLASK_DEBUG = _env_bool('FLASK_DEBUG', False)

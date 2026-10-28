import os
from flask import Blueprint, jsonify

from config import OLLAMA_MODEL, LLM_PROVIDER, LLM_API_MODEL

from middleware.business_layer import fetch_system_counts
from services.ocr_service import (
    PDF2IMAGE_AVAILABLE, MLX_AVAILABLE, GLM_OCR_MODEL_PATH,
    TESSERACT_AVAILABLE, EASYOCR_AVAILABLE, PYMUPDF_AVAILABLE, ollama_ocr_available
)

system_bp = Blueprint('system', __name__)


def _available_chat_models():
    """Which of the UI's model choices can really run here (others silently fall back to the default)."""
    import requests
    from config import OLLAMA_HOST
    from services.rag_service import BITNET_CPP_AVAILABLE
    try:
        names = {m.get('name', '') for m in requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3).json().get('models', [])}
    except Exception:
        names = set()
    has = lambda n: n in names or f"{n}:latest" in names
    return {'phi3:mini': has('phi3:mini'), 'qwen3.5:2b': has('qwen3.5:2b'), 'bitnet': bool(BITNET_CPP_AVAILABLE)}


def _installed_chat_models():
    """Installed Ollama models that can answer questions (embedding/OCR models excluded), default first."""
    if LLM_PROVIDER == 'openai':
        return [LLM_API_MODEL]
    import requests
    from config import OLLAMA_HOST
    from routes.chat import is_chat_model
    try:
        names = [m.get('name', '') for m in requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3).json().get('models', [])]
    except Exception:
        names = []
    models = sorted({n[:-7] if n.endswith(':latest') else n for n in names if is_chat_model(n)})
    if OLLAMA_MODEL in models:
        models.remove(OLLAMA_MODEL)
    return [OLLAMA_MODEL] + models

@system_bp.route('/health', methods=['GET'])
def health():
    counts = fetch_system_counts()
    glm_mlx = MLX_AVAILABLE and os.path.exists(GLM_OCR_MODEL_PATH)
    glm_ollama = bool(ollama_ocr_available())
    return jsonify({
        'status': 'healthy',
        'vectors': counts['vectors'],
        'policies': counts['policies'],
        'models': _available_chat_models(),
        'chat_models': _installed_chat_models(),
        'default_model': OLLAMA_MODEL if LLM_PROVIDER != 'openai' else LLM_API_MODEL,
        'extraction_method': 'pdf text layer + glm-ocr for scanned pages (with fallbacks)',
        'pdf2image_available': PDF2IMAGE_AVAILABLE,
        'pymupdf_available': PYMUPDF_AVAILABLE,
        'mlx_available': MLX_AVAILABLE,
        'glm_ocr_available': glm_mlx or glm_ollama,
        'extraction_methods': {
            'glm_ocr': glm_mlx or glm_ollama,
            'glm_ocr_mlx': glm_mlx,
            'glm_ocr_ollama': glm_ollama,
            'tesseract': TESSERACT_AVAILABLE,
            'easyocr': EASYOCR_AVAILABLE
        }
    })

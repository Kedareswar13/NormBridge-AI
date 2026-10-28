# NormBridge AI API - Hugging Face Spaces (Docker, free CPU tier: 2 vCPU / 16 GB RAM)
# Built by scripts/prepare_hf_space.ps1, which copies backend/, data/, evals/ and scripts/ next to this file.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HF_HOME=/home/user/.cache/huggingface \
    OLLAMA_MODELS=/home/user/.ollama/models

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates zstd \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://ollama.com/install.sh | sh

# Spaces run as uid 1000
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY --chown=user backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r backend/requirements.txt

COPY --chown=user . .
RUN chmod +x start.sh

USER user
# Bake the models into the image so a cold start doesn't download ~400 MB: the embedding model (Ollama) and the
# cross-encoder re-ranker (Hugging Face cache under HF_HOME). start.sh still pulls if they're missing.
RUN (ollama serve > /tmp/ollama-build.log 2>&1 &) \
    && for i in $(seq 1 30); do curl -sf http://localhost:11434/api/tags > /dev/null && break; sleep 1; done \
    && ollama pull nomic-embed-text \
    && python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-12-v2')"
EXPOSE 7860
CMD ["./start.sh"]

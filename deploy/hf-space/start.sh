#!/usr/bin/env bash
# Start order: Ollama (embeddings only) -> API (waitress) -> ingest bundled policies if the DB is empty.
set -e

ollama serve > /tmp/ollama.log 2>&1 &
for i in $(seq 1 60); do curl -sf http://localhost:11434/api/tags > /dev/null && break; sleep 1; done
ollama pull "${EMBEDDING_MODEL:-nomic-embed-text}"

cd backend
python serve.py &
API_PID=$!
cd ..

for i in $(seq 1 180); do curl -sf "http://localhost:${PORT}/health" > /dev/null && break; sleep 2; done
# OCR results ship in data/ocr_cache, so this only computes embeddings. Already-ingested files are skipped.
python scripts/ingest_policies.py --api "http://localhost:${PORT}" || true

wait $API_PID

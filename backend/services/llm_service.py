"""LLM provider switch.

LLM_PROVIDER=ollama (default)  -> local Ollama /api/chat (model's own chat template, cached system-prompt prefix).
LLM_PROVIDER=openai            -> any OpenAI-compatible chat API, for free cloud deployment:
                                  Groq            LLM_API_BASE=https://api.groq.com/openai/v1   LLM_API_MODEL=llama-3.1-8b-instant
                                  Gemini          LLM_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai  LLM_API_MODEL=gemini-2.0-flash
                                  OpenRouter      LLM_API_BASE=https://openrouter.ai/api/v1      LLM_API_MODEL=<free model id>
                                  Ollama itself   LLM_API_BASE=http://localhost:11434/v1         LLM_API_MODEL=phi3:mini
"""
import json

import requests

from config import (OLLAMA_URL, LLM_TIMEOUT, LLM_PROVIDER, LLM_API_BASE, LLM_API_KEY, LLM_API_MODEL,
                    LLM_MAX_TOKENS, MODEL_KEEP_ALIVE, CPU_THREADS)

# num_ctx 3072 comfortably fits the compressed prompt (~500-900 tokens) + answer; smaller ctx = less KV memory/time.
GENERATION_OPTIONS = {"temperature": 0.1, "top_p": 0.9, "num_predict": LLM_MAX_TOKENS, "num_ctx": 3072, "num_thread": CPU_THREADS}
OLLAMA_BASE = OLLAMA_URL.split('/api/')[0]


def provider_label(default_label):
    if LLM_PROVIDER != 'openai':
        return default_label
    return default_label if '(yes/no route)' in str(default_label) else f"{LLM_API_MODEL} (API)"


def generate_answer(prompt, ollama_model, messages=None, on_token=None):
    """Return (ok, text).

    `messages` = [{'role': 'system', ...}, {'role': 'user', ...}] is preferred (proper chat template, and the constant
    system message lets Ollama reuse its KV cache across questions). `prompt` is the flattened fallback.
    `on_token(delta)` turns on token streaming (SSE to the browser); the full text is still returned at the end.
    """
    messages = messages or [{"role": "user", "content": prompt}]
    if LLM_PROVIDER == 'openai':
        # "api:<model>" = a per-request model on the same API (query routing); anything else = the configured model
        api_model = ollama_model[4:] if isinstance(ollama_model, str) and ollama_model.startswith('api:') else LLM_API_MODEL
        headers = {"Content-Type": "application/json"}
        if LLM_API_KEY:
            headers["Authorization"] = f"Bearer {LLM_API_KEY}"
        r = requests.post(
            f"{LLM_API_BASE.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": api_model,
                "messages": messages,
                "temperature": GENERATION_OPTIONS["temperature"],
                "top_p": GENERATION_OPTIONS["top_p"],
                "max_tokens": GENERATION_OPTIONS["num_predict"],
                "stream": bool(on_token),
            },
            timeout=LLM_TIMEOUT,
            stream=bool(on_token),
        )
        if r.status_code != 200:
            return False, f"LLM API error ({r.status_code}): {r.text[:300]}"
        if on_token:
            parts = []
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith('data:'):
                    continue
                payload = line[5:].strip()
                if payload == '[DONE]':
                    break
                try:
                    delta = ((json.loads(payload).get('choices') or [{}])[0].get('delta') or {}).get('content') or ''
                except ValueError:
                    continue
                if delta:
                    parts.append(delta)
                    on_token(delta)
            return True, ''.join(parts).strip()
        choices = r.json().get("choices") or [{}]
        return True, ((choices[0].get("message") or {}).get("content") or "").strip()

    r = requests.post(
        f"{OLLAMA_BASE}/api/chat",
        json={"model": ollama_model, "messages": messages, "stream": bool(on_token), "keep_alive": MODEL_KEEP_ALIVE,
              "options": GENERATION_OPTIONS},
        timeout=LLM_TIMEOUT,
        stream=bool(on_token),
    )
    if r.status_code != 200:
        return False, f"Ollama model error: {r.text}"
    if on_token:
        parts = []
        for line in r.iter_lines(decode_unicode=True):  # NDJSON: one {"message": {"content": delta}} per token
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            delta = (chunk.get('message') or {}).get('content') or ''
            if delta:
                parts.append(delta)
                on_token(delta)
            if chunk.get('done'):
                break
        return True, ''.join(parts).strip()
    return True, ((r.json().get("message") or {}).get("content") or "").strip()

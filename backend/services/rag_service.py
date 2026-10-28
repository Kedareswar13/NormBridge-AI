import os
import re
import json
import math
import time
import typing
import hashlib
import threading
from collections import Counter
import numpy as np
import ollama
import requests
import psycopg2
from config import *
from core.db import get_db, get_ist_now

_OLLAMA_CLIENT = ollama.Client(host=OLLAMA_HOST)

CHUNK_SIZE = 300
CHUNK_OVERLAP = 50
# target size of structure-aware chunks: big enough to keep a clause/list together, small enough that contextual
# compression (220/70 words) rarely has to drop sentences and embeddings stay specific
CHUNK_WORDS = int(os.environ.get('CHUNK_WORDS', '200'))
CONFIDENCE_THRESHOLD = 0.38

try:
    from sentence_transformers import CrossEncoder
    _cross_encoder = None
    CROSS_ENCODER_AVAILABLE = True
    print("✅ sentence-transformers available - cross-encoder re-ranking enabled")
except ImportError:
    CROSS_ENCODER_AVAILABLE = False
    _cross_encoder = None
    print("⚠️ sentence-transformers not installed: pip install sentence-transformers")

def _get_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        print(f"🔄 Loading cross-encoder {RERANKER_MODEL}...")
        _cross_encoder = CrossEncoder(RERANKER_MODEL, max_length=256)
        print("✅ Cross-encoder loaded")
    return _cross_encoder

def cross_encoder_scores(question: str, chunks: list):
    """Relevance score per chunk from the cross-encoder (memoised per question+chunk), or None if unavailable."""
    if not CROSS_ENCODER_AVAILABLE or not chunks:
        return None
    try:
        keys = [hashlib.sha1(f"{question}\x00{c.get('pdf', '')}\x00{c['text']}".encode('utf-8')).hexdigest() for c in chunks]
        scores = [_lru_get(_RERANK_CACHE, k) for k in keys]
        todo = [i for i, s in enumerate(scores) if s is None]
        CACHE_STATS['rerank_hits'] += len(chunks) - len(todo)
        CACHE_STATS['rerank_misses'] += len(todo)
        if todo:
            # score each chunk on its most question-relevant sentences (shorter = faster + less noise), prefixed with
            # the document title and section so near-duplicate policies (Student vs Employee) are told apart
            from services.chunking import contextual_text
            pairs = [(question, contextual_text(chunks[i].get('pdf'), chunks[i].get('section'),
                                                compress_chunk(question, chunks[i]['text'], 120))) for i in todo]
            fresh = _get_cross_encoder().predict(pairs, batch_size=32)
            for i, s in zip(todo, fresh):
                scores[i] = float(s)
                _lru_put(_RERANK_CACHE, keys[i], float(s), _RERANK_CACHE_MAX)
        return scores
    except Exception as e:
        print(f"⚠️ Cross-encoder error: {e}")
        return None

def rerank_chunks(question: str, chunks: list, top_k: int = 3) -> list:
    scores = cross_encoder_scores(question, chunks)
    if scores is None:
        return chunks[:top_k]
    ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
    for s, c in ranked:
        c['rerank_score'] = s
    return [c for _, c in ranked[:top_k]]

def pick_pdf_by_reranker(question: str, pdf_chunks: dict, candidates: list, per_pdf: int = 4):
    """Doc-level re-ranking: score the best chunks of each candidate PDF with the cross-encoder and
    return the PDF owning the single most relevant chunk. Fixes the bi-encoder + chunk-count bias where
    long documents that merely mention a keyword many times beat the short document that answers it."""
    pool = []
    for pdf in candidates:
        ranked = sorted(pdf_chunks[pdf], key=lambda c: c['score'], reverse=True)
        # keyword hits outside the vector top-k still get scored, but at most 2 per document: an uncapped pool made
        # rerank p95 ~6 s on broad questions
        pool += ranked[:per_pdf] + [c for c in ranked[per_pdf:] if c.get('bm25')][:2]
    scores = cross_encoder_scores(question, pool)
    if scores is None:
        return None, {}
    best = {}
    for s, c in zip(scores, pool):
        pdf = str(c['pdf'])
        best[pdf] = max(best.get(pdf, float('-inf')), s)
    return max(best, key=best.get), best

BITNET_CPP_DIR = os.environ.get(
    'BITNET_CPP_DIR',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'BitNet')
)
BITNET_MODEL_PATH = os.environ.get(
    'BITNET_MODEL_PATH',
    os.path.join(BITNET_CPP_DIR, 'models', 'Falcon3-1B-Instruct-1.58bit', 'ggml-model-i2_s.gguf')
)
_BITNET_SCRIPT = os.path.join(BITNET_CPP_DIR, 'run_inference.py')
BITNET_CPP_AVAILABLE = os.path.isfile(_BITNET_SCRIPT) and os.path.isfile(BITNET_MODEL_PATH)

if BITNET_CPP_AVAILABLE:
    print(f"[OK] BitNet.cpp found - real 1-bit BitNet 1.58b inference enabled")
else:
    print("[WARN] BitNet.cpp not configured - BitNet mode will fall back to phi3:mini")

BITNET_SERVER_URL = os.environ.get('BITNET_SERVER_URL', 'http://localhost:8080/completion')

def run_bitnet_inference(system_prompt: str, user_prompt: str, max_tokens: int = 512, temperature: float = 0.1) -> str:
    if not BITNET_CPP_AVAILABLE:
        raise RuntimeError("bitnet.cpp not installed — run backend/setup_bitnet.py")

    formatted_prompt = f"<|system|>\n{system_prompt}\n<|user|>\n{user_prompt}\n<|assistant|>\n"
    
    payload = {
        "prompt": formatted_prompt,
        "n_predict": max_tokens,
        "temperature": temperature,
        "stop": ["<|endoftext|>", "<|end|>"],
        "stream": False
    }

    try:
        response = requests.post(BITNET_SERVER_URL, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        return data.get('content', "").strip()
    except Exception as e:
        raise RuntimeError(f"BitNet Server error: {e}")

def chunk_text_intelligently(text_by_page, max_words=None):
    """Structure-aware chunking (see services/chunking.py): whole document, so lists/paragraphs can cross pages;
    table rows stay with their header; continuation chunks carry their section heading."""
    from services.chunking import structure_aware_chunks
    return structure_aware_chunks(text_by_page, max_words or CHUNK_WORDS, CHUNK_OVERLAP)


def _legacy_chunk_text(text_by_page):
    chunks = []
    
    for page_data in text_by_page:
        text = page_data['text']
        page_num = page_data['page']
        
        section_breaks = []
        
        numbered = list(re.finditer(r'\n\s*(\d+\.)\s+[A-Z]', text))
        for match in numbered:
            section_breaks.append(match.start())
        
        caps = list(re.finditer(r'\n\s*([A-Z][A-Z\s]{2,})\n', text))
        for match in caps:
            section_breaks.append(match.start())
        
        colon = list(re.finditer(r'\n\s*([A-Z][a-z]+:)\s', text))
        for match in colon:
            section_breaks.append(match.start())
        
        section_breaks.sort()
        section_breaks = [0] + section_breaks + [len(text)]
        
        for i in range(len(section_breaks) - 1):
            start = section_breaks[i]
            end = section_breaks[i + 1]
            
            section_text = text[start:end].strip()
            
            if not section_text or len(section_text.split()) < 5:
                continue
            
            section_title = "General"
            first_line = section_text.split('\n')[0].strip()
            
            if (first_line.isupper() or 
                first_line.endswith(':') or 
                re.match(r'^\d+\.', first_line)):
                section_title = re.sub(r':$', '', first_line)
            
            words = section_text.split()
            if len(words) > CHUNK_SIZE:
                for j in range(0, len(words), CHUNK_SIZE - CHUNK_OVERLAP):
                    chunk_words = words[j:j + CHUNK_SIZE]
                    if len(chunk_words) > 20:
                        chunk_text = ' '.join(chunk_words)
                        chunks.append({
                            'text': chunk_text,
                            'page': page_num,
                            'section': section_title,
                            'chunk_index': len(chunks),
                            'is_header': j == 0
                        })
            else:
                chunks.append({
                    'text': section_text,
                    'page': page_num,
                    'section': section_title,
                    'chunk_index': len(chunks),
                    'is_header': True
                })
    
    return chunks

from collections import OrderedDict

# ----------------------------------------------------------------------------- caches
# 1) query/chunk embedding cache  2) cross-encoder score cache  (+ the semantic answer cache in ChromaDB)
CACHE_STATS = {'embedding_hits': 0, 'embedding_misses': 0, 'answer_cache_hits': 0, 'answer_cache_misses': 0,
               'rerank_hits': 0, 'rerank_misses': 0}
_EMBED_CACHE = OrderedDict()
_EMBED_CACHE_MAX = 4096
_RERANK_CACHE = OrderedDict()
_RERANK_CACHE_MAX = 20000


def _lru_get(cache, key):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    return None


def _lru_put(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def create_embedding(text):
    key = hashlib.sha1(f"{EMBEDDING_MODEL}|{text}".encode('utf-8')).hexdigest()
    cached = _lru_get(_EMBED_CACHE, key)
    if cached is not None:
        CACHE_STATS['embedding_hits'] += 1
        return cached
    CACHE_STATS['embedding_misses'] += 1
    try:
        response = _OLLAMA_CLIENT.embeddings(
            model=EMBEDDING_MODEL,
            prompt=text,
            keep_alive=MODEL_KEEP_ALIVE
        )
        embedding = np.array(
            response["embedding"],
            dtype=np.float32
        ).tobytes()
        _lru_put(_EMBED_CACHE, key, embedding, _EMBED_CACHE_MAX)
        return embedding
    except Exception as e:
        print("❌ Embedding error:", e)
        raise RuntimeError("Embedding generation failed")


_STOP = set('a an the of to in on for and or is are was were be by with as at from that this it its can do does i my me you your we our what which who whom how when where why will would should could may must not no if than then there their they them'.split())


def _terms(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or '').lower()) if w not in _STOP and (len(w) > 2 or w.isdigit())}


_ABBREVIATIONS = ['Rs', 'No', 'Dr', 'Mr', 'Ms', 'Mrs', 'St', 'vs', 'viz', 'i.e', 'e.g', 'etc', 'Sl', 'S.N', 'Ph.D', 'Prof', 'Govt', 'Dept', 'approx', 'Fig', 'Ref', 'Reg', 'Off']
_ABBREV = ''.join(rf'(?<!\b{re.escape(a)}\.)' for a in _ABBREVIATIONS)
_SENT_SPLIT = re.compile(_ABBREV + r'(?<=[.;!?])\s+(?=[A-Z(•\-\d])|\n+')


def compress_chunk(question, text, max_words):
    """Contextual compression: keep the sentences of a chunk that overlap most with the question
    (in original order) up to `max_words`, plus the sentence right after each strong match (it often carries
    the number/amount). Shrinks the LLM prompt ~2-3x, which dominates CPU latency."""
    words = text.split()
    if len(words) <= max_words:
        return text
    q = _terms(question)
    sentences = [s.strip() for s in _SENT_SPLIT.split(text) if s and s.strip()]
    overlap = [len(q & _terms(s)) for s in sentences]
    scored = sorted(range(len(sentences)), key=lambda i: (-overlap[i], i))
    keep, budget = set(), 0

    def add(i):
        nonlocal budget
        if i in keep or i >= len(sentences):
            return
        n = len(sentences[i].split())
        if budget and budget + n > max_words:
            return
        keep.add(i)
        budget += n

    for i in scored:
        if overlap[i] == 0 and keep:
            break
        add(i)
        if overlap[i] >= 2:
            add(i + 1)
        if budget >= max_words:
            break
    return ' '.join(sentences[i] for i in sorted(keep))


def warm_up_models():
    """Load the embedding model, the LLM and the cross-encoder once at startup so the first user doesn't pay for it."""
    t0 = time.time()
    try:
        create_embedding("warm up")
    except Exception as e:
        print(f"⚠️ warm-up embedding failed: {e}")
    try:
        cross_encoder_scores("warm up", [{'text': 'warm up'}])
    except Exception as e:
        print(f"⚠️ warm-up cross-encoder failed: {e}")
    if LLM_PROVIDER != 'openai':
        try:
            requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": "Hi", "stream": False, "keep_alive": MODEL_KEEP_ALIVE,
                                            "options": {"num_predict": 1, "num_thread": CPU_THREADS, "num_ctx": 3072}}, timeout=300)
        except Exception as e:
            print(f"⚠️ warm-up LLM failed: {e}")
    print(f"🔥 Models warmed up in {time.time() - t0:.1f}s (LLM: {OLLAMA_MODEL}, kept in memory for {MODEL_KEEP_ALIVE})")

def cosine_similarity(a_bytes, b_bytes):
    try:
        a = np.frombuffer(a_bytes, dtype=np.float32)
        b = np.frombuffer(b_bytes, dtype=np.float32)
        
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        
        if a_norm == 0 or b_norm == 0:
            return 0
        
        return float(np.dot(a, b) / (a_norm * b_norm))
    except Exception as e:
        return 0

from services.chroma_service import qa_cache_chroma, policy_chroma

def normalize_question(question: str) -> str:
    if not question:
        return ""
    cleaned = re.sub(r'[^\w\s]', '', question.lower()).strip()
    return re.sub(r'\s+', ' ', cleaned)

def check_exact_cache(question: str):
    """Exact/High-similarity cache lookup in ChromaDB QA cache."""
    try:
        if not question:
            return None, None
        emb = create_embedding(question)
        answer, sources, sim = qa_cache_chroma.search_cache(emb, threshold=0.99)
        if answer:
            print(f"🚀 [ChromaDB EXACT CACHE HIT]: Match found for '{question}' (Sim: {sim:.4f})")
            return answer, sources
        return None, None
    except Exception as e:
        print(f"⚠️ Exact cache check error: {e}")
        return None, None

def check_semantic_cache(question_embedding_bytes_or_str, threshold=None, scope='student'):
    """
    Semantic answer cache (ChromaDB ./chroma_db/qa_cache): if a previous question is at least
    SEMANTIC_CACHE_THRESHOLD cosine-similar, return its stored (answer, sources) instantly.
    """
    threshold = SEMANTIC_CACHE_THRESHOLD if threshold is None else threshold
    try:
        if isinstance(question_embedding_bytes_or_str, str):
            question_embedding_bytes_or_str = create_embedding(question_embedding_bytes_or_str)

        answer, sources, similarity = qa_cache_chroma.search_cache(
            question_embedding_bytes_or_str, threshold=threshold, scope=scope
        )
        if answer:
            CACHE_STATS['answer_cache_hits'] += 1
            return answer, sources

        CACHE_STATS['answer_cache_misses'] += 1
        return None, None
    except Exception as e:
        print(f"⚠️ ChromaDB QA Cache lookup error: {e}")
        return None, None


def cache_overview():
    try:
        from services.chroma_service import qa_cache_chroma as qc
        cached_answers = qc.collection.count()
    except Exception:
        cached_answers = None
    lookups = CACHE_STATS['answer_cache_hits'] + CACHE_STATS['answer_cache_misses']
    return {
        **CACHE_STATS,
        'cached_answers': cached_answers,
        'answer_cache_hit_rate': round(CACHE_STATS['answer_cache_hits'] / lookups, 3) if lookups else None,
        'embedding_cache_size': len(_EMBED_CACHE),
        'rerank_cache_size': len(_RERANK_CACHE),
        'semantic_threshold': SEMANTIC_CACHE_THRESHOLD,
    }

def add_to_semantic_cache(question: str, embedding_bytes: bytes = None, answer: str = "", sources_json: str = "[]", scope: str = "student"):
    """
    Store a Q&A pair in the semantic answer cache, partitioned by `scope` (the asker's role) so a cached
    answer built from staff-only documents can never be served to a student (security layer 2).
    """
    if isinstance(embedding_bytes, str) and not answer:
        answer = embedding_bytes
        embedding_bytes = None
        
    try:
        if embedding_bytes is None and question:
            embedding_bytes = create_embedding(question)

        # 1. Primary: Save to physically isolated ChromaDB QA Cache instance
        qa_cache_chroma.add_to_cache(question, embedding_bytes, answer, sources_json, scope=scope)

        # 2. Secondary: Persist backup record to PostgreSQL
        db = get_db()
        emb_binary = psycopg2.Binary(embedding_bytes) if embedding_bytes else None

        db.execute(
            '''INSERT INTO semantic_cache (question, embedding, answer, sources) 
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (question) 
               DO UPDATE SET embedding = EXCLUDED.embedding, answer = EXCLUDED.answer, sources = EXCLUDED.sources''',
            (question, emb_binary, answer, sources_json)
        )
        db.commit()
    except Exception as e:
        print(f"⚠️ Cache storage error: {e}")

def get_policy_list_formatted():
    try:
        db = get_db()
        policies = db.execute('''
            SELECT name, chunks 
            FROM policies 
            ORDER BY uploaded_at DESC
        ''').fetchall()
        
        if policies:
            return "\n".join([
                f"  • {p['name']} ({p['chunks']} sections)" 
                for p in policies
            ])
        else:
            return "  • No policies uploaded yet"
    except Exception as e:
        print(f"Error getting policy list: {e}")
        return "  • Unable to fetch policy list"

def semantic_search(query, n_results=15, min_score=0.25, pdf_filter=None, query_embedding=None, role='student'):
    """Vector search restricted to the documents `role` may see in this tenant (security layer 2)."""
    from services.security_service import retrieval_filter, allowed_audiences, TENANT_ID
    try:
        if query_embedding is None:
            query_embedding = create_embedding(query)

        chroma_results = policy_chroma.search_policy_chunks(
            query_embedding, top_k=n_results, min_score=min_score, pdf_filter=pdf_filter,
            access_filter=retrieval_filter(role)
        )

        if chroma_results:
            doc_scores: typing.Dict[str, typing.List[float]] = {}
            for r in chroma_results:
                pdf = str(r['pdf'])
                if pdf not in doc_scores:
                    doc_scores[pdf] = []
                doc_scores[pdf].append(float(r['score']))

            doc_avg = {pdf: sum(scores)/len(scores) for pdf, scores in doc_scores.items() if len(scores) > 0}
            if doc_avg:
                best_doc = max(doc_avg, key=lambda k: doc_avg[k])
                best_avg = doc_avg[best_doc]
                print(f"\n📊 Document with highest semantic relevance (ChromaDB Policy DB): {best_doc}")
                print(f"   Average semantic score: {best_avg:.3f}")
            return chroma_results[:n_results]

        db = get_db()
        query_np = np.frombuffer(query_embedding, dtype=np.float32)
        
        query_str = '''
            SELECT e.*, p.name as pdf_name 
            FROM embeddings e 
            JOIN policies p ON e.policy_id = p.id
            WHERE COALESCE(p.audience, 'public') = ANY(%s) AND COALESCE(p.tenant_id, %s) = %s
        '''
        params = [allowed_audiences(role), TENANT_ID, TENANT_ID]
        if pdf_filter:
            query_str += " AND p.name = %s"
            params.append(pdf_filter)
            
        chunks = db.execute(query_str, params).fetchall()
        
        if not chunks:
            return []
        
        scored_results = []
        for chunk in chunks:
            if chunk['embedding']:
                chunk_vec = np.frombuffer(bytes(chunk['embedding']), dtype=np.float32)
                similarity = float(np.dot(query_np, chunk_vec) / 
                                 (np.linalg.norm(query_np) * np.linalg.norm(chunk_vec)))
                
                if similarity >= min_score:
                    scored_results.append({
                        'text': chunk['text'],
                        'pdf': chunk['pdf_name'],
                        'page': chunk['page_number'] or 0,
                        'score': similarity,
                        'chunk_index': chunk['chunk_index'] or 0,
                        'section': chunk['section_title'] or 'General'
                    })
        
        if not scored_results:
            return []
        
        scored_results.sort(key=lambda x: x['score'], reverse=True)
        
        doc_scores = {}
        doc_chunks = {}
        
        for r in scored_results:
            pdf = str(r['pdf'])
            if pdf not in doc_scores:
                doc_scores[pdf] = []
                doc_chunks[pdf] = []
            doc_scores[pdf].append(float(r['score']))
            doc_chunks[pdf].append(r)
        
        doc_avg = {pdf: sum(scores)/len(scores) if len(scores) > 0 else 0.0 for pdf, scores in doc_scores.items()}
        
        best_doc = max(doc_avg, key=lambda k: doc_avg[k])
        best_avg = doc_avg[best_doc]
        
        print(f"\n📊 Document with highest semantic relevance (PostgreSQL Fallback): {best_doc}")
        print(f"   Average semantic score: {best_avg:.3f}")
        
        final_results = list(doc_chunks[best_doc])
        final_results.sort(key=lambda x: x['score'], reverse=True)
        
        return [final_results[i] for i in range(min(5, len(final_results)))]
        
    except Exception as e:
        print(f"❌ Semantic search error: {e}")
        return []

# --------------------------------------------------------------------------- hybrid retrieval (BM25 keyword side)
# Dense embeddings miss exact-term matches ("vacate", "UGC", "one year") that a keyword index finds immediately.
# The corpus is small (hundreds of chunks), so a pure-Python BM25 index built lazily from Postgres is enough.
_KW_INDEX = None
_KW_LOCK = threading.Lock()


def _kw_stem(word):
    for suffix in ('ations', 'ation', 'ments', 'ment', 'ings', 'ing', 'ies', 'ied', 'ers', 'es', 'ed', 'er', 's'):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ('y' if suffix in ('ies', 'ied') else '')
    return word


def _kw_tokens(text):
    return [_kw_stem(w) for w in re.findall(r"[a-z0-9]+", (text or '').lower()) if w not in _STOP and (len(w) > 2 or w.isdigit())]


def invalidate_keyword_index():
    global _KW_INDEX
    with _KW_LOCK:
        _KW_INDEX = None


def _build_keyword_index():
    rows = get_db().execute('''
        SELECT e.text, e.embedding, e.page_number, e.chunk_index, e.section_title, p.name AS pdf_name,
               COALESCE(p.audience, 'public') AS audience, COALESCE(p.tenant_id, '') AS tenant_id
        FROM embeddings e JOIN policies p ON e.policy_id = p.id''').fetchall()
    docs, df = [], Counter()
    for r in rows:
        tf = Counter(_kw_tokens(contextual_text(r['pdf_name'], r['section_title'], r['text'])))
        df.update(tf.keys())
        docs.append({'tf': tf, 'len': sum(tf.values()), 'row': r})
    n = len(docs) or 1
    idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
    avg_len = (sum(d['len'] for d in docs) / n) if docs else 1.0
    return {'docs': docs, 'idf': idf, 'avg_len': avg_len or 1.0}


from services.retrieval_utils import reciprocal_rank_fusion  # noqa: E402,F401  (re-exported for routes)
from services.chunking import contextual_text  # noqa: E402


def keyword_search(query, role='student', top_k=6, query_embedding=None, k1=1.5, b=0.75):
    """BM25 over the chunks `role` may see (same access rules as the vector search). Results carry the
    cosine similarity to the query as `score`, so they can be merged with vector hits on one scale."""
    global _KW_INDEX
    from services.security_service import allowed_audiences, TENANT_ID
    try:
        with _KW_LOCK:
            if _KW_INDEX is None:
                _KW_INDEX = _build_keyword_index()
            index = _KW_INDEX
        q_terms = set(_kw_tokens(query))
        if not q_terms or not index['docs']:
            return []
        allowed = set(allowed_audiences(role))
        scored = []
        for d in index['docs']:
            row = d['row']
            if row['audience'] not in allowed or (row['tenant_id'] and row['tenant_id'] != TENANT_ID):
                continue
            s = 0.0
            for t in q_terms:
                f = d['tf'].get(t)
                if f:
                    s += index['idf'][t] * f * (k1 + 1) / (f + k1 * (1 - b + b * d['len'] / index['avg_len']))
            if s > 0:
                scored.append((s, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        q_vec = np.frombuffer(query_embedding, dtype=np.float32) if query_embedding else None
        out = []
        for s, row in scored[:top_k]:
            cos = 0.0
            if q_vec is not None and row['embedding']:
                v = np.frombuffer(bytes(row['embedding']), dtype=np.float32)
                cos = float(np.dot(q_vec, v) / ((np.linalg.norm(q_vec) * np.linalg.norm(v)) or 1.0))
            out.append({'text': row['text'], 'pdf': row['pdf_name'], 'page': row['page_number'] or 0, 'score': cos,
                        'chunk_index': row['chunk_index'] or 0, 'section': row['section_title'] or 'General', 'bm25': round(s, 3)})
        return out
    except Exception as e:
        print(f"⚠️ Keyword search error: {e}")
        return []

from services.prompt_service import build_dynamic_prompt

def create_enhanced_prompt(question, chunks, pdf_name, policy_type="General", model_type="phi3", is_doc_id_intent=False):
    if not chunks:
        return None
    
    top_score = chunks[0].get('score', 0)
    if top_score < (CONFIDENCE_THRESHOLD - 0.08):
        section = chunks[0].get('section', 'the relevant section')
        return f"__LOW_CONFIDENCE__::{pdf_name}::{section}"
    
    return build_dynamic_prompt(pdf_name, policy_type, chunks, question, model_type, is_doc_id_intent=is_doc_id_intent)

def _normalize_spaces(text):
    """Collapse runs of spaces/tabs but keep line breaks, so markdown lists and tables survive."""
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def clean_excerpt_references(answer):
    phrases_to_remove = [
        "Based on the provided excerpt",
        "According to the excerpt",
        "In the provided text",
        "The excerpt states that",
        "As mentioned in the excerpt",
        "From the excerpt above",
        "Based on the information provided",
        "According to the document"
    ]
    
    for phrase in phrases_to_remove:
        answer = re.sub(phrase, "", answer, flags=re.IGNORECASE)
    
    return _normalize_spaces(answer)

def _known_policy_names():
    try:
        from services.chroma_service import policy_chroma
        metas = policy_chroma.collection.get(include=["metadatas"]).get("metadatas") or []
        return {m.get("pdf_name") for m in metas if m and m.get("pdf_name")}
    except Exception:
        return set()

def clean_answer(answer, pdf_name):
    # Strip attributions to any policy document the answer was NOT grounded in (pdf_name: one name or several).
    allowed = {pdf_name} if isinstance(pdf_name, str) else set(pdf_name or [])
    for other in _known_policy_names():
        if other not in allowed:
            patterns = [
                f"according to {other}",
                f"in {other}",
                f"from {other}",
                f"{other} states",
                f"{other} says"
            ]
            for pattern in patterns:
                answer = re.sub(re.escape(pattern), "", answer, flags=re.IGNORECASE)
    
    return _normalize_spaces(answer)

def remove_citations_from_text(answer):
    answer = re.sub(r'[\(\[]Policy Document:.*?[\)\]]', '', answer, flags=re.IGNORECASE)
    answer = re.sub(r'[\(\[]Page\s*\d+.*?[\)\]]', '', answer, flags=re.IGNORECASE)
    answer = re.sub(r'[\(\[]p\.?\s*\d+.*?[\)\]]', '', answer, flags=re.IGNORECASE)
    answer = re.sub(r'According to (?:page|p\.?)\s*\d+.*?,?\s*', '', answer, flags=re.IGNORECASE)
    return _normalize_spaces(answer)

def clear_semantic_cache(reason=""):
    """Drop every cached Q&A pair. Called whenever the policy corpus changes so answers never go stale."""
    invalidate_keyword_index()
    try:
        from services.chroma_service import qa_cache_chroma
        ids = qa_cache_chroma.collection.get().get("ids") or []
        if ids:
            qa_cache_chroma.collection.delete(ids=ids)
        conn = get_db()
        conn.execute('DELETE FROM semantic_cache')
        conn.commit()
        print(f"🧹 [QA Cache] Cleared {len(ids)} cached answers{f' ({reason})' if reason else ''}")
    except Exception as e:
        print(f"⚠️ Failed to clear semantic cache: {e}")

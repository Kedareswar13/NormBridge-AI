import time
import uuid
import json
import queue
import threading
import traceback
import requests
from flask import Blueprint, request, jsonify, g, Response, copy_current_request_context

from config import OLLAMA_URL, OLLAMA_MODEL, LLM_TIMEOUT, RERANK_PDF_CANDIDATES, RERANK_TOP_K, PROMPT_CHUNK_WORDS, PROMPT_TOP_CHUNK_WORDS, YESNO_MODEL, \
    MULTI_DOC_MARGIN, MULTI_DOC_MIN_SCORE, LLM_PROVIDER, YESNO_API_MODEL
from core.db import log_admin_action
from core.auth import ALLOWED_DOMAIN, authenticate_admin, EVAL_USER_EMAIL, EVAL_STAFF_EMAIL
from core.session import session_email
from services.conversation_service import (append_exchange, list_conversations, delete_conversation,
                                           rename_conversation, import_conversations, valid_id)
from services.rag_service import (
    create_embedding, check_semantic_cache, add_to_semantic_cache, compress_chunk, keyword_search, reciprocal_rank_fusion,
    semantic_search, rerank_chunks, create_enhanced_prompt, pick_pdf_by_reranker,
    BITNET_CPP_AVAILABLE, run_bitnet_inference,
    clean_excerpt_references, clean_answer, remove_citations_from_text
)
from services.eval_service import record_answer_eval
from services.security_service import validate_query, resolve_role, mask_pii, write_audit, BLOCKED_REPLY
from services.llm_service import generate_answer, provider_label
from services.prompt_service import build_prompt_parts
from services.query_service import clean_history, needs_rewrite, rewrite_followup, is_yes_no_question
from services.tracing import start_trace, finish_trace, add_span
from middleware.db_middleware import (
    save_chat_record, update_chat_satisfaction, save_user_feedback,
    reset_all_database_records, fetch_all_policies
)

chat_bp = Blueprint('chat', __name__)

_installed_models = {'checked_at': 0.0, 'names': set()}


def _model_installed(name):
    """Cached check against Ollama's /api/tags so a missing optional model falls back instead of erroring."""
    if time.time() - _installed_models['checked_at'] > 60:
        try:
            base = OLLAMA_URL.split('/api/')[0]
            tags = requests.get(f"{base}/api/tags", timeout=3).json().get('models', [])
            _installed_models['names'] = {m.get('name', '') for m in tags}
            _installed_models['checked_at'] = time.time()
        except Exception:
            return True  # can't tell - let the real call surface any error
    names = _installed_models['names']
    return name in names or f"{name}:latest" in names


def is_chat_model(name):
    """Embedding and OCR models are installed in Ollama too, but must never be offered as answer models."""
    n = (name or '').lower()
    return not any(k in n for k in ('embed', 'ocr', 'bge', 'minilm', 'rerank'))


def _emit(kind, data):
    """Push a server-sent event when this request is being streamed (/chat/stream); no-op for plain /chat."""
    fn = getattr(g, 'emit', None)
    if fn:
        fn(kind, data)


def _logged_refusal(answer, user_email, question, session_id, start, confidence=0.0):
    """Refusals are answers too: log them so admins and evals can measure abstention behaviour."""
    chat_id = save_chat_record(user_email, question, answer, "[]", time.time() - start, confidence, 'Guardrail')
    metrics = record_answer_eval(chat_id, question, answer, [], confidence)
    return jsonify({"answer": answer, "sources": [], "session_id": session_id, "chat_id": chat_id, "eval": metrics})

@chat_bp.route("/chat", methods=["POST"])
def chat():
    """Every /chat request writes exactly one PII-masked audit row (security layer 4) and one trace, whatever path
    it takes. The trace id is returned so a user-visible answer can be matched to its trace in Admin -> Traces."""
    started = time.time()
    g.audit = {}
    trace_id = start_trace('/chat/stream' if getattr(g, 'emit', None) else '/chat')
    resp = _chat_impl()
    try:
        response = resp[0] if isinstance(resp, tuple) else resp
        status = resp[1] if isinstance(resp, tuple) else response.status_code
        payload = response.get_json(silent=True) or {}
        body = request.get_json(silent=True) or {}
        ev = payload.get('eval') or {}
        decision = g.audit.get('decision') or (
            'error' if status >= 400 else
            'cached' if payload.get('cached') else
            'refused' if ev.get('refused') or not payload.get('sources') else
            'answered')
        write_audit(g.audit.get('email'), g.audit.get('role'), body.get('question'), decision,
                    g.audit.get('reason') or ev.get('verdict'), payload.get('sources'),
                    g.audit.get('top_score'), g.audit.get('model'), time.time() - started)
        finish_trace(status, decision, g.audit.get('role'), g.audit.get('model') or payload.get('model'))
        if isinstance(payload, dict) and status < 400 and response.is_json:
            payload['trace_id'] = trace_id
            response.set_data(json.dumps(payload))
            # conversation history (what the sidebar shows after refresh / sign-in): the user's own question as typed
            email = g.audit.get('email')
            if email and email not in (EVAL_USER_EMAIL, EVAL_STAFF_EMAIL) and payload.get('answer'):
                try:
                    append_exchange(email, body.get('session_id') or payload.get('session_id'), body.get('question'),
                                    payload.get('answer'), {**payload, 'fetch_time': round(time.time() - started, 2)})
                except Exception as e:
                    print(f"⚠️ [History] could not save the conversation: {e}")
    except Exception as e:
        print(f"⚠️ [Audit] could not record chat request: {e}")
    return resp


@chat_bp.route("/chat/stream", methods=["POST"])
def chat_stream():
    """Same pipeline as /chat, streamed as Server-Sent Events:
         event: stage  {"stage": "searching" | "ranking" | "rewriting" | "writing", ...}
         event: token  "<text delta>"                      (only while the LLM is generating)
         event: done   {"status": 200, "payload": <the /chat JSON>}   (final answer after guardrails + evals)
    The pipeline runs in a worker thread with a copy of the request context (auth, audit and evals all apply);
    the generator only forwards queued events, so a slow LLM never blocks the HTTP worker from flushing."""
    events = queue.Queue()

    @copy_current_request_context
    def worker():
        g.emit = lambda kind, data: events.put((kind, data))
        try:
            resp = chat()
            response = resp[0] if isinstance(resp, tuple) else resp
            status = resp[1] if isinstance(resp, tuple) else response.status_code
            events.put(('done', {'status': status, 'payload': response.get_json(silent=True) or {}}))
        except Exception as e:
            traceback.print_exc()
            events.put(('done', {'status': 500, 'payload': {'answer': f'Error: {e}', 'sources': []}}))
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        yield ': stream open\n\n'
        while True:
            item = events.get()
            if item is None:
                break
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _chat_impl():
    try:
        chat_process_start = time.time()
        data = request.json
        question = data.get("question", "")
        if not question or len(question.strip()) < 2:
            return jsonify({
                "answer": "I apologize, but I couldn't understand your question. Please provide a more specific query related to university policies.",
                "sources": [],
                "session_id": data.get("session_id", str(uuid.uuid4())),
                "chat_id": 0
            })
        
        user_email = session_email()
        session_id = data.get("session_id", str(uuid.uuid4()))
        bypass_cache = bool(data.get("bypass_cache"))
        timings = {}
        
        if not user_email:
            g.audit.update(decision='unauthenticated')
            return jsonify({"error": "Your session has expired. Please sign in again.", "auth": "required"}), 401
        g.audit['email'] = user_email

        if not user_email.lower().endswith(f"@{ALLOWED_DOMAIN}"):
            return jsonify({"error": f"Only @{ALLOWED_DOMAIN} emails allowed"}), 403

        # Security layer 2: role is resolved server-side and scopes retrieval, cache and policy listings.
        role = resolve_role(user_email)
        g.audit['role'] = role

        # Security layer 1: validate the query BEFORE it can reach the retriever or the LLM.
        ok, category, detail = validate_query(question)
        if not ok:
            print(f"  🛡️ Blocked query ({category}): {mask_pii(detail or '')}")
            g.audit.update(decision='blocked', reason=f"{category}: {detail}")
            return _logged_refusal(BLOCKED_REPLY, user_email, question, session_id, chat_process_start)
        
        print(f"\n" + "="*60)
        print(f"💬 CHAT REQUEST")
        print(f"="*60)
        print(f"👤 User: {mask_pii(user_email)} (role: {role})")
        print(f"❓ Question: {mask_pii(question)}")
        
        question_lower = question.lower().strip()

        greetings = ["hi", "hello", "hey", "good morning", "good afternoon", "good evening", "how are you", "who are you"]
        is_greeting = any(question_lower == g or question_lower.startswith(g + " ") for g in greetings)
        
        if is_greeting:
            g.audit['decision'] = 'smalltalk'
            return jsonify({
                "answer": "Hello! I am your **NormBridge AI** assistant. I can help you find specific information, rules, and guidelines from SRM University AP's official policy documents. How can I assist you with university policies today?",
                "sources": [],
                "session_id": session_id,
                "chat_id": 0
            })

        # Multi-turn memory: rewrite context-dependent follow-ups into standalone questions before retrieval.
        rewritten_from, rewrite_method = None, None
        history = clean_history(data.get("history"))
        if needs_rewrite(question, history):
            stage = time.time()
            _emit('stage', {'stage': 'rewriting'})
            standalone, method = rewrite_followup(question, history,
                                                  lambda msgs: generate_answer(None, OLLAMA_MODEL, messages=msgs))
            timings['rewrite'] = round(time.time() - stage, 2)
            add_span('rewrite', stage, method=method, history_turns=len(history))
            if standalone != question:
                ok, category, detail = validate_query(standalone)
                if not ok:
                    g.audit.update(decision='blocked', reason=f"{category} (after rewrite): {detail}")
                    return _logged_refusal(BLOCKED_REPLY, user_email, question, session_id, chat_process_start)
                print(f"🧭 Follow-up rewritten ({method}): {mask_pii(standalone)}")
                rewritten_from, question = question, standalone
                rewrite_method = method
                question_lower = question.lower().strip()

        OFF_TOPIC_SIGNALS = [
            "capital of", "longest river", "tallest mountain", "largest country",
            "population of", "currency of", "language spoken in",
            "square root", "square root of", "what is \u03c0", "calculate", "solve for",
            "derivative of", "integral of", "factorial",
            "write a python", "write a script", "write code", "python script",
            "javascript code", "html code", "sql query", "program to",
            "oscar", "emmy", "grammy", "who won", "box office", "movie", "film release",
            "singer", "actor", "celebrity", "top chart",
            "how to bake", "recipe for", "cook a", "calories in", "bake at",
            "speed of light", "distance from earth", "diameter of", "age of the universe",
            "boiling point", "melting point",
            "who invented", "when was discovered", "president of", "prime minister of",
            "world war", "cold war", "year of independence",
            "meaning of life", "what is consciousness", "free will", "postmodernism",
        ]
        POLICY_SAFE_WORDS = [
            "policy", "srm", "university", "student", "attendance", "leave",
            "conduct", "harassment", "abuse", "logo", "email", "campus",
            "faculty", "department", "dean", "exam", "semester", "fee",
            "internship", "grant", "council", "ragging", "hostel", "library",
        ]
        has_off_topic_signal = any(sig in question_lower for sig in OFF_TOPIC_SIGNALS)
        has_policy_context = any(kw in question_lower for kw in POLICY_SAFE_WORDS)

        if has_off_topic_signal and not has_policy_context:
            print(f"  🚫 Off-topic guard triggered: '{question_lower[:80]}'")
            return _logged_refusal(
                "I apologize, but I can only answer questions related to university policy documents. Please ask me about SRM University AP policies, rules, or guidelines.",
                user_email, question, session_id, chat_process_start)

        list_keywords = ["what policies", "which policies", "list of policies", "available policies", "all policies", "collection of policies", "show policies", "policies available", "tell me everything you know", "what do you know"]
        has_list_keyword = any(question_lower == k or question_lower == k + "?" for k in list_keywords)
        has_list_verb = any(v in question_lower for v in ["list", "show", "tell me", "display"])
        has_policy_noun = any(n in question_lower for n in ["policy", "policies", "documents"])
        
        list_intent = has_list_keyword or \
                      (has_policy_noun and question_lower in ["how many", "how many?", "how many are there", "how many are there?"]) or \
                      (has_list_verb and has_policy_noun and len(question_lower.split()) <= 5) or \
                      (question_lower.strip('?') in ["what are the policies", "list policies", "show policies", "what do you know", "what policies do you have"])
        
        if list_intent:
            policies = fetch_all_policies(role)
            g.audit['decision'] = 'listed'
            count = len(policies)
            
            if count == 0:
                answer = "I currently don't have access to any policy documents."
            else:
                policy_list = "\n".join([f"• {p['name']} ({p['policy_type'] or 'General'})" for p in policies])
                if "how many" in question_lower:
                    answer = f"I currently have access to {count} policy documents:\n\n{policy_list}"
                else:
                    answer = f"I have access to the following {count} policy documents:\n\n{policy_list}"
            
            chat_id = save_chat_record(user_email, question, answer, "[]")
            
            return jsonify({
                "answer": answer,
                "sources": [],
                "session_id": session_id,
                "chat_id": chat_id
            })

        else:
            stage = time.time()
            _emit('stage', {'stage': 'searching'})
            question_embedding_bytes = create_embedding(question)
            timings['embed'] = round(time.time() - stage, 2)
            add_span('embed', stage)
            cache_start = time.time()
            cached_answer, cached_sources = (None, None) if bypass_cache else check_semantic_cache(question_embedding_bytes, scope=role)
            add_span('answer_cache', cache_start, hit=bool(cached_answer), bypassed=bypass_cache or None, scope=role)
            
            if cached_answer:
                chat_id = save_chat_record(user_email, question, cached_answer, cached_sources)
                cached_sources_list = json.loads(cached_sources)
                metrics = record_answer_eval(chat_id, question, cached_answer, cached_sources_list)
                return jsonify({
                    "answer": cached_answer,
                    "sources": cached_sources_list,
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "cached": True,
                    "rewritten_question": question if rewritten_from else None,
                    "rewrite_method": rewrite_method,
                    "timings": {**timings, "total": round(time.time() - chat_process_start, 2)},
                    "eval": metrics
                })

            min_relevance_score = 0.22
            stage = time.time()
            results = semantic_search(question, n_results=50, min_score=min_relevance_score, query_embedding=question_embedding_bytes, role=role)
            # Hybrid retrieval: add BM25 keyword hits the dense search missed, then fuse both rankings with
            # Reciprocal Rank Fusion (RRF); the fused order decides which chunks the cross-encoder sees.
            _key = lambda r: (str(r['pdf']), r.get('chunk_index'), (r.get('text') or '')[:60])
            vector_ranking = [_key(r) for r in sorted(results, key=lambda r: float(r['score']), reverse=True)]
            seen = set(vector_ranking)
            kw_added = 0
            kw_results = keyword_search(question, role=role, top_k=6, query_embedding=question_embedding_bytes)
            for r in kw_results:
                key = _key(r)
                if key not in seen and r['score'] >= min_relevance_score:
                    seen.add(key)
                    results.append(r)
                    kw_added += 1
            if kw_added:
                print(f"🔎 Hybrid retrieval: +{kw_added} keyword (BM25) chunks")
            fused = reciprocal_rank_fusion([vector_ranking, [_key(r) for r in kw_results]])
            for r in results:
                r['rrf'] = fused.get(_key(r), 0.0)
            timings['search'] = round(time.time() - stage, 2)
            add_span('retrieve', stage, vector_hits=len(vector_ranking), keyword_hits=len(kw_results), keyword_added=kw_added,
                     documents=len({str(r['pdf']) for r in results}))
            stage = time.time()
            _emit('stage', {'stage': 'ranking'})
            
            if not results:
                return _logged_refusal(
                    "I apologize, but I couldn't find any relevant policy information for your question. I am programmed to only answer questions based on university policy documents. Please try rephrasing your question or ask about a specific policy.",
                    user_email, question, session_id, chat_process_start)

            all_policies = fetch_all_policies(role)
            policy_map = { p['name']: dict(p) for p in all_policies }
            
            category_latest = {}
            for p in all_policies:
                cat = p.get('category_name') or p.get('policy_type') or 'General'
                up_time = str(p.get('uploaded_at') or '')
                if cat not in category_latest or up_time > category_latest[cat]['uploaded_at']:
                    category_latest[cat] = {'name': p['name'], 'uploaded_at': up_time}

            pdf_scores = {}
            pdf_counts = {}
            pdf_total_score = {}
            pdf_best_score = {}
            pdf_chunks = {}
            
            for r in results:
                pdf = str(r['pdf'])
                if pdf not in pdf_scores:
                    pdf_scores[pdf] = []
                    pdf_counts[pdf] = 0
                    pdf_total_score[pdf] = 0.0
                    pdf_best_score[pdf] = 0.0
                    pdf_chunks[pdf] = []
                
                pdf_scores[pdf].append(float(r['score']))
                pdf_counts[pdf] += 1
                pdf_total_score[pdf] += float(r['score'])
                if float(r['score']) > pdf_best_score[pdf]:
                    pdf_best_score[pdf] = float(r['score'])
                pdf_chunks[pdf].append(r)
            
            best_pdf = ""
            best_pdf_score = 0.0
            best_pdf_avg = 0.0
            best_pdf_count = 0
            pdf_combined = {}
            
            print(f"\n📊 PDF STATISTICS (with Recency Priority Boost):")
            for pdf in pdf_counts:
                count = pdf_counts[pdf]
                total = pdf_total_score[pdf]
                best = pdf_best_score[pdf]
                avg_score = total / count if count > 0 else 0.0
                
                count_factor = min(count / 10.0, 1.0) * 0.2
                base_combined_score = (avg_score * 0.4) + (best * 0.4) + count_factor
                
                recency_boost = 0.0
                pmeta = policy_map.get(pdf)
                if pmeta:
                    cat = pmeta.get('category_name') or pmeta.get('policy_type') or 'General'
                    if category_latest.get(cat) and category_latest[cat]['name'] == pdf:
                        recency_boost = 0.15

                combined_score = base_combined_score + recency_boost
                pdf_combined[pdf] = combined_score
                print(f"   • {pdf}: Chunks={count}, Avg={avg_score:.3f}, Best={best:.3f}, RecencyBoost={recency_boost:.2f}, Final={combined_score:.3f}")
                
                if combined_score > best_pdf_score:
                    best_pdf_score = combined_score
                    best_pdf = pdf
                    best_pdf_avg = avg_score
                    best_pdf_count = count
            
            print(f"\n🏆 BEST PDF: {best_pdf} (Combined: {best_pdf_score:.3f})")
            g.audit['top_score'] = round(float(best_pdf_score), 3)
            
            if best_pdf_score < 0.42:
                print(f"⚠️ Refusal: Score {best_pdf_score:.3f} too low")
                return _logged_refusal("I can only answer questions related to SRM University AP policy documents.",
                                       user_email, question, session_id, chat_process_start, best_pdf_score)

            # Doc-level cross-encoder check among the top candidates (superseded versions are excluded so the
            # recency rule still decides between versions of the same policy).
            def _is_superseded(name):
                meta = policy_map.get(name) or {}
                cat = meta.get('category_name') or meta.get('policy_type') or 'General'
                latest = category_latest.get(cat, {}).get('name')
                return latest is not None and latest != name and latest in pdf_combined
            ranked_combined = [p for p, _ in sorted(pdf_combined.items(), key=lambda kv: kv[1], reverse=True)[:RERANK_PDF_CANDIDATES]]
            ranked_best_chunk = [p for p, _ in sorted(pdf_best_score.items(), key=lambda kv: kv[1], reverse=True)[:RERANK_PDF_CANDIDATES]]
            candidates = list(dict.fromkeys(ranked_combined + ranked_best_chunk))  # strongest overall + strongest single evidence
            # the strongest keyword (BM25) document also gets a seat, so exact-term matches can win on the cross-encoder
            kw_top_pdfs = [str(r['pdf']) for r in kw_results[:2] if r['score'] >= min_relevance_score and str(r['pdf']) in pdf_chunks]
            candidates = list(dict.fromkeys(candidates + kw_top_pdfs))
            candidates = [p for p in candidates if not _is_superseded(p)] or candidates
            pdf_rerank = {}
            if len(candidates) > 1:
                reranked_pdf, pdf_rerank = pick_pdf_by_reranker(question, pdf_chunks, candidates)
                if reranked_pdf and reranked_pdf != best_pdf:
                    print(f"🔁 Cross-encoder prefers {reranked_pdf} over {best_pdf}: " + ", ".join(f"{k}={v:.2f}" for k, v in pdf_rerank.items()))
                    best_pdf = reranked_pdf
                    best_pdf_score = pdf_combined[reranked_pdf]
            
            best_chunks = list(pdf_chunks[best_pdf])
            best_chunks.sort(key=lambda x: (x.get('rrf', 0.0), x['score']), reverse=True)
            top_candidates = best_chunks[:10] + [c for c in best_chunks[10:] if c.get('bm25')][:2]
            top_chunks = rerank_chunks(question, top_candidates, top_k=RERANK_TOP_K)

            # Multi-document answers: when another (non-superseded) policy is almost as relevant as the best one on the
            # cross-encoder, its best chunk is ADDED (never replaces evidence from the best document: replacing the
            # weakest chunk dropped the answer in phd-02), so questions that two policies answer or contradict get both.
            # The prompt labels every excerpt with its document.
            used_pdfs = [best_pdf]
            if MULTI_DOC_MARGIN > 0 and pdf_rerank and best_pdf in pdf_rerank:
                runner_up = max(((p, s) for p, s in pdf_rerank.items() if p != best_pdf), key=lambda kv: kv[1], default=None)
                if runner_up and runner_up[1] >= MULTI_DOC_MIN_SCORE and runner_up[1] >= pdf_rerank[best_pdf] - MULTI_DOC_MARGIN:
                    second = sorted(pdf_chunks[runner_up[0]], key=lambda x: (x.get('rrf', 0.0), x['score']), reverse=True)[:6]
                    extra = rerank_chunks(question, second, top_k=1)
                    if extra:
                        top_chunks = top_chunks + extra
                        used_pdfs.append(runner_up[0])
                        print(f"📚 Multi-document answer: + {runner_up[0]} (ce {runner_up[1]:.2f} vs {pdf_rerank[best_pdf]:.2f})")
            timings['rerank'] = round(time.time() - stage, 2)
            add_span('rerank', stage, candidate_docs=len(candidates), chosen=best_pdf, documents_used=len(used_pdfs),
                     chunks=len(top_chunks), ce_best=round(pdf_rerank.get(best_pdf, 0.0), 2) if pdf_rerank else None)
            
            doc_id_triggers = [
                "in which policy", "which policy", "which document", "in which document",
                "where is the policy", "what policy document", "where can i find the policy",
                "in what policy", "what document contains", "which file contains", "in which file",
                "in which policy is", "which policy contains", "which document has"
            ]
            is_doc_id_intent = any(trigger in question_lower for trigger in doc_id_triggers)
            if is_doc_id_intent:
                print(f"🎯 Document Identification Intent detected for: '{question}'")

            sources = []
            for r in top_chunks:
                sources.append({
                    "pdf": r['pdf'],
                    "page": r['page'],
                    "similarity": r['score'],
                    "relevance": r.get('final_score', r['score']),
                    "text": r['text'],
                    "section": r.get('section', 'General'),
                    "text_snippet": r['text'][:200] + "..." if len(r['text']) > 200 else r['text']
                })
            
            # Contextual compression: the LLM only reads the most question-relevant sentences of each chunk
            # (the full chunks are still returned as evidence and used by the evals). The top-ranked chunk gets a
            # bigger budget because it usually holds the answer (golden-set fact retention 34/38 -> 38/38).
            prompt_chunks = [dict(c, text=compress_chunk(question, c['text'], PROMPT_TOP_CHUNK_WORDS if i == 0 else PROMPT_CHUNK_WORDS))
                             for i, c in enumerate(top_chunks)] if PROMPT_CHUNK_WORDS > 0 else top_chunks
            prompt = create_enhanced_prompt(question, prompt_chunks, best_pdf, is_doc_id_intent=is_doc_id_intent)
            
            if prompt is None or str(prompt).startswith('__LOW_CONFIDENCE__'):
                parts = str(prompt or '').split('::')
                policy_name = parts[1] if len(parts) > 1 else best_pdf
                section_hint = parts[2] if len(parts) > 2 else 'the relevant section'
                answer = (
                    f"I found relevant documents in **{policy_name}**, but my confidence "
                    f"in generating an accurate answer is too low for this specific question. "
                    f"Please refer directly to **{section_hint}** in that document for the "
                    f"most accurate information."
                )
                return _logged_refusal(answer, user_email, question, session_id, chat_process_start, best_pdf_score)
            
            try:
                selected_model = (data.get("model") or OLLAMA_MODEL).strip()
                
                if "bitnet" in selected_model.lower():
                    model_used_label = "BitNet 1.58b"
                    if BITNET_CPP_AVAILABLE:
                        system_prompt = (
                            "You are the official NormBridge AI for SRM University AP. "
                            "You must answer user questions based STRICTLY on the provided policy context, "
                            "and never invent or hallucinate rules. Be clear, concise, and professional."
                        )
                        user_prompt = f"Context from {best_pdf}:\n"
                        for c in top_chunks[:3]:
                            user_prompt += f"{c['text']}\n"
                        user_prompt += f"\nQuestion: {question}"
                        
                        print(f"  [BitNet] Running REAL BitNet inference via bitnet.cpp/llama-server")
                        try:
                            bitnet_answer = run_bitnet_inference(
                                system_prompt, user_prompt, max_tokens=300, temperature=0.1
                            )
                            if bitnet_answer:
                                answer = clean_excerpt_references(bitnet_answer)
                                answer = clean_answer(answer, best_pdf)
                                answer = remove_citations_from_text(answer)
                                
                                duration2 = time.time() - chat_process_start
                                confidence2 = best_pdf_score if 'best_pdf_score' in locals() else 0.0
                                chat_id2 = save_chat_record(
                                    user_email, question, answer, 
                                    json.dumps(sources) if 'sources' in locals() else '[]',
                                    duration2, confidence2, model_used_label
                                )
                                return jsonify({"answer": answer, "sources": sources if 'sources' in locals() else [], "session_id": session_id, "chat_id": chat_id2})
                            else:
                                print("  [BitNet] Empty response, falling back to the default model")
                                model = OLLAMA_MODEL
                        except Exception as bn_err:
                            print(f"  [BitNet] Error: {bn_err} — falling back to the default model")
                            model = OLLAMA_MODEL
                    else:
                        model = OLLAMA_MODEL
                        model_used_label = OLLAMA_MODEL
                elif selected_model and selected_model != OLLAMA_MODEL and _model_installed(selected_model) and is_chat_model(selected_model):
                    # any installed Ollama chat model the user picked in the composer's model menu
                    model = selected_model
                    model_used_label = selected_model
                else:
                    model = OLLAMA_MODEL
                    model_used_label = OLLAMA_MODEL
                    # Query routing: small models have a measured "yes" bias on yes/no questions (1.5B: 3/7 verdicts
                    # right, 3B: 6/7), so only those go to the bigger model; streaming hides the extra seconds.
                    # Cloud (LLM_PROVIDER=openai): route to YESNO_API_MODEL on the same API instead.
                    if LLM_PROVIDER == 'openai':
                        if YESNO_API_MODEL and is_yes_no_question(question):
                            model = f"api:{YESNO_API_MODEL}"
                            model_used_label = f"{YESNO_API_MODEL} (yes/no route)"
                            print(f"🧭 Routed yes/no question to {YESNO_API_MODEL} (API)")
                    elif YESNO_MODEL and YESNO_MODEL != OLLAMA_MODEL and is_yes_no_question(question) \
                            and _model_installed(YESNO_MODEL) and is_chat_model(YESNO_MODEL):
                        model = YESNO_MODEL
                        model_used_label = f"{YESNO_MODEL} (yes/no route)"
                        print(f"🧭 Routed yes/no question to {YESNO_MODEL}")

                stage = time.time()
                _emit('stage', {'stage': 'writing', 'model': model_used_label, 'documents': used_pdfs})
                system_msg, user_msg = build_prompt_parts(best_pdf, None, prompt_chunks, question, is_doc_id_intent=is_doc_id_intent)
                first_token = {}

                def _on_token(t):
                    first_token.setdefault('at', time.time())
                    _emit('token', t)
                ok, raw_ans = generate_answer(prompt, model, messages=[{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': user_msg}],
                                              on_token=_on_token if getattr(g, 'emit', None) else None)
                timings['llm'] = round(time.time() - stage, 2)
                add_span('llm', stage, model=model, ok=ok, streamed=bool(first_token) or None,
                         ttft_ms=round((first_token['at'] - stage) * 1000) if first_token else None,
                         prompt_chars=len(system_msg) + len(user_msg), answer_chars=len(raw_ans or ''))
                model_used_label = provider_label(model_used_label)
                g.audit['model'] = model_used_label
                
                if not ok:
                    answer = raw_ans
                else:
                    if not raw_ans:
                        answer = "I'm sorry, I found the relevant documents but I'm unable to summarize the answer right now. Please check the 'Evidence' panel below for the specific policy details."
                    else:
                        answer = raw_ans
                
                answer = clean_excerpt_references(answer)
                answer = clean_answer(answer, used_pdfs)
                answer = remove_citations_from_text(answer)

                if is_doc_id_intent and best_pdf:
                    if best_pdf.lower() not in answer.lower():
                        answer = f"The requested information is provided in the policy document **{best_pdf}**.\n\n{answer}"
                
                refusal_phrases = [
                    "i can only answer questions related to srm university ap",
                    "i am programmed to only answer questions",
                    "i apologize, but this information is not mentioned",
                    "i cannot assist with",
                    "this information is not mentioned in the provided"
                ]
                if any(phrase in answer.lower() for phrase in refusal_phrases):
                    sources = []
                
            except Exception as e:
                print(f"⚠️ LLM error: {e}")
                answer = "I apologize, but I am having trouble processing your request right now. Please try again in a moment."
        
        duration = time.time() - chat_process_start
        confidence = best_pdf_score if 'best_pdf_score' in locals() else 0.0
        model_label = model_used_label if 'model_used_label' in locals() else 'Phi-3 Mini'
        
        chat_id = save_chat_record(
            user_email, question, answer, 
            json.dumps(sources) if 'sources' in locals() else '[]',
            duration, confidence, model_label
        )
        eval_start = time.time()
        metrics = record_answer_eval(chat_id, question, answer, sources if 'sources' in locals() else [], confidence)
        add_span('evaluate', eval_start, verdict=(metrics or {}).get('verdict'), groundedness=(metrics or {}).get('groundedness'))
        
        if not bypass_cache and "answer" in locals() and "sources" in locals() and sources and \
           "I apologize" not in answer and "I can only answer questions" not in answer:
            try:
                sources_json = json.dumps(sources)
                add_to_semantic_cache(question, question_embedding_bytes, answer, sources_json, scope=role)
            except Exception as e:
                print(f"⚠️ Failed to cache: {e}")
        
        return jsonify({
            "answer": answer,
            "sources": sources if 'sources' in locals() else [],
            "session_id": session_id,
            "chat_id": chat_id,
            "eval": metrics,
            "model": model_used_label if 'model_used_label' in locals() else None,
            "rewritten_question": question if rewritten_from else None,
            "rewrite_method": rewrite_method,
            "timings": {**timings, "total": round(time.time() - chat_process_start, 2)}
        })
        
    except Exception as e:
        print(f"\n❌ CHAT ERROR: {e}")
        traceback.print_exc()
        return jsonify({"answer": f"Error: {str(e)}", "sources": []}), 500

@chat_bp.route("/chat/<int:chat_id>/satisfaction", methods=["POST"])
def update_satisfaction(chat_id):
    try:
        if not session_email():
            return jsonify({"error": "Please sign in again", "auth": "required"}), 401
        data = request.json
        satisfaction = data.get("satisfaction")
        
        if satisfaction is None:
            return jsonify({"error": "Satisfaction value required"}), 400
            
        update_chat_satisfaction(chat_id, satisfaction)
        return jsonify({"success": True, "message": "Satisfaction recorded"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@chat_bp.route("/feedback", methods=["POST"])
def submit_feedback():
    try:
        data = request.json
        user_email = session_email()
        stars = data.get("stars")
        review = data.get("review", "")
        
        if not user_email:
            return jsonify({"error": "Please sign in again", "auth": "required"}), 401
        if not stars:
            return jsonify({"error": "Email and stars required"}), 400
            
        save_user_feedback(user_email, stars, review)
        return jsonify({"success": True, "message": "Feedback recorded"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@chat_bp.route('/reset', methods=['POST'])
@authenticate_admin
def reset_database():
    reset_all_database_records()
    admin_user = request.authorization.username if request.authorization else "Admin"
    log_admin_action(admin_user, "RESET", "Database has been reset (policies, embeddings, chat history)")
    return jsonify({'success': True})


# ----------------------------------------------------------------------------- conversation history (owner only)
def _owner_or_401():
    email = session_email()
    return email, (None if email else (jsonify({"error": "Please sign in again", "auth": "required"}), 401))


@chat_bp.route('/conversations', methods=['GET'])
def get_conversations():
    """The signed-in user's conversations with their messages, newest first (what the sidebar shows)."""
    email, err = _owner_or_401()
    if err:
        return err
    return jsonify({'conversations': list_conversations(email)})


@chat_bp.route('/conversations/import', methods=['POST'])
def import_local_conversations():
    """One-time upload of chats that so far only existed in this browser (idempotent)."""
    email, err = _owner_or_401()
    if err:
        return err
    sessions = (request.get_json(silent=True) or {}).get('sessions') or []
    if not isinstance(sessions, list):
        return jsonify({'error': 'sessions must be a list'}), 400
    return jsonify({'imported': import_conversations(email, sessions)})


@chat_bp.route('/conversations/<conversation_id>', methods=['DELETE', 'PATCH'])
def modify_conversation(conversation_id):
    email, err = _owner_or_401()
    if err:
        return err
    if not valid_id(conversation_id):
        return jsonify({'error': 'invalid conversation id'}), 400
    if request.method == 'DELETE':
        return jsonify({'deleted': delete_conversation(email, conversation_id)})
    title = (request.get_json(silent=True) or {}).get('title')
    return jsonify({'renamed': rename_conversation(email, conversation_id, title)})

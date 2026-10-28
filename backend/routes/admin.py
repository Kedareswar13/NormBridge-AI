from flask import Blueprint, request, jsonify, Response

from core.db import log_admin_action
from core.auth import authenticate_admin, ADMIN_EMAIL_MAPPINGS, EVAL_USER_EMAIL, EVAL_STAFF_EMAIL
from config import OLLAMA_MODEL, LLM_PROVIDER, LLM_API_MODEL
from services import eval_service
from middleware.business_layer import (
    fetch_admin_dashboard_stats,
    fetch_admin_analytics_data,
    fetch_admin_model_metrics,
    fetch_admin_users,
    fetch_admin_chats,
    fetch_admin_user_chats,
    delete_user_and_history
)

admin_bp = Blueprint('admin', __name__)


def _excluded_emails():
    """Admin and synthetic eval traffic never counts towards user-facing analytics."""
    return list(ADMIN_EMAIL_MAPPINGS.values()) + [EVAL_USER_EMAIL, EVAL_STAFF_EMAIL]


@admin_bp.route('/admin/stats', methods=['GET'])
@authenticate_admin
def admin_stats():
    stats = fetch_admin_dashboard_stats(_excluded_emails())
    return jsonify(stats)

@admin_bp.route('/admin/analytics', methods=['GET'])
@authenticate_admin
def admin_analytics():
    analytics = fetch_admin_analytics_data(_excluded_emails())
    return jsonify(analytics)

@admin_bp.route('/admin/model-metrics', methods=['GET'])
@authenticate_admin
def admin_model_metrics():
    """Return per-model evaluation metrics plus system-wide averages using business layer."""
    metrics = fetch_admin_model_metrics(_excluded_emails())
    try:
        live = eval_service.live_eval_summary(limit=500, exclude_emails=_excluded_emails())
        metrics.setdefault('overall', {})['groundedness'] = live.get('avg_groundedness')
    except Exception as e:
        print(f"⚠️ live eval summary failed: {e}")
    return jsonify(metrics)

@admin_bp.route('/admin/users', methods=['GET'])
@authenticate_admin
def admin_users():
    """Return list of non-admin verified users using business layer."""
    users = fetch_admin_users(_excluded_emails())
    return jsonify(users)

@admin_bp.route('/admin/users/<email>', methods=['DELETE'])
@authenticate_admin
def admin_delete_user(email):
    """Delete user and user chat history using business layer."""
    delete_user_and_history(email)
    log_admin_action(request.authorization.username if request.authorization else "Admin", "DELETE_USER", f"Deleted user: {email}")
    return jsonify({'success': True})

@admin_bp.route('/admin/chats', methods=['GET'])
@authenticate_admin
def admin_chats():
    """Return recent global chats using business layer."""
    chats = fetch_admin_chats()
    return jsonify({'chats': chats})

@admin_bp.route('/admin/chats/user/<email>', methods=['GET'])
@authenticate_admin
def admin_user_chats(email):
    """Return chats for a specific user using business layer."""
    chats = fetch_admin_user_chats(email)
    return jsonify({'chats': chats})


# ----------------------------------------------------------------------------- security
@admin_bp.route('/admin/security/audit', methods=['GET'])
@authenticate_admin
def admin_security_audit():
    """RAG audit trail: who (masked) asked what (masked), when, under which role, decision and which pages were used."""
    from services.security_service import audit_summary
    limit = min(int(request.args.get('limit', 100)), 1000)
    return jsonify(audit_summary(limit))


# ----------------------------------------------------------------------------- tracing
@admin_bp.route('/admin/traces', methods=['GET'])
@authenticate_admin
def admin_traces():
    """Recent request traces (stage spans, no question text) + p50/p95 per stage."""
    from services.tracing import list_traces, stage_percentiles
    traces = list_traces(min(int(request.args.get('limit', 50)), 500))
    return jsonify({'traces': traces, 'stages': stage_percentiles(traces)})


# ----------------------------------------------------------------------------- cache
@admin_bp.route('/admin/cache', methods=['GET'])
@authenticate_admin
def admin_cache_stats():
    from services.rag_service import cache_overview
    return jsonify(cache_overview())

@admin_bp.route('/admin/cache/clear', methods=['POST'])
@authenticate_admin
def admin_cache_clear():
    from services.rag_service import clear_semantic_cache
    clear_semantic_cache("cleared by admin")
    log_admin_action(request.authorization.username if request.authorization else "Admin", "CACHE_CLEAR", "Cleared the semantic answer cache")
    return jsonify({'success': True})


# ----------------------------------------------------------------------------- evals
@admin_bp.route('/admin/evals/live', methods=['GET'])
@authenticate_admin
def admin_evals_live():
    """Deterministic quality scores of real user answers (groundedness, numeric faithfulness, abstention)."""
    limit = min(int(request.args.get('limit', 200)), 2000)
    return jsonify(eval_service.live_eval_summary(limit=limit, exclude_emails=_excluded_emails()))

@admin_bp.route('/admin/evals/golden', methods=['GET'])
@authenticate_admin
def admin_evals_golden_set():
    return jsonify(eval_service.load_golden_set())

@admin_bp.route('/admin/evals/runs', methods=['GET'])
@authenticate_admin
def admin_evals_runs():
    return jsonify({'runs': eval_service.list_runs()})

@admin_bp.route('/admin/evals/runs', methods=['POST'])
@authenticate_admin
def admin_evals_start_run():
    body = request.get_json(silent=True) or {}
    admin = request.authorization.username if request.authorization else 'Admin'
    try:
        run_id = eval_service.start_golden_run(model=body.get('model') or (LLM_API_MODEL if LLM_PROVIDER == 'openai' else OLLAMA_MODEL), triggered_by=admin,
                                               user_email=EVAL_USER_EMAIL, case_ids=body.get('case_ids'),
                                               dataset='heldout' if body.get('dataset') == 'heldout' else 'golden',
                                               notes=body.get('notes'))
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 409
    log_admin_action(admin, "EVAL_RUN", f"Started golden-set eval run #{run_id}")
    return jsonify({'run_id': run_id}), 202


@admin_bp.route('/admin/evals/history', methods=['GET'])
@authenticate_admin
def admin_evals_history():
    """Case x run pass/fail matrix (eval-driven development view)."""
    return jsonify(eval_service.case_history('heldout' if request.args.get('dataset') == 'heldout' else 'golden'))


@admin_bp.route('/admin/evals/compare', methods=['GET'])
@authenticate_admin
def admin_evals_compare():
    """Fixed / regressed / still-failing cases between two runs, with the answers before and after."""
    try:
        a, b = int(request.args.get('a')), int(request.args.get('b'))
    except (TypeError, ValueError):
        return jsonify({'error': 'a and b must be run ids'}), 400
    out = eval_service.compare_runs(a, b)
    return (jsonify(out), 200) if out else (jsonify({'error': 'run not found'}), 404)


@admin_bp.route('/admin/evals/runs/<int:run_id>', methods=['PATCH'])
@authenticate_admin
def admin_evals_annotate(run_id):
    """Record what changed before this run (shown on the timeline)."""
    ok = eval_service.set_run_notes(run_id, (request.get_json(silent=True) or {}).get('notes'))
    return (jsonify({'updated': True}), 200) if ok else (jsonify({'error': 'run not found'}), 404)

@admin_bp.route('/admin/evals/runs/<int:run_id>', methods=['GET'])
@authenticate_admin
def admin_evals_run_detail(run_id):
    run = eval_service.get_run(run_id)
    if not run:
        return jsonify({'error': 'Run not found'}), 404
    if request.args.get('format') == 'md':
        return Response(eval_service.export_run_markdown(run), mimetype='text/markdown',
                        headers={'Content-Disposition': f'attachment; filename=normbridge-eval-run-{run_id}.md'})
    return jsonify(run)

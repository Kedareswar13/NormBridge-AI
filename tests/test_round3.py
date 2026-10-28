"""Unit tests for this round's additions (pure functions, no DB/LLM): conversation history helpers, trace
percentiles, yes/no routing detection, held-out fact derivation and the cloud model route."""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(ROOT, 'backend'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ.setdefault('SKIP_WARMUP', '1')

from services.conversation_service import valid_id, _title_from, _meta  # noqa: E402
from services.tracing import stage_percentiles  # noqa: E402
from services.query_service import is_yes_no_question  # noqa: E402
from make_heldout_set import facts_from_span  # noqa: E402


# ---------------------------------------------------------------- conversation history
def test_conversation_ids_are_validated():
    assert valid_id('1761573000123') and valid_id('conv-abc_1.2:3')
    assert not valid_id('') and not valid_id(None)
    assert not valid_id("1'; DROP TABLE conversations; --") and not valid_id('x' * 65)


def test_title_is_first_question_trimmed():
    assert _title_from('  What   is the fine? ') == 'What is the fine?'
    assert _title_from('q' * 80).endswith('...') and len(_title_from('q' * 80)) == 60
    assert _title_from('') == 'New Chat'


def test_only_known_meta_is_stored():
    m = _meta({'sources': list(range(20)), 'eval': {'verdict': 'pass'}, 'answer': 'dup', 'user_email': 'x@y', 'model': None})
    assert set(m) == {'sources', 'eval'} and len(m['sources']) == 8   # no answer copy, no email, no Nones


# ---------------------------------------------------------------- tracing
def test_stage_percentiles():
    traces = [{'spans': [{'name': 'llm', 'duration_ms': d}, {'name': 'embed', 'duration_ms': 10}]} for d in (100, 200, 300, 400, 5000)]
    p = stage_percentiles(traces)
    assert p['llm']['count'] == 5 and p['llm']['p50_ms'] == 300 and p['llm']['p95_ms'] == 5000
    assert p['embed']['p50_ms'] == 10


# ---------------------------------------------------------------- query routing
def test_yes_no_detection():
    for q in ('Can I stay in the hostel during semester breaks?', 'Is the caution deposit refundable?',
              'Does forcing a fresher to do my assignments count as ragging?', 'Will a medical certificate be accepted?'):
        assert is_yes_no_question(q), q
    for q in ('What is the library fine?', 'How many days do I have to appeal?', 'Who approves student email groups?'):
        assert not is_yes_no_question(q), q


def test_cloud_route_label_survives_provider_label(monkeypatch):
    import services.llm_service as llm
    monkeypatch.setattr(llm, 'LLM_PROVIDER', 'openai')
    monkeypatch.setattr(llm, 'LLM_API_MODEL', 'llama-3.1-8b-instant')
    assert llm.provider_label('qwen2.5:1.5b') == 'llama-3.1-8b-instant (API)'
    assert llm.provider_label('llama-3.3-70b-versatile (yes/no route)') == 'llama-3.3-70b-versatile (yes/no route)'


# ---------------------------------------------------------------- held-out set: deterministic expected facts
def test_heldout_facts_prefer_specific_signals():
    assert facts_from_span('9:30 PM', 'By when must I return?') == [['9:30']]
    assert ['10 pm', '10:00'] == [a for a in facts_from_span('10:00 PM and 6:00 AM', 'Quiet hours?')[0] if a in ('10 pm', '10:00')]
    assert facts_from_span('INR 1000', 'Maximum fine?') == [['inr 1000', '1000']]
    assert facts_from_span('seven working days', 'How long to reply?') == [['seven', '7']]
    assert facts_from_span('2 to 4', 'How many members?') == [['2 to 4', '2-4']]
    assert facts_from_span('office of the Director, Communications', 'Whom to consult?')[0][0].startswith('communicatio')


# ---------------------------------------------------------------- eval-driven fixes view
from services.eval_service import case_progress  # noqa: E402

RUNS = [1, 2, 3, 4, 9, 10, 12, 15, 16]


def test_fixed_in_is_the_run_after_the_last_failure():
    # ragging-02 pattern: fails for many runs, passes from #10 on
    r = {'1': False, '2': True, '3': False, '4': False, '9': False, '10': True, '12': True, '15': True, '16': True}
    p = case_progress(r, RUNS)
    assert p == {'ever_failed': True, 'now_passing': True, 'first_failed_run': 1, 'fixed_in_run': 10}


def test_flaky_case_counts_from_the_last_failure_and_subset_runs_are_skipped():
    # fee-03 pattern: absent from subset run #4, fails again in #15, so the fix that stuck is #16
    r = {'1': True, '2': True, '3': True, '9': False, '10': True, '12': True, '15': False, '16': True}
    assert case_progress(r, RUNS)['fixed_in_run'] == 16


def test_open_and_never_failed_cases():
    assert case_progress({'12': True, '15': False, '16': False}, RUNS) == \
        {'ever_failed': True, 'now_passing': False, 'first_failed_run': 15, 'fixed_in_run': None}
    assert case_progress({'1': True, '16': True}, RUNS)['ever_failed'] is False

"""Unit tests for the deterministic evaluator (no DB, no LLM).

Run:  backend\\.venv\\Scripts\\python -m pytest tests -q      (from the repo root)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from services.eval_service import evaluate_answer, score_golden_case, is_refusal, numbers_in  # noqa: E402

SOURCES = [{'pdf': 'Students-Attendance-Policy.pdf', 'text': 'To appear in the End Semester Examination in a course, a student should have a '
            'minimum of 75 % attendance. No appeal is considered for students below 60% attendance.'}]


def test_grounded_answer_passes():
    m = evaluate_answer('What is the minimum attendance?', 'A student should have a minimum of 75% attendance to appear in the End Semester Examination.', SOURCES)
    assert m['groundedness'] >= 0.9
    assert m['numeric_faithfulness'] == 1.0
    assert m['verdict'] == 'pass'


def test_hallucinated_number_is_caught():
    m = evaluate_answer('What is the minimum attendance?', 'A student should have a minimum of 85% attendance to appear in the examination.', SOURCES)
    assert m['unsupported_numbers'] == ['85']
    assert m['numeric_faithfulness'] == 0.0
    assert m['verdict'] == 'fail'


def test_unsupported_sentence_lowers_groundedness():
    m = evaluate_answer('Minimum attendance?', 'Students need 75% attendance. Parking stickers are issued by the transport office every Monday morning.', SOURCES)
    assert m['groundedness'] < 0.8
    assert any('Parking' in s for s in m['unsupported_sentences'])


def test_answer_without_sources_fails():
    m = evaluate_answer('Minimum attendance?', 'You need 75% attendance.', [])
    assert m['verdict'] == 'fail'


def test_refusal_detection():
    assert is_refusal('I apologize, but I can only answer questions related to university policy documents.')
    assert is_refusal('This information is not mentioned in the provided policy excerpts.')
    assert not is_refusal('You need 75% attendance.')
    assert evaluate_answer('capital of France?', 'I can only answer questions related to SRM University AP policy documents.', [])['verdict'] == 'abstained'


def test_hedged_answer_is_not_a_refusal():
    hedged = ('The tuition fee refund policy is as per UGC norms. However, the specific details of the refund process are not provided '
              'in the provided excerpts, so please contact the administration for details on withdrawal refunds.')
    assert not is_refusal(hedged)
    assert evaluate_answer('How is tuition refunded?', hedged, SOURCES)['hedged']


def test_numbers_normalisation():
    assert {'15000', '200'} <= numbers_in('Rs. 200/- per hour subject to a ceiling of Rs. 15,000/- per month')
    assert '6' in numbers_in('within six months')


def test_golden_case_scoring():
    case = {'question': 'Minimum attendance?', 'expected_pdf': 'Students-Attendance-Policy.pdf', 'must_include': [['75'], ['end semester', 'final exam']]}
    m = score_golden_case(case, 'You need 75% attendance to sit the End Semester Examination.', SOURCES, 1.0)
    assert m['doc_hit'] and m['fact_recall'] == 1.0 and m['passed']

    wrong_doc = [{'pdf': 'Email-Policy.pdf', 'text': SOURCES[0]['text']}]
    assert not score_golden_case(case, 'You need 75% attendance for the End Semester Examination.', wrong_doc, 1.0)['passed']

    refuse_case = {'question': 'capital of Australia?', 'should_refuse': True}
    assert score_golden_case(refuse_case, 'I can only answer questions related to SRM University AP policy documents.', [], 0.1)['passed']
    assert not score_golden_case(refuse_case, 'Canberra is the capital of Australia.', [], 0.1)['passed']

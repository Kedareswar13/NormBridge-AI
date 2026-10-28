"""Unit tests for structure-aware chunking and contextual chunk headers (pure functions)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from services.chunking import structure_aware_chunks, contextual_text, doc_title, is_heading  # noqa: E402

PAGES = [
    {'page': 0, 'text': "7. Penalties\nPenalties apply after misconduct is established.\n"
                        "7.1. Penalties for thesis submissions\na. Level 0: up to 10% - no penalty.\nb. Level 1: 10% to 40% - revise."},
    {'page': 1, 'text': "c. Level 2: 40% to 60% - debarred for one year.\nd. Level 3: above 60% - registration cancelled."},
]


def test_lists_continue_across_pages():
    chunks = structure_aware_chunks(PAGES, max_words=300)
    whole = [c for c in chunks if 'Level 0' in c['text'] and 'Level 3' in c['text']]
    assert whole, 'the thesis penalty list must not be cut at the page break'
    assert whole[0]['page'] == 0


def test_long_sections_split_with_section_context():
    chunks = structure_aware_chunks(PAGES, max_words=25)
    tail = [c for c in chunks if 'Level 3' in c['text']][0]
    assert tail['text'].startswith('[7.1. Penalties for thesis submissions (continued)]')
    assert tail['section'] == '7.1. Penalties for thesis submissions'


def test_table_rows_keep_their_header():
    rows = '\n'.join(f'| {i} | Fee {i} | Refund rule number {i} with some extra words here |' for i in range(1, 40))
    page = {'page': 0, 'text': f"Refund Policy\n| S No | Fee type | Refund policy |\n| :--- | :--- | :--- |\n{rows}"}
    chunks = structure_aware_chunks([page], max_words=80)
    table_chunks = [c for c in chunks if '| Fee ' in c['text']]
    assert len(table_chunks) > 1
    assert all('| S No | Fee type | Refund policy |' in c['text'] for c in table_chunks)
    import re
    assert sum(len(re.findall(r'\| \d+ \| Fee \d+ \|', c['text'])) for c in table_chunks) == 39   # no row lost or duplicated


def test_table_continuing_on_next_page_is_merged():
    p1 = {'page': 0, 'text': "| S No | Fee | Rule |\n| :--- | :--- | :--- |\n| 1 | Tuition | As per UGC norms |"}
    p2 = {'page': 1, 'text': "| 2 | Hostel | Non-refundable once classes commence |"}
    chunks = structure_aware_chunks([p1, p2], max_words=300)
    assert any('Tuition' in c['text'] and 'Hostel' in c['text'] for c in chunks)


def test_heading_detection():
    assert is_heading('7.1. Penalties in case of plagiarism in submission of thesis and dissertations')
    assert is_heading('HOSTEL RULES AND REGULATIONS')
    assert is_heading('## Fund Allocations')
    assert not is_heading('1. A research scholar will not be entitled to mid-semester breaks, summer and winter vacations.')


def test_contextual_header_separates_near_duplicate_documents():
    a = contextual_text('Employee-Grievance-Redressal-Policy.pdf', '4. Procedure', 'Submit the grievance in writing.')
    b = contextual_text('Student-Grievance-Redressal-Policy.pdf', '4. Procedure', 'Submit the grievance in writing.')
    assert a.startswith('Employee Grievance Redressal Policy > 4. Procedure') and a != b
    assert doc_title('Use-of-Logo-and-Name-Policy.pdf') == 'Use of Logo and Name Policy'
    assert contextual_text('X.pdf', 'General', 'body') == 'X\nbody'

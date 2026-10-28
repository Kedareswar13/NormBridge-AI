"""Structure-aware chunking + contextual chunk headers (pure functions, unit-tested).

Why: word-window chunking per page broke the two things policies are made of.
  * Lists and clauses that continue on the next page were cut ("7.1 Penalties ... c. Level 2:" | next page),
    so the answer row lived in a chunk with no idea which list it belonged to.
  * OCR'd markdown tables were split by word count, so rows lost their header ("Fee type | Refund policy").
What this does:
  1. Treats the document as one stream of blocks (paragraphs, list items, headings, tables), across pages.
  2. Packs whole blocks into chunks of <= max_words; a block is only split when it alone is too long
     (tables are split by rows and every piece repeats the header row).
  3. A chunk that continues a section starts with "[<section heading> (continued)]".
  4. contextual_text() prefixes "document title > section" for embedding, BM25 and the cross-encoder
     (contextual retrieval), which separates near-duplicate policies (Student vs Employee grievance).
"""
import os
import re

_TABLE_LINE = re.compile(r'^\s*\|')
_TABLE_SEP = re.compile(r'^\s*\|?\s*:?-{3,}')
_LIST_ITEM = re.compile(r'^\s*(?:[a-z]\.|[ivxIVX]{1,5}\.|\(?[a-zA-Z0-9]{1,3}\)|\d+(?:\.\d+)*\.?|[•\-\*▪●])\s+\S')
_NUMBERED_HEADING = re.compile(r'^\s*\d+(?:\.\d+)*\.?\s+[A-Z]')
_MD_HEADING = re.compile(r'^\s*#{1,6}\s+\S')


def _clean_heading(line):
    return re.sub(r'^\s*#{1,6}\s*|\*\*|__', '', line).strip().rstrip(':').strip()


def is_heading(line):
    s = (line or '').strip()
    if not s or _TABLE_LINE.match(s):
        return False
    if _MD_HEADING.match(s):
        return True
    words = s.split()
    if len(words) > 14 or s.endswith(('.', ';', ',')):
        return False
    bare = re.sub(r'\*\*|__', '', s).strip()
    if s.startswith('**') and s.endswith('**') and len(words) <= 12:
        return True
    if _NUMBERED_HEADING.match(s):
        return True
    letters = [ch for ch in bare if ch.isalpha()]
    if len(letters) >= 4 and all(ch.isupper() for ch in letters):
        return True
    return bare.endswith(':') and len(words) <= 6 and bare[:1].isupper()


def _iter_blocks(text_by_page):
    """(kind, lines, page) with kind in {'heading', 'text', 'table'}, in reading order across pages."""
    for page_data in text_by_page:
        page = page_data.get('page', 0)
        lines = (page_data.get('text') or '').replace('\r', '').split('\n')
        para, i = [], 0

        def flush():
            nonlocal para
            if para:
                block = ('text', para, page)
                para = []
                return block
            return None

        while i < len(lines):
            line = lines[i].rstrip()
            if _TABLE_LINE.match(line):
                b = flush()
                if b:
                    yield b
                table = []
                while i < len(lines) and _TABLE_LINE.match(lines[i]):
                    table.append(lines[i].strip())
                    i += 1
                yield ('table', table, page)
                continue
            if not line.strip():
                b = flush()
                if b:
                    yield b
            elif is_heading(line):
                b = flush()
                if b:
                    yield b
                yield ('heading', [line.strip()], page)
            elif para and _LIST_ITEM.match(line):
                yield flush()
                para = [line.strip()]
            else:
                para.append(line.strip())
            i += 1
        b = flush()
        if b:
            yield b


def _merge_table_continuations(blocks):
    """A table that continues on the next page (rows without their own separator row) joins the previous table."""
    out = []
    for kind, lines, page in blocks:
        if kind == 'table' and out and out[-1][0] == 'table' and not any(_TABLE_SEP.match(l) for l in lines[:2]):
            out[-1] = ('table', out[-1][1] + lines, out[-1][2])
        else:
            out.append((kind, lines, page))
    return out


def _words(text):
    return len(text.split())


def _table_pieces(lines, max_words):
    """Split a table by rows; every piece repeats the header (+ separator) so rows never lose their columns."""
    head_n = 2 if len(lines) > 1 and _TABLE_SEP.match(lines[1]) else (1 if len(lines) > 1 else 0)
    header, rows = lines[:head_n], lines[head_n:]
    if _words('\n'.join(lines)) <= max_words or not rows:
        return ['\n'.join(lines)]
    pieces, cur = [], []
    budget = max(max_words - _words('\n'.join(header)), 40)
    for row in rows:
        if cur and _words('\n'.join(cur + [row])) > budget:
            pieces.append('\n'.join(header + cur))
            cur = []
        cur.append(row)
    if cur:
        pieces.append('\n'.join(header + cur))
    return pieces


_SENT = re.compile(r'(?<=[.;!?])\s+(?=[A-Z(\d•\-])')


def _text_pieces(text, max_words, overlap):
    """Only an over-long paragraph is split: by sentences, with a small word overlap between pieces."""
    if _words(text) <= max_words:
        return [text]
    sentences = [s for s in _SENT.split(text) if s.strip()]
    pieces, cur = [], []
    for s in sentences:
        if cur and _words(' '.join(cur + [s])) > max_words:
            pieces.append(' '.join(cur))
            tail = ' '.join(cur).split()[-overlap:] if overlap else []
            cur = [' '.join(tail)] if tail else []
        cur.append(s)
    if cur:
        pieces.append(' '.join(cur))
    out = []
    for p in pieces:  # a single sentence longer than max_words: hard word windows
        w = p.split()
        if len(w) <= max_words:
            out.append(p)
        else:
            step = max(max_words - overlap, 1)
            out += [' '.join(w[j:j + max_words]) for j in range(0, len(w), step) if len(w[j:j + max_words]) > 20]
    return out


def structure_aware_chunks(text_by_page, max_words=300, overlap=50):
    blocks = _merge_table_continuations(list(_iter_blocks(text_by_page)))
    chunks, cur, cur_words, cur_page, cur_section = [], [], 0, 0, 'General'
    section = 'General'
    starts_with_heading = False

    def flush():
        nonlocal cur, cur_words, starts_with_heading
        text = '\n'.join(cur).strip()
        if text and _words(text) >= 8:
            if not starts_with_heading and cur_section != 'General':
                text = f'[{cur_section} (continued)]\n{text}'
            chunks.append({'text': text, 'page': cur_page, 'section': cur_section,
                           'chunk_index': len(chunks), 'is_header': starts_with_heading})
        cur, cur_words, starts_with_heading = [], 0, False

    for kind, lines, page in blocks:
        if kind == 'heading':
            title = _clean_heading(lines[0])
            # a new section starts a new chunk once the current one has real content
            if cur and cur_words >= max_words * 0.5:
                flush()
            section = title or section
            pieces = [lines[0]]
        elif kind == 'table':
            pieces = _table_pieces(lines, max_words)
        else:
            pieces = _text_pieces('\n'.join(lines), max_words, overlap)

        for piece in pieces:
            n = _words(piece)
            if cur and cur_words + n > max_words:
                flush()
            if not cur:
                cur_page, cur_section = page, section
                starts_with_heading = kind == 'heading'
            cur.append(piece)
            cur_words += n
    flush()
    return chunks


# --------------------------------------------------------------------------- contextual retrieval
def doc_title(filename):
    base = os.path.splitext(os.path.basename(str(filename or '')))[0]
    return re.sub(r'[-_]+', ' ', base).strip()


def contextual_text(filename, section, text):
    """'Employee Grievance Redressal Policy > 4. Procedure' + chunk text: what gets embedded, keyword-indexed
    and cross-encoded. The stored/displayed chunk text stays unchanged."""
    head = doc_title(filename)
    if section and section != 'General':
        head = f'{head} > {section}'
    return f'{head}\n{text}' if head else text

"""RAG security: four layers that run around the retrieval pipeline.

Layer 1  Query validation   - block prompt-injection / data-exfiltration attempts BEFORE retrieval.
Layer 2  Access control     - resolve the user's role + tenant server-side and filter what the retriever
                              (and the answer cache) may return. Users never see documents above their role.
Layer 3  Fixed prompt template - user text and retrieved text only fill dedicated, delimited slots; any
                              role markers / delimiters inside them are neutralised (see prompt_service).
Layer 4  Observability      - an audit row per question: who (hashed + masked), when, role, decision,
                              which document pages were used (ids only, never full text), latency.
                              PII (emails, phones, Aadhaar, PAN, cards, bank accounts) is masked first.
"""
import hashlib
import json
import os
import re
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL

TENANT_ID = os.environ.get('TENANT_ID', 'srmap')
MAX_QUERY_CHARS = int(os.environ.get('MAX_QUERY_CHARS', '800'))
AUDIT_SALT = os.environ.get('AUDIT_SALT', 'normbridge-audit')

# ----------------------------------------------------------------------------- layer 1: query validation
INJECTION_PATTERNS = [
    ('instruction_override', r"\b(ignore|disregard|forget|override|bypass)\b\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|preceding|your|these|those|system|initial|original)\s+(instructions?|rules?|prompts?|guidelines?|directions?|context)\b"),
    ('instruction_override', r"\b(bypass|disable|turn off|deactivate)\b[\w\s]{0,15}\b(guardrails?|safety (rules|filters?)|filters?|restrictions?|content polic(y|ies))\b"),
    ('instruction_override', r"\b(new|updated|real|actual)\s+(instructions?|system prompt)\s*[:\-]"),
    ('role_hijack', r"\b(you are now|from now on,? you|pretend (to be|you are)|roleplay as|simulate being|act as (an?|my) (unrestricted|uncensored|unfiltered|different|new|evil|hacker))\b"),
    ('role_hijack', r"\b(developer|god|sudo|jailbreak|dan)\s+mode\b|\bdo anything now\b"),
    ('prompt_leak', r"\b(reveal|show|print|display|repeat|output|leak|tell me)\b[\w\s]{0,30}\b(system prompt|hidden prompt|your (instructions|prompt|rules|system message)|initial prompt|context window|raw context|full (context|prompt))\b"),
    ('delimiter_injection', r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?(inst|sys)\]|<<\s*sys\s*>>|^\s*(system|assistant)\s*:"),
    ('data_exfiltration', r"\b(list|dump|export|show|give|print|reveal|share)\b[\w\s]{0,20}\b(all (the )?(users|students|emails|accounts|user data)|user (emails?|list|data|accounts?)|email (ids|addresses) of|passwords?|credentials|api keys?|secret keys?|the database|database (contents|tables|dump))\b"),
    ('data_exfiltration', r"\b(other|another)\s+(users?|students?|people|persons?)(?:'s|')?\s.{0,25}?\b(chats?|questions?|history|data|emails?|messages?)\b"),
    ('data_exfiltration', r"\bsomeone else(?:'s)?\s.{0,25}?\b(chats?|history|data|emails?|messages?)\b"),
    ('code_injection', r"(\bunion\s+select\b|\bdrop\s+table\b|;\s*--|\bor\s+1\s*=\s*1\b|\.\./\.\./|<script\b|\bjavascript:)"),
]
_COMPILED = [(cat, re.compile(p, re.I | re.M)) for cat, p in INJECTION_PATTERNS]


def validate_query(text):
    """Return (ok, category, detail). Runs before embedding/retrieval; blocked queries never reach the retriever."""
    if not text or not text.strip():
        return False, 'empty', 'empty question'
    if len(text) > MAX_QUERY_CHARS:
        return False, 'too_long', f'question longer than {MAX_QUERY_CHARS} characters'
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', text):
        return False, 'control_chars', 'control characters in question'
    for cat, rx in _COMPILED:
        m = rx.search(text)
        if m:
            return False, cat, m.group(0)[:80]
    return True, None, None


BLOCKED_REPLY = ("I can't help with that request. I only answer questions about SRM University AP policies, "
                 "and I can't change my instructions or share internal or other users' data.")


# ----------------------------------------------------------------------------- layer 2: access control
AUDIENCE_LEVELS = ['public', 'student', 'staff', 'admin']
ROLE_AUDIENCES = {
    'student': ['public', 'student'],
    'staff': ['public', 'student', 'staff'],
    'admin': AUDIENCE_LEVELS,
}


def _email_set(var):
    return {e.strip().lower() for e in os.environ.get(var, '').split(',') if e.strip()}


def resolve_role(email):
    """Role is decided ONLY server-side from configuration, never from anything the client sends."""
    from core.auth import ADMIN_EMAIL_MAPPINGS, EVAL_STAFF_EMAIL
    e = (email or '').strip().lower()
    if e in {v.lower() for v in ADMIN_EMAIL_MAPPINGS.values()}:
        return 'admin'
    if e in _email_set('STAFF_EMAILS') or e == EVAL_STAFF_EMAIL:
        return 'staff'
    pattern = os.environ.get('STAFF_EMAIL_REGEX', '').strip()
    if pattern and re.fullmatch(pattern, e):
        return 'staff'
    return 'student'


def allowed_audiences(role):
    return ROLE_AUDIENCES.get(role, ROLE_AUDIENCES['student'])


def retrieval_filter(role):
    """Chroma `where` clause: tenant isolation + audience (role) filtering, applied inside the vector search."""
    return {"$and": [{"tenant_id": TENANT_ID}, {"audience": {"$in": allowed_audiences(role)}}]}


def can_view_policy(role, audience, tenant_id=None):
    return (tenant_id or TENANT_ID) == TENANT_ID and (audience or 'public') in allowed_audiences(role)


# ----------------------------------------------------------------------------- layer 3: prompt slot hygiene
_ROLE_MARKERS = re.compile(r"<\|?(im_start|im_end|system|user|assistant|endoftext)\|?>|\[/?(INST|SYS)\]|<<\s*/?SYS\s*>>|</?(user_question|policy_excerpts|excerpt)[^>]*>", re.I)
_ROLE_PREFIX = re.compile(r"(?im)^\s*(system|assistant|user|instruction|###\s*instruction)\s*:")


def sanitize_for_slot(text, max_chars=None):
    """Neutralise anything that could change the prompt's structure: chat-template tokens, our own slot tags,
    role prefixes at line start, and code fences. The text can only ever be data inside its slot."""
    t = _ROLE_MARKERS.sub(' ', text or '')
    t = _ROLE_PREFIX.sub(lambda m: m.group(1) + ' -', t)
    t = t.replace('```', "'''")
    if max_chars and len(t) > max_chars:
        t = t[:max_chars]
    return t.strip()


# ----------------------------------------------------------------------------- layer 4: PII masking + audit
PII_PATTERNS = [
    ('email', re.compile(r'\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b'), lambda m: f'{m.group(1)}***@{m.group(2)}'),
    ('card', re.compile(r'\b(?:\d[ -]?){12,15}(\d{4})\b'), lambda m: f'[CARD ****{m.group(1)}]'),
    ('aadhaar', re.compile(r'\b[2-9]\d{3}[ -]?\d{4}[ -]?(\d{4})\b'), lambda m: f'[AADHAAR ****{m.group(1)}]'),
    ('pan', re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b'), lambda m: '[PAN]'),
    ('ifsc', re.compile(r'\b[A-Z]{4}0[A-Z0-9]{6}\b'), lambda m: '[IFSC]'),
    ('phone', re.compile(r'(?<!\d)(?:\+?91[ -]?)?[6-9]\d{4}[ -]?\d{5}(?!\d)'), lambda m: '[PHONE]'),
    ('bank_account', re.compile(r'(?i)\b(a/?c|acc(?:ount)?)(?:\s*(?:no|number|num))?\.?\s*[:#]?\s*\d{9,18}\b'), lambda m: f'{m.group(1)} [ACCOUNT]'),
]


def mask_pii(text):
    if not text:
        return text
    out = text
    for _, rx, repl in PII_PATTERNS:
        out = rx.sub(repl, out)
    return out


def user_ref(email):
    """Stable pseudonymous id for joining audit rows without storing the raw identity."""
    return hashlib.sha256(f"{AUDIT_SALT}|{(email or '').lower()}".encode()).hexdigest()[:16]


@contextmanager
def _cursor(dict_rows=False):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        with conn.cursor(cursor_factory=RealDictCursor if dict_rows else None) as cur:
            yield cur
    finally:
        conn.close()


def init_security_tables():
    with _cursor() as cur:
        cur.execute("ALTER TABLE policies ADD COLUMN IF NOT EXISTS audience VARCHAR(20) DEFAULT 'public'")
        cur.execute(f"ALTER TABLE policies ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(50) DEFAULT '{TENANT_ID}'")
        cur.execute('''
            CREATE TABLE IF NOT EXISTS rag_audit (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP DEFAULT NOW(),
                user_ref VARCHAR(32),
                user_masked VARCHAR(255),
                role VARCHAR(20),
                tenant_id VARCHAR(50),
                query_masked TEXT,
                decision VARCHAR(20),
                reason VARCHAR(255),
                documents TEXT,
                top_score FLOAT,
                model VARCHAR(100),
                latency_ms INTEGER
            )''')


def write_audit(email, role, query, decision, reason=None, sources=None, top_score=None, model=None, latency_s=None):
    """Store WHO/WHEN/WHAT-DECISION/WHICH-DOCS. Documents are recorded as (pdf, page, chunk) ids only."""
    try:
        docs = []
        for s in sources or []:
            ref = {'pdf': s.get('pdf'), 'page': s.get('page'), 'chunk': s.get('chunk_index')}
            if ref not in docs:
                docs.append(ref)
        with _cursor() as cur:
            cur.execute('''INSERT INTO rag_audit (user_ref, user_masked, role, tenant_id, query_masked, decision, reason, documents, top_score, model, latency_ms)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (user_ref(email), mask_pii(email or ''), role, TENANT_ID, mask_pii((query or '')[:500]), decision,
                         (reason or '')[:255], json.dumps(docs[:10]), top_score, model,
                         int(latency_s * 1000) if latency_s is not None else None))
    except Exception as e:
        print(f"⚠️ [Audit] write failed: {e}")


def audit_summary(limit=100):
    with _cursor(True) as cur:
        cur.execute('SELECT decision, COUNT(*) AS n FROM rag_audit GROUP BY decision')
        counts = {r['decision']: r['n'] for r in cur.fetchall()}
        cur.execute("SELECT reason, COUNT(*) AS n FROM rag_audit WHERE decision = 'blocked' GROUP BY reason ORDER BY n DESC LIMIT 10")
        blocked_by = [dict(r) for r in cur.fetchall()]
        cur.execute('SELECT * FROM rag_audit ORDER BY id DESC LIMIT %s', (limit,))
        rows = cur.fetchall()
        cur.execute('SELECT name, COALESCE(audience, %s) AS audience FROM policies ORDER BY name', ('public',))
        audiences = [dict(r) for r in cur.fetchall()]
    return {
        'counts': counts,
        'blocked_by': blocked_by,
        'policy_audiences': audiences,
        'tenant_id': TENANT_ID,
        'rows': [{**{k: v for k, v in dict(r).items() if k != 'documents'},
                  'ts': r['ts'].isoformat() if r['ts'] else None,
                  'documents': json.loads(r['documents'] or '[]')} for r in rows],
    }

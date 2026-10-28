"""Per-user conversation history (server-side source of truth for the chat sidebar).

Why: the browser's localStorage was the only copy of a user's chats, and it was only read when the page first loaded.
Signing in again (after logout or an expired session) started from an empty list, and that list then overwrote the
saved one, so history disappeared. Conversations now live in Postgres, keyed by (user, conversation id). The UI
loads them after every sign-in and refresh, and localStorage is just a fast local cache.

Privacy:
  * Only the owner can read or delete a conversation; identity comes from the signed session token.
  * What the user typed is PII-masked before storage (layer 4). Assistant answers are not masked: they quote public
    policy documents, and masking would hide things like helpline numbers.
  * Deleting a conversation removes it (soft delete + messages purged).
"""
import json
import re
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL
from services.security_service import mask_pii

MAX_CONVERSATIONS = 200          # returned to the sidebar
MAX_MESSAGES_PER_CONVERSATION = 400
_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
_META_KEYS = ('sources', 'eval', 'model', 'cached', 'rewritten_question', 'rewrite_method', 'chat_id', 'fetch_time',
              'trace_id', 'time')


@contextmanager
def _cursor(dict_rows=False):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        with conn.cursor(cursor_factory=RealDictCursor if dict_rows else None) as cur:
            yield cur
    finally:
        conn.close()


def init_conversation_tables():
    with _cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS conversations (
                           user_email VARCHAR(255) NOT NULL,
                           id VARCHAR(64) NOT NULL,
                           title VARCHAR(120) NOT NULL DEFAULT 'New Chat',
                           created_at TIMESTAMPTZ DEFAULT NOW(),
                           updated_at TIMESTAMPTZ DEFAULT NOW(),
                           deleted BOOLEAN DEFAULT FALSE,
                           PRIMARY KEY (user_email, id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS conversation_messages (
                           id BIGSERIAL PRIMARY KEY,
                           user_email VARCHAR(255) NOT NULL,
                           conversation_id VARCHAR(64) NOT NULL,
                           role VARCHAR(10) NOT NULL,
                           content TEXT NOT NULL,
                           meta JSONB,
                           created_at TIMESTAMPTZ DEFAULT NOW())''')
        cur.execute('CREATE INDEX IF NOT EXISTS conv_msgs_owner ON conversation_messages (user_email, conversation_id, id)')
        cur.execute('CREATE INDEX IF NOT EXISTS conv_owner_updated ON conversations (user_email, updated_at DESC)')


def valid_id(conversation_id):
    return bool(conversation_id) and bool(_ID_RE.match(str(conversation_id)))


def _title_from(question):
    q = ' '.join((question or '').split())
    return (q[:57] + '...') if len(q) > 60 else (q or 'New Chat')


def _meta(meta):
    meta = meta or {}
    out = {k: meta[k] for k in _META_KEYS if k in meta and meta[k] is not None}
    if isinstance(out.get('sources'), list):
        # keep what the evidence panel needs; drop nothing the user saw
        out['sources'] = out['sources'][:8]
    return out


def append_exchange(user_email, conversation_id, question, answer, meta=None):
    """Store one user question + assistant answer. Creates the conversation on first use (title = first question)."""
    if not (user_email and valid_id(conversation_id) and question):
        return False
    email = user_email.strip().lower()
    with _cursor() as cur:
        cur.execute('''INSERT INTO conversations (user_email, id, title) VALUES (%s, %s, %s)
                       ON CONFLICT (user_email, id) DO UPDATE SET updated_at = NOW(), deleted = FALSE''',
                    (email, conversation_id, _title_from(mask_pii(question))))
        cur.execute('''INSERT INTO conversation_messages (user_email, conversation_id, role, content, meta)
                       VALUES (%s, %s, 'user', %s, NULL), (%s, %s, 'assistant', %s, %s)''',
                    (email, conversation_id, mask_pii(question), email, conversation_id, answer or '', json.dumps(_meta(meta))))
    return True


def list_conversations(user_email, include_messages=True, limit=MAX_CONVERSATIONS):
    email = (user_email or '').strip().lower()
    with _cursor(True) as cur:
        cur.execute('''SELECT id, title, created_at, updated_at FROM conversations
                       WHERE user_email = %s AND NOT deleted ORDER BY updated_at DESC LIMIT %s''', (email, limit))
        convs = cur.fetchall()
        by_id = {c['id']: {'id': c['id'], 'title': c['title'],
                           'created_at': c['created_at'].isoformat() if c['created_at'] else None,
                           'updated_at': c['updated_at'].isoformat() if c['updated_at'] else None,
                           'messages': []} for c in convs}
        if include_messages and by_id:
            cur.execute('''SELECT conversation_id, role, content, meta, created_at FROM (
                               SELECT *, ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY id DESC) AS rn
                               FROM conversation_messages WHERE user_email = %s AND conversation_id = ANY(%s)) m
                           WHERE rn <= %s ORDER BY conversation_id, id''',
                        (email, list(by_id), MAX_MESSAGES_PER_CONVERSATION))
            for m in cur.fetchall():
                meta = m['meta'] if isinstance(m['meta'], dict) else json.loads(m['meta'] or '{}')
                by_id[m['conversation_id']]['messages'].append({
                    'role': m['role'], 'content': m['content'], 'meta': meta,
                    'created_at': m['created_at'].isoformat() if m['created_at'] else None})
    return [by_id[c['id']] for c in convs]


def delete_conversation(user_email, conversation_id):
    email = (user_email or '').strip().lower()
    with _cursor() as cur:
        cur.execute('DELETE FROM conversation_messages WHERE user_email = %s AND conversation_id = %s', (email, conversation_id))
        cur.execute('UPDATE conversations SET deleted = TRUE, updated_at = NOW() WHERE user_email = %s AND id = %s',
                    (email, conversation_id))
        return cur.rowcount > 0


def rename_conversation(user_email, conversation_id, title):
    email = (user_email or '').strip().lower()
    title = ' '.join(str(title or '').split())[:120] or 'New Chat'
    with _cursor() as cur:
        cur.execute('UPDATE conversations SET title = %s, updated_at = NOW() WHERE user_email = %s AND id = %s AND NOT deleted',
                    (mask_pii(title), email, conversation_id))
        return cur.rowcount > 0


def import_conversations(user_email, sessions, limit=100):
    """One-time migration of chats that only exist in this browser's localStorage. Idempotent: a conversation that
    already has messages on the server is skipped."""
    email = (user_email or '').strip().lower()
    imported = 0
    for s in (sessions or [])[:limit]:
        cid = str(s.get('id') or '')
        msgs = s.get('messages') or []
        if not valid_id(cid) or not msgs:
            continue
        with _cursor() as cur:
            cur.execute('SELECT 1 FROM conversation_messages WHERE user_email = %s AND conversation_id = %s LIMIT 1', (email, cid))
            if cur.fetchone():
                continue
            pending_q = None
            for m in msgs[:MAX_MESSAGES_PER_CONVERSATION]:
                text = str(m.get('text') or '')[:8000]
                if m.get('type') == 'user':
                    pending_q = text
                elif m.get('type') == 'bot' and pending_q is not None:
                    append_exchange(email, cid, pending_q, text, {
                        'sources': m.get('sources'), 'eval': m.get('evaluation'), 'model': m.get('model'),
                        'cached': m.get('cached'), 'chat_id': m.get('chatId'), 'fetch_time': m.get('fetchTime'),
                        'time': m.get('time')})
                    pending_q = None
            title = str(s.get('title') or '')
            if title and title != 'New Chat':
                rename_conversation(email, cid, title)
            imported += 1
    return imported

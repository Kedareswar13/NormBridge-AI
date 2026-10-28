"""Signed session tokens: the server, not the client, decides who is asking.

After a successful login the API returns a token that contains the verified email, signed with SESSION_SECRET and
time-limited. Endpoints that depend on identity (chat, policy list, PDF viewer, feedback) read the email from the
token and ignore any email in the request body, so a user can't claim a staff address to unlock staff-only
documents (security layer 2 depends on this).
"""
import hashlib
import os
import secrets

from flask import request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config  # noqa: F401  (loads .env before the settings below are read)

SESSION_TTL_HOURS = float(os.environ.get('SESSION_TTL_HOURS', '12'))
AUTH_REQUIRED = os.environ.get('AUTH_REQUIRED', 'true').strip().lower() not in ('0', 'false', 'no')


def _secret():
    explicit = os.environ.get('SESSION_SECRET', '').strip()
    if explicit and explicit != 'change-me':
        return explicit
    # derive from other per-install secrets so existing installs keep working; random only as a last resort
    seed = ''.join(os.environ.get(k, '') for k in ('AUDIT_SALT', 'ADMIN_PASSWORD', 'DB_PASSWORD'))
    if seed.strip() and 'change-me' not in seed:
        return hashlib.sha256(('normbridge-session|' + seed).encode()).hexdigest()
    print('⚠️ SESSION_SECRET not set: using a random secret (users must sign in again after a restart)')
    return secrets.token_hex(32)


_serializer = URLSafeTimedSerializer(_secret(), salt='normbridge-session')


def issue_token(email):
    return _serializer.dumps({'email': (email or '').strip().lower()})


def verify_token(token):
    """Email inside a valid, unexpired token, else None."""
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=int(SESSION_TTL_HOURS * 3600))
        return data.get('email') or None
    except (SignatureExpired, BadSignature):
        return None


def token_from_request():
    header = request.headers.get('Authorization', '')
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    # <iframe>/<a> requests can't set headers, so the PDF viewer may pass the token as a query parameter
    return request.args.get('token', '')


def session_email():
    """Verified email of the caller, or None. With AUTH_REQUIRED=false (local demos only) the body email is trusted."""
    email = verify_token(token_from_request())
    if email or AUTH_REQUIRED:
        return email
    body = request.get_json(silent=True) or {}
    return (body.get('user_email') or request.args.get('user_email') or '').strip().lower() or None

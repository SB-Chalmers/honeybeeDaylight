"""
api.auth
────────
HMAC request signing. Each client application gets an id and a shared secret:

  APP_KEYS="heath:<secret>,mr-app:<secret>"

Every request carries three headers:

  X-App-Id      the application id
  X-Timestamp   unix time in seconds (must be within MAX_SKEW_SECONDS of the server)
  X-Signature   hex HMAC-SHA256(secret, f"{timestamp}\n{METHOD}\n{path}\n{sha256_hex(body)}")

``body`` is the uncompressed request body (empty for GET). See
scripts/client.py for a reference client.

If APP_KEYS is empty, signing is not enforced (local development only).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)
MAX_SKEW_SECONDS = int(os.environ.get('AUTH_MAX_SKEW_SECONDS', '300'))


def _load_keys(raw: str) -> dict[str, bytes]:
    """Parse APP_KEYS ("app:secret,app:secret") into {app_id: secret bytes}."""
    keys = {}
    for item in filter(None, (part.strip() for part in raw.split(','))):
        app_id, sep, secret = item.partition(':')
        if not sep or not app_id or not secret:
            raise RuntimeError('APP_KEYS must look like "app1:secret1,app2:secret2".')
        keys[app_id.strip()] = secret.strip().encode()
    return keys


APP_KEYS = _load_keys(os.environ.get('APP_KEYS', ''))
if not APP_KEYS:
    log.warning('APP_KEYS is empty: request signing is DISABLED. Do not run like this in production.')


def signature(secret: bytes, timestamp: str, method: str, path: str, body: bytes) -> str:
    """The hex HMAC-SHA256 a client must send in X-Signature (clients reuse this)."""
    message = f'{timestamp}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}'
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def _deny(message: str):
    """Reject the request with 401."""
    raise HTTPException(status_code=401, detail={'error': 'unauthorized', 'message': message})


async def require_signature(request: Request) -> str:
    """FastAPI dependency that checks the signature headers. Returns the app id.

    The timestamp window stops a captured request from being replayed later.
    """
    if not APP_KEYS:
        return 'anonymous'
    app_id = request.headers.get('x-app-id', '')
    timestamp = request.headers.get('x-timestamp', '')
    given = request.headers.get('x-signature', '')
    secret = APP_KEYS.get(app_id)
    if not secret or not timestamp or not given:
        _deny('Missing or unknown X-App-Id, X-Timestamp or X-Signature.')
    try:
        skew = abs(time.time() - float(timestamp))
    except ValueError:
        _deny('X-Timestamp must be unix seconds.')
    if skew > MAX_SKEW_SECONDS:
        _deny('X-Timestamp is too far from server time.')
    expected = signature(secret, timestamp, request.method, request.url.path, await request.body())
    if not hmac.compare_digest(expected, given.lower()):
        _deny('Bad signature.')
    return app_id

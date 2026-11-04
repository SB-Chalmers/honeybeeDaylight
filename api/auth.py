"""
api.auth
────────
Per-app API keys. Each client application gets its own key:

  APP_KEYS="heath:<key>,mr-app:<key>"

and sends it with every request:

  Authorization: Bearer <key>

Run the API behind HTTPS so keys cannot be read in transit. If APP_KEYS is
empty, keys are not checked (local development only).
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


def _load_keys(raw: str) -> dict[str, str]:
    """Parse APP_KEYS ("app:key,app:key") into {key: app_id}."""
    keys = {}
    for item in filter(None, (part.strip() for part in raw.split(','))):
        app_id, sep, key = item.partition(':')
        if not sep or not app_id.strip() or not key.strip():
            raise RuntimeError('APP_KEYS must look like "app1:key1,app2:key2".')
        keys[key.strip()] = app_id.strip()
    return keys


APP_KEYS = _load_keys(os.environ.get('APP_KEYS', ''))
if not APP_KEYS:
    log.warning('APP_KEYS is empty: API keys are NOT checked. Do not run like this in production.')


async def require_api_key(request: Request) -> str:
    """FastAPI dependency: accept the request only with a known key. Returns the app id."""
    if not APP_KEYS:
        return 'anonymous'
    given = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    # compare_digest takes the same time whether or not the key matches.
    for key, app_id in APP_KEYS.items():
        if hmac.compare_digest(given, key):
            return app_id
    raise HTTPException(401, detail={'error': 'unauthorized', 'message': 'Missing or unknown API key.'})

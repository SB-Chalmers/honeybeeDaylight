"""Accept gzip-compressed request bodies (Content-Encoding: gzip)."""

from __future__ import annotations

import json
import os
import zlib

MAX_BODY_BYTES = int(os.environ.get('MAX_BODY_BYTES', str(20 * 1024 * 1024)))


class GzipRequestMiddleware:
    """Pure ASGI middleware: inflates gzip request bodies before routing.

    Responses are compressed separately by Starlette's GZipMiddleware.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        """Inflate the body (with a size cap against gzip bombs), then pass it on."""
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        if headers.get(b'content-encoding', b'').lower() != b'gzip':
            return await self.app(scope, receive, send)

        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            try:
                body += inflater.decompress(message.get('body', b''), MAX_BODY_BYTES + 1 - len(body))
            except zlib.error:
                return await _reply(send, 400, 'bad_gzip', 'Request body is not valid gzip.')
            if len(body) > MAX_BODY_BYTES:
                return await _reply(send, 413, 'too_large', 'Decompressed request body is too large.')
            more = message.get('more_body', False)

        scope = dict(scope)
        scope['headers'] = [
            (k, v) for k, v in scope['headers'] if k not in (b'content-encoding', b'content-length')
        ] + [(b'content-length', str(len(body)).encode())]
        sent = False

        async def replay():
            """Hand the inflated body to the app as one message."""
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {'type': 'http.request', 'body': bytes(body), 'more_body': False}

        return await self.app(scope, replay, send)


async def _reply(send, status: int, error: str, message: str):
    """Send a JSON error response straight from the middleware."""
    payload = json.dumps({'error': error, 'message': message}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'),
                            (b'content-length', str(len(payload)).encode())]})
    await send({'type': 'http.response.body', 'body': payload})

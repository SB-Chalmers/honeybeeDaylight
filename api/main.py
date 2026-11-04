"""
api.main
────────
FastAPI entry point. The API is a thin layer: it validates requests, checks
signatures, queues jobs on Celery and returns their results.

    uvicorn api.main:app
"""

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from worker.celery_app import app as celery

from .routes import router

app = FastAPI(
    title='Simulation API',
    description='Send a building description, get daylight and energy results.',
    version='0.2.0',
)

# Gzip responses (result arrays are the big part of the traffic), and CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get('ALLOWED_ORIGINS', '*').split(','),
    allow_methods=['GET', 'POST'],
    allow_headers=['*'],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """Return every HTTP error as {"error", "message"}."""
    detail = exc.detail if isinstance(exc.detail, dict) else {'error': 'http_error', 'message': str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=detail)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Return the first validation problem as one readable message."""
    first = exc.errors()[0] if exc.errors() else {}
    field = ' → '.join(str(part) for part in first.get('loc', []))
    return JSONResponse(status_code=422, content={
        'error': 'validation_error', 'message': f"{field}: {first.get('msg', 'Validation error.')}",
    })


@app.get('/', include_in_schema=False)
def root():
    """Point people at the interactive docs."""
    return {'message': 'Simulation API', 'docs': '/docs'}


@app.get('/health')
def health():
    """Unauthenticated liveness check, also confirms Redis is reachable."""
    try:
        with celery.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
        return {'status': 'ok'}
    except Exception as exc:
        return JSONResponse(status_code=503, content={'status': 'degraded', 'message': str(exc)})

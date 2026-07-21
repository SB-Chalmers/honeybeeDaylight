"""
api.main
────────
FastAPI application entry point.

Run from the project root:
    uvicorn api.main:app --reload
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import router

app = FastAPI(
    title='Daylight Simulation API',
    description='Teaching tool — submit a building description, get daylight results.',
    version='0.1.0',
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Allow all origins in development. Set ALLOWED_ORIGINS env var in production.
import os
_origins = os.environ.get('ALLOWED_ORIGINS', '*').split(',')

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(router)


# ── Consistent error envelope ─────────────────────────────────────────────────
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(status_code=404, content={'error': 'not_found', 'message': str(exc.detail)})


@app.exception_handler(422)
async def validation_handler(request: Request, exc):
    first = exc.errors()[0] if exc.errors() else {}
    msg = first.get('msg', 'Validation error.')
    field = ' → '.join(str(l) for l in first.get('loc', []))
    return JSONResponse(status_code=422, content={'error': 'validation_error', 'message': f'{field}: {msg}'})


@app.get('/', include_in_schema=False)
def root():
    return {'message': 'Daylight Simulation API', 'docs': '/docs'}

"""Look up construction and material records from the external EPSM service.

EPSM is served separately (``EPSM_BASE_URL``); this backend only reads its
catalogue. Each worker process caches the catalogue for ``CACHE_SECONDS``
so a burst of jobs does not refetch it every time.
"""

from __future__ import annotations

import os
import threading
import time

import httpx

EPSM_BASE = os.environ.get('EPSM_BASE_URL', 'https://epsm.chalmers.se').rstrip('/')
CACHE_SECONDS = 600

_cache: dict = {'at': 0.0, 'constructions': None, 'materials': None}
_lock = threading.Lock()


def _records(payload) -> list[dict]:
    """Accept a plain list or a paginated {"results": [...]} / {"data": [...]} reply."""
    if isinstance(payload, dict):
        payload = payload.get('results', payload.get('data', []))
    return [item for item in payload if isinstance(item, dict)]


def _catalogue() -> tuple[dict, dict]:
    """Return ({name: construction}, {name: material}), refetched every CACHE_SECONDS."""
    with _lock:
        stale = time.monotonic() - _cache['at'] > CACHE_SECONDS
        if _cache['constructions'] is None or stale:
            with httpx.Client(timeout=60) as client:
                constructions = client.get(f'{EPSM_BASE}/api/constructions/')
                materials = client.get(f'{EPSM_BASE}/api/materials/')
            constructions.raise_for_status()
            materials.raise_for_status()
            _cache['constructions'] = {r['name']: r for r in _records(constructions.json())}
            _cache['materials'] = {r['name']: r for r in _records(materials.json())}
            _cache['at'] = time.monotonic()
        return _cache['constructions'], _cache['materials']


def fetch_constructions(names: dict) -> dict:
    """Return {element_type: construction record with material data merged into layers}.

    Raises ``LookupError`` when a requested construction name is not in EPSM.
    """
    by_name, material_by_name = _catalogue()
    missing = sorted({name for name in names.values() if name not in by_name})
    if missing:
        raise LookupError(f'EPSM constructions not found: {missing}')
    selected = {}
    for element_type, name in names.items():
        construction = dict(by_name[name])
        construction['layers'] = [
            {**layer, **material_by_name.get(layer['material_name'], {})}
            for layer in construction.get('layers', [])
        ]
        selected[element_type] = construction
    return selected

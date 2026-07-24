"""Async client for the public EPSM construction and simulation API."""

from __future__ import annotations

import os

EPSM_BASE = os.environ.get('EPSM_BASE_URL', 'https://epsm.chalmers.se').rstrip('/')


def _httpx():
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError('httpx is required for EPSM energy simulations. Install requirements.txt.') from exc
    return httpx


def _payload_record(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return next((item for item in payload if isinstance(item, dict)), {})
    return {}


async def fetch_constructions(names: dict) -> dict:
    httpx = _httpx()
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f'{EPSM_BASE}/api/constructions/')
        response.raise_for_status()
        records = response.json()
        if isinstance(records, dict):
            records = records.get('results', records.get('data', []))
        materials_response = await client.get(f'{EPSM_BASE}/api/materials/')
        materials_response.raise_for_status()
        materials = materials_response.json()
        if isinstance(materials, dict):
            materials = materials.get('results', materials.get('data', []))

    material_by_name = {item['name']: item for item in materials}
    by_name = {item['name']: item for item in records}
    missing = [name for name in names.values() if name not in by_name]
    if missing:
        raise ValueError(f'EPSM constructions not found: {missing}')
    selected = {}
    for element_type, name in names.items():
        construction = dict(by_name[name])
        construction['layers'] = [
            {**layer, **material_by_name.get(layer['material_name'], {})}
            for layer in construction.get('layers', [])
        ]
        selected[element_type] = construction
    return selected


async def submit_idf(idf_path: str, epw_path: str) -> str:
    httpx = _httpx()
    async with httpx.AsyncClient(timeout=120) as client:
        with open(idf_path, 'rb') as idf, open(epw_path, 'rb') as epw:
            response = await client.post(
                f'{EPSM_BASE}/api/simulation/run/',
                files={
                    'idf_files': (os.path.basename(idf_path), idf),
                    'weather_file': (os.path.basename(epw_path), epw),
                },
            )
        response.raise_for_status()
        payload = _payload_record(response.json())
    simulation_id = payload.get('simulation_id') or payload.get('simulationId')
    if not simulation_id:
        raise ValueError('EPSM did not return a simulation_id.')
    return simulation_id


async def simulation_status(simulation_id: str) -> dict:
    httpx = _httpx()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f'{EPSM_BASE}/api/simulation/{simulation_id}/status/')
        response.raise_for_status()
        return _payload_record(response.json())


async def simulation_results(simulation_id: str) -> dict:
    httpx = _httpx()
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f'{EPSM_BASE}/api/simulation/{simulation_id}/results/')
        response.raise_for_status()
        return _payload_record(response.json())
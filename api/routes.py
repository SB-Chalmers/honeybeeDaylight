"""
api.routes
──────────
All v1 endpoints:
  POST   /v1/studies
  GET    /v1/studies/{study_id}
  GET    /v1/studies/{study_id}/result
  POST   /v1/studies:run-sync
  GET    /v1/locations
"""

from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException

from simulation.inputs import load_room_inputs, validate_room_inputs
from simulation.model import build_model

from .schemas import (
    DfResult,
    ErrorResponse,
    LocationItem,
    LocationsResponse,
    SdaResult,
    StudyQueued,
    StudyRequest,
    StudyResult,
    StudyStatus,
)
from .store import store
from .worker import WEATHER_DIR, run_study

router = APIRouter(prefix='/v1')

# Thread pool for the sync convenience route
_executor = ThreadPoolExecutor(max_workers=4)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sensor_count(room_dict: dict) -> int:
    """Build the model just to read sensor count — fast, no I/O."""
    cfg = load_room_inputs(room_dict)
    validate_room_inputs(cfg)
    _, grid = build_model(cfg)
    return grid.count


def _study_links(study_id: str) -> dict:
    return {
        'status': f'/v1/studies/{study_id}',
        'result': f'/v1/studies/{study_id}/result',
    }


def _to_result_response(study_id: str, data: dict) -> StudyResult:
    df_data = data['df']
    sda_data = data.get('sda')

    df = DfResult(
        summary=df_data['summary'],
        values=df_data['values'],
    )
    sda = None
    if sda_data:
        sda = SdaResult.model_validate({
            'summary': sda_data['summary'],
            'values':  sda_data['values'],
            'pass':    sda_data['pass'],
        })
    return StudyResult(study_id=study_id, df=df, sda=sda)


# ── POST /v1/studies ──────────────────────────────────────────────────────────

@router.post('/studies', response_model=StudyQueued, status_code=202)
def start_study(request: StudyRequest, background_tasks: BackgroundTasks):
    room_dict = request.room.to_simulation_dict()

    try:
        sensor_count = _sensor_count(room_dict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={'error': 'invalid_room', 'message': str(exc)})

    if request.run_sda and not request.location:
        raise HTTPException(
            status_code=400,
            detail={'error': 'missing_location', 'message': 'location is required when run_sda=true.'},
        )

    study_id = f'std_{uuid.uuid4().hex[:12]}'
    store.create(study_id)

    background_tasks.add_task(
        run_study,
        study_id=study_id,
        room_dict=room_dict,
        run_sda=request.run_sda,
        location=request.location,
        draft=(request.quality == 'draft'),
        da_lux=request.thresholds.da_lux,
        sda_target_pct=request.thresholds.sda_target_pct,
    )

    return StudyQueued(
        study_id=study_id,
        status='queued',
        sensor_count=sensor_count,
        links=_study_links(study_id),
    )


# ── GET /v1/studies/{study_id} ────────────────────────────────────────────────

@router.get('/studies/{study_id}', response_model=StudyStatus)
def get_status(study_id: str):
    record = store.get(study_id)
    if not record:
        raise HTTPException(status_code=404, detail={'error': 'not_found', 'message': f'Study {study_id} not found.'})
    return StudyStatus(
        study_id=record.study_id,
        status=record.status,
        stage=record.stage,
        error=record.error,
    )


# ── GET /v1/studies/{study_id}/result ─────────────────────────────────────────

@router.get('/studies/{study_id}/result', response_model=StudyResult)
def get_result(study_id: str):
    record = store.get(study_id)
    if not record:
        raise HTTPException(status_code=404, detail={'error': 'not_found', 'message': f'Study {study_id} not found.'})
    if record.status == 'failed':
        raise HTTPException(status_code=500, detail={'error': 'simulation_failed', 'message': record.error or 'Unknown error.'})
    if record.status != 'complete':
        raise HTTPException(status_code=202, detail={'error': 'not_ready', 'message': f'Study is {record.status}.'})
    return _to_result_response(study_id, record.result)


# ── POST /v1/studies:run-sync ─────────────────────────────────────────────────

@router.post('/studies:run-sync', response_model=StudyResult)
async def run_sync(request: StudyRequest):
    """Run DF synchronously and return results directly. sDA is ignored."""
    room_dict = request.room.to_simulation_dict()

    try:
        _sensor_count(room_dict)  # validates inputs early
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={'error': 'invalid_room', 'message': str(exc)})

    study_id = f'std_{uuid.uuid4().hex[:12]}'
    store.create(study_id)

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                _executor,
                lambda: run_study(
                    study_id=study_id,
                    room_dict=room_dict,
                    run_sda=False,
                    location=None,
                    draft=(request.quality == 'draft'),
                    da_lux=request.thresholds.da_lux,
                    sda_target_pct=request.thresholds.sda_target_pct,
                ),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=202,
            detail={'error': 'timeout', 'message': f'Study is still running. Poll /v1/studies/{study_id}.', 'study_id': study_id},
        )

    record = store.get(study_id)
    if record.status == 'failed':
        raise HTTPException(status_code=500, detail={'error': 'simulation_failed', 'message': record.error})
    return _to_result_response(study_id, record.result)


# ── GET /v1/locations ─────────────────────────────────────────────────────────

@router.get('/locations', response_model=LocationsResponse)
def list_locations():
    """List EPW files available on the server."""
    items = []
    if os.path.isdir(WEATHER_DIR):
        for fname in sorted(os.listdir(WEATHER_DIR)):
            if fname.lower().endswith('.epw'):
                location_id = fname[:-4].lower()
                label = fname[:-4].replace('_', ' ').title()
                items.append(LocationItem(id=location_id, label=label))
    return LocationsResponse(locations=items)

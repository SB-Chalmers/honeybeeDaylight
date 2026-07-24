"""
api.routes
──────────
All v1 endpoints:
  POST   /v1/studies
  GET    /v1/studies/{study_id}
  GET    /v1/studies/{study_id}/result
    GET    /v1/studies/{study_id}/logs
    GET    /v1/studies/{study_id}/events
  POST   /v1/studies:run-sync
  GET    /v1/locations
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from simulation.inputs import load_room_inputs, validate_room_inputs
from simulation.model import build_model
from simulation.results import parse_annual, parse_df, parse_sensor_points
from simulation.energy.epsm_client import simulation_results, simulation_status
from simulation.energy.idf_builder import footprint_area

from .schemas import (
    DfResult,
    EnergyQueued,
    EnergyRequest,
    EnergyResult,
    EnergyStatus,
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
from .worker import STUDIES_ROOT, WEATHER_DIR, run_energy_study, run_study

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
    sensor_points = data.get('sensor_points', [])

    # Backward compatibility: older completed records may not store sensor_points.
    if not sensor_points:
      try:
          sensor_points = parse_sensor_points(os.path.join(STUDIES_ROOT, study_id))
      except Exception:
          sensor_points = []

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
    return StudyResult(study_id=study_id, df=df, sda=sda, sensor_points=sensor_points)


def _study_log_files(study_id: str) -> list[tuple[str, str]]:
    """Return known log files for a study as (label, absolute_path)."""
    root = os.path.join(STUDIES_ROOT, study_id)
    candidates = [
        ('daylight_factor.log', os.path.join(root, 'daylight_factor', '__logs__', 'logs.log')),
        ('daylight_factor.err', os.path.join(root, 'daylight_factor', '__logs__', 'err.log')),
        ('annual_daylight.log', os.path.join(root, 'annual_daylight', '__logs__', 'logs.log')),
        ('annual_daylight.err', os.path.join(root, 'annual_daylight', '__logs__', 'err.log')),
    ]
    return [(label, path) for label, path in candidates if os.path.isfile(path)]


def _tail_lines(path: str, max_lines: int) -> list[str]:
    """Read up to the last max_lines from a text file."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    if max_lines <= 0:
        return []
    return [line.rstrip('\n') for line in lines[-max_lines:]]


def _sse(event: str, payload: dict) -> str:
    return f'event: {event}\ndata: {json.dumps(payload)}\n\n'


def _request_hash(room_dict: dict, request: StudyRequest) -> str:
    """Create a stable hash for de-duplicating equivalent study requests."""
    payload = {
        'room': room_dict,
        'context_buildings': [building.model_dump() for building in request.context_buildings],
        'run_sda': request.run_sda,
        'location': request.location,
        'quality': request.quality,
        'thresholds': request.thresholds.model_dump(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _energy_request_hash(room_dict: dict, request: EnergyRequest) -> str:
    payload = {
        'room': room_dict,
        'context_buildings': [building.model_dump() for building in request.context_buildings],
        'constructions': request.constructions.model_dump(),
        'building_program': request.building_program,
        'hvac_system': request.hvac_system,
        'natural_ventilation': request.natural_ventilation,
        'location': request.location,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _parse_db_timestamp(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # SQLite CURRENT_TIMESTAMP format: YYYY-MM-DD HH:MM:SS (UTC)
        return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_stale_running(record) -> bool:
    if record.status not in ('running', 'queued'):
        return False
    updated_at = _parse_db_timestamp(record.updated_at)
    if updated_at is None:
        # Legacy rows created before updated_at tracking are treated as stale.
        return True
    return datetime.now(timezone.utc) - updated_at > timedelta(hours=2)


def _recover_if_outputs_exist(study_id: str) -> bool:
    """If outputs exist on disk for a stale record, synthesize and persist final result."""
    project_folder = os.path.join(STUDIES_ROOT, study_id)
    df_path = os.path.join(project_folder, 'daylight_factor', 'results', 'floor_grid.res')
    if not os.path.isfile(df_path):
        return False

    try:
        df_result = parse_df(project_folder)
        sensor_points = parse_sensor_points(project_folder)

        sda_result = None
        annual_da = os.path.join(project_folder, 'annual_daylight', 'metrics', 'da', 'floor_grid.da')
        if os.path.isfile(annual_da):
            # Defaults match API defaults when thresholds are not custom-provided.
            sda_result = parse_annual(project_folder)

        store.update(
            study_id,
            status='complete',
            stage=None,
            result={
                'df': df_result,
                'sda': sda_result,
                'sensor_points': sensor_points,
            },
            error=None,
        )
        return True
    except Exception:
        return False


# ── POST /v1/studies ──────────────────────────────────────────────────────────

@router.post('/studies', response_model=StudyQueued, status_code=202)
def start_study(request: StudyRequest, background_tasks: BackgroundTasks):
    room_dict = request.room.to_simulation_dict()
    req_hash = _request_hash(room_dict, request)

    try:
        sensor_count = _sensor_count(room_dict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={'error': 'invalid_room', 'message': str(exc)})
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                'error': 'invalid_room',
                'message': f'Unable to process room geometry: {exc}',
            },
        )

    if request.run_sda and not request.location:
        raise HTTPException(
            status_code=400,
            detail={'error': 'missing_location', 'message': 'location is required when run_sda=true.'},
        )

    cached = store.find_by_request_hash(req_hash)
    if cached:
        return StudyQueued(
            study_id=cached.study_id,
            status=cached.status,
            sensor_count=sensor_count,
            links=_study_links(cached.study_id),
        )

    study_id = f'std_{uuid.uuid4().hex[:12]}'
    store.create(study_id, request_hash=req_hash)

    background_tasks.add_task(
        run_study,
        study_id=study_id,
        room_dict=room_dict,
        context_buildings=[building.model_dump() for building in request.context_buildings],
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

    if _is_stale_running(record):
        if not _recover_if_outputs_exist(study_id):
            store.update(
                study_id,
                status='failed',
                stage=None,
                error='Stale running study record detected. Please re-run the same request.',
            )
        record = store.get(study_id)

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

    if _is_stale_running(record):
        if not _recover_if_outputs_exist(study_id):
            store.update(
                study_id,
                status='failed',
                stage=None,
                error='Stale running study record detected. Please re-run the same request.',
            )
        record = store.get(study_id)

    if record.status == 'failed':
        raise HTTPException(status_code=500, detail={'error': 'simulation_failed', 'message': record.error or 'Unknown error.'})
    if record.status != 'complete':
        raise HTTPException(status_code=202, detail={'error': 'not_ready', 'message': f'Study is {record.status}.'})
    return _to_result_response(study_id, record.result)


# ── GET /v1/studies/{study_id}/logs ──────────────────────────────────────────

@router.get('/studies/{study_id}/logs')
def get_logs(study_id: str, lines: int = Query(default=200, ge=1, le=2000)):
    """Return a tail snapshot of known study log files."""
    record = store.get(study_id)
    if not record:
        raise HTTPException(status_code=404, detail={'error': 'not_found', 'message': f'Study {study_id} not found.'})

    logs = []
    for label, path in _study_log_files(study_id):
        logs.append({
            'name': label,
            'path': path,
            'lines': _tail_lines(path, lines),
        })

    return {
        'study_id': study_id,
        'status': record.status,
        'stage': record.stage,
        'error': record.error,
        'logs': logs,
    }


# ── GET /v1/studies/{study_id}/events (SSE) ──────────────────────────────────

@router.get('/studies/{study_id}/events')
async def stream_study_events(
    study_id: str,
    request: Request,
    tail_lines: int = Query(default=50, ge=0, le=500),
):
    """Stream status and appended log lines as Server-Sent Events."""
    record = store.get(study_id)
    if not record:
        raise HTTPException(status_code=404, detail={'error': 'not_found', 'message': f'Study {study_id} not found.'})

    async def event_generator():
        last_status = None
        last_stage = None
        offsets: dict[str, int] = {}

        # Initial status snapshot.
        current = store.get(study_id)
        if current:
            last_status = current.status
            last_stage = current.stage
            yield _sse('status', {
                'study_id': study_id,
                'status': current.status,
                'stage': current.stage,
                'error': current.error,
            })

        # Initial log tail snapshot.
        for label, path in _study_log_files(study_id):
            tail = _tail_lines(path, tail_lines)
            for line in tail:
                yield _sse('log', {'study_id': study_id, 'file': label, 'line': line})
            try:
                offsets[path] = os.path.getsize(path)
            except OSError:
                offsets[path] = 0

        # Live stream loop.
        while True:
            if await request.is_disconnected():
                break

            current = store.get(study_id)
            if current:
                if current.status != last_status or current.stage != last_stage:
                    last_status = current.status
                    last_stage = current.stage
                    yield _sse('status', {
                        'study_id': study_id,
                        'status': current.status,
                        'stage': current.stage,
                        'error': current.error,
                    })

                for label, path in _study_log_files(study_id):
                    start = offsets.get(path, 0)
                    try:
                        with open(path, 'r', encoding='utf-8', errors='replace') as f:
                            f.seek(start)
                            chunk = f.read()
                            offsets[path] = f.tell()
                    except OSError:
                        continue

                    if chunk:
                        for line in chunk.splitlines():
                            yield _sse('log', {'study_id': study_id, 'file': label, 'line': line})

                if current.status in ('complete', 'failed'):
                    yield _sse('done', {
                        'study_id': study_id,
                        'status': current.status,
                        'error': current.error,
                    })
                    break

            await asyncio.sleep(1)

    headers = {
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    }
    return StreamingResponse(event_generator(), media_type='text/event-stream', headers=headers)


# ── POST /v1/studies:run-sync ─────────────────────────────────────────────────

@router.post('/studies:run-sync', response_model=StudyResult)
async def run_sync(request: StudyRequest):
    """Run DF synchronously and return results directly. sDA is ignored."""
    room_dict = request.room.to_simulation_dict()

    try:
        _sensor_count(room_dict)  # validates inputs early
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={'error': 'invalid_room', 'message': str(exc)})
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                'error': 'invalid_room',
                'message': f'Unable to process room geometry: {exc}',
            },
        )

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
                    context_buildings=[building.model_dump() for building in request.context_buildings],
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


# ── EnergyPlus / EPSM endpoints ──────────────────────────────────────────────

def _energy_result(record, raw: dict) -> dict:
    if raw.get('error') or raw.get('error_message'):
        raise ValueError(raw.get('error_message') or raw.get('error'))
    floor_area = max(float(record.footprint_area or 0), 1e-9)
    heating = float(raw.get('heating_energy_kwh', 0) or 0)
    cooling = float(raw.get('cooling_energy_kwh', 0) or 0)
    total = float(raw.get('total_energy_kwh', heating + cooling) or 0)
    return {
        'study_id': record.study_id,
        'status': 'complete',
        'heating_energy_kwh': heating,
        'cooling_energy_kwh': cooling,
        'total_energy_kwh': total,
        'heating_demand_kwh_m2': heating / floor_area,
        'cooling_demand_kwh_m2': cooling / floor_area,
    }


def _poll_energy_record(record):
    # Local mode: the background task writes the result directly; nothing to poll.
    if not record.epsm_simulation_id:
        return record
    # EPSM fallback mode: poll the remote service for status updates.
    if record.status not in ('queued', 'running'):
        return record
    epsm = asyncio.run(simulation_status(record.epsm_simulation_id))
    status = str(epsm.get('status', 'running')).lower()
    if status in ('completed', 'complete'):
        raw = asyncio.run(simulation_results(record.epsm_simulation_id))
        store.update_energy(record.study_id, status='complete', stage=None,
                            result=_energy_result(record, raw), error=None)
    elif status in ('failed', 'error'):
        store.update_energy(record.study_id, status='failed', stage=None,
                            error=epsm.get('error', 'EPSM simulation failed'))
    else:
        store.update_energy(record.study_id, status='running', stage='energyplus_simulation')
    return store.get_energy(record.study_id)


@router.post('/energy/studies', response_model=EnergyQueued, status_code=202)
def start_energy_study(request: EnergyRequest, background_tasks: BackgroundTasks):
    room_dict = request.room.to_simulation_dict()
    req_hash = _energy_request_hash(room_dict, request)
    try:
        validate_room_inputs(load_room_inputs(room_dict))
        area = footprint_area(room_dict['footprint_coordinates']) * request.room.floors
    except Exception as exc:
        raise HTTPException(status_code=400, detail={'error': 'invalid_room', 'message': str(exc)})

    cached = store.find_energy_by_request_hash(req_hash)
    if cached:
        return EnergyQueued(study_id=cached.study_id, status=cached.status)
    study_id = f'eng_{uuid.uuid4().hex[:12]}'
    store.create_energy(study_id, request_hash=req_hash, footprint_area=area,
                        floors=request.room.floors)
    background_tasks.add_task(
        run_energy_study,
        study_id=study_id,
        room_dict=room_dict,
        context_buildings=[building.model_dump() for building in request.context_buildings],
        constructions=request.constructions.model_dump(),
        building_program=request.building_program,
        hvac_system=request.hvac_system,
        natural_ventilation=request.natural_ventilation,
        location=request.location,
    )
    return EnergyQueued(study_id=study_id, status='queued')


@router.get('/energy/studies/{study_id}', response_model=EnergyStatus)
def get_energy_status(study_id: str):
    record = store.get_energy(study_id)
    if not record:
        raise HTTPException(status_code=404, detail={'error': 'not_found', 'message': f'Energy study {study_id} not found.'})
    try:
        record = _poll_energy_record(record)
    except Exception as exc:
        store.update_energy(study_id, status='failed', stage=None, error=str(exc))
        record = store.get_energy(study_id)
    return EnergyStatus(study_id=record.study_id, status=record.status,
                        stage=record.stage, error=record.error)


@router.get('/energy/studies/{study_id}/result', response_model=EnergyResult)
def get_energy_result(study_id: str):
    record = store.get_energy(study_id)
    if not record:
        raise HTTPException(status_code=404, detail={'error': 'not_found', 'message': f'Energy study {study_id} not found.'})
    try:
        record = _poll_energy_record(record)
    except Exception as exc:
        store.update_energy(study_id, status='failed', stage=None, error=str(exc))
        record = store.get_energy(study_id)
    if record.status == 'failed':
        raise HTTPException(status_code=500, detail={'error': 'simulation_failed', 'message': record.error or 'Unknown error.'})
    if record.status != 'complete' or not record.result:
        raise HTTPException(status_code=202, detail={'error': 'not_ready', 'message': f'Energy study is {record.status}.'})
    return EnergyResult.model_validate(record.result)


@router.post('/energy/studies:run-sync', response_model=EnergyResult)
async def run_energy_sync(request: EnergyRequest):
    room_dict = request.room.to_simulation_dict()
    area = footprint_area(room_dict['footprint_coordinates']) * request.room.floors
    study_id = f'eng_{uuid.uuid4().hex[:12]}'
    store.create_energy(study_id, footprint_area=area, floors=request.room.floors)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(
            _executor,
            lambda: run_energy_study(
                study_id, room_dict,
                [building.model_dump() for building in request.context_buildings],
                request.constructions.model_dump(), request.building_program,
                request.hvac_system, request.natural_ventilation, request.location,
            ),
        ), timeout=30.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=202, detail={'error': 'timeout', 'message': f'Energy study is still running. Poll /v1/energy/studies/{study_id}.', 'study_id': study_id})
    record = _poll_energy_record(store.get_energy(study_id))
    if record.status == 'failed':
        raise HTTPException(status_code=500, detail={'error': 'simulation_failed', 'message': record.error})
    if record.status != 'complete':
        raise HTTPException(status_code=202, detail={'error': 'not_ready', 'message': f'Energy study is {record.status}.'})
    return EnergyResult.model_validate(record.result)


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

"""
api.routes
──────────
  POST /v1/daylight   daylight only (DF, optional DA/sDA)
  POST /v1/energy     energy only (annual totals, monthly balance, hourly loads)
  POST /v1/simulate   both, run at the same time

Each request is validated, put on a Celery queue, and answered when the
worker is done. Nothing is stored: results expire from Redis shortly after.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException

from worker import celery_app

from .auth import require_signature
from .schemas import (
    DaylightRequest,
    DaylightResult,
    EnergyRequest,
    EnergyResult,
    ErrorResponse,
    SimulateRequest,
    SimulateResult,
)

router = APIRouter(prefix='/v1', dependencies=[Depends(require_signature)])
_ERRORS = {code: {'model': ErrorResponse} for code in (400, 401, 500, 504)}


def _payload(request) -> dict:
    data = request.model_dump(mode='json')
    data['room'] = request.room.to_simulation_dict()
    return data


async def _result(job):
    """Wait for a Celery job without blocking the event loop, then unwrap it."""
    deadline = time.monotonic() + celery_app.app.conf.task_time_limit + 60
    while not job.ready():
        if time.monotonic() > deadline:
            raise HTTPException(504, detail={'error': 'timeout', 'message': 'Simulation took too long.'})
        await asyncio.sleep(0.25)
    if job.failed():
        raise HTTPException(500, detail={'error': 'simulation_failed', 'message': str(job.result)})
    result = job.result
    if result.get('error') == 'bad_input':
        raise HTTPException(400, detail=result)
    return result


@router.post('/daylight', response_model=DaylightResult, responses=_ERRORS)
async def daylight(request: DaylightRequest):
    return await _result(celery_app.daylight.delay(_payload(request)))


@router.post('/energy', response_model=EnergyResult, responses=_ERRORS)
async def energy(request: EnergyRequest):
    return await _result(celery_app.energy.delay(_payload(request)))


@router.post('/simulate', response_model=SimulateResult, responses=_ERRORS)
async def simulate(request: SimulateRequest):
    """Daylight and energy go to their own queues, so they run in parallel."""
    payload = _payload(request)
    daylight_job = celery_app.daylight.delay(payload)
    energy_job = celery_app.energy.delay(payload)
    daylight_result, energy_result = await asyncio.gather(_result(daylight_job), _result(energy_job))
    return {'daylight': daylight_result, 'energy': energy_result}

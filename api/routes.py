"""
api.routes
──────────
  POST /v1/daylight   daylight only (DF, optional DA/sDA)
  POST /v1/energy     energy only (annual totals, monthly balance, hourly loads)
  POST /v1/simulate   both, run at the same time

Each request is validated, put on a Celery queue, and answered when the
worker is done. Identical requests share one job and abandoned jobs are
cancelled (see api/jobs.py). Nothing is stored: results expire from Redis
shortly after.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request

from worker import celery_app

from .auth import require_api_key
from .jobs import submit, wait
from .schemas import (
    DaylightRequest,
    DaylightResult,
    EnergyRequest,
    EnergyResult,
    ErrorResponse,
    SimulateRequest,
    SimulateResult,
)

router = APIRouter(prefix='/v1', dependencies=[Depends(require_api_key)])
_ERRORS = {code: {'model': ErrorResponse} for code in (400, 401, 500, 503, 504)}


def _payload(request) -> dict:
    """Turn the validated request into the plain dict the Celery task receives."""
    data = request.model_dump(mode='json')
    data['room'] = request.room.to_simulation_dict()
    return data


@router.post('/daylight', response_model=DaylightResult, responses=_ERRORS)
async def daylight(body: DaylightRequest, request: Request):
    """Daylight factor per sensor, plus DA/sDA when daylight.run_sda is true."""
    return await wait(*submit(celery_app.daylight, _payload(body)), request)


@router.post('/energy', response_model=EnergyResult, responses=_ERRORS)
async def energy(body: EnergyRequest, request: Request):
    """Annual heating/cooling, monthly heat balance and hourly loads."""
    return await wait(*submit(celery_app.energy, _payload(body)), request)


@router.post('/simulate', response_model=SimulateResult, responses=_ERRORS)
async def simulate(body: SimulateRequest, request: Request):
    """Daylight and energy go to their own queues, so they run in parallel.

    The response waits for both. To show energy as soon as it is ready, call
    /v1/daylight and /v1/energy at the same time instead.
    """
    payload = _payload(body)
    daylight_result, energy_result = await asyncio.gather(
        wait(*submit(celery_app.daylight, payload), request),
        wait(*submit(celery_app.energy, payload), request),
    )
    return {'daylight': daylight_result, 'energy': energy_result}

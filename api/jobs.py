"""
api.jobs
────────
Submit simulation jobs and wait for them, with two shortcuts that keep the GPU
queue short:

  Identical requests share one job. The job key is a hash of only the fields
  the task reads, so a repeat (a student moving a slider back), or the daylight
  half of a /simulate where only the constructions changed, joins the running
  job or reuses its stored result instead of queuing another run.

  Abandoned jobs are cancelled. Each waiting client counts itself in Redis.
  When the last one disconnects before the job has started, the job is revoked
  and never takes a worker slot. A job that has already started is left to
  finish, because its result can still answer the next identical request.

Keys live as long as results (RESULT_TTL_SECONDS), so nothing builds up.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

from celery import states
from celery.result import AsyncResult
from fastapi import HTTPException, Request

from worker import celery_app

app = celery_app.app

# The request fields each task actually reads.
FIELDS = {
    'simulation.daylight': ('room', 'context_buildings', 'epw', 'pack', 'daylight'),
    'simulation.energy': ('room', 'context_buildings', 'epw', 'pack', 'energy'),
}
POLL_SECONDS = 0.1


def job_key(task_name: str, payload: dict) -> str:
    """Redis key for a job: the task name plus a hash of the fields it reads."""
    relevant = {field: payload.get(field) for field in FIELDS[task_name]}
    blob = json.dumps(relevant, sort_keys=True, separators=(',', ':')).encode()
    return f'job:{task_name}:{hashlib.sha256(blob).hexdigest()}'


def _redis():
    """The Redis client Celery already uses for results."""
    return app.backend.client


def submit(task, payload: dict) -> tuple[AsyncResult, str | None]:
    """Queue the job, or join an identical one that is queued, running or done.

    Returns the job and its key (None when Celery runs eagerly, as in tests).
    """
    if app.conf.task_always_eager:
        return task.delay(payload), None
    redis, key, ttl = _redis(), job_key(task.name, payload), app.conf.result_expires
    while True:
        task_id = uuid.uuid4().hex
        if redis.set(key, task_id, nx=True, ex=ttl):
            try:
                task.apply_async((payload,), task_id=task_id)
            except Exception:
                redis.delete(key)
                raise
            return AsyncResult(task_id, app=app), key
        existing = redis.get(key)
        if existing:  # else it expired between set and get: try again
            return AsyncResult(existing.decode(), app=app), key


def _forget(key: str, task_id: str) -> None:
    """Drop the key if it still points at this job, so the next request runs afresh."""
    redis = _redis()
    if (redis.get(key) or b'').decode() == task_id:
        redis.delete(key)


async def wait(job: AsyncResult, key: str | None, request: Request) -> dict:
    """Wait for the job without blocking the event loop, then unwrap its result.

    Stops early if the client disconnects; the job is then cancelled if nobody
    else is waiting for it and it has not started yet.
    """
    waiters = f'waiters:{job.id}'
    if key:
        _redis().incr(waiters)
        _redis().expire(waiters, app.conf.result_expires)
    deadline = time.monotonic() + app.conf.task_time_limit + 60
    try:
        while not job.ready():
            if time.monotonic() > deadline:
                raise HTTPException(504, detail={'error': 'timeout', 'message': 'Simulation took too long.'})
            if await request.is_disconnected():
                raise HTTPException(499, detail={'error': 'client_gone', 'message': 'Client disconnected.'})
            await asyncio.sleep(POLL_SECONDS)
    finally:
        if key and _redis().decr(waiters) <= 0:
            _redis().delete(waiters)
            if job.state == states.PENDING:
                app.control.revoke(job.id)  # still queued: the worker will skip it
                _forget(key, job.id)
    if job.state == states.REVOKED:
        # Rare: joined a job just as its last waiter cancelled it.
        raise HTTPException(503, detail={'error': 'cancelled', 'message': 'Job was cancelled, please retry.'})
    if job.failed():
        if key:
            _forget(key, job.id)  # do not serve a crash to the next identical request
        raise HTTPException(500, detail={'error': 'simulation_failed', 'message': str(job.result)})
    result = job.result
    if result.get('error') == 'bad_input':
        raise HTTPException(400, detail=result)
    return result

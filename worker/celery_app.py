"""
worker.celery_app
─────────────────
Celery app and the two tasks. Each task goes to its own queue so the hardware
can be matched to the work:

  daylight  Foton ray tracing. Run one worker process per GPU (concurrency 1).
  energy    EnergyPlus. Run about one worker process per CPU core.

Workers pull a job only when they are free (acks_late + prefetch 1), so jobs
spread across every worker on every machine attached to the same Redis.
Results live in Redis for RESULT_TTL_SECONDS and then expire; clients keep
their own copies.

Start a worker:
  celery -A worker.celery_app worker -Q daylight --concurrency 1
  celery -A worker.celery_app worker -Q energy   --concurrency $(nproc)
"""

from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

app = Celery('simulation', broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    result_compression='gzip',
    result_expires=int(os.environ.get('RESULT_TTL_SECONDS', '3600')),
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=int(os.environ.get('TASK_TIME_LIMIT_SECONDS', '1200')),
    worker_max_tasks_per_child=int(os.environ.get('WORKER_MAX_TASKS_PER_CHILD', '200')),
    broker_connection_retry_on_startup=True,
    task_routes={
        'simulation.daylight': {'queue': 'daylight'},
        'simulation.energy': {'queue': 'energy'},
    },
)


def _run(job, request: dict) -> dict:
    from .jobs import BadInput

    try:
        return job(request)
    except BadInput as exc:
        # Returned, not raised: a client error is a normal outcome for the API.
        return {'error': 'bad_input', 'message': str(exc)}


@app.task(name='simulation.daylight')
def daylight(request: dict) -> dict:
    from .jobs import daylight_job

    return _run(daylight_job, request)


@app.task(name='simulation.energy')
def energy(request: dict) -> dict:
    from .jobs import energy_job

    return _run(energy_job, request)

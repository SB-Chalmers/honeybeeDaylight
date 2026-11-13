"""
worker.celery_app
─────────────────
Celery app and the two tasks. Each task goes to its own queue so the hardware
can be matched to the work:

  daylight  Foton. Mostly CPU setup, a little GPU tracing: one process per core.
  energy    EnergyPlus. One process per core.

Workers pull a job only when they are free (acks_late + prefetch 1), so jobs
spread across every worker on every machine attached to the same Redis.
Results live in Redis for RESULT_TTL_SECONDS and then expire; clients keep
their own copies.

Start a worker (docker-compose.yml picks the counts with worker/concurrency.py):
  celery -A worker.celery_app worker -Q daylight --concurrency 8
  celery -A worker.celery_app worker -Q energy   --concurrency 8
"""

from __future__ import annotations

import os
import socket
import time

from billiard.process import current_process
from celery import Celery

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

app = Celery('simulation', broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    result_compression='gzip',
    result_expires=int(os.environ.get('RESULT_TTL_SECONDS', '600')),
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=int(os.environ.get('TASK_TIME_LIMIT_SECONDS', '1200')),
    worker_max_tasks_per_child=int(os.environ.get('WORKER_MAX_TASKS_PER_CHILD', '200')),
    broker_connection_retry_on_startup=True,
    task_routes={
        'simulation.daylight': {'queue': 'daylight'},
        'simulation.energy': {'queue': 'energy'},
        # RC runs in about 0.1 s, so it shares the CPU queue with EnergyPlus.
        'simulation.energy_rc': {'queue': 'energy'},
    },
)


def _run(job, request: dict) -> dict:
    """Run a job; turn a client mistake into a result the API answers with 400."""
    from .jobs import BadInput

    started_at = time.time()
    try:
        result = job(request)
    except BadInput as exc:
        # Returned, not raised: a client error is a normal outcome for the API.
        return {'error': 'bad_input', 'message': str(exc)}
    # Which worker slot ran the job and when it started: lets the stress test
    # replay how jobs queued and spread over the workers.
    slot = getattr(current_process(), 'index', 0)
    return {**result, 'worker': f'{socket.gethostname()}:{slot}', 'started_at': round(started_at, 3)}


@app.task(name='simulation.daylight')
def daylight(request: dict) -> dict:
    """Celery task on the "daylight" (GPU) queue."""
    from .jobs import daylight_job

    return _run(daylight_job, request)


@app.task(name='simulation.energy')
def energy(request: dict) -> dict:
    """Celery task on the "energy" (CPU) queue."""
    from .jobs import energy_job

    return _run(energy_job, request)


@app.task(name='simulation.energy_rc')
def energy_rc(request: dict) -> dict:
    """Celery task on the "energy" (CPU) queue: the energy job on EPSM RC."""
    from .jobs import energy_rc_job

    return _run(energy_rc_job, request)

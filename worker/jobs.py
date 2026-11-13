"""
worker.jobs
───────────
The two simulation jobs, as plain functions so they can be tested without
Celery. Each takes the validated request as a dict, rebuilds what it needs in
a throwaway folder, and returns a compact, JSON-ready result. Nothing is kept
on disk afterwards.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import numpy as np

from . import packing
from simulation.energy.epsm_client import fetch_constructions
from simulation.energy.simulate import run_energy
from simulation.inputs import GRID_DIM, load_room_inputs, validate_room_inputs
from simulation.model import build_model
from simulation.runner import df_summary, run_daylight, sda_summary

WEATHER_DIR = Path(os.environ.get('WEATHER_DIR', Path(__file__).resolve().parent.parent / 'weather'))
DEFAULT_EPW = WEATHER_DIR / os.environ.get('DEFAULT_EPW', 'gothenburg_tmyx.epw')

# Sky matrices for the default EPW are computed once per worker process and
# reused. Uploaded EPWs are not memoised, so memory does not grow with uploads.
_DEFAULT_WEATHER_MEMO: dict = {}


class BadInput(ValueError):
    """A problem the client can fix. The API turns it into HTTP 400."""


def _weather(epw: str | None, folder: str) -> tuple[str, str]:
    """Return (EPW path, label): the default file, or the uploaded text written to folder."""
    if not epw:
        return str(DEFAULT_EPW), f'default:{DEFAULT_EPW.name}'
    if not epw.lstrip().upper().startswith('LOCATION'):
        raise BadInput('epw does not look like an EPW file (it should start with LOCATION).')
    path = os.path.join(folder, 'weather.epw')
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(epw)
    return path, 'uploaded'


def build(room: dict, context: list[dict]):
    """Rebuild the Honeybee model (and its sensor grids) from the compact room description."""
    try:
        cfg = load_room_inputs(room)
        validate_room_inputs(cfg)
        return build_model(cfg, context)
    except Exception as exc:
        raise BadInput(f'Unable to build the model from room: {exc}') from exc


def check(room: dict) -> None:
    """The room checks of build() without building the Honeybee model, for jobs that do not use it."""
    try:
        validate_room_inputs(load_room_inputs(room))
    except Exception as exc:
        raise BadInput(f'Unable to build the model from room: {exc}') from exc


def _array(values, pack: bool, kind: str, decimals: int):
    """Encode a result array: packed base64 (see packing.py) or a rounded JSON list."""
    if values is None:
        return None
    if pack:
        return getattr(packing, kind)(values)
    return np.round(np.asarray(values, dtype=np.float64), decimals).tolist()


def daylight_job(request: dict) -> dict:
    """Rebuild the model, run Foton DF (and DA/sDA if asked), return compact results."""
    started = time.perf_counter()
    opts = request.get('daylight') or {}
    da_lux = opts.get('da_lux', 300)
    target = opts.get('sda_target_pct', 50.0)
    pack = request.get('pack', True)
    with tempfile.TemporaryDirectory(prefix='daylight_') as folder:
        epw_path, weather = _weather(request.get('epw'), folder)
        model, grid = build(request['room'], request.get('context_buildings', []))
        if grid.count == 0:
            spacing = request['room'].get('sensor_grid') or {}
            raise BadInput(
                'No sensor fits on the floor: the room is too small or narrow for a '
                f"{spacing.get('x_dim', GRID_DIM):g} × {spacing.get('y_dim', GRID_DIM):g} m sensor grid."
            )
        result = run_daylight(
            model, grid.count, epw_path,
            run_sda=opts.get('run_sda', False),
            da_lux=da_lux,
            sda_target_pct=target,
            quality=opts.get('quality', 'preview'),
            weather_memo=_DEFAULT_WEATHER_MEMO if weather.startswith('default') else None,
        )
    df, da = result['df'], result['da']
    return {
        'weather': weather,
        'seconds': round(time.perf_counter() - started, 3),
        'sensor_count': int(df.size),
        'sensor_grids': result['grids'],
        'sensor_points': _array(result['points'], pack, 'points', 3),
        'df_summary': df_summary(df),
        'df': _array(df, pack, 'percent', 2),
        'sda_summary': sda_summary(da, da_lux, target) if da is not None else None,
        'da': _array(da, pack, 'percent', 2),
    }


def energy_job(request: dict) -> dict:
    """Fetch constructions from EPSM, run EnergyPlus, return totals and load profiles."""
    started = time.perf_counter()
    opts = request['energy']
    pack = request.get('pack', True)
    room = request['room']
    context = request.get('context_buildings', [])
    build(room, context)  # same geometry checks as daylight, so errors read the same
    try:
        constructions = fetch_constructions(opts['constructions'])
    except LookupError as exc:
        raise BadInput(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix='energy_') as folder:
        epw_path, weather = _weather(request.get('epw'), folder)
        result = run_energy(
            room, context, constructions,
            opts.get('building_program', 'Office'),
            opts.get('natural_ventilation', False),
            epw_path, os.path.join(folder, 'run'),
        )
    for key in ('heating_hourly_kwh', 'cooling_hourly_kwh'):
        result[key] = _array(result.get(key), pack, 'series', 4)
    return {'weather': weather, 'seconds': round(time.perf_counter() - started, 3), **result}


def energy_rc_job(request: dict) -> dict:
    """The energy job on EPSM RC (ISO 52016-1) instead of EnergyPlus; same request and result."""
    from simulation.energy.rc import run_energy_rc

    started = time.perf_counter()
    opts = request['energy']
    pack = request.get('pack', True)
    room = request['room']
    context = request.get('context_buildings', [])
    check(room)  # same room checks as daylight and EnergyPlus, so errors read the same
    try:
        constructions = fetch_constructions(opts['constructions'])
    except LookupError as exc:
        raise BadInput(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix='energy_rc_') as folder:
        epw_path, weather = _weather(request.get('epw'), folder)
        try:
            result = run_energy_rc(
                room, context, constructions,
                opts.get('building_program', 'Office'),
                opts.get('natural_ventilation', False),
                epw_path,
            )
        except ValueError as exc:
            # EPSM rejects inputs it cannot model (for example an unreadable EPW) before solving.
            raise BadInput(f'EPSM RC cannot model this request: {exc}') from exc
    for key in ('heating_hourly_kwh', 'cooling_hourly_kwh'):
        result[key] = _array(result.get(key), pack, 'series', 4)
    return {'weather': weather, 'seconds': round(time.perf_counter() - started, 3), **result}

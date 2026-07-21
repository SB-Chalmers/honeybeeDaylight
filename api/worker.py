"""
api.worker
──────────
Background task that runs a study end-to-end and writes the result to the store.
Called by FastAPI BackgroundTasks — runs in a thread pool, not an async coroutine.
"""

from __future__ import annotations

import os

from ladybug.wea import Wea

from simulation.inputs import load_room_inputs, validate_room_inputs
from simulation.model import build_model, export_model
from simulation.results import parse_annual, parse_df
from simulation.runner import run_annual, run_df

from .store import store

STUDIES_ROOT = os.path.abspath('./studies')
WEATHER_DIR  = os.path.abspath('./weather')


def _epw_path(location: str) -> str:
    """Resolve a location name to an EPW file path."""
    path = os.path.join(WEATHER_DIR, f'{location}.epw')
    if not os.path.exists(path):
        raise FileNotFoundError(
            f'No EPW file found for location "{location}". '
            f'Expected: {path}'
        )
    return path


def run_study(
    study_id: str,
    room_dict: dict,
    run_sda: bool,
    location: str | None,
    draft: bool,
    da_lux: int,
    sda_target_pct: float,
) -> None:
    """
    Execute a full study and store the result.
    Intended to be called as a FastAPI BackgroundTask.
    """
    project_folder = os.path.join(STUDIES_ROOT, study_id)
    os.makedirs(project_folder, exist_ok=True)

    try:
        # ── Build model ───────────────────────────────────────────────────────
        store.update(study_id, status='running', stage='building_model')
        cfg = load_room_inputs(room_dict)
        validate_room_inputs(cfg)
        model, grid = build_model(cfg)
        model_path = export_model(model, project_folder)

        # ── Run DF ────────────────────────────────────────────────────────────
        store.update(study_id, stage='daylight_factor_ray_tracing')
        run_df(model_path, project_folder, grid.count, draft=draft)
        df_result = parse_df(project_folder)

        # ── Run sDA (optional) ────────────────────────────────────────────────
        sda_result = None
        if run_sda:
            if not location:
                raise ValueError('location is required when run_sda=true.')
            epw = _epw_path(location)
            wea_path = os.path.join(project_folder, 'weather.wea')
            Wea.from_epw_file(epw).write(wea_path)

            store.update(study_id, stage='annual_daylight_ray_tracing')
            run_annual(model_path, project_folder, grid.count, wea_path, draft=draft)
            sda_result = parse_annual(
                project_folder,
                da_lux=da_lux,
                sda_target_pct=sda_target_pct,
            )

        # ── Store result ──────────────────────────────────────────────────────
        store.update(
            study_id,
            status='complete',
            stage=None,
            result={'df': df_result, 'sda': sda_result},
        )

    except Exception as exc:
        store.update(study_id, status='failed', stage=None, error=str(exc))
        raise

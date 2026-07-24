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
from simulation.results import parse_annual, parse_df, parse_sensor_points
from simulation.runner import run_annual, run_df
from simulation.energy.epsm_client import fetch_constructions, submit_idf
from simulation.energy.idf_builder import build_idf
from simulation.energy.local_runner import run_local_eplus

# Set USE_LOCAL_EPLUS=false to fall back to the remote EPSM service.
USE_LOCAL_EPLUS = os.environ.get('USE_LOCAL_EPLUS', 'true').lower() not in ('0', 'false', 'no')

from .store import store

STUDIES_ROOT = os.path.abspath('./studies')
WEATHER_DIR  = os.path.abspath('./weather')


def _epw_path(location: str, energy: bool = False) -> str:
    """Resolve a location name to an EPW file path.

    For energy simulations, prefer a *_tmyx.epw variant when one exists
    because the EPSM service uses TMYx EPWs and mismatched files can
    cause EnergyPlus to skip all environment periods.
    """
    if energy:
        tmyx = os.path.join(WEATHER_DIR, f'{location}_tmyx.epw')
        if os.path.exists(tmyx):
            return tmyx
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
    context_buildings: list[dict],
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
        model, grid = build_model(cfg, context_buildings)
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
            result={
                'df': df_result,
                'sda': sda_result,
                'sensor_points': parse_sensor_points(project_folder),
            },
        )

    except Exception as exc:
        store.update(study_id, status='failed', stage=None, error=str(exc))
        raise


def run_energy_study(
    study_id: str,
    room_dict: dict,
    context_buildings: list[dict],
    constructions: dict,
    building_program: str,
    hvac_system: str,
    natural_ventilation: bool,
    location: str,
) -> None:
    """Build an EnergyPlus IDF, submit it to EPSM, and persist its ID."""
    project_folder = os.path.join(STUDIES_ROOT, study_id)
    os.makedirs(project_folder, exist_ok=True)
    try:
        store.update_energy(study_id, status='running', stage='fetching_constructions')
        construction_records = __import__('asyncio').run(fetch_constructions(constructions))
        store.update_energy(study_id, stage='idf_generation')
        idf_text, floor_area = build_idf(
            room_dict, construction_records, building_program, hvac_system,
            natural_ventilation, context_buildings,
        )
        idf_path = os.path.join(project_folder, 'energy.idf')
        with open(idf_path, 'w', encoding='utf-8') as idf_file:
            idf_file.write(idf_text)
        epw_path = _epw_path(location, energy=True)
        store.update_energy(study_id, footprint_area=floor_area)
        if USE_LOCAL_EPLUS:
            store.update_energy(study_id, stage='energyplus_simulation')
            eplus_dir = os.path.join(project_folder, 'eplus_run')
            parsed = run_local_eplus(idf_path, epw_path, eplus_dir)
            store.update_energy(
                study_id,
                status='complete',
                stage=None,
                result=parsed,
            )
        else:
            store.update_energy(study_id, stage='submitting_to_epsm')
            epsm_id = __import__('asyncio').run(submit_idf(idf_path, epw_path))
            store.update_energy(study_id, epsm_simulation_id=epsm_id,
                                stage='energyplus_simulation')
    except Exception as exc:
        store.update_energy(study_id, status='failed', stage=None, error=str(exc))
        raise

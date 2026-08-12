"""Run Honeybee daylight studies with Foton.

Foton performs scene conversion, ray transport, and daylight metric reduction.
Annual weather preparation still uses ``gendaymtx``, which is the sole Radiance
binary required by this module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from foton import Engine
from foton.honeybee import run_annual_daylight
from foton.honeybee.adapter import prepare_honeybee_scene


DF_QUALITY = {
    True: {
        'quality': 'preview',
        'sky_rows': 146,
        'direct_samples': 1,
        'maximum_samples': 64,
        'maximum_bounces': 1,
    },
    False: {
        'quality': 'final',
        'sky_rows': 578,
        'direct_samples': 64,
        'maximum_samples': 4096,
        'maximum_bounces': 2,
    },
}


def _backend() -> str:
    """Return the requested Foton backend (auto-select by default)."""
    return os.environ.get('FOTON_BACKEND', 'auto').strip().lower() or 'auto'


def _check_sensor_count(actual: int, expected: int) -> None:
    if actual != expected:
        raise RuntimeError(
            f'Foton prepared {actual} sensors, but the Honeybee model reported '
            f'{expected}.'
        )


def _write_sensor_points(prepared, output_folder: Path) -> None:
    """Write Honeybee-style ``.pts`` files for the API result coordinates."""
    grid_folder = output_folder / 'model' / 'grid'
    grid_folder.mkdir(parents=True, exist_ok=True)
    positions = prepared.arrays['sensor_positions']
    normals = prepared.arrays['sensor_normals']

    for grid in prepared.grid_info:
        start = int(grid['start_sensor_index'])
        end = start + int(grid['sensor_count'])
        points = np.column_stack((positions[start:end], normals[start:end]))
        np.savetxt(
            grid_folder / f"{grid['full_identifier']}.pts",
            points,
            fmt='%.8f',
        )


def run_df(model_path: str, project_folder: str, sensor_count: int,
           *, draft: bool = True) -> None:
    """Run daylight factor natively with Foton and write compatible results."""
    output = Path(project_folder).resolve() / 'daylight_factor'
    results_folder = output / 'results'
    results_folder.mkdir(parents=True, exist_ok=True)

    prepared = prepare_honeybee_scene(
        model_path,
        include_aperture_glazing=True,
    )
    actual_count = int(prepared.arrays['sensor_positions'].shape[0])
    _check_sensor_count(actual_count, sensor_count)

    engine = Engine({'backend': _backend()})
    scene = prepared.create_native_scene(engine)
    settings = DF_QUALITY[draft]

    # DF is evaluated against Foton's normalized CIE overcast sky. The sky
    # values passed here establish only the patch basis and timestep count.
    sky = np.zeros((settings['sky_rows'], 1, 3), dtype=np.float32)
    occupancy = np.ones(1, dtype=np.float32)
    result = scene.analyze(
        sky,
        occupancy,
        quality=settings['quality'],
        metrics=['df'],
        direct_samples=settings['direct_samples'],
        maximum_samples=settings['maximum_samples'],
        maximum_bounces=settings['maximum_bounces'],
        scene_seed=0,
    ).result()
    values = result.daylight_factor()
    if values is None:
        raise RuntimeError('Foton did not return daylight-factor results.')
    values = np.asarray(values, dtype=np.float32)

    for grid in prepared.grid_info:
        start = int(grid['start_sensor_index'])
        end = start + int(grid['sensor_count'])
        np.savetxt(
            results_folder / f"{grid['full_identifier']}.res",
            values[start:end],
            fmt='%.6f',
        )

    _write_sensor_points(prepared, output)
    metadata = {
        'engine': 'foton',
        'backend': dict(engine.capabilities()),
        'quality': settings,
        'solver': json.loads(result.metadata_json()),
        'timings': dict(result.timings()),
    }
    (output / 'metadata.json').write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding='utf-8',
    )


def run_annual(model_path: str, project_folder: str, sensor_count: int,
               wea_path: str, *, draft: bool = True, da_lux: int = 300,
               sda_target_pct: float = 50.0) -> None:
    """Run annual daylight with Foton and write Honeybee-compatible metrics."""
    run = run_annual_daylight(
        model=model_path,
        wea=wea_path,
        output_folder=Path(project_folder).resolve() / 'annual_daylight',
        backend=_backend(),
        quality='preview' if draft else 'final',
        threshold=float(da_lux),
        target_time=float(sda_target_pct),
        export_illuminance=False,
    )
    actual_count = int(run.metadata['sensor_count'])
    _check_sensor_count(actual_count, sensor_count)

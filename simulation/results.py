"""
simulation.results
──────────────────
Parse Honeybee recipe output files into plain dicts matching the API response
schema:

  DF  → { summary: {mean_df, min_df, max_df}, values: [float, ...] }
  sDA → { summary: {sda_300_50, sensor_count, passing_sensors},
           values: [float, ...], pass: [bool, ...] }

Public API
----------
parse_df(project_folder, grid_name='floor_grid') -> dict
parse_annual(project_folder, grid_name='floor_grid',
             da_lux=300, sda_target_pct=50) -> dict
parse_sensor_points(project_folder, grid_name='floor_grid') -> list[list[float]]
"""

import json
from pathlib import Path

import numpy as np


def parse_sensor_grids(project_folder: str, grid_name: str = 'floor_grid') -> list[dict]:
    """Return ordered grid slices for the flattened point-cloud arrays."""
    manifest_path = Path(project_folder) / 'daylight_factor' / 'grids_info.json'
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    root = Path(project_folder)
    candidates = (
        root / 'daylight_factor' / 'results' / f'{grid_name}.res',
        root / 'annual_daylight' / 'metrics' / 'da' / f'{grid_name}.da',
        root / 'daylight_factor' / 'model' / 'grid' / f'{grid_name}.pts',
    )
    source = next((path for path in candidates if path.is_file()), candidates[0])
    values = np.atleast_1d(np.loadtxt(source))
    sensor_count = int(values.shape[0]) if values.ndim > 1 else int(values.size)
    return [{
        'identifier': grid_name,
        'full_identifier': grid_name,
        'room_identifier': 'Daylight_Room',
        'floor_number': None,
        'start_sensor_index': 0,
        'sensor_count': sensor_count,
    }]


def parse_sensor_points(project_folder: str, grid_name: str = 'floor_grid') -> list[list[float]]:
    """Parse sensor point coordinates from Honeybee grid .pts output.

    Returns [x, y, z] points in the same ordering as the result files.
    """
    points: list[list[float]] = []
    grid_folder = Path(project_folder) / 'daylight_factor' / 'model' / 'grid'
    for grid in parse_sensor_grids(project_folder, grid_name):
        pts_path = grid_folder / f"{grid['full_identifier']}.pts"
        with pts_path.open('r', encoding='utf-8') as stream:
            for raw in stream:
                parts = raw.strip().split()
                if len(parts) < 3:
                    continue
                points.append([float(parts[0]), float(parts[1]), float(parts[2])])

    return points


def parse_df(project_folder: str, grid_name: str = 'floor_grid') -> dict:
    """
    Parse daylight-factor results.

    Parameters
    ----------
    project_folder : str
        Root folder passed to RecipeSettings (e.g. ./daylight_factor_sim).
    grid_name : str
        Sensor grid identifier used when the recipe was run.
    """
    results_folder = Path(project_folder) / 'daylight_factor' / 'results'
    arrays = [
        np.atleast_1d(np.loadtxt(results_folder / f"{grid['full_identifier']}.res"))
        for grid in parse_sensor_grids(project_folder, grid_name)
    ]
    values = np.concatenate(arrays)
    return {
        'summary': {
            'mean_df': round(float(np.mean(values)), 3),
            'min_df':  round(float(np.min(values)), 3),
            'max_df':  round(float(np.max(values)), 3),
        },
        'values': values.tolist(),
    }


def parse_annual(project_folder: str, grid_name: str = 'floor_grid',
                 da_lux: int = 300, sda_target_pct: float = 50.0) -> dict:
    """
    Parse annual-daylight DA results and compute sDA.

    Parameters
    ----------
    project_folder : str
        Root folder passed to RecipeSettings (e.g. ./annual_daylight_sim).
    grid_name : str
        Sensor grid identifier.
    da_lux : int
        Lux threshold used by the recipe (informational; baked into .da files).
    sda_target_pct : float
        Percentage threshold for the sDA pass/fail mask.
    """
    da_folder = Path(project_folder) / 'annual_daylight' / 'metrics' / 'da'
    arrays = [
        np.atleast_1d(np.loadtxt(da_folder / f"{grid['full_identifier']}.da"))
        for grid in parse_sensor_grids(project_folder, grid_name)
    ]
    da_values = np.concatenate(arrays)
    pass_mask = (da_values >= sda_target_pct).tolist()
    passing = int(sum(pass_mask))
    total = len(pass_mask)

    return {
        'summary': {
            'sda_300_50':      round(passing / total * 100.0, 2),
            'sensor_count':    total,
            'passing_sensors': passing,
        },
        'values': da_values.tolist(),
        'pass':   pass_mask,
    }

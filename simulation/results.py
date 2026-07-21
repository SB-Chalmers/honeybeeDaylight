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
"""

import os

import numpy as np


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
    res_path = os.path.join(
        project_folder, 'daylight_factor', 'results', f'{grid_name}.res'
    )
    values = np.loadtxt(res_path)
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
    da_path = os.path.join(
        project_folder, 'annual_daylight', 'metrics', 'da', f'{grid_name}.da'
    )
    da_values = np.loadtxt(da_path)
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

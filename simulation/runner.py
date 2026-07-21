"""
simulation.runner
─────────────────
Execute Honeybee recipes and manage output folders.

Public API
----------
run_df(model_path, project_folder, sensor_count, *, draft=True) -> None
run_annual(model_path, project_folder, sensor_count, wea_path, *, draft=True) -> None

Both functions resolve the queenbee binary from the current Python executable's
venv so subprocess tasks always use the right CLI tools.
"""

import multiprocessing
import os
import sys

from lbt_recipes.recipe import Recipe
from lbt_recipes.settings import RecipeSettings

# ── Venv path setup (must run before any subprocess is spawned) ───────────────

_venv_bin = os.path.dirname(sys.executable)
os.environ['PATH'] = _venv_bin + os.pathsep + os.environ.get('PATH', '')
_pycompat = os.path.abspath('./pycompat')
os.environ['PYTHONPATH'] = _pycompat + os.pathsep + os.environ.get('PYTHONPATH', '')

_QUEENBEE = os.path.join(_venv_bin, 'queenbee')

# ── Radiance parameters ───────────────────────────────────────────────────────

RAD_PARAMS = {
    'daylight_factor': {
        True:  '-ab 1 -aa 0.25 -ad 256 -ar 16',
        False: '-ab 2 -aa 0.1  -ad 2048 -ar 64',
    },
    'annual_daylight': {
        True:  '-ab 1 -ad 512  -lw 2e-05 -dr 0',
        False: '-ab 2 -ad 5000 -lw 2e-05 -dr 0',
    },
    'annual_irradiance': {
        True:  '-ab 1 -ad 512  -lw 2e-05 -dr 0',
        False: '-ab 2 -ad 5000 -lw 2e-05 -dr 0',
    },
    'sky_view': {
        True:  '-aa 0.1 -ad 256  -ar 16',
        False: '-aa 0.1 -ad 2048 -ar 64',
    },
    'point_in_time_grid': {
        True:  '-ab 1 -aa 0.25 -ad 256 -ar 16',
        False: '-ab 2 -aa 0.1  -ad 2048 -ar 64',
    },
    'direct_sun_hours': {True: None, False: None},
}


# ── Internal helper ───────────────────────────────────────────────────────────

def _base_settings(project_folder: str) -> RecipeSettings:
    return RecipeSettings(
        folder=project_folder,
        workers=multiprocessing.cpu_count(),
        reload_old=False,
    )


def _run(recipe_name: str, model_path: str, project_folder: str,
         sensor_count: int, draft: bool, extra_inputs: dict | None = None) -> None:
    recipe = Recipe(recipe_name)
    recipe.input_value_by_name('model', model_path)
    recipe.input_value_by_name('min-sensor-count', sensor_count)

    rad = RAD_PARAMS.get(recipe_name, {}).get(draft)
    if rad:
        recipe.input_value_by_name('radiance-parameters', rad)

    for name, value in (extra_inputs or {}).items():
        recipe.input_value_by_name(name, value)

    recipe.run(_base_settings(project_folder), queenbee_path=_QUEENBEE)


# ── Public API ────────────────────────────────────────────────────────────────

def run_df(model_path: str, project_folder: str, sensor_count: int,
           *, draft: bool = True) -> None:
    """Run the daylight-factor recipe."""
    _run('daylight-factor', model_path, project_folder, sensor_count, draft)


def run_annual(model_path: str, project_folder: str, sensor_count: int,
               wea_path: str, *, draft: bool = True) -> None:
    """Run the annual-daylight recipe with the supplied WEA file."""
    _run('annual_daylight', model_path, project_folder, sensor_count, draft,
         extra_inputs={'wea': wea_path})

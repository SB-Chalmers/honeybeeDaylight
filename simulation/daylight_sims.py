"""CLI entry point — multi-recipe simulation (JSON-driven).

Run from the project root:
    python simulation/daylight_sims.py

Room inputs are read from ./study_info.json (room_inputs key).
Configure RECIPE, DRAFT, and EPW_PATH below.
"""

import os

from ladybug.wea import Wea

from simulation.inputs import load_room_inputs, validate_room_inputs
from simulation.model import build_model, export_model
from simulation.runner import RAD_PARAMS, _QUEENBEE, _base_settings
from lbt_recipes.recipe import Recipe

# ── Config ────────────────────────────────────────────────────────────────────
# Pick one: daylight_factor | sky_view | point_in_time_grid
#           annual_daylight | annual_irradiance | direct_sun_hours
RECIPE     = 'annual_daylight'
DRAFT      = True
EPW_PATH   = os.path.abspath('./weather/gothenburg.epw')
SKY_STRING = 'cie -alt 54 -az 180 -type 0'  # used by point_in_time_grid only

INPUT_JSON     = os.path.abspath('./study_info.json')
PROJECT_FOLDER = os.path.abspath(f'./{RECIPE}_sim')

if __name__ == '__main__':
    cfg = load_room_inputs(INPUT_JSON)
    validate_room_inputs(cfg)
    model, grid = build_model(cfg)
    model_path = export_model(model, PROJECT_FOLDER)

    recipe = Recipe(RECIPE)
    recipe.input_value_by_name('model', model_path)
    recipe.input_value_by_name('min-sensor-count', len(grid.sensors))

    rad = RAD_PARAMS.get(RECIPE, {}).get(DRAFT)
    if rad:
        recipe.input_value_by_name('radiance-parameters', rad)

    if RECIPE in ('annual_daylight', 'annual_irradiance', 'direct_sun_hours'):
        wea = Wea.from_epw_file(EPW_PATH)
        wea_path = os.path.join(PROJECT_FOLDER, 'weather.wea')
        os.makedirs(PROJECT_FOLDER, exist_ok=True)
        wea.write(wea_path)
        recipe.input_value_by_name('wea', wea_path)

    if RECIPE == 'point_in_time_grid':
        recipe.input_value_by_name('sky', SKY_STRING)

    recipe.run(_base_settings(PROJECT_FOLDER), queenbee_path=_QUEENBEE)
    print(f'Done -> {PROJECT_FOLDER}')

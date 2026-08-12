"""Run a JSON-configured Foton daylight simulation from the project root."""

import os

from ladybug.wea import Wea

from simulation.inputs import load_room_inputs, validate_room_inputs
from simulation.model import build_model, export_model
from simulation.runner import run_annual, run_df


# Foton currently supports the two recipes used by the API.
RECIPE = 'annual_daylight'  # daylight_factor | annual_daylight
DRAFT = True
EPW_PATH = os.path.abspath('./weather/gothenburg.epw')
INPUT_JSON = os.path.abspath('./study_info.json')
PROJECT_FOLDER = os.path.abspath(f'./{RECIPE}_sim')


if __name__ == '__main__':
    cfg = load_room_inputs(INPUT_JSON)
    validate_room_inputs(cfg)
    model, grid = build_model(cfg)
    model_path = export_model(model, PROJECT_FOLDER)

    if RECIPE == 'daylight_factor':
        run_df(model_path, PROJECT_FOLDER, grid.count, draft=DRAFT)
    elif RECIPE == 'annual_daylight':
        wea_path = os.path.join(PROJECT_FOLDER, 'weather.wea')
        os.makedirs(PROJECT_FOLDER, exist_ok=True)
        Wea.from_epw_file(EPW_PATH).write(wea_path)
        run_annual(model_path, PROJECT_FOLDER, grid.count, wea_path, draft=DRAFT)
    else:
        raise ValueError(
            f'Unsupported Foton recipe {RECIPE!r}; choose daylight_factor or '
            'annual_daylight.'
        )

    print(f'Done -> {PROJECT_FOLDER}')

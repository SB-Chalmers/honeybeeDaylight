"""CLI entry point — daylight factor simulation (JSON-driven).

Run from the project root:
    python simulation/daylight_minimal_optimised.py

Room inputs are read from ./study_info.json (room_inputs key).
"""

import os

from simulation.inputs import load_room_inputs, validate_room_inputs
from simulation.model import build_model, export_model
from simulation.runner import run_df

DRAFT          = True
INPUT_JSON     = os.path.abspath('./study_info.json')
PROJECT_FOLDER = os.path.abspath('./daylight_factor_sim')

if __name__ == '__main__':
    cfg = load_room_inputs(INPUT_JSON)
    validate_room_inputs(cfg)
    model, grid = build_model(cfg)
    model_path = export_model(model, PROJECT_FOLDER)
    run_df(model_path, PROJECT_FOLDER, len(grid.sensors), draft=DRAFT)
    print(f'Done → {PROJECT_FOLDER}')

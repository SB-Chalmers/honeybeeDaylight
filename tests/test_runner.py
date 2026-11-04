"""Foton daylight runner (reference CPU backend, preview quality for speed)."""

import numpy as np
import pytest

from simulation.inputs import load_room_inputs
from simulation.model import build_model
from simulation.runner import run_daylight, sda_summary

EPW = 'weather/gothenburg_tmyx.epw'


@pytest.fixture(autouse=True)
def cpu_backend(monkeypatch):
    monkeypatch.setenv('FOTON_BACKEND', 'reference')


def _model(floors=2):
    cfg = load_room_inputs({
        'footprint_coordinates': [[0, 0], [4, 0], [4, 4], [0, 4]],
        'floors': floors,
        'simulate_all_floors': True,
        'floor_to_floor_height': 3.0,
        'sensor_grid': {'x_dim': 2.0, 'y_dim': 2.0, 'offset': 0.75},
    })
    return build_model(cfg)


def test_df_is_flat_and_ordered_by_floor():
    model, grids = _model()
    result = run_daylight(model, grids.count, EPW, quality='preview')

    assert result['df'].shape == (grids.count,)
    assert result['points'].shape == (grids.count, 3)
    assert result['da'] is None
    assert [g['floor_number'] for g in result['grids']] == [1, 2]
    assert [g['start_sensor_index'] for g in result['grids']] == [0, grids[0].count]
    assert sorted({round(float(z), 2) for z in result['points'][:, 2]}) == [0.75, 3.75]
    assert np.all(result['df'] >= 0)


def test_sda_is_index_aligned_with_df():
    model, grids = _model(floors=1)
    result = run_daylight(model, grids.count, EPW, run_sda=True, quality='preview')
    assert result['da'].shape == result['df'].shape
    assert np.all((result['da'] >= 0) & (result['da'] <= 100))


def test_sensor_count_mismatch_fails():
    model, grids = _model(floors=1)
    with pytest.raises(RuntimeError, match='prepared'):
        run_daylight(model, grids.count + 1, EPW, quality='preview')


def test_sda_summary_counts_passing_sensors():
    summary = sda_summary(np.array([10.0, 50.0, 80.0, 49.9]), 300, 50)
    assert summary['passing_sensors'] == 2
    assert summary['sda'] == 50.0

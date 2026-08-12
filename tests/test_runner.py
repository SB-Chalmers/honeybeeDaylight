"""Tests for the Foton daylight runner."""

from types import SimpleNamespace

from simulation.inputs import load_room_inputs
from simulation.model import build_model, export_model
from simulation.results import parse_df, parse_sensor_points
from simulation import runner


def _small_model(tmp_path):
    cfg = load_room_inputs({
        'footprint_coordinates': [[0, 0], [4, 0], [4, 4], [0, 4]],
        'sensor_grid': {'x_dim': 2.0, 'y_dim': 2.0, 'offset': 0.75},
    })
    context = [{
        'footprint_coordinates': [[6, 0], [8, 0], [8, 2], [6, 2]],
        'floors': 2,
        'floor_to_floor_height': 3.0,
    }]
    model, grid = build_model(cfg, context)
    model_path = export_model(model, str(tmp_path))
    return model_path, grid.count


def test_run_df_writes_api_compatible_outputs(tmp_path, monkeypatch):
    model_path, sensor_count = _small_model(tmp_path)
    monkeypatch.setenv('FOTON_BACKEND', 'reference')

    runner.run_df(model_path, str(tmp_path), sensor_count, draft=True)

    result = parse_df(str(tmp_path))
    points = parse_sensor_points(str(tmp_path))
    assert len(result['values']) == sensor_count
    assert len(points) == sensor_count
    assert result['summary']['min_df'] >= 0
    assert (tmp_path / 'daylight_factor' / 'metadata.json').is_file()


def test_run_annual_forwards_foton_quality_and_thresholds(tmp_path, monkeypatch):
    captured = {}

    def fake_run_annual_daylight(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(metadata={'sensor_count': 4})

    monkeypatch.setattr(runner, 'run_annual_daylight', fake_run_annual_daylight)
    monkeypatch.setenv('FOTON_BACKEND', 'reference')

    runner.run_annual(
        'model.hbjson',
        str(tmp_path),
        4,
        'weather.wea',
        draft=False,
        da_lux=500,
        sda_target_pct=60,
    )

    assert captured['backend'] == 'reference'
    assert captured['quality'] == 'final'
    assert captured['threshold'] == 500.0
    assert captured['target_time'] == 60.0
    assert captured['output_folder'] == tmp_path / 'annual_daylight'
    assert captured['export_illuminance'] is False


def test_run_annual_rejects_changed_sensor_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner,
        'run_annual_daylight',
        lambda **kwargs: SimpleNamespace(metadata={'sensor_count': 3}),
    )

    try:
        runner.run_annual('model.hbjson', str(tmp_path), 4, 'weather.wea')
    except RuntimeError as error:
        assert 'prepared 3 sensors' in str(error)
    else:
        raise AssertionError('sensor-count mismatch should fail')

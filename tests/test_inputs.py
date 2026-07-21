"""
Tests for simulation.inputs
────────────────────────────
Covers: load_room_inputs (dict, JSON string, file), wwr fan-out,
        deep merge, validate_room_inputs error cases.
"""

import json
import os
import tempfile

import pytest

from simulation.inputs import (
    DEFAULT_ROOM_INPUTS,
    GRID_DIM,
    load_room_inputs,
    validate_room_inputs,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

MINIMAL = {
    'footprint_coordinates': [[0, 0], [4, 0], [4, 3], [0, 3]],
    'orientation_offset': 0.0,
    'floor_to_floor_height': 3.0,
    'selected_floor_number': 1,
    'wwr_by_cardinal': {'north': 0.4, 'east': 0.3, 'south': 0.25, 'west': 0.2},
    'wall_thickness': 0.3,
}


# ── load_room_inputs ──────────────────────────────────────────────────────────

class TestLoadFromDict:
    def test_returns_merged_dict(self):
        cfg = load_room_inputs({'footprint_coordinates': [[0,0],[5,0],[5,5],[0,5]]})
        assert cfg['floor_to_floor_height'] == DEFAULT_ROOM_INPUTS['floor_to_floor_height']

    def test_override_is_respected(self):
        cfg = load_room_inputs({'floor_to_floor_height': 4.0})
        assert cfg['floor_to_floor_height'] == 4.0

    def test_partial_sensor_grid_merges(self):
        cfg = load_room_inputs({'sensor_grid': {'x_dim': 1.0}})
        assert cfg['sensor_grid']['x_dim'] == 1.0
        assert cfg['sensor_grid']['y_dim'] == DEFAULT_ROOM_INPUTS['sensor_grid']['y_dim']

    def test_does_not_mutate_input(self):
        inp = {'floor_to_floor_height': 5.0}
        load_room_inputs(inp)
        assert inp == {'floor_to_floor_height': 5.0}


class TestLoadFromJsonString:
    def test_reads_room_inputs_key(self):
        payload = json.dumps({'room_inputs': MINIMAL})
        cfg = load_room_inputs(payload)
        assert cfg['floor_to_floor_height'] == MINIMAL['floor_to_floor_height']

    def test_falls_back_to_root_when_no_key(self):
        cfg = load_room_inputs(json.dumps(MINIMAL))
        assert cfg['selected_floor_number'] == 1

    def test_uses_defaults_when_key_missing(self, capsys):
        payload = json.dumps({'some_other_key': 1})
        cfg = load_room_inputs(payload)
        captured = capsys.readouterr()
        assert 'defaults' in captured.out.lower()
        assert cfg['floor_to_floor_height'] == DEFAULT_ROOM_INPUTS['floor_to_floor_height']


class TestLoadFromFile:
    def test_reads_room_inputs_key_from_file(self, tmp_path):
        p = tmp_path / 'study_info.json'
        p.write_text(json.dumps({'room_inputs': MINIMAL}))
        cfg = load_room_inputs(str(p))
        assert cfg['wall_thickness'] == MINIMAL['wall_thickness']

    def test_missing_file_returns_defaults(self, capsys):
        cfg = load_room_inputs('/nonexistent/path/study_info.json')
        assert cfg == DEFAULT_ROOM_INPUTS


class TestWwrFanout:
    def test_single_wwr_fans_to_all_cardinals(self):
        cfg = load_room_inputs({'wwr': 0.5})
        wwr = cfg['wwr_by_cardinal']
        assert wwr['north'] == 0.5
        assert wwr['east'] == 0.5
        assert wwr['south'] == 0.5
        assert wwr['west'] == 0.5

    def test_wwr_key_removed_after_fanout(self):
        cfg = load_room_inputs({'wwr': 0.5})
        assert 'wwr' not in cfg

    def test_wwr_by_cardinal_wins_over_wwr(self):
        cfg = load_room_inputs({'wwr': 0.5, 'wwr_by_cardinal': {'north': 0.6, 'east': 0.3, 'south': 0.2, 'west': 0.1}})
        assert cfg['wwr_by_cardinal']['north'] == 0.6

    def test_wwr_does_not_override_existing_per_direction(self):
        cfg = load_room_inputs({'wwr': 0.5, 'wwr_by_cardinal': {'north': 0.9, 'east': 0.3, 'south': 0.2, 'west': 0.1}})
        assert cfg['wwr_by_cardinal']['north'] == 0.9
        assert cfg['wwr_by_cardinal']['east'] == 0.3


# ── validate_room_inputs ──────────────────────────────────────────────────────

class TestValidate:
    def _valid(self, **overrides):
        cfg = load_room_inputs(MINIMAL)
        cfg.update(overrides)
        return cfg

    def test_valid_config_passes(self):
        validate_room_inputs(load_room_inputs(MINIMAL))  # should not raise

    def test_floor_number_zero_raises(self):
        with pytest.raises(ValueError, match='selected_floor_number'):
            validate_room_inputs(self._valid(selected_floor_number=0))

    def test_negative_floor_height_raises(self):
        with pytest.raises(ValueError, match='floor_to_floor_height'):
            validate_room_inputs(self._valid(floor_to_floor_height=-1.0))

    def test_negative_wall_thickness_raises(self):
        with pytest.raises(ValueError, match='wall_thickness'):
            validate_room_inputs(self._valid(wall_thickness=-0.1))

    def test_too_few_footprint_points_raises(self):
        with pytest.raises(ValueError, match='footprint_coordinates'):
            validate_room_inputs(self._valid(footprint_coordinates=[[0,0],[1,0]]))

    def test_wwr_above_one_raises(self):
        cfg = self._valid()
        cfg['wwr_by_cardinal']['north'] = 1.1
        with pytest.raises(ValueError, match='north'):
            validate_room_inputs(cfg)

    def test_wwr_negative_raises(self):
        cfg = self._valid()
        cfg['wwr_by_cardinal']['south'] = -0.1
        with pytest.raises(ValueError, match='south'):
            validate_room_inputs(cfg)

    def test_missing_cardinal_raises(self):
        cfg = self._valid()
        del cfg['wwr_by_cardinal']['east']
        with pytest.raises(ValueError, match='east'):
            validate_room_inputs(cfg)

    def test_zero_grid_dim_raises(self):
        cfg = self._valid()
        cfg['sensor_grid']['x_dim'] = 0
        with pytest.raises(ValueError, match='x_dim'):
            validate_room_inputs(cfg)

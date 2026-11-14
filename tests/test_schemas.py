"""
Tests for api.schemas
──────────────────────
Covers: defaults and window ratios handed to the simulations, room checks,
the shared_floors_by_edge field and clockwise footprints.
"""

import pytest
from pydantic import ValidationError

from api.schemas import RoomInput

ROOM = {'footprint_coordinates': [[0, 0], [8, 0], [8, 6], [0, 6]], 'floors': 3, 'wwr': 0.4}


class TestSharedFloorsByEdge:
    def test_is_passed_to_the_simulation_when_sent(self):
        room = RoomInput(**ROOM, shared_floors_by_edge=[[], [1, 2], [], []])
        assert room.to_simulation_dict()['shared_floors_by_edge'] == [[], [1, 2], [], []]

    def test_is_left_out_when_not_sent(self):
        assert 'shared_floors_by_edge' not in RoomInput(**ROOM).to_simulation_dict()

    def test_rejects_floor_zero(self):
        with pytest.raises(ValidationError, match='floor 0'):
            RoomInput(**ROOM, shared_floors_by_edge=[[], [0], [], []])

    def test_rejects_a_floor_above_the_building(self):
        with pytest.raises(ValidationError, match='floor 4'):
            RoomInput(**ROOM, shared_floors_by_edge=[[], [4], [], []])

    def test_needs_one_entry_per_edge(self):
        with pytest.raises(ValidationError, match='exactly 4 entries'):
            RoomInput(**ROOM, shared_floors_by_edge=[[], [1], []])


class TestSimulationDict:
    def test_every_field_is_filled_in(self):
        room = RoomInput(footprint_coordinates=ROOM['footprint_coordinates']).to_simulation_dict()
        assert room['sensor_grid'] == {'x_dim': 0.5, 'y_dim': 0.5, 'offset': 0.75}
        assert room['floor_to_floor_height'] == 3.2
        assert room['wwr_by_cardinal'] == {'north': 0.4, 'east': 0.3, 'south': 0.25, 'west': 0.2}

    def test_single_wwr_fans_out_to_every_direction(self):
        room = RoomInput(**ROOM).to_simulation_dict()
        assert room['wwr_by_cardinal'] == dict.fromkeys(('north', 'east', 'south', 'west'), 0.4)
        assert 'wwr' not in room

    def test_wwr_by_cardinal_wins_over_wwr(self):
        ratios = {'north': 0.6, 'east': 0.3, 'south': 0.2, 'west': 0.1}
        assert RoomInput(**ROOM, wwr_by_cardinal=ratios).to_simulation_dict()['wwr_by_cardinal'] == ratios


BROKEN_ROOMS = {
    'zero width': ({'footprint_coordinates': [[0, 0], [8, 0], [8, 0], [0, 0]]}, 'enclose 0.00'),
    'points on one line': ({'footprint_coordinates': [[0, 0], [4, 0], [8, 0]]}, 'enclose 0.00'),
    'bow tie': ({'footprint_coordinates': [[0, 0], [4, 4], [4, 0], [0, 4]]}, 'cross'),
    'sensors above ceiling': ({'floor_to_floor_height': 0.5}, 'below the ceiling'),
    'zero sensor spacing': ({'sensor_grid': {'x_dim': 0}}, 'greater than 0'),
    'wwr_by_edge too short': ({'wwr_by_edge': [0.1, 0.2, 0.3]}, 'exactly 4 values'),
    'wwr_by_edge above 1': ({'wwr_by_edge': [0.1, 0.2, 0.3, 1.1]}, r'\[0, 1\]'),
}


@pytest.mark.parametrize('fields, message', BROKEN_ROOMS.values(), ids=BROKEN_ROOMS.keys())
def test_impossible_rooms_are_rejected_before_any_job_runs(fields, message):
    with pytest.raises(ValidationError, match=message):
        RoomInput(**{**ROOM, **fields})


def test_clockwise_footprint_is_reversed_with_its_edge_lists():
    from api.schemas import RoomInput

    from simulation.model import build_model

    ccw = [[0, 0], [14, 0], [14, 5], [6, 5], [6, 11], [0, 11]]
    edge_wwr = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]  # edge i runs from point i to point i+1
    cw = RoomInput(footprint_coordinates=ccw[::-1], wwr_by_edge=edge_wwr[::-1][1:] + edge_wwr[-1:])
    assert cw.footprint_coordinates == ccw
    # Each wall keeps its ratio: in ccw[::-1], edge k is ccw edge 4-k (mod 6).
    assert cw.wwr_by_edge == edge_wwr
    model, grids = build_model(cw.to_simulation_dict())
    assert grids[0].count > 0 and len(model.rooms[0].apertures) > 0

"""
Tests for api.schemas
──────────────────────
Covers: the shared_floors_by_edge request field.
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
        with pytest.raises(ValidationError, match='>= 1'):
            RoomInput(**ROOM, shared_floors_by_edge=[[], [0], [], []])


def test_clockwise_footprint_is_reversed_with_its_edge_lists():
    from api.schemas import RoomInput
    from simulation.inputs import load_room_inputs
    from simulation.model import build_model

    ccw = [[0, 0], [14, 0], [14, 5], [6, 5], [6, 11], [0, 11]]
    edge_wwr = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]  # edge i runs from point i to point i+1
    cw = RoomInput(footprint_coordinates=ccw[::-1], wwr_by_edge=edge_wwr[::-1][1:] + edge_wwr[-1:])
    assert cw.footprint_coordinates == ccw
    # Each wall keeps its ratio: in ccw[::-1], edge k is ccw edge 4-k (mod 6).
    assert cw.wwr_by_edge == edge_wwr
    model, grid = build_model(load_room_inputs(cw.to_simulation_dict()))
    assert grid.count > 0 and len(model.rooms[0].apertures) > 0

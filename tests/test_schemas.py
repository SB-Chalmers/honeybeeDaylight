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

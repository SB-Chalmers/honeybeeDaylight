"""
Tests for simulation.energy.idf_builder
────────────────────────────────────────
Covers: walls shared with neighbouring buildings become adiabatic, per storey.
"""

import honeybee_energy.writer as writer
import pytest

from simulation.energy.idf_builder import build_idf

ROOM = {
    'footprint_coordinates': [[0, 0], [8, 0], [8, 6], [0, 6]],
    'floor_to_floor_height': 3.0,
    'floors': 3,
    'orientation_offset': 0.0,
    'wwr': 0.4,
    'window_width': 1.2,
    'window_height': 1.5,
    'window_spacing': 0.0,
    'wall_thickness': 0.3,
}
CONSTRUCTIONS = {key: {'name': key.title(), 'layers': []} for key in ('wall', 'floor', 'roof', 'window')}


@pytest.fixture
def captured_model(monkeypatch):
    """Run build_idf while keeping a reference to the Honeybee model it writes to IDF."""
    captured = {}
    original = writer.model_to_idf

    def capture(model, *args, **kwargs):
        captured['model'] = model
        return original(model, *args, **kwargs)

    monkeypatch.setattr(writer, 'model_to_idf', capture)

    def build(room):
        idf_text, _ = build_idf(room, CONSTRUCTIONS, 'Office', False, [])
        return captured['model'], idf_text

    return build


def walls_by_floor(model):
    """Wall faces of each storey's room, in footprint edge order."""
    return [[face for face in room.faces if str(face.type) == 'Wall'] for room in model.rooms]


class TestSharedWalls:
    """Edge 1 (x = 8) is shared with a neighbour on floors 1 and 2 of a 3-storey building."""

    def test_shared_walls_are_adiabatic_without_windows_on_shared_floors(self, captured_model):
        model, _ = captured_model({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]})
        floors = walls_by_floor(model)
        for floor_walls in floors[:2]:
            assert str(floor_walls[1].boundary_condition) == 'Adiabatic'
            assert len(floor_walls[1].apertures) == 0

    def test_floor_above_the_neighbour_keeps_windows(self, captured_model):
        model, _ = captured_model({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]})
        top_wall = walls_by_floor(model)[2][1]
        assert str(top_wall.boundary_condition) == 'Outdoors'
        assert len(top_wall.apertures) > 0

    def test_unshared_walls_are_unchanged(self, captured_model):
        model, _ = captured_model({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]})
        for floor_walls in walls_by_floor(model):
            for index in (0, 2, 3):
                assert str(floor_walls[index].boundary_condition) == 'Outdoors'
                assert len(floor_walls[index].apertures) > 0

    def test_without_shared_walls_every_wall_is_exterior(self, captured_model):
        model, _ = captured_model(ROOM)
        assert all(str(wall.boundary_condition) == 'Outdoors' for floor_walls in walls_by_floor(model) for wall in floor_walls)

    def test_idf_contains_the_adiabatic_walls(self, captured_model):
        _, idf_text = captured_model({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]})
        assert idf_text.count('Adiabatic') >= 2

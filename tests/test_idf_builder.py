"""
Tests for simulation.energy.idf_builder
────────────────────────────────────────
Covers: walls shared with neighbouring buildings become adiabatic, per storey.
"""

import pytest

from simulation.energy import idf_builder
from simulation.energy.idf_builder import build_idf
from tests import full_room

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
    original = idf_builder.model_to_idf

    def capture(model, *args, **kwargs):
        captured['model'] = model
        return original(model, *args, **kwargs)

    monkeypatch.setattr(idf_builder, 'model_to_idf', capture)

    def build(room):
        idf_text, _, _ = build_idf(room, CONSTRUCTIONS, 'Office', False, [])
        return captured['model'], idf_text

    return build


def walls_by_floor(model):
    """Wall faces of each storey's room, in footprint edge order."""
    return [[face for face in room.faces if str(face.type) == 'Wall'] for room in model.rooms]


class TestSharedWalls:
    """Edge 1 (x = 8) is shared with a neighbour on floors 1 and 2 of a 3-storey building."""

    def test_shared_walls_are_adiabatic_without_windows_on_shared_floors(self, captured_model):
        model, _ = captured_model(full_room({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]}))
        floors = walls_by_floor(model)
        for floor_walls in floors[:2]:
            assert str(floor_walls[1].boundary_condition) == 'Adiabatic'
            assert len(floor_walls[1].apertures) == 0

    def test_floor_above_the_neighbour_keeps_windows(self, captured_model):
        model, _ = captured_model(full_room({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]}))
        top_wall = walls_by_floor(model)[2][1]
        assert str(top_wall.boundary_condition) == 'Outdoors'
        assert len(top_wall.apertures) > 0

    def test_unshared_walls_are_unchanged(self, captured_model):
        model, _ = captured_model(full_room({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]}))
        for floor_walls in walls_by_floor(model):
            for index in (0, 2, 3):
                assert str(floor_walls[index].boundary_condition) == 'Outdoors'
                assert len(floor_walls[index].apertures) > 0

    def test_without_shared_walls_every_wall_is_exterior(self, captured_model):
        model, _ = captured_model(full_room(ROOM))
        assert all(str(wall.boundary_condition) == 'Outdoors' for floor_walls in walls_by_floor(model) for wall in floor_walls)

    def test_idf_contains_the_adiabatic_walls(self, captured_model):
        _, idf_text = captured_model(full_room({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]}))
        assert idf_text.count('Adiabatic') >= 2


class TestMultipliers:
    def test_identical_middle_floors_share_one_zone(self):
        from simulation.energy.idf_builder import floor_groups
        assert floor_groups(full_room({**ROOM, 'floors': 1})) == [[1]]
        assert floor_groups(full_room({**ROOM, 'floors': 6})) == [[1], [2, 3, 4, 5], [6]]

    def test_middle_floors_with_different_shared_walls_stay_apart(self):
        from simulation.energy.idf_builder import floor_groups
        room = full_room({**ROOM, 'floors': 6, 'shared_floors_by_edge': [[], [1, 2, 3], [], []]})
        assert floor_groups(room) == [[1], [2, 3], [4, 5], [6]]

    def test_only_ground_and_roof_are_exposed(self, captured_model):
        model, _ = captured_model(full_room({**ROOM, 'floors': 6}))
        assert [room.multiplier for room in model.rooms] == [1, 4, 1]
        bcs = [{str(f.type): str(f.boundary_condition) for f in room.faces if str(f.type) != 'Wall'}
               for room in model.rooms]
        assert bcs == [
            {'Floor': 'Ground', 'RoofCeiling': 'Adiabatic'},
            {'Floor': 'Adiabatic', 'RoofCeiling': 'Adiabatic'},
            {'Floor': 'Adiabatic', 'RoofCeiling': 'Outdoors'},
        ]

    def test_one_window_per_glazed_wall(self, captured_model):
        model, _ = captured_model(full_room(ROOM))
        assert all(len(wall.apertures) == 1 for walls in walls_by_floor(model) for wall in walls)

    def test_returns_multipliers_by_zone_name(self):
        _, area, multipliers = build_idf(full_room({**ROOM, 'floors': 6}), CONSTRUCTIONS, 'Office', False, [])
        assert multipliers == {'FLOOR_1': 1, 'FLOORS_2_5': 4, 'FLOOR_6': 1}
        assert area == pytest.approx(8 * 6 * 6)


def test_each_window_gets_an_overhang_and_two_fins(captured_model):
    """Depth = wall thickness + extra depth, as in the daylight model."""
    model, _ = captured_model(full_room({**ROOM, 'additional_horizontal_shading_depth': 0.5}))
    apertures = [aperture for hb_room in model.rooms for aperture in hb_room.apertures]
    assert apertures
    for aperture in apertures:
        shades = aperture.outdoor_shades
        assert len(shades) == 3
        overhang = next(s for s in shades if 'Overhang' in s.identifier).geometry
        width = max(v.distance_to_point(w) for v in aperture.geometry.vertices
                    for w in aperture.geometry.vertices if abs(v.z - w.z) < 1e-9)
        assert overhang.area == pytest.approx(width * 0.8)

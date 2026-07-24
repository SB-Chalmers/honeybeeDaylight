"""
Tests for simulation.model
───────────────────────────
Covers: room geometry from footprint, orientation, floor elevation,
        per-cardinal WWR assignment, shading, sensor grid dimensions.
"""

import math

import pytest

from simulation.inputs import load_room_inputs
from simulation.model import build_model, export_model

# ── Fixtures ──────────────────────────────────────────────────────────────────

RECT_8x6 = {
    'footprint_coordinates': [[0,0],[8,0],[8,6],[0,6]],
    'orientation_offset': 0.0,
    'floor_to_floor_height': 3.2,
    'selected_floor_number': 1,
    'wwr_by_cardinal': {'north': 0.4, 'east': 0.3, 'south': 0.25, 'west': 0.2},
    'wall_thickness': 0.0,
    'additional_horizontal_shading_depth': 0.0,
    'additional_vertical_shading_depth': 0.0,
    'sensor_grid': {'x_dim': 0.5, 'y_dim': 0.5, 'offset': 0.75},
}


def cfg(**overrides):
    c = load_room_inputs(RECT_8x6)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


# ── Room geometry ─────────────────────────────────────────────────────────────

class TestRoomGeometry:
    def test_returns_model_and_grid(self):
        model, grid = build_model(cfg())
        assert model is not None
        assert grid is not None

    def test_room_has_six_faces(self):
        model, _ = build_model(cfg())
        room = model.rooms[0]
        assert len(room.faces) == 6

    def test_floor_elevation_for_floor_1(self):
        model, _ = build_model(cfg(selected_floor_number=1))
        room = model.rooms[0]
        floor_face = next(f for f in room.faces if str(f.type) == 'Floor')
        z_coords = [v.z for v in floor_face.vertices]
        assert all(abs(z - 0.0) < 1e-6 for z in z_coords)

    def test_floor_elevation_for_floor_3(self):
        model, _ = build_model(cfg(selected_floor_number=3, floor_to_floor_height=3.0))
        room = model.rooms[0]
        floor_face = next(f for f in room.faces if str(f.type) == 'Floor')
        expected_z = 2 * 3.0  # (3-1) * 3.0
        z_coords = [v.z for v in floor_face.vertices]
        assert all(abs(z - expected_z) < 1e-6 for z in z_coords)

    def test_room_height_matches_floor_to_floor(self):
        h = 4.0
        model, _ = build_model(cfg(floor_to_floor_height=h))
        room = model.rooms[0]
        zs = [v.z for f in room.faces for v in f.vertices]
        assert abs(max(zs) - min(zs) - h) < 1e-4


# ── Orientation ───────────────────────────────────────────────────────────────

class TestOrientation:
    def test_zero_offset_north_wall_faces_positive_y(self):
        model, _ = build_model(cfg(orientation_offset=0.0))
        walls = [f for f in model.rooms[0].faces if str(f.type) == 'Wall']
        normals_y = [round(w.normal.y, 3) for w in walls]
        assert 1.0 in normals_y  # +Y = north

    def test_90_degree_offset_rotates_walls(self):
        model0, _ = build_model(cfg(orientation_offset=0.0))
        model90, _ = build_model(cfg(orientation_offset=90.0))
        # After 90° rotation the north wall (+Y) should become the east wall (+X).
        walls90 = [f for f in model90.rooms[0].faces if str(f.type) == 'Wall']
        x_normals = [round(abs(f.normal.x), 2) for f in walls90]
        assert 1.0 in x_normals  # one wall now faces ±X


# ── WWR and apertures ─────────────────────────────────────────────────────────

class TestWWR:
    def test_all_walls_have_apertures_when_wwr_nonzero(self):
        model, _ = build_model(cfg())
        walls = [f for f in model.rooms[0].faces if str(f.type) == 'Wall']
        assert all(len(w.apertures) > 0 for w in walls)

    def test_zero_wwr_wall_has_no_aperture(self):
        c = cfg()
        c['wwr_by_cardinal'] = {'north': 0.0, 'east': 0.0, 'south': 0.0, 'west': 0.0}
        model, _ = build_model(c)
        walls = [f for f in model.rooms[0].faces if str(f.type) == 'Wall']
        assert all(len(w.apertures) == 0 for w in walls)

    def test_sensor_grid_is_registered_to_room(self):
        model, grid = build_model(cfg())
        assert grid.room_identifier == model.rooms[0].identifier


# ── Shading ───────────────────────────────────────────────────────────────────

class TestShading:
    def test_no_shading_when_wall_thickness_zero(self):
        model, _ = build_model(cfg(wall_thickness=0.0))
        walls = [f for f in model.rooms[0].faces if str(f.type) == 'Wall']
        for wall in walls:
            for aperture in wall.apertures:
                assert len(aperture.outdoor_shades) == 0

    def test_shading_present_when_wall_thickness_nonzero(self):
        model, _ = build_model(cfg(wall_thickness=0.5))
        walls = [f for f in model.rooms[0].faces if str(f.type) == 'Wall']
        apertures = [a for w in walls for a in w.apertures]
        assert all(len(a.outdoor_shades) > 0 for a in apertures)

    def test_both_h_and_v_shading_adds_three_shades_per_aperture(self):
        # overhang + left_fin + right_fin = 3 shades
        model, _ = build_model(cfg(wall_thickness=0.3,
                                   additional_horizontal_shading_depth=0.0,
                                   additional_vertical_shading_depth=0.3))
        walls = [f for f in model.rooms[0].faces if str(f.type) == 'Wall']
        apertures = [a for w in walls for a in w.apertures]
        assert all(len(a.outdoor_shades) == 3 for a in apertures)


# ── Sensor grid ───────────────────────────────────────────────────────────────

class TestSensorGrid:
    def test_sensor_count_scales_with_footprint(self):
        _, grid_small = build_model(cfg(**{'footprint_coordinates': [[0,0],[4,0],[4,4],[0,4]]}))
        _, grid_large = build_model(cfg(**{'footprint_coordinates': [[0,0],[8,0],[8,8],[0,8]]}))
        assert grid_large.count > grid_small.count

    def test_coarser_grid_has_fewer_sensors(self):
        _, grid_fine   = build_model(cfg(**{'sensor_grid': {'x_dim': 0.5, 'y_dim': 0.5, 'offset': 0.75}}))
        _, grid_coarse = build_model(cfg(**{'sensor_grid': {'x_dim': 1.0, 'y_dim': 1.0, 'offset': 0.75}}))
        assert grid_coarse.count < grid_fine.count


class TestContextBuildings:
    def test_context_building_is_added_as_detached_shades(self):
        context = {
            'footprint_coordinates': [[20, 0], [24, 0], [24, 4], [20, 4]],
            'floors': 2,
            'floor_to_floor_height': 3.0,
        }
        model, _ = build_model(cfg(), [context])

        assert len(model.rooms) == 1
        assert len(model.shades) == 6
        assert all(shade.is_detached for shade in model.shades)


# ── Export ────────────────────────────────────────────────────────────────────

class TestExportModel:
    def test_writes_hbjson(self, tmp_path):
        model, _ = build_model(cfg())
        path = export_model(model, str(tmp_path))
        assert path.endswith('model.hbjson')
        assert (tmp_path / 'model.hbjson').exists()

    def test_exported_json_is_valid(self, tmp_path):
        import json
        model, _ = build_model(cfg())
        path = export_model(model, str(tmp_path))
        with open(path) as f:
            data = json.load(f)
        assert data.get('type') == 'Model'

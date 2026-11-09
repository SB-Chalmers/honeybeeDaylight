"""
The Grasshopper components must give what the backend uses, so a student who
rebuilds a Heath request in Grasshopper gets the same results.
"""

import math

import pytest
from ladybug_geometry.geometry3d.face import Face3D
from ladybug_geometry.geometry3d.pointvector import Point3D

from grasshopper import heath_request, heath_sensors
from simulation.energy.idf_builder import floor_groups
from simulation.inputs import load_room_inputs
from simulation.model import build_model

L_SHAPE = [[0, 0], [14, 0], [14, 5], [6, 5], [6, 11], [0, 11]]


def _rotated(room, floor):
    """The footprint as the Grasshopper canvas makes it: rotated, at floor level."""
    z = (floor - 1) * room['floor_to_floor_height']
    face = Face3D([Point3D(x, y, z) for x, y in room['footprint_coordinates']])
    face = face.rotate_xy(math.radians(room['orientation_offset']), Point3D(0, 0, z))
    return [(p.x, p.y, p.z) for p in face.boundary]


@pytest.mark.parametrize('room', [
    {'footprint_coordinates': [[0, 0], [20, 0], [20, 12], [0, 12]], 'floors': 3,
     'selected_floor_number': 2},
    {'footprint_coordinates': L_SHAPE, 'orientation_offset': 33.0, 'floors': 6,
     'selected_floor_number': 4},
    {'footprint_coordinates': L_SHAPE, 'orientation_offset': -71.5, 'floors': 3,
     'simulate_all_floors': True, 'sensor_grid': {'x_dim': 0.7, 'y_dim': 0.7, 'offset': 0.85}},
])
def test_sensor_points_match_backend(room):
    cfg = load_room_inputs(room)
    model, _ = build_model(cfg)
    floors = range(1, cfg['floors'] + 1) if cfg['simulate_all_floors'] else [cfg['selected_floor_number']]
    grid = cfg['sensor_grid']
    for grid_hb, floor in zip(model.properties.radiance.sensor_grids, floors):
        ours = heath_sensors.sensor_points(_rotated(cfg, floor), grid['x_dim'], grid['offset'])
        assert len(ours) == grid_hb.count > 0
        for sensor, point in zip(grid_hb.sensors, ours):
            assert all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(sensor.pos, point))


@pytest.mark.parametrize('floors,shared', [
    (1, None), (2, None), (7, None), (6, [[], [2, 3], [], [], [], [5]]),
])
def test_floor_groups_match_backend(floors, shared):
    room = {'floors': floors, 'shared_floors_by_edge': shared}
    assert heath_request.floor_groups(room, floors) == floor_groups(room)


def test_layer_values_use_backend_defaults():
    record = {'name': 'Wall', 'layers': [{'material_name': 'Brick', 'thickness_m': 0.2}]}
    assert heath_request.layer_values(record) == [['Brick', 0.2, 0.8, 1800.0, 900.0]]
    assert heath_request.layer_values({'name': 'Slab', 'layers': []}) == [['Slab_mat', 0.2, 0.8, 1800, 900]]

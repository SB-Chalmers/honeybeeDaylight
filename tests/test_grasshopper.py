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


def test_clockwise_footprint_is_reversed_like_the_api():
    from api.schemas import RoomInput
    cw = {'footprint_coordinates': L_SHAPE[::-1], 'wwr_by_edge': [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]}
    api = RoomInput(**cw)
    ours = heath_request.counter_clockwise(dict(cw))
    assert ours['footprint_coordinates'] == api.footprint_coordinates
    assert ours['wwr_by_edge'] == api.wwr_by_edge


@pytest.mark.parametrize('floors,alone,expected', [
    (6, [3], [[1], [2], [3], [4, 5], [6]]),
    (6, [1], [[1], [2, 3, 4, 5], [6]]),
    (3, [1, 2, 3], [[1], [2], [3]]),
])
def test_daylight_floors_get_their_own_zone(floors, alone, expected):
    assert heath_request.floor_groups({}, floors, alone) == expected


def test_each_sensor_gets_its_grid_cell():
    footprint = _rotated(load_room_inputs({'footprint_coordinates': L_SHAPE, 'orientation_offset': 33.0}), 1)
    cells = []
    points = heath_sensors.sensor_points(footprint, 0.5, 0.75, cells)
    assert len(cells) == len(points)
    for point, cell in zip(points, cells):
        centre = [sum(c[k] for c in cell) / 4 for k in range(3)]
        assert all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(centre, point))
        side = math.dist(cell[0], cell[1])
        assert math.isclose(side, 0.5) and math.isclose(math.dist(cell[1], cell[2]), 0.5)
        # counter-clockwise seen from above: upward normal
        (x0, y0, _), (x1, y1, _), (x2, y2, _) = cell[:3]
        assert (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0) > 0

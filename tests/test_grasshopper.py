"""
The Grasshopper "Heath Building" code must build the same model as the backend,
so a student who rebuilds a Heath request in Grasshopper gets the same results.
"""

import math

import pytest
from honeybee.model import Model
from honeybee_energy.writer import model_to_idf

from grasshopper import heath_building as gh
from simulation.energy.idf_builder import build_idf
from simulation.inputs import load_room_inputs
from simulation.model import build_model

L_SHAPE = [[0, 0], [14, 0], [14, 5], [6, 5], [6, 11], [0, 11]]
CONTEXT = [{'footprint_coordinates': [[20, 0], [30, 0], [30, 10], [20, 10]],
            'floors': 4, 'floor_to_floor_height': 3.0}]
EPSM = {
    'wall': {'name': 'Brick wall', 'layers': [
        {'material_name': 'Brick', 'thickness_m': 0.2, 'conductivity_w_mk': 0.7,
         'density_kg_m3': 1900, 'specific_heat_j_kgk': 840}]},
    'floor': {'name': 'Slab', 'layers': []},
    'roof': {'name': 'Roof', 'layers': [
        {'material_name': 'Insulation', 'thickness_m': 0.3, 'conductivity_w_mk': 0.04,
         'density_kg_m3': 30, 'specific_heat_j_kgk': 1400}]},
    'window': {'name': 'Triple', 'u_value_w_m2k': 0.9},
}

CASES = [
    {'footprint_coordinates': [[0, 0], [20, 0], [20, 12], [0, 12]], 'floors': 3,
     'selected_floor_number': 2},
    {'footprint_coordinates': L_SHAPE, 'orientation_offset': 33.0, 'floors': 6,
     'selected_floor_number': 4, 'wall_thickness': 0.4,
     'additional_horizontal_shading_depth': 0.5, 'wwr': 0.35},
    {'footprint_coordinates': L_SHAPE, 'orientation_offset': -71.5, 'floors': 5,
     'simulate_all_floors': True, 'wwr_by_edge': [0.2, 0.5, 0, 0.3, 0.6, 0.4],
     'shared_floors_by_edge': [[], [], [], [], [], [1, 2, 3]],
     'window_width': 2.0, 'window_height': 1.8, 'window_spacing': 0.5,
     'sensor_grid': {'x_dim': 0.7, 'y_dim': 0.4, 'offset': 0.85}},
]


def _backend_room(room):
    """The room dict as the API hands it to the simulation (schemas.to_simulation_dict)."""
    from api.schemas import RoomInput
    return RoomInput(**room).to_simulation_dict()


@pytest.mark.parametrize('room', CASES)
def test_daylight_model_matches_backend(room):
    model, grids = build_model(load_room_inputs(_backend_room(room)), CONTEXT)
    built = gh.build({'room': room, 'context_buildings': CONTEXT})
    ours = Model('Daylight_Model', rooms=built['daylight_rooms'],
                 orphaned_shades=built['daylight_context'])
    for grid in built['sensor_grids']:
        ours.properties.radiance.add_sensor_grid(grid)

    a, b = model.to_dict(), ours.to_dict()
    assert a['rooms'] == b['rooms']
    assert a['orphaned_shades'] == b['orphaned_shades']
    theirs = model.properties.radiance.sensor_grids
    mine = ours.properties.radiance.sensor_grids
    assert [g.identifier for g in theirs] == [g.identifier for g in mine]
    for g1, g2 in zip(theirs, mine):
        assert g1.count == g2.count > 0
        for s1, s2 in zip(g1.sensors, g2.sensors):
            assert all(math.isclose(p, q, abs_tol=1e-9) for p, q in zip(s1.pos, s2.pos))


@pytest.mark.parametrize('room', CASES)
@pytest.mark.parametrize('program,natvent', [('Office', False), ('School', True)])
def test_energy_model_matches_backend(room, program, natvent):
    idf, _, _ = build_idf(_backend_room(room), EPSM, program, natvent, CONTEXT)
    built = gh.build({'room': room, 'context_buildings': CONTEXT,
                      'energy': {'building_program': program, 'natural_ventilation': natvent}},
                     constructions=EPSM)
    ours = Model('Energy_Model', rooms=built['energy_rooms'],
                 orphaned_shades=built['energy_context'])
    assert idf.endswith(model_to_idf(ours))


def test_overrides_replace_request_values():
    request = gh.load_request({'room': {'footprint_coordinates': L_SHAPE, 'floors': 2}},
                              {'floors': 7, 'wwr_by_cardinal': {'south': 0.6}, 'window_width': None})
    room = request['room']
    assert room['floors'] == 7
    assert room['wwr_by_cardinal'] == {'north': 0.4, 'east': 0.3, 'south': 0.6, 'west': 0.2}
    assert room['window_width'] == 1.2

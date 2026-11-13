"""POST /v1/energy-rc: the energy job on EPSM RC, same request and result as EnergyPlus."""

import math

import numpy as np
import pytest

pytest.importorskip('epsm_rc')

from api.schemas import EnergyResult  # noqa: E402
from simulation.energy import rc  # noqa: E402
from worker import jobs, packing  # noqa: E402

from .test_jobs import CONSTRUCTIONS, ROOM  # noqa: E402

ENERGY = {'constructions': {}, 'building_program': 'Office'}


def request(**room):
    return {'room': {**ROOM, **room}, 'energy': dict(ENERGY)}


@pytest.fixture(autouse=True)
def catalogue(monkeypatch):
    monkeypatch.setattr(jobs, 'fetch_constructions', lambda names: CONSTRUCTIONS)


def test_result_has_the_energyplus_shape():
    result = jobs.energy_rc_job(request())
    EnergyResult(**result)  # same response model as /v1/energy
    assert result['weather'] == 'default:gothenburg_tmyx.epw'
    assert result['floor_area_m2'] == pytest.approx(32.0)
    assert result['heating_demand_kwh_m2'] > 0
    heating = packing.unpack(result['heating_hourly_kwh'])
    assert heating.shape == (8760,)
    assert heating.sum() == pytest.approx(result['heating_energy_kwh'], rel=1e-3)
    balance = result['monthly_heat_balance']
    assert balance['months'][0] == 'Jan'
    for key in ('heating', 'cooling', 'people', 'lighting', 'equipment', 'solar',
                'infiltration_gain', 'infiltration_loss'):
        assert len(balance[key]) == 12
    assert sum(balance['heating']) == pytest.approx(result['heating_demand_kwh_m2'], rel=1e-3)
    plain = jobs.energy_rc_job({**request(), 'pack': False})
    assert len(plain['heating_hourly_kwh']) == 8760


def test_natural_ventilation_turns_cooling_off():
    req = request(wwr=0.6)
    req['energy']['natural_ventilation'] = True
    assert jobs.energy_rc_job(req)['cooling_energy_kwh'] == 0


def test_window_ratio_per_cardinal_and_per_edge():
    building = rc.build_building(
        {**ROOM, 'wwr_by_cardinal': {'north': 0.1, 'east': 0.2, 'south': 0.5, 'west': 0.3}},
        [], CONSTRUCTIONS, 'Office', False)
    by_facade = {o.facade_id: o.wwr for o in building.facade.overrides if o.storey is None}
    # Counter-clockwise footprint, so edge 0 (p0->p1) faces south, then east, north, west.
    assert by_facade == {'main.r0.e0': 0.5, 'main.r0.e1': 0.2, 'main.r0.e2': 0.1, 'main.r0.e3': 0.3}
    edges = rc.build_building({**ROOM, 'wwr_by_edge': [0.1, 0.2, 0.3, 0.4]}, [], CONSTRUCTIONS,
                              'Office', False)
    assert [o.wwr for o in edges.facade.overrides] == [0.1, 0.2, 0.3, 0.4]


def test_rotation_moves_cardinal_ratios_with_the_walls():
    building = rc.build_building(
        {**ROOM, 'orientation_offset': 90.0,
         'wwr_by_cardinal': {'north': 0.1, 'east': 0.2, 'south': 0.5, 'west': 0.3}},
        [], CONSTRUCTIONS, 'Office', False)
    by_facade = {o.facade_id: o.wwr for o in building.facade.overrides}
    # Turned 90° counter-clockwise, edge 0 faces east.
    assert by_facade['main.r0.e0'] == 0.2


def test_shared_walls_are_party_walls_without_windows():
    building = rc.build_building({**ROOM, 'shared_floors_by_edge': [[], [1, 2], [], []]}, [],
                                 CONSTRUCTIONS, 'Office', False)
    walls = building.geometry.party_walls
    assert [(w.facade_id, w.bottom_m, w.top_m) for w in walls] == [('main.r0.e1', 0.0, 6.0)]
    zeroed = {(o.facade_id, o.storey) for o in building.facade.overrides if o.wwr == 0}
    assert zeroed == {('main.r0.e1', 0), ('main.r0.e1', 1)}
    open_room = jobs.energy_rc_job(request(wwr=0.4))
    shared = jobs.energy_rc_job(request(wwr=0.4, shared_floors_by_edge=[[], [1, 2], [], []]))
    assert shared['heating_energy_kwh'] < open_room['heating_energy_kwh']


def test_context_is_placed_in_the_rotated_frame_and_shades():
    south = {'footprint_coordinates': [[-10, -20], [14, -20], [14, -8], [-10, -8]], 'floors': 6,
             'floor_to_floor_height': 3.2}
    building = rc.build_building({**ROOM, 'orientation_offset': 30.0}, [south], CONSTRUCTIONS, 'Office', False)
    ring = building.context.buildings[0].footprints[0].exterior
    a = math.radians(-30.0)
    x, y = south['footprint_coordinates'][0]
    assert ring[0] == pytest.approx((x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)))
    bare = jobs.energy_rc_job(request(wwr=0.5))
    shaded = jobs.energy_rc_job({**request(wwr=0.5), 'context_buildings': [south]})
    assert shaded['heating_energy_kwh'] > bare['heating_energy_kwh']


def test_deeper_overhang_lowers_cooling():
    shallow = jobs.energy_rc_job(request(wwr=0.6, wall_thickness=0.1))
    deep = jobs.energy_rc_job(request(wwr=0.6, wall_thickness=0.1, additional_horizontal_shading_depth=1.0))
    assert deep['cooling_energy_kwh'] < shallow['cooling_energy_kwh']


@pytest.mark.parametrize('program', ['Office', 'Residential', 'School', 'Retail', 'Hospital', 'Industrial'])
def test_every_program_runs(program):
    req = request()
    req['energy']['building_program'] = program
    result = jobs.energy_rc_job(req)
    assert np.isfinite(result['total_energy_kwh']) and result['total_energy_kwh'] > 0


def test_broken_geometry_and_unknown_constructions_are_client_errors(monkeypatch):
    with pytest.raises(jobs.BadInput, match='enclose 0.00'):
        jobs.energy_rc_job(request(footprint_coordinates=[[0, 0], [4, 0], [8, 0]]))

    def missing(names):
        raise LookupError('EPSM constructions not found: [x]')

    monkeypatch.setattr(jobs, 'fetch_constructions', missing)
    with pytest.raises(jobs.BadInput, match='not found'):
        jobs.energy_rc_job(request())

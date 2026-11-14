"""Daylight/energy jobs, packing and EnergyPlus output parsing."""

import numpy as np
import pytest

from simulation.energy import idf_builder, local_runner
from tests import full_room
from worker import jobs, packing

ROOM = {
    'footprint_coordinates': [[0, 0], [4, 0], [4, 4], [0, 4]],
    'floors': 2,
    'simulate_all_floors': True,
    'floor_to_floor_height': 3.0,
    'sensor_grid': {'x_dim': 2.0, 'y_dim': 2.0, 'offset': 0.75},
}
CONSTRUCTIONS = {
    kind: {'name': f'{kind}_c', 'u_value_w_m2k': 1.4, 'layers': [
        {'material_name': 'Concrete', 'thickness_m': 0.2, 'conductivity_w_mk': 1.1,
         'density_kg_m3': 2000, 'specific_heat_j_kgk': 900},
    ]}
    for kind in ('wall', 'floor', 'roof', 'window')
}


def test_packing_round_trip():
    values = np.array([0.0, 1.234, 99.99, 100.0])
    assert np.allclose(packing.unpack(packing.percent(values)), values, atol=0.005)
    pts = np.arange(12, dtype=np.float32).reshape(4, 3)
    assert np.array_equal(packing.unpack(packing.points(pts)), pts)


def test_daylight_job_returns_packed_and_plain(monkeypatch):
    monkeypatch.setenv('FOTON_BACKEND', 'reference')
    request = {'room': full_room(ROOM), 'daylight': {'quality': 'preview'}}
    packed = jobs.daylight_job(request)
    plain = jobs.daylight_job({**request, 'pack': False})

    assert packed['weather'] == 'default:gothenburg_tmyx.epw'
    assert packed['df']['dtype'] == 'uint16'
    assert len(plain['df']) == plain['sensor_count'] == packed['sensor_count']
    assert np.allclose(packing.unpack(packed['df']), plain['df'], atol=0.01)


def test_bad_epw_is_a_client_error():
    with pytest.raises(jobs.BadInput, match='EPW'):
        jobs.daylight_job({'room': full_room(ROOM), 'epw': 'not a weather file'})


def test_room_too_small_for_its_sensor_grid_is_a_client_error(monkeypatch):
    monkeypatch.setenv('FOTON_BACKEND', 'reference')
    room = full_room({**ROOM, 'sensor_grid': {'x_dim': 20.0, 'y_dim': 20.0, 'offset': 0.75}})
    with pytest.raises(jobs.BadInput, match='No sensor fits'):
        jobs.daylight_job({'room': room})


@pytest.mark.parametrize('program', list(idf_builder.PROGRAMS))
def test_idf_builds_for_every_program(program):
    idf, area, _ = idf_builder.build_idf(full_room(ROOM), CONSTRUCTIONS, program, False, [])
    assert area == pytest.approx(32.0)
    assert 'Zone Ideal Loads Supply Air Sensible Heating Energy,Hourly' in idf


def test_energy_job_with_fake_energyplus(monkeypatch):
    monkeypatch.setattr(jobs, 'fetch_constructions', lambda names: CONSTRUCTIONS)

    def fake_eplus(idf_path, epw_path, sim_dir, multipliers):
        return {'heating_energy_kwh': 3200.0, 'cooling_energy_kwh': 320.0,
                'total_energy_kwh': 6400.0, 'monthly_heat_balance': None,
                'heating_hourly_kwh': [1.0] * 8760, 'cooling_hourly_kwh': [0.0] * 8760}

    monkeypatch.setattr('simulation.energy.simulate.run_local_eplus', fake_eplus)
    result = jobs.energy_job({'room': full_room(ROOM), 'energy': {'constructions': {}}})

    assert result['floor_area_m2'] == 32.0
    assert result['heating_demand_kwh_m2'] == 100.0
    assert packing.unpack(result['heating_hourly_kwh']).shape == (8760,)


def test_unknown_construction_is_client_error(monkeypatch):
    def missing(names):
        raise LookupError('EPSM constructions not found: [x]')

    monkeypatch.setattr(jobs, 'fetch_constructions', missing)
    with pytest.raises(jobs.BadInput, match='not found'):
        jobs.energy_job({'room': full_room(ROOM), 'energy': {'constructions': {'wall': 'x'}}})


def test_hourly_loads_from_eso_keep_last_environment(tmp_path):
    lines = [
        'Program Version,EnergyPlus, Version 24.2.0-94a887817b',
        '1,5,Environment Title[],Latitude[deg],Longitude[deg],Time Zone[],Elevation[m]',
        '2,8,Day of Simulation[],Month[],Day of Month[],DST Indicator[1=yes 0=no],Hour[],StartMinute[],EndMinute[],DayType',
        '471,9,ZONE_1 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Heating Energy [J] !Monthly [Value,Min,Day,Hour,Minute,Max,Day,Hour,Minute]',
        '472,1,ZONE_1 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Heating Energy [J] !Hourly',
        '523,1,ZONE_1 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Cooling Energy [J] !Hourly',
        '574,1,ZONE_2 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Heating Energy [J] !Hourly',
        'End of Data Dictionary',
        '1,SIZING DAY,57.7,11.9,1.0,10.0',
        '2,1,1,21, 0, 1, 0.00,60.00,SummerDesignDay',
        '472,99',
        '1,RUN PERIOD 1,57.7,11.9,1.0,10.0',
        '2,1,1,1, 0, 1, 0.00,60.00,Monday',
        '472,3600000',
        '523,0',
        '574,7200000',
        '2,1,1,1, 0, 2, 0.00,60.00,Monday',
        '472,0',
        '523,1800000',
        '574,3600000',
        '4,31, 1',
        '471,99,0,1,1,0,99,1,1,0',
        'End of Data',
    ]
    path = tmp_path / 'eplusout.eso'
    path.write_text('\n'.join(lines))
    loads = local_runner.parse_hourly_loads(path)
    assert loads['heating_hourly_kwh'] == [3.0, 1.0]
    assert loads['cooling_hourly_kwh'] == [0.0, 0.5]

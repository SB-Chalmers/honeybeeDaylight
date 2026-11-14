"""Daylight/energy jobs, packing and EnergyPlus output parsing."""

import numpy as np
import pytest

from simulation.energy import idf_builder, simulate
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


MTR = """Program Version,EnergyPlus, Version 24.2.0-94a887817b
5,1,Cumulative Days of Simulation[] ! When Run Period Meters Requested
13,11,Heating:DistrictHeatingWater [J] !RunPeriod [Value,Min,Month,Day,Hour,Minute,Max,Month,Day,Hour,Minute]
14,11,Electricity:Facility [J] !RunPeriod [Value,Min,Month,Day,Hour,Minute,Max,Month,Day,Hour,Minute]
15,11,DistrictHeatingWater:Facility [J] !RunPeriod [Value,Min,Month,Day,Hour,Minute,Max,Month,Day,Hour,Minute]
End of Data Dictionary
1,RUN PERIOD 1,57.7,11.9,1.0,10.0
5,365
13,11520000000,0,1,1,1,0,1,1,1,1,0
14,7200000000,0,1,1,1,0,1,1,1,1,0
15,11520000000,0,1,1,1,0,1,1,1,1,0
End of Data
"""

ESO = """Program Version,EnergyPlus, Version 24.2.0-94a887817b
1,5,Environment Title[],Latitude[deg],Longitude[deg],Time Zone[],Elevation[m]
2,8,Day of Simulation[],Month[],Day of Month[],DST Indicator[1=yes 0=no],Hour[],StartMinute[],EndMinute[],DayType
4,2,Cumulative Days of Simulation[],Month[]  ! When Monthly Report Variables Requested
471,9,FLOORS_2_5 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Heating Energy [J] !Monthly [Value,Min,Day,Hour,Minute,Max,Day,Hour,Minute]
472,1,FLOOR_1 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Heating Energy [J] !Hourly
523,1,FLOOR_1 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Cooling Energy [J] !Hourly
574,1,FLOORS_2_5 IDEAL LOADS AIR SYSTEM,Zone Ideal Loads Supply Air Sensible Heating Energy [J] !Hourly
600,9,FLOORS_2_5,Zone Lights Electricity Energy [J] !Monthly [Value,Min,Day,Hour,Minute,Max,Day,Hour,Minute]
End of Data Dictionary
1,SIZING DAY,57.7,11.9,1.0,10.0
2,1,1,21, 0, 1, 0.00,60.00,SummerDesignDay
472,99
1,RUN PERIOD 1,57.7,11.9,1.0,10.0
2,1,1,1, 0, 1, 0.00,60.00,Monday
472,3600000
523,0
574,7200000
2,1,1,1, 0, 2, 0.00,60.00,Monday
472,0
523,1800000
574,3600000
4,31, 1
471,36000000,0,1,1,0,99,1,1,0
600,3600000,0,1,1,0,99,1,1,0
End of Data
"""


def _fake_energyplus(idf, epw_path, out):
    (out / 'eplusout.mtr').write_text(MTR)
    (out / 'eplusout.eso').write_text(ESO)


def test_energy_job_with_fake_energyplus(monkeypatch):
    monkeypatch.setattr(jobs, 'fetch_constructions', lambda names: CONSTRUCTIONS)
    monkeypatch.setattr(simulate, 'run_eplus', _fake_energyplus)
    result = jobs.energy_job({'room': full_room(ROOM), 'energy': {'constructions': {}}})

    assert result['floor_area_m2'] == 32.0
    assert result['heating_energy_kwh'] == 3200.0
    assert result['heating_demand_kwh_m2'] == 100.0
    assert result['total_energy_kwh'] == 3200.0 + 2000.0  # every fuel: heating + electricity
    assert packing.unpack(result['heating_hourly_kwh']).tolist() == pytest.approx([3.0, 1.0])


def test_unknown_construction_is_client_error(monkeypatch):
    def missing(names):
        raise LookupError('EPSM constructions not found: [x]')

    monkeypatch.setattr(jobs, 'fetch_constructions', missing)
    with pytest.raises(jobs.BadInput, match='not found'):
        jobs.energy_job({'room': full_room(ROOM), 'energy': {'constructions': {'wall': 'x'}}})


def test_eso_keeps_the_last_environment_and_scales_zone_variables(tmp_path):
    path = tmp_path / 'eplusout.eso'
    path.write_text(ESO)
    series = simulate.read_eso(path)
    heating, cooling = (simulate._sum_zones(series, simulate.BALANCE[key], 'Hourly') for key in ('heating', 'cooling'))
    assert heating == pytest.approx([3.0, 1.0])  # summed over zones, the sizing day dropped
    assert cooling == pytest.approx([0.0, 0.5])

    balance = simulate.monthly_balance(series, area=1.0, multipliers={'FLOORS_2_5': 4})
    assert balance['heating'][0] == pytest.approx(10.0)   # ideal loads already include the multiplier
    assert balance['lighting'][0] == pytest.approx(4.0)   # zone variables are for one copy of the zone
    assert balance['solar'] == [0.0] * 12  # not reported: zeros

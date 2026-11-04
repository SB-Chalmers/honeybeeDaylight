"""Daylight/energy jobs, packing and EnergyPlus output parsing."""

import numpy as np
import pytest

from simulation.energy import idf_builder, local_runner
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
    request = {'room': ROOM, 'daylight': {'quality': 'preview'}}
    packed = jobs.daylight_job(request)
    plain = jobs.daylight_job({**request, 'pack': False})

    assert packed['weather'] == 'default:gothenburg_tmyx.epw'
    assert packed['df']['dtype'] == 'uint16'
    assert len(plain['df']) == plain['sensor_count'] == packed['sensor_count']
    assert np.allclose(packing.unpack(packed['df']), plain['df'], atol=0.01)


def test_bad_room_and_bad_epw_are_client_errors():
    with pytest.raises(jobs.BadInput):
        jobs.daylight_job({'room': {**ROOM, 'floors': 0}})
    with pytest.raises(jobs.BadInput, match='EPW'):
        jobs.daylight_job({'room': ROOM, 'epw': 'not a weather file'})


@pytest.mark.parametrize('program', list(idf_builder.PROGRAMS))
def test_idf_builds_for_every_program(program):
    idf, area = idf_builder.build_idf(ROOM, CONSTRUCTIONS, program, False, [])
    assert area == pytest.approx(32.0)
    assert 'Zone Ideal Loads Supply Air Sensible Heating Energy,Hourly' in idf


def test_energy_job_with_fake_energyplus(monkeypatch):
    monkeypatch.setattr(jobs, 'fetch_constructions', lambda names: CONSTRUCTIONS)

    def fake_eplus(idf_path, epw_path, sim_dir):
        return {'heating_energy_kwh': 3200.0, 'cooling_energy_kwh': 320.0,
                'total_energy_kwh': 6400.0, 'monthly_heat_balance': None,
                'heating_hourly_kwh': [1.0] * 8760, 'cooling_hourly_kwh': [0.0] * 8760}

    monkeypatch.setattr('simulation.energy.simulate.run_local_eplus', fake_eplus)
    result = jobs.energy_job({'room': ROOM, 'energy': {'constructions': {}}})

    assert result['floor_area_m2'] == 32.0
    assert result['heating_demand_kwh_m2'] == 100.0
    assert packing.unpack(result['heating_hourly_kwh']).shape == (8760,)


def test_unknown_construction_is_client_error(monkeypatch):
    def missing(names):
        raise LookupError('EPSM constructions not found: [x]')

    monkeypatch.setattr(jobs, 'fetch_constructions', missing)
    with pytest.raises(jobs.BadInput, match='not found'):
        jobs.energy_job({'room': ROOM, 'energy': {'constructions': {'wall': 'x'}}})


def test_hourly_loads_sum_zones_and_skip_monthly_rows(tmp_path):
    heat = 'ZONE_{}_IDEALAIR IDEAL LOADS AIR SYSTEM:Zone Ideal Loads Supply Air Sensible Heating Energy [J](Hourly)'
    cool = 'ZONE_1_IDEALAIR IDEAL LOADS AIR SYSTEM:Zone Ideal Loads Supply Air Sensible Cooling Energy [J](Hourly)'
    monthly = 'ZONE_1_IDEALAIR IDEAL LOADS AIR SYSTEM:Zone Ideal Loads Supply Air Sensible Heating Energy [J](Monthly)'
    rows = [
        ['Date/Time', heat.format(1), heat.format(2), cool, monthly],
        [' 01/01  01:00:00', '3600000', '7200000', '0', ''],
        [' 01/01  02:00:00', '0', '3600000', '1800000', ''],
        [' January', '', '', '', '99'],
    ]
    path = tmp_path / 'output.csv'
    path.write_text('\n'.join(','.join(row) for row in rows))
    loads = local_runner.parse_hourly_loads(path)
    assert loads['heating_hourly_kwh'] == [3.0, 1.0]
    assert loads['cooling_hourly_kwh'] == [0.0, 0.5]

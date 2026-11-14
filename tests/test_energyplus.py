"""One real EnergyPlus run (skipped where EnergyPlus is not installed; CI runs it in the image)."""

import shutil

import pytest

from simulation.energy.simulate import run_energy

pytestmark = pytest.mark.skipif(not shutil.which('energyplus'), reason='EnergyPlus not on PATH')

LAYER = {'material_name': 'Concrete', 'thickness_m': 0.2, 'conductivity_w_mk': 1.1,
         'density_kg_m3': 2000, 'specific_heat_j_kgk': 900}
CONSTRUCTIONS = {kind: {'name': f'{kind}_c', 'u_value_w_m2k': 1.4, 'layers': [LAYER]}
                 for kind in ('wall', 'floor', 'roof', 'window')}


def test_six_storeys_add_up(tmp_path):
    """Annual, hourly and monthly heating agree, so no zone multiplier is applied twice or not at all."""
    # 6 floors: the 4 middle floors run as one zone with multiplier 4.
    room = {'footprint_coordinates': [[0, 0], [8, 0], [8, 6], [0, 6]], 'floors': 6,
            'floor_to_floor_height': 3.2, 'wwr': 0.4}
    result = run_energy(room, [], CONSTRUCTIONS, 'Office', False, 'weather/gothenburg_tmyx.epw', str(tmp_path))
    annual = result['heating_energy_kwh']
    hourly = sum(result['heating_hourly_kwh'])
    monthly = sum(result['monthly_heat_balance']['heating']) * result['floor_area_m2']
    assert annual > 0
    assert len(result['heating_hourly_kwh']) == 8760
    assert abs(hourly - annual) < 0.05 * annual
    assert abs(monthly - annual) < 0.05 * annual

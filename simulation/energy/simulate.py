"""Build the EnergyPlus IDF for a building and run it locally."""

from __future__ import annotations

import os

from .idf_builder import build_idf
from .local_runner import run_local_eplus


def run_energy(
    room: dict,
    context_buildings: list[dict],
    constructions: dict,
    building_program: str,
    natural_ventilation: bool,
    epw_path: str,
    workdir: str,
) -> dict:
    """Generate the IDF, run EnergyPlus in ``workdir`` and return totals and demands.

    Blocking (EnergyPlus runs as a subprocess), so call it from a worker thread.
    Each call uses its own folder, so any number can run at the same time.
    """
    os.makedirs(workdir, exist_ok=True)
    idf_text, floor_area = build_idf(
        room, constructions, building_program, natural_ventilation, context_buildings,
    )
    idf_path = os.path.join(workdir, 'energy.idf')
    with open(idf_path, 'w', encoding='utf-8') as stream:
        stream.write(idf_text)

    raw = run_local_eplus(idf_path, epw_path, os.path.join(workdir, 'eplus'))
    area = max(float(floor_area), 1e-9)
    heating = float(raw.get('heating_energy_kwh') or 0)
    cooling = float(raw.get('cooling_energy_kwh') or 0)
    total = float(raw.get('total_energy_kwh') or heating + cooling)
    return {
        'floor_area_m2': round(float(floor_area), 2),
        'heating_energy_kwh': heating,
        'cooling_energy_kwh': cooling,
        'total_energy_kwh': total,
        'heating_demand_kwh_m2': round(heating / area, 3),
        'cooling_demand_kwh_m2': round(cooling / area, 3),
        'total_energy_kwh_m2': round(total / area, 3),
        'monthly_heat_balance': raw.get('monthly_heat_balance'),
        'heating_hourly_kwh': raw.get('heating_hourly_kwh'),
        'cooling_hourly_kwh': raw.get('cooling_hourly_kwh'),
    }

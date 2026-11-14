"""Build the EnergyPlus IDF for a building, run EnergyPlus and read its results.

Uses the ``energyplus`` binary on PATH (or ``EPLUS_BINARY``); the Docker image
ships EnergyPlus 24.2. Each run gets its own folder, so any number can run at
the same time. Every result comes from the two text outputs EnergyPlus always
writes: annual totals from meters (eplusout.mtr), hourly loads and the monthly
heat balance from report variables (eplusout.eso).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .idf_builder import BALANCE, COOLING_METER, HEATING_METER, SITE_METERS, build_idf

EPLUS_BINARY = os.environ.get('EPLUS_BINARY', 'energyplus')
EPLUS_TIMEOUT = 600  # seconds
J_TO_KWH = 1 / 3_600_000
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

def run_energy(
    room: dict,
    context_buildings: list[dict],
    constructions: dict,
    building_program: str,
    natural_ventilation: bool,
    epw_path: str,
    workdir: str,
) -> dict:
    """Generate the IDF, run EnergyPlus in ``workdir`` and return totals, demands and load profiles.

    Blocking (EnergyPlus runs as a subprocess). Raises RuntimeError if EnergyPlus fails.
    """
    idf_text, area, multipliers = build_idf(room, constructions, building_program, natural_ventilation,
                                            context_buildings)
    out = Path(workdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'in.idf').write_text(idf_text, encoding='utf-8')
    run_eplus(out / 'in.idf', epw_path, out)

    meters = read_eso(out / 'eplusout.mtr')
    heating, cooling, total = (sum(meters.get(f'{name} (RunPeriod)'.upper(), [0.0])[0] for name in names) * J_TO_KWH
                               for names in ((HEATING_METER,), (COOLING_METER,), SITE_METERS))
    series = read_eso(out / 'eplusout.eso')
    return {
        'floor_area_m2': round(area, 2),
        'heating_energy_kwh': round(heating, 1),
        'cooling_energy_kwh': round(cooling, 1),
        'total_energy_kwh': round(total, 1),
        'heating_demand_kwh_m2': round(heating / area, 3),
        'cooling_demand_kwh_m2': round(cooling / area, 3),
        'total_energy_kwh_m2': round(total / area, 3),
        'monthly_heat_balance': monthly_balance(series, area, multipliers),
        'heating_hourly_kwh': _sum_zones(series, BALANCE['heating'], 'Hourly'),
        'cooling_hourly_kwh': _sum_zones(series, BALANCE['cooling'], 'Hourly'),
    }


def run_eplus(idf: Path, epw_path: str, out: Path) -> None:
    """Run EnergyPlus; ``--expandobjects`` turns Honeybee's HVACTemplate ideal loads into real objects."""
    binary = shutil.which(EPLUS_BINARY)
    if not binary:
        raise RuntimeError(f'EnergyPlus binary not found ({EPLUS_BINARY!r}). Set EPLUS_BINARY.')
    proc = subprocess.run(
        [binary, '--weather', os.path.abspath(epw_path), '--output-directory', str(out.resolve()),
         '--expandobjects', str(idf.resolve())],
        capture_output=True, text=True, timeout=EPLUS_TIMEOUT, cwd=out,
    )
    if proc.returncode != 0 or not (out / 'eplusout.mtr').exists():
        err = out / 'eplusout.err'
        tail = err.read_text(errors='replace')[-3000:] if err.exists() else '(no error file)'
        raise RuntimeError(f'EnergyPlus failed (exit={proc.returncode}). Last errors:\n{tail}\n'
                           f'output:\n{(proc.stdout + proc.stderr)[-2000:]}')


def read_eso(path: Path) -> dict[str, list[float]]:
    """Every series in an .eso or .mtr file, for the last environment (the run period).

    Keys look like ``'FLOOR_1:ZONE LIGHTS ELECTRICITY ENERGY (MONTHLY)'`` for a
    variable and ``'ELECTRICITY:FACILITY (RUNPERIOD)'`` for a meter. Dictionary
    lines are ``code,count,[key,]name [unit] !Frequency``; data lines are
    ``code,value[,min/max...]``. Codes 1 to 6 are environment and time stamps;
    a new environment (code 1) starts the series again.
    """
    names: dict[str, str] = {}
    series: dict[str, list[float]] = {}
    with open(path, encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if line.startswith('End of Data Dictionary'):
                break
            code, _, rest = line.partition(',')
            if not code.isdigit() or int(code) <= 6 or '!' not in rest:
                continue
            fields, _, frequency = rest.partition('!')
            name = ':'.join(f.split('[')[0].strip() for f in fields.split(',')[1:] if f.strip())
            names[code] = f'{name} ({frequency.split()[0]})'.upper()
        for line in stream:
            code, _, rest = line.partition(',')
            if code == '1':
                series = {}
            elif code in names:
                series.setdefault(names[code], []).append(float(rest.split(',')[0]))
    return series


def _sum_zones(series: dict, variable: str, frequency: str, multipliers: dict | None = None) -> list[float] | None:
    """A variable summed over every zone, in kWh, optionally times each zone's multiplier."""
    total = None
    suffix = f':{variable} ({frequency})'.upper()
    for name, values in series.items():
        if name.endswith(suffix):
            scale = J_TO_KWH * (multipliers or {}).get(name[:-len(suffix)], 1)
            total = [v * scale + (total[i] if total else 0.0) for i, v in enumerate(values)]
    return total


def monthly_balance(series: dict, area: float, multipliers: dict) -> dict:
    """Monthly heat balance chart in kWh/m2 of floor area.

    EnergyPlus reports zone variables (people, lights, windows...) for one copy
    of a multiplied zone, so they are scaled by its multiplier. Ideal-loads
    variables already include it.
    """
    balance: dict = {'months': MONTHS}
    for key, variable in BALANCE.items():
        values = _sum_zones(series, variable, 'Monthly', None if key in ('heating', 'cooling') else multipliers)
        balance[key] = [round(v / area, 4) for v in values or [0.0] * 12]
    return balance

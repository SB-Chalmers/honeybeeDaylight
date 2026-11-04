"""Run EnergyPlus locally and parse its results.

Uses the ``energyplus`` binary on PATH (or ``EPLUS_BINARY``). The Docker image
ships EnergyPlus 24.2. Each run gets its own folder, so runs are independent
and can happen in parallel.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

EPLUS_BINARY = os.environ.get('EPLUS_BINARY', 'energyplus')
EPLUS_TIMEOUT = int(os.environ.get('EPLUS_TIMEOUT_SECONDS', '600'))


def run_local_eplus(idf_path: str, epw_path: str, sim_dir: str, multipliers: dict | None = None) -> dict:
    """Run EnergyPlus and return annual totals, monthly balance and hourly loads.

    ``multipliers`` maps upper-case zone names to zone multipliers. EnergyPlus
    reports zone variables (people, lights, windows...) for one copy of the
    zone, so the monthly balance scales them. Ideal-loads variables already
    include the multiplier and are left as they are.

    Blocking. Raises RuntimeError if EnergyPlus produces no HTML summary.
    """
    sim_path = Path(sim_dir)
    sim_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(idf_path, sim_path / 'input.idf')
    shutil.copy2(epw_path, sim_path / 'weather.epw')

    binary = shutil.which(EPLUS_BINARY)
    if not binary:
        raise RuntimeError(f'EnergyPlus binary not found ({EPLUS_BINARY!r}). Set EPLUS_BINARY.')
    cmd = [
        binary,
        '--weather', str(sim_path / 'weather.epw'),
        '--output-directory', str(sim_path),
        '--expandobjects',
        str(sim_path / 'input.idf'),
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=EPLUS_TIMEOUT,
        cwd=str(sim_path),
    )

    htm = sim_path / 'eplustbl.htm'
    if not htm.exists():
        err_file = sim_path / 'eplusout.err'
        err_tail = err_file.read_text(errors='replace')[-3000:] if err_file.exists() else '(no error file)'
        raise RuntimeError(
            f'EnergyPlus produced no HTML results table (exit={proc.returncode}). '
            f'Last errors:\n{err_tail}\nstderr:\n{proc.stderr[-2000:]}'
        )

    result = _parse_htm(htm, multipliers)
    # Hourly loads come straight from eplusout.eso (no ReadVarsESO/CSV step).
    eso_path = sim_path / 'eplusout.eso'
    if eso_path.exists():
        result.update(parse_hourly_loads(eso_path))
    else:
        result.update({'heating_hourly_kwh': None, 'cooling_hourly_kwh': None})
    return result


# ---------------------------------------------------------------------------
# Hourly heating / cooling series (eplusout.eso)
# ---------------------------------------------------------------------------

_HOURLY_COLUMNS = {
    'heating_hourly_kwh': 'ZONE IDEAL LOADS SUPPLY AIR SENSIBLE HEATING ENERGY [J](HOURLY)',
    'cooling_hourly_kwh': 'ZONE IDEAL LOADS SUPPLY AIR SENSIBLE COOLING ENERGY [J](HOURLY)',
}


def parse_hourly_loads(eso_path: Path) -> dict:
    """Sum hourly ideal-loads heating and cooling across zones, in kWh per hour.

    ESO dictionary lines look like ``472,1,KEY,Variable [J] !Hourly``; data
    lines are ``472,value`` after a ``2,...`` time stamp. Only the last
    environment (the run period) is kept.
    """
    codes: dict[str, set[str]] = {key: set() for key in _HOURLY_COLUMNS}
    with open(eso_path, encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if line.startswith('End of Data Dictionary'):
                break
            parts = line.rstrip('\n').split(',', 3)
            if len(parts) < 4 or '!' not in parts[3]:
                continue
            variable, _, frequency = parts[3].partition('!')
            frequency = frequency.split('[')[0].strip()
            name = f'{parts[2]}:{variable.strip()}({frequency})'.upper()
            for key, suffix in _HOURLY_COLUMNS.items():
                if name.endswith(suffix):
                    codes[key].add(parts[0])

        series: dict[str, list[float]] = {key: [] for key in codes}
        new_stamp = False
        for line in stream:
            code, _, value = line.partition(',')
            if code == '1':  # new environment
                series = {key: [] for key in codes}
            elif code == '2':
                new_stamp = True
            else:
                for key, key_codes in codes.items():
                    if code in key_codes:
                        if new_stamp:
                            for values in series.values():
                                values.append(0.0)
                            new_stamp = False
                        series[key][-1] += float(value) / 3_600_000

    return {key: (values or None) for key, values in series.items()}


# ---------------------------------------------------------------------------
# HTML table parser (mirrors EPSM's parse_html_with_table_lookup logic)
# ---------------------------------------------------------------------------

def _table_value(soup, table_title: str, row_label: str, col_index: int = 1):
    """Find a table by its bold header and return a numeric cell value."""
    header = soup.find('b', string=table_title)
    if not header:
        return None
    table = header.find_next('table')
    if not table:
        return None
    for row in table.find_all('tr'):
        cells = row.find_all('td')
        if not cells:
            continue
        if cells[0].get_text(strip=True) == row_label:
            try:
                return float(cells[col_index].get_text(strip=True))
            except (IndexError, ValueError):
                return None
    return None


def _parse_htm(htm_path: Path, multipliers: dict | None = None) -> dict:
    """Parse EnergyPlus HTML summary tables and return absolute energy values."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        htm_path.read_text(encoding='utf-8', errors='replace'), 'html.parser'
    )

    # Building area [m²] — col 1 of the Building Area table.
    area = _table_value(soup, 'Building Area', 'Total Building Area', 1)

    # ZoneHVAC:IdealLoadsAirSystem reports heating as District Heating Water
    # and cooling as District Cooling in the EnergyPlus End Uses summary table.
    # Col 12 = District Heating Water [GJ], col 11 = District Cooling [GJ].
    # All End Uses values are in GJ — convert to kWh (1 GJ = 277.778 kWh).
    GJ_TO_KWH = 277.778

    heating_gj = _table_value(soup, 'End Uses', 'Heating', 12)
    cooling_gj = _table_value(soup, 'End Uses', 'Cooling', 11)
    if cooling_gj is None:
        cooling_gj = _table_value(soup, 'End Uses', 'Cooling', 12)

    heating_kwh = (heating_gj or 0.0) * GJ_TO_KWH
    cooling_kwh = (cooling_gj or 0.0) * GJ_TO_KWH

    # Total Site Energy [GJ] is col 1 of the Site and Source Energy table.
    # This includes all end uses (lighting, equipment, HVAC), not just HVAC.
    total_gj = _table_value(soup, 'Site and Source Energy', 'Total Site Energy', 1)
    total_kwh = (total_gj * GJ_TO_KWH) if total_gj else (heating_kwh + cooling_kwh)

    # Monthly heat balance from Output:Table:Monthly
    monthly = _parse_monthly_balance(soup, area or 1.0, multipliers)

    return {
        'heating_energy_kwh': round(heating_kwh, 1),
        'cooling_energy_kwh': round(cooling_kwh, 1),
        'total_energy_kwh': round(total_kwh, 1),
        'floor_area_m2': round(area or 0.0, 1),
        'monthly_heat_balance': monthly,
    }


# ---------------------------------------------------------------------------
# Monthly heat balance parser (Output:Table:Monthly → heat balance chart)
# ---------------------------------------------------------------------------

_MONTHS_LONG  = ['January','February','March','April','May','June',
                 'July','August','September','October','November','December']
_MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun',
                 'Jul','Aug','Sep','Oct','Nov','Dec']

# Maps uppercase substrings in column headers to component keys.
# Order matters: more specific patterns first.
_COL_PATTERNS = [
    ('ZONE IDEAL LOADS SUPPLY AIR SENSIBLE HEATING', 'heating'),
    ('ZONE IDEAL LOADS SUPPLY AIR SENSIBLE COOLING', 'cooling'),
    ('ZONE PEOPLE SENSIBLE HEATING',                 'people'),
    ('ZONE LIGHTS ELECTRICITY',                      'lighting'),
    ('ZONE ELECTRIC EQUIPMENT ELECTRICITY',          'equipment'),
    ('ZONE WINDOWS TOTAL HEAT GAIN',                 'solar'),
    ('ZONE INFILTRATION SENSIBLE HEAT GAIN',         'infiltration_gain'),
    ('ZONE INFILTRATION SENSIBLE HEAT LOSS',         'infiltration_loss'),
]


def _parse_monthly_balance(soup, floor_area_m2: float, multipliers: dict | None = None) -> dict | None:
    """Parse all 'Custom Monthly Report' tables produced by Output:Table:Monthly.

    EnergyPlus emits one table per zone keyname.  The table is transposed:
    rows = months (January … December + Annual/Min/Max), columns = variables.
    Zone-level variables are summed across all zone tables; building-level
    variables use max() to avoid double-counting identical values.
    Values with empty units '[]' are not tracked — skip them.
    """
    aggregated: dict[str, list[float]] = {}

    for b_tag in soup.find_all('b', string='Custom Monthly Report'):
        tbl = b_tag.find_next('table')
        if not tbl:
            continue
        rows = tbl.find_all('tr')
        if len(rows) < 2:
            continue
        # Each table is preceded by "For: <b>KEY</b>"; zone tables need scaling.
        for_label = b_tag.find_previous(string=lambda text: text and text.strip().startswith('For:'))
        zone_tag = for_label.find_next('b') if for_label else None
        zone = zone_tag.get_text(strip=True).upper() if zone_tag else ''
        multiplier = (multipliers or {}).get(zone, 1)

        # --- Header: map column index → (component_key, J→kWh factor) ---
        hdr_cells = rows[0].find_all(['td', 'th'])
        col_map: dict[int, tuple[str, float]] = {}
        for ci, cell in enumerate(hdr_cells[1:], 1):
            text = cell.get_text(strip=True).upper()
            # Skip untracked columns (empty units means EnergyPlus found no data)
            if '[J]' in text:
                factor = 1.0 / 3_600_000     # J → kWh
            elif '[GJ]' in text:
                factor = 277.778
            elif '[MJ]' in text:
                factor = 0.2778
            else:
                continue  # '[]' or no unit = variable not tracked
            for pattern, key in _COL_PATTERNS:
                if pattern in text:
                    col_map[ci] = (key, factor)
                    break

        if not col_map:
            continue

        # --- Data rows: each row is one month ---
        for row in rows[1:]:
            cells = row.find_all('td')
            if not cells:
                continue
            label = cells[0].get_text(strip=True)
            month_idx = next(
                (i for i, m in enumerate(_MONTHS_LONG) if m.lower() == label.lower()),
                -1
            )
            if month_idx < 0:
                continue

            for ci, (key, factor) in col_map.items():
                if ci >= len(cells):
                    continue
                try:
                    val_kwh = float(cells[ci].get_text(strip=True)) * factor
                except (ValueError, TypeError):
                    val_kwh = 0.0

                if key not in aggregated:
                    aggregated[key] = [0.0] * 12

                # Heating and cooling from IdealLoads appear once per zone;
                # sum across zones to get building total.
                scale = 1 if key in ('heating', 'cooling') else multiplier
                aggregated[key][month_idx] += val_kwh * scale

    if not aggregated:
        return None

    fa = max(floor_area_m2, 1.0)
    result: dict = {'months': _MONTHS_SHORT}
    for k, vals in aggregated.items():
        result[k] = [round(v / fa, 4) for v in vals]
    return result

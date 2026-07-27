"""Local EnergyPlus runner using the same Docker image as EPSM.

Drop-in replacement for the EPSM HTTP submission path. Runs
nrel/energyplus:23.2.0 directly via Docker and parses the HTML summary
that EnergyPlus writes (eplustbl.htm → output.htm).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

EPLUS_IMAGE = os.environ.get('EPLUS_DOCKER_IMAGE', 'nrel/energyplus:24.2.0')
EPLUS_PLATFORM = os.environ.get('EPLUS_DOCKER_PLATFORM', 'linux/amd64')
EPLUS_TIMEOUT = int(os.environ.get('EPLUS_TIMEOUT_SECONDS', '600'))

# Optional path to a native EnergyPlus binary.  When set (or auto-detected via
# PATH), it is used instead of Docker — avoids QEMU emulation on Apple Silicon.
EPLUS_BINARY = os.environ.get('EPLUS_BINARY', '')


def _find_native_eplus() -> str | None:
    """Return path to a native EnergyPlus binary, or None if not found."""
    import shutil
    if EPLUS_BINARY:
        return EPLUS_BINARY if os.path.isfile(EPLUS_BINARY) else None
    # Common install locations on macOS and Linux
    for candidate in ('energyplus', 'EnergyPlus'):
        path = shutil.which(candidate)
        if path:
            return path
    for prefix in (
        '/usr/local/EnergyPlus-23-2-0',
        '/Applications/EnergyPlus-23-2-0',
        '/usr/local/EnergyPlus-24-2-0',
        '/Applications/EnergyPlus-24-2-0',
        '/Applications/OpenStudio-3.9.0/EnergyPlus',  # OpenStudio-bundled ARM64 binary
        '/Applications/OpenStudio-3.8.0/EnergyPlus',
        '/Applications/OpenStudio-3.7.0/EnergyPlus',
    ):
        candidate = os.path.join(prefix, 'energyplus')
        if os.path.isfile(candidate):
            return candidate
    return None


def run_local_eplus(idf_path: str, epw_path: str, sim_dir: str) -> dict:
    """Run EnergyPlus via Docker and return parsed energy totals.

    Blocking call — safe to use from a background thread.

    Returns
    -------
    dict with keys:
        heating_energy_kwh, cooling_energy_kwh, total_energy_kwh, floor_area_m2

    Raises
    ------
    RuntimeError if EnergyPlus does not produce HTML output.
    """
    sim_path = Path(sim_dir)
    sim_path.mkdir(parents=True, exist_ok=True)

    shutil.copy2(idf_path, sim_path / 'input.idf')
    shutil.copy2(epw_path, sim_path / 'weather.epw')

    native = _find_native_eplus()
    if native:
        # Native binary — no Docker, no emulation. Works on macOS ARM64 and Linux.
        cmd = [
            native,
            '--weather', str(sim_path / 'weather.epw'),
            '--output-directory', str(sim_path),
            '--expandobjects', '--readvars',
            str(sim_path / 'input.idf'),
        ]
    else:
        # Docker fallback — works natively on Linux x86_64 deployment.
        # Will segfault on Apple Silicon due to QEMU emulation of amd64 image.
        cmd = [
            'docker', 'run', '--rm',
            '-v', f'{sim_path}:/var/simdata/energyplus',
            '--platform', EPLUS_PLATFORM,
            EPLUS_IMAGE,
            'energyplus',
            '--weather', '/var/simdata/energyplus/weather.epw',
            '--output-directory', '/var/simdata/energyplus',
            '--expandobjects', '--readvars',
            '/var/simdata/energyplus/input.idf',
        ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=EPLUS_TIMEOUT,
        cwd=str(sim_path),
    )

    (sim_path / 'run_output.log').write_text(
        f"COMMAND: {' '.join(cmd)}\n\n"
        f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
    )

    # Rename standard EnergyPlus output files to the names the rest of the
    # backend expects (same mapping as EPSM's services.py).
    for src, dst in {
        'eplusout.err': 'output.err',
        'eplustbl.htm': 'output.htm',
        'eplusout.eso': 'output.eso',
        'eplusout.csv': 'output.csv',
        'eplusout.mtr': 'output.mtr',
        'eplusout.sql': 'output.sql',
        'eplusout.html': 'output.html',
    }.items():
        src_p = sim_path / src
        if src_p.exists():
            src_p.rename(sim_path / dst)

    htm = sim_path / 'output.htm'
    if not htm.exists() and (sim_path / 'output.html').exists():
        htm = sim_path / 'output.html'

    if not htm.exists():
        err_file = sim_path / 'output.err'
        err_tail = err_file.read_text(errors='replace')[-3000:] if err_file.exists() else '(no error file)'
        raise RuntimeError(
            f'EnergyPlus produced no HTML results table (exit={proc.returncode}). '
            f'Last errors:\n{err_tail}'
        )

    return _parse_htm(htm)


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


def _parse_htm(htm_path: Path) -> dict:
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
    monthly = _parse_monthly_balance(soup, area or 1.0)

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


def _parse_monthly_balance(soup, floor_area_m2: float) -> dict | None:
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
                aggregated[key][month_idx] += val_kwh

    if not aggregated:
        return None

    fa = max(floor_area_m2, 1.0)
    result: dict = {'months': _MONTHS_SHORT}
    for k, vals in aggregated.items():
        result[k] = [round(v / fa, 4) for v in vals]
    return result

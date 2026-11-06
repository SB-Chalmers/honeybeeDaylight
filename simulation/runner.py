"""Run daylight studies with Foton, entirely in memory.

One ``HoneybeeStudy`` traces the sky coefficients once and both the daylight
factor and (optionally) annual daylight reuse them. Foton builds the annual
sky matrix itself, so no Radiance binaries are needed.
"""

from __future__ import annotations

import os
import re

import numpy as np
from foton import Engine
from foton.honeybee import HoneybeeStudy


QUALITIES = ('preview', 'final')


def _backend() -> str:
    """Return the requested Foton backend (auto-select by default)."""
    return os.environ.get('FOTON_BACKEND', 'auto').strip().lower() or 'auto'


# One engine kept for the life of the worker process. Without it, every study
# loads and unloads the NVIDIA Vulkan driver, and after about 12 jobs glibc runs
# out of static TLS for libnvidia-tls and every later job fails with "Unable to
# find a Vulkan driver".
_DRIVER_ANCHOR = None


def _keep_driver_loaded(backend: str) -> None:
    global _DRIVER_ANCHOR
    if _DRIVER_ANCHOR is None and backend != 'reference':
        _DRIVER_ANCHOR = Engine({'backend': backend})


def _floor_number(room_identifier: str) -> int | None:
    """Floor number from a room id like 'Daylight_Room_Floor_03' (None if absent)."""
    match = re.search(r'_Floor_(\d+)$', room_identifier or '')
    return int(match.group(1)) if match else None


def run_daylight(
    model,
    sensor_count: int,
    epw_path: str,
    *,
    run_sda: bool = False,
    da_lux: float = 300,
    sda_target_pct: float = 50.0,
    quality: str = 'final',
    weather_memo: dict | None = None,
) -> dict:
    """Run DF (and optionally annual DA/sDA) for a Honeybee model.

    Returns flat, index-aligned numpy arrays in Honeybee grid order:
    ``points`` (n, 3), ``df`` (n,), ``da`` (n,) or None, plus the grid slices.
    """
    if quality not in QUALITIES:
        raise ValueError(f'quality must be one of {QUALITIES}.')

    backend = _backend()
    _keep_driver_loaded(backend)
    study = HoneybeeStudy(model, backend=backend, weather_memo=weather_memo)
    positions = np.asarray(study.prepared.arrays['sensor_positions'], dtype=np.float32)

    df_run = study.daylight_factor(quality=quality)
    order = np.concatenate([grid.sensor_indices for grid in df_run.grids])
    if order.size != sensor_count:
        raise RuntimeError(
            f'Foton prepared {order.size} sensors, but the Honeybee model reported '
            f'{sensor_count}.'
        )

    grids = []
    start = 0
    for grid in df_run.grids:
        grids.append({
            'identifier': grid.identifier,
            'full_identifier': grid.full_identifier,
            'room_identifier': grid.room_identifier,
            'floor_number': _floor_number(grid.room_identifier),
            'start_sensor_index': start,
            'sensor_count': grid.sensor_count,
        })
        start += grid.sensor_count

    df = np.concatenate([np.asarray(grid.values, dtype=np.float32) for grid in df_run.grids])

    da = None
    if run_sda:
        annual = study.annual_daylight(
            epw_path,
            quality=quality,
            threshold=float(da_lux),
            target_time=float(sda_target_pct),
        )
        da = np.concatenate([
            np.asarray(annual.grid(grid['full_identifier']).da, dtype=np.float32)
            for grid in grids
        ])
        if da.size != df.size:
            raise RuntimeError(
                f'Daylight result arrays are not index-aligned: df={df.size}, da={da.size}.'
            )

    return {
        'grids': grids,
        'points': positions[order],
        'df': df,
        'da': da,
        'timings': {'df': df_run.timings.get('total_seconds')},
    }


def df_summary(df: np.ndarray) -> dict:
    """Mean, min and max daylight factor (%)."""
    return {
        'mean_df': round(float(np.mean(df)), 3),
        'min_df': round(float(np.min(df)), 3),
        'max_df': round(float(np.max(df)), 3),
    }


def sda_summary(da: np.ndarray, da_lux: float, sda_target_pct: float) -> dict:
    """sDA = share of sensors whose DA (% of occupied hours >= da_lux) meets the target."""
    passing = int(np.count_nonzero(da >= sda_target_pct))
    total = int(da.size)
    return {
        'sda': round(passing / total * 100.0, 2) if total else 0.0,
        'da_lux': int(da_lux),
        'sda_target_pct': float(sda_target_pct),
        'sensor_count': total,
        'passing_sensors': passing,
    }

"""Compact encodings for large result arrays.

A packed array is ``{"dtype", "scale", "shape", "data"}`` where ``data`` is the
base64 of a little-endian typed array and ``value = raw / scale``. In
JavaScript::

    const bytes = Uint8Array.from(atob(p.data), c => c.charCodeAt(0));
    const raw = new ({uint8: Uint8Array, uint16: Uint16Array,
                      float32: Float32Array})[p.dtype](bytes.buffer);
    const values = Array.from(raw, v => v / p.scale);
"""

from __future__ import annotations

import base64

import numpy as np

_DTYPES = {'uint8': '<u1', 'uint16': '<u2', 'float32': '<f4'}


def pack(values: np.ndarray, dtype: str = 'float32', scale: float = 1.0) -> dict:
    arr = np.asarray(values, dtype=np.float64)
    if dtype != 'float32':
        info = np.iinfo(np.dtype(_DTYPES[dtype]))
        arr = np.clip(np.rint(arr * scale), info.min, info.max)
    raw = arr.astype(_DTYPES[dtype])
    return {
        'dtype': dtype,
        'scale': float(scale),
        'shape': list(raw.shape),
        'data': base64.b64encode(raw.tobytes()).decode('ascii'),
    }


def unpack(packed: dict) -> np.ndarray:
    raw = np.frombuffer(base64.b64decode(packed['data']), dtype=_DTYPES[packed['dtype']])
    return (raw.astype(np.float64) / packed['scale']).reshape(packed['shape'])


def percent(values: np.ndarray) -> dict:
    """Percentages (DF, DA) at 0.01 % resolution, 2 bytes per sensor."""
    return pack(values, 'uint16', 100.0)


def points(values: np.ndarray) -> dict:
    """Sensor coordinates as float32, 12 bytes per sensor."""
    return pack(values, 'float32')


def series(values) -> dict:
    """Time series (e.g. hourly kWh) as float32, 4 bytes per value."""
    return pack(values, 'float32')

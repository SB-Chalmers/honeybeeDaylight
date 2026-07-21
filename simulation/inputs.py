"""
simulation.inputs
─────────────────
Load, merge, and validate room inputs from a dict or JSON file.

Public API
----------
load_room_inputs(source, key='room_inputs') -> dict
validate_room_inputs(cfg) -> None  (raises ValueError on bad input)
DEFAULT_ROOM_INPUTS                -> reference defaults dict
"""

import copy
import json
import os

# ── Defaults ──────────────────────────────────────────────────────────────────

GRID_DIM = 0.5  # fallback sensor spacing (m)

DEFAULT_ROOM_INPUTS = {
    # 2-D footprint polygon; auto-closed if last point ≠ first.
    'footprint_coordinates': [
        (0.0, 0.0),
        (8.0, 0.0),
        (8.0, 6.0),
        (0.0, 6.0),
    ],
    # 0° = north (+Y in Ladybug Tools); positive values rotate clockwise.
    'orientation_offset': 0.0,
    'floor_to_floor_height': 3.2,
    # 1-based; floor elevation = (selected_floor_number - 1) × floor_to_floor_height
    'selected_floor_number': 1,
    # Per-cardinal WWR in [0, 1]. Use 'wwr' shorthand to set all four at once.
    'wwr_by_cardinal': {
        'north': 0.40,
        'east': 0.30,
        'south': 0.25,
        'west': 0.20,
    },
    # Sensor grid; omit to use GRID_DIM defaults.
    'sensor_grid': {
        'x_dim': 0.5,
        'y_dim': 0.5,
        'offset': 0.75,
    },
    # Wall thickness is the baseline shading depth for overhang + fins.
    'wall_thickness': 0.30,
    'additional_horizontal_shading_depth': 0.00,
    'additional_vertical_shading_depth': 0.00,
    # Window-generation defaults aligned with the Three.js editor settings.
    'window_width': 1.2,
    'window_height': 1.5,
    'window_spacing': 0.3,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _deep_merge(base: dict, updates: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _fanout_wwr(cfg: dict) -> dict:
    """Expand a top-level 'wwr' float into wwr_by_cardinal if present."""
    if 'wwr' in cfg:
        ratio = float(cfg['wwr'])
        cfg.setdefault('wwr_by_cardinal', {})
        for d in ('north', 'east', 'south', 'west'):
            cfg['wwr_by_cardinal'].setdefault(d, ratio)
        del cfg['wwr']
    return cfg


# ── Public API ────────────────────────────────────────────────────────────────

def load_room_inputs(source, key: str = 'room_inputs') -> dict:
    """
    Load room inputs from a dict, a JSON file path, or a raw JSON string.
    Missing fields are filled from DEFAULT_ROOM_INPUTS.

    Parameters
    ----------
    source : dict | str
        A dict of room params, a path to a JSON file, or a raw JSON string.
    key : str
        Top-level key to read from when source is a file/string. Falls back
        to the root object if the key is absent but the root looks like a
        room input schema.
    """
    if isinstance(source, dict):
        incoming = copy.deepcopy(source)
    else:
        # Treat as file path first, then raw JSON string.
        if os.path.isabs(source) or os.path.exists(source):
            if not os.path.exists(source):
                print(f'Input JSON not found at {source}. Using defaults.')
                return copy.deepcopy(DEFAULT_ROOM_INPUTS)
            with open(source, 'r') as f:
                payload = json.load(f)
        else:
            payload = json.loads(source)

        if not isinstance(payload, dict):
            raise ValueError('Input JSON must be an object at the root level.')

        required = {'footprint_coordinates', 'orientation_offset',
                    'floor_to_floor_height', 'selected_floor_number', 'wwr_by_cardinal'}
        if key and key in payload:
            incoming = payload[key]
        elif required.issubset(payload.keys()):
            incoming = payload
        else:
            print(f'Key "{key}" not found. Using defaults.')
            return copy.deepcopy(DEFAULT_ROOM_INPUTS)

    incoming = _fanout_wwr(incoming)
    return _deep_merge(DEFAULT_ROOM_INPUTS, incoming)


def validate_room_inputs(cfg: dict) -> None:
    """Raise ValueError if cfg contains invalid room parameters."""
    if cfg['selected_floor_number'] < 1:
        raise ValueError('selected_floor_number must be >= 1.')
    if cfg['floor_to_floor_height'] <= 0:
        raise ValueError('floor_to_floor_height must be > 0.')
    if cfg['wall_thickness'] < 0:
        raise ValueError('wall_thickness must be >= 0.')
    if cfg.get('window_width', 1.2) <= 0:
        raise ValueError('window_width must be > 0.')
    if cfg.get('window_height', 1.5) <= 0:
        raise ValueError('window_height must be > 0.')
    if cfg.get('window_spacing', 0.3) < 0:
        raise ValueError('window_spacing must be >= 0.')

    coords = cfg['footprint_coordinates']
    if len(coords) < 3:
        raise ValueError('footprint_coordinates must include at least 3 points.')

    expected_edges = len(coords) - 1 if coords[0] == coords[-1] else len(coords)

    edge_wwr = cfg.get('wwr_by_edge')
    if edge_wwr is not None:
        if len(edge_wwr) != expected_edges:
            raise ValueError(
                f'wwr_by_edge must contain exactly {expected_edges} values to match footprint edges.'
            )
        for i, ratio in enumerate(edge_wwr):
            if not 0 <= ratio <= 1:
                raise ValueError(f'wwr_by_edge[{i}] must be in [0, 1].')

    wwr = cfg['wwr_by_cardinal']
    for direction in ('north', 'east', 'south', 'west'):
        if direction not in wwr:
            raise ValueError(f'wwr_by_cardinal is missing "{direction}".')
        if not 0 <= wwr[direction] <= 1:
            raise ValueError(f'wwr_by_cardinal["{direction}"] must be in [0, 1].')

    grid_cfg = cfg.get('sensor_grid') or {}
    if grid_cfg.get('x_dim', GRID_DIM) <= 0 or grid_cfg.get('y_dim', GRID_DIM) <= 0:
        raise ValueError('sensor_grid x_dim and y_dim must be > 0.')

"""
simulation.inputs
─────────────────
Fill in defaults for and validate the room inputs sent by the API.

Public API
----------
load_room_inputs(source) -> dict
validate_room_inputs(cfg) -> None  (raises ValueError on bad input)
DEFAULT_ROOM_INPUTS                -> reference defaults dict
"""

import copy

from ladybug_geometry.geometry2d import Point2D, Polygon2D

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
    'floors': 1,
    # 1-based; floor elevation = (selected_floor_number - 1) × floor_to_floor_height
    'selected_floor_number': 1,
    # When true, generate one Room and SensorGrid for every floor.
    'simulate_all_floors': False,
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
    'window_spacing': 0.0,  # Heath's default: one pane per window
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _deep_merge(base: dict, updates: dict) -> dict:
    """Copy of base with updates applied; nested dicts are merged, not replaced."""
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

def load_room_inputs(source: dict) -> dict:
    """Return the room inputs with missing fields filled from DEFAULT_ROOM_INPUTS."""
    return _deep_merge(DEFAULT_ROOM_INPUTS, _fanout_wwr(copy.deepcopy(source)))


def validate_room_inputs(cfg: dict) -> None:
    """Raise ValueError if cfg contains invalid room parameters."""
    if cfg.get('floors', 1) < 1:
        raise ValueError('floors must be >= 1.')
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
    if cfg.get('window_spacing', 0.0) < 0:
        raise ValueError('window_spacing must be >= 0.')

    coords = cfg['footprint_coordinates']
    if len(coords) < 3:
        raise ValueError('footprint_coordinates must include at least 3 points.')
    _check_footprint_shape(coords)

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

    shared_floors_by_edge = cfg.get('shared_floors_by_edge')
    if shared_floors_by_edge is not None:
        if len(shared_floors_by_edge) != expected_edges:
            raise ValueError(
                f'shared_floors_by_edge must contain exactly {expected_edges} entries to match footprint edges.'
            )
        floors = cfg.get('floors')
        for i, edge_floors in enumerate(shared_floors_by_edge):
            for floor in edge_floors:
                if floor < 1 or (floors is not None and floor > floors):
                    raise ValueError(f'shared_floors_by_edge[{i}] contains floor {floor}, which the building does not have.')

    wwr = cfg['wwr_by_cardinal']
    for direction in ('north', 'east', 'south', 'west'):
        if direction not in wwr:
            raise ValueError(f'wwr_by_cardinal is missing "{direction}".')
        if not 0 <= wwr[direction] <= 1:
            raise ValueError(f'wwr_by_cardinal["{direction}"] must be in [0, 1].')

    grid_cfg = cfg.get('sensor_grid') or {}
    if grid_cfg.get('x_dim', GRID_DIM) <= 0 or grid_cfg.get('y_dim', GRID_DIM) <= 0:
        raise ValueError('sensor_grid x_dim and y_dim must be > 0.')
    if grid_cfg.get('offset', 0.75) >= cfg['floor_to_floor_height']:
        raise ValueError('sensor_grid offset must be below the ceiling (less than floor_to_floor_height).')


MIN_FLOOR_AREA = 1.0  # m²; smaller footprints are slider accidents, not rooms


def _check_footprint_shape(coords) -> None:
    """Raise ValueError if the footprint cannot be extruded into a room.

    Catches what Honeybee and EnergyPlus would otherwise fail on later with an
    unclear error: a footprint with (almost) no area, such as a zero-width
    rectangle or points on one line, and one whose edges cross (a bow tie).
    Repeated points are allowed and ignored.
    """
    points = []
    for x, y in coords:
        if not points or (x, y) != points[-1]:
            points.append((x, y))
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    polygon = Polygon2D([Point2D(x, y) for x, y in points]) if len(points) >= 3 else None
    if polygon and polygon.is_self_intersecting:
        raise ValueError('footprint_coordinates edges cross each other; list the corners in order around the room.')
    area = polygon.area if polygon else 0.0
    if area < MIN_FLOOR_AREA:
        raise ValueError(
            f'footprint_coordinates enclose {area:.2f} m²; a room needs at least '
            f'{MIN_FLOOR_AREA:g} m². Check for zero width or points on one line.'
        )

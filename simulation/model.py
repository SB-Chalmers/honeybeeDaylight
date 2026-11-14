"""
simulation.model
────────────────
Build the daylight Honeybee Model and its SensorGrids from a room.

    build_model(room, context_buildings) -> (Model, [SensorGrid per floor])

This is the daylight model: detailed windows (one per window module, centred
in the storey), overhangs and fins. The energy model is built separately in
energy/idf_builder.py with one window per wall.
"""

import numpy as np
from honeybee.model import Model
from honeybee.room import Room
from honeybee_radiance.sensor import Sensor
from honeybee_radiance.sensorgrid import SensorGrid

from .geometry import (context_shades, make_adiabatic, shade_windows, shading_depths, shared_edges, storey,
                       window_ratios)


def _add_windows(room: Room, cfg: dict, floor_number: int) -> None:
    """Add rectangular windows by ratio, overhangs and fins; make shared walls adiabatic."""
    window_height, window_spacing = cfg['window_height'], cfg['window_spacing']
    overhang, fin = shading_depths(cfg)
    walls = [face for face in room.faces if str(face.type) == 'Wall']
    # Like Heath: windows centred vertically in the storey.
    sill_height = max(0.0, (cfg['floor_to_floor_height'] - window_height) / 2.0)
    centerline_separation = max(cfg['window_width'] + window_spacing, 0.01)
    shared = shared_edges(cfg, floor_number)
    for index, (face, ratio) in enumerate(zip(walls, window_ratios(cfg, walls))):
        if index in shared:
            make_adiabatic(face)
        elif ratio > 0:
            face.apertures_by_ratio_rectangle(
                ratio=ratio,
                aperture_height=window_height,
                sill_height=sill_height,
                horizontal_separation=centerline_separation,
                vertical_separation=window_spacing,
            )
            shade_windows(face, overhang, fin)


def _points_inside(polygon: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Even-odd (ray casting) point-in-polygon test, vectorised over points.

    polygon is (m, 2), points (n, 2). A horizontal ray from each point counts
    the polygon edges it crosses; an odd count means inside.
    """
    x, y = points[:, 0, None], points[:, 1, None]
    x0, y0 = polygon[:, 0], polygon[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    straddles = (y0 <= y) != (y1 <= y)
    with np.errstate(divide='ignore', invalid='ignore'):
        x_cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return (np.count_nonzero(straddles & (x < x_cross), axis=1) % 2) == 1


def _sensor_grid(room: Room, cfg: dict, identifier: str) -> SensorGrid:
    """Upward-facing sensors on a regular grid over the room floor.

    Ladybug's ``Face3D.mesh_grid`` does the same in pure Python, one sensor at
    a time, and was about a fifth of a daylight job. Here the grid is laid out
    with numpy: cell centres aligned with the floor's first edge (so it turns
    with the building), kept where they fall inside the footprint, lifted by
    ``offset`` metres. Foton weights sensors equally when a grid has no mesh,
    which a regular grid is anyway.
    """
    g = cfg['sensor_grid']
    x_dim, y_dim = g['x_dim'], g['y_dim']
    floor = room[0].geometry  # room[0] is the floor face
    z = floor.vertices[0].z + g['offset']
    polygon = np.array([(v.x, v.y) for v in floor.boundary], dtype=np.float64)

    # Grid axes: along the first footprint edge and perpendicular to it.
    u = polygon[1] - polygon[0]
    u /= np.linalg.norm(u)
    axes = np.array([u, [-u[1], u[0]]])          # rows: u, v
    local = (polygon - polygon[0]) @ axes.T       # footprint in (u, v)
    lo, hi = local.min(axis=0), local.max(axis=0)
    us = np.arange(lo[0] + x_dim / 2, hi[0], x_dim)
    vs = np.arange(lo[1] + y_dim / 2, hi[1], y_dim)
    centres = np.stack(np.meshgrid(us, vs, indexing='ij'), axis=-1).reshape(-1, 2)
    world = centres @ axes + polygon[0]           # back to world x, y
    world = world[_points_inside(polygon, world)]

    sensors = [Sensor((float(x), float(y), float(z)), (0.0, 0.0, 1.0)) for x, y in world]
    grid = SensorGrid(identifier, sensors)
    grid.room_identifier = room.identifier
    return grid


def build_model(room: dict, context_buildings: list[dict] | None = None):
    """The daylight Model and one SensorGrid per simulated floor (only the selected one by default)."""
    all_floors = room['simulate_all_floors']
    floor_count = room['floors']
    floor_numbers = range(1, floor_count + 1) if all_floors else (room['selected_floor_number'],)
    width = max(2, len(str(floor_count)))
    rooms, grids = [], []
    for floor_number in floor_numbers:
        suffix = f'{floor_number:0{width}d}'
        hb_room = storey(room, floor_number, f'Daylight_Room_Floor_{suffix}' if all_floors else 'Daylight_Room')
        _add_windows(hb_room, room, floor_number)
        rooms.append(hb_room)
        grids.append(_sensor_grid(hb_room, room, f'floor_{suffix}_grid' if all_floors else 'floor_grid'))

    if len(rooms) > 1:
        Room.solve_adjacency(rooms)

    model = Model('Daylight_Model', rooms=rooms)
    for shade in context_shades(context_buildings):
        model.add_shade(shade)
    for grid in grids:
        model.properties.radiance.add_sensor_grid(grid)
    return model, grids

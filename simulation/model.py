"""
simulation.model
────────────────
Build a Honeybee Model with one or more SensorGrids from validated inputs.

Public API
----------
build_model(cfg, context_buildings) -> (Model, SensorGrid or SensorGridCollection)

This is the daylight model: detailed windows (one per window module, centred
in the storey), overhangs and fins. The energy model is built separately in
energy/idf_builder.py with simpler windows.
"""

import math

import numpy as np
from honeybee.model import Model
from honeybee.room import Room
from honeybee.shade import Shade
from honeybee_radiance.sensor import Sensor
from honeybee_radiance.sensorgrid import SensorGrid
from ladybug_geometry.geometry3d.face import Face3D
from ladybug_geometry.geometry3d.pointvector import Point3D
from ladybug_geometry.geometry3d.polyface import Polyface3D

from .inputs import GRID_DIM
from .shared_walls import make_adiabatic, shared_edges_on_floor


class SensorGridCollection(tuple):
    """Floor-ordered SensorGrids with aggregate legacy-compatible properties."""

    @property
    def count(self) -> int:
        """Total sensors over all floors."""
        return sum(grid.count for grid in self)

    @property
    def sensors(self) -> tuple:
        """All sensors, floor by floor."""
        return tuple(sensor for grid in self for sensor in grid.sensors)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _cardinal_from_normal(normal) -> str:
    """Map an outward face normal to the nearest cardinal direction."""
    azimuth = (math.degrees(math.atan2(normal.x, normal.y)) + 360.0) % 360.0
    cardinals = [('north', 0.0), ('east', 90.0), ('south', 180.0), ('west', 270.0)]
    return min(cardinals, key=lambda c: min((azimuth - c[1]) % 360, (c[1] - azimuth) % 360))[0]


def _room_from_footprint(cfg: dict, floor_number: int, identifier: str) -> Room:
    """Extrude the (rotated) footprint into the Room for one storey."""
    coords = list(cfg['footprint_coordinates'])
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    floor_z = (floor_number - 1) * cfg['floor_to_floor_height']
    points = [Point3D(x, y, floor_z) for x, y in coords[:-1]]
    floor_face = Face3D(points)
    rotated = floor_face.rotate_xy(math.radians(cfg['orientation_offset']), Point3D(0, 0, floor_z))
    polyface = Polyface3D.from_offset_face(rotated, cfg['floor_to_floor_height'])
    return Room.from_polyface3d(identifier, polyface)


def _apply_wwr_and_shading(room: Room, cfg: dict, floor_number: int) -> None:
    """Add rectangular windows by ratio, overhangs and fins; make shared walls adiabatic.

    Shading depth = wall thickness (the window reveal) + any extra depth.
    """
    wwr = cfg['wwr_by_cardinal']
    edge_wwr = cfg.get('wwr_by_edge')
    window_width = float(cfg.get('window_width', 1.2))
    window_height = float(cfg.get('window_height', 1.5))
    window_spacing = float(cfg.get('window_spacing', 0.3))
    h_depth = cfg['wall_thickness'] + cfg.get('additional_horizontal_shading_depth', 0.0)
    v_depth = cfg['wall_thickness'] + cfg.get('additional_vertical_shading_depth', 0.0)

    walls = [face for face in room.faces if str(face.type) == 'Wall']

    if edge_wwr is not None:
        if len(edge_wwr) != len(walls):
            raise ValueError(
                f'wwr_by_edge length ({len(edge_wwr)}) does not match wall count ({len(walls)}).'
            )
        ratios = edge_wwr
    else:
        ratios = [wwr.get(_cardinal_from_normal(face.normal), 0.0) for face in walls]

    # Match frontend logic: center windows vertically within each floor height.
    floor_height = float(cfg['floor_to_floor_height'])
    sill_height = max(0.0, (floor_height - window_height) / 2.0)
    centerline_separation = max(window_width + window_spacing, 0.01)

    shared = shared_edges_on_floor(cfg, floor_number)
    for index, (face, ratio) in enumerate(zip(walls, ratios)):
        if index in shared:
            make_adiabatic(face)
            continue
        if ratio <= 0:
            continue
        face.apertures_by_ratio_rectangle(
            ratio=ratio,
            aperture_height=window_height,
            sill_height=sill_height,
            horizontal_separation=centerline_separation,
            vertical_separation=window_spacing,
        )

        for aperture in face.apertures:
            if h_depth > 0:
                aperture.overhang(depth=h_depth)
            if v_depth > 0:
                aperture.left_fin(depth=v_depth)
                aperture.right_fin(depth=v_depth)



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
    g = cfg.get('sensor_grid') or {}
    x_dim, y_dim = g.get('x_dim', GRID_DIM), g.get('y_dim', GRID_DIM)
    floor = room[0].geometry  # room[0] is the floor face
    z = floor.vertices[0].z + g.get('offset', 0.75)
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


def _add_context_buildings(model: Model, context_buildings: list[dict]) -> None:
    """Add each context building as opaque detached shade faces."""
    for building_index, context in enumerate(context_buildings):
        coords = list(context['footprint_coordinates'])
        if coords[0] != coords[-1]:
            coords.append(coords[0])

        height = context['floors'] * context['floor_to_floor_height']
        points = [Point3D(x, y, 0) for x, y in coords[:-1]]
        footprint = Face3D(points)
        prism = Polyface3D.from_offset_face(footprint, height)

        for face_index, face in enumerate(prism.faces):
            model.add_shade(
                Shade(
                    f'ContextBuilding_{building_index}_{face_index}',
                    face,
                    is_detached=True
                )
            )


# ── Public API ────────────────────────────────────────────────────────────────

def build_model(cfg: dict, context_buildings: list[dict] | None = None):
    """
    Build and return a (Model, SensorGrid) pair from validated room inputs.

    Parameters
    ----------
    cfg : dict
        Validated room inputs (output of inputs.load_room_inputs).
    """
    all_floors = bool(cfg.get('simulate_all_floors', False))
    floor_count = int(cfg.get('floors', 1))
    floor_numbers = (
        range(1, floor_count + 1)
        if all_floors
        else (int(cfg['selected_floor_number']),)
    )
    identifier_width = max(2, len(str(floor_count)))
    rooms = []
    grids = []
    for floor_number in floor_numbers:
        suffix = f'{floor_number:0{identifier_width}d}'
        room_identifier = (
            f'Daylight_Room_Floor_{suffix}' if all_floors else 'Daylight_Room'
        )
        grid_identifier = f'floor_{suffix}_grid' if all_floors else 'floor_grid'
        room = _room_from_footprint(cfg, floor_number, room_identifier)
        _apply_wwr_and_shading(room, cfg, floor_number)
        rooms.append(room)
        grids.append(_sensor_grid(room, cfg, grid_identifier))

    if all_floors and len(rooms) > 1:
        Room.solve_adjacency(rooms)

    model = Model('Daylight_Model', rooms=rooms)
    _add_context_buildings(model, context_buildings or [])
    for grid in grids:
        model.properties.radiance.add_sensor_grid(grid)
    return model, SensorGridCollection(grids) if all_floors else grids[0]

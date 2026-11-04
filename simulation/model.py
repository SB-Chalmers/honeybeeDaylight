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

from honeybee.model import Model
from honeybee.room import Room
from honeybee.shade import Shade
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



def _sensor_grid(room: Room, cfg: dict, identifier: str) -> SensorGrid:
    """Sensor grid over the room floor (room[0] is the floor face), facing up."""
    g = cfg.get('sensor_grid') or {}
    grid = SensorGrid.from_face3d(
        identifier,
        [room[0].geometry],
        x_dim=g.get('x_dim', GRID_DIM),
        y_dim=g.get('y_dim', GRID_DIM),
        offset=g.get('offset', 0.75),
        flip=True,
    )
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

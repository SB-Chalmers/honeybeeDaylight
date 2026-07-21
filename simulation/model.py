"""
simulation.model
────────────────
Build a Honeybee Model + SensorGrid from validated room inputs.

Public API
----------
build_model(cfg) -> (Model, SensorGrid)
"""

import json
import math
import os

from honeybee.model import Model
from honeybee.room import Room
from honeybee_radiance.sensorgrid import SensorGrid
from ladybug_geometry.geometry3d.face import Face3D
from ladybug_geometry.geometry3d.pointvector import Point3D
from ladybug_geometry.geometry3d.polyface import Polyface3D

from .inputs import GRID_DIM


# ── Internal helpers ──────────────────────────────────────────────────────────

def _cardinal_from_normal(normal) -> str:
    """Map an outward face normal to the nearest cardinal direction."""
    azimuth = (math.degrees(math.atan2(normal.x, normal.y)) + 360.0) % 360.0
    cardinals = [('north', 0.0), ('east', 90.0), ('south', 180.0), ('west', 270.0)]
    return min(cardinals, key=lambda c: min((azimuth - c[1]) % 360, (c[1] - azimuth) % 360))[0]


def _room_from_footprint(cfg: dict) -> Room:
    coords = list(cfg['footprint_coordinates'])
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    floor_z = (cfg['selected_floor_number'] - 1) * cfg['floor_to_floor_height']
    points = [Point3D(x, y, floor_z) for x, y in coords[:-1]]
    floor_face = Face3D(points)
    rotated = floor_face.rotate_xy(math.radians(cfg['orientation_offset']), Point3D(0, 0, floor_z))
    polyface = Polyface3D.from_offset_face(rotated, cfg['floor_to_floor_height'])
    return Room.from_polyface3d('Daylight_Room', polyface)


def _apply_wwr_and_shading(room: Room, cfg: dict) -> None:
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

    for face, ratio in zip(walls, ratios):
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



def _sensor_grid(room: Room, cfg: dict) -> SensorGrid:
    g = cfg.get('sensor_grid') or {}
    grid = SensorGrid.from_face3d(
        'floor_grid',
        [room[0].geometry],
        x_dim=g.get('x_dim', GRID_DIM),
        y_dim=g.get('y_dim', GRID_DIM),
        offset=g.get('offset', 0.75),
        flip=True,
    )
    grid.room_identifier = room.identifier
    return grid


# ── Public API ────────────────────────────────────────────────────────────────

def build_model(cfg: dict) -> tuple[Model, SensorGrid]:
    """
    Build and return a (Model, SensorGrid) pair from validated room inputs.

    Parameters
    ----------
    cfg : dict
        Validated room inputs (output of inputs.load_room_inputs).
    """
    room = _room_from_footprint(cfg)
    _apply_wwr_and_shading(room, cfg)
    grid = _sensor_grid(room, cfg)

    model = Model('Daylight_Model', rooms=[room])
    model.properties.radiance.add_sensor_grid(grid)
    return model, grid


def export_model(model: Model, folder: str) -> str:
    """Write model HBJSON to folder and return the file path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, 'model.hbjson')
    with open(path, 'w') as f:
        json.dump(model.to_dict(), f)
    return path

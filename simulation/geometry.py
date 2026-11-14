"""
simulation.geometry
───────────────────
Geometry shared by the daylight model (model.py), the EnergyPlus model
(energy/idf_builder.py) and EPSM RC (energy/rc.py), so the three describe the
same building.

``room`` is the dict from ``api.schemas.RoomInput.to_simulation_dict``: every
field filled in, ``wwr_by_cardinal`` always set, footprint counter-clockwise.
"""

from __future__ import annotations

import math

CARDINALS = (('north', 0.0), ('east', 90.0), ('south', 180.0), ('west', 270.0))


def cardinal(azimuth_deg: float) -> str:
    """Nearest cardinal direction for an azimuth (0 = north, clockwise)."""
    return min(CARDINALS, key=lambda c: min((azimuth_deg - c[1]) % 360, (c[1] - azimuth_deg) % 360))[0]


def facing(normal) -> str:
    """Nearest cardinal direction for an outward wall normal (+Y is north)."""
    return cardinal((math.degrees(math.atan2(normal.x, normal.y)) + 360.0) % 360.0)


def footprint(points) -> list[tuple[float, float]]:
    """The footprint as an open ring of (x, y): the closing point, if sent, removed."""
    ring = [tuple(p) for p in points]
    return ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring


def signed_area(points) -> float:
    """Shoelace area of a footprint, closed or not: positive when counter-clockwise."""
    ring = footprint(points)
    n = len(ring)
    return sum(ring[i][0] * ring[(i + 1) % n][1] - ring[(i + 1) % n][0] * ring[i][1] for i in range(n)) / 2


def footprint_area(points) -> float:
    """Area (m²) of a footprint, closed or not."""
    return abs(signed_area(points))


def window_ratios(room: dict, walls: list) -> list[float]:
    """Window-to-wall ratio per wall, in footprint edge order: wwr_by_edge, else by facing."""
    if room.get('wwr_by_edge') is not None:
        if len(room['wwr_by_edge']) != len(walls):
            raise ValueError(f"wwr_by_edge has {len(room['wwr_by_edge'])} values for {len(walls)} walls.")
        return [float(r) for r in room['wwr_by_edge']]
    return [float(room['wwr_by_cardinal'][facing(wall.normal)]) for wall in walls]


def shading_depths(room: dict) -> tuple[float, float]:
    """(overhang, fin) depth in metres: the wall thickness (window reveal) plus any extra."""
    return (room['wall_thickness'] + room['additional_horizontal_shading_depth'],
            room['wall_thickness'] + room['additional_vertical_shading_depth'])


def shade_windows(wall, overhang: float, fin: float) -> None:
    """Give every window in the wall an overhang and two fins (HB Louver/Extruded Border)."""
    for aperture in wall.apertures:
        if overhang > 0:
            aperture.overhang(depth=overhang)
        if fin > 0:
            aperture.left_fin(depth=fin)
            aperture.right_fin(depth=fin)


def storey(room: dict, floor: int, identifier: str):
    """The Honeybee Room for one storey (1 = ground): the footprint rotated and extruded."""
    from honeybee.room import Room
    from ladybug_geometry.geometry3d import Face3D, Point3D, Polyface3D

    height = room['floor_to_floor_height']
    z = (floor - 1) * height
    face = Face3D([Point3D(x, y, z) for x, y in footprint(room['footprint_coordinates'])])
    face = face.rotate_xy(math.radians(room['orientation_offset']), Point3D(0, 0, z))
    return Room.from_polyface3d(identifier, Polyface3D.from_offset_face(face, height))


def context_shades(context_buildings: list[dict]) -> list:
    """Each context building as opaque detached Shades (HB Shade), unrotated."""
    from honeybee.shade import Shade
    from ladybug_geometry.geometry3d import Face3D, Point3D, Polyface3D

    shades = []
    for i, building in enumerate(context_buildings or []):
        base = Face3D([Point3D(x, y, 0.0) for x, y in footprint(building['footprint_coordinates'])])
        prism = Polyface3D.from_offset_face(base, building['floors'] * building['floor_to_floor_height'])
        shades += [Shade(f'Context_{i}_{j}', face, is_detached=True) for j, face in enumerate(prism.faces)]
    return shades


def shared_edges(room: dict, floor: int) -> set[int]:
    """Footprint edges whose wall touches a neighbouring building on this floor (shared_floors_by_edge)."""
    return {edge for edge, floors in enumerate(room.get('shared_floors_by_edge') or []) if floor in floors}


def make_adiabatic(wall) -> None:
    """Turn a wall into an internal wall with no heat flow through it. The wall must not have windows."""
    from honeybee.boundarycondition import boundary_conditions

    wall.boundary_condition = boundary_conditions.adiabatic

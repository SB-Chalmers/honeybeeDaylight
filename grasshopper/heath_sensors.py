"""
Heath Sensors: the backend's sensor points for Honeybee rooms.

Code of the "Heath Sensors" GhPython component; feed its output to the stock
"HB Sensor Grid" component. The input is the rotated footprint curve of each
simulated floor, at floor level, in Heath's point order (the curves that are
extruded into the rooms). The grid is aligned with the first edge of the
Honeybee floor face, which lists the footprint in reverse, so it turns with
the building: cell centres start half a cell in
from the floor's bounding box in that frame and are kept where they fall inside
the floor, ``offset`` metres above it. Stock "HB Sensor Grid from Rooms" aligns
its grid with the world axes instead, which puts the sensors elsewhere (and
drops cells on rotated buildings). Same rule as simulation/model.py.

Inputs: _floors (closed polylines), _size_ (m, default 0.5), _offset_ (m, default 0.75)
Outputs: points (a tree, one branch per floor)
         mesh   one square face per sensor, the grid cell around it, in the same
                order: give it to HB Sensor Grid's mesh_ input (or straight to
                LB Spatial Heatmap) to colour the result cell by cell
"""

import math


def _inside(polygon, x, y):
    """Even-odd point-in-polygon test."""
    inside = False
    n = len(polygon)
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        if (y0 <= y) != (y1 <= y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _arange(start, stop, step):
    """numpy.arange for floats: start, start + step, ... below stop."""
    return [start + i * step for i in range(max(0, int(math.ceil((stop - start) / step))))]


def sensor_points(footprint, size=0.5, offset=0.75, cells=None):
    """(x, y, z) sensor positions over a floor given as footprint (x, y, z) points.

    When ``cells`` is a list, the four corners of each sensor's grid cell are
    appended to it, counter-clockwise seen from above.
    """
    z = footprint[0][2] + offset
    polygon = [(p[0], p[1]) for p in reversed(footprint)]  # the Honeybee floor face order
    ox, oy = polygon[0]
    length = math.hypot(polygon[1][0] - ox, polygon[1][1] - oy)
    ux, uy = (polygon[1][0] - ox) / length, (polygon[1][1] - oy) / length
    vx, vy = -uy, ux
    local = [((x - ox) * ux + (y - oy) * uy, (x - ox) * vx + (y - oy) * vy) for x, y in polygon]
    us = _arange(min(p[0] for p in local) + size / 2, max(p[0] for p in local), size)
    vs = _arange(min(p[1] for p in local) + size / 2, max(p[1] for p in local), size)
    points = []
    for u in us:
        for v in vs:
            x, y = ox + u * ux + v * vx, oy + u * uy + v * vy
            if _inside(polygon, x, y):
                points.append((x, y, z))
                if cells is not None:
                    h = size / 2
                    cells.append([(x + a * h * ux + b * h * vx, y + a * h * uy + b * h * vy, z)
                                  for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
    return points


if 'ghenv' in globals() and _floors:
    import Rhino.Geometry as rg
    from Grasshopper import DataTree
    from Grasshopper.Kernel.Data import GH_Path

    points = DataTree[object]()
    mesh = DataTree[object]()
    for i, curve in enumerate(_floors):
        ok, polyline = curve.TryGetPolyline()
        corners = [(p.X, p.Y, p.Z) for p in polyline][:-1]  # drop the closing point
        cells = []
        floor_mesh = rg.Mesh()
        for x, y, z in sensor_points(corners, _size_ or 0.5, _offset_ or 0.75, cells):
            points.Add(rg.Point3d(x, y, z), GH_Path(i))
        for cell in cells:
            first = floor_mesh.Vertices.Count
            for x, y, z in cell:
                floor_mesh.Vertices.Add(x, y, z)
            floor_mesh.Faces.AddFace(first, first + 1, first + 2, first + 3)
        floor_mesh.Normals.ComputeNormals()
        mesh.Add(floor_mesh, GH_Path(i))

"""
Heath Request: read a Heath request (the JSON body of /v1/simulate) in Grasshopper.

Code of the "Heath Request" GhPython component. It only reads values; the
model is built by stock Honeybee components on the canvas. Unlike the Heath
backend, which builds a detailed daylight room and a simpler energy model, the
canvas builds one model for both: every zone has the detailed windows,
overhangs and fins, and the daylight floor is a zone of its own. Numbers that Heath
shows as sliders (floors, orientation, window ratios, window sizes, shading
depths) are sliders on the canvas, so they are not outputs here.

Outputs:
    footprint      the building footprint as a closed polyline at z = 0, not rotated
    floor_height   floor-to-floor height (m)
    context        neighbour footprints (closed polylines at z = 0)
    context_height height of each neighbour (floors x floor height)
    grid_size      sensor spacing (m); grid_offset: sensor height above the floor (m)
    group_floors   first floor (1-based) of each zone
    multipliers    floors each zone stands for (identical middle floors)
    grid_zones     indices of the zones that get a sensor grid (the daylight floors)
    shared_edges   per footprint edge, the floors where it is a party wall
    program        honeybee-energy program type for the Heath building_program
    natural_vent   True when Heath's natural ventilation is on (no cooling)
    wall_layers, roof_layers, floor_layers   EPSM layers, one branch per layer:
                   [name, thickness, conductivity, density, specific heat]
    window_u       EPSM window U-value (W/m2K); SHGC is 0.40 like the backend
    epw            weather file path; quality, da_lux, sda_target: daylight options

Runs in Grasshopper (IronPython 2.7) and CPython: no f-strings or type hints.
tests/test_grasshopper.py checks it against the backend.
"""

import json

EPSM_URL = 'https://epsm.chalmers.se'
DEFAULT_EPW_URL = ('https://raw.githubusercontent.com/SB-Chalmers/ace-sim-api/'
                   'main/weather/gothenburg_tmyx.epw')

# Heath building_program -> honeybee-energy program type (idf_builder.PROGRAMS).
PROGRAMS = {
    'Office': 'Generic Office Program',
    'Residential': '2019::MidriseApartment::Apartment',
    'Retail': '2019::Retail::Retail',
    'School': '2019::SecondarySchool::Classroom',
    'Hospital': '2019::Hospital::Office',
    'Industrial': '2019::Warehouse::Office',
}


def footprint_points(coords):
    """Footprint [x, y] points without a repeated closing point."""
    coords = [tuple(p) for p in coords]
    return coords[:-1] if coords[0] == coords[-1] else coords


def counter_clockwise(room):
    """Reverse a clockwise footprint (and its per-edge lists) like the API does."""
    points = footprint_points(room['footprint_coordinates'])
    n = len(points)
    area2 = sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
                for i in range(n))
    if area2 < 0:
        room['footprint_coordinates'] = [list(p) for p in points[::-1]]
        for name in ('wwr_by_edge', 'shared_floors_by_edge'):
            edges = room.get(name)
            if edges is not None and len(edges) == n:
                room[name] = [edges[(n - 2 - k) % n] for k in range(n)]
    return room


def shared_edges(room, floor):
    """Footprint edges whose wall is a party wall on this floor (1-based)."""
    return set(i for i, floors in enumerate(room.get('shared_floors_by_edge') or [])
               if floor in floors)


def floor_groups(room, floors, alone=()):
    """Zones: ground and top floors alone, identical middle floors merged.

    Floors in ``alone`` (the daylight floors) also get a zone of their own, so
    their sensors sit in real geometry. Without ``alone`` this returns
    [[first, ..., last], ...] like simulation/energy/idf_builder.py.
    """
    groups, previous = [], None
    for floor in range(1, floors + 1):
        key = (floor == 1 or floor in alone, floor == floors or floor in alone,
               frozenset(shared_edges(room, floor)))
        if key == previous and not key[0] and not key[1]:
            groups[-1].append(floor)
        else:
            groups.append([floor])
        previous = key
    return groups


def _get_json(url):
    try:  # Grasshopper: .NET handles TLS more reliably than IronPython's urllib
        from System.Net import WebClient
        return json.loads(WebClient().DownloadString(url))
    except ImportError:
        from urllib.request import urlopen
        return json.loads(urlopen(url, timeout=60).read().decode('utf-8'))


def epsm_layers(names, url=EPSM_URL):
    """{element: record} from EPSM by name, with material data merged into the layers."""
    def records(payload):
        if isinstance(payload, dict):
            payload = payload.get('results', payload.get('data', []))
        return dict((r['name'], r) for r in payload if isinstance(r, dict))

    constructions = records(_get_json(url + '/api/constructions/'))
    materials = records(_get_json(url + '/api/materials/'))
    missing = sorted(set(n for n in names.values() if n not in constructions))
    if missing:
        raise ValueError('EPSM constructions not found: {}'.format(missing))
    selected = {}
    for element, name in names.items():
        record = dict(constructions[name])
        record['layers'] = [dict(layer, **materials.get(layer['material_name'], {}))
                            for layer in record.get('layers', [])]
        selected[element] = record
    return selected


def layer_values(record):
    """[name, thickness, conductivity, density, specific heat] per layer.

    Same defaults as the backend; a record without layers becomes one 0.2 m layer.
    """
    layers = [[l['material_name'], l.get('thickness_m', 0.1), l.get('conductivity_w_mk', 0.8),
               l.get('density_kg_m3', l.get('density', 1800.0)),
               l.get('specific_heat_j_kgk', l.get('specific_heat', 900.0))]
              for l in record.get('layers', [])]
    return layers or [[record['name'] + '_mat', 0.2, 0.8, 1800, 900]]


def weather_file(request, folder):
    """Path of the request's EPW, or of the backend default downloaded once into folder."""
    import io
    import os
    if not os.path.isdir(folder):
        os.makedirs(folder)
    if request.get('epw'):
        path = os.path.join(folder, 'heath_request.epw')
        with io.open(path, 'w', encoding='utf-8') as stream:
            stream.write(request['epw'])
        return path
    path = os.path.join(folder, 'gothenburg_tmyx.epw')
    if not os.path.isfile(path):
        try:
            from System.Net import WebClient
            WebClient().DownloadFile(DEFAULT_EPW_URL, path)
        except ImportError:
            from urllib.request import urlretrieve
            urlretrieve(DEFAULT_EPW_URL, path)
    return path


# The Grasshopper component. Inputs: _request (JSON text) and the sliders
# _floors_, _floor_ (the daylight floor) and all_floors_ (daylight on every floor).
if 'ghenv' in globals() and _request:
    import os
    import tempfile
    import Rhino.Geometry as rg

    def _polyline(coords):
        points = [rg.Point3d(x, y, 0) for x, y in footprint_points(coords)]
        return rg.PolylineCurve(points + [points[0]])

    request = json.loads(_request)
    room = counter_clockwise(request['room'])
    floors = int(_floors_) if _floors_ is not None else int(room.get('floors', 1))
    footprint = _polyline(room['footprint_coordinates'])
    floor_height = float(room.get('floor_to_floor_height', 3.2))
    context = [_polyline(c['footprint_coordinates']) for c in request.get('context_buildings', [])]
    context_height = [c.get('floors', 1) * c.get('floor_to_floor_height', 3.2)
                      for c in request.get('context_buildings', [])]
    grid = room.get('sensor_grid') or {}
    grid_size = grid.get('x_dim', 0.5)
    grid_offset = grid.get('offset', 0.75)
    if all_floors_:
        daylight_floors = list(range(1, floors + 1))
    else:
        selected = int(_floor_) if _floor_ is not None else int(room.get('selected_floor_number', 1))
        daylight_floors = [min(max(selected, 1), floors)]
    groups = floor_groups(room, floors, daylight_floors)
    group_floors = [g[0] for g in groups]
    multipliers = [len(g) for g in groups]
    grid_zones = [i for i, g in enumerate(groups) if g[0] in daylight_floors]
    shared_edges = [str(f) for f in (room.get('shared_floors_by_edge') or [])]
    daylight = request.get('daylight') or {}
    quality = daylight.get('quality', 'preview')
    da_lux = daylight.get('da_lux', 300)
    sda_target = daylight.get('sda_target_pct', 50.0)
    energy = request.get('energy')
    if energy:
        program = PROGRAMS[energy.get('building_program', 'Office')]
        natural_vent = bool(energy.get('natural_ventilation', False))
        records = epsm_layers(energy['constructions'])
        wall_layers = layer_values(records['wall'])
        roof_layers = layer_values(records['roof'])
        floor_layers = layer_values(records['floor'])
        window_u = records['window'].get('u_value_w_m2k', 2.0)
        # Nested lists reach Grasshopper as opaque objects; one branch per layer
        # lets List Item feed the HB Opaque Material inputs.
        from Grasshopper import DataTree
        from Grasshopper.Kernel.Data import GH_Path

        def _tree(rows):
            tree = DataTree[object]()
            for i, row in enumerate(rows):
                tree.AddRange(row, GH_Path(i))
            return tree

        wall_layers, roof_layers, floor_layers = (
            _tree(wall_layers), _tree(roof_layers), _tree(floor_layers))
    epw =weather_file(request, os.path.join(tempfile.gettempdir(), 'heath'))

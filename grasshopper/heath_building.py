"""
Heath Building: rebuild a Heath request as Honeybee objects in Grasshopper.

This is the code inside the "Heath Building" GhPython component. It takes the
JSON body Heath sends to /v1/simulate and makes the same Honeybee objects the
backend makes, so stock Honeybee components and Foton downstream give the same
results:

- ``daylight_rooms``: one room per simulated floor with the Heath windows,
  overhangs and fins (simulation/model.py);
- ``sensor_grids``: the 0.5 m grid aligned with the first footprint edge;
- ``context_shades``: neighbouring buildings as detached shades;
- ``energy_rooms``: one room per floor group with a multiplier, one window per
  wall, program, EPSM constructions and ideal air (simulation/energy/idf_builder.py);
- ``simulation_parameter`` and ``extra_idf``: the EnergyPlus settings and outputs.

The code runs both in Grasshopper (IronPython 2.7) and in CPython, so it uses
no f-strings, type hints or numpy. tests/test_grasshopper.py checks that it
builds the same model as the backend.
"""

import copy
import json
import math

from ladybug_geometry.geometry3d.face import Face3D
from ladybug_geometry.geometry3d.pointvector import Point3D
from ladybug_geometry.geometry3d.polyface import Polyface3D
from honeybee.boundarycondition import boundary_conditions
from honeybee.room import Room
from honeybee.shade import Shade
from honeybee_radiance.sensor import Sensor
from honeybee_radiance.sensorgrid import SensorGrid

EPSM_URL = 'https://epsm.chalmers.se'

# The backend's defaults (api/schemas.py RoomInput).
ROOM_DEFAULTS = {
    'orientation_offset': 0.0,
    'floor_to_floor_height': 3.2,
    'floors': 1,
    'selected_floor_number': 1,
    'simulate_all_floors': False,
    'wwr_by_cardinal': {'north': 0.4, 'east': 0.3, 'south': 0.25, 'west': 0.2},
    'wall_thickness': 0.3,
    'additional_horizontal_shading_depth': 0.0,
    'additional_vertical_shading_depth': 0.0,
    'window_width': 1.2,
    'window_height': 1.5,
    'window_spacing': 0.3,
    'sensor_grid': {'x_dim': 0.5, 'y_dim': 0.5, 'offset': 0.75},
}

# Heath building_program -> honeybee-energy program type (idf_builder.PROGRAMS).
PROGRAMS = {
    'Office': 'Generic Office Program',
    'Residential': '2019::MidriseApartment::Apartment',
    'Retail': '2019::Retail::Retail',
    'School': '2019::SecondarySchool::Classroom',
    'Hospital': '2019::Hospital::Office',
    'Industrial': '2019::Warehouse::Office',
}

CARDINALS = (('north', 0.0), ('east', 90.0), ('south', 180.0), ('west', 270.0))


# 1. Read the request ──────────────────────────────────────────────────────────

def load_request(source, overrides=None):
    """Return the request as a dict with the room defaults filled in.

    ``source`` is the JSON text (or a dict). ``overrides`` replaces room fields,
    e.g. {'floors': 4, 'wwr_by_cardinal': {'south': 0.5}}; None values are ignored.
    """
    request = json.loads(source) if isinstance(source, str) else copy.deepcopy(source)
    room = copy.deepcopy(ROOM_DEFAULTS)
    given = dict(request.get('room') or {})
    if 'wwr' in given:  # one ratio for every direction, unless wwr_by_cardinal is given
        ratio = given.pop('wwr')
        if ratio is not None and 'wwr_by_cardinal' not in given:
            given['wwr_by_cardinal'] = dict((d, ratio) for d, _ in CARDINALS)
    for source_dict in (given, overrides or {}):
        for key, value in source_dict.items():
            if value is None:
                continue
            if isinstance(value, dict) and isinstance(room.get(key), dict):
                room[key].update(value)
            else:
                room[key] = value
    request['room'] = room
    request.setdefault('context_buildings', [])
    request.setdefault('daylight', {})
    return request


def _closed_points(coords):
    """Footprint points without the repeated closing point."""
    coords = [tuple(p) for p in coords]
    return coords[:-1] if coords[0] == coords[-1] else coords


def _cardinal(normal):
    """Nearest cardinal direction for an outward wall normal (+Y is north)."""
    azimuth = (math.degrees(math.atan2(normal.x, normal.y)) + 360.0) % 360.0
    return min(CARDINALS, key=lambda c: min((azimuth - c[1]) % 360, (c[1] - azimuth) % 360))[0]


def _storey(room, floor_number, identifier):
    """Extrude the rotated footprint into the Room for one storey."""
    z = (floor_number - 1) * room['floor_to_floor_height']
    face = Face3D([Point3D(x, y, z) for x, y in _closed_points(room['footprint_coordinates'])])
    face = face.rotate_xy(math.radians(room['orientation_offset']), Point3D(0, 0, z))
    return Room.from_polyface3d(
        identifier, Polyface3D.from_offset_face(face, room['floor_to_floor_height']))


def _wall_ratios(room, walls):
    """Window-to-wall ratio of each wall: per edge if given, else by direction."""
    if room.get('wwr_by_edge') is not None:
        return [float(v) for v in room['wwr_by_edge']]
    return [float(room['wwr_by_cardinal'].get(_cardinal(w.normal), 0.0)) for w in walls]


def shared_edges(room, floor_number):
    """Footprint edges whose wall touches a neighbour on this floor (1-based)."""
    edges = room.get('shared_floors_by_edge') or []
    return set(i for i, floors in enumerate(edges) if floor_number in floors)


# 2. Daylight model (simulation/model.py) ──────────────────────────────────────

def daylight_floors(room):
    """The floor numbers the daylight study simulates."""
    if room.get('simulate_all_floors'):
        return list(range(1, int(room['floors']) + 1))
    return [int(room['selected_floor_number'])]


def daylight_rooms(room):
    """One Room per simulated floor with Heath's windows, overhangs and fins.

    Windows are centred in the storey, ``window_height`` high and
    ``window_width + window_spacing`` apart (HB Apertures by Ratio, subdivided).
    Overhang depth = wall thickness + extra horizontal depth; fins likewise.
    """
    floors = daylight_floors(room)
    all_floors = bool(room.get('simulate_all_floors'))
    width = max(2, len(str(int(room['floors']))))
    height = float(room['floor_to_floor_height'])
    win_h = float(room['window_height'])
    spacing = float(room['window_spacing'])
    sill = max(0.0, (height - win_h) / 2.0)
    separation = max(float(room['window_width']) + spacing, 0.01)
    h_depth = room['wall_thickness'] + room['additional_horizontal_shading_depth']
    v_depth = room['wall_thickness'] + room['additional_vertical_shading_depth']

    rooms = []
    for floor in floors:
        suffix = str(floor).zfill(width)
        name = 'Daylight_Room_Floor_' + suffix if all_floors else 'Daylight_Room'
        hb_room = _storey(room, floor, name)
        walls = [f for f in hb_room.faces if str(f.type) == 'Wall']
        shared = shared_edges(room, floor)
        for index, (wall, ratio) in enumerate(zip(walls, _wall_ratios(room, walls))):
            if index in shared:
                wall.boundary_condition = boundary_conditions.adiabatic
                continue
            if ratio <= 0:
                continue
            wall.apertures_by_ratio_rectangle(ratio, win_h, sill, separation, spacing)
            for aperture in wall.apertures:
                if h_depth > 0:
                    aperture.overhang(depth=h_depth)
                if v_depth > 0:
                    aperture.left_fin(depth=v_depth)
                    aperture.right_fin(depth=v_depth)
        rooms.append(hb_room)
    if len(rooms) > 1:
        Room.solve_adjacency(rooms)
    return rooms


def _inside(polygon, x, y):
    """Even-odd point-in-polygon test (same rule as the backend's numpy version)."""
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
    count = max(0, int(math.ceil((stop - start) / step)))
    return [start + i * step for i in range(count)]


def sensor_grid(hb_room, room, identifier):
    """Upward sensors on a grid aligned with the floor's first edge.

    Cell centres start half a cell in from the footprint's bounding box in that
    rotated frame and are kept where they fall inside the footprint, ``offset``
    metres above the floor. Stock "HB Sensor Grid from Rooms" aligns its grid
    with the world axes instead, which puts the sensors elsewhere.
    """
    g = room['sensor_grid']
    x_dim, y_dim = float(g['x_dim']), float(g['y_dim'])
    floor = hb_room[0].geometry
    z = floor.vertices[0].z + float(g['offset'])
    polygon = [(v.x, v.y) for v in floor.boundary]
    ox, oy = polygon[0]
    length = math.hypot(polygon[1][0] - ox, polygon[1][1] - oy)
    ux, uy = (polygon[1][0] - ox) / length, (polygon[1][1] - oy) / length
    vx, vy = -uy, ux
    local = [((x - ox) * ux + (y - oy) * uy, (x - ox) * vx + (y - oy) * vy) for x, y in polygon]
    us = _arange(min(p[0] for p in local) + x_dim / 2, max(p[0] for p in local), x_dim)
    vs = _arange(min(p[1] for p in local) + y_dim / 2, max(p[1] for p in local), y_dim)
    sensors = []
    for u in us:
        for v in vs:
            x, y = ox + u * ux + v * vx, oy + u * uy + v * vy
            if _inside(polygon, x, y):
                sensors.append(Sensor((x, y, z), (0.0, 0.0, 1.0)))
    grid = SensorGrid(identifier, sensors)
    grid.room_identifier = hb_room.identifier
    return grid


def sensor_grids(rooms, room):
    """One grid per daylight room, named like the backend's."""
    if not room.get('simulate_all_floors'):
        return [sensor_grid(rooms[0], room, 'floor_grid')]
    width = max(2, len(str(int(room['floors']))))
    return [sensor_grid(r, room, 'floor_' + str(f).zfill(width) + '_grid')
            for r, f in zip(rooms, daylight_floors(room))]


def context_shades(context_buildings, prefix='ContextBuilding_'):
    """Each neighbouring building as detached shade faces of its extruded footprint."""
    shades = []
    for b, ctx in enumerate(context_buildings):
        face = Face3D([Point3D(x, y, 0) for x, y in _closed_points(ctx['footprint_coordinates'])])
        prism = Polyface3D.from_offset_face(face, ctx['floors'] * ctx['floor_to_floor_height'])
        for i, geo in enumerate(prism.faces):
            shades.append(Shade('{}{}_{}'.format(prefix, b, i), geo, is_detached=True))
    return shades


# 3. Energy model (simulation/energy/idf_builder.py) ───────────────────────────

def floor_groups(room):
    """Ground and top floors alone; identical middle floors merged into one zone."""
    floors = int(room['floors'])
    groups, previous = [], None
    for floor in range(1, floors + 1):
        key = (floor == 1, floor == floors, frozenset(shared_edges(room, floor)))
        if key == previous and not key[0] and not key[1]:
            groups[-1].append(floor)
        else:
            groups.append([floor])
        previous = key
    return groups


def fetch_epsm(names, url=EPSM_URL):
    """Construction records from EPSM by name, with material data merged into the layers."""
    def get(path):
        try:  # Grasshopper: .NET handles TLS more reliably than IronPython's urllib
            from System.Net import WebClient
            return json.loads(WebClient().DownloadString(url + path))
        except ImportError:
            from urllib.request import urlopen
            return json.loads(urlopen(url + path, timeout=60).read().decode('utf-8'))

    def records(payload):
        if isinstance(payload, dict):
            payload = payload.get('results', payload.get('data', []))
        return dict((r['name'], r) for r in payload if isinstance(r, dict))

    constructions, materials = records(get('/api/constructions/')), records(get('/api/materials/'))
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


def construction_set(constructions):
    """A ConstructionSet from EPSM records (idf_builder._construction_set_from_epsm)."""
    import re
    from honeybee_energy.constructionset import ConstructionSet
    from honeybee_energy.construction.opaque import OpaqueConstruction
    from honeybee_energy.construction.window import WindowConstruction
    from honeybee_energy.material.glazing import EnergyWindowMaterialSimpleGlazSys
    from honeybee_energy.material.opaque import EnergyMaterial

    def safe(name):
        return re.sub(r'[^A-Za-z0-9_\-]', '_', name)[:100]

    def opaque(record):
        layers = [EnergyMaterial(
            safe(l['material_name']), l.get('thickness_m', 0.1), l.get('conductivity_w_mk', 0.8),
            l.get('density_kg_m3', l.get('density', 1800.0)),
            l.get('specific_heat_j_kgk', l.get('specific_heat', 900.0)))
            for l in record.get('layers', [])]
        if not layers:
            layers = [EnergyMaterial(safe(record['name']) + '_mat', 0.2, 0.8, 1800, 900)]
        return OpaqueConstruction(safe(record['name']), layers)

    def window(record):  # simple glazing system, SHGC fixed at 0.40
        glass = EnergyWindowMaterialSimpleGlazSys(
            safe(record['name']) + '_glaz', record.get('u_value_w_m2k', 2.0), 0.40)
        return WindowConstruction(safe(record['name']), [glass])

    cs = ConstructionSet('EPSM_ConstructionSet')
    if 'wall' in constructions:
        cs.wall_set.exterior_construction = opaque(constructions['wall'])
    if 'floor' in constructions:
        cs.floor_set.interior_construction = opaque(constructions['floor'])
    if 'roof' in constructions:
        cs.roof_ceiling_set.exterior_construction = opaque(constructions['roof'])
    if 'window' in constructions:
        cs.aperture_set.window_construction = window(constructions['window'])
    return cs


def energy_rooms(room, constructions, building_program='Office', natural_ventilation=False):
    """One Room per floor group, ready for EnergyPlus.

    Each group is one zone with a multiplier (identical middle floors run once);
    floors and ceilings between storeys and shared walls are adiabatic; each wall
    gets one window at its ratio (HB Apertures by Ratio, not subdivided); no
    overhangs or fins. Program, EPSM constructions and an ideal air system with
    no limits and no economiser are assigned to every room.
    """
    from honeybee.altnumber import no_limit
    from honeybee_energy.hvac.idealair import IdealAirSystem
    from honeybee_energy.lib.programtypes import program_type_by_identifier
    from honeybee_energy.schedule.ruleset import ScheduleRuleset

    floors = int(room['floors'])
    cs = construction_set(constructions)
    rooms = []
    for group in floor_groups(room):
        first, last = group[0], group[-1]
        name = 'Floor_{}'.format(first) if first == last else 'Floors_{}_{}'.format(first, last)
        hb_room = _storey(room, first, name)
        hb_room.multiplier = len(group)
        for face in hb_room.faces:
            if str(face.type) == 'Floor':
                face.boundary_condition = (boundary_conditions.ground if first == 1
                                           else boundary_conditions.adiabatic)
            elif str(face.type) == 'RoofCeiling':
                face.boundary_condition = (boundary_conditions.outdoors if last == floors
                                           else boundary_conditions.adiabatic)
        walls = [f for f in hb_room.faces if str(f.type) == 'Wall']
        shared = shared_edges(room, first)
        for index, (wall, ratio) in enumerate(zip(walls, _wall_ratios(room, walls))):
            if index in shared:
                wall.boundary_condition = boundary_conditions.adiabatic
            elif ratio > 0:
                wall.apertures_by_ratio(ratio, 0.01)

        program = program_type_by_identifier(PROGRAMS[building_program]).duplicate()
        if natural_ventilation and program.setpoint:  # no mechanical cooling
            setpoint = program.setpoint.duplicate()
            setpoint.cooling_schedule = ScheduleRuleset.from_constant_value(
                'NoCooling_' + name, 50.0, program.setpoint.cooling_schedule.schedule_type_limit)
            program.setpoint = setpoint
        hb_room.properties.energy.program_type = program
        hb_room.properties.energy.construction_set = cs
        ideal_air = IdealAirSystem(name + '_IdealAir')
        ideal_air.economizer_type = 'NoEconomizer'
        ideal_air.heating_limit = no_limit
        ideal_air.cooling_limit = no_limit
        hb_room.properties.energy.hvac = ideal_air
        rooms.append(hb_room)
    return rooms


def simulation_parameter():
    """The backend's EnergyPlus settings: no sizing, 3 timesteps per hour, FullExterior.

    Unlike the backend it keeps the SQLite file and hourly zone energy outputs,
    which Honeybee's result components read. Outputs do not change the results.
    """
    from honeybee_energy.simulation.parameter import SimulationParameter
    sim = SimulationParameter()
    control = sim.simulation_control
    control.do_zone_sizing = control.do_system_sizing = control.do_plant_sizing = False
    control.run_for_sizing_periods = False
    control.run_for_run_periods = True
    sim.timestep = 3
    sim.shadow_calculation.solar_distribution = 'FullExterior'
    sim.output.reporting_frequency = 'Hourly'
    sim.output.add_zone_energy_use()
    sim.output.add_gains_and_losses()
    return sim


# 4. Everything at once ────────────────────────────────────────────────────────

def build(source, overrides=None, constructions=None):
    """All Heath objects for a request, as a dict of plain Honeybee objects.

    ``constructions`` are EPSM records; when None they are fetched by the names
    in the request (no energy rooms if the request has no energy section).
    """
    request = load_request(source, overrides)
    room = request['room']
    rooms = daylight_rooms(room)
    result = {
        'request': request,
        'daylight_rooms': rooms,
        'sensor_grids': sensor_grids(rooms, room),
        'daylight_context': context_shades(request['context_buildings']),
        'energy_rooms': [],
        'energy_context': context_shades(request['context_buildings'], 'Context_'),
        'simulation_parameter': simulation_parameter(),
    }
    energy = request.get('energy')
    if energy:
        if constructions is None:
            constructions = fetch_epsm(energy['constructions'])
        result['energy_rooms'] = energy_rooms(
            room, constructions, energy.get('building_program', 'Office'),
            bool(energy.get('natural_ventilation', False)))
    return result


# 5. Weather ───────────────────────────────────────────────────────────────────

DEFAULT_EPW_URL = ('https://raw.githubusercontent.com/SB-Chalmers/honeybeeDaylight/'
                   'main/weather/gothenburg_tmyx.epw')


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


# 6. The Grasshopper component ─────────────────────────────────────────────────
# Inputs: _request (the Heath JSON text) and optional slider overrides.
# Outputs: the objects of build() plus epw and the request after overrides.

if 'ghenv' in globals():
    import os
    import tempfile

    def _number(value, kind=float):
        return None if value is None else kind(value)

    overrides = {
        'floors': _number(_floors_, int),
        'selected_floor_number': _number(_floor_, int),
        'simulate_all_floors': all_floors_,
        'orientation_offset': _number(_orientation_),
        'wwr_by_cardinal': {'north': _number(wwr_north_), 'east': _number(wwr_east_),
                            'south': _number(wwr_south_), 'west': _number(wwr_west_)},
        'wall_thickness': _number(wall_thickness_),
        'additional_horizontal_shading_depth': _number(overhang_extra_),
        'additional_vertical_shading_depth': _number(fin_extra_),
        'window_width': _number(window_width_),
        'window_height': _number(window_height_),
        'window_spacing': _number(window_spacing_),
    }
    overrides['wwr_by_cardinal'] = dict(
        (k, v) for k, v in overrides['wwr_by_cardinal'].items() if v is not None)
    if _request:
        _built = build(_request, overrides)
        daylight_rooms = _built['daylight_rooms']
        grids = _built['sensor_grids']
        context = _built['daylight_context']
        energy_rooms = _built['energy_rooms']
        energy_context = _built['energy_context']
        sim_par = _built['simulation_parameter']
        epw = weather_file(_built['request'], os.path.join(tempfile.gettempdir(), 'heath'))
        request = json.dumps(_built['request'], indent=2)

"""
api.grasshopper
───────────────
Fill the Grasshopper template (grasshopper/heath.ghx) with one Heath request.

A .ghx file is the XML form of a Grasshopper definition, so no Rhino is
needed: the request goes into the panel called ``request``, each slider named
after a room field is set to the request's value, and the two GhPython
components get the current code from this folder, so the file never lags
behind the backend.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent / 'grasshopper'
TEMPLATE = FOLDER / 'heath.ghx'
COMPONENTS = {'Heath Request': 'heath_request.py', 'Heath Sensors': 'heath_sensors.py'}

# Slider nickname -> how to read its value from the room dict.
SLIDERS = {
    '_floors_': lambda r: r['floors'],
    '_floor_': lambda r: r['selected_floor_number'],
    '_orientation_': lambda r: r['orientation_offset'],
    'wwr_north_': lambda r: r['wwr_by_cardinal']['north'],
    'wwr_east_': lambda r: r['wwr_by_cardinal']['east'],
    'wwr_south_': lambda r: r['wwr_by_cardinal']['south'],
    'wwr_west_': lambda r: r['wwr_by_cardinal']['west'],
    'wall_thickness_': lambda r: r['wall_thickness'],
    'overhang_extra_': lambda r: r['additional_horizontal_shading_depth'],
    'fin_extra_': lambda r: r['additional_vertical_shading_depth'],
    'window_width_': lambda r: r['window_width'],
    'window_height_': lambda r: r['window_height'],
    'window_spacing_': lambda r: r['window_spacing'],
}


def _objects(root: ET.Element):
    """(nickname, object chunk) for every object on the canvas."""
    for chunk in root.iter('chunk'):
        if chunk.get('name') != 'Object':
            continue
        nick = chunk.find(".//chunk[@name='Container']/items/item[@name='NickName']")
        if nick is not None:
            yield nick.text, chunk


def _set(chunk: ET.Element, path: str, value: str) -> None:
    item = chunk.find(path)
    if item is None:
        raise RuntimeError(f'Grasshopper template is missing {path}.')
    item.text = value


def grasshopper_file(request: dict) -> bytes:
    """The template .ghx with the request filled in (``request`` as the API received it)."""
    room = request['room']
    tree = ET.parse(TEMPLATE)
    found = set()
    for nick, chunk in _objects(tree.getroot()):
        if nick == 'request':
            _set(chunk, ".//item[@name='UserText']", json.dumps(request, indent=2))
        elif nick in COMPONENTS:
            code = (FOLDER / COMPONENTS[nick]).read_text(encoding='utf-8')
            _set(chunk, ".//item[@name='CodeInput']", code)
        elif nick == 'all_floors_':
            _set(chunk, ".//item[@name='ToggleValue']", str(bool(room['simulate_all_floors'])).lower())
        elif nick in SLIDERS:
            value = float(SLIDERS[nick](room))
            slider = chunk.find(".//chunk[@name='Slider']/items")
            _set(slider, "item[@name='Value']", repr(value))
            for name, keep in (('Min', min), ('Max', max)):  # widen the range if needed
                limit = slider.find(f"item[@name='{name}']")
                limit.text = repr(keep(float(limit.text), value))
        else:
            continue
        found.add(nick)
    missing = {'request', 'all_floors_', *COMPONENTS, *SLIDERS} - found
    if missing:
        raise RuntimeError(f'Grasshopper template has no objects named {sorted(missing)}.')
    return ET.tostring(tree.getroot(), encoding='utf-8', xml_declaration=True)

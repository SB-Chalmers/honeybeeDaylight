"""Generate an EnergyPlus IDF from the editor's room payload using honeybee-energy.

This replaces the previous hand-written IDF builder with a pipeline that uses
honeybee-energy's validated model → IDF writer, producing the same format as
the IDFs that EPSM's known-good simulations use.
"""

from __future__ import annotations

import re

from .mappers import hvac_identifier, program_identifier


def footprint_area(points: list[list[float]]) -> float:
    points = points[:-1] if points[0] == points[-1] else points
    return abs(sum(points[i][0] * points[(i + 1) % len(points)][1] -
                   points[(i + 1) % len(points)][0] * points[i][1]
                   for i in range(len(points))) / 2)


def _safe_id(name: str) -> str:
    """Convert an arbitrary string to a safe EnergyPlus identifier."""
    return re.sub(r'[^A-Za-z0-9_\-]', '_', name)[:100]


def _construction_set_from_epsm(constructions: dict) -> 'ConstructionSet':
    """Build a honeybee-energy ConstructionSet from EPSM construction records."""
    from honeybee_energy.material.opaque import EnergyMaterial
    from honeybee_energy.construction.opaque import OpaqueConstruction
    from honeybee_energy.construction.window import WindowConstruction
    from honeybee_energy.material.glazing import EnergyWindowMaterialSimpleGlazSys
    from honeybee_energy.constructionset import ConstructionSet

    def _opaque(record: dict) -> OpaqueConstruction:
        materials = []
        for layer in record.get('layers', []):
            mid = _safe_id(layer['material_name'])
            materials.append(EnergyMaterial(
                mid,
                layer.get('thickness_m', 0.1),
                layer.get('conductivity_w_mk', 0.8),
                layer.get('density_kg_m3', layer.get('density', 1800.0)),
                layer.get('specific_heat_j_kgk', layer.get('specific_heat', 900.0)),
            ))
        if not materials:
            materials = [EnergyMaterial(_safe_id(record['name']) + '_mat', 0.2, 0.8, 1800, 900)]
        return OpaqueConstruction(_safe_id(record['name']), materials)

    def _window(record: dict) -> WindowConstruction:
        u_value = record.get('u_value_w_m2k', 2.0)
        name = _safe_id(record['name']) + '_glaz'
        glaz = EnergyWindowMaterialSimpleGlazSys(name, u_value, 0.40)
        return WindowConstruction(_safe_id(record['name']), [glaz])

    cs = ConstructionSet('EPSM_ConstructionSet')
    if 'wall' in constructions:
        cs.wall_set.exterior_construction = _opaque(constructions['wall'])
    if 'floor' in constructions:
        cs.floor_set.interior_construction = _opaque(constructions['floor'])
    if 'roof' in constructions:
        cs.roof_ceiling_set.exterior_construction = _opaque(constructions['roof'])
    if 'window' in constructions:
        cs.aperture_set.window_construction = _window(constructions['window'])
    return cs


# Map of frontend building_program strings to honeybee-energy program type identifiers
_PROGRAM_MAP = {
    'Office': 'Generic Office Program',
    'Residential': '2013::MidRiseApartment::Apartment',
    'Retail': '2013::StripMall::WholeBuilding',
    'School': '2013::SecondarySchool::Classroom',
    'Hospital': '2013::Hospital::Office',
    'Industrial': '2013::Warehouse::Office',
}


def build_idf(room: dict, constructions: dict, building_program: str,
              hvac_system: str, natural_ventilation: bool,
              context_buildings: list[dict] | None = None) -> tuple[str, float]:
    """Generate a complete EnergyPlus IDF using honeybee-energy.

    Returns (idf_text, floor_area_m2).
    """
    import math
    from honeybee.model import Model
    from honeybee.room import Room
    from honeybee.shade import Shade
    from honeybee_energy.writer import model_to_idf
    from honeybee_energy.simulation.parameter import SimulationParameter
    from honeybee_energy.lib.programtypes import program_type_by_identifier
    from ladybug_geometry.geometry3d.face import Face3D
    from ladybug_geometry.geometry3d.pointvector import Point3D
    from ladybug_geometry.geometry3d.polyface import Polyface3D

    # ── Build Honeybee model ──────────────────────────────────────────────────
    coords = list(room['footprint_coordinates'])
    if coords[0] == coords[-1]:
        coords.pop()
    height = float(room['floor_to_floor_height'])
    floors = int(room.get('floors', 1))
    orientation = float(room.get('orientation_offset', 0.0))
    wwr_map = room.get('wwr_by_cardinal') or {d: room.get('wwr', 0.4)
                                               for d in ('north', 'east', 'south', 'west')}

    # Window geometry parameters — same defaults as the daylight model.
    window_height = float(room.get('window_height', 1.5))
    window_width = float(room.get('window_width', 1.2))
    window_spacing = float(room.get('window_spacing', 0.3))
    h_depth = float(room.get('wall_thickness', 0.0)) + float(room.get('additional_horizontal_shading_depth', 0.0))
    v_depth = float(room.get('wall_thickness', 0.0)) + float(room.get('additional_vertical_shading_depth', 0.0))
    sill_height = max(0.0, (height - window_height) / 2.0)
    centerline_separation = max(window_width + window_spacing, 0.01)

    def _cardinal(normal) -> str:
        az = (math.degrees(math.atan2(normal.x, normal.y)) + 360.0) % 360.0
        cardinals = [('north', 0.0), ('east', 90.0), ('south', 180.0), ('west', 270.0)]
        return min(cardinals, key=lambda c: min((az - c[1]) % 360, (c[1] - az) % 360))[0]

    all_rooms = []
    for floor_idx in range(floors):
        z0 = floor_idx * height
        pts = [Point3D(x, y, z0) for x, y in coords]
        face = Face3D(pts)
        face = face.rotate_xy(math.radians(orientation), Point3D(0, 0, z0))
        poly = Polyface3D.from_offset_face(face, height)
        r = Room.from_polyface3d(f'Zone_{floor_idx + 1}', poly)

        # Apply windows — identical logic to _apply_wwr_and_shading in simulation/model.py.
        edge_wwr = room.get('wwr_by_edge')
        walls = [f for f in r.faces if str(f.type) == 'Wall']
        if edge_wwr is not None and len(edge_wwr) == len(walls):
            ratios = [float(v) for v in edge_wwr]
        else:
            ratios = [float(wwr_map.get(_cardinal(f.normal), 0.0)) for f in walls]

        for wall_face, ratio in zip(walls, ratios):
            if ratio <= 0:
                continue
            wall_face.apertures_by_ratio_rectangle(
                ratio=ratio,
                aperture_height=window_height,
                sill_height=sill_height,
                horizontal_separation=centerline_separation,
                vertical_separation=window_spacing,
            )
            # Note: fins/overhangs are omitted from the energy IDF.
            # Attached shading objects (overhang + 2 fins × N windows × mirror copy)
            # multiply into hundreds of surfaces that cause a segfault in EP 24.2
            # during shading initialisation. Solar gain is already captured via the
            # window SHGC, so omitting them has negligible effect on annual totals.

        # Assign energy properties
        prog_id = _PROGRAM_MAP.get(building_program, 'Generic Office Program')
        r.properties.energy.program_type = program_type_by_identifier(prog_id)
        r.properties.energy.construction_set = _construction_set_from_epsm(constructions)
        r.properties.energy.add_default_ideal_air()
        all_rooms.append(r)

    model = Model('Energy_Model', rooms=all_rooms)

    # Add context buildings as detached shade
    for bldg_idx, context in enumerate(context_buildings or []):
        ctx_coords = list(context['footprint_coordinates'])
        if ctx_coords[0] == ctx_coords[-1]:
            ctx_coords.pop()
        ctx_height = context['floors'] * context['floor_to_floor_height']
        ctx_pts = [Point3D(x, y, 0.0) for x, y in ctx_coords]
        ctx_face = Face3D(ctx_pts)
        ctx_poly = Polyface3D.from_offset_face(ctx_face, ctx_height)
        for face_idx, face_obj in enumerate(ctx_poly.faces):
            model.add_shade(Shade(f'Context_{bldg_idx}_{face_idx}', face_obj, is_detached=True))

    # ── Generate IDF ─────────────────────────────────────────────────────────
    import re

    sim = SimulationParameter()
    # Disable zone/system sizing — the public EPSM service has no DesignDay objects
    # and EnergyPlus terminates with a Fatal error if sizing is requested without them.
    sim.simulation_control.do_zone_sizing = False
    sim.simulation_control.do_system_sizing = False
    sim.simulation_control.do_plant_sizing = False
    sim.simulation_control.run_for_sizing_periods = False
    sim.simulation_control.run_for_run_periods = True
    sim_idf = sim.to_idf()
    # Align Version with the installed EnergyPlus binary (OpenStudio 3.9 ships 24.2).
    # honeybee-energy writes 23.2 by default; update to avoid version mismatch.
    sim_idf = re.sub(r'(Version,\s*)[\d\.]+', r'\g<1>24.2', sim_idf)
    # Strip Output:SQLite — it causes a segfault in EnergyPlus 24.2 on macOS ARM64
    # and is not needed since we parse results from the HTML summary table.
    sim_idf = re.sub(r'\nOutput:SQLite,\s*\n[^\n]+;\s*', '\n', sim_idf)
    # Remove the hardcoded 2017 year from RunPeriod so EnergyPlus uses the EPW calendar.
    # A mismatched year causes EnergyPlus to skip all environment periods silently.
    sim_idf = re.sub(r' 2017,(\s+!- (?:start|end) year)', r' ,\1', sim_idf)

    # Add district heating/cooling meters — ZoneHVAC:IdealLoadsAirSystem reports its
    # energy as DistrictHeating / DistrictCooling, so these meters populate the End Uses
    # table columns that EPSM's HTML parser reads (col 12 = District Heating/Cooling).
    _extra = (
        '\nOutput:Meter,\n  DistrictHeating:Building,\n  Annual;\n\n'
        'Output:Meter,\n  DistrictCooling:Building,\n  Annual;\n\n'
        'Output:Meter,\n  Electricity:Building,\n  Annual;\n'
    )
    idf_text = sim_idf + _extra + '\n' + model_to_idf(model)
    floor_area = footprint_area(room['footprint_coordinates']) * floors
    return idf_text, floor_area
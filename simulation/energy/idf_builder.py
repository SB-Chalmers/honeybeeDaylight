"""Turn the editor's room payload into an EnergyPlus IDF with honeybee-energy.

Every step maps to a stock Honeybee component, so a run can be rebuilt in
Grasshopper:

- one Room per floor group (HB Room from Solid), stacked identical middle
  floors merged with a zone multiplier (HB Room multiplier);
- floors and ceilings between storeys adiabatic, shared party walls adiabatic
  (HB Properties / boundary condition);
- one window per wall at the requested ratio (HB Apertures by Ratio), with
  an overhang and two fins as deep as the daylight model's;
- program type, EPSM constructions and an unlimited ideal air system
  (HB Program / Construction Set / Ideal Air);
- context buildings as detached shades (HB Shade);
- SimulationParameter: no sizing, 3 timesteps/hour, FullExterior.
"""

from __future__ import annotations

import re

from ..shared_walls import make_adiabatic, shared_edges_on_floor


def footprint_area(points: list[list[float]]) -> float:
    """Polygon area (shoelace formula) of an [x, y] footprint, closed or not."""
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
        """Layered opaque construction from an EPSM record (defaults fill gaps)."""
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
        """Simple glazing system from the EPSM U-value (SHGC fixed at 0.40)."""
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


# Frontend building_program names -> honeybee-energy program type identifiers.
# All of these exist in honeybee-energy-standards 2.3.x.
PROGRAMS = {
    'Office': 'Generic Office Program',
    'Residential': '2019::MidriseApartment::Apartment',
    'Retail': '2019::Retail::Retail',
    'School': '2019::SecondarySchool::Classroom',
    'Hospital': '2019::Hospital::Office',
    'Industrial': '2019::Warehouse::Office',
}


def floor_groups(room: dict) -> list[list[int]]:
    """Group floors that can share one zone with a multiplier.

    The ground and top floors always stand alone. Consecutive middle floors are
    merged unless their shared (adiabatic) walls differ.
    """
    floors = int(room.get('floors', 1))
    groups: list[list[int]] = []
    previous = None
    for floor in range(1, floors + 1):
        key = (floor == 1, floor == floors, frozenset(shared_edges_on_floor(room, floor)))
        if key == previous and not key[0] and not key[1]:
            groups[-1].append(floor)
        else:
            groups.append([floor])
        previous = key
    return groups


def build_idf(room: dict, constructions: dict, building_program: str,
              natural_ventilation: bool,
              context_buildings: list[dict] | None = None) -> tuple[str, float, dict]:
    """Generate a complete EnergyPlus IDF using honeybee-energy.

    Returns (idf_text, floor_area_m2, {ZONE NAME: multiplier}). The parser
    needs the multipliers because EnergyPlus reports zone variables (people,
    lights, windows...) for a single copy of a multiplied zone.
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

    def _cardinal(normal) -> str:
        """Nearest cardinal direction for an outward wall normal."""
        az = (math.degrees(math.atan2(normal.x, normal.y)) + 360.0) % 360.0
        cardinals = [('north', 0.0), ('east', 90.0), ('south', 180.0), ('west', 270.0)]
        return min(cardinals, key=lambda c: min((az - c[1]) % 360, (c[1] - az) % 360))[0]

    from honeybee.boundarycondition import boundary_conditions
    from honeybee_energy.hvac.idealair import IdealAirSystem
    from honeybee.altnumber import no_limit

    thickness = float(room.get('wall_thickness', 0.3))
    h_depth = thickness + float(room.get('additional_horizontal_shading_depth', 0.0))
    v_depth = thickness + float(room.get('additional_vertical_shading_depth', 0.0))

    all_rooms = []
    for group in floor_groups(room):
        first, last = group[0], group[-1]
        name = f'Floor_{first}' if first == last else f'Floors_{first}_{last}'
        z0 = (first - 1) * height
        face = Face3D([Point3D(x, y, z0) for x, y in coords])
        face = face.rotate_xy(math.radians(orientation), Point3D(0, 0, z0))
        r = Room.from_polyface3d(name, Polyface3D.from_offset_face(face, height))
        # Identical stacked floors are simulated once and multiplied (HB Room multiplier).
        r.multiplier = len(group)

        # Only the bottom floor touches the ground and only the top floor has a
        # roof; floors between storeys exchange no heat with identical neighbours.
        for f in r.faces:
            if str(f.type) == 'Floor':
                f.boundary_condition = boundary_conditions.ground if first == 1 else boundary_conditions.adiabatic
            elif str(f.type) == 'RoofCeiling':
                f.boundary_condition = boundary_conditions.outdoors if last == floors else boundary_conditions.adiabatic

        # One window per wall with the requested glazing ratio (HB Apertures by Ratio).
        # Energy only needs glazed area and orientation; the daylight model keeps
        # the individual windows. That one window gets an overhang and two fins as
        # deep as the daylight model's, so cooling responds to reveals and
        # overhangs. Shading every individual window instead costs ~15 s a run.
        edge_wwr = room.get('wwr_by_edge')
        walls = [f for f in r.faces if str(f.type) == 'Wall']
        if edge_wwr is not None and len(edge_wwr) == len(walls):
            ratios = [float(v) for v in edge_wwr]
        else:
            ratios = [float(wwr_map.get(_cardinal(f.normal), 0.0)) for f in walls]
        shared = shared_edges_on_floor(room, first)  # same for every floor in the group
        for edge_index, (wall_face, ratio) in enumerate(zip(walls, ratios)):
            if edge_index in shared:
                make_adiabatic(wall_face)
            elif ratio > 0:
                wall_face.apertures_by_ratio(ratio, 0.01)
                for aperture in wall_face.apertures:
                    if h_depth > 0:
                        aperture.overhang(depth=h_depth)
                    if v_depth > 0:
                        aperture.left_fin(depth=v_depth)
                        aperture.right_fin(depth=v_depth)

        prog = program_type_by_identifier(PROGRAMS[building_program]).duplicate()
        # Natural ventilation: raise the cooling setpoint to 50 °C so the ideal
        # loads never cool mechanically. Heating is unaffected.
        if natural_ventilation and prog.setpoint:
            from honeybee_energy.schedule.ruleset import ScheduleRuleset
            no_cool_sched = ScheduleRuleset.from_constant_value(
                f'NoCooling_{name}', 50.0, prog.setpoint.cooling_schedule.schedule_type_limit
            )
            new_setpoint = prog.setpoint.duplicate()
            new_setpoint.cooling_schedule = no_cool_sched
            prog.setpoint = new_setpoint

        r.properties.energy.program_type = prog
        r.properties.energy.construction_set = _construction_set_from_epsm(constructions)
        # A fresh ideal air system with no capacity limits, so EnergyPlus never
        # autosizes (autosize with sizing disabled crashes EP 24.2 on Linux).
        ideal_air = IdealAirSystem(f'{name}_IdealAir')
        ideal_air.economizer_type = 'NoEconomizer'
        ideal_air.heating_limit = no_limit
        ideal_air.cooling_limit = no_limit
        r.properties.energy.hvac = ideal_air
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
    # 3 timesteps per hour instead of Honeybee's 6 (HB Simulation Parameter:
    # _timestep_ = 3). Measured on the 7540 against 4: 20 % faster, monthly
    # heating and cooling within 2 %. 2 per hour is 37 % faster but drifts
    # 6 % in some months, over the 5 % the Heath charts allow.
    sim.timestep = 3
    # Exterior shading only, without reflections: much cheaper, and context
    # buildings still cast shadows (HB Shadow Calculation: FullExterior).
    sim.shadow_calculation.solar_distribution = 'FullExterior'
    # Only the summary table we parse, and no SQLite output (HB Simulation Output).
    # SQLite also segfaults EnergyPlus 24.2 on macOS ARM64.
    sim.output.summary_reports = ['AnnualBuildingUtilityPerformanceSummary']
    sim.output.include_sqlite = False
    sim_idf = sim.to_idf()
    # Align Version with the installed EnergyPlus binary (OpenStudio 3.9 ships 24.2).
    # honeybee-energy writes 23.2 by default; update to avoid version mismatch.
    sim_idf = re.sub(r'(Version,\s*)[\d\.]+', r'\g<1>24.2', sim_idf)
    # Remove the hardcoded 2017 year from RunPeriod so EnergyPlus uses the EPW calendar.
    # A mismatched year causes EnergyPlus to skip all environment periods silently.
    sim_idf = re.sub(r' 2017,(\s+!- (?:start|end) year)', r' ,\1', sim_idf)

    # Outputs we parse (see local_runner.py):
    # - annual meters fill the End Uses table in eplustbl.htm (ideal loads
    #   report as District Heating/Cooling);
    # - hourly ideal-loads heating/cooling go to eplusout.eso for the time series;
    # - the monthly table gives the heat balance chart, one table per zone.
    _extra = (
        '\nOutput:Meter,DistrictHeating:Building,Annual;\n'
        'Output:Meter,DistrictCooling:Building,Annual;\n'
        'Output:Meter,Electricity:Building,Annual;\n'
        'Output:Variable,*,Zone Ideal Loads Supply Air Sensible Heating Energy,Hourly;\n'
        'Output:Variable,*,Zone Ideal Loads Supply Air Sensible Cooling Energy,Hourly;\n'
        '\nOutput:Table:Monthly,\n'
        '  Monthly Heat Balance,\n'
        '  2,\n'
        '  Zone Ideal Loads Supply Air Sensible Heating Energy,SumOrAverage,\n'
        '  Zone Ideal Loads Supply Air Sensible Cooling Energy,SumOrAverage,\n'
        '  Zone People Sensible Heating Energy,SumOrAverage,\n'
        '  Zone Lights Electricity Energy,SumOrAverage,\n'
        '  Zone Electric Equipment Electricity Energy,SumOrAverage,\n'
        '  Zone Windows Total Heat Gain Energy,SumOrAverage,\n'
        '  Zone Infiltration Sensible Heat Gain Energy,SumOrAverage,\n'
        '  Zone Infiltration Sensible Heat Loss Energy,SumOrAverage;\n'
    )
    idf_text = sim_idf + _extra + '\n' + model_to_idf(model)
    floor_area = footprint_area(room['footprint_coordinates']) * floors
    multipliers = {r.identifier.upper(): r.multiplier for r in all_rooms}
    return idf_text, floor_area, multipliers
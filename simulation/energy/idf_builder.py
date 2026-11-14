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

from honeybee.altnumber import no_limit
from honeybee.boundarycondition import boundary_conditions
from honeybee.model import Model
from honeybee_energy.construction.opaque import OpaqueConstruction
from honeybee_energy.construction.window import WindowConstruction
from honeybee_energy.constructionset import ConstructionSet
from honeybee_energy.hvac.idealair import IdealAirSystem
from honeybee_energy.lib.programtypes import program_type_by_identifier
from honeybee_energy.material.glazing import EnergyWindowMaterialSimpleGlazSys
from honeybee_energy.material.opaque import EnergyMaterial
from honeybee_energy.schedule.ruleset import ScheduleRuleset
from honeybee_energy.simulation.parameter import SimulationParameter
from honeybee_energy.writer import model_to_idf

from ..geometry import (context_shades, footprint_area, make_adiabatic, shade_windows, shading_depths,
                        shared_edges, storey, window_ratios)


def _safe_id(name: str) -> str:
    """Convert an arbitrary string to a safe EnergyPlus identifier."""
    return re.sub(r'[^A-Za-z0-9_\-]', '_', name)[:100]


def _construction_set_from_epsm(constructions: dict) -> ConstructionSet:
    """Build a honeybee-energy ConstructionSet from EPSM construction records."""
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


# Monthly heat balance term -> EnergyPlus report variable (OUTPUTS asks for them, simulate.py reads them).
BALANCE = {
    'heating': 'Zone Ideal Loads Supply Air Sensible Heating Energy',
    'cooling': 'Zone Ideal Loads Supply Air Sensible Cooling Energy',
    'people': 'Zone People Sensible Heating Energy',
    'lighting': 'Zone Lights Electricity Energy',
    'equipment': 'Zone Electric Equipment Electricity Energy',
    'solar': 'Zone Windows Total Heat Gain Energy',
    'infiltration_gain': 'Zone Infiltration Sensible Heat Gain Energy',
    'infiltration_loss': 'Zone Infiltration Sensible Heat Loss Energy',
}
# Ideal loads report as district heating and cooling, under the Heating and Cooling end uses
# (district heating also serves hot water in some programs). Total Site Energy adds every fuel.
HEATING_METER, COOLING_METER = 'Heating:DistrictHeatingWater', 'Cooling:DistrictCooling'
SITE_METERS = ('Electricity:Facility', 'NaturalGas:Facility', 'DistrictHeatingWater:Facility',
               'DistrictCooling:Facility')


# What simulate.py reads: annual site meters, hourly ideal loads and the monthly heat balance.
OUTPUTS = '\n\n' + '\n'.join(
    [f'Output:Meter,{meter},RunPeriod;' for meter in (HEATING_METER, COOLING_METER, *SITE_METERS)]
    + [f'Output:Variable,*,{BALANCE[key]},Hourly;' for key in ('heating', 'cooling')]
    + [f'Output:Variable,*,{variable},Monthly;' for variable in BALANCE.values()]
) + '\n\n'


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
    floors = room['floors']
    groups: list[list[int]] = []
    previous = None
    for floor in range(1, floors + 1):
        key = (floor == 1, floor == floors, frozenset(shared_edges(room, floor)))
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
    floors = room['floors']
    overhang, fin = shading_depths(room)
    construction_set = _construction_set_from_epsm(constructions)

    all_rooms = []
    for group in floor_groups(room):
        first, last = group[0], group[-1]
        name = f'Floor_{first}' if first == last else f'Floors_{first}_{last}'
        r = storey(room, first, name)
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
        walls = [f for f in r.faces if str(f.type) == 'Wall']
        shared = shared_edges(room, first)  # same for every floor in the group
        for edge_index, (wall_face, ratio) in enumerate(zip(walls, window_ratios(room, walls))):
            if edge_index in shared:
                make_adiabatic(wall_face)
            elif ratio > 0:
                wall_face.apertures_by_ratio(ratio, 0.01)
                shade_windows(wall_face, overhang, fin)

        prog = program_type_by_identifier(PROGRAMS[building_program]).duplicate()
        # Natural ventilation: raise the cooling setpoint to 50 °C so the ideal
        # loads never cool mechanically. Heating is unaffected.
        if natural_ventilation and prog.setpoint:
            no_cool_sched = ScheduleRuleset.from_constant_value(
                f'NoCooling_{name}', 50.0, prog.setpoint.cooling_schedule.schedule_type_limit
            )
            new_setpoint = prog.setpoint.duplicate()
            new_setpoint.cooling_schedule = no_cool_sched
            prog.setpoint = new_setpoint

        r.properties.energy.program_type = prog
        r.properties.energy.construction_set = construction_set
        # A fresh ideal air system with no capacity limits, so EnergyPlus never
        # autosizes (autosize with sizing disabled crashes EP 24.2 on Linux).
        ideal_air = IdealAirSystem(f'{name}_IdealAir')
        ideal_air.economizer_type = 'NoEconomizer'
        ideal_air.heating_limit = no_limit
        ideal_air.cooling_limit = no_limit
        r.properties.energy.hvac = ideal_air
        all_rooms.append(r)

    model = Model('Energy_Model', rooms=all_rooms)

    for shade in context_shades(context_buildings):
        model.add_shade(shade)

    # ── Generate IDF ─────────────────────────────────────────────────────────
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
    # No summary tables and no SQLite: results come from the meter and variable
    # files (see simulate.py). SQLite also segfaults EnergyPlus 24.2 on macOS ARM64.
    sim.output.summary_reports = []
    sim.output.include_sqlite = False
    sim_idf = sim.to_idf()
    # Remove the hardcoded 2017 year from RunPeriod so EnergyPlus uses the EPW calendar.
    # A mismatched year causes EnergyPlus to skip all environment periods silently.
    sim_idf = re.sub(r' 2017,(\s+!- (?:start|end) year)', r' ,\1', sim_idf)
    idf_text = sim_idf + OUTPUTS + model_to_idf(model)
    floor_area = footprint_area(room['footprint_coordinates']) * floors
    multipliers = {r.identifier.upper(): r.multiplier for r in all_rooms}
    return idf_text, floor_area, multipliers
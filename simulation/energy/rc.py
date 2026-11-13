"""Run a Heath energy request through EPSM RC (ISO 52016-1) instead of EnergyPlus.

The model mirrors what idf_builder.py gives EnergyPlus, so /v1/energy-rc and
/v1/energy (and the Grasshopper definition) describe the same building:

- footprint, storeys, storey height and orientation;
- window ratio per facade (wwr, wwr_by_cardinal or wwr_by_edge), with no windows on
  shared walls;
- shared walls (shared_floors_by_edge) as adiabatic party walls, per storey;
- an overhang as deep as the wall plus any extra depth (the fins are not modelled);
- context buildings as neighbours that shade direct and diffuse sky radiation;
- the EPSM constructions for wall, roof and interior floors, Honeybee's generic
  ground slab, and simple glazing with SHGC 0.40;
- the Honeybee program's loads, schedules, setpoints, infiltration and outdoor air;
- an ideal air system without heat recovery, with cooling off under natural ventilation;
- EnergyPlus's default 18 °C ground boundary.

The result has the same keys and units as run_energy (simulate.py).
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from .idf_builder import PROGRAMS, footprint_area

SHGC = 0.40  # the backend's simple glazing system
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
CARDINALS = (('north', 0.0), ('east', 90.0), ('south', 180.0), ('west', 270.0))
# The 2017 calendar the backend's EnergyPlus run uses starts on a Sunday, like Core's 2023.
_FIRST_MONDAY = 1


@lru_cache
def _settings():
    from epsm_rc import RCSettings

    return RCSettings(
        airflow_policy='sensible_only_unlimited_supply',
        ground_policy='conductance_multiplier',
        # Neighbours also hide diffuse sky, as in EnergyPlus (an epsm_rc opt-in beyond ISO G.3).
        context_sky_diffuse='isotropic_horizon',
    )


@lru_cache
def _cache():
    """One preparation cache per worker process; each run still gets fresh thermal state."""
    from epsm_rc import PreparationCache

    return PreparationCache()


def _week(schedule) -> list[list[float]]:
    """Seven 24-hour profiles, Monday first, from the first full week of 2017."""
    if schedule is None:
        return [[1.0] * 24] * 7
    values = schedule.values()
    start = _FIRST_MONDAY * 24
    return [list(values[start + 24 * d:start + 24 * (d + 1)]) for d in range(7)]


@lru_cache
def _program(name: str) -> dict:
    """The Honeybee program the EnergyPlus model uses, as plain numbers and weekly profiles."""
    from honeybee_energy.lib.programtypes import program_type_by_identifier

    p = program_type_by_identifier(PROGRAMS[name])
    eq, vent = p.electric_equipment, p.ventilation
    return {
        'people_per_m2': p.people.people_per_area,
        'activity_w': _week(p.people.activity_schedule)[0][12],
        'people_radiant': p.people.radiant_fraction,
        'occupancy': _week(p.people.occupancy_schedule),
        'lighting_w_m2': p.lighting.watts_per_area,
        'lighting': _week(p.lighting.schedule),
        'lighting_fractions': (p.lighting.radiant_fraction, p.lighting.visible_fraction,
                               p.lighting.return_air_fraction),
        'equipment_w_m2': (eq.watts_per_area if eq else 0.0)
                          + (p.gas_equipment.watts_per_area if p.gas_equipment else 0.0),
        'equipment': _week(eq.schedule if eq else None),
        'equipment_fractions': ((eq.radiant_fraction, eq.latent_fraction, eq.lost_fraction)
                                if eq else (0.5, 0.0, 0.0)),
        'heating_setpoint': _week(p.setpoint.heating_schedule),
        'cooling_setpoint': _week(p.setpoint.cooling_schedule),
        'infiltration_per_exterior_m2': p.infiltration.flow_per_exterior_area,
        'infiltration': _week(p.infiltration.schedule),
        # Ideal loads outdoor air: per person (design occupancy) plus per area, always on.
        'outdoor_air_m3_s_m2': (vent.flow_per_person * p.people.people_per_area
                                + vent.flow_per_area) if vent else 0.0,
        'outdoor_air_ach': vent.air_changes_per_hour if vent else 0.0,
        'outdoor_air': _week(vent.schedule if vent else None),
    }


@lru_cache
def _ground_slab() -> tuple:
    """Honeybee's generic ground slab, which the backend leaves on ground floors."""
    from honeybee_energy.lib.constructionsets import generic_construction_set

    return tuple((m.identifier, m.thickness, m.conductivity, m.density, m.specific_heat)
                 for m in generic_construction_set.floor_set.ground_construction.materials)


def _sensible_fraction(activity_w: float, temperature_c: float = 22.0) -> float:
    """EnergyPlus's people sensible-heat polynomial (Engineering Reference)."""
    m, t = activity_w, temperature_c
    s = (6.461927 + 0.946892 * m + 0.0000255737 * m ** 2 + 7.139322 * t - 0.0627909 * t * m
         + 0.0000589172 * t * m ** 2 - 0.198550 * t ** 2 + 0.000940018 * t ** 2 * m
         - 0.00000149532 * t ** 2 * m ** 2)
    return max(0.0, min(1.0, s / m))


def _schedule(profiles):
    from epsm_core.specs.schedules import ScheduleSpec

    return ScheduleSpec(values=tuple(profiles[0]), weekday_values=tuple(tuple(d) for d in profiles))


def _changed(obj, **fields):
    """Validated copy with some fields replaced."""
    if hasattr(obj, 'with_changes'):
        return obj.with_changes(**fields)
    return obj.updated(**fields)


def _element(name, role, layers):
    """A Data Element from (name, thickness, conductivity, density, specific heat) layers, outside first."""
    from epsm_data import Component, Element, Layer, LayerMaterial, LayerSlot, OpaqueMaterial, Roughness, ShearingLayer

    slots = []
    for i, (material, thickness, conductivity, density, specific_heat) in enumerate(layers):
        m = OpaqueMaterial(
            name=material, json_id=f'heath.{material}', boverket_id=None, material_type='heath',
            roughness=Roughness.MEDIUM_ROUGH, density=float(density), specific_heat=float(specific_heat),
            thermal_absorptance=0.9, solar_absorptance=0.7, visible_absorptance=0.7,
            conductivity=float(conductivity))
        layer = Layer(name=f'{name}.{i}', layer_code=f'{name}.{i}', thickness=float(thickness),
                      materials=(LayerMaterial(m, 1.0, float(thickness)),))
        slots.append(LayerSlot(layer=layer, position=i))
    component = Component(name=name, archetype='heath', application=role, layers=tuple(slots),
                          shearing_layer=ShearingLayer.STRUCTURE)
    return Element(name=name, archetype='heath', age=None, components=(component,), application=role)


def _layers(record: dict) -> list[tuple]:
    """EPSM catalogue layers (outside first), with the backend's fallbacks for gaps."""
    return [(l['material_name'], l.get('thickness_m', 0.1), l.get('conductivity_w_mk', 0.8),
             l.get('density_kg_m3', l.get('density', 1800.0)),
             l.get('specific_heat_j_kgk', l.get('specific_heat', 900.0)))
            for l in record.get('layers', []) if not l.get('is_glazing_layer')]


def _construction_set(constructions: dict, reference):
    from epsm_data import BuildingElement as E, ConstructionSet, WindowElement

    elements = dict(reference.construction_set.elements)
    elements[E.EXT_WALL] = _element('wall', E.EXT_WALL, _layers(constructions['wall']))
    for role in (E.ROOF, E.TOP_FLOOR):
        elements[role] = _element('roof', role, _layers(constructions['roof']))
    elements[E.GROUND_FLOOR] = _element('ground', E.GROUND_FLOOR, _ground_slab())
    elements[E.INTERMEDIATE_FLOOR] = _element('floor', E.INTERMEDIATE_FLOOR, _layers(constructions['floor']))
    elements[E.WINDOW] = WindowElement(
        name='window', archetype='heath', u_value=float(constructions['window'].get('u_value_w_m2k', 2.0)),
        shgc=SHGC)
    return ConstructionSet(elements=elements)


def _cardinal(azimuth_deg: float) -> str:
    return min(CARDINALS, key=lambda c: min((azimuth_deg - c[1]) % 360, (c[1] - azimuth_deg) % 360))[0]


def _facades(room: dict, geometry) -> tuple[list, list]:
    """Per-facade window ratios and party walls, following idf_builder's per-wall rules."""
    from epsm_core.geometry import facade_catalog
    from epsm_core.specs.facade import FacadeOverride
    from epsm_core.specs.geometry import PartyWall

    facades = {f.facade_id: f for f in facade_catalog(geometry)}
    edges = sorted(facades, key=lambda fid: int(fid.rsplit('e', 1)[1]))
    edge_wwr = room.get('wwr_by_edge')
    wwr_map = room.get('wwr_by_cardinal') or {c: room.get('wwr', 0.4) for c, _ in CARDINALS}
    shared = room.get('shared_floors_by_edge') or []
    floors, height = int(room.get('floors', 1)), float(room['floor_to_floor_height'])
    overrides, party_walls = [], []
    for k, fid in enumerate(edges):
        if edge_wwr is not None and len(edge_wwr) == len(edges):
            ratio = float(edge_wwr[k])
        else:
            ratio = float(wwr_map.get(_cardinal(facades[fid].azimuth_deg), 0.0))
        overrides.append(FacadeOverride(facade_id=fid, wwr=min(ratio, 0.94)))
        storeys = sorted({f for f in (shared[k] if k < len(shared) else []) if 1 <= f <= floors})
        for f in storeys:
            overrides.append(FacadeOverride(facade_id=fid, storey=f - 1, wwr=0.0))
        # One party wall per run of consecutive shared storeys.
        runs = []
        for f in storeys:
            if runs and f == runs[-1][1] + 1:
                runs[-1][1] = f
            else:
                runs.append([f, f])
        for first, last in runs:
            party_walls.append(PartyWall(
                facade_id=fid, neighbor_id=f'heath_shared_edge_{k}', neighbor_conditioning='heated',
                contact_confirmed=True, assumption_source='Heath shared_floors_by_edge (adiabatic, as the backend)',
                bottom_m=(first - 1) * height, top_m=last * height))
    return overrides, party_walls


def _context(buildings: list[dict], orientation_offset: float):
    """Heath context buildings as Core context, in the building's unrotated frame.

    The backend rotates the room by orientation_offset (counter-clockwise, about the
    origin) and leaves neighbours where Heath put them. Core rotates the building and
    its context together, so neighbours are given here rotated back by the same angle.
    """
    from epsm_core import Footprint
    from epsm_core.specs import ContextBuilding, ContextSpec

    a = math.radians(-orientation_offset)
    c, s = math.cos(a), math.sin(a)
    out = []
    for i, b in enumerate(buildings or []):
        ring = [tuple(p) for p in b['footprint_coordinates']]
        if ring[0] == ring[-1]:
            ring.pop()
        out.append(ContextBuilding(
            identifier=f'context_{i}',
            footprints=(Footprint(exterior=tuple((x * c - y * s, x * s + y * c) for x, y in ring)),),
            height_m=float(b['floors']) * float(b['floor_to_floor_height']),
            base_elevation_m=0.0))
    return ContextSpec(buildings=tuple(out))


def build_building(room: dict, context_buildings: list[dict], constructions: dict, building_program: str,
                   natural_ventilation: bool):
    """The EPSM Core Building for one Heath request."""
    from epsm_core import Building, Footprint
    from epsm_core.specs.facade import OverhangSpec
    from epsm_core.specs.physics import FoundationSpec, GroundTemperatureSpec

    program = _program(building_program)
    coords = [tuple(p) for p in room['footprint_coordinates']]
    if coords[0] == coords[-1]:
        coords.pop()
    offset = float(room.get('orientation_offset', 0.0))
    # Heath (ladybug rotate_xy) turns the plan counter-clockwise; Core's orientation is clockwise.
    orientation = (-offset) % 360.0
    floors, height = int(room.get('floors', 1)), float(room['floor_to_floor_height'])
    reference = Building(preset='illustrative-reference')

    area = footprint_area(room['footprint_coordinates'])
    perimeter = sum(math.dist(coords[i], coords[i - 1]) for i in range(len(coords)))
    exterior = perimeter * height * floors + area  # walls (with windows) plus roof, as Honeybee counts it
    volume = area * height * floors
    op = reference.operation
    radiant_l, visible_l, return_l = program['lighting_fractions']
    radiant_e, latent_e, lost_e = program['equipment_fractions']
    operation = _changed(
        op,
        occupancy_category='residential' if building_program == 'Residential' else 'non_residential',
        occupant_density_per_m2=program['people_per_m2'],
        occupant_activity_w=program['activity_w'],
        occupancy_fraction=_schedule(program['occupancy']),
        equipment_gain_w_m2=program['equipment_w_m2'],
        equipment_fraction=_schedule(program['equipment']),
        equipment_heat_fraction=1.0 - latent_e - lost_e,
        lighting_gain_w_m2=program['lighting_w_m2'],
        lighting_fraction=_schedule(program['lighting']),
        heating_setpoint=_schedule(program['heating_setpoint']),
        cooling_setpoint=_schedule(program['cooling_setpoint']),
        cooling_enabled=not natural_ventilation,
        dhw_kwh_m2_year=0.0,
        gain_partitions=_changed(
            op.gain_partitions, policy='explicit',
            people_fraction_radiant=program['people_radiant'],
            people_sensible_heat_fraction=_sensible_fraction(program['activity_w']),
            equipment_fraction_radiant=radiant_e, equipment_fraction_latent=latent_e,
            lighting_fraction_radiant=radiant_l, lighting_fraction_visible=visible_l,
            lighting_return_air_fraction=return_l),
    )
    airflow = _changed(
        reference.hvac.airflow,
        topology='FTX',  # balanced supply carries the ideal loads' outdoor air; no recovery
        envelope_infiltration_ach=program['infiltration_per_exterior_m2'] * exterior * 3600 / volume,
        infiltration_fraction=_schedule(program['infiltration']),
        mechanical_vent_l_s_m2=program['outdoor_air_m3_s_m2'] * 1000 + program['outdoor_air_ach'] * height / 3.6,
        mechanical_vent_fraction=_schedule(program['outdoor_air']),
        sensible_recovery_effectiveness=0.0,
        latent_recovery_effectiveness=0.0,
    )
    overhang = float(room.get('wall_thickness', 0.30)) + float(room.get('additional_horizontal_shading_depth', 0.0))
    common = dict(
        storeys=floors, storey_height_m=height, orientation_deg=orientation, identifier='heath',
        construction_set=_construction_set(constructions, reference),
        hvac=reference.hvac.with_changes(airflow=airflow), operation=operation,
        foundation=FoundationSpec(method='prescribed_temperature'),
        ground_temperature=GroundTemperatureSpec(monthly_temperatures_c=(18.0,) * 12,
                                                 source='energyplus_24_2_default_18C_uncalibrated'),
        thermal_bridge_uplift_fraction=0.0, load_reference_area_policy='conditioned_floor_area',
        context=_context(context_buildings, offset),
    )
    footprint = Footprint(exterior=tuple(coords))
    plain = Building.from_footprint(footprint, **common)
    overrides, party_walls = _facades(room, plain.geometry)
    facade = _changed(reference.facade, default_wwr=float(room.get('wwr', 0.4) or 0.0),
                      overrides=tuple(overrides), overhang=OverhangSpec(depth_m=overhang))
    return Building.from_footprint(footprint, facade=facade, party_walls=tuple(party_walls), **common)


def run_energy_rc(room: dict, context_buildings: list[dict], constructions: dict, building_program: str,
                  natural_ventilation: bool, epw_path: str) -> dict:
    """Same keys and units as simulate.run_energy (kWh, kWh/m2, monthly kWh/m2)."""
    from epsm_core import Weather
    from epsm_rc import simulate_building

    building = build_building(room, context_buildings, constructions, building_program, natural_ventilation)
    weather = Weather.from_epw(epw_path)
    result = simulate_building(building, weather=weather, settings=_settings(), preparation_cache=_cache())
    hourly = {k: np.asarray(v, dtype=float) for k, v in result.hourly.items()}
    year = int(result.time_axis[0].astype('datetime64[Y]').astype(int) + 1970)
    month = result.time_axis.astype('datetime64[M]').astype(int) % 12
    op, airflow = building.operation, building.hvac.airflow

    def sched(spec):
        return np.asarray(spec.hourly(year), dtype=float)

    floors, height = int(room.get('floors', 1)), float(room['floor_to_floor_height'])
    area = footprint_area(room['footprint_coordinates']) * floors
    volume = area * height
    windows = [e for e in building.elements.values() if 'WINDOW' in str(e.role)] \
        if isinstance(building.elements, dict) else [e for e in building.elements if 'WINDOW' in str(e.role)]
    window_ua = sum(e.area_m2 * float(e.construction.u_value) for e in windows if e.boundary == 'Outdoors')

    outdoor = np.asarray(_outdoor_temperature(epw_path))[:month.size]
    delta = outdoor - hourly['indoor_temperature_c']
    people = area * op.occupant_density_per_m2 * op.occupant_activity_w * sched(op.occupancy_fraction) \
        * op.gain_partitions.people_sensible_heat_fraction / 1000
    lighting = area * op.lighting_gain_w_m2 * sched(op.lighting_fraction) / 1000
    equipment = area * op.equipment_gain_w_m2 * sched(op.equipment_fraction) / 1000
    # 'solar' in the backend is EnergyPlus's Zone Windows Total Heat Gain: solar plus window
    # conduction, counted only in hours when the net is a gain.
    solar = np.clip(hourly['window_solar_heat_gain_w'] + window_ua * delta, 0, None) / 1000
    infiltration = airflow.envelope_infiltration_ach * sched(airflow.infiltration_fraction) * volume / 3600 \
        * 1200 * delta / 1000
    terms = {
        'heating': hourly['space_heating_demand_kwh'], 'cooling': hourly['cooling_demand_kwh'],
        'people': people, 'lighting': lighting, 'equipment': equipment, 'solar': solar,
        'infiltration_gain': np.clip(infiltration, 0, None), 'infiltration_loss': np.clip(-infiltration, 0, None),
    }
    balance = {'months': MONTHS}
    for key, values in terms.items():
        balance[key] = [round(float(values[month == m].sum() / area), 4) for m in range(12)]
    heating, cooling = float(terms['heating'].sum()), float(terms['cooling'].sum())
    # EnergyPlus's Total Site Energy: ideal-loads heating and cooling plus lighting and equipment.
    total = heating + cooling + float(lighting.sum() + equipment.sum())
    return {
        'floor_area_m2': round(area, 2),
        'heating_energy_kwh': round(heating, 1),
        'cooling_energy_kwh': round(cooling, 1),
        'total_energy_kwh': round(total, 1),
        'heating_demand_kwh_m2': round(heating / area, 3),
        'cooling_demand_kwh_m2': round(cooling / area, 3),
        'total_energy_kwh_m2': round(total / area, 3),
        'monthly_heat_balance': balance,
        'heating_hourly_kwh': terms['heating'].tolist(),
        'cooling_hourly_kwh': terms['cooling'].tolist(),
    }


@lru_cache(maxsize=8)
def _outdoor_temperature(epw_path: str) -> tuple:
    """Hourly dry-bulb temperature from the EPW (column 7)."""
    with open(epw_path, encoding='utf-8', errors='replace') as stream:
        lines = stream.read().splitlines()[8:]
    return tuple(float(line.split(',')[6]) for line in lines if line.strip())

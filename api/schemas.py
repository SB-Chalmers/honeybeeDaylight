"""
api.schemas
───────────
Request and response models for every /v1 endpoint.

The request is a compact building description; the server rebuilds the
Honeybee model from it. Large result arrays are returned packed (base64
typed arrays, see ``worker.packing``) unless the client asks for plain JSON.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Union

from ladybug_geometry.geometry2d import Point2D, Polygon2D
from pydantic import BaseModel, Field, field_validator, model_validator

from simulation.geometry import footprint, signed_area

MIN_FLOOR_AREA = 1.0  # m²; smaller footprints are slider accidents, not rooms


# ── Building description ─────────────────────────────────────────────────────

def _check_footprint(v: list[list[float]]) -> list[list[float]]:
    """A footprint needs at least 3 [x, y] points."""
    if len(v) < 3:
        raise ValueError('footprint_coordinates must have at least 3 points.')
    for point in v:
        if len(point) != 2:
            raise ValueError('Each footprint coordinate must be a 2-D pair: [x, y].')
    return v


def _check_shape(points: list[list[float]]) -> None:
    """Reject a footprint Honeybee and EnergyPlus would fail on later with an unclear error.

    That is one with (almost) no area, such as a zero-width rectangle or points
    on one line, or one whose edges cross (a bow tie). Repeated points are ignored.
    """
    ring = []
    for x, y in footprint(points):
        if not ring or (x, y) != ring[-1]:
            ring.append((x, y))
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    polygon = Polygon2D([Point2D(x, y) for x, y in ring]) if len(ring) >= 3 else None
    if polygon and polygon.is_self_intersecting:
        raise ValueError('footprint_coordinates edges cross each other; list the corners in order around the room.')
    area = polygon.area if polygon else 0.0
    if area < MIN_FLOOR_AREA:
        raise ValueError(f'footprint_coordinates enclose {area:.2f} m²; a room needs at least '
                         f'{MIN_FLOOR_AREA:g} m². Check for zero width or points on one line.')


class SensorGridConfig(BaseModel):
    """Sensor spacing (m) and height above the floor (m)."""
    x_dim: float = Field(0.5, gt=0)
    y_dim: float = Field(0.5, gt=0)
    offset: float = 0.75


DIRECTIONS = ('north', 'east', 'south', 'west')


class WwrByCardinal(BaseModel):
    """Window-to-wall ratio for walls facing each direction."""
    north: float = Field(0.4, ge=0, le=1)
    east:  float = Field(0.3, ge=0, le=1)
    south: float = Field(0.25, ge=0, le=1)
    west:  float = Field(0.2, ge=0, le=1)


class RoomInput(BaseModel):
    """The building being studied, as the editor describes it."""
    footprint_coordinates: list[list[float]] = Field(
        ..., min_length=3,
        description='2-D polygon [[x,y], ...] in metres; auto-closed.'
    )
    orientation_offset: float = Field(
        0.0,
        description='Clockwise rotation from north (+Y). 0 = north-aligned.'
    )
    floor_to_floor_height: float = Field(3.2, gt=0)
    floors: int = Field(1, ge=1)
    selected_floor_number: int = Field(1, ge=1)
    simulate_all_floors: bool = Field(
        False,
        description='Simulate every floor instead of only selected_floor_number. '
                    'Each floor adds a room and a sensor grid, so the job takes '
                    'proportionally longer.',
    )

    # Accept either single float (fans out) or per-cardinal dict.
    wwr: Optional[float] = Field(
        None, ge=0, le=1,
        description='Single WWR applied to all four walls. Overridden by wwr_by_cardinal.'
    )
    wwr_by_cardinal: Optional[WwrByCardinal] = None
    wwr_by_edge: Optional[list[float]] = Field(
        None,
        description='Optional per-edge WWR list aligned to footprint edge order [p0->p1, p1->p2, ...].'
    )
    shared_floors_by_edge: Optional[list[list[int]]] = Field(
        None,
        description='Optional per-edge list of floors (1-based), aligned to footprint edge order, whose wall '
                    'is shared with a neighbouring building. Shared walls are adiabatic and get no windows.'
    )

    wall_thickness: float = Field(0.3, ge=0)
    additional_horizontal_shading_depth: float = Field(0.0, ge=0)
    additional_vertical_shading_depth: float = Field(0.0, ge=0)
    window_width: float = Field(1.2, gt=0)
    window_height: float = Field(1.5, gt=0)
    window_spacing: float = Field(
        0.0, ge=0,
        description='Gap between the two rows a window is split into (Honeybee vertical_separation); '
                    'it also widens the window module to window_width + window_spacing. '
                    'Heath sends 0: one pane per window.',
    )
    sensor_grid: SensorGridConfig = Field(default_factory=SensorGridConfig)

    @field_validator('footprint_coordinates')
    @classmethod
    def _footprint(cls, v):
        return _check_footprint(v)

    @model_validator(mode='after')
    def _check_and_orient(self):
        """Check what single fields cannot, then store the footprint counter-clockwise.

        Honeybee needs a counter-clockwise footprint to extrude it; a clockwise one
        gives an inside-out room. The per-edge lists follow their edges: in the
        reversed list, edge k is the old edge n-2-k.
        """
        _check_shape(self.footprint_coordinates)
        points = footprint(self.footprint_coordinates)
        n = len(points)
        if self.wwr_by_edge is not None:
            if len(self.wwr_by_edge) != n:
                raise ValueError(f'wwr_by_edge must contain exactly {n} values to match footprint edges.')
            if not all(0 <= ratio <= 1 for ratio in self.wwr_by_edge):
                raise ValueError('Each wwr_by_edge value must be in [0, 1].')
        if self.shared_floors_by_edge is not None:
            if len(self.shared_floors_by_edge) != n:
                raise ValueError(f'shared_floors_by_edge must contain exactly {n} entries to match footprint edges.')
            for i, floors in enumerate(self.shared_floors_by_edge):
                for floor in floors:
                    if not 1 <= floor <= self.floors:
                        raise ValueError(f'shared_floors_by_edge[{i}] contains floor {floor}, '
                                         'which the building does not have (floors are 1-based).')
        if self.sensor_grid.offset >= self.floor_to_floor_height:
            raise ValueError('sensor_grid offset must be below the ceiling (less than floor_to_floor_height).')

        if signed_area(points) < 0:
            self.footprint_coordinates = [list(p) for p in points[::-1]]
            for name in ('wwr_by_edge', 'shared_floors_by_edge'):
                edges = getattr(self, name)
                if edges is not None:
                    setattr(self, name, [edges[(n - 2 - k) % n] for k in range(n)])
        return self

    def to_simulation_dict(self) -> dict:
        """Every field filled in, with the window ratio always as wwr_by_cardinal: what the simulations read."""
        d = self.model_dump(exclude={'wwr'}, exclude_none=True)
        if self.wwr_by_cardinal is None:
            ratios = WwrByCardinal() if self.wwr is None else WwrByCardinal(**dict.fromkeys(DIRECTIONS, self.wwr))
            d['wwr_by_cardinal'] = ratios.model_dump()
        return d


class ContextBuildingInput(BaseModel):
    """A neighbouring building; only casts shadows."""
    footprint_coordinates: list[list[float]] = Field(
        ..., min_length=3,
        description='2-D polygon [[x,y], ...] in metres; auto-closed.'
    )
    floors: int = Field(1, ge=1)
    floor_to_floor_height: float = Field(3.2, gt=0)

    @field_validator('footprint_coordinates')
    @classmethod
    def _footprint(cls, v):
        return _check_footprint(v)


# ── Requests ─────────────────────────────────────────────────────────────────

Program = Literal['Office', 'Residential', 'Retail', 'School', 'Hospital', 'Industrial']


class DaylightOptions(BaseModel):
    """What the daylight job computes."""
    run_sda: bool = Field(False, description='Also run annual daylight and return DA / sDA.')
    da_lux: int = Field(300, gt=0)
    sda_target_pct: float = Field(50.0, ge=0, le=100)
    quality: Literal['preview', 'final'] = 'preview'


class ConstructionNames(BaseModel):
    """Construction names as listed by the EPSM service."""
    wall: str = Field(..., min_length=1)
    floor: str = Field(..., min_length=1)
    roof: str = Field(..., min_length=1)
    window: str = Field(..., min_length=1)


class EnergyOptions(BaseModel):
    """What the energy job needs."""
    constructions: ConstructionNames = Field(description='EPSM construction names.')
    building_program: Program = 'Office'
    natural_ventilation: bool = False


class _Base(BaseModel):
    """Fields shared by every request."""
    room: RoomInput
    context_buildings: list[ContextBuildingInput] = Field(default_factory=list)
    epw: Optional[str] = Field(
        None,
        description='EPW file contents. Omit to use the server default weather file.',
    )
    pack: bool = Field(
        True,
        description='Return large arrays as base64 typed arrays (see README). '
                    'false returns plain JSON number lists.',
    )


class DaylightRequest(_Base):
    """Body of POST /v1/daylight."""
    daylight: DaylightOptions = Field(default_factory=DaylightOptions)


class EnergyRequest(_Base):
    """Body of POST /v1/energy."""
    energy: EnergyOptions


class SimulateRequest(_Base):
    """Body of POST /v1/simulate."""
    daylight: DaylightOptions = Field(default_factory=DaylightOptions)
    energy: EnergyOptions


class GrasshopperRequest(_Base):
    """Body of POST /v1/grasshopper: a /v1/simulate body, energy optional."""
    daylight: DaylightOptions = Field(default_factory=DaylightOptions)
    energy: Optional[EnergyOptions] = None


# ── Responses ────────────────────────────────────────────────────────────────

class PackedArray(BaseModel):
    """A base64-encoded little-endian typed array. value = raw / scale."""
    dtype: str          # 'uint16' | 'float32'
    scale: float = 1.0
    shape: list[int]
    data: str


ArrayField = Union[PackedArray, list]


class SensorGridInfo(BaseModel):
    """Where one floor's sensors sit in the flat result arrays."""
    identifier: str
    full_identifier: str
    room_identifier: str = ''
    floor_number: Optional[int] = None
    start_sensor_index: int
    sensor_count: int


class DfSummary(BaseModel):
    """Daylight factor statistics (%)."""
    mean_df: float
    min_df: float
    max_df: float


class SdaSummary(BaseModel):
    """Spatial daylight autonomy: % of sensors meeting DA >= target."""
    sda: float
    da_lux: int
    sda_target_pct: float
    sensor_count: int
    passing_sensors: int


class DaylightResult(BaseModel):
    """Response of POST /v1/daylight."""
    weather: str
    seconds: float
    worker: Optional[str] = Field(None, description='Worker slot that ran the job (host:slot).')
    started_at: Optional[float] = Field(None, description='Unix time the worker started the job.')
    sensor_count: int
    sensor_grids: list[SensorGridInfo] = Field(
        description='One slice per floor into the flat sensor arrays.'
    )
    sensor_points: ArrayField = Field(description='[x, y, z] per sensor, shape (n, 3).')
    df_summary: DfSummary
    df: ArrayField = Field(description='Daylight factor (%) per sensor.')
    sda_summary: Optional[SdaSummary] = None
    da: Optional[ArrayField] = Field(
        None, description='Daylight autonomy (%) per sensor. A sensor passes sDA when da >= sda_target_pct.'
    )


class EnergyResult(BaseModel):
    """Response of POST /v1/energy (kWh, and kWh/m2 of floor area)."""
    weather: str
    seconds: float
    worker: Optional[str] = Field(None, description='Worker slot that ran the job (host:slot).')
    started_at: Optional[float] = Field(None, description='Unix time the worker started the job.')
    floor_area_m2: float
    heating_energy_kwh: float
    cooling_energy_kwh: float
    total_energy_kwh: float
    heating_demand_kwh_m2: float
    cooling_demand_kwh_m2: float
    total_energy_kwh_m2: float
    monthly_heat_balance: Optional[Dict[str, Any]] = None
    heating_hourly_kwh: Optional[ArrayField] = Field(None, description='8760 hourly values, kWh.')
    cooling_hourly_kwh: Optional[ArrayField] = Field(None, description='8760 hourly values, kWh.')


class SimulateResult(BaseModel):
    """Response of POST /v1/simulate."""
    daylight: DaylightResult
    energy: EnergyResult


class ErrorResponse(BaseModel):
    """Every error looks like this."""
    error: str
    message: str

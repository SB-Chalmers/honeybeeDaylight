"""
api.schemas
───────────
Request and response models for /v1/daylight, /v1/energy and /v1/simulate.

The request is a compact building description; the server rebuilds the
Honeybee model from it. Large result arrays are returned packed (base64
typed arrays, see ``api.packing``) unless the client asks for plain JSON.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ── Building description ─────────────────────────────────────────────────────

def _check_footprint(v: list[list[float]]) -> list[list[float]]:
    if len(v) < 3:
        raise ValueError('footprint_coordinates must have at least 3 points.')
    for point in v:
        if len(point) != 2:
            raise ValueError('Each footprint coordinate must be a 2-D pair: [x, y].')
    return v


class SensorGridConfig(BaseModel):
    x_dim: float = 0.3
    y_dim: float = 0.3
    offset: float = 0.75


class WwrByCardinal(BaseModel):
    north: float = Field(0.4, ge=0, le=1)
    east:  float = Field(0.3, ge=0, le=1)
    south: float = Field(0.25, ge=0, le=1)
    west:  float = Field(0.2, ge=0, le=1)


class RoomInput(BaseModel):
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
        True,
        description='Generate and sensor one Honeybee Room per building floor.',
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

    wall_thickness: float = Field(0.3, ge=0)
    additional_horizontal_shading_depth: float = Field(0.0, ge=0)
    additional_vertical_shading_depth: float = Field(0.0, ge=0)
    window_width: float = Field(1.2, gt=0)
    window_height: float = Field(1.5, gt=0)
    window_spacing: float = Field(0.3, ge=0)
    sensor_grid: SensorGridConfig = Field(default_factory=SensorGridConfig)

    @field_validator('footprint_coordinates')
    @classmethod
    def _footprint(cls, v):
        return _check_footprint(v)

    @field_validator('wwr_by_edge')
    @classmethod
    def validate_wwr_by_edge(cls, v):
        if v is None:
            return v
        if len(v) < 3:
            raise ValueError('wwr_by_edge must include at least 3 values.')
        for ratio in v:
            if ratio < 0 or ratio > 1:
                raise ValueError('Each wwr_by_edge value must be in [0, 1].')
        return v

    def to_simulation_dict(self) -> dict:
        """Convert to the flat dict expected by simulation.inputs.load_room_inputs."""
        d = self.model_dump(exclude={'wwr', 'wwr_by_cardinal', 'wwr_by_edge'})
        d['sensor_grid'] = self.sensor_grid.model_dump()
        if self.wwr_by_edge is not None:
            d['wwr_by_edge'] = self.wwr_by_edge
        if self.wwr_by_cardinal:
            d['wwr_by_cardinal'] = self.wwr_by_cardinal.model_dump()
        elif self.wwr is not None:
            d['wwr'] = self.wwr
        else:
            d['wwr_by_cardinal'] = WwrByCardinal().model_dump()
        return d


class ContextBuildingInput(BaseModel):
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
    run_sda: bool = Field(False, description='Also run annual daylight and return DA / sDA.')
    da_lux: int = Field(300, gt=0)
    sda_target_pct: float = Field(50.0, ge=0, le=100)
    quality: Literal['preview', 'final'] = 'final'


class ConstructionNames(BaseModel):
    wall: str = Field(..., min_length=1)
    floor: str = Field(..., min_length=1)
    roof: str = Field(..., min_length=1)
    window: str = Field(..., min_length=1)


class EnergyOptions(BaseModel):
    constructions: ConstructionNames = Field(description='EPSM construction names.')
    building_program: Program = 'Office'
    natural_ventilation: bool = False


class _Base(BaseModel):
    room: RoomInput
    context_buildings: list[ContextBuildingInput] = Field(default_factory=list)
    epw: Optional[str] = Field(
        None,
        description='EPW file contents. Omit to use the server default weather file. '
                    'Send the request with Content-Encoding: gzip to keep it small.',
    )
    pack: bool = Field(
        True,
        description='Return large arrays as base64 typed arrays (see README). '
                    'false returns plain JSON number lists.',
    )


class DaylightRequest(_Base):
    daylight: DaylightOptions = Field(default_factory=DaylightOptions)


class EnergyRequest(_Base):
    energy: EnergyOptions


class SimulateRequest(_Base):
    daylight: DaylightOptions = Field(default_factory=DaylightOptions)
    energy: EnergyOptions


# ── Responses ────────────────────────────────────────────────────────────────

class PackedArray(BaseModel):
    """A base64-encoded little-endian typed array. value = raw / scale."""
    dtype: str          # 'uint16' | 'float32'
    scale: float = 1.0
    shape: list[int]
    data: str


ArrayField = Union[PackedArray, list]


class SensorGridInfo(BaseModel):
    identifier: str
    full_identifier: str
    room_identifier: str = ''
    floor_number: Optional[int] = None
    start_sensor_index: int
    sensor_count: int


class DfSummary(BaseModel):
    mean_df: float
    min_df: float
    max_df: float


class SdaSummary(BaseModel):
    sda: float
    da_lux: int
    sda_target_pct: float
    sensor_count: int
    passing_sensors: int


class DaylightResult(BaseModel):
    weather: str
    seconds: float
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
    weather: str
    seconds: float
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
    daylight: DaylightResult
    energy: EnergyResult


class ErrorResponse(BaseModel):
    error: str
    message: str

"""
api.schemas
───────────
Pydantic request and response models for all endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, field_validator


# ── Room input ────────────────────────────────────────────────────────────────

class SensorGridConfig(BaseModel):
    x_dim: float = 0.5
    y_dim: float = 0.5
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
    def at_least_three_points(cls, v):
        if len(v) < 3:
            raise ValueError('footprint_coordinates must have at least 3 points.')
        for point in v:
            if len(point) != 2:
                raise ValueError('Each footprint coordinate must be a 2-D pair: [x, y].')
        return v

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
    def at_least_three_points(cls, v):
        if len(v) < 3:
            raise ValueError('footprint_coordinates must have at least 3 points.')
        for point in v:
            if len(point) != 2:
                raise ValueError('Each footprint coordinate must be a 2-D pair: [x, y].')
        return v


# ── Study request ─────────────────────────────────────────────────────────────

class Thresholds(BaseModel):
    da_lux: int = 300
    sda_target_pct: float = 50.0


class StudyRequest(BaseModel):
    room: RoomInput
    context_buildings: list[ContextBuildingInput] = Field(
        default_factory=list,
        description='Other buildings represented as opaque context geometry for this study.'
    )
    run_sda: bool = False
    location: Optional[str] = Field(
        None,
        description='Server-side EPW location name. Required when run_sda=true.'
    )
    quality: str = Field('draft', pattern='^(draft|full)$')
    thresholds: Thresholds = Field(default_factory=Thresholds)


class ConstructionNames(BaseModel):
    wall: str = Field(..., min_length=1)
    floor: str = Field(..., min_length=1)
    roof: str = Field(..., min_length=1)
    window: str = Field(..., min_length=1)


class EnergyRequest(BaseModel):
    room: RoomInput
    context_buildings: list[ContextBuildingInput] = Field(default_factory=list)
    constructions: ConstructionNames
    building_program: str = 'Office'
    hvac_system: str = 'Default HVAC'
    natural_ventilation: bool = False
    location: str = Field(..., min_length=1)


# ── Responses ─────────────────────────────────────────────────────────────────

class StudyLinks(BaseModel):
    status: str
    result: str


class StudyQueued(BaseModel):
    study_id: str
    status: str
    sensor_count: int
    links: StudyLinks


class StudyStatus(BaseModel):
    study_id: str
    status: str                       # queued | running | complete | failed
    stage: Optional[str] = None
    error: Optional[str] = None


class DfSummary(BaseModel):
    mean_df: float
    min_df: float
    max_df: float


class DfResult(BaseModel):
    summary: DfSummary
    values: list[float]


class SdaSummary(BaseModel):
    sda_300_50: float
    sensor_count: int
    passing_sensors: int


class SdaResult(BaseModel):
    summary: SdaSummary
    values: list[float]
    pass_: list[bool] = Field(alias='pass')

    model_config = {'populate_by_name': True}


class StudyResult(BaseModel):
    study_id: str
    df: DfResult
    sda: Optional[SdaResult] = None
    sensor_points: list[list[float]] = Field(
        default_factory=list,
        description='Sensor coordinates in Honeybee world [x, y, z], ordered to match df.values.'
    )


class EnergyQueued(BaseModel):
    study_id: str
    status: str


class EnergyStatus(BaseModel):
    study_id: str
    status: str
    stage: Optional[str] = None
    error: Optional[str] = None


class EnergyResult(BaseModel):
    study_id: str
    status: str
    heating_energy_kwh: float
    cooling_energy_kwh: float
    total_energy_kwh: float
    heating_demand_kwh_m2: float
    cooling_demand_kwh_m2: float
    total_energy_kwh_m2: float
    monthly_heat_balance: Optional[Dict[str, Any]] = None


# ── Locations ─────────────────────────────────────────────────────────────────

class LocationItem(BaseModel):
    id: str
    label: str


class LocationsResponse(BaseModel):
    locations: list[LocationItem]


# ── Errors ────────────────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    error: str
    message: str

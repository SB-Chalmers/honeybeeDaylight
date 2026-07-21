"""
api.schemas
───────────
Pydantic request and response models for all endpoints.
"""

from __future__ import annotations

from typing import Optional
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
    selected_floor_number: int = Field(1, ge=1)

    # Accept either single float (fans out) or per-cardinal dict.
    wwr: Optional[float] = Field(
        None, ge=0, le=1,
        description='Single WWR applied to all four walls. Overridden by wwr_by_cardinal.'
    )
    wwr_by_cardinal: Optional[WwrByCardinal] = None

    wall_thickness: float = Field(0.3, ge=0)
    additional_horizontal_shading_depth: float = Field(0.0, ge=0)
    additional_vertical_shading_depth: float = Field(0.0, ge=0)
    sensor_grid: SensorGridConfig = Field(default_factory=SensorGridConfig)

    @field_validator('footprint_coordinates')
    @classmethod
    def at_least_three_points(cls, v):
        if len(v) < 3:
            raise ValueError('footprint_coordinates must have at least 3 points.')
        return v

    def to_simulation_dict(self) -> dict:
        """Convert to the flat dict expected by simulation.inputs.load_room_inputs."""
        d = self.model_dump(exclude={'wwr', 'wwr_by_cardinal'})
        d['sensor_grid'] = self.sensor_grid.model_dump()
        if self.wwr_by_cardinal:
            d['wwr_by_cardinal'] = self.wwr_by_cardinal.model_dump()
        elif self.wwr is not None:
            d['wwr'] = self.wwr
        else:
            d['wwr_by_cardinal'] = WwrByCardinal().model_dump()
        return d


# ── Study request ─────────────────────────────────────────────────────────────

class Thresholds(BaseModel):
    da_lux: int = 300
    sda_target_pct: float = 50.0


class StudyRequest(BaseModel):
    room: RoomInput
    run_sda: bool = False
    location: Optional[str] = Field(
        None,
        description='Server-side EPW location name. Required when run_sda=true.'
    )
    quality: str = Field('draft', pattern='^(draft|full)$')
    thresholds: Thresholds = Field(default_factory=Thresholds)


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

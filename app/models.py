"""Pydantic models for the venue evacuation scheduling service."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ClosureRange(BaseModel):
    """A left-closed right-open [start, end) interval."""

    start: int = Field(..., ge=0)
    end: int = Field(..., ge=1)

    @model_validator(mode="after")
    def _check_half_open(self):
        if self.end <= self.start:
            raise ValueError("closure must satisfy start < end ([start, end))")
        return self

    def as_tuple(self) -> tuple[int, int]:
        return self.start, self.end


class CorridorIn(BaseModel):
    """A directed corridor between two numbered positions."""

    model_config = ConfigDict(populate_by_name=True)

    number: int = Field(..., alias="corridor", ge=1)
    source: int = Field(..., ge=0)
    target: int = Field(..., ge=0)
    minutes: int = Field(..., alias="travel_minutes", ge=1)
    capacity: int = Field(..., description="people per minute", ge=1)
    closures: list[ClosureRange] = Field(default_factory=list)


class ClosureIn(BaseModel):
    """A left-closed right-open [start, end) closure of one corridor."""

    model_config = ConfigDict(populate_by_name=True)

    corridor: int = Field(..., ge=1)
    start: int = Field(..., ge=0)
    end: int = Field(..., gt=0)

    @field_validator("end")
    @classmethod
    def _validate_end(cls, v: int, info) -> int:
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("closure end must be greater than start ([start, end))")
        return v


class ExitIn(BaseModel):
    """An exit located at a numbered position."""

    node: int = Field(..., ge=0)


class GroupIn(BaseModel):
    """An indivisible crowd group."""

    model_config = ConfigDict(populate_by_name=True)

    number: int = Field(..., alias="group", ge=1)
    people: int = Field(..., ge=1)
    source: int = Field(..., ge=0)
    earliest_departure: int = Field(..., alias="earliest", ge=0)
    priority: int = Field(..., ge=0)
    deadline: int = Field(..., ge=0)

    @field_validator("deadline")
    @classmethod
    def _validate_deadline(cls, v: int, info) -> int:
        earliest = info.data.get("earliest_departure")
        if earliest is not None and v < earliest:
            raise ValueError("deadline must be >= earliest departure minute")
        return v


class AdvanceIn(BaseModel):
    minutes: int = Field(..., ge=1)

"""RAMSES RF - Nested Value Objects for Schedules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as dt

from .state_base import _now_utc

# --- Nested Value Objects for Schedules ---


@dataclass(frozen=True, slots=True)
class SwitchPoint:
    """A single schedule setpoint rule."""

    time_of_day: str
    setpoint: float | None = None
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class DailySchedule:
    """A daily schedule block containing immutable switchpoints."""

    day_of_week: int
    switchpoints: tuple[SwitchPoint, ...]


@dataclass(frozen=True, slots=True)
class ScheduleState:
    """The immutable state of a zone's 7-day schedule."""

    zone_idx: str
    days: tuple[DailySchedule, ...]
    version: int | None = None
    is_current: bool = False
    last_updated: dt = field(default_factory=_now_utc)

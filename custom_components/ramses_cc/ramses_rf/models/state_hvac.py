"""RAMSES RF - HVAC state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as dt

from .state_base import _now_utc


@dataclass(frozen=True, slots=True)
class HvacState:
    """The immutable state of an HVAC ventilation or fan entity."""

    co2_level: int | None = None
    air_quality: float | None = None
    air_quality_basis: float | None = None
    bypass_mode: str | None = None
    bypass_position: float | str | None = None
    bypass_state: str | None = None
    exhaust_fan_speed: float | None = None
    exhaust_flow: float | None = None
    exhaust_temp: float | None = None
    fan_rate: str | None = None
    fan_mode: str | None = None
    fan_info: str | None = None
    indoor_humidity: float | None = None
    indoor_temp: float | None = None
    outdoor_humidity: float | None = None
    outdoor_temp: float | None = None
    post_heat: int | None = None
    pre_heat: int | None = None
    remaining_mins: int | None = None
    request_fan_speed: float | None = None
    request_reason: str | None = None
    speed_capabilities: int | None = None
    supply_fan_speed: float | None = None
    supply_flow: float | None = None
    supply_temp: float | None = None
    temperature: float | None = None
    dewpoint_temp: float | None = None
    presence_detected: bool | None = None
    filter_remaining_days: int | None = None
    filter_remaining_percent: float | None = None
    boost_timer_mins: int | None = None
    filter_dirty: bool | None = None
    frost_cycle: bool | None = None
    has_fault: bool | None = None

    last_updated: dt = field(default_factory=_now_utc)

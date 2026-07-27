"""RAMSES RF - OpenTherm state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as dt

from .state_base import _now_utc


@dataclass(frozen=True)
class OpenThermFlags:
    """Immutable representation of OpenTherm status flags."""

    ch_active: bool | None = None
    ch_enabled: bool | None = None
    cooling_active: bool | None = None
    cooling_enabled: bool | None = None
    dhw_active: bool | None = None
    dhw_blocking: bool | None = None
    dhw_enabled: bool | None = None
    fault_present: bool | None = None
    flame_active: bool | None = None
    otc_active: bool | None = None
    summer_mode: bool | None = None


@dataclass(frozen=True)
class OpenThermTemperatures:
    """Immutable representation of OpenTherm temperatures."""

    boiler_output: float | None = None
    boiler_return: float | None = None
    boiler_setpoint: float | None = None
    ch_max_setpoint: float | None = None
    ch_setpoint: float | None = None
    dhw: float | None = None
    dhw_setpoint: float | None = None
    outside: float | None = None


@dataclass(frozen=True)
class OpenThermCounters:
    """Immutable representation of OpenTherm counters."""

    burner_failed_starts: int | None = None
    burner_hours: int | None = None
    burner_starts: int | None = None
    ch_pump_hours: int | None = None
    ch_pump_starts: int | None = None
    dhw_burner_hours: int | None = None
    dhw_burner_starts: int | None = None
    dhw_pump_hours: int | None = None
    dhw_pump_starts: int | None = None
    flame_signal_low: int | None = None


@dataclass(frozen=True, slots=True)
class OpenThermState:
    """The immutable state of an OpenTherm Bridge (OTB) boiler matrix."""

    last_updated: dt = field(default_factory=_now_utc)
    flags: OpenThermFlags = field(default_factory=OpenThermFlags)
    temperatures: OpenThermTemperatures = field(default_factory=OpenThermTemperatures)
    counters: OpenThermCounters = field(default_factory=OpenThermCounters)
    ch_water_pressure: float | None = None
    dhw_flow_rate: float | None = None
    max_rel_modulation: float | None = None
    rel_modulation_level: float | None = None
    oem_code: int | None = None

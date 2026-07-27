"""RAMSES RF - Climate/Heating state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as dt

from .state_base import _now_utc

# --- Compositional State Blocks ---


@dataclass(frozen=True, slots=True)
class TemperatureState:
    """State for entities that measure or target temperature."""

    temperature: float | None = None
    setpoint: float | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class TrvState:
    """State for TRV (Thermostatic Radiator Valve) entities."""

    window_open: bool | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class DemandState:
    """State for entities that request or actuate heat/cooling."""

    zone_demand: float | None = None
    relay_active: bool = False
    relay_demand: float | None = None
    relay_failsafe: bool | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class DhwState:
    """State for DHW (Domestic Hot Water) entities."""

    setpoint: float | None = None
    overrun: int | None = None
    differential: float | None = None
    mode: str | None = None
    active: bool | None = None
    until: dt | str | None = None
    temperature: float | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class SystemState:
    """State for central system controllers."""

    system_mode: str | None = None
    until: dt | str | None = None
    datetime: str | None = None
    language: str | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class PowerState:
    """Power and battery state for wireless entities."""

    battery_low: bool | None = None
    battery_level: float | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class ZoneState:
    """State for standard heating zones."""

    name: str | None = None
    mode: str | None = None
    setpoint: float | None = None
    until: dt | str | None = None
    min_temp: float | None = None
    max_temp: float | None = None
    local_override: bool | None = None
    openwindow_function: bool | None = None
    multiroom_mode: bool | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class UfhState:
    """State for Underfloor Heating (UFH) controllers."""

    zone_demands: dict[str, float | None] = field(default_factory=dict)
    setpoints: dict[str, dict[str, float | None]] = field(default_factory=dict)
    relay_demand_fa: float | None = None
    last_updated: dt = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class ActuatorState:
    """State for boiler and heating actuators (e.g., BDR91)."""

    modulation_level: float | None = None
    actuator_enabled: bool | None = None
    ch_active: bool | None = None
    ch_enabled: bool | None = None
    dhw_active: bool | None = None
    flame_active: bool | None = None
    # Legacy payload restorations for ramses_cc backwards compatibility
    ch_setpoint: float | None = None
    cool_active: bool | None = None
    flame_on: bool | None = None
    max_rel_modulation: float | None = None
    actuator_countdown: int | None = None
    cycle_countdown: int | None = None
    last_updated: dt = field(default_factory=_now_utc)

#!/usr/bin/env python3
"""RAMSES RF - Zone command constructors."""

from __future__ import annotations

from datetime import datetime as dt
from typing import TypeVar

from .. import exceptions as exc
from ..const import RQ, W_, Code
from ..helpers import hex_from_dtm, hex_from_str, hex_from_temp
from ..typing import DeviceIdT, PayloadT
from .base import CommandBase, _check_idx, _normalise_mode, _normalise_until, _ZoneIdxT

_T = TypeVar("_T", bound=CommandBase)


class ZoneMixins(CommandBase):
    """Mixins for Heating Zone commands."""

    @classmethod
    def get_zone_name(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> _T:
        payload = f"{_check_idx(zone_idx)}00"
        return cls.from_attrs(RQ, ctl_id, Code._0004, PayloadT(payload))

    @classmethod
    def set_zone_name(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT, name: str
    ) -> _T:
        payload = f"{_check_idx(zone_idx)}00{hex_from_str(name)[:40]:0<40}"
        return cls.from_attrs(W_, ctl_id, Code._0004, PayloadT(payload))

    @classmethod
    def get_zone_config(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> _T:
        zon_idx = _check_idx(zone_idx)
        return cls.from_attrs(RQ, ctl_id, Code._000A, PayloadT(zon_idx))

    @classmethod
    def set_zone_config(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        zone_idx: _ZoneIdxT,
        *,
        min_temp: float = 5,
        max_temp: float = 35,
        local_override: bool = False,
        openwindow_function: bool = False,
        multiroom_mode: bool = False,
    ) -> _T:
        zon_idx = _check_idx(zone_idx)

        if not (5 <= min_temp <= 21):
            raise exc.CommandInvalid(f"Out of range, min_temp: {min_temp}")
        if not (21 <= max_temp <= 35):
            raise exc.CommandInvalid(f"Out of range, max_temp: {max_temp}")
        if not isinstance(local_override, bool):
            raise exc.CommandInvalid(f"Invalid arg, local_override: {local_override}")
        if not isinstance(openwindow_function, bool):
            raise exc.CommandInvalid(
                f"Invalid arg, openwindow_function: {openwindow_function}"
            )
        if not isinstance(multiroom_mode, bool):
            raise exc.CommandInvalid(f"Invalid arg, multiroom_mode: {multiroom_mode}")

        bitmap = 0 if local_override else 1
        bitmap |= 0 if openwindow_function else 2
        bitmap |= 0 if multiroom_mode else 16

        payload = "".join(
            (zon_idx, f"{bitmap:02X}", hex_from_temp(min_temp), hex_from_temp(max_temp))
        )

        return cls.from_attrs(W_, ctl_id, Code._000A, PayloadT(payload))

    @classmethod
    def get_zone_window_state(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> _T:
        return cls.from_attrs(RQ, ctl_id, Code._12B0, PayloadT(_check_idx(zone_idx)))

    @classmethod
    def get_zone_setpoint(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> _T:
        return cls.from_attrs(RQ, ctl_id, Code._2309, PayloadT(_check_idx(zone_idx)))

    @classmethod
    def set_zone_setpoint(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT, setpoint: float
    ) -> _T:
        payload = f"{_check_idx(zone_idx)}{hex_from_temp(setpoint)}"
        return cls.from_attrs(W_, ctl_id, Code._2309, PayloadT(payload))

    @classmethod
    def get_zone_mode(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> _T:
        return cls.from_attrs(RQ, ctl_id, Code._2349, PayloadT(_check_idx(zone_idx)))

    @classmethod
    def set_zone_mode(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        zone_idx: _ZoneIdxT,
        *,
        mode: int | str | None = None,
        setpoint: float | None = None,
        until: dt | str | None = None,
        duration: int | None = None,
    ) -> _T:
        mode = _normalise_mode(mode, setpoint, until, duration)

        if setpoint is not None and not isinstance(setpoint, float | int):
            raise exc.CommandInvalid(
                f"Invalid args: setpoint={setpoint}, but must be a float"
            )

        until, duration = _normalise_until(mode, setpoint, until, duration)

        payload = "".join(
            (
                _check_idx(zone_idx),
                hex_from_temp(setpoint),
                mode,
                "FFFFFF" if duration is None else f"{duration:06X}",
                "" if until is None else hex_from_dtm(until),
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._2349, PayloadT(payload))

    @classmethod
    def get_zone_temp(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> _T:
        return cls.from_attrs(RQ, ctl_id, Code._30C9, PayloadT(_check_idx(zone_idx)))

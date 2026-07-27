#!/usr/bin/env python3
"""RAMSES RF - DHW command constructors."""

from __future__ import annotations

from datetime import datetime as dt
from typing import Any, TypeVar

from .. import exceptions as exc
from ..const import I_, RQ, SZ_DHW_IDX, W_, ZON_MODE_MAP, Code
from ..helpers import hex_from_dtm, hex_from_temp
from ..typing import DeviceIdT, PayloadT
from .base import CommandBase, _check_idx, _normalise_mode, _normalise_until

_T = TypeVar("_T", bound=CommandBase)


class DhwMixins(CommandBase):
    """Mixins for DHW commands."""

    @classmethod
    def get_dhw_params(cls: type[_T], ctl_id: DeviceIdT | str, **kwargs: Any) -> _T:
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"
        return cls.from_attrs(RQ, ctl_id, Code._10A0, PayloadT(dhw_idx))

    @classmethod
    def set_dhw_params(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        *,
        setpoint: float | None = 50.0,
        overrun: int | None = 5,
        differential: float | None = 1,
        **kwargs: Any,
    ) -> _T:
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        setpoint = 50.0 if setpoint is None else setpoint
        overrun = 5 if overrun is None else overrun
        differential = 1.0 if differential is None else differential

        if not (30.0 <= setpoint <= 85.0):
            raise exc.CommandInvalid(f"Out of range, setpoint: {setpoint}")
        if not (0 <= overrun <= 10):
            raise exc.CommandInvalid(f"Out of range, overrun: {overrun}")
        if not (1 <= differential <= 10):
            raise exc.CommandInvalid(f"Out of range, differential: {differential}")

        payload = f"{dhw_idx}{hex_from_temp(setpoint)}{overrun:02X}{hex_from_temp(differential)}"
        return cls.from_attrs(W_, ctl_id, Code._10A0, PayloadT(payload))

    @classmethod
    def get_dhw_temp(cls: type[_T], ctl_id: DeviceIdT | str, **kwargs: Any) -> _T:
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"
        return cls.from_attrs(RQ, ctl_id, Code._1260, PayloadT(dhw_idx))

    @classmethod
    def put_dhw_temp(
        cls: type[_T], dev_id: DeviceIdT | str, temperature: float | None, **kwargs: Any
    ) -> _T:
        from ..const import DEV_TYPE_MAP

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        if dev_id[:2] != DEV_TYPE_MAP.DHW:
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.DHW}:xxxxxx"
            )

        payload = f"{dhw_idx}{hex_from_temp(temperature)}"
        return cls._from_attrs(
            I_, Code._1260, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def get_dhw_mode(cls: type[_T], ctl_id: DeviceIdT | str, **kwargs: Any) -> _T:
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"
        return cls.from_attrs(RQ, ctl_id, Code._1F41, PayloadT(dhw_idx))

    @classmethod
    def set_dhw_mode(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        *,
        mode: int | str | None = None,
        active: bool | None = None,
        until: dt | str | None = None,
        duration: int | None = None,
        **kwargs: Any,
    ) -> _T:
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        mode = _normalise_mode(mode, active, until, duration)

        if mode == ZON_MODE_MAP.FOLLOW:
            active = None
        if active is not None and not isinstance(active, bool | int):
            raise exc.CommandInvalid(
                f"Invalid args: active={active}, but must be a bool"
            )

        until, duration = _normalise_until(mode, active, until, duration)

        payload = "".join(
            (
                dhw_idx,
                "FF" if active is None else "01" if bool(active) else "00",
                mode,
                "FFFFFF" if duration is None else f"{duration:06X}",
                "" if until is None else hex_from_dtm(until),
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._1F41, PayloadT(payload))

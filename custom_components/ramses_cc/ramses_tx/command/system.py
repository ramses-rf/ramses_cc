#!/usr/bin/env python3
"""RAMSES RF - System-wide command constructors."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ...ramses_rf.protocol.opentherm import parity

from .. import exceptions as exc
from ..address import ALL_DEV_ADDR, dev_id_to_hex_id
from ..const import (
    DEV_TYPE_MAP,
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    FC,
    FF,
    I_,
    LOOKUP_PUZZ,
    RP,
    RQ,
    SYS_MODE_MAP,
    W_,
    Code,
)
from ..helpers import (
    hex_from_bool,
    hex_from_dtm,
    hex_from_dts,
    hex_from_percent,
    hex_from_str,
    hex_from_temp,
    timestamp,
)
from ..typing import DeviceIdT, PayloadT
from ..version import VERSION
from .base import CommandBase, _check_idx

if TYPE_CHECKING:
    from ..const import FaultDeviceClass, FaultState, FaultType, VerbT

_T = TypeVar("_T", bound="SystemMixins")


class SystemMixins(CommandBase):
    """Mixins for System-wide commands."""

    @classmethod
    def put_weather_temp(
        cls: type[_T], dev_id: DeviceIdT | str, temperature: float
    ) -> _T:
        if dev_id[:2] != DEV_TYPE_MAP.OUT:
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.OUT}:xxxxxx"
            )

        payload = f"00{hex_from_temp(temperature)}01"
        return cls._from_attrs(
            I_, Code._0002, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def get_schedule_version(cls: type[_T], ctl_id: DeviceIdT | str) -> _T:
        return cls.from_attrs(RQ, ctl_id, Code._0006, PayloadT("00"))

    @classmethod
    def get_relay_demand(
        cls: type[_T], dev_id: DeviceIdT | str, zone_idx: int | str | None = None
    ) -> _T:
        payload = "00" if zone_idx is None else _check_idx(zone_idx)
        return cls.from_attrs(RQ, dev_id, Code._0008, PayloadT(payload))

    @classmethod
    def get_system_language(
        cls: type[_T], ctl_id: DeviceIdT | str, **kwargs: Any
    ) -> _T:
        assert not kwargs, kwargs
        return cls.from_attrs(RQ, ctl_id, Code._0100, PayloadT("00"), **kwargs)

    @classmethod
    def get_schedule_fragment(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        zone_idx: int | str,
        frag_number: int,
        total_frags: int | None,
        **kwargs: Any,
    ) -> _T:
        from ..const import FA

        assert not kwargs, kwargs
        zon_idx = _check_idx(zone_idx)

        if total_frags is None:
            total_frags = 0

        kwargs.pop("frag_length", None)
        frag_length = "00"

        if frag_number == 0:
            raise exc.CommandInvalid(f"frag_number={frag_number}, but it is 1-indexed")
        elif frag_number == 1 and total_frags != 0:
            raise exc.CommandInvalid(
                f"total_frags={total_frags}, but must be 0 when frag_number=1"
            )
        elif frag_number > total_frags and total_frags != 0:
            raise exc.CommandInvalid(
                f"frag_number={frag_number}, but must be <= total_frags={total_frags}"
            )

        header = "00230008" if zon_idx == FA else f"{zon_idx}200008"

        payload = f"{header}{frag_length}{frag_number:02X}{total_frags:02X}"
        return cls.from_attrs(RQ, ctl_id, Code._0404, PayloadT(payload), **kwargs)

    @classmethod
    def set_schedule_fragment(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        zone_idx: int | str,
        frag_num: int,
        frag_cnt: int,
        fragment: str,
    ) -> _T:
        from ..const import FA

        zon_idx = _check_idx(zone_idx)

        if frag_num == 0:
            raise exc.CommandInvalid(f"frag_num={frag_num}, but it is 1-indexed")
        elif frag_num > frag_cnt:
            raise exc.CommandInvalid(
                f"frag_num={frag_num}, but must be <= frag_cnt={frag_cnt}"
            )

        header = "00230008" if zon_idx == FA else f"{zon_idx}200008"
        frag_length = int(len(fragment) / 2)

        payload = f"{header}{frag_length:02X}{frag_num:02X}{frag_cnt:02X}{fragment}"
        return cls.from_attrs(W_, ctl_id, Code._0404, PayloadT(payload))

    @classmethod
    def get_system_log_entry(
        cls: type[_T], ctl_id: DeviceIdT | str, log_idx: int | str
    ) -> _T:
        log_idx = log_idx if isinstance(log_idx, int) else int(log_idx, 16)
        return cls.from_attrs(RQ, ctl_id, Code._0418, PayloadT(f"{log_idx:06X}"))

    @classmethod
    def _put_system_log_entry(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        fault_state: FaultState | str,
        fault_type: FaultType | str,
        device_class: FaultDeviceClass | str,
        device_id: DeviceIdT | str | None = None,
        domain_idx: int | str = "00",
        _log_idx: int | str | None = None,
        timestamp: dt | str | None = None,  # <-- Reverted to 'timestamp'
        **kwargs: Any,
    ) -> _T:
        import enum

        if isinstance(device_class, enum.Enum):
            device_class = {v: k for k, v in FAULT_DEVICE_CLASS.items()}[device_class]
        assert device_class in FAULT_DEVICE_CLASS

        if isinstance(fault_state, enum.Enum):
            fault_state = {v: k for k, v in FAULT_STATE.items()}[fault_state]
        assert fault_state in FAULT_STATE

        if isinstance(fault_type, enum.Enum):
            fault_type = {v: k for k, v in FAULT_TYPE.items()}[fault_type]
        assert fault_type in FAULT_TYPE

        assert isinstance(domain_idx, str) and len(domain_idx) == 2

        if _log_idx is None:
            _log_idx = 0
        if not isinstance(_log_idx, str):
            _log_idx = f"{_log_idx:02X}"
        assert 0 <= int(_log_idx, 16) <= 0x3F

        if timestamp is None:
            timestamp = dt.now()  # Reverted back to standard dt.now()
        ts = hex_from_dts(timestamp)

        dev_id = dev_id_to_hex_id(device_id) if device_id else "000000"  # type: ignore[arg-type]

        payload = "".join(
            (
                "00",
                fault_state,
                _log_idx,
                "B0",
                fault_type,
                domain_idx,
                device_class,
                "0000",
                ts,
                "FFFF7000",
                dev_id,
            )
        )

        return cls.from_attrs(I_, ctl_id, Code._0418, PayloadT(payload))

    @classmethod
    def get_mix_valve_params(
        cls: type[_T], ctl_id: DeviceIdT | str, zone_idx: int | str
    ) -> _T:
        zon_idx = _check_idx(zone_idx)
        return cls.from_attrs(RQ, ctl_id, Code._1030, PayloadT(zon_idx))

    @classmethod
    def set_mix_valve_params(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        zone_idx: int | str,
        *,
        max_flow_setpoint: int = 55,
        min_flow_setpoint: int = 15,
        valve_run_time: int = 150,
        pump_run_time: int = 15,
        **kwargs: Any,
    ) -> _T:
        boolean_cc = kwargs.pop("boolean_cc", 1)
        assert not kwargs, kwargs

        zon_idx = _check_idx(zone_idx)

        if not (0 <= max_flow_setpoint <= 99):
            raise exc.CommandInvalid(
                f"Out of range, max_flow_setpoint: {max_flow_setpoint}"
            )
        if not (0 <= min_flow_setpoint <= 50):
            raise exc.CommandInvalid(
                f"Out of range, min_flow_setpoint: {min_flow_setpoint}"
            )
        if not (0 <= valve_run_time <= 240):
            raise exc.CommandInvalid(f"Out of range, valve_run_time: {valve_run_time}")
        if not (0 <= pump_run_time <= 99):
            raise exc.CommandInvalid(f"Out of range, pump_run_time: {pump_run_time}")

        payload = "".join(
            (
                zon_idx,
                f"C801{max_flow_setpoint:02X}",
                f"C901{min_flow_setpoint:02X}",
                f"CA01{valve_run_time:02X}",
                f"CB01{pump_run_time:02X}",
                f"CC01{boolean_cc:02X}",
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._1030, PayloadT(payload), **kwargs)

    @classmethod
    def get_tpi_params(
        cls: type[_T], dev_id: DeviceIdT | str, *, domain_id: int | str | None = None
    ) -> _T:
        if domain_id is None:
            domain_id = "00" if dev_id[:2] == DEV_TYPE_MAP.BDR else FC

        return cls.from_attrs(RQ, dev_id, Code._1100, PayloadT(_check_idx(domain_id)))

    @classmethod
    def set_tpi_params(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        domain_id: int | str | None,
        *,
        cycle_rate: int = 3,
        min_on_time: int = 5,
        min_off_time: int = 5,
        proportional_band_width: float | None = None,
    ) -> _T:
        if domain_id is None:
            domain_id = "00"

        payload = "".join(
            (
                _check_idx(domain_id),
                f"{cycle_rate * 4:02X}",
                f"{int(min_on_time * 4):02X}",
                f"{int(min_off_time * 4):02X}00",
                f"{hex_from_temp(proportional_band_width)}01",
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._1100, PayloadT(payload))

    @classmethod
    def put_outdoor_temp(
        cls: type[_T], dev_id: DeviceIdT | str, temperature: float | None
    ) -> _T:
        payload = f"00{hex_from_temp(temperature)}"
        return cls._from_attrs(
            I_, Code._1290, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def put_bind(
        cls: type[_T],
        verb: VerbT,
        src_id: DeviceIdT | str,
        codes: Code | Iterable[Code] | None,
        dst_id: DeviceIdT | str | None = None,
        **kwargs: Any,
    ) -> _T:
        kodes: list[Code]

        if not codes:
            kodes = []
        elif len(list(codes)[0]) == len(Code._1FC9):
            kodes = list(codes)  # type: ignore[arg-type]
        elif len(list(codes)[0]) == len(Code._1FC9[0]):
            kodes = [cast(Code, codes)]
        else:
            raise exc.CommandInvalid(f"Invalid codes for a bind command: {codes}")

        if verb == I_ and dst_id in (None, src_id, ALL_DEV_ADDR.id):
            oem_code = kwargs.pop("oem_code", None)
            assert not kwargs, f"Unexpected arguments: {kwargs}"
            return cls._put_bind_offer(src_id, dst_id, kodes, oem_code=oem_code)

        elif verb == W_ and dst_id not in (None, src_id):
            idx = kwargs.pop("idx", None)
            assert not kwargs, kwargs
            return cls._put_bind_accept(src_id, cast(DeviceIdT, dst_id), kodes, idx=idx)

        elif verb == I_:
            idx = kwargs.pop("idx", None)
            assert not kwargs, kwargs
            return cls._put_bind_confirm(
                src_id, cast(DeviceIdT, dst_id), kodes, idx=idx
            )

        raise exc.CommandInvalid(
            f"Invalid verb|dst_id for a bind command: {verb}|{dst_id}"
        )

    @classmethod
    def _put_bind_offer(
        cls: type[_T],
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str | None,
        codes: list[Code],
        *,
        oem_code: str | None = None,
    ) -> _T:
        kodes = [c for c in codes if c not in (Code._1FC9, Code._10E0)]
        if not kodes:
            raise exc.CommandInvalid(f"Invalid codes for a bind offer: {codes}")

        hex_id = dev_id_to_hex_id(cast(DeviceIdT, src_id))
        payload = "".join(f"00{c}{hex_id}" for c in kodes)

        if oem_code:
            payload += f"{oem_code}{Code._10E0}{hex_id}"
        payload += f"00{Code._1FC9}{hex_id}"

        return cls.from_attrs(
            I_, dst_id or src_id, Code._1FC9, PayloadT(payload), from_id=src_id
        )

    @classmethod
    def _put_bind_accept(
        cls: type[_T],
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str,
        codes: list[Code],
        *,
        idx: str | None = "00",
    ) -> _T:
        if not codes:
            raise exc.CommandInvalid(f"Invalid codes for a bind accept: {codes}")

        hex_id = dev_id_to_hex_id(cast(DeviceIdT, src_id))
        payload = "".join(f"{idx or '00'}{c}{hex_id}" for c in codes)

        return cls.from_attrs(W_, dst_id, Code._1FC9, PayloadT(payload), from_id=src_id)

    @classmethod
    def _put_bind_confirm(
        cls: type[_T],
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str,
        codes: list[Code],
        *,
        idx: str | None = "00",
    ) -> _T:
        if not codes:
            payload = idx or "00"
        else:
            hex_id = dev_id_to_hex_id(cast(DeviceIdT, src_id))
            payload = f"{idx or '00'}{codes[0]}{hex_id}"

        return cls.from_attrs(I_, dst_id, Code._1FC9, PayloadT(payload), from_id=src_id)

    @classmethod
    def get_system_mode(cls: type[_T], ctl_id: DeviceIdT | str) -> _T:
        return cls.from_attrs(RQ, ctl_id, Code._2E04, PayloadT(FF))

    @classmethod
    def set_system_mode(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        system_mode: int | str | None,
        *,
        until: dt | str | None = None,
    ) -> _T:
        if system_mode is None:
            system_mode = SYS_MODE_MAP.AUTO
        if isinstance(system_mode, int):
            system_mode = f"{system_mode:02X}"
        if system_mode not in SYS_MODE_MAP:
            system_mode = SYS_MODE_MAP._hex(system_mode)

        if until is not None and system_mode in (
            SYS_MODE_MAP.AUTO,
            SYS_MODE_MAP.AUTO_WITH_RESET,
            SYS_MODE_MAP.HEAT_OFF,
        ):
            raise exc.CommandInvalid(
                f"Invalid args: For system_mode={SYS_MODE_MAP[system_mode]},"
                " until must be None"
            )

        assert isinstance(system_mode, str)

        payload = "".join(
            (
                system_mode,
                hex_from_dtm(until),
                "00" if until is None else "01",
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._2E04, PayloadT(payload))

    @classmethod
    def put_presence_detected(
        cls: type[_T], dev_id: DeviceIdT | str, presence_detected: bool | None
    ) -> _T:
        payload = f"00{hex_from_bool(presence_detected)}"
        return cls._from_attrs(
            I_, Code._2E10, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def put_sensor_temp(
        cls: type[_T], dev_id: DeviceIdT | str, temperature: float | None
    ) -> _T:
        if dev_id[:2] not in (
            DEV_TYPE_MAP.TR0,
            DEV_TYPE_MAP.HCW,
            DEV_TYPE_MAP.TRV,
            DEV_TYPE_MAP.DTS,
            DEV_TYPE_MAP.DT2,
            DEV_TYPE_MAP.RND,
        ):
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.HCW}:xxxxxx"
            )

        payload = f"00{hex_from_temp(temperature)}"
        return cls._from_attrs(
            I_, Code._30C9, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def get_system_time(cls: type[_T], ctl_id: DeviceIdT | str) -> _T:
        return cls.from_attrs(RQ, ctl_id, Code._313F, PayloadT("00"))

    @classmethod
    def set_system_time(
        cls: type[_T],
        ctl_id: DeviceIdT | str,
        datetime: dt | str,
        is_dst: bool = False,
    ) -> _T:
        dt_str = hex_from_dtm(datetime, is_dst=is_dst, incl_seconds=True)
        return cls.from_attrs(W_, ctl_id, Code._313F, PayloadT(f"0060{dt_str}"))

    @classmethod
    def get_opentherm_data(
        cls: type[_T], otb_id: DeviceIdT | str, msg_id: int | str
    ) -> _T:
        msg_id = msg_id if isinstance(msg_id, int) else int(msg_id, 16)
        payload = f"0080{msg_id:02X}0000" if parity(msg_id) else f"0000{msg_id:02X}0000"
        return cls.from_attrs(RQ, otb_id, Code._3220, PayloadT(payload))

    @classmethod
    def put_actuator_state(
        cls: type[_T], dev_id: DeviceIdT | str, modulation_level: float
    ) -> _T:
        if dev_id[:2] != DEV_TYPE_MAP.BDR:
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.BDR}:xxxxxx"
            )

        payload = (
            "007FFF"
            if modulation_level is None
            else f"00{int(modulation_level * 200):02X}FF"
        )
        return cls._from_attrs(
            I_, Code._3EF0, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def put_actuator_cycle(
        cls: type[_T],
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str,
        modulation_level: float,
        actuator_countdown: int,
        *,
        cycle_countdown: int | None = None,
    ) -> _T:
        if src_id[:2] != DEV_TYPE_MAP.BDR:
            raise exc.CommandInvalid(
                f"Faked device {src_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.BDR}:xxxxxx"
            )

        payload = "00"
        payload += f"{cycle_countdown:04X}" if cycle_countdown is not None else "7FFF"
        payload += f"{actuator_countdown:04X}"
        payload += hex_from_percent(modulation_level)
        payload += "FF"
        return cls._from_attrs(
            RP, Code._3EF1, PayloadT(payload), addr0=src_id, addr1=dst_id
        )

    @classmethod
    def _puzzle(cls: type[_T], msg_type: str | None = None, message: str = "") -> _T:
        if msg_type is None:
            msg_type = "12" if message else "10"

        assert msg_type in LOOKUP_PUZZ, f"Invalid/deprecated Puzzle type: {msg_type}"

        payload = f"00{msg_type}"

        if int(msg_type, 16) >= int("20", 16):
            payload += f"{int(timestamp() * 1e7):012X}"
        elif msg_type != "13":
            payload += f"{int(timestamp() * 1000):012X}"

        if msg_type == "10":
            payload += hex_from_str(f"v{VERSION}")
        elif msg_type == "11":
            payload += hex_from_str(message[:4] + message[5:7] + message[8:])
        else:
            payload += hex_from_str(message)

        return cls.from_attrs(I_, ALL_DEV_ADDR.id, Code._PUZZ, PayloadT(payload[:48]))

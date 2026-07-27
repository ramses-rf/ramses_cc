#!/usr/bin/env python3
"""RAMSES RF - HVAC and ventilation command constructors."""

from __future__ import annotations

import logging
import math
from typing import Any, TypeVar

from ...ramses_rf.protocol.ramses import (
    _22F1_SCHEMES,
    _2411_PARAMS_SCHEMA,
    SZ_DATA_TYPE,
    SZ_MAX_VALUE,
    SZ_MIN_VALUE,
    SZ_PRECISION,
)

from .. import exceptions as exc
from ..address import NON_DEV_ADDR
from ..const import I_, RQ, SZ_MINUTES, W_, Code
from ..helpers import (
    air_quality_code,
    capability_bits,
    fan_info_flags,
    fan_info_to_byte,
    hex_from_double,
    hex_from_percent,
    hex_from_temp,
)
from ..typing import DeviceIdT, PayloadT
from .base import CommandBase

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T", bound=CommandBase)

# Mode-max (3rd payload byte) per scheme, used to build 3-byte 22F1 payloads
# for devices that require them (e.g. Siber DF Evo 4, ClimaRad MiniBox).
# See issue #547.  When None, the builder emits a 2-byte payload (legacy form).
_22F1_MODE_MAX: dict[str, str | None] = {
    "itho": "04",
    "nuaire": "0A",
    "vasco": "06",
    "orcon": "07",
}


class HvacMixins(CommandBase):
    """Mixins for HVAC commands."""

    @classmethod
    def put_co2_level(
        cls: type[_T], dev_id: DeviceIdT | str, co2_level: float | None
    ) -> _T:
        payload = f"00{hex_from_double(co2_level)}"
        return cls._from_attrs(
            I_, Code._1298, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def put_indoor_humidity(
        cls: type[_T], dev_id: DeviceIdT | str, indoor_humidity: float | None
    ) -> _T:
        payload = "00" + hex_from_percent(indoor_humidity, high_res=False)
        return cls._from_attrs(
            I_, Code._12A0, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

    @classmethod
    def set_fan_mode(
        cls: type[_T],
        fan_id: DeviceIdT | str,
        fan_mode: int | str | None,
        *,
        scheme: str = "orcon",
        seqn: int | str | None = None,
        src_id: DeviceIdT | str | None = None,
        idx: str = "00",
        mode_max: str | None = None,
        legacy_format: bool = False,
    ) -> _T:
        """Build a 22F1 (fan mode) command for the given scheme.

        :param fan_id: The target fan device ID.
        :param fan_mode: The desired mode (int index, hex string, or name).
        :param scheme: The fan manufacturer scheme (orcon/itho/vasco/nuaire).
            Defaults to "orcon" for backward compatibility.  See issue #547.
        :param seqn: Optional sequence number (mutually exclusive with src_id).
        :param src_id: Optional source device ID (mutually exclusive with seqn).
        :param idx: The payload index byte, defaults to "00".
        :param mode_max: Optional override for the 3rd payload byte (mode_max).
            If None, auto-determined from the scheme via ``_22F1_MODE_MAX``.
        :param legacy_format: If True, forces a legacy 2-byte payload.
        """
        if scheme not in _22F1_SCHEMES:
            raise exc.CommandInvalid(
                f"fan_mode scheme is not valid: {scheme} "
                f"(expected one of: {', '.join(sorted(_22F1_SCHEMES))})"
            )

        mode_map = _22F1_SCHEMES[scheme]
        mode_map_r = {v: k for k, v in mode_map.items()}

        if fan_mode is None:
            mode = "00"
        elif isinstance(fan_mode, int):
            mode = f"{fan_mode:02X}"
        else:
            mode = fan_mode

        if mode in mode_map:
            pass
        elif mode in mode_map_r:
            mode = mode_map_r[mode]
        else:
            raise exc.CommandInvalid(
                f"fan_mode is not valid for scheme '{scheme}': {fan_mode}"
            )

        # Determine the mode_max (3rd byte) for 3-byte payloads.
        if mode_max is None:
            mode_max = _22F1_MODE_MAX.get(scheme)

        if legacy_format or not mode_max:
            payload = f"{idx}{mode}"
        else:
            payload = f"{idx}{mode}{mode_max}"

        if src_id and seqn:
            raise exc.CommandInvalid(
                "seqn and src_id are mutually exclusive (you can have neither)"
            )

        if seqn:
            return cls._from_attrs(
                I_, Code._22F1, PayloadT(payload), addr2=fan_id, seqn=seqn
            )
        return cls._from_attrs(
            I_, Code._22F1, PayloadT(payload), addr0=src_id, addr1=fan_id
        )

    @classmethod
    def set_bypass_position(
        cls: type[_T],
        fan_id: DeviceIdT | str,
        *,
        bypass_position: float | None = None,
        src_id: DeviceIdT | str | None = None,
        **kwargs: Any,
    ) -> _T:
        bypass_mode = kwargs.pop("bypass_mode", None)
        assert not kwargs, kwargs

        src_id = src_id or fan_id

        if bypass_mode and bypass_position is not None:
            raise exc.CommandInvalid(
                "bypass_mode and bypass_position are mutually exclusive, "
                "both cannot be provided, and neither is OK"
            )
        elif bypass_position is not None:
            pos = f"{int(bypass_position * 200):02X}"
        elif bypass_mode:
            pos = {"auto": "FF", "off": "00", "on": "C8"}[bypass_mode]
        else:
            pos = "FF"

        return cls._from_attrs(
            W_, Code._22F7, PayloadT(f"00{pos}"), addr0=src_id, addr1=fan_id
        )

    @classmethod
    def set_fan_param(
        cls: type[_T],
        fan_id: DeviceIdT | str,
        param_id: str,
        value: str | int | float | bool,
        *,
        src_id: DeviceIdT | str | None = None,
    ) -> _T:
        try:
            param_id = param_id.strip().upper()
            if len(param_id) != 2:
                raise ValueError(
                    "Parameter ID must be exactly 2 hexadecimal characters"
                )
            int(param_id, 16)
        except ValueError as err:
            raise exc.CommandInvalid(
                f"Invalid parameter ID: '{param_id}'. "
                "Must be a 2-digit hexadecimal value (00-FF)"
            ) from err

        if (param_schema := _2411_PARAMS_SCHEMA.get(param_id)) is None:
            raise exc.CommandInvalid(
                f"Unknown parameter ID: '{param_id}'. "
                "This parameter is not defined in the device schema"
            )

        min_val = param_schema[SZ_MIN_VALUE]
        max_val = param_schema[SZ_MAX_VALUE]
        precision = param_schema.get(SZ_PRECISION, 1.0)
        data_type = param_schema.get(SZ_DATA_TYPE, "00")

        try:
            if isinstance(value, float) and not math.isfinite(value):
                raise exc.CommandInvalid(
                    f"Parameter {param_id}: Invalid value '{value}'. "
                    "Must be a finite number"
                )

            if str(data_type) == "01":  # %
                value_scaled = int(round(float(value) / precision))
                min_val_scaled = int(round(float(min_val) / precision))
                max_val_scaled = int(round(float(max_val) / precision))
                precision_scaled = int(round(float(precision) * 10))
                trailer = "0032"
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: Value {value_scaled / 10}% "
                        f"is out of allowed range ({min_val_scaled / 10}% "
                        f"to {max_val_scaled / 10}%)"
                    )
            elif str(data_type) == "0F":  # %
                value_scaled = int(round((float(value) / 100.0) / float(precision)))
                min_val_scaled = int(round(float(min_val) / float(precision)))
                max_val_scaled = int(round(float(max_val) / float(precision)))
                precision_scaled = int(round(float(precision) * 200))
                trailer = "0032"
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: Value {value_scaled / 2}% "
                        f"is out of allowed range ({min_val_scaled / 2}% "
                        f"to {max_val_scaled / 2}%)"
                    )
            elif str(data_type) == "92":  # °C
                value_rounded = round(float(value) * 10) / 10
                value_scaled = int(value_rounded * 100)
                min_val_scaled = int(float(min_val) * 100)
                max_val_scaled = int(float(max_val) * 100)
                precision_scaled = int(float(precision) * 100)
                trailer = "0001"
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: "
                        f"Temperature {value_scaled / 100:.1f}°C is out of "
                        f"allowed range ({min_val_scaled / 100:.1f}°C to "
                        f"{max_val_scaled / 100:.1f}°C)"
                    )
            elif (
                (str(data_type) == "00")
                or (str(data_type) == "10")
                or (str(data_type) == "20")
                or (str(data_type) == "90")
            ):
                value_scaled = int(float(value))
                min_val_scaled = int(float(min_val))
                max_val_scaled = int(float(max_val))
                precision = 1
                precision_scaled = int(precision)
                trailer = "0001"
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    unit = SZ_MINUTES if data_type == "00" else ""
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: Value {value_scaled}"
                        f"{' ' + unit if unit else ''} is out of allowed "
                        f"range ({min_val_scaled} to {max_val_scaled}"
                        f"{' ' + unit if unit else ''})"
                    )
            else:
                raise exc.CommandInvalid(
                    f"Parameter {param_id}: Invalid data type '{data_type}'. "
                    "Must be one of '00', '01', '0F', '10', '20', '90', or '92'"
                )

            leading = "00"
            param_id_hex = f"{int(param_id, 16):04X}"

            data_type_hex = f"00{data_type}"
            value_hex = f"{value_scaled:08X}"
            min_hex = f"{min_val_scaled:08X}"
            max_hex = f"{max_val_scaled:08X}"
            precision_hex = f"{precision_scaled:08X}"

            _LOGGER.debug(
                f"set_fan_param: value={value}, min={min_val}, "
                f"max={max_val}, precision={precision}\n  Scaled: "
                f"value={value_scaled} (0x{value_hex}), min={min_val_scaled} "
                f"(0x{min_hex}), max={max_val_scaled} (0x{max_hex}), "
                f"precision={precision_scaled} (0x{precision_hex})"
            )

            payload = (
                f"{leading}"
                f"{param_id_hex}"
                f"{data_type_hex}"
                f"{value_hex}"
                f"{min_hex}"
                f"{max_hex}"
                f"{precision_hex}"
                f"{trailer}"
            )
            payload = "".join(payload)
            _LOGGER.debug(
                f"set_fan_param: Final frame: {W_} --- {src_id} {fan_id} "
                f"--:------ 2411 {len(payload):03d} {payload}"
            )

            return cls._from_attrs(
                W_,
                Code._2411,
                PayloadT(payload),
                addr0=src_id,
                addr1=fan_id,
                addr2=NON_DEV_ADDR.id,
            )

        except (ValueError, TypeError) as err:
            raise exc.CommandInvalid(f"Invalid value: {value}") from err

    @classmethod
    def get_fan_param(
        cls: type[_T],
        fan_id: DeviceIdT | str,
        param_id: str,
        *,
        src_id: DeviceIdT | str,
    ) -> _T:
        if param_id is None:
            raise exc.CommandInvalid("Parameter ID cannot be None")

        if not isinstance(param_id, str):
            raise exc.CommandInvalid(
                f"Parameter ID must be a string, got {type(param_id).__name__}"
            )

        param_id_stripped = param_id.strip()
        if param_id != param_id_stripped:
            raise exc.CommandInvalid(
                f"Parameter ID cannot have leading or trailing whitespace: '{param_id}'"
            )

        try:
            if len(param_id) != 2:
                raise ValueError("Invalid length")
            int(param_id, 16)
        except ValueError as err:
            raise exc.CommandInvalid(
                f"Invalid parameter ID: '{param_id}'. "
                "Must be a 2-character hex string (00-FF)."
            ) from err

        payload = f"0000{param_id.upper()}"
        _LOGGER.debug(
            "Created get_fan_param command for %s from %s to %s",
            param_id,
            src_id,
            fan_id,
        )

        return cls._from_attrs(
            RQ, Code._2411, PayloadT(payload), addr0=src_id, addr1=fan_id
        )

    @classmethod
    def get_hvac_fan_31da(
        cls: type[_T],
        dev_id: DeviceIdT | str,
        hvac_id: str,
        bypass_position: float | None,
        air_quality: int | None,
        co2_level: int | None,
        indoor_humidity: float | None,
        outdoor_humidity: float | None,
        exhaust_temp: float | None,
        supply_temp: float | None,
        indoor_temp: float | None,
        outdoor_temp: float | None,
        speed_capabilities: list[str],
        fan_info: str,
        _unknown_fan_info_flags: list[int],
        exhaust_fan_speed: float | None,
        supply_fan_speed: float | None,
        remaining_mins: int | None,
        post_heat: int | None,
        pre_heat: int | None,
        supply_flow: float | None,
        exhaust_flow: float | None,
        **kwargs: Any,
    ) -> _T:
        air_quality_basis: str = kwargs.pop("air_quality_basis", "00")
        extra: str = kwargs.pop("_extra", "")
        assert not kwargs, kwargs

        payload = hvac_id
        payload += (
            f"{(int(air_quality * 200)):02X}" if air_quality is not None else "EF"
        )
        payload += (
            f"{air_quality_code(air_quality_basis)}"
            if air_quality_basis is not None
            else "00"
        )
        payload += f"{co2_level:04X}" if co2_level is not None else "7FFF"
        payload += (
            hex_from_percent(indoor_humidity, high_res=False)
            if indoor_humidity is not None
            else "EF"
        )
        payload += (
            hex_from_percent(outdoor_humidity, high_res=False)
            if outdoor_humidity is not None
            else "EF"
        )
        payload += hex_from_temp(exhaust_temp) if exhaust_temp is not None else "7FFF"
        payload += hex_from_temp(supply_temp) if supply_temp is not None else "7FFF"
        payload += hex_from_temp(indoor_temp) if indoor_temp is not None else "7FFF"
        payload += hex_from_temp(outdoor_temp) if outdoor_temp is not None else "7FFF"
        payload += (
            f"{capability_bits(speed_capabilities):04X}"
            if speed_capabilities is not None
            else "7FFF"
        )
        payload += (
            hex_from_percent(bypass_position, high_res=True)
            if bypass_position is not None
            else "EF"
        )
        payload += (
            f"{(fan_info_to_byte(fan_info) | fan_info_flags(_unknown_fan_info_flags)):02X}"
            if fan_info is not None
            else "EF"
        )
        payload += (
            hex_from_percent(exhaust_fan_speed, high_res=True)
            if exhaust_fan_speed is not None
            else "FF"
        )
        payload += (
            hex_from_percent(supply_fan_speed, high_res=True)
            if supply_fan_speed is not None
            else "FF"
        )
        payload += f"{remaining_mins:04X}" if remaining_mins is not None else "7FFF"
        payload += f"{int(post_heat * 200):02X}" if post_heat is not None else "EF"
        payload += f"{int(pre_heat * 200):02X}" if pre_heat is not None else "EF"
        payload += (
            f"{(int(supply_flow * 100)):04X}" if supply_flow is not None else "7FFF"
        )
        payload += (
            f"{(int(exhaust_flow * 100)):04X}" if exhaust_flow is not None else "7FFF"
        )
        payload += extra

        return cls._from_attrs(
            I_, Code._31DA, PayloadT(payload), addr0=dev_id, addr2=dev_id
        )

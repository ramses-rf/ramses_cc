#!/usr/bin/env python3
"""RAMSES RF - Protocol/Transport layer - Helper functions."""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime as dt
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

from ..ramses_rf.protocol.ramses import _31DA_FAN_INFO

from .address import hex_id_to_dev_id
from .const import (
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BYPASS_POSITION,
    SZ_CO2_LEVEL,
    SZ_DEVICE_CLASS,
    SZ_DEVICE_ID,
    SZ_DEWPOINT_TEMP,
    SZ_DOMAIN_IDX,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAULT_STATE,
    SZ_FAULT_TYPE,
    SZ_ZONE_DEMAND,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_LOG_IDX,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_REL_HUMIDITY,
    SZ_REMAINING_MINS,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_TEMPERATURE,
    SZ_TIMESTAMP,
    FaultDeviceClass,
    FaultState,
    FaultType,
)

if TYPE_CHECKING:
    from .typing import PayDictT

# Sensor faults
SZ_UNRELIABLE: Final = "unreliable"
SZ_TOO_HIGH: Final = "out_of_range_high"
SZ_TOO_LOW: Final = "out_of_range_low"
# Actuator, Valve/damper faults
SZ_STUCK_VALVE: Final = "stuck_valve"  # Damper/Valve jammed
SZ_STUCK_ACTUATOR: Final = "stuck_actuator"  # Actuator jammed
# Common (to both) faults
SZ_OPEN_CIRCUIT: Final = "open_circuit"
SZ_SHORT_CIRCUIT: Final = "short_circuit"
SZ_UNAVAILABLE: Final = "unavailable"
SZ_OTHER_FAULT: Final = "other_fault"  # Non-specific fault

DEVICE_FAULT_CODES = {
    0x0: SZ_OPEN_CIRCUIT,  # NOTE: open, short
    0x1: SZ_SHORT_CIRCUIT,
    0x2: SZ_UNAVAILABLE,
    0xD: SZ_STUCK_VALVE,
    0xE: SZ_STUCK_ACTUATOR,
    0xF: SZ_OTHER_FAULT,
}
SENSOR_FAULT_CODES = {
    0x0: SZ_SHORT_CIRCUIT,  # NOTE: short, open
    0x1: SZ_OPEN_CIRCUIT,
    0x2: SZ_UNAVAILABLE,
    0x3: SZ_TOO_HIGH,
    0x4: SZ_TOO_LOW,
    0x5: SZ_UNRELIABLE,
    # 0xF: SZ_OTHER_FAULT,  # No evidence is explicitly part of the specification
}


# TODO: consider returning from helpers as TypeGuard[HexByte]
# fmt: off
HexByteAlt = Literal[
    '00', '01', '02', '03', '04', '05', '06', '07', '08', '09', '0A', '0B', '0C', '0D', '0E', '0F',
    '10', '11', '12', '13', '14', '15', '16', '17', '18', '19', '1A', '1B', '1C', '1D', '1E', '1F',
    '20', '21', '22', '23', '24', '25', '26', '27', '28', '29', '2A', '2B', '2C', '2D', '2E', '2F',
    '30', '31', '32', '33', '34', '35', '36', '37', '38', '39', '3A', '3B', '3C', '3D', '3E', '3F',
    '40', '41', '42', '43', '44', '45', '46', '47', '48', '49', '4A', '4B', '4C', '4D', '4E', '4F',
    '50', '51', '52', '53', '54', '55', '56', '57', '58', '59', '5A', '5B', '5C', '5D', '5E', '5F',
    '60', '61', '62', '63', '64', '65', '66', '67', '68', '69', '6A', '6B', '6C', '6D', '6E', '6F',
    '70', '71', '72', '73', '74', '75', '76', '77', '78', '79', '7A', '7B', '7C', '7D', '7E', '7F',
    '80', '81', '82', '83', '84', '85', '86', '87', '88', '89', '8A', '8B', '8C', '8D', '8E', '8F',
    '90', '91', '92', '93', '94', '95', '96', '97', '98', '99', '9A', '9B', '9C', '9D', '9E', '9F',
    'A0', 'A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9', 'AA', 'AB', 'AC', 'AD', 'AE', 'AF',
    'B0', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B9', 'BA', 'BB', 'BC', 'BD', 'BE', 'BF',
    'C0', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9', 'CA', 'CB', 'CC', 'CD', 'CE', 'CF',
    'D0', 'D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7', 'D8', 'D9', 'DA', 'DB', 'DC', 'DD', 'DE', 'DF',
    'E0', 'E1', 'E2', 'E3', 'E4', 'E5', 'E6', 'E7', 'E8', 'E9', 'EA', 'EB', 'EC', 'ED', 'EE', 'EF',
    'F0', 'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'FA', 'FB', 'FC', 'FD', 'FE', 'FF'
]
# fmt: on

HexByte: TypeAlias = str
HexStr2: TypeAlias = str  # two characters, one byte
HexStr4: TypeAlias = str
HexStr8: TypeAlias = str
HexStr12: TypeAlias = str
HexStr14: TypeAlias = str


ReturnValueDictT: TypeAlias = Mapping[str, float | str | None]


class _FILE_TIME(ctypes.Structure):
    """Data structure for GetSystemTimePreciseAsFileTime()."""

    _fields_ = [("dwLowDateTime", ctypes.c_uint), ("dwHighDateTime", ctypes.c_uint)]


file_time = _FILE_TIME()


def timestamp() -> float:
    """Return the number of seconds since the Unix epoch.

    This function attempts to return a high-precision value, using specific
    system calls on Windows if available.
    :return: The current timestamp in seconds.
    :rtype: float
    """

    # see: https://www.python.org/dev/peps/pep-0564/
    if sys.platform == "win32":
        # Windows uses a different epoch (1601-01-01)
        ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(file_time))
        _time = (file_time.dwLowDateTime + (file_time.dwHighDateTime << 32)) / 1e7
        return float(_time - 134774 * 24 * 60 * 60)
    else:
        # Linux/macOS uses the Unix epoch (1970-01-01)
        return time.time_ns() / 1e9


def dt_now() -> dt:
    """Get the current datetime as a local/naive datetime object.

    This is slower, but potentially more accurate, than dt.now(), and is used mainly for
    packet timestamps.

    :return: The current local datetime.
    :rtype: dt
    """
    if sys.platform == "win32":
        return dt.fromtimestamp(timestamp())
    else:
        return dt.now()


def dt_str() -> str:
    """Return the current datetime as an isoformat string."""
    return dt_now().isoformat(timespec="microseconds")


####################################################################################################


def hex_to_bool(value: HexStr2) -> bool | None:  # either False, True or None
    """Convert a 2-char hex string into a boolean."""
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")
    if value == "FF":
        return None
    return {"00": False, "C8": True}[value]


def hex_from_bool(value: bool | None) -> HexStr2:  # either 00, C8 or FF
    """Convert a boolean into a 2-char hex string."""
    if value is None:
        return "FF"
    if not isinstance(value, bool):
        raise ValueError(f"Invalid value: {value}, is not bool")
    return {False: "00", True: "C8"}[value]


def hex_to_date(value: HexStr8) -> str | None:  # YY-MM-DD
    """Convert am 8-char hex string into a date, format YY-MM-DD."""
    if not isinstance(value, str) or len(value) != 8:
        raise ValueError(f"Invalid value: {value}, is not an 8-char hex string")
    if value == "FFFFFFFF":
        return None
    return dt(
        year=int(value[4:8], 16),
        month=int(value[2:4], 16),
        day=int(value[:2], 16) & 0b11111,  # 1st 3 bits: DayOfWeek
    ).strftime("%Y-%m-%d")


# FIXME: factor=1 should return an int
def hex_to_double(value: HexStr4, factor: int = 1) -> float | None:
    """Convert a 4-char hex string into a double."""
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")
    if value == "7FFF":
        return None
    return int(value, 16) / factor


def hex_from_double(value: float | None, factor: int = 1) -> HexStr4:
    """Convert a double into 4-char hex string."""
    if value is None:
        return "7FFF"
    if not isinstance(value, float | int):
        raise ValueError(f"Invalid value: {value}, is not a double (a float/int)")
    return f"{int(value * factor):04X}"


def hex_to_dtm(value: HexStr12 | HexStr14) -> str | None:  # from parsers
    """Convert a 12/14-char hex string to an isoformat datetime (naive, local)."""
    #        00141B0A07E3  (...HH:MM:00)    for system_mode, zone_mode (schedules?)
    #      0400041C0A07E3  (...HH:MM:SS)    for sync_datetime

    if not isinstance(value, str) or len(value) not in (12, 14):
        raise ValueError(f"Invalid value: {value}, is not a 12/14-char hex string")
    if value[-12:] == "FF" * 6:
        return None
    if len(value) == 12:
        value = f"00{value}"
    return dt(
        year=int(value[10:14], 16),
        month=int(value[8:10], 16),
        day=int(value[6:8], 16),
        hour=int(value[4:6], 16) & 0b11111,  # 1st 3 bits: DayOfWeek
        minute=int(value[2:4], 16),
        second=int(value[:2], 16) & 0b1111111,  # 1st bit: used for DST
    ).isoformat(timespec="seconds")


def hex_from_dtm(
    dtm: date | dt | str | None, is_dst: bool = False, incl_seconds: bool = False
) -> HexStr12 | HexStr14:
    """Convert a datetime (isoformat str, or naive dtm) to a 12/14-char hex str."""

    def _dtm_to_hex(year, mon, mday, hour, min, sec, *args: int) -> str:  # type: ignore[no-untyped-def]
        return f"{sec:02X}{min:02X}{hour:02X}{mday:02X}{mon:02X}{year:04X}"

    if dtm is None:
        return "FF" * (7 if incl_seconds else 6)
    if isinstance(dtm, str):
        dtm = dt.fromisoformat(dtm)
    dtm_str = _dtm_to_hex(*dtm.timetuple())  # TODO: add DST for tm_isdst
    if is_dst:
        dtm_str = f"{int(dtm_str[:2], 16) | 0x80:02X}" + dtm_str[2:]
    return dtm_str if incl_seconds else dtm_str[2:]


def hex_to_dts(value: HexStr12) -> str | None:
    """YY-MM-DD HH:MM:SS."""
    if not isinstance(value, str) or len(value) != 12:
        raise ValueError(f"Invalid value: {value}, is not a 12-char hex string")
    if value == "00000000007F":
        return None
    _seqx = int(value, 16)
    return dt(
        year=(_seqx & 0b1111111 << 24) >> 24,
        month=(_seqx & 0b1111 << 36) >> 36,
        day=(_seqx & 0b11111 << 31) >> 31,
        hour=(_seqx & 0b11111 << 19) >> 19,
        minute=(_seqx & 0b111111 << 13) >> 13,
        second=(_seqx & 0b111111 << 7) >> 7,
    ).strftime("%y-%m-%dT%H:%M:%S")


def hex_from_dts(dtm: dt | str | None) -> HexStr12:  # TODO: WIP
    """Convert a datetime (isoformat str, or dtm) to a packed 12-char hex str."""
    """YY-MM-DD HH:MM:SS."""
    if dtm is None:
        return "00000000007F"
    if isinstance(dtm, str):
        try:
            dtm = dt.strptime(dtm, "%y-%m-%dT%H:%M:%S")
        except ValueError:
            dtm = dt.fromisoformat(dtm)  # type: ignore[arg-type]

    (tm_year, tm_mon, tm_mday, tm_hour, tm_min, tm_sec, *_) = dtm.timetuple()
    result = sum(
        (
            tm_year % 100 << 24,
            tm_mon << 36,
            tm_mday << 31,
            tm_hour << 19,
            tm_min << 13,
            tm_sec << 7,
        )
    )
    return f"{result:012X}"


def hex_to_flag8(byte: HexByte, lsb: bool = False) -> list[int]:  # TODO: use tuple
    """Split a hex str (a byte) into a list of 8 bits, MSB as first bit by default.

    If lsb==True, then the LSB is first.
    The `lsb` boolean is used so that flag[0] is `zone_idx["00"]`, etc.
    """
    if not isinstance(byte, str) or len(byte) != 2:
        raise ValueError(f"Invalid value: '{byte}', is not a 2-char hex string")
    if lsb:  # make LSB is first bit
        return list((int(byte, 16) & (1 << x)) >> x for x in range(8))
    return list((int(byte, 16) & (1 << x)) >> x for x in reversed(range(8)))


def hex_from_flag8(flags: Iterable[int], lsb: bool = False) -> HexByte:
    """Convert list of 8 bits, MSB bit 1 by default, to a two-char ASCII hex string.

    The `lsb` boolean is used so that flag[0] is `zone_idx["00"]`, etc.
    """
    if not isinstance(flags, list) or len(flags) != 8:
        raise ValueError(f"Invalid value: '{flags}', is not a list of 8 bits")
    if lsb:  # LSB is first bit
        return f"{sum(x << idx for idx, x in enumerate(flags)):02X}"
    return f"{sum(x << idx for idx, x in enumerate(reversed(flags))):02X}"


# TODO: add a wrapper for EF, & 0xF0
def hex_to_percent(
    value: HexStr2, high_res: bool = True
) -> float | None:  # c.f. valve_demand
    """Convert a 2-char hex string into a percentage.

    The range is 0-100%, with resolution of 0.5% (high_res, 00-C8) or 1% (00-64).
    """
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")
    if value == "EF":  # TODO: when EF, when 7F?
        return None  # TODO: raise NotImplementedError
    if (raw_result := int(value, 16)) & 0xF0 == 0xF0:
        return None  # TODO: raise errors
    result = float(raw_result) / (200 if high_res else 100)
    if result > 1.0:  # move to outer wrapper
        raise ValueError(f"Invalid result: {result} (0x{value}) is > 1")
    return result


def hex_from_percent(value: float | None, high_res: bool = True) -> HexStr2:
    """Convert a percentage into a 2-char hex string.

    The range is 0-100%, with resolution of 0.5% (high_res, 00-C8) or 1% (00-64).
    """
    if value is None:
        return "EF"
    if not isinstance(value, float | int) or not 0 <= value <= 1:
        raise ValueError(f"Invalid value: {value}, is not a percentage")
    result = int(value * (200 if high_res else 100))
    return f"{result:02X}"


def hex_to_str(value: str) -> str:  # printable ASCII characters
    """Return a string of printable ASCII characters."""
    # result = bytearray.fromhex(value).split(b"\x7F")[0]  # TODO: needs checking
    if not isinstance(value, str):
        raise ValueError(f"Invalid value: {value}, is not a string")
    result = bytearray([x for x in bytearray.fromhex(value) if 31 < x < 127])
    return result.decode("ascii").strip() if result else ""


def hex_from_str(value: str) -> str:
    """Convert a string to a variable-length ASCII hex string."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid value: {value}, is not a string")
    return "".join(f"{ord(x):02X}" for x in value)  # or: value.encode().hex()


def hex_to_temp(value: HexStr4) -> bool | float | None:  # TODO: remove bool
    """Convert a 4-byte 2's complement hex string to a float temperature ('C).

    :param value: The 4-character hex string (e.g., '07D0')
    :type value: HexStr4
    :return: The temperature in Celsius, or None if N/A
    :rtype: float | None
    :raises ValueError: If input is not a 4-char hex string or temperature is invalid.
    """
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")
    if value == "31FF":  # means: N/A (== 127.99, 2s complement), signed?
        return None
    if value == "7EFF":  # possibly only for setpoints? unsigned?
        return False
    if value == "7FFF":  # also: FFFF?, means: N/A (== 327.67)
        return None
    temp: float = int(value, 16)
    temp = (temp if temp < 2**15 else temp - 2**16) / 100
    if temp < -273.15:
        raise ValueError(f"Invalid value: {temp} (0x{value}) is < -273.15")
    return temp


def hex_from_temp(value: bool | float | None) -> HexStr4:
    """Convert a float to a 2's complement 4-byte hex string."""
    if value is None:
        return "7FFF"  # or: "31FF"?
    if value is False:
        return "7EFF"
    if not isinstance(value, float | int):
        raise TypeError(f"Invalid temp: {value} is not a float")
    # if not -(2**7) <= value < 2**7:  # TODO: tighten range
    #     raise ValueError(f"Invalid temp: {value} is out of range")
    temp = int(value * 100)
    return f"{temp if temp >= 0 else temp + 2**16:04X}"


########################################################################################


def parse_fault_log_entry(
    payload: str,
) -> PayDictT.FAULT_LOG_ENTRY | PayDictT.FAULT_LOG_ENTRY_NULL:
    """Return the fault log entry."""

    assert len(payload) == 44

    # NOTE: the log_idx will increment as the entry moves down the log, hence '_log_idx'

    # these are only useful for I_, not RP
    if (timestamp := hex_to_dts(payload[18:30])) is None:
        return {f"_{SZ_LOG_IDX}": payload[4:6]}  # type: ignore[misc,return-value]

    result: PayDictT.FAULT_LOG_ENTRY = {
        f"_{SZ_LOG_IDX}": payload[4:6],  # type: ignore[misc]
        SZ_TIMESTAMP: timestamp,
        SZ_FAULT_STATE: FAULT_STATE.get(payload[2:4], FaultState.UNKNOWN),
        SZ_FAULT_TYPE: FAULT_TYPE.get(payload[8:10], FaultType.UNKNOWN),
        SZ_DOMAIN_IDX: payload[10:12],
        SZ_DEVICE_CLASS: FAULT_DEVICE_CLASS.get(
            payload[12:14], FaultDeviceClass.UNKNOWN
        ),
        SZ_DEVICE_ID: hex_id_to_dev_id(payload[38:]),
        "_unknown_3": payload[6:8],  # B0 ?priority
        "_unknown_7": payload[14:18],  # 0000
        "_unknown_15": payload[30:38],  # FFFF7000/1/2
    }

    return result


def _faulted_common(param_name: str, value: str) -> dict[str, str]:
    return {f"{param_name}_fault": f"invalid_{value}"}


def _faulted_sensor(param_name: str, value: str) -> dict[str, str]:
    # assert value[:1] in ("8", "F"), value
    code = int(value[:2], 16) & 0xF
    fault = SENSOR_FAULT_CODES.get(code, f"invalid_{value}")
    return {f"{param_name}_fault": fault}


def _faulted_device(param_name: str, value: str) -> dict[str, str]:
    assert value[:1] in ("8", "F"), value
    code = int(value[:2], 16) & 0xF
    fault: str = DEVICE_FAULT_CODES.get(code, f"invalid_{value}")
    return {f"{param_name}_fault": fault}


# TODO: refactor as per 31DA parsers
def parse_valve_demand(
    value: HexStr2,
) -> dict[str, float] | dict[str, str] | dict[str, None]:
    """Convert a 2-char hex string into a percentage.

    The range is 0-100%, with resolution of 0.5% (high_res) or 1%.
    """  # for a damper (restricts flow), or a valve (permits flow)

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "EF":
        return {SZ_ZONE_DEMAND: None}  # Not Implemented

    if int(value, 16) & 0xF0 == 0xF0:
        return _faulted_device(SZ_ZONE_DEMAND, value)

    result = int(value, 16) / 200  # c.f. hex_to_percent
    if result == 1.01:  # HACK - does it mean maximum?
        result = 1.0
    elif result > 1.0:
        raise ValueError(f"Invalid result: {result} (0x{value}) is > 1")

    return {SZ_ZONE_DEMAND: result}


AIR_QUALITY_BASIS: dict[str, str] = {
    "10": "voc",  # volatile compounds
    "20": "co2",  # carbon dioxide
    "40": "rel_humidity",  # relative humidity
}


# 31DA[2:6] and 12C8[2:6]
def parse_air_quality(value: HexStr4) -> PayDictT.AIR_QUALITY:
    """Return the air quality percentage (0.0 to 1.0) and its basis.

    The basis of the air quality level should be one of: VOC, CO2 or relative humidity.
    If air_quality is EF, air_quality_basis should be 00.

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.

    :param value: The 4-character hex string encoding quality and basis
    :type value: HexStr4
    :return: A dictionary containing the air quality and its basis (e.g., CO2, VOC)
    :rtype: PayDictT.AIR_QUALITY
    """  # VOC: Volatile organic compounds

    # TODO: remove this as API used only internally...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    assert value[:2] != "EF" or value[2:] == "00", value  # TODO: raise exception
    if value == "EF00":  # Not implemented
        return {SZ_AIR_QUALITY: None}

    if int(value[:2], 16) & 0xF0 == 0xF0:
        return _faulted_sensor(SZ_AIR_QUALITY, value)  # type: ignore[return-value]

    level = int(value[:2], 16) / 200  # was: hex_to_percent(value[:2], True)
    assert level <= 1.0, value[:2]  # TODO: raise exception

    assert value[2:] in ("10", "20", "40"), value[2:]  # TODO: remove assert

    basis: str = AIR_QUALITY_BASIS.get(
        value[2:], f"unknown_{value[2:]}"
    )  # TODO: remove get/unknown

    return {SZ_AIR_QUALITY: level, SZ_AIR_QUALITY_BASIS: basis}


def air_quality_code(desc: str) -> str:
    for k, v in AIR_QUALITY_BASIS.items():
        if v == desc:
            return k
    return "00"


# 31DA[6:10] and 1298[2:6]
def parse_co2_level(value: HexStr4) -> PayDictT.CO2_LEVEL:
    """Return the co2 level (ppm).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # Not implemented
        return {SZ_CO2_LEVEL: None}

    level = int(value, 16)  # was: hex_to_double(value)  # is it 2's complement?

    if int(value[:2], 16) & 0x80 or level >= 0x8000:
        return _faulted_sensor(SZ_CO2_LEVEL, value)  # type: ignore[return-value]

    # assert int(value[:2], 16) <= 0x8000, value
    return {SZ_CO2_LEVEL: level}


def parse_humidity_element(value: str, index: str) -> PayDictT._12A0:
    """Return the relative humidity (%) and 2 temperatures

    The result may include current temperature ('C) and include dewpoint temperature ('C).
    """
    if index == "01":
        return _parse_hvac_humidity(SZ_REL_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]
    if index == "02":
        return _parse_hvac_humidity(
            SZ_OUTDOOR_HUMIDITY, value[:2], value[2:6], value[6:10]
        )  # type: ignore[return-value]
    return _parse_hvac_humidity(SZ_INDOOR_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]


# 31DA[10:12] and 12A0[2:12]
def parse_indoor_humidity(value: str) -> PayDictT.INDOOR_HUMIDITY:
    """Return the relative indoor humidity (%).

    The result may include current temperature ('C), and dewpoint temperature ('C).
    """
    return _parse_hvac_humidity(SZ_INDOOR_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]


# 31DA[12:14] and 1280[2:12]
def parse_outdoor_humidity(value: str) -> PayDictT.OUTDOOR_HUMIDITY:
    """Return the relative outdoor humidity (%).

    The result may include current temperature ('C), and dewpoint temperature ('C).
    """
    return _parse_hvac_humidity(SZ_OUTDOOR_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]


def _parse_hvac_humidity(
    param_name: str, value: HexStr2, temp: HexStr4, dewpoint: HexStr4
) -> ReturnValueDictT:
    """Return the relative humidity, etc. (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")
    if not isinstance(temp, str) or len(temp) not in (0, 4):
        raise ValueError(f"Invalid temp: {temp}, is not a 4-char hex string")
    if not isinstance(dewpoint, str) or len(dewpoint) not in (0, 4):
        raise ValueError(f"Invalid dewpoint: {dewpoint}, is not a 4-char hex string")

    if value == "EF":  # Not implemented
        return {param_name: None}

    if int(value, 16) & 0xF0 == 0xF0:
        return _faulted_sensor(param_name, value)

    percentage = int(value[:2], 16) / 100
    if percentage > 1.0:  # seen regularly, unknown meaning
        return _faulted_common(param_name, value)

    result: dict[str, float | str | None] = {param_name: percentage}
    if temp:
        result |= {SZ_TEMPERATURE: hex_to_temp(temp)}
    if dewpoint:
        result |= {SZ_DEWPOINT_TEMP: hex_to_temp(dewpoint)}
    return result


# 31DA[14:18]
def parse_exhaust_temp(value: HexStr4) -> PayDictT.EXHAUST_TEMP:
    """Return the exhaust temperature ('C)."""
    return _parse_hvac_temp(SZ_EXHAUST_TEMP, value)  # type: ignore[return-value]


# 31DA[18:22]
def parse_supply_temp(value: HexStr4) -> PayDictT.SUPPLY_TEMP:
    """Return the supply temperature ('C)."""
    return _parse_hvac_temp(SZ_SUPPLY_TEMP, value)  # type: ignore[return-value]


# 31DA[22:26]
def parse_indoor_temp(value: HexStr4) -> PayDictT.INDOOR_TEMP:
    """Return the indoor temperature ('C)."""
    return _parse_hvac_temp(SZ_INDOOR_TEMP, value)  # type: ignore[return-value]


# 31DA[26:30] & 1290[2:6]?
def parse_outdoor_temp(value: HexStr4) -> PayDictT.OUTDOOR_TEMP:
    """Return the outdoor temperature ('C)."""
    return _parse_hvac_temp(SZ_OUTDOOR_TEMP, value)  # type: ignore[return-value]


def _parse_hvac_temp(param_name: str, value: HexStr4) -> Mapping[str, float | None]:
    """Return the temperature ('C) (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # Not implemented
        return {param_name: None}
    if value == "31FF":  # Other
        return {param_name: None}

    if int(value[:2], 16) & 0xF0 == 0x80:  # or temperature < -273.15:
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    temp: float = int(value, 16)
    temp = (temp if temp < 2**15 else temp - 2**16) / 100
    if temp <= -273:  # TODO: < 273.15?
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    return {param_name: temp}


ABILITIES = {
    15: "off",
    14: "low_med_high",  # 3,2,1 = high,med,low?
    13: "timer",
    12: "boost",
    11: "auto",
    10: "speed_4",
    9: "speed_5",
    8: "speed_6",
    7: "speed_7",
    6: "speed_8",
    5: "speed_9",
    4: "speed_10",
    3: "auto_night",
    2: "reserved",
    1: "post_heater",
    0: "pre_heater",
}


# 31DA[30:34]
def parse_capabilities(value: HexStr4) -> PayDictT.CAPABILITIES:
    """Return the speed capabilities (a bitmask).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # TODO: Not implemented???
        return {SZ_SPEED_CAPABILITIES: None}

    # assert value in ("0002", "4000", "4808", "F000", "F001", "F800", "F808"), value

    return {
        SZ_SPEED_CAPABILITIES: [
            v for k, v in ABILITIES.items() if int(value, 16) & 2**k
        ]
    }


def capability_bits(cap_list: list[str]) -> int:
    # 0xF800 = 0b1111100000000000
    cap_res: int = 0
    for cap in cap_list:
        for k, v in ABILITIES.items():
            if v == cap:
                cap_res |= 2**k  # set bit
    return cap_res


# 31DA[34:36]
def parse_bypass_position(value: HexStr2) -> PayDictT.BYPASS_POSITION:
    """Return the bypass position (%), usually fully open or closed (0%, no bypass).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "EF":  # Not implemented
        return {SZ_BYPASS_POSITION: None}

    if int(value[:2], 16) & 0xF0 == 0xF0:
        return _faulted_device(SZ_BYPASS_POSITION, value)  # type: ignore[return-value]

    bypass_pos = int(value, 16) / 200  # was: hex_to_percent(value)
    assert bypass_pos <= 1.0, value

    return {SZ_BYPASS_POSITION: bypass_pos}


# 31DA[36:38]  # TODO: WIP (3 more bits), also 22F3 and 22F4?
def parse_fan_info(value: HexStr2) -> PayDictT.FAN_INFO:
    """Return the fan state (lookup table for current speed and mode).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    # TODO: Not implemented???  # EF, FF = no data / not implemented
    if value in ("EF", "FF"):
        return {
            SZ_FAN_INFO: None,
            "_unknown_fan_info_flags": [0, 0, 0],
        }

    if int(value, 16) & 0xE0 not in (0x00, 0x20, 0x40, 0x60, 0x80):
        # Unknown fan_info code (e.g. Ventura 0x1F) — return as unknown
        # instead of crashing with AssertionError.  The quirks layer
        # will prevent it from overwriting a valid fan_info from 22F1/22F4.
        return {
            SZ_FAN_INFO: f"-unknown 0x{value}-",
            "_unknown_fan_info_flags": [
                (int(value, 16) >> x) & 1 for x in range(7, 4, -1)
            ],
        }

    flags = list((int(value, 16) & (1 << x)) >> x for x in range(7, 4, -1))

    return {
        SZ_FAN_INFO: _31DA_FAN_INFO[
            int(value, 16) & 0x1F
        ],  # lookup description from code
        "_unknown_fan_info_flags": flags,
    }


def fan_info_to_byte(info: str) -> int:
    for k, v in _31DA_FAN_INFO.items():
        if v == info:
            return int(k) & 0x1F
    return 0x0000


def fan_info_flags(flags_list: list[int]) -> int:
    flag_res: int = 0
    for index, shift in enumerate(range(7, 4, -1)):  # index = 7, 6 and 5
        if flags_list[index] == 1:
            flag_res |= 1 << shift  # set bits
    return flag_res


# 31DA[38:40], also 2210
def parse_exhaust_fan_speed(value: HexStr2) -> PayDictT.EXHAUST_FAN_SPEED:
    """Return the exhaust fan speed (% of max speed)."""
    return _parse_fan_speed(SZ_EXHAUST_FAN_SPEED, value)  # type: ignore[return-value]


# 31DA[40:42]
def parse_supply_fan_speed(value: HexStr2) -> PayDictT.SUPPLY_FAN_SPEED:
    """Return the supply fan speed (% of max speed)."""
    return _parse_fan_speed(SZ_SUPPLY_FAN_SPEED, value)  # type: ignore[return-value]


def _parse_fan_speed(param_name: str, value: HexStr2) -> Mapping[str, float | None]:
    """Return the fan speed (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "FF":  # Not implemented (is definitely FF, not EF!)
        return {param_name: None}

    percentage = int(value, 16) / 200  # was: hex_to_percent(value, True)
    if percentage > 1.0:
        return _faulted_common(param_name, value)  # type: ignore[return-value]

    return {param_name: percentage}


# 31DA[42:46] & 22F3[2:6]  # TODO: make 22F3-friendly
def parse_remaining_mins(value: HexStr4) -> PayDictT.REMAINING_MINUTES:
    """Return the remaining time for temporary modes (whole minutes).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "0000":
        return {SZ_REMAINING_MINS: 0}
    if value == "3FFF":
        return {SZ_REMAINING_MINS: None}

    minutes = int(value, 16)  # was: hex_to_double(value)
    assert minutes > 0, value  # TODO: raise assert

    return {SZ_REMAINING_MINS: minutes}  # usu. 0-60 mins


# 31DA[46:48]
def parse_post_heater(value: HexStr2) -> PayDictT.POST_HEATER:
    """Return the post-heater state (% of max heat)."""
    return _parse_fan_heater(SZ_POST_HEAT, value)  # type: ignore[return-value]


# 31DA[48:50]
def parse_pre_heater(value: HexStr2) -> PayDictT.PRE_HEATER:
    """Return the pre-heater state (% of max heat)."""
    return _parse_fan_heater(SZ_PRE_HEAT, value)  # type: ignore[return-value]


def _parse_fan_heater(param_name: str, value: HexStr2) -> Mapping[str, float | None]:
    """Return the heater state (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "EF":  # Not implemented
        return {param_name: None}

    if int(value, 16) & 0xF0 == 0xF0:
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    percentage = int(value, 16) / 200  # Siber DF EVO 2 is /200, not /100 (Others?)
    assert percentage <= 1.0, value  # TODO: raise exception if > 1.0?

    return {param_name: percentage}  # was: percent_from_hex(value, high_res=False)


# 31DA[50:54]
def parse_supply_flow(value: HexStr4) -> PayDictT.SUPPLY_FLOW:
    """Return the supply flow rate in m^3/hr (Orcon) ?or L/sec (?Itho)."""
    return _parse_fan_flow(SZ_SUPPLY_FLOW, value)  # type: ignore[return-value]


# 31DA[54:58]
def parse_exhaust_flow(value: HexStr4) -> PayDictT.EXHAUST_FLOW:
    """Return the exhaust flow rate in m^3/hr (Orcon) ?or L/sec (?Itho)"""
    return _parse_fan_flow(SZ_EXHAUST_FLOW, value)  # type: ignore[return-value]


def _parse_fan_flow(param_name: str, value: HexStr4) -> Mapping[str, float | None]:
    """Return the air flow rate (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # Not implemented
        return {param_name: None}

    if int(value[:2], 16) & 0x80:
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    flow = int(value, 16) / 100  # was: hex_to_double(value, factor=100)
    assert flow >= 0, value  # TODO: raise exception if < 0?

    return {param_name: flow}

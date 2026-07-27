"""RAMSES RF - HVAC and Ventilation payload parsers.

This module provides parsers for standard RAMSES RF packets related to
ventilation systems, fan speeds, air quality, and HVAC equipment (e.g.,
Itho, Orcon, Nuaire, ClimaRad).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, cast

from ...ramses_tx.address import NON_DEV_ADDR, hex_id_to_dev_id
from ...ramses_tx.const import (
    I_,
    RP,
    RQ,
    SZ_BYPASS_MODE,
    SZ_BYPASS_STATE,
    SZ_DEMAND,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FILTER_DIRTY,
    SZ_FROST_CYCLE,
    SZ_HAS_FAULT,
    SZ_MINUTES,
    SZ_MODE,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_PERCENT,
    SZ_REQ_REASON,
    SZ_SETPOINT_BOUNDS,
    SZ_TEMPERATURE,
    W_,
    Code,
)
from ...ramses_tx.helpers import (
    hex_to_flag8,
    hex_to_percent,
    hex_to_temp,
    parse_air_quality,
    parse_bypass_position,
    parse_capabilities,
    parse_co2_level,
    parse_exhaust_fan_speed,
    parse_exhaust_flow,
    parse_exhaust_temp,
    parse_fan_info,
    parse_humidity_element,
    parse_indoor_humidity,
    parse_indoor_temp,
    parse_outdoor_humidity,
    parse_outdoor_temp,
    parse_post_heater,
    parse_pre_heater,
    parse_remaining_mins,
    parse_supply_fan_speed,
    parse_supply_flow,
    parse_supply_temp,
)
from ...ramses_tx.typing import PayDictT

from ..protocol.ramses import _31D9_FAN_INFO_VASCO, _2411_PARAMS_SCHEMA
from .registry import register_parser

if TYPE_CHECKING:
    from ..messages import Message

_LOGGER = logging.getLogger(__name__)
_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"

_2411_TABLE = {k: v["description"] for k, v in _2411_PARAMS_SCHEMA.items()}


# unknown_01ff, to/from a Itho Spider/Thermostat
@register_parser("01FF")
def parser_01ff(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 01ff (Itho Spider) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of temperature, setpoint bounds, and flags
    :rtype: dict[str, Any]
    :raises AssertionError: If internal payload constraints are violated.
    """
    # see: https://github.com/zxdavb/ramses_rf/issues/73 & 101

    # lots of '80's, and I see temps are `int(payload[6:8], 16) / 2`
    # so I wonder if 0x80 is N/A? also is '7F'

    assert payload[:4] in ("0080", "0180"), f"{_INFORM_DEV_MSG} ({payload[:4]})"
    assert payload[12:14] == "00", f"{_INFORM_DEV_MSG} ({payload[12:14]})"
    assert payload[26:30] == "0000", f"{_INFORM_DEV_MSG} ({payload[26:30]})"
    assert payload[34:46] == "80800280FF80", f"{_INFORM_DEV_MSG} ({payload[34:46]})"

    if msg.verb in (I_, RQ):  # from Spider thermostat to gateway
        assert payload[14:16] == "80", f"{_INFORM_DEV_MSG} ({payload[14:16]})"
        assert payload[46:48] in ("04", "07"), f"{_INFORM_DEV_MSG} ({payload[46:48]})"

    if msg.verb in (RP, W_):  # from Spider gateway to thermostat
        assert payload[46:48] in (
            "00",
            "04",
            "07",
        ), f"{_INFORM_DEV_MSG} ({payload[46:48]})"

    setpoint_bounds = (
        int(payload[6:8], 16) / 2,  # as: 22C9[2:6] and [6:10] ???
        None if msg.verb in (RP, W_) else int(payload[8:10], 16) / 2,
    )

    return {
        SZ_TEMPERATURE: None if msg.verb in (RP, W_) else int(payload[4:6], 16) / 2,
        SZ_SETPOINT_BOUNDS: setpoint_bounds,
        "time_planning": not bool(int(payload[10:12], 16) & 1 << 6),
        "temp_adjusted": bool(int(payload[10:12], 16) & 1 << 5),
        "_flags_10": payload[10:12],  #
    }


# filter_change, HVAC
@register_parser("10D0")
def parser_10d0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 10d0 (filter_change) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of remaining days, lifetime, and percentage
    :rtype: dict[str, Any]
    """
    # 2022-07-03T22:52:34.571579 045  W --- 37:171871 32:155617 --:------ 10D0 002 00FF
    # 2022-07-03T22:52:34.596526 066  I --- 32:155617 37:171871 --:------ 10D0 006 0047B44F0000
    # then...
    # 2022-07-03T23:14:23.854089 000 RQ --- 37:155617 32:155617 --:------ 10D0 002 0000
    # 2022-07-03T23:14:23.876088 084 RP --- 32:155617 37:155617 --:------ 10D0 006 00B4B4C80000

    # _I only sent after _W=reset, must RQ to fetch current val
    # 00-FF resets the counter, 00-47-B4-4F-0000 is the value (71 180 79).
    # Default is 180 180 200. The returned value is the amount of days (180),
    # total amount of days till change (180), percentage (200)

    result: dict[str, bool | float | None]

    if msg.verb == W_:
        return {"reset_counter": payload[2:4] != "00"}

    result = {}

    if payload[2:4] not in ("FF", "FE"):
        result[SZ_REMAINING_DAYS] = int(payload[2:4], 16)

    if payload[4:6] not in ("FF", "FE"):
        result["days_lifetime"] = int(payload[4:6], 16)

    result[SZ_REMAINING_PERCENT] = hex_to_percent(payload[6:8])

    return result


# unknown_10e2 - HVAC
@register_parser("10E2")
def parser_10e2(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 10e2 (HVAC counter) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the extracted counter
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload length is not 6 or prefix is not '00'.
    """
    # .I --- --:------ --:------ 20:231151 10E2 003 00AD74  # every 2 minutes

    assert payload[:2] == "00", _INFORM_DEV_MSG
    assert len(payload) == 6, _INFORM_DEV_MSG

    return {
        "counter": int(payload[2:], 16),
    }


# HVAC: outdoor humidity
@register_parser("1280")
def parser_1280(payload: str, msg: Message) -> PayDictT._1280:
    """Parse the 1280 (outdoor_humidity) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the outdoor humidity percentage
    :rtype: PayDictT._1280
    """
    return parse_outdoor_humidity(payload[2:])


# HVAC: co2_level, see: 31DA[6:10]
@register_parser("1298")
def parser_1298(payload: str, msg: Message) -> PayDictT._1298:
    """Parse the 1298 (co2_level) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the CO2 level in PPM
    :rtype: PayDictT._1298
    """
    return parse_co2_level(payload[2:6])


# HVAC: indoor_humidity, array of 3 sets for HRU
@register_parser("12A0")
def parser_12a0(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 12a0 (indoor_humidity) packet.

    Restored to legacy list format to preserve historical log tests.
    Structural unpacking occurs in the CQRS quirks pipeline.
    """
    if len(payload) <= 14:
        return cast("dict[str, Any]", parse_indoor_humidity(payload[2:12]))

    return [
        {
            "hvac_idx": payload[i : i + 2],  # used as index
            **parse_humidity_element(payload[i + 2 : i + 12], payload[i : i + 2]),
            "_unknown_12": payload[i + 12 : i + 14],  # sporadic one of {00, 02}
        }
        for i in range(0, len(payload), 14)
    ]


# HVAC: air_quality (and air_quality_basis), see: 31DA[2:6]
@register_parser("12C8")
def parser_12c8(payload: str, msg: Message) -> PayDictT._12C8:
    """Parse the 12c8 (air_quality) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the air quality percentage and basis
    :rtype: PayDictT._12C8
    """
    return parse_air_quality(payload[2:6])


# programme_scheme, HVAC
@register_parser("1470")
def parser_1470(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 1470 (programme_scheme) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of the schedule scheme and daily setpoint count
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload format or constants are unrecognized.
    """
    # Seen on Orcon: see 1470, 1F70, 22B0

    SCHEDULE_SCHEME = {
        "9": "one_per_week",
        "A": "two_per_week",  # week_day, week_end
        "B": "one_each_day",  # seven_per_week (default?)
    }

    assert payload[8:10] == "80", _INFORM_DEV_MSG
    assert msg.verb == W_ or payload[4:8] == "0E60", _INFORM_DEV_MSG
    assert msg.verb == W_ or payload[10:] == "2A0108", _INFORM_DEV_MSG
    assert msg.verb != W_ or payload[4:] == "000080000000", _INFORM_DEV_MSG

    # schedule...
    # [2:3] - 1, every/all days, 1&6, weekdays/weekends, 1-7, each individual day
    # [3:4] - # setpoints/day (default 3)
    assert payload[2:3] in SCHEDULE_SCHEME and (
        payload[3:4] in ("2", "3", "4", "5", "6")
    ), _INFORM_DEV_MSG

    return {
        "scheme": SCHEDULE_SCHEME.get(payload[2:3], f"unknown_{payload[2:3]}"),
        "daily_setpoints": payload[3:4],
        "_value_4": payload[4:8],
        "_value_8": payload[8:10],
        "_value_10": payload[10:],
    }


# programme_config, HVAC
@register_parser("1F70")
def parser_1f70(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 1f70 (programme_config) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing schedule indices and start times
    :rtype: dict[str, Any]
    :raises AssertionError: If internal payload constraints are violated.
    """
    # Seen on Orcon: see 1470, 1F70, 22B0

    try:
        assert payload[:2] == "00", f"expected 00, not {payload[:2]}"
        assert payload[2:4] in ("00", "01"), f"expected (00|01), not {payload[2:4]}"
        assert payload[4:8] == "0800", f"expected 0800, not {payload[4:8]}"
        assert payload[10:14] == "0000", f"expected 0000, not {payload[10:14]}"
        assert msg.verb in (RQ, W_) or payload[14:16] == "15"
        assert msg.verb in (I_, RP) or payload[14:16] == "00"
        assert msg.verb == RQ or payload[22:24] == "60"
        assert msg.verb != RQ or payload[22:24] == "00"
        assert msg.verb == RQ or payload[24:26] in ("E4", "E5", "E6"), _INFORM_DEV_MSG
        assert msg.verb == RP or payload[26:] == "000000"
        assert msg.verb != RP or payload[26:] == "008000"

    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        "day_idx": payload[16:18],  # depends upon 1470[3:4]?
        "setpoint_idx": payload[8:10],  # needs to be mod 1470[3:4]?
        "start_time": f"{int(payload[18:20], 16):02d}:{int(payload[20:22], 16):02d}",
        "fan_speed_wip": payload[24:26],  # # E4/E5/E6   / 00(RQ)
        "_value_02": payload[2:4],  # # 00/01      / 00(RQ)
        "_value_04": payload[4:8],  # # 0800
        "_value_10": payload[10:14],  # 0000
        "_value_14": payload[14:16],  # 15(RP,I)   / 00(RQ,W)
        "_value_22": payload[22:24],  # 60         / 00(RQ)
        "_value_26": payload[26:],  # # 008000(RP) / 000000(I/RQ/W)
    }


# unknown_1fca, HVAC?
@register_parser("1FCA")
def parser_1fca(payload: str, msg: Message) -> Mapping[str, str]:
    """Parse the 1fca packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A mapping of unknown identifiers and associated device IDs
    :rtype: Mapping[str, str]
    """
    # .W --- 30:248208 34:021943 --:------ 1FCA 009 00-01FF-7BC990-FFFFFF  # sent x2

    return {
        "_unknown_0": payload[:2],
        "_unknown_1": payload[2:6],
        "device_id_0": hex_id_to_dev_id(payload[6:12]),
        "device_id_1": hex_id_to_dev_id(payload[12:]),
    }


# WIP: HVAC auto requests (confirmed for Orcon, others?)
@register_parser("2210")
def parser_2210(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2210 (HVAC auto request) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary of fan speed, request reason, and unknown flags
    :rtype: dict[str, Any]
    :raises AssertionError: If payload constants or internal consistency checks fail.
    """
    try:
        assert msg.verb in (RP, I_) or payload == "00"
        assert payload[10:12] == payload[38:40], (
            f"expected byte 19 {payload[10:12]}, not {payload[38:40]}"
        )  # auto requested fan speed %. Identical [38:40] is for supply?
        assert payload[20:22] == payload[48:50] and payload[20:22] in (
            "00",  # idle
            "02",  # requested by CO2 level/sensor
            "03",  # requested by humidity level/sensor
        ), f"expected req_reason (00|02|03), not {payload[20:22]}"
        assert payload[78:80] in (
            "00",
            "02",
        ), f"expected byte 39 (00|02), not {payload[78:80]}"
        assert payload[80:82] in (
            "01",
            "08",
            "0C",  # seen on Orcon HCR-400 EcoMax
        ), f"expected byte 40 (01|08), not {payload[80:82]}"
        assert payload[82:] in (
            "00",
            "40",
        ), f"expected byte 41- (00|40), not {payload[82:]}"

    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    _req = "IDL"
    if payload[20:22] == "02":
        _req = "CO2"
    elif payload[20:22] == "03":
        _req = "HUM"

    return {
        **parse_exhaust_fan_speed(
            payload[10:12]
        ),  # for Orcon: 29 hex == 41 decimal divided by 2 gives 20.5 (%)
        SZ_REQ_REASON: _req,
        "unknown_78": payload[78:80],
        "unknown_80": payload[80:82],
        "unknown_82": payload[82:],
    }


# program_enabled, HVAC
@register_parser("22B0")
def parser_22b0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 22b0 (program_enabled) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the program enabled status
    :rtype: dict[str, Any]
    """
    # Seen on Orcon: see 1470, 1F70, 22B0

    # .W --- 37:171871 32:155617 --:------ 22B0 002 0005  # enable, calendar on
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0005

    # .W --- 37:171871 32:155617 --:------ 22B0 002 0006  # disable, calendar off
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0006

    return {
        "enabled": {"06": False, "05": True}.get(payload[2:4]),
    }


# WIP: unknown, HVAC
@register_parser("22E0")
def parser_22e0(payload: str, msg: Message) -> Mapping[str, float | None]:
    """Parse the 22e0 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A mapping of percentage values extracted from the payload
    :rtype: Mapping[str, float | None]
    :raises AssertionError: If a value exceeds the expected 200 threshold.
    :raises ValueError: If the payload cannot be parsed as percentages.
    """

    # RP --- 32:155617 18:005904 --:------ 22E0 004 00-34-A0-1E
    # RP --- 32:153258 18:005904 --:------ 22E0 004 00-64-A0-1E
    def _parser(seqx: str) -> float:
        assert int(seqx, 16) <= 200 or seqx == "E6"  # only for 22E0, not 22E5/22E9
        return int(seqx, 16) / 200

    try:
        return {
            f"percent_{i}": hex_to_percent(payload[i : i + 2])
            for i in range(2, len(payload), 2)
        }
    except ValueError:
        return {
            "percent_2": hex_to_percent(payload[2:4]),
            "percent_4": _parser(payload[4:6]),
            "percent_6": hex_to_percent(payload[6:8]),
        }


# WIP: unknown, HVAC
@register_parser("22E5")
def parser_22e5(payload: str, msg: Message) -> Mapping[str, float | None]:
    """Parse the 22e5 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A mapping of percentage values extracted from the payload
    :rtype: Mapping[str, float | None]
    """
    # RP --- 32:153258 18:005904 --:------ 22E5 004 00-96-C8-14
    # RP --- 32:155617 18:005904 --:------ 22E5 004 00-72-C8-14

    return cast("Mapping[str, float | None]", parser_22e0(payload, msg))


# WIP: unknown, HVAC
@register_parser("22E9")
def parser_22e9(payload: str, msg: Message) -> Mapping[str, float | str | None]:
    """Parse the 22e9 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A mapping of unknown identifiers or percentage values
    :rtype: Mapping[str, float | str | None]
    """
    if payload[2:4] == "01":
        return {
            "unknown_4": payload[4:6],
            "unknown_6": payload[6:8],
        }
    return cast("Mapping[str, float | str | None]", parser_22e0(payload, msg))


# fan_speed (switch_mode), HVAC
@register_parser("22F1")
def parser_22f1(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 22f1 (fan_speed) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the fan mode, scheme, and internal indices
    :rtype: dict[str, Any]
    :raises AssertionError: If the fan mode or mode set is unrecognized.
    """
    try:
        assert payload[0:2] in ("00", "63")
        assert not payload[4:] or int(payload[2:4], 16) <= int(payload[4:], 16), (
            "mode_idx > mode_max"
        )
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    # Scheme detection: the mode_max byte (payload[4:6]) is the primary
    # indicator.  mode_max=04 → itho (5 modes: 00-04), mode_max=0A → nuaire,
    # mode_max=06 → vasco, and mode_max 07/0B/empty → orcon (8 modes: 00-07).
    #
    # Previously, mode_max=04 was only treated as itho when the source was
    # the broadcast address (NON_DEV_ADDR).  Directed 22F1 packets from Itho
    # remotes (e.g. CVE-S CO2 with AUTO RFT-N, see issue #87) were therefore
    # misidentified as orcon, causing fan_mode names to be wrong
    # (02→"medium" instead of "low", 04→"auto" instead of "high", etc.).
    #
    # The mode_max=04 → itho detection is scoped to standalone 22F1 packets
    # (msg.code == 22F1) to avoid affecting 22F3 boost payloads, which embed
    # 22F1-style mode info but may use different mode_max conventions.
    is_standalone_22f1 = getattr(msg, "code", None) == Code._22F1
    if is_standalone_22f1 and (
        payload[4:6] == "04" or (msg._addrs[0] == NON_DEV_ADDR and not payload[4:6])
    ):  # broadcast, 2-byte → itho
        from ..protocol.ramses import _22F1_MODE_ITHO as _22F1_FAN_MODE

        _22f1_mode_set: tuple[str, ...] = ("", "04")
        _22f1_scheme = "itho"

    elif payload[4:6] == "0A":
        from ..protocol.ramses import _22F1_MODE_NUAIRE as _22F1_FAN_MODE

        _22f1_mode_set = ("", "0A")
        _22f1_scheme = "nuaire"

    elif payload[4:6] == "06":
        from ..protocol.ramses import _22F1_MODE_VASCO as _22F1_FAN_MODE

        _22f1_mode_set = (
            "",
            "00",
            "06",
        )  # "00" seen incidentally on a ClimaRad 4-button remote: OFF?
        _22f1_scheme = "vasco"

    else:
        from ..protocol.ramses import _22F1_MODE_ORCON as _22F1_FAN_MODE

        _22f1_mode_set = ("", "07", "0B")  # 0B?
        _22f1_scheme = "orcon"

    try:
        assert payload[2:4] in _22F1_FAN_MODE, f"unknown fan_mode: {payload[2:4]}"
        assert payload[4:6] in _22f1_mode_set, f"unknown mode_set: {payload[4:6]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        SZ_FAN_MODE: _22F1_FAN_MODE.get(payload[2:4], f"unknown_{payload[2:4]}"),
        "_scheme": _22f1_scheme,
        "_mode_idx": f"{int(payload[2:4], 16) & 0x0F:02X}",
        "_mode_max": payload[4:6] or None,
    }


# WIP: unknown, HVAC (flow rate?)
@register_parser("22F2")
def parser_22f2(payload: str, msg: Message) -> list[dict[str, Any]]:
    """Parse the 22f2 (HVAC flow rate) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A list of dictionaries containing HVAC indices and measurements
    :rtype: list[dict[str, Any]]
    """
    # ClimeRad minibox uses 22F2 for speed feedback

    def _parser(seqx: str) -> dict[str, Any]:
        assert seqx[:2] in ("00", "01"), f"is {seqx[:2]}, expecting 00/01"

        return {
            "hvac_idx": seqx[:2],
            "measure": hex_to_temp(seqx[2:]),
        }

    return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]


# fan_boost, HVAC
@register_parser("22F3")
def parser_22f3(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 22f3 (fan_boost) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of boost settings, duration, and fan modes
    :rtype: dict[str, Any]
    :raises AssertionError: If internal payload structure is malformed.
    """
    # NOTE: for boost timer for high
    try:
        assert msg.len <= 7 or payload[14:] == "0000", f"byte 7: {payload[14:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    new_speed = {  # from now, until timer expiry
        0x00: "fan_boost",  # set fan off, or 'boost' mode?
        0x01: "per_request?",  # set fan as per payload[6:10]?
        0x02: "per_request",  # set fan as per payload[6:10]
    }.get(int(payload[2:4], 0x10) & 0x07)  # 0b0000-0111

    fallback_speed = {  # after timer expiry
        0x00: "per_vent_speed",  # set fan as per current fan mode
        0x08: "fan_off",  # set fan off?
        0x10: "per_request",  # set fan as per payload[10:14]
        0x18: "per_vent_speed?",  # set fan as per current fan mode/speed?
    }.get(int(payload[2:4], 0x10) & 0x38)  # 0b0011-1000

    units = {
        0x00: SZ_MINUTES,
        0x40: "hours",
        0x80: "index",  # TODO: days, day-of-week, day-of-month?
    }.get(int(payload[2:4], 0x10) & 0xC0)  # 0b1100-0000

    duration = int(payload[4:6], 16) * 60 if units == "hours" else int(payload[4:6], 16)
    result = {}

    if msg.len >= 3:
        result = {
            SZ_MINUTES if units != "index" else "index": duration,
            "flags": hex_to_flag8(payload[2:4]),
            "new_speed_mode": new_speed,
            "fallback_speed_mode": fallback_speed,
        }

    if msg._addrs[0] == NON_DEV_ADDR and msg.len <= 3:
        result["_scheme"] = "itho"

    if msg.len >= 5 and payload[6:10] != "0000":  # new speed
        mode_info = parser_22f1(f"00{payload[6:10]}", msg)
        result["_scheme"] = mode_info.get("_scheme")
        result["fan_mode"] = mode_info.get("fan_mode")

    if msg.len >= 7 and payload[10:14] != "0000":  # fallback speed
        mode_info = parser_22f1(f"00{payload[10:14]}", msg)
        result["fallback_fan_mode"] = mode_info.get("fan_mode")

    return result


# WIP: unknown, HVAC
@register_parser("22F4")
def parser_22f4(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 22f4 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing interpreted fan mode and rate
    :rtype: dict[str, Any]
    :raises AssertionError: If the extracted mode or rate is invalid.
    """
    if msg.len == 13 and payload[14:] == "000000000000":
        # ClimaRad Ventura fan & remote
        _pl = payload[:4] + payload[12:14] if payload[10:12] == "00" else payload[8:14]
    else:
        _pl = payload[:6]

    MODE_LOOKUP = {
        0x00: "off",
        0x20: "paused",
        0x40: "auto",
        0x60: "manual",
    }
    mode = int(_pl[2:4], 16) & 0x60
    assert mode in MODE_LOOKUP, mode

    RATE_LOOKUP = {
        0x00: "speed 0",  # "off"?,
        0x01: "speed 1",  # "low", or trickle?
        0x02: "speed 2",  # "medium-low", or low?
        0x03: "speed 3",  # "medium",
        0x04: "speed 4",  # "medium-high", or high?
        0x05: "boost",  # "boost", aka purge?
    }
    rate = int(_pl[4:6], 16) & 0x03
    assert mode != 0x60 or rate in RATE_LOOKUP, rate

    return {
        SZ_FAN_MODE: MODE_LOOKUP[mode],
        SZ_FAN_RATE: RATE_LOOKUP.get(rate),
    }


# bypass_mode, HVAC
@register_parser("22F7")
def parser_22f7(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 22f7 (bypass_mode) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of bypass mode, state, and position
    :rtype: dict[str, Any]
    """
    result = {
        SZ_BYPASS_MODE: {"00": "off", "C8": "on", "FF": "auto"}.get(payload[2:4]),
    }
    if msg.verb != W_ or payload[4:] not in ("", "EF"):
        result[SZ_BYPASS_STATE] = {"00": "off", "C8": "on"}.get(payload[4:])
        result.update(**parse_bypass_position(payload[4:]))  # type: ignore[arg-type]

    return result


# WIP: unknown_mode, HVAC
@register_parser("22F8")
def parser_22f8(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 22f8 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of raw internal values
    :rtype: dict[str, Any]
    """
    # message command bytes specific for AUTO RFT (536-0150)
    # ithoMessageAUTORFTAutoNightCommandBytes[] = {0x22, 0xF8, 0x03, 0x63, 0x02, 0x03};
    # .W --- 32:111111 37:111111 --:------ 22F8 003 630203

    return {
        "value_02": payload[2:4],
        "value_04": payload[4:6],
    }


# fan_params, HVAC
@register_parser("2411")
def parser_2411(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2411 (fan_params) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the parameter ID, description, and decoded value
    :rtype: dict[str, Any]
    """

    def counter(x: str) -> int:
        return int(x, 16)

    def centile(x: str) -> float:
        return int(x, 16) / 10

    _2411_DATA_TYPES = {
        "00": (2, counter),  # 4E (0-1), 54 (15-60)
        "01": (2, centile),  # 52 (0.0-25.0) (%)
        "0F": (2, hex_to_percent),  # xx (0.0-1.0) (%)
        "10": (4, counter),  # 31 (0-1800) (days)
        "13": (4, counter),  # 31 - ClimaRad Ventura filter time (variant)
        "20": (4, counter),  # 01 - Support parameter
        "90": (4, counter),  # 3E - Away mode Exhaust fan rate (%)
        "92": (4, hex_to_temp),  # 75 (0-30) (C)
    }

    param_id = payload[4:6]
    try:
        description = _2411_TABLE.get(param_id, "Unknown")
        if param_id not in _2411_TABLE:
            _LOGGER.warning(
                f"2411 message received with unknown parameter ID: {param_id}. "
                f"This parameter is not in the known parameter schema. "
                f"Message: {msg!r}"
            )
    except Exception as err:
        _LOGGER.warning(f"Error looking up 2411 parameter {param_id}: {err}")
        description = "Unknown"

    result = {
        "parameter": param_id,
        "description": description,
    }

    if msg.verb == RQ:
        return result

    try:
        if payload[8:10] not in _2411_DATA_TYPES:
            warningmsg = (
                f"{msg!r} < {_INFORM_DEV_MSG} "
                f"(param {param_id} has unknown data_type: {payload[8:10]}). "
                "This parameter uses an unrecognized data type."
            )
            if msg.len == 9:
                result |= {
                    "value": f"0x{payload[10:18]}",
                    "_value_06": payload[6:10],
                    "_unknown_data_type": payload[8:10],
                }
            else:
                result |= {
                    "value": f"0x{payload[10:18]}",
                    "_value_06": payload[6:10],
                    "min_value": f"0x{payload[18:26]}",
                    "max_value": f"0x{payload[26:34]}",
                    "precision": f"0x{payload[34:42]}",
                    "_value_42": payload[42:],
                }
            _LOGGER.warning(f"{warningmsg} Found values: {result}")
            return result

        length, parser = _2411_DATA_TYPES[payload[8:10]]
        result |= {
            "value": parser(payload[10:18][-length:]),  # type: ignore[operator]
            "_value_06": payload[6:10],
        }

        if msg.len == 9:
            return result

        return (
            result
            | {
                "min_value": parser(payload[18:26][-length:]),  # type: ignore[operator]
                "max_value": parser(payload[26:34][-length:]),  # type: ignore[operator]
                "precision": parser(payload[34:42][-length:]),  # type: ignore[operator]
                "_value_42": payload[42:],
            }
        )
    except Exception as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} (Error parsing 2411: {err})")
        result["value"] = f"0x{payload[10:18]}"
        result["_parse_error"] = f"Parser error: {err}"
        return result


# ufc_demand, HVAC (Itho autotemp / spider)
@register_parser("3110")
def parser_3110(payload: str, msg: Message) -> PayDictT._3110:
    """Parse the 3110 (ufc_demand) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the operating mode and demand percentage
    :rtype: PayDictT._3110
    :raises AssertionError: If payload constants or demand values are invalid.
    """
    # .I --- 02:250708 --:------ 02:250708 3110 004 0000C820  # cooling, 100%
    # .I --- 21:042656 --:------ 21:042656 3110 004 00000010  # heating, 0%

    SZ_COOLING = "cooling"
    SZ_DISABLE = "disabled"
    SZ_HEATING = "heating"
    SZ_UNKNOWN = "unknown"

    try:
        assert payload[2:4] == "00", f"byte 1: {payload[2:4]}"  # ?circuit_idx?
        assert int(payload[4:6], 16) <= 200, f"byte 2: {payload[4:6]}"
        assert payload[6:] in ("00", "10", "20"), f"byte 3: {payload[6:]}"
        assert payload[6:] in ("10", "20") or payload[4:6] == "00", (
            f"byte 3: {payload[6:]}"
        )
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    mode = {
        0x00: SZ_DISABLE,
        0x10: SZ_HEATING,
        0x20: SZ_COOLING,
    }.get(int(payload[6:8], 16) & 0x30, SZ_UNKNOWN)

    if mode not in (SZ_COOLING, SZ_HEATING):
        return {SZ_MODE: mode}

    return {SZ_MODE: mode, SZ_DEMAND: hex_to_percent(payload[4:6])}


# unknown_3120, from STA, FAN
@register_parser("3120")
def parser_3120(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 3120 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of raw internal segments
    :rtype: dict[str, Any]
    :raises AssertionError: If individual byte segments fail validation.
    """
    # .I --- 34:136285 --:------ 34:136285 3120 007 0070B0000000FF  # every ~3:45:00!
    # RP --- 20:008749 18:142609 --:------ 3120 007 0070B000009CFF
    # .I --- 37:258565 --:------ 37:258565 3120 007 0080B0010003FF

    try:
        assert payload[:2] == "00", f"byte 0: {payload[:2]}"
        assert payload[2:4] in ("00", "70", "80"), f"byte 1: {payload[2:4]}"
        assert payload[4:6] == "B0", f"byte 2: {payload[4:6]}"
        assert payload[6:8] in ("00", "01"), f"byte 3: {payload[6:8]}"
        assert payload[8:10] == "00", f"byte 4: {payload[8:10]}"
        assert payload[10:12] in ("00", "03", "0A", "9C"), f"byte 5: {payload[10:12]}"
        assert payload[12:] == "FF", f"byte 6: {payload[12:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        "unknown_0": payload[2:10],
        "unknown_5": payload[10:12],
        "unknown_2": payload[12:],
    }


# WIP: unknown, HVAC
@register_parser("313E")
def parser_313e(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 313e packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing calculated Zulu time and raw internal values
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload prefix or expected constant suffix is invalid.
    """
    assert payload[:2] == "00"
    assert payload[12:] == "003C800000"

    result = (
        msg.dtm - td(seconds=int(payload[10:12], 16), minutes=int(payload[2:10], 16))
    ).isoformat()

    return {
        "zulu": result,
        "value_02": payload[2:10],
        "value_10": payload[10:12],
        "value_12": payload[12:],
    }


# fan state (ventilation status), HVAC
@register_parser("31D9")
def parser_31d9(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 31d9 (fan state) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing fan mode, speed, and status flags
    :rtype: dict[str, Any]
    :raises AssertionError: If payload constants or byte segments fail validation.
    """
    # NOTE: Itho and ClimaRad use 0x00-C8 for %, whilst Nuaire uses 0x00-64
    try:
        assert payload[4:6] == "FF" or int(payload[4:6], 16) <= 200, (
            f"byte 2: {payload[4:6]}"
        )
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    bitmap = int(payload[2:4], 16)

    # NOTE: 31D9[4:6] is fan_speed (ClimaRad minibox, Itho) *or* fan_mode (Orcon, Vasco)
    result = {
        SZ_FAN_MODE: payload[4:6],  # orcon, vasco/climarad
        "passive": bool(bitmap & 0x02),
        "damper_only": bool(bitmap & 0x04),  # i.e. valve only
        SZ_FILTER_DIRTY: bool(bitmap & 0x20),
        SZ_FROST_CYCLE: bool(bitmap & 0x40),
        SZ_HAS_FAULT: bool(bitmap & 0x80),
        "_flags": hex_to_flag8(payload[2:4]),
    }

    # Orcon and Brofer long payloads provide accurate fan speed in 31DA.
    # Emitting exhaust_fan_speed here incorrectly converts mode bytes to speed.
    # Itho formats end with "00", whereas Orcon/Brofer formats end with "04" or "08".
    _has_exhaust_fan_speed = (
        len(payload) < 34 or payload[8:32] != "20" * 12 or payload[32:34] == "00"
    )
    if _has_exhaust_fan_speed:
        result.update(parse_exhaust_fan_speed(payload[4:6]))  # for itho, nuaire

    # Fan Mode Lookup 1 for Vasco codes
    if msg.len == 3:  # usu: I -->20: (no seq#)
        if (
            (payload[:4] == "0000" or payload[:4] == "0080")  # Senza, meaning of 0x80?
            and msg._addrs[0] == msg._addrs[2]
            and msg._addrs[1] == NON_DEV_ADDR
        ):
            # _31D9_FAN_INFO for Vasco D60 HRU and ClimaRad Minibox, S-Fan
            try:
                assert int(payload[4:6], 16) & 0xFF in _31D9_FAN_INFO_VASCO, (
                    f"unknown 31D9 fan_mode lookup key: {payload[4:6]}"
                )
            except AssertionError as err:
                _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")
            fan_mode = _31D9_FAN_INFO_VASCO.get(
                int(payload[4:6], 16) & 0xFF, f"unknown_{payload[4:6]}"
            )
            result[SZ_FAN_MODE] = fan_mode
        return result

    try:
        assert payload[6:8] in ("00", "07", "0A", "FE"), f"byte 3: {payload[6:8]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    result.update({"_unknown_3": payload[6:8]})

    if msg.len == 4:  # usu: I -->20: (no seq#)
        return result

    try:
        assert payload[8:32] in ("00" * 12, "20" * 12), f"byte 4: {payload[8:32]}"
        assert payload[32:] in ("00", "04", "08"), f"byte 16: {payload[32:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        **result,
        "_unknown_4": payload[8:32],
        "unknown_16": payload[32:],
    }


# ventilation state (extended), HVAC
@register_parser("31DA")
def parser_31da(payload: str, msg: Message) -> PayDictT._31DA:
    """Parse the 31da (extended ventilation state) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of all decoded ventilation parameters
    :rtype: PayDictT._31DA
    """
    result = {
        **parse_exhaust_fan_speed(payload[38:40]),  # maybe 31D9[4:6] for some?
        **parse_fan_info(payload[36:38]),  # 22F3-ish
        **parse_air_quality(payload[2:6]),  # 12C8[2:6]
        **parse_co2_level(payload[6:10]),  # 1298[2:6]
        **parse_indoor_humidity(payload[10:12]),  # 12A0?
        **parse_outdoor_humidity(payload[12:14]),
        **parse_exhaust_temp(payload[14:18]),  # to outside
        **parse_supply_temp(payload[18:22]),  # to home
        **parse_indoor_temp(payload[22:26]),  # in home
        **parse_outdoor_temp(payload[26:30]),  # 1290?
        **parse_capabilities(payload[30:34]),
        **parse_bypass_position(payload[34:36]),  # 22F7-ish
        **parse_supply_fan_speed(payload[40:42]),
        **parse_remaining_mins(payload[42:46]),  # mins, ~22F3[2:6]
        **parse_post_heater(payload[46:48]),
        **parse_pre_heater(payload[48:50]),
        **parse_supply_flow(payload[50:54]),  # NOTE: is supply, not exhaust
        **parse_exhaust_flow(payload[54:58]),  # NOTE: order switched from others
    }
    if len(payload) == 58:
        return result  # type: ignore[return-value]

    result.update(
        {"_extra": payload[58:]}
    )  # sporadic [58:60] one of {00, 20, 40} version?
    return result  # type: ignore[return-value]

    # From an Orcon 15RF Display
    #  1 Software version
    #  4 RH value in home (%)                 SZ_INDOOR_HUMIDITY
    #  5 RH value supply air (%)              SZ_OUTDOOR_HUMIDITY
    #  6 Exhaust air temperature out (°C)     SZ_EXHAUST_TEMPERATURE
    #  7 Supply air temperature to home (°C)  SZ_SUPPLY_TEMPERATURE
    #  8 Temperature from home (°C)           SZ_INDOOR_TEMPERATURE
    #  9 Temperature outside (°C)             SZ_OUTDOOR_TEMPERATURE
    # 10 Bypass position                      SZ_BYPASS_POSITION
    # 11 Exhaust fan speed (%)                SZ_EXHAUST_FAN_SPEED
    # 12 Fan supply speed (%)                 SZ_SUPPLY_FAN_SPEED
    # 13 Remaining after run time (min.)      SZ_REMAINING_TIME - for humidity scenario
    # 14 Preheater control (MaxComfort) (%)   SZ_PRE_HEAT
    # 16 Actual supply flow rate (m3/h)       SZ_SUPPLY_FLOW (Orcon is m3/h, data is L/s)
    # 17 Current discharge flow rate (m3/h)   SZ_EXHAUST_FLOW


# vent_demand, HVAC
@register_parser("31E0")
def parser_31e0(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 31e0 (vent_demand) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary or list of dictionaries containing flags and demand percentage
    :rtype: dict[str, Any] | list[dict[str, Any]]
    :raises AssertionError: If the payload suffix is not a recognized constant.
    """
    if payload == "00":
        return {}

    # .I --- 37:005302 32:132403 --:------ 31E0 008 00-0000-00 01-0064-00
    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-0000
    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-00C8

    def _parser(seqx: str) -> dict[str, Any]:
        assert seqx[6:] in ("", "00", "FF")
        return {
            "flags": seqx[2:4],
            "vent_demand": hex_to_percent(seqx[4:6]),
            "_unknown_3": payload[6:],
        }

    if len(payload) > 8:
        return [_parser(payload[x : x + 8]) for x in range(0, len(payload), 8)]
    return _parser(payload)


# timestamp, HVAC
@register_parser("4401")
def parser_4401(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4401 (HVAC timestamp) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of source/destination timestamps and update flags
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload format or constants are invalid.
    """
    if msg.verb == RP:
        return {}

    # 2022-07-28T14:21:38.895354 095  W --- 37:010164 37:010151 --:------ 4401 020 10  7E-E99E90C8  00-E99E90C7-3BFF  7E-E99E90C8-000B
    # 2022-07-28T14:21:57.414447 076 RQ --- 20:225479 20:257336 --:------ 4401 020 10  2E-E99E90DB  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:21:57.625474 045  I --- 20:257336 20:225479 --:------ 4401 020 10  2E-E99E90DB  00-E99E90DA-F0FF  BD-00000000-000A
    # 2022-07-28T14:22:02.932576 088 RQ --- 37:010188 20:257336 --:------ 4401 020 10  22-E99E90E0  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:03.053744 045  I --- 20:257336 37:010188 --:------ 4401 020 10  22-E99E90E0  00-E99E90E0-75FF  BD-00000000-000A
    # 2022-07-28T14:22:20.516363 045 RQ --- 20:255710 20:257400 --:------ 4401 020 10  0B-E99E90F2  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:20.571640 085  I --- 20:255251 20:229597 --:------ 4401 020 10  39-E99E90F1  00-E99E90F1-5CFF  40-00000000-000A
    # 2022-07-28T14:22:20.648696 058  I --- 20:257400 20:255710 --:------ 4401 020 10  0B-E99E90F2  00-E99E90F1-D4FF  DA-00000000-000B

    # 2022-11-03T23:00:04.854479 088 RQ --- 20:256717 37:013150 --:------ 4401 020 10  00-00259261  00-00000000-0000  00-00000000-0063
    # 2022-11-03T23:00:05.102491 045  I --- 37:013150 20:256717 --:------ 4401 020 10  00-00259261  00-000C9E4C-1800  00-00000000-0063
    # 2022-11-03T23:00:17.820659 072  I --- 20:256112 20:255825 --:------ 4401 020 10  00-00F1EB91  00-00E8871B-B700  00-00000000-0063
    # 2022-11-03T23:01:25.495391 065  I --- 20:257732 20:257680 --:------ 4401 020 10  00-002E9C98  00-00107923-9E00  00-00000000-0063
    # 2022-11-03T23:01:33.753467 066 RQ --- 20:257732 20:256112 --:------ 4401 020 10  00-0010792C  00-00000000-0000  00-00000000-0063
    # 2022-11-03T23:01:33.997485 072  I --- 20:256112 20:257732 --:------ 4401 020 10  00-0010792C  00-00E88767-AD00  00-00000000-0063
    # 2022-11-03T23:01:52.391989 090  I --- 20:256717 20:255301 --:------ 4401 020 10  00-009870E1  00-002592CC-6300  00-00000000-0063

    def hex_to_epoch(seqx: str) -> None | str:  # seconds since 1-1-1970
        if seqx == "00" * 4:
            return None
        return str(dt.fromtimestamp(int(seqx, 16)))

    assert payload[:2] == "10", payload[:2]
    assert payload[12:14] == "00", payload[12:14]
    assert payload[36:38] == "00", payload[36:38]

    assert msg.verb != I_ or payload[24:26] in ("00", "7C", "FF"), payload[24:26]
    assert msg.verb != W_ or payload[24:26] in ("7C", "FF"), payload[24:26]
    assert msg.verb != RQ or payload[24:26] == "00", payload[24:26]

    assert msg.verb != RQ or payload[14:22] == "00" * 4, payload[14:22]
    assert msg.verb != W_ or payload[28:36] != "00" * 4, payload[28:36]

    assert payload[38:40] in ("08", "09", "0A", "0B", "63"), payload[38:40]

    return {
        "last_update_dst": payload[2:4],
        "time_dst": hex_to_epoch(payload[4:12]),
        "_unknown_12": payload[12:14],  # usu.00
        "time_src": hex_to_epoch(payload[14:22]),
        "offset": payload[22:24],  # *15 mins?
        "_unknown_24": payload[24:26],
        "last_update_src": payload[26:28],
        "time_dst_receive_src": hex_to_epoch(payload[28:36]),
        "_unknown_36": payload[36:38],  # usu.00
        "hops_dst_src": payload[38:40],
    }


# temperatures (see: 4e02) - Itho spider/autotemp
@register_parser("4E01")
def parser_4e01(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e01 (Itho temperatures) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing an array of temperature measurements
    :rtype: dict[str, Any]
    :raises AssertionError: If the number of temperature groups is malformed.
    """
    # .I --- 02:248945 02:250708 --:------ 4E01 018 00-7FFF7FFF7FFF09077FFF7FFF7FFF7FFF-00
    # .I --- 02:250984 02:250704 --:------ 4E01 018 00-7FFF7FFF7FFF7FFF08387FFF7FFF7FFF-00

    num_groups = int((msg.len - 2) / 2)  # e.g. (18 - 2) / 2
    assert num_groups * 2 == msg.len - 2, _INFORM_DEV_MSG

    x, y = 0, 2 + num_groups * 4

    assert payload[x : x + 2] == "00", _INFORM_DEV_MSG
    assert payload[y : y + 2] == "00", _INFORM_DEV_MSG

    return {
        "temperatures": [hex_to_temp(payload[i : i + 4]) for i in range(2, y, 4)],
    }


# setpoint_bounds (see: 4e01) - Itho spider/autotemp
@register_parser("4E02")
def parser_4e02(
    payload: str, msg: Message
) -> dict[str, Any]:  # sent a triplets, 1 min apart
    """Parse the 4e02 (Itho setpoint bounds) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the mode and associated setpoint bounds
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload constants or mode indicators are invalid.
    """
    num_groups = int((msg.len - 2) / 4)  # e.g. (34 - 2) / 4
    assert num_groups * 4 == msg.len - 2, _INFORM_DEV_MSG

    x, y = 0, 2 + num_groups * 4

    assert payload[x : x + 2] == "00", _INFORM_DEV_MSG  # expect no context
    assert payload[y : y + 2] in (
        "02",
        "03",
        "04",
        "05",
    ), _INFORM_DEV_MSG  # mode: cool/heat?

    setpoints = [
        (hex_to_temp(payload[x + i :][:4]), hex_to_temp(payload[y + i :][:4]))
        for i in range(2, y, 4)
    ]  # lower, upper setpoints

    return {
        SZ_MODE: {"02": "cool", "03": "cool+", "04": "heat", "05": "cool+"}[
            payload[y : y + 2]
        ],
        SZ_SETPOINT_BOUNDS: [s if s != (None, None) else None for s in setpoints],
    }


# hvac_4e04
@register_parser("4E04")
def parser_4e04(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e04 (HVAC mode) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the system mode
    :rtype: dict[str, Any]
    :raises AssertionError: If the mode byte or data value is unrecognized.
    """
    MODE = {
        "00": "off",
        "01": "heat",
        "02": "cool",
    }

    assert payload[2:4] in MODE, _INFORM_DEV_MSG
    assert int(payload[4:], 16) < 0x40 or payload[4:] in (
        "FB",  # error code?
        "FC",  # error code?
        "FD",  # error code?
        "FE",  # error code?
        "FF",  # N/A?
    )

    return {
        SZ_MODE: MODE.get(payload[2:4], "Unknown"),
        "_unknown_2": payload[4:],
    }


# WIP: AT outdoor low - Itho spider/autotemp
@register_parser("4E0D")
def parser_4e0d(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e0d packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    """
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0100  # Itho Autotemp
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0101  # context?

    return {
        "_payload": payload,
    }


# AT fault circulation - Itho spider/autotemp
@register_parser("4E14")
def parser_4e14(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e14 (circulation fault) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary indicating fault and circulation states
    :rtype: dict[str, Any]
    """
    return {}


# wpu_state (hvac state) - Itho spider/autotemp
@register_parser("4E15")
def parser_4e15(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e15 (WPU state) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of boolean flags for cooling, heating, and DHW activity
    :rtype: dict[str, Any]
    :raises TypeError: If the payload indicates simultaneous heating and cooling.
    :raises AssertionError: If unknown bit flags are present.
    """
    if int(payload[2:], 16) & 0xF0:
        pass

    # If none of these, then is 'Off'
    SZ_COOLING = "is_cooling"
    SZ_DHW_ING = "is_dhw_ing"
    SZ_HEATING = "is_heating"

    assert int(payload[2:], 16) & 0xF8 == 0x00, _INFORM_DEV_MSG
    if int(payload[2:], 16) & 0x03 == 0x03:  # is_cooling *and* is_heating
        raise TypeError
    assert int(payload[2:], 16) & 0x07 != 0x06, _INFORM_DEV_MSG

    return {
        "_flags": hex_to_flag8(payload[2:]),
        SZ_DHW_ING: bool(int(payload[2:], 16) & 0x04),
        SZ_HEATING: bool(int(payload[2:], 16) & 0x02),
        SZ_COOLING: bool(int(payload[2:], 16) & 0x01),
    }


# TODO: hvac_4e16 - Itho spider/autotemp
@register_parser("4E16")
def parser_4e16(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e16 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload is not the expected null sequence.
    """

    # .I --- 02:250984 02:250704 --:------ 4E16 007 00000000000000  # Itho Autotemp: slave -> master

    assert payload == "00000000000000", _INFORM_DEV_MSG

    return {
        "_payload": payload,
    }


# TODO: Fan characteristics - Itho
@register_parser("4E20")
def parser_4e20(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e20 (fan characteristics) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of decoded fan constants
    :rtype: dict[str, Any]
    """
    return {}


# TODO: Potentiometer control - Itho
@register_parser("4E21")
def parser_4e21(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 4e21 (potentiometer control) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of absolute and relative power limits
    :rtype: dict[str, Any]
    """
    return {}

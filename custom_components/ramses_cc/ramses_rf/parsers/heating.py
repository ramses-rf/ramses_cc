"""RAMSES RF - Heating and Zone payload parsers.

This module provides parsers for standard RAMSES RF packets related to
Evohome heating control, zones, TRVs, and underfloor heating (UFH).
"""

from __future__ import annotations

import logging
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any

from ...ramses_tx import exceptions as exc
from ...ramses_tx.address import hex_id_to_dev_id
from ...ramses_tx.const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    F9,
    FA,
    FC,
    I_,
    RQ,
    SZ_DEVICE_ROLE,
    SZ_DEVICES,
    SZ_DOMAIN_ID,
    SZ_DURATION,
    SZ_LOCAL_OVERRIDE,
    SZ_MAX_TEMP,
    SZ_MIN_TEMP,
    SZ_MODE,
    SZ_MULTIROOM_MODE,
    SZ_NAME,
    SZ_OPENWINDOW_FUNCTION,
    SZ_PAYLOAD,
    SZ_PRESSURE,
    SZ_RELAY_DEMAND,
    SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS,
    SZ_TEMPERATURE,
    SZ_UFH_IDX,
    SZ_UNTIL,
    SZ_WINDOW_OPEN,
    SZ_ZONE_CLASS,
    SZ_ZONE_IDX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    ZON_MODE_MAP,
    ZON_ROLE_MAP,
    DevRole,
)
from ...ramses_tx.helpers import (
    hex_to_bool,
    hex_to_dtm,
    hex_to_flag8,
    hex_to_percent,
    hex_to_str,
    hex_to_temp,
    parse_valve_demand,
)
from ...ramses_tx.typing import PayDictT

from .registry import register_parser

if TYPE_CHECKING:
    from ..messages import Message

_LOGGER = logging.getLogger(__name__)
_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"

# Global store for UFC pump state (set by parser, read by heat_controllers.pump_active)
_UFC_PUMP_STATE: dict[str, bool] = {}

# zone_name
@register_parser("0004")
def parser_0004(payload: str, msg: Message) -> PayDictT._0004:
    """Parse the 0004 (zone_name) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the zone name
    :rtype: PayDictT._0004
    """
    # RQ payload is zz00; limited to 12 chars in evohome UI? if "7F"*20: not a zone

    return {} if payload[4:] == "7F" * 20 else {SZ_NAME: hex_to_str(payload[4:])}


# system_zones (add/del a zone?)
@register_parser("0005")
def parser_0005(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 0005 (system_zones) packet to identify zone types and masks.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A list or dictionary of zone classes and masks
    :rtype: dict[str, Any] | list[dict[str, Any]]
    :raises AssertionError: If the message source is not a recognized device type.
    """
    # .I --- 01:145038 --:------ 01:145038 0005 004 00000100
    # RP --- 02:017205 18:073736 --:------ 0005 004 0009001F
    # .I --- 34:064023 --:------ 34:064023 0005 012 000A0000-000F0000-00100000

    def _parser(seqx: str) -> dict[str, Any]:
        if msg.src.type == DEV_TYPE_MAP.UFC:  # DEX, or use: seqx[2:4] == ...
            zone_mask = hex_to_flag8(seqx[6:8], lsb=True)
        elif msg.len == 3:  # ATC928G1000 - 1st gen monochrome model, max 8 zones
            zone_mask = hex_to_flag8(seqx[4:6], lsb=True)
        else:
            zone_mask = hex_to_flag8(seqx[4:6], lsb=True) + hex_to_flag8(
                seqx[6:8], lsb=True
            )
        zone_class = ZON_ROLE_MAP.get(seqx[2:4], DEV_ROLE_MAP[seqx[2:4]])
        return {
            SZ_ZONE_TYPE: seqx[2:4],  # TODO: ?remove & keep zone_class?
            SZ_ZONE_MASK: zone_mask,
            SZ_ZONE_CLASS: zone_class,  # TODO: ?remove & keep zone_type?
        }

    if msg.verb == RQ:  # RQs have a context: zone_type
        return {
            SZ_ZONE_TYPE: payload[2:4],
            SZ_ZONE_CLASS: DEV_ROLE_MAP[payload[2:4]],
        }

    if msg._has_array:
        assert msg.verb == I_ and msg.src.type == DEV_TYPE_MAP.RND, (
            f"{msg!r} # expecting I/{DEV_TYPE_MAP.RND}:"
        )  # DEX
        return [_parser(payload[i : i + 8]) for i in range(0, len(payload), 8)]

    return _parser(payload)


# relay_demand (domain/zone/device)
@register_parser("0008")
def parser_0008(payload: str, msg: Message) -> PayDictT._0008:
    """Parse the 0008 (relay_demand) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the relay demand percentage
    :rtype: PayDictT._0008
    :raises AssertionError: If the message length is invalid for specific device types.
    """
    # https://www.domoticaforum.eu/viewtopic.php?f=7&t=5806&start=105#p73681
    # e.g. Electric Heat Zone

    # .I --- 01:145038 --:------ 01:145038 0008 002 0314
    # .I --- 01:145038 --:------ 01:145038 0008 002 F914
    # .I --- 01:054173 --:------ 01:054173 0008 002 FA00
    # .I --- 01:145038 --:------ 01:145038 0008 002 FC14

    # RP --- 13:109598 18:199952 --:------ 0008 002 0000
    # RP --- 13:109598 18:199952 --:------ 0008 002 00C8

    if msg.src.type == DEV_TYPE_MAP.JST and msg.len == 13:  # Honeywell Japser, DEX
        assert msg.len == 13, "expecting length 13"
        return {  # type: ignore[typeddict-item]
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    return {SZ_RELAY_DEMAND: hex_to_percent(payload[2:4])}  # 3EF0[2:4], 3EF1[10:12]


# relay_failsafe
@register_parser("0009")
def parser_0009(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 0009 (relay_failsafe) packet.
    The relay failsafe mode.

    The failsafe mode defines the relay behaviour if the RF communication is
    lost (e.g. when a room thermostat stops communicating due to discharged
    batteries):

    - False (disabled) - if RF comms are lost, relay will be held in OFF position
    - True  (enabled)  - if RF comms are lost, relay will cycle at 20% ON, 80% OFF

    This setting may need to be enabled to ensure frost protect mode.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary defining if failsafe mode is enabled
    :rtype: dict[str, Any] | list[dict[str, Any]]
    :raises AssertionError: If the domain ID in the payload is invalid.
    """
    # can get: 003 or 006, e.g.: FC01FF-F901FF or FC00FF-F900FF
    # .I --- 23:100224 --:------ 23:100224 0009 003 0100FF  # 2-zone ST9520C
    # .I --- 10:040239 01:223036 --:------ 0009 003 000000

    def _parser(seqx: str) -> dict[str, Any]:
        assert seqx[:2] in (F9, FC) or int(seqx[:2], 16) < 16
        return {
            SZ_DOMAIN_ID if seqx[:1] == "F" else SZ_ZONE_IDX: seqx[:2],
            "failsafe_enabled": {"00": False, "01": True}.get(seqx[2:4]),
            "unknown_0": seqx[4:],
        }

    if msg._has_array:
        return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]

    return {
        "failsafe_enabled": {"00": False, "01": True}.get(payload[2:4]),
        "unknown_0": payload[4:],
    }


# zone_params (zone_config)
@register_parser("000A")
def parser_000a(
    payload: str, msg: Message
) -> PayDictT._000A | list[PayDictT._000A] | PayDictT.EMPTY:
    """Parse the 000a (zone_params) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary of zone parameters including min/max temps
    :rtype: PayDictT._000A | list[PayDictT._000A] | PayDictT.EMPTY
    :raises AssertionError: If the message length is unexpected.
    """

    def _parser(seqx: str) -> PayDictT._000A:  # null_rp: "007FFF7FFF"
        bitmap = int(seqx[2:4], 16)
        return {
            SZ_MIN_TEMP: hex_to_temp(seqx[4:8]),
            SZ_MAX_TEMP: hex_to_temp(seqx[8:]),
            SZ_LOCAL_OVERRIDE: not bool(bitmap & 1),
            SZ_OPENWINDOW_FUNCTION: not bool(bitmap & 2),
            SZ_MULTIROOM_MODE: not bool(bitmap & 16),
            "_unknown_bitmap": f"0b{bitmap:08b}",  # TODO: try W with this
        }  # cannot determine zone_type from this information

    if msg._has_array:  # NOTE: these arrays can span 2 pkts!
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                **_parser(payload[i : i + 12]),
            }
            for i in range(0, len(payload), 12)
        ]

    if msg.verb == RQ and msg.len <= 2:  # some RQs have a payload (why?)
        return {}

    assert msg.len == 6, f"{msg!r} # expecting length 006"
    return _parser(payload)


# zone_devices
@register_parser("000C")
def parser_000c(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 000c (zone_devices) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary mapping device IDs to zone indices
    :rtype: dict[str, Any]
    :raises PacketPayloadInvalid: If the element length in the payload is malformed.
    :raises AssertionError: If indices or device IDs are invalid.
    """
    # .I --- 34:092243 --:------ 34:092243 000C 018 00-0A-7F-FFFFFF
    # 00-0F-7F-FFFFFF 00-10-7F-FFFFFF  # noqa: E501
    # RP --- 01:145038 18:013393 --:------ 000C 006 00-00-00-10DAFD
    # RP --- 01:145038 18:013393 --:------ 000C 012 01-00-00-10DAF5 01-00-00-10DAFB

    def complex_idx(seqx: str, msg: Message) -> dict[str, Any]:
        """domain_id, zone_idx, or ufx_idx|zone_idx."""
        # TODO: 000C to a UFC should be ufh_ifx, not zone_idx
        if msg.src.type == DEV_TYPE_MAP.UFC:  # DEX
            assert int(seqx, 16) < 8, f"invalid ufh_idx: '{seqx}' (0x00)"
            return {
                SZ_UFH_IDX: seqx,
                SZ_ZONE_IDX: None if payload[4:6] == "7F" else payload[4:6],
            }

        if payload[2:4] in (DEV_ROLE_MAP.DHW, DEV_ROLE_MAP.HTG):
            assert int(seqx, 16) < 1 if payload[2:4] == DEV_ROLE_MAP.DHW else 2, (
                f"invalid _idx: '{seqx}' (0x01)"
            )
            return {SZ_DOMAIN_ID: FA if payload[:2] == "00" else F9}

        if payload[2:4] == DEV_ROLE_MAP.APP:
            assert int(seqx, 16) < 1, f"invalid _idx: '{seqx}' (0x02)"
            return {SZ_DOMAIN_ID: FC}

        assert int(seqx, 16) < 16, f"invalid zone_idx: '{seqx}' (0x03)"
        return {SZ_ZONE_IDX: seqx}

    def _parser(seqx: str) -> dict[str, Any]:
        assert seqx[:2] == payload[:2], (
            f"idx != {payload[:2]} (seqx = {seqx}), short={is_short_000C(payload)}"
        )
        assert int(seqx[:2], 16) < 16
        assert seqx[4:6] == "7F" or seqx[6:] != "F" * 6, f"Bad device_id: {seqx[6:]}"
        return {hex_id_to_dev_id(seqx[6:12]): seqx[4:6]}

    def is_short_000C(payload: str) -> bool:
        """Return True if it is a short 000C (element length is 5, not 6)."""
        if (pkt_len := len(payload)) != 72:
            return pkt_len % 12 != 0

        # len(element) = 6
        # 0608-001099C3 0608-001099C5 0608-001099BF 0608-001099BE 0608-001099BD
        elif all(payload[i : i + 4] == payload[:4] for i in range(12, pkt_len, 12)):
            return False  # len(element) = 6 (12)

        # len(element) = 5
        # 0508-00109901 0800-10990208 0010-99030800 1099-04080010 9905-08001099
        elif all(payload[i : i + 2] == payload[2:4] for i in range(12, pkt_len, 10)):
            return True  # len(element) = 5 (10)

        raise exc.PacketPayloadInvalid("Unable to determine element length")

    if payload[2:4] == DEV_ROLE_MAP.HTG and payload[:2] == "01":
        dev_role = DEV_ROLE_MAP[DevRole.HT1]
    else:
        dev_role = DEV_ROLE_MAP[payload[2:4]]

    result = {
        SZ_ZONE_TYPE: payload[2:4],
        **complex_idx(payload[:2], msg),
        SZ_DEVICE_ROLE: dev_role,
    }
    if msg.verb == RQ:  # RQs have a context: index, zone_type, payload is iitt
        return result

    # NOTE: Both these are valid! So collision when len = 036!
    # RP --- 01:239474 18:198929 --:------ 000C 012 06-00-00119A99 06-00-00119B21
    # RP --- 01:069616 18:205592 --:------ 000C 011 01-00-00121B54    00-00121B52
    # RP --- 01:239700 18:009874 --:------ 000C 018 07-08-001099C3 07-08-001099C5
    # RP --- 01:059885 18:010642 --:------ 000C 016 00-00-0011EDAA    00-0011ED92

    devs = (
        [_parser(payload[:2] + payload[i : i + 10]) for i in range(2, len(payload), 10)]
        if is_short_000C(payload)
        else [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]
    )

    return {
        **result,
        SZ_DEVICES: [k for d in devs for k, v in d.items() if v != "7F"],
    }


# mixvalve_config (zone)
@register_parser("1030")
def parser_1030(payload: str, msg: Message) -> PayDictT._1030:
    """Parse the 1030 (mixvalve_config) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of mixing valve parameters
    :rtype: PayDictT._1030
    :raises AssertionError: If the message length is unexpected or malformed.
    """
    # .I --- 01:145038 --:------ 01:145038 1030 016 0A-C80137-C9010F-CA0196-CB0100
    # .I --- --:------ --:------ 12:144017 1030 016 01-C80137-C9010F-CA0196-CB010F
    # RP --- 32:155617 18:005904 --:------ 1030 007 00-200100-21011F

    def _parser(seqx: str) -> dict[str, Any]:
        assert seqx[2:4] == "01", seqx[2:4]

        param_name = {
            "20": "unknown_20",  # HVAC
            "21": "unknown_21",  # HVAC
            "C8": "max_flow_setpoint",  # 55 (0-99) C
            "C9": "min_flow_setpoint",  # 15 (0-50) C
            "CA": "valve_run_time",  # 150 (0-240) sec, aka actuator_run_time
            "CB": "pump_run_time",  # 15 (0-99) sec
            "CC": "boolean_cc",  # ?boolean?
        }[seqx[:2]]

        return {param_name: int(seqx[4:], 16)}

    assert (msg.len - 1) / 3 in (2, 5), msg.len
    # assert payload[30:] in ("00", "01"), payload[30:]

    params = [_parser(payload[i : i + 6]) for i in range(2, len(payload), 6)]
    return {k: v for x in params for k, v in x.items()}  # type: ignore[return-value]


# max_ch_setpoint (supply high limit)
@register_parser("1081")
def parser_1081(payload: str, msg: Message) -> PayDictT._1081:
    """Parse the 1081 (max_ch_setpoint) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the temperature setpoint
    :rtype: PayDictT._1081
    """
    return {SZ_SETPOINT: hex_to_temp(payload[2:])}


# unknown_1090 (non-Evohome, e.g. ST9520C)
@register_parser("1090")
def parser_1090(payload: str, msg: Message) -> PayDictT._1090:
    """Parse the 1090 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing two temperature values
    :rtype: PayDictT._1090
    :raises AssertionError: If the message length or payload index is invalid.
    """
    # 14:08:05.176 095 RP --- 23:100224 22:219457 --:------ 1090 005
    # 007FFF01F4
    # 18:08:05.809 095 RP --- 23:100224 22:219457 --:------ 1090 005
    # 007FFF01F4

    # this is an educated guess
    assert msg.len == 5, _INFORM_DEV_MSG
    assert int(payload[:2], 16) < 2, _INFORM_DEV_MSG

    return {
        "temperature_0": hex_to_temp(payload[2:6]),
        "temperature_1": hex_to_temp(payload[6:10]),
    }


# tpi_params (domain/zone/device)  # FIXME: a bit messy
@register_parser("1100")
def parser_1100(
    payload: str, msg: Message
) -> PayDictT._1100 | PayDictT._1100_IDX | PayDictT._JASPER | PayDictT.EMPTY:
    """Parse the 1100 (tpi_params) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of TPI parameters or domain index
    :rtype: PayDictT._1100 | PayDictT._1100_IDX | PayDictT._JASPER | PayDictT.EMPTY
    :raises AssertionError: If TPI values are outside of recognized ranges.
    """

    def complex_idx(seqx: str) -> PayDictT._1100_IDX | PayDictT.EMPTY:
        if seqx[:1] == "F":
            return {SZ_DOMAIN_ID: seqx}  # type: ignore[typeddict-item, unused-ignore]  # only FC
        return {}

    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Japser, DEX
        assert msg.len == 19, msg.len
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        return complex_idx(payload[:2])

    assert int(payload[2:4], 16) / 4 in range(1, 13), payload[2:4]
    assert int(payload[4:6], 16) / 4 in range(1, 31), payload[4:6]
    assert int(payload[6:8], 16) / 4 in range(0, 16), payload[6:8]

    # for:             TPI              // heatpump
    #  - cycle_rate:   6 (3, 6, 9, 12)  // ?? (1-9)
    #  - min_on_time:  1 (1-5)          // ?? (1, 5, 10,...30)
    #  - min_off_time: 1 (1-?)          // ?? (0, 5, 10, 15)

    def _parser(seqx: str) -> PayDictT._1100:
        return {
            "cycle_rate": int(int(payload[2:4], 16) / 4),  # cycles/hour
            "min_on_time": int(payload[4:6], 16) / 4,  # min
            "min_off_time": int(payload[6:8], 16) / 4,  # min
            "_unknown_0": payload[8:10],  # always 00, FF?
        }

    result = _parser(payload)

    if msg.len > 5:
        pbw = hex_to_temp(payload[10:14])

        assert pbw is None or 1.5 <= pbw <= 3.0, (
            f"unexpected value for PBW: {payload[10:14]}"
        )

        result.update(
            {
                "proportional_band_width": pbw,
                "_unknown_1": payload[14:],  # always 01?
            }
        )

    return complex_idx(payload[:2]) | result


# unknown_11f0, from heatpump relay
@register_parser("11F0")
def parser_11f0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 11f0 (heatpump relay) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload does not match the expected constant string.
    """
    assert payload == "000009000000000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# window_state (of a device/zone)
@register_parser("12B0")
def parser_12b0(payload: str, msg: Message) -> PayDictT._12B0:
    """Parse the 12b0 (window_state) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the window open status
    :rtype: PayDictT._12B0
    :raises AssertionError: If the payload state bytes are unrecognized.
    """
    # "FFFF" means N/A
    assert payload[2:] in ("0000", "C800", "FFFF"), payload[2:]

    return {
        SZ_WINDOW_OPEN: hex_to_bool(payload[2:4]),
    }


# displayed temperature (on a TR87RF bound to a RFG100)
@register_parser("12C0")
def parser_12c0(payload: str, msg: Message) -> PayDictT._12C0:
    """Parse the 12c0 (displayed_temp) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the temperature and its measurement units
    :rtype: PayDictT._12C0
    """
    if payload[2:4] == "80":
        temp: float | None = None
    elif payload[4:6] == "00":  # units are 1.0 F
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} (Fahrenheit TRV detected)")
        temp = round((int(payload[2:4], 16) - 32) * 5 / 9, 2)
    else:  # if payload[4:] == "01":  # units are 0.5 C
        temp = int(payload[2:4], 16) / 2

    result: PayDictT._12C0 = {
        SZ_TEMPERATURE: temp,
        "units": "Celsius",
    }
    if len(payload) > 6:
        result["_unknown_6"] = payload[6:]
    return result


# ch_pressure
@register_parser("1300")
def parser_1300(payload: str, msg: Message) -> PayDictT._1300:
    """Parse the 1300 (ch_pressure) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the system pressure in bar
    :rtype: PayDictT._1300
    """
    # 0x9F6 (2550 dec = 2.55 bar) appears to be a sentinel value
    return {SZ_PRESSURE: None if payload[2:] == "09F6" else hex_to_temp(payload[2:])}


# now_next_setpoint - Programmer/Hometronics
@register_parser("2249")
def parser_2249(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 2249 (now_next_setpoint) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary or list of current/next setpoints and time remaining
    :rtype: dict[str, Any] | list[dict[str, Any]]
    """
    # see: https://github.com/jrosser/honeymon/blob/master/decoder.cpp#L357-L370
    # .I --- 23:100224 --:------ 23:100224 2249 007 00-7EFF-7EFF-FFFF

    def _parser(seqx: str) -> dict[str, bool | float | int | str | None]:
        minutes = int(seqx[10:], 16)
        next_setpoint = msg.dtm + td(minutes=minutes)
        return {
            "setpoint_now": hex_to_temp(seqx[2:6]),
            "setpoint_next": hex_to_temp(seqx[6:10]),
            "minutes_remaining": minutes,
            "_next_setpoint": dt.strftime(next_setpoint, "%H:%M:%S"),
        }

    # the ST9520C can support two heating zones, so: msg.len in (7, 14)?
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                **_parser(payload[i + 2 : i + 14]),
            }
            for i in range(0, len(payload), 14)
        ]

    return _parser(payload)


# setpoint_bounds, TODO: max length = 24?
@register_parser("22C9")
def parser_22c9(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 22c9 (setpoint_bounds) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary or list containing mode and temperature bounds
    :rtype: dict[str, Any] | list[dict[str, Any]]
    :raises AssertionError: If the payload length or suffix is unrecognized.
    """
    _LOGGER.warning(
        "RAMSES 22C9 %s from %s to %s: payload=%s [%s]",
        msg.verb,
        msg.src.id,
        msg.dst.id,
        payload,
        dt.now().isoformat(timespec='seconds'),
    )

    # .I --- 02:001107 --:------ 02:001107 22C9 024 00-0834-0A28-01-0108340A2801-0208340A2801-0308340A2801  # noqa: E501
    # .I --- 02:001107 --:------ 02:001107 22C9 006 04-0834-0A28-01

    # .I --- 21:064743 --:------ 21:064743 22C9 006 00-07D0-0834-02
    # .W --- 21:064743 02:250708 --:------ 22C9 006 03-07D0-0834-02
    # .I --- 02:250708 21:064743 --:------ 22C9 008 03-07D0-7FFF-020203

    # Notes on 008|suffix: only seen as I, only when no array, only as 7FFF(0101|0202)03$

    def _parser(seqx: str) -> dict[str, Any]:
        assert seqx[10:] in ("01", "02"), f"is {seqx[10:]}, expecting 01 or 02"

        return {
            SZ_MODE: {"01": "heat", "02": "cool"}[seqx[10:]],  # TODO: or action?
            SZ_SETPOINT_BOUNDS: (hex_to_temp(seqx[2:6]), hex_to_temp(seqx[6:10])),
        }  # lower, upper setpoints

    if msg._has_array:
        return [
            {
                SZ_UFH_IDX: payload[i : i + 2],
                **_parser(payload[i : i + 12]),
            }
            for i in range(0, len(payload), 12)
        ]

    assert msg.len != 8 or payload[10:] in ("010103", "020203"), _INFORM_DEV_MSG

    return _parser(payload[:12])


# Map the legacy DT4R bounds code to the above parser
parser_2209 = register_parser("2209")(parser_22c9)


# unknown_22d0, UFH system mode (heat/cool)
@register_parser("22D0")
def parser_22d0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 22d0 (UFH system mode) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary of UFH index, flags, and active modes
    :rtype: dict[str, Any]
    :raises AssertionError: If payload constants or flags are invalid.
    """

    def _parser(seqx: str) -> dict[str, Any]:
        # assert seqx[2:4] in ("00", "03", "10", "13", "14"), _INFORM_DEV_MSG
        assert seqx[4:6] == "00", _INFORM_DEV_MSG
        return {
            "idx": seqx[:2],
            "_flags": hex_to_flag8(seqx[2:4]),
            "cool_mode": bool(int(seqx[2:4], 16) & 0x02),
            "heat_mode": bool(int(seqx[2:4], 16) & 0x04),
            "is_active": bool(int(seqx[2:4], 16) & 0x10),
            "_unknown": payload[4:],
        }

    if len(payload) == 8:
        assert payload[6:] in ("00", "02", "0A"), _INFORM_DEV_MSG
    else:
        assert payload[4:] == "001E14030020", _INFORM_DEV_MSG

    return _parser(payload)


# desired boiler setpoint
@register_parser("22D9")
def parser_22d9(payload: str, msg: Message) -> PayDictT._22D9:
    """Parse the 22d9 (desired boiler setpoint) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the target temperature setpoint
    :rtype: PayDictT._22D9
    """
    return {SZ_SETPOINT: hex_to_temp(payload[2:6])}


# setpoint (of device/zones)
@register_parser("2309")
def parser_2309(
    payload: str, msg: Message
) -> PayDictT._2309 | list[PayDictT._2309] | PayDictT.EMPTY:
    """Parse the 2309 (setpoint) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A setpoint dictionary, list of setpoints, or an empty dictionary
    :rtype: PayDictT._2309 | list[PayDictT._2309] | PayDictT.EMPTY
    """
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                SZ_SETPOINT: hex_to_temp(payload[i + 2 : i + 6]),
            }
            for i in range(0, len(payload), 6)
        ]

    # RQ --- 22:131874 01:063844 --:------ 2309 003 020708
    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        return {}

    return {SZ_SETPOINT: hex_to_temp(payload[2:])}


# zone_mode  # TODO: messy
@register_parser("2349")
def parser_2349(payload: str, msg: Message) -> PayDictT._2349 | PayDictT.EMPTY:
    """Parse the 2349 (zone_mode) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing zone mode, setpoint, and override details
    :rtype: PayDictT._2349 | PayDictT.EMPTY
    :raises AssertionError: If the message length or mode is invalid.
    """
    # RP --- 30:258557 34:225071 --:------ 2349 013 007FFF00FFFFFFFFFFFFFFFFFF
    # RP --- 30:253184 34:010943 --:------ 2349 013 00064000FFFFFF00110E0507E5
    # RQ --- 34:225071 30:258557 --:------ 2349 001 00
    # .I --- 10:067219 --:------ 10:067219 2349 004 00000001

    if msg.verb == RQ and msg.len <= 2:  # some RQs have a payload (why?)
        return {}

    assert msg.len in (7, 13), f"expected len 7,13, got {msg.len}"

    assert payload[6:8] in ZON_MODE_MAP, f"unknown zone_mode: {payload[6:8]}"
    result: PayDictT._2349 = {
        SZ_MODE: ZON_MODE_MAP.get(payload[6:8]),  # type: ignore[typeddict-item]
        SZ_SETPOINT: hex_to_temp(payload[2:6]),
    }

    if msg.len >= 7:  # has a dtm if mode == "04"
        if payload[8:14] == "FF" * 3:  # 03/FFFFFF OK if W?
            assert payload[6:8] != ZON_MODE_MAP.COUNTDOWN, f"{payload[6:8]} (0x00)"
        else:
            assert payload[6:8] == ZON_MODE_MAP.COUNTDOWN, f"{payload[6:8]} (0x01)"
            result[SZ_DURATION] = int(payload[8:14], 16)

    if msg.len >= 13:
        if payload[14:] == "FF" * 6:
            assert payload[6:8] in (
                ZON_MODE_MAP.FOLLOW,
                ZON_MODE_MAP.PERMANENT,
            ), f"{payload[6:8]} (0x02)"
            result[SZ_UNTIL] = None  # TODO: remove?
        else:
            assert payload[6:8] != ZON_MODE_MAP.PERMANENT, f"{payload[6:8]} (0x03)"
            result[SZ_UNTIL] = hex_to_dtm(payload[14:26])

    return result


# unknown_2389, from 03:
@register_parser("2389")
def parser_2389(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2389 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing an unknown temperature measurement
    :rtype: dict[str, Any]
    """
    return {
        "_unknown": hex_to_temp(payload[2:6]),
    }


# _state (of cooling?), from BDR91T, hometronics CTL
@register_parser("2D49")
def parser_2d49(payload: str, msg: Message) -> PayDictT._2D49:
    """Parse the 2d49 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the boolean state
    :rtype: PayDictT._2D49
    :raises AssertionError: If the payload state bytes are unrecognized.
    """
    _LOGGER.warning(
        "RAMSES 2D49 %s from %s to %s: payload=%s [%s]",
        msg.verb,
        msg.src.id,
        msg.dst.id,
        payload,
        dt.now().isoformat(timespec='seconds'),
    )

    assert payload[2:] in ("0000", "00FF", "C800", "C8FF"), _INFORM_DEV_MSG

    return {
        "zone_idx": payload[0:2],
        "cooling_demand": payload[2:4] == "C8",
    }


# current temperature (of device, zone/s)
@register_parser("30C9")
def parser_30c9(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 30c9 (temperature) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary or list of temperatures by zone index
    :rtype: dict[str, Any] | list[dict[str, Any]]
    """
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                SZ_TEMPERATURE: hex_to_temp(payload[i + 2 : i + 6]),
            }
            for i in range(0, len(payload), 6)
        ]

    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# zone_demand (of device, FC domain) - valve status (%open)
@register_parser("3150")
def parser_3150(payload: str, msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse the 3150 (zone_demand) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary or list of dictionaries containing zone indices and demand
    :rtype: dict[str, Any] | list[dict[str, Any]]
    """
    # event-driven, and periodically; FC domain is maximum of all zones
    # TODO: all have a valid domain will UFC/CTL respond to an RQ,
    # for FC, for a zone?

    # .I --- 04:136513 --:------ 01:158182 3150 002 01CA < often seen CA, artefact?

    def complex_idx(seqx: str, msg: Message) -> dict[str, str]:
        # assert seqx[:2] == FC or (int(seqx[:2], 16) < MAX_ZONES)  # <5,
        # 8 for UFC
        idx_name = "ufx_idx" if msg.src.type == DEV_TYPE_MAP.UFC else SZ_ZONE_IDX  # DEX
        return {SZ_DOMAIN_ID if seqx[:1] == "F" else idx_name: seqx[:2]}

    if msg._has_array:
        return [
            {
                **complex_idx(payload[i : i + 2], msg),
                **parse_valve_demand(payload[i + 2 : i + 4]),
            }
            for i in range(0, len(payload), 4)
        ]

    return parse_valve_demand(payload[2:])  # TODO: check UFC/FC is == CTL/FC


# supplied boiler water (flow) temp
@register_parser("3200")
def parser_3200(payload: str, msg: Message) -> PayDictT._3200:
    """Parse the 3200 (supplied_temp) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the water flow temperature
    :rtype: PayDictT._3200
    """
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# return (boiler) water temp
@register_parser("3210")
def parser_3210(payload: str, msg: Message) -> PayDictT._3210:
    """Parse the 3210 (return_temp) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the return water temperature
    :rtype: PayDictT._3210
    """
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# actuator_sync (aka sync_tpi: TPI cycle sync)
@register_parser("3B00")
def parser_3b00(payload: str, msg: Message) -> PayDictT._3B00:
    """Decode a 3B00 packet (actuator_sync).

    This signal marks the start or end of a TPI cycle to synchronize relay behavior.

    The heat relay regularly broadcasts a 3B00 at the end(?) of every TPI
    cycle, the frequency of which is determined by the (TPI) cycle rate in
    1100.

    The CTL subsequently broadcasts a 3B00 (i.e. at the start of every TPI
    cycle).

    The OTB does not send these packets, but the CTL sends a regular
    broadcast anyway for the benefit of any zone actuators (e.g. zone
    valve zones).

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the sync state and domain ID
    :rtype: PayDictT._3B00
    :raises AssertionError: If the payload length or constants are invalid.
    """
    _LOGGER.warning(
        "RAMSES 3B00 %s from %s to %s: payload=%s [%s]",
        msg.verb,
        msg.src.id,
        msg.dst.id,
        payload,
        dt.now().isoformat(timespec='seconds'),
    )

    # system timing master: the device that sends I/FCC8 pkt controls the heater relay

    # 053  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8
    # 052  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8

    # 063  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8
    # 064  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8

    def complex_idx(payload: str, msg: Message) -> dict[str, str]:  # has complex idx
        if (
            msg.verb == I_
            and msg.src.type in (DEV_TYPE_MAP.CTL, DEV_TYPE_MAP.PRG)
            and msg.src is msg.dst
        ):  # DEX
            assert payload[:2] == FC
            return {SZ_DOMAIN_ID: FC}
        assert payload[:2] == "00"
        return {}

    assert msg.len == 2, msg.len
    assert payload[:2] == {
        DEV_TYPE_MAP.CTL: FC,
        DEV_TYPE_MAP.BDR: "00",
        DEV_TYPE_MAP.PRG: FC,
    }.get(msg.src.type, "00")  # DEX
    assert payload[2:] == "C8", payload[2:]  # Could it be a percentage?

    return {
        **complex_idx(payload[:2], msg),  # type: ignore[typeddict-item]
        "actuator_sync": hex_to_bool(payload[2:]),
    }


# actuator_state
@register_parser("3EF0")
def parser_3ef0(payload: str, msg: Message) -> PayDictT._3EF0 | PayDictT._JASPER:
    """Parse the 3ef0 (actuator_state) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of modulation levels, flags, and setpoints
    :rtype: PayDictT._3EF0 | PayDictT._JASPER
    :raises AssertionError: If payload constants, flags, or message lengths
        are unrecognized.
    """
    _LOGGER.warning(
        "RAMSES 3EF0 %s from %s to %s: payload=%s [%s]",
        msg.verb,
        msg.src.id,
        msg.dst.id,
        payload,
        dt.now().isoformat(timespec='seconds'),
    )

    result: dict[str, Any]

    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Jasper
        assert msg.len == 20, f"expecting len 20, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if msg.src.type == DEV_TYPE_MAP.UFC:  # HCC100 UFH controller
        # HCC100 sends 9-byte 3EF0 with a non-standard format.
        # Known payloads:
        #   760000100000000000 — pump ON in cooling (byte 3 = 0x10)
        #   760000020000000000 — pump ON in heating/post-rebind (byte 3 = 0x02)
        #   760000000000000000 — pump OFF (byte 3 = 0x00)
        # Byte 3 flags: bit 4 (0x10) = cooling pump, bit 1 (0x02) = heating pump
        # pump_active = True if ANY pump-related bit is set (0x12 mask)
        byte3 = int(payload[6:8], 16)
        pump_active = bool(byte3 & 0x12)
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG} (UFC 3EF0 byte3=0x{byte3:02X})"
        )
        # Store globally for direct access by pump_active property
        _UFC_PUMP_STATE[msg.src.id] = pump_active
        return {  # type: ignore[return-value]
            "pump_active": pump_active,
        }

    # TODO: These two should be picked up by the regex
    assert msg.len in (3, 6, 9), f"Invalid payload length: {msg.len}"
    # assert payload[:2] == "00", f"Invalid payload context: {payload[:2]}"

    # NOTE: some [2:4] appear to intend 0x00-0x64 (high_res=False), instead of 0x00-0xC8
    # NOTE: for best compatibility, all will be switched to 0x00-0xC8 (high_res=True)

    if msg.len == 3:
        # I|BDR|003 (the following are the only two payloads ever seen)
        # .I --- 13:042805 --:------ 13:042805 3EF0 003 0000FF
        # .I --- 13:023770 --:------ 13:023770 3EF0 003 00C8FF
        assert payload[2:4] in ("00", "C8"), f"byte 1: {payload[2:4]} (not 00/C8)"
        assert payload[4:6] == "FF", f"byte 2: {payload[4:6]} (not FF)"
        mod_level = hex_to_percent(payload[2:4], high_res=True)

    else:  # msg.len >= 6:  # RP|OTB|006 (to RQ|CTL/HGI/RFG)
        # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000100000FF
        # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000110000FF
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100C00FF
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100200FF
        assert payload[4:6] in ("00", "10", "11"), f"byte 2: {payload[4:6]}"
        mod_level = hex_to_percent(payload[2:4], high_res=True)  # 00-64/C8 (or FF)

    result = {
        "modulation_level": mod_level,  # 0008[2:4], 3EF1[10:12]
        "_flags_2": payload[4:6],
    }

    if msg.len >= 6:  # RP|OTB|006 (to RQ|CTL/HGI/RFG)
        # ?corrupt
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 000110FA00FF

        # for OTB (there's no reliable) modulation_level <-> flame_state)

        result.update(
            {
                "_flags_3": hex_to_flag8(payload[6:8]),
                "ch_active": bool(int(payload[6:8], 0x10) & 1 << 1),
                "dhw_active": bool(int(payload[6:8], 0x10) & 1 << 2),
                "cool_active": bool(int(payload[6:8], 0x10) & 1 << 4),
                "flame_on": bool(int(payload[6:8], 0x10) & 1 << 3),  # flame_on
                "_unknown_4": payload[8:10],  # FF, 00, 01, 0A
                "_unknown_5": payload[10:12],  # FF, 13, 1C, ?others
            }  # TODO: change to flame_active?
        )

    if msg.len >= 9:  # I/RP|OTB|009 (R8820A only?)
        assert int(payload[12:14], 16) & 0b11111100 == 0, f"byte 6: {payload[12:14]}"
        assert int(payload[12:14], 16) & 0b00000010 == 2, f"byte 6: {payload[12:14]}"
        assert 10 <= int(payload[14:16], 16) <= 90, f"byte 7: {payload[14:16]}"
        assert int(payload[16:18], 16) in (0, 100), f"byte 8: {payload[18:]}"

        result.update(
            {
                "_flags_6": hex_to_flag8(payload[12:14]),
                "ch_enabled": bool(int(payload[12:14], 0x10) & 1 << 0),
                "ch_setpoint": int(payload[14:16], 0x10),
                "max_rel_modulation": hex_to_percent(payload[16:18], high_res=True),
            }
        )

    try:  # Trying to decode flags...
        # assert payload[4:6] != "11" or (
        #     payload[2:4] == "00"
        # ), f"bytes 1+2: {payload[2:6]}"  # 97% is 00 when 11, but not always

        assert payload[4:6] in (
            "00",
            "10",
            "11",
            "FF",
        ), f"byte 2: {payload[4:6]}"

        assert "_flags_3" not in result or (
            payload[6:8] == "FF" or int(payload[6:8], 0x10) & 0b10100000 == 0
        ), f"byte 3: {result['_flags_3']}"
        # only 10:040239 does 0b01000000, only Itho Autotemp does 0b00010000

        assert "_unknown_4" not in result or (
            payload[8:10] in ("FF", "00", "01", "02", "04", "07", "0A")
        ), f"byte 4: {payload[8:10]}"
        # only 10:040239 does 04

        assert "_unknown_5" not in result or (
            payload[10:12]
            in ("00", "01", "08", "10", "13", "14", "1C", "1D", "2F", "FF")
        ), f"byte 5: {payload[10:12]}"

        assert "_flags_6" not in result or (
            int(payload[12:14], 0x10) & 0b11111100 == 0
        ), f"byte 6: {result['_flags_6']}"

    except AssertionError as err:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG} ({err}), with a description of your system"
        )
    return result  # type: ignore[return-value]


# actuator_cycle
@register_parser("3EF1")
def parser_3ef1(payload: str, msg: Message) -> PayDictT._3EF1 | PayDictT._JASPER:
    """Parse the 3ef1 (actuator_cycle) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of modulation levels and cycle/actuator countdowns
    :rtype: PayDictT._3EF1 | PayDictT._JASPER
    :raises AssertionError: If the countdown values exceed recognized
        thresholds.
    """
    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Jasper, DEX
        assert msg.len == 18, f"expecting len 18, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if (
        msg.src.type == DEV_TYPE_MAP.JST
    ):  # and msg.len == 12:  # or (12, 20) Japser, DEX
        assert msg.len == 12, f"expecting len 12, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    percent = hex_to_percent(payload[10:12])

    if payload[12:] == "FF":  # is BDR
        assert percent is None or percent in (
            0,
            1,
        ), f"byte 5: {payload[10:12]}"

    else:  # is OTB
        # assert (
        #     re.compile(r"^00[0-9A-F]{10}10").match(payload)
        # ), "doesn't match: " + r"^00[0-9A-F]{10}10"
        assert payload[2:6] == "7FFF", f"byte 1: {payload[2:6]}"
        assert payload[6:10] == "003C", f"byte 3: {payload[6:10]}"  # 60s
        assert percent is None or percent <= 1, f"byte 5: {payload[10:12]}"

    cycle_countdown = None if payload[2:6] == "7FFF" else int(payload[2:6], 16)
    if cycle_countdown is not None:
        if cycle_countdown > 0x7FFF:
            cycle_countdown -= 0x10000
        assert cycle_countdown < 7200, f"byte 1: {payload[2:6]}"  # 7200s

    actuator_countdown = None if payload[6:10] == "7FFF" else int(payload[6:10], 16)
    if actuator_countdown is not None:
        # "87B3", "9DFA", "DCE1", "E638", "F8F7"
        if actuator_countdown > 0x7FFF:
            # actuator_countdown = 0x10000 - actuator_countdown  + cycle_countdown
            actuator_countdown = cycle_countdown  # Needs work
        # assert actuator_countdown <= cycle_countdown, f"byte 3: {payload[6:10]}"

    return {
        "modulation_level": percent,  # 0008[2:4], 3EF0[2:4]
        "actuator_countdown": actuator_countdown,
        "cycle_countdown": cycle_countdown,
        "_unknown_0": payload[12:],
    }

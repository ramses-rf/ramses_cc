"""RAMSES RF - System and Device Management payload parsers.

NOTES: aspirations on a consistent Schema, going forward:

==============  ========  ===================================  ========================
  :mode/state:   :bool:    :mutex (infinitive. vs -ing):        :flags:
mode (config.)   enabled   disabled, heat, cool, heat_cool...   ch_enabled, dhw_enabled
state (action)   active    idle, heating, cooling...            is_heating, is_cooling
==============  ========  ===================================  ========================

- prefer: enabled: True over xx_enabled: True (if only ever 1 flag)
- prefer:  active: True over is_heating: True (if only ever 1 flag)
- avoid: is_enabled, is_active

Kudos & many thanks to:
- Evsdd: 0404 (wow!)
- Ierlandfan: 3150, 31D9, 31DA, others
- ReneKlootwijk: 3EF0
- brucemiranda: 3EF0, others
- janvken: 10D0, 1470, 1F70, 22B0, 2411, several others
- tomkooij: 3110
- RemyDeRuysscher: 10E0, 31DA (and related), others
- silverailscolo:  12A0, 31DA, others
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any

from ...ramses_tx import exceptions as exc
from ...ramses_tx.address import ALL_DEV_ADDR, hex_id_to_dev_id
from ...ramses_tx.const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    F6,
    F8,
    F9,
    FA,
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    FB,
    FC,
    FF,
    I_,
    LOOKUP_PUZZ,
    RP,
    RQ,
    SYS_MODE_MAP,
    SZ_ACCEPT,
    SZ_BINDINGS,
    SZ_CHANGE_COUNTER,
    SZ_CONFIRM,
    SZ_DATETIME,
    SZ_DEVICE_CLASS,
    SZ_DEVICE_ID,
    SZ_DOMAIN_IDX,
    SZ_FAULT_STATE,
    SZ_FAULT_TYPE,
    SZ_FRAG_LENGTH,
    SZ_FRAG_NUMBER,
    SZ_FRAGMENT,
    SZ_IS_DST,
    SZ_LANGUAGE,
    SZ_LOG_ENTRY,
    SZ_LOG_IDX,
    SZ_OEM_CODE,
    SZ_OFFER,
    SZ_PAYLOAD,
    SZ_PHASE,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_TIMESTAMP,
    SZ_TOTAL_FRAGS,
    SZ_UNTIL,
    W_,
    DevRole,
    FaultDeviceClass,
)
from ...ramses_tx.helpers import (
    hex_to_date,
    hex_to_dtm,
    hex_to_dts,
    hex_to_percent,
    hex_to_str,
    hex_to_temp,
    parse_fault_log_entry,
    parse_outdoor_temp,
)
from ...ramses_tx.typing import PayDictT
from ...ramses_tx.version import VERSION

from ..protocol.fingerprints import check_signature
from .registry import register_parser

if TYPE_CHECKING:
    from ..messages import Message

_LOGGER = logging.getLogger(__name__)
_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"


# rf_unknown
@register_parser("0001")
def parser_0001(payload: str, msg: Message) -> Mapping[str, bool | str | None]:
    """Parse the 0001 (rf_unknown) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A mapping of parsed slot and parameter data
    :rtype: Mapping[str, bool | str | None]
    :raises AssertionError: If the payload format does not match expected constants.
    """
    # When in test mode, a 12: will send a W ?every 6 seconds:
    # 12:39:56.099 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 12:40:02.098 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 12:40:08.099 058  W --- 12:010740 --:------ 12:010740 0001 005 0000000501

    # sent by a THM when is signal strength test mode (0505, except 1st pkt)
    # 13:48:38.518 080  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:45.518 074  W --- 12:010740 --:------ 12:010740 0001 005 0000000505
    # 13:48:50.518 077  W --- 12:010740 --:------ 12:010740 0001 005 0000000505

    # sent by a CTL before a rf_check
    # 15:12:47.769 053  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:12:47.869 053 RQ --- 01:145038 13:237335 --:------ 0016 002 00FF
    # 15:12:47.880 053 RP --- 13:237335 01:145038 --:------ 0016 002 0017

    # 12:30:18.083 047  W --- 01:145038 --:------ 01:145038 0001 005 0800000505
    # 12:30:23.084 049  W --- 01:145038 --:------ 01:145038 0001 005 0800000505

    # 15:03:33.187 054  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:38.188 063  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:43.188 064  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:13:19.757 053  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:24.758 054  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:29.758 068  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:34.759 063  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505

    # sent by a CTL
    # 16:49:46.125 057  W --- 04:166090 --:------ 01:032820 0001 005 0100000505
    # 16:53:34.635 058  W --- 04:166090 --:------ 01:032820 0001 005 0100000505

    # loopback (not Tx'd) by a HGI80 whenever its button is pressed
    # 00:22:41.540 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:41.757 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200
    # 00:22:43.320 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:43.415 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200

    # From a CM927:
    # W/--:/--:/12:/00-0000-0501 = Test transmit
    # W/--:/--:/12:/00-0000-0505 = Field strength

    if payload[2:6] in ("2000", "8000", "A000"):
        mode = "hvac"
    elif payload[2:6] in ("0000", "FFFF"):
        mode = "heat"
    else:
        mode = "heat"

    if mode == "hvac":
        result: dict[str, bool | str | None]

        assert payload[:2] == "00", payload[:2]
        assert payload[8:10] in ("00", "04", "10", "20", "FF"), payload[8:10]

        result = {"payload": payload, "slot_num": payload[6:8]}
        if msg.len >= 6:
            result.update({"param_num": payload[10:12]})
        if msg.len >= 7:
            result.update({"next_slot_num": payload[12:14]})
        if msg.len >= 8:
            _14 = None if payload[14:16] == "FF" else bool(int(payload[14:16]))
            result.update({"boolean_14": _14})
        return result

    assert payload[2:6] in ("0000", "FFFF"), payload[2:6]
    assert payload[8:10] in ("00", "02", "05"), payload[8:10]

    return {
        SZ_PAYLOAD: "-".join((payload[:2], payload[2:6], payload[6:8], payload[8:])),
    }


# outdoor_sensor (outdoor_weather / outdoor_temperature)
@register_parser("0002")
def parser_0002(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 0002 (outdoor_sensor) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the outdoor temperature
    :rtype: dict[str, Any]
    """
    if payload[6:] == "02":  # or: msg.src.type == DEV_TYPE_MAP.OUT:
        return {
            SZ_TEMPERATURE: hex_to_temp(payload[2:6]),
            "_unknown": payload[6:],
        }

    return {"_payload": payload}


# schedule_sync (any changes?)
@register_parser("0006")
def parser_0006(payload: str, msg: Message) -> PayDictT._0006:
    """Return the total number of changes to the system schedules.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the schedule change counter
    :rtype: PayDictT._0006
    :raises AssertionError: If the payload header is invalid.
    """
    if payload[2:] == "FFFFFF":  # RP to an invalid RQ
        return {}

    assert payload[2:4] == "05"

    return {
        SZ_CHANGE_COUNTER: None if payload[4:] == "FFFF" else int(payload[4:], 16),
    }


# unknown_000e, from STA
@register_parser("000E")
def parser_000e(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 000e packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload value is not recognized.
    """
    assert payload in ("000014", "000028"), _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# rf_check
@register_parser("0016")
def parser_0016(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 0016 (rf_check) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing rf_strength and rf_value
    :rtype: dict[str, Any]
    """
    if msg.verb == RQ:
        return {}

    rf_value = int(payload[2:4], 16)
    return {
        "rf_strength": min(int(rf_value / 5) + 1, 5),
        "rf_value": rf_value,
    }


# language (of device/system)
@register_parser("0100")
def parser_0100(payload: str, msg: Message) -> PayDictT._0100 | PayDictT.EMPTY:
    """Parse the 0100 (language) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the language string
    :rtype: PayDictT._0100 | PayDictT.EMPTY
    """
    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload
        return {}

    return {
        SZ_LANGUAGE: hex_to_str(payload[2:6]),
        "_unknown_0": payload[6:],
    }


# unknown_01d0, from a HR91 (when its buttons are pushed)
@register_parser("01D0")
def parser_01d0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 01d0 packet (HR91 button push).

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the unknown state value
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload value is not recognized.
    """
    assert payload[2:] in ("00", "03"), _INFORM_DEV_MSG
    return {
        "unknown_0": payload[2:],
    }


# unknown_01e9, from a HR91 (when its buttons are pushed)
@register_parser("01E9")
def parser_01e9(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 01e9 packet (HR91 button push).

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the unknown state value
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload value is not recognized.
    """
    assert payload[2:] in ("00", "03"), _INFORM_DEV_MSG
    return {
        "unknown_0": payload[2:],
    }


# zone_schedule (fragment)
@register_parser("0404")
def parser_0404(payload: str, msg: Message) -> PayDictT._0404:
    """Parse the 0404 (zone_schedule) fragment.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing schedule fragment data and total fragments
    :rtype: PayDictT._0404
    :raises PacketPayloadInvalid: If the fragment length does not match the header.
    :raises AssertionError: If internal context bytes are invalid.
    """
    assert payload[4:6] in ("00", payload[:2]), _INFORM_DEV_MSG

    if int(payload[8:10], 16) * 2 != (frag_length := len(payload[14:])) and (
        msg.verb != I_ or frag_length != 0
    ):
        raise exc.PacketPayloadInvalid(f"Incorrect fragment length: 0x{payload[8:10]}")

    if msg.verb == RQ:  # have a ctx: idx|frag_idx
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: None if payload[12:14] == "00" else int(payload[12:14], 16),
        }

    if msg.verb == I_:  # have a ctx: idx|frag_idx
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: int(payload[12:14], 16),
            SZ_FRAG_LENGTH: None if payload[8:10] == "00" else int(payload[8:10], 16),
        }

    if payload[12:14] == FF:
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: None,
        }

    return {
        SZ_FRAG_NUMBER: int(payload[10:12], 16),
        SZ_TOTAL_FRAGS: int(payload[12:14], 16),
        SZ_FRAG_LENGTH: None if payload[8:10] == "FF" else int(payload[8:10], 16),
        SZ_FRAGMENT: payload[14:],
    }


# system_fault (fault_log_entry) - needs refactoring
@register_parser("0418")
def parser_0418(payload: str, msg: Message) -> PayDictT._0418 | PayDictT._0418_NULL:
    """Parse the 0418 (system_fault) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing a fault log entry or null entry
    :rtype: PayDictT._0418 | PayDictT._0418_NULL
    """
    null_result: PayDictT._0418_NULL
    full_result: PayDictT._0418

    if msg.verb == RQ:  # has a ctx: log_idx
        null_result = {SZ_LOG_IDX: payload[4:6]}  # type: ignore[typeddict-item]
        return null_result

    elif hex_to_dts(payload[18:30]) is None:
        null_result = {SZ_LOG_ENTRY: None}
        if msg.verb == I_:
            null_result = {SZ_LOG_IDX: payload[4:6]} | null_result  # type: ignore[assignment]
        return null_result

    try:
        assert payload[2:4] in FAULT_STATE, f"fault state: {payload[2:4]}"
        assert payload[8:10] in FAULT_TYPE, f"fault type: {payload[8:10]}"
        assert payload[12:14] in FAULT_DEVICE_CLASS, f"device class: {payload[12:14]}"
        assert int(payload[10:12], 16) < 16 or (
            payload[10:12] in ("1C", F6, F9, FA, FC)
        ), f"domain id: {payload[10:12]}"
    except AssertionError as err:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG} ({err}), with a photo of your fault log"
        )

    log_entry: PayDictT.FAULT_LOG_ENTRY = parse_fault_log_entry(payload)  # type: ignore[assignment]

    log_entry.pop(f"_{SZ_LOG_IDX}")  # type: ignore[misc]

    _KEYS = (SZ_TIMESTAMP, SZ_FAULT_STATE, SZ_FAULT_TYPE)
    entry = [v for k, v in log_entry.items() if k in _KEYS]

    if log_entry[SZ_DEVICE_CLASS] != FaultDeviceClass.ACTUATOR:
        entry.append(log_entry[SZ_DEVICE_CLASS])
    elif log_entry[SZ_DOMAIN_IDX] == FC:
        entry.append(DEV_ROLE_MAP[DevRole.APP])  # actual evohome UI
    elif log_entry[SZ_DOMAIN_IDX] == FA:
        entry.append(DEV_ROLE_MAP[DevRole.HTG])  # speculative
    elif log_entry[SZ_DOMAIN_IDX] == F9:
        entry.append(DEV_ROLE_MAP[DevRole.HT1])  # speculative
    else:
        entry.append(FaultDeviceClass.ACTUATOR)

    if log_entry[SZ_DEVICE_CLASS] != FaultDeviceClass.CONTROLLER:
        entry.append(log_entry[SZ_DOMAIN_IDX])

    if log_entry[SZ_DEVICE_ID] not in ("00:000000", "00:000001", "00:000002"):
        entry.append(log_entry[SZ_DEVICE_ID])

    entry.extend((payload[6:8], payload[14:18], payload[30:38]))

    full_result = {
        SZ_LOG_IDX: payload[4:6],  # type: ignore[typeddict-item]
        SZ_LOG_ENTRY: tuple([str(r) for r in entry]),
    }
    return full_result


# unknown_042f, from STA, VMS
@register_parser("042F")
def parser_042f(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 042f packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary of extracted hex counters
    :rtype: dict[str, Any]
    """
    return {
        "counter_1": f"0x{payload[2:6]}",
        "counter_3": f"0x{payload[6:10]}",
        "counter_5": f"0x{payload[10:14]}",
        "unknown_7": f"0x{payload[14:]}",
    }


# TODO: unknown_0b04, from THM (only when its a CTL?)
@register_parser("0B04")
def parser_0b04(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 0b04 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the unknown data value
    :rtype: dict[str, Any]
    """
    return {
        "unknown_1": payload[2:],
    }


# device_battery (battery_state)
@register_parser("1060")
def parser_1060(payload: str, msg: Message) -> PayDictT._1060:
    """Parse the 1060 (device_battery) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing battery low status and level percentage
    :rtype: PayDictT._1060
    :raises AssertionError: If the message length is invalid.
    """
    assert msg.len == 3, msg.len
    assert payload[4:6] in ("00", "01")

    return {
        "battery_low": payload[4:] == "00",
        "battery_level": None if payload[2:4] == "00" else hex_to_percent(payload[2:4]),
    }


# device_info
@register_parser("10E0")
def parser_10e0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 10e0 (device_info) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary of device specifications and manufacturing data
    :rtype: dict[str, Any]
    :raises AssertionError: If the message length is invalid for the reported signature.
    """
    if payload == "00":  # some HVAC devices will RP|10E0|00
        return {}

    assert msg.len in (19, 28, 29, 30, 36, 38), msg.len  # >= 19, msg.len

    payload = re.sub("(00)*$", "", payload)  # remove trailing 00s
    assert len(payload) >= 18 * 2

    try:  # DEX
        check_signature(msg.src.type, payload[2:20])
    except ValueError as err:
        _LOGGER.info(
            f"{msg!r} < {_INFORM_DEV_MSG}, with the make/model of device: {msg.src} ({err})"
        )

    description, _, unknown = payload[36:].partition("00")
    if len(description) % 2 != 0:
        description = description + "0"
        if not unknown.startswith("0"):  # expected for '000'
            _LOGGER.debug("Unexpected 2E10 payload: %s", payload)
        unknown = unknown[1:]  # trim first char

    result = {
        SZ_OEM_CODE: payload[14:16],
        "manufacturer_sub_id": payload[6:8],
        "product_id": payload[8:10],
        "date_1": hex_to_date(payload[28:36]) or "0000-00-00",
        "date_2": hex_to_date(payload[20:28]) or "0000-00-00",
        "description": bytearray.fromhex(description).decode(),
    }
    if unknown:
        result["_unknown"] = unknown
    return result


# device_id
@register_parser("10E1")
def parser_10e1(payload: str, msg: Message) -> PayDictT._10E1:
    """Parse the 10e1 (device_id) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the device ID
    :rtype: PayDictT._10E1
    """
    return {SZ_DEVICE_ID: hex_id_to_dev_id(payload[2:])}


# outdoor temperature
@register_parser("1290")
def parser_1290(payload: str, msg: Message) -> PayDictT._1290:
    """Parse the 1290 (outdoor_temp) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the outdoor temperature
    :rtype: PayDictT._1290
    """
    return parse_outdoor_temp(payload[2:])


# system_sync
@register_parser("1F09")
def parser_1f09(payload: str, msg: Message) -> PayDictT._1F09:
    """Parse the 1f09 (system_sync) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary with remaining seconds and the calculated next sync time
    :rtype: PayDictT._1F09
    :raises AssertionError: If the packet length is not 3.
    """
    assert msg.len == 3, f"length is {msg.len}, expecting 3"
    assert payload[:2] in ("00", "01", F8, FF)  # W/F8

    seconds = int(payload[2:6], 16) / 10
    next_sync = msg.dtm + td(seconds=seconds)

    return {
        "remaining_seconds": seconds,
        "_next_sync": dt.strftime(next_sync, "%H:%M:%S"),
    }


# rf_bind
@register_parser("1FC9")
def parser_1fc9(payload: str, msg: Message) -> PayDictT._1FC9:
    """Parse the 1fc9 (rf_bind) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary identifying the binding phase (Offer/Accept/Confirm) and bindings
    :rtype: PayDictT._1FC9
    :raises PacketPayloadInvalid: If the binding format is unknown.
    :raises AssertionError: If the payload length or constants are invalid.
    """

    def _parser(seqx: str) -> list[str]:
        if seqx[:2] not in ("90",):
            assert seqx[6:] == payload[6:12], f"{seqx[6:]} != {payload[6:12]}"
        if seqx[:2] not in (
            "21",
            "63",
            "65",
            "66",
            "67",
            "6C",
            "90",
            F6,
            F9,
            FA,
            FB,
            FC,
            FF,
        ):
            assert int(seqx[:2], 16) < 16, _INFORM_DEV_MSG
        return [seqx[:2], seqx[2:6], hex_id_to_dev_id(seqx[6:])]

    if msg.verb == I_ and msg.dst.id in (msg.src.id, ALL_DEV_ADDR.id):
        bind_phase = SZ_OFFER
    elif msg.verb == W_ and msg.src is not msg.dst:
        bind_phase = SZ_ACCEPT
    elif msg.verb == I_:
        bind_phase = SZ_CONFIRM
    elif msg.verb == RP:
        bind_phase = None
    else:
        raise exc.PacketPayloadInvalid("Unknown binding format")

    if len(payload) == 2 and bind_phase == SZ_CONFIRM:
        return {SZ_PHASE: bind_phase, SZ_BINDINGS: [[payload]]}

    assert msg.len >= 6 and msg.len % 6 == 0, msg.len
    assert msg.verb in (I_, W_, RP), msg.verb
    bindings = [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]
    return {SZ_PHASE: bind_phase, SZ_BINDINGS: bindings}


# system_mode
@register_parser("2E04")
def parser_2e04(payload: str, msg: Message) -> PayDictT._2E04:
    """Parse the 2e04 (system_mode) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the system mode and optional duration
    :rtype: PayDictT._2E04
    :raises AssertionError: If the system mode or packet length is invalid.
    """
    _LOGGER.warning(
        "RAMSES 2E04 %s from %s to %s: payload=%s [%s]",
        msg.verb,
        msg.src.id,
        msg.dst.id,
        payload,
        dt.now().isoformat(timespec='seconds'),
    )

    if msg.len == 8:  # evohome
        assert payload[:2] in SYS_MODE_MAP, f"Unknown system mode: {payload[:2]}"

    elif msg.len == 16:  # hometronics, lifestyle ID:
        assert 0 <= int(payload[:2], 16) <= 15 or payload[:2] == FF, payload[:2]
        assert payload[16:18] in (SYS_MODE_MAP.AUTO, SYS_MODE_MAP.CUSTOM), payload[
            16:18
        ]
        assert payload[30:32] == SYS_MODE_MAP.DAY_OFF, payload[30:32]

    else:
        assert False, f"Packet length is {msg.len} (expecting 8, 16)"

    result: PayDictT._2E04 = {SZ_SYSTEM_MODE: SYS_MODE_MAP[payload[:2]]}
    if payload[:2] not in (
        SYS_MODE_MAP.AUTO,
        SYS_MODE_MAP.HEAT_OFF,
        SYS_MODE_MAP.AUTO_WITH_RESET,
    ):
        result.update(
            {SZ_UNTIL: hex_to_dtm(payload[2:14]) if payload[14:16] != "00" else None}
        )
    return result


# presence_detect, HVAC sensor, or Timed boost for Vasco D60
@register_parser("2E10")
def parser_2e10(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2e10 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary defining if presence is detected
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload is not in a recognized format.
    """
    assert payload in ("0001", "000000", "000100"), _INFORM_DEV_MSG
    presence: int = int(payload[2:4])
    return {
        "presence_detected": bool(presence),
        "_unknown_4": payload[4:],
    }


# datetime
@register_parser("313F")
def parser_313f(payload: str, msg: Message) -> PayDictT._313F:
    """Parse the 313f (datetime) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the datetime and DST flag
    :rtype: PayDictT._313F
    :raises AssertionError: If the payload context is unexpected for the source device type.
    """
    assert msg.src.type != DEV_TYPE_MAP.CTL or payload[2:4] in (
        "F0",
        "F9",
        "FC",
    ), f"{payload[2:4]} unexpected for CTL"
    assert (
        msg.src.type not in (DEV_TYPE_MAP.DTS, DEV_TYPE_MAP.DT2) or payload[2:4] == "38"
    ), f"{payload[2:4]} unexpected for DTS"
    assert msg.src.type != DEV_TYPE_MAP.RFG or payload[2:4] == "60", (
        "{payload[2:4]} unexpected for RFG"
    )

    return {
        SZ_DATETIME: hex_to_dtm(payload[4:18]),
        SZ_IS_DST: True if bool(int(payload[4:6], 16) & 0x80) else None,
        "_unknown_0": payload[2:4],
    }


# WIP: unknown
@register_parser("3222")
def parser_3222(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 3222 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing offset, length, and raw data
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload prefix is not '00'.
    """
    assert payload[:2] == "00"

    if msg.len == 3:
        assert payload[4:] == "00"
        return {
            "_value": f"0x{payload[2:4]}",
        }

    return {
        "offset": f"0x{payload[2:4]}",
        "length": f"0x{payload[4:6]}",
        "_data": f"{'..' * int(payload[2:4])}{payload[6:]}",
    }


# faked puzzle pkt shouldn't be decorated
@register_parser("7FFF")
def parser_7fff(payload: str, _: Message) -> dict[str, Any]:
    """Parse the 7fff (puzzle) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param _: The message object (unused)
    :return: A dictionary containing the message type, timestamp, and metadata
    :rtype: dict[str, Any]
    """
    if payload[:2] != "00":
        _LOGGER.debug("Invalid/deprecated Puzzle packet")
        return {
            "msg_type": payload[:2],
            SZ_PAYLOAD: hex_to_str(payload[2:]),
        }

    if payload[2:4] not in LOOKUP_PUZZ:
        _LOGGER.debug("Invalid/deprecated Puzzle packet")
        return {
            "msg_type": payload[2:4],
            "message": hex_to_str(payload[4:]),
        }

    result: dict[str, None | str] = {}
    if int(payload[2:4]) >= int("20", 16):
        dtm = dt.fromtimestamp(int(payload[4:16], 16) / 1e7)  # TZ-naive
        result["datetime"] = dtm.isoformat(timespec="milliseconds")
    elif payload[2:4] != "13":
        dtm = dt.fromtimestamp(int(payload[4:16], 16) / 1000)  # TZ-naive
        result["datetime"] = dtm.isoformat(timespec="milliseconds")

    msg_type = LOOKUP_PUZZ.get(payload[2:4], SZ_PAYLOAD)

    if payload[2:4] == "11":
        mesg = hex_to_str(payload[16:])
        result[msg_type] = f"{mesg[:4]}|{mesg[4:6]}|{mesg[6:]}"

    elif payload[2:4] == "13":
        result[msg_type] = hex_to_str(payload[4:])

    elif payload[2:4] == "7F":
        result[msg_type] = payload[4:]

    else:
        result[msg_type] = hex_to_str(payload[16:])

    return {**result, "parser": f"v{VERSION}"}

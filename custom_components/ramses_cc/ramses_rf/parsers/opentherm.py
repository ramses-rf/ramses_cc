"""RAMSES RF - OpenTherm payload parsers.

This module provides parsers for standard RAMSES RF packets that encapsulate
or interact with the OpenTherm protocol and OTB bridges.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ...ramses_tx import exceptions as exc
from ...ramses_tx.const import RQ, SZ_PAYLOAD, SZ_VALUE
from ...ramses_tx.helpers import hex_to_flag8, hex_to_percent, parse_valve_demand
from ...ramses_tx.typing import PayDictT

from ..protocol.opentherm import (
    EN,
    SZ_DESCRIPTION,
    SZ_MSG_ID,
    SZ_MSG_NAME,
    SZ_MSG_TYPE,
    OtMsgType,
    decode_frame,
)
from .registry import register_parser

if TYPE_CHECKING:
    from ..messages import Message

_LOGGER = logging.getLogger(__name__)
_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"


# unknown_0150, from OTB
@register_parser("0150")
def parser_0150(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 0150 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload is not the expected '000000'.
    """
    assert payload == "000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# unknown_1098, from OTB
@register_parser("1098")
def parser_1098(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 1098 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the raw payload and its interpreted value
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload does not match expected constants.
    """
    assert payload == "00C8", _INFORM_DEV_MSG

    return {
        "_payload": payload,
        "_value": {"00": False, "C8": True}.get(
            payload[2:], hex_to_percent(payload[2:])
        ),
    }


# unknown_10b0, from OTB
@register_parser("10B0")
def parser_10b0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 10b0 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the raw payload and interpreted value
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload is invalid.
    """
    assert payload == "0000", _INFORM_DEV_MSG

    return {
        "_payload": payload,
        "_value": {"00": False, "C8": True}.get(
            payload[2:], hex_to_percent(payload[2:])
        ),
    }


# unknown_1fd0, from OTB
@register_parser("1FD0")
def parser_1fd0(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 1fd0 (OpenTherm) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload does not match the expected null string.
    """
    assert payload == "0000000000000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# opentherm_sync, otb_sync
@register_parser("1FD4")
def parser_1fd4(payload: str, msg: Message) -> PayDictT._1FD4:
    """Parse the 1fd4 (opentherm_sync) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the sync ticker value
    :rtype: PayDictT._1FD4
    """
    return {"ticker": int(payload[2:], 16)}


# unknown_2400, from OTB, FAN
@register_parser("2400")
def parser_2400(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2400 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    """
    # RP --- 32:155617 18:005904 --:------ 2400 045 00001111-1010929292921110101020110010000080100010100000009191111191910011119191111111111100  # Orcon FAN
    # RP --- 10:048122 18:006402 --:------ 2400 004 0000000F
    # assert payload == "0000000F", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# unknown_2401, from OTB
@register_parser("2401")
def parser_2401(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2401 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of decoded flags and valve demand
    :rtype: dict[str, Any]
    :raises AssertionError: If payload constants or bit flags are unrecognized.
    """
    try:
        assert payload[2:4] == "00", f"byte 1: {payload[2:4]}"
        assert int(payload[4:6], 16) & 0b11110000 == 0, (
            f"byte 2: {hex_to_flag8(payload[4:6])}"
        )
        assert int(payload[6:], 0x10) <= 200, f"byte 3: {payload[6:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        "_flags_2": hex_to_flag8(payload[4:6]),
        **parse_valve_demand(payload[6:8]),  # ~3150|FC
        "_value_2": int(payload[4:6], 0x10),
    }


# unknown_2410, from OTB, FAN
@register_parser("2410")
def parser_2410(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2410 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of current, min, and max values and metadata
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload format does not match expected constants.
    """
    # RP --- 10:048122 18:006402 --:------ 2410 020 00-00000000-00000000-00000001-00000001-00000C  # OTB
    # RP --- 32:155617 18:005904 --:------ 2410 020 00-00003EE8-00000000-FFFFFFFF-00000000-1002A6  # Orcon Fan

    def unstuff(seqx: str) -> tuple[bool, int, str | int]:
        val = int(seqx, 16)
        signed = bool(val & 0x80)
        length = (val >> 3 & 0x07) or 1
        d_type = {0b000: "a", 0b001: "b", 0b010: "c", 0b100: "d"}.get(
            val & 0x07, val & 0x07
        )
        return signed, length, d_type

    try:
        assert payload[:6] == "00" * 3, _INFORM_DEV_MSG
        assert payload[10:18] == "00" * 4, _INFORM_DEV_MSG
        assert payload[18:26] in ("00000001", "FFFFFFFF"), _INFORM_DEV_MSG
        assert payload[26:34] in ("00000001", "00000000"), _INFORM_DEV_MSG
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        "tail": payload[34:],
        "xxx_34": unstuff(payload[34:36]),
        "xxx_36": unstuff(payload[36:38]),
        "xxx_38": unstuff(payload[38:]),
        "cur_value": payload[2:10],
        "min_value": payload[10:18],
        "max_value": payload[18:26],
        "oth_value": payload[26:34],
    }


# unknown_2420, from OTB
@register_parser("2420")
def parser_2420(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 2420 (OpenTherm) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the raw payload
    :rtype: dict[str, Any]
    :raises AssertionError: If the payload does not match the expected constant string.
    """
    assert payload == "00000010" + "00" * 34, _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# opentherm_msg, from OTB (and OT_RND)
@register_parser("3220")
def parser_3220(payload: str, msg: Message) -> dict[str, Any]:
    """Parse an OpenTherm message packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of decoded OpenTherm data and descriptions
    :rtype: dict[str, Any]
    :raises AssertionError: If internal OpenTherm consistency checks fail.
    :raises PacketPayloadInvalid: If the OpenTherm frame is malformed.
    """
    try:
        ot_type, ot_id, ot_value, ot_schema = decode_frame(payload[2:10])
    except AssertionError as err:
        raise AssertionError(f"OpenTherm: {err}") from err
    except ValueError as err:
        raise exc.PacketPayloadInvalid(f"OpenTherm: {err}") from err

    # NOTE: Unknown-DataId isn't an invalid payload & is useful to train the OTB device
    if ot_schema is None and ot_type != OtMsgType.UNKNOWN_DATAID:  # type: ignore[unreachable]
        raise exc.PacketPayloadInvalid(
            f"OpenTherm: Unknown data-id: 0x{ot_id:02X} ({ot_id})"
        )

    result = {
        SZ_MSG_ID: ot_id,
        SZ_MSG_TYPE: str(ot_type),
        SZ_MSG_NAME: ot_value.pop(SZ_MSG_NAME, None),
    }

    if msg.verb == RQ:  # RQs have a context: msg_id (and a payload)
        assert (
            ot_type != OtMsgType.READ_DATA
            or payload[6:10] == "0000"  # likely true for RAMSES
        ), f"OpenTherm: Invalid msg-type|data-value: {ot_type}|{payload[6:10]}"

        if ot_type != OtMsgType.READ_DATA:
            assert ot_type in (
                OtMsgType.WRITE_DATA,
                OtMsgType.INVALID_DATA,
            ), f"OpenTherm: Invalid msg-type for RQ: {ot_type}"

            result.update(ot_value)  # TODO: find some of these packets to review

        result[SZ_DESCRIPTION] = ot_schema.get(EN) if ot_schema else None
        return result

    _LIST = (OtMsgType.DATA_INVALID, OtMsgType.UNKNOWN_DATAID, OtMsgType.RESERVED)
    assert ot_type not in _LIST or payload[6:10] in (
        "0000",
        "FFFF",
    ), f"OpenTherm: Invalid msg-type|data-value: {ot_type}|{payload[6:10]}"

    # HACK: These OT data id's can pop in/out of 47AB, which is an invalid value
    if payload[6:] == "47AB":
        if ot_id in (0x12, 0x13, 0x19, 0x1A, 0x1B, 0x1C):
            ot_value[SZ_VALUE] = None
    # HACK: This OT data id can be 1980, which is an invalid value
    if payload[6:] == "1980":
        if ot_id:  # CH pressure is 25.5 bar!
            ot_value[SZ_VALUE] = None

    if ot_type not in _LIST:
        assert ot_type in (
            OtMsgType.READ_ACK,
            OtMsgType.WRITE_ACK,
        ), f"OpenTherm: Invalid msg-type for RP: {ot_type}"

        result.update(ot_value)

    try:  # These are checking flags in payload of data-id 0x00
        assert ot_id != 0 or (
            [result[SZ_VALUE][i] for i in (2, 3, 4, 5, 6, 7)] == [0] * 6
        ), result[SZ_VALUE]

        assert ot_id != 0 or (
            [result[SZ_VALUE][8 + i] for i in (0, 4, 5, 6, 7)] == [0] * 5
        ), result[SZ_VALUE]

    except AssertionError:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG}, with a description of your system"
        )

    result[SZ_DESCRIPTION] = ot_schema.get(EN) if ot_schema else None
    return result


# unknown_3221, from OTB, FAN
@register_parser("3221")
def parser_3221(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 3221 packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the extracted numeric value
    :rtype: dict[str, Any]
    :raises AssertionError: If the extracted value exceeds the valid 0xC8 threshold.
    """
    # RP --- 10:052644 18:198151 --:------ 3221 002 000F
    # RP --- 10:048122 18:006402 --:------ 3221 002 0000
    # RP --- 32:155617 18:005904 --:------ 3221 002 000A

    assert int(payload[2:], 16) <= 0xC8, _INFORM_DEV_MSG

    return {
        "_payload": payload,
        SZ_VALUE: int(payload[2:], 16),
    }


# unknown_3223, from OTB
@register_parser("3223")
def parser_3223(payload: str, msg: Message) -> dict[str, Any]:
    """Parse the 3223 (OpenTherm) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the extracted value
    :rtype: dict[str, Any]
    :raises AssertionError: If the value exceeds the valid 0xC8 threshold.
    """
    assert int(payload[2:], 16) <= 0xC8, _INFORM_DEV_MSG

    return {
        "_payload": payload,
        SZ_VALUE: int(payload[2:], 16),
    }

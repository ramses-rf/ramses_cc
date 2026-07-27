"""RAMSES RF - Domestic Hot Water (DHW) payload parsers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...ramses_tx.const import (
    RQ,
    SZ_ACTIVE,
    SZ_DHW_FLOW_RATE,
    SZ_MODE,
    SZ_SETPOINT,
    SZ_TEMPERATURE,
    SZ_UNTIL,
    ZON_MODE_MAP,
)
from ...ramses_tx.helpers import hex_to_dtm, hex_to_temp
from ...ramses_tx.typing import PayDictT

from .registry import register_parser

if TYPE_CHECKING:
    from ..messages import Message


# dhw (cylinder) params  # FIXME: a bit messy
@register_parser("10A0")
def parser_10a0(payload: str, msg: Message) -> PayDictT._10A0 | PayDictT.EMPTY:
    """Parse the 10a0 (dhw_params) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary of DHW parameters or an empty dictionary
    :rtype: PayDictT._10A0 | PayDictT.EMPTY
    :raises AssertionError: If the message length or valve index is invalid.
    """
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1087-00-03E4  # RQ/RP, every 24h
    # RP --- 01:145038 07:045960 --:------ 10A0 006 00-109A-00-03E8
    # RP --- 10:048122 18:006402 --:------ 10A0 003 00-1B58

    # these may not be reliable...
    # RQ --- 01:136410 10:067219 --:------ 10A0 002 0000
    # RQ --- 07:017494 01:078710 --:------ 10A0 006 00-1566-00-03E4

    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-31FF-00-31FF  # null
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1770-00-03E8
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1374-00-03E4
    # RQ --- 07:030741 01:102458 --:------ 10A0 006 00-181F-00-03E4
    # RQ --- 07:036831 23:100224 --:------ 10A0 006 01-1566-00-03E4  # non-evohome

    # these from a RFG...
    # RQ --- 30:185469 01:037519 --:------ 0005 002 000E
    # RP --- 01:037519 30:185469 --:------ 0005 004 000E0300  # two DHW valves
    # RQ --- 30:185469 01:037519 --:------ 10A0 001 01 (01 )

    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        # 045 RQ --- 07:045960 01:145038 --:------ 10A0 006 0013740003E4
        # 037 RQ --- 18:013393 01:145038 --:------ 10A0 001 00
        # 054 RP --- 01:145038 18:013393 --:------ 10A0 006 0013880003E8
        return {}

    assert msg.len in (1, 3, 6), msg.len  # OTB uses 3, evohome uses 6
    assert payload[:2] in ("00", "01"), payload[:2]  # can be two DHW valves/system

    result: PayDictT._10A0 = {}  # type: ignore[typeddict-item]
    if msg.len >= 2:
        setpoint = hex_to_temp(payload[2:6])  # 255 for OTB? iff no DHW?
        result = {SZ_SETPOINT: None if setpoint == 255 else setpoint}  # 30.0-85.0 C
    if msg.len >= 4:
        result["overrun"] = int(payload[6:8], 16)  # 0-10 minutes
    if msg.len >= 6:
        result["differential"] = hex_to_temp(payload[8:12])  # 1.0-10.0 C

    return result


# dhw cylinder temperature
@register_parser("1260")
def parser_1260(payload: str, msg: Message) -> PayDictT._1260:
    """Parse the 1260 (dhw_temp) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the DHW temperature
    :rtype: PayDictT._1260
    """
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# dhw_flow_rate
@register_parser("12F0")
def parser_12f0(payload: str, msg: Message) -> PayDictT._12F0:
    """Parse the 12f0 (dhw_flow_rate) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing the DHW flow rate
    :rtype: PayDictT._12F0
    """
    return {SZ_DHW_FLOW_RATE: hex_to_temp(payload[2:])}


# dhw_mode
@register_parser("1F41")
def parser_1f41(payload: str, msg: Message) -> PayDictT._1F41:
    """Parse the 1f41 (dhw_mode) packet.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object
    :type msg: Message
    :return: A dictionary containing DHW mode, activity, and duration/until data
    :rtype: PayDictT._1F41
    :raises AssertionError: If payload constants or message lengths are invalid.
    """
    # 053 RP --- 01:145038 18:013393 --:------ 1F41 006 00FF00FFFFFF  # no stored DHW

    assert payload[4:6] in ZON_MODE_MAP, f"{payload[4:6]} (0xjj)"
    assert payload[4:6] == ZON_MODE_MAP.TEMPORARY or msg.len == 6, (
        f"{msg!r}: expected length 6"
    )
    assert payload[4:6] != ZON_MODE_MAP.TEMPORARY or msg.len == 12, (
        f"{msg!r}: expected length 12"
    )
    assert payload[6:12] == "FFFFFF", (
        f"{msg!r}: expected FFFFFF instead of '{payload[6:12]}'"
    )

    result: PayDictT._1F41 = {
        SZ_MODE: ZON_MODE_MAP.get(payload[4:6])  # type: ignore[typeddict-item]
    }
    if payload[2:4] != "FF":
        result[SZ_ACTIVE] = {"00": False, "01": True, "FF": None}[payload[2:4]]
    # if payload[4:6] == ZON_MODE_MAP.COUNTDOWN:
    #     result[SZ_UNTIL] = dtm_from_hex(payload[6:12])
    if payload[4:6] == ZON_MODE_MAP.TEMPORARY:
        result[SZ_UNTIL] = hex_to_dtm(payload[12:24])

    return result

"""RAMSES RF - Payload decoding pipeline.

This module provides the Chain of Responsibility pipeline for decoding
RAMSES RF payloads.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ..protocol.ramses import CODES_SCHEMA, RQ_IDX_COMPLEX
from ...ramses_tx import exceptions as exc
from ...ramses_tx.const import RQ, Code
from ...ramses_tx.helpers import hex_to_temp

from .registry import get_parser

if TYPE_CHECKING:
    from ..messages import Message

_LOGGER = logging.getLogger(__name__)
_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"


def parser_heartbeat(payload: str, msg: Message) -> dict[str, Any]:
    """Parse a 1-byte heartbeat packet (payload '00').

    :param payload: The raw hex payload (expected '00').
    :type payload: str
    :param msg: The message object containing context.
    :type msg: Message
    :return: A dictionary identifying the packet as a heartbeat.
    :rtype: dict[str, Any]
    """
    return {"heartbeat": True}


def parser_unknown(payload: str, msg: Message) -> dict[str, Any]:
    """Apply a generic parser for unrecognized packet codes.

    :param payload: The raw hex payload
    :type payload: str
    :param msg: The message object containing context
    :type msg: Message
    :return: A dictionary containing the raw payload and code information
    :rtype: dict[str, Any]
    """
    # TODO: it may be useful to generically search payloads for hex_ids

    # These are generic parsers
    if msg.len == 2 and payload[:2] == "00":
        return {
            "_payload": payload,
            "_value": {"00": False, "C8": True}.get(payload[2:], int(payload[2:], 16)),
        }

    if msg.len == 3 and payload[:2] == "00":
        return {
            "_payload": payload,
            "_value": hex_to_temp(payload[2:]),
        }

    return {
        "_payload": payload,
        "_unknown_code": msg.code,
        "_parse_error": "No parser available for this packet type",
    }


class PayloadDecoder(ABC):
    """Abstract base class for the payload decoder chain."""

    def __init__(self) -> None:
        """Initialize the base decoder state."""
        self._next_decoder: PayloadDecoder | None = None

    def set_next(self, decoder: PayloadDecoder) -> PayloadDecoder:
        """Set the next decoder in the chain.

        :param decoder: The next decoder
        :type decoder: PayloadDecoder
        :return: The next decoder
        :rtype: PayloadDecoder
        """
        self._next_decoder = decoder
        return decoder

    @abstractmethod
    def decode(
        self, msg: Message, payload_str: str, payload_len: int
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Decode the payload. Returns None to bypass index merging.

        :param msg: The message context
        :type msg: Message
        :param payload_str: The raw payload string
        :type payload_str: str
        :param payload_len: The payload length
        :type payload_len: int
        :return: The decoded result or None to trigger early bypass
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        if self._next_decoder:
            return self._next_decoder.decode(msg, payload_str, payload_len)
        return {}


class RegexValidatorDecoder(PayloadDecoder):
    """Decoder that evaluates empty payloads and validates constraints."""

    def decode(
        self, msg: Message, payload_str: str, payload_len: int
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Validate the payload string rules or execute early exit bypasses.

        :param msg: The message context
        :type msg: Message
        :param payload_str: The raw payload string
        :type payload_str: str
        :param payload_len: The payload length
        :type payload_len: int
        :return: The decoded result or None to signal early exit
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        :raises PacketPayloadInvalid: If the validation rules fail.
        """
        try:
            # Force packet evaluation via string representation
            _ = repr(msg._pkt)
        except Exception as err:
            raise exc.PacketPayloadInvalid(
                f"Packet formatting/evaluation failed: {err}"
            ) from err

        if msg.code in CODES_SCHEMA:
            if msg.verb in ("RQ", "RP", " I", " W"):
                regex = CODES_SCHEMA[msg.code].get(msg.verb)
                if regex and not bool(re.compile(str(regex)).match(payload_str)):
                    if not msg._has_payload:
                        return None  # Sentinel for fallback on nulls
                    raise exc.PacketPayloadInvalid(
                        f"Payload doesn't match {msg.verb}/{msg.code}: "
                        f"{payload_str} != {regex}"
                    )

        # Standard bypass rule for requests with null payloads
        if not msg._has_payload and (msg.verb == RQ and msg.code not in RQ_IDX_COMPLEX):
            return None

        if self._next_decoder:
            return self._next_decoder.decode(msg, payload_str, payload_len)
        return {}


class HeartbeatDecoder(PayloadDecoder):
    """Decoder that intercepts 1-byte '00' heartbeats."""

    def decode(
        self, msg: Message, payload_str: str, payload_len: int
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Intercept and decode heartbeat payloads.

        :param msg: The message context
        :type msg: Message
        :param payload_str: The raw payload string
        :type payload_str: str
        :param payload_len: The payload length
        :type payload_len: int
        :return: The decoded result
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        if payload_len == 1 and payload_str == "00" and msg.code != Code._1FC9:
            try:
                parser = get_parser(msg.code) or parser_unknown
                res = parser(payload_str, msg)
                if res == {}:
                    return None
            except Exception:
                pass
            return parser_heartbeat(payload_str, msg)

        if self._next_decoder:
            return self._next_decoder.decode(msg, payload_str, payload_len)
        return {}


class StandardParserDecoder(PayloadDecoder):
    """Decoder routing payload to the appropriate 4-digit code parser."""

    def decode(
        self, msg: Message, payload_str: str, payload_len: int
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Route payload to the relevant parser function.

        :param msg: The message context
        :type msg: Message
        :param payload_str: The raw payload string
        :type payload_str: str
        :param payload_len: The payload length
        :type payload_len: int
        :return: The decoded result
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        try:
            parser = get_parser(msg.code) or parser_unknown
            result = parser(payload_str, msg)
            if isinstance(result, dict) and msg.seqn and msg.seqn.isnumeric():
                result["seqx_num"] = msg.seqn
            # Narrowing to strictly list or dict
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result
            return {}
        except AssertionError as err:
            _LOGGER.warning(
                f"{msg!r} < {_INFORM_DEV_MSG} ({err}). "
                f"This packet could not be parsed completely."
            )
            err_result = {
                "_payload": payload_str,
                "_parse_error": f"AssertionError: {err}",
                "_unknown_code": msg.code,
            }
            if msg.seqn and msg.seqn.isnumeric():
                err_result["seqx_num"] = msg.seqn
            return err_result


class PayloadDecoderPipeline:
    """The Chain of Responsibility pipeline for decoding RAMSES payloads."""

    def __init__(self) -> None:
        """Initialize the decoder pipeline."""
        self.head = RegexValidatorDecoder()
        self.head.set_next(HeartbeatDecoder()).set_next(StandardParserDecoder())

    def decode(self, msg: Message) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Process a message through the payload decoding pipeline.

        :param msg: A Message object containing packet data
        :type msg: Message
        :return: A dict of key:value pairs or a list of such dicts
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        payload_str: str = getattr(
            msg._pkt, "payload", getattr(msg._pkt, "_payload", "")
        )
        payload_len: int = getattr(msg._pkt, "len", getattr(msg._pkt, "_len", 0))

        return self.head.decode(msg, payload_str, payload_len)


def re_compile_re_match(regex: str, string: str) -> bool:
    """Check if the provided string matches the regex pattern.

    :param regex: The regex pattern string
    :type regex: str
    :param string: The text payload to test
    :type string: str
    :return: True if matched, False otherwise
    :rtype: bool
    """
    return bool(re.compile(regex).match(string))


def _check_msg_payload(msg: Message, payload: str) -> None:
    """Validate a packet's payload against its verb/code pair.

    :param msg: The message object being validated
    :type msg: Message
    :param payload: The raw hex payload string
    :type payload: str
    :raises PacketInvalid: If the code or verb/code pair is unknown.
    :raises PacketPayloadInvalid: If payload doesn't match expected regex.
    """
    try:
        # Force packet evaluation via string representation
        _ = repr(msg._pkt)
    except Exception as err:
        raise exc.PacketPayloadInvalid(
            f"Packet formatting/evaluation failed: {err}"
        ) from err

    if msg.code not in CODES_SCHEMA:
        raise exc.PacketInvalid(f"Unknown code: {msg.code}")

    if msg.verb not in ("RQ", "RP", " I", " W"):
        raise exc.PacketInvalid(f"Unknown verb/code pair: {msg.verb}/{msg.code}")

    regex = CODES_SCHEMA[msg.code].get(msg.verb)

    if not regex:
        raise exc.PacketInvalid(f"Unknown verb/code pair: {msg.verb}/{msg.code}")

    if not re_compile_re_match(str(regex), payload):
        raise exc.PacketPayloadInvalid(
            f"Payload doesn't match {msg.verb}/{msg.code}: {payload} != {regex}"
        )

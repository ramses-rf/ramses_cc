"""RAMSES RF - DTO-based Payload Decoder module.

This module provides the entry point for decoding L2 PacketDTOs into
L7 semantic dictionaries, strictly separating domain logic from transport.
"""

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from ..protocol_schema import (
    CODE_IDX_ARE_COMPLEX,
    CODE_IDX_ARE_NONE,
    CODE_IDX_ARE_SIMPLE,
    CODES_ONLY_FROM_CTL,
    CODES_SCHEMA,
    CODES_WITH_ARRAYS,
    RQ_IDX_COMPLEX,
    RQ_NO_PAYLOAD,
)
from ...ramses_tx import exceptions as exc
from ...ramses_tx.const import Code
from ...ramses_tx.dtos import PacketDTO

from .registry import get_parser


# --- Persistent packet file logger (protocol analysis) ---
_PKT_LOG_PATH = "/config/ramses_packets.log"
_PKT_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

_pkt_logger = logging.getLogger("ramses_rf.packet_log")
_pkt_logger.setLevel(logging.DEBUG)
_pkt_logger.propagate = False  # don't send to root logger
try:
    _pkt_handler = RotatingFileHandler(
        _PKT_LOG_PATH, maxBytes=_PKT_LOG_MAX_BYTES, backupCount=1
    )
    _pkt_handler.setFormatter(logging.Formatter("%(message)s"))
    _pkt_logger.addHandler(_pkt_handler)
except OSError:
    pass  # can't write to log file, fail silently

_pkt_log_last = ""  # dedup: skip consecutive identical packets


def _log_packet_to_file(code: str, verb: str, src: str, dst: str, payload: str) -> None:
    """Append a packet line to the persistent log file with dedup."""
    global _pkt_log_last

    ts = datetime.now().isoformat(timespec='seconds')
    line = f"{ts} {code} {verb} {src}\u2192{dst} {payload}"
    dedup_key = f"{ts} {code} {verb} {src} {dst} {payload}"
    if dedup_key == _pkt_log_last:
        return  # skip duplicate (same packet processed multiple times within same second)
    _pkt_log_last = dedup_key

    _pkt_logger.debug(line)


# Constants
_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"

# Variables
_LOGGER = logging.getLogger(__name__)


def _get_code(code_str: str) -> Code | None:
    """Safely convert a string to a Code enum, returning None if invalid.

    :param code_str: The raw string representation of the packet code.
    :return: A matching Code enum object or None if invalid.
    """
    try:
        return Code(code_str)
    except ValueError:
        return None


class _LegacyAddress:
    """Adapter class to mimic ramses_tx.Address for legacy parsers."""

    def __init__(self, addr_str: str) -> None:
        """Initialize the legacy address with id and type.

        :param addr_str: The raw address string instance.
        """
        self.id = addr_str
        self.type = addr_str.split(":")[0] if ":" in addr_str else ""

    def __eq__(self, other: Any) -> bool:
        """Evaluate equality based on address ID.

        :param other: The other object to compare against.
        :return: True if addresses match, otherwise NotImplemented.
        """
        if hasattr(other, "id"):
            return bool(self.id == other.id)
        return NotImplemented


class _LegacyMessage:
    """Anti-corruption adapter mimicking the legacy ramses_tx.Message interface."""

    def __init__(self, dto: PacketDTO) -> None:
        """Initialize the legacy message using data strictly from the DTO.

        :param dto: The data transfer object representation of the packet.
        """
        self.verb = dto.verb  # Do not strip: Preserves padding for I_ (" I")
        self.seqn = dto.seq
        try:
            self.len = int(dto.length)
        except ValueError:
            self.len = 0

        self.code = dto.code
        self.code_enum = _get_code(self.code)
        self.payload = dto.payload
        self._len = int(len(self.payload) / 2)

        # Instance interning cache to support Python 'is'/'is not' identity checks
        self._addr_cache: dict[str, _LegacyAddress] = {}

        raw_addrs = [dto.addr1, dto.addr2, dto.addr3]
        valid_addrs = [a for a in raw_addrs if a and a != "--:------"]

        src_id = valid_addrs[0] if valid_addrs else "--:------"
        dst_id = valid_addrs[1] if len(valid_addrs) > 1 else "--:------"

        if src_id == dst_id:
            dst_id = src_id

        self.src = self._get_addr(src_id)
        self.dst = self._get_addr(dst_id)

        self._addrs = [
            self._get_addr(dto.addr1),
            self._get_addr(dto.addr2),
            self._get_addr(dto.addr3),
        ]

        self.dtm = dto.timestamp
        _tz = getattr(self.dtm, "tzinfo", None)
        if _tz is not None and hasattr(self.dtm, "replace"):
            self.dtm = self.dtm.replace(tzinfo=None)

        self._has_ctl_: bool | None = None
        self._idx_: bool | str | None = None
        self._ctx_: bool | str | None = None

        self._has_array = self._calculate_has_array()

    def _get_addr(self, addr_str: str) -> _LegacyAddress:
        """Retrieve or create an interned legacy address instance.

        :param addr_str: The address identity string.
        :return: A cached or newly instantiated _LegacyAddress object.
        """
        if addr_str not in self._addr_cache:
            self._addr_cache[addr_str] = _LegacyAddress(addr_str)
        return self._addr_cache[addr_str]

    @property
    def _has_payload(self) -> bool:
        """Return False if there is no payload, matching legacy message.py exactly.

        :return: True if a parsable payload is expected, otherwise False.
        """
        if self._len == 1:
            return False
        if self.verb.strip() == "RQ":
            if self.code_enum in RQ_NO_PAYLOAD:
                return False
            if self._len == 2 and self.code != "0016":
                return False
        return True

    def _calculate_has_array(self) -> bool:
        """Determine if the payload represents an array.

        :return: True if payload structure models an array configuration.
        """
        if self.code == "1FC9":
            return self.verb.strip() != "RQ"

        if self.verb.strip() != "I" or self.code_enum not in CODES_WITH_ARRAYS:
            return False

        element_len = CODES_WITH_ARRAYS[self.code_enum][0]
        assert isinstance(element_len, int)

        if self._len != element_len:
            a, b = divmod(self._len, element_len)
            return bool(a > 0 and b == 0)

        return bool(
            self.code in ("22C9", "3150")
            and self.src.type == "02"
            and self.src.id == self.dst.id
            and self.payload[:1] != "F"
        )

    @property
    def _has_ctl(self) -> bool:
        """Return True if the packet is to/from a controller.

        :return: Boolean evaluation denoting controller interaction flags.
        """
        if self._has_ctl_ is not None:
            return self._has_ctl_

        if {self.src.type, self.dst.type} & {"01", "02", "23"}:
            self._has_ctl_ = True
        elif self.dst.id == self.src.id:
            self._has_ctl_ = any(
                (
                    self.code == "3B00" and self.payload[:2] == "FC",
                    self.code_enum
                    in tuple(CODES_ONLY_FROM_CTL) + (Code._31D9, Code._31DA),
                )
            )
        elif self.dst.id == "--:------":
            self._has_ctl_ = self.src.type != "10"  # OTB (OpenTherm Bridge)
        elif self.dst.type in ("04", "22"):
            self._has_ctl_ = True
        else:
            self._has_ctl_ = False

        return self._has_ctl_

    @property
    def _idx(self) -> bool | str:
        """Return the payload's index, if any.

        :return: String value of index parameter or boolean flag state.
        """
        if self._idx_ is not None:
            return self._idx_

        res = self._pkt_idx()
        self._idx_ = res if res is not None else False
        return self._idx_

    def _pkt_idx(self) -> bool | str | None:
        """Extract the exact index leveraging protocol_schema definitions.

        :return: String or boolean representation if index parsing passes constraints.
        :raises exc.PacketPayloadInvalid: If structural legacy checks fail validations.
        """
        if self.code == "0005":
            return self._has_array

        if self.code == "0009" and self.src.type == "10":
            return False

        if self.code == "000C":
            if self.payload[2:4] == "000F":
                return "FC"
            if self.payload[0:4] == "010E":
                return "F9"
            if self.payload[2:4] in ("000D", "000E"):
                return "FA"
            return self.payload[:2]

        if self.code == "0404":
            return "HW" if self.payload[2:4] == "23" else self.payload[:2]

        if self.code == "0418":
            return self.payload[4:6]

        if self.code == "1100":
            return self.payload[:2] if self.payload[:1] == "F" else False

        if self.code == "3220":
            return self.payload[4:6]

        if self.code_enum in CODE_IDX_ARE_COMPLEX:
            pass

        if self.code_enum in CODE_IDX_ARE_NONE:
            if self.code_enum in CODES_SCHEMA:
                regex_str = str(CODES_SCHEMA[self.code_enum].get(self.verb, ""))
                if regex_str.startswith("^00") and self.payload[:2] != "00":
                    raise exc.PacketPayloadInvalid(
                        f"Packet idx is {self.payload[:2]}, but expecting no idx (00) (0xAA)"
                    )
            return False

        if self._has_array:
            return True

        if self.payload[:2] in ("F8", "F9", "FA", "FC"):
            return self.payload[:2]

        if self._has_ctl:
            return self.payload[:2]

        if self.code in ("31D9", "31DA"):
            return self.payload[:2]

        # Explicit legacy guard (0xAB block) - non-controllers cannot send non-zero indices here
        if self.payload[:2] != "00":
            raise exc.PacketPayloadInvalid(
                f"Packet idx is {self.payload[:2]}, but expecting no idx (00) (0xAB)"
            )

        if self.code_enum in CODE_IDX_ARE_SIMPLE:
            return None

        return None

    @property
    def _ctx(self) -> bool | str:
        """Return the payload's full context, if any.

        :return: Context state metadata mapping.
        """
        if self._ctx_ is not None:
            return self._ctx_

        if self.code in ("0005", "000C"):
            self._ctx_ = self.payload[:4]
        elif self.code == "0404":
            idx_str = str(self._idx) if isinstance(self._idx, str) else ""
            self._ctx_ = idx_str + self.payload[10:12]
        else:
            self._ctx_ = self._idx
        return self._ctx_


def _build_idx_dict(msg: _LegacyMessage) -> dict[str, str]:
    """Build the dictionary for index merging, matching message.py logic exactly.

    :param msg: The _LegacyMessage instance being processed.
    :return: Generated payload structural dictionary mappings.
    """
    if not isinstance(msg._idx, str):
        return {}

    if msg.code_enum in CODE_IDX_ARE_COMPLEX:
        return {}

    if msg.code in ("31D9", "31DA"):
        return {"hvac_id": str(msg._idx)}

    if msg.code == "3220":
        return {}

    # Legacy (AA) logic: Filters strictly for allowed indexed device types
    if not {msg.src.type, msg.dst.type} & {
        "01",
        "02",
        "03",
        "04",
        "12",
        "18",
        "23",
    }:
        return {}

    # Legacy (AB) logic: Additional constraint if sent to itself
    if msg.src.type == msg.dst.type and msg.src.type not in (
        "01",
        "02",
        "03",
        "18",
        "23",
    ):
        return {}

    # Legacy (BC) logic: Bypasses strict internal constraints if it isn't a recognized controller
    if msg.src.type == msg.dst.type and not msg._has_ctl:
        return {}

    if msg.code in ("000A", "2309") and msg.src.type == "02":
        return {"ufh_idx": str(msg._idx)}

    idx_val = str(msg._idx)
    idx_name = "domain_id" if idx_val.startswith("F") else "zone_idx"

    idx_names = {
        "0002": "other_idx",
        "10A0": "dhw_idx",
        "1260": "dhw_idx",
        "1F41": "dhw_idx",
        "22C9": "ufh_idx",
        "2389": "other_idx",
        "2D49": "other_idx",
    }

    index_name = idx_names.get(msg.code, idx_name)
    return {index_name: idx_val}


def parser_unknown(payload: str, msg: _LegacyMessage) -> dict[str, Any]:
    """Apply a generic parser for unrecognized packet codes.

    :param payload: The raw hex string of the packet payload.
    :param msg: The legacy message abstraction layer object.
    :return: Standard structural error dictionaries or generic value payloads.
    """
    if msg.len == 2 and payload[:2] == "00":
        return {
            "_payload": payload,
            "_value": {"00": False, "C8": True}.get(
                payload[2:], int(payload[2:] or "0", 16)
            ),
        }

    if msg.len == 3 and payload[:2] == "00":
        # HACK: Using ramses_tx helper locally, replace with explicit logic if desired.
        from ...ramses_tx.helpers import hex_to_temp

        return {
            "_payload": payload,
            "_value": hex_to_temp(payload[2:]),
        }

    return {
        "_payload": payload,
        "_unknown_code": msg.code,
        "_parse_error": "No parser available for this packet type",
    }


def parser_heartbeat(payload: str, msg: _LegacyMessage) -> dict[str, Any]:
    """Parse a 1-byte heartbeat packet (payload '00').

    :param payload: The raw packet payload value string.
    :param msg: The legacy structural envelope message object.
    :return: Decoded validation flags.
    """
    return {"heartbeat": True}


class PayloadDecoder(ABC):
    """Abstract base class for the payload decoder chain."""

    def __init__(self) -> None:
        """Initialize the base decoder state."""
        self._next_decoder: PayloadDecoder | None = None

    def set_next(self, decoder: "PayloadDecoder") -> "PayloadDecoder":
        """Set the next decoder in the chain.

        :param decoder: The next step implementation payload decoder instance.
        :return: Fluid returned instance object.
        """
        self._next_decoder = decoder
        return decoder

    @abstractmethod
    def decode(
        self, dto: PacketDTO, payload_str: str, payload_len: int, msg: _LegacyMessage
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Decode the payload.

        :param dto: The packet raw serialization structure object.
        :param payload_str: Raw configuration hex payload representation.
        :param payload_len: Clean length tracking metric.
        :param msg: Internal compatibility envelope schema instance.
        :return: Extracted parser structural outputs.
        """
        if self._next_decoder:
            return self._next_decoder.decode(dto, payload_str, payload_len, msg)
        return {}


class RegexValidatorDecoder(PayloadDecoder):
    """Decoder that evaluates empty payloads and validates constraints."""

    def decode(
        self, dto: PacketDTO, payload_str: str, payload_len: int, msg: _LegacyMessage
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Validate expressions against schema and enforce strict execution conditions."""
        # Universal packet logging — persistent file, deduped, size-guarded
        if msg._has_payload and dto.verb.strip() in ("I", "W", "RP"):
            _log_packet_to_file(
                dto.code, dto.verb.strip(), msg.src.id, msg.dst.id, payload_str
            )

        try:
            _ = repr(dto)
        except Exception as err:
            raise exc.PacketPayloadInvalid(f"Packet formatting failed: {err}") from err

        code = _get_code(dto.code)

        if code is not None and code in CODES_SCHEMA:
            if dto.verb in ("RQ", "RP", " I", " W"):
                regex = CODES_SCHEMA[code].get(dto.verb)
                if regex:
                    match = (
                        bool(regex.match(payload_str))
                        if hasattr(regex, "match")
                        else bool(re.match(str(regex), payload_str))
                    )

                    if not match:
                        if not msg._has_payload:
                            return None
                        if dto.verb.strip() != "RQ":
                            # Log UFC (type 02) packets that fail regex validation
                            # before discarding — these are HCC100 multi-circuit
                            # messages using non-standard zone indices (e.g. E1-E6,
                            # 21xx-22xx) that are not yet covered by the schema regex.
                            if msg.src.type == "02" and dto.code in ("3150", "30C9"):
                                _LOGGER.warning(
                                    "RAMSES %s %s from %s to %s: payload=%s [%s]",
                                    dto.code,
                                    dto.verb,
                                    msg.src.id,
                                    msg.dst.id,
                                    payload_str,
                                    datetime.now().isoformat(timespec='seconds'),
                                )
                            msg_str = f"Payload doesn't match {dto.verb}/{dto.code}: {payload_str} != {regex}"
                            raise exc.PacketPayloadInvalid(msg_str)

        if not msg._has_payload and (
            dto.verb.strip() == "RQ" and code not in RQ_IDX_COMPLEX
        ):
            return None

        if self._next_decoder:
            return self._next_decoder.decode(dto, payload_str, payload_len, msg)
        return {}


class HeartbeatDecoder(PayloadDecoder):
    """Decoder that intercepts 1-byte '00' heartbeats."""

    def decode(
        self, dto: PacketDTO, payload_str: str, payload_len: int, msg: _LegacyMessage
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Evaluate and intercept simple heartbeat packages."""
        if payload_len == 1 and payload_str == "00" and dto.code != "1FC9":
            try:
                parser = get_parser(dto.code) or parser_unknown
                res = parser(payload_str, msg)
                if res == {}:
                    return None
            except Exception:
                pass
            return parser_heartbeat(payload_str, msg)

        if self._next_decoder:
            return self._next_decoder.decode(dto, payload_str, payload_len, msg)
        return {}


class StandardParserDecoder(PayloadDecoder):
    """Decoder routing payload to the appropriate 4-digit code parser."""

    def decode(
        self, dto: PacketDTO, payload_str: str, payload_len: int, msg: _LegacyMessage
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Parse structured metrics passing execution objects forward to specific system decoders."""
        try:
            parser = get_parser(dto.code) or parser_unknown
            result = parser(payload_str, msg)

            if isinstance(result, dict) and dto.seq and dto.seq.isnumeric():
                result["seqx_num"] = dto.seq

            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result
            return {}
        except AssertionError as err:
            err_result = {
                "_payload": payload_str,
                "_parse_error": f"AssertionError: {err}",
                "_unknown_code": dto.code,
            }
            if dto.seq and dto.seq.isnumeric():
                err_result["seqx_num"] = dto.seq
            return err_result


class DtoPayloadDecoderPipeline:
    """The Chain of Responsibility pipeline for decoding DTO payloads."""

    def __init__(self) -> None:
        """Initialize pipeline linking specific system validation decoders."""
        self.head = RegexValidatorDecoder()
        self.head.set_next(HeartbeatDecoder()).set_next(StandardParserDecoder())

    def decode(self, dto: PacketDTO) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Route tracking models cleanly through downstream chain decoders.

        :param dto: The network transfer packet state container model.
        :return: Fully structured mapping outputs or null evaluations.
        """
        payload_str: str = dto.payload
        try:
            payload_len: int = int(dto.length)
        except ValueError:
            payload_len = 0

        msg = _LegacyMessage(dto)

        # 1. Parsing Phase (Catches and suppresses exceptions for null payloads)
        try:
            result = self.head.decode(dto, payload_str, payload_len, msg)
        except exc.PacketPayloadInvalid as err:
            if not msg._has_payload:
                result = {}
            else:
                raise err
        except exc.PacketInvalid as err:
            raise err
        except AssertionError as err:
            raise exc.PacketInvalid(f"Bad packet: {err}") from err
        except (AttributeError, LookupError, TypeError, ValueError) as err:
            raise exc.PacketInvalid(f"Coding error: {err}") from err
        except NotImplementedError as err:
            raise exc.PacketInvalid("Unknown packet code") from err

        # 2. Evaluation Phase
        if result is None:
            return {}
        if isinstance(result, list):
            return result

        # 3. Index Injection Phase (Errors raised here will bypass the null-payload swallow)
        try:
            idx_dict = _build_idx_dict(msg)
            return {**idx_dict, **result}
        except exc.PacketInvalid as err:
            raise err


def decode_packet(dto: PacketDTO) -> dict[str, Any] | list[dict[str, Any]]:
    """Entry point for the new DTO-based payload decoder.

    :param dto: The network protocol target transfer object packet frame.
    :return: Processed semantic runtime dictionary details.
    """
    pipeline = DtoPayloadDecoderPipeline()
    result = pipeline.decode(dto)

    if result is None:
        return {}

    return result

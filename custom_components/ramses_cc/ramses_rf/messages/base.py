#!/usr/bin/env python3
"""RAMSES RF - Decode/process a message (payload into JSON/DTO)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, TypeAlias, TypeVar

from ..address import Address
from ...ramses_tx.command import Command
from ...ramses_tx.dtos import PacketDTO
from ...ramses_tx.models import DeviceId, RawPacket, TransportMessage
from ...ramses_tx.typing import DeviceIdT

from .. import exceptions as exc
from ..const import DEV_TYPE_MAP, SZ_DHW_IDX, SZ_DOMAIN_ID, SZ_UFH_IDX, SZ_ZONE_IDX
from ..parsers.decoder import decode_packet
from ..protocol.ramses import CODE_IDX_ARE_COMPLEX
from ..routing import RoutingContext, StateHeader

from ..const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from ..const import IndexT, VerbT  # noqa: F401


__all__ = ["Message"]


MSG_FORMAT_10: str = "|| {:10s} | {:10s} | {:2s} | {:16s} | {:^4s} || {}"


_LOGGER = logging.getLogger(__name__)


@dataclass
class PayloadBase:
    """Base Data Transfer Object for parsed payloads.

    Acts as the foundation for the strict DTO migration, replacing raw
    dicts.
    """

    pass


# Transition alias for typing until full payload migration is complete
PayloadT: TypeAlias = Any


# TypeVar bound to Message to allow strict inheritance typing
_MessageT = TypeVar("_MessageT", bound="Message")


class _LegacyPktShim:
    """A temporary shim bridging PacketDTO to legacy L3 attributes."""

    def __init__(self, msg: Message) -> None:
        """Initialize the shim with the parent Message.

        :param msg: The parent Message instance.
        :type msg: Message
        """
        self._msg = msg
        self._dto = msg._dto

    @property
    def _ctx(self) -> Any:
        """Legacy context bridge."""
        return self._msg.context.value

    @property
    def _hdr(self) -> str:
        """Legacy header bridge."""
        return self._msg.state_header.legacy_hdr

    @property
    def _frame(self) -> str:
        """Legacy frame bridge for backwards compatibility in tests.

        Calculates the frame string dynamically from the L7 properties.
        """
        seqn = self._msg.seqn if self._msg.seqn else "---"
        addr1 = self._msg._addrs[0].id
        addr2 = self._msg._addrs[1].id
        addr3 = self._msg._addrs[2].id
        return (
            f"{self._msg.verb} {seqn} {addr1} {addr2} {addr3} "
            f"{self._msg.code} {self._msg.len:03d} {self._dto.payload}"
        )

    def __getattr__(self, name: str) -> Any:
        """Delegate all other attributes to the underlying DTO.

        :param name: The attribute name.
        :type name: str
        :return: The attribute value.
        :rtype: Any
        """
        return getattr(self._dto, name)


class Message:
    """The Message class; will trap/log invalid msgs."""

    # Domain Bridges (Injected by Gateway)
    _IS_CONTROLLER_CB: Callable[[str], bool] | None = None
    _GET_CODE_NAME_CB: Callable[[Code | str], str] | None = None
    _GET_MSG_IDX_CB: Callable[[Any], dict[str, str]] | None = None

    _gwy: Any | None = None

    def __init__(self, dto: PacketDTO) -> None:
        """Create a message from a valid packet.

        :param dto: The packet data transfer object to process.
        :type dto: PacketDTO
        :raises PacketInvalid: If the packet payload cannot be parsed.
        """
        self._dto: PacketDTO = dto

        self.dtm: dt = dto.timestamp
        self.rssi: str = dto.rssi

        # Cleanly cast properties
        self.verb: VerbT = dto.verb  # type: ignore[assignment]
        self.seqn: str = dto.seq

        try:
            self.code: Code = Code(dto.code)
        except ValueError:
            self.code = dto.code  # type: ignore[assignment]

        try:
            self.len: int = int(dto.length)
        except ValueError:
            self.len = 0

        # Safely resolve addresses via L2 Positional MACs
        addr1 = dto.addr1 if dto.addr1 else "--:------"
        addr2 = dto.addr2 if dto.addr2 else "--:------"
        addr3 = dto.addr3 if dto.addr3 else "--:------"

        self._addrs: tuple[Address, Address, Address] = (
            Address(DeviceIdT(addr1)),
            Address(DeviceIdT(addr2)),
            Address(DeviceIdT(addr3)),
        )

        valid = [a for a in self._addrs if a.id != "--:------"]
        self.src: Address = valid[0] if valid else Address(DeviceIdT("--:------"))
        self.dst: Address = valid[1] if len(valid) > 1 else self.src

        # Initialize attributes before parsing to prevent AttributeError
        # if an exception is raised and __repr__ is called.
        self._str: str | None = None
        self._payload: PayloadT = {}

        self._has_array_: bool = False
        self._idx_val: str | bool = dto.payload[:2] if dto.payload else False

        self._payload = self._validate(dto.payload)

    @property
    def context(self) -> RoutingContext:
        """Calculate the sub-payload context natively.

        :return: The context value.
        :rtype: RoutingContext
        """
        code = str(getattr(self, "code", ""))
        payload = getattr(self._dto, "payload", "")
        if code == "3220" and len(payload) >= 6:
            return RoutingContext(payload[4:6])
        return RoutingContext(getattr(self, "_idx_val", None))

    @property
    def state_header(self) -> StateHeader:
        """Calculate the state routing header natively.

        :return: The state header instance.
        :rtype: StateHeader
        """
        return StateHeader.create(
            code=self.code,
            verb=self.verb,
            source_id=self.src.id,
            context_val=self.context.value,
        )

    @property
    def _pkt(self) -> Any:
        """Legacy shim for downstream parsers and tests.

        Returns the DTO so legacy tests accessing msg._pkt.payload
        continue to function during the boundary migration.
        """
        return _LegacyPktShim(self)

    @classmethod
    def _from_pkt(cls: type[_MessageT], pkt: Any) -> _MessageT:
        """Create a Message (or subclass) from a legacy Packet.

        :param pkt: The legacy packet object.
        :type pkt: Any
        :return: The generated message.
        :rtype: Message
        """
        return cls(pkt.to_dto())

    @classmethod
    def _from_cmd(
        cls: type[_MessageT], cmd: Command, dtm: dt | None = None
    ) -> _MessageT:
        """Create a Message (or subclass) from a Command.

        :param cmd: The command.
        :type cmd: Command
        :param dtm: Datetime overrides.
        :type dtm: dt | None
        :return: The generated message.
        :rtype: Message
        """
        # Temporary shim bridging backwards logic during Phase 2
        from ...ramses_tx.packet import Packet

        pkt = Packet._from_cmd(cmd, dtm=dtm)
        return cls(pkt.to_dto())

    def __str__(self) -> str:
        """Return a human-readable string representation of this object.

        :return: A human-readable string representation of this object.
        :rtype: str
        """

        def ctx(dto: PacketDTO) -> str:
            """Extract the context string from the packet safely."""
            val: str = ""
            if self._idx_val is True:
                val = "[..]"
            elif self._idx_val is False:
                val = ""
            elif self._idx_val is None:
                val = "??"  # type: ignore[unreachable]
            else:
                val = str(self._idx_val)

            if (
                not val
                and isinstance(dto.payload, str)
                and dto.payload[:2] not in ("00", "FF")
            ):
                return f"({dto.payload[:2]})"
            return val

        if self._str is not None:
            return self._str

        if self.src.id == self._addrs[0].id:
            name_0 = self._name(self.src)
            # use 'is', issue_cc 318
            name_1 = "" if self.dst is self.src else self._name(self.dst)
        else:
            name_0 = ""
            name_1 = self._name(self.src)

        if Message._GET_CODE_NAME_CB is not None:
            code_name = Message._GET_CODE_NAME_CB(self.code)
        else:
            code_name = f"unknown_{self.code}"

        self._str = MSG_FORMAT_10.format(
            name_0, name_1, self.verb, code_name, ctx(self._dto), self.payload
        )
        return self._str

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object.

        :return: An unambiguous string representation of this object.
        :rtype: str
        """
        raw_payload = self._dto.payload
        addr1 = self._addrs[0].id
        addr2 = self._addrs[1].id
        addr3 = self._addrs[2].id
        seqn = self.seqn if self.seqn else "---"
        return (
            f"{self.verb} {seqn} {addr1} {addr2} {addr3} "
            f"{self.code} {self.len:03d} {raw_payload}"
        )

    def __eq__(self, other: object) -> bool:
        """Check equality against another Message."""
        if not isinstance(other, Message):
            return NotImplemented
        return (
            self.src,
            self.dst,
            self.verb,
            self.code,
            self._dto.payload,
        ) == (
            other.src,
            other.dst,
            other.verb,
            other.code,
            other._dto.payload,
        )

    def __lt__(self, other: object) -> bool:
        """Compare timestamps for ordering."""
        if not isinstance(other, Message):
            return NotImplemented
        return self.dtm < other.dtm

    def _name(self, addr: Address) -> str:
        """Return a friendly name for an Address, or a Device.

        :param addr: The address to identify.
        :type addr: Address
        :return: A friendly name for an Address, or a Device.
        :rtype: str
        """
        # can't do 'CTL:123456' instead of ' 01:123456'
        return f" {addr.id}"

    @property
    def payload(self) -> PayloadT:
        """Return the parsed payload, preferably as a strongly-typed DTO.

        :return: The payload.
        :rtype: PayloadT
        """
        return self._payload

    @property
    def _has_payload(self) -> bool:
        """Return False if there is no payload (may falsely return True).

        The message (i.e. the raw payload) may still have an idx.

        :return: False if there is no payload (may falsely return True).
        :rtype: bool
        """
        if self.len == 1:
            return False
        if str(self.verb).strip() == "RQ":
            if self.len == 2 and self.code != "0016":
                return False
        return True

    @property
    def _has_array(self) -> bool:
        """Return True if the message's raw payload is an array.

        :return: True if the message's raw payload is an array.
        :rtype: bool
        """
        return self._has_array_

    def _force_has_array(self) -> None:
        """Force the payload to be interpreted as an array fragment."""
        self._has_array_ = True

    @property
    def _idx(self) -> dict[str, str]:
        """Get the domain_id/zone_idx/other_idx of a message payload,
        if any.
        Used to identify the zone/domain that a message applies to.

        :return: an empty dict if there is none such, or None if
            undetermined.
        :rtype: dict[str, str]
        """
        if Message._GET_MSG_IDX_CB is not None:
            return Message._GET_MSG_IDX_CB(self)

        IDX_NAMES = {
            Code._0002: "other_idx",
            Code._10A0: SZ_DHW_IDX,
            Code._1260: SZ_DHW_IDX,
            Code._1F41: SZ_DHW_IDX,
            Code._22C9: SZ_UFH_IDX,
            Code._2389: "other_idx",
            Code._2D49: "other_idx",
            Code._31D9: "hvac_id",
            Code._31DA: "hvac_id",
            Code._3220: "msg_id",
        }  # ALSO: SZ_DOMAIN_ID, SZ_ZONE_IDX

        if self.code in (Code._31D9, Code._31DA):
            assert isinstance(self._idx_val, str)  # mypy hint
            return {"hvac_id": self._idx_val}

        if self._idx_val in (True, False) or self.code in CODE_IDX_ARE_COMPLEX:
            return {}

        if self.code in (Code._3220,):  # FIXME: should be _SIMPLE
            return {}

        if not {self.src.type, self.dst.type} & {
            DEV_TYPE_MAP.CTL,
            DEV_TYPE_MAP.UFC,
            DEV_TYPE_MAP.HCW,
            DEV_TYPE_MAP.DTS,
            DEV_TYPE_MAP.HGI,
            DEV_TYPE_MAP.DT2,
            DEV_TYPE_MAP.PRG,
            # FIXME: DEX should be deprecated to use device type rather than class
        }:
            assert self._idx_val == "00", "What!! (AA)"
            return {}

        if self.src.type == self.dst.type and self.src.type not in (
            DEV_TYPE_MAP.CTL,
            DEV_TYPE_MAP.UFC,
            DEV_TYPE_MAP.HCW,
            DEV_TYPE_MAP.HGI,
            DEV_TYPE_MAP.PRG,
        ):
            assert self._idx_val == "00", "What!! (AB)"
            return {}

        # BRIDGED LOGIC:
        is_controller = True
        if Message._IS_CONTROLLER_CB is not None:
            # Use the injected domain logic from ramses_rf
            is_controller = Message._IS_CONTROLLER_CB(self.src.id)
        else:
            # Fallback for legacy tests until they are updated
            is_controller = getattr(self.src, "_is_controller", True)

        if self.src.type == self.dst.type and not is_controller:
            assert self._idx_val == "00", "What!! (BC)"
            return {}

        if self.code in (Code._000A, Code._2309) and (
            self.src.type == DEV_TYPE_MAP.UFC
        ):
            assert isinstance(self._idx_val, str)  # mypy hint
            return {IDX_NAMES[Code._22C9]: self._idx_val}

        assert isinstance(self._idx_val, str)  # mypy hint
        idx_name = SZ_DOMAIN_ID if self._idx_val[:1] == "F" else SZ_ZONE_IDX
        index_name = IDX_NAMES.get(self.code, idx_name)

        return {index_name: self._idx_val}

    @property
    def dto(self) -> TransportMessage:
        """Generate a strictly-typed TransportMessage DTO from this
        legacy Message.

        This acts as a safe, passive bridge to validate the new Data
        Transfer Objects against the legacy snapshot tests before fully
        migrating the transport layer.
        """
        raw_hex_payload = self._dto.payload
        payload_length = self.len

        addr1_str = self._addrs[0].id
        addr2_str = self._addrs[1].id
        addr3_str = self._addrs[2].id

        code_str = self._dto.code
        try:
            code_int = int(code_str, 16)
        except ValueError:
            code_int = 0

        raw_pkt = RawPacket(
            raw_packet=repr(self),
            rssi=str(self.rssi),
            verb=self.verb,
            seq=str(self.seqn),
            device_id_1=addr1_str,
            device_id_2=addr2_str,
            device_id_3=addr3_str,
            code=code_str,
            payload_len=f"{payload_length:03d}",
            payload=raw_hex_payload,
        )

        return TransportMessage(
            dtm=self.dtm,
            source_packets=(raw_pkt,),
            rssi=int(self.rssi) if str(self.rssi).lstrip("-").isdigit() else 0,
            verb=self.verb,
            device_id_1=DeviceId.from_string(addr1_str),
            device_id_2=DeviceId.from_string(addr2_str),
            device_id_3=DeviceId.from_string(addr3_str),
            code=code_int,
            payload_len=int(payload_length),
            raw_payload=raw_hex_payload,
        )

    def _validate(self, raw_payload: str) -> PayloadT:
        """Validate a message packet payload, and parse it if valid.

        :param raw_payload: The raw payload string.
        :type raw_payload: str
        :return: A dict containing key: value pairs, or a list/DTO
            created from the payload.
        :rtype: PayloadT
        :raises PacketInvalid: If it is not valid or parsable.
        """
        # TODO: only accept invalid packets to/from HGI when flag raised
        try:
            try:
                # Semantic parsing is explicitly mapped to DTO processing
                result = decode_packet(self._dto)
            except exc.PacketPayloadInvalid as err:
                if not self._has_payload:
                    return {}  # Heartbeat fallback for null payloads
                raise err

            if isinstance(result, list):
                self._has_array_ = True
                return result

            # The DTO pipeline natively handles index extraction.
            # Return the strongly-typed PayloadBase DTO object
            return result

        except exc.PacketInvalid as err:
            _LOGGER.warning("%s < %s", repr(self), err)
            raise err

        except AssertionError as err:
            _LOGGER.exception(
                "%s < %s",
                repr(self),
                f"{err.__class__.__name__}({err})",
            )
            raise exc.PacketInvalid("Bad packet") from err

    @property
    def addr3(self) -> Address:
        """Return the third address field (the logical destination or owner).

        :return: The third address object.
        :rtype: Address
        """
        return self._addrs[2]

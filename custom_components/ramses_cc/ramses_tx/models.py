"""RAMSES RF - Transport Layer Domain Models (DTOs)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime as dt

from .const import VerbT


@dataclass(frozen=True)
class DeviceId:
    """Strict representation of a Ramses RF device ID.

    Stores the device type and ID as memory-efficient integers.
    """

    device_type: int
    device_id: int

    def __str__(self) -> str:
        """Return the strictly formatted string representation."""
        # Handle the standard Null/Broadcast address (63:262142 / FFFFFE)
        if self.device_type == 63 and self.device_id == 262142:
            return "--:------"
        return f"{self.device_type:02d}:{self.device_id:06d}"

    @classmethod
    def from_string(cls, address_str: str) -> DeviceId:
        """Safely parse a raw string into a DeviceId object."""
        if not address_str or ":" not in address_str or address_str == "--:------":
            return cls(63, 262142)

        dev_type_str, dev_id_str = address_str.split(":", 1)
        return cls(int(dev_type_str), int(dev_id_str))


@dataclass(frozen=True)
class RawPacket:
    """Represents the raw RF transmission (Lexical Layer).

    Holds every piece of data from the raw radio string exactly as it
    arrived, without interpreting its domain meaning.

    Note: Named 'RawPacket' temporarily to avoid clashing with the
    legacy 'Packet' class during the incremental refactoring phase.
    """

    raw_packet: str
    rssi: str
    verb: str
    seq: str
    device_id_1: str
    device_id_2: str
    device_id_3: str
    code: str
    payload_len: str
    payload: str


@dataclass(frozen=True)
class TransportMessage:
    """The semantically parsed structural object.

    Promotes the raw string components into rich Python types but
    applies no domain logic (e.g., does not know which ID is 'src').
    """

    dtm: dt
    source_packets: tuple[RawPacket, ...]

    rssi: int
    verb: VerbT

    device_id_1: DeviceId
    device_id_2: DeviceId
    device_id_3: DeviceId

    code: int
    payload_len: int
    raw_payload: str  # The pure hexadecimal string

    @property
    def is_multipart(self) -> bool:
        """Indicates if this message was stitched together.

        :returns: True if constructed from multiple packets.
        :rtype: bool
        """
        return len(self.source_packets) > 1

    @property
    def code_hex(self) -> str:
        """Returns the code in its standard uppercase hex format.

        :returns: The hex string formatted code (e.g., '30C9').
        :rtype: str
        """
        return f"{self.code:04X}"

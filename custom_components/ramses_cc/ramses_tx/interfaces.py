#!/usr/bin/env python3
"""RAMSES RF - Interfaces for the RAMSES-II protocol stack."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .command import Command
    from .const import Priority
    from .packet import Packet
    from .typing import QosParams


class TransportInterface(ABC):
    """Interface for the Packet Transport layer."""

    @abstractmethod
    def close(self) -> None:
        """Close the transport."""

    @abstractmethod
    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra information about the transport."""

    @abstractmethod
    async def send_frame(self, frame: str) -> None:
        """Send a frame."""

    @abstractmethod
    async def write_frame(self, frame: str) -> None:
        """Write a frame (legacy alias for send_frame)."""


class ProtocolInterface(ABC):
    """Interface for the RAMSES-II Protocol layer."""

    @abstractmethod
    def connection_made(self, transport: TransportInterface) -> None:
        """Called when a connection is made."""

    @abstractmethod
    def connection_lost(self, err: Exception | None) -> None:
        """Called when the connection is lost."""

    @abstractmethod
    def pause_writing(self) -> None:
        """Pause writing."""

    @abstractmethod
    def pkt_received(self, pkt: "Packet") -> None:
        """Receive a packet."""

    @abstractmethod
    def resume_writing(self) -> None:
        """Resume writing."""

    @abstractmethod
    async def send_cmd(
        self,
        cmd: "Command",
        /,
        *,
        qos: "QosParams | None" = None,
    ) -> "Packet | None":
        """Send a command."""


class StateMachineInterface(ABC):
    """Interface for the Protocol State Machine."""

    @abstractmethod
    def connection_made(self, transport: TransportInterface) -> None:
        """Called when a connection is made."""

    @abstractmethod
    def connection_lost(self, err: Exception | None) -> None:
        """Called when the connection is lost."""

    @abstractmethod
    def pkt_received(self, pkt: "Packet") -> None:
        """Called when a packet is received."""

    @abstractmethod
    async def send_cmd(
        self,
        send_fnc: Any,
        cmd: "Command",
        priority: "Priority",
        qos: "QosParams",
    ) -> "Packet":
        """Send a command."""

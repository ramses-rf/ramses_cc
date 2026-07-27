#!/usr/bin/env python3
"""RAMSES RF - exceptions within the packet/protocol/transport layer.

This module defines the centralized exception hierarchy for handling errors
across the parser, protocol, and transport layers.
"""

from __future__ import annotations


class _RamsesBaseException(Exception):
    """Base class for all ramses_tx exceptions."""

    pass


class RamsesException(_RamsesBaseException):
    """Base class for all ramses_tx exceptions providing hint support.

    :param args: The arguments passed to the exception, typically the message.
    """

    HINT: str | None = None

    def __init__(self, *args: object) -> None:
        """Initialize the exception with an optional message."""
        super().__init__(*args)
        self.message: str | None = str(args[0]) if args else None

    def __str__(self) -> str:
        """Return the string representation of the exception including the hint."""
        if self.message and self.HINT:
            return f"{self.message} (hint: {self.HINT})"
        if self.message:
            return self.message
        if self.HINT:
            return f"Hint: {self.HINT}"
        return ""


class _RamsesLowerError(RamsesException):
    """A failure in the lower layer (parser, protocol, transport, serial)."""


########################################################################################
# Transport Layer Errors
########################################################################################


class TransportError(_RamsesLowerError):
    """An error when sending or receiving frames (bytes) via the transport."""


class TransportStateError(TransportError):
    """The transport is in an invalid state for the requested operation."""


class TransportSerialError(TransportError):
    """The transport's serial port has thrown an error."""


class TransportSourceInvalid(TransportError):
    """The source of packets (frames) is not a valid type or configuration."""


class TransportMqttError(TransportError):
    """A failure occurred specifically within the MQTT transport layer."""


class TransportZigbeeError(TransportError):
    """A failure occurred specifically within the Zigbee ZHA transport layer."""


########################################################################################
# Protocol & FSM Layer Errors
########################################################################################


class ProtocolError(_RamsesLowerError):
    """An error occurred when sending, receiving, or exchanging packets."""


class ProtocolFsmError(ProtocolError):
    """The protocol FSM was or became inconsistent (logical state error)."""


class ProtocolSendFailed(ProtocolError):
    """The Command failed to elicit an echo or the expected response."""


class ProtocolTimeoutError(ProtocolSendFailed):
    """A specific operational timeout occurred while waiting for a packet."""


########################################################################################
# Parser Layer Errors
########################################################################################


class ParserBaseError(_RamsesLowerError):
    """The packet is corrupt, not internally consistent, or cannot be parsed."""


class PacketInvalid(ParserBaseError):
    """The packet is corrupt or not internally consistent."""


class PacketAddrSetInvalid(PacketInvalid):
    """The packet's address set is inconsistent."""


class PacketPayloadInvalid(PacketInvalid):
    """The packet's payload is inconsistent."""


class MessageInvalid(ParserBaseError):
    """The message is structurally sound as a packet, but semantically invalid."""


class ParserError(ParserBaseError):
    """The packet cannot be parsed without error."""


class CommandInvalid(ParserError):
    """The command is corrupt or invalid."""

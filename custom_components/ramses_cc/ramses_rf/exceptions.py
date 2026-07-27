#!/usr/bin/env python3
"""RAMSES RF - exceptions above the packet/protocol/transport layer."""

from __future__ import annotations

from ..ramses_tx.exceptions import (
    PacketAddrSetInvalid as PacketAddrSetInvalid,
    PacketInvalid as PacketInvalid,
    PacketPayloadInvalid as PacketPayloadInvalid,
    ProtocolError as ProtocolError,
    RamsesException as RamsesException,
)


class _RamsesUpperError(RamsesException):
    """A failure in the upper layer (state/schema, gateway, bindings, schedule)."""


########################################################################################
# Errors above the protocol/transport layer, incl. message processing, state & schema


class BindingError(_RamsesUpperError):
    """An error occurred when binding."""


class BindingFsmError(BindingError):
    """The binding FSM was/became inconsistent (this shouldn't happen)."""


class BindingFlowFailed(BindingError):
    """The binding failed due to a timeout or retry limit being exceeded."""


class BindingTimeoutError(BindingError):
    """Raised when a binding operation exceeds the allotted time."""


########################################################################################
# Errors above the protocol/transport layer, incl. message processing, state & schema


class ScheduleError(_RamsesUpperError):
    """An error occurred when getting/setting a schedule."""


class ScheduleFsmError(ScheduleError):
    """The schedule FSM was/became inconsistent (this shouldn't happen)."""


class ScheduleFlowError(ScheduleError):
    """The get/set schedule failed due to a timeout or retry limit being exceeded."""


########################################################################################
# Errors above the protocol/transport layer, incl. message processing, state & schema


class DatabaseQueryError(_RamsesUpperError):
    """Raised when a query to the message database fails or is invalid."""


class DeviceNotFoundError(_RamsesUpperError):
    """Raised when a specific device cannot be found in the gateway or system."""


class ExpiredCallbackError(_RamsesUpperError):
    """Raised when the callback has expired."""


class SystemInconsistent(_RamsesUpperError):
    """Base class for exceptions in this module."""


class SystemSchemaInconsistent(SystemInconsistent):
    """Raised when the system state (usu. schema) is inconsistent."""

    HINT = "try restarting the client library"


class SchemaInconsistentError(SystemSchemaInconsistent):
    """Raised when the loaded schema contradicts the known state or rules."""

    HINT = "check the configuration schema for conflicting definitions"


class DeviceNotFaked(SystemInconsistent):
    """Raised when the device does not have faking enabled."""

    HINT = "faking is configured in the known_list"


class ForeignGatewayError(SystemInconsistent):
    """Raised when a foreign gateway is detected.

    These devices may not be gateways (set a class), or belong to a neighbour (exclude
    via block_list/known_list), or should be allowed (known_list)."""

    HINT = "consider enforcing a known_list"


class DeviceNotRecognised(_RamsesUpperError):
    """Raised when a device is not recognized.

    This typically happens when trying to interact with a device that doesn't exist
    or is not properly configured in the system."""

    HINT = "check the device ID and ensure the device is properly configured"


class CommandInvalid(_RamsesUpperError):
    """Raised when an invalid command is sent to a device.

    This can happen if the command format is incorrect or if the command is not
    supported by the target device."""

    HINT = "verify the command format and ensure it's supported by the device"


class SendFailure(_RamsesUpperError):
    """Raised when a command fails to be sent to a device.

    This typically indicates a communication issue with the device or gateway."""

    HINT = "check the device connection and try again"


class SendPriority(_RamsesUpperError):
    """Raised when the command queue is full.

    This happens when too many commands are queued for sending and the queue
    has reached its maximum capacity."""

    HINT = "wait for pending commands to complete and try again"

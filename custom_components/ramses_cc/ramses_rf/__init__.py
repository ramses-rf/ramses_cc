#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

`ramses_rf` takes care of the device (upper) layer.

Works with (amongst others):
- evohome (up to 12 zones)
- sundial (up to 2 zones)
- chronotherm (CM60xNG can do 4 zones)
- hometronics (16? zones)
- vision pro
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import exceptions
from .const import I_, RP, RQ, W_, Code
from .protocol.ramses import (
    _2411_PARAMS_SCHEMA,
    CODES_BY_DEV_SLUG,
    CODES_SCHEMA,
    SZ_DATA_TYPE,
    SZ_DATA_UNIT,
    SZ_DESCRIPTION,
    SZ_MAX_VALUE,
    SZ_MIN_VALUE,
    SZ_PRECISION,
)
from .version import VERSION

if TYPE_CHECKING:
    from ..ramses_tx import Address, Command, Packet

    from .config import GatewayConfig
    from .const import IndexT, VerbT
    from .devices import Device
    from .exceptions import CommandInvalid
    from .gateway import Gateway
    from .messages import Message


__all__ = [
    "VERSION",
    "Gateway",
    "GatewayConfig",
    #
    "Address",
    "Command",
    "CommandInvalid",
    "Device",
    "Message",
    "Packet",
    # Schema-related constants
    "SZ_DATA_UNIT",
    "SZ_DESCRIPTION",
    "SZ_DATA_TYPE",
    "SZ_MAX_VALUE",
    "SZ_MIN_VALUE",
    "SZ_PRECISION",
    "_2411_PARAMS_SCHEMA",
    "CODES_BY_DEV_SLUG",
    "CODES_SCHEMA",
    #
    "I_",
    "RP",
    "RQ",
    "W_",
    #
    "Code",
    "IndexT",
    "VerbT",
    #
    "exceptions",
    #
    "GracefulExit",
]

_LOGGER = logging.getLogger(__name__)


def __getattr__(name: str) -> Any:
    """Lazy-load heavy L7 domain objects and L3 primitives.

    :param name: The name of the attribute to retrieve.
    :type name: str
    :return: The requested domain object or primitive class.
    :rtype: Any
    :raises AttributeError: If the requested attribute is not exported.
    """
    if name in ("Address", "Command", "Packet"):
        from .. import ramses_tx

        return getattr(ramses_tx, name)

    if name == "Gateway":
        from .gateway import Gateway

        return Gateway

    if name == "GatewayConfig":
        from .config import GatewayConfig

        return GatewayConfig

    if name == "Device":
        from .devices import Device

        return Device

    if name == "Message":
        from .messages import Message

        return Message

    if name == "CommandInvalid":
        from .exceptions import CommandInvalid

        return CommandInvalid

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class GracefulExit(SystemExit):
    """Exit the program gracefully."""

    code = 1

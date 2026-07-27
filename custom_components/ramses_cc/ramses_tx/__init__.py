#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.
`ramses_tx` takes care of the RF protocol (lower) layer.
"""

from __future__ import annotations

import asyncio
from functools import partial
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, Any

from .address import ALL_DEV_ADDR, ALL_DEVICE_ID, NON_DEV_ADDR, NON_DEVICE_ID, Address
from .command import CODE_API_MAP, Command
from .const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    F9,
    FA,
    FC,
    FF,
    SZ_ACTIVE_HGI,
    SZ_DEVICE_ROLE,
    SZ_DOMAIN_ID,
    SZ_ZONE_CLASS,
    SZ_ZONE_IDX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    ZON_ROLE_MAP,
    DevRole,
    DevType,
    IndexT,
    Priority,
    VerbT,
    ZoneRole,
)
from .discovery import is_hgi80
from .engine import Engine
from .logger import set_pkt_logging
from .packet import PKT_LOGGER, Packet
from .protocol import PortProtocol, ReadProtocol, protocol_factory
from .schemas import SZ_SERIAL_PORT
from .transport import RamsesTransportT, ZigbeeTransport, transport_factory
from .typing import DeviceIdT, QosParams
from .version import VERSION

from .const import (  # isort: skip
    I_,
    RP,
    RQ,
    W_,
    Code,
)


__all__ = [
    "VERSION",
    "Engine",
    #
    "SZ_ACTIVE_HGI",
    "SZ_DEVICE_ROLE",
    "SZ_DOMAIN_ID",
    "SZ_SERIAL_PORT",
    "SZ_ZONE_CLASS",
    "SZ_ZONE_IDX",
    "SZ_ZONE_MASK",
    "SZ_ZONE_TYPE",
    #
    "ALL_DEV_ADDR",
    "ALL_DEVICE_ID",
    "NON_DEV_ADDR",
    "NON_DEVICE_ID",
    #
    "CODE_API_MAP",
    "DEV_ROLE_MAP",
    "DEV_TYPE_MAP",
    "ZON_ROLE_MAP",
    #
    "I_",
    "RP",
    "RQ",
    "W_",
    "F9",
    "FA",
    "FC",
    "FF",
    #
    "DeviceIdT",
    "DevRole",
    "DevType",
    "IndexT",
    "VerbT",
    "ZoneRole",
    #
    "Address",
    "Code",
    "Command",
    "Packet",
    "Priority",
    "QosParams",
    #
    "PortProtocol",
    "ReadProtocol",
    "RamsesProtocolT",
    "protocol_factory",
    #
    "RamsesTransportT",
    "ZigbeeTransport",
    "is_hgi80",
    "transport_factory",
    #
    "is_valid_dev_id",
    "set_pkt_logging_config",
]


is_valid_dev_id = Address.is_valid

if TYPE_CHECKING:
    from logging import Logger


async def set_pkt_logging_config(**config: Any) -> tuple[Logger, QueueListener | None]:
    """
    Set up ramses packet logging to a file or port.
    Must run async in executor to prevent HA blocking call opening packet log file.

    :param config: if file_name is included, opens packet_log file
    :return: a tuple (logging.Logger, QueueListener)
    """
    loop = asyncio.get_running_loop()
    listener = await loop.run_in_executor(
        None, partial(set_pkt_logging, PKT_LOGGER, **config)
    )
    return PKT_LOGGER, listener

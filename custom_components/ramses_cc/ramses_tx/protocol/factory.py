#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet protocol factory.

This module provides the factory methods to instantiate the protocol
layer and construct the complete protocol-transport stack.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime as dt
from typing import Any

from ..const import DEFAULT_DISABLE_QOS
from ..helpers import dt_now
from ..logger import set_logger_timesource
from ..transport import RamsesTransportT, TransportConfig, transport_factory
from ..typing import MsgHandlerT, PortConfigT, SerPortNameT
from .core import PortProtocol, RamsesProtocolT, ReadProtocol

_LOGGER = logging.getLogger(__name__)


def protocol_factory(
    msg_handler: MsgHandlerT,
    /,
    *,
    disable_qos: bool | None = DEFAULT_DISABLE_QOS,
    disable_sending: bool | None = False,
    enforce_include_list: bool = False,
    exclude_list: list[str] | None = None,
    include_list: list[str] | None = None,
    hgi_id: str | None = None,
) -> RamsesProtocolT:
    """Create and return a Ramses-specific async packet Protocol."""
    if disable_sending:
        _LOGGER.debug("ReadProtocol: Sending has been disabled")
        return ReadProtocol(
            msg_handler,
            enforce_include_list=enforce_include_list,
            exclude_list=exclude_list,
            include_list=include_list,
            hgi_id=hgi_id,
        )

    if disable_qos:
        _LOGGER.debug("PortProtocol: QoS has been disabled (will wait_for echos)")

    return PortProtocol(
        msg_handler,
        disable_qos=disable_qos,
        enforce_include_list=enforce_include_list,
        exclude_list=exclude_list,
        include_list=include_list,
        hgi_id=hgi_id,
    )


async def create_stack(
    msg_handler: MsgHandlerT,
    /,
    *,
    transport_config: TransportConfig,
    protocol_factory_: Callable[..., RamsesProtocolT] | None = None,
    transport_factory_: Callable[..., Awaitable[RamsesTransportT]] | None = None,
    port_name: SerPortNameT | None = None,
    port_config: PortConfigT | None = None,
    packet_log: str | None = None,
    packet_dict: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    disable_qos: bool | None = DEFAULT_DISABLE_QOS,
    enforce_include_list: bool = False,
    exclude_list: list[str] | None = None,
    include_list: list[str] | None = None,
    hgi_id: str | None = None,
) -> tuple[RamsesProtocolT, RamsesTransportT]:
    """Utility function to provide a Protocol / Transport pair.

    Architecture: gwy (client) -> msg (Protocol) -> pkt (Transport) -> HGI/log (or dict)
    - send Commands via awaitable Protocol.send_cmd(cmd)
    - receive Messages via msg_handler callback
    """
    read_only = bool(packet_dict or packet_log)
    if read_only:
        transport_config.disable_sending = True

    protocol: RamsesProtocolT = (protocol_factory_ or protocol_factory)(
        msg_handler,
        disable_qos=disable_qos,
        disable_sending=transport_config.disable_sending,
        enforce_include_list=enforce_include_list,
        exclude_list=exclude_list,
        include_list=include_list,
        hgi_id=hgi_id,
    )

    transport: RamsesTransportT = await (transport_factory_ or transport_factory)(
        protocol,
        config=transport_config,
        port_name=port_name,
        port_config=port_config,
        packet_log=packet_log,
        packet_dict=packet_dict,
        extra=extra,
        loop=loop,
    )

    if not port_name:
        # Safely extract the transport's mocked clock (e.g., FileTransport) without breaking the interface
        timesource: Callable[[], dt] = getattr(transport, "_dt_now", dt_now)
        set_logger_timesource(timesource)
        _LOGGER.warning("Logger datetimes maintained as most recent packet timestamp")

    return protocol, transport

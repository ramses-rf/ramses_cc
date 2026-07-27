#!/usr/bin/env python3
"""RAMSES RF - Factory for RAMSES-II compatible packet transports."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from serial import Serial, SerialException, serial_for_url

from .. import exceptions as exc
from ..interfaces import TransportInterface
from ..schemas import SCH_SERIAL_PORT_CONFIG
from ..typing import PortConfigT, SerPortNameT
from .base import TransportConfig
from .file import FileTransport
from .mqtt import MqttTransport
from .port import PortTransport

if TYPE_CHECKING:
    from ..protocol import RamsesProtocolT

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_PORT: Final[float] = 3.0
_DEFAULT_TIMEOUT_MQTT: Final[float] = 60.0
_DEFAULT_TIMEOUT_ZIGBEE: Final[float] = 60.0

RamsesTransportT: TypeAlias = TransportInterface


async def transport_factory(
    protocol: RamsesProtocolT,
    /,
    *,
    config: TransportConfig,
    port_name: SerPortNameT | None = None,
    port_config: PortConfigT | None = None,
    packet_log: str | None = None,
    packet_dict: dict[str, str] | None = None,
    transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None = None,
    extra: dict[str, Any] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> RamsesTransportT:
    """Create and return a Ramses-specific async packet Transport.

    :param protocol: The protocol instance that will use this transport.
    :type protocol: RamsesProtocolT
    :param config: Extracted setup configuration for transports.
    :type config: TransportConfig
    :param port_name: Serial port name or MQTT URL, defaults to None.
    :type port_name: SerPortNameT | None, optional
    :param port_config: Configuration dictionary for serial port, defaults to None.
    :type port_config: PortConfigT | None, optional
    :param packet_log: Path to a file containing packet logs for playback/parsing, defaults to None.
    :type packet_log: str | None, optional
    :param packet_dict: Dictionary of packets for playback, defaults to None.
    :type packet_dict: dict[str, str] | None, optional
    :param transport_constructor: Custom async callable to create a transport, defaults to None.
    :type transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None, optional
    :param extra: Extra configuration options, defaults to None.
    :type extra: dict[str, Any] | None, optional
    :param loop: Asyncio event loop, defaults to None.
    :type loop: asyncio.AbstractEventLoop | None, optional
    :return: An instantiated RamsesTransportT object.
    :rtype: RamsesTransportT
    :raises exc.TransportSourceInvalid: If the packet source is invalid or multiple sources are specified.
    """

    # Apply regex rules to the Protocol before binding the Transport
    if config.use_regex:
        protocol.set_regex_rules(config.use_regex)

    # If a constructor is provided, delegate entirely to it.
    if transport_constructor:
        _LOGGER.debug("transport_factory: Delegating to external transport_constructor")
        return await transport_constructor(
            protocol,
            config=config,
            extra=extra,
            loop=loop,
        )

    def get_serial_instance(
        ser_name: SerPortNameT, ser_config: PortConfigT | None
    ) -> Serial:
        """Return a Serial instance for the given port name and config.

        May: raise TransportSourceInvalid("Unable to open serial port...")

        :param ser_name: Name of the serial port.
        :type ser_name: SerPortNameT
        :param ser_config: Configuration for the serial port.
        :type ser_config: PortConfigT | None
        :return: Configured Serial object.
        :rtype: Serial
        :raises exc.TransportSourceInvalid: If the serial port cannot be opened.
        """
        # For example:
        # - python client.py monitor 'rfc2217://localhost:5001'
        # - python client.py monitor 'alt:///dev/ttyUSB0?class=PosixPollSerial'

        ser_config = SCH_SERIAL_PORT_CONFIG(ser_config or {})

        try:
            ser_obj = serial_for_url(ser_name, **ser_config)
        except SerialException as err:
            _LOGGER.error(
                "Failed to open %s (config: %s): %s", ser_name, ser_config, err
            )
            raise exc.TransportSourceInvalid(
                f"Unable to open the serial port: {ser_name}"
            ) from err

        # FTDI on Posix/Linux would be a common environment for this library...
        with contextlib.suppress(AttributeError, NotImplementedError, ValueError):
            ser_obj.set_low_latency_mode(True)

        return ser_obj

    def issue_warning() -> None:
        """Warn of the perils of semi-supported configurations."""
        _LOGGER.warning(
            f"{'Windows' if os.name == 'nt' else 'This type of serial interface'} "
            "is not fully supported by this library: "
            "please don't report any Transport/Protocol errors/warnings, "
            "unless they are reproducible with a standard configuration "
            "(e.g. linux with a local serial port)"
        )

    if len([x for x in (packet_dict, packet_log, port_name) if x is not None]) != 1:
        _LOGGER.warning(
            f"Input: packet_dict: {packet_dict}, packet_log: {packet_log}, port_name: {port_name}"
        )
        raise exc.TransportSourceInvalid(
            "Packet source must be exactly one of: packet_dict, packet_log, port_name"
        )

    # File
    if (pkt_source := packet_log or packet_dict) is not None:
        return FileTransport(
            pkt_source, protocol, config=config, extra=extra, loop=loop
        )

    assert port_name is not None  # mypy check

    # Zigbee
    if port_name[:6] == "zigbee":
        from .zigbee import ZigbeeTransport

        transport = ZigbeeTransport(
            port_name,
            protocol,
            config=config,
            loop=loop,
        )
        try:
            await protocol.wait_for_connection_made(timeout=_DEFAULT_TIMEOUT_ZIGBEE)
        except Exception:
            transport.close()
            raise
        return transport

    assert port_config is not None  # mypy check

    # MQTT
    if port_name[:4] == "mqtt":
        # Check for custom timeout in config, fallback to constant
        mqtt_timeout = config.timeout or _DEFAULT_TIMEOUT_MQTT

        transport = MqttTransport(
            port_name,
            protocol,
            config=config,
            extra=extra,
            loop=loop,
        )

        try:
            # Wait with timeout, handle failure gracefully
            await protocol.wait_for_connection_made(timeout=mqtt_timeout)
        except Exception:
            # Close the transport if setup fails to prevent "Zombie" callbacks
            transport.close()
            raise

        return transport

    # Serial
    ser_instance = get_serial_instance(port_name, port_config)

    if os.name == "nt" or (
        ser_instance.portstr and ser_instance.portstr[:7] in ("rfc2217", "socket:")
    ):
        issue_warning()  # TODO: add tests for these...

    transport_port = PortTransport(
        ser_instance,
        protocol,
        config=config,
        extra=extra,
        loop=loop,
    )

    # TODO: remove this? better to invoke timeout after factory returns?
    await protocol.wait_for_connection_made(
        timeout=config.timeout or _DEFAULT_TIMEOUT_PORT
    )
    # pytest-cov times out in virtual_rf.py when set below 30.0 on GitHub Actions
    return transport_port

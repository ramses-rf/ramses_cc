#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

:term:`Schema` processor for protocol (lower) layer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final, cast

import voluptuous as vol

from .const import (
    DEFAULT_ECHO_TIMEOUT,
    DEFAULT_RPLY_TIMEOUT,
    MAX_DUTY_CYCLE_RATE,
    MIN_INTER_WRITE_GAP,
)
from .typing import PktLogConfigT, PortConfigT

_LOGGER = logging.getLogger(__name__)


#
# 0/5: Packet source configuration
SZ_COMMS_PARAMS: Final = "comms_params"
SZ_DUTY_CYCLE_LIMIT: Final = "duty_cycle_limit"
SZ_GAP_BETWEEN_WRITES: Final = "gap_between_writes"
SZ_ECHO_TIMEOUT: Final = "echo_timeout"
SZ_RPLY_TIMEOUT: Final = "reply_timeout"

SCH_COMMS_PARAMS = vol.Schema(
    {
        vol.Required(SZ_DUTY_CYCLE_LIMIT, default=MAX_DUTY_CYCLE_RATE): vol.All(
            float, vol.Range(min=0.005, max=0.2)
        ),
        vol.Required(SZ_GAP_BETWEEN_WRITES, default=MIN_INTER_WRITE_GAP): vol.All(
            float, vol.Range(min=0.05, max=1.0)
        ),
        vol.Required(SZ_ECHO_TIMEOUT, default=DEFAULT_ECHO_TIMEOUT): vol.All(
            float, vol.Range(min=0.01, max=1.0)
        ),
        vol.Required(SZ_RPLY_TIMEOUT, default=DEFAULT_RPLY_TIMEOUT): vol.All(
            float, vol.Range(min=0.01, max=1.0)
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

#
# 1/5: Packet source configuration
SZ_INPUT_FILE: Final = "input_file"
SZ_PACKET_SOURCE: Final = "packet_source"


#
# 2/5: Packet log configuration
SZ_PACKET_LOG: Final = "packet_log"
SZ_PACKET_LOG_PATH: Final = "packet_log_path"
SZ_PACKET_LOG_PREFIX: Final = "packet_log_prefix"
SZ_PACKET_LOG_RETENTION_DAYS: Final = "packet_log_retention_days"
SZ_FLUSH_INTERVAL: Final = "flush_interval"
SZ_BUFFER_CAPACITY: Final = "buffer_capacity"
SZ_ROTATE_BYTES: Final = "rotate_bytes"


def sch_packet_log_dict_factory(
    default_backups: int | None = None,
    default_retention_days: int = 7,
) -> dict[vol.Required, vol.Any]:
    """
    :return: a packet log dict with a configurable default rotation policy.
    """

    if default_backups is not None:
        default_retention_days = default_backups

    SCH_PACKET_LOG_CONFIG = vol.Schema(
        {
            vol.Optional(SZ_PACKET_LOG_PATH, default=""): str,
            vol.Optional(SZ_PACKET_LOG_PREFIX, default="packet_log"): str,
            vol.Optional(
                SZ_PACKET_LOG_RETENTION_DAYS, default=default_retention_days
            ): vol.Any(None, int),
            vol.Optional(SZ_ROTATE_BYTES): vol.Any(None, int),
            vol.Optional(SZ_FLUSH_INTERVAL, default=60): vol.Any(None, int, float),
            vol.Optional(SZ_BUFFER_CAPACITY, default=0): vol.Any(None, int),
            vol.Optional("flush_level"): vol.Any(None, int, str),
        },
        extra=vol.PREVENT_EXTRA,
    )

    SCH_PACKET_LOG_NAME = str

    def NormalisePacketLog(
        retention_days: int = 7,
    ) -> Callable[[str | PktLogConfigT], PktLogConfigT]:
        def normalise_packet_log(
            node_value: str | PktLogConfigT,
        ) -> PktLogConfigT:
            if isinstance(node_value, str):
                return {
                    SZ_PACKET_LOG_PATH: "",
                    SZ_PACKET_LOG_PREFIX: node_value,
                    SZ_PACKET_LOG_RETENTION_DAYS: retention_days,
                    SZ_ROTATE_BYTES: None,
                    SZ_FLUSH_INTERVAL: 60,
                    SZ_BUFFER_CAPACITY: 0,
                }
            return node_value

        return normalise_packet_log

    return {  # SCH_PACKET_LOG_DICT
        vol.Required(SZ_PACKET_LOG, default=None): vol.Any(
            None,
            vol.All(
                SCH_PACKET_LOG_NAME,
                NormalisePacketLog(retention_days=default_retention_days),
            ),
            SCH_PACKET_LOG_CONFIG,
        )
    }


SCH_PACKET_LOG = vol.Schema(
    sch_packet_log_dict_factory(default_retention_days=7),
    extra=vol.PREVENT_EXTRA,
)

#
# 3/5: Serial port configuration
SZ_PORT_CONFIG: Final = "port_config"
SZ_PORT_NAME: Final = "port_name"
SZ_SERIAL_PORT: Final = "serial_port"

SZ_BAUDRATE: Final = "baudrate"
SZ_DSRDTR: Final = "dsrdtr"
SZ_RTSCTS: Final = "rtscts"
SZ_TIMEOUT: Final = "timeout"
SZ_XONXOFF: Final = "xonxoff"


SCH_SERIAL_PORT_CONFIG = vol.Schema(
    {
        vol.Optional(SZ_BAUDRATE, default=115200): vol.All(
            vol.Coerce(int), vol.Any(57600, 115200)
        ),
        vol.Optional(SZ_DSRDTR, default=False): bool,
        vol.Optional(SZ_RTSCTS, default=False): bool,
        vol.Optional(SZ_TIMEOUT, default=0): vol.Any(None, int),
        vol.Optional(SZ_XONXOFF, default=True): bool,
    },
    extra=vol.PREVENT_EXTRA,
)


def sch_serial_port_dict_factory() -> dict[vol.Required, vol.Any]:
    """Return a serial port dict."""

    SCH_SERIAL_PORT_NAME = str

    def NormaliseSerialPort() -> Callable[[str | PortConfigT], PortConfigT]:
        def normalise_serial_port(
            node_value: str | PortConfigT,
        ) -> PortConfigT:
            if isinstance(node_value, str):
                return cast(
                    "PortConfigT",
                    {SZ_PORT_NAME: node_value} | SCH_SERIAL_PORT_CONFIG({}),
                )
            return node_value

        return normalise_serial_port

    return {  # SCH_SERIAL_PORT_DICT
        vol.Required(SZ_SERIAL_PORT): vol.Any(
            vol.All(
                SCH_SERIAL_PORT_NAME,
                NormaliseSerialPort(),
            ),
            SCH_SERIAL_PORT_CONFIG.extend(
                {vol.Required(SZ_PORT_NAME): SCH_SERIAL_PORT_NAME}
            ),
        )
    }


def extract_serial_port(ser_port_dict: dict[str, Any]) -> tuple[str, PortConfigT]:
    """Extract a serial port, port_config_dict tuple from a
    sch_serial_port_dict."""
    port_name = str(ser_port_dict.get(SZ_PORT_NAME, ""))
    port_config = cast(
        "PortConfigT", {k: v for k, v in ser_port_dict.items() if k != SZ_PORT_NAME}
    )
    return port_name, port_config


#
# 4/5: Traits (of devices) configuration (basic)

SZ_BLOCK_LIST: Final = "block_list"
SZ_KNOWN_LIST: Final = "known_list"


def select_device_filter_mode(
    enforce_known_list: bool,
    known_list: list[str],
    block_list: list[str],
) -> bool:
    """Determine which device filter to use, if any."""

    known_warn_line2: Final = (
        "In Ramses RF Config, turn On 'Accept packets from known device IDs only'. "
    )
    known_warn_line3: str = (
        f"For CLI, add `configure: enforce_{SZ_KNOWN_LIST} = True` to a config file."
    )

    if enforce_known_list and not known_list:
        _LOGGER.warning(
            f"Best practice is to enforce a {SZ_KNOWN_LIST} (an allow list), "
            f"but it is empty, so it can't be used. "
        )
        enforce_known_list = False

    if enforce_known_list:
        _LOGGER.info(
            f"A valid {SZ_KNOWN_LIST} was provided, "
            f"and will be enforced as a allow list: length = {len(known_list)}"
        )
        _LOGGER.debug(f"known_list = {known_list}")

    elif block_list:
        _LOGGER.info(
            f"A valid {SZ_BLOCK_LIST} was provided, "
            f"and will be used as a deny list: length = {len(block_list)}"
        )
        _LOGGER.debug(f"block_list = {block_list}")

    elif known_list:
        _LOGGER.warning(
            f"Best practice is to enforce the {SZ_KNOWN_LIST} as an allow "
            "list, " + known_warn_line2 + known_warn_line3
        )
        _LOGGER.debug(f"known_list = {known_list}")

    else:
        _LOGGER.warning(
            f"Best practice is to provide a {SZ_KNOWN_LIST} and enforce it, "
            + known_warn_line2
            + known_warn_line3
        )

    return enforce_known_list


#
# 5/5: Gateway (engine) configuration

SZ_DISABLE_SENDING: Final = "disable_sending"
SZ_AUTOSTART: Final = "autostart"
SZ_DISABLE_QOS: Final = "disable_qos"
SZ_ENFORCE_KNOWN_LIST: Final[str] = f"enforce_{SZ_KNOWN_LIST}"
SZ_EVOFW_FLAG: Final = "evofw_flag"
SZ_LOG_ALL_MQTT: Final = "log_all_mqtt"
SZ_USE_REGEX: Final = "use_regex"

SCH_ENGINE_DICT = {
    vol.Optional(SZ_DISABLE_SENDING, default=False): bool,
    vol.Optional(SZ_AUTOSTART, default=False): bool,
    vol.Optional(SZ_DISABLE_QOS, default=None): vol.Any(None, bool),
    vol.Optional(SZ_ENFORCE_KNOWN_LIST, default=False): bool,
    vol.Optional(SZ_EVOFW_FLAG): vol.Any(None, str),
    vol.Optional(SZ_LOG_ALL_MQTT, default=False): bool,
    vol.Optional(SZ_USE_REGEX): dict,
    vol.Optional(SZ_COMMS_PARAMS): SCH_COMMS_PARAMS,
}
SCH_ENGINE_CONFIG = vol.Schema(SCH_ENGINE_DICT, extra=vol.REMOVE_EXTRA)

SZ_INBOUND: Final = "inbound"
SZ_OUTBOUND: Final = "outbound"

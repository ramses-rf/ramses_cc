#!/usr/bin/env python3

"""RAMSES RF - Transport layer configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .typing import PktLogConfigT, PortConfigT


@dataclass
class EngineConfig:
    """Configuration parameters for the Ramses Engine.

    :param port_name: The serial port name (e.g., '/dev/ttyUSB0').
    :type port_name: str | None
    :param input_file: Path to a packet log file for playback/parsing.
    :type input_file: str | None
    :param port_config: Configuration dictionary for the serial port.
    :type port_config: PortConfigT | None
    :param packet_log: Configuration for packet logging.
    :type packet_log: PktLogConfigT | None
    :param block_list: A list of device IDs to block/ignore.
    :type block_list: list[str] | None
    :param known_list: A list of known device IDs.
    :type known_list: list[str] | None
    :param hgi_id: The Device ID to use for the HGI, overriding defaults.
    :type hgi_id: str | None
    :param disable_sending: Prevent sending any packets.
    :type disable_sending: bool
    :param disable_qos: Disable the Quality of Service mechanism.
    :type disable_qos: bool | None
    :param enforce_known_list: Enforce that only known devices are used.
    :type enforce_known_list: bool
    :param log_all_mqtt: Enable logging all MQTT messages.
    :type log_all_mqtt: bool
    :param evofw_flag: Specific flag for evofw3 usage.
    :type evofw_flag: str | None
    :param use_regex: Regex patterns for matching devices.
    :type use_regex: dict[str, dict[str, str]]
    :param app_context: Optional application context object.
    :type app_context: Any | None
    """

    port_name: str | None = None
    input_file: str | None = None
    port_config: PortConfigT | None = None
    packet_log: PktLogConfigT | None = None
    block_list: list[str] | None = None
    known_list: list[str] | None = None
    hgi_id: str | None = None
    disable_sending: bool = False
    disable_qos: bool | None = None
    enforce_known_list: bool = False
    log_all_mqtt: bool = False
    evofw_flag: str | None = None
    use_regex: dict[str, dict[str, str]] = field(default_factory=dict)
    app_context: Any | None = None

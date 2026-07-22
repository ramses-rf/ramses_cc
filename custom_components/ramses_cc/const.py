"""Constants for RAMSES integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from homeassistant.const import CONF_SCAN_INTERVAL as CONF_SCAN_INTERVAL

from ramses_rf.protocol.ramses import _2411_PARAMS_SCHEMA as _2411_PARAMS_SCHEMA
from ramses_rf.schemas import SZ_BOUND_TO as SZ_BOUND_TO, SZ_SCHEMA as SZ_SCHEMA
from ramses_tx.address import HGI_DEVICE_ID as HGI_DEVICE_ID
from ramses_tx.const import SZ_IS_EVOFW3 as SZ_IS_EVOFW3
from ramses_tx.schemas import (
    SZ_BUFFER_CAPACITY as SZ_BUFFER_CAPACITY,
    SZ_ENFORCE_KNOWN_LIST as SZ_ENFORCE_KNOWN_LIST,
    SZ_FLUSH_INTERVAL as SZ_FLUSH_INTERVAL,
    SZ_KNOWN_LIST as SZ_KNOWN_LIST,
    SZ_PACKET_LOG as SZ_PACKET_LOG,
    SZ_PACKET_LOG_PATH as SZ_PACKET_LOG_PATH,
    SZ_PACKET_LOG_PREFIX as SZ_PACKET_LOG_PREFIX,
    SZ_PACKET_LOG_RETENTION_DAYS as SZ_PACKET_LOG_RETENTION_DAYS,
    SZ_PORT_NAME as SZ_PORT_NAME,
    SZ_SERIAL_PORT as SZ_SERIAL_PORT,
)

DOMAIN: Final = "ramses_cc"

STORAGE_VERSION: Final[int] = 1
STORAGE_KEY: Final = DOMAIN

# Dispatcher signals
SIGNAL_NEW_DEVICES: Final = f"{DOMAIN}_new_devices_" + "{}"
SIGNAL_UPDATE: Final = f"{DOMAIN}_update"

# Config
CONF_ADVANCED_FEATURES: Final = "advanced_features"
CONF_COMMANDS: Final = "commands"
CONF_DEV_MODE: Final = "dev_mode"
CONF_FRESH_START: Final = "fresh_start"
CONF_SSOT_MIGRATED: Final = "ssot_migration_done"
CONF_GATEWAY_TIMEOUT: Final = "gateway_timeout"
CONF_MESSAGE_EVENTS: Final = "message_events"
CONF_MQTT_USE_HA: Final = "mqtt_use_ha"
CONF_MQTT_HGI_ID: Final = "mqtt_hgi_id"
CONF_MQTT_TOPIC: Final = "mqtt_topic"
CONF_RAMSES_RF: Final = "ramses_rf"
CONF_SCHEMA: Final = "schema"
CONF_SEND_PACKET: Final = "send_packet"
CONF_UNKNOWN_CODES: Final = "unknown_codes"

# Defaults
DEFAULT_MQTT_TOPIC: Final = "RAMSES/GATEWAY"
DEFAULT_HGI_ID: Final = HGI_DEVICE_ID

# State
SZ_CLIENT_STATE: Final = "client_state"
SZ_PACKETS: Final = "packets"
SZ_REMOTES: Final = "remotes"
SZ_HVAC_SCHEMA: Final = "hvac_schema"  # cached HVAC topology (load_fan stub workaround)

# Entity/service attributes
ATTR_ACTIVE: Final = "active"
ATTR_ACTIVE_FAULTS: Final = "active_faults"
ATTR_ACTUATOR: Final = "enabled"
ATTR_CO2_LEVEL: Final = "co2_level"
ATTR_COMMAND: Final = "command"
ATTR_DELAY_SECS: Final = "delay_secs"
ATTR_DEVICE_ID: Final = "device_id"
ATTR_DIFFERENTIAL: Final = "differential"
ATTR_DURATION: Final = "duration"
ATTR_FAN_RATE: Final = "fan_rate"
ATTR_FAULT_LOG: Final = "fault_log"
ATTR_HEAT_DEMAND: Final = "heat_demand"
ATTR_HUMIDITY: Final = "relative_humidity"
ATTR_INDOOR_HUMIDITY: Final = "indoor_humidity"
ATTR_LATEST_EVENT: Final = "latest_event"
ATTR_LATEST_FAULT: Final = "latest_fault"
ATTR_LOCAL_OVERRIDE: Final = "local_override"
ATTR_MAX_TEMP: Final = "max_temp"
ATTR_MIN_TEMP: Final = "min_temp"
ATTR_MODE: Final = "mode"
ATTR_MULTIROOM: Final = "multiroom_mode"
ATTR_NUM_ENTRIES: Final = "num_entries"
ATTR_NUM_REPEATS: Final = "num_repeats"
ATTR_OPENWINDOW: Final = "openwindow_function"
ATTR_OVERRUN: Final = "overrun"
ATTR_PERIOD: Final = "period"
ATTR_RELAY_DEMAND: Final = "relay_demand"
ATTR_SCHEDULE: Final = "schedule"
ATTR_SETPOINT: Final = "setpoint"
ATTR_SYSTEM_MODE: Final = "system_mode"
ATTR_TEMPERATURE: Final = "temperature"
ATTR_TIMEOUT: Final = "timeout"
ATTR_UNTIL: Final = "until"
ATTR_WINDOW: Final = "window_open"
ATTR_WORKING_SCHEMA: Final = "working_schema"

# Unofficial presets
PRESET_CUSTOM: Final = "custom"
PRESET_TEMPORARY: Final = "temporary"
PRESET_PERMANENT: Final = "permanent"

# Service name
SVC_DISCOVER_KNOWN_DEVICES: Final = "discover_known_devices"
SVC_GET_DISCOVERED_DEVICES: Final = "get_discovered_devices"
SVC_ACCEPT_DISCOVERED_DEVICE: Final = "accept_discovered_device"
SVC_DISCARD_DISCOVERED_DEVICE: Final = "discard_discovered_device"
SVC_REMOVE_DISCOVERED_DEVICE: Final = "remove_discovered_device"
SVC_ENABLE_DISCOVERED_DEVICE: Final = "enable_discovered_device"
SVC_DISABLE_DISCOVERED_DEVICE: Final = "disable_discovered_device"
SVC_ADD_FAKED_REM: Final = "add_faked_rem"
SVC_REMOVE_DEVICE: Final = "remove_device"

# Discovery config
CONF_PASSIVE_SCAN: Final = "passive_scan"
CONF_AUTO_NOTIFY: Final = "auto_notify"
CONF_LOST_THRESHOLD: Final = "lost_threshold_days"

# Schema extensions (ramses_cc-only keys, stripped before passing to ramses_rf)
SZ_DEVICE_COMMENTS: Final = "device_comments"
SZ_SCHEMA_BACKUP: Final = "schema_backup"

# User-authored schema traits (_ prefixed keys, stripped before ramses_rf).
# These live inside device entries in the schema and are preserved by
# sync_learned_topology, but stripped by _strip_schema_extensions and
# strip_traits_for_validation before the schema reaches ramses_rf.
SZ_TR_DISABLED: Final = "_disabled"  # bool: exclude from entity creation
SZ_TR_SKIPPED: Final = "_skipped"  # bool: user deferred decision, re-appears in review
SZ_TR_NAME: Final = "_name"  # str: human-friendly display name
SZ_TR_ALIAS: Final = "_alias"  # str: alternate name (e.g. for entities)
SZ_TR_CLASS: Final = "_class"  # str: override device class (CTL, TRV, DHW, ...)
SZ_TR_COMMENT: Final = "_comment"  # str: free-form per-device comment
SZ_TR_OWNER: Final = (
    "_owner"  # str: owner name (matches root _owner = ours, else foreign)
)
SZ_TR_FAKED: Final = "_faked"  # bool: create a virtual/fake device (no RF traffic)
SZ_TR_BOUND: Final = (
    "_bound"  # str: for FAN, the bound REM/DIS device ID (2411 routing)
)
SZ_TR_SCHEME: Final = (
    "_scheme"  # str: FAN manufacturer scheme (orcon/itho/vasco/nuaire)
)
SZ_TR_COMMANDS: Final = (
    "_commands"  # dict[str, str]: learned RF payloads for REM entities
)

# Root-level schema key for the system owner name.
# Devices whose _owner matches this value are "ours" (included in known_list).
# Devices with a different _owner are "foreign" (added to block_list).
SZ_OWNER: Final = "_owner"  # root-level key (same name as per-device trait)

# All recognised trait keys (for iteration / validation)
SZ_TRAITS: Final = (
    SZ_TR_DISABLED,
    SZ_TR_SKIPPED,
    SZ_TR_NAME,
    SZ_TR_ALIAS,
    SZ_TR_CLASS,
    SZ_TR_COMMENT,
    SZ_TR_OWNER,
    SZ_TR_FAKED,
    SZ_TR_BOUND,
    SZ_TR_SCHEME,
)


# Volume Flow Rate units, these specific unit are not defined in HA v2024.1
class UnitOfVolumeFlowRate(StrEnum):
    """Volume flow rate units (defined by integration)."""

    LITERS_PER_MINUTE = "L/min"
    LITERS_PER_SECOND = "L/s"


class SystemMode(StrEnum):
    """System modes."""

    AUTO = "auto"
    AWAY = "away"
    CUSTOM = "custom"
    DAY_OFF = "day_off"
    DAY_OFF_ECO = "day_off_eco"  # set to Eco when DayOff ends
    ECO_BOOST = "eco_boost"  # Eco, or Boost
    HEAT_OFF = "heat_off"
    RESET = "auto_with_reset"


class ZoneMode(StrEnum):
    """Zone modes."""

    SCHEDULE = "follow_schedule"
    ADVANCED = "advanced_override"  # until the next setpoint
    PERMANENT = "permanent_override"  # indefinitely
    COUNTDOWN = "countdown_override"  # for a number of minutes (max 1,215)
    TEMPORARY = "temporary_override"  # until a given date/time

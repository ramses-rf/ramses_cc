"""Constants for RAMSES integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "ramses_cc"

STORAGE_VERSION: Final[int] = 1
STORAGE_KEY: Final = DOMAIN

# Dispatcher signals
SIGNAL_NEW_DEVICES: Final = f"{DOMAIN}_new_devices_" + "{}"
SIGNAL_UPDATE: Final = f"{DOMAIN}_update"
SIGNAL_PARAM_SUPPORT_CHANGED: Final = f"{DOMAIN}_param_support_changed"

# Config
CONF_ADVANCED_FEATURES: Final = "advanced_features"
CONF_COMMANDS: Final = "commands"
CONF_DEV_MODE: Final = "dev_mode"
CONF_MESSAGE_EVENTS: Final = "message_events"
CONF_RAMSES_RF: Final = "ramses_rf"
CONF_SCHEMA: Final = "schema"
CONF_SEND_PACKET: Final = "send_packet"
CONF_UNKNOWN_CODES: Final = "unknown_codes"

# State
SZ_CLIENT_STATE: Final = "client_state"
SZ_PACKETS: Final = "packets"
SZ_REMOTES: Final = "remotes"

# Entity/service attributes
ATTR_ACTIVE: Final = "active"
ATTR_ACTIVE_FAULTS: Final = "active_faults"
ATTR_ACTUATOR: Final = "enabled"
ATTR_BATTERY: Final = "battery_low"
ATTR_BATTERY_LEVEL: Final = "battery_level"
ATTR_CO2_LEVEL: Final = "co2_level"
ATTR_COMMAND: Final = "command"
ATTR_DELAY_SECS: Final = "delay_secs"
ATTR_DEVICE_ID: Final = "device_id"
ATTR_DIFFERENTIAL: Final = "differential"
ATTR_DURATION: Final = "duration"
ATTR_FAN_RATE: Final = "fan_rate"
ATTR_FAULT_LOG: Final = "fault_log"
ATTR_HEAT_DEMAND: Final = "heat_demand"
ATTR_PARAM_ID: Final = "parameter_id"
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


# Volume Flow Rate units, these specific unit are not defined in HA v2024.1
class UnitOfVolumeFlowRate(StrEnum):
    """Volume flow rate units (defined by integration)."""

    LITERS_PER_MINUTE: Final = "L/min"
    LITERS_PER_SECOND: Final = "L/s"


class SystemMode(StrEnum):
    """System modes."""

    AUTO: Final = "auto"
    AWAY: Final = "away"
    CUSTOM: Final = "custom"
    DAY_OFF: Final = "day_off"
    DAY_OFF_ECO: Final = "day_off_eco"  # set to Eco when DayOff ends
    ECO_BOOST: Final = "eco_boost"  # Eco, or Boost
    HEAT_OFF: Final = "heat_off"
    RESET: Final = "auto_with_reset"


class ZoneMode(StrEnum):
    """Zone modes."""

    SCHEDULE: Final = "follow_schedule"
    ADVANCED: Final = "advanced_override"  # until the next setpoint
    PERMANENT: Final = "permanent_override"  # indefinitely
    COUNTDOWN: Final = "countdown_override"  # for a number of minutes (max 1,215)
    TEMPORARY: Final = "temporary_override"  # until a given date/time

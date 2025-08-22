"""Schemas for RAMSES integration."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import timedelta
from typing import Any, Final, NewType

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers import config_validation as cv

from ramses_rf.helpers import deep_merge, is_subset, shrink
from ramses_rf.schemas import (
    SCH_GATEWAY_CONFIG,
    SCH_GLOBAL_SCHEMAS_DICT,
    SCH_RESTORE_CACHE_DICT,
    SZ_APPLIANCE_CONTROL,
    SZ_BLOCK_LIST,
    SZ_CONFIG,
    SZ_KNOWN_LIST,
    SZ_ORPHANS_HEAT,
    SZ_ORPHANS_HVAC,
    SZ_RESTORE_CACHE,
    SZ_SENSOR,
    SZ_SYSTEM,
    SZ_ZONES,
)
from ramses_tx.const import COMMAND_REGEX
from ramses_tx.schemas import (
    SCH_ENGINE_DICT,
    SZ_PORT_CONFIG,
    SZ_SERIAL_PORT,
    extract_serial_port,
    sch_global_traits_dict_factory,
    sch_packet_log_dict_factory,
    sch_serial_port_dict_factory,
)

from .const import (
    ATTR_ACTIVE,
    ATTR_CO2_LEVEL,
    ATTR_COMMAND,
    ATTR_DELAY_SECS,
    ATTR_DEVICE_ID,
    ATTR_DIFFERENTIAL,
    ATTR_DURATION,
    ATTR_INDOOR_HUMIDITY,
    ATTR_LOCAL_OVERRIDE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_MODE,
    ATTR_MULTIROOM,
    ATTR_NUM_ENTRIES,
    ATTR_NUM_REPEATS,
    ATTR_OPENWINDOW,
    ATTR_OVERRUN,
    ATTR_PERIOD,
    ATTR_SCHEDULE,
    ATTR_SETPOINT,
    ATTR_TEMPERATURE,
    ATTR_TIMEOUT,
    ATTR_UNTIL,
    CONF_ADVANCED_FEATURES,
    CONF_COMMANDS,
    CONF_DEV_MODE,
    CONF_MESSAGE_EVENTS,
    CONF_RAMSES_RF,
    CONF_SEND_PACKET,
    CONF_UNKNOWN_CODES,
    SystemMode,
    ZoneMode,
)

_SchemaT = NewType("_SchemaT", dict[str, Any])

_LOGGER = logging.getLogger(__name__)

#
# Configuration schema for Integration/domain
SCAN_INTERVAL_DEFAULT = timedelta(seconds=60)
SCAN_INTERVAL_MINIMUM = timedelta(seconds=3)


SCH_ADVANCED_FEATURES = vol.Schema(
    {
        vol.Optional(CONF_SEND_PACKET, default=False): cv.boolean,
        vol.Optional(CONF_MESSAGE_EVENTS, default=None): vol.Any(None, cv.is_regex),
        vol.Optional(CONF_DEV_MODE): cv.boolean,
        vol.Optional(CONF_UNKNOWN_CODES): cv.boolean,
    }
)

SCH_GLOBAL_TRAITS_DICT, SCH_TRAITS = sch_global_traits_dict_factory(
    hvac_traits={vol.Optional(CONF_COMMANDS): dict}
)

SCH_GATEWAY_CONFIG = SCH_GATEWAY_CONFIG.extend(
    SCH_ENGINE_DICT,
    extra=vol.PREVENT_EXTRA,
)

SCH_PACKET_LOG = sch_packet_log_dict_factory(default_backups=7)

SCH_DOMAIN_CONFIG = (
    vol.Schema(
        {
            vol.Optional(CONF_RAMSES_RF, default={}): SCH_GATEWAY_CONFIG,
            vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL_DEFAULT): vol.All(
                cv.time_period, vol.Range(min=SCAN_INTERVAL_MINIMUM)
            ),
            vol.Optional(CONF_ADVANCED_FEATURES, default={}): SCH_ADVANCED_FEATURES,
        },
        extra=vol.PREVENT_EXTRA,  # will be system, orphan schemas for ramses_rf
    )
    .extend(SCH_GLOBAL_SCHEMAS_DICT)
    .extend(SCH_GLOBAL_TRAITS_DICT)
    .extend(sch_packet_log_dict_factory(default_backups=7))
    .extend(SCH_RESTORE_CACHE_DICT)
    .extend(sch_serial_port_dict_factory())
)

SCH_MINIMUM_TCS = vol.Schema(
    {
        vol.Optional(SZ_SYSTEM): vol.Schema(
            {vol.Required(SZ_APPLIANCE_CONTROL): vol.Match(r"^10:[0-9]{6}$")}
        ),
        vol.Optional(SZ_ZONES, default={}): vol.Schema(
            {
                vol.Required(str): vol.Schema(
                    {vol.Required(SZ_SENSOR): vol.Match(r"^01:[0-9]{6}$")}
                )
            }
        ),
    },
    extra=vol.PREVENT_EXTRA,
)


def normalise_config(config: _SchemaT) -> tuple[str, _SchemaT, _SchemaT]:
    """Return a port/client_config/broker_config for the library."""

    config = deepcopy(config)

    config[SZ_CONFIG] = config.pop(CONF_RAMSES_RF)

    port_name, port_config = extract_serial_port(config.pop(SZ_SERIAL_PORT))

    remote_commands = {
        k: v.pop(CONF_COMMANDS)
        for k, v in config[SZ_KNOWN_LIST].items()
        if v.get(CONF_COMMANDS)
    }

    broker_keys = (CONF_SCAN_INTERVAL, CONF_ADVANCED_FEATURES, SZ_RESTORE_CACHE)
    return (  # type: ignore[return-value]
        port_name,
        {k: v for k, v in config.items() if k not in broker_keys}
        | {SZ_PORT_CONFIG: port_config},
        {k: v for k, v in config.items() if k in broker_keys}
        | {"remotes": remote_commands},
    )


def merge_schemas(config_schema: _SchemaT, cached_schema: _SchemaT) -> _SchemaT | None:
    """Return the config schema deep merged into the cached schema."""

    if is_subset(shrink(config_schema), shrink(cached_schema)):
        _LOGGER.info("Using the cached schema")
        return cached_schema

    merged_schema: _SchemaT = deep_merge(config_schema, cached_schema)  # 1st precedent

    if is_subset(shrink(config_schema), shrink(merged_schema)):
        _LOGGER.info("Using a merged schema")
        return merged_schema

    _LOGGER.info("Cached schema is a subset of config schema")
    return None


def schema_is_minimal(schema: _SchemaT) -> bool:
    """Return True if the schema is minimal (i.e. no optional keys)."""

    key: str
    sch: _SchemaT

    for key, sch in schema.items():
        if key in (SZ_BLOCK_LIST, SZ_KNOWN_LIST, SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC):
            continue

        try:
            _ = SCH_MINIMUM_TCS(shrink(sch))
        except vol.Invalid:
            return False

        if SZ_ZONES in sch and list(sch[SZ_ZONES].values())[0][SZ_SENSOR] != key:
            return False

    return True


SCH_NO_SVC_PARAMS = vol.Schema({}, extra=vol.PREVENT_EXTRA)
SCH_NO_ENTITY_SVC_PARAMS = cv.make_entity_service_schema(
    {},
    extra=vol.PREVENT_EXTRA,
)


# services for ramses_cc integration

_SCH_DEVICE_ID = cv.matches_regex(r"^[0-9]{2}:[0-9]{6}$")
_SCH_CMD_CODE = cv.matches_regex(r"^[0-9A-F]{4}$")
_SCH_DOM_IDX = cv.matches_regex(r"^[0-9A-F]{2}$")
_SCH_COMMAND = cv.matches_regex(COMMAND_REGEX)

_SCH_BINDING = vol.Schema({vol.Required(_SCH_CMD_CODE): vol.Any(None, _SCH_DOM_IDX)})

SCH_BIND_DEVICE = vol.Schema(
    {
        vol.Required("device_id"): _SCH_DEVICE_ID,
        vol.Required("offer"): vol.All(_SCH_BINDING, vol.Length(min=1)),
        vol.Optional("confirm", default={}): vol.Any(
            {}, vol.All(_SCH_BINDING, vol.Length(min=1))
        ),
        vol.Optional("device_info", default=None): vol.Any(None, _SCH_COMMAND),
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SEND_PACKET = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
        vol.Optional("from_id"): _SCH_DEVICE_ID,
        vol.Required("verb"): vol.In((" I", "I", "RQ", "RP", " W", "W")),
        vol.Required("code"): cv.matches_regex(r"^[0-9A-F]{4}$"),
        vol.Required("payload"): cv.matches_regex(r"^([0-9A-F][0-9A-F]){1,48}$"),
    }
)

SVC_BIND_DEVICE: Final = "bind_device"
SVC_FORCE_UPDATE: Final = "force_update"
SVC_SEND_PACKET: Final = "send_packet"


# services for sensor platform

MIN_CO2_LEVEL: Final[int] = 300
MAX_CO2_LEVEL: Final[int] = 9999

SVC_PUT_CO2_LEVEL: Final = "put_co2_level"
SCH_PUT_CO2_LEVEL = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_CO2_LEVEL): vol.All(
            cv.positive_int,
            vol.Range(min=MIN_CO2_LEVEL, max=MAX_CO2_LEVEL),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

MIN_DHW_TEMP: Final[float] = 0
MAX_DHW_TEMP: Final[float] = 99

SVC_PUT_DHW_TEMP: Final = "put_dhw_temp"
SCH_PUT_DHW_TEMP = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float),
            vol.Range(min=MIN_DHW_TEMP, max=MAX_DHW_TEMP),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

MIN_INDOOR_HUMIDITY: Final[float] = 0
MAX_INDOOR_HUMIDITY: Final[float] = 100

SVC_PUT_INDOOR_HUMIDITY: Final = "put_indoor_humidity"
SCH_PUT_INDOOR_HUMIDITY = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_INDOOR_HUMIDITY): vol.All(
            cv.positive_float,
            vol.Range(min=MIN_INDOOR_HUMIDITY, max=MAX_INDOOR_HUMIDITY),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

MIN_ROOM_TEMP: Final[float] = -20
MAX_ROOM_TEMP: Final[float] = 60

SVC_PUT_ROOM_TEMP: Final = "put_room_temp"
SCH_PUT_ROOM_TEMP = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float),
            vol.Range(min=MIN_ROOM_TEMP, max=MAX_ROOM_TEMP),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

SVCS_RAMSES_SENSOR = {
    SVC_PUT_CO2_LEVEL: SCH_PUT_CO2_LEVEL,
    SVC_PUT_DHW_TEMP: SCH_PUT_DHW_TEMP,
    SVC_PUT_INDOOR_HUMIDITY: SCH_PUT_INDOOR_HUMIDITY,
    SVC_PUT_ROOM_TEMP: SCH_PUT_ROOM_TEMP,
}

# services for climate platform

SCH_DURATION = vol.All(  # of time (<=24h)
    cv.time_period,
    vol.Range(min=timedelta(hours=1), max=timedelta(hours=24)),
)
SCH_PERIOD = vol.All(  # of days (0-99)
    cv.time_period, vol.Range(min=timedelta(days=0), max=timedelta(days=99))
)

SVC_SET_SYSTEM_MODE: Final = "set_system_mode"
SCH_SET_SYSTEM_MODE = cv.make_entity_service_schema(
    # nested schemas not allowed after HA 2025.9, extra check moved to climate.py
    {
        vol.Required(ATTR_MODE): vol.In(SystemMode),
        vol.Optional(ATTR_DURATION): vol.Any(SCH_DURATION, None),
        # canBeTemporary: true, timingMode: Duration
        vol.Optional(ATTR_PERIOD): vol.Any(SCH_PERIOD, None),
        # Period: None is indefinitely; 0 is the end of today, 1 is end of tomorrow
    }
)

SCH_SET_SYSTEM_MODE_EXTRA = vol.Schema(  # original Entity Service action validation schema
    # vol.Msg(  # TODO turn on if good checks are working 8-2025
    vol.Any(
        {  # A also: Off, Heat, Cool (for pre-evohome)
            vol.Required(ATTR_MODE): vol.In(
                [SystemMode.AUTO, SystemMode.HEAT_OFF, SystemMode.RESET]
            )
        },
        {  # B
            vol.Required(ATTR_MODE): vol.In([SystemMode.ECO_BOOST]),
            vol.Optional(ATTR_DURATION): vol.Any(SCH_DURATION, None),
        },  # duration: : None is indefinitely; 0 is invalid
        {  # C canBeTemporary: true, timingMode: Period
            vol.Required(ATTR_MODE): vol.In(
                [
                    SystemMode.AWAY,
                    SystemMode.CUSTOM,
                    SystemMode.DAY_OFF,
                    SystemMode.DAY_OFF_ECO,
                ]
            ),
            vol.Optional(ATTR_PERIOD): vol.Any(SCH_PERIOD, None),
        },  # Period: None is indefinitely; 0 is the end of today, 1 is end of tomorrow
    ),
    #     msg="Invalid ramses_cc Zone Mode entry in Entity Service call",
    # ),
    extra=vol.PREVENT_EXTRA,
)

DEFAULT_MIN_TEMP: Final[float] = 5
MIN_MIN_TEMP: Final[float] = 5
MAX_MIN_TEMP: Final[float] = 21

DEFAULT_MAX_TEMP: Final[float] = 35
MIN_MAX_TEMP: Final[float] = 21
MAX_MAX_TEMP: Final[float] = 35

SVC_SET_ZONE_CONFIG: Final = "set_zone_config"
SCH_SET_ZONE_CONFIG = cv.make_entity_service_schema(
    {
        vol.Optional(ATTR_MAX_TEMP, default=DEFAULT_MAX_TEMP): vol.All(
            cv.positive_float, vol.Range(min=MIN_MAX_TEMP, max=MAX_MAX_TEMP)
        ),
        vol.Optional(ATTR_MIN_TEMP, default=DEFAULT_MIN_TEMP): vol.All(
            cv.positive_float, vol.Range(min=MIN_MIN_TEMP, max=MAX_MIN_TEMP)
        ),
        vol.Optional(ATTR_LOCAL_OVERRIDE, default=True): cv.boolean,
        vol.Optional(ATTR_OPENWINDOW, default=True): cv.boolean,
        vol.Optional(ATTR_MULTIROOM, default=True): cv.boolean,
    }
)

SVC_SET_ZONE_MODE: Final = "set_zone_mode"
SCH_SET_ZONE_MODE = cv.make_entity_service_schema(
    # nested schemas not allowed after HA 2025.9, extra check moved to climate.py
    {
        vol.Required(ATTR_MODE): vol.In(
            [
                ZoneMode.SCHEDULE,
                ZoneMode.PERMANENT,
                ZoneMode.ADVANCED,
                ZoneMode.TEMPORARY,
            ]
        ),
        vol.Optional(ATTR_SETPOINT): vol.All(
            cv.positive_float, vol.Range(min=5, max=35)
        ),
        vol.Optional(ATTR_UNTIL): cv.datetime,
        vol.Optional(ATTR_DURATION): vol.All(
            cv.time_period,
            vol.Range(min=timedelta(minutes=5), max=timedelta(days=1)),
        ),
    }
)

SCH_SET_ZONE_MODE_EXTRA = (
    vol.Schema(  # original Entity Service action validation schema
        # vol.Msg(  # TODO turn on if good checks are working 8-2025
        vol.Any(
            {  # A
                vol.Required(ATTR_MODE): vol.In([ZoneMode.SCHEDULE]),
                # only mode with no setpoint
            },
            {  # B
                vol.Required(ATTR_MODE): vol.In(
                    [ZoneMode.PERMANENT, ZoneMode.ADVANCED]
                ),
                vol.Required(ATTR_SETPOINT): vol.All(
                    cv.positive_float, vol.Range(min=5, max=35)
                ),
            },
            {  # C
                vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
                vol.Required(ATTR_SETPOINT): vol.All(
                    cv.positive_float, vol.Range(min=5, max=35)
                ),
                vol.Required(ATTR_DURATION, default=timedelta(hours=1)): vol.All(
                    cv.time_period,
                    vol.Range(min=timedelta(minutes=5), max=timedelta(days=1)),
                ),
            },
            {  # D
                vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
                vol.Required(ATTR_SETPOINT): vol.All(
                    cv.positive_float, vol.Range(min=5, max=35)
                ),
                vol.Required(ATTR_UNTIL): cv.datetime,
            },
        ),
        #     msg="Invalid ramses_cc Zone Mode entry in Entity Service call",
        # ),
        extra=vol.PREVENT_EXTRA,
    )
)

SVC_SET_ZONE_SCHEDULE: Final = "set_zone_schedule"
SCH_SET_ZONE_SCHEDULE = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_SCHEDULE): cv.string,
    }
)

DEFAULT_NUM_ENTRIES: Final[float] = 8
MIN_NUM_ENTRIES: Final[float] = 1
MAX_NUM_ENTRIES: Final[float] = 64

SVC_GET_SYSTEM_FAULTS: Final = "get_system_faults"
SCH_GET_SYSTEM_FAULTS = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_NUM_ENTRIES, default=DEFAULT_NUM_ENTRIES): vol.All(
            cv.positive_int, vol.Range(min=MIN_NUM_ENTRIES, max=MAX_NUM_ENTRIES)
        ),
    }
)

# services without their own schema
SVC_FAKE_ZONE_TEMP: Final = "fake_zone_temp"
SVC_GET_ZONE_SCHEDULE: Final = "get_zone_schedule"
SVC_RESET_SYSTEM_MODE: Final = "reset_system_mode"
SVC_RESET_ZONE_CONFIG: Final = "reset_zone_config"
SVC_RESET_ZONE_MODE: Final = "reset_zone_mode"

SVCS_RAMSES_CLIMATE = {
    SVC_FAKE_ZONE_TEMP: SCH_PUT_ROOM_TEMP,  # a convenience for SVC_PUT_ROOM_TEMP
    SVC_SET_SYSTEM_MODE: SCH_SET_SYSTEM_MODE,
    SVC_SET_ZONE_CONFIG: SCH_SET_ZONE_CONFIG,
    SVC_SET_ZONE_MODE: SCH_SET_ZONE_MODE,
    SVC_RESET_SYSTEM_MODE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_RESET_ZONE_CONFIG: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_RESET_ZONE_MODE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_GET_ZONE_SCHEDULE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_ZONE_SCHEDULE: SCH_SET_ZONE_SCHEDULE,
    SVC_GET_SYSTEM_FAULTS: SCH_GET_SYSTEM_FAULTS,
}

# services for water_heater platform

SVC_SET_DHW_MODE: Final = "set_dhw_mode"
SCH_SET_DHW_MODE = cv.make_entity_service_schema(
    # nested schemas not allowed after HA 2025.9, extra check moved to climate.py
    {
        vol.Required(ATTR_MODE): vol.In(
            [
                ZoneMode.SCHEDULE,
                ZoneMode.PERMANENT,
                ZoneMode.ADVANCED,
                ZoneMode.TEMPORARY,
            ]
        ),
        vol.Optional(ATTR_ACTIVE): cv.boolean,
        vol.Optional(ATTR_UNTIL): cv.datetime,
        vol.Optional(ATTR_DURATION): vol.All(
            cv.time_period,
            vol.Range(min=timedelta(minutes=5), max=timedelta(days=1)),
        ),
    }
)

SCH_SET_DHW_MODE_EXTRA = vol.Schema(  # original Entity Service action validation schema
    # vol.Msg(  # TODO turn on if good checks are working 8-2025
    vol.Any(
        {  # A
            vol.Required(ATTR_MODE): vol.In([ZoneMode.SCHEDULE]),
            # only mode with no active
        },
        {
            vol.Required(ATTR_MODE): vol.In([ZoneMode.PERMANENT, ZoneMode.ADVANCED]),
            vol.Required(ATTR_ACTIVE): cv.boolean,
        },
        {  # B a.k.a DHW boost
            vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
            vol.Required(ATTR_ACTIVE): True,  # TODO: vol.Any(truthy)
            vol.Required(ATTR_DURATION, default=timedelta(hours=1)): vol.All(
                cv.time_period,
                vol.Range(min=timedelta(minutes=5), max=timedelta(days=1)),
            ),
        },
        {  # C
            vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
            vol.Required(ATTR_ACTIVE): cv.boolean,
            vol.Required(ATTR_DURATION): vol.All(
                cv.time_period,
                vol.Range(min=timedelta(minutes=5), max=timedelta(days=1)),
            ),
        },
        {  # D
            vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
            vol.Required(ATTR_ACTIVE): cv.boolean,
            vol.Required(ATTR_UNTIL): cv.datetime,
        },
    ),
    #     msg="Invalid ramses_cc Zone Mode entry in Entity Service call",
    # ),
    extra=vol.PREVENT_EXTRA,
)

DEFAULT_DHW_SETPOINT: Final[float] = 50  # degrees celsius, float
MIN_DHW_SETPOINT: Final[float] = 30
MAX_DHW_SETPOINT: Final[float] = 85

DEFAULT_OVERRUN: Final[int] = 5  # minutes, int
MIN_OVERRUN: Final[int] = 0
MAX_OVERRUN: Final[int] = 10

DEFAULT_DIFFERENTIAL: Final[float] = 10  # degrees celsius, float
MIN_DIFFERENTIAL: Final[float] = 1
MAX_DIFFERENTIAL: Final[float] = 10

SVC_SET_DHW_PARAMS: Final = "set_dhw_params"
SCH_SET_DHW_PARAMS = cv.make_entity_service_schema(
    {
        vol.Optional(ATTR_SETPOINT, default=DEFAULT_DHW_SETPOINT): vol.All(
            cv.positive_float, vol.Range(min=MIN_DHW_SETPOINT, max=MAX_DHW_SETPOINT)
        ),
        vol.Optional(ATTR_OVERRUN, default=DEFAULT_OVERRUN): vol.All(
            cv.positive_int, vol.Range(min=MIN_OVERRUN, max=MAX_OVERRUN)
        ),
        vol.Optional(ATTR_DIFFERENTIAL, default=DEFAULT_DIFFERENTIAL): vol.All(
            cv.positive_float, vol.Range(min=MIN_DIFFERENTIAL, max=MAX_DIFFERENTIAL)
        ),
    }
)

SVC_SET_DHW_SCHEDULE: Final = "set_dhw_schedule"
SCH_SET_DHW_SCHEDULE = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_SCHEDULE): cv.string,
    }
)

SVC_FAKE_DHW_TEMP: Final = "fake_dhw_temp"
SVC_GET_DHW_SCHEDULE: Final = "get_dhw_schedule"
SVC_RESET_DHW_MODE: Final = "reset_dhw_mode"
SVC_RESET_DHW_PARAMS: Final = "reset_dhw_params"
SVC_SET_DHW_BOOST: Final = "set_dhw_boost"

SVCS_RAMSES_WATER_HEATER = {
    SVC_FAKE_DHW_TEMP: SCH_PUT_DHW_TEMP,  # a convenience for SVC_PUT_DHW_TEMP
    SVC_RESET_DHW_MODE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_RESET_DHW_PARAMS: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_DHW_BOOST: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_DHW_MODE: SCH_SET_DHW_MODE,
    SVC_SET_DHW_PARAMS: SCH_SET_DHW_PARAMS,
    SVC_GET_DHW_SCHEDULE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_DHW_SCHEDULE: SCH_SET_DHW_SCHEDULE,
}

# services for remote platform

DEFAULT_TIMEOUT: Final[int] = 60
MIN_TIMEOUT: Final[int] = 30
MAX_TIMEOUT: Final[int] = 300

SVC_LEARN_COMMAND: Final = "learn_command"
SCH_LEARN_COMMAND = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Required(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(
            cv.positive_int, vol.Range(min=MIN_TIMEOUT, max=MAX_TIMEOUT)
        ),
    }
)

DEFAULT_NUM_REPEATS: Final[int] = 3
MIN_NUM_REPEATS: Final[int] = 1
MAX_NUM_REPEATS: Final[int] = 5

DEFAULT_DELAY_SECS: Final[float] = 0.05
MIN_DELAY_SECS: Final[float] = 0.02
MAX_DELAY_SECS: Final[float] = 1.0

SVC_SEND_COMMAND: Final = "send_command"
SCH_SEND_COMMAND = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Required(ATTR_NUM_REPEATS, default=3): vol.All(
            cv.positive_int,
            vol.Range(min=MIN_NUM_REPEATS, max=MAX_NUM_REPEATS),
        ),
        vol.Required(ATTR_DELAY_SECS, default=DEFAULT_DELAY_SECS): vol.All(
            cv.positive_float,
            vol.Range(min=MIN_DELAY_SECS, max=MAX_DELAY_SECS),
        ),
    },
)

SVC_DELETE_COMMAND: Final = "delete_command"
SCH_DELETE_COMMAND = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
    },
)

SVCS_RAMSES_REMOTE = {
    SVC_DELETE_COMMAND: SCH_DELETE_COMMAND,
    SVC_LEARN_COMMAND: SCH_LEARN_COMMAND,
    SVC_SEND_COMMAND: SCH_SEND_COMMAND,
}

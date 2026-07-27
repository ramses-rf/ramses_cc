#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser."""

from __future__ import annotations

from datetime import timedelta as td
from enum import IntEnum
from typing import Final

from ..ramses_tx.const import (
    DEFAULT_MAX_ZONES as DEFAULT_MAX_ZONES,
    DEV_ROLE_MAP as DEV_ROLE_MAP,
    DEV_TYPE_MAP as DEV_TYPE_MAP,
    DEVICE_ID_REGEX as DEVICE_ID_REGEX,
    DOMAIN_TYPE_MAP as DOMAIN_TYPE_MAP,
    F9 as F9,
    FA as FA,
    FAN_MODE as FAN_MODE,  # deprecated, use SZ_FAN_MODE, to be removed in Q1 2026
    FC as FC,
    FF as FF,
    I_ as I_,
    RP as RP,
    RQ as RQ,
    SYS_MODE_MAP as SYS_MODE_MAP,
    SZ_ACCEPT as SZ_ACCEPT,
    SZ_ACTIVE as SZ_ACTIVE,
    SZ_ACTUATOR_COUNTDOWN as SZ_ACTUATOR_COUNTDOWN,
    SZ_ACTUATOR_ENABLED as SZ_ACTUATOR_ENABLED,
    SZ_ACTUATORS as SZ_ACTUATORS,
    SZ_AIR_QUALITY as SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS as SZ_AIR_QUALITY_BASIS,
    SZ_BATTERY_LEVEL as SZ_BATTERY_LEVEL,
    SZ_BATTERY_LOW as SZ_BATTERY_LOW,
    SZ_BOOST_TIMER as SZ_BOOST_TIMER,
    SZ_BYPASS_MODE as SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION as SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE as SZ_BYPASS_STATE,
    SZ_CH_ACTIVE as SZ_CH_ACTIVE,
    SZ_CH_ENABLED as SZ_CH_ENABLED,
    SZ_CH_SETPOINT as SZ_CH_SETPOINT,
    SZ_CHANGE_COUNTER as SZ_CHANGE_COUNTER,
    SZ_CO2_LEVEL as SZ_CO2_LEVEL,
    SZ_CONFIRM as SZ_CONFIRM,
    SZ_COOL_ACTIVE as SZ_COOL_ACTIVE,
    SZ_CYCLE_COUNTDOWN as SZ_CYCLE_COUNTDOWN,
    SZ_DATETIME as SZ_DATETIME,
    SZ_DEVICE_ID as SZ_DEVICE_ID,
    SZ_DEVICE_ROLE as SZ_DEVICE_ROLE,
    SZ_DEVICES as SZ_DEVICES,
    SZ_DEWPOINT_TEMP as SZ_DEWPOINT_TEMP,
    SZ_DHW_ACTIVE as SZ_DHW_ACTIVE,
    SZ_DHW_FLOW_RATE as SZ_DHW_FLOW_RATE,
    SZ_DHW_IDX as SZ_DHW_IDX,
    SZ_DIFFERENTIAL as SZ_DIFFERENTIAL,
    SZ_DOMAIN_ID as SZ_DOMAIN_ID,
    SZ_DURATION as SZ_DURATION,
    SZ_EXHAUST_FAN_SPEED as SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW as SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP as SZ_EXHAUST_TEMP,
    SZ_FAN_INFO as SZ_FAN_INFO,
    SZ_FAN_MODE as SZ_FAN_MODE,
    SZ_FAN_RATE as SZ_FAN_RATE,
    SZ_FILTER_DIRTY as SZ_FILTER_DIRTY,
    SZ_FILTER_REMAINING as SZ_FILTER_REMAINING,
    SZ_FILTER_REMAINING_PERCENT as SZ_FILTER_REMAINING_PERCENT,
    SZ_FLAME_ACTIVE as SZ_FLAME_ACTIVE,
    SZ_FLAME_ON as SZ_FLAME_ON,
    SZ_FRAG_LENGTH as SZ_FRAG_LENGTH,
    SZ_FRAG_NUMBER as SZ_FRAG_NUMBER,
    SZ_FRAGMENT as SZ_FRAGMENT,
    SZ_FROST_CYCLE as SZ_FROST_CYCLE,
    SZ_HAS_FAULT as SZ_HAS_FAULT,
    SZ_ZONE_DEMAND as SZ_ZONE_DEMAND,
    SZ_INDOOR_HUMIDITY as SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP as SZ_INDOOR_TEMP,
    SZ_LANGUAGE as SZ_LANGUAGE,
    SZ_MAX_REL_MODULATION as SZ_MAX_REL_MODULATION,
    SZ_MINUTES as SZ_MINUTES,
    SZ_MODE as SZ_MODE,
    SZ_MODULATION_LEVEL as SZ_MODULATION_LEVEL,
    SZ_NAME as SZ_NAME,
    SZ_OEM_CODE as SZ_OEM_CODE,
    SZ_OFFER as SZ_OFFER,
    SZ_OUTDOOR_HUMIDITY as SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP as SZ_OUTDOOR_TEMP,
    SZ_OVERRUN as SZ_OVERRUN,
    SZ_PAYLOAD as SZ_PAYLOAD,
    SZ_PHASE as SZ_PHASE,
    SZ_POST_HEAT as SZ_POST_HEAT,
    SZ_PRE_HEAT as SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED as SZ_PRESENCE_DETECTED,
    SZ_PRESSURE as SZ_PRESSURE,
    SZ_REL_MODULATION_LEVEL as SZ_REL_MODULATION_LEVEL,
    SZ_RELAY_DEMAND as SZ_RELAY_DEMAND,
    SZ_RELAY_FAILSAFE as SZ_RELAY_FAILSAFE,
    SZ_REMAINING_DAYS as SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS as SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT as SZ_REMAINING_PERCENT,
    SZ_REQ_REASON as SZ_REQ_REASON,
    SZ_REQ_SPEED as SZ_REQ_SPEED,
    SZ_SCHEDULE as SZ_SCHEDULE,
    SZ_SENSOR as SZ_SENSOR,
    SZ_SETPOINT as SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS as SZ_SETPOINT_BOUNDS,
    SZ_SPEED_CAPABILITIES as SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED as SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW as SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP as SZ_SUPPLY_TEMP,
    SZ_SYSTEM_MODE as SZ_SYSTEM_MODE,
    SZ_TEMP_HIGH as SZ_TEMP_HIGH,
    SZ_TEMP_LOW as SZ_TEMP_LOW,
    SZ_TEMPERATURE as SZ_TEMPERATURE,
    SZ_TOTAL_FRAGS as SZ_TOTAL_FRAGS,
    SZ_UFH_IDX as SZ_UFH_IDX,
    SZ_UNKNOWN as SZ_UNKNOWN,
    SZ_UNTIL as SZ_UNTIL,
    SZ_VALUE as SZ_VALUE,
    SZ_WINDOW_OPEN as SZ_WINDOW_OPEN,
    SZ_ZONE_CLASS as SZ_ZONE_CLASS,
    SZ_ZONE_IDX as SZ_ZONE_IDX,
    SZ_ZONE_MASK as SZ_ZONE_MASK,
    SZ_ZONE_TYPE as SZ_ZONE_TYPE,
    SZ_ZONES as SZ_ZONES,
    W_ as W_,
    ZON_MODE_MAP as ZON_MODE_MAP,
    ZON_ROLE_MAP as ZON_ROLE_MAP,
    Code as Code,
    DevRole as DevRole,
    DevType as DevType,
    IndexT as IndexT,
    SystemType as SystemType,
    VerbT as VerbT,
    ZoneRole as ZoneRole,
)

__dev_mode__ = False  # NOTE: this is const.py


class Discover(IntEnum):
    """Flags for the discovery process."""

    NOTHING = 0
    SCHEMA = 1
    PARAMS = 2
    STATUS = 4
    FAULTS = 8
    SCHEDS = 16
    TRAITS = 32
    DEFAULT = 1 + 2 + 4


DONT_CREATE_MESSAGES: Final[int] = 3
DONT_CREATE_ENTITIES: Final[int] = 2
DONT_UPDATE_ENTITIES: Final[int] = 1

SCHED_REFRESH_INTERVAL: Final[int] = 3  # minutes

HIGH_VOLUME_STATUS_CODES: Final = (
    Code._0004,
    Code._1060,
    Code._2309,
    Code._2349,
    Code._30C9,
)

# Status codes for Worcester Bosch boilers - OT|OEM diagnostic code
WB_STATUS_CODES: Final[dict[str, str]] = {
    "200": "CH system is being heated.",
    "201": "DHW system is being heated.",
    "202": (
        "Anti rapid cycle mode. The boiler has commenced anti-cycle period for CH."
    ),
    "203": "System standby mode.",
    "204": "System waiting, appliance waiting for heating system to cool.",
    "208": "Appliance in service Test mode (Min/Max)",
    "265": (
        "EMS controller has forced stand-by-mode due to low heating "
        "load (power required is less than the minimum output)"
    ),
    "268": (
        "Component test mode (is running the manual component test as "
        "activated in the menus)."
    ),
    "270": "Power up mode (appliance is powering up).",
    "283": "Burner starting. The fan and the pump are being controlled.",
    "284": (
        "Gas valve(s) opened, flame must be detected within safety "
        "time. The gas valve is being controlled."
    ),
    "305": (
        "Anti fast cycle mode (DHW keep warm function). Diverter valve "
        "is held in DHW position for a period of time after DHW demand."
    ),
    "357": (
        "Appliance in air purge mode. Primary heat exchanger air "
        "venting program active - approximately 100 seconds."
    ),
    "358": (
        "Three way valve kick. If the 3-way valve hasn't moved in "
        "within 48 hours, the valve will operate once to prevent "
        "seizure"
    ),
}

# Device Availability Timeouts
HEARTBEAT_TIMEOUT_DEFAULT = td(hours=1)
HEARTBEAT_TIMEOUT_FILTER = td(hours=24)
HEARTBEAT_TIMEOUT_OTB = td(hours=24)
HEARTBEAT_TIMEOUT_TRV = td(hours=12)
HEARTBEAT_TIMEOUT_REMOTE = td(hours=24)
HEARTBEAT_TIMEOUT_SENSOR = td(hours=12)
GATEWAY_MESSAGE_TIMEOUT: td = td(minutes=10)

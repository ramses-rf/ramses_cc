#!/usr/bin/env python3
"""RAMSES RF - The modular Command package."""

from __future__ import annotations

from ..const import I_, RP, RQ, W_, Code
from .base import CommandBase
from .dhw import DhwMixins
from .hvac import HvacMixins
from .system import SystemMixins
from .zones import ZoneMixins


class Command(DhwMixins, HvacMixins, SystemMixins, ZoneMixins, CommandBase):
    """The Command class (packets to be transmitted).

    They have QoS and/or callbacks (but no RSSI).
    """


# A convenience dict
CODE_API_MAP = {
    f"{RP}|{Code._3EF1}": Command.put_actuator_cycle,
    f"{I_}|{Code._3EF0}": Command.put_actuator_state,
    f"{I_}|{Code._1FC9}": Command.put_bind,
    f"{W_}|{Code._1FC9}": Command.put_bind,
    f"{W_}|{Code._22F7}": Command.set_bypass_position,
    f"{I_}|{Code._1298}": Command.put_co2_level,
    f"{RQ}|{Code._1F41}": Command.get_dhw_mode,
    f"{W_}|{Code._1F41}": Command.set_dhw_mode,
    f"{RQ}|{Code._10A0}": Command.get_dhw_params,
    f"{W_}|{Code._10A0}": Command.set_dhw_params,
    f"{RQ}|{Code._1260}": Command.get_dhw_temp,
    f"{I_}|{Code._1260}": Command.put_dhw_temp,
    f"{I_}|{Code._22F1}": Command.set_fan_mode,
    f"{W_}|{Code._2411}": Command.set_fan_param,
    f"{RQ}|{Code._2411}": Command.get_fan_param,
    f"{I_}|{Code._12A0}": Command.put_indoor_humidity,
    f"{RQ}|{Code._1030}": Command.get_mix_valve_params,
    f"{W_}|{Code._1030}": Command.set_mix_valve_params,
    f"{RQ}|{Code._3220}": Command.get_opentherm_data,
    f"{I_}|{Code._1290}": Command.put_outdoor_temp,
    f"{I_}|{Code._2E10}": Command.put_presence_detected,
    f"{RQ}|{Code._0008}": Command.get_relay_demand,
    f"{RQ}|{Code._0404}": Command.get_schedule_fragment,
    f"{W_}|{Code._0404}": Command.set_schedule_fragment,
    f"{RQ}|{Code._0006}": Command.get_schedule_version,
    f"{I_}|{Code._30C9}": Command.put_sensor_temp,
    f"{RQ}|{Code._0100}": Command.get_system_language,
    f"{RQ}|{Code._0418}": Command.get_system_log_entry,
    f"{RQ}|{Code._2E04}": Command.get_system_mode,
    f"{W_}|{Code._2E04}": Command.set_system_mode,
    f"{RQ}|{Code._313F}": Command.get_system_time,
    f"{W_}|{Code._313F}": Command.set_system_time,
    f"{RQ}|{Code._1100}": Command.get_tpi_params,
    f"{W_}|{Code._1100}": Command.set_tpi_params,
    f"{I_}|{Code._0002}": Command.put_weather_temp,
    f"{RQ}|{Code._000A}": Command.get_zone_config,
    f"{W_}|{Code._000A}": Command.set_zone_config,
    f"{RQ}|{Code._2349}": Command.get_zone_mode,
    f"{W_}|{Code._2349}": Command.set_zone_mode,
    f"{RQ}|{Code._0004}": Command.get_zone_name,
    f"{W_}|{Code._0004}": Command.set_zone_name,
    f"{RQ}|{Code._2309}": Command.get_zone_setpoint,
    f"{W_}|{Code._2309}": Command.set_zone_setpoint,
    f"{RQ}|{Code._30C9}": Command.get_zone_temp,
    f"{RQ}|{Code._12B0}": Command.get_zone_window_state,
    f"{I_}|{Code._31DA}": Command.get_hvac_fan_31da,
}

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![ruff](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-lint.yml/badge.svg?master)
![mypy](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-type.yml/badge.svg?master)
![pytest](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-test.yml/badge.svg?master)
[![Coverage](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-cov.yml/badge.svg?event=push)](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-cov.yml)

Notice: our Wiki [Config System Schema](https://github.com/ramses-rf/ramses_cc/wiki/2.1-Configuration-step-3:-Schemas) explains the new Config Schema Discovery tool.

Requires HA Core 2026.5.0 or later. Uses config format V2 since 0.56.3.

## Overview
**ramses_cc** is a Home Assistant custom integration that works with RAMSES II-based RF 868 Mhz systems for (heating) **CH/DHW** (e.g. Honeywell Evohome) and (ventilation) **HVAC** (e.g. Itho Spider, Orcon).

> [!NOTE]
> Ramses RF can **not** interpret the new Honeywell Ramses-III (R3) messages used after a firmware upgrade since 2025 and (some) new devices.

This includes CH/DHW systems such as **evohome**, **Sundial**, **Hometronic**, **Chronotherm** and others.

The simplest way to know if it will work with your CH/DHW system is to identify the box connected to your boiler (or other heat source) to one of (there will be other systems that also work):
 - **R8810A** or **R8820A**: OpenTherm Bridge
 - **BDR91A** or **BDR91T**: Wireless Relay
 - **HC60NG**: Wireless Relay (older hardware version)

**ramses_cc** also works with HVAC (ventilation) systems using the Ramses-II protocol, such as from **Itho**, **Orcon**, **Nuaire**, **Ventiline**, **Vasco**, etc.

It uses the [ramses_rf](https://github.com/ramses-rf/ramses_rf) client library to decode the RAMSES-II protocol used by these devices. Note that other systems may also use this protocol. YMMV!

The library requires a USB-to-RF device, either a Honeywell HGI80 (rare, expensive) or a USB/MQTT dongle running [ramses_esp](https://github.com/IndaloTech/ramses_esp) or [evofw3](https://github.com/ghoti57/evofw3), such as the one from [here](https://indalo-tech.onlineweb.shop/) or your own ESP32-S3-WROOM-1 N16R8 with a CC1100 transponder.

Our [Code of Conduct](CODE_OF_CONDUCT.md) applies to all use of this repository and the code it creates.

## Actions

This integration provides the following Actions (in Developer's Tools > Actions):

| Name                                 | Description                                                                                      | Fields                              |
|--------------------------------------|--------------------------------------------------------------------------------------------------|-------------------------------------|
| Bind a Device                        | Bind a device to a CH/DHW controller or a fan/ventilation unit.                                  | device_id, offer, confirm, device_info |
| Update the System state              | Immediately update the system state                                                              |
| Get Fan Parameter                    | Request value of a parameter (2411) from a FAN                                                   | param_id, from_id |
| Get Fan Parameter via REM            | Request value of a configuration parameter (2411) from a FAN via its Remote                      | param_id |
| Set Fan Parameter                    | Set a configuration parameter (2411) on a FAN                                                    | param_id, value, name |
| Set Fan Parameter via REM            | Set a configuration parameter (2411) on a FAN via its Remote                                     | param_id, value |
| Set Fan Parameter (device)           | Set a specific configuration parameter (2411) on a FAN                                           | device | device_id, param_id, value, from_id |
| Update Fan Parameters                | Request all configuration parameters (2411) from a FAN                                           | from_id |
| Send a Command packet                | Send a completely bespoke RAMSES II command packet from the gateway                              | device_id, from_id, verb, code, payload |
| Get the Fault log of a TCS           | Obtain the controller's latest fault log                                                         | num_entries |
| Fully reset the Mode of a TCS        | The system will be in auto mode and all zones in follow_schedule mode                            |
| Set the Mode of a TCS                | The system will be in the new mode and all zones not in permanent_override mode will be affected | mode, period, duration |
| Get the Weekly schedule of a Zone    | Obtain the zone's latest weekly schedule (only evohome)                                          | |
| Fake Sensor temperature of a Zone    | Deprecated, use `fake_zone_temp` or `put_room_temp` instead                                      | |
| Reset Configuration of a Zone        | Reset the configuration of the zone                                                              | |
| Reset the Mode of a Zone             | Reset the operating mode of the zone                                                             | |
| Set the Configuration of a Zone      | Set the configuration of the zone                                                                | min_temp, max_temp |
| Set the Mode of a Zone               | Set the operating mode of the zone, indefinitely or for a duration                               | mode, setpoint, duration, until |
| Set the Weekly schedule of a Zone    | Upload the zone's weekly schedule                                                                | schedule |
| Get the Weekly schedule of a DHW     | Obtain the DHW's latest weekly schedule                                                          | |
| Reset the Mode of a DHW              | Reset the operating mode of the system's DHW                                                     | |
| Reset the Configuration of a DHW     | Reset the configuration of the system's DHW                                                      | |
| Start Boost mode for a DHW           | Enable the system's DHW for an hour.                                                             | |
| Set the Mode of a DHW                | Set the operating mode of the system's DHW                                                       | mode, active, duration, until |
| Set the Configuration of a DHW       | Set the configuration of the system's DHW                                                        | setpoint, overrun, differential |
| Set the Weekly schedule of a DHW     | Upload the DHW's weekly schedule                                                                 | schedule |
| Fake a Room temperature              | Set the current temperature (not setpoint) of an evohome zone                                    | temperature |
| Fake a DHW temperature               | Set the current temperature (not setpoint) of an evohome water heater                            | temperature |
| Announce a Room temperature          | Announce the measured room temperature of an evohome zone sensor                                 | temperature |
| Announce a DHW temperature           | Announce the measured temperature of an evohome DHW sensor                                       | temperature |
| Announce an Indoor CO2 level         | Announce the measured CO2 level of a indoor sensor                                               | co2_level |
| Announce an Indoor relative humidity | Announce the measured relative humidity of a indoor sensor                                       | indoor_humidity |
| Delete a Remote command              | Delete a RAMSES command from the database                                                        | command |
| Learn a Remote command               | Learn a RAMSES command and adds it to the database                                               | command, timeout |
| Add a Remote command                 | Add a RAMSES command to the database until restart                                               | command, packet_string |
| Send a Remote command                | Send a RAMSES command as if from a remote                                                        | command, num_repeats, delay_secs |
| Discover Known Devices               | Call all devices in the Known List, returning a report in your system log                        | device_id |

Additionally, there are Home Assistant's built-in services for climate HEAT/HVAC.

Search for "ramses" in Developer Tools > Actions in your Home Assistant instance to get the full list plus an interactive UI.

[![Open your Home Assistant instance and show your service developer tools with a specific service selected.](https://my.home-assistant.io/badges/developer_call_service.svg)](https://my.home-assistant.io/redirect/developer_call_service/?service=ramses_cc.send_command)

## More in the Wiki

See the [ramses_cc wiki](https://github.com/ramses-rf/ramses_cc/wiki) for installation, configuration, troubleshooting, etc.

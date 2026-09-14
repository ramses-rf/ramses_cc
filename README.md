[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![ruff](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-lint.yml/badge.svg?master)
![mypy](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-type.yml/badge.svg?master)
![pytest](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-test.yml/badge.svg?master)
[![Coverage](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-cov.yml/badge.svg?event=push)](https://github.com/ramses-rf/ramses_cc/actions/workflows/check-cov.yml)

* Pre-release 0.60.7
* Requires HA Core 2026.8.0 or later

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

## Prerequisites

1. Obtain a USB-to-RF transceiver — either a Honeywell HGI80 or a USB/MQTT dongle running [ramses_esp](https://github.com/IndaloTech/ramses_esp) or [evofw3](https://github.com/ghoti57/evofw3).
2. Connect the transceiver to your Home Assistant host (USB) or network (MQTT).
3. Ensure you are running HA Core 2026.8.0 or later.

## Installation

### Via HACS

1. Open HACS in Home Assistant.
2. Go to **Integrations** > **Explore & Download Repositories**.
3. Search for **ramses_cc** and install it.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/ramses_cc/` folder from this repository into your `custom_components/` directory.
2. Restart Home Assistant.

### Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**.
2. Search for **ramses_rf** (the integration name).
3. Select your transceiver port (USB or MQTT) and configure the serial/MQTT settings.
4. Complete the config flow — the integration will start a passive scan to discover devices.
5. See the [wiki](https://github.com/ramses-rf/ramses_cc/wiki) for detailed configuration of the system schema.

### Installation Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :---: | :--- |
| **Transceiver Hardware** | Hardware | Yes | Honeywell HGI80, SSM-D2 / evofw3 dongle, or Indalo Tech ramses_esp (ESP32-S3). |
| **Local USB Port** | String / Path | Optional | Path to local serial port (e.g. `/dev/serial/by-id/usb-Texas_Instruments...`, `COM3`). Baud rate `115200` (evofw3/ramses_esp) or `57600` (HGI80). |
| **Network Serial Port** | URI / URL | Optional | Remote serial socket using `rfc2217://<host>:<port>`, `socket://<host>:<port>`, or `tcp://<host>:<port>`. |
| **Home Assistant MQTT** | Service | Optional | Built-in Home Assistant MQTT integration bridge with broker topic subscription. |
| **Standalone MQTT** | URI / URL | Optional | Custom Paho MQTT broker connection string (`mqtt://[user:pass@]<host>[:port]`). |
| **Zigbee Bridge** | URI / URL | Optional | Zigbee coordinator bridge URI (`zigbee://<ieee_address>/<cluster>/<attribute>`). |

### Configuration & Options Parameters

| Option Parameter | Type | Default | Description |
| :--- | :--- | :---: | :--- |
| `scan_interval` | Integer (seconds) | `1200` | Periodic polling interval (in seconds) for supported passive parameter queries. |
| `packet_log` | File Path | None | Absolute path to a file where raw RAMSES II protocol packet logs are recorded. |
| `restore_cache` | Boolean | `True` | Restore learned device traits and schema from cache upon startup. |
| `advanced_features` | Boolean | `False` | Enable advanced entity creation (e.g. packet debug event entities). |
| `message_events` | Boolean | `False` | Dispatch RAMSES RF packet message events on Home Assistant's event bus. |
| `known_list` | Dictionary | `{}` | Explicit list of authorized device IDs and optional device class / faked traits. |
| `block_list` | Dictionary | `{}` | Explicit list of device IDs to ignore/block from discovery and packet processing. |
| `system_schema` | Dictionary | `{}` | Declarative schema defining controller (CTL), heating/cooling zones, and ventilation topology. |

## Removal

1. Go to **Settings** > **Devices & Services**.
2. Find the **ramses_rf** integration and select it.
3. Click **Delete** to remove the config entry and all its entities.
4. If installed via HACS, optionally remove it from HACS > **Integrations**.
5. If installed manually, delete the `custom_components/ramses_cc/` folder.
6. Restart Home Assistant.

## Actions

This integration provides the following Actions (in Tools > Actions).
Search for "ramses" in Developer Tools > Actions in your Home Assistant
instance to get the full list plus an interactive UI.

[![Open your Home Assistant instance and show your service developer tools with a specific service selected.](https://my.home-assistant.io/badges/developer_call_service.svg)](https://my.home-assistant.io/redirect/developer_call_service/?service=ramses_cc.send_command)

### HVAC / Ventilation

| Service                  | Description                                                                                      | Fields                              |
|--------------------------|--------------------------------------------------------------------------------------------------|-------------------------------------|
| `get_fan_param`          | Request value of a configuration parameter (2411) from a FAN (domain service)                   | device_id, param_id, from_id        |
| `get_fan_clim_param`     | Request value of a configuration parameter (2411) from a FAN (climate entity)                   | param_id, from_id                   |
| `get_fan_rem_param`      | Request value of a configuration parameter (2411) from a FAN via its Remote                      | param_id                            |
| `set_fan_param`          | Set a configuration parameter (2411) on a FAN (domain service)                                  | device_id, param_id, value, from_id |
| `set_fan_clim_param`     | Set a configuration parameter (2411) on a FAN (climate entity)                                  | param_id, value                     |
| `set_fan_rem_param`      | Set a configuration parameter (2411) on a FAN via its Remote                                     | param_id, value                     |
| `update_fan_params`      | Request all configuration parameters (2411) from a FAN                                           | device_id, from_id                  |
| `add_faked_rem`          | Create a faked REM (virtual remote) for sending commands to a FAN                                | device_id, bound_to, alias          |
| `send_command`           | Send a RAMSES command as if from a remote                                                        | command, num_repeats, delay_secs    |
| `learn_command`          | Learn a RAMSES command and add it to the database                                                | command, timeout                    |
| `add_command`            | Add a RAMSES command to the database until restart                                               | command, packet_string              |
| `delete_command`         | Delete a RAMSES command from the database                                                        | command                             |
| `send_packet`            | Send a completely bespoke RAMSES II command packet from the gateway (Enable Send Packet Config)  | device_id, from_id, verb, code, payload |

### Heat / CH / DHW

| Service                  | Description                                                                                      | Fields                              |
|--------------------------|--------------------------------------------------------------------------------------------------|-------------------------------------|
| `bind_device`            | Bind a device to a CH/DHW controller or a fan/ventilation unit                                   | device_id, offer, confirm, device_info |
| `force_update`           | Immediately update the system state                                                              |                                     |
| `get_system_faults`      | Obtain the controller's latest fault log                                                         | num_entries                         |
| `reset_system_mode`      | The system will be in auto mode and all zones in follow_schedule mode                            |                                     |
| `set_system_mode`        | The system will be in the new mode and all zones not in permanent_override mode will be affected | mode, period, duration              |
| `get_zone_schedule`      | Obtain the zone's latest weekly schedule (only evohome)                                          |                                     |
| `reset_zone_config`      | Reset the configuration of the zone                                                              |                                     |
| `reset_zone_mode`        | Reset the operating mode of the zone                                                             |                                     |
| `set_zone_config`        | Set the configuration of the zone                                                                | min_temp, max_temp                  |
| `set_zone_mode`          | Set the operating mode of the zone, indefinitely or for a duration                               | mode, setpoint, duration, until     |
| `set_zone_schedule`      | Upload the zone's weekly schedule                                                                | schedule                            |
| `get_dhw_schedule`       | Obtain the DHW's latest weekly schedule                                                          |                                     |
| `reset_dhw_mode`         | Reset the operating mode of the system's DHW                                                     |                                     |
| `reset_dhw_params`       | Reset the configuration of the system's DHW                                                      |                                     |
| `set_dhw_boost`          | Enable the system's DHW for an hour                                                              |                                     |
| `set_dhw_mode`           | Set the operating mode of the system's DHW                                                       | mode, active, duration, until       |
| `set_dhw_params`         | Set the configuration of the system's DHW                                                        | setpoint, overrun, differential     |
| `set_dhw_schedule`       | Upload the DHW's weekly schedule                                                                 | schedule                            |

### Sensors / Fake data

| Service                  | Description                                                                                      | Fields                              |
|--------------------------|--------------------------------------------------------------------------------------------------|-------------------------------------|
| `fake_zone_temp`         | Set the current temperature (not setpoint) of an evohome zone                                    | temperature                         |
| `fake_dhw_temp`          | Set the current temperature (not setpoint) of an evohome water heater                            | temperature                         |
| `put_zone_temp`          | Fake the sensor temperature of a zone (replaces deprecated `fake_sensor_temp`)                   | temperature                         |
| `put_room_temp`          | Announce the measured room temperature of an evohome zone sensor                                 | temperature                         |
| `put_dhw_temp`           | Announce the measured temperature of an evohome DHW sensor                                       | temperature                         |
| `put_co2_level`          | Announce the measured CO2 level of an indoor sensor                                              | co2_level                           |
| `put_ventilation_demand` | Send a transient 0-100% demand from a faked CO2 sensor to its bound Orcon ventilation unit       | ventilation_demand                  |
| `put_indoor_humidity`    | Announce the measured relative humidity of an indoor sensor                                      | indoor_humidity                     |

### Device management / Discovery

| Service                       | Description                                                                   | Fields                         |
|-------------------------------|-------------------------------------------------------------------------------|--------------------------------|
| `discover_known_devices`      | Call all devices in the Known List, returning a report in your system log     | device_id                      |
| `sync_topology`               | Immediately sync the learned topology to the config entry                     |                                |
| `get_discovered_devices`      | Retrieve the list of devices discovered by the passive scan                   | status, enabled                |
| `accept_discovered_device`    | Accept a discovered device and add it to the schema                           | device_id, owner, schema_entry |
| `discard_discovered_device`   | Discard a discovered device. It stays in the list for spam prevention         | device_id                      |
| `remove_discovered_device`    | Mark a previously accepted device as removed                                  | device_id                      |
| `enable_discovered_device`    | Enable a disabled or discarded device without changing its status             | device_id                      |
| `disable_discovered_device`   | Disable an accepted device temporarily (e.g. for maintenance)                 | device_id                      |
| `remove_device`               | Remove a device from the schema and HA device registry e.g. when replaced     | device_id                      |
| `set_polling_interval`        | Configure or reset the effective polling interval for a RAMSES device         | device_id, polling_interval    |

Additionally, there are Home Assistant's built-in services for climate HEAT/HVAC.

## More in the Wiki

See the [ramses_cc wiki](https://github.com/ramses-rf/ramses_cc/wiki) for installation, configuration, troubleshooting, etc.
The Wiki [Config System Schema](https://github.com/ramses-rf/ramses_cc/wiki/2.1-Configuration-step-3:-Schemas) page explains the Passive Device Scan tool and Migrating from an earlier version.

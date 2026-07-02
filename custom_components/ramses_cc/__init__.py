# ruff: noqa: E402
# WE DISABLE E402 (Module level import not at top of file) BECAUSE:
# The "Development Hook" logic below must modify `sys.path` BEFORE any other
# imports run. This ensures that if a local development version of `ramses_rf`
# exists, Python loads it instead of the system-installed version.
"""Support for Honeywell's RAMSES-II RF protocol, as used by CH/DHW & HVAC.

Requires a Honeywell HGI80 (or compatible) gateway.
"""

from __future__ import annotations

import logging
import os
import sys
from types import ModuleType
from typing import Any

# from collections.abc import Callable
#
# from homeassistant.components.event import EventEntity

# --- DEVELOPMENT HOOK ---
# If a local copy of ramses_rf exists, use it instead of the system installed version.
# This allows for testing changes without rebuilding the container.

ENABLE_DEV_HOOK = False  # Set to true to enable the dev hook
DEV_LIB_PATH = "/config/deps/ramses_rf/src"

if ENABLE_DEV_HOOK and os.path.isdir(DEV_LIB_PATH):  # pragma: no cover
    # Insert at index 0 so it takes precedence over system libraries
    sys.path.insert(0, DEV_LIB_PATH)

    logging.getLogger(__name__).warning(
        "SECURITY WARNING: 'ramses_rf' is being loaded from a local development path: %s. "
        "Do not use this in a production environment unless you understand the risks.",
        DEV_LIB_PATH,
    )
# ------------------------

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant import config_entries
from homeassistant.components.climate.const import DOMAIN as CLIMATE_ENTITY_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_ENTITY_DOMAIN
from homeassistant.components.remote import DOMAIN as REMOTE_ENTITY_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_ENTITY_DOMAIN
from homeassistant.components.water_heater.const import (
    DOMAIN as WATERHEATER_ENTITY_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.service import verify_domain_control
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ADVANCED_FEATURES,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_PASSIVE_SCAN,
    CONF_SEND_PACKET,
    DOMAIN,
    SVC_ACCEPT_DISCOVERED_DEVICE,
    SVC_ADD_FAKED_REM,
    SVC_DISABLE_DISCOVERED_DEVICE,
    SVC_DISCARD_DISCOVERED_DEVICE,
    SVC_DISCOVER_KNOWN_DEVICES,
    SVC_ENABLE_DISCOVERED_DEVICE,
    SVC_GET_DISCOVERED_DEVICES,
    SVC_REMOVE_DISCOVERED_DEVICE,
    SZ_PORT_NAME,
    SZ_SERIAL_PORT,
)
from .coordinator import RamsesCoordinator
from .schemas import (
    SCH_ACCEPT_DISCOVERED_DEVICE,
    SCH_ADD_FAKED_REM,
    SCH_BIND_DEVICE,
    SCH_DISABLE_DISCOVERED_DEVICE,
    SCH_DISCARD_DISCOVERED_DEVICE,
    SCH_DISCOVER_KNOWN_DEVICES,
    SCH_DOMAIN_CONFIG,
    SCH_ENABLE_DISCOVERED_DEVICE,
    SCH_GET_DISCOVERED_DEVICES,
    SCH_GET_FAN_PARAM_DOMAIN,
    SCH_NO_SVC_PARAMS,
    SCH_REMOVE_DISCOVERED_DEVICE,
    SCH_SEND_PACKET,
    SCH_SET_FAN_PARAM_DOMAIN,
    SCH_UPDATE_FAN_PARAMS_DOMAIN,
    SVC_BIND_DEVICE,
    SVC_FORCE_UPDATE,
    SVC_GET_FAN_PARAM,
    SVC_SEND_PACKET,
    SVC_SET_FAN_PARAM,
    SVC_UPDATE_FAN_PARAMS,
    SVCS_RAMSES_CLIMATE,
    SVCS_RAMSES_NUMBER,
    SVCS_RAMSES_REMOTE,
    SVCS_RAMSES_SENSOR,
    SVCS_RAMSES_WATER_HEATER,
)

_LOGGER = logging.getLogger(__name__)

_RAMSES_TX_EXC: ModuleType | None = None


def _get_ramses_tx_exceptions() -> ModuleType:
    """Import ramses_tx.exceptions lazily to avoid circular import issues."""

    global _RAMSES_TX_EXC
    if _RAMSES_TX_EXC is None:
        from ramses_tx import exceptions as exc_module

        _RAMSES_TX_EXC = exc_module
    return _RAMSES_TX_EXC


CONFIG_SCHEMA = vol.All(
    cv.deprecated(DOMAIN, raise_if_present=False),
    vol.Schema({DOMAIN: SCH_DOMAIN_CONFIG}, extra=vol.ALLOW_EXTRA),
)

PLATFORMS = [Platform.EVENT]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Ramses integration."""

    hass.data[DOMAIN] = {}

    # If required, do a one-off import of entry from config yaml
    if DOMAIN in config and not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )

    # register all platform services during async_setup, since 2025.10, see
    # https://developers.home-assistant.io/blog/2025/09/25/entity-services-api-changes
    for entity_domain, services in (
        (CLIMATE_ENTITY_DOMAIN, SVCS_RAMSES_CLIMATE),
        (REMOTE_ENTITY_DOMAIN, SVCS_RAMSES_REMOTE),
        (SENSOR_ENTITY_DOMAIN, SVCS_RAMSES_SENSOR),
        (WATERHEATER_ENTITY_DOMAIN, SVCS_RAMSES_WATER_HEATER),
        (NUMBER_ENTITY_DOMAIN, SVCS_RAMSES_NUMBER),
    ):
        for key, schema in services.items():
            _LOGGER.debug(
                "Registering %s entity service %s with schema %s",
                entity_domain,
                key,
                schema,
            )
            service.async_register_platform_entity_service(
                hass,
                DOMAIN,
                key,
                entity_domain=entity_domain,
                schema=schema,
                func=f"async_{key}",
            )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create a ramses_rf (RAMSES_II)-based system."""

    _LOGGER.debug("Setting up entry %s...", entry.entry_id)

    tx_exc = _get_ramses_tx_exceptions()

    # Check if this entry is already set up
    if entry.entry_id in hass.data[DOMAIN]:
        _LOGGER.debug("Entry %s is already set up", entry.entry_id)
        return True

    healed_options = _healed_serial_port_options(
        {**entry.options},
        mqtt_entries_present=bool(hass.config_entries.async_entries("mqtt")),
    )
    if healed_options is not None:
        hass.config_entries.async_update_entry(entry, options=healed_options)
        _LOGGER.warning(
            "Healed missing serial_port for entry %s by defaulting to mqtt_ha. "
            "Please verify transport settings in the options flow.",
            entry.entry_id,
        )

    coordinator = RamsesCoordinator(hass, entry)

    try:
        # Store the coordinator in hass.data before setting it up
        hass.data[DOMAIN][entry.entry_id] = coordinator
        await coordinator.async_setup()
    except tx_exc.TransportSourceInvalid as err:  # not TransportSerialError
        _LOGGER.error("Unrecoverable problem with the serial port: %s", err)
        hass.data[DOMAIN].pop(entry.entry_id, None)  # Clean up if setup fails
        return False
    except tx_exc.TransportError as err:
        msg = f"There is a problem with the serial port: {err} (check config)"
        _LOGGER.warning(
            "Failed to set up entry %s (will retry): %s", entry.entry_id, msg
        )
        hass.data[DOMAIN].pop(entry.entry_id, None)  # Clean up if setup fails
        raise ConfigEntryNotReady(msg) from err
    except Exception as err:
        _LOGGER.error(
            "Unexpected error during setup of entry %s: %s",
            entry.entry_id,
            err,
            exc_info=True,
        )
        hass.data[DOMAIN].pop(entry.entry_id, None)  # Clean up if setup fails
        raise ConfigEntryNotReady(f"Setup failed: {err}") from err

    # Start the coordinator after successful setup
    await coordinator.async_start()

    _LOGGER.debug("Registering domain services and events")
    async_register_domain_services(hass, entry, coordinator)  # for Services
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)  # for Events
    _LOGGER.debug("Finished registering domain services and events")

    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    _LOGGER.debug("Successfully set up entry %s", entry.entry_id)

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy configuration options to the current version 2.

    This handles the transition away from user-selectable database storage
    and removes deprecated packet log keys (e.g., `file_name`) that cause
    strict schema validation to fail during setup.

    :param hass: The Home Assistant instance.
    :param entry: The ConfigEntry to migrate.
    :return: True if the migration succeeded.
    """
    _LOGGER.debug("Migrating ramses_cc config entry from version %s", entry.version)

    if entry.version == 1:
        # Create a deep copy of the immutable MappingProxyType to mutate it
        new_options = {**entry.options}

        # 1. Clean up packet_log dictionary
        if isinstance(new_options.get("packet_log"), dict):
            packet_log = {**new_options["packet_log"]}
            # Remove deprecated key mentioned in issue #592
            packet_log.pop("file_name", None)
            # Translate legacy rotate_backups to modern key
            if "rotate_backups" in packet_log:
                packet_log["packet_log_retention_days"] = packet_log.pop(
                    "rotate_backups"
                )

            new_options["packet_log"] = packet_log

        # 2. Clean up ramses_rf dictionary (legacy database storage flags)
        if isinstance(new_options.get("ramses_rf"), dict):
            ramses_rf = {**new_options["ramses_rf"]}
            # Remove deprecated database keys
            for deprecated_key in ["use_database", "database_file", "file_name"]:
                ramses_rf.pop(deprecated_key, None)
            new_options["ramses_rf"] = ramses_rf

        # Update the entry with the cleaned options and bump version
        hass.config_entries.async_update_entry(entry, options=new_options, version=2)
        _LOGGER.info(
            "Successfully migrated ramses_cc config entry %s to version 2",
            entry.entry_id,
        )

    return True


def _healed_serial_port_options(
    options: dict[str, Any], *, mqtt_entries_present: bool
) -> dict[str, Any] | None:
    """Return healed options if serial_port is missing and MQTT is implied."""

    serial_port = options.get(SZ_SERIAL_PORT)
    serial_port_missing = not isinstance(serial_port, dict) or not serial_port.get(
        SZ_PORT_NAME
    )

    mqtt_hints_present = bool(options.get(CONF_MQTT_USE_HA)) or any(
        key in options for key in (CONF_MQTT_HGI_ID, CONF_MQTT_TOPIC)
    )

    if not serial_port_missing or not (mqtt_hints_present or mqtt_entries_present):
        return None

    new_options = {**options}
    new_options[SZ_SERIAL_PORT] = {SZ_PORT_NAME: "mqtt_ha"}
    new_options.setdefault(CONF_MQTT_USE_HA, True)
    return new_options


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Config entry %s updated, reloading integration...", entry.entry_id)

    # Just reload the entry, which will handle unloading and setting up again
    # instead of fire and forget with async_create_task
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not await coordinator.async_unload_platforms():
        return False

    # Only remove domain-level services registered in async_register_domain_services.
    # Entity platform services (registered once in async_setup) must NOT be removed
    # here because async_setup is not called again on reload, which would cause
    # "Action ramses_cc.<service> not found" errors after every reload.
    _domain_services = {
        SVC_BIND_DEVICE,
        SVC_FORCE_UPDATE,
        SVC_SEND_PACKET,
        SVC_SET_FAN_PARAM,
        SVC_GET_FAN_PARAM,
        SVC_UPDATE_FAN_PARAMS,
        SVC_DISCOVER_KNOWN_DEVICES,
    }
    for svc in _domain_services:
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)  # for Events

    hass.data[DOMAIN].pop(entry.entry_id)
    return True


@callback
def async_register_domain_services(
    hass: HomeAssistant, entry: ConfigEntry, _coordinator: RamsesCoordinator
) -> None:
    """Set up and register handlers for the domain-wide services."""

    @verify_domain_control(DOMAIN)
    async def async_bind_device(call: ServiceCall) -> None:
        await _coordinator.async_bind_device(call)

    @verify_domain_control(DOMAIN)
    async def async_force_update(call: ServiceCall) -> None:
        await _coordinator.async_force_update(call)

    @verify_domain_control(DOMAIN)
    async def async_send_packet(call: ServiceCall) -> None:
        await _coordinator.async_send_packet(call)

    @verify_domain_control(DOMAIN)
    async def async_discover_known_devices(call: ServiceCall) -> None:
        await _coordinator.async_discover_known_devices(call)

    @verify_domain_control(DOMAIN)
    async def async_get_discovered_devices(call: ServiceCall) -> None:
        await _coordinator.async_get_discovered_devices(call)

    @verify_domain_control(DOMAIN)
    async def async_accept_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_accept_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_discard_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_discard_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_remove_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_remove_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_enable_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_enable_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_disable_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_disable_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_add_faked_rem(call: ServiceCall) -> None:
        await _coordinator.async_add_faked_rem(call)

    @verify_domain_control(DOMAIN)
    async def async_set_fan_param(call: ServiceCall) -> None:
        await _coordinator.async_set_fan_param(call)

    @verify_domain_control(DOMAIN)
    async def async_get_fan_param(call: ServiceCall) -> None:
        await _coordinator.async_get_fan_param(call)

    @verify_domain_control(DOMAIN)
    async def async_update_fan_params(call: ServiceCall) -> None:
        await _coordinator._async_run_fan_param_sequence(call)

    # register the handlers
    hass.services.async_register(
        DOMAIN, SVC_BIND_DEVICE, async_bind_device, schema=SCH_BIND_DEVICE
    )

    hass.services.async_register(
        DOMAIN, SVC_FORCE_UPDATE, async_force_update, schema=SCH_NO_SVC_PARAMS
    )

    hass.services.async_register(
        DOMAIN,
        SVC_DISCOVER_KNOWN_DEVICES,
        async_discover_known_devices,
        schema=SCH_DISCOVER_KNOWN_DEVICES,
    )

    # Passive device scan services (only if scan is enabled)
    if entry.options.get(CONF_ADVANCED_FEATURES, {}).get(CONF_PASSIVE_SCAN):
        hass.services.async_register(
            DOMAIN,
            SVC_GET_DISCOVERED_DEVICES,
            async_get_discovered_devices,
            schema=SCH_GET_DISCOVERED_DEVICES,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_ACCEPT_DISCOVERED_DEVICE,
            async_accept_discovered_device,
            schema=SCH_ACCEPT_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_DISCARD_DISCOVERED_DEVICE,
            async_discard_discovered_device,
            schema=SCH_DISCARD_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_REMOVE_DISCOVERED_DEVICE,
            async_remove_discovered_device,
            schema=SCH_REMOVE_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_ENABLE_DISCOVERED_DEVICE,
            async_enable_discovered_device,
            schema=SCH_ENABLE_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_DISABLE_DISCOVERED_DEVICE,
            async_disable_discovered_device,
            schema=SCH_DISABLE_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_ADD_FAKED_REM,
            async_add_faked_rem,
            schema=SCH_ADD_FAKED_REM,
        )

    hass.services.async_register(
        DOMAIN, SVC_SET_FAN_PARAM, async_set_fan_param, schema=SCH_SET_FAN_PARAM_DOMAIN
    )
    hass.services.async_register(
        DOMAIN, SVC_GET_FAN_PARAM, async_get_fan_param, schema=SCH_GET_FAN_PARAM_DOMAIN
    )
    hass.services.async_register(
        DOMAIN,
        SVC_UPDATE_FAN_PARAMS,
        async_update_fan_params,
        schema=SCH_UPDATE_FAN_PARAMS_DOMAIN,
    )

    # Advanced features
    if entry.options.get(CONF_ADVANCED_FEATURES, {}).get(CONF_SEND_PACKET):
        hass.services.async_register(
            DOMAIN, SVC_SEND_PACKET, async_send_packet, schema=SCH_SEND_PACKET
        )

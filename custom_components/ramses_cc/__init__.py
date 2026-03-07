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
import re
import sys

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

from typing import TYPE_CHECKING, Any

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant import config_entries
from homeassistant.components.climate import DOMAIN as CLIMATE_ENTITY_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_ENTITY_DOMAIN
from homeassistant.components.remote import DOMAIN as REMOTE_ENTITY_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_ENTITY_DOMAIN
from homeassistant.components.water_heater import DOMAIN as WATERHEATER_ENTITY_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import verify_domain_control
from homeassistant.helpers.typing import ConfigType

from ramses_tx import exceptions as exc

from .const import (
    CONF_ADVANCED_FEATURES,
    CONF_MESSAGE_EVENTS,
    CONF_SEND_PACKET,
    DOMAIN,
    SIGNAL_UPDATE,
)
from .coordinator import RamsesCoordinator
from .schemas import (
    SCH_BIND_DEVICE,
    SCH_DOMAIN_CONFIG,
    SCH_GET_FAN_PARAM_DOMAIN,
    SCH_NO_SVC_PARAMS,
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

if TYPE_CHECKING:
    from ramses_tx.message import Message


_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.All(
    cv.deprecated(DOMAIN, raise_if_present=False),
    vol.Schema({DOMAIN: SCH_DOMAIN_CONFIG}, extra=vol.ALLOW_EXTRA),
)


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

    # Check if this entry is already set up
    if entry.entry_id in hass.data[DOMAIN]:
        _LOGGER.debug("Entry %s is already set up", entry.entry_id)
        return True

    coordinator = RamsesCoordinator(hass, entry)

    try:
        # Store the coordinator in hass.data before setting it up
        hass.data[DOMAIN][entry.entry_id] = coordinator
        await coordinator.async_setup()
    except exc.TransportSourceInvalid as err:  # not TransportSerialError
        _LOGGER.error("Unrecoverable problem with the serial port: %s", err)
        hass.data[DOMAIN].pop(entry.entry_id, None)  # Clean up if setup fails
        return False
    except exc.TransportError as err:
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
    async_register_domain_services(hass, entry, coordinator)
    async_register_domain_events(hass, entry, coordinator)
    _LOGGER.debug("Finished registering domain services and events")

    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    _LOGGER.debug("Successfully set up entry %s", entry.entry_id)

    return True


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
    }
    for svc in _domain_services:
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)

    hass.data[DOMAIN].pop(entry.entry_id)

    return True


@callback  # TODO: the following is a mess - to add register/deregister of clients
def async_register_domain_events(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: RamsesCoordinator
) -> None:
    """Set up the handlers for the system-wide events."""

    features: dict[str, Any] = entry.options.get(CONF_ADVANCED_FEATURES, {})
    if message_events := features.get(CONF_MESSAGE_EVENTS):
        message_events_regex = re.compile(message_events)
    else:
        message_events_regex = None

    @callback
    def async_process_msg(msg: Message, *args: Any, **kwargs: Any) -> None:
        """Process a message from the event bus as pass it on."""

        async_dispatcher_send(hass, f"{SIGNAL_UPDATE}_{msg.src.id}")
        if msg.dst and msg.dst.id != msg.src.id:
            async_dispatcher_send(hass, f"{SIGNAL_UPDATE}_{msg.dst.id}")

        if message_events_regex and message_events_regex.search(f"{msg!r}"):
            event_data = {
                "dtm": msg.dtm.isoformat(),
                "src": msg.src.id,
                "dst": msg.dst.id,
                "verb": msg.verb,
                "code": msg.code,
                "payload": msg.payload,
                "packet": str(msg._pkt),
            }
            hass.bus.async_fire(f"{DOMAIN}_message", event_data)

        if coordinator.learn_device_id and coordinator.learn_device_id == msg.src.id:
            event_data = {
                "src": msg.src.id,
                "code": msg.code,
                "packet": str(msg._pkt),
            }
            hass.bus.async_fire(f"{DOMAIN}_learn", event_data)

    if coordinator.client:
        coordinator.client.add_msg_handler(async_process_msg)


@callback
def async_register_domain_services(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: RamsesCoordinator
) -> None:
    """Set up the handlers for the domain-wide services."""

    @verify_domain_control(DOMAIN)  # TODO: is a work in progress
    async def async_bind_device(call: ServiceCall) -> None:
        await coordinator.async_bind_device(call)

    @verify_domain_control(DOMAIN)
    async def async_force_update(call: ServiceCall) -> None:
        await coordinator.async_force_update(call)

    @verify_domain_control(DOMAIN)
    async def async_send_packet(call: ServiceCall) -> None:
        await coordinator.async_send_packet(call)

    @verify_domain_control(DOMAIN)
    async def async_set_fan_param(call: ServiceCall) -> None:
        await coordinator.async_set_fan_param(call)

    @verify_domain_control(DOMAIN)
    async def async_get_fan_param(call: ServiceCall) -> None:
        await coordinator.async_get_fan_param(call)

    @verify_domain_control(DOMAIN)
    async def async_update_fan_params(call: ServiceCall) -> None:
        await coordinator._async_run_fan_param_sequence(call)

    hass.services.async_register(
        DOMAIN, SVC_BIND_DEVICE, async_bind_device, schema=SCH_BIND_DEVICE
    )
    hass.services.async_register(
        DOMAIN, SVC_FORCE_UPDATE, async_force_update, schema=SCH_NO_SVC_PARAMS
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

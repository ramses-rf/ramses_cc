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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant import config_entries
from homeassistant.components.climate import DOMAIN as CLIMATE_ENTITY_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_ENTITY_DOMAIN
from homeassistant.components.remote import DOMAIN as REMOTE_ENTITY_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_ENTITY_DOMAIN
from homeassistant.components.water_heater import DOMAIN as WATERHEATER_ENTITY_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.service import verify_domain_control
from homeassistant.helpers.typing import ConfigType

from ramses_rf.entity_base import Entity as RamsesRFEntity
from ramses_tx import exceptions as exc

from .broker import RamsesBroker
from .const import (
    CONF_ADVANCED_FEATURES,
    CONF_MESSAGE_EVENTS,
    CONF_SEND_PACKET,
    DOMAIN,
    SIGNAL_UPDATE,
)
from .schemas import (
    SCH_BIND_DEVICE,
    SCH_DOMAIN_CONFIG,
    SCH_NO_SVC_PARAMS,
    SCH_SEND_PACKET,
    SCH_SET_FAN_PARAM_DOMAIN,
    SVC_BIND_DEVICE,
    SVC_FORCE_UPDATE,
    SVC_SEND_PACKET,
    SVC_SET_FAN_PARAM,
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
# seems not being used ...
PLATFORMS: Final[tuple[Platform, ...]] = (
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.REMOTE,
    Platform.SENSOR,
    Platform.WATER_HEATER,
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

    broker = RamsesBroker(hass, entry)

    try:
        # Store the broker in hass.data before setting it up
        hass.data[DOMAIN][entry.entry_id] = broker
        await broker.async_setup()
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

    # Start the broker after successful setup
    await broker.async_start()

    _LOGGER.debug("Registering domain services and events")
    async_register_domain_services(hass, entry, broker)
    async_register_domain_events(hass, entry, broker)
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
    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    if not await broker.async_unload_platforms():
        return False

    for svc in hass.services.async_services_for_domain(DOMAIN):
        hass.services.async_remove(DOMAIN, svc)

    hass.data[DOMAIN].pop(entry.entry_id)

    return True


@callback  # TODO: the following is a mess - to add register/deregister of clients
def async_register_domain_events(
    hass: HomeAssistant, entry: ConfigEntry, broker: RamsesBroker
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

        if broker.learn_device_id and broker.learn_device_id == msg.src.id:
            event_data = {
                "src": msg.src.id,
                "code": msg.code,
                "packet": str(msg._pkt),
            }
            hass.bus.async_fire(f"{DOMAIN}_learn", event_data)

    broker.client.add_msg_handler(async_process_msg)


@callback
def async_register_domain_services(
    hass: HomeAssistant, entry: ConfigEntry, broker: RamsesBroker
) -> None:
    """Set up the handlers for the domain-wide services."""

    @verify_domain_control(DOMAIN)  # TODO: is a work in progress
    async def async_bind_device(call: ServiceCall) -> None:
        await broker.async_bind_device(call)

    @verify_domain_control(DOMAIN)
    async def async_force_update(call: ServiceCall) -> None:
        await broker.async_force_update(call)

    @verify_domain_control(DOMAIN)
    async def async_send_packet(call: ServiceCall) -> None:
        await broker.async_send_packet(call)

    @verify_domain_control(DOMAIN)
    async def async_set_fan_param(call: ServiceCall) -> None:
        await broker.async_set_fan_param(call)

    # @verify_domain_control(DOMAIN)
    # async def async_get_fan_param(call: ServiceCall) -> None:
    #     await broker.async_get_fan_param(call)
    #
    # @verify_domain_control(DOMAIN)
    # async def async_set_fan_param(call: ServiceCall) -> None:
    #     await broker.async_set_fan_param(call)
    #
    # @verify_domain_control(DOMAIN)
    # async def async_update_fan_params(call: ServiceCall) -> None:
    #     await broker._async_run_fan_param_sequence(call)

    hass.services.async_register(
        DOMAIN, SVC_BIND_DEVICE, async_bind_device, schema=SCH_BIND_DEVICE
    )
    hass.services.async_register(
        DOMAIN, SVC_FORCE_UPDATE, async_force_update, schema=SCH_NO_SVC_PARAMS
    )

    hass.services.async_register(
        DOMAIN, SVC_SET_FAN_PARAM, async_set_fan_param, schema=SCH_SET_FAN_PARAM_DOMAIN
    )

    # general access fan_param services for code
    # hass.services.async_register(
    #     DOMAIN, SVC_GET_FAN_PARAM, async_get_fan_param, schema=SCH_GET_FAN_PARAM_DOMAIN
    # )
    # hass.services.async_register(
    #     DOMAIN, SVC_SET_FAN_PARAM, async_set_fan_param, schema=SCH_SET_FAN_PARAM_DOMAIN
    # )
    # hass.services.async_register(
    #     DOMAIN,
    #     SVC_UPDATE_FAN_PARAMS,
    #     async_update_fan_params,
    #     schema=SCH_UPDATE_FAN_PARAMS_DOMAIN,
    # )

    # Advanced features
    if entry.options.get(CONF_ADVANCED_FEATURES, {}).get(CONF_SEND_PACKET):
        hass.services.async_register(
            DOMAIN, SVC_SEND_PACKET, async_send_packet, schema=SCH_SEND_PACKET
        )


class RamsesEntity(Entity):
    """Base for any RAMSES II-compatible entity (e.g. Climate, Sensor)."""

    _broker: RamsesBroker
    _device: RamsesRFEntity

    _attr_should_poll = False

    entity_description: RamsesEntityDescription

    def __init__(
        self,
        broker: RamsesBroker,
        device: RamsesRFEntity,
        entity_description: RamsesEntityDescription,
    ) -> None:
        """Initialize the entity."""
        self.hass = broker.hass
        self._broker = broker
        self._device = device
        self.entity_description = entity_description

        self._attr_unique_id = device.id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.id)})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        attrs = {
            ATTR_ID: self._device.id,
        }
        if self.entity_description.ramses_cc_extra_attributes:
            attrs |= {
                k: getattr(self._device, v)
                for k, v in self.entity_description.ramses_cc_extra_attributes.items()
                if hasattr(self._device, v)
            }
        return attrs

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        self._broker._entities[self.unique_id] = self

        # Listen for general update signal (for backward compatibility)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE, self.async_write_ha_state
            )
        )

        # Also listen for device-specific update signal
        device_signal = f"{SIGNAL_UPDATE}_{self._device.id}"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, device_signal, self.async_write_ha_state
            )
        )

    @callback
    def async_write_ha_state_delayed(self, delay: int = 3) -> None:
        """Write to the state machine after a short delay to allow system to quiesce."""

        # NOTE: this doesn't work (below), as call_later injects `_now: dt`
        #     async_call_later(self.hass, delay, self.async_write_ha_state)
        # but only self is expected:
        #     def async_write_ha_state(self) -> None:

        self.hass.loop.call_later(delay, self.async_write_ha_state)  # pragma: no cover


@dataclass(frozen=True, kw_only=True)
class RamsesEntityDescription(EntityDescription):
    """Class describing Ramses entities."""

    has_entity_name: bool = True

    # integration-specific attributes
    ramses_cc_extra_attributes: dict[str, str] | None = None  # TODO: may not be None?

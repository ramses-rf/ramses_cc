"""Fan Logic Handler for RAMSES integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ramses_rf.device import Device
from ramses_rf.device.hvac import HvacRemoteBase, HvacVentilator
from ramses_rf.entity_base import Entity as RamsesRFEntity
from ramses_tx.const import DevType
from ramses_tx.schemas import DeviceIdT

from .const import DOMAIN, SIGNAL_NEW_DEVICES, SZ_BOUND_TO, SZ_KNOWN_LIST

if TYPE_CHECKING:
    from . import RamsesEntity
    from .broker import RamsesBroker

_LOGGER = logging.getLogger(__name__)


class RamsesFanHandler:
    """Handler for FAN (HVAC) specific logic, bindings, and parameters."""

    def __init__(self, broker: RamsesBroker) -> None:
        """Initialize the Fan Handler."""
        self._broker = broker
        self.hass = broker.hass
        self._fan_bound_to_remote: dict[str, DeviceIdT] = {}

    def find_param_entity(self, device_id: str, param_id: str) -> RamsesEntity | None:
        """Find a parameter entity by device ID and parameter ID.

        Helper Method that searches for a number entity corresponding to a specific
        parameter on a device.
        """
        # Normalize device ID to use underscores and lowercase for entity ID
        safe_device_id = str(device_id).replace(":", "_").lower()
        target_entity_id = f"number.{safe_device_id}_param_{param_id.lower()}"

        # First try to find the entity in the entity registry
        ent_reg = er.async_get(self.hass)
        entity_entry = ent_reg.async_get(target_entity_id)
        if entity_entry:
            _LOGGER.debug("Found entity %s in entity registry", target_entity_id)
            # Get the actual entity from the platform to make sure entity is fully loaded
            platforms = self._broker.platforms.get(Platform.NUMBER, [])
            for platform in platforms:
                if (
                    hasattr(platform, "entities")
                    and target_entity_id in platform.entities
                ):
                    return platform.entities[target_entity_id]

            # Entity exists in registry but not yet loaded in platform
            return None

        _LOGGER.debug("Entity %s not found in registry.", target_entity_id)
        return None

    def create_parameter_entities(self, device: RamsesRFEntity) -> None:
        """Create parameter entities for a device that supports 2411 parameters."""
        device_id = device.id
        from .number import create_parameter_entities

        entities = create_parameter_entities(self._broker, device)
        _LOGGER.debug(
            "create_parameter_entities returned %d entities for %s",
            len(entities),
            device_id,
        )
        if entities:
            _LOGGER.info(
                "Adding %d parameter entities for %s", len(entities), device_id
            )
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_DEVICES.format(Platform.NUMBER),
                entities,
            )
        else:
            _LOGGER.debug("No parameter entities created for %s", device_id)

    async def setup_fan_bound_devices(self, device: Device) -> None:
        """Set up bound devices for a FAN device."""
        # Only proceed if this is a FAN device
        if not isinstance(device, HvacVentilator):
            return

        # Get device configuration from known_list
        device_config = self._broker.options.get(SZ_KNOWN_LIST, {}).get(device.id, {})

        # Use .get() and handle None/Empty immediately
        bound_device_id = device_config.get(SZ_BOUND_TO)
        if not bound_device_id:
            return

        # Explicit type check for safety
        if not isinstance(bound_device_id, str):
            _LOGGER.warning(
                "Cannot bind device %s to FAN %s: invalid bound device id type (%s)",
                bound_device_id,
                device.id,
                type(bound_device_id),
            )
            return

        _LOGGER.info("Binding FAN %s and REM/DIS device %s", device.id, bound_device_id)

        # Find the bound device and get its type
        bound_device = next(
            (d for d in self._broker.client.devices if d.id == bound_device_id),
            None,
        )

        if bound_device:
            # Determine the device type based on the class
            if isinstance(bound_device, HvacRemoteBase):
                device_type = DevType.REM
            elif hasattr(bound_device, "_SLUG") and bound_device._SLUG == DevType.DIS:
                device_type = DevType.DIS
            else:
                _LOGGER.warning(
                    "Cannot bind device %s of type %s to FAN %s: must be REM or DIS",
                    bound_device_id,
                    getattr(bound_device, "_SLUG", "unknown"),
                    device.id,
                )
                return

            # Add the bound device to the FAN's tracking
            device.add_bound_device(bound_device_id, device_type)
            _LOGGER.info(
                "Bound FAN %s to %s device %s",
                device.id,
                device_type,
                bound_device_id,
            )
            # add the HvacVentilator device id to the broker's dict
            self._fan_bound_to_remote[str(bound_device_id)] = device.id
        else:
            _LOGGER.warning(
                "Bound device %s not found for FAN %s", bound_device_id, device.id
            )

    async def async_setup_fan_device(self, device: Device) -> None:
        """Set up a FAN device and its parameter entities."""
        _LOGGER.debug("Setting up device: %s", device.id)

        # For FAN devices, set up bound devices and parameter handling
        if hasattr(device, "_SLUG") and device._SLUG == "FAN":
            await self.setup_fan_bound_devices(device)

            # Set up the initialization callback - will be called on first message
            if hasattr(device, "set_initialized_callback"):

                async def on_fan_first_message() -> None:
                    """Handle the first message received from a FAN device."""
                    _LOGGER.debug(
                        "First message received from FAN %s, creating parameter entities",
                        device.id,
                    )
                    # Create parameter entities after first message is received
                    self.create_parameter_entities(device)
                    # Request all parameters after creating entities (non-blocking if fails)
                    _call: dict[str, DeviceIdT] = {
                        "device_id": device.id,
                    }
                    try:
                        self._broker.get_all_fan_params(_call)
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to request parameters for device %s during startup: %s. "
                            "Entities will still work for received parameter updates.",
                            device.id,
                            err,
                        )

                device.set_initialized_callback(
                    lambda: self.hass.async_create_task(on_fan_first_message())
                )

            # Set up parameter update callback
            if hasattr(device, "set_param_update_callback"):
                # Create a closure to capture the current device_id
                def create_param_callback(dev_id: str) -> Callable[[str, Any], None]:
                    def param_callback(param_id: str, value: Any) -> None:
                        _LOGGER.debug(
                            "Parameter %s updated for device %s: %s (firing event)",
                            param_id,
                            dev_id,
                            value,
                        )
                        # Fire the event for Home Assistant entities
                        self.hass.bus.async_fire(
                            f"{DOMAIN}.fan_param_updated",
                            {"device_id": dev_id, "param_id": param_id, "value": value},
                        )

                    return param_callback

                device.set_param_update_callback(create_param_callback(device.id))
                _LOGGER.debug(
                    "Set up parameter update callback for device %s", device.id
                )

            # Check if device is already initialized (e.g., from cached messages)
            if hasattr(device, "supports_2411") and device.supports_2411:
                if getattr(device, "_initialized", False):
                    _LOGGER.debug(
                        "Device %s already initialized, creating parameter entities and requesting parameters",
                        device.id,
                    )
                    self.create_parameter_entities(device)
                    async_dispatcher_send(
                        self.hass,
                        SIGNAL_NEW_DEVICES.format(Platform.NUMBER),
                        [device],
                    )
                call: dict[str, Any] = {
                    "device_id": device.id,
                }
                try:
                    self._broker.get_all_fan_params(call)
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to request parameters for device %s during setup: %s. "
                        "Entities will still work for received parameter updates.",
                        device.id,
                        err,
                    )

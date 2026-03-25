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
from ramses_tx.typing import DeviceIdT

from .const import DOMAIN, SIGNAL_NEW_DEVICES, SZ_BOUND_TO, SZ_KNOWN_LIST
from .helpers import resolve_async_attr

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator
    from .entity import RamsesEntity

_LOGGER = logging.getLogger(__name__)


class RamsesFanHandler:
    """Handler for FAN (HVAC) specific logic, bindings, and parameters."""

    def __init__(self, coordinator: RamsesCoordinator) -> None:
        """Initialize the Fan Handler."""
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self._fan_bound_to_remote: dict[str, DeviceIdT] = {}

    def find_param_entity(self, device_id: str, param_id: str) -> RamsesEntity | None:
        """Find a parameter entity by device ID and parameter ID.

        Helper Method that searches for a number entity corresponding to a specific
        parameter on a device.

        :param device_id: The ID of the device (e.g., '30:123456').
        :param param_id: The 2-character hex ID of the parameter.
        :return: The found entity or None if not found in the registry/platform.
        """
        safe_device_id = str(device_id).replace(":", "_").lower()
        unique_id = f"{safe_device_id}_param_{param_id.lower()}"
        ent_reg = er.async_get(self.hass)
        entity_id = ent_reg.async_get_entity_id("number", DOMAIN, unique_id)
        if entity_id is None:
            _LOGGER.debug("Entity (unique_id=%s) not found in registry.", unique_id)
            return None

        _LOGGER.debug("Found entity %s in entity registry", entity_id)

        platforms = self.coordinator.platforms.get(Platform.NUMBER, [])
        for platform in platforms:
            if hasattr(platform, "entities") and entity_id in platform.entities:
                return platform.entities[entity_id]

        return None

    def create_parameter_entities(self, device: RamsesRFEntity) -> None:
        """Create parameter entities for a device that supports 2411 parameters.

        Delegates to the number platform to create entities and signals the
        platform to add them.

        :param device: The ramses_rf device instance to create parameters for.
        """
        device_id = device.id
        from .number import create_parameter_entities

        entities = create_parameter_entities(self.coordinator, device)
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
        """Set up bound devices for a FAN device.

        Checks the known_list configuration for devices bound to this FAN
        (REMotes or DIStribution units) and registers the binding in the
        underlying library.

        :param device: The FAN device instance to configure bindings for.
        """
        # Only proceed if this is a FAN device
        if not isinstance(device, HvacVentilator):
            return

        # Get device configuration from known_list
        device_config = self.coordinator.options.get(SZ_KNOWN_LIST, {}).get(
            device.id, {}
        )

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

        if not self.coordinator.client:
            _LOGGER.warning("Cannot look up bound device: Client not ready")
            return

        bound_device = self.coordinator._get_device(bound_device_id)

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
            # add the HvacVentilator device id to the coordinator's dict
            self._fan_bound_to_remote[str(bound_device_id)] = device.id
        else:
            _LOGGER.warning(
                "Bound device %s not found for FAN %s", bound_device_id, device.id
            )

    async def async_setup_fan_device(self, device: Device) -> None:
        """Set up a FAN device and its parameter entities.

        Configures bindings, sets up initialization callbacks for parameter
        discovery, and establishes parameter update callbacks for event firing.

        :param device: The device instance to set up.
        """
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
                        self.coordinator.get_all_fan_params(_call)
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
            if (
                hasattr(device, "supports_2411")
                and device.supports_2411
                and resolve_async_attr(self, device, "_initialized", False)
            ):
                _LOGGER.debug(
                    "Device %s already initialized, creating parameter entities",
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
                self.coordinator.get_all_fan_params(call)
            except Exception as err:
                _LOGGER.warning(
                    "Failed to request parameters for device %s during setup: %s. "
                    "Entities will still work for received parameter updates.",
                    device.id,
                    err,
                )

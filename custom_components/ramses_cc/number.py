"""Support for RAMSES RF number entities.

.. note::
    Currently only used for fan parameters but can be extended for other NUMBER entities.

.. rubric:: Module Functions

.. py:function:: normalize_device_id(device_id: str) -> str
    :module: number

    Normalize a device ID for use in entity IDs by replacing colons with underscores
    and converting to lowercase.

.. py:function:: async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None
    :module: number

    Set up the RAMSES number platform from a config entry. This function is called by
    Home Assistant when the integration is being set up. It registers the service calls
    and sets up the device discovery callback.

.. py:function:: get_param_descriptions(device: RamsesRFEntity) -> list[RamsesNumberEntityDescription]
    :module: number

    Get parameter descriptions for a device. Returns a list of entity descriptions
    for all parameters supported by the device.

.. py:function:: async_create_parameter_entities(broker: RamsesBroker, device: RamsesRFEntity) -> list[RamsesNumberParam]
    :module: number

    Create parameter entities for a device. This function creates number entities for
    each parameter supported by the device. The caller is responsible for registering
    the platform using async_add_entities.

.. rubric:: Class Structure

.. code-block:: text

    RamsesNumberBase (RamsesEntity, NumberEntity)
    ├── RamsesNumberParam (RamsesNumberBase)
    └── RamsesNumberEntityDescription (RamsesEntityDescription, NumberEntityDescription)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import UnionType
from typing import Any

from homeassistant.components.number import (
    ENTITY_ID_FORMAT,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)

from ramses_rf.entity_base import Entity as RamsesRFEntity
from ramses_tx import (
    _2411_PARAMS_SCHEMA,
    SZ_DATA_TYPE,
    SZ_DATA_UNIT,
    SZ_DESCRIPTION,
    SZ_MAX_VALUE,
    SZ_MIN_VALUE,
    SZ_PRECISION,
)

from . import RamsesEntity, RamsesEntityDescription
from .broker import RamsesBroker
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def normalize_device_id(device_id: str) -> str:
    """Normalize a device ID for use in entity IDs.

    Replaces colons with underscores and converts to lowercase to ensure consistency.

    :param device_id: The device ID to normalize
    :type device_id: str
    :return: The normalized device ID
    :rtype: str
    """
    return str(device_id).replace(":", "_").lower()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the RAMSES number platform from a config entry.

    This function is called by Home Assistant when the integration is being set up.
    It registers the service calls and sets up the device discovery callback.

    :param hass: The Home Assistant instance
    :type hass: ~homeassistant.core.HomeAssistant
    :param entry: The config entry used to set up the platform
    :type entry: ~homeassistant.config_entries.ConfigEntry
    :param async_add_entities: Async function to add entities to the platform
    :type async_add_entities: ~homeassistant.helpers.entity_platform.AddEntitiesCallback
    :return: None
    :rtype: None
    """

    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    _LOGGER.debug("Setting up platform")

    @callback
    async def add_devices(devices: list[RamsesRFEntity | RamsesNumberParam]) -> None:
        """Add number entities for the given devices or entities.

        This callback coordinates the creation of all number entity types. It can handle
        both direct entity addition and device-based entity creation.

        :param devices: List of devices or entities to process
        :type devices: list[RamsesRFEntity | RamsesNumberParam]
        :return: None
        :rtype: None
        """
        _LOGGER.debug("Processing %d items", len(devices))
        if not devices:
            return

        # If we received entities directly (not devices), just add them
        if all(isinstance(d, RamsesNumberParam) for d in devices):
            _LOGGER.debug("Adding %d entities directly", len(devices))
            async_add_entities(devices)
            return

        # Otherwise, process as devices and create entities
        entities: list[RamsesNumberBase] = []

        for device in devices:
            if not isinstance(device, RamsesRFEntity):
                _LOGGER.debug("Skipping non-device item: %s", device)
                continue

            # Always try to create parameter entities, even if they exist
            # The async_create_parameter_entities function will handle duplicates
            if param_entities := await async_create_parameter_entities(broker, device):
                entities.extend(param_entities)

            # Future: Add other entity types here
            # if other_entities := await async_create_other_entities(broker, devices):
            #     entities.extend(other_entities)

        if entities:
            _LOGGER.debug(
                "Adding %d parameter entities to Home Assistant", len(entities)
            )
            # Log entity details for debugging
            for entity in entities:
                _LOGGER.debug(
                    "Adding entity: %s (unique_id: %s, device: %s)",
                    entity.entity_id,
                    entity.unique_id,
                    entity._device.id if hasattr(entity, "_device") else "no device",
                )
            async_add_entities(entities, update_before_add=True)

            # After adding entities, request their current values
            for entity in entities:
                if hasattr(entity, "_request_parameter_value"):
                    await entity._request_parameter_value()

    # Register the callback with the broker
    broker.async_register_platform(platform, add_devices)  # looks circular
    # TODO(wi): mypy Expected type '(Entity) -> None',
    # TODO got '(devices: list[Entity | RamsesNumberParam]) -> Coroutine[Any, Any, None]' instead

    # Load any existing devices that were discovered before platform registration
    if hasattr(broker, "devices") and broker.devices:
        _LOGGER.debug("Processing %d existing devices", len(broker.devices))
        # Filter only devices that support parameters
        fan_devices = [
            d for d in broker.devices if hasattr(d, "supports_2411") and d.supports_2411
        ]
        if fan_devices:
            _LOGGER.debug("Found %d FAN devices to process", len(fan_devices))
            # Load entities from registry for existing devices
            for device in fan_devices:
                if hasattr(device, "supports_2411") and device.supports_2411:
                    # Check if we've already created parameter entities for this device in this session
                    device_id = normalize_device_id(device.id)
                    if (
                        hasattr(broker, "_parameter_entities_created")
                        and device_id in broker._parameter_entities_created
                    ):
                        _LOGGER.debug(
                            "Parameter entities already created for %s in current session, skipping",
                            device.id,
                        )
                        continue

                    _LOGGER.debug(
                        "Loading parameter entities from registry for %s", device.id
                    )
                    # Force creation of entities that exist in registry but not in platform
                    await async_create_parameter_entities(broker, device)
            await add_devices(fan_devices)


# TODO(wi): fix sphinx error in this class:
# AttributeError: type object 'RamsesNumberBase' has no attribute '_RamsesNumberBase__attr_entity_category'
# skipping in sphinx_apidoc because of errors. BTW: same error as class RamsesWaterHeater in water_heater.py
class RamsesNumberBase(RamsesEntity, NumberEntity):
    """Base class for all RAMSES number entities.

    This abstract base class provides common functionality for all RAMSES number entities,
    including state management and pending state handling. Specific number entity types
    should inherit from this class and implement the required methods.

    :cvar entity_description: The entity description for this entity
    :vartype entity_description: RamsesNumberEntityDescription
    :cvar _attr_should_poll: Whether the entity should be polled (default: False)
    :vartype _attr_should_poll: bool
    :cvar _attr_entity_category: The category of the entity (default: CONFIG)
    :vartype _attr_entity_category: str
    """

    # TODO(wi): fix sphinx error in this class:
    # AttributeError: type object 'RamsesNumberBase' has no attribute '_RamsesNumberBase__attr_entity_category'

    entity_description: RamsesNumberEntityDescription
    _attr_should_poll = (
        False  # Disable polling by default, can be overridden by subclasses
    )
    _attr_entity_category: EntityCategory = EntityCategory.CONFIG
    _is_pending: bool = False
    _pending_value: float | None = None

    def set_pending(self, value: float | None = None) -> None:
        """Set the entity to a pending state with an optional value.

        This method updates the internal pending state and optionally stores a pending value.
        It also triggers an immediate UI update to reflect the pending state.

        :param value: The pending value to set, or None to just set the pending state
        :type value: float | None, optional
        :return: None
        :rtype: None
        """
        self._is_pending = True
        self._pending_value = value
        self.async_write_ha_state()

    def clear_pending(self) -> None:
        """Clear the pending state and any pending value.

        This method resets the internal pending state and clears any stored pending value.
        It also triggers an immediate UI update to reflect the cleared state.

        :return: None
        :rtype: None
        """
        self._is_pending = False
        self._pending_value = None
        self.async_write_ha_state()

    async def _clear_pending_after_timeout(self, timeout: int) -> None:
        """Clear pending state after timeout if still pending.

        :param timeout: Timeout in seconds
        :type timeout: int
        :return: None
        :rtype: None
        """
        try:
            await asyncio.sleep(timeout)
            if self._is_pending:
                _LOGGER.debug(
                    "No response received after %s seconds, clearing pending state",
                    timeout,
                )
                self.clear_pending()
        except Exception as ex:
            _LOGGER.debug("Error in pending clear task: %s", ex, exc_info=True)

    def __init__(
        self,
        broker: RamsesBroker,
        device: RamsesRFEntity,
        entity_description: RamsesNumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(broker, device, entity_description)
        self._is_percentage = getattr(self.entity_description, "percentage", False)

    def _scale_for_storage(self, value: float | None) -> float | None:
        """Scale a value for storage based on the entity's configuration.

        This method converts a display value to its storage representation.
        For percentage values, it converts from 0-100% range to 0.0-1.0 range.

        :param value: The display value to scale (e.g., 100% -> 1.0)
        :type value: float | None
        :return: The scaled value for storage, or None if input is None
        :rtype: float | None
        """
        if value is None:
            return None
        return value / 100 if self._is_percentage else value

    def _scale_for_display(self, value: Any) -> float | None:
        """Convert and scale a stored value for display based on the entity's configuration.

        This method converts a stored value to its display representation.
        For percentage values, it converts from 0.0-1.0 range to 0-100% range.

        :param value: The stored value to scale for display (e.g., 0.5 -> 50.0%)
        :type value: Any
        :return: The scaled display value, or None if value cannot be converted to float
        :rtype: float | None
        """
        if value is None:
            param_id = getattr(self.entity_description, "ramses_rf_attr", "unknown")
            _LOGGER.debug("No value available yet for parameter %s", param_id)
            return None

        try:
            float_value = float(value)
            # Base class only handles basic percentage scaling
            return round(float_value * 100.0, 1) if self._is_percentage else float_value
        except (TypeError, ValueError) as err:
            _LOGGER.debug(
                "Could not convert value '%s' to float: %s",
                value,
                str(err),
            )
            return None

    def _validate_value_range(self, value: float | None) -> tuple[bool, str | None]:
        """Validate that a value is within the allowed range for this entity.

        This method checks if the provided value is within the minimum and maximum
        bounds defined for this entity. It's used to ensure values are valid before
        they are set on the device.

        :param value: The value to validate
        :type value: float | None
        :return: A tuple containing:
                 - bool: True if the value is valid, False otherwise
                 - str | None: Error message if validation fails, None if valid
        :rtype: tuple[bool, str | None]
        """
        if value is None:
            return False, "Value is required"

        min_val = getattr(self, "native_min_value", None)
        max_val = getattr(self, "native_max_value", None)

        if min_val is not None and value < min_val:
            return False, f"Value {value} is below minimum {min_val}"
        if max_val is not None and value > max_val:
            return False, f"Value {value} is above maximum {max_val}"

        return True, None

    def _validate_and_scale_value(self, value: float) -> tuple[bool, str | None, float]:
        """Validate and scale a value for the entity in a single operation.

        This method combines range validation and value scaling into one step.
        It's used when setting a new value to ensure it's both valid and properly
        scaled for the target device.

        :param value: The value to validate and scale
        :type value: float
        :return: A tuple containing:
                 - bool: True if the value is valid, False otherwise
                 - str | None: Error message if validation fails, None if valid
                 - float: The scaled value (only valid if first item is True)
        :rtype: tuple[bool, str | None, float]
        """
        is_valid, error_msg = self._validate_value_range(value)
        if not is_valid:
            return False, error_msg, 0.0

        scaled_value = self._scale_for_storage(value)
        return True, None, scaled_value if scaled_value is not None else 0.0


class RamsesNumberParam(RamsesNumberBase):
    """Class for RAMSES parameter number entities.

    This class is specifically designed for handling 2411 fan parameters.

    :ivar _param_native_value: Dictionary to store parameter values by parameter ID.
    :type _param_native_value: dict[str, float | None]
    :ivar _is_pending: Boolean indicating if there's a pending value update.
    :type _is_pending: bool
    :ivar _pending_value: The pending value to be set.
    :type _pending_value: float | None

    .. note::
        - Special use for 2411 fan parameters
        - The entities are listed under device as Configuration
        - There is no active polling by HA
        - Updates are received via events
        - A pending state mechanism is implemented since we don't wait for a response on RQ
    """

    _param_native_value: dict[str, float | None] = {}

    @property
    def mode(self) -> str:
        """Return the input mode of the entity.

        This property determines the UI input mode for the number entity.

        :return: The input mode, either 'slider' for temperature parameters
                or 'auto' for other parameter types.
        :rtype: str
        """
        if (
            hasattr(self.entity_description, "ramses_rf_attr")
            and self.entity_description.ramses_rf_attr == "75"
        ):
            return "slider"
        return "auto"

    @property
    def _normalized_param_id(self) -> str | None:
        """Get the normalized parameter ID from entity description.

        This property retrieves and normalizes the parameter ID from the entity's
        description, converting it to uppercase for consistency.

        :return: The normalized parameter ID in uppercase, or None if not available
        :rtype: str | None
        """
        param_id = getattr(self.entity_description, "ramses_rf_attr", None)
        return str(param_id).upper() if param_id else None

    async def async_added_to_hass(self) -> None:
        """Run when entity is about to be added to Home Assistant.

        This method is called when the entity is being added to Home Assistant.
        It performs the following operations:

        1. Calls the parent class's async_added_to_hass method
        2. Sets up an event listener for parameter updates
        3. Requests the initial parameter value from the device

        :return: None
        :rtype: None
        """
        await super().async_added_to_hass()

        # Listen for parameter update events
        self.async_on_remove(
            self.hass.bus.async_listen(
                "ramses_cc.fan_param_updated", self._async_param_updated
            )
        )

        # Request initial value
        await self._request_parameter_value()

    @callback
    def _async_param_updated(self, event: dict[str, Any]) -> None:
        """Handle parameter updates from the device.

        This callback is triggered when a fan parameter update event is received.
        It processes the update and updates the entity's state if the parameter
        matches this entity's parameter ID.

        :param event: The event data containing the parameter update
        :type event: dict[str, Any]
        :return: None
        :rtype: None
        """
        # Get the parameter ID we're interested in
        our_param_id = getattr(self.entity_description, "ramses_rf_attr", "")
        if not our_param_id:
            return

        # Extract data from event
        event_data = event.data if hasattr(event, "data") else event

        # Only process if this is our parameter
        if (
            str(event_data.get("device_id", "")).lower() == str(self._device.id).lower()
            and str(event_data.get("param_id", "")).lower() == str(our_param_id).lower()
        ):
            new_value = event_data.get("value")

            param_id = str(our_param_id).upper()
            self._param_native_value[param_id] = new_value
            _LOGGER.debug(
                "Parameter %s updated for device %s: %s (stored as: %s, full dict: %s)",
                our_param_id,
                self._device.id,
                new_value,
                self._param_native_value.get(param_id),
                self._param_native_value,
            )

            self.clear_pending()

    def __init__(
        self,
        broker: RamsesBroker,
        device: RamsesRFEntity,
        entity_description: RamsesEntityDescription,
    ) -> None:
        """Initialize the RAMSES number parameter entity.

        This constructor sets up the entity with the provided broker, device, and
        entity description. It also initializes the parameter value storage and
        configures the entity based on the parameter type.

        :param broker: The RAMSES broker instance for device communication
        :type broker: RamsesBroker
        :param device: The device this entity is associated with
        :type device: RamsesRFEntity
        :param entity_description: The entity description containing parameter metadata
        :type entity_description: RamsesEntityDescription
        :return: None
        :rtype: None
        """
        super().__init__(broker, device, entity_description)

        # Initialize parameter storage
        self._param_native_value = {}  # Dictionary to store parameter values
        self._attr_native_value = None
        self._pending_update = False
        self._pending_timer = None
        self._param_id = self.entity_description.key.replace("param_", "").upper()
        self._attr_unique_id = f"{self._device.id}_{self.entity_description.key}"

        # Initialize with None for this parameter
        self._param_native_value[self._param_id] = None

        # Clear any existing value from the store if needed
        if hasattr(self._device, "clear_fan_param"):
            self._device.clear_fan_param(self._param_id)

        # Get the normalized device ID
        device_id = normalize_device_id(device.id)
        param_id = getattr(entity_description, "ramses_rf_attr", "").lower()

        # Create base ID with device ID and parameter ID
        base_id = f"{device_id}_param_{param_id}"
        self.entity_id = ENTITY_ID_FORMAT.format(base_id)
        self._attr_unique_id = base_id

        _LOGGER.debug(
            "Found entity_id: %s, unique_id: %s", self.entity_id, self._attr_unique_id
        )

        param_id = getattr(entity_description, "ramses_rf_attr", "")
        if param_id:
            self._param_native_value[param_id.upper()] = None
        self._is_pending = False
        self._pending_value = None

        # Special case for parameters that are already in percentage - don't scale them
        # Parameter 95 (Boost mode) is a percentage but is handled as 0-1 in the device
        self._is_percentage = (
            hasattr(entity_description, "unit_of_measurement")
            and entity_description.unit_of_measurement == "%"
            and param_id not in ("52",)  # Don't scale parameter 52
        )

        # Set min/max/step values from entity description if available
        if (
            hasattr(entity_description, "min_value")
            and entity_description.min_value is not None
        ):
            min_val = float(entity_description.min_value)
            # For parameter 95 (Boost mode), display as percentage but keep internal range 0-1
            if param_id == "95":
                self._attr_native_min_value = min_val * 100  # Show 0-100% in UI
            elif self._is_percentage:
                self._attr_native_min_value = min_val * 100  # Scale other percentages
            else:
                self._attr_native_min_value = min_val

        if (
            hasattr(entity_description, "max_value")
            and entity_description.max_value is not None
        ):
            max_val = float(entity_description.max_value)
            # For parameter 95 (Boost mode), display as percentage but keep internal range 0-1
            if param_id == "95":
                self._attr_native_max_value = max_val * 100  # Show 0-100% in UI
            elif self._is_percentage:
                self._attr_native_max_value = max_val * 100  # Scale other percentages
            else:
                self._attr_native_max_value = max_val

        # Special handling for temperature parameters (param 75) - force 0.1°C precision
        if param_id == "75":
            self._attr_native_step = 0.1
        elif (
            hasattr(entity_description, "precision")
            and entity_description.precision is not None
        ):
            precision = float(entity_description.precision)
            self._attr_native_step = precision * (100 if self._is_percentage else 1)

        # Set unit of measurement if available
        if (
            hasattr(entity_description, "unit_of_measurement")
            and entity_description.unit_of_measurement
        ):
            self._attr_native_unit_of_measurement = (
                entity_description.unit_of_measurement
            )

        _LOGGER.debug(
            "Initialized number entity %s with min=%s, max=%s, step=%s, unit=%s, is_percentage=%s, param_id=%s",
            self.entity_id,
            getattr(self, "_attr_native_min_value", "unset"),
            getattr(self, "_attr_native_max_value", "unset"),
            getattr(self, "_attr_native_step", "unset"),
            getattr(self, "_attr_native_unit_of_measurement", "unset"),
            self._is_percentage,
            param_id,
        )

    @property
    def available(self) -> bool:
        """Determine if the entity is available.

        An entity is considered available if we have received a valid value
        for its parameter from the device.

        :return: True if the entity has a valid value, False otherwise
        :rtype: bool
        """
        if not self._normalized_param_id:
            return False

        value = self._param_native_value.get(self._normalized_param_id)
        return value is not None

    async def _request_parameter_value(self) -> None:
        """Request the current value of this parameter from the device.

        This method initiates a request to the device to get the current value
        of the parameter associated with this entity. It handles the pending state.

        :return: None
        :rtype: None
        """
        if (
            not hasattr(self, "hass")
            or not hasattr(self, "_device")
            or not hasattr(self.entity_description, "ramses_rf_attr")
        ):
            _LOGGER.debug("_request_parameter_value: missing required attributes")
            return

        if not self._device:
            _LOGGER.debug("No device available to request parameter %s", self._param_id)
            return

        # Get the parameter ID from the entity description
        param_id = self.entity_description.ramses_rf_attr
        if not param_id:
            _LOGGER.debug("_request_parameter_value: missing parameter ID")
            return

        # This just checks the store, doesn't send RQ
        value = self._device.get_fan_param(param_id)

        _LOGGER.debug(
            "Got value %s for parameter %s from device %s store",
            value,
            param_id,
            self._device.id,
        )

        # Ensure the parameter exists in our dictionary
        if param_id not in self._param_native_value:
            self._param_native_value[param_id] = None

        if value is not None:
            self._param_native_value[param_id] = value
            self._attr_native_value = value
            self.async_write_ha_state()
            self.clear_pending()
        else:
            _LOGGER.debug("No value available for parameter %s", param_id)
            self.async_write_ha_state()

        _LOGGER.debug("Requesting parameter %s from %s", param_id, self._device.id)

        self.set_pending()

        self._device.get_fan_param(param_id)

        self.hass.async_create_task(self._clear_pending_after_timeout(30))

    def _is_boost_mode_param(self) -> bool:
        """Check if this is a boost mode parameter (ID 95).

        :return: True if this is a boost mode parameter, False otherwise
        :rtype: bool
        """
        return getattr(self.entity_description, "ramses_rf_attr", "") == "95"

    @property
    def native_value(self) -> float | None:
        """Return the current value of the entity.

        This property returns the current value of the parameter, scaled appropriately
        for display in the UI. If no value is available, it returns None.

        :return: The current value of the parameter, or None if no value is available
        :rtype: float | None
        """
        if not hasattr(self, "_normalized_param_id") or not self._normalized_param_id:
            param_id = getattr(self.entity_description, "ramses_rf_attr", "unknown")
            _LOGGER.error("Cannot get value: missing parameter ID for %s", param_id)
            return None

        value = self._param_native_value.get(self._normalized_param_id)

        # For boost mode (param 95), scale from 0-1 to 0-100%
        if value is not None and self._is_boost_mode_param():
            return round(float(value) * 100.0, 1)
        return self._scale_for_display(value)

    async def async_set_native_value(self, value: float) -> None:
        """Set a new value for the parameter.

        This method validates the new value, scales it appropriately for the device,
        and sends the update command. It also handles the pending state and error
        conditions.

        :param value: The new value to set for the parameter
        :type value: float
        :raises HomeAssistantError: If the value is invalid or the parameter cannot be set
        :return: None
        :rtype: None
        """
        if not self._normalized_param_id:
            param_id = getattr(self.entity_description, "ramses_rf_attr", "unknown")
            _LOGGER.error("Cannot set value: missing parameter ID for %s", param_id)
            return

        try:
            # For boost mode (param 95), send the raw value (0-100) without scaling
            if self._is_boost_mode_param():
                display_value = round(float(value), 1)
                self.set_pending(display_value)
                await self.hass.services.async_call(
                    DOMAIN,
                    "set_fan_param",
                    {
                        "device_id": self._device.id,
                        "param_id": self._normalized_param_id,
                        "value": float(value),
                    },
                )
                return

            # For non-boost mode parameters
            is_valid, error_msg, scaled_value = self._validate_and_scale_value(value)
            if not is_valid:
                _LOGGER.error(
                    "%s: %s",
                    getattr(self, "unique_id", "unknown"),
                    error_msg or "Invalid value",
                )
                return

            display_value = float(value)

            # Set pending state with the display value
            self.set_pending(display_value)

            await self.hass.services.async_call(
                DOMAIN,
                "set_fan_param",
                {
                    "device_id": self._device.id,
                    "param_id": self._normalized_param_id,
                    "value": float(value),
                },
            )
        except Exception as ex:
            _LOGGER.error(
                "%s: Error setting parameter %s to %s: %s",
                self.unique_id,
                self._normalized_param_id,
                value,
                ex,
                exc_info=True,
            )
            raise

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend.

        :return: The icon string or None if no specific icon is defined.
        :rtype: str | None
        """
        # Show loading icon when update is in progress
        if self._is_pending:
            return "mdi:timer-sand"

        # First check if there's a specific icon defined in the entity description
        if (
            hasattr(self.entity_description, "ramses_cc_icon_off")
            and not self.native_value
        ):
            return self.entity_description.ramses_cc_icon_off

        param_id = getattr(self.entity_description, "ramses_rf_attr", "")
        unit = getattr(self, "_attr_native_unit_of_measurement", "")

        # Select icon based on parameter ID and unit
        if unit == "°C":
            return "mdi:thermometer"
        elif unit == "%" and param_id == "52":  # Sensor sensitivity
            return "mdi:gauge"
        elif unit == "%":
            return "mdi:percent"
        elif unit == "min":
            return "mdi:timer"
        elif param_id == "54":  # Moisture sensor overrun time
            return "mdi:water-percent"
        elif param_id == "95":  # Boost mode fan rate
            return "mdi:fan-speed-3"

        # Default icon if no specific match found
        return "mdi:counter"


@dataclass(frozen=True, kw_only=True)
class RamsesNumberEntityDescription(RamsesEntityDescription, NumberEntityDescription):
    """Description for RAMSES number entities.

    This class extends Home Assistant's NumberEntityDescription with RAMSES-specific
    attributes needed for number entities.

    :cvar ramses_cc_class: The RAMSES number entity class to use.
    :vartype ramses_cc_class: type[RamsesNumberBase]
    :cvar ramses_cc_icon_off: Optional icon to use when the entity is off.
    :vartype ramses_cc_icon_off: str | None
    :cvar ramses_rf_attr: The RAMSES RF attribute this entity represents.
    :vartype ramses_rf_attr: str
    :cvar ramses_rf_class: The RAMSES RF entity class this description applies to.
    :vartype ramses_rf_class: type[RamsesRFEntity] | UnionType
    :cvar check_attr: Optional attribute to check for entity availability.
    :vartype check_attr: str | None
    :cvar data_type: The data type of the number (e.g., 'float', 'int').
    :vartype data_type: str | None
    :cvar precision: The precision of the number value.
    :vartype precision: float | None
    :cvar parameter_id: The parameter ID for 2411 parameters.
    :vartype parameter_id: str | None
    :cvar parameter_desc: Description of the parameter.
    :vartype parameter_desc: str | None
    :cvar unit_of_measurement: The unit of measurement for the number.
    :vartype unit_of_measurement: str | None
    :cvar mode: The input mode ('auto', 'box', 'slider').
    :vartype mode: str
    """

    # integration-specific attributes
    ramses_cc_class: type[RamsesNumberBase] = RamsesNumberParam
    ramses_cc_icon_off: str | None = None  # no NumberEntityDescription.icon_off attr
    ramses_rf_attr: str = ""
    ramses_rf_class: type[RamsesRFEntity] | UnionType = RamsesRFEntity

    # Parameters for 2411 parameter entities
    check_attr: str | None = None
    data_type: str | None = None
    precision: float | None = None
    parameter_id: str | None = None
    parameter_desc: str | None = None
    unit_of_measurement: str | None = None
    mode: str = "auto"


def get_param_descriptions(
    device: RamsesRFEntity,
) -> list[RamsesNumberEntityDescription]:
    """Get parameter descriptions for a device.

    :param device: The device to get parameter descriptions for
    :type device: RamsesRFEntity
    :return: List of RamsesNumberEntityDescription objects for the device's parameters
    :rtype: list[RamsesNumberEntityDescription]
    """
    if not hasattr(device, "supports_2411") or not device.supports_2411:
        return []

    descriptions: list[RamsesNumberEntityDescription] = []

    for param_id, param_info in _2411_PARAMS_SCHEMA.items():
        # Determine precision and mode based on parameter type
        precision = float(param_info.get(SZ_PRECISION, 1.0))
        mode = "auto"
        if param_id == "75":  # Comfort temperature parameter
            precision = 0.1
            mode = "slider"

        desc = RamsesNumberEntityDescription(
            key=f"param_{param_id}",
            name=param_info.get(SZ_DESCRIPTION, f"Parameter {param_id}"),
            ramses_rf_attr=param_id,
            parameter_id=param_id,
            parameter_desc=param_info.get(SZ_DESCRIPTION, ""),
            min_value=param_info.get(SZ_MIN_VALUE, 0),
            max_value=param_info.get(SZ_MAX_VALUE, 255),
            precision=precision,
            unit_of_measurement=param_info.get(SZ_DATA_UNIT, None),
            mode=mode,
            ramses_cc_class=RamsesNumberParam,
            ramses_rf_class=type(device),
            data_type=param_info.get(SZ_DATA_TYPE, None),
        )
        descriptions.append(desc)

    return descriptions


async def async_create_parameter_entities(
    broker: RamsesBroker, device: RamsesRFEntity
) -> list[RamsesNumberParam]:
    """Create parameter entities for a device.

    This function creates number entities for each parameter supported by the device.
    It checks if the device supports 2411 parameters and creates appropriate entities.
    It also ensures that duplicate entities are not created.

    :param broker: The broker instance
    :type broker: RamsesBroker
    :param device: The device to create parameter entities for
    :type device: RamsesRFEntity
    :return: A list of created RamsesNumberParam entities
    :rtype: list[RamsesNumberParam]
    """
    # Normalize device ID once at the start
    device_id = normalize_device_id(device.id)
    _LOGGER.debug("async_create_parameter_entities for %s", device_id)

    if not hasattr(device, "supports_2411") or not device.supports_2411:
        _LOGGER.debug(
            "Device %s does not support 2411 parameters, skipping parameter entities",
            device_id,
        )
        return []

    # Check if we've already created parameter entities for this device in this session
    if (
        hasattr(broker, "_parameter_entities_created")
        and device_id in broker._parameter_entities_created
    ):
        _LOGGER.debug(
            "Parameter entities already created for %s in current session, skipping",
            device_id,
        )
        return []

    _LOGGER.info(
        "Creating parameter entities for %s (supports 2411 parameters)", device_id
    )

    # Get existing entity registry entries
    ent_reg = er.async_get(broker.hass)
    existing_entities = {
        ent.unique_id: ent.entity_id
        for ent in ent_reg.entities.values()
        if ent.platform == "ramses_cc"
        and ent.unique_id.startswith(f"{device_id}_param_")
    }

    _LOGGER.debug(
        "Found %d existing parameter entities in registry for %s: %s",
        len(existing_entities),
        device_id,
        list(existing_entities.keys()),
    )

    # Log details about what we're doing
    if existing_entities:
        _LOGGER.debug(
            "Found existing entities in registry, checking if they need to be created in platform"
        )

    param_descriptions = get_param_descriptions(device)
    entities: list[RamsesNumberParam] = []

    for description in param_descriptions:
        if not description.ramses_rf_attr:
            _LOGGER.debug(
                "Skipping parameter %s - no ramses_rf_attr",
                getattr(description, "key", "unknown"),
            )
            continue

        param_id = getattr(description, "ramses_rf_attr", "unknown")
        # Create a unique ID for this parameter entity
        unique_id = f"{device_id}_param_{param_id.lower()}"

        # If the entity is already in the HA entity registry, don't create it.
        # TODO(wi): we have getOrCreate() for that ?
        # HA will restore it from the registry, and we'll pick it up there.
        if unique_id in existing_entities:
            entity_id = existing_entities[unique_id]
            _LOGGER.debug(
                "Parameter entity %s already exists in registry as %s, creating it for platform",
                unique_id,
                entity_id,
            )
            # Continue to create the entity so it can be loaded into the platform

        try:
            # Set the entity key to just the parameter ID - the RamsesNumberParam will handle the full ID
            if not description.key:
                description.key = f"param_{param_id.lower()}"
                # sphinx error caused here?
                # TODO(wi): 'RamsesNumberEntityDescription' object attribute 'key' is read-only

            entity = description.ramses_cc_class(broker, device, description)
            entities.append(entity)
            _LOGGER.info(
                "Created parameter entity: %s for %s (param_id=%s)",
                entity.entity_id,
                device_id,
                param_id,
            )
        except Exception as e:
            _LOGGER.error(
                "Error creating parameter entity: %s",
                str(e),
                exc_info=True,
            )

    _LOGGER.debug(
        "Processed %d parameter entities for %s (%d created, %d skipped)",
        len(param_descriptions),
        device_id,
        len(entities),
        len(param_descriptions) - len(entities),
    )
    return entities

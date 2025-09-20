"""Support for RAMSES numbers."""
# Currently only used for fan parameters
# Can be extended for other NUMBER entities:
# - add_devices
# - extend RamsesNumberBase like we did with RamsesNumberParam

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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
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
from .schemas import SVCS_RAMSES_FAN_PARAM

_LOGGER = logging.getLogger(__name__)


def normalize_device_id(device_id: str) -> str:
    """Normalize device ID by replacing colons with underscores and converting to lowercase."""
    return str(device_id).replace(":", "_").lower()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the RAMSES number platform from a config entry.
    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param entry: The config entry used to set up the platform.
    :type entry: ConfigEntry
    :param async_add_entities: Async function to add entities to the platform.
    :type async_add_entities: AddEntitiesCallback
    :return: None
    :rtype: None
    """

    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    _LOGGER.debug("[Number] Setting up platform")

    # register the FAN PARAM services to the platform
    for k, v in SVCS_RAMSES_FAN_PARAM.items():
        platform.async_register_entity_service(k, v, f"async_{k}")

    @callback
    async def add_devices(devices: list[RamsesRFEntity | RamsesNumberParam]) -> None:
        """Add number entities for the given devices or entities.

        This function coordinates the creation of all number entity types.
        """
        _LOGGER.debug("[Number] Processing %d items", len(devices))
        if not devices:
            return

        # If we received entities directly (not devices), just add them
        if all(isinstance(d, RamsesNumberParam) for d in devices):
            _LOGGER.debug("[Number] Adding %d entities directly", len(devices))
            async_add_entities(devices)
            return

        # Otherwise, process as devices and create entities
        entities: list[RamsesNumberBase] = []

        for device in devices:
            if not isinstance(device, RamsesRFEntity):
                _LOGGER.debug("Skipping non-device item: %s", device)
                continue

            if param_entities := await async_create_parameter_entities(broker, device):
                entities.extend(param_entities)

            # Future: Add other entity types here
            # if other_entities := await async_create_other_entities(broker, devices):
            #     entities.extend(other_entities)

        if entities:
            _LOGGER.debug("[Number] Adding %d entities", len(entities))
            async_add_entities(entities)

    # Register the callback with the broker
    broker.async_register_platform(platform, add_devices)

    # Load any existing devices that were discovered before platform registration
    if hasattr(broker, "devices") and broker.devices:
        _LOGGER.debug("Processing %d existing devices", len(broker.devices))
        # Filter only devices that support parameters
        fan_devices = [
            d for d in broker.devices if hasattr(d, "supports_2411") and d.supports_2411
        ]
        if fan_devices:
            _LOGGER.debug("Found %d FAN devices to process", len(fan_devices))
            await add_devices(fan_devices)


class RamsesNumberBase(RamsesEntity, NumberEntity):
    """Base class for all RAMSES number entities.

    This class provides common functionality for all RAMSES number entities.
    Specific number entity types should inherit from this class.
    """

    entity_description: RamsesNumberEntityDescription
    _attr_should_poll = (
        False  # Disable polling by default, can be overridden by subclasses
    )
    _attr_entity_category = EntityCategory.CONFIG
    _is_pending: bool = False
    _pending_value: float | None = None

    def set_pending(self, value: float | None = None) -> None:
        """Set the entity to a pending state with an optional value.

        :param value: The pending value to set, or None to just set the pending state.
        :type value: float | None
        """
        self._is_pending = True
        self._pending_value = value
        self.async_write_ha_state()

    def clear_pending(self) -> None:
        """Clear the pending state and any pending value."""
        self._is_pending = False
        self._pending_value = None
        self.async_write_ha_state()

    async def _clear_pending_after_timeout(self, timeout: float = 30.0) -> None:
        """Clear the pending state after a timeout if it's still set.

        :param timeout: The timeout in seconds to wait before clearing the pending state.
        :type timeout: float
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
            _LOGGER.warning("Error in clear_pending: %s", ex)

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

        Args:
            value: The value to scale (e.g., 100% -> 1.0)

        Returns:
            Scaled value if percentage mode is enabled, otherwise the original value.
        """
        if value is None:
            return None
        return value / 100 if self._is_percentage else value

    def _scale_for_display(self, value: Any) -> float | None:
        """Convert and scale a stored value for display based on the entity's configuration.

        Args:
            value: The stored value to scale for display (e.g., "1.0" -> 100.0%)

        Returns:
            Scaled float value if percentage mode is enabled, otherwise the original value as float.
            Returns None if value cannot be converted to float.
        """
        if value is None:
            _LOGGER.debug(
                "No value available yet for parameter %s", self._normalized_param_id
            )
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
        """Validate that a value is within the allowed range.

        Args:
            value: The value to validate

        Returns:
            tuple: (is_valid, error_message)
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
        """Validate and scale a value for the entity.

        This combines validation and scaling into a single operation.

        Args:
            value: The value to validate and scale

        Returns:
            tuple: A tuple containing:
                - bool: Whether the value is valid
                - str | None: Error message if invalid, None if valid
                - float: The scaled value (only valid if first item is True)
        """
        # First validate the range
        is_valid, error_msg = self._validate_value_range(value)
        if not is_valid:
            # Return 0.0 as a safe default since we can't return None
            return False, error_msg, 0.0

        # Then scale the value for storage
        scaled_value = self._scale_for_storage(value)
        # Ensure we always return a float, defaulting to 0.0 if None
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

        :return: The input mode, either 'slider' for temperature parameters
                 (ID 75) or 'auto' for all other parameters.
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
        """Get the normalized parameter ID from entity description."""
        param_id = getattr(self.entity_description, "ramses_rf_attr", None)
        return str(param_id).upper() if param_id else None

    async def async_added_to_hass(self) -> None:
        """Run when entity is about to be added to Home Assistant.

        This method:
        1. Calls the parent class's async_added_to_hass method
        2. Sets up an event listener for parameter updates
        3. Requests the initial parameter value from the device
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

        Called when a fan parameter update event is received (for any 2411 entity).

        :param event: The event data containing the parameter update information.
                      Expected keys: 'device_id', 'param_id', 'value'.
        :type event: dict[str, Any]
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
        """Initialize the number."""
        super().__init__(broker, device, entity_description)

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

        :return: True if the entity is available, False otherwise.
        :rtype: bool
        """
        if not self._normalized_param_id:
            return False

        value = self._param_native_value.get(self._normalized_param_id)
        return value is not None

    async def _request_parameter_value(self) -> None:
        """Request the current value of this parameter from the device.

        This method implements rate limiting to prevent duplicate requests for the same parameter.
        It will only make a new request if:
        1. No request is currently pending for this parameter, AND
        2. No request has been made for this parameter in the last 15 seconds

        The method will set the entity to a pending state while waiting for a response.
        """
        if (
            not hasattr(self, "hass")
            or not hasattr(self, "_device")
            or not hasattr(self.entity_description, "ramses_rf_attr")
        ):
            _LOGGER.debug("_request_parameter_value: missing required attributes")
            return

        # Get the parameter ID from the entity description
        param_id = self.entity_description.ramses_rf_attr
        if not param_id:
            _LOGGER.debug("_request_parameter_value: missing parameter ID")
            return

        _LOGGER.debug("Requesting parameter %s from %s", param_id, self._device.id)

        self.set_pending()

        # Request the parameter value from the device
        self._device.get_fan_param(param_id)

        # Schedule a check to clear the pending state if we don't get a response
        self.hass.async_create_task(self._clear_pending_after_timeout(30.0))

    def _is_boost_mode_param(self) -> bool:
        """Check if this is a boost mode parameter (ID 95)."""
        return getattr(self.entity_description, "ramses_rf_attr", "") == "95"

    @property
    def native_value(self) -> float | None:
        """Return the current value of the entity.

        :return: The current value of the parameter, or None if no value is available.
        :rtype: float | None
        """
        if not hasattr(self, "_normalized_param_id") or not self._normalized_param_id:
            _LOGGER.error("Cannot get value: missing parameter ID")
            return None

        value = self._param_native_value.get(self._normalized_param_id)

        # For boost mode (param 95), scale from 0-1 to 0-100%
        if value is not None and self._is_boost_mode_param():
            return round(float(value) * 100.0, 1)

        return self._scale_for_display(value)

    async def async_set_native_value(self, value: float) -> None:
        """Set a new value for the parameter.

        :param value: The new value to set for the parameter.
        :type value: float
        :raises ValueError: If the value is outside the valid range.
        """
        if not hasattr(self, "_device") or not hasattr(self, "_normalized_param_id"):
            _LOGGER.error("Cannot set value: missing required attributes")
            return

        _LOGGER.debug(
            "Set native value for parameter %s to %s", self._normalized_param_id, value
        )

        try:
            # For boost mode (param 95), scale from percentage (0-100) to 0-1
            if self._is_boost_mode_param():
                scaled_value = float(value) / 100.0
                display_value = round(float(value), 1)
                self.set_pending(display_value)

                if hasattr(self._device, "set_fan_param"):
                    await self._device.set_fan_param(
                        self._normalized_param_id, scaled_value
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

            # Call the device's set_fan_param method
            if hasattr(self._device, "set_fan_param"):
                await self._device.set_fan_param(
                    self._normalized_param_id, scaled_value
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

        # Get parameter ID for special cases
        param_id = getattr(self.entity_description, "ramses_rf_attr", "")

        # Get unit of measurement for icon selection
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
    :return: List of parameter descriptions for the device
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
    Still async_add_entities may need to be called to register the platform
    (async_setup_platform/SIGNAL_NEW_DEVICES)

    :param broker: The RamsesBroker instance for managing device communication.
    :type broker: RamsesBroker
    :param device: The device to create parameter entities for.
    :type device: RamsesRFEntity
    :return: A list of created RamsesNumberParam entities.
    :rtype: list[RamsesNumberParam]
    """
    # Normalize device ID once at the start
    device_id = normalize_device_id(device.id)
    _LOGGER.debug("async_create_parameter_entities for %s", device_id)

    if not hasattr(device, "supports_2411") or not device.supports_2411:
        _LOGGER.debug(
            "Skipping parameter entities for %s - 2411 not supported", device_id
        )
        return []

    _LOGGER.info(
        "Creating parameter entities for %s (supports 2411 parameters)", device_id
    )

    param_descriptions = get_param_descriptions(device)
    entities: list[RamsesNumberParam] = []

    for description in param_descriptions:
        if not hasattr(description, "ramses_rf_attr"):
            _LOGGER.debug(
                "Skipping parameter %s - no ramses_rf_attr",
                getattr(description, "key", "unknown"),
            )
            continue

        param_id = getattr(description, "ramses_rf_attr", "unknown")
        try:
            # Set the entity key to just the parameter ID - the RamsesNumberParam will handle the full ID
            if not hasattr(description, "key"):
                description.key = f"param_{param_id.lower()}"

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

    _LOGGER.debug("Created %d parameter entities for %s", len(entities), device_id)
    return entities

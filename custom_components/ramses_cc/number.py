"""Support for RAMSES numbers."""

from __future__ import annotations

import asyncio
import logging
import time
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

from ramses_rf.device import Fakeable
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

# from homeassistant.helpers.dispatcher import async_dispatcher_connect
from . import RamsesEntity, RamsesEntityDescription
from .broker import RamsesBroker
from .const import DOMAIN
from .schemas import SVCS_RAMSES_FAN_PARAM

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the number platform."""

    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()
    # register the FAN PARAM services to the platform
    for k, v in SVCS_RAMSES_FAN_PARAM.items():
        platform.async_register_entity_service(k, v, f"async_{k}")

    @callback
    def add_devices(devices: list[RamsesRFEntity]) -> None:
        _LOGGER.debug("[Number] Adding %d devices", len(devices))
        entities = [
            description.ramses_cc_class(broker, device, description)
            for device in devices
            for description in NUMBER_DESCRIPTIONS
            if (
                isinstance(device, description.ramses_rf_class)
                and (
                    description.check_attr is None
                    or hasattr(device, description.check_attr)
                )
            )
        ]
        async_add_entities(entities)

    broker.async_register_platform(platform, add_devices)


class RamsesNumber(RamsesEntity, NumberEntity):
    """Representation of a generic number."""

    entity_description: RamsesNumberEntityDescription
    _attr_should_poll = False  # Disable polling, we'll update via events
    _param_native_value: dict[str, float | None] = {}
    _is_pending: bool = False
    _pending_value: float | None = None

    @property
    def mode(self) -> str:
        """Return the mode of the entity."""
        if (
            hasattr(self.entity_description, "ramses_rf_attr")
            and self.entity_description.ramses_rf_attr == "75"
        ):
            return "slider"
        return "auto"

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
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

        This method is called when a fan parameter update event is received (for any 2411 entity).
        It processes the event data and updates the entity state if the event is relevant to this entity.
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
            if (
                not hasattr(self, "_param_native_value")
                or self._param_native_value is None
            ):
                self._param_native_value = {}

            # Ensure we're using the same key type as in native_value property
            param_id = str(
                our_param_id
            ).upper()  # Convert to uppercase string for consistency
            self._param_native_value[param_id] = new_value
            _LOGGER.debug(
                "Parameter %s updated for device %s: %s (stored as: %s, full dict: %s)",
                our_param_id,
                self._device.id,
                new_value,
                self._param_native_value.get(param_id),
                self._param_native_value,
            )

            # Clear pending state since we've received the update
            if self._is_pending:
                _LOGGER.debug("Clearing pending state for parameter %s", our_param_id)
                self._is_pending = False
                self._pending_value = None

            self.async_write_ha_state()

    def __init__(
        self,
        broker: RamsesBroker,
        device: RamsesRFEntity,
        entity_description: RamsesEntityDescription,
    ) -> None:
        """Initialize the number."""
        _LOGGER.info("Found %r: %s", device, entity_description.key)
        super().__init__(broker, device, entity_description)

        # Initialize parameter values dictionary
        self._param_native_value: dict[str, float | None] = {}

        # Initialize pending state
        self._is_pending = False
        self._pending_value: float | None = None

        self.entity_id = ENTITY_ID_FORMAT.format(
            f"{device.id}_{entity_description.key}"
        )
        self._attr_unique_id = f"{device.id}-{entity_description.key}"

        # Get the parameter ID from the entity description
        param_id = getattr(entity_description, "ramses_rf_attr", "")

        # Special case for parameters that are already in percentage - don't scale them
        self._is_percentage = (
            hasattr(entity_description, "unit_of_measurement")
            and entity_description.unit_of_measurement == "%"
            and param_id not in ("52", "95")
        )  # Don't scale these parameters

        # Set min/max/step values from entity description if available
        if (
            hasattr(entity_description, "min_value")
            and entity_description.min_value is not None
        ):
            min_val = float(entity_description.min_value)
            self._attr_native_min_value = (
                min_val * 100 if self._is_percentage else min_val
            )

        if (
            hasattr(entity_description, "max_value")
            and entity_description.max_value is not None
        ):
            max_val = float(entity_description.max_value)
            self._attr_native_max_value = (
                max_val * 100 if self._is_percentage else max_val
            )

        # Special handling for temperature parameters (param 75) - force 0.1°C precision
        if param_id == "75":
            self._attr_native_step = 0.1
            _LOGGER.debug(
                "Forcing 0.1°C precision for parameter 75 (Comfort temperature)"
            )
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
        """Return if the entity is available."""
        """Return True if the entity is available."""
        # TODO: Should use dtm of last packet received, rather than is not None
        return (
            isinstance(self._device, Fakeable) and self._device.is_faked
        ) or self.state is not None  # TODO: but what if None _is_ its state?

    async def _request_parameter_value(self) -> None:
        """Request the current value of this parameter from the device.

        This method implements rate limiting to prevent duplicate requests for the same parameter.
        It will only make a new request if:
        1. No request is currently pending for this parameter, AND
        2. No request has been made for this parameter in the last 30 seconds

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

        # Check if we have a pending request for this parameter
        request_key = f"_param_request_{param_id}"
        last_request = getattr(self._device, request_key, None)

        # Rate limiting: Only make a new request if the last one was more than 30 seconds ago
        if last_request and (time.time() - last_request) < 30:
            _LOGGER.debug(
                "Skipping parameter %s request for %s: last request was %d seconds ago",
                param_id,
                self._device.id,
                time.time() - last_request,
            )
            return

        _LOGGER.debug("Requesting parameter %s from %s", param_id, self._device.id)

        # Set pending state and update UI
        self._is_pending = True
        self.async_write_ha_state()

        # Mark that we've made a request for this parameter
        setattr(self._device, request_key, time.time())

        # Request the parameter value from the device
        self._device.get_fan_param(param_id)

        # Schedule a check to clear the pending state if we don't get a response
        async def clear_pending() -> None:
            """Clear the pending state of the entity if no response is received."""
            await asyncio.sleep(30)  # Wait 30 seconds for a response
            if self._is_pending:
                _LOGGER.debug(
                    "No response received for parameter %s from %s, clearing pending state",
                    param_id,
                    self._device.id,
                )
                self._is_pending = False
                self.async_write_ha_state()

        self.hass.async_create_task(clear_pending())

    @callback
    def async_update(self) -> None:
        """Update the entity state."""
        self._attr_available = self.available
        if hasattr(self, "native_value"):
            self._param_native_value = {"value": self.native_value}

    @property
    def native_value(self) -> float | None:
        """Return the cached value."""
        param_id = getattr(self.entity_description, "ramses_rf_attr", "")
        if not param_id:
            _LOGGER.error("Cannot get value: missing parameter ID")
            return None

        if not hasattr(self, "_param_native_value") or self._param_native_value is None:
            _LOGGER.debug("No _param_native_value dict for parameter %s yet", param_id)
            return None

        # Convert param_id to uppercase string to match storage format
        param_key = str(param_id).upper()
        value = self._param_native_value.get(param_key)

        if value is None:
            _LOGGER.debug("No value available yet for parameter %s", param_id)
            return None

        try:
            float_value = float(value)
            return float_value * 100 if self._is_percentage else float_value
        except (TypeError, ValueError) as err:
            _LOGGER.debug(
                "Could not convert parameter %s value '%s' to float: %s",
                param_id,
                value,
                str(err),
            )
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        if not hasattr(self, "_device") or not hasattr(
            self.entity_description, "ramses_rf_attr"
        ):
            _LOGGER.error("Cannot set value: missing required attributes")
            return

        param_id = self.entity_description.ramses_rf_attr
        if not param_id:
            _LOGGER.error("Cannot set value: missing parameter ID")
            return

        _LOGGER.debug("Set native value for parameter %s to %s", param_id, value)

        # Store the pending value and set pending state
        self._pending_value = float(value) if value is not None else None
        self._is_pending = True
        self.async_write_ha_state()

        try:
            # Scale percentage values back to 0-1 range for the device
            if self._is_percentage and value is not None:
                value = value / 100.0
                _LOGGER.debug(
                    "%s: Scaled parameter %s value for device: %s",
                    self.unique_id,
                    param_id,
                    value,
                )

            # Call the device's set_fan_param method
            if hasattr(self._device, "set_fan_param"):
                _LOGGER.debug(
                    "%s: Setting parameter %s to %s", self.unique_id, param_id, value
                )
                await self._device.set_fan_param(param_id, value)
                _LOGGER.debug(
                    "%s: Successfully set parameter %s to %s",
                    self.unique_id,
                    param_id,
                    value,
                )

                # Update the displayed value
                self.async_write_ha_state()

        except Exception as ex:
            _LOGGER.error(
                "%s: Error setting parameter %s to %s: %s",
                self.unique_id,
                param_id,
                value,
                ex,
                exc_info=True,
            )
        finally:
            # Clear pending state and update the UI
            self._is_pending = False
            self._pending_value = None
            self.async_write_ha_state()

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
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
    """Class describing Ramses binary number entities."""

    # integration-specific attributes
    ramses_cc_class: type[RamsesNumber] = RamsesNumber
    ramses_cc_icon_off: str | None = None  # no NumberEntityDescription.icon_off attr
    ramses_rf_attr: str = ""
    ramses_rf_class: type[RamsesRFEntity] | UnionType = RamsesRFEntity

    # Parameters for 2411 parameter entities
    check_attr: str | None = None
    data_type: str | None = None
    _pending_value: float | None = None
    precision: float | None = None
    parameter_id: str | None = None
    parameter_desc: str | None = None
    unit_of_measurement: str | None = None
    mode: str = "auto"


def get_number_descriptions(
    device: RamsesRFEntity,
) -> list[RamsesNumberEntityDescription]:
    """Generate number entity descriptions for a device that supports 2411 parameters.

    Args:
        device: The device to generate descriptions for.

    Returns:
        A list of RamsesNumberEntityDescription objects for the device's parameters.
    """
    if not hasattr(device, "supports_2411") or not device.supports_2411:
        return []

    descriptions: list[RamsesNumberEntityDescription] = []

    # Get parameter schema from the device
    if not hasattr(device, "_2411_PARAMS_SCHEMA"):
        return []
    param_schema = device._2411_PARAMS_SCHEMA

    # Create a description for each parameter
    for param_id, param_info in param_schema.items():
        # Get precision from schema or default to 1.0 for floats, 1 for ints
        precision = param_info.get(
            SZ_PRECISION, 1.0 if param_info.get(SZ_DATA_TYPE) == "float" else 1
        )

        # Special handling for temperature parameters (param 75) - force 0.1°C precision and slider mode
        mode = "auto"
        if (
            param_id == "75"
        ):  # comfort temp, keep it at 0.01 in 2411_params_schema since custom automations may depend on it
            precision = 0.1
            mode = "slider"  # Use slider mode for comfort temperature

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
            mode=mode,  # Use slider mode for comfort temperature
            ramses_cc_class=RamsesNumber,
            ramses_rf_class=type(device),
            data_type=param_info.get(SZ_DATA_TYPE, None),
        )
        descriptions.append(desc)

    return descriptions


async def async_create_parameter_entities(
    broker: RamsesBroker, device: RamsesRFEntity
) -> list[RamsesNumber]:
    """Create parameter entities for a device.

    Args:
        broker: The RamsesBroker instance.
        device: The device to create parameter entities for.

    Returns:
        A list of created RamsesNumber entities.
    """
    if not hasattr(device, "supports_2411") or not device.supports_2411:
        _LOGGER.debug(
            "Skipping parameter entities for %s - 2411 not supported", device.id
        )
        return []

    _LOGGER.info(
        "Creating parameter entities for %s (supports 2411 parameters)", device.id
    )
    descriptions = get_number_descriptions(device)
    entities: list[RamsesNumber] = []

    for description in descriptions:
        if not hasattr(description, "ramses_rf_attr"):
            continue
        entity = description.ramses_cc_class(broker, device, description)
        entities.append(entity)
        _LOGGER.debug("Created parameter entity: %s for %s", description.key, device.id)

    return entities


NUMBER_DESCRIPTIONS: tuple[RamsesNumberEntityDescription, ...] = (
    *[
        RamsesNumberEntityDescription(
            check_attr="supports_2411",
            key=f"param_{param_id}",
            entity_category=EntityCategory.CONFIG,
            ramses_cc_class=RamsesNumber,
            ramses_rf_attr=param_id,
            ramses_rf_class=RamsesRFEntity,
            data_type=param[SZ_DATA_TYPE],
            min_value=float(param[SZ_MIN_VALUE]),
            max_value=float(param[SZ_MAX_VALUE]),
            precision=float(param[SZ_PRECISION]),
            name=param[SZ_DESCRIPTION],
            unit_of_measurement=param[SZ_DATA_UNIT],
            mode="auto",
        )
        for param_id, param in _2411_PARAMS_SCHEMA.items()
    ],
    # Hardcoded item appended to the dynamic list can go below
    # RamsesNumberEntityDescription(
    # ),
)

"""Base Entity for RAMSES integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_ID
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ramses_rf.entity_base import Entity as RamsesRFEntity

from .const import DOMAIN, SIGNAL_UPDATE

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator


@dataclass(frozen=True, kw_only=True)
class RamsesEntityDescription(EntityDescription):
    """Class describing Ramses entities."""

    has_entity_name: bool = True

    # integration-specific attributes
    ramses_cc_extra_attributes: dict[str, str] | None = None


class RamsesEntity(CoordinatorEntity):
    """Base for any RAMSES II-compatible entity (e.g. Climate, Sensor).

    This class handles the connection between the Home Assistant entity and the
    underlying ramses_rf device, including device registry registration and
    state updates via dispatcher signals.
    """

    _device: RamsesRFEntity
    coordinator: RamsesCoordinator  # Type hint for the coordinator

    _attr_should_poll = False
    _attr_has_entity_name = True

    entity_description: RamsesEntityDescription

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: RamsesRFEntity,
        entity_description: RamsesEntityDescription,
    ) -> None:
        """Initialize the entity.

        :param coordinator: The data update coordinator for the integration.
        :param device: The underlying ramses_rf device instance.
        :param entity_description: Description of the entity's attributes.
        """
        super().__init__(coordinator)
        self._device = device
        self.entity_description = entity_description

        self._attr_unique_id = device.id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.id)})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Get the integration-specific state attributes.

        :return: A dictionary of attributes derived from the device and description.
        """
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
        """Run when entity about to be added to hass.

        Registers the entity with the coordinator and subscribes to
        device-specific update signals.
        """
        await super().async_added_to_hass()
        self.coordinator._entities[self.unique_id] = self

        # Listen for device-specific update signal
        device_signal = f"{SIGNAL_UPDATE}_{self._device.id}"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, device_signal, self.async_write_ha_state
            )
        )

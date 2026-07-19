"""Base Entity for RAMSES integration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_ID
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ramses_rf.devices import Fakeable
from ramses_rf.entity import Entity as RamsesRFEntity

from .const import DOMAIN, SIGNAL_UPDATE
from .helpers import resolve_async_attr

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class RamsesEntityDescription(EntityDescription):
    """Class describing Ramses entities."""

    has_entity_name: bool = True

    # integration-specific attributes
    ramses_cc_extra_attributes: dict[str, str] | None = None


class RamsesEntity(CoordinatorEntity):
    """Base for any RAMSES II-compatible entity (e.g. Climate, Sensor).

    This class handles the connection between the Home Assistant entity
    and the underlying ramses_rf device, including device registry
    registration and state updates via dispatcher signals.
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

        :param coordinator: The data update coordinator for the
            integration.
        :param device: The underlying ramses_rf device instance.
        :param entity_description: Description of the entity's
            attributes.
        """
        super().__init__(coordinator)
        self._device = device
        self.entity_description = entity_description

        self._attr_unique_id = device.id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.id)})
        self._update_lock = asyncio.Lock()
        self._dropped_updates: int = 0
        self._last_drop_report: float = time.monotonic()

    @property
    def available(self) -> bool:
        """Return True if the entity is available based on protocol
        health.

        Delegates the health check to the underlying ramses_rf device.
        Faked devices are always considered available.

        :return: True if the device is active and communicating, False
            otherwise.
        :rtype: bool
        """
        # Explicit exemption for the HGI gateway (always available)
        if self._device.id.startswith("18:"):
            return True

        # Resilient faked check for cache restoration
        if isinstance(self._device, Fakeable) and (
            getattr(self._device, "is_faked", False)
            or getattr(self._device, "_is_faked", False)
        ):
            return True

        # Safely delegate to the library's is_available property.
        # Defaults to True if an older version of ramses_rf is present.
        return bool(getattr(self._device, "is_available", True))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: A dictionary of attributes derived from the device
            and description.
        """
        attrs = {
            ATTR_ID: self._device.id,
        }
        if self.entity_description.ramses_cc_extra_attributes:
            for k, v in self.entity_description.ramses_cc_extra_attributes.items():
                if hasattr(self._device, v):
                    # Safely resolve callable/async attributes
                    attrs[k] = resolve_async_attr(self, self._device, v)

        # Surface discovery mismatch flags (Phase 3c) so they're visible
        # in Developer Tools without opening the config flow review step.
        discovery_mgr = getattr(self.coordinator, "discovery_manager", None)
        if discovery_mgr is not None:
            meta = discovery_mgr._metadata.get(self._device.id)
            if meta is not None:
                for flag_key, flag_val in (
                    ("class_mismatch", meta.class_mismatch),
                    ("bound_mismatch", meta.bound_mismatch),
                    ("missing_class", meta.missing_class),
                    ("orphaned", meta.orphaned),
                ):
                    if isinstance(flag_val, str) and flag_val:
                        attrs[flag_key] = flag_val

        return attrs

    async def async_added_to_hass(self) -> None:
        """Run when entity is about to be added to hass.

        Registers the entity with the coordinator and subscribes to
        device-specific update signals.
        """
        await super().async_added_to_hass()
        if self.unique_id:
            self.coordinator._entities[self.unique_id] = self

        # Listen for device-specific update signal
        device_signal = f"{SIGNAL_UPDATE}_{self._device.id}"
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, device_signal, self._async_update_and_write_state
            )
        )

    async def _async_update_and_write_state(self) -> None:
        """Safely write HA state using an async lock.

        Prevents event loop saturation from concurrent updates.
        """
        if self._update_lock.locked():
            self._dropped_updates += 1
            now = time.monotonic()
            if now - self._last_drop_report >= 60.0:
                _LOGGER.warning(
                    "[%s] Dropped %s concurrent HA state updates in the "
                    "last minute to prevent event loop saturation.",
                    self.unique_id,
                    self._dropped_updates,
                )
                self._dropped_updates = 0
                self._last_drop_report = now
            return

        async with self._update_lock:
            self.async_write_ha_state()

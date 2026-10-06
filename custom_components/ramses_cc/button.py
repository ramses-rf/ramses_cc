"""Support for RAMSES RF button entities.

Adapted from number.py

Buttons are stateless momentary entities that trigger an action when
pressed. This module provides:

- gateway-level buttons that invoke the integration's own domain-wide
  services (see ./services.yaml), and
- per-FAN buttons that reset the filter counter (W 10D0) via the
  ``reset_filter_counter`` entity service (see ./remote.py).

The button platform may be set up before the HGIs are loaded or before
the remote platform has registered the FAN REM entities. A discovery
callback is therefore registered with the coordinator: as devices
(re)appear, any missing buttons are created once their REM entity
exists.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import (
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    entity_platform,
    entity_registry as er,
)
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
)

from ramses_rf.devices import HgiGateway, HvacVentilator
from ramses_rf.entity import Entity as RamsesRFEntity

from .const import DOMAIN, SVC_DISCOVER_KNOWN_DEVICES
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import device_slug, normalize_device_id
from .schemas import (
    SVC_FORCE_UPDATE,
    SVC_RESET_FILTER,
    SVC_SYNC_TOPOLOGY,
)
from .typing import RamsesConfigEntry

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class RamsesButtonEntityDescription(
    RamsesEntityDescription, ButtonEntityDescription
):
    """Class describing Ramses button entities."""

    service: str | None = None  # ramses_cc service to call when pressed
    service_data: dict[str, str] | None = None
    target: dict[str, Any] | None = None
    entity_category: EntityCategory | None = None


# Gateway-level service buttons created for each HGI.
HGI_BUTTON_DESCRIPTIONS: tuple[RamsesButtonEntityDescription, ...] = (
    RamsesButtonEntityDescription(
        key="force_update",
        translation_key="force_update",
        icon="mdi:refresh",
        service=SVC_FORCE_UPDATE,
    ),
    RamsesButtonEntityDescription(
        key="sync_topology",
        translation_key="sync_topology",
        icon="mdi:lan-connect",
        service=SVC_SYNC_TOPOLOGY,
    ),
    RamsesButtonEntityDescription(
        key="discover_known_devices",
        translation_key="discover_known_devices",
        icon="mdi:magnify",
        service=SVC_DISCOVER_KNOWN_DEVICES,
    ),
)

FILTER_RESET_KEY = "reset_filter_counter"


class RamsesButtonBase(RamsesEntity, ButtonEntity):
    """Base for any RAMSES II-compatible button entity."""

    entity_description: RamsesButtonEntityDescription

    _attr_has_entity_name = True

    async def async_press(self) -> None:
        """Handle the button press.

        Calls the ramses_cc service named in the entity description,
        passing any configured service data (e.g. the entity_id of the
        FAN remote entity for the filter reset).
        """
        service = self.entity_description.service
        if not service:
            _LOGGER.warning(
                "Button %s has no service configured", self.entity_id
            )
            return

        _LOGGER.debug(
            "Button %s calls service %s (data=%s, target=%s)",
            self.entity_id,
            service,
            self.entity_description.service_data,
            self.entity_description.target,
        )
        await self.hass.services.async_call(
            DOMAIN,
            service,
            service_data=self.entity_description.service_data or {},
            target=self.entity_description.target or {},
            blocking=True,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the target attribute, plus any base attributes."""
        return super().extra_state_attributes | {
            "target": self.entity_description.target,
        }


def _fan_remote_entity(
    ent_reg: er.EntityRegistry, entry_id: str, fan_id: str
) -> er.RegistryEntry | None:
    """Return the registry entry of the remote entity on a FAN device.

    The ``reset_filter_counter`` entity service accepts a FAN REM
    entity_id as its target (the handler resolves the bound REM itself).
    """
    return next(
        (
            entity
            for entity in er.async_entries_for_config_entry(ent_reg, entry_id)
            if entity.domain == "remote" and entity.unique_id == str(fan_id)
        ),
        None,
    )


class _ButtonFactory:
    """Creates button entities for newly discovered devices.

    Tracks unique_ids to prevent duplicate entities across the initial
    setup pass and subsequent coordinator discovery callbacks.
    """

    def __init__(self, coordinator: RamsesCoordinator) -> None:
        self._coordinator = coordinator
        self._hass = coordinator.hass
        self._entry_id = coordinator.entry.entry_id
        self._known_unique_ids: set[str] = set()

    def hgi_buttons(self, hgi: RamsesRFEntity) -> list[RamsesButtonBase]:
        """Create gateway service buttons for an HGI device (deduped)."""
        if not isinstance(hgi, HgiGateway) or hgi.id is None:
            return []

        device_id = normalize_device_id(hgi.id)
        _LOGGER.debug("Adding HGI buttons for %s", device_id)

        return [
            button
            for description in HGI_BUTTON_DESCRIPTIONS
            if (button := self._make_button(hgi, description, device_id))
        ]

    def fan_buttons(self, fan: RamsesRFEntity) -> list[RamsesButtonBase]:
        """Create a filter-reset button for a FAN device (deduped).

        Returns an empty list (to be retried on the next discovery
        callback) if the FAN has no bound REM yet or the REM entity has
        not yet been registered by the remote platform. A FAN bound to
        a non-faked REM gets no button at all.
        """
        if not isinstance(fan, HvacVentilator):
            return []

        if hasattr(fan, "supports_10d0"):
            if not getattr(fan, "supports_10d0", False):
                _LOGGER.debug(
                    "Device %s does not seem to support 10D0 Filter Change",
                    fan.id,
                )
                # return []  # continue until the _supports check works reliably

        rem_id = fan.get_bound_rem()
        if rem_id is None:
            _LOGGER.debug(
                "No bound REM for FAN %s; will retry when devices are"
                " (re)discovered",
                fan.id,
            )
            return []

        # Only faked REMs can transmit (real REMs can't be impersonated),
        # so a FAN bound to a real REM gets no button.
        rem_dev = self._coordinator._get_device(rem_id)
        if rem_dev is not None and not rem_dev.is_faked:
            _LOGGER.debug(
                "Bound REM %s is not faked; no reset button for FAN %s",
                rem_id,
                fan.id,
            )
            return []

        remote_entity = _fan_remote_entity(
            er.async_get(self._hass), self._entry_id, fan.id
        )
        if remote_entity is None:
            _LOGGER.debug(
                "No remote entity yet for FAN %s; will retry when devices"
                " are (re)discovered",
                fan.id,
            )
            return []

        _LOGGER.debug(
            "Preparing filter counter reset button, targeting REM %s on"
            " FAN %s",
            remote_entity.entity_id,
            fan.id,
        )

        button = self._make_button(
            fan,
            RamsesButtonEntityDescription(
                key=FILTER_RESET_KEY,
                translation_key=FILTER_RESET_KEY,
                icon="mdi:restart-alert",
                service=SVC_RESET_FILTER,
                target={"entity_id": [remote_entity.entity_id]},
            ),
            normalize_device_id(fan.id),
        )
        if button is None:
            return []

        return [button]

    def button_entities(
        self, device: RamsesRFEntity
    ) -> list[RamsesButtonBase]:
        """Create all applicable buttons for a single device."""
        match device_slug(device):
            case "HGI":
                return self.hgi_buttons(device)
            case "FAN":
                return self.fan_buttons(device)
            case _:
                return []

    def _make_button(
        self,
        device: RamsesRFEntity,
        description: RamsesButtonEntityDescription,
        device_id: str,
    ) -> RamsesButtonBase | None:
        """Create a button unless its unique_id was already created."""
        unique_id = f"{device_id}-{description.key}"
        if unique_id in self._known_unique_ids:
            _LOGGER.debug("Button %s already created, skipping", unique_id)
            return None

        # hass.services.async_call rejects a set as entity_id target, so
        # coerce any set to a sorted list once, here (single choke point
        # for every description, current or future).
        if description.target and isinstance(
            description.target.get("entity_id"), set
        ):
            description = replace(
                description,
                target=description.target
                | {"entity_id": sorted(description.target["entity_id"])},
            )

        self._known_unique_ids.add(unique_id)
        button = RamsesButtonBase(self._coordinator, device, description)
        button._attr_unique_id = unique_id
        return button


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RamsesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RAMSES button platform from a config entry.

    Creates one gateway-level button per domain-wide service action,
    plus one filter-reset button per FAN device. Devices discovered
    after setup (or whose REM entity was not yet registered) are
    handled by the discovery callback registered with the coordinator.
    """
    coordinator: RamsesCoordinator = entry.runtime_data
    platform: EntityPlatform = entity_platform.async_get_current_platform()
    factory = _ButtonFactory(coordinator)

    @callback
    def add_devices(
        devices: RamsesRFEntity
        | RamsesButtonBase
        | Sequence[RamsesRFEntity | RamsesButtonBase],
    ) -> None:
        """Add button entities for the given devices or entities.

        Devices arrive via the coordinator's new-device dispatch;
        prebuilt entities arrive from the setup pass below. Both are
        deduplicated via the factory's known unique_ids and the
        platform's loaded entities.

        :param devices: Devices or button entities to process.
        """
        device_list = devices if isinstance(devices, Sequence) else [devices]
        if not device_list:
            return

        if all(isinstance(d, RamsesButtonBase) for d in device_list):
            # Entities passed directly: add the ones not already
            # loaded by this platform.
            entities_to_add = [
                entity
                for entity in device_list
                if isinstance(entity, RamsesButtonBase)
                and entity.entity_id not in platform.entities
            ]
            if entities_to_add:
                async_add_entities(entities_to_add)
            return

        for device in device_list:
            if not isinstance(device, RamsesRFEntity):
                _LOGGER.debug("Skipping non-device item: %s", device)
                continue
            if _buttons := factory.button_entities(device):
                async_add_entities(_buttons)

    coordinator.async_register_platform(platform, add_devices)

    # Buttons for devices already known at setup time; anything
    # discovered later (or still missing its REM entity) is handled by
    # the callback above.
    entities: list[RamsesButtonBase] = [
        button
        for device in coordinator._devices
        if isinstance(device, RamsesRFEntity)
        for button in factory.button_entities(device)
    ]
    if entities:
        _LOGGER.debug("Adding %d button entities", len(entities))
        add_devices(entities)
    else:
        _LOGGER.debug("No button entities registered at setup time")

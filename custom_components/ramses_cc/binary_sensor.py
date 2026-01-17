"""Support for RAMSES binary sensors."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime as dt, timedelta
from types import UnionType
from typing import Any

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ramses_rf.device.base import BatteryState, HgiGateway
from ramses_rf.device.heat import (
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_COOLING_ACTIVE,
    SZ_COOLING_ENABLED,
    SZ_DHW_ACTIVE,
    SZ_DHW_BLOCKING,
    SZ_DHW_ENABLED,
    SZ_FAULT_PRESENT,
    SZ_FLAME_ACTIVE,
    SZ_OTC_ACTIVE,
    SZ_SUMMER_MODE,
    BdrSwitch,
    OtbGateway,
    TrvActuator,
)
from ramses_rf.entity_base import Entity as RamsesRFEntity
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import SZ_BLOCK_LIST, SZ_CONFIG, SZ_KNOWN_LIST, SZ_SCHEMA
from ramses_rf.system.heat import Logbook, System
from ramses_tx.const import SZ_BYPASS_POSITION, SZ_IS_EVOFW3

from . import RamsesEntity, RamsesEntityDescription
from .const import (
    ATTR_ACTIVE_FAULTS,
    ATTR_BATTERY_LEVEL,
    ATTR_LATEST_EVENT,
    ATTR_LATEST_FAULT,
    ATTR_WORKING_SCHEMA,
    DOMAIN,
)
from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensor platform."""

    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    platform = entity_platform.async_get_current_platform()

    @callback
    def add_devices(devices: list[RamsesRFEntity]) -> None:
        entities = [
            description.ramses_cc_class(coordinator, rf_device, description)
            for rf_device in devices
            for description in BINARY_SENSOR_DESCRIPTIONS
            if isinstance(rf_device, description.ramses_rf_class)
            and hasattr(rf_device, description.ramses_rf_attr)
        ]
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)


class RamsesBinarySensor(RamsesEntity, BinarySensorEntity):
    """Representation of a generic binary sensor."""

    entity_description: RamsesBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: RamsesRFEntity,
        entity_description: RamsesEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        _LOGGER.info("Found %s: %s", device.id, entity_description.key)
        super().__init__(coordinator, device, entity_description)

        self.entity_id = ENTITY_ID_FORMAT.format(
            f"{device.id}_{entity_description.key}"
        )
        self._attr_unique_id = f"{device.id}-{entity_description.key}"

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self.state is not None

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor."""
        return getattr(self._device, self.entity_description.ramses_rf_attr)

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        return (
            self.entity_description.icon
            if self.is_on
            else self.entity_description.icon_off
        )


class RamsesBatteryBinarySensor(RamsesBinarySensor):
    """Representation of a generic binary sensor."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        if self._device.battery_state is None:
            level = None
        else:
            level = self._device.battery_state[ATTR_BATTERY_LEVEL]

        return super().extra_state_attributes | {ATTR_BATTERY_LEVEL: level}


class RamsesLogbookBinarySensor(RamsesBinarySensor):
    """Representation of a fault log."""

    _device: Logbook

    @property
    def available(self) -> bool:
        """Return True if the device has been seen recently."""
        msg = self._device._msgs.get("0418")
        return bool(msg and dt.now() - msg.dtm < timedelta(seconds=1200))

    @property
    def is_on(self) -> bool:
        """Return the state of the binary sensor."""
        return bool(self._device.active_faults)


class RamsesSystemBinarySensor(RamsesBinarySensor):
    """Representation of a system (a controller)."""

    _device: System

    @property
    def available(self) -> bool:
        """Return True if the last system sync message is recent."""
        msg = self._device._msgs.get("1F09")
        return bool(
            msg
            and dt.now() - msg.dtm
            < timedelta(seconds=msg.payload["remaining_seconds"] * 3)
        )

    @property
    def is_on(self) -> bool:
        """Return True if the gateway has received messages recently."""
        return not bool(super().is_on)  # TODO


class RamsesGatewayBinarySensor(RamsesBinarySensor):
    """Representation of a gateway (a HGI80)."""

    _device: HgiGateway

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""

        def shrink(device_hints: dict[str, bool | str]) -> dict[str, Any]:
            return {
                k: v
                for k, v in device_hints.items()
                if k in ("alias", "class", "faked") and v not in (None, False)
            }

        gwy: Gateway = self._device._gwy

        return super().extra_state_attributes | {
            SZ_SCHEMA: {gwy.tcs.id: gwy.tcs._schema_min} if gwy.tcs else {},
            SZ_CONFIG: {"enforce_known_list": gwy._enforce_known_list},
            SZ_KNOWN_LIST: [{k: shrink(v)} for k, v in gwy.known_list.items()],
            SZ_BLOCK_LIST: [{k: shrink(v)} for k, v in gwy._exclude.items()],
            SZ_IS_EVOFW3: gwy._transport.get_extra_info(SZ_IS_EVOFW3),
        }

    @property
    def is_on(self) -> bool:
        """Return True if the gateway has received messages recently."""
        msg = self._device._gwy._this_msg
        return not bool(msg and dt.now() - msg.dtm < timedelta(seconds=300))


@dataclass(frozen=True, kw_only=True)
class RamsesBinarySensorEntityDescription(
    RamsesEntityDescription, BinarySensorEntityDescription
):
    """Class describing Ramses binary sensor entities."""

    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    icon_off: str | None = None

    # integration-specific attributes
    ramses_cc_class: type[RamsesBinarySensor] = RamsesBinarySensor
    ramses_rf_attr: str
    ramses_rf_class: type[RamsesRFEntity] | UnionType = RamsesRFEntity


BINARY_SENSOR_DESCRIPTIONS: tuple[RamsesBinarySensorEntityDescription, ...] = (
    RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="id",
        name="Gateway status",
        ramses_rf_class=HgiGateway,
        ramses_cc_class=RamsesGatewayBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="id",
        name="System status",
        ramses_rf_class=System,
        ramses_cc_class=RamsesSystemBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
        ramses_cc_extra_attributes={
            ATTR_WORKING_SCHEMA: SZ_SCHEMA,
        },
    ),
    RamsesBinarySensorEntityDescription(
        key=TrvActuator.WINDOW_OPEN,
        ramses_rf_attr=TrvActuator.WINDOW_OPEN,
        name="Window open",
        device_class=BinarySensorDeviceClass.WINDOW,
    ),
    RamsesBinarySensorEntityDescription(
        key=BdrSwitch.ACTIVE,
        ramses_rf_attr=BdrSwitch.ACTIVE,
        name="Active",
        icon="mdi:electric-switch-closed",
        icon_off="mdi:electric-switch",
        entity_category=None,
    ),
    RamsesBinarySensorEntityDescription(
        key=BatteryState.BATTERY_LOW,
        ramses_rf_attr=BatteryState.BATTERY_LOW,
        ramses_cc_class=RamsesBatteryBinarySensor,
        device_class=BinarySensorDeviceClass.BATTERY,
    ),
    RamsesBinarySensorEntityDescription(
        key="active_fault",
        name="Active fault",
        ramses_rf_class=Logbook,
        ramses_rf_attr="active_faults",
        ramses_cc_class=RamsesLogbookBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
        ramses_cc_extra_attributes={
            ATTR_ACTIVE_FAULTS: "active_faults",
            ATTR_LATEST_EVENT: "latest_event",
            ATTR_LATEST_FAULT: "latest_fault",
        },
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_CH_ACTIVE,
        ramses_rf_attr=SZ_CH_ACTIVE,
        name="CH active",
        icon="mdi:radiator",
        icon_off="mdi:radiator-off",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_CH_ENABLED,
        ramses_rf_attr=SZ_CH_ENABLED,
        name="CH enabled",
        icon="mdi:radiator",
        icon_off="mdi:radiator-off",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_COOLING_ACTIVE,
        ramses_rf_attr=SZ_COOLING_ACTIVE,
        name="Cooling active",
        icon="mdi:snowflake",
        icon_off="mdi:snowflake-off",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_COOLING_ENABLED,
        ramses_rf_attr=SZ_COOLING_ENABLED,
        name="Cooling enabled",
        icon_off="mdi:snowflake-off",
        icon="mdi:snowflake",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_DHW_ACTIVE,
        ramses_rf_attr=SZ_DHW_ACTIVE,
        name="DHW active",
        icon_off="mdi:water-off",
        icon="mdi:water",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_DHW_ENABLED,
        ramses_rf_attr=SZ_DHW_ENABLED,
        name="DHW enabled",
        icon_off="mdi:water-off",
        icon="mdi:water",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_FLAME_ACTIVE,
        ramses_rf_attr=SZ_FLAME_ACTIVE,
        name="Flame active",
        icon="mdi:fire",
        icon_off="mdi:fire-off",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_DHW_BLOCKING,
        ramses_rf_attr=SZ_DHW_BLOCKING,
        name="DHW blocking",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_OTC_ACTIVE,
        ramses_rf_attr=SZ_OTC_ACTIVE,
        name="OTC active",
        icon="mdi:weather-snowy-heavy",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_SUMMER_MODE,
        ramses_rf_attr=SZ_SUMMER_MODE,
        name="Summer mode",
        icon="mdi:sun-clock",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_FAULT_PRESENT,
        ramses_rf_attr=SZ_FAULT_PRESENT,
        icon="mdi:alert",
        name="Fault present",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_BYPASS_POSITION,
        ramses_rf_attr=SZ_BYPASS_POSITION,
        name="Bypass position",
    ),
    # Special projects
    RamsesBinarySensorEntityDescription(
        key="bit_2_4",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="bit_2_4",
        name="Bit 2/4",
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key="bit_2_5",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="bit_2_5",
        name="Bit 2/5",
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key="bit_2_6",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="bit_2_6",
        name="Bit 2/6",
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key="bit_2_7",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="bit_2_7",
        name="Bit 2/7",
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key="bit_3_7",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="bit_3_7",
        name="Bit 3/7",
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key="bit_6_6",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="bit_6_6",
        name="Bit 6/6",
        entity_registry_enabled_default=False,
    ),
)

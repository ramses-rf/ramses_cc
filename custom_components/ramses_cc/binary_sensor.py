"""Support for RAMSES binary sensors."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from types import UnionType
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ramses_rf.devices import BdrSwitch, HgiGateway, OtbGateway, TrvActuator
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import SZ_CONFIG, SZ_SCHEMA
from ramses_rf.systems.tcs import Logbook, System
from ramses_tx.const import (
    SZ_BATTERY_LEVEL,
    SZ_BATTERY_LOW,
    SZ_BATTERY_STATE,
    SZ_BYPASS_POSITION,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_COOLING_ACTIVE,
    SZ_COOLING_ENABLED,
    SZ_DHW_ACTIVE,
    SZ_DHW_BLOCKING,
    SZ_DHW_ENABLED,
    SZ_FAULT_PRESENT,
    SZ_FLAME_ACTIVE,
    SZ_IS_EVOFW3,
    SZ_OTC_ACTIVE,
    SZ_SUMMER_MODE,
)
from ramses_tx.dtos import CommandDTO
from ramses_tx.schemas import SZ_KNOWN_LIST

from .const import (
    ATTR_ACTIVE_FAULTS,
    ATTR_LATEST_EVENT,
    ATTR_LATEST_FAULT,
    ATTR_WORKING_SCHEMA,
    DOMAIN,
)
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import resolve_async_attr

_LOGGER = logging.getLogger(__name__)


def _shrink_hints(device_hints: dict[str, Any]) -> dict[str, Any]:
    """Shrink hints to minimal required state.

    :param device_hints: Original hints dict.
    :type device_hints: dict[str, Any]
    :return: Minimised hints dict.
    :rtype: dict[str, Any]
    """
    return {
        k: v
        for k, v in device_hints.items()
        if k in ("alias", "class", "faked") and v not in (None, False)
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param entry: The configuration entry.
    :type entry: ConfigEntry
    :param async_add_entities: Callback to add entities.
    :type async_add_entities: AddEntitiesCallback
    """
    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    platform = entity_platform.async_get_current_platform()

    @callback
    def add_devices(
        devices: RamsesRFEntity | Sequence[RamsesRFEntity],
    ) -> None:
        """Add new devices to the platform.

        :param devices: A list of RAMSES RF devices to be added.
        :type devices: RamsesRFEntity | list[RamsesRFEntity]
        """
        device_list = devices if isinstance(devices, Sequence) else [devices]

        entities = [
            description.ramses_cc_class(coordinator, rf_device, description)
            for rf_device in device_list
            for description in BINARY_SENSOR_DESCRIPTIONS
            if isinstance(rf_device, description.ramses_rf_class)
            and hasattr(rf_device, description.ramses_rf_attr)
        ]
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)


class RamsesBinarySensor(RamsesEntity, BinarySensorEntity):
    """Representation of a Ramses binary sensor."""

    entity_description: RamsesBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: RamsesRFEntity,
        entity_description: RamsesBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor.

        :param coordinator: The integration coordinator.
        :type coordinator: RamsesCoordinator
        :param device: The underlying RAMSES RF device.
        :type device: RamsesRFEntity
        :param entity_description: The entity description to apply.
        :type entity_description: RamsesBinarySensorEntityDescription
        """
        _LOGGER.debug("Initializing %s: %s", device.id, entity_description.key)
        super().__init__(coordinator, device, entity_description)

        self._attr_unique_id = f"{device.id}-{entity_description.key}"
        self._last_known_state: bool | None = None

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor.

        :return: The state of the sensor, or None if unknown.
        :rtype: bool | None
        """
        val = resolve_async_attr(
            self, self._device, self.entity_description.ramses_rf_attr
        )
        if val is not None:
            self._last_known_state = bool(val)
        return self._last_known_state

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any.

        :return: The appropriate string icon reference.
        :rtype: str | None
        """
        if self.is_on:
            return self.entity_description.icon
        return self.entity_description.icon_off


class RamsesBatteryBinarySensor(RamsesBinarySensor):
    """Representation of a Ramses Battery binary sensor."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes for BatteryState.
        For is_faked remotes, does not return battery state from real rem.

        :return: Dictionary of attributes or "N/A" for display in UI if empty
        :rtype: dict[str, Any]
        """
        state_dict = resolve_async_attr(self, self._device, SZ_BATTERY_STATE)
        level = "N/A" if state_dict is None else state_dict.get(SZ_BATTERY_LEVEL, "N/A")
        return super().extra_state_attributes | {SZ_BATTERY_LEVEL: level}


class RamsesBypassBinarySensor(RamsesBinarySensor):
    """Representation of a Ramses Bypass binary sensor."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: Dictionary of attributes.
        :rtype: dict[str, Any]
        """
        pos_as_float = resolve_async_attr(self, self._device, SZ_BYPASS_POSITION)

        return super().extra_state_attributes | {SZ_BYPASS_POSITION: pos_as_float}


class RamsesLogbookBinarySensor(RamsesBinarySensor):
    """Representation of a fault log."""

    _device: Logbook

    async def async_added_to_hass(self) -> None:
        """Called when entity is added to Home Assistant."""
        await super().async_added_to_hass()
        if resolve_async_attr(self, self._device, "active_faults") is None:
            try:
                tcs = getattr(self._device, "_tcs", None)
                if tcs and hasattr(tcs, "get_faultlog"):
                    await tcs.get_faultlog(limit=1, force_refresh=True)
                elif tcs and hasattr(tcs, "_gwy"):
                    cmd = CommandDTO(
                        verb="RQ",
                        addr1="18:000730",
                        addr2=tcs.id,
                        addr3="--:------",
                        code="0418",
                        payload="00",
                    )
                    await tcs._gwy.async_send_cmd(cmd)
            except Exception as err:
                _LOGGER.debug(
                    "Failed to poll active_faults for %s: %s",
                    self.entity_id,
                    err,
                )

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor.

        ``active_faults`` returns ``None`` when the fault log is empty or not
        yet loaded — both mean 'no active faults', so report ``False`` (off)
        rather than ``unknown`` (issue 841).

        :return: True if faults are active, False otherwise.
        :rtype: bool | None
        """
        faults = resolve_async_attr(self, self._device, "active_faults")
        self._last_known_state = bool(faults)
        return self._last_known_state


class RamsesSystemBinarySensor(RamsesBinarySensor):
    """Legacy representation of a system for EvoControl compatibility.

    NOTE: This entity exists purely to serve the working_schema JSON to the
    EvoControl Wi-Fi display. It evaluates to False (STATE_OFF) to indicate
    a healthy system. For actual fault detection, use the active_fault sensor.
    """

    _device: System

    @property
    def is_on(self) -> bool | None:
        """Return False (STATE_OFF) to satisfy EvoControl's health check.

        :return: False if system is present, None if unknown.
        :rtype: bool | None
        """
        is_on = super().is_on
        return None if is_on is None else not is_on


class RamsesGatewayBinarySensor(RamsesBinarySensor):
    """Representation of a gateway (a HGI80 or substitute)."""

    _device: HgiGateway
    _cached_attrs: dict[str, Any] | None = None
    _last_known_list_size: int = -1

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific gateway state attributes.

        :return: Dictionary of attributes for the gateway.
        :rtype: dict[str, Any]
        """
        gwy: Gateway = self._device._gwy
        engine = getattr(gwy, "_engine", None)
        gwy_config = getattr(gwy, "config", getattr(gwy, "_gwy_config", None))

        # TODO Q3 2026: return await gwy._config() (only) instead of all below
        # code
        # not yet working: self._cached_attrs = await gwy._config()
        known_list: Any = getattr(gwy_config, "known_list", None)
        if not isinstance(known_list, dict):
            fallback = getattr(engine, "_include", None)
            if not isinstance(fallback, dict):
                fallback = getattr(gwy, "_include", {})
            known_list = fallback if isinstance(fallback, dict) else {}

        enforce_kl: bool | None = getattr(engine, "_enforce_known_list", None)
        if not isinstance(enforce_kl, bool):
            enforce_kl = getattr(gwy, "_enforce_known_list", None)

        transport = getattr(engine, "_transport", None)
        if not transport:
            transport = getattr(gwy, "_transport", None)

        current_size = len(known_list)

        if self._cached_attrs is None or current_size != self._last_known_list_size:
            tcs_schema: dict[str, Any] = {}
            if gwy.tcs:
                schema_min = resolve_async_attr(self, gwy.tcs, "_schema_min")
                if schema_min is not None:
                    tcs_schema = {gwy.tcs.id: schema_min}

            evo_fw3 = None
            if transport:
                evo_fw3 = transport.get_extra_info(SZ_IS_EVOFW3)

            self._cached_attrs = {
                SZ_SCHEMA: tcs_schema,
                SZ_CONFIG: {"enforce_known_list": enforce_kl},
                SZ_KNOWN_LIST: [{k: _shrink_hints(v)} for k, v in known_list.items()],
                SZ_IS_EVOFW3: evo_fw3,
            }
            self._last_known_list_size = current_size

        return super().extra_state_attributes | self._cached_attrs

    @property
    def available(self) -> bool:
        """Always True, since we always have an HGI gateway."""
        # must override super Entity is_on
        return True

    @property
    def is_on(self) -> bool | None:
        """Return True if the gateway has a problem (no recent messages).

        `is_active` returns True when the gateway is healthy (recent
        messages received).  Since this sensor uses
        `BinarySensorDeviceClass.PROBLEM`, we invert so that `is_on=True`
        means "problem" and `is_on=False` means "OK".

        :return: True if there is a problem, None if unknown.
        :rtype: bool | None
        """
        is_on = super().is_on
        return None if is_on is None else not is_on


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
        ramses_rf_attr="is_active",
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
        key=SZ_BATTERY_LOW,
        ramses_rf_attr=SZ_BATTERY_LOW,
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
        ramses_cc_class=RamsesBypassBinarySensor,
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

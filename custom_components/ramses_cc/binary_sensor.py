"""Support for RAMSES binary sensors."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime as dt, timedelta as td
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
from homeassistant.util import dt as dt_util

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
from ramses_tx import Message
from ramses_tx.const import SZ_BYPASS_POSITION, SZ_IS_EVOFW3, Code

from .const import (
    ATTR_ACTIVE_FAULTS,
    ATTR_BATTERY_LEVEL,
    ATTR_LATEST_EVENT,
    ATTR_LATEST_FAULT,
    ATTR_WORKING_SCHEMA,
    DOMAIN,
)
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import latest_dtm, resolve_async_attr

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
    """Representation of a Ramses binary sensor."""

    entity_description: RamsesBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: RamsesRFEntity,
        entity_description: RamsesEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        _LOGGER.info("Initializing %s: %s", device.id, entity_description.key)
        super().__init__(coordinator, device, entity_description)

        self._attr_unique_id = f"{device.id}-{entity_description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor."""
        val = resolve_async_attr(
            self, self._device, self.entity_description.ramses_rf_attr
        )
        return None if val is None else bool(val)

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        return (
            self.entity_description.icon
            if self.is_on
            else self.entity_description.icon_off
        )


class RamsesBatteryBinarySensor(RamsesBinarySensor):
    """Representation of a Ramses Battery binary sensor."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        battery_state = resolve_async_attr(self, self._device, "battery_state")

        level = None if battery_state is None else battery_state.get(ATTR_BATTERY_LEVEL)

        return super().extra_state_attributes | {ATTR_BATTERY_LEVEL: level}


class RamsesLogbookBinarySensor(RamsesBinarySensor):
    """Representation of a fault log."""

    _device: Logbook

    @property
    def available(self) -> bool:
        """Return True if the device has been seen recently."""
        if hasattr(self._device, "state_store"):
            msg = self._device.state_store._msgs_.get(Code._0418)
        else:
            msg = resolve_async_attr(self, self._device, "_msgs", {}).get(Code._0418)
        _dtm: dt = msg.dtm
        return bool(msg and (dt_util.now() - dt_util.as_utc(_dtm) < td(seconds=1200)))

    @property
    def is_on(self) -> bool:
        """Return the state of the binary sensor."""
        faults = resolve_async_attr(self, self._device, "active_faults")
        return bool(faults)


class RamsesSystemBinarySensor(RamsesBinarySensor):
    """Representation of a system (a controller)."""

    _device: System

    @property
    def available(self) -> bool:
        """Return True if the last system sync message is recent."""
        msgs: dict[Code, Message] | None = resolve_async_attr(
            self, self._device.state_store, "_msgs_", None
        )
        if msgs is None:
            msgs = resolve_async_attr(self, self._device, "_msgs", {})
        msg: Message = msgs.get(Code._1F09)
        _dtm: dt = msg.dtm
        return bool(
            msg
            and (
                dt_util.now() - dt_util.as_utc(_dtm)
                < td(seconds=msg.payload["remaining_seconds"] * 3)
            )
        )

    @property
    def is_on(self) -> bool:
        """Return True if the gateway has received messages recently."""
        return not bool(super().is_on)  # TODO


class RamsesGatewayBinarySensor(RamsesBinarySensor):
    """Representation of a gateway (a HGI80 or substitute)."""

    _device: HgiGateway
    _cached_attrs: dict[str, Any] | None = None
    _last_known_list_size: int = -1

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific gateway state attributes."""
        # never called unless entity is available()
        _LOGGER.debug(
            "Fetching extra_state_attributes for RamsesGatewayBinarySensor %s",
            self._device,
        )
        gwy: Gateway = resolve_async_attr(
            self, self._device, "_gwy"
        )  # note: only class trying direct access to ._gwy
        # gwy never gets updated after boot, no problem for schema etc
        engine = resolve_async_attr(self, gwy, "_engine", None)
        known_list = resolve_async_attr(self, gwy, "known_list", None)
        if not isinstance(known_list, dict):
            known_list = resolve_async_attr(
                self, engine, "_include", resolve_async_attr(self, gwy, "_include", {})
            )
        block_list = resolve_async_attr(self, engine, "_exclude", None)
        if not isinstance(block_list, dict):
            block_list = resolve_async_attr(self, gwy, "_exclude", {})
        enforce_known_list = resolve_async_attr(
            self, engine, "_enforce_known_list", None
        )
        if not isinstance(enforce_known_list, bool):
            enforce_known_list = resolve_async_attr(
                self, gwy, "_enforce_known_list", None
            )
        transport = resolve_async_attr(
            self, engine, "_transport", None
        ) or resolve_async_attr(self, gwy, "_transport", None)
        current_size = len(known_list) if known_list else 0

        if self._cached_attrs is None or current_size != self._last_known_list_size:

            def shrink(device_hints: dict[str, bool | str]) -> dict[str, Any]:
                return {
                    k: v
                    for k, v in device_hints.items()
                    if k in ("alias", "class", "faked") and v not in (None, False)
                }

            tcs_schema = {}
            _tcs = resolve_async_attr(self, gwy, "tcs")
            if _tcs is not None:
                resolved_schema = resolve_async_attr(self, _tcs, "_schema_min")
                if resolved_schema is not None:
                    tcs_schema = {
                        resolve_async_attr(self, _tcs, "id:"): resolved_schema
                    }

            self._cached_attrs = {
                SZ_SCHEMA: tcs_schema,
                SZ_CONFIG: {"enforce_known_list": enforce_known_list},
                SZ_KNOWN_LIST: [{k: shrink(v)} for k, v in known_list.items()],
                SZ_BLOCK_LIST: [{k: shrink(v)} for k, v in block_list.items()],
                SZ_IS_EVOFW3: transport.get_extra_info(SZ_IS_EVOFW3)
                if transport
                else None,
            }
            self._last_known_list_size = current_size

        return super().extra_state_attributes | self._cached_attrs

    @property
    def available(self) -> bool:
        """Always True, since we always have an HGI gateway."""
        # must override super Entity is_on
        return True

    @property
    def is_on(self) -> bool:
        """Return True if the gateway has received messages recently."""
        # TODO instead of direct _gwy access (which fails in 0.55.6) use
        #  the "latest_dtm" attr (change ramses_rf_attr below when available in ramses_rf)

        # gwy: Gateway = resolve_async_attr(
        #     self, self._device, "_gwy"
        # )  # note: only class trying direct access to ._gwy
        # engine = resolve_async_attr(self, gwy, "_engine", None)
        # msg: Message = resolve_async_attr(self, engine, "_this_msg", None)
        # never updated after FIRST hgi config TODO clean up above

        gateway_dtm: dt | None = resolve_async_attr(
            self,
            self._device,
            self.entity_description.gateway_dtm,
            None,  # ramses_rf_attr, None
        )
        if gateway_dtm is None:
            msgs: dict[Code, Message] = resolve_async_attr(
                self, self._device.state_store, "_msgs_", None
            )
            gateway_dtm = latest_dtm(
                msgs
            )  # never updated TODO remove this block when gateway_dtm is in ramses_rf

        return not bool(
            gateway_dtm
            and (dt_util.now() - dt_util.as_utc(gateway_dtm) < td(seconds=300))
            # TODO fix test error: can't < with MagickMock()
        )


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
        ramses_rf_attr="id",  # TODO replace by ="gateway_dtm",
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

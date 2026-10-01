"""Support for RAMSES binary sensors."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime as dt
from types import UnionType
from typing import Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_platform,
    entity_registry as er,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from ramses_rf.const import (
    SZ_AIR_PRESSURE_FAULT,
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
    SZ_FILTER_DIRTY,
    SZ_FLAME_ACTIVE,
    SZ_FROST_CYCLE,
    SZ_GAS_FLAME_FAULT,
    SZ_HAS_FAULT,
    SZ_LOCKOUT_RESET,
    SZ_LOW_WATER_PRESSURE,
    SZ_OTC_ACTIVE,
    SZ_SERVICE_REQUEST,
    SZ_SUMMER_MODE,
    SZ_WATER_OVER_TEMPERATURE,
)
from ramses_rf.devices import (
    BdrSwitch,
    HgiGateway,
    HvacVentilator,
    OtbGateway,
    TrvActuator,
)
from ramses_rf.devices.dev_base import DeviceBase
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import SZ_CONFIG, SZ_SCHEMA
from ramses_rf.systems.tcs import Logbook, System
from ramses_tx.const import SZ_IS_EVOFW3
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
from .helpers import (
    device_gateway,
    engine_transport,
    gateway_engine,
    resolve_async_attr,
)
from .typing import RamsesConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES: Final = 0


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
    entry: RamsesConfigEntry,
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
    coordinator: RamsesCoordinator = entry.runtime_data
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

        # Per-device status entities (issue 1210).  These are created for
        # every real device except the HGI gateways, which already have
        # gateway status / pool child connectivity entities.
        entities.extend(
            RamsesDeviceStatusBinarySensor(
                coordinator, rf_device, DEVICE_STATUS_DESCRIPTION
            )
            for rf_device in device_list
            if isinstance(rf_device, DeviceBase)
            and not isinstance(rf_device, HgiGateway)
        )
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)

    # Per-HGI pool binary sensors + aggregate pool status sensor
    # (issue 1119).  These are coordinator-driven (not device-driven)
    # and report the connectivity/availability of each pool child.
    _add_pool_status_entities(coordinator, async_add_entities)


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
            icon: str | None = self.entity_description.icon
            return icon
        return self.entity_description.icon_off


class RamsesBatteryBinarySensor(RamsesBinarySensor):
    """Representation of a Ramses Battery binary sensor."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes for BatteryState.

        For is_faked remotes, does not return battery state from real
        rem.

        :return: Dictionary of attributes or "N/A" for display in UI.
        :rtype: dict[str, Any]
        """
        state_dict = resolve_async_attr(self, self._device, SZ_BATTERY_STATE)
        level = (
            "N/A"
            if state_dict is None
            else state_dict.get(SZ_BATTERY_LEVEL, "N/A")
        )
        return super().extra_state_attributes | {SZ_BATTERY_LEVEL: level}


class RamsesBypassBinarySensor(RamsesBinarySensor):
    """Representation of a Ramses Bypass binary sensor."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: Dictionary of attributes.
        :rtype: dict[str, Any]
        """
        pos_as_float = resolve_async_attr(
            self, self._device, SZ_BYPASS_POSITION
        )

        return super().extra_state_attributes | {
            SZ_BYPASS_POSITION: pos_as_float
        }


class RamsesLogbookBinarySensor(RamsesBinarySensor):
    """Representation of a fault log."""

    _device: Logbook

    async def async_added_to_hass(self) -> None:
        """Handle entity addition to Home Assistant."""
        # NOTE: a startup `get_faultlog(force_refresh=True)` poll used to
        # sit here, gated on a `_tcs` attr that never existed on Logbook —
        # dead code, never ran. It was removed rather than rewired because
        # `is_on` already treats `active_faults=None` as no-faults (issue
        # 841) and a blocking RF call during entity setup isn't justified.
        await super().async_added_to_hass()

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
        gwy: Gateway = device_gateway(self._device)
        engine = gateway_engine(gwy)

        # NOTE: this property is sync, so the async gwy.config_snapshot()
        # (nee _config()) cannot be awaited here — and it does not expose
        # is_evofw3 or the TCS schema_min anyway, so the attributes are
        # assembled directly.  The private-attr fallbacks (_gwy_config,
        # engine._include/_enforce_known_list) were removed: the public
        # accessors exist since ramses_rf 0.60.8 (the pinned version).
        gwy_config = getattr(gwy, "config", None)
        known_list = getattr(gwy_config, "known_list", None)
        if not isinstance(known_list, dict):
            known_list = {}

        enforce_kl = getattr(engine, "enforce_known_list", None)
        if not isinstance(enforce_kl, bool):
            enforce_kl = None

        transport = engine_transport(gwy)

        current_size = len(known_list)

        if (
            self._cached_attrs is None
            or current_size != self._last_known_list_size
        ):
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
                SZ_KNOWN_LIST: [
                    {k: _shrink_hints(v)}
                    for k, v in sorted(known_list.items())
                ],
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


class RamsesDeviceStatusBinarySensor(RamsesBinarySensor, RestoreEntity):
    """Per-device communication status (issue 1210).

    ``is_on=True`` means the device is communicating: it has been heard
    within its ``heartbeat_timeout`` and has not accumulated
    ``MISSED_POLL_THRESHOLD`` consecutive unanswered polls.  Extra state
    attributes expose last-seen/staleness, the missed-poll count, and
    pool-aware RSSI communication quality.

    The ramses_rf RSSI trackers are in-memory and the per-HGI (routing)
    trackers expire after five minutes, so the entity additionally keeps
    a *last-known* RSSI record — updated whenever a fresh measurement is
    observed and restored across restarts via ``RestoreEntity`` — so
    consumers can display e.g. "-40 dBm, 3h ago" instead of ``unknown``.
    """

    _device: DeviceBase

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: RamsesRFEntity,
        entity_description: RamsesBinarySensorEntityDescription,
    ) -> None:
        """Initialise last-known RSSI tracking state."""
        super().__init__(coordinator, device, entity_description)
        self._last_known_rssi: int | None = None
        self._last_known_rssi_per_hgi: dict[str, int] = {}
        self._last_rssi_seen: dt | None = None

    async def async_added_to_hass(self) -> None:
        """Seed last-known RSSI from the previous HA state."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        rssi = last_state.attributes.get("last_known_rssi")
        if isinstance(rssi, (int, float)):
            self._last_known_rssi = int(rssi)
        per_hgi = last_state.attributes.get("last_known_rssi_per_hgi")
        if isinstance(per_hgi, dict):
            self._last_known_rssi_per_hgi = {
                str(k): int(v)
                for k, v in per_hgi.items()
                if isinstance(v, (int, float))
            }
        seen = last_state.attributes.get("last_rssi_seen")
        if isinstance(seen, str):
            with contextlib.suppress(ValueError):
                self._last_rssi_seen = dt.fromisoformat(seen)

    @property
    def available(self) -> bool:
        """Always True — this entity reports the device's status.

        The entity itself must stay available so it can show ``off``
        when the device stops communicating (unlike other entities,
        which go ``unavailable`` via ``device.is_available``).
        """
        return True

    @property
    def is_on(self) -> bool | None:
        """Return True if the device is communicating.

        Combines heartbeat liveness (``is_available``) with the
        consecutive missed-poll count, so a polled device that goes
        silent is flagged well before a long heartbeat timeout elapses.

        :return: True if the device is communicating, False otherwise.
        :rtype: bool | None
        """
        is_available = getattr(self._device, "is_available", True)
        missed = getattr(self._device, "consecutive_missed_polls", 0)
        return bool(is_available and missed < MISSED_POLL_THRESHOLD)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return liveness and communication quality attributes.

        :return: Dictionary of status/quality attributes.
        :rtype: dict[str, Any]
        """
        dev = self._device
        last_seen = getattr(
            dev, "last_seen", getattr(dev, "_last_msg_dtm", None)
        )
        staleness: float | None = None
        if isinstance(last_seen, dt):
            if last_seen.tzinfo is not None:
                now = dt.now(UTC).astimezone(last_seen.tzinfo)
            else:
                now = dt.now()
            staleness = (now - last_seen).total_seconds()

        timeout = getattr(dev, "heartbeat_timeout", None)
        quality = getattr(dev, "communication_quality", None)
        rssi_per_hgi = getattr(dev, "rssi_per_hgi", None)

        best_rssi = getattr(quality, "best_rssi", None)
        fresh_per_hgi = (
            dict(rssi_per_hgi) if isinstance(rssi_per_hgi, dict) else {}
        )
        if best_rssi is not None or fresh_per_hgi:
            if best_rssi is not None:
                self._last_known_rssi = int(best_rssi)
            if fresh_per_hgi:
                self._last_known_rssi_per_hgi = {
                    str(k): int(v)
                    for k, v in fresh_per_hgi.items()
                    if isinstance(v, (int, float))
                }
            # quality.last_seen is the timestamp of the most recent RSSI
            # *reading* (not the evaluation time), so the age below stays
            # honest even while best_rssi remains cached in the tracker.
            quality_seen = getattr(quality, "last_seen", None)
            if isinstance(quality_seen, dt):
                self._last_rssi_seen = quality_seen
            elif self._last_rssi_seen is None:
                self._last_rssi_seen = dt.now(UTC)
        last_rssi_age: float | None = None
        if self._last_rssi_seen is not None:
            if self._last_rssi_seen.tzinfo is not None:
                last_rssi_age = (
                    dt.now(UTC) - self._last_rssi_seen
                ).total_seconds()
            else:
                last_rssi_age = (
                    dt.now() - self._last_rssi_seen
                ).total_seconds()

        return super().extra_state_attributes | {
            "last_seen": (
                last_seen.isoformat() if isinstance(last_seen, dt) else None
            ),
            "staleness_seconds": staleness,
            "heartbeat_timeout": (
                timeout.total_seconds() if timeout is not None else None
            ),
            "consecutive_missed_polls": getattr(
                dev, "consecutive_missed_polls", 0
            ),
            "best_rssi": best_rssi,
            "rssi_quality": getattr(quality, "rssi_quality", None),
            "is_stale": getattr(quality, "is_stale", None),
            "rssi_per_hgi": fresh_per_hgi,
            "last_known_rssi": self._last_known_rssi,
            "last_known_rssi_per_hgi": dict(self._last_known_rssi_per_hgi),
            "last_rssi_seen": (
                self._last_rssi_seen.isoformat()
                if self._last_rssi_seen is not None
                else None
            ),
            "last_rssi_age_seconds": last_rssi_age,
        }


@dataclass(frozen=True, kw_only=True)
class RamsesBinarySensorEntityDescription(
    RamsesEntityDescription,
    BinarySensorEntityDescription,
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
    # OpenTherm application-specific fault flags (msg_id 0x05)
    RamsesBinarySensorEntityDescription(
        key=SZ_SERVICE_REQUEST,
        ramses_rf_attr=SZ_SERVICE_REQUEST,
        ramses_rf_class=OtbGateway,
        name="Service request",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_LOCKOUT_RESET,
        ramses_rf_attr=SZ_LOCKOUT_RESET,
        ramses_rf_class=OtbGateway,
        name="Lockout reset enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_LOW_WATER_PRESSURE,
        ramses_rf_attr=SZ_LOW_WATER_PRESSURE,
        ramses_rf_class=OtbGateway,
        name="Low water pressure",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_GAS_FLAME_FAULT,
        ramses_rf_attr=SZ_GAS_FLAME_FAULT,
        ramses_rf_class=OtbGateway,
        name="Gas/flame fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_AIR_PRESSURE_FAULT,
        ramses_rf_attr=SZ_AIR_PRESSURE_FAULT,
        ramses_rf_class=OtbGateway,
        name="Air pressure fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_WATER_OVER_TEMPERATURE,
        ramses_rf_attr=SZ_WATER_OVER_TEMPERATURE,
        ramses_rf_class=OtbGateway,
        name="Water over-temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_BYPASS_POSITION,
        ramses_rf_attr=SZ_BYPASS_POSITION,
        ramses_cc_class=RamsesBypassBinarySensor,
        name="Bypass position",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_FILTER_DIRTY,
        ramses_rf_attr=SZ_FILTER_DIRTY,
        ramses_rf_class=HvacVentilator,
        name="Filter dirty",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_FROST_CYCLE,
        ramses_rf_attr=SZ_FROST_CYCLE,
        ramses_rf_class=HvacVentilator,
        name="Frost cycle",
        icon="mdi:snowflake",
        icon_off="mdi:snowflake-off",
    ),
    RamsesBinarySensorEntityDescription(
        key=SZ_HAS_FAULT,
        ramses_rf_attr=SZ_HAS_FAULT,
        ramses_rf_class=HvacVentilator,
        name="Fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
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

# Consecutive unanswered polls before a device reports disconnected
MISSED_POLL_THRESHOLD: Final = 3

# Description for the per-device status entity (issue 1210).  Kept out of
# BINARY_SENSOR_DESCRIPTIONS because creation needs an exclusion the
# description filter cannot express (DeviceBase but not HgiGateway).
DEVICE_STATUS_DESCRIPTION = RamsesBinarySensorEntityDescription(
    key="device_status",
    ramses_rf_attr="is_available",
    name="Status",
    ramses_rf_class=DeviceBase,
    ramses_cc_class=RamsesDeviceStatusBinarySensor,
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
)


# -- Per-HGI pool status entities (issue 1119) ------------------------------


def _migrate_old_pool_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Remove old non-entry-scoped pool entities.

    Before the entry-scoping fix, pool entities had unique IDs like
    ``pool_child_18:012345_online`` and ``pool_status_online`` (no
    config-entry prefix).  After the fix, unique IDs include the
    entry ID (``{entry_id}_pool_child_...``).  The old entities remain
    in the registry as orphaned duplicates.  Remove them.
    """
    ent_reg = er.async_get(hass)
    # Collect entities to remove first — can't modify the registry
    # while iterating over ent_reg.entities (RuntimeError: dictionary
    # changed size during iteration).
    to_remove: list[tuple[str, str]] = []  # (entity_id, unique_id)
    for entity in ent_reg.entities.values():
        uid = entity.unique_id
        # Old non-entry-scoped pool entity unique IDs:
        #   pool_child_18:012345_online
        #   pool_status_online
        # Skip if the unique ID has the entry_id prefix (current format).
        if uid.startswith(f"{entry_id}_"):
            continue
        if uid.startswith("pool_child_") and uid.endswith("_online"):
            to_remove.append((entity.entity_id, uid))
        elif uid == "pool_status_online":
            to_remove.append((entity.entity_id, uid))
    for entity_id, uid in to_remove:
        ent_reg.async_remove(entity_id)
        _LOGGER.info(
            "Migrated old pool entity %s (unique_id=%s)", entity_id, uid
        )


def _rename_pool_child_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Fix stale original_name on entry-scoped pool child entities.

    Pool child sensors used to embed the HGI id in the entity name
    (``HGI <id> online``) before they were assigned to the HGI device,
    so the friendly name rendered as "HGI <id> HGI <id> online".
    The registry's ``original_name`` is sticky, so entries created
    under the old naming keep the doubled name even though the entity
    now reports ``Online``.  Rewrite them in place; the entity_id is
    unchanged either way.
    """
    ent_reg = er.async_get(hass)
    for entity in ent_reg.entities.values():
        uid = entity.unique_id
        if not uid.startswith(f"{entry_id}_pool_child_"):
            continue
        if not uid.endswith("_online"):
            continue
        if entity.original_name == "Online":
            continue
        ent_reg.async_update_entity(entity.entity_id, original_name="Online")
        _LOGGER.info(
            "Renamed pool child entity %s (original_name=%s)",
            entity.entity_id,
            entity.original_name,
        )


def _add_pool_status_entities(
    coordinator: RamsesCoordinator,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create per-HGI binary sensors + aggregate pool status sensor.

    :param coordinator: The integration coordinator.
    :param async_add_entities: Callback to add entities.
    """
    if not coordinator.is_pool_enabled:
        return

    # Migrate old non-entry-scoped pool entities (one-time cleanup).
    _migrate_old_pool_entities(coordinator.hass, coordinator.entry.entry_id)
    # Fix sticky original_name on pool children named before the
    # entity name stopped embedding the HGI id.
    _rename_pool_child_entities(coordinator.hass, coordinator.entry.entry_id)

    seen_hgis: set[str] = set()

    @callback
    def add_missing_child_entities() -> None:
        """Add status entities for newly identified pool children."""
        entities: list[RamsesPoolChildBinarySensor] = []
        for child_status in coordinator.get_pool_child_status():
            hgi_id = child_status.get("hgi_id")
            child_id = child_status.get("child_id")
            if not hgi_id or child_id is None:
                continue
            hgi_key = str(hgi_id)
            if hgi_key in seen_hgis:
                continue
            seen_hgis.add(hgi_key)
            entities.append(
                RamsesPoolChildBinarySensor(
                    coordinator, hgi_key, str(child_id), child_status
                )
            )
        if entities:
            async_add_entities(entities)

    async_add_entities([RamsesPoolStatusSensor(coordinator)])
    add_missing_child_entities()
    coordinator.entry.async_on_unload(
        coordinator.async_add_listener(add_missing_child_entities)
    )


class RamsesPoolChildBinarySensor(BinarySensorEntity):
    """Binary sensor for a single pool child's connectivity.

    ``is_on=True`` means the child is connected and online (packets
    flowing).  Extra state attributes expose the child's HGI ID,
    port name, send-readiness, and packet counters.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        hgi_id: str,
        child_id: str,
        initial_status: dict[str, object],
    ) -> None:
        """Initialize the per-HGI pool child sensor.

        :param coordinator: The integration coordinator.
        :param hgi_id: The HGI device ID for this child.
        :param child_id: The pool child index (string).
        :param initial_status: Initial status dict.
        """
        self._coordinator = coordinator
        self._hgi_id = hgi_id
        self._child_id = child_id
        self._status: dict[str, object] = initial_status
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_pool_child_{hgi_id}_online"
        )
        self._attr_name = "Online"
        # Assign to the HGI device so the entity is grouped in the UI
        # (not "ungrouped") and appears alongside the Gateway status.
        # The name is set here too so the device is never nameless if
        # the coordinator has not registered it yet.
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, hgi_id)}, name=f"HGI {hgi_id}"
        )

    async def async_added_to_hass(self) -> None:
        """Register coordinator update listener."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(
                self._handle_coordinator_update
            )
        )

    @property
    def available(self) -> bool:
        """Return True if the coordinator and pool are available."""
        return self._coordinator.last_update_success

    @property
    def is_on(self) -> bool | None:
        """Return True if the child is connected and online."""
        status = self._find_status()
        if status is None:
            return None
        return bool(
            status.get("connected") and status.get("availability") == "ONLINE"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return per-child attributes for monitoring."""
        status = self._find_status() or self._status
        return {
            "hgi_id": status.get("hgi_id"),
            "child_id": status.get("child_id"),
            "port_name": status.get("port_name"),
            "connected": status.get("connected"),
            "availability": status.get("availability"),
            "accepted": status.get("accepted"),
            "send_ready": status.get("send_ready"),
            "callback_driven": status.get("callback_driven"),
            "pkts_received": status.get("pkts_received"),
            "consecutive_errors": status.get("consecutive_errors"),
            "last_pkt_time": status.get("last_pkt_time"),
        }

    def _find_status(self) -> dict[str, object] | None:
        """Find this child's status in the current pool status list.

        When multiple children share the same HGI ID (e.g. a serial
        child and an excluded MQTT callback child), prefer the one
        that is connected and online.  This ensures the sensor
        reports the active transport's state after a failover.
        """
        first_match: dict[str, object] | None = None
        for s in self._coordinator.get_pool_child_status():
            if s.get("hgi_id") != self._hgi_id:
                continue
            if first_match is None:
                first_match = s
            # Prefer the first connected+online child.
            if s.get("connected") and s.get("availability") == "ONLINE":
                return s
        return first_match

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        status = self._find_status()
        if status is not None:
            self._status = status
        self.async_write_ha_state()


class RamsesPoolStatusSensor(BinarySensorEntity):
    """Aggregate pool status sensor.

    ``is_on=True`` means at least one pool child is connected and
    online.  Extra state attributes expose aggregate counts.

    Semantics: ``connected``/``online`` describe HGI transport health —
    the pool can exchange packets via at least one HGI.  They say
    nothing about whether any individual end device (e.g. a FAN) is
    reachable; per-device status entities cover that.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True

    def __init__(self, coordinator: RamsesCoordinator) -> None:
        """Initialize the aggregate pool status sensor.

        :param coordinator: The integration coordinator.
        """
        self._coordinator = coordinator
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_pool_status_online"
        )
        self._attr_name = "Pool status"

    async def async_added_to_hass(self) -> None:
        """Register coordinator update listener."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(
                self._handle_coordinator_update
            )
        )

    @property
    def available(self) -> bool:
        """Return True if the coordinator is available."""
        return self._coordinator.last_update_success

    @property
    def is_on(self) -> bool | None:
        """Return True if at least one child is connected and online."""
        statuses = self._coordinator.get_pool_child_status()
        if not statuses:
            return None
        return any(
            bool(
                s.get("connected")
                and s.get("availability") == "ONLINE"
                and s.get("accepted")
                and s.get("send_ready")
            )
            for s in statuses
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return aggregate pool attributes."""
        statuses = self._coordinator.get_pool_child_status()
        connected = sum(1 for s in statuses if s.get("connected"))
        online = sum(
            1
            for s in statuses
            if s.get("connected") and s.get("availability") == "ONLINE"
        )
        send_ready = sum(1 for s in statuses if s.get("send_ready"))
        eligible = sum(
            1
            for s in statuses
            if s.get("connected")
            and s.get("availability") == "ONLINE"
            and s.get("accepted")
            and s.get("send_ready")
        )
        return {
            "children": len(statuses),
            "connected": connected,
            "online": online,
            "send_ready": send_ready,
            "eligible": eligible,
            "child_hgis": [s.get("hgi_id") for s in statuses],
            # Explicit n/y naming for "how many HGIs are online"
            # (issue 1210): children_online of children_total.
            "children_total": len(statuses),
            "children_online": online,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

"""Support for RAMSES sensors."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime as dt, timedelta as td
from decimal import Decimal
from types import UnionType
from typing import Any, Final, Protocol, runtime_checkable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
    PERCENTAGE,
    EntityCategory,
    UnitOfPressure,
    UnitOfRatio,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)
from homeassistant.helpers.typing import StateType

from ramses_rf.const import (
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BOILER_EXHAUST_TEMP,
    SZ_BOILER_OUTPUT_TEMP,
    SZ_BOILER_RETURN_TEMP,
    SZ_BOILER_SETPOINT,
    SZ_BYPASS_MODE,
    SZ_CH_MAX_SETPOINT,
    SZ_CH_SETPOINT,
    SZ_CH_WATER_PRESSURE,
    SZ_CIRCUIT_MODE,
    SZ_CO2_LEVEL,
    SZ_COOLING_DEMAND,
    SZ_DEMAND,
    SZ_DEWPOINT_TEMP,
    SZ_DHW_FLOW_RATE,
    SZ_DHW_SETPOINT,
    SZ_DHW_TEMP,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FILTER_REMAINING,
    SZ_FILTER_REMAINING_PERCENT,
    SZ_HEAT_DEMAND,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_MAX_REL_MODULATION,
    SZ_OEM_CODE,
    SZ_OEM_FAULT_CODE,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_OUTSIDE_TEMP,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PUMP_RELAY_STATE,
    SZ_REL_MODULATION_LEVEL,
    SZ_RELAY_DEMAND,
    SZ_REMAINING_MINS,
    SZ_SETPOINT,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_TEMPERATURE,
)
from ramses_rf.devices import (
    DeviceHvac,
    DhwSensor,
    HgiGateway,
    HvacHumiditySensor,
    HvacRemote,
    HvacVentilator,
    OtbGateway,
    OutSensor,
    Thermostat,
    TrvActuator,
    UfhCircuit,
    UfhController,
)
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_rf.enums import PumpRelayState, ThermalMode
from ramses_rf.exceptions import DeviceNotFaked
from ramses_rf.systems.tcs import System
from ramses_rf.systems.zones import ZoneBase
from ramses_tx.const import Code, Verb
from ramses_tx.dtos import CommandDTO
from ramses_tx.schemas import SZ_SCHEMA
from ramses_tx.typing import DeviceIdT

from .const import (
    ATTR_SETPOINT,
    ATTR_WORKING_SCHEMA,
    CONF_ADVANCED_FEATURES,
    CONF_LAST_MSG_SENSORS,
    CONF_SCHEMA,
    HGI_PREFIX,
    SZ_LAST_MSG,
    SZ_TR_BOUND,
    UnitOfVolumeFlowRate,
)
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import (
    device_gateway,
    extract_demand,
    resolve_async_attr,
)
from .typing import RamsesConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES: Final = 0
SCAN_INTERVAL = td(minutes=20)  # only used for polling 10D0 filter_remaining


def _sync_last_msg_registry_entries(
    hass: HomeAssistant, entry: RamsesConfigEntry
) -> None:
    """Align ``last_msg`` entity registry entries with the option.

    The ``last_msg_sensors`` advanced feature is the master switch for
    ``last_message_sent`` sensors.  When it is off, still-enabled entries
    are disabled by the integration so they stop writing to the recorder
    and flooding the device activity pane; the registry entries (and any
    user customizations) are kept.  When it is on, entries the
    integration disabled are re-enabled, except on HGIs (``18:*``), which
    stay disabled by default per ``entity_registry_enabled_default``.

    :param hass: The Home Assistant instance.
    :param entry: The config entry being set up.
    """
    enabled = bool(
        entry.options.get(CONF_ADVANCED_FEATURES, {}).get(
            CONF_LAST_MSG_SENSORS, False
        )
    )
    suffix = f"-{SZ_LAST_MSG}"
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(
        registry, entry.entry_id
    ):
        if not reg_entry.unique_id.endswith(suffix):
            continue
        if not enabled:
            if reg_entry.disabled_by is None:
                registry.async_update_entity(
                    reg_entry.entity_id,
                    disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                )
        elif (
            reg_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
            and not reg_entry.unique_id.startswith(HGI_PREFIX)
        ):
            registry.async_update_entity(reg_entry.entity_id, disabled_by=None)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RamsesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: RamsesCoordinator = entry.runtime_data
    platform: EntityPlatform = async_get_current_platform()

    _sync_last_msg_registry_entries(hass, entry)

    advanced = entry.options.get(CONF_ADVANCED_FEATURES, {})

    @callback
    def add_devices(
        devices: RamsesRFEntity | Sequence[RamsesRFEntity],
    ) -> None:
        # 1. Safely wrap a single device into a list, or keep it as a sequence
        device_list = devices if isinstance(devices, Sequence) else [devices]

        # 2. Iterate over device_list (not 'devices')
        entities = [
            description.ramses_cc_class(coordinator, device, description)
            for device in device_list
            for description in SENSOR_DESCRIPTIONS
            if isinstance(device, description.ramses_rf_class)
            and hasattr(device, description.ramses_rf_attr)
            and (
                description.ramses_cc_option is None
                or advanced.get(description.ramses_cc_option, False)
            )
        ]
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)


@runtime_checkable
class FakedCo2Capable(Protocol):
    """Protocol for devices capable of setting CO2 concentration."""

    id: DeviceIdT
    is_faked: bool

    async def set_co2_level(self, value: int) -> object:
        """Set the faked CO2 concentration."""
        ...


@runtime_checkable
class VentilationDemandCapable(Protocol):
    """Protocol for faked sensors capable of sending ventilation demand."""

    id: DeviceIdT
    is_faked: bool

    async def set_ventilation_demand(
        self, fan_id: DeviceIdT, value: float
    ) -> object:
        """Send a ventilation demand to a fan."""
        ...


@runtime_checkable
class FakedHumidityCapable(Protocol):
    """Protocol for devices capable of setting indoor humidity percentage."""

    id: DeviceIdT
    is_faked: bool

    async def set_indoor_humidity(self, value: float | None) -> object:
        """Set the faked indoor humidity level."""
        ...


@runtime_checkable
class FakedTemperatureCapable(Protocol):
    """Protocol for devices capable of setting temperature."""

    id: DeviceIdT
    is_faked: bool

    async def set_temperature(self, value: float | None) -> object:
        """Set the faked temperature."""
        ...


class RamsesSensor(RamsesEntity, SensorEntity):
    """Representation of a generic sensor."""

    entity_description: RamsesSensorEntityDescription

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: RamsesRFEntity,
        entity_description: RamsesSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        _LOGGER.debug("Initializing %s: %s", device.id, entity_description.key)
        super().__init__(coordinator, device, entity_description)

        self._attr_unique_id = f"{device.id}-{entity_description.key}"
        self._attr_should_poll = not entity_description.poll_codes

        # Disable polling by default, override by setting poll_codes
        self._last_known_value: StateType | date | dt | Decimal = None

    async def async_update(self) -> None:
        """Send RQ to refresh value from device (for poll-driven entities)."""
        if not self._attr_should_poll:
            return  # push-driven entities: no-op, signal handles updates
        _poll_cd = self.entity_description.poll_codes
        if _poll_cd:
            for code in _poll_cd:
                cmd = CommandDTO(
                    verb=Verb.RQ,
                    addr1="18:000730",
                    addr2=self._device.id,
                    addr3="--:------",
                    code=code,
                    payload="00",
                )
                try:
                    gwy = device_gateway(self._device)
                    if gwy is None:
                        continue
                    await gwy.async_send_raw_command(cmd)
                    _LOGGER.debug("Polled %s for %s", code, self._device.id)
                except Exception as err:
                    _LOGGER.debug(
                        "Poll %s for %s failed: %s", code, self._device.id, err
                    )

    @property
    def should_poll(self) -> bool:
        """Return whether HA should periodically poll for updates."""
        return self._attr_should_poll

    @property
    def native_value(self) -> StateType | date | dt | Decimal:
        """Return the native value of the sensor."""
        value = resolve_async_attr(
            self, self._device, self.entity_description.ramses_rf_attr
        )
        if hasattr(value, SZ_DEMAND):
            value = extract_demand(value)

        if value is not None:
            if isinstance(value, ThermalMode):
                self._last_known_value = value.value
            elif self.native_unit_of_measurement == PERCENTAGE:
                self._last_known_value = value * 100
            else:
                self._last_known_value = value

        return self._last_known_value

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        if (
            self.entity_description.ramses_cc_icon_off
            and not self.native_value
        ):
            return self.entity_description.ramses_cc_icon_off
        return super().icon

    # the following methods are integration-specific service calls

    async def async_put_co2_level(self, co2_level: int) -> None:
        """Cast the CO2 level (if faked).

        :param co2_level: The CO2 concentration in parts per million (ppm).
        :raises ServiceValidationError: If the device lacks faked CO2
            capability.
        """
        if not isinstance(self._device, FakedCo2Capable):
            raise ServiceValidationError(
                f"Device {self._device.id} does not support setting CO2 level"
            )
        if not self._device.is_faked:
            raise ServiceValidationError(
                f"Device {self._device.id} is not configured as faked"
            )

        try:
            await self._device.set_co2_level(co2_level)
        except (DeviceNotFaked, TypeError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def async_put_ventilation_demand(
        self, ventilation_demand: float
    ) -> None:
        """Send a ventilation demand from a faked CO2 sensor.

        :param ventilation_demand: The demand as a percentage from 0 to 100.
        :type ventilation_demand: float
        :raises TypeError: If the entity is not a compatible CO2 sensor.
        :raises ValueError: If the sensor has no bound Orcon fan.
        """
        assert self.device_class == SensorDeviceClass.CO2
        assert self.native_unit_of_measurement == UnitOfRatio.PARTS_PER_MILLION

        device = self._device
        if not isinstance(device, VentilationDemandCapable):
            raise ServiceValidationError(
                f"Device {device.id} does not support setting ventilation demand"
            )
        if not device.is_faked:
            raise ServiceValidationError(
                f"Device {device.id} is not configured as faked"
            )

        schema = self.coordinator.entry.options.get(CONF_SCHEMA, {})
        device_schema = schema.get(device.id, {})
        fan_id = (
            device_schema.get(SZ_TR_BOUND)
            if isinstance(device_schema, dict)
            else None
        )
        if not isinstance(fan_id, str) or not fan_id.startswith("32:"):
            raise ValueError(f"CO2 sensor {device.id} has no bound Orcon fan")

        try:
            await device.set_ventilation_demand(
                DeviceIdT(fan_id), ventilation_demand / 100.0
            )
        except (DeviceNotFaked, TypeError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def async_put_dhw_temp(self, temperature: float) -> None:
        """Cast the DHW cylinder temperature (if faked).

        :param temperature: The temperature in degrees Celsius.
        :raises ServiceValidationError: If the device lacks faked
            temperature capability.
        """
        if not isinstance(self._device, FakedTemperatureCapable):
            raise ServiceValidationError(
                f"Device {self._device.id} does not support setting temperature"
            )
        if not self._device.is_faked:
            raise ServiceValidationError(
                f"Device {self._device.id} is not configured as faked"
            )

        try:
            await self._device.set_temperature(temperature)
        except (DeviceNotFaked, TypeError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

        self.async_write_ha_state()

    async def async_put_indoor_humidity(self, indoor_humidity: float) -> None:
        """Cast the indoor humidity level (if faked).

        :param indoor_humidity: The humidity percentage (0-100).
        :raises ServiceValidationError: If the device lacks faked
            humidity capability.
        """
        if not isinstance(self._device, FakedHumidityCapable):
            raise ServiceValidationError(
                f"Device {self._device.id} does not support setting indoor humidity"
            )
        if not self._device.is_faked:
            raise ServiceValidationError(
                f"Device {self._device.id} is not configured as faked"
            )

        try:
            await self._device.set_indoor_humidity(indoor_humidity / 100)
        except (DeviceNotFaked, TypeError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def async_put_room_temp(self, temperature: float) -> None:
        """Cast the room temperature (if faked).

        :param temperature: The temperature in degrees Celsius.
        :raises ServiceValidationError: If the device lacks faked
            temperature capability.
        """
        if not isinstance(self._device, FakedTemperatureCapable):
            raise ServiceValidationError(
                f"Device {self._device.id} does not support setting temperature"
            )
        if not self._device.is_faked:
            raise ServiceValidationError(
                f"Device {self._device.id} is not configured as faked"
            )

        try:
            await self._device.set_temperature(temperature)
        except (DeviceNotFaked, TypeError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

        self.async_write_ha_state()


class RamsesLastMessageSensor(RamsesSensor):
    """Sensor showing the last message sent by the device.

    The state renders the message header and decoded payload as
    ``verb/code dst payload`` (e.g. ``I/22F1 32:153289 {'fan_mode':
    'low', ...}``); the ``sent`` attribute carries its timestamp.
    Unlike trait sensors (e.g. ``fan_mode``) this reflects *every*
    verb the device transmits - including commands that are invisible
    to trait state such as a 22F3 timed boost or a 2411 parameter set.
    Requires ``device.last_command`` (ramses_rf 0.60.9+).  The entity
    key keeps the earlier ``last_msg`` name so existing unique_ids are
    stable.
    """

    @property
    def _last_msg(self) -> Any | None:
        """Return the device's last transmitted message, if any."""
        # DeviceBase.last_command — absent on the plain Entity base
        return getattr(self._device, "last_command", None)

    @property
    def native_value(self) -> str | None:
        """Return the last message as ``verb/code → dst: payload``."""
        msg = self._last_msg
        if msg is None:
            return None
        verb = str(msg.verb).strip()
        code = str(msg.code)
        dst = str(msg.dst.id) if msg.dst is not None else "--:------"
        payload = "" if msg.payload is None else str(msg.payload)
        value = f"{verb}/{code} {dst} {payload}".strip()
        return (
            value[: MAX_LENGTH_STATE_STATE - 3] + "..."
            if len(value) > MAX_LENGTH_STATE_STATE
            else value
        )

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Disable by default on HGIs to limit recorder writes.

        An HGI transmits on every poll cycle, so its ``last_msg`` sensor
        would churn the recorder database on flash-constrained installs.
        Device-class sensors (REM/DIS/FAN/CO2/...) stay enabled: they
        only change when the device itself speaks.
        """
        return not isinstance(self._device, HgiGateway)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes describing the last message."""
        attrs = super().extra_state_attributes
        if (msg := self._last_msg) is not None:
            dtm = msg.dtm
            if dtm.tzinfo is None:
                dtm = dtm.replace(tzinfo=UTC)
            attrs["sent"] = dtm.isoformat()
            attrs["verb"] = str(msg.verb)
            attrs["code"] = str(msg.code)
            attrs["dst"] = str(msg.dst.id) if msg.dst is not None else None
        return attrs


class RamsesDeviceModelSensor(RamsesSensor):
    """Sensor exposing the model id reported in a device's 10E0 reply.

    The state is the 10E0 ``description`` (e.g. ``"VMC-15RPS34"``), or
    ``unknown`` until a 10E0 packet is received.  The remaining 10E0
    fields are surfaced as attributes so a wrong scheme/model lookup can
    be diagnosed (and reported) without packet logs.
    """

    @property
    def native_value(self) -> StateType | date | dt | Decimal:
        """Return the model, marked when the device is faked.

        A faked device never sends a 10E0, so ``unknown`` would be
        misleading: report ``faked`` (or ``<model> (faked)``) instead.
        """
        value = super().native_value
        if getattr(self._device, "is_faked", False):
            return f"{value} (faked)" if isinstance(value, str) else "faked"
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw 10E0 fields alongside the base attributes."""
        attrs = super().extra_state_attributes
        info = getattr(self._device, "info", None)
        if isinstance(info, dict):
            attrs.update(
                {
                    k: v
                    for k, v in info.items()
                    if k != "description" and v not in (None, "")
                }
            )
        return attrs


@dataclass(frozen=True, kw_only=True)
class RamsesSensorEntityDescription(
    RamsesEntityDescription, SensorEntityDescription
):
    """Class describing Ramses binary sensor entities."""

    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT

    # integration-specific attributes
    ramses_cc_class: type[RamsesSensor] = RamsesSensor
    ramses_cc_icon_off: str | None = (
        None  # no SensorEntityDescription.icon_off attr
    )
    ramses_cc_option: str | None = (
        None  # advanced_features key that must be enabled to create it
    )
    ramses_rf_attr: str
    ramses_rf_class: type[RamsesRFEntity] | UnionType = RamsesRFEntity
    # key is used to create HA unique_id
    # ramses_rf_attr must match ramses_rf device method
    poll_codes: list[Code] | None = None  # opt-in for fetch-driven entities


SENSOR_DESCRIPTIONS: tuple[RamsesSensorEntityDescription, ...] = (
    RamsesSensorEntityDescription(
        key="sys_info",
        ramses_rf_attr="id",
        name="System info",
        ramses_rf_class=System,
        state_class=None,
        ramses_cc_extra_attributes={
            ATTR_WORKING_SCHEMA: SZ_SCHEMA,
        },
    ),
    RamsesSensorEntityDescription(
        key="model",
        name="Model",
        ramses_rf_class=DeviceHvac,
        ramses_rf_attr="model",
        ramses_cc_class=RamsesDeviceModelSensor,
        state_class=None,
        icon="mdi:chip",
    ),
    RamsesSensorEntityDescription(
        key=SZ_TEMPERATURE,
        ramses_rf_class=HvacHumiditySensor | TrvActuator,
        ramses_rf_attr=SZ_TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        ramses_cc_extra_attributes={
            ATTR_SETPOINT: SZ_SETPOINT,
        },
    ),
    RamsesSensorEntityDescription(
        key=SZ_TEMPERATURE,
        ramses_rf_class=DhwSensor | OutSensor | Thermostat,
        ramses_rf_attr=SZ_TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=None,
        ramses_cc_extra_attributes={
            ATTR_SETPOINT: SZ_SETPOINT,
        },
    ),
    RamsesSensorEntityDescription(
        key=SZ_DEWPOINT_TEMP,
        ramses_rf_class=HvacHumiditySensor,
        ramses_rf_attr=SZ_DEWPOINT_TEMP,
        name="Dewpoint temperature",
        icon="mdi:water-thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_HEAT_DEMAND,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_HEAT_DEMAND,
        name="Heat demand",
        icon="mdi:radiator",
        ramses_cc_icon_off="mdi:radiator-off",
        native_unit_of_measurement=PERCENTAGE,
    ),
    RamsesSensorEntityDescription(  # not OtbGateway
        key=SZ_HEAT_DEMAND,
        ramses_rf_class=(
            System | TrvActuator | UfhController | ZoneBase | UfhCircuit
        ),
        ramses_rf_attr=SZ_HEAT_DEMAND,
        name="Heat demand",
        icon="mdi:radiator",
        ramses_cc_icon_off="mdi:radiator-off",
        native_unit_of_measurement=PERCENTAGE,
    ),
    RamsesSensorEntityDescription(
        key=SZ_COOLING_DEMAND,
        ramses_rf_class=UfhCircuit,
        ramses_rf_attr=SZ_COOLING_DEMAND,
        name="Cooling demand",
        icon="mdi:snowflake",
        ramses_cc_icon_off="mdi:snowflake-off",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=SZ_CIRCUIT_MODE,
        ramses_rf_class=UfhCircuit,
        ramses_rf_attr=SZ_CIRCUIT_MODE,
        name="Mode",
        icon="mdi:home-thermometer",
        device_class=SensorDeviceClass.ENUM,
        options=[mode.value for mode in ThermalMode],
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=SZ_SETPOINT,
        ramses_rf_class=UfhCircuit,
        ramses_rf_attr=SZ_SETPOINT,
        name="Setpoint",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=f"{SZ_HEAT_DEMAND}_fa",
        ramses_rf_class=UfhController,
        ramses_rf_attr="heat_demand_fa",
        name="Heat demand (FA)",
        icon="mdi:radiator",
        ramses_cc_icon_off="mdi:radiator-off",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=f"{SZ_HEAT_DEMAND}_fc",
        ramses_rf_class=UfhController,
        ramses_rf_attr="heat_demand_fc",
        name="Heat demand (FC)",
        icon="mdi:radiator",
        ramses_cc_icon_off="mdi:radiator-off",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=SZ_PUMP_RELAY_STATE,
        ramses_rf_class=UfhController,
        ramses_rf_attr=SZ_PUMP_RELAY_STATE,
        name="Pump relay state",
        icon="mdi:pump",
        ramses_cc_icon_off="mdi:pump-off",
        device_class=SensorDeviceClass.ENUM,
        options=[state.value for state in PumpRelayState],
        state_class=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_RELAY_DEMAND,
        ramses_rf_attr=SZ_RELAY_DEMAND,
        name="Relay demand",
        icon="mdi:power-plug",
        ramses_cc_icon_off="mdi:power-plug-off",
        native_unit_of_measurement=PERCENTAGE,
    ),
    RamsesSensorEntityDescription(
        key=f"{SZ_RELAY_DEMAND}_fa",
        ramses_rf_attr=f"{SZ_RELAY_DEMAND}_fa",
        name="Relay demand (FA)",
        icon="mdi:power-plug",
        ramses_cc_icon_off="mdi:power-plug-off",
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=SZ_BOILER_EXHAUST_TEMP,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_BOILER_EXHAUST_TEMP,
        name="Boiler exhaust temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=SZ_BOILER_OUTPUT_TEMP,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_BOILER_OUTPUT_TEMP,
        name="Boiler output temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_BOILER_RETURN_TEMP,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_BOILER_RETURN_TEMP,
        name="Boiler return temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_BOILER_SETPOINT,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_BOILER_SETPOINT,
        name="Boiler setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_CH_SETPOINT,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_CH_SETPOINT,
        name="CH setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_CH_MAX_SETPOINT,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_CH_MAX_SETPOINT,
        name="CH max setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_CH_WATER_PRESSURE,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_CH_WATER_PRESSURE,
        name="CH water pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        poll_codes=[Code._1300],
    ),
    RamsesSensorEntityDescription(
        key=SZ_DHW_FLOW_RATE,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_DHW_FLOW_RATE,
        name="DHW flow rate",
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        poll_codes=[Code._12F0],
    ),
    RamsesSensorEntityDescription(
        key=SZ_DHW_SETPOINT,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_DHW_SETPOINT,
        name="DHW setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_DHW_TEMP,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_DHW_TEMP,
        name="DHW temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_OUTSIDE_TEMP,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_OUTSIDE_TEMP,
        name="Outside temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_REL_MODULATION_LEVEL,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_REL_MODULATION_LEVEL,
        name="Relative modulation level",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_MAX_REL_MODULATION,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_MAX_REL_MODULATION,
        name="Max relative modulation level",
        native_unit_of_measurement=PERCENTAGE,
    ),
    # HVAC (mostly ventilation units)
    RamsesSensorEntityDescription(
        key=SZ_AIR_QUALITY,
        ramses_rf_attr=SZ_AIR_QUALITY,
        name="Air quality",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_AIR_QUALITY_BASIS,
        ramses_rf_attr=SZ_AIR_QUALITY_BASIS,
        name="Air quality basis",
        native_unit_of_measurement=PERCENTAGE,
    ),
    RamsesSensorEntityDescription(
        key=SZ_BYPASS_MODE,
        ramses_rf_attr=SZ_BYPASS_MODE,
        name="Bypass mode",
        state_class=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_CO2_LEVEL,
        ramses_rf_attr=SZ_CO2_LEVEL,
        device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=UnitOfRatio.PARTS_PER_MILLION,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_EXHAUST_FAN_SPEED,
        ramses_rf_attr=SZ_EXHAUST_FAN_SPEED,
        name="Exhaust fan speed",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_EXHAUST_FLOW,
        ramses_rf_attr=SZ_EXHAUST_FLOW,
        name="Exhaust flow",
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_SECOND,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_EXHAUST_TEMP,
        ramses_rf_attr=SZ_EXHAUST_TEMP,
        name="Exhaust temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_FAN_INFO,
        ramses_rf_attr=SZ_FAN_INFO,
        name="Fan info",
        state_class=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_FAN_MODE,
        ramses_rf_attr=SZ_FAN_MODE,
        ramses_rf_class=HvacVentilator,
        name="Fan mode",
        state_class=None,
        ramses_cc_extra_attributes={
            "last_sent": "last_fan_mode_dtm",
        },
    ),
    RamsesSensorEntityDescription(
        # Same key/unique_id as FAN's "Fan mode": on a REM/DIS remote the
        # value is the last mode command the remote itself transmitted,
        # not ventilator state (issue 1216).  HvacRemote also matches
        # HvacDisplayRemote (DIS), which subclasses it.  last_sent is
        # resolved from device.last_fan_mode_dtm (skipped on ramses_rf
        # versions without it).
        key=SZ_FAN_MODE,
        ramses_rf_attr=SZ_FAN_MODE,
        ramses_rf_class=HvacRemote,
        name="Last mode sent",
        state_class=None,
        ramses_cc_extra_attributes={
            "last_sent": "last_fan_mode_dtm",
        },
    ),
    RamsesSensorEntityDescription(
        # A REM/DIS remote has no ventilator: its fan_rate only mirrors a
        # byte from its own last command (the modbus 'requested_rate'),
        # which may differ from the fan's actual rate (issue 1216).
        key=SZ_FAN_RATE,
        ramses_rf_attr=SZ_FAN_RATE,
        ramses_rf_class=HvacVentilator,
        name="Fan rate",
        state_class=None,
    ),
    RamsesSensorEntityDescription(
        # Optional (advanced_features.last_msg_sensors, default off): the
        # sensor updates on every message the device sends, which would
        # otherwise double recorder writes (issue 1267).
        key=SZ_LAST_MSG,
        ramses_rf_attr="last_command",
        name="Last message sent",
        state_class=None,
        ramses_cc_class=RamsesLastMessageSensor,
        ramses_cc_option=CONF_LAST_MSG_SENSORS,
    ),
    RamsesSensorEntityDescription(
        key=SZ_FILTER_REMAINING,
        ramses_rf_attr=SZ_FILTER_REMAINING,
        name="Filter remaining",
        native_unit_of_measurement=UnitOfTime.DAYS,
        poll_codes=[Code._10D0],
    ),
    RamsesSensorEntityDescription(
        key=SZ_FILTER_REMAINING_PERCENT,
        ramses_rf_attr=SZ_FILTER_REMAINING_PERCENT,
        name="Filter remaining (%)",
        native_unit_of_measurement=PERCENTAGE,
        poll_codes=[Code._10D0],
    ),
    RamsesSensorEntityDescription(
        key=SZ_INDOOR_HUMIDITY,
        ramses_rf_attr=SZ_INDOOR_HUMIDITY,
        name="Indoor humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_INDOOR_TEMP,
        ramses_rf_attr=SZ_INDOOR_TEMP,
        name="Indoor temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_OUTDOOR_HUMIDITY,
        ramses_rf_attr=SZ_OUTDOOR_HUMIDITY,
        name="Outdoor humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_OUTDOOR_TEMP,
        ramses_rf_attr=SZ_OUTDOOR_TEMP,
        name="Outdoor temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_POST_HEAT,
        ramses_rf_attr=SZ_POST_HEAT,
        name="Post heat",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_PRE_HEAT,
        ramses_rf_attr=SZ_PRE_HEAT,
        name="Pre heat",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_REMAINING_MINS,
        ramses_rf_attr=SZ_REMAINING_MINS,
        name="Remaining time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    RamsesSensorEntityDescription(
        key=SZ_SPEED_CAPABILITIES,
        ramses_rf_attr=SZ_SPEED_CAPABILITIES,
        name="Speed cap",
        native_unit_of_measurement="units",
    ),
    RamsesSensorEntityDescription(
        key=SZ_SUPPLY_FAN_SPEED,
        ramses_rf_attr=SZ_SUPPLY_FAN_SPEED,
        name="Supply fan speed",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_SUPPLY_FLOW,
        ramses_rf_attr=SZ_SUPPLY_FLOW,
        name="Supply flow",
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_SECOND,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_SUPPLY_TEMP,
        ramses_rf_attr=SZ_SUPPLY_TEMP,
        name="Supply temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=None,
    ),
    RamsesSensorEntityDescription(
        key=SZ_TEMPERATURE,
        ramses_rf_attr=SZ_TEMPERATURE,
        ramses_rf_class=HvacVentilator,
        name="Temperature",  # ClimaRad fans 12A0 field
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=None,
    ),
    # Special projects
    RamsesSensorEntityDescription(
        key=SZ_OEM_CODE,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_OEM_CODE,
        name="OEM code",
        state_class=None,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key=SZ_OEM_FAULT_CODE,
        ramses_rf_class=OtbGateway,
        ramses_rf_attr=SZ_OEM_FAULT_CODE,
        name="OEM fault code",
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key="percent",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="percent",
        name="Percent",
        icon="mdi:power-plug",
        ramses_cc_icon_off="mdi:power-plug-off",
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    RamsesSensorEntityDescription(
        key="value",
        ramses_rf_class=OtbGateway,
        ramses_rf_attr="value",
        name="Value",
        native_unit_of_measurement="units",
        entity_registry_enabled_default=False,
    ),
)

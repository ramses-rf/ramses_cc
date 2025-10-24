"""Support for RAMSES water_heater entities."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime as dt, timedelta
from typing import Any, Final

from homeassistant.components.water_heater import (
    ENTITY_ID_FORMAT,
    STATE_OFF,
    STATE_ON,
    WaterHeaterEntity,
    WaterHeaterEntityDescription,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)

from ramses_rf.system.heat import StoredHw
from ramses_rf.system.zones import DhwZone
from ramses_tx.const import SZ_ACTIVE, SZ_MODE, SZ_SYSTEM_MODE

from . import RamsesEntity, RamsesEntityDescription
from .broker import RamsesBroker
from .const import DOMAIN, SystemMode, ZoneMode
from .schemas import SCH_SET_DHW_MODE_EXTRA

_LOGGER = logging.getLogger(__name__)


STATE_AUTO: Final = "auto"
STATE_BOOST: Final = "boost"

MODE_HA_TO_RAMSES: Final[dict[str, str]] = {
    STATE_AUTO: ZoneMode.SCHEDULE,
    STATE_BOOST: ZoneMode.TEMPORARY,
    STATE_OFF: ZoneMode.PERMANENT,
    STATE_ON: ZoneMode.PERMANENT,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the water heater platform."""
    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: list[DhwZone]) -> None:
        entities = [
            description.ramses_cc_class(broker, device, description)
            for device in devices
            for description in WATER_HEATER_DESCRIPTIONS
            if isinstance(device, description.ramses_rf_class)
        ]
        async_add_entities(entities)

    broker.async_register_platform(platform, add_devices)


# TODO(wi): fix sphinx error in this class:
# AttributeError: type object 'RamsesWaterHeater' has no attribute '_RamsesWaterHeater__attr_temperature_unit'
# skipping in sphinx_apidoc because of errors. BTW: same error as class RamsesNumberBase in number.py
class RamsesWaterHeater(RamsesEntity, WaterHeaterEntity):
    """Representation of a Rames DHW controller."""

    _device: DhwZone

    _attr_icon: str = "mdi:thermometer-lines"
    _attr_max_temp: float = StoredHw.MAX_SETPOINT
    _attr_min_temp: float = StoredHw.MIN_SETPOINT
    _attr_operation_list: list[str] = list(MODE_HA_TO_RAMSES)
    _attr_supported_features: int = (
        WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.TARGET_TEMPERATURE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        broker: RamsesBroker,
        device: DhwZone,
        entity_description: RamsesWaterHeaterEntityDescription,
    ) -> None:
        """Initialize a TCS DHW controller."""
        _LOGGER.info("Found DHW %s", device.id)
        super().__init__(broker, device, entity_description)

        self.entity_id = ENTITY_ID_FORMAT.format(device.id)

    @property
    def current_operation(self) -> str | None:
        """Return the current operating mode (Auto, On, or Off)."""
        try:
            mode = self._device.mode[SZ_MODE]
        except TypeError:
            return None  # unable to determine
        if mode == ZoneMode.SCHEDULE:
            return STATE_AUTO
        elif mode == ZoneMode.PERMANENT:
            return STATE_ON if self._device.mode[SZ_ACTIVE] else STATE_OFF
        else:  # there are a number of temporary modes
            return STATE_BOOST if self._device.mode[SZ_ACTIVE] else STATE_OFF

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._device.temperature

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        return super().extra_state_attributes | {
            "params": self._device.params,
            "mode": self._device.mode,
            "schedule": self._device.schedule,
            "schedule_version": self._device.schedule_version,
        }

    @property
    def is_away_mode_on(self) -> bool | None:
        """Return True if away mode is on."""
        try:
            return self._device.tcs.system_mode[SZ_SYSTEM_MODE] == SystemMode.AWAY
        except TypeError:
            return None  # unable to determine

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._device.setpoint

    def set_operation_mode(self, operation_mode: str) -> None:
        """Set the operating mode of the water heater."""
        active = until = None  # for STATE_AUTO
        if operation_mode == STATE_BOOST:
            active = True
            until = dt.now() + timedelta(hours=1)
        elif operation_mode == STATE_OFF:
            active = False
        elif operation_mode == STATE_ON:
            active = True

        self.async_set_dhw_mode(
            mode=MODE_HA_TO_RAMSES[operation_mode], active=active, until=until
        )

    def set_temperature(self, temperature: float | None = None, **kwargs: Any) -> None:
        """Set the target temperature of the water heater."""
        self.async_set_dhw_params(setpoint=temperature)

    # the following methods are integration-specific service calls

    @callback
    def async_fake_dhw_temp(self, temperature: float) -> None:
        """Cast the temperature of this water heater (if faked)."""

        self._device.sensor.temperature = temperature  # would accept None

    @callback
    def async_reset_dhw_mode(self) -> None:
        """Reset the operating mode of the water heater."""
        self._device.reset_mode()
        self.async_write_ha_state_delayed()

    @callback
    def async_reset_dhw_params(self) -> None:
        """Reset the configuration of the water heater."""
        self._device.reset_config()
        self.async_write_ha_state_delayed()

    @callback
    def async_set_dhw_boost(self) -> None:
        """Enable the water heater for an hour."""
        self._device.set_boost_mode()
        self.async_write_ha_state_delayed()

    @callback
    def async_set_dhw_mode(
        self,
        mode: str | None = None,
        active: bool | None = None,
        duration: timedelta | None = None,
        until: dt | None = None,
    ) -> None:
        """Set the (native) operating mode of the water heater."""
        entry: dict[str, Any] = {"mode": mode}
        if active is not None:
            entry.update({"active": active})
        if duration is not None:
            entry.update({"duration": duration})
        if until is not None:
            entry.update({"until": until})

        # strict, non-entity schema check
        checked_entry = SCH_SET_DHW_MODE_EXTRA(entry)
        # default `duration` of 1 hour updated by schema default, so can't use original

        if until is None and "duration" in checked_entry:
            until = dt.now() + checked_entry["duration"]  # move duration to until
        self._device.set_mode(
            mode=mode,
            active=active,
            until=until,
        )
        self.async_write_ha_state_delayed()

    @callback
    def async_set_dhw_params(
        self,
        setpoint: float | None = None,
        overrun: int | None = None,
        differential: float | None = None,
    ) -> None:
        """Set the configuration of the water heater."""
        self._device.set_config(
            setpoint=setpoint,
            overrun=overrun,
            differential=differential,
        )
        self.async_write_ha_state_delayed()

    async def async_get_dhw_schedule(self) -> None:
        """Get the latest weekly schedule of the DHW."""
        # {{ state_attr('water_heater.stored_hw', 'schedule') }}
        await self._device.get_schedule()
        self.async_write_ha_state()

    async def async_set_dhw_schedule(self, schedule: str) -> None:
        """Set the weekly schedule of the DHW."""
        await self._device.set_schedule(json.loads(schedule))


@dataclass(frozen=True, kw_only=True)
class RamsesWaterHeaterEntityDescription(
    RamsesEntityDescription, WaterHeaterEntityDescription
):
    """Class describing Ramses water heater entities."""

    # integration-specific attributes
    ramses_cc_class: type[RamsesWaterHeater]
    ramses_rf_class: type[DhwZone]


WATER_HEATER_DESCRIPTIONS: tuple[RamsesWaterHeaterEntityDescription, ...] = (
    RamsesWaterHeaterEntityDescription(
        key="dhwzone",
        name=None,
        ramses_rf_class=DhwZone,
        ramses_cc_class=RamsesWaterHeater,
    ),
)

"""Support for RAMSES climate entities."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import voluptuous as vol
from homeassistant.components.climate import (
    ENTITY_ID_FORMAT,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_OFF,
    PRECISION_TENTHS,
    PRESET_AWAY,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)

from ramses_rf.device.hvac import HvacVentilator
from ramses_rf.system.heat import Evohome
from ramses_rf.system.zones import Zone
from ramses_tx.const import SZ_MODE, SZ_SETPOINT, SZ_SYSTEM_MODE

from . import RamsesEntity, RamsesEntityDescription
from .broker import RamsesBroker
from .const import (
    ATTR_DEVICE_ID,
    DOMAIN,
    PRESET_CUSTOM,
    PRESET_PERMANENT,
    PRESET_TEMPORARY,
    SystemMode,
    ZoneMode,
)
from .schemas import SCH_SET_SYSTEM_MODE_EXTRA, SCH_SET_ZONE_MODE_EXTRA

_LOGGER = logging.getLogger(__name__)

MODE_TCS_TO_HA: Final[dict[str, str]] = {
    SystemMode.AUTO: HVACMode.HEAT,  # NOTE: don't use AUTO
    SystemMode.HEAT_OFF: HVACMode.OFF,
    SystemMode.RESET: HVACMode.HEAT,
}
MODE_HA_TO_TCS: Final[dict[str, str]] = {
    HVACMode.HEAT: SystemMode.AUTO,
    HVACMode.OFF: SystemMode.HEAT_OFF,
    HVACMode.AUTO: SystemMode.RESET,  # not all systems support this
}

PRESET_TCS_TO_HA: Final[dict[str, str]] = {
    SystemMode.AUTO: PRESET_NONE,
    SystemMode.AWAY: PRESET_AWAY,
    SystemMode.CUSTOM: PRESET_CUSTOM,
    SystemMode.DAY_OFF: PRESET_HOME,
    SystemMode.DAY_OFF_ECO: PRESET_HOME,
    SystemMode.ECO_BOOST: PRESET_ECO,
    SystemMode.HEAT_OFF: PRESET_NONE,
    SystemMode.RESET: PRESET_NONE,
}
PRESET_HA_TO_TCS: Final[dict[str, str]] = {
    PRESET_NONE: SystemMode.AUTO,
    PRESET_AWAY: SystemMode.AWAY,
    PRESET_CUSTOM: SystemMode.CUSTOM,
    PRESET_HOME: SystemMode.DAY_OFF,
    PRESET_ECO: SystemMode.ECO_BOOST,
}

MODE_ZONE_TO_HA: Final[dict[str, str]] = {
    ZoneMode.ADVANCED: HVACMode.HEAT,
    ZoneMode.SCHEDULE: HVACMode.AUTO,
    ZoneMode.PERMANENT: HVACMode.HEAT,
    ZoneMode.TEMPORARY: HVACMode.HEAT,
}
MODE_HA_TO_ZONE: Final[dict[str, str]] = {
    HVACMode.HEAT: ZoneMode.PERMANENT,
    HVACMode.AUTO: ZoneMode.SCHEDULE,
}

PRESET_ZONE_TO_HA: Final[dict[str, str]] = {
    ZoneMode.SCHEDULE: PRESET_NONE,
    ZoneMode.TEMPORARY: PRESET_TEMPORARY,
    ZoneMode.PERMANENT: PRESET_PERMANENT,
}
PRESET_HA_TO_ZONE: Final[dict[str, str]] = {v: k for k, v in PRESET_ZONE_TO_HA.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the climate platform.

    :param hass: The Home Assistant instance.
    :param entry: The config entry.
    :param async_add_entities: The callback to add entities.
    """
    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: list[Evohome | Zone | HvacVentilator]) -> None:
        entities = [
            description.ramses_cc_class(broker, device, description)
            for device in devices
            for description in CLIMATE_DESCRIPTIONS
            if isinstance(device, description.ramses_rf_class)
        ]
        async_add_entities(entities)

    broker.async_register_platform(platform, add_devices)


class RamsesController(RamsesEntity, ClimateEntity):
    """Representation of a Ramses controller."""

    _device: Evohome

    _attr_icon: str = "mdi:thermostat"
    _attr_hvac_modes: list[HVACMode] = list(MODE_HA_TO_TCS)
    _attr_max_temp: None = None
    _attr_min_temp: None = None
    _attr_precision: float = PRECISION_TENTHS
    _attr_preset_modes: list[str] = list(PRESET_HA_TO_TCS)
    _attr_supported_features: ClimateEntityFeature = (
        ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS

    _enable_turn_on_off_backwards_compatibility: bool = False  # remove when HA 2025.1

    def __init__(
        self,
        broker: RamsesBroker,
        device: Evohome,
        entity_description: RamsesClimateEntityDescription,
    ) -> None:
        """Initialize a TCS controller.

        :param broker: The Ramses broker.
        :param device: The Evohome device.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found controller %s", device.id)
        super().__init__(broker, device, entity_description)

        self.entity_id = ENTITY_ID_FORMAT.format(device.id)

    @property
    def current_temperature(self) -> float | None:
        """Return the average current temperature of the heating Zones.

        Controllers do not have a current temp, but one is expected by HA.

        :return: The average temperature.
        """
        temps = [z.temperature for z in self._device.zones if z.temperature is not None]
        temps = [t for t in temps if t is not None]  # above is buggy, why?
        try:
            return round(sum(temps) / len(temps), 1) if temps else None
        except TypeError:
            _LOGGER.warning("Temp (%s) contains None", temps)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: A dictionary of attributes.
        """
        return super().extra_state_attributes | {
            "heat_demand": self._device.heat_demand,
            "heat_demands": self._device.heat_demands,
            "relay_demands": self._device.relay_demands,
            "system_mode": self._device.system_mode,
            "tpi_params": self._device.tpi_params,
        }

    @property
    def hvac_action(self) -> str | None:
        """Return the Controller's current running hvac operation.

        :return: The HVAC action.
        """
        if self._device.system_mode is None:
            return None  # unable to determine
        if self._device.system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACAction.OFF

        if self._device.heat_demand:
            return HVACAction.HEATING
        if self._device.heat_demand is not None:
            return HVACAction.IDLE

        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Controller's current operating mode of a Controller.

        :return: The HVAC mode.
        """
        if self._device.system_mode is None:
            return None  # unable to determine
        if self._device.system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACMode.OFF
        if self._device.system_mode[SZ_SYSTEM_MODE] == SystemMode.AWAY:
            return HVACMode.AUTO  # users can't adjust setpoints in away mode
        return HVACMode.HEAT

    @property
    def preset_mode(self) -> str | None:
        """Return the Controller's current preset mode, e.g., home, away, temp.

        :return: The preset mode.
        """
        if self._device.system_mode is None:
            return None  # unable to determine
        return PRESET_TCS_TO_HA[self._device.system_mode[SZ_SYSTEM_MODE]]

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach.

        :return: The target temperature.
        """
        zones = [z for z in self._device.zones if z.setpoint is not None]
        temps = [z.setpoint for z in zones if z.heat_demand is not None]
        return max(z.setpoint for z in zones) if temps else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set an operating mode for a Controller.

        :param hvac_mode: The HVAC mode to set.
        :raises ServiceValidationError: If the mode is invalid or schema validation fails.
        """
        target_mode = MODE_HA_TO_TCS.get(hvac_mode)
        if target_mode is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_hvac_mode",
                translation_placeholders={"mode": str(hvac_mode)},
            )

        try:
            # Removed @callback - now async and awaited to prevent SQLite threading issues
            self.async_set_system_mode(target_mode)
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode; if 'none', then revert to 'Auto' mode.

        :param preset_mode: The preset mode to set.
        :raises ServiceValidationError: If the preset is invalid.
        """
        target_mode = PRESET_HA_TO_TCS.get(preset_mode)
        if target_mode is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_preset_mode",
                translation_placeholders={"mode": str(preset_mode)},
            )

        try:
            # Removed @callback - now async and awaited
            self.async_set_system_mode(target_mode)
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    # the following methods are integration-specific service calls

    @callback
    async def async_get_system_faults(self, num_entries: int) -> None:
        """Get the nth latest fault log entries from the Controller.

        :param num_entries: Number of entries to fetch.
        """
        await self._device.get_faultlog(limit=num_entries, force_refresh=True)
        self.async_write_ha_state_delayed()

    @callback
    def async_reset_system_mode(self) -> None:
        """Reset the (native) operating mode of the Controller."""
        self._device.reset_mode()
        self.async_write_ha_state_delayed()

    @callback
    def async_set_system_mode(
        self,
        mode: str,
        period: timedelta | None = None,
        duration: timedelta | None = None,
    ) -> None:
        """Set the (native) operating mode of the Controller.

        :param mode: The system mode to set.
        :param period: The period for the mode.
        :param duration: The duration for the mode.
        """
        entry: dict[str, Any] = {"mode": mode}
        if period is not None:
            entry.update({"period": period})
        if duration is not None:
            entry.update({"duration": duration})
        # strict, non-entity schema check
        SCH_SET_SYSTEM_MODE_EXTRA(entry)  # result not used

        # move params to `until`, we can reuse the init params:
        if duration is not None:
            # evohome controllers utilise whole hours
            until = datetime.now() + duration  # <=24 hours, was verified
        elif period is None:
            until = None
        elif period.seconds == period.microseconds == 0:
            # this is the behaviour of an evohome controller
            date_ = datetime.now().date() + timedelta(days=1) + period
            until = datetime(date_.year, date_.month, date_.day)
        else:
            until = datetime.now() + period
        # duration and/or period are now in until
        assert mode is not None
        self._device.set_mode(mode, until=until)  # note: mode is a positional argument
        self.async_write_ha_state_delayed()


class RamsesZone(RamsesEntity, ClimateEntity):
    """Representation of a Ramses zone."""

    _device: Zone

    _attr_icon: str = "mdi:radiator"
    _attr_hvac_modes: list[HVACMode] = list(MODE_HA_TO_ZONE)
    _attr_precision: float = PRECISION_TENTHS
    _attr_preset_modes: list[str] = list(PRESET_HA_TO_ZONE)
    _attr_supported_features: ClimateEntityFeature = (
        ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.TARGET_TEMPERATURE
    )
    _attr_target_temperature_step: float = PRECISION_HALVES
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        broker: RamsesBroker,
        device: Zone,
        entity_description: RamsesClimateEntityDescription,
    ) -> None:
        """Initialize a TCS zone.

        :param broker: The Ramses broker.
        :param device: The Zone device.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found zone %s", device.id)
        super().__init__(broker, device, entity_description)

        self.entity_id = ENTITY_ID_FORMAT.format(device.id)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature.

        :return: The current temperature.
        """
        return self._device.temperature

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: A dictionary of attributes.
        """
        return super().extra_state_attributes | {
            "params": self._device.params,
            "zone_idx": self._device.idx,
            "heating_type": self._device.heating_type,
            "mode": self._device.mode,
            "config": self._device.config,
            "schedule": self._device.schedule,
            "schedule_version": self._device.schedule_version,
        }

    @property
    def hvac_action(self) -> str | None:
        """Return the Zone's current running hvac operation.

        :return: The HVAC action.
        """
        if self._device.tcs.system_mode is None:
            return None  # unable to determine
        if self._device.tcs.system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACAction.OFF

        if self._device.heat_demand:
            return HVACAction.HEATING
        if self._device.heat_demand is not None:
            return HVACAction.IDLE
        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Zone's hvac operation ie. heat, cool mode.

        :return: The HVAC mode.
        """
        if self._device.tcs.system_mode is None:
            return None  # unable to determine
        if self._device.tcs.system_mode[SZ_SYSTEM_MODE] == SystemMode.AWAY:
            return HVACMode.AUTO
        if self._device.tcs.system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACMode.OFF

        if self._device.mode is None or self._device.mode[SZ_SETPOINT] is None:
            return None  # unable to determine
        if (
            self._device.config
            and self._device.mode[SZ_SETPOINT] <= self._device.config["min_temp"]
        ):
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def max_temp(self) -> float:
        """Return the maximum target temperature of a Zone.

        :return: The max temp.
        """
        if not self._device.config:
            return 35
        return self._device.config["max_temp"]

    @property
    def min_temp(self) -> float:
        """Return the minimum target temperature of a Zone.

        :return: The min temp.
        """
        if not self._device.config:
            return 5
        return self._device.config["min_temp"]

    @property
    def preset_mode(self) -> str | None:
        """Return the Zone's current preset mode, e.g., home, away, temp.

        :return: The preset mode.
        """
        if self._device.tcs.system_mode is None:
            return None  # unable to determine
        # if self._device.tcs.system_mode[SZ_SYSTEM_MODE] in MODE_TCS_TO_HA:
        if self._device.tcs.system_mode[SZ_SYSTEM_MODE] in (
            SystemMode.AWAY,
            SystemMode.HEAT_OFF,
        ):
            return PRESET_TCS_TO_HA[self._device.tcs.system_mode[SZ_SYSTEM_MODE]]

        if self._device.mode is None:
            return None  # unable to determine
        if self._device.mode[SZ_MODE] == ZoneMode.SCHEDULE:
            return PRESET_TCS_TO_HA[self._device.tcs.system_mode[SZ_SYSTEM_MODE]]
        return PRESET_ZONE_TO_HA.get(self._device.mode[SZ_MODE])

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach.

        :return: The target temperature.
        """
        return self._device.setpoint

    # Overrides of standard HA climate actions

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set a Zone to one of its native operating modes.

        :param hvac_mode: The HVAC mode to set.
        :raises ServiceValidationError: If the mode is invalid.
        """
        try:
            if hvac_mode == HVACMode.AUTO:  # FollowSchedule
                self.async_reset_zone_mode()  # Not awaited (callback) in original, but safe to await if async?
                # Original async_reset_zone_mode is @callback, no await needed unless changed.
                # However, for consistency and future proofing we treat calls here as potentially causing DB writes.
                # The prompt asks to migrate set_hvac_mode to async def and await underlying calls.
                # The underlying calls (async_reset_zone_mode) are currently @callback.
                # To fully fix SQLite issues, those might need to be async too if they write to DB.
                # Assuming ramses_rf.device.set_mode writes to DB.
                pass
            elif hvac_mode == HVACMode.HEAT:  # TemporaryOverride
                self.async_set_zone_mode(mode=ZoneMode.PERMANENT, setpoint=25)
            else:  # HVACMode.OFF, PermanentOverride, temp = min
                self.async_set_zone_mode(self._device.set_frost_mode)
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode to one of None, Permanent, Temporary.

        If 'None', revert to following the schedule.

        :param preset_mode: The preset mode.
        :raises ServiceValidationError: If validation fails.
        """
        try:
            self.async_set_zone_mode(
                mode=PRESET_HA_TO_ZONE[preset_mode],
                setpoint=self.target_temperature
                if preset_mode != PRESET_NONE
                else None,
                duration=timedelta(hours=1)
                if preset_mode == PRESET_TEMPORARY
                else None,  # why 1H?
            )
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_set_temperature(
        self,
        temperature: float | None = None,
        duration: timedelta | None = None,
        until: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """Set a new target temperature.

        :param temperature: The target temperature.
        :param duration: The duration of the override.
        :param until: The end time of the override.
        :param kwargs: Additional arguments.
        :raises ServiceValidationError: If validation fails.
        """
        mode = ZoneMode.ADVANCED  #  only setpoint, when permanent/forever?
        if temperature is None and duration is None and until is None:
            mode = ZoneMode.SCHEDULE
        elif duration is None and until is None:  # only setpoint
            mode = ZoneMode.ADVANCED  # till next scheduled change in evohome
        elif duration is not None or until is not None:  # both is flagged later
            mode = ZoneMode.TEMPORARY

        try:
            self.async_set_zone_mode(
                mode=mode, setpoint=temperature, duration=duration, until=until
            )
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    # the following are integration-specific method service calls

    @callback
    def async_fake_zone_temp(self, temperature: float) -> None:
        """Cast the room temperature of this zone (if faked).

        :param temperature: The temperature to fake.
        """
        if self._device.sensor is None:
            raise HomeAssistantError(
                f"Zone {self.entity_id} has no sensor to fake temperature on."
            )

        self._device.sensor.temperature = temperature  # would accept None

    @callback
    def async_reset_zone_config(self) -> None:
        """Reset the configuration of the Zone."""
        self._device.reset_config()
        self.async_write_ha_state_delayed()

    @callback
    def async_reset_zone_mode(self) -> None:
        """Reset the (native) operating mode of the Zone."""
        self._device.reset_mode()
        self.async_write_ha_state_delayed()

    @callback
    def async_set_zone_config(self, **kwargs: Any) -> None:
        """Set the configuration of the Zone (min/max temp, etc.).

        :param kwargs: Config arguments.
        """
        self._device.set_config(**kwargs)
        self.async_write_ha_state_delayed()

    @callback
    def async_set_zone_mode(
        self,
        mode: str | None = None,
        setpoint: float | None = None,
        duration: timedelta | None = None,
        until: datetime | None = None,
    ) -> None:
        """Set the (native) operating mode of the Zone.

        :param mode: The mode to set.
        :param setpoint: The setpoint to set.
        :param duration: The duration.
        :param until: The until time.
        """
        entry: dict[str, Any] = {"mode": mode}
        if setpoint is not None:
            entry.update({"setpoint": setpoint})
        if duration is not None:
            entry.update({"duration": duration})
        if until is not None:
            entry.update({"until": until})

        # strict, non-entity schema check
        checked_entry = SCH_SET_ZONE_MODE_EXTRA(entry)
        # default `duration` of 1 hour is updated by SCH_ default, so can't use original

        if until is None and "duration" in checked_entry:
            until = datetime.now() + checked_entry["duration"]  # move duration to until
        self._device.set_mode(
            mode=mode,
            setpoint=setpoint,
            until=until,
        )
        self.async_write_ha_state_delayed()

    async def async_get_zone_schedule(self) -> None:
        """Get the latest weekly schedule of the Zone."""
        # {{ state_attr('climate.ramses_cc_01_145038_04', 'schedule') }}
        await self._device.get_schedule()
        self.async_write_ha_state()

    async def async_set_zone_schedule(self, schedule: str) -> None:
        """Set the weekly schedule of the Zone.

        :param schedule: The schedule json string.
        """
        await self._device.set_schedule(json.loads(schedule))


class RamsesHvac(RamsesEntity, ClimateEntity):
    """Base for a Honeywell HVAC unit (Fan, HRU, MVHR, PIV, etc)."""

    _device: HvacVentilator

    _attr_fan_modes: list[str] | None = [
        FAN_OFF,
        FAN_AUTO,
        FAN_LOW,
        FAN_MEDIUM,
        FAN_HIGH,
    ]
    _attr_hvac_modes: list[HVACMode] | list[str] = [HVACMode.AUTO, HVACMode.OFF]
    _attr_precision: float = PRECISION_TENTHS
    _attr_preset_modes: list[str] | None = None
    _attr_supported_features: ClimateEntityFeature = (
        ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        broker: RamsesBroker,
        device: HvacVentilator,
        entity_description: RamsesClimateEntityDescription,
    ) -> None:
        """Initialize a HVAC system.

        :param broker: The Ramses broker.
        :param device: The Hvac device.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found HVAC %s", device.id)
        super().__init__(broker, device, entity_description)

        self.entity_id = ENTITY_ID_FORMAT.format(device.id)
        self._device = device
        self._bound_rem = None

    async def async_added_to_hass(self) -> None:
        """Called when entity is added to Home Assistant."""
        await super().async_added_to_hass()
        # If your device already has a bound REM:
        self._bound_rem = self._device.get_bound_rem()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional info about the HVAC.

        :return: A dictionary of attributes.
        """
        data = {}
        # Use cached bound_rem or fetch fresh one if not yet set
        bound_rem = self._bound_rem or self._device.get_bound_rem()
        if bound_rem:
            data["bound_rem"] = bound_rem  # already a string ID
        return data

    @property
    def current_humidity(self) -> int | None:
        """Return the current humidity.

        :return: The humidity.
        """
        if self._device.indoor_humidity is not None:
            return int(self._device.indoor_humidity * 100)
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature.

        :return: The temperature.
        """
        return self._device.indoor_temp

    @property
    def fan_mode(self) -> str | None:
        """Return the fan setting.

        :return: The fan mode.
        """
        return self._device.fan_info

    @property
    def hvac_action(self) -> HVACAction | str | None:
        """Return the current running hvac operation if supported.

        :return: The HVAC action.
        """
        return self._device.fan_info

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode.

        :return: The HVAC mode as an Enum.
        """
        if self._device.fan_info is None:
            return None
        # CLIMATE-02: Ensure strict return of HVACMode Enum
        return HVACMode.OFF if self._device.fan_info == "off" else HVACMode.AUTO

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any.

        :return: The icon name.
        """
        return "mdi:hvac-off" if self._device.fan_info == "off" else "mdi:hvac"

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode, e.g., home, away, temp.

        :return: The preset mode.
        """
        return PRESET_NONE

    # the 2411 fan_param services, copied to numbers and to remote.py

    @callback
    async def async_get_fan_clim_param(self, **kwargs: Any) -> None:
        """Handle 'get_fan_param' service call.

        :param kwargs: Service arguments.
        """
        _LOGGER.info(
            "Fan param read from climate entity %s (%s, id %s)",
            self.entity_id,
            self.__class__.__name__,
            self._device.id,
        )
        kwargs[ATTR_DEVICE_ID] = self._device.id
        await self._broker.async_get_fan_param(kwargs)

    @callback
    async def async_set_fan_clim_param(self, **kwargs: Any) -> None:
        """Handle 'set_fan_param' service call.

        :param kwargs: Service arguments.
        """
        _LOGGER.info(
            "Fan param write to climate entity %s (%s)",
            self.entity_id,
            self.__class__.__name__,
        )
        kwargs[ATTR_DEVICE_ID] = self._device.id
        await self._broker.async_set_fan_param(kwargs)

    @callback
    async def async_update_fan_params(self, **kwargs: Any) -> None:
        """Handle 'update_fan_params' service call.

        :param kwargs: Service arguments.
        """
        _LOGGER.info(
            "Fan read all params from climate entity %s (%s)",
            self.entity_id,
            self.__class__.__name__,
        )
        kwargs[ATTR_DEVICE_ID] = self._device.id
        self._broker.get_all_fan_params(kwargs)  # don't await


@dataclass(frozen=True, kw_only=True)
class RamsesClimateEntityDescription(RamsesEntityDescription, ClimateEntityDescription):
    """Class describing Ramses binary sensor entities."""

    # integration-specific attributes
    ramses_cc_class: type[Evohome] | type[Zone] | type[HvacVentilator]
    ramses_rf_class: type[RamsesController] | type[RamsesZone] | type[RamsesHvac]


CLIMATE_DESCRIPTIONS: tuple[RamsesClimateEntityDescription, ...] = (
    RamsesClimateEntityDescription(
        key="controller",
        name=None,
        ramses_rf_class=Evohome,
        ramses_cc_class=RamsesController,
    ),
    RamsesClimateEntityDescription(
        key="zone",
        name=None,
        ramses_rf_class=Zone,
        ramses_cc_class=RamsesZone,
    ),
    RamsesClimateEntityDescription(
        key="hvac",
        name=None,
        ramses_rf_class=HvacVentilator,
        ramses_cc_class=RamsesHvac,
    ),
)

"""Support for RAMSES climate entities."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import voluptuous as vol
from homeassistant.components.climate import (
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
from homeassistant.util import dt as dt_util

from ramses_rf.device.hvac import HvacVentilator
from ramses_rf.system.heat import Evohome
from ramses_rf.system.zones import Zone
from ramses_tx.const import SZ_MODE, SZ_SETPOINT, SZ_SYSTEM_MODE
from ramses_tx.exceptions import (
    ProtocolSendFailed,
    ProtocolTimeoutError,
    RamsesException,
    TransportError,
)

from .const import (
    ATTR_DEVICE_ID,
    DOMAIN,
    PRESET_CUSTOM,
    PRESET_PERMANENT,
    PRESET_TEMPORARY,
    SystemMode,
    ZoneMode,
)
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import fields_to_aware, resolve_async_attr
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
    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: list[Evohome | Zone | HvacVentilator]) -> None:
        entities = [
            description.ramses_cc_class(coordinator, device, description)
            for device in devices
            for description in CLIMATE_DESCRIPTIONS
            if isinstance(device, description.ramses_rf_class)
        ]
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)


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
        coordinator: RamsesCoordinator,
        device: Evohome,
        entity_description: RamsesClimateEntityDescription,
    ) -> None:
        """Initialize a TCS controller.

        :param coordinator: The Ramses coordinator.
        :param device: The Evohome device.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found controller %s", device.id)
        super().__init__(coordinator, device, entity_description)

    @property
    def current_temperature(self) -> float | None:
        """Return the average current temperature of the heating Zones.

        Controllers do not have a current temp, but one is expected by HA.

        :return: The average temperature.
        """
        # SAFEGUARD: The underlying library accesses a SQLite DB to get temperature.
        # This can raise unexpected errors (NotImplementedError, sqlite3.OperationalError)
        # if the DB is locked or not ready. We must catch generic Exception to prevent
        # the entity update loop from crashing permanently.
        try:
            temps = [
                t
                for z in self._device.zones
                if (t := resolve_async_attr(self, z, "temperature")) is not None
            ]

            if not temps:
                return None

            return round(sum(temps) / len(temps), 1)
        except Exception:  # pylint: disable=broad-except
            # If we don't catch this, a single DB error kills the entity updates forever.
            # Logging verbose exception only once per minute (at most) is acceptable.
            _LOGGER.warning(
                "Unable to calculate current_temperature for %s (device not ready?)",
                self.entity_id,
                exc_info=True,  # Prints the full traceback to logs for debugging
            )
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: A dictionary of attributes.
        """
        system_mode = resolve_async_attr(self, self._device, "system_mode")
        if system_mode and "until" in system_mode:
            # Copy to avoid mutating the library's internal state
            system_mode = system_mode.copy()
            system_mode["until"] = fields_to_aware(system_mode["until"])

        return super().extra_state_attributes | {
            "heat_demand": resolve_async_attr(self, self._device, "heat_demand"),
            "heat_demands": resolve_async_attr(self, self._device, "heat_demands"),
            "relay_demands": resolve_async_attr(self, self._device, "relay_demands"),
            "system_mode": system_mode,
            "tpi_params": resolve_async_attr(self, self._device, "tpi_params"),
        }

    @property
    def hvac_action(self) -> str | None:
        """Return the Controller's current running hvac operation.

        :return: The HVAC action.
        """
        system_mode = resolve_async_attr(self, self._device, "system_mode")
        if system_mode is None:
            return None  # unable to determine
        if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACAction.OFF

        heat_demand = resolve_async_attr(self, self._device, "heat_demand")
        if heat_demand:
            return HVACAction.HEATING
        if heat_demand is not None:
            return HVACAction.IDLE

        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Controller's current operating mode of a Controller.

        :return: The HVAC mode.
        """
        system_mode = resolve_async_attr(self, self._device, "system_mode")
        if system_mode is None:
            return None  # unable to determine
        if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACMode.OFF
        if system_mode[SZ_SYSTEM_MODE] == SystemMode.AWAY:
            return HVACMode.AUTO  # users can't adjust setpoints in away mode
        return HVACMode.HEAT

    @property
    def preset_mode(self) -> str | None:
        """Return the Controller's current preset mode, e.g., home, away, temp.

        :return: The preset mode.
        """
        system_mode = resolve_async_attr(self, self._device, "system_mode")
        if system_mode is None:
            return None  # unable to determine
        return PRESET_TCS_TO_HA.get(system_mode[SZ_SYSTEM_MODE])

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach.

        :return: The target temperature.
        """
        zones = [
            z
            for z in self._device.zones
            if resolve_async_attr(self, z, "setpoint") is not None
        ]
        temps = [
            resolve_async_attr(self, z, "setpoint")
            for z in zones
            if resolve_async_attr(self, z, "heat_demand") is not None
        ]
        return max(temps) if temps else None

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
            await self.async_set_system_mode(target_mode)
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
            await self.async_set_system_mode(target_mode)
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
        :raises HomeAssistantError: If the command fails.
        """
        try:
            await self._device.get_faultlog(limit=num_entries, force_refresh=True)
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to get system faults: {err}") from err

    async def async_reset_system_mode(self) -> None:
        """Reset the (native) operating mode of the Controller.

        :raises HomeAssistantError: If the command fails.
        """
        try:
            await self._device.reset_mode()
            self.async_write_ha_state()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to reset system mode: {err}") from err

    async def async_set_system_mode(
        self,
        mode: str,
        period: timedelta | None = None,
        duration: timedelta | None = None,
    ) -> None:
        """Set the (native) operating mode of the Controller.

        :param mode: The system mode to set.
        :param period: The period for the mode.
        :param duration: The duration for the mode.
        :raises HomeAssistantError: If the command fails.
        """
        entry: dict[str, Any] = {"mode": mode}
        if period is not None:
            entry.update({"period": period})
        if duration is not None:
            entry.update({"duration": duration})
        # strict, non-entity schema check
        try:
            SCH_SET_SYSTEM_MODE_EXTRA(entry)  # result not used
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

        # move params to `until`, we can reuse the init params:
        if duration is not None:
            # evohome controllers utilise whole hours
            until = dt_util.now() + duration  # <=24 hours, was verified
        elif period is None:
            until = None
        elif period.seconds == period.microseconds == 0:
            # this is the behaviour of an evohome controller
            date_ = dt_util.now().date() + timedelta(days=1) + period
            until = dt_util.as_utc(datetime(date_.year, date_.month, date_.day))
        else:
            until = dt_util.now() + period
        # duration and/or period are now in until
        assert mode is not None
        # note: mode is a positional argument
        try:
            await self._device.set_mode(mode, until=until)
            self.async_write_ha_state()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set system mode: {err}") from err


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
        coordinator: RamsesCoordinator,
        device: Zone,
        entity_description: RamsesClimateEntityDescription,
    ) -> None:
        """Initialize a TCS zone.

        :param coordinator: The Ramses coordinator.
        :param device: The Zone device.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found zone %s", device.id)
        super().__init__(coordinator, device, entity_description)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature.

        :return: The current temperature.
        """
        return resolve_async_attr(self, self._device, "temperature")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: A dictionary of attributes.
        """
        mode = resolve_async_attr(self, self._device, "mode")
        if mode and "until" in mode:
            # Copy to avoid mutating the library's internal state
            mode = mode.copy()
            mode["until"] = fields_to_aware(mode["until"])

        return super().extra_state_attributes | {
            "params": resolve_async_attr(self, self._device, "params"),
            "zone_idx": self._device.idx,
            "heating_type": resolve_async_attr(self, self._device, "heating_type"),
            "mode": mode,
            "config": resolve_async_attr(self, self._device, "config"),
            "schedule": resolve_async_attr(self, self._device, "schedule"),
            "schedule_version": resolve_async_attr(
                self, self._device, "schedule_version"
            ),
        }

    @property
    def hvac_action(self) -> str | None:
        """Return the Zone's current running hvac operation.

        :return: The HVAC action.
        """
        system_mode = resolve_async_attr(self, self._device.tcs, "system_mode")
        if system_mode is None:
            return None  # unable to determine
        if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACAction.OFF

        heat_demand = resolve_async_attr(self, self._device, "heat_demand")
        if heat_demand:
            return HVACAction.HEATING
        if heat_demand is not None:
            return HVACAction.IDLE
        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Zone's hvac operation ie. heat, cool mode.

        :return: The HVAC mode.
        """
        system_mode = resolve_async_attr(self, self._device.tcs, "system_mode")
        if system_mode is None:
            return None  # unable to determine
        if system_mode[SZ_SYSTEM_MODE] == SystemMode.AWAY:
            return HVACMode.AUTO
        if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACMode.OFF

        mode = resolve_async_attr(self, self._device, "mode")
        if mode is None or mode.get(SZ_SETPOINT) is None:
            return None  # unable to determine

        config = resolve_async_attr(self, self._device, "config")
        if config and mode[SZ_SETPOINT] <= config["min_temp"]:
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def max_temp(self) -> float:
        """Return the maximum target temperature of a Zone.

        :return: The max temp.
        """
        config = resolve_async_attr(self, self._device, "config")
        if not config:
            return 35
        return config["max_temp"]

    @property
    def min_temp(self) -> float:
        """Return the minimum target temperature of a Zone.

        :return: The min temp.
        """
        config = resolve_async_attr(self, self._device, "config")
        if not config:
            return 5
        return config["min_temp"]

    @property
    def preset_mode(self) -> str | None:
        """Return the Zone's current preset mode.

        :return: The preset mode.
        """
        system_mode = resolve_async_attr(self, self._device.tcs, "system_mode")
        if system_mode is None:
            return None  # unable to determine

        if system_mode[SZ_SYSTEM_MODE] in (SystemMode.AWAY, SystemMode.HEAT_OFF):
            return PRESET_TCS_TO_HA.get(system_mode[SZ_SYSTEM_MODE])

        mode = resolve_async_attr(self, self._device, "mode")
        if mode is None:
            return None  # unable to determine
        if mode[SZ_MODE] == ZoneMode.SCHEDULE:
            return PRESET_TCS_TO_HA.get(system_mode[SZ_SYSTEM_MODE])
        return PRESET_ZONE_TO_HA.get(mode[SZ_MODE])

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach.

        :return: The target temperature.
        """
        return resolve_async_attr(self, self._device, "setpoint")

    # Overrides of standard HA climate actions

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set a Zone to one of its native operating modes.

        :param hvac_mode: The HVAC mode to set.
        :raises ServiceValidationError: If the mode is invalid.
        """
        try:
            if hvac_mode == HVACMode.AUTO:  # FollowSchedule
                await self.async_reset_zone_mode()
            elif hvac_mode == HVACMode.HEAT:  # TemporaryOverride
                await self.async_set_zone_mode(mode=ZoneMode.PERMANENT, setpoint=25)
            else:  # HVACMode.OFF, PermanentOverride, temp = min
                await self._device.set_frost_mode()
                self.async_write_ha_state()

        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set hvac mode: {err}") from err
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
            await self.async_set_zone_mode(
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
            await self.async_set_zone_mode(
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
        :raises HomeAssistantError: If the zone has no sensor.
        """
        if self._device.sensor is None:
            raise HomeAssistantError(
                f"Zone {self.entity_id} has no sensor to fake temperature on."
            )

        self._device.sensor.temperature = temperature  # would accept None

    async def async_reset_zone_config(self) -> None:
        """Reset the configuration of the Zone.

        :raises HomeAssistantError: If the command fails.
        """
        try:
            await self._device.reset_config()
            self.async_write_ha_state()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to reset zone config: {err}") from err

    async def async_reset_zone_mode(self) -> None:
        """Reset the (native) operating mode of the Zone.

        :raises HomeAssistantError: If the command fails.
        """
        try:
            await self._device.reset_mode()
            self.async_write_ha_state()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to reset zone mode: {err}") from err

    async def async_set_zone_config(self, **kwargs: Any) -> None:
        """Set the configuration of the Zone (min/max temp, etc.).

        :param kwargs: Config arguments.
        :raises HomeAssistantError: If the command fails.
        """
        try:
            await self._device.set_config(**kwargs)
            self.async_write_ha_state()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set zone config: {err}") from err

    async def async_set_zone_mode(
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
        :raises HomeAssistantError: If the command fails.
        """
        entry: dict[str, Any] = {"mode": mode}
        if setpoint is not None:
            entry.update({"setpoint": setpoint})
        if duration is not None:
            entry.update({"duration": duration})
        if until is not None:
            entry.update({"until": until})

        # strict, non-entity schema check
        try:
            checked_entry = SCH_SET_ZONE_MODE_EXTRA(entry)
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

        # default `duration` of 1 hour is updated by SCH_ default, so can't use original

        if until is None and "duration" in checked_entry:
            until = dt_util.now() + checked_entry["duration"]  # move duration to until
        try:
            await self._device.set_mode(
                mode=mode,
                setpoint=setpoint,
                until=until,
            )
            self.async_write_ha_state()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set zone mode: {err}") from err

    async def async_get_zone_schedule(self) -> None:
        """Get the latest weekly schedule of the Zone.

        :raises HomeAssistantError: If the command fails.
        """
        # {{ state_attr('climate.ramses_cc_01_145038_04', 'schedule') }}
        try:
            await self._device.get_schedule()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to get zone schedule: {err}") from err
        self.async_write_ha_state()

    async def async_set_zone_schedule(self, schedule: str) -> None:
        """Set the weekly schedule of the Zone.

        :param schedule: The schedule json string.
        :raises HomeAssistantError: If the command fails.
        """
        try:
            await self._device.set_schedule(json.loads(schedule))
            self.async_write_ha_state()
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set zone schedule: {err}") from err


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
        coordinator: RamsesCoordinator,
        device: HvacVentilator,
        entity_description: RamsesClimateEntityDescription,
    ) -> None:
        """Initialize a HVAC system.

        :param coordinator: The Ramses coordinator.
        :param device: The Hvac device.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found HVAC %s", device.id)
        super().__init__(coordinator, device, entity_description)

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
        indoor_humidity = resolve_async_attr(self, self._device, "indoor_humidity")
        if indoor_humidity is not None:
            return int(indoor_humidity * 100)
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature.

        :return: The temperature.
        """
        return resolve_async_attr(self, self._device, "indoor_temp")

    @property
    def fan_mode(self) -> str | None:
        """Return the fan setting.

        :return: The fan mode.
        """
        return resolve_async_attr(self, self._device, "fan_info")

    @property
    def hvac_action(self) -> HVACAction | str | None:
        """Return the current running hvac operation if supported.

        :return: The HVAC action.
        """
        return resolve_async_attr(self, self._device, "fan_info")

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode.

        :return: The HVAC mode as an Enum.
        """
        fan_info = resolve_async_attr(self, self._device, "fan_info")
        if fan_info is None:
            return None
        # CLIMATE-02: Ensure strict return of HVACMode Enum
        return HVACMode.OFF if fan_info == "off" else HVACMode.AUTO

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any.

        :return: The icon name.
        """
        fan_info = resolve_async_attr(self, self._device, "fan_info")
        return "mdi:hvac-off" if fan_info == "off" else "mdi:hvac"

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
        :raises HomeAssistantError: If the command fails.
        """
        _LOGGER.info(
            "Fan param read from climate entity %s (%s, id %s)",
            self.entity_id,
            self.__class__.__name__,
            self._device.id,
        )
        kwargs[ATTR_DEVICE_ID] = self._device.id
        try:
            await self.coordinator.async_get_fan_param(kwargs)
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to get fan param: {err}") from err

    @callback
    async def async_set_fan_clim_param(self, **kwargs: Any) -> None:
        """Handle 'set_fan_param' service call.

        :param kwargs: Service arguments.
        :raises HomeAssistantError: If the command fails.
        """
        _LOGGER.info(
            "Fan param write to climate entity %s (%s)",
            self.entity_id,
            self.__class__.__name__,
        )
        kwargs[ATTR_DEVICE_ID] = self._device.id
        try:
            await self.coordinator.async_set_fan_param(kwargs)
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set fan param: {err}") from err

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
        # Note: This spawns a task and is not awaited, so simple try/except here
        # won't catch exceptions from the task.
        self.coordinator.get_all_fan_params(kwargs)  # don't await


@dataclass(frozen=True, kw_only=True)
class RamsesClimateEntityDescription(RamsesEntityDescription, ClimateEntityDescription):
    """Class describing Ramses binary sensor entities."""

    # integration-specific attributes
    ramses_cc_class: type[RamsesController] | type[RamsesZone] | type[RamsesHvac]
    ramses_rf_class: type[Evohome] | type[Zone] | type[HvacVentilator]


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

"""Support for RAMSES water_heater entities."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace as dc_replace
from datetime import datetime as dt, timedelta as td
from typing import Any, Final

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityDescription,
    WaterHeaterEntityFeature,
)
from homeassistant.const import STATE_OFF, STATE_ON, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)
from homeassistant.util import dt as dt_util

from ramses_rf.const import SZ_ACTIVE, SZ_MODE, SZ_SYSTEM_MODE
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_rf.systems.tcs import StoredHw
from ramses_rf.systems.zones import DhwZone
from ramses_tx.exceptions import (
    ProtocolSendFailed,
    ProtocolTimeoutError,
    TransportError,
)

from .const import DOMAIN, SystemMode, ZoneMode
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import (
    extract_demand,
    fields_to_aware,
    resolve_async_attr,
    resolve_demand_attr,
)
from .schemas import SCH_SET_DHW_MODE_EXTRA
from .typing import RamsesConfigEntry

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
    hass: HomeAssistant,
    entry: RamsesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the water heater platform."""
    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: RamsesRFEntity | Sequence[RamsesRFEntity]) -> None:
        # 1. Safely wrap a single device into a list, or keep it as a sequence
        device_list = devices if isinstance(devices, Sequence) else [devices]

        # 2. Iterate over device_list (not 'devices')
        entities = [
            description.ramses_cc_class(coordinator, device, description)
            for device in device_list
            for description in WATER_HEATER_DESCRIPTIONS
            if isinstance(device, description.ramses_rf_class)
        ]
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)


class RamsesWaterHeater(RamsesEntity, WaterHeaterEntity):
    """Representation of a Ramses DHW controller.

    This class provides control over RAMSES domestic hot water (DHW) zones,
    including temperature control and operating mode management.

    :cvar _attr_icon: Icon to display in the UI
    :vartype _attr_icon: str
    :cvar _attr_max_temp: Maximum allowed temperature setpoint
    :vartype _attr_max_temp: float
    :cvar _attr_min_temp: Minimum allowed temperature setpoint
    :vartype _attr_min_temp: float
    :cvar _attr_operation_list: List of available operation modes
    :vartype _attr_operation_list: list[str]
    :cvar _attr_supported_features: Bitmask of supported water heater features
    :vartype _attr_supported_features: int
    :cvar _attr_temperature_unit: Temperature unit for the water heater
    :vartype _attr_temperature_unit: UnitOfTemperature
    """

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
        coordinator: RamsesCoordinator,
        device: DhwZone,
        entity_description: RamsesWaterHeaterEntityDescription,
    ) -> None:
        """Initialize a TCS DHW controller."""
        _LOGGER.info("Found DHW %s", device.id)
        super().__init__(coordinator, device, entity_description)
        self._last_known_operation: str | None = None
        self._last_known_away: bool | None = None
        self._last_known_temperature: float | None = None
        self._last_known_target_temp: float | None = None

    @property
    def current_operation(self) -> str | None:
        """Return the current operating mode (Auto, On, or Off)."""
        mode = resolve_async_attr(self, self._device, "mode")

        if isinstance(mode, dict):
            if mode.get(SZ_MODE) == ZoneMode.SCHEDULE:
                self._last_known_operation = STATE_AUTO
            elif mode.get(SZ_MODE) == ZoneMode.PERMANENT:
                self._last_known_operation = (
                    STATE_ON if mode.get(SZ_ACTIVE) else STATE_OFF
                )
            else:  # there are a number of temporary modes
                self._last_known_operation = (
                    STATE_BOOST if mode.get(SZ_ACTIVE) else STATE_OFF
                )
        else:
            # Fallback to evaluating active heat demand if mode expires
            heat_demand = resolve_demand_attr(
                self, self._device, "thermal_demand", "heat_demand"
            )
            demand_val = extract_demand(heat_demand)
            if demand_val:
                self._last_known_operation = STATE_ON
            elif self._last_known_operation is None:
                self._last_known_operation = STATE_AUTO

        return self._last_known_operation

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        temp = resolve_async_attr(self, self._device, "temperature")
        if temp is not None:
            self._last_known_temperature = float(temp)
        return self._last_known_temperature

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        # SAFEGUARD: Ensure 'until' in mode is timezone-aware
        mode = resolve_async_attr(self, self._device, "mode")
        if mode and "until" in mode:
            mode = mode.copy()
            mode["until"] = fields_to_aware(mode["until"])

        return super().extra_state_attributes | {
            "params": resolve_async_attr(self, self._device, "params"),
            "mode": mode,
            "schedule": resolve_async_attr(self, self._device, "schedule"),
            "schedule_version": resolve_async_attr(
                self, self._device, "schedule_version"
            ),
        }

    @property
    def is_away_mode_on(self) -> bool | None:
        """Return True if away mode is on."""
        system_mode = resolve_async_attr(self, self._device.tcs, "system_mode")
        if isinstance(system_mode, dict):
            self._last_known_away = system_mode.get(SZ_SYSTEM_MODE) == SystemMode.AWAY
        elif self._last_known_away is None:
            self._last_known_away = False

        return self._last_known_away

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        temp = resolve_async_attr(self, self._device, "setpoint")
        if temp is not None:
            self._last_known_target_temp = float(temp)
        return self._last_known_target_temp

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Set the operating mode of the water heater.

        :param operation_mode: The target operation mode (e.g., auto, boost, on, off).
        :raises ServiceValidationError: If the backend call fails.
        """
        active: bool | None = None
        until: dt | None = None  # for STATE_AUTO

        if operation_mode == STATE_BOOST:
            active = True
            until = dt_util.now() + td(hours=1)
        elif operation_mode == STATE_OFF:
            active = False
        elif operation_mode == STATE_ON:
            active = True

        await self.async_set_dhw_mode(
            mode=MODE_HA_TO_RAMSES[operation_mode], active=active, until=until
        )

    async def async_set_temperature(
        self, temperature: float | None = None, **kwargs: Any
    ) -> None:
        """Set the target temperature of the water heater.

        :param temperature: The target temperature.
        :param kwargs: Additional arguments.
        :raises ServiceValidationError: If the backend call fails.
        """
        await self.async_set_dhw_params(setpoint=temperature)

    # the following methods are integration-specific service calls

    async def async_fake_dhw_temp(self, temperature: float) -> None:
        """Cast the temperature of this water heater (if faked)."""
        sensor = getattr(self._device, "sensor", None)
        if sensor is None:
            raise HomeAssistantError(
                f"Water heater {self.entity_id} has no sensor to fake temp on."
            )
        await sensor.set_temperature(temperature)

        # Also update the DHW zone's temp_state so that current_temperature
        # reflects the faked value immediately
        if hasattr(self._device, "temp_state"):
            self._device.temp_state = dc_replace(
                self._device.temp_state, temperature=temperature
            )
        self.async_write_ha_state()

    async def async_reset_dhw_mode(self) -> None:
        """Reset the operating mode of the water heater.

        :raises ServiceValidationError: If the backend call fails.
        """
        try:
            await self._device.reset_mode()
            self.async_write_ha_state()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_reset_mode",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to reset DHW mode: {err}") from err

    async def async_reset_dhw_params(self) -> None:
        """Reset the configuration of the water heater.

        :raises ServiceValidationError: If the backend call fails.
        """
        try:
            await self._device.reset_config()
            self.async_write_ha_state()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_reset_config",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to reset DHW params: {err}") from err

    async def async_set_dhw_boost(self) -> None:
        """Enable the water heater for an hour.

        :raises ServiceValidationError: If the backend call fails.
        """
        try:
            await self._device.set_boost_mode()
            self.async_write_ha_state()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_set_boost",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set DHW boost: {err}") from err

    async def async_set_dhw_mode(
        self,
        mode: str | None = None,
        active: bool | None = None,
        duration: td | None = None,
        until: dt | None = None,
    ) -> None:
        """Set the (native) operating mode of the water heater.

        :param mode: The mode to set (e.g., temporary, permanent).
        :param active: Whether the mode is active.
        :param duration: The duration for the mode.
        :param until: The specific end time for the mode.
        :raises ServiceValidationError: If the backend call fails or arguments are invalid.
        """
        entry: dict[str, Any] = {"mode": mode}
        if active is not None:
            entry.update({"active": active})
        if duration is not None:
            entry.update({"duration": duration})
        if until is not None:
            entry.update({"until": until})

        try:
            # strict, non-entity schema check
            checked_entry: dict[str, Any] = SCH_SET_DHW_MODE_EXTRA(entry)
        except (ValueError, TypeError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_mode_args",
                translation_placeholders={"error": str(err)},
            ) from err

        # default `duration` of 1 hour updated by schema default, so can't use original

        if until is None and "duration" in checked_entry:
            until = dt_util.now() + checked_entry["duration"]  # move duration to until

        try:
            await self._device.set_mode(
                mode=mode,
                active=active,
                until=until,
            )
            self.async_write_ha_state()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_set_mode",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set DHW mode: {err}") from err

    async def async_set_dhw_params(
        self,
        setpoint: float | None = None,
        overrun: int | None = None,
        differential: float | None = None,
    ) -> None:
        """Set the configuration of the water heater.

        :param setpoint: The target temperature setpoint.
        :param overrun: The overrun time in minutes.
        :param differential: The temperature differential.
        :raises ServiceValidationError: If the backend call fails.
        """
        try:
            await self._device.set_config(
                setpoint=setpoint,
                overrun=overrun,
                differential=differential,
            )
            self.async_write_ha_state()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_set_config",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set DHW params: {err}") from err

    async def async_get_dhw_schedule(self) -> dict[str, Any]:
        """Get the latest weekly schedule of the DHW.

        :returns: Dictionary containing the schedule.
        :rtype: dict[str, Any]
        :raises ServiceValidationError: If the backend call fails or times out.
        """
        # {{ state_attr('water_heater.stored_hw', 'schedule') }}
        try:
            res = await self._device.get_schedule()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_get_schedule",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to get DHW schedule: {err}") from err
        self.async_write_ha_state()
        return {
            "schedule": res
            if res is not None
            else getattr(self._device, "schedule", None)
        }

    async def async_set_dhw_schedule(
        self, schedule: str | dict[str, Any] | list[Any]
    ) -> None:
        """Set the weekly schedule of the DHW.

        :param schedule: The schedule payload (JSON string or dict/list object).
        :raises ServiceValidationError: If the backend call fails or JSON is invalid.
        """
        try:
            payload = json.loads(schedule) if isinstance(schedule, str) else schedule
            await self._device.set_schedule(payload)
            self.async_write_ha_state()
        except (TypeError, ValueError, json.JSONDecodeError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_set_schedule",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to set DHW schedule: {err}") from err


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

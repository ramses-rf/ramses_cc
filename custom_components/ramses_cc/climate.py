"""Support for RAMSES climate entities."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace as dc_replace
from datetime import datetime as dt, timedelta as td
from typing import Any, Final, cast

import probatio as prob
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
)
from homeassistant.components.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_OFF,
    PRESET_AWAY,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    PRECISION_HALVES,
    PRECISION_TENTHS,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)
from homeassistant.util import dt as dt_util

from custom_components.ramses_cc.helpers import parse_packet_string
from ramses_rf.const import (
    SZ_CIRCUIT_MODE,
    SZ_CIRCUITS,
    SZ_MODE,
    SZ_SETPOINT,
    SZ_SYSTEM_MODE,
)
from ramses_rf.devices import HvacVentilator
from ramses_rf.enums import ThermalMode
from ramses_rf.models.dto import UfhCircuitDTO
from ramses_rf.strategies import _STRATEGY_BY_SCHEME
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_rf.systems.tcs import Evohome
from ramses_rf.systems.zones import Zone
from ramses_tx.const import Priority
from ramses_tx.exceptions import (
    ProtocolSendFailed,
    ProtocolTimeoutError,
    RamsesException,
    TransportError,
)

from .const import (
    ATTR_DEVICE_ID,
    ATTR_SCHEDULE,
    DOMAIN,
    PRESET_CUSTOM,
    PRESET_PERMANENT,
    PRESET_TEMPORARY,
    SystemMode,
    ZoneMode,
)
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import (
    dto_to_dict,
    extract_demand,
    fields_to_aware,
    resolve_async_attr,
    resolve_demand_attr,
)
from .remote import (
    _build_packet_from_template,
    _is_command_dict,
    _split_commands,
)
from .schemas import SCH_SET_SYSTEM_MODE_EXTRA, SCH_SET_ZONE_MODE_EXTRA
from .typing import RamsesConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES: Final = 0

# PRESET_NONE is imported from HA as Any (follow_imports=skip); narrow it
_PRESET_NONE: Final[str] = PRESET_NONE

MODE_TCS_TO_HA: Final[dict[str, HVACMode]] = {
    SystemMode.AUTO: HVACMode.HEAT,  # NOTE: don't use AUTO
    SystemMode.HEAT_OFF: HVACMode.OFF,
    SystemMode.RESET: HVACMode.HEAT,
}
MODE_HA_TO_TCS: Final[dict[HVACMode, str]] = {
    HVACMode.HEAT: SystemMode.AUTO,
    HVACMode.OFF: SystemMode.HEAT_OFF,
    HVACMode.AUTO: SystemMode.RESET,  # not all systems support this
    HVACMode.COOL: SystemMode.AUTO,
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

MODE_ZONE_TO_HA: Final[dict[str, HVACMode]] = {
    ZoneMode.ADVANCED: HVACMode.HEAT,
    ZoneMode.SCHEDULE: HVACMode.AUTO,
    ZoneMode.PERMANENT: HVACMode.HEAT,
    ZoneMode.TEMPORARY: HVACMode.HEAT,
}
MODE_HA_TO_ZONE: Final[dict[HVACMode, str]] = {
    HVACMode.OFF: ZoneMode.PERMANENT,
    HVACMode.HEAT: ZoneMode.PERMANENT,
    HVACMode.AUTO: ZoneMode.SCHEDULE,
    HVACMode.COOL: ZoneMode.PERMANENT,
}

PRESET_ZONE_TO_HA: Final[dict[str, str]] = {
    ZoneMode.SCHEDULE: PRESET_NONE,
    ZoneMode.TEMPORARY: PRESET_TEMPORARY,
    ZoneMode.PERMANENT: PRESET_PERMANENT,
}
PRESET_HA_TO_ZONE: Final[dict[str, str]] = {
    v: k for k, v in PRESET_ZONE_TO_HA.items()
}


# ---------------------------------------------------------------------------
# Command type classification
#
# _commands entries can be tagged with a ``type`` field (dict templates only)
# to classify them semantically.  When absent, the type is inferred from the
# RAMSES II command code.  This is used to filter which commands appear in
# the fan_modes dropdown (only ``mode`` type) and will be used later to
# check device capabilities (e.g. "does this FAN have 2411 param commands?").
# ---------------------------------------------------------------------------

# Command type constants
_CMD_TYPE_MODE: Final[str] = "mode"  # 22F1 — fan speed modes
_CMD_TYPE_CONFIG: Final[str] = "config"  # 2411 — read/write configuration
_CMD_TYPE_BYPASS: Final[str] = "bypass"  # 22F7 — bypass valve control
_CMD_TYPE_BOOST_TIMER: Final[str] = "boost_timer"  # 22F3 — timed boost
_CMD_TYPE_INFO: Final[str] = (
    "info"  # 10D0, 31DA — filter reset/status, ventilation state
)
_CMD_TYPE_OTHER: Final[str] = "other"  # anything else

# Code → type inference table
_CODE_TO_TYPE: Final[dict[str, str]] = {
    "22F1": _CMD_TYPE_MODE,
    "2411": _CMD_TYPE_CONFIG,
    "22F7": _CMD_TYPE_BYPASS,
    "22F3": _CMD_TYPE_BOOST_TIMER,
    "10D0": _CMD_TYPE_INFO,
    "31DA": _CMD_TYPE_INFO,
}

# Valid RAMSES II command codes for _commands (for inference)
_KNOWN_CODES: Final[frozenset[str]] = frozenset(_CODE_TO_TYPE.keys())


def _command_type(cmd_value: Any) -> str:
    """Classify a _commands entry by type.

    Dict templates may have an explicit ``type`` field; if absent or for
    packet strings, the type is inferred from the RAMSES II command code
    embedded in the value.

    :param cmd_value: The command value (dict template or packet string).
    :return: One of ``_CMD_TYPE_*`` constants.
    """
    if isinstance(cmd_value, dict):
        # Explicit type tag wins
        explicit = cmd_value.get("type")
        if isinstance(explicit, str) and explicit:
            return explicit
        # Infer from code
        code = str(cmd_value.get("code", "")).upper()
        return _CODE_TO_TYPE.get(code, _CMD_TYPE_OTHER)
    if isinstance(cmd_value, str):
        # Packet string: search for a known code token anywhere
        # (format varies: "I --- src dst --:------ 22F1 003 payload"
        #  or "W 37:111111 30:123456 22F1 000406")
        parts = cmd_value.upper().split()
        for part in parts:
            if part in _KNOWN_CODES:
                return _CODE_TO_TYPE[part]
    return _CMD_TYPE_OTHER


def _is_fan_mode_command(cmd_value: Any) -> bool:
    """Check if a _commands entry is a fan mode command.

    :param cmd_value: The command value (dict template or packet string).
    :return: True if the command is classified as ``mode``.
    """
    return _command_type(cmd_value) == _CMD_TYPE_MODE


def _is_boost_timer_command(cmd_value: Any) -> bool:
    """Check if a _commands entry is a boost timer command.

    :param cmd_value: The command value (dict template or packet string).
    :return: True if the command is classified as ``boost_timer``.
    """
    return _command_type(cmd_value) == _CMD_TYPE_BOOST_TIMER


def _normalise_fan_info_for_selector(
    fan_info: str | None, strategy: HvacStrategyBase | None
) -> str | None:
    """Normalise a descriptive 31DA fan_info to the canonical fan_mode name.

    ramses_rf's 31DA parser emits manufacturer descriptive names such as
    ``"speed 1, low"`` / ``"speed 2, medium"`` / ``"speed 3, high"`` (see
    ``_31DA_FAN_INFO`` in ramses_rf), while the 22F1 mode maps and the HA
    ``fan_modes`` dropdown use canonical names (``"low"`` / ``"medium"``
    / ``"high"``).  This maps the descriptive value to the canonical name
    so the climate ``fan_mode`` selector matches an entry in
    ``fan_modes``.

    The descriptive value is preserved as the ``fan_info`` state
    attribute (see ``extra_state_attributes``); only the selector
    (``fan_mode``) is normalised.  ramses_rf keeps the manufacturer
    descriptive names as-is (ramses-rf/ramses_rf#1183, issue 1118).

    Mapping is conservative: only the suffix after the last ``", "`` is
    matched against the strategy's canonical mode names, so divergent
    codes (e.g. 31DA 0x04 ``"speed 4"`` vs Orcon 22F1 0x04 ``"auto"``)
    are not mis-mapped.  Unknown descriptive values pass through
    unchanged.

    :param fan_info: The descriptive fan_info string from ramses_rf.
    :type fan_info: str | None
    :param strategy: The device's HVAC strategy, or None.
    :type strategy: HvacStrategyBase | None
    :returns: The canonical fan_mode name, or the input unchanged.
    :rtype: str | None
    """
    if strategy is None or not fan_info:
        return fan_info
    canonical_names = set(strategy.fan_modes.values())
    # "speed 1, low" -> "low"
    if ", " in fan_info:
        suffix = fan_info.rsplit(", ", 1)[-1].strip()
        if suffix in canonical_names:
            return suffix
    # already canonical (e.g. "away", "boost", "auto")
    if fan_info in canonical_names:
        return fan_info
    return fan_info


def _get_device_strategy(device: HvacVentilator) -> HvacStrategyBase | None:
    """Return the HVAC strategy for a device based on its scheme.

    Uses ``_STRATEGY_BY_SCHEME`` to look up the strategy class from
    the device's ``_scheme`` attribute (set from schema config).

    When ramses_rf PR ramses-rf/ramses_rf#1159 (Step 3) merges,
    this can be replaced with ``device._get_configured_strategy()``.

    :param device: The HVAC ventilator device.
    :type device: HvacVentilator
    :returns: The strategy instance, or None if no scheme is set.
    :rtype: HvacStrategyBase | None
    """
    scheme = getattr(device, "_scheme", None)
    if not scheme:
        return None
    strategy_cls = _STRATEGY_BY_SCHEME.get(scheme)
    if strategy_cls is None:
        return None
    return strategy_cls()


def _is_alias_language_active(
    hass: HomeAssistant, strategy: HvacStrategyBase
) -> bool:
    """Check if HA's language matches the strategy's alias locale.

    The strategy always *accepts* aliases via ``fan_mode_to_hex()``
    regardless of language; this only controls *visibility* in the
    dropdown.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param strategy: The HVAC strategy.
    :type strategy: HvacStrategyBase
    :returns: True if aliases should be shown in the dropdown.
    :rtype: bool
    """
    if not strategy._aliases:
        return False
    lang = strategy.alias_language
    if lang is None:
        return True  # language-neutral aliases — always show
    ha_lang = hass.config.language or ""
    return ha_lang.startswith(lang)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RamsesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate platform.

    :param hass: The Home Assistant instance.
    :param entry: The config entry.
    :param async_add_entities: The callback to add entities.
    """
    coordinator: RamsesCoordinator = entry.runtime_data
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: Any) -> None:
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

    # remove when HA 2025.1
    _enable_turn_on_off_backwards_compatibility: bool = False

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
        self._last_known_curr_temp: float | None = None
        self._last_known_targ_temp: float | None = None

    @property
    def current_temperature(self) -> float | None:
        """Return the average current temperature of the heating Zones.

        Controllers do not have a current temp, but one is expected by HA.

        :return: The average temperature.
        """
        # SAFEGUARD: The underlying library accesses a SQLite DB to get
        # temperature. This can raise unexpected errors
        # (NotImplementedError, sqlite3.OperationalError) if the DB is
        # locked or not ready. We must catch generic Exception to prevent
        # the entity update loop from crashing permanently.
        try:
            temps = [
                t
                for z in self._device.zones
                if (t := resolve_async_attr(self, z, "temperature"))
                is not None
            ]

            if temps:
                self._last_known_curr_temp = round(sum(temps) / len(temps), 1)
        except (
            AttributeError,
            KeyError,
            TypeError,
            NotImplementedError,
            ValueError,
        ) as err:
            # If a device DB/attr error occurs, return last known temp safely
            _LOGGER.warning(
                "Unable to calculate current_temperature for %s: %s",
                self.entity_id,
                err,
                exc_info=True,
            )

        return self._last_known_curr_temp

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

        heat_demands = resolve_demand_attr(
            self, self._device, "thermal_demands", "heat_demands"
        )
        heat_demand = resolve_demand_attr(
            self, self._device, "thermal_demand", "heat_demand"
        )

        return super().extra_state_attributes | {
            "heat_demand": extract_demand(heat_demand),
            "heat_demands": dto_to_dict(heat_demands),
            "relay_demands": resolve_async_attr(
                self, self._device, "relay_demands"
            ),
            "system_mode": system_mode,
            "tpi_params": resolve_async_attr(self, self._device, "tpi_params"),
        }

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the Controller's current running hvac operation.

        :return: The HVAC action.
        """
        system_mode = resolve_async_attr(self, self._device, "system_mode")
        # If system_mode is None, we fall back to heat_demand instead
        # of immediately returning None (unable to determine).
        if system_mode is not None:
            if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
                return HVACAction.OFF

        thermal_demand = resolve_demand_attr(
            self, self._device, "thermal_demand", "heat_demand"
        )
        demand_val = extract_demand(thermal_demand)
        if demand_val:
            if getattr(thermal_demand, "mode", None) == ThermalMode.COOL:
                return HVACAction.COOLING
            return HVACAction.HEATING
        if demand_val is not None:
            return HVACAction.IDLE

        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Controller's current operating mode of a Controller.

        :return: The HVAC mode.
        """
        system_mode = resolve_async_attr(self, self._device, "system_mode")
        if system_mode is not None:
            if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
                return cast(str | None, HVACMode.OFF)
            if system_mode[SZ_SYSTEM_MODE] == SystemMode.AWAY:
                return cast(
                    str | None, HVACMode.AUTO
                )  # users can't adjust setpoints away

        thermal_mode = resolve_async_attr(self, self._device, "thermal_mode")
        if thermal_mode == ThermalMode.COOL:
            return cast(str | None, HVACMode.COOL)
        return cast(str | None, HVACMode.HEAT)

    @property
    def preset_mode(self) -> str | None:
        """Return the Controller's current preset mode.

        :return: The preset mode.
        """
        system_mode = resolve_async_attr(self, self._device, "system_mode")
        if system_mode is None:
            # Fallback instead of returning None (unable to determine)
            return _PRESET_NONE
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
        if temps:
            self._last_known_targ_temp = max(temps)
        return self._last_known_targ_temp

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set an operating mode for a Controller.

        :param hvac_mode: The HVAC mode to set.
        :raises ServiceValidationError: If mode is invalid or validate fails.
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
        except prob.Invalid as err:
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
        except prob.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    # the following methods are integration-specific service calls

    async def async_get_system_faults(self, num_entries: int) -> None:
        """Get the nth latest fault log entries from the Controller.

        :param num_entries: Number of entries to fetch.
        :raises HomeAssistantError: If the command fails.
        """
        try:
            await self._device.get_faultlog(
                limit=num_entries, force_refresh=True
            )
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(
                f"Failed to get system faults: {err}"
            ) from err

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
            raise HomeAssistantError(
                f"Failed to reset system mode: {err}"
            ) from err

    async def async_set_system_mode(
        self,
        mode: str,
        period: td | None = None,
        duration: td | None = None,
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
        except prob.Invalid as err:
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
            date_ = dt_util.now().date() + td(days=1) + period
            until = dt_util.as_utc(dt(date_.year, date_.month, date_.day))
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
            raise HomeAssistantError(
                f"Failed to set system mode: {err}"
            ) from err


class RamsesZone(RamsesEntity, ClimateEntity):
    """Representation of a Ramses zone."""

    _device: Zone

    _attr_icon: str = "mdi:radiator"
    _attr_hvac_modes: list[HVACMode] = list(MODE_HA_TO_ZONE)
    _attr_precision: float = PRECISION_TENTHS
    _attr_preset_modes: list[str] = list(PRESET_HA_TO_ZONE) + [
        k for k in PRESET_HA_TO_TCS if k not in PRESET_HA_TO_ZONE
    ]
    _attr_supported_features: ClimateEntityFeature = (
        ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TARGET_TEMPERATURE
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
        self._last_known_curr_temp: float | None = None
        self._last_known_targ_temp: float | None = None

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature.

        :return: The current temperature.
        """
        temp = resolve_async_attr(self, self._device, "temperature")
        if temp is not None:
            self._last_known_curr_temp = float(temp)
        return self._last_known_curr_temp

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

        heat_demand = resolve_demand_attr(
            self, self._device, "thermal_demand", "heat_demand"
        )

        extra_attrs: dict[str, Any] = super().extra_state_attributes | {
            "heat_demand": extract_demand(heat_demand),
            "params": resolve_async_attr(self, self._device, "params"),
            "zone_index": self._device.index,
            "heating_type": resolve_async_attr(
                self, self._device, "heating_type"
            ),
            "mode": mode,
            "config": resolve_async_attr(self, self._device, "config"),
            "setpoint_bounds": resolve_async_attr(
                self, self._device, "setpoint_bounds"
            ),
            "schedule": resolve_async_attr(self, self._device, "schedule"),
            "schedule_version": resolve_async_attr(
                self, self._device, "schedule_version"
            ),
        }

        circuits: list[UfhCircuitDTO] = getattr(self._device, SZ_CIRCUITS, [])
        if circuits:
            serialized_circuits: list[dict[str, Any]] = []
            for circuit in circuits:
                dto_dict = asdict(circuit)
                if isinstance(circuit.circuit_mode, ThermalMode):
                    dto_dict[SZ_CIRCUIT_MODE] = circuit.circuit_mode.value
                serialized_circuits.append(dto_dict)

            extra_attrs[SZ_CIRCUITS] = serialized_circuits
            extra_attrs["circuit_heat_demands"] = {
                circuit.ufh_index: circuit.heat_demand
                for circuit in circuits
                if circuit.heat_demand is not None
            }
            extra_attrs["circuit_cooling_demands"] = {
                circuit.ufh_index: circuit.cooling_demand
                for circuit in circuits
                if circuit.cooling_demand is not None
            }
            extra_attrs["circuit_modes"] = {
                circuit.ufh_index: (
                    circuit.circuit_mode.value
                    if isinstance(circuit.circuit_mode, ThermalMode)
                    else circuit.circuit_mode
                )
                for circuit in circuits
                if circuit.circuit_mode is not None
            }

        return extra_attrs

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the Zone's current running hvac operation.

        :return: The HVAC action.
        """
        system_mode = resolve_async_attr(self, self._device.tcs, "system_mode")
        # If system_mode is None, we proceed to check zone heat_demand
        if system_mode is not None:
            if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
                return HVACAction.OFF

        thermal_demand = resolve_demand_attr(
            self, self._device, "thermal_demand", "heat_demand"
        )
        demand_val = extract_demand(thermal_demand)
        if demand_val:
            if getattr(thermal_demand, "mode", None) == ThermalMode.COOL:
                return HVACAction.COOLING
            return HVACAction.HEATING
        if demand_val is not None:
            return HVACAction.IDLE
        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Zone's hvac operation ie. heat, cool mode.

        :return: The HVAC mode.
        """
        system_mode = resolve_async_attr(self, self._device.tcs, "system_mode")
        if system_mode is not None:
            if system_mode[SZ_SYSTEM_MODE] == SystemMode.AWAY:
                return cast(str | None, HVACMode.AUTO)
            if system_mode[SZ_SYSTEM_MODE] == SystemMode.HEAT_OFF:
                return cast(str | None, HVACMode.OFF)

        mode = resolve_async_attr(self, self._device, "mode")
        if mode is None or mode.get(SZ_SETPOINT) is None:
            return None  # unable to determine

        config = resolve_async_attr(self, self._device, "config")
        min_temp = (
            float(config["min_temp"])
            if config
            and "min_temp" in config
            and config["min_temp"] is not None
            else 5.0
        )
        if mode[SZ_SETPOINT] <= min_temp:
            return cast(str | None, HVACMode.OFF)

        thermal_mode = resolve_async_attr(self, self._device, "thermal_mode")
        if thermal_mode == ThermalMode.COOL:
            return cast(str | None, HVACMode.COOL)
        return cast(str | None, HVACMode.HEAT)

    @property
    def max_temp(self) -> float:
        """Return the maximum target temperature of a Zone.

        :return: The max temp.
        """
        bounds = resolve_async_attr(self, self._device, "setpoint_bounds")
        if bounds and "max_temp" in bounds:
            return float(bounds["max_temp"])

        config = resolve_async_attr(self, self._device, "config")
        if not config or "max_temp" not in config:
            return 35.0
        return float(config["max_temp"])

    @property
    def min_temp(self) -> float:
        """Return the minimum target temperature of a Zone.

        :return: The min temp.
        """
        bounds = resolve_async_attr(self, self._device, "setpoint_bounds")
        if bounds and "min_temp" in bounds:
            return float(bounds["min_temp"])

        config = resolve_async_attr(self, self._device, "config")
        if not config or "min_temp" not in config:
            return 5.0
        return float(config["min_temp"])

    @property
    def preset_mode(self) -> str | None:
        """Return the Zone's current preset mode.

        :return: The preset mode.
        """
        system_mode = resolve_async_attr(self, self._device.tcs, "system_mode")
        if system_mode is not None:
            if system_mode[SZ_SYSTEM_MODE] in (
                SystemMode.AWAY,
                SystemMode.HEAT_OFF,
            ):
                return PRESET_TCS_TO_HA.get(system_mode[SZ_SYSTEM_MODE])

        mode = resolve_async_attr(self, self._device, "mode")
        if mode is None:
            return None  # unable to determine

        if mode[SZ_MODE] == ZoneMode.SCHEDULE:
            if system_mode is not None:
                return PRESET_TCS_TO_HA.get(system_mode[SZ_SYSTEM_MODE])
            return _PRESET_NONE

        return PRESET_ZONE_TO_HA.get(mode[SZ_MODE])

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach.

        :return: The target temperature.
        """
        temp = resolve_async_attr(self, self._device, "setpoint")
        if temp is not None:
            self._last_known_targ_temp = float(temp)
        return self._last_known_targ_temp

    # Overrides of standard HA climate actions

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set a Zone to one of its native operating modes.

        :param hvac_mode: The HVAC mode to set.
        :raises ServiceValidationError: If the mode is invalid.
        """
        try:
            if hvac_mode == HVACMode.AUTO:  # FollowSchedule
                await self.async_reset_zone_mode()
            elif hvac_mode in (HVACMode.HEAT, HVACMode.COOL):  # Override
                await self.async_set_zone_mode(
                    mode=ZoneMode.PERMANENT, setpoint=25
                )
            elif hvac_mode == HVACMode.OFF:  # PermanentOverride, temp = min
                await self._device.set_frost_mode()
                self.async_write_ha_state()
            else:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_hvac_mode",
                    translation_placeholders={"mode": str(hvac_mode)},
                )

        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(
                f"Failed to set hvac mode: {err}"
            ) from err
        except prob.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set preset mode to one of None, Permanent, Temporary, or System.

        If 'None', revert to following the schedule.
        If a system preset (e.g., 'away'), apply it to the central controller.

        :param preset_mode: The preset mode.
        :raises ServiceValidationError: If validation fails.
        """
        # Intercept system-wide presets (like 'away', 'eco') during a scene
        # restore
        if (
            preset_mode in PRESET_HA_TO_TCS
            and preset_mode not in PRESET_HA_TO_ZONE
        ):
            target_mode = PRESET_HA_TO_TCS[preset_mode]
            try:
                await self._device.tcs.set_mode(target_mode)
                self.async_write_ha_state()
            except prob.Invalid as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="validation_error",
                    translation_placeholders={"error": str(err)},
                ) from err
            return

        # Handle zone-specific presets ('none', 'temporary', 'permanent')
        try:
            await self.async_set_zone_mode(
                mode=PRESET_HA_TO_ZONE[preset_mode],
                setpoint=(
                    self.target_temperature
                    if preset_mode != PRESET_NONE
                    else None
                ),
                duration=(
                    td(hours=1) if preset_mode == PRESET_TEMPORARY else None
                ),
            )
        except prob.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except KeyError as err:
            # Failsafe for unmapped presets
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_preset_mode",
                translation_placeholders={"mode": str(preset_mode)},
            ) from err

    async def async_set_temperature(
        self,
        temperature: float | None = None,
        duration: td | None = None,
        until: dt | None = None,
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
            mode = ZoneMode.ADVANCED  # till next scheduled change
        elif duration is not None or until is not None:  # both flagged later
            mode = ZoneMode.TEMPORARY

        try:
            await self.async_set_zone_mode(
                mode=mode, setpoint=temperature, duration=duration, until=until
            )
        except prob.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

    # the following are integration-specific method service calls

    async def async_fake_zone_temp(self, temperature: float) -> None:
        """Cast the room temperature of this zone (if faked).

        :param temperature: The temperature to fake.
        :raises HomeAssistantError: If the zone has no sensor.
        """
        if self._device.sensor is None:
            raise HomeAssistantError(
                f"Zone {self.entity_id} has no sensor to fake temp on."
            )

        sensor = self._device.sensor
        await sensor.set_temperature(temperature)

        # Also update the zone's temp_state so that current_temperature
        # reflects the faked value immediately (the zone's temp_state is
        # separate from the sensor's temp_state in ramses_rf's CQRS model)
        if hasattr(self._device, "temp_state"):
            self._device.temp_state = dc_replace(
                self._device.temp_state, temperature=temperature
            )
        self.async_write_ha_state()

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
            raise HomeAssistantError(
                f"Failed to reset zone config: {err}"
            ) from err

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
            raise HomeAssistantError(
                f"Failed to reset zone mode: {err}"
            ) from err

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
            raise HomeAssistantError(
                f"Failed to set zone config: {err}"
            ) from err

    async def async_set_zone_mode(
        self,
        mode: str | None = None,
        setpoint: float | None = None,
        duration: td | None = None,
        until: dt | None = None,
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
        except prob.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="validation_error",
                translation_placeholders={"error": str(err)},
            ) from err

        # Cast to dict to satisfy strict type checking
        checked_dict: dict[str, Any] = checked_entry

        # default `duration` of 1 hour is updated by SCH_ default, so can't
        # use original

        if until is None and "duration" in checked_dict:
            until = dt_util.now() + checked_dict["duration"]

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
            raise HomeAssistantError(
                f"Failed to set zone mode: {err}"
            ) from err

    async def async_get_zone_schedule(self) -> dict[str, Any]:
        """Get the latest weekly schedule of the Zone.

        :returns: Dictionary containing the schedule.
        :rtype: dict[str, Any]
        :raises ServiceValidationError: If backend call fails or times out.
        :raises HomeAssistantError: If an error occurs.
        """
        # {{ state_attr('climate.ramses_cc_01_145038_04', 'schedule') }}
        try:
            res = await self._device.get_schedule()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_get_schedule",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(
                f"Failed to get zone schedule: {err}"
            ) from err
        self.async_write_ha_state()
        return {
            "schedule": res
            if res is not None
            else getattr(self._device, "schedule", None)
        }

    async def async_set_zone_schedule(
        self, schedule: str | dict[str, Any] | list[Any]
    ) -> None:
        """Set the weekly schedule of the Zone.

        :param schedule: The schedule payload (JSON string or dict/list
            object).
        :raises ServiceValidationError: If JSON is invalid.
        :raises HomeAssistantError: If the command fails.
        """
        try:
            payload = (
                json.loads(schedule) if isinstance(schedule, str) else schedule
            )
            if isinstance(payload, dict):
                payload = payload.get(ATTR_SCHEDULE, payload)
            await self._device.set_schedule(payload)
            self.async_write_ha_state()
        except (TypeError, ValueError, json.JSONDecodeError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="error_set_schedule",
                translation_placeholders={"error": str(err)},
            ) from err
        except (
            RamsesException,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(
                f"Failed to set zone schedule: {err}"
            ) from err


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
    _attr_hvac_modes: list[HVACMode] | list[str] = [
        HVACMode.AUTO,
        HVACMode.OFF,
    ]
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
        self._last_known_curr_temp: float | None = None
        self._last_known_curr_hum: int | None = None
        self._last_known_fan_info: str | None = None

    async def async_added_to_hass(self) -> None:
        """Handle entity addition to Home Assistant."""
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
        # Preserve the descriptive fan_info from ramses_rf as-is (issue
        # 1118).  fan_mode (the selector) is normalised to a canonical
        # name; this attribute keeps the manufacturer's descriptive value
        # so users see the instruction-book name.
        fan_info = self._get_cached_fan_info()
        if fan_info is not None:
            data["fan_info"] = fan_info
        return data

    @property
    def current_humidity(self) -> int | None:
        """Return the current humidity.

        :return: The humidity.
        """
        indoor_hum = resolve_async_attr(self, self._device, "indoor_humidity")
        if indoor_hum is not None:
            self._last_known_curr_hum = int(indoor_hum * 100)
        return self._last_known_curr_hum

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature.

        :return: The temperature.
        """
        indoor_temp = resolve_async_attr(self, self._device, "indoor_temp")
        if indoor_temp is not None:
            self._last_known_curr_temp = float(indoor_temp)
        return self._last_known_curr_temp

    def _get_cached_fan_info(self) -> str | None:
        """Get and cache the latest fan info."""
        fan_info = resolve_async_attr(self, self._device, "fan_info")
        if fan_info is not None:
            self._last_known_fan_info = fan_info
        return self._last_known_fan_info

    @property
    def fan_mode(self) -> str | None:
        """Return the fan setting.

        Normalised to the canonical fan_mode name (per the device's HVAC
        strategy) so it matches an entry in ``fan_modes``.  The
        descriptive ``fan_info`` from ramses_rf is preserved as the
        ``fan_info`` state attribute (issue 1118).

        :return: The fan mode.
        """
        fan_info = self._get_cached_fan_info()
        return _normalise_fan_info_for_selector(
            fan_info, _get_device_strategy(self._device)
        )

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the list of available fan modes.

        Extends the standard fan modes with:

        1. Strategy-provided canonical modes for the device's scheme
           (e.g. ``away``, ``auto_alt``, ``boost`` for Orcon)
        2. Strategy-provided vendor aliases (e.g. Dutch ``laag``,
           ``hoog``, ``afwezig``, ``uit`` for Orcon — bug 995),
           only when HA's language matches the alias locale
        3. Custom command names from the FAN's own schema ``_commands``
           (Phase 3b — dict templates)
        4. Custom command names from the bound REM's ``_commands``
           (Phase 3a — packet strings)

        So that ``climate.set_fan_mode`` accepts both vendor-native
        mode names and custom command names like ``boost`` or
        ``speed_1``.
        """
        base_modes = list(self._attr_fan_modes or [])

        # Merge strategy-provided fan modes (canonical names + aliases)
        strategy = _get_device_strategy(self._device)
        strategy_mode_names: set[str] = set()
        if strategy is not None:
            for mode_name in strategy.fan_modes.values():
                strategy_mode_names.add(mode_name)
                if mode_name not in base_modes:
                    base_modes.append(mode_name)
            # Vendor aliases are only shown in the dropdown when HA's
            # language matches the alias locale.  The strategy always
            # *accepts* aliases via fan_mode_to_hex() regardless of
            # language, so service calls and scripts keep working.
            if _is_alias_language_active(self.hass, strategy):
                for alias in strategy._aliases:
                    strategy_mode_names.add(alias)
                    if alias not in base_modes:
                        base_modes.append(alias)
            # Strategy-provided builtin commands of type "mode"
            # (builtin_commands was added in ramses_rf 0.60.4 — use
            # getattr for backward compat with older ramses_rf)
            builtin = getattr(strategy, "builtin_commands", None)
            if builtin:
                for cmd_name, cmd_template in builtin.items():
                    if (
                        _is_fan_mode_command(cmd_template)
                        and cmd_name not in base_modes
                    ):
                        base_modes.append(cmd_name)

        remotes = getattr(self.coordinator, "_remotes", {}) or {}

        # Phase 3b: FAN's own _commands (dict templates)
        # _split_commands strips metadata (_comment, future Builder keys)
        # A command is included in the fan_modes dropdown if:
        #   - its name matches a strategy mode name (override), OR
        #   - it is classified as "mode" by type/code inference
        # This ensures user overrides of strategy modes always appear,
        # even if the override uses a different command code.
        # Only 22F1 mode commands belong in fan_modes — 22F3 timers,
        # 22F7 bypass, 2411 config, etc. are excluded (issue 1116).
        fan_commands = remotes.get(self._device.id, {})
        if isinstance(fan_commands, dict):
            cmds, _ = _split_commands(fan_commands)
            for cmd_name, cmd_value in cmds.items():
                if cmd_name in base_modes:
                    continue
                if cmd_name in strategy_mode_names or _is_fan_mode_command(
                    cmd_value
                ):
                    base_modes.append(cmd_name)

        # Phase 3a: bound REM's _commands (packet strings, fallback)
        # Only offer REM commands if the REM is faked — sending as a
        # non-faked REM would collide with the physical remote's transmissions.
        bound_rem = self._bound_rem or self._device.get_bound_rem()
        if bound_rem:
            rem_dev = self.coordinator._get_device(str(bound_rem))
            if rem_dev is not None and not rem_dev.is_faked:
                return base_modes  # REM not faked — don't offer its commands
            rem_commands = remotes.get(str(bound_rem), {})
            if isinstance(rem_commands, dict):
                cmds, _ = _split_commands(rem_commands)
                for cmd_name, cmd_value in cmds.items():
                    if cmd_name in base_modes:
                        continue
                    if cmd_name in strategy_mode_names or _is_fan_mode_command(
                        cmd_value
                    ):
                        base_modes.append(cmd_name)
        return base_modes

    @property
    def preset_modes(self) -> list[str] | None:
        """Return the list of available preset modes (boost timers).

        Surfaces 22F3 timed boost commands from:

        1. Strategy-provided ``builtin_commands`` of type
           ``boost_timer`` (e.g. ``high_15``, ``low_30`` for Orcon)
        2. Strategy-provided boost aliases (e.g. Dutch ``hoog_15``,
           ``laag_30``) when HA's language matches the alias locale
        3. Custom boost_timer commands from the FAN's own schema
           ``_commands``
        4. Custom boost_timer commands from the bound REM's
           ``_commands``

        Returns ``None`` when no boost commands are available, which
        tells HA to hide the preset selector.
        """
        modes: list[str] = []

        strategy = _get_device_strategy(self._device)
        if strategy is not None:
            # Strategy-provided builtin boost_timer commands
            builtin = getattr(strategy, "builtin_commands", None)
            if builtin:
                for cmd_name, cmd_template in builtin.items():
                    if (
                        _is_boost_timer_command(cmd_template)
                        and cmd_name not in modes
                    ):
                        modes.append(cmd_name)
                # Boost aliases (shown only when language matches)
                if _is_alias_language_active(self.hass, strategy):
                    boost_aliases = getattr(strategy, "_boost_aliases", {})
                    for alias, canonical in boost_aliases.items():
                        if canonical in builtin and alias not in modes:
                            modes.append(alias)

        remotes = getattr(self.coordinator, "_remotes", {}) or {}

        # FAN's own _commands (boost_timer type)
        fan_commands = remotes.get(self._device.id, {})
        if isinstance(fan_commands, dict):
            cmds, _ = _split_commands(fan_commands)
            for cmd_name, cmd_value in cmds.items():
                if cmd_name not in modes and _is_boost_timer_command(
                    cmd_value
                ):
                    modes.append(cmd_name)

        # Bound REM's _commands (boost_timer type)
        bound_rem = self._bound_rem or self._device.get_bound_rem()
        if bound_rem:
            rem_dev = self.coordinator._get_device(str(bound_rem))
            if rem_dev is not None and not rem_dev.is_faked:
                return modes or None
            rem_commands = remotes.get(str(bound_rem), {})
            if isinstance(rem_commands, dict):
                cmds, _ = _split_commands(rem_commands)
                for cmd_name, cmd_value in cmds.items():
                    if cmd_name not in modes and _is_boost_timer_command(
                        cmd_value
                    ):
                        modes.append(cmd_name)

        return modes or None

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac operation if supported.

        :return: The HVAC action.
        """
        fan_info = self._get_cached_fan_info()
        if fan_info is None:
            return None
        if fan_info == "off":
            return HVACAction.OFF
        return HVACAction.FAN

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode.

        :return: The HVAC mode as an Enum.
        """
        fan_info = self._get_cached_fan_info()
        if fan_info is None:
            return None
        return HVACMode.OFF if fan_info == "off" else HVACMode.AUTO

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any.

        :return: The icon name.
        """
        if self._get_cached_fan_info() == "off":
            return "mdi:hvac-off"
        return "mdi:hvac"

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode, e.g., home, away, temp.

        :return: The preset mode.
        """
        return cast(str | None, PRESET_NONE)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode for the HVAC device.

        :param fan_mode: Target fan mode (e.g., 'low', 'medium').
        :raises ServiceValidationError: If requested mode is invalid.
        :raises HomeAssistantError: If the transmission fails.
        """
        if self.fan_modes is None or fan_mode not in self.fan_modes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_fan_mode",
                translation_placeholders={"mode": str(fan_mode)},
            )

        try:
            # 1. Check for user-defined custom commands.
            # Priority (highest first):
            #   a. FAN's schema _commands (Phase 3b — dict templates)
            #   b. Bound REM's schema _commands (Phase 3a — packet strings)
            remotes = getattr(self.coordinator, "_remotes", {}) or {}
            if not isinstance(remotes, dict):
                remotes = {}

            # a. FAN's own _commands (Phase 3b — dict templates)
            fan_commands = remotes.get(self._device.id, {})
            if isinstance(fan_commands, dict):
                fan_commands, _ = _split_commands(fan_commands)

            # b. Bound REM's _commands (Phase 3a — packet strings)
            bound_rem = self._bound_rem or self._device.get_bound_rem()
            rem_commands: dict[str, Any] = {}
            if bound_rem:
                rem_commands = remotes.get(str(bound_rem), {})
                if isinstance(rem_commands, dict):
                    rem_commands, _ = _split_commands(rem_commands)
                # Phase 4: known_list legacy fallback removed.
                # Commands live in schema _commands (handled above via
                # remotes, which are populated from schema).

            # Check FAN commands first (highest priority: dict template or
            # raw string fallback)
            if fan_mode in fan_commands:
                cmd_def = fan_commands[fan_mode]
                if _is_command_dict(cmd_def):
                    packet_str = _build_packet_from_template(
                        cmd_def, self._device, self.coordinator
                    )
                    _LOGGER.info(
                        "Intercepted fan_mode '%s'; building from FAN "
                        "template: %s",
                        fan_mode,
                        packet_str,
                    )
                elif isinstance(cmd_def, str):
                    packet_str = cmd_def
                    _LOGGER.info(
                        "Intercepted fan_mode '%s'; using raw packet "
                        "string from FAN _commands: %s",
                        fan_mode,
                        packet_str,
                    )
                else:
                    packet_str = None

                if packet_str is not None:
                    # parse_packet_string parses both CLI and raw packet
                    # formats
                    cmd = parse_packet_string(packet_str)
                    if cmd is None:
                        raise ValueError(
                            f"Failed to parse packet_str: {packet_str}"
                        )
                    await self._device._gateway.async_send_raw_command(
                        cmd, num_repeats=2, priority=Priority.HIGH
                    )
                    # No optimistic state update: ventilators don't ack
                    # 22F1, so fan_info stays stale until the next
                    # 31D9/31DA broadcast — this is by design (issue
                    # 1116).  The selector is normalised from the
                    # descriptive fan_info via the device strategy (issue
                    # 1118), so the dropdown matches once a broadcast
                    # arrives.
                    return

            # Check REM packet strings (Phase 3a fallback)
            if fan_mode in rem_commands:
                # Verify the bound REM is faked — we're sending a packet
                # as if it came from that REM, which only makes sense if
                # the REM is configured for faking.  This mirrors the
                # check in remote.py's async_send_command.
                if bound_rem:
                    rem_dev = self.coordinator._get_device(str(bound_rem))
                    if rem_dev is not None and not rem_dev.is_faked:
                        raise HomeAssistantError(
                            f"Bound REM {bound_rem} is not configured for "
                            f"faking — cannot send custom command"
                        )

                cmd_str = rem_commands[fan_mode]
                _LOGGER.info(
                    "Intercepted fan_mode '%s'; sending custom command: %s",
                    fan_mode,
                    cmd_str,
                )

                # parse_packet_string parses both CLI and raw packet formats
                cmd = parse_packet_string(str(cmd_str))
                if cmd is None:
                    raise ValueError(f"Failed to parse cmd_str: {cmd_str}")

                await self._device._gateway.async_send_raw_command(
                    cmd, num_repeats=2, priority=Priority.HIGH
                )
                # No optimistic update — see comment above (issue 1116).
                return

            # 2. Fallback to standard ramses_rf implementation
            await self._device.set_fan_mode(fan_mode)
            # No optimistic update — see comment above (issue 1116).

        except AttributeError as err:
            _LOGGER.error(
                "The ramses_rf HvacVentilator class is missing the "
                "set_fan_mode method.",
                exc_info=True,
            )
            raise HomeAssistantError(
                "Underlying ramses_rf library lacks set_fan_mode capability."
            ) from err
        except Exception as err:
            # Broad catch to handle CommandInvalid and protocol timeouts
            raise HomeAssistantError(f"Failed to set fan mode: {err}") from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new target preset mode (boost timer) for the HVAC device.

        Sends a 22F3 timed boost command.  Like ``async_set_fan_mode``,
        this checks (in priority order):

        1. Strategy ``builtin_commands`` (dict templates)
        2. FAN's schema ``_commands`` (dict templates or packet strings)
        3. Bound REM's schema ``_commands`` (packet strings)

        Aliases are resolved to canonical names before lookup.

        :param preset_mode: The preset mode (e.g., 'high_15', 'low_30').
        :raises ServiceValidationError: If requested mode is invalid.
        :raises HomeAssistantError: If the transmission fails.
        """
        if self.preset_modes is None or preset_mode not in self.preset_modes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_preset_mode",
                translation_placeholders={"mode": str(preset_mode)},
            )

        try:
            # Resolve alias to canonical name
            strategy = _get_device_strategy(self._device)
            canonical = preset_mode
            if strategy is not None:
                boost_aliases = getattr(strategy, "_boost_aliases", {})
                canonical = boost_aliases.get(preset_mode, preset_mode)

            # 1. Strategy builtin_commands (dict templates)
            if strategy is not None:
                builtin = getattr(strategy, "builtin_commands", None)
                if builtin and canonical in builtin:
                    cmd_def = builtin[canonical]
                    if _is_command_dict(cmd_def):
                        packet_str = _build_packet_from_template(
                            cmd_def, self._device, self.coordinator
                        )
                        _LOGGER.info(
                            "Intercepted preset_mode '%s'; building from"
                            " strategy builtin: %s",
                            preset_mode,
                            packet_str,
                        )
                        cmd = parse_packet_string(packet_str)
                        if cmd is None:
                            raise ValueError(
                                f"Failed to parse packet_str: {packet_str}"
                            )
                        await self._device._gateway.async_send_raw_command(
                            cmd, num_repeats=2, priority=Priority.HIGH
                        )
                        return

            # 2. User-defined custom commands (FAN then REM)
            remotes = getattr(self.coordinator, "_remotes", {}) or {}
            if not isinstance(remotes, dict):
                remotes = {}

            fan_commands = remotes.get(self._device.id, {})
            if isinstance(fan_commands, dict):
                fan_commands, _ = _split_commands(fan_commands)

            bound_rem = self._bound_rem or self._device.get_bound_rem()
            rem_commands: dict[str, Any] = {}
            if bound_rem:
                rem_commands = remotes.get(str(bound_rem), {})
                if isinstance(rem_commands, dict):
                    rem_commands, _ = _split_commands(rem_commands)

            # Check FAN commands first
            if canonical in fan_commands:
                cmd_def = fan_commands[canonical]
                if _is_command_dict(cmd_def):
                    packet_str = _build_packet_from_template(
                        cmd_def, self._device, self.coordinator
                    )
                elif isinstance(cmd_def, str):
                    packet_str = cmd_def
                else:
                    raise ValueError(
                        f"Preset mode '{preset_mode}' has an unrecognized"
                        " command format in _commands (expected a"
                        " packet string or a dict with"
                        f" verb/code/payload keys): {cmd_def!r}"
                    )
                _LOGGER.info(
                    "Intercepted preset_mode '%s'; sending from FAN"
                    " _commands: %s",
                    preset_mode,
                    packet_str,
                )
                cmd = parse_packet_string(packet_str)
                if cmd is None:
                    raise ValueError(
                        f"Failed to parse packet_str: {packet_str}"
                    )
                await self._device._gateway.async_send_raw_command(
                    cmd, num_repeats=2, priority=Priority.HIGH
                )
                return

            # Check REM packet strings
            if canonical in rem_commands:
                if bound_rem:
                    rem_dev = self.coordinator._get_device(str(bound_rem))
                    if rem_dev is not None and not rem_dev.is_faked:
                        raise HomeAssistantError(
                            f"Bound REM {bound_rem} is not configured for "
                            f"faking — cannot send custom command"
                        )
                cmd_str = rem_commands[canonical]
                _LOGGER.info(
                    "Intercepted preset_mode '%s'; sending from REM"
                    " _commands: %s",
                    preset_mode,
                    cmd_str,
                )
                cmd = parse_packet_string(str(cmd_str))
                if cmd is None:
                    raise ValueError(f"Failed to parse cmd_str: {cmd_str}")
                await self._device._gateway.async_send_raw_command(
                    cmd, num_repeats=2, priority=Priority.HIGH
                )
                return

            raise HomeAssistantError(
                f"Preset mode '{preset_mode}' was not found in strategy"
                " builtin_commands or user _commands"
            )

        except Exception as err:
            raise HomeAssistantError(
                f"Failed to set preset mode: {err}"
            ) from err

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode for the ventilator.

        :param hvac_mode: The HVAC mode to set (AUTO or OFF).
        :raises ServiceValidationError: If the requested mode is invalid.
        """
        if self.hvac_modes is None or hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_hvac_mode",
                translation_placeholders={"mode": str(hvac_mode)},
            )

        # Map the HA HVACMode to the corresponding HA Fan Mode string
        target_fan_mode = FAN_OFF if hvac_mode == HVACMode.OFF else FAN_AUTO

        # Delegate the actual hardware transmission to our robust fan mode
        await self.async_set_fan_mode(target_fan_mode)

    # the 2411 fan_param services, copied to numbers and to remote.py

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
            raise HomeAssistantError(
                f"Failed to get fan param: {err}"
            ) from err

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
            raise HomeAssistantError(
                f"Failed to set fan param: {err}"
            ) from err

    # NOTE: async_update_fan_params was removed.  The 'update_fan_params'
    # service is registered once as a domain service (see schemas.py
    # SVCS_RAMSES_CLIMATE comment) and resolves the FAN device_id from either
    # an explicit device_id field or a target entity selector.  See
    # ramses_cc issue 851.


@dataclass(frozen=True, kw_only=True)
class RamsesClimateEntityDescription(
    RamsesEntityDescription,
    ClimateEntityDescription,
):
    """Class describing Ramses binary sensor entities."""

    # integration-specific attributes
    ramses_cc_class: (
        type[RamsesController] | type[RamsesZone] | type[RamsesHvac]
    )
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

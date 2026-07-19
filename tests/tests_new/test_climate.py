"""Tests for the ramses_cc climate platform to achieve 100% coverage."""

from datetime import datetime as dt, timedelta as td
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_NONE,
    HVACAction,
    HVACMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.ramses_cc.climate import (
    RamsesController,
    RamsesHvac,
    RamsesZone,
    SystemMode,
    ZoneMode,
    async_setup_entry,
)
from custom_components.ramses_cc.const import (
    ATTR_DEVICE_ID,
    CONF_COMMANDS,
    DOMAIN,
    PRESET_PERMANENT,
    PRESET_TEMPORARY,
    SZ_KNOWN_LIST,
)
from ramses_rf.devices import HvacVentilator
from ramses_rf.models import TemperatureState
from ramses_rf.systems.tcs import Evohome
from ramses_rf.systems.zones import Zone
from ramses_tx.const import SZ_MODE, SZ_SETPOINT, SZ_SYSTEM_MODE
from ramses_tx.exceptions import ProtocolSendFailed, TransportError

# Constants
SZ_HEAT_DEMAND = "heat_demand"


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Return a mock RamsesCoordinator.

    :return: A mock object simulating the RamsesCoordinator.
    """
    coordinator = MagicMock()
    coordinator.async_post_update = MagicMock()
    coordinator.async_register_platform = MagicMock()
    coordinator.async_get_fan_param = AsyncMock()
    coordinator.async_set_fan_param = AsyncMock()
    coordinator.get_all_fan_params = MagicMock()
    return coordinator


@pytest.fixture
def mock_description() -> MagicMock:
    """Return a mock EntityDescription.

    :return: A mock object simulating the RamsesClimateEntityDescription.
    """
    desc = MagicMock()
    # FIX: Assign a concrete string to the key to satisfy the new unique_id
    # logic in entity.py
    desc.key = "controller"
    return desc


async def test_async_setup_entry(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test the platform setup and entity creation callback.

    :param hass: The Home Assistant instance.
    :param mock_coordinator: The mock coordinator fixture.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}
    async_add_entities = MagicMock()

    # Mock async_get_current_platform to avoid RuntimeError in test env
    with patch(
        "custom_components.ramses_cc.climate.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    mock_coordinator.async_register_platform.assert_called_once()
    callback_func = mock_coordinator.async_register_platform.call_args[0][1]

    # Use spec mocks here only to ensure isinstance checks pass during mapping
    dev_evo = MagicMock(spec=Evohome)
    dev_zone = MagicMock(spec=Zone)
    dev_hvac = MagicMock(spec=HvacVentilator)
    dev_evo.id = "01:111"
    dev_zone.id = "04:111"
    dev_hvac.id = "30:111"

    callback_func([dev_evo, dev_zone, dev_hvac])
    assert async_add_entities.call_count == 1
    entities = async_add_entities.call_args[0][0]
    assert len(entities) == 3
    assert isinstance(entities[0], RamsesController)
    assert isinstance(entities[1], RamsesZone)
    assert isinstance(entities[2], RamsesHvac)


async def test_controller_properties_and_attributes(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesController properties, state attributes, and edges.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:123456"
    mock_device.zones = []

    controller = RamsesController(mock_coordinator, mock_device, mock_description)
    assert controller.unique_id == "01:123456"

    # 1. extra_state_attributes
    mock_device.heat_demand = MagicMock(return_value=0.5)
    mock_device.heat_demands = MagicMock(return_value={"01": 0.5})
    mock_device.relay_demands = MagicMock(return_value={"01": 1.0})
    mock_device.system_mode = MagicMock(return_value={SZ_SYSTEM_MODE: SystemMode.AUTO})
    mock_device.tpi_params = MagicMock(return_value={"p": 1})

    attrs = controller.extra_state_attributes
    assert attrs["heat_demand"] == 0.5
    assert attrs["heat_demands"] == {"01": 0.5}
    assert attrs["system_mode"] == {SZ_SYSTEM_MODE: SystemMode.AUTO}

    # Coverage for lines 213-214: system_mode with 'until'
    # Inject a naive datetime to verify fields_to_aware processing
    naive_dt = dt(2023, 1, 1, 12, 0, 0)
    mock_device.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO, "until": naive_dt}
    )
    attrs_until = controller.extra_state_attributes
    # Verify the branch was taken and 'until' exists in the output
    assert "until" in attrs_until["system_mode"]

    # 2. current_temperature logic
    # Case A: Happy Path (calculation successful)
    z1 = MagicMock()
    z1.temperature = MagicMock(return_value=20.0)
    z1.setpoint = MagicMock(return_value=21.0)
    z1.heat_demand = MagicMock(return_value=0.5)

    z2 = MagicMock()
    z2.temperature = MagicMock(return_value=22.0)
    z2.setpoint = MagicMock(return_value=19.0)
    z2.heat_demand = MagicMock(return_value=0.0)

    mock_device.zones = [z1, z2]
    # (20 + 22) / 2 = 21.0
    assert controller.current_temperature == 21.0

    # Coverage for line 190: Zones exist, but have no temp (filtered list is
    # empty)
    z_no_temp = MagicMock()
    z_no_temp.temperature = MagicMock(return_value=None)
    mock_device.zones = [z_no_temp]
    # NEW CACHE LOGIC: Should return the last known good temp (21.0)
    assert controller.current_temperature == 21.0

    # Case B: TypeError logic (sum failure due to invalid type)
    zone_bad = MagicMock()
    zone_bad.temperature = MagicMock(return_value="error")
    mock_device.zones = [zone_bad]
    # NEW CACHE LOGIC: Should return the last known good temp (21.0)
    assert controller.current_temperature == 21.0

    # 3. target_temperature logic (max of zones with demand)
    z1.setpoint = MagicMock(return_value=20.0)
    z1.heat_demand = MagicMock(return_value=None)
    z2.setpoint = MagicMock(return_value=22.0)
    z2.heat_demand = MagicMock(return_value=0.5)

    z3 = MagicMock()
    z3.setpoint = MagicMock(return_value=None)
    z3.heat_demand = MagicMock(return_value=0.5)

    mock_device.zones = [z1, z2, z3]
    assert controller.target_temperature == 22.0

    mock_device.zones = [z1]
    # Filtered out (demand None), temps list is empty -> uses cache 22.0
    assert controller.target_temperature == 22.0


async def test_controller_modes_and_actions(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesController HVAC modes, actions, and presets.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:123456"
    mock_device.zones = []
    controller = RamsesController(mock_coordinator, mock_device, mock_description)

    # 1. hvac_action
    mock_device.system_mode = MagicMock(return_value=None)
    mock_device.heat_demand = MagicMock(return_value=None)
    assert controller.hvac_action is None

    mock_device.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    )
    assert controller.hvac_action == HVACAction.OFF

    mock_device.system_mode = MagicMock(return_value={SZ_SYSTEM_MODE: SystemMode.AUTO})
    mock_device.heat_demand = MagicMock(return_value=0.5)
    assert controller.hvac_action == HVACAction.HEATING

    mock_device.heat_demand = MagicMock(return_value=0)
    assert controller.hvac_action == HVACAction.IDLE

    mock_device.heat_demand = MagicMock(return_value=None)
    assert controller.hvac_action is None

    # 2. hvac_mode
    mock_device.system_mode = MagicMock(return_value=None)
    assert controller.hvac_mode == HVACMode.HEAT

    mock_device.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    )
    assert controller.hvac_mode == HVACMode.OFF

    mock_device.system_mode = MagicMock(return_value={SZ_SYSTEM_MODE: SystemMode.AWAY})
    assert controller.hvac_mode == HVACMode.AUTO

    mock_device.system_mode = MagicMock(return_value={SZ_SYSTEM_MODE: SystemMode.AUTO})
    assert controller.hvac_mode == HVACMode.HEAT

    # 3. preset_mode
    mock_device.system_mode = MagicMock(return_value=None)
    assert controller.preset_mode == PRESET_NONE

    mock_device.system_mode = MagicMock(return_value={SZ_SYSTEM_MODE: SystemMode.AUTO})
    assert controller.preset_mode == PRESET_NONE

    mock_device.system_mode = MagicMock(return_value={SZ_SYSTEM_MODE: SystemMode.AWAY})
    assert controller.preset_mode == PRESET_AWAY


async def test_controller_services(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_description: MagicMock,
    freezer: Any,
) -> None:
    """Test RamsesController service calls and mode setting logic.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    :param freezer: The freezer fixture to control time.
    """

    # Force HA to UTC to align with freezer's default behavior
    await hass.config.async_set_time_zone("UTC")

    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:000001"
    mock_device.zones = []

    # Update: Ensure async methods are AsyncMock (from new code)
    mock_device.set_mode = AsyncMock()
    mock_device.reset_mode = AsyncMock()
    mock_device.get_faultlog = AsyncMock()

    controller = RamsesController(mock_coordinator, mock_device, mock_description)
    cast(Any, controller).async_write_ha_state_delayed = MagicMock()
    controller.async_write_ha_state = MagicMock()

    # 1. set_hvac_mode and set_preset_mode wrappers
    # Patch the instance method to verify it is called correctly
    with patch.object(controller, "async_set_system_mode") as mock_set_mode:
        await controller.async_set_hvac_mode(HVACMode.HEAT)
        mock_set_mode.assert_called_with(SystemMode.AUTO)

        await controller.async_set_preset_mode(PRESET_AWAY)
        mock_set_mode.assert_called_with(SystemMode.AWAY)

    # 2. async_set_system_mode with 'period' AND 'duration' logic
    with patch("custom_components.ramses_cc.climate.SCH_SET_SYSTEM_MODE_EXTRA"):
        # Case A: Period None
        await controller.async_set_system_mode("auto", period=None)
        # Update: use assert_awaited_with for async methods
        mock_device.set_mode.assert_awaited_with("auto", until=None)

        # Set frozen time for duration/period calculations
        freezer.move_to("2023-01-01 12:00:00")

        # Case B: Duration provided (Coverage for lines 266 & 273)
        test_duration = td(hours=1)
        await controller.async_set_system_mode("auto", duration=test_duration)

        # 12:00 + 1h = 13:00
        expected_until_dur = dt_util.as_utc(dt(2023, 1, 1, 13, 0, 0))
        mock_device.set_mode.assert_awaited_with("auto", until=expected_until_dur)

        # Case C: Period 0 (Next Day)
        zero_period = td(0)
        await controller.async_set_system_mode("auto", period=zero_period)

        # Calculation for next day 00:00:00 local time
        expected_midnight = dt_util.as_utc(dt(2023, 1, 2, 0, 0, 0))
        mock_device.set_mode.assert_awaited_with("auto", until=expected_midnight)

        # Case D: Standard Period
        std_period = td(hours=2)
        await controller.async_set_system_mode("auto", period=std_period)
        # Use dt_util.as_utc to ensure object matches aware datetime from mock
        expected_std_until = dt_util.as_utc(dt(2023, 1, 1, 14, 0, 0))
        mock_device.set_mode.assert_awaited_with("auto", until=expected_std_until)

    # 3. Service Calls
    await controller.async_reset_system_mode()
    mock_device.reset_mode.assert_awaited_once()

    await controller.async_get_system_faults(5)
    mock_device.get_faultlog.assert_awaited_with(limit=5, force_refresh=True)


async def test_zone_properties_and_config(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesZone properties, config, and attributes.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    # Removed spec=Zone because it blocks access to .tcs
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs = MagicMock()

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )
    mock_device.setpoint_bounds = MagicMock(return_value=None)
    mock_device.config = MagicMock(return_value={"min_temp": 5, "max_temp": 35})
    mock_device.temperature = MagicMock(return_value=19.5)
    mock_device.setpoint = MagicMock(return_value=20.0)
    mock_device.heat_demand = MagicMock(return_value=None)

    zone = RamsesZone(mock_coordinator, mock_device, mock_description)

    # Basics
    assert zone.target_temperature == 20.0
    assert zone.current_temperature == 19.5

    # NEW CACHE LOGIC:
    mock_device.temperature = MagicMock(return_value=None)
    mock_device.setpoint = MagicMock(return_value=None)
    assert zone.current_temperature == 19.5
    assert zone.target_temperature == 20.0

    # Config checks (min/max)
    # 1. Fallback when bounds and config are missing
    mock_device.setpoint_bounds = MagicMock(return_value=None)
    mock_device.config = MagicMock(return_value=None)
    assert zone.min_temp == 5.0
    assert zone.max_temp == 35.0

    # 2. Fallback when config is present but missing specific keys
    mock_device.config = MagicMock(return_value={})
    assert zone.min_temp == 5.0
    assert zone.max_temp == 35.0

    # 3. Uses config values when bounds are missing
    mock_device.config = MagicMock(return_value={"min_temp": 10.0, "max_temp": 30.0})
    assert zone.min_temp == 10.0
    assert zone.max_temp == 30.0

    # 4. Prioritizes setpoint_bounds over config
    mock_device.setpoint_bounds = MagicMock(
        return_value={"min_temp": 12.0, "max_temp": 28.0}
    )
    assert zone.min_temp == 12.0
    assert zone.max_temp == 28.0

    # Extra state attributes
    mock_device.params = MagicMock(return_value={"p": 1})
    mock_device.idx = "01"
    mock_device.heating_type = "radiator"
    mock_device.mode = MagicMock(return_value={"m": 1})
    mock_device.schedule = MagicMock(return_value=[])
    mock_device.schedule_version = 1

    attrs = zone.extra_state_attributes
    assert attrs["zone_idx"] == "01"

    # Coverage for mode with 'until'
    naive_dt = dt(2023, 1, 1, 12, 0, 0)
    mock_device.mode = MagicMock(
        return_value={SZ_MODE: ZoneMode.TEMPORARY, "until": naive_dt}
    )
    attrs_until = zone.extra_state_attributes
    # Verify the branch was taken and 'until' exists in the output
    assert "until" in attrs_until["mode"]


async def test_zone_modes_and_actions(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesZone HVAC modes, actions, and presets.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs = MagicMock()

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )
    mock_device.config = MagicMock(return_value={"min_temp": 5, "max_temp": 35})

    zone = RamsesZone(mock_coordinator, mock_device, mock_description)

    # 1. hvac_action
    mock_device.tcs.system_mode = MagicMock(return_value=None)
    mock_device.heat_demand = MagicMock(return_value=None)
    assert zone.hvac_action is None

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    )
    assert zone.hvac_action == HVACAction.OFF

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )
    mock_device.heat_demand = MagicMock(return_value=0.5)
    assert zone.hvac_action == HVACAction.HEATING

    mock_device.heat_demand = MagicMock(return_value=0)
    assert zone.hvac_action == HVACAction.IDLE

    mock_device.heat_demand = MagicMock(return_value=None)
    assert zone.hvac_action is None

    # 2. hvac_mode
    mock_device.tcs.system_mode = MagicMock(return_value=None)
    mock_device.mode = MagicMock(return_value=None)
    assert zone.hvac_mode is None

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AWAY}
    )
    assert zone.hvac_mode == HVACMode.AUTO

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    )
    assert zone.hvac_mode == HVACMode.OFF

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )
    mock_device.mode = MagicMock(return_value=None)
    assert zone.hvac_mode is None

    # Config checks for Off vs Heat
    mock_device.mode = MagicMock(
        return_value={SZ_SETPOINT: 4.0, SZ_MODE: ZoneMode.ADVANCED}
    )
    assert zone.hvac_mode == HVACMode.OFF  # Below min_temp

    mock_device.mode = MagicMock(
        return_value={SZ_SETPOINT: 20.0, SZ_MODE: ZoneMode.ADVANCED}
    )
    assert zone.hvac_mode == HVACMode.HEAT

    # 3. preset_mode

    # Verify combined preset_modes list includes system presets
    assert zone.preset_modes is not None
    assert PRESET_AWAY in zone.preset_modes
    assert PRESET_NONE in zone.preset_modes
    assert PRESET_TEMPORARY in zone.preset_modes

    mock_device.tcs.system_mode = MagicMock(return_value=None)
    mock_device.mode = MagicMock(return_value=None)
    assert zone.preset_mode is None

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AWAY}
    )
    assert zone.preset_mode == PRESET_AWAY

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )
    mock_device.mode = MagicMock(return_value=None)
    assert zone.preset_mode is None

    mock_device.mode = MagicMock(return_value={SZ_MODE: ZoneMode.SCHEDULE})
    assert zone.preset_mode == PRESET_NONE

    mock_device.mode = MagicMock(return_value={SZ_MODE: ZoneMode.TEMPORARY})
    assert zone.preset_mode == "temporary"


async def test_zone_methods_and_services(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_description: MagicMock,
    freezer: Any,
) -> None:
    """Test RamsesZone methods (set_temp, set_mode) and services.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    :param freezer: The freezer fixture.
    """

    await hass.config.async_set_time_zone("UTC")

    mock_device = MagicMock()
    mock_device.id = "04:000001"
    mock_device.tcs = MagicMock()
    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )

    # Update: Ensure async methods are AsyncMock (from new code)
    mock_device.set_mode = AsyncMock()
    mock_device.reset_mode = AsyncMock()
    mock_device.set_config = AsyncMock()
    mock_device.reset_config = AsyncMock()
    mock_device.get_schedule = AsyncMock()
    mock_device.set_schedule = AsyncMock()
    mock_device.set_frost_mode = AsyncMock()

    zone = RamsesZone(mock_coordinator, mock_device, mock_description)
    cast(Any, zone).async_write_ha_state_delayed = MagicMock()
    zone.async_write_ha_state = MagicMock()

    # 1. set_hvac_mode
    # Mock async_set_zone_mode to verify calls
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        # Mock async_reset_zone_mode only for this specific test block
        zone.async_reset_zone_mode = AsyncMock()

        await zone.async_set_hvac_mode(HVACMode.AUTO)
        zone.async_reset_zone_mode.assert_called_once()

        await zone.async_set_hvac_mode(HVACMode.HEAT)
        mock_set.assert_called_with(mode=ZoneMode.PERMANENT, setpoint=25)

        # Update test logic for OFF: it calls set_frost_mode directly now
        mock_set.reset_mock()
        await zone.async_set_hvac_mode(HVACMode.OFF)
        mock_device.set_frost_mode.assert_awaited_once()
        mock_set.assert_not_called()

    # 1a. Explicit coverage for async_reset_zone_mode body
    del zone.async_reset_zone_mode
    await zone.async_reset_zone_mode()
    mock_device.reset_mode.assert_awaited_once()

    # 2. set_preset_mode
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        # A. Zone-specific preset (handled locally)
        await zone.async_set_preset_mode(PRESET_NONE)
        mock_set.assert_called_with(
            mode=ZoneMode.SCHEDULE, setpoint=None, duration=None
        )

        # B. System-wide preset (routed to TCS - Issue #566)
        mock_set.reset_mock()
        mock_device.tcs.set_mode = AsyncMock()
        await zone.async_set_preset_mode(PRESET_AWAY)

        # Verify it hit the central controller and DID NOT hit the zone
        mock_device.tcs.set_mode.assert_awaited_once_with(SystemMode.AWAY)
        mock_set.assert_not_called()

    # 3. set_temperature variations
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        # A. No args -> Schedule
        await zone.async_set_temperature(temperature=None)
        mock_set.assert_called_with(
            mode=ZoneMode.SCHEDULE, setpoint=None, duration=None, until=None
        )
        # B. Temp only -> Advanced
        await zone.async_set_temperature(temperature=21.0)
        mock_set.assert_called_with(
            mode=ZoneMode.ADVANCED, setpoint=21.0, duration=None, until=None
        )
        # C. Duration -> Temporary
        dur = td(hours=1)
        await zone.async_set_temperature(temperature=21.0, duration=dur)
        mock_set.assert_called_with(
            mode=ZoneMode.TEMPORARY, setpoint=21.0, duration=dur, until=None
        )
        # D. Until -> Temporary (Covering 'or until is not None' branch)
        until = dt(2023, 1, 1, 12, 0, 0)
        await zone.async_set_temperature(temperature=21.0, until=until)
        mock_set.assert_called_with(
            mode=ZoneMode.TEMPORARY, setpoint=21.0, duration=None, until=until
        )

    # 4. async_set_zone_mode logic (calculating 'until' from duration)
    # We patch SCH_SET_ZONE_MODE_EXTRA to control schema validation return
    # values
    with patch("custom_components.ramses_cc.climate.SCH_SET_ZONE_MODE_EXTRA") as m_sch:
        # Case: Just setpoint (schema returns input)
        m_sch.side_effect = lambda x: x
        await zone.async_set_zone_mode(setpoint=21.0)
        mock_device.set_mode.assert_awaited_with(mode=None, setpoint=21.0, until=None)

        # Case: Duration provided (schema returns dict with duration)
        m_sch.side_effect = None
        m_sch.return_value = {"duration": td(hours=1)}
        freezer.move_to("2023-01-01 12:00:00")

        await zone.async_set_zone_mode(mode="temp", duration=td(hours=1))

        # Expected is now 13:00 UTC
        expected_until = dt_util.as_utc(dt(2023, 1, 1, 13, 0, 0))
        mock_device.set_mode.assert_awaited_with(
            mode="temp", setpoint=None, until=expected_until
        )

        # Case: Duration provided BUT until is ALSO provided
        # if until is None and "duration" in checked_entry: -> False because
        # until is NOT None
        m_sch.return_value = {"duration": td(hours=1)}
        explicit_until = dt(2023, 1, 1, 15, 0, 0)
        await zone.async_set_zone_mode(
            mode="temp", duration=td(hours=1), until=explicit_until
        )
        # Expectation: The loop calculation for until is SKIPPED, uses
        # explicit_until
        mock_device.set_mode.assert_awaited_with(
            mode="temp", setpoint=None, until=explicit_until
        )

    # 5. Miscellaneous Services
    # async_fake_zone_temp
    mock_device.sensor = None
    with pytest.raises(HomeAssistantError):
        await zone.async_fake_zone_temp(20.0)

    mock_device.sensor = AsyncMock()
    mock_device.temp_state = TemperatureState()
    await zone.async_fake_zone_temp(22.5)
    mock_device.sensor.set_temperature.assert_awaited_with(22.5)
    assert mock_device.temp_state.temperature == 22.5

    # Config / Schedule
    await zone.async_reset_zone_config()
    mock_device.reset_config.assert_awaited_once()

    await zone.async_set_zone_config(min_temp=10)
    mock_device.set_config.assert_awaited_with(min_temp=10)

    await zone.async_get_zone_schedule()
    mock_device.get_schedule.assert_awaited_once()

    await zone.async_set_zone_schedule('{"day": 1}')
    mock_device.set_schedule.assert_awaited_once()


async def test_hvac_properties_and_modes(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac properties and mode determination.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:654321"

    mock_device.indoor_humidity = MagicMock(return_value=0.55)
    mock_device.indoor_temp = MagicMock(return_value=21.5)
    mock_device.fan_info = MagicMock(return_value=None)
    mock_device.get_bound_rem = MagicMock(return_value="30:987654")

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)

    # 1. async_added_to_hass
    with patch(
        "custom_components.ramses_cc.climate.RamsesEntity.async_added_to_hass",
        new_callable=AsyncMock,
    ) as mock_added:
        await hvac.async_added_to_hass()
        mock_added.assert_awaited()
        # Ensure underlying method was called
        mock_device.get_bound_rem.assert_called()
        assert hvac._bound_rem == "30:987654"

    # 2. Properties
    assert hvac.current_humidity == 55

    mock_device.indoor_humidity = MagicMock(return_value=None)
    # NEW CACHE LOGIC:
    assert hvac.current_humidity == 55

    assert hvac.current_temperature == 21.5

    mock_device.indoor_temp = MagicMock(return_value=None)
    # NEW CACHE LOGIC:
    assert hvac.current_temperature == 21.5

    assert hvac.preset_mode == PRESET_NONE

    attrs = hvac.extra_state_attributes
    assert attrs["bound_rem"] == "30:987654"

    # 3. Mode/Action Logic
    # Fan Info None (Initial state without cache)
    mock_device.fan_info = MagicMock(return_value=None)
    assert hvac.hvac_mode is None
    assert hvac.fan_mode is None

    # Fan Off
    mock_device.fan_info = MagicMock(return_value="off")
    assert hvac.hvac_mode == HVACMode.OFF
    assert hvac.icon == "mdi:hvac-off"

    # Fan Low
    mock_device.fan_info = MagicMock(return_value="low")
    assert hvac.hvac_mode == HVACMode.AUTO
    assert hvac.icon == "mdi:hvac"
    assert hvac.hvac_action == "low"
    assert hvac.fan_mode == "low"

    # NEW CACHE LOGIC: Dropped fan info retains cached "low"
    mock_device.fan_info = MagicMock(return_value=None)
    assert hvac.hvac_mode == HVACMode.AUTO
    assert hvac.fan_mode == "low"


async def test_hvac_services(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac specific service calls.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)

    # async_get_fan_clim_param
    await hvac.async_get_fan_clim_param(param="p1")
    mock_coordinator.async_get_fan_param.assert_called_with(
        {"param": "p1", ATTR_DEVICE_ID: mock_device.id}
    )

    # async_set_fan_clim_param
    await hvac.async_set_fan_clim_param(param="p1", value=1)
    mock_coordinator.async_set_fan_param.assert_called_with(
        {"param": "p1", "value": 1, ATTR_DEVICE_ID: mock_device.id}
    )

    # async_update_fan_params
    await hvac.async_update_fan_params()
    mock_coordinator.get_all_fan_params.assert_called_with(
        {ATTR_DEVICE_ID: mock_device.id}
    )


async def test_error_handling(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test protocol/transport errors raise HomeAssistantError.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:999999"
    mock_device.zones = []

    mock_device.reset_mode = AsyncMock()
    mock_device.set_mode = AsyncMock()
    mock_device.get_faultlog = AsyncMock()

    controller = RamsesController(mock_coordinator, mock_device, mock_description)
    cast(Any, controller).async_write_ha_state_delayed = MagicMock()

    # Define a list of methods and the mock target to fail
    # (method_to_call, args, mock_method_name_on_device)
    test_cases = [
        (controller.async_reset_system_mode, [], "reset_mode"),
        (controller.async_set_system_mode, ["auto"], "set_mode"),
        (controller.async_get_system_faults, [5], "get_faultlog"),
    ]

    for method, args, device_method_name in test_cases:
        # Case 1: ProtocolSendFailed
        getattr(mock_device, device_method_name).side_effect = ProtocolSendFailed(
            "Send failed"
        )
        with pytest.raises(HomeAssistantError, match="Failed to .*"):
            await method(*args)

        # Case 2: TimeoutError
        getattr(mock_device, device_method_name).side_effect = TimeoutError("Timed out")
        with pytest.raises(HomeAssistantError, match="Failed to .*"):
            await method(*args)

        # Case 3: TransportError
        getattr(mock_device, device_method_name).side_effect = TransportError(
            "Transport error"
        )
        with pytest.raises(HomeAssistantError, match="Failed to .*"):
            await method(*args)

    # Zone Error Handling
    zone_device = MagicMock()
    zone_device.id = "04:888888"

    zone_device.reset_mode = AsyncMock()
    zone_device.reset_config = AsyncMock()
    zone_device.set_config = AsyncMock()
    zone_device.set_mode = AsyncMock()
    zone_device.get_schedule = AsyncMock()
    zone_device.set_schedule = AsyncMock()

    zone = RamsesZone(mock_coordinator, zone_device, mock_description)
    cast(Any, zone).async_write_ha_state_delayed = MagicMock()
    zone.async_write_ha_state = MagicMock()

    zone_cases = [
        (zone.async_reset_zone_mode, [], "reset_mode"),
        (zone.async_reset_zone_config, [], "reset_config"),
        (zone.async_set_zone_config, [], "set_config"),
        (zone.async_set_zone_mode, [ZoneMode.SCHEDULE], "set_mode"),
        (zone.async_get_zone_schedule, [], "get_schedule"),
        (zone.async_set_zone_schedule, ["{}"], "set_schedule"),
    ]

    for method, args, device_method_name in zone_cases:
        getattr(zone_device, device_method_name).side_effect = ProtocolSendFailed(
            "Boom"
        )
        with pytest.raises(HomeAssistantError, match="Failed to .*"):
            await method(*args)

    # HVAC Error Handling
    hvac_device = MagicMock(spec=HvacVentilator)
    hvac_device.id = "30:777777"
    hvac = RamsesHvac(mock_coordinator, hvac_device, mock_description)

    # Coordinator failures
    mock_coordinator.async_get_fan_param.side_effect = ProtocolSendFailed(
        "Coordinator fail"
    )
    with pytest.raises(HomeAssistantError, match="Failed to get fan param"):
        await hvac.async_get_fan_clim_param(param="p")

    mock_coordinator.async_set_fan_param.side_effect = TimeoutError(
        "Coordinator timeout"
    )
    with pytest.raises(HomeAssistantError, match="Failed to set fan param"):
        await hvac.async_set_fan_clim_param(param="p", value=1)


async def test_service_validation_errors(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test ServiceValidationError handling in Controller and Zone.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:999999"
    mock_device.zones = []
    controller = RamsesController(mock_coordinator, mock_device, mock_description)

    # 1. Invalid HVAC Mode
    with pytest.raises(ServiceValidationError, match="invalid_hvac_mode"):
        await controller.async_set_hvac_mode(cast(HVACMode, "invalid_mode"))

    # 2. Invalid Preset Mode
    with pytest.raises(ServiceValidationError, match="invalid_preset_mode"):
        await controller.async_set_preset_mode("invalid_preset")

    # 3. vol.Invalid in async_set_hvac_mode
    with (
        patch.object(
            controller,
            "async_set_system_mode",
            side_effect=vol.Invalid("Boom"),
        ),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await controller.async_set_hvac_mode(HVACMode.HEAT)

    # 4. vol.Invalid in async_set_preset_mode
    with (
        patch.object(
            controller,
            "async_set_system_mode",
            side_effect=vol.Invalid("Boom"),
        ),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await controller.async_set_preset_mode(PRESET_AWAY)

    # Zone Validation Errors
    mock_zone_dev = MagicMock()
    mock_zone_dev.id = "04:123456"
    zone = RamsesZone(mock_coordinator, mock_zone_dev, mock_description)

    # 5. vol.Invalid in async_set_hvac_mode
    # We patch async_set_zone_mode, which is called by async_set_hvac_mode
    with (
        patch.object(zone, "async_set_zone_mode", side_effect=vol.Invalid("Boom")),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await zone.async_set_hvac_mode(HVACMode.HEAT)

    # 6. vol.Invalid in async_set_preset_mode (Zone mode fallback)
    with (
        patch.object(zone, "async_set_zone_mode", side_effect=vol.Invalid("Boom")),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await zone.async_set_preset_mode(PRESET_NONE)

    # 6a. vol.Invalid in async_set_preset_mode (TCS system routing)
    mock_zone_dev.tcs.set_mode = AsyncMock(side_effect=vol.Invalid("Boom"))
    with pytest.raises(ServiceValidationError, match="validation_error"):
        await zone.async_set_preset_mode(PRESET_AWAY)

    # 6b. KeyError for invalid preset fallback
    with pytest.raises(ServiceValidationError, match="invalid_preset_mode"):
        await zone.async_set_preset_mode("invalid_unmapped_preset")

    # 7. vol.Invalid in async_set_temperature
    with (
        patch.object(zone, "async_set_zone_mode", side_effect=vol.Invalid("Boom")),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await zone.async_set_temperature(temperature=20)


async def test_zone_extended_coverage(
    mock_coordinator: MagicMock, mock_description: MagicMock, freezer: Any
) -> None:
    """Test extended Zone logic for presets and config edges.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    :param freezer: The freezer fixture.
    """
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs = MagicMock()

    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )
    mock_device.setpoint = MagicMock(return_value=20.0)

    # Needs async mocks for the awaits
    mock_device.set_mode = AsyncMock()

    zone = RamsesZone(mock_coordinator, mock_device, mock_description)

    # 1. Preset Temporary (1 hour duration)
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        await zone.async_set_preset_mode(PRESET_TEMPORARY)
        mock_set.assert_called_with(
            mode=ZoneMode.TEMPORARY, setpoint=20.0, duration=td(hours=1)
        )

    # 2. Preset Permanent
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        await zone.async_set_preset_mode(PRESET_PERMANENT)
        mock_set.assert_called_with(
            mode=ZoneMode.PERMANENT, setpoint=20.0, duration=None
        )

    # 3. HVAC Mode logic when Config is None
    # Code: if (self._device.config and setpoint <= min): return OFF
    mock_device.config = MagicMock(return_value=None)
    mock_device.mode = MagicMock(
        return_value={SZ_SETPOINT: 4.0, SZ_MODE: ZoneMode.ADVANCED}
    )
    # Should default to HEAT because config check fails (short-circuit)
    assert zone.hvac_mode == HVACMode.HEAT


async def test_controller_immediate_update_on_commands(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test that controller writes state immediately after commands."""
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:123456"
    # Ensure device methods are AsyncMocks so they can be awaited
    mock_device.set_mode = AsyncMock()
    mock_device.reset_mode = AsyncMock()
    mock_device.get_faultlog = AsyncMock()

    controller = RamsesController(mock_coordinator, mock_device, mock_description)
    # Mock the HA state writer to verify it gets called
    controller.async_write_ha_state = MagicMock()

    # 1. Set HVAC Mode (calls set_mode)
    await controller.async_set_hvac_mode(HVACMode.OFF)
    mock_device.set_mode.assert_awaited()
    controller.async_write_ha_state.assert_called()
    controller.async_write_ha_state.reset_mock()

    # 2. Set Preset Mode (calls set_mode)
    await controller.async_set_preset_mode(PRESET_AWAY)
    mock_device.set_mode.assert_awaited()
    controller.async_write_ha_state.assert_called()
    controller.async_write_ha_state.reset_mock()

    # 3. Reset System Mode
    await controller.async_reset_system_mode()
    mock_device.reset_mode.assert_awaited()
    controller.async_write_ha_state.assert_called()
    controller.async_write_ha_state.reset_mock()


async def test_zone_immediate_update_on_commands(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test that the zone writes HA state immediately after commands."""
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs = MagicMock()
    mock_device.tcs.system_mode = MagicMock(
        return_value={SZ_SYSTEM_MODE: SystemMode.AUTO}
    )

    # Ensure device methods are AsyncMocks
    mock_device.set_mode = AsyncMock()
    mock_device.reset_mode = AsyncMock()
    mock_device.set_config = AsyncMock()
    mock_device.reset_config = AsyncMock()
    mock_device.set_schedule = AsyncMock()
    # Correctly mock this as async since it is now awaited
    mock_device.set_frost_mode = AsyncMock()

    zone = RamsesZone(mock_coordinator, mock_device, mock_description)
    zone.async_write_ha_state = MagicMock()

    # 1. Set HVAC Mode: AUTO (calls reset_mode)
    await zone.async_set_hvac_mode(HVACMode.AUTO)
    mock_device.reset_mode.assert_awaited()
    zone.async_write_ha_state.assert_called()
    zone.async_write_ha_state.reset_mock()

    # 2. Set HVAC Mode: HEAT (calls set_mode)
    await zone.async_set_hvac_mode(HVACMode.HEAT)
    mock_device.set_mode.assert_awaited()
    zone.async_write_ha_state.assert_called()
    zone.async_write_ha_state.reset_mock()

    # 3. Set Preset Mode (calls set_mode)
    # Note: Requires target_temperature logic, so we ensure it's not None
    with patch.object(RamsesZone, "target_temperature", new=20.0):
        await zone.async_set_preset_mode(PRESET_TEMPORARY)
    mock_device.set_mode.assert_awaited()
    zone.async_write_ha_state.assert_called()
    zone.async_write_ha_state.reset_mock()

    # 4. Set Temperature (calls set_mode)
    await zone.async_set_temperature(temperature=21.0)
    mock_device.set_mode.assert_awaited()
    zone.async_write_ha_state.assert_called()
    zone.async_write_ha_state.reset_mock()

    # 5. Reset Zone Config
    await zone.async_reset_zone_config()
    mock_device.reset_config.assert_awaited()
    zone.async_write_ha_state.assert_called()
    zone.async_write_ha_state.reset_mock()

    # 6. Set Zone Config
    await zone.async_set_zone_config(min_temp=10)
    mock_device.set_config.assert_awaited()
    zone.async_write_ha_state.assert_called()
    zone.async_write_ha_state.reset_mock()

    # 7. Set Zone Schedule
    await zone.async_set_zone_schedule("{}")
    mock_device.set_schedule.assert_awaited()
    zone.async_write_ha_state.assert_called()
    zone.async_write_ha_state.reset_mock()


async def test_hvac_update_fan_params_coverage(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test update_fan_params specifically to guarantee coverage of args."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:COVERAGE"
    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)

    # Pass specific kwargs to trace the flow through lines 557-558
    await hvac.async_update_fan_params(explicit_arg=True)

    mock_coordinator.get_all_fan_params.assert_called_with(
        {"explicit_arg": True, ATTR_DEVICE_ID: "30:COVERAGE"}
    )


async def test_zone_set_hvac_mode_error(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test error handling specifically for set_hvac_mode (HVACMode.OFF)."""
    mock_device = MagicMock()
    mock_device.id = "04:ERROR_MODE"
    # Ensure set_frost_mode fails with a transport exception
    mock_device.set_frost_mode = AsyncMock(
        side_effect=ProtocolSendFailed("Transport failed")
    )

    zone = RamsesZone(mock_coordinator, mock_device, mock_description)

    with pytest.raises(HomeAssistantError, match="Failed to set hvac mode"):
        await zone.async_set_hvac_mode(HVACMode.OFF)


async def test_extra_schema_validation(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test schema validation failures raise ServiceValidationError."""
    # 1. Controller: async_set_system_mode
    mock_ctl_device = MagicMock(spec=Evohome)
    mock_ctl_device.id = "01:000001"
    mock_ctl_device.zones = []
    controller = RamsesController(mock_coordinator, mock_ctl_device, mock_description)

    with (
        patch(
            "custom_components.ramses_cc.climate.SCH_SET_SYSTEM_MODE_EXTRA",
            side_effect=vol.Invalid("Invalid system mode extra"),
        ),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await controller.async_set_system_mode(SystemMode.AUTO)

    # 2. Zone: async_set_zone_mode
    mock_zone_device = MagicMock()
    mock_zone_device.id = "04:000001"
    zone = RamsesZone(mock_coordinator, mock_zone_device, mock_description)

    with (
        patch(
            "custom_components.ramses_cc.climate.SCH_SET_ZONE_MODE_EXTRA",
            side_effect=vol.Invalid("Invalid zone mode extra"),
        ),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await zone.async_set_zone_mode(mode=ZoneMode.TEMPORARY)


async def test_hvac_set_fan_mode_success_and_validation(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac async_set_fan_mode success and input validation.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.set_fan_mode = AsyncMock()

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    # 1. Success Path
    await hvac.async_set_fan_mode("low")
    mock_device.set_fan_mode.assert_awaited_once_with("low")
    hvac.async_write_ha_state.assert_called_once()

    # 2. Validation Error (Invalid Mode)
    with pytest.raises(ServiceValidationError, match="invalid_fan_mode"):
        await hvac.async_set_fan_mode("invalid_mode")

    # 3. Validation Error (fan_modes is None)
    # Temporarily override the class attribute for this instance
    cast(Any, hvac)._attr_fan_modes = None
    with pytest.raises(ServiceValidationError, match="invalid_fan_mode"):
        await hvac.async_set_fan_mode("low")


async def test_hvac_set_fan_mode_errors(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac async_set_fan_mode error handling.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)

    # 1. AttributeError (simulating missing set_fan_mode in ramses_rf)
    mock_device.set_fan_mode = MagicMock(side_effect=AttributeError("Missing method"))

    with pytest.raises(
        HomeAssistantError,
        match="Underlying ramses_rf library lacks set_fan_mode",
    ):
        await hvac.async_set_fan_mode("low")

    # 2. Transport/Protocol Error
    # We simply overwrite the mock for the next test case; no deletion needed!
    mock_device.set_fan_mode = AsyncMock(side_effect=ProtocolSendFailed("Comms down"))

    with pytest.raises(HomeAssistantError, match="Failed to set fan mode"):
        await hvac.async_set_fan_mode("low")


@pytest.mark.parametrize(
    ("fan_mode", "cmd_string", "should_succeed"),
    [
        # 1. Valid CLI shorthand (Parsed cleanly by Command.from_cli)
        (
            "low",
            "W 37:111111 30:123456 22F1 000406",
            True,
        ),
        # 2. Raw log packet frame with leading space
        (
            "medium",
            " I --- 29:123150 29:099029 --:------ 22F1 003 000506",
            True,
        ),
        # 3. Raw log packet frame with "W" verb
        (
            "high",
            " W --- 29:123150 29:099029 --:------ 22F1 003 000606",
            True,
        ),
        # 4. Completely invalid garbage string
        (
            "auto",
            "THIS_IS_NOT_A_VALID_COMMAND",
            False,
        ),
        # 5. Malformed packet with missing device addresses
        (
            "low",
            " I --- 22F1 003 000406",
            False,
        ),
        # 6. Wrong verb letter
        (
            "medium",
            " X --- 29:123150 29:099029 --:------ 22F1 003 000506",
            False,
        ),
        # 7. Too much metadata / incorrect structure
        (
            "high",
            " W --- 29:123150 29:099029 --:------ 22F1 003 000606 GARBAGE",
            False,
        ),
    ],
)
async def test_hvac_set_fan_mode_custom_command_variations(
    mock_coordinator: MagicMock,
    mock_description: MagicMock,
    fan_mode: str,
    cmd_string: str,
    should_succeed: bool,
) -> None:
    """Test RamsesHvac async_set_fan_mode custom command logic."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem.return_value = "37:111111"

    # Explicitly mock the gateway and its async send command
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    # Inject parameterized custom command into the mocked coordinator options
    mock_coordinator.options = {
        SZ_KNOWN_LIST: {"37:111111": {CONF_COMMANDS: {fan_mode: cmd_string}}}
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    if should_succeed:
        await hvac.async_set_fan_mode(fan_mode)

        # Verify it was transmitted via the gateway
        mock_device._gwy.async_send_cmd.assert_awaited_once()
        # Verify the fallback 2-byte default method was NOT called
        mock_device.set_fan_mode.assert_not_called()
        # Verify the state was written
        hvac.async_write_ha_state.assert_called_once()
    else:
        with pytest.raises(HomeAssistantError, match="Failed to set fan mode"):
            await hvac.async_set_fan_mode(fan_mode)

        # Verify it aborted before sending
        mock_device._gwy.async_send_cmd.assert_not_called()


async def test_hvac_set_fan_mode_reads_from_remotes(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test that async_set_fan_mode reads from coordinator._remotes (schema _commands)."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem.return_value = "37:111111"
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    # Set up _remotes (schema _commands) with a custom command for "low"
    mock_coordinator._remotes = {
        "37:111111": {"low": "W 37:111111 30:123456 22F1 000406"}
    }
    # Also set up known_list with a DIFFERENT command for the same mode
    # to verify _remotes takes priority
    mock_coordinator.options = {
        SZ_KNOWN_LIST: {
            "37:111111": {CONF_COMMANDS: {"low": "W 37:111111 30:123456 22F1 000999"}}
        }
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    await hvac.async_set_fan_mode("low")

    # Should have sent the command from _remotes (schema), not known_list
    mock_device._gwy.async_send_cmd.assert_awaited_once()
    sent_cmd = mock_device._gwy.async_send_cmd.call_args[0][0]
    assert "000406" in str(sent_cmd), "Should use _remotes command, not known_list"
    mock_device.set_fan_mode.assert_not_called()


async def test_hvac_set_fan_mode_falls_back_to_known_list(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test that async_set_fan_mode falls back to known_list when _remotes is empty."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem.return_value = "37:111111"
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    # _remotes is empty (no schema _commands for this REM)
    mock_coordinator._remotes = {}
    # known_list has the command
    mock_coordinator.options = {
        SZ_KNOWN_LIST: {
            "37:111111": {CONF_COMMANDS: {"low": "W 37:111111 30:123456 22F1 000999"}}
        }
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    await hvac.async_set_fan_mode("low")

    # Should have sent the command from known_list (legacy fallback)
    mock_device._gwy.async_send_cmd.assert_awaited_once()
    sent_cmd = mock_device._gwy.async_send_cmd.call_args[0][0]
    assert "000999" in str(sent_cmd), "Should fall back to known_list command"
    mock_device.set_fan_mode.assert_not_called()


async def test_hvac_set_fan_mode_rem_not_faked_raises(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """async_set_fan_mode REM fallback raises if bound REM is not faked."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem.return_value = "37:111111"
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    mock_coordinator._remotes = {
        "37:111111": {"low": "W 37:111111 30:123456 22F1 000406"}
    }
    mock_coordinator.options = {SZ_KNOWN_LIST: {}}

    # Bound REM device exists but is NOT faked
    rem_dev = MagicMock()
    rem_dev.is_faked = False
    mock_coordinator._get_device = MagicMock(return_value=rem_dev)

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()
    hvac._bound_rem = "37:111111"

    with pytest.raises(HomeAssistantError, match="not configured for faking"):
        await hvac.async_set_fan_mode("low")

    # Should NOT have sent the command
    mock_device._gwy.async_send_cmd.assert_not_awaited()


async def test_hvac_set_fan_mode_rem_faked_sends(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """async_set_fan_mode REM fallback sends when bound REM IS faked."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem.return_value = "37:111111"
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    mock_coordinator._remotes = {
        "37:111111": {"low": "W 37:111111 30:123456 22F1 000406"}
    }
    mock_coordinator.options = {SZ_KNOWN_LIST: {}}

    # Bound REM device exists and IS faked
    rem_dev = MagicMock()
    rem_dev.is_faked = True
    mock_coordinator._get_device = MagicMock(return_value=rem_dev)

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()
    hvac._bound_rem = "37:111111"

    await hvac.async_set_fan_mode("low")

    # Should have sent the command
    mock_device._gwy.async_send_cmd.assert_awaited_once()
    mock_device.set_fan_mode.assert_not_called()


async def test_hvac_set_fan_mode_rem_not_found_sends(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """async_set_fan_mode REM fallback sends when bound REM device not found.

    If _get_device returns None (device not in registry), we can't check
    is_faked — fall through to sending, matching the remote.py behaviour
    where the check is only done when the device object is available.
    """
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem.return_value = "37:111111"
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    mock_coordinator._remotes = {
        "37:111111": {"low": "W 37:111111 30:123456 22F1 000406"}
    }
    mock_coordinator.options = {SZ_KNOWN_LIST: {}}

    # Bound REM device not found in registry
    mock_coordinator._get_device = MagicMock(return_value=None)

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()
    hvac._bound_rem = "37:111111"

    await hvac.async_set_fan_mode("low")

    # Should have sent the command (no is_faked check possible)
    mock_device._gwy.async_send_cmd.assert_awaited_once()


# ---------------------------------------------------------------------------
# Phase 3a: fan_modes property tests
# ---------------------------------------------------------------------------


def test_fan_modes_includes_custom_commands_from_remotes(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """fan_modes property extends base modes with custom command names."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")

    mock_coordinator._remotes = {
        "37:111111": {
            "boost": "W 37:111111 30:123456 22F1 000406",
            "speed_1": "W 37:111111 30:123456 22F1 000407",
        }
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac._bound_rem = "37:111111"

    modes = hvac.fan_modes
    assert modes is not None
    # Base modes are present
    assert "off" in modes
    assert "low" in modes
    assert "medium" in modes
    assert "high" in modes
    # Custom commands are appended
    assert "boost" in modes
    assert "speed_1" in modes
    # No duplicates
    assert len(modes) == len(set(modes))


def test_fan_modes_no_bound_rem_returns_base_only(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """fan_modes returns base modes when no bound REM exists."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value=None)

    mock_coordinator._remotes = {"37:111111": {"boost": "packet"}}

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac._bound_rem = None

    modes = hvac.fan_modes
    assert modes is not None
    assert "boost" not in modes
    assert "low" in modes


def test_fan_modes_empty_remotes_returns_base_only(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """fan_modes returns base modes when _remotes has no commands for bound REM."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")

    mock_coordinator._remotes = {}

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac._bound_rem = "37:111111"

    modes = hvac.fan_modes
    assert modes is not None
    assert "low" in modes
    assert "boost" not in modes


def test_fan_modes_no_duplicates_when_command_matches_base_mode(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """fan_modes doesn't duplicate a custom command that matches a base mode name."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")

    # "low" is both a base mode and a custom command
    mock_coordinator._remotes = {
        "37:111111": {"low": "W 37:111111 30:123456 22F1 000406"},
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac._bound_rem = "37:111111"

    modes = hvac.fan_modes
    assert modes is not None
    assert modes.count("low") == 1


# ---------------------------------------------------------------------------
# Phase 3a: set_fan_mode intercept — additional edge case tests
# ---------------------------------------------------------------------------


async def test_set_fan_mode_standard_mode_not_intercepted(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Standard fan modes call device.set_fan_mode, not the custom command path."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")
    mock_device.set_fan_mode = AsyncMock()
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    # _remotes has a custom command, but NOT for "low"
    mock_coordinator._remotes = {
        "37:111111": {"boost": "W 37:111111 30:123456 22F1 000406"}
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    await hvac.async_set_fan_mode("low")

    # Standard path: set_fan_mode called, async_send_cmd NOT called
    mock_device.set_fan_mode.assert_awaited_once_with("low")
    mock_device._gwy.async_send_cmd.assert_not_called()


async def test_set_fan_mode_custom_command_sends_via_gateway(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Custom fan mode sends raw packet via gateway, not device.set_fan_mode."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")
    mock_device.set_fan_mode = AsyncMock()
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    mock_coordinator._remotes = {
        "37:111111": {"boost": "W 37:111111 30:123456 22F1 000406"}
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    await hvac.async_set_fan_mode("boost")

    # Intercept path: async_send_cmd called, set_fan_mode NOT called
    mock_device._gwy.async_send_cmd.assert_awaited_once()
    mock_device.set_fan_mode.assert_not_called()
    hvac.async_write_ha_state.assert_called_once()


async def test_set_fan_mode_custom_command_priority_remotes_over_known_list(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """_remotes (schema _commands) takes priority over known_list[commands]."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")
    mock_device.set_fan_mode = AsyncMock()
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    mock_coordinator._remotes = {
        "37:111111": {"boost": "W 37:111111 30:123456 22F1 000AAA"}
    }
    mock_coordinator.options = {
        SZ_KNOWN_LIST: {
            "37:111111": {CONF_COMMANDS: {"boost": "W 37:111111 30:123456 22F1 000BBB"}}
        }
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    await hvac.async_set_fan_mode("boost")

    mock_device._gwy.async_send_cmd.assert_awaited_once()
    sent_cmd = mock_device._gwy.async_send_cmd.call_args[0][0]
    assert "000AAA" in str(sent_cmd), "Should use _remotes, not known_list"


async def test_set_fan_mode_validation_uses_dynamic_fan_modes(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """set_fan_mode accepts custom command names because fan_modes is dynamic."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")
    mock_device.set_fan_mode = AsyncMock()
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    mock_coordinator._remotes = {
        "37:111111": {"my_custom_mode": "W 37:111111 30:123456 22F1 000406"}
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    # "my_custom_mode" is NOT in the static _attr_fan_modes, but IS in the
    # dynamic fan_modes property (extended from _remotes). This should NOT
    # raise ServiceValidationError.
    await hvac.async_set_fan_mode("my_custom_mode")

    mock_device._gwy.async_send_cmd.assert_awaited_once()
    mock_device.set_fan_mode.assert_not_called()


async def test_set_fan_mode_unknown_custom_mode_raises_validation_error(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """A mode not in base fan_modes and not in _remotes raises validation error."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    mock_device.get_bound_rem = MagicMock(return_value="37:111111")
    mock_device.set_fan_mode = AsyncMock()

    mock_coordinator._remotes = {
        "37:111111": {"boost": "W 37:111111 30:123456 22F1 000406"}
    }

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)

    # "turbo" is not a base mode and not in _remotes
    with pytest.raises(ServiceValidationError, match="invalid_fan_mode"):
        await hvac.async_set_fan_mode("turbo")


async def test_hvac_set_preset_mode(
    mock_coordinator: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac async_set_preset_mode success and error handling."""
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"

    hvac = RamsesHvac(mock_coordinator, mock_device, mock_description)
    hvac.async_write_ha_state = MagicMock()

    # 1. Validation Error (preset_modes is currently None)
    with pytest.raises(ServiceValidationError, match="invalid_preset_mode"):
        await hvac.async_set_preset_mode("eco")

    # Temporarily override the class attribute to test the execution paths
    cast(Any, hvac)._attr_preset_modes = ["eco", "away"]

    # 2. Validation Error (Invalid Mode requested)
    with pytest.raises(ServiceValidationError, match="invalid_preset_mode"):
        await hvac.async_set_preset_mode("invalid_preset")

    # 3. AttributeError (simulating missing set_preset_mode in ramses_rf)
    mock_device.set_preset_mode = MagicMock(
        side_effect=AttributeError("Missing method")
    )
    with pytest.raises(
        HomeAssistantError,
        match="Underlying ramses_rf lacks set_preset_mode",
    ):
        await hvac.async_set_preset_mode("eco")

    # 4. Success Path
    mock_device.set_preset_mode = AsyncMock()
    await hvac.async_set_preset_mode("away")
    mock_device.set_preset_mode.assert_awaited_once_with("away")
    hvac.async_write_ha_state.assert_called_once()

    # 5. Generic Error Path
    mock_device.set_preset_mode = AsyncMock(side_effect=TransportError("Comms down"))
    with pytest.raises(HomeAssistantError, match="Failed to set preset mode"):
        await hvac.async_set_preset_mode("eco")


@patch("custom_components.ramses_cc.climate.Command")
@patch("custom_components.ramses_cc.climate.resolve_async_attr")
async def test_controller_async_added_to_hass(
    mock_resolve: MagicMock,
    mock_cmd: MagicMock,
    mock_coordinator: MagicMock,
    mock_description: MagicMock,
) -> None:
    """Test RamsesController.async_added_to_hass polling logic."""
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:123456"
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    controller = RamsesController(mock_coordinator, mock_device, mock_description)

    # 1. system_mode is None
    mock_resolve.return_value = None
    mock_cmd.from_cli.return_value = "mock_cmd"

    with patch("custom_components.ramses_cc.climate.RamsesEntity.async_added_to_hass"):
        await controller.async_added_to_hass()

    mock_cmd.from_cli.assert_called_once_with("RQ 01:123456 2E04 FF")
    mock_device._gwy.async_send_cmd.assert_awaited_once_with("mock_cmd")

    # 2. Exception handling
    mock_device._gwy.async_send_cmd.side_effect = Exception("Boom")
    with patch("custom_components.ramses_cc.climate.RamsesEntity.async_added_to_hass"):
        await controller.async_added_to_hass()

    # 3. system_mode is not None
    mock_resolve.return_value = {"mode": "auto"}
    mock_cmd.from_cli.reset_mock()
    with patch("custom_components.ramses_cc.climate.RamsesEntity.async_added_to_hass"):
        await controller.async_added_to_hass()

    mock_cmd.from_cli.assert_not_called()


@patch("custom_components.ramses_cc.climate.Command")
@patch("custom_components.ramses_cc.climate.resolve_async_attr")
async def test_zone_async_added_to_hass(
    mock_resolve: MagicMock,
    mock_cmd: MagicMock,
    mock_coordinator: MagicMock,
    mock_description: MagicMock,
) -> None:
    """Test RamsesZone.async_added_to_hass polling logic."""
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.idx = "01"
    mock_device.tcs = MagicMock()
    mock_device.tcs.id = "01:123456"
    mock_device._gwy = MagicMock()
    mock_device._gwy.async_send_cmd = AsyncMock()

    zone = RamsesZone(mock_coordinator, mock_device, mock_description)

    # 1. mode is None
    mock_resolve.return_value = None
    mock_cmd.from_cli.return_value = "mock_cmd"

    with patch("custom_components.ramses_cc.climate.RamsesEntity.async_added_to_hass"):
        await zone.async_added_to_hass()

    mock_cmd.from_cli.assert_called_once_with("RQ 01:123456 2349 01")
    mock_device._gwy.async_send_cmd.assert_awaited_once_with("mock_cmd")

    # 2. Exception handling
    mock_device._gwy.async_send_cmd.side_effect = Exception("Boom")
    with patch("custom_components.ramses_cc.climate.RamsesEntity.async_added_to_hass"):
        await zone.async_added_to_hass()

    # 3. mode is not None
    mock_resolve.return_value = {"mode": "schedule"}
    mock_cmd.from_cli.reset_mock()
    with patch("custom_components.ramses_cc.climate.RamsesEntity.async_added_to_hass"):
        await zone.async_added_to_hass()

    mock_cmd.from_cli.assert_not_called()

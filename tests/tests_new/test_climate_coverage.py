"""Tests for the ramses_cc climate platform to achieve 100% coverage."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.climate import (
    PRESET_AWAY,
    PRESET_NONE,
    HVACAction,
    HVACMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ramses_cc.climate import (
    RamsesController,
    RamsesHvac,
    RamsesZone,
    SystemMode,
    ZoneMode,
    async_setup_entry,
)
from custom_components.ramses_cc.const import DOMAIN, PRESET_PERMANENT, PRESET_TEMPORARY
from ramses_rf.device.hvac import HvacVentilator
from ramses_rf.system.heat import Evohome
from ramses_rf.system.zones import Zone
from ramses_tx.const import SZ_MODE, SZ_SETPOINT, SZ_SYSTEM_MODE
from ramses_tx.exceptions import ProtocolSendFailed, TransportError

# Constants
SZ_HEAT_DEMAND = "heat_demand"


@pytest.fixture
def mock_broker() -> MagicMock:
    """Return a mock RamsesBroker.

    :return: A mock object simulating the RamsesBroker.
    """
    broker = MagicMock()
    broker.async_post_update = MagicMock()
    broker.async_register_platform = MagicMock()
    broker.async_get_fan_param = AsyncMock()
    broker.async_set_fan_param = AsyncMock()
    broker.get_all_fan_params = MagicMock()
    return broker


@pytest.fixture
def mock_description() -> MagicMock:
    """Return a mock EntityDescription.

    :return: A mock object simulating the RamsesClimateEntityDescription.
    """
    return MagicMock()


async def test_async_setup_entry(hass: HomeAssistant, mock_broker: MagicMock) -> None:
    """Test the platform setup and entity creation callback.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    hass.data[DOMAIN] = {entry.entry_id: mock_broker}
    async_add_entities = MagicMock()

    # Mock async_get_current_platform to avoid RuntimeError in test env
    with patch(
        "custom_components.ramses_cc.climate.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    mock_broker.async_register_platform.assert_called_once()
    callback_func = mock_broker.async_register_platform.call_args[0][1]

    # Use spec mocks to ensure isinstance checks pass
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
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesController properties, extra state attributes, and edge cases.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:123456"
    mock_device.zones = []

    controller = RamsesController(mock_broker, mock_device, mock_description)
    assert controller.unique_id == "01:123456"

    # 1. extra_state_attributes
    mock_device.heat_demand = 0.5
    mock_device.heat_demands = {"01": 0.5}
    mock_device.relay_demands = {"01": 1.0}
    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.tpi_params = {"p": 1}

    attrs = controller.extra_state_attributes
    assert attrs["heat_demand"] == 0.5
    assert attrs["heat_demands"] == {"01": 0.5}
    assert attrs["system_mode"] == {SZ_SYSTEM_MODE: SystemMode.AUTO}

    # 2. current_temperature logic
    # Case A: Happy Path (calculation successful)
    z1 = MagicMock()
    z1.temperature = 20.0
    z2 = MagicMock()
    z2.temperature = 22.0
    mock_device.zones = [z1, z2]
    # (20 + 22) / 2 = 21.0
    assert controller.current_temperature == 21.0

    # Case B: TypeError logic (sum failure due to invalid type)
    zone_bad = MagicMock()
    zone_bad.temperature = "error"
    mock_device.zones = [zone_bad]
    assert controller.current_temperature is None

    # 3. target_temperature logic (max of zones with demand)
    # We include a zone with setpoint=None to exercise the list comprehension filter
    z1 = MagicMock()
    z1.setpoint = 20.0
    z1.heat_demand = None
    z2 = MagicMock()
    z2.setpoint = 22.0
    z2.heat_demand = 0.5
    z3 = MagicMock()
    z3.setpoint = None
    z3.heat_demand = 0.5

    mock_device.zones = [z1, z2, z3]
    assert controller.target_temperature == 22.0

    mock_device.zones = [z1]
    assert controller.target_temperature is None


async def test_controller_modes_and_actions(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesController HVAC modes, actions, and presets.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:123456"
    mock_device.zones = []
    controller = RamsesController(mock_broker, mock_device, mock_description)

    # 1. hvac_action
    mock_device.system_mode = None
    assert controller.hvac_action is None

    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    assert controller.hvac_action == HVACAction.OFF

    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.heat_demand = 0.5
    assert controller.hvac_action == HVACAction.HEATING

    mock_device.heat_demand = 0
    assert controller.hvac_action == HVACAction.IDLE

    mock_device.heat_demand = None
    assert controller.hvac_action is None

    # 2. hvac_mode
    mock_device.system_mode = None
    assert controller.hvac_mode is None

    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    assert controller.hvac_mode == HVACMode.OFF

    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AWAY}
    assert controller.hvac_mode == HVACMode.AUTO

    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    assert controller.hvac_mode == HVACMode.HEAT

    # 3. preset_mode
    mock_device.system_mode = None
    assert controller.preset_mode is None

    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    assert controller.preset_mode == PRESET_NONE

    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AWAY}
    assert controller.preset_mode == PRESET_AWAY


async def test_controller_services(
    mock_broker: MagicMock, mock_description: MagicMock, freezer: Any
) -> None:
    """Test RamsesController service calls and mode setting logic.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    :param freezer: The freezer fixture to control time.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:000001"
    mock_device.zones = []

    # Update: Ensure async methods are AsyncMock (from new code)
    mock_device.set_mode = AsyncMock()
    mock_device.reset_mode = AsyncMock()
    mock_device.get_faultlog = AsyncMock()

    controller = RamsesController(mock_broker, mock_device, mock_description)
    controller.async_write_ha_state_delayed = MagicMock()

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
        test_duration = timedelta(hours=1)
        await controller.async_set_system_mode("auto", duration=test_duration)

        # 12:00 + 1h = 13:00
        expected_until_dur = datetime(2023, 1, 1, 13, 0, 0)
        mock_device.set_mode.assert_awaited_with("auto", until=expected_until_dur)

        # Case C: Period 0 (Next Day)
        zero_period = timedelta(0)
        await controller.async_set_system_mode("auto", period=zero_period)
        mock_device.set_mode.assert_awaited_with(
            "auto", until=datetime(2023, 1, 2, 0, 0, 0)
        )

        # Case D: Standard Period
        std_period = timedelta(hours=2)
        await controller.async_set_system_mode("auto", period=std_period)
        mock_device.set_mode.assert_awaited_with(
            "auto", until=datetime(2023, 1, 1, 14, 0, 0)
        )

    # 3. Service Calls
    await controller.async_reset_system_mode()
    mock_device.reset_mode.assert_awaited_once()

    await controller.async_get_system_faults(5)
    mock_device.get_faultlog.assert_awaited_with(limit=5, force_refresh=True)


async def test_zone_properties_and_config(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesZone properties, config, and attributes.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    # Removed spec=Zone because it blocks access to .tcs
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.config = {"min_temp": 5, "max_temp": 35}
    mock_device.temperature = 19.5
    mock_device.setpoint = 20.0
    mock_device.heat_demand = None

    zone = RamsesZone(mock_broker, mock_device, mock_description)

    # Basics
    assert zone.target_temperature == 20.0
    assert zone.current_temperature == 19.5

    # Config checks (min/max)
    mock_device.config = None
    assert zone.min_temp == 5
    assert zone.max_temp == 35

    mock_device.config = {"min_temp": 10.0, "max_temp": 30.0}
    assert zone.min_temp == 10.0
    assert zone.max_temp == 30.0

    # Extra state attributes
    mock_device.params = {"p": 1}
    mock_device.idx = "01"
    mock_device.heating_type = "radiator"
    mock_device.mode = {"m": 1}
    mock_device.schedule = []
    mock_device.schedule_version = 1
    attrs = zone.extra_state_attributes
    assert attrs["zone_idx"] == "01"


async def test_zone_modes_and_actions(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesZone HVAC modes, actions, and presets.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    # Removed spec=Zone because it blocks access to .tcs
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.config = {"min_temp": 5, "max_temp": 35}
    zone = RamsesZone(mock_broker, mock_device, mock_description)

    # 1. hvac_action
    mock_device.tcs.system_mode = None
    assert zone.hvac_action is None

    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    assert zone.hvac_action == HVACAction.OFF

    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.heat_demand = 0.5
    assert zone.hvac_action == HVACAction.HEATING

    mock_device.heat_demand = 0
    assert zone.hvac_action == HVACAction.IDLE

    mock_device.heat_demand = None
    assert zone.hvac_action is None

    # 2. hvac_mode
    mock_device.tcs.system_mode = None
    assert zone.hvac_mode is None

    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AWAY}
    assert zone.hvac_mode == HVACMode.AUTO

    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    assert zone.hvac_mode == HVACMode.OFF

    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.mode = None
    assert zone.hvac_mode is None

    # Config checks for Off vs Heat
    mock_device.mode = {SZ_SETPOINT: 4.0, SZ_MODE: ZoneMode.ADVANCED}
    assert zone.hvac_mode == HVACMode.OFF  # Below min_temp

    mock_device.mode = {SZ_SETPOINT: 20.0, SZ_MODE: ZoneMode.ADVANCED}
    assert zone.hvac_mode == HVACMode.HEAT

    # 3. preset_mode
    mock_device.tcs.system_mode = None
    assert zone.preset_mode is None

    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AWAY}
    assert zone.preset_mode == PRESET_AWAY

    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.mode = None
    assert zone.preset_mode is None

    mock_device.mode = {SZ_MODE: ZoneMode.SCHEDULE}
    assert zone.preset_mode == PRESET_NONE

    mock_device.mode = {SZ_MODE: ZoneMode.TEMPORARY}
    assert zone.preset_mode == "temporary"


async def test_zone_methods_and_services(
    mock_broker: MagicMock, mock_description: MagicMock, freezer: Any
) -> None:
    """Test RamsesZone methods (set_temp, set_mode) and services.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    :param freezer: The freezer fixture.
    """
    # Removed spec=Zone because it blocks access to .tcs
    mock_device = MagicMock()
    mock_device.id = "04:000001"
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}

    # Update: Ensure async methods are AsyncMock (from new code)
    mock_device.set_mode = AsyncMock()
    mock_device.reset_mode = AsyncMock()
    mock_device.set_config = AsyncMock()
    mock_device.reset_config = AsyncMock()
    mock_device.get_schedule = AsyncMock()
    mock_device.set_schedule = AsyncMock()

    zone = RamsesZone(mock_broker, mock_device, mock_description)
    zone.async_write_ha_state_delayed = MagicMock()
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

        await zone.async_set_hvac_mode(HVACMode.OFF)
        # Verify it passed the set_frost_mode function
        assert mock_set.called
        assert mock_set.call_args[0][0] == mock_device.set_frost_mode

    # 1a. Explicit coverage for async_reset_zone_mode body
    del zone.async_reset_zone_mode
    await zone.async_reset_zone_mode()
    mock_device.reset_mode.assert_awaited_once()

    # 2. set_preset_mode
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        await zone.async_set_preset_mode(PRESET_NONE)
        mock_set.assert_called_with(
            mode=ZoneMode.SCHEDULE, setpoint=None, duration=None
        )

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
        dur = timedelta(hours=1)
        await zone.async_set_temperature(temperature=21.0, duration=dur)
        mock_set.assert_called_with(
            mode=ZoneMode.TEMPORARY, setpoint=21.0, duration=dur, until=None
        )
        # D. Until -> Temporary (Covering 'or until is not None' branch)
        until = datetime(2023, 1, 1, 12, 0, 0)
        await zone.async_set_temperature(temperature=21.0, until=until)
        mock_set.assert_called_with(
            mode=ZoneMode.TEMPORARY, setpoint=21.0, duration=None, until=until
        )

    # 4. async_set_zone_mode internal logic (calculating 'until' from duration)
    # We patch SCH_SET_ZONE_MODE_EXTRA to control schema validation return values
    with patch("custom_components.ramses_cc.climate.SCH_SET_ZONE_MODE_EXTRA") as m_sch:
        # Case: Just setpoint (schema returns input)
        m_sch.side_effect = lambda x: x
        await zone.async_set_zone_mode(setpoint=21.0)
        mock_device.set_mode.assert_awaited_with(mode=None, setpoint=21.0, until=None)

        # Case: Duration provided (schema returns dict with duration)
        m_sch.side_effect = None
        m_sch.return_value = {"duration": timedelta(hours=1)}
        freezer.move_to("2023-01-01 12:00:00")

        await zone.async_set_zone_mode(mode="temp", duration=timedelta(hours=1))

        expected_until = datetime(2023, 1, 1, 13, 0, 0)
        mock_device.set_mode.assert_awaited_with(
            mode="temp", setpoint=None, until=expected_until
        )

        # Case: Duration provided BUT until is ALSO provided
        # if until is None and "duration" in checked_entry: -> False because until is NOT None
        m_sch.return_value = {"duration": timedelta(hours=1)}
        explicit_until = datetime(2023, 1, 1, 15, 0, 0)
        await zone.async_set_zone_mode(
            mode="temp", duration=timedelta(hours=1), until=explicit_until
        )
        # Expectation: The loop calculation for until is SKIPPED, uses explicit_until
        mock_device.set_mode.assert_awaited_with(
            mode="temp", setpoint=None, until=explicit_until
        )

    # 5. Miscellaneous Services
    # async_fake_zone_temp
    mock_device.sensor = None
    with pytest.raises(HomeAssistantError):
        zone.async_fake_zone_temp(20.0)

    mock_device.sensor = MagicMock()
    zone.async_fake_zone_temp(22.5)
    assert mock_device.sensor.temperature == 22.5

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
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac properties and mode determination.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:654321"
    mock_device.indoor_humidity = 0.55
    mock_device.indoor_temp = 21.5
    mock_device.fan_info = None
    mock_device.get_bound_rem.return_value = "30:987654"

    hvac = RamsesHvac(mock_broker, mock_device, mock_description)

    # 1. async_added_to_hass
    # Update: Use the patch context from the new code for cleaner testing
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
    mock_device.indoor_humidity = None
    assert hvac.current_humidity is None

    assert hvac.current_temperature == 21.5
    assert hvac.preset_mode == PRESET_NONE

    attrs = hvac.extra_state_attributes
    assert attrs["bound_rem"] == "30:987654"

    # 3. Mode/Action Logic
    # Fan Info None
    mock_device.fan_info = None
    assert hvac.hvac_mode is None
    assert hvac.fan_mode is None

    # Fan Off
    mock_device.fan_info = "off"
    assert hvac.hvac_mode == HVACMode.OFF
    assert hvac.icon == "mdi:hvac-off"

    # Fan Low
    mock_device.fan_info = "low"
    assert hvac.hvac_mode == HVACMode.AUTO
    assert hvac.icon == "mdi:hvac"
    assert hvac.hvac_action == "low"
    assert hvac.fan_mode == "low"


async def test_hvac_services(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac specific service calls.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=HvacVentilator)
    mock_device.id = "30:123456"
    hvac = RamsesHvac(mock_broker, mock_device, mock_description)

    # async_get_fan_clim_param
    await hvac.async_get_fan_clim_param(param="p1")
    mock_broker.async_get_fan_param.assert_called_with(
        {"param": "p1", "device_id": mock_device.id}
    )

    # async_set_fan_clim_param
    await hvac.async_set_fan_clim_param(param="p1", value=1)
    mock_broker.async_set_fan_param.assert_called_with(
        {"param": "p1", "value": 1, "device_id": mock_device.id}
    )

    # async_update_fan_params
    await hvac.async_update_fan_params()
    mock_broker.get_all_fan_params.assert_called_with({"device_id": mock_device.id})


async def test_error_handling(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test that protocol/transport errors are caught and re-raised as HomeAssistantError.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:999999"
    mock_device.zones = []
    controller = RamsesController(mock_broker, mock_device, mock_description)
    controller.async_write_ha_state_delayed = MagicMock()

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
    zone = RamsesZone(mock_broker, zone_device, mock_description)
    zone.async_write_ha_state_delayed = MagicMock()
    zone.async_write_ha_state = MagicMock()

    zone_cases = [
        (zone.async_reset_zone_mode, [], "reset_mode"),
        (zone.async_reset_zone_config, [], "reset_config"),
        (zone.async_set_zone_config, [], "set_config"),  # kwargs handling
        (
            zone.async_set_zone_mode,
            [ZoneMode.SCHEDULE],
            "set_mode",
        ),  # Provide valid mode
        (zone.async_get_zone_schedule, [], "get_schedule"),
        (zone.async_set_zone_schedule, ["{}"], "set_schedule"),
    ]

    for method, args, device_method_name in zone_cases:
        getattr(zone_device, device_method_name).side_effect = ProtocolSendFailed(
            "Boom"
        )
        with pytest.raises(HomeAssistantError, match="Failed to .*"):
            await method(*args)

    # HVAC Error Handling (calls broker methods)
    hvac_device = MagicMock(spec=HvacVentilator)
    hvac_device.id = "30:777777"
    hvac = RamsesHvac(mock_broker, hvac_device, mock_description)

    # Broker failures
    mock_broker.async_get_fan_param.side_effect = ProtocolSendFailed("Broker fail")
    with pytest.raises(HomeAssistantError, match="Failed to get fan param"):
        await hvac.async_get_fan_clim_param(param="p")

    mock_broker.async_set_fan_param.side_effect = TimeoutError("Broker timeout")
    with pytest.raises(HomeAssistantError, match="Failed to set fan param"):
        await hvac.async_set_fan_clim_param(param="p", value=1)


async def test_service_validation_errors(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test ServiceValidationError handling in Controller and Zone.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    """
    mock_device = MagicMock(spec=Evohome)
    mock_device.id = "01:999999"
    mock_device.zones = []
    controller = RamsesController(mock_broker, mock_device, mock_description)

    # 1. Invalid HVAC Mode
    with pytest.raises(ServiceValidationError, match="invalid_hvac_mode"):
        await controller.async_set_hvac_mode("invalid_mode")

    # 2. Invalid Preset Mode
    with pytest.raises(ServiceValidationError, match="invalid_preset_mode"):
        await controller.async_set_preset_mode("invalid_preset")

    # 3. vol.Invalid in async_set_hvac_mode
    with (
        patch.object(
            controller, "async_set_system_mode", side_effect=vol.Invalid("Boom")
        ),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await controller.async_set_hvac_mode(HVACMode.HEAT)

    # 4. vol.Invalid in async_set_preset_mode
    with (
        patch.object(
            controller, "async_set_system_mode", side_effect=vol.Invalid("Boom")
        ),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await controller.async_set_preset_mode(PRESET_AWAY)

    # Zone Validation Errors
    mock_zone_dev = MagicMock()
    mock_zone_dev.id = "04:123456"
    zone = RamsesZone(mock_broker, mock_zone_dev, mock_description)

    # 5. vol.Invalid in async_set_hvac_mode
    # We patch async_set_zone_mode, which is called by async_set_hvac_mode
    with (
        patch.object(zone, "async_set_zone_mode", side_effect=vol.Invalid("Boom")),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await zone.async_set_hvac_mode(HVACMode.HEAT)

    # 6. vol.Invalid in async_set_preset_mode
    with (
        patch.object(zone, "async_set_zone_mode", side_effect=vol.Invalid("Boom")),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await zone.async_set_preset_mode(PRESET_NONE)

    # 7. vol.Invalid in async_set_temperature
    with (
        patch.object(zone, "async_set_zone_mode", side_effect=vol.Invalid("Boom")),
        pytest.raises(ServiceValidationError, match="validation_error"),
    ):
        await zone.async_set_temperature(temperature=20)


async def test_zone_extended_coverage(
    mock_broker: MagicMock, mock_description: MagicMock, freezer: Any
) -> None:
    """Test extended Zone logic for presets and config edges.

    :param mock_broker: The mock broker fixture.
    :param mock_description: The mock description fixture.
    :param freezer: The freezer fixture.
    """
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.setpoint = 20.0

    # Needs async mocks for the awaits
    mock_device.set_mode = AsyncMock()

    zone = RamsesZone(mock_broker, mock_device, mock_description)

    # 1. Preset Temporary (1 hour duration)
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        await zone.async_set_preset_mode(PRESET_TEMPORARY)
        mock_set.assert_called_with(
            mode=ZoneMode.TEMPORARY, setpoint=20.0, duration=timedelta(hours=1)
        )

    # 2. Preset Permanent
    with patch.object(zone, "async_set_zone_mode") as mock_set:
        await zone.async_set_preset_mode(PRESET_PERMANENT)
        mock_set.assert_called_with(
            mode=ZoneMode.PERMANENT, setpoint=20.0, duration=None
        )

    # 3. HVAC Mode logic when Config is None
    # Code: if (self._device.config and setpoint <= min): return OFF
    mock_device.config = None
    mock_device.mode = {SZ_SETPOINT: 4.0, SZ_MODE: ZoneMode.ADVANCED}
    # Should default to HEAT because config check fails (short-circuit)
    assert zone.hvac_mode == HVACMode.HEAT

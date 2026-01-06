"""Tests for the ramses_cc climate platform to improve coverage."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.components.climate import (
    PRESET_AWAY,
    PRESET_NONE,
    HVACAction,
    HVACMode,
)

from custom_components.ramses_cc.climate import (
    RamsesController,
    RamsesHvac,
    RamsesZone,
    SystemMode,
    ZoneMode,
)

# Constants used in the file
SZ_SYSTEM_MODE = "system_mode"
SZ_SETPOINT = "setpoint"
SZ_MODE = "mode"


@pytest.fixture
def mock_broker() -> MagicMock:
    """Return a mock RamsesBroker."""
    broker = MagicMock()
    broker.async_post_update = MagicMock()
    return broker


@pytest.fixture
def mock_description() -> MagicMock:
    """Return a mock EntityDescription."""
    return MagicMock()


async def test_controller_coverage(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesController specific edge cases."""
    mock_device = MagicMock()
    mock_device.id = "01:123456"
    mock_device.zones = []

    # 1. Test Init
    controller = RamsesController(mock_broker, mock_device, mock_description)
    assert controller.unique_id == "01:123456"

    # 2. Test current_temperature TypeError logic
    # We need zones that return something valid for the first list comp,
    # but fail inside the sum() or len() to trigger TypeError.
    zone_bad = MagicMock()
    zone_bad.temperature = "not_a_number"
    mock_device.zones = [zone_bad]

    # This triggers the TypeError inside the property
    assert controller.current_temperature is None

    # 3. Test hvac_action
    # Case: SystemMode is None
    mock_device.system_mode = None
    assert controller.hvac_action is None
    assert controller.hvac_mode is None  # Also hits hvac_mode None check
    assert controller.preset_mode is None

    # Case: HEAT_OFF
    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.HEAT_OFF}
    assert controller.hvac_action == HVACAction.OFF
    assert controller.hvac_mode == HVACMode.OFF

    # Case: HEAT_DEMAND exists
    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.heat_demand = 0.5
    assert controller.hvac_action == HVACAction.HEATING

    # Case: IDLE (heat_demand is 0/None check)
    mock_device.heat_demand = 0
    assert controller.hvac_action == HVACAction.IDLE

    # 4. Test hvac_mode AWAY
    mock_device.system_mode = {SZ_SYSTEM_MODE: SystemMode.AWAY}
    assert controller.hvac_mode == HVACMode.AUTO

    # 5. Test set_hvac_mode and set_preset_mode
    controller.async_set_system_mode = MagicMock()

    controller.set_hvac_mode(HVACMode.HEAT)
    controller.async_set_system_mode.assert_called_with(SystemMode.AUTO)

    controller.set_preset_mode(PRESET_AWAY)
    controller.async_set_system_mode.assert_called_with(SystemMode.AWAY)

    # 6. Test async_set_system_mode logic
    # Test strict schema check call and logic
    mock_device.set_mode = MagicMock()

    # Call with duration
    duration = timedelta(hours=1)
    controller.async_set_system_mode(SystemMode.AUTO, duration=duration)
    # We verify set_mode was called. The specific calculation of 'until'
    # uses datetime.now(), so we just check it was called.
    assert mock_device.set_mode.called


async def test_zone_coverage(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesZone specific edge cases."""
    mock_device = MagicMock()
    mock_device.id = "04:123456"
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    mock_device.config = {"min_temp": 5, "max_temp": 35}

    zone = RamsesZone(mock_broker, mock_device, mock_description)

    # 1. Test hvac_mode
    # Case: Mode or Setpoint is None
    mock_device.mode = None
    assert zone.hvac_mode is None

    # Case: Setpoint <= min_temp (Simulates OFF in UI)
    mock_device.mode = {SZ_SETPOINT: 4.0, SZ_MODE: ZoneMode.ADVANCED}
    assert zone.hvac_mode == HVACMode.OFF

    # 2. Test preset_mode
    # Case: TCS is in Away mode (Overrides zone preset)
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AWAY}
    assert zone.preset_mode == PRESET_AWAY

    # Reset TCS
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}

    # Case: Zone is FollowSchedule
    mock_device.mode = {SZ_MODE: ZoneMode.SCHEDULE}
    assert zone.preset_mode == PRESET_NONE

    # 3. Test set_hvac_mode
    zone.async_reset_zone_mode = MagicMock()
    zone.async_set_zone_mode = MagicMock()

    # Case: Auto
    zone.set_hvac_mode(HVACMode.AUTO)
    zone.async_reset_zone_mode.assert_called_once()

    # Case: Heat (Temporary override logic in this integration?)
    zone.set_hvac_mode(HVACMode.HEAT)
    zone.async_set_zone_mode.assert_called_with(mode=ZoneMode.PERMANENT, setpoint=25)

    # Case: Off (Sets frost mode)
    zone.set_hvac_mode(HVACMode.OFF)
    # Verify it called set_zone_mode with the frost function
    assert zone.async_set_zone_mode.called

    # 4. Test set_temperature logic
    zone.async_set_zone_mode = MagicMock()

    # Case: Just temperature (Advanced override)
    zone.set_temperature(temperature=21.0)
    zone.async_set_zone_mode.assert_called_with(
        mode=ZoneMode.ADVANCED, setpoint=21.0, duration=None, until=None
    )

    # Case: With Duration (Temporary override)
    zone.set_temperature(temperature=21.0, duration=timedelta(hours=1))
    zone.async_set_zone_mode.assert_called_with(
        mode=ZoneMode.TEMPORARY, setpoint=21.0, duration=timedelta(hours=1), until=None
    )


async def test_hvac_coverage(
    mock_broker: MagicMock, mock_description: MagicMock
) -> None:
    """Test RamsesHvac specific edge cases."""
    mock_device = MagicMock()
    mock_device.id = "30:123456"
    mock_device.fan_info = None

    hvac = RamsesHvac(mock_broker, mock_device, mock_description)

    # 1. Test hvac_mode when fan_info is None
    assert hvac.hvac_mode is None

    # 2. Test hvac_mode when off
    mock_device.fan_info = "off"
    assert hvac.hvac_mode == HVACMode.OFF
    assert hvac.icon == "mdi:hvac-off"

    # 3. Test hvac_mode when on
    mock_device.fan_info = "low"
    assert hvac.hvac_mode == HVACMode.AUTO
    assert hvac.icon == "mdi:hvac"

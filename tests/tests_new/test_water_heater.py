"""Tests for the water heater platform in ramses_cc."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.components.water_heater import STATE_OFF, STATE_ON
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.ramses_cc.const import DOMAIN, SystemMode, ZoneMode
from custom_components.ramses_cc.water_heater import (
    STATE_AUTO,
    STATE_BOOST,
    RamsesWaterHeater,
    async_setup_entry,
)
from ramses_rf.system.zones import DhwZone
from ramses_tx.const import SZ_SYSTEM_MODE
from ramses_tx.exceptions import ProtocolSendFailed

# Constants for testing
TEST_DEVICE_ID = "10:123456"
SZ_ACTIVE = "active"


@pytest.fixture
def mock_device() -> MagicMock:
    """Return a mock DhwZone device."""
    # Note: Do not use spec=DhwZone here as it prevents mocking 'tcs' if not explicitly in the class
    device = MagicMock()
    device.id = TEST_DEVICE_ID
    device.mode = {"mode": ZoneMode.SCHEDULE, SZ_ACTIVE: True}
    device.temperature = 55.0
    device.setpoint = 60.0
    device.params = {}
    device.schedule = []
    device.schedule_version = 1
    device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}

    # Async methods on the device
    device.set_mode = AsyncMock()
    device.set_config = AsyncMock()
    device.reset_mode = AsyncMock()
    device.reset_config = AsyncMock()
    device.set_boost_mode = AsyncMock()
    device.get_schedule = AsyncMock()
    device.set_schedule = AsyncMock()

    # Sensor mock for fake temp
    device.sensor = MagicMock()

    return device


@pytest.fixture
def mock_broker() -> MagicMock:
    """Return a mock RamsesBroker."""
    return MagicMock()


@pytest.fixture
def water_heater(mock_broker: MagicMock, mock_device: MagicMock) -> RamsesWaterHeater:
    """Return an instantiated RamsesWaterHeater entity."""
    description = MagicMock()
    entity = RamsesWaterHeater(mock_broker, mock_device, description)
    entity.hass = MagicMock()
    # Mock the internal delayed writer
    entity.async_write_ha_state_delayed = MagicMock()
    entity.async_write_ha_state = MagicMock()
    return entity


async def test_async_setup_entry(hass: HomeAssistant) -> None:
    """Test the platform setup."""
    # Mock ConfigEntry
    mock_entry = MagicMock(spec=ConfigEntry)
    mock_entry.entry_id = "test_entry_id"

    # Mock Broker
    mock_broker = MagicMock()
    hass.data[DOMAIN] = {mock_entry.entry_id: mock_broker}

    # Mock AddEntitiesCallback
    mock_add_entities = MagicMock()

    # Patch async_get_current_platform to return a mock
    with patch(
        "custom_components.ramses_cc.water_heater.async_get_current_platform"
    ) as mock_get_platform:
        mock_platform = MagicMock()
        mock_get_platform.return_value = mock_platform

        # Call setup
        await async_setup_entry(hass, mock_entry, mock_add_entities)

        # Assert register_platform called
        mock_broker.async_register_platform.assert_called_once()
        call_args = mock_broker.async_register_platform.call_args
        assert call_args[0][0] == mock_platform
        callback_func = call_args[0][1]

        # Test the callback function (add_devices)
        # Create a mock device that passes isinstance(device, DhwZone)
        mock_device = MagicMock(spec=DhwZone)
        mock_device.id = TEST_DEVICE_ID

        # Call the internal callback
        callback_func([mock_device])

        # Assert entities added
        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RamsesWaterHeater)
        assert entities[0]._device == mock_device


async def test_property_current_operation(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test the current_operation property logic."""
    # Case 1: Auto (Schedule)
    mock_device.mode = {"mode": ZoneMode.SCHEDULE}
    assert water_heater.current_operation == STATE_AUTO

    # Case 2: On (Permanent & Active)
    mock_device.mode = {"mode": ZoneMode.PERMANENT, SZ_ACTIVE: True}
    assert water_heater.current_operation == STATE_ON

    # Case 3: Off (Permanent & Inactive)
    mock_device.mode = {"mode": ZoneMode.PERMANENT, SZ_ACTIVE: False}
    assert water_heater.current_operation == STATE_OFF

    # Case 4: Boost (Temporary & Active)
    mock_device.mode = {"mode": ZoneMode.TEMPORARY, SZ_ACTIVE: True}
    assert water_heater.current_operation == STATE_BOOST

    # Case 5: Off (Temporary & Inactive)
    mock_device.mode = {"mode": ZoneMode.TEMPORARY, SZ_ACTIVE: False}
    assert water_heater.current_operation == STATE_OFF


async def test_property_error_handling(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test property access handles ValueErrors/TypeErrors gracefully."""
    # Test current_operation error handling
    # We patch the instance attribute 'mode' on the mock object to act as a PropertyMock
    # that raises TypeError when accessed.
    with patch.object(mock_device, "mode", new_callable=PropertyMock) as mock_mode:
        mock_mode.side_effect = TypeError
        assert water_heater.current_operation is None

    # Test is_away_mode_on error handling
    # Similarly, patch 'system_mode' on the 'tcs' child mock
    with patch.object(
        mock_device.tcs, "system_mode", new_callable=PropertyMock
    ) as mock_sys_mode:
        mock_sys_mode.side_effect = TypeError
        assert water_heater.is_away_mode_on is None


async def test_is_away_mode_on(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test is_away_mode_on property."""
    # Test True
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AWAY}
    assert water_heater.is_away_mode_on is True

    # Test False
    mock_device.tcs.system_mode = {SZ_SYSTEM_MODE: SystemMode.AUTO}
    assert water_heater.is_away_mode_on is False


async def test_simple_properties(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test simple properties."""
    assert water_heater.current_temperature == 55.0
    assert water_heater.target_temperature == 60.0


async def test_extra_state_attributes(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test integration-specific attributes."""
    # Re-establish clean mode data
    mock_device.mode = {"mode": ZoneMode.SCHEDULE}
    attrs = water_heater.extra_state_attributes
    assert "params" in attrs
    assert "mode" in attrs
    assert "schedule" in attrs
    assert "schedule_version" in attrs


async def test_async_set_operation_mode_auto(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test setting operation mode to AUTO."""
    await water_heater.async_set_operation_mode(STATE_AUTO)

    mock_device.set_mode.assert_awaited_once()
    _, kwargs = mock_device.set_mode.call_args
    assert kwargs["mode"] == ZoneMode.SCHEDULE
    # Auto typically doesn't set active explicitly in this mapping logic unless strictly needed
    assert kwargs["active"] is None
    assert kwargs["active"] is None


async def test_async_set_operation_mode_boost(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test setting operation mode to BOOST."""
    # We use real datetime to ensure voluptuous checks (cv.datetime) pass.
    # Mocking datetime.datetime often fails isinstance(x, datetime) checks.
    now = datetime.now()
    await water_heater.async_set_operation_mode(STATE_BOOST)

    mock_device.set_mode.assert_awaited_once()
    _, kwargs = mock_device.set_mode.call_args
    assert kwargs["mode"] == ZoneMode.TEMPORARY
    assert kwargs["active"] is True

    # Verify 'until' is approximately 1 hour from now (allow 1s tolerance)
    # We cannot check for exact equality due to execution time
    assert abs((kwargs["until"] - (now + timedelta(hours=1))).total_seconds()) < 1.0


async def test_async_set_operation_mode_on(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test setting operation mode to ON."""
    await water_heater.async_set_operation_mode(STATE_ON)

    mock_device.set_mode.assert_awaited_once()
    _, kwargs = mock_device.set_mode.call_args
    assert kwargs["mode"] == ZoneMode.PERMANENT
    assert kwargs["active"] is True


async def test_async_set_operation_mode_off(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test setting operation mode to OFF."""
    await water_heater.async_set_operation_mode(STATE_OFF)

    mock_device.set_mode.assert_awaited_once()
    _, kwargs = mock_device.set_mode.call_args
    assert kwargs["mode"] == ZoneMode.PERMANENT
    assert kwargs["active"] is False


async def test_async_set_temperature(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test setting the target temperature."""
    await water_heater.async_set_temperature(temperature=65.0)

    mock_device.set_config.assert_awaited_once()
    _, kwargs = mock_device.set_config.call_args
    assert kwargs["setpoint"] == 65.0


async def test_async_set_dhw_params(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test setting advanced DHW parameters."""
    await water_heater.async_set_dhw_params(setpoint=50.0, overrun=5, differential=2.0)

    mock_device.set_config.assert_awaited_once()
    _, kwargs = mock_device.set_config.call_args
    assert kwargs["setpoint"] == 50.0
    assert kwargs["overrun"] == 5
    assert kwargs["differential"] == 2.0


async def test_async_set_dhw_mode_with_duration(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test setting DHW mode with a duration string."""
    now = datetime.now()
    duration = timedelta(hours=2)

    await water_heater.async_set_dhw_mode(
        mode=ZoneMode.TEMPORARY, active=True, duration=duration
    )

    mock_device.set_mode.assert_awaited_once()
    _, kwargs = mock_device.set_mode.call_args

    # Logic: duration is converted to 'until'
    assert kwargs["until"] is not None
    assert abs((kwargs["until"] - (now + duration)).total_seconds()) < 1.0


async def test_integration_services(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test integration-specific service calls."""
    # fake_dhw_temp
    water_heater.async_fake_dhw_temp(45.0)
    assert mock_device.sensor.temperature == 45.0

    # reset_dhw_mode
    await water_heater.async_reset_dhw_mode()
    mock_device.reset_mode.assert_awaited_once()

    # reset_dhw_params
    await water_heater.async_reset_dhw_params()
    mock_device.reset_config.assert_awaited_once()

    # set_dhw_boost
    await water_heater.async_set_dhw_boost()
    mock_device.set_boost_mode.assert_awaited_once()


async def test_schedule_management(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test get and set schedule methods."""
    # Test Get
    await water_heater.async_get_dhw_schedule()
    mock_device.get_schedule.assert_awaited_once()

    # Test Set (Valid JSON)
    valid_json = '{"mon": []}'
    await water_heater.async_set_dhw_schedule(valid_json)
    mock_device.set_schedule.assert_awaited_once()

    # Test Set (Invalid JSON)
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_set_dhw_schedule("{invalid")
    assert excinfo.value.translation_key == "error_set_schedule"


async def test_backend_error_handling(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test that backend errors raise ServiceValidationError."""
    # Test set_mode error
    mock_device.set_mode.side_effect = ValueError("SQLite error")
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_set_operation_mode(STATE_AUTO)
    assert excinfo.value.translation_key == "error_set_mode"

    # Test set_config error
    mock_device.set_config.side_effect = TypeError("Bad config")
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_set_temperature(60.0)
    assert excinfo.value.translation_key == "error_set_config"

    # Test get_schedule timeout
    mock_device.get_schedule.side_effect = TimeoutError("Timed out")
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_get_dhw_schedule()
    assert excinfo.value.translation_key == "error_get_schedule"

    # Test reset methods
    mock_device.reset_mode.side_effect = ValueError("Reset failed")
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_reset_dhw_mode()
    assert excinfo.value.translation_key == "error_reset_mode"


async def test_async_set_dhw_mode_invalid_args(
    water_heater: RamsesWaterHeater,
) -> None:
    """Test schema validation for internal helpers."""
    # Sending invalid data that fails the internal schema check
    # Note: Using patch to mock SCH_SET_DHW_MODE_EXTRA would be cleaner if the schema logic is complex,
    # but here we test the wrapper's response to a ValueError from the schema.

    with patch(
        "custom_components.ramses_cc.water_heater.SCH_SET_DHW_MODE_EXTRA",
        side_effect=ValueError("Invalid Schema"),
    ):
        with pytest.raises(ServiceValidationError) as excinfo:
            await water_heater.async_set_dhw_mode(mode="invalid_mode")

        assert excinfo.value.translation_key == "invalid_mode_args"


async def test_error_handling_coverage_gap(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test error handling paths that were previously missed."""

    # 1. Test ProtocolSendFailed suppression (logs error, does not raise)

    # set_dhw_mode (async_set_dhw_mode)
    mock_device.set_mode.side_effect = ProtocolSendFailed("RF transmission failed")
    # FIX: ZoneMode.TEMPORARY requires active=True to pass schema validation
    await water_heater.async_set_dhw_mode(mode=ZoneMode.TEMPORARY, active=True)
    mock_device.set_mode.assert_awaited()

    # reset_mode (async_reset_dhw_mode)
    mock_device.reset_mode.side_effect = ProtocolSendFailed("RF transmission failed")
    await water_heater.async_reset_dhw_mode()
    mock_device.reset_mode.assert_awaited()

    # reset_config (async_reset_dhw_params)
    mock_device.reset_config.side_effect = ProtocolSendFailed("RF transmission failed")
    await water_heater.async_reset_dhw_params()
    mock_device.reset_config.assert_awaited()

    # set_boost (async_set_dhw_boost)
    mock_device.set_boost_mode.side_effect = ProtocolSendFailed(
        "RF transmission failed"
    )
    await water_heater.async_set_dhw_boost()
    mock_device.set_boost_mode.assert_awaited()

    # set_config (async_set_dhw_params) - ProtocolSendFailed path
    mock_device.set_config.side_effect = ProtocolSendFailed("RF transmission failed")
    await water_heater.async_set_dhw_params(setpoint=50)
    mock_device.set_config.assert_awaited()

    # 2. Test ServiceValidationError mapping for methods not covered in test_backend_error_handling

    # reset_config raising ValueError (async_reset_dhw_params)
    mock_device.reset_config.side_effect = ValueError("Invalid config")
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_reset_dhw_params()
    assert excinfo.value.translation_key == "error_reset_config"

    # set_boost raising TypeError (async_set_dhw_boost)
    mock_device.set_boost_mode.side_effect = TypeError("Invalid argument")
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_set_dhw_boost()
    assert excinfo.value.translation_key == "error_set_boost"

    # set_dhw_params raising ValueError (async_set_dhw_params)
    mock_device.set_config.side_effect = ValueError("Value out of range")
    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_set_dhw_params(setpoint=100)
    assert excinfo.value.translation_key == "error_set_config"

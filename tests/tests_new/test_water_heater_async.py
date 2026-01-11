"""Tests for the asynchronous water heater platform in ramses_cc."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.water_heater import STATE_OFF
from homeassistant.exceptions import ServiceValidationError

from custom_components.ramses_cc.const import DOMAIN, ZoneMode
from custom_components.ramses_cc.water_heater import (
    STATE_AUTO,
    STATE_BOOST,
    RamsesWaterHeater,
)

# Constants for testing
TEST_DEVICE_ID = "10:123456"


@pytest.fixture
def mock_device() -> MagicMock:
    """Return a mock DhwZone device."""
    device = MagicMock()
    device.id = TEST_DEVICE_ID
    device.mode = {"mode": ZoneMode.SCHEDULE, "active": True}
    device.temperature = 55.0
    device.setpoint = 60.0
    device.params = {}
    device.schedule = []
    device.schedule_version = 1

    # Async methods on the device
    device.set_mode = AsyncMock()
    device.set_config = AsyncMock()
    device.reset_mode = AsyncMock()
    device.reset_config = AsyncMock()
    device.set_boost_mode = AsyncMock()
    device.get_schedule = AsyncMock()
    device.set_schedule = AsyncMock()

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
    until_arg = kwargs["until"]
    expected_until = now + timedelta(hours=1)
    assert abs((until_arg - expected_until).total_seconds()) < 1.0


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


async def test_backend_error_handling(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test that backend errors raise ServiceValidationError."""

    # Simulate a backend error (e.g., thread violation or device unreachability)
    mock_device.set_mode.side_effect = ValueError("SQLite error")

    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_set_operation_mode(STATE_AUTO)

    # Check the translation key and parameters
    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "error_set_mode"
    # ServiceValidationError string representation is the key, not the full message.
    # We must check the placeholders to find the underlying error text.
    assert "SQLite error" in str(excinfo.value.translation_placeholders)


async def test_async_get_dhw_schedule_timeout(
    water_heater: RamsesWaterHeater, mock_device: MagicMock
) -> None:
    """Test timeout handling when fetching schedule."""
    mock_device.get_schedule.side_effect = TimeoutError("Timed out")

    with pytest.raises(ServiceValidationError) as excinfo:
        await water_heater.async_get_dhw_schedule()

    assert excinfo.value.translation_key == "error_get_schedule"


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

"""Tests for the climate async migration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import ServiceValidationError

from custom_components.ramses_cc.climate import RamsesController, RamsesZone
from custom_components.ramses_cc.const import SystemMode, ZoneMode


@pytest.fixture
def mock_broker() -> MagicMock:
    """Mock the RamsesBroker."""
    return MagicMock()


@pytest.fixture
def mock_evohome() -> MagicMock:
    """Mock the Evohome device."""
    device = MagicMock()
    device.id = "01:123456"
    device.set_mode = AsyncMock()
    device.reset_mode = AsyncMock()
    # Mocks for properties used in __init__ or properties
    device.zones = []
    device.system_mode = {
        "system_mode": SystemMode.AUTO,
    }
    return device


@pytest.fixture
def mock_zone() -> MagicMock:
    """Mock the Zone device."""
    device = MagicMock()
    device.id = "04:123456"
    device.set_mode = AsyncMock()
    device.reset_mode = AsyncMock()
    device.set_config = AsyncMock()
    device.reset_config = AsyncMock()
    # Mocks for properties
    device.temperature = 20.0
    device.setpoint = 21.0
    device.params = {}
    device.idx = "01"
    device.heating_type = "radiator"
    device.mode = {"mode": ZoneMode.SCHEDULE, "setpoint": 21.0}
    device.config = {"min_temp": 5.0, "max_temp": 35.0}
    device.schedule = []
    device.schedule_version = 1
    # Linked TCS
    tcs = MagicMock()
    tcs.system_mode = {"system_mode": SystemMode.AUTO}
    device.tcs = tcs
    return device


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_controller_async_set_hvac_mode(
    mock_init: MagicMock, mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test RamsesController.async_set_hvac_mode awaits set_mode."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())
    entity._device = mock_evohome
    entity.async_write_ha_state_delayed = MagicMock()

    # Test Valid Mode
    await entity.async_set_hvac_mode(HVACMode.OFF)
    mock_evohome.set_mode.assert_awaited_once_with(SystemMode.HEAT_OFF, until=None)


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_controller_async_set_preset_mode(
    mock_init: MagicMock, mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test RamsesController.async_set_preset_mode awaits set_mode."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())
    entity._device = mock_evohome
    entity.async_write_ha_state_delayed = MagicMock()

    # Test Valid Preset
    await entity.async_set_preset_mode("away")
    mock_evohome.set_mode.assert_awaited_once_with(SystemMode.AWAY, until=None)


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_controller_validation_error(
    mock_init: MagicMock, mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test validation errors raise ServiceValidationError."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())
    entity._device = mock_evohome

    # Mock set_mode to raise Voluptuous error
    mock_evohome.set_mode.side_effect = vol.Invalid("Invalid mode")

    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_zone_async_set_hvac_mode(
    mock_init: MagicMock, mock_broker: MagicMock, mock_zone: MagicMock
) -> None:
    """Test RamsesZone.async_set_hvac_mode awaits helpers."""
    entity = RamsesZone(mock_broker, mock_zone, MagicMock())
    entity._device = mock_zone
    entity.async_write_ha_state_delayed = MagicMock()

    # Test Auto (Reset Mode)
    await entity.async_set_hvac_mode(HVACMode.AUTO)
    mock_zone.reset_mode.assert_awaited_once()

    # Test Heat (Set Permanent)
    mock_zone.reset_mode.reset_mock()
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    mock_zone.set_mode.assert_awaited_once_with(
        mode=ZoneMode.PERMANENT, setpoint=25, until=None
    )


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_zone_async_set_temperature(
    mock_init: MagicMock, mock_broker: MagicMock, mock_zone: MagicMock
) -> None:
    """Test RamsesZone.async_set_temperature awaits set_mode."""
    entity = RamsesZone(mock_broker, mock_zone, MagicMock())
    entity._device = mock_zone
    entity.async_write_ha_state_delayed = MagicMock()

    await entity.async_set_temperature(temperature=22.5)
    mock_zone.set_mode.assert_awaited()
    # Verify kwargs were passed correctly (mode=ADVANCED inferred from args)
    _, kwargs = mock_zone.set_mode.call_args
    assert kwargs["setpoint"] == 22.5


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_zone_helpers_are_async(
    mock_init: MagicMock, mock_broker: MagicMock, mock_zone: MagicMock
) -> None:
    """Verify helpers are awaitable."""
    entity = RamsesZone(mock_broker, mock_zone, MagicMock())
    entity._device = mock_zone
    entity.async_write_ha_state_delayed = MagicMock()

    await entity.async_reset_zone_config()
    mock_zone.reset_config.assert_awaited_once()

    await entity.async_set_zone_config(min_temp=10)
    mock_zone.set_config.assert_awaited_once_with(min_temp=10)

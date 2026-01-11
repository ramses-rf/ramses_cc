"""Tests for fixes in climate.py (Extension)."""

from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import ServiceValidationError

from custom_components.ramses_cc.climate import RamsesController, RamsesHvac, RamsesZone


@pytest.fixture
def mock_broker() -> MagicMock:
    """Mock the RamsesBroker."""
    return MagicMock()


@pytest.fixture
def mock_evohome() -> MagicMock:
    """Mock the Evohome device."""
    device = MagicMock()
    device.id = "01:123456"
    device.zones = []
    device.system_mode = None
    return device


@pytest.fixture
def mock_zone() -> MagicMock:
    """Mock the Zone device."""
    device = MagicMock()
    device.id = "04:123456"
    device.idx = "01"
    device.tcs.system_mode = None
    device.mode = None
    device.config = {}
    return device


@pytest.fixture
def mock_hvac() -> MagicMock:
    """Mock the Hvac device."""
    device = MagicMock()
    device.id = "30:123456"
    device.fan_info = None
    return device


async def test_controller_set_hvac_mode_success(
    mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test setting HVAC mode successfully (Async migration check)."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())

    # Test converting HA HEAT to TCS AUTO
    with patch.object(entity, "async_set_system_mode") as mock_set_mode:
        await entity.async_set_hvac_mode(HVACMode.HEAT)
        mock_set_mode.assert_called_once_with("auto")


async def test_controller_set_hvac_mode_invalid(
    mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test setting invalid HVAC mode raises ServiceValidationError."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())

    # Test passing None or unmapped mode
    # Assuming Mapped dict is {HEAT: auto, OFF: heat_off, AUTO: reset}
    # Passing DRY (Cool) should fail
    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.DRY)


async def test_controller_set_hvac_mode_validation_error(
    mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test internal validation error catches Voluptuous error."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())

    with (
        patch.object(entity, "async_set_system_mode", side_effect=vol.Invalid("Bad")),
        pytest.raises(ServiceValidationError),
    ):
        await entity.async_set_hvac_mode(HVACMode.HEAT)


async def test_controller_set_preset_mode_success(
    mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test setting preset mode successfully."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())

    # Test converting HA AWAY to TCS AWAY
    with patch.object(entity, "async_set_system_mode") as mock_set_mode:
        await entity.async_set_preset_mode("away")
        mock_set_mode.assert_called_once_with("away")


async def test_controller_set_preset_mode_invalid(
    mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test setting invalid preset mode raises ServiceValidationError."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())

    with pytest.raises(ServiceValidationError):
        await entity.async_set_preset_mode("invalid_preset")


async def test_zone_set_temperature_async(
    mock_broker: MagicMock, mock_zone: MagicMock
) -> None:
    """Test setting zone temperature is async."""
    entity = RamsesZone(mock_broker, mock_zone, MagicMock())

    with patch.object(entity, "async_set_zone_mode") as mock_set_mode:
        await entity.async_set_temperature(temperature=21.5)
        mock_set_mode.assert_called_once()


async def test_hvac_mode_enum_return(
    mock_broker: MagicMock, mock_hvac: MagicMock
) -> None:
    """Test RamsesHvac.hvac_mode returns strict Enum."""
    entity = RamsesHvac(mock_broker, mock_hvac, MagicMock())

    # Case 1: Fan is "off" -> HVACMode.OFF
    mock_hvac.fan_info = "off"
    assert entity.hvac_mode is HVACMode.OFF
    assert isinstance(entity.hvac_mode, HVACMode)

    # Case 2: Fan is "low" -> HVACMode.AUTO
    mock_hvac.fan_info = "low"
    assert entity.hvac_mode is HVACMode.AUTO
    assert isinstance(entity.hvac_mode, HVACMode)

    # Case 3: None
    mock_hvac.fan_info = None
    assert entity.hvac_mode is None

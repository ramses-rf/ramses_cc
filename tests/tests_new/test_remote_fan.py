"""Tests for ramses_cc remote platform and fan rate entities.

This module tests the Remote entity (for sending commands) and the
Number entities specifically used for Fan Rate overrides.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.number import (
    RamsesFanRateNumber,
    RamsesNumberEntityDescription,
)
from custom_components.ramses_cc.remote import RamsesRemote

# Constants
REMOTE_ID = "32:123456"
FAN_ID = "30:654321"


@pytest.fixture
def mock_broker() -> MagicMock:
    """Return a mock RamsesBroker.

    :return: A mock object simulating the RamsesBroker.
    """
    broker = MagicMock()
    broker.async_set_fan_rate = AsyncMock()
    return broker


@pytest.fixture
def mock_remote_device() -> MagicMock:
    """Return a mock Remote device.

    :return: A mock representing a RAMSES remote device.
    """
    device = MagicMock()
    device.id = REMOTE_ID
    device._SLUG = "REM"
    return device


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device.

    :return: A mock representing a RAMSES fan device.
    """
    device = MagicMock()
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.boost_inf = None
    return device


async def test_remote_entity_send_command(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test the RamsesRemote send_command logic.

    :param mock_broker: The mock broker fixture.
    :param mock_remote_device: The mock remote device fixture.
    """
    description = MagicMock()
    remote = RamsesRemote(mock_broker, mock_remote_device, description)

    # Mock the internal RF device command method
    mock_remote_device.send_cmd = MagicMock()

    # Simulate sending a raw command string
    remote.send_command(command="RQ 01:123456 1F09 00")

    assert mock_remote_device.send_cmd.called
    assert mock_remote_device.send_cmd.call_args[0][0] == "RQ 01:123456 1F09 00"


async def test_fan_rate_entity_logic(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test RamsesFanRateNumber speed override logic in number.py.

    This targets the missing lines in number.py related to fan rate overrides.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    desc = RamsesNumberEntityDescription(
        key="fan_rate",
        min_value=0,
        max_value=100,
        native_step=1,
    )

    entity = RamsesFanRateNumber(mock_broker, mock_fan_device, desc)
    entity.hass = hass

    # 1. Test state from device (simulating override state)
    mock_fan_device.boost_inf = {"speed": 0.5}  # 50%
    assert entity.native_value == 50.0

    # 2. Test setting rate (async_set_native_value)
    await entity.async_set_native_value(80.0)

    # Verify broker call
    mock_broker.async_set_fan_rate.assert_called_once()
    call_args = mock_broker.async_set_fan_rate.call_args[0][0]
    assert call_args["device_id"] == FAN_ID
    assert call_args["value"] == 0.8  # 80% converted to 0.8

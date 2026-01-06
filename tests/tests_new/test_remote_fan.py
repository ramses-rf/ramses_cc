"""Tests for ramses_cc remote platform and fan parameter entities.

This module tests the Remote entity and the Number entities used for
fan parameter overrides.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
    normalize_device_id,
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
    # Mock the entry for unique_id/registry logic if needed
    broker.entry = MagicMock()
    broker.entry.entry_id = "test_entry"
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
    device.get_fan_param = MagicMock(return_value=None)
    return device


def test_normalize_device_id() -> None:
    """Test the device ID normalization helper."""
    assert normalize_device_id("01:123456") == "01_123456"
    assert normalize_device_id("30:ABCDEF") == "30_abcdef"


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


async def test_fan_boost_param_logic(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test RamsesNumberParam logic specifically for Boost Mode (Param 95).

    This targets scaling logic in RamsesNumberParam.native_value.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    desc = RamsesNumberEntityDescription(
        key="param_95",
        ramses_rf_attr="95",
        min_value=0,
        max_value=1.0,
        unit_of_measurement="%",
    )

    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity.hass = hass

    # 1. Test scaling for display (0.7 internally -> 70.0% UI)
    # This hits the _is_boost_mode_param() branch in native_value
    entity._param_native_value["95"] = 0.7
    assert entity.native_value == 70.0

    # 2. Test setting value (async_set_native_value)
    # For param 95, it should send the raw value without scaling
    with patch.object(hass.services, "async_call", new_callable=AsyncMock) as mock_call:
        await entity.async_set_native_value(80.0)

        assert mock_call.called
        service_data = mock_call.call_args[0][2]
        assert service_data["value"] == 80.0

"""Tests for error handling in broker service calls."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from ramses_rf.exceptions import BindingFlowFailed


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> RamsesBroker:
    """Return a mock broker with an entry attached."""
    entry = MagicMock()
    entry.entry_id = "service_test_entry"
    entry.options = {"ramses_rf": {}, "serial_port": "/dev/ttyUSB0"}

    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()
    broker.client.async_send_cmd = AsyncMock()

    hass.data[DOMAIN] = {entry.entry_id: broker}
    return broker


async def test_bind_device_raises_ha_error(mock_broker: RamsesBroker) -> None:
    """Test that async_bind_device raises HomeAssistantError on binding failure."""

    # Mock the device
    mock_device = MagicMock()
    mock_device.id = "01:123456"
    # Setup the binding process to raise BindingFlowFailed
    mock_device._initiate_binding_process = AsyncMock(
        side_effect=BindingFlowFailed("Timeout waiting for confirm")
    )

    mock_broker.client.fake_device.return_value = mock_device

    # USE MagicMock instead of ServiceCall to ensure .data is a simple, accessible dict
    call = MagicMock()
    call.data = {
        "device_id": "01:123456",
        "offer": {"key": "val"},
        "confirm": {"key": "val"},
        "device_info": None,
    }

    # Assert that HomeAssistantError is raised (wrapping the original error)
    # instead of the raw BindingFlowFailed exception
    with pytest.raises(HomeAssistantError, match="Binding failed for device"):
        await mock_broker.async_bind_device(call)


async def test_set_fan_param_raises_ha_error_invalid_value(
    mock_broker: RamsesBroker,
) -> None:
    """Test that async_set_fan_param raises HomeAssistantError on invalid input."""

    # Call with missing value to trigger ValueError in validation
    call_data = {
        "device_id": "30:111222",
        "param_id": "0A",
        # "value": missing -> This triggers the ValueError
        "from_id": "32:111111",
    }

    # Verify that ValueError is caught and re-raised as HomeAssistantError
    with pytest.raises(HomeAssistantError, match="Invalid parameter for set_fan_param"):
        await mock_broker.async_set_fan_param(call_data)


async def test_set_fan_param_raises_ha_error_no_source(
    mock_broker: RamsesBroker,
) -> None:
    """Test that async_set_fan_param raises HomeAssistantError when no source device is found."""

    call_data = {
        "device_id": "30:111222",
        "param_id": "0A",
        "value": 1,
        # No from_id and no bound device configured in mock -> triggers logic error
    }

    with pytest.raises(HomeAssistantError, match="No valid source device available"):
        await mock_broker.async_set_fan_param(call_data)

"""Tests for ramses_cc remote platform and fan parameter entities.

This module tests the Remote entity and the Number entities used for
fan parameter overrides.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import DOMAIN
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


async def test_remote_entity_unique_id(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test the RamsesRemote unique ID logic.

    :param mock_broker: The mock broker fixture.
    :param mock_remote_device: The mock remote device fixture.
    """
    description = MagicMock()
    # Provide a name in the description to avoid Home Assistant's translation lookup
    description.name = f"Remote {REMOTE_ID}"

    remote = RamsesRemote(mock_broker, mock_remote_device, description)

    assert remote.unique_id == REMOTE_ID
    # We test the unique_id primarily as the name property triggers complex
    # HA internal translation logic that is difficult to mock in isolation.
    assert REMOTE_ID in remote.unique_id


async def test_fan_boost_param_logic(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test RamsesNumberParam logic specifically for Boost Mode (Param 95).

    This targets scaling logic in RamsesNumberParam.native_value and verifies
    service calls.

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
    entity._param_native_value["95"] = 0.7
    assert entity.native_value == 70.0

    # 2. Test setting value (async_set_native_value)
    mock_service_handler = AsyncMock()
    hass.services.async_register(DOMAIN, "set_fan_param", mock_service_handler)

    await entity.async_set_native_value(80.0)
    await hass.async_block_till_done()

    # Verify service was called with correct data
    assert mock_service_handler.called
    service_call = mock_service_handler.call_args[0][0]
    assert service_call.data["value"] == 80.0
    assert service_call.data["param_id"] == "95"

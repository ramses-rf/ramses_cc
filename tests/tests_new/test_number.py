"""Tests for ramses_cc number entities and scaling logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
    normalize_device_id,
)


@pytest.fixture
def mock_broker() -> MagicMock:
    """Return a mock RamsesBroker."""
    broker = MagicMock()
    broker.entry = MagicMock()
    broker.entry.entry_id = "test_entry"
    return broker


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device."""
    device = MagicMock()
    device.id = "30:654321"
    device._SLUG = "FAN"
    device.get_fan_param = MagicMock(return_value=None)
    return device


def test_normalize_device_id() -> None:
    """Test the device ID normalization helper."""
    assert normalize_device_id("01:123456") == "01_123456"
    assert normalize_device_id("30:ABCDEF") == "30_abcdef"


async def test_number_scaling_logic(
    hass: HomeAssistant, mock_broker: MagicMock
) -> None:
    """Test the internal scaling logic of RamsesNumberBase.

    This targets _scale_for_display and _scale_for_storage.
    """
    device = MagicMock()
    device.id = "30:111111"

    # Test Percentage Scaling using parameter 95 (which is scaled)
    desc_pct = RamsesNumberEntityDescription(
        key="test_pct",
        ramses_rf_attr="95",
        unit_of_measurement="%",
    )
    entity_pct = RamsesNumberParam(mock_broker, device, desc_pct)

    # 0.5 internal -> 50.0 display
    assert entity_pct._scale_for_display(0.5) == 50.0
    # 80.0 display -> 0.8 internal
    assert entity_pct._scale_for_storage(80.0) == 0.8

    # Test Non-Percentage Scaling (Pass-through)
    desc_val = RamsesNumberEntityDescription(
        key="test_val",
        ramses_rf_attr="01",
    )
    entity_val = RamsesNumberParam(mock_broker, device, desc_val)

    assert entity_val._scale_for_display(20.5) == 20.5
    assert entity_val._scale_for_storage(20.5) == 20.5

    # Test Error Handling for Invalid Values
    assert entity_val._scale_for_display("invalid") is None


async def test_fan_boost_param_logic(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test RamsesNumberParam logic specifically for Boost Mode (Param 95)."""
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

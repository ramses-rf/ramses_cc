"""Advanced tests for ramses_cc remote platform and number scaling logic.

This module targets coverage for remote platform setup and internal
scaling logic in number entities.
"""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
)
from custom_components.ramses_cc.remote import async_setup_entry

# Constants
REMOTE_ID = "32:222333"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> MagicMock:
    """Return a mock broker configured for remote setup.

    :param hass: The Home Assistant instance.
    :return: A mock RamsesBroker.
    """
    broker = MagicMock()
    broker.entry = MagicMock()
    broker.entry.entry_id = "remote_test_entry"
    broker.devices = []

    hass.data[DOMAIN] = {broker.entry.entry_id: broker}
    return broker


async def test_remote_platform_setup(
    hass: HomeAssistant, mock_broker: MagicMock
) -> None:
    """Test the asynchronous setup of the remote platform.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    """
    async_add_entities = MagicMock()

    # Mock a remote device in the broker
    mock_remote = MagicMock()
    mock_remote.id = REMOTE_ID
    mock_remote._SLUG = "REM"
    mock_broker.devices = [mock_remote]

    # Mock async_get_current_platform to avoid RuntimeError
    with patch(
        "custom_components.ramses_cc.remote.async_get_current_platform"
    ) as mock_get_platform:
        mock_get_platform.return_value = MagicMock()

        # Target async_setup_entry in remote.py
        await async_setup_entry(hass, mock_broker.entry, async_add_entities)

        # Verify that the discovery callback was registered
        assert mock_broker.async_register_platform.called

        # Simulate the callback being triggered with the remote device
        add_devices_callback = mock_broker.async_register_platform.call_args[0][1]
        add_devices_callback([mock_remote])

        assert async_add_entities.called


async def test_number_scaling_logic(
    hass: HomeAssistant, mock_broker: MagicMock
) -> None:
    """Test the internal scaling logic of RamsesNumberBase.

    This targets _scale_for_display and _scale_for_storage.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
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

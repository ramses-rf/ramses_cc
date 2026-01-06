"""Tests for ramses_cc broker service coordination and entity creation.

This module tests the service registration in broker.py and the
entity creation factory in number.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.number import (
    RamsesNumberParam,
    create_parameter_entities,
)

# Constants
FAN_ID = "30:111222"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> RamsesBroker:
    """Return a mock broker with an entry attached.

    :param hass: The Home Assistant instance.
    :return: A configured RamsesBroker.
    """
    entry = MagicMock()
    entry.entry_id = "service_test_entry"
    entry.options = {}

    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()
    broker.client.async_send_cmd = AsyncMock()

    hass.data[DOMAIN] = {entry.entry_id: broker}
    return broker


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device that supports 2411.

    :return: A mock fan device.
    """
    device = MagicMock()
    device.id = FAN_ID
    device.supports_2411 = True
    return device


async def test_create_parameter_entities_logic(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test the factory function for creating number entities.

    This targets the create_parameter_entities logic in number.py.

    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    with patch("custom_components.ramses_cc.number.er.async_get") as mock_ent_reg:
        # Mock the entity registry to return no existing entities
        mock_reg = mock_ent_reg.return_value
        mock_reg.async_get_entity_id.return_value = None

        entities = create_parameter_entities(mock_broker, mock_fan_device)

        # Verify entities were created
        assert len(entities) > 0
        assert all(isinstance(e, RamsesNumberParam) for e in entities)
        # Verify the first entity is correctly linked
        assert entities[0]._device == mock_fan_device


async def test_broker_service_registration(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test that the broker correctly registers services.

    This targets the async_register_services block in broker.py.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    """
    # Trigger the registration logic
    mock_broker.async_register_services()

    # Verify services were registered in the Home Assistant ServiceRegistry
    # ServiceRegistry stores services in a nested dict: {domain: {service_name: handler}}
    services = hass.services.async_services()
    assert DOMAIN in services
    assert "get_fan_param" in services[DOMAIN]
    assert "set_fan_param" in services[DOMAIN]


async def test_broker_device_lookup_fail(mock_broker: RamsesBroker) -> None:
    """Test broker handling when a device lookup fails.

    :param mock_broker: The mock broker fixture.
    """
    # 1. Test get_fan_param with non-existent device
    call_data = {"device_id": "99:999999", "param_id": "01"}

    # We expect a warning in the log but no crash
    with patch("custom_components.ramses_cc.broker._LOGGER.warning") as mock_warn:
        await mock_broker.async_get_fan_param(call_data)
        assert mock_warn.called
        assert "No valid source device available" in mock_warn.call_args[0][0]

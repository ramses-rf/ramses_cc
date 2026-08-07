"""Tests for Polling Interval Diagnostics and set_polling_interval service in ramses_cc."""

from unittest.mock import MagicMock

import pytest

from custom_components.ramses_cc.const import ATTR_POLLING_INTERVAL
from custom_components.ramses_cc.entity import RamsesEntity, RamsesEntityDescription
from custom_components.ramses_cc.services import RamsesServiceHandler


def test_ramses_entity_polling_interval_attribute() -> None:
    """Test RamsesEntity extra_state_attributes includes effective_polling_interval when supported."""
    # Arrange
    coordinator = MagicMock()
    device = MagicMock()
    device.id = "04:123456"
    device.effective_polling_interval = 300.0

    desc = RamsesEntityDescription(key="test_entity")
    entity = RamsesEntity(coordinator, device, desc)

    # Act
    attrs = entity.extra_state_attributes

    # Assert
    assert attrs["id"] == "04:123456"
    assert attrs["effective_polling_interval"] == 300.0


def test_ramses_entity_polling_interval_attribute_absent() -> None:
    """Test RamsesEntity extra_state_attributes when effective_polling_interval is None."""
    # Arrange
    coordinator = MagicMock()
    device = MagicMock()
    device.id = "04:123456"
    device.effective_polling_interval = None

    desc = RamsesEntityDescription(key="test_entity")
    entity = RamsesEntity(coordinator, device, desc)

    # Act
    attrs = entity.extra_state_attributes

    # Assert
    assert attrs["id"] == "04:123456"
    assert "effective_polling_interval" not in attrs


@pytest.mark.asyncio
async def test_service_set_polling_interval() -> None:
    """Test RamsesServiceHandler.async_set_polling_interval updates device polling interval."""
    # Arrange
    hass = MagicMock()
    coordinator = MagicMock()
    device = MagicMock()
    device.id = "04:123456"

    coordinator.hass = hass
    coordinator.client.device_by_id = {"04:123456": device}

    handler = RamsesServiceHandler(coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:123456", ATTR_POLLING_INTERVAL: 600.0}

    # Act
    await handler.async_set_polling_interval(call)

    # Assert
    device.set_polling_interval.assert_called_once_with(600.0)

"""Unit tests for RAMSES CC polling interval diagnostic entity and service."""

from unittest.mock import MagicMock

import pytest

from custom_components.ramses_cc.number import RamsesPollingInterval
from custom_components.ramses_cc.services import RamsesServiceHandler


def test_ramses_polling_interval_native_value() -> None:
    # Arrange
    coordinator = MagicMock()
    device = MagicMock()
    device.id = "10:123456"
    device.effective_polling_interval = {"3EF0": 300, "10E0": 86400}
    entity = RamsesPollingInterval(coordinator, device)

    # Act
    value = entity.native_value

    # Assert
    assert value == 300.0
    assert entity.entity_category.value == "diagnostic"
    assert entity.unique_id == "10:123456_polling_interval"


@pytest.mark.asyncio
async def test_ramses_polling_interval_set_native_value() -> None:
    # Arrange
    coordinator = MagicMock()
    device = MagicMock()
    device.id = "10:123456"
    device.set_polling_interval = MagicMock()
    entity = RamsesPollingInterval(coordinator, device)
    entity.async_write_ha_state = MagicMock()

    # Act
    await entity.async_set_native_value(600.0)

    # Assert
    device.set_polling_interval.assert_called_once_with(600)
    entity.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_service_set_polling_interval_success() -> None:
    # Arrange
    coordinator = MagicMock()
    client = MagicMock()
    device = MagicMock()
    device.set_polling_interval = MagicMock()
    client.device_by_id = {"10:123456": device}
    coordinator.client = client

    services = RamsesServiceHandler(coordinator)
    call = MagicMock()
    call.data = {"device_id": "10:123456", "polling_interval": 600}

    # Act
    await services.async_set_polling_interval(call)

    # Assert
    device.set_polling_interval.assert_called_once_with(600)

"""Tests for the RamsesEntity base class."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import ATTR_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.ramses_cc.const import DOMAIN, SIGNAL_UPDATE
from custom_components.ramses_cc.entity import RamsesEntity, RamsesEntityDescription
from ramses_rf.device import Fakeable
from ramses_rf.entity_base import Entity as RamsesRFEntity

# Constants
DEVICE_ID = "32:123456"


@pytest.fixture
def mock_coordinator(hass: HomeAssistant) -> Any:
    """Return a mock coordinator.

    :param hass: The Home Assistant instance.
    :return: A mocked RamsesCoordinator.
    """
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator._entities = {}
    return coordinator


@pytest.fixture
def mock_device() -> Any:
    """Return a mock RAMSES RF entity with is_available property.

    :return: A MagicMock configured with an is_available PropertyMock.
    """
    device = MagicMock(spec=RamsesRFEntity)
    device.id = DEVICE_ID
    # Configure the class-level property mock
    device.is_available = True
    return device


def test_init(mock_coordinator: Any, mock_device: Any) -> None:
    """Test entity initialization and default attributes.

    :param mock_coordinator: Mocked coordinator fixture.
    :param mock_device: Mocked device fixture.
    """
    description = RamsesEntityDescription(key="test_key")
    entity = RamsesEntity(mock_coordinator, mock_device, description)

    assert entity.unique_id == DEVICE_ID
    assert entity.device_info == DeviceInfo(identifiers={(DOMAIN, DEVICE_ID)})
    assert entity.should_poll is False
    assert entity.has_entity_name is True


def test_extra_state_attributes_basic(mock_coordinator: Any, mock_device: Any) -> None:
    """Test extra_state_attributes returns the device ID by default."""
    description = RamsesEntityDescription(key="test_key")
    entity = RamsesEntity(mock_coordinator, mock_device, description)

    attrs = entity.extra_state_attributes
    assert attrs == {ATTR_ID: DEVICE_ID}


def test_extra_state_attributes_with_extras(
    mock_coordinator: Any, mock_device: Any
) -> None:
    """Test extra_state_attributes includes mapped attributes from the device."""
    # Setup device with specific attributes
    mock_device.attribute_a = "value_a"
    mock_device.attribute_b = "value_b"

    description = RamsesEntityDescription(
        key="test_key",
        ramses_cc_extra_attributes={
            "output_a": "attribute_a",
            "output_b": "attribute_b",
            "output_missing": "attribute_missing",  # Should be ignored
        },
    )
    entity = RamsesEntity(mock_coordinator, mock_device, description)

    attrs = entity.extra_state_attributes
    assert attrs[ATTR_ID] == DEVICE_ID
    assert attrs["output_a"] == "value_a"
    assert attrs["output_b"] == "value_b"
    assert "output_missing" not in attrs


def test_available_property(mock_coordinator: Any, mock_device: Any) -> None:
    """Test the 'available' property via delegation to the RF device.

    :param mock_coordinator: Mocked coordinator fixture.
    :param mock_device: Mocked device fixture.
    """
    description = RamsesEntityDescription(key="test_key")

    # Use cast to Any to stop Mypy from incorrectly assuming 'available' is always True
    entity = RamsesEntity(mock_coordinator, cast(Any, mock_device), description)

    # 1. Device reports available -> True
    mock_device.is_available = True
    assert entity.available is True

    # 2. Device reports unavailable -> False
    mock_device.is_available = False
    assert entity.available is False

    # 3. Legacy check: Device missing is_available attribute -> True (fallback)
    # We create a specific mock without the attribute to test getattr default
    legacy_mock = MagicMock()  # type: ignore[unreachable]
    del legacy_mock.is_available  # Ensure it doesn't exist
    legacy_entity = RamsesEntity(mock_coordinator, legacy_mock, description)
    assert legacy_entity.available is True

    # 4. Faked device -> Always True (Precedence check)
    mock_fake_device = MagicMock(spec=Fakeable)
    mock_fake_device.id = "02:000000"
    mock_fake_device.is_faked = True
    mock_fake_device.is_available = False  # Property says False, but is_faked is True

    entity_fake = RamsesEntity(
        mock_coordinator, cast(Any, mock_fake_device), description
    )
    assert entity_fake.available is True


def test_extra_state_attributes(mock_coordinator: Any, mock_device: Any) -> None:
    """Test the extraction of extra state attributes.

    :param mock_coordinator: Mocked coordinator fixture.
    :param mock_device: Mocked device fixture.
    """
    mock_device.battery_level = 0.5
    mock_device.rssi = -60

    description = RamsesEntityDescription(
        key="test_key",
        ramses_cc_extra_attributes={
            "native_id": "id",
            "battery": "battery_level",
            "signal": "rssi",
        },
    )
    entity = RamsesEntity(mock_coordinator, mock_device, description)

    attrs = entity.extra_state_attributes
    assert attrs[ATTR_ID] == DEVICE_ID
    assert attrs["native_id"] == DEVICE_ID
    assert attrs["battery"] == 0.5
    assert attrs["signal"] == -60


async def test_async_added_to_hass(
    hass: HomeAssistant, mock_coordinator: Any, mock_device: Any
) -> None:
    """Test lifecycle hook when entity is added to Home Assistant.

    :param hass: The Home Assistant instance.
    :param mock_coordinator: Mocked coordinator fixture.
    :param mock_device: Mocked device fixture.
    """
    description = RamsesEntityDescription(key="test_key")
    entity = RamsesEntity(mock_coordinator, mock_device, description)
    entity.hass = hass

    with (
        patch(
            "custom_components.ramses_cc.entity.async_dispatcher_connect"
        ) as mock_connect,
        patch.object(entity, "async_on_remove") as mock_on_remove,
    ):
        await entity.async_added_to_hass()

        # 1. Verify entity is registered in the coordinator
        assert mock_coordinator._entities[DEVICE_ID] == entity

        # 2. Verify signal listener is attached
        expected_signal = f"{SIGNAL_UPDATE}_{DEVICE_ID}"
        mock_connect.assert_called_once_with(
            hass, expected_signal, entity.async_write_ha_state
        )
        assert mock_on_remove.called

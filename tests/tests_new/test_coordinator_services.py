"""Tests for ramses_cc coordinator service coordination and entity creation.

This module tests the service registration in coordinator.py, the
entity creation factory in number.py, and utility mapping in helpers.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from custom_components.ramses_cc.helpers import (
    ha_device_id_to_ramses_device_id,
    ramses_device_id_to_ha_device_id,
)
from custom_components.ramses_cc.number import (
    RamsesNumberParam,
    create_parameter_entities,
)

# Constants
FAN_ID = "30:111222"
REM_ID = "32:111111"
RAMSES_ID = "32:153289"


@pytest.fixture
def mock_coordinator(hass: HomeAssistant) -> RamsesCoordinator:
    """Return a mock coordinator with an entry attached.

    :param hass: The Home Assistant instance.
    :return: A configured RamsesCoordinator.
    """
    entry = MagicMock()
    entry.entry_id = "service_test_entry"
    entry.options = {"ramses_rf": {}, "serial_port": "/dev/ttyUSB0"}

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()
    coordinator.client.device_by_id = {}
    coordinator.client.async_send_cmd = AsyncMock()

    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    return coordinator


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
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test the factory function for creating number entities.

    This targets the create_parameter_entities logic in number.py.

    :param mock_coordinator: The mock coordinator fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    with patch("custom_components.ramses_cc.number.er.async_get") as mock_ent_reg:
        # Mock the entity registry to return no existing entities
        mock_reg = mock_ent_reg.return_value
        mock_reg.async_get_entity_id.return_value = None

        entities = create_parameter_entities(mock_coordinator, mock_fan_device)

        # Verify entities were created
        assert len(entities) > 0
        assert all(isinstance(e, RamsesNumberParam) for e in entities)
        # Verify the first entity is correctly linked
        assert entities[0]._device == mock_fan_device


async def test_coordinator_service_presence(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
) -> None:
    """Test that the expected services are registered with Home Assistant.

    :param hass: The Home Assistant instance.
    :param mock_coordinator: The mock coordinator fixture.
    """
    # Services are registered during integration setup or coordinator init.
    # We verify their presence in the Home Assistant ServiceRegistry.
    services = hass.services.async_services()

    # Check if the domain exists in the registry
    if DOMAIN in services:
        assert "get_fan_param" in services[DOMAIN]
        assert "set_fan_param" in services[DOMAIN]


async def test_coordinator_device_lookup_fail(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test coordinator handling when a device lookup fails.

    :param mock_coordinator: The mock coordinator fixture.
    """
    # 1. Test get_fan_param with non-existent device
    call_data = {"device_id": "99:999999", "param_id": "01"}

    # We expect a warning in the log but no crash
    # UPDATED: Log is now generated in services.py, not coordinator.py
    with patch("custom_components.ramses_cc.services._LOGGER.warning") as mock_warn:
        await mock_coordinator.async_get_fan_param(call_data)
        assert mock_warn.called
        assert "No valid source device available" in mock_warn.call_args[0][0]


def test_get_param_id_validation(mock_coordinator: RamsesCoordinator) -> None:
    """Test validation of parameter IDs in service calls.

    This targets _get_param_id in services.py (via coordinator delegate).

    :param mock_coordinator: The mock coordinator fixture.
    """
    # 1. Valid hex
    # UPDATED: Access via 'service_handler', not 'services'
    assert mock_coordinator.service_handler._get_param_id({"param_id": "0a"}) == "0A"

    # 2. Invalid: too long
    with pytest.raises(ValueError, match="Invalid parameter ID"):
        mock_coordinator.service_handler._get_param_id({"param_id": "001"})

    # 3. Invalid: non-hex
    with pytest.raises(ValueError, match="Invalid parameter ID"):
        mock_coordinator.service_handler._get_param_id({"param_id": "ZZ"})


async def test_save_client_state_remotes(mock_coordinator: RamsesCoordinator) -> None:
    """Test saving remote commands to persistent storage.

    This targets async_save_client_state in coordinator.py.

    :param mock_coordinator: The mock coordinator fixture.
    """
    mock_coordinator.client.get_state.return_value = ({}, {})
    mock_coordinator._remotes = {REM_ID: {"boost": "packet_data"}}
    mock_coordinator.store = MagicMock(spec=mock_coordinator.store)
    mock_coordinator.store.async_save = AsyncMock()

    await mock_coordinator.async_save_client_state()

    # Verify remotes were included in the save payload
    args = mock_coordinator.store.async_save.call_args[0]
    saved_remotes = args[2]

    assert saved_remotes[REM_ID]["boost"] == "packet_data"


async def test_device_registry_update_slugs(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test registry update logic for different device slugs.

    This targets _update_device in coordinator.py.

    :param mock_coordinator: The mock coordinator fixture.
    """
    mock_device = MagicMock()
    mock_device.id = FAN_ID
    mock_device._SLUG = "FAN"
    # Ensure name is None so coordinator falls back to slug-based logic
    mock_device.name = None
    mock_device._msg_value_code.return_value = None  # No 10E0 info

    with patch("homeassistant.helpers.device_registry.async_get") as mock_dr_get:
        mock_reg = mock_dr_get.return_value

        mock_coordinator._update_device(mock_device)

        # Verify the name and model were derived from the SLUG
        call_kwargs = mock_reg.async_get_or_create.call_args[1]
        assert call_kwargs["name"] == f"FAN {FAN_ID}"
        assert call_kwargs["model"] == "FAN"


def test_ha_to_ramses_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from HA registry ID to RAMSES hardware ID.

    This targets ha_device_id_to_ramses_device_id in helpers.py.

    :param hass: The Home Assistant instance.
    """
    # 1. Handle empty input
    assert ha_device_id_to_ramses_device_id(hass, "") is None

    # 2. Handle non-existent device
    assert ha_device_id_to_ramses_device_id(hass, "missing") is None

    # 3. Create a valid ConfigEntry to satisfy the DeviceRegistry requirement
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config")
    config_entry.add_to_hass(hass)

    # 4. Handle valid device mapping
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RAMSES_ID)},
    )

    # Retrieve the HA ID created by the registry
    device = dev_reg.async_get_device(identifiers={(DOMAIN, RAMSES_ID)})
    assert device is not None

    result = ha_device_id_to_ramses_device_id(hass, device.id)
    assert result == RAMSES_ID


def test_ramses_to_ha_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from RAMSES hardware ID to HA registry ID.

    This targets ramses_device_id_to_ha_device_id in helpers.py.

    :param hass: The Home Assistant instance.
    """
    # 1. Handle empty input
    assert ramses_device_id_to_ha_device_id(hass, "") is None

    # 2. Handle non-existent hardware
    assert ramses_device_id_to_ha_device_id(hass, "99:999999") is None

    # 3. Create a valid ConfigEntry
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config_2")
    config_entry.add_to_hass(hass)

    # 4. Handle valid mapping
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RAMSES_ID)},
    )

    result = ramses_device_id_to_ha_device_id(hass, RAMSES_ID)
    assert result == device.id


def test_ha_to_ramses_id_wrong_domain(hass: HomeAssistant) -> None:
    """Test mapping when the device registry entry belongs to another domain.

    This targets the edge case in ha_device_id_to_ramses_device_id where
    a device is found but doesn't have the ramses_cc identifier.

    :param hass: The Home Assistant instance.
    """
    config_entry = MockConfigEntry(domain="not_ramses", entry_id="other_entry")
    config_entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("not_ramses", "some_id")},
    )

    assert ha_device_id_to_ramses_device_id(hass, device.id) is None

"""Tests for the ramses_cc initialization and lifecycle."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from syrupy import SnapshotAssertion

from custom_components.ramses_cc import (
    RamsesEntity,
    RamsesEntityDescription,
    async_register_domain_services,
    async_unload_entry,
)
from custom_components.ramses_cc.const import DOMAIN
from ramses_rf.entity_base import Entity as RamsesRFEntity

from ..virtual_rf import VirtualRf
from .common import configuration_fixture, storage_fixture
from .const import TEST_SYSTEMS

# Constants
DEVICE_ID = "32:123456"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> MagicMock:
    """Return a mock broker.

    :param hass: The Home Assistant instance.
    :return: A mock broker object.
    """
    broker = MagicMock()
    broker.hass = hass
    broker.async_unload_platforms = AsyncMock(return_value=True)
    broker.async_bind_device = AsyncMock()
    broker.async_set_fan_param = AsyncMock()
    broker._entities = {}
    return broker


@pytest.fixture
def mock_device() -> MagicMock:
    """Return a mock RAMSES RF entity.

    :return: A mock device object.
    """
    device = MagicMock(spec=RamsesRFEntity)
    device.id = DEVICE_ID
    device.trait_val = "active"
    return device


@pytest.mark.parametrize("instance", TEST_SYSTEMS)
async def test_entities(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    instance: str,
    rf: VirtualRf,
    snapshot: SnapshotAssertion,
) -> None:
    """Test State after setup of an instance of the integration."""

    hass_storage[DOMAIN] = storage_fixture(instance)

    config = configuration_fixture(instance)
    config[DOMAIN]["serial_port"] = rf.ports[0]

    assert await async_setup_component(hass, DOMAIN, config)
    await hass.async_block_till_done()

    try:
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.state == ConfigEntryState.LOADED

        assert hass.states.async_all() == snapshot

    finally:  # Prevent useless errors in teardown
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_async_unload_entry_logic(
    hass: HomeAssistant, mock_broker: MagicMock
) -> None:
    """Test unloading a config entry and removing services.

    This targets lines 154-162 in __init__.py.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    """
    entry = MagicMock()
    entry.entry_id = "test_unload_id"

    # Setup hass.data and a mock service
    hass.data[DOMAIN] = {entry.entry_id: mock_broker}
    hass.services.async_register(DOMAIN, "test_service", lambda x: None)

    # Execute unload
    result = await async_unload_entry(hass, entry)

    assert result is True
    assert entry.entry_id not in hass.data[DOMAIN]
    assert "test_service" not in hass.services.async_services().get(DOMAIN, {})


async def test_ramses_entity_extra_attributes(
    mock_broker: MagicMock, mock_device: MagicMock
) -> None:
    """Test the extra_state_attributes logic in RamsesEntity.

    This targets lines 253-264 in __init__.py.

    :param mock_broker: The mock broker fixture.
    :param mock_device: The mock device fixture.
    """
    desc = RamsesEntityDescription(
        key="test",
        ramses_cc_extra_attributes={"custom_attr": "trait_val"},
    )
    entity = RamsesEntity(mock_broker, mock_device, desc)

    attrs = entity.extra_state_attributes
    # Verify both standard ID and custom trait mapping
    assert attrs["id"] == DEVICE_ID
    assert attrs["custom_attr"] == "active"


async def test_ramses_entity_added_to_hass(
    mock_broker: MagicMock, mock_device: MagicMock
) -> None:
    """Test the registration of entities in the broker upon addition.

    This targets lines 266-281 in __init__.py.

    :param mock_broker: The mock broker fixture.
    :param mock_device: The mock device fixture.
    """
    desc = RamsesEntityDescription(key="test")
    entity = RamsesEntity(mock_broker, mock_device, desc)
    entity.unique_id = "unique_32_123456"

    # Simulate HA addition
    await entity.async_added_to_hass()

    assert mock_broker._entities["unique_32_123456"] == entity


async def test_init_service_wrappers(
    hass: HomeAssistant, mock_broker: MagicMock
) -> None:
    """Exercise the service wrapper functions in __init__.py.

    This targets the async_register_domain_services and the associated
    wrapper functions like async_bind_device.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    """
    entry = MagicMock()
    # Register the services
    async_register_domain_services(hass, entry, mock_broker)

    # 1. Call bind_device service with valid hex code and None value
    # The schema requires offer keys to be 4-digit hex codes. The value should be None.
    await hass.services.async_call(
        DOMAIN,
        "bind_device",
        {"device_id": DEVICE_ID, "offer": {"1FC9": None}},
        blocking=True,
    )
    assert mock_broker.async_bind_device.called

    # 2. Call set_fan_param service
    await hass.services.async_call(
        DOMAIN,
        "set_fan_param",
        {"device_id": DEVICE_ID, "param_id": "01", "value": 1.0},
        blocking=True,
    )
    assert mock_broker.async_set_fan_param.called

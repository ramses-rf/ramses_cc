"""Tests for the ramses_cc initialization and lifecycle."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.setup import async_setup_component
from syrupy import SnapshotAssertion

from custom_components.ramses_cc import (
    RamsesEntity,
    RamsesEntityDescription,
    async_register_domain_events,
    async_register_domain_services,
    async_unload_entry,
    async_update_listener,
)
from custom_components.ramses_cc.const import (
    CONF_ADVANCED_FEATURES,
    CONF_MESSAGE_EVENTS,
    CONF_SEND_PACKET,
    DOMAIN,
)
from ramses_rf.entity_base import Entity as RamsesRFEntity
from ramses_tx import exceptions as exc

from ..virtual_rf import VirtualRf
from .common import configuration_fixture, storage_fixture
from .const import TEST_SYSTEMS

# Constants
DEVICE_ID = "32:123456"


@pytest.fixture
def mock_coordinator(hass: HomeAssistant) -> MagicMock:
    """Return a mock coordinator.

    :param hass: The Home Assistant instance.
    :return: A mock coordinator object.
    """
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.async_unload_platforms = AsyncMock(return_value=True)
    coordinator.async_bind_device = AsyncMock()
    coordinator.async_force_update = AsyncMock()
    coordinator.async_send_packet = AsyncMock()
    coordinator.async_set_fan_param = AsyncMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_setup = AsyncMock()
    coordinator._entities = {}
    return coordinator


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


async def test_setup_entry_transport_error(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test setup fails with ConfigEntryNotReady on TransportError."""
    entry = MagicMock()
    entry.entry_id = "test_transport_error"
    # Ensure options are present to avoid KeyError
    entry.options = {}

    # Mock RamsesCoordinator class to return our mock_coordinator
    with (
        patch(
            "custom_components.ramses_cc.RamsesCoordinator",
            return_value=mock_coordinator,
        ),
        patch("custom_components.ramses_cc.async_register_domain_services"),
        patch("custom_components.ramses_cc.async_register_domain_events"),
    ):
        # Configure coordinator.async_setup to raise TransportError
        mock_coordinator.async_setup.side_effect = exc.TransportError("Boom")

        # Import the function to test
        from custom_components.ramses_cc import async_setup_entry

        # Initialize data structure
        hass.data[DOMAIN] = {}

        # Expect ConfigEntryNotReady
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

        # Verify cleanup
        assert entry.entry_id not in hass.data[DOMAIN]


async def test_setup_entry_source_invalid(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test setup returns False on TransportSourceInvalid."""
    entry = MagicMock()
    entry.entry_id = "test_source_invalid"
    entry.options = {}

    with (
        patch(
            "custom_components.ramses_cc.RamsesCoordinator",
            return_value=mock_coordinator,
        ),
        patch("custom_components.ramses_cc.async_register_domain_services"),
        patch("custom_components.ramses_cc.async_register_domain_events"),
    ):
        # Configure coordinator.async_setup to raise TransportSourceInvalid
        mock_coordinator.async_setup.side_effect = exc.TransportSourceInvalid(
            "Bad Path"
        )

        from custom_components.ramses_cc import async_setup_entry

        hass.data[DOMAIN] = {}

        # Expect return False
        result = await async_setup_entry(hass, entry)
        assert result is False

        # Verify cleanup
        assert entry.entry_id not in hass.data[DOMAIN]


async def test_setup_entry_already_setup(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test setup returns True if entry is already set up."""
    entry = MagicMock()
    entry.entry_id = "test_already_setup"

    # Pre-populate hass.data to simulate already setup entry
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}

    from custom_components.ramses_cc import async_setup_entry

    # Should return True immediately
    assert await async_setup_entry(hass, entry) is True


async def test_async_update_listener(hass: HomeAssistant) -> None:
    """Test the update listener reloads the entry."""
    entry = MagicMock()
    entry.entry_id = "test_reload"

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await async_update_listener(hass, entry)
        mock_reload.assert_called_once_with(entry.entry_id)


async def test_async_unload_entry_success(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test successful unloading of a config entry."""
    entry = MagicMock()
    entry.entry_id = "test_unload_success"

    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}
    hass.services.async_register(DOMAIN, "test_service", lambda x: None)

    assert await async_unload_entry(hass, entry) is True
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_async_unload_entry_failure(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test unloading failure when platforms fail to unload."""
    entry = MagicMock()
    entry.entry_id = "test_unload_fail"
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}

    # Simulate platform unload failure
    mock_coordinator.async_unload_platforms.return_value = False

    assert await async_unload_entry(hass, entry) is False
    # Coordinator should still be in hass.data if unload failed
    assert entry.entry_id in hass.data[DOMAIN]


async def test_ramses_entity_extra_attributes(
    mock_coordinator: MagicMock, mock_device: MagicMock
) -> None:
    """Test the extra_state_attributes logic in RamsesEntity."""
    desc = RamsesEntityDescription(
        key="test",
        ramses_cc_extra_attributes={"custom_attr": "trait_val"},
    )
    entity = RamsesEntity(mock_coordinator, mock_device, desc)

    attrs = entity.extra_state_attributes
    # Verify both standard ID and custom trait mapping
    assert attrs["id"] == DEVICE_ID
    assert attrs["custom_attr"] == "active"


async def test_ramses_entity_delayed_update(
    mock_coordinator: MagicMock, mock_device: MagicMock
) -> None:
    """Test async_write_ha_state_delayed calls call_later."""
    desc = RamsesEntityDescription(key="test")
    entity = RamsesEntity(mock_coordinator, mock_device, desc)

    with patch.object(mock_coordinator.hass.loop, "call_later") as mock_call_later:
        entity.async_write_ha_state_delayed(5)
        mock_call_later.assert_called_once_with(5, entity.async_write_ha_state)


async def test_ramses_entity_added_to_hass(
    mock_coordinator: MagicMock, mock_device: MagicMock
) -> None:
    """Test the registration of entities in the coordinator upon addition."""
    desc = RamsesEntityDescription(key="test")
    entity = RamsesEntity(mock_coordinator, mock_device, desc)
    entity.unique_id = "unique_32_123456"

    # Simulate HA addition
    await entity.async_added_to_hass()

    assert mock_coordinator._entities["unique_32_123456"] == entity


async def test_init_service_wrappers(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Exercise the service wrapper functions in __init__.py."""
    entry = MagicMock()
    entry.options = {}  # No advanced features

    # Register the services
    async_register_domain_services(hass, entry, mock_coordinator)

    # 1. Bind Device
    await hass.services.async_call(
        DOMAIN,
        "bind_device",
        {"device_id": DEVICE_ID, "offer": {"1FC9": None}},
        blocking=True,
    )
    assert mock_coordinator.async_bind_device.called

    # 2. Force Update
    await hass.services.async_call(
        DOMAIN,
        "force_update",
        {},
        blocking=True,
    )
    assert mock_coordinator.async_force_update.called

    # 3. Set Fan Param
    await hass.services.async_call(
        DOMAIN,
        "set_fan_param",
        {"device_id": DEVICE_ID, "param_id": "01", "value": 1.0},
        blocking=True,
    )
    assert mock_coordinator.async_set_fan_param.called

    # 4. Check that Send Packet is NOT registered by default
    assert not hass.services.has_service(DOMAIN, "send_packet")


async def test_init_service_wrappers_advanced(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test registration of advanced services (send_packet)."""
    entry = MagicMock()
    # Enable advanced features
    entry.options = {CONF_ADVANCED_FEATURES: {CONF_SEND_PACKET: True}}

    async_register_domain_services(hass, entry, mock_coordinator)

    # Check that Send Packet IS registered
    assert hass.services.has_service(DOMAIN, "send_packet")

    # Call it to ensure wrapper works
    await hass.services.async_call(
        DOMAIN,
        "send_packet",
        {"device_id": DEVICE_ID, "verb": "RQ", "code": "1234", "payload": "00"},
        blocking=True,
    )
    assert mock_coordinator.async_send_packet.called


async def test_domain_events(hass: HomeAssistant, mock_coordinator: MagicMock) -> None:
    """Test async_register_domain_events callbacks."""
    # 1. Test with configured message events
    entry = MagicMock()
    entry.options = {CONF_ADVANCED_FEATURES: {CONF_MESSAGE_EVENTS: ".*"}}

    # We need to capture the inner 'async_process_msg' function defined inside async_register_domain_events
    with patch.object(mock_coordinator.client, "add_msg_handler") as mock_add_handler:
        async_register_domain_events(hass, entry, mock_coordinator)
        assert mock_add_handler.called
        callback_func = mock_add_handler.call_args[0][0]

    # Mock a Ramses Message
    msg = MagicMock()
    msg.dtm.isoformat.return_value = "2023-01-01T12:00:00"
    msg.src.id = "01:111111"
    msg.dst.id = "01:222222"
    msg.verb = " I"
    msg.code = "1234"
    msg.payload = {}
    msg._pkt = "PACKET_STRING"

    # Create a listener for the bus event
    events = []

    async def capture_event(event: Any) -> None:
        events.append(event)

    hass.bus.async_listen(f"{DOMAIN}_message", capture_event)

    # Fire the callback
    callback_func(msg)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["code"] == "1234"
    assert events[0].data["packet"] == "PACKET_STRING"

    # 2. Test Learn Mode Event Firing
    # Set coordinator to learn mode for this device
    mock_coordinator.learn_device_id = "01:111111"  # Matches msg.src.id
    learn_events = []

    async def capture_learn(event: Any) -> None:
        learn_events.append(event)

    hass.bus.async_listen(f"{DOMAIN}_learn", capture_learn)

    # Fire the callback again
    callback_func(msg)
    await hass.async_block_till_done()

    assert len(learn_events) == 1
    assert learn_events[0].data["src"] == "01:111111"
    assert learn_events[0].data["packet"] == "PACKET_STRING"


async def test_domain_events_no_config(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test async_register_domain_events with no message events configured."""
    entry = MagicMock()
    # No advanced features / message events configured
    entry.options = {}

    with patch.object(mock_coordinator.client, "add_msg_handler") as mock_add_handler:
        async_register_domain_events(hass, entry, mock_coordinator)
        assert mock_add_handler.called
        callback_func = mock_add_handler.call_args[0][0]

    msg = MagicMock()
    msg._pkt = "PACKET"

    events = []

    async def capture_event(event: Any) -> None:
        events.append(event)

    hass.bus.async_listen(f"{DOMAIN}_message", capture_event)

    # Fire callback - should NOT generate an event because no regex was compiled
    callback_func(msg)
    await hass.async_block_till_done()

    assert len(events) == 0

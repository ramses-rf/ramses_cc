"""Tests for the ramses_cc initialization and lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.setup import async_setup_component
from syrupy.assertion import SnapshotAssertion

from custom_components.ramses_cc import (
    _healed_serial_port_options,
    async_migrate_entry,
    async_register_domain_services,
    async_unload_entry,
    async_update_listener,
)
from custom_components.ramses_cc.const import (
    CONF_ADVANCED_FEATURES,
    CONF_FRESH_START,
    CONF_SEND_PACKET,
    DOMAIN,
)
from ramses_tx import exceptions as exc

from ..virtual_rf import VirtualRf
from .common import configuration_fixture, storage_fixture
from .const import TEST_SYSTEMS

# Constants
DEVICE_ID = "32:123456"


async def async_flush_queues(gwy: Any) -> None:
    """Deterministically drain specific backend CQRS queues.

    Hardcoded references are used to avoid introspection side-effects
    (e.g., prematurely joining transport queues causing test teardown
    drops and lost connections).
    """
    queues: list[asyncio.Queue[Any]] = []

    # 1. Legacy / Top-level Gateway Queues
    if hasattr(gwy, "msg_queue") and isinstance(gwy.msg_queue, asyncio.Queue):
        queues.append(gwy.msg_queue)

    # 2. Engine Layer Queues
    engine = getattr(gwy, "_engine", None)
    if engine and hasattr(engine, "_msg_queue"):
        if isinstance(engine._msg_queue, asyncio.Queue):
            queues.append(engine._msg_queue)

    # 3. Phase 2.95+ Central Dispatcher Queues
    dispatcher = getattr(gwy, "dispatcher", None) or getattr(
        gwy, "central_dispatcher", None
    )
    if dispatcher:
        for q_name in (
            "_in_queue",
            "ssot_queue",
            "discovery_queue",
            "binding_queue",
            "faked_queue",
        ):
            if hasattr(dispatcher, q_name):
                q = getattr(dispatcher, q_name)
                if isinstance(q, asyncio.Queue):
                    queues.append(q)

    # Await specifically targeted queues
    for q in queues:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(q.join(), timeout=5.0)

    # Ensure the event loop has ticked enough to process immediate task
    # results from synchronous TopologyBuilder iterations.
    for _ in range(50):
        await asyncio.sleep(0)


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
    coordinator.async_sync_topology = AsyncMock()
    coordinator.async_send_packet = AsyncMock()
    coordinator.async_set_fan_param = AsyncMock()
    coordinator.async_get_fan_param = AsyncMock()
    coordinator._async_run_fan_param_sequence = AsyncMock()
    coordinator.async_remove_device = AsyncMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_setup = AsyncMock()
    coordinator._entities = {}
    # Mock client for domain events
    coordinator.client = MagicMock()
    return coordinator


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

    # Convert legacy packet_log keys from fixtures to the new schema
    # dynamically
    if "packet_log" in config.get(DOMAIN, {}) and isinstance(
        config[DOMAIN]["packet_log"], dict
    ):
        pkt_log = config[DOMAIN]["packet_log"]
        if "file_name" in pkt_log:
            file_prefix = pkt_log.pop("file_name").split(".")[0]
            pkt_log["packet_log_prefix"] = file_prefix
        if "rotate_backups" in pkt_log:
            pkt_log["packet_log_retention_days"] = pkt_log.pop("rotate_backups")

    # Ensure VirtualRf gateway is in known_list to prevent strict filtering
    # drops
    config[DOMAIN].setdefault("known_list", {})["18:006402"] = {"class": "HGI"}

    # Patch 'available' to always be True during setup so historical packet
    # logs render fully populated states in the snapshot, bypassing 60-min
    # timeout.
    with (
        patch(
            "custom_components.ramses_cc.entity.RamsesEntity.available",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "custom_components.ramses_cc.binary_sensor.RamsesLogbookBinarySensor.available",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "custom_components.ramses_cc.binary_sensor.RamsesSystemBinarySensor.available",
            new_callable=PropertyMock,
            return_value=True,
        ),
    ):
        assert await async_setup_component(hass, DOMAIN, config)
        await hass.async_block_till_done()

        # Deterministically flush all background queues via hardcoded paths
        if DOMAIN in hass.data:
            for coordinator in hass.data[DOMAIN].values():
                if getattr(coordinator, "client", None):
                    await async_flush_queues(coordinator.client)
        await hass.async_block_till_done()

    entry = None
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            entry = entries[0]
            assert entry.state == ConfigEntryState.LOADED

        assert hass.states.async_all() == snapshot

    finally:  # Prevent useless errors in teardown
        if entry:
            assert await hass.config_entries.async_unload(entry.entry_id)
            # Give the transport's background _create_connection task time
            # to finish — ramses_tx creates it in PortTransport.__init__
            # and it can still be pending when the test framework checks
            # for lingering tasks. Increased to 0.5s as 0.1s is insufficient
            # until ramses_rf PR with proper task awaiting is merged.
            # TODO: Revert to 0.1s or remove sleep once ramses_rf fix is merged
            # (commit 96c9d26e: fix: cancel lingering _create_connection task on transport close)
            await asyncio.sleep(0.5)
            await hass.async_block_till_done()


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
        # no events platform setup
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
        # no events platform setup
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


async def test_async_unload_entry_removes_domain_services(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Unload removes all domain services, including discovery scan ones.

    Discovery scan services are registered conditionally (passive scan
    enabled). If not removed on unload, they would linger with a stale
    coordinator reference when scan is disabled before a reload.
    """
    entry = MagicMock()
    entry.entry_id = "test_unload_services"
    entry.options = {CONF_ADVANCED_FEATURES: {"passive_scan": True}}

    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}
    async_register_domain_services(hass, entry, mock_coordinator)

    # Discovery scan services registered (passive scan enabled)
    assert hass.services.has_service(DOMAIN, "get_discovered_devices")
    assert hass.services.has_service(DOMAIN, "sync_topology")

    assert await async_unload_entry(hass, entry) is True

    # All domain services removed, including the conditional ones
    for svc in (
        "force_update",
        "sync_topology",
        "get_discovered_devices",
        "accept_discovered_device",
        "discard_discovered_device",
        "remove_discovered_device",
        "enable_discovered_device",
        "disable_discovered_device",
        "add_faked_rem",
    ):
        assert not hass.services.has_service(DOMAIN, svc), svc


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

    # 2b. Sync Topology
    await hass.services.async_call(
        DOMAIN,
        "sync_topology",
        {},
        blocking=True,
    )
    assert mock_coordinator.async_sync_topology.called

    # 3. Set Fan Param
    await hass.services.async_call(
        DOMAIN,
        "set_fan_param",
        {"device_id": DEVICE_ID, "param_id": "01", "value": 1.0},
        blocking=True,
    )
    assert mock_coordinator.async_set_fan_param.called

    # 4. Get Fan Param
    await hass.services.async_call(
        DOMAIN,
        "get_fan_param",
        {"device_id": DEVICE_ID, "param_id": "01"},
        blocking=True,
    )
    assert mock_coordinator.async_get_fan_param.called

    # 5. Update Fan Params
    await hass.services.async_call(
        DOMAIN,
        "update_fan_params",
        {"device_id": DEVICE_ID},
        blocking=True,
    )
    assert mock_coordinator._async_run_fan_param_sequence.called

    # 6. Check that Send Packet is NOT registered by default
    assert not hass.services.has_service(DOMAIN, "send_packet")

    # 7. Remove Device (always registered, no passive scan needed)
    await hass.services.async_call(
        DOMAIN,
        "remove_device",
        {"device_id": "04:056053"},
        blocking=True,
    )
    assert mock_coordinator.async_remove_device.called


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
        {
            "device_id": DEVICE_ID,
            "verb": "RQ",
            "code": "1234",
            "payload": "00",
        },
        blocking=True,
    )
    assert mock_coordinator.async_send_packet.called


async def test_async_migrate_entry_v1_to_v3(hass: HomeAssistant) -> None:
    """Test the migration of a config entry from version 1 to 3 (chained)."""
    entry = MagicMock()
    entry.version = 1
    entry.entry_id = "test_migration_v1_v3"

    # Mocking legacy options that need to be cleaned up
    entry.options = {
        "packet_log": {
            "file_name": "packet.log",
            "buffer_capacity": 100,
        },
        "ramses_rf": {
            "use_database": True,
            "database_file": "ramses.db",
            "enforce_known_list": True,
        },
        "other_setting": "kept",
    }

    with patch.object(hass.config_entries, "async_update_entry") as mock_update:
        # Make async_update_entry actually update entry.version and entry.options
        # so the chained v2→v3 migration sees the updated state
        def _do_update(ent, **kwargs):
            if "version" in kwargs:
                ent.version = kwargs["version"]
            if "options" in kwargs:
                ent.options = kwargs["options"]

        mock_update.side_effect = _do_update
        result = await async_migrate_entry(hass, entry)

        assert result is True
        # v1→v2 is called first, then v2→v3 (no known_list, so just version bump)
        assert mock_update.call_count == 2
        # First call: v1→v2 (strip deprecated keys)
        assert mock_update.call_args_list[0] == call(
            entry,
            options={
                "packet_log": {
                    "buffer_capacity": 100,
                },
                "ramses_rf": {
                    "enforce_known_list": True,
                },
                "other_setting": "kept",
            },
            version=2,
        )
        # Second call: v2→v3 (no known_list to merge, just version bump)
        assert mock_update.call_args_list[1] == call(
            entry,
            options={
                "packet_log": {
                    "buffer_capacity": 100,
                },
                "ramses_rf": {
                    "enforce_known_list": True,
                },
                "other_setting": "kept",
            },
            version=3,
        )


async def test_async_migrate_entry_v2_to_v3_no_known_list(
    hass: HomeAssistant,
) -> None:
    """Test that a version 2 config entry is migrated to v3 (version bump only
    when there's no known_list to merge)."""
    entry = MagicMock()
    entry.version = 2
    entry.entry_id = "test_migration_v2_v3"
    entry.options = {"packet_log": {}}

    with patch.object(hass.config_entries, "async_update_entry") as mock_update:
        result = await async_migrate_entry(hass, entry)

        assert result is True
        mock_update.assert_called_once_with(
            entry,
            options={"packet_log": {}},
            version=3,
        )


def test_healed_serial_port_options_from_mqtt_hints() -> None:
    """Test setup-time healing when MQTT hints exist in options."""

    healed = _healed_serial_port_options(
        {
            "serial_port": {},
            "ramses_rf": {"log_all_mqtt": True},
            "mqtt_topic": "RAMSES/GATEWAY_SIM",
        },
        mqtt_entries_present=False,
    )

    assert healed is not None
    assert healed["serial_port"] == {"port_name": "mqtt_ha"}
    assert healed["mqtt_use_ha"] is True


def test_healed_serial_port_options_no_heal_without_mqtt() -> None:
    """Test no healing occurs when MQTT is not implied."""

    healed = _healed_serial_port_options(
        {
            "serial_port": {},
            "ramses_rf": {"log_all_mqtt": False},
        },
        mqtt_entries_present=False,
    )

    assert healed is None


async def test_init_service_wrappers_passive_scan(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test registration of passive scan services when enabled."""
    entry = MagicMock()
    entry.options = {
        CONF_ADVANCED_FEATURES: {"passive_scan": True},
    }

    # Add AsyncMocks for the passive scan service methods
    mock_coordinator.async_get_discovered_devices = AsyncMock()
    mock_coordinator.async_accept_discovered_device = AsyncMock()
    mock_coordinator.async_discard_discovered_device = AsyncMock()
    mock_coordinator.async_remove_discovered_device = AsyncMock()
    mock_coordinator.async_enable_discovered_device = AsyncMock()
    mock_coordinator.async_disable_discovered_device = AsyncMock()
    mock_coordinator.async_add_faked_rem = AsyncMock()
    mock_coordinator.async_discover_known_devices = AsyncMock()

    async_register_domain_services(hass, entry, mock_coordinator)

    # Verify all passive scan services are registered
    assert hass.services.has_service(DOMAIN, "get_discovered_devices")
    assert hass.services.has_service(DOMAIN, "accept_discovered_device")
    assert hass.services.has_service(DOMAIN, "discard_discovered_device")
    assert hass.services.has_service(DOMAIN, "remove_discovered_device")
    assert hass.services.has_service(DOMAIN, "enable_discovered_device")
    assert hass.services.has_service(DOMAIN, "disable_discovered_device")
    assert hass.services.has_service(DOMAIN, "add_faked_rem")
    assert hass.services.has_service(DOMAIN, "discover_known_devices")


async def test_init_service_wrappers_passive_scan_not_registered(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test passive scan services are NOT registered when scan is disabled."""
    entry = MagicMock()
    entry.options = {
        CONF_ADVANCED_FEATURES: {"passive_scan": False},
    }

    async_register_domain_services(hass, entry, mock_coordinator)

    # Passive scan services should NOT be registered
    assert not hass.services.has_service(DOMAIN, "get_discovered_devices")
    assert not hass.services.has_service(DOMAIN, "accept_discovered_device")
    assert not hass.services.has_service(DOMAIN, "discard_discovered_device")
    assert not hass.services.has_service(DOMAIN, "add_faked_rem")

    # remove_device is always registered (not passive-scan-only)
    assert hass.services.has_service(DOMAIN, "remove_device")


async def test_init_passive_scan_service_wrappers_called(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test that passive scan service wrappers actually call the coordinator."""
    entry = MagicMock()
    entry.options = {
        CONF_ADVANCED_FEATURES: {"passive_scan": True},
    }

    # Add AsyncMocks for all passive scan service methods
    mock_coordinator.async_get_discovered_devices = AsyncMock()
    mock_coordinator.async_accept_discovered_device = AsyncMock()
    mock_coordinator.async_discard_discovered_device = AsyncMock()
    mock_coordinator.async_remove_discovered_device = AsyncMock()
    mock_coordinator.async_enable_discovered_device = AsyncMock()
    mock_coordinator.async_disable_discovered_device = AsyncMock()
    mock_coordinator.async_add_faked_rem = AsyncMock()
    mock_coordinator.async_discover_known_devices = AsyncMock()

    async_register_domain_services(hass, entry, mock_coordinator)

    # Call each service and verify the coordinator method was called
    await hass.services.async_call(
        DOMAIN, "get_discovered_devices", {"status": "new"}, blocking=True
    )
    assert mock_coordinator.async_get_discovered_devices.called

    await hass.services.async_call(
        DOMAIN, "accept_discovered_device", {"device_id": "04:123456"}, blocking=True
    )
    assert mock_coordinator.async_accept_discovered_device.called

    await hass.services.async_call(
        DOMAIN, "discard_discovered_device", {"device_id": "04:123456"}, blocking=True
    )
    assert mock_coordinator.async_discard_discovered_device.called

    await hass.services.async_call(
        DOMAIN, "remove_discovered_device", {"device_id": "04:123456"}, blocking=True
    )
    assert mock_coordinator.async_remove_discovered_device.called

    await hass.services.async_call(
        DOMAIN, "enable_discovered_device", {"device_id": "04:123456"}, blocking=True
    )
    assert mock_coordinator.async_enable_discovered_device.called

    await hass.services.async_call(
        DOMAIN, "disable_discovered_device", {"device_id": "04:123456"}, blocking=True
    )
    assert mock_coordinator.async_disable_discovered_device.called

    await hass.services.async_call(
        DOMAIN,
        "add_faked_rem",
        {"device_id": "32:123456", "bound_to": "30:160000"},
        blocking=True,
    )
    assert mock_coordinator.async_add_faked_rem.called

    await hass.services.async_call(DOMAIN, "discover_known_devices", {}, blocking=True)
    assert mock_coordinator.async_discover_known_devices.called


async def test_fresh_start_wipes_storage(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """When CONF_FRESH_START is set, async_setup_entry deletes .storage and
    resets the flag before creating the coordinator.
    """
    entry = MagicMock()
    entry.entry_id = "test_fresh_start"
    entry.options = {CONF_FRESH_START: True}

    with (
        patch(
            "custom_components.ramses_cc.RamsesCoordinator",
            return_value=mock_coordinator,
        ),
        patch("custom_components.ramses_cc.async_register_domain_services"),
        patch("homeassistant.helpers.storage.Store") as mock_store_cls,
        patch.object(hass.config_entries, "async_update_entry") as mock_update,
    ):
        mock_store = MagicMock()
        mock_store.async_remove = AsyncMock()
        mock_store_cls.return_value = mock_store

        from custom_components.ramses_cc import async_setup_entry

        hass.data[DOMAIN] = {}
        with contextlib.suppress(Exception):
            await async_setup_entry(hass, entry)

    # .storage cache should have been invalidated
    assert mock_store.async_remove.called, "Expected .storage to be removed"

    # The flag should have been removed via async_update_entry
    mock_update.assert_called()
    update_kwargs = mock_update.call_args.kwargs
    assert CONF_FRESH_START not in update_kwargs.get("options", {})


async def test_no_fresh_start_preserves_storage(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Without CONF_FRESH_START, async_setup_entry does NOT delete .storage."""
    entry = MagicMock()
    entry.entry_id = "test_no_fresh_start"
    entry.options = {}

    with (
        patch(
            "custom_components.ramses_cc.RamsesCoordinator",
            return_value=mock_coordinator,
        ),
        patch("custom_components.ramses_cc.async_register_domain_services"),
        patch("homeassistant.helpers.storage.Store") as mock_store_cls,
    ):
        mock_store = MagicMock()
        mock_store.async_remove = AsyncMock()
        mock_store_cls.return_value = mock_store

        from custom_components.ramses_cc import async_setup_entry

        hass.data[DOMAIN] = {}
        with contextlib.suppress(Exception):
            await async_setup_entry(hass, entry)

    # .storage should NOT have been removed
    mock_store.async_remove.assert_not_called()

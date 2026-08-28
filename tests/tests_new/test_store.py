"""Tests for the storage aspect of RamsesCoordinator (Persistence)."""

import asyncio
from datetime import datetime as dt, timedelta as td
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ramses_cc.const import (
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    SZ_CLIENT_STATE,
    SZ_PACKETS,
    SZ_REMOTES,
    SZ_SCHEMA,
    SZ_TR_COMMANDS,
)
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from custom_components.ramses_cc.discovery import SZ_DISCOVERY
from custom_components.ramses_cc.store import RamsesCcStore, RamsesStore
from ramses_rf.gateway import Gateway

REM_ID = "32:111111"


# -- Part 1: Unit Tests for RamsesStore (Fixes Coverage) --


async def test_store_init(hass: HomeAssistant) -> None:
    """Test the initialization of the store."""
    with patch(
        "custom_components.ramses_cc.store.RamsesCcStore"
    ) as mock_store_cls:
        store = RamsesStore(hass)
        mock_store_cls.assert_called_once()
        assert store._store is not None


async def test_store_uses_ramses_cc_store_subclass(
    hass: HomeAssistant,
) -> None:
    """Test that RamsesStore uses the RamsesCcStore subclass (migration hook)."""
    store = RamsesStore(hass)
    assert isinstance(store._store, RamsesCcStore)


async def test_store_migration_is_noop_identity(hass: HomeAssistant) -> None:
    """Test that the migration func is a no-op identity (STORAGE_VERSION stays at 1).

    The Phase 3a command migration (remotes → schema _commands) is handled
    at runtime by the coordinator's ``_sync_remotes_to_schema``, not by a
    storage version bump.  Bumping the version would break the downgrade
    path: HA's Store raises ``UnsupportedStorageVersionError`` when the
    stored version exceeds ``max_readable_version`` (which defaults to the
    code's ``version``).  Since 0.58.0/0.58.1 have ``STORAGE_VERSION = 1``
    and don't set ``max_readable_version``, they cannot read v2 data.
    """
    store = RamsesStore(hass)
    assert isinstance(store._store, RamsesCcStore)

    v1_data: dict[str, Any] = {
        SZ_CLIENT_STATE: {
            SZ_SCHEMA: {"01:123456": {"_alias": "Test"}},
            SZ_PACKETS: {},
        },
        SZ_REMOTES: {
            "32:153001": {
                "turn_on": "I --- 32:153001 18:006402 --:------ 22F1 003 000030"
            },
        },
    }

    result = await store._store._async_migrate_func(1, 1, v1_data)
    # No-op: data returned as-is, no _commands injected
    assert result is v1_data
    assert SZ_TR_COMMANDS not in result[SZ_CLIENT_STATE][SZ_SCHEMA].get(
        "32:153001", {}
    )


async def test_store_migration_future_version_unchanged(
    hass: HomeAssistant,
) -> None:
    """Test that future version data is returned unchanged."""
    store = RamsesStore(hass)
    data: dict[str, Any] = {"some_key": "some_value"}
    result = await store._store._async_migrate_func(99, 1, data)
    assert result is data


async def test_store_migration_v1_empty_remotes(hass: HomeAssistant) -> None:
    """Test v1 migration with no remotes (no-op identity)."""
    store = RamsesStore(hass)
    v1_data: dict[str, Any] = {
        SZ_CLIENT_STATE: {SZ_SCHEMA: {"01:123456": {}}, SZ_PACKETS: {}},
        SZ_REMOTES: {},
    }
    result = await store._store._async_migrate_func(1, 1, v1_data)
    # No-op: schema unchanged
    assert result is v1_data
    assert result[SZ_CLIENT_STATE][SZ_SCHEMA] == {"01:123456": {}}


async def test_store_migration_v2_to_v1_identity(hass: HomeAssistant) -> None:
    """Test v2 → v1 migration (no-op identity for briefly-released v2).

    The v2 code was briefly released, so some users have .storage files
    with version: 2. The current code (v1, max_readable_version=2) can
    read them — the migrate function is a no-op identity since the v2
    data format is identical to v1.
    """
    store = RamsesStore(hass)
    v2_data: dict[str, Any] = {
        SZ_CLIENT_STATE: {
            SZ_SCHEMA: {"01:123456": {"_commands": {"boost": "packet"}}},
            SZ_PACKETS: {},
        },
        SZ_REMOTES: {"32:153001": {"boost": "packet"}},
    }
    result = await store._store._async_migrate_func(2, 1, v2_data)
    # No-op identity: data returned unchanged
    assert result is v2_data
    assert result[SZ_REMOTES] == {"32:153001": {"boost": "packet"}}


async def test_store_max_readable_version_is_2(hass: HomeAssistant) -> None:
    """Test that the store can read v2 files (max_readable_version=2).

    This ensures users with v2 .storage files (from the briefly-released
    v2 code) can upgrade to the current version without manual intervention.
    """
    store = RamsesStore(hass)
    assert store._store._max_readable_version == 2
    assert store._store.version == 1


async def test_store_async_load(hass: HomeAssistant) -> None:
    """Test loading data from the store."""
    store = RamsesStore(hass)
    # Mock the internal HA Store instance
    store._store = AsyncMock()

    # Case 1: Data exists
    mock_data = {"some_key": "some_value"}
    store._store.async_load.return_value = mock_data
    assert await store.async_load() == mock_data

    # Case 2: No data (None) -> Should return empty dict (Line 32 coverage)
    store._store.async_load.return_value = None
    assert await store.async_load() == {}


async def test_store_async_save(hass: HomeAssistant) -> None:
    """Test saving data to the store."""
    store = RamsesStore(hass)
    store._store = AsyncMock()

    schema = {"device_id": "123"}
    packets: dict[str, Any] = {"date": "packet_data"}
    remotes = {"remote_id": "command"}

    # Execute save (Line 43-47 coverage)
    await store.async_save(schema, packets, remotes)

    expected_data = {
        SZ_CLIENT_STATE: {SZ_SCHEMA: schema, SZ_PACKETS: packets},
        SZ_REMOTES: remotes,
    }
    store._store.async_save.assert_called_once_with(expected_data)


async def test_store_async_save_with_discovery(hass: HomeAssistant) -> None:
    """Test saving data with discovery state included."""
    store = RamsesStore(hass)
    store._store = AsyncMock()

    schema = {"device_id": "123"}
    packets: dict[str, Any] = {"date": "packet_data"}
    remotes = {"remote_id": "command"}
    discovery = {"devices": {"04:056053": {"status": "accepted"}}}

    await store.async_save(schema, packets, remotes, discovery=discovery)

    # Verify discovery state was included in the saved data
    saved_data = store._store.async_save.call_args[0][0]
    assert saved_data[SZ_DISCOVERY] == discovery


async def test_store_async_save_preserves_existing_discovery(
    hass: HomeAssistant,
) -> None:
    """Test that save preserves existing discovery state when discovery=None."""
    store = RamsesStore(hass)
    store._store = AsyncMock()

    existing_discovery = {"devices": {"04:056053": {"status": "accepted"}}}
    # First load returns existing data with discovery
    store._store.async_load.return_value = {SZ_DISCOVERY: existing_discovery}

    schema = {"device_id": "123"}
    packets: dict[str, Any] = {"date": "packet_data"}
    remotes = {"remote_id": "command"}

    # Save without discovery param — should preserve existing
    await store.async_save(schema, packets, remotes, discovery=None)

    saved_data = store._store.async_save.call_args[0][0]
    assert saved_data[SZ_DISCOVERY] == existing_discovery


async def test_store_async_save_preserves_backups(hass: HomeAssistant) -> None:
    """Test that save preserves existing backups."""
    store = RamsesStore(hass)
    store._store = AsyncMock()

    existing_backups = [{"timestamp": 123, "schema": {}, "known_list": {}}]
    store._store.async_load.return_value = {"schema_backups": existing_backups}

    await store.async_save({}, {}, {})

    saved_data = store._store.async_save.call_args[0][0]
    assert saved_data["schema_backups"] == existing_backups


async def test_store_async_save_backup(hass: HomeAssistant) -> None:
    """Test saving a schema backup as a YAML file."""
    store = RamsesStore(hass)
    store._store = AsyncMock()

    # No existing backups
    store._store.async_load.return_value = {}
    schema = {"main_tcs": "01:123456"}
    known_list: dict[str, Any] = {"01:123456": {}}

    filepath = await store.async_save_backup(schema, known_list, reason="test")

    # The backup file should have been written
    assert filepath is not None
    assert "ramses_cc_backups" in filepath
    assert filepath.endswith(".yaml")

    # The .storage index should track the backup
    saved_data = store._store.async_save.call_args[0][0]
    backups = saved_data["schema_backups"]
    assert len(backups) == 1
    assert backups[0]["filepath"] == filepath
    assert backups[0]["reason"] == "test"
    assert "timestamp" in backups[0]
    assert "filename" in backups[0]

    # The YAML file should be readable and contain the schema
    import yaml

    with open(filepath, encoding="utf-8") as f:
        content = yaml.load(f, Loader=yaml.SafeLoader)
    assert content["schema"] == schema
    assert content["known_list"] == known_list


async def test_store_async_save_backup_trims_to_max(
    hass: HomeAssistant,
) -> None:
    """Test that backups are trimmed to _MAX_BACKUPS (5)."""
    store = RamsesStore(hass)
    store._store = AsyncMock()

    # Pre-fill with 5 existing backups (old format with filepath)
    existing = [
        {
            "timestamp": i,
            "reason": "old",
            "filepath": f"/tmp/old_{i}.yaml",
            "filename": f"old_{i}.yaml",
        }
        for i in range(5)
    ]
    store._store.async_load.return_value = {"schema_backups": existing}

    await store.async_save_backup({"new": True}, {}, reason="new")

    saved_data = store._store.async_save.call_args[0][0]
    backups = saved_data["schema_backups"]
    assert len(backups) == 5  # trimmed to max
    # The oldest should have been removed, newest kept
    assert backups[-1]["reason"] == "new"


async def test_store_async_load_backups(hass: HomeAssistant) -> None:
    """Test loading backups from storage."""
    store = RamsesStore(hass)
    store._store = AsyncMock()

    # Case 1: Backups exist
    backups_data = [{"timestamp": 123, "schema": {}, "known_list": {}}]
    store._store.async_load.return_value = {"schema_backups": backups_data}
    result = await store.async_load_backups()
    assert result == backups_data

    # Case 2: No backups key — should return empty list
    store._store.async_load.return_value = {}
    result = await store.async_load_backups()
    assert result == []


# -- Part 2: Integration Tests for Coordinator Persistence (Existing Tests) --


@pytest.fixture  # type: ignore[untyped-decorator]
def mock_hass(event_loop: asyncio.AbstractEventLoop) -> MagicMock:
    """Return a mock Home Assistant instance."""
    hass = MagicMock()
    hass.loop = event_loop  # Use the actual test event loop

    # Use an AsyncMock for async_create_task so it's awaited correctly by HA
    hass.async_create_task = MagicMock(
        side_effect=lambda coro: event_loop.create_task(coro)
    )

    return hass


@pytest.fixture  # type: ignore[untyped-decorator]
def mock_entry() -> MagicMock:
    """Return a mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {
        CONF_SCHEMA: {"18:006402": {"_class": "HGI"}},
        CONF_RAMSES_RF: {},
        "serial_port": "/dev/ttyUSB0",
    }
    entry.async_on_unload = MagicMock()
    return entry


@pytest.fixture  # type: ignore[untyped-decorator]
def mock_coordinator(
    hass: HomeAssistant, mock_entry: MagicMock
) -> RamsesCoordinator:
    """Return a mock coordinator for storage tests."""
    # We use the real RamsesCoordinator but mock its internal store/client
    coordinator = RamsesCoordinator(hass, mock_entry)

    # Mock the store for persistence tests
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock()
    coordinator.store.async_save = AsyncMock()

    # Pre-set the client as a MagicMock with Gateway spec
    coordinator.client = MagicMock(spec=Gateway)
    coordinator.client.start = AsyncMock()
    return coordinator


async def test_setup_with_corrupted_storage_dates(
    hass: HomeAssistant, mock_entry: MagicMock
) -> None:
    """Test that startup survives invalid date strings in storage."""
    # 1. Setup Coordinator
    coordinator = RamsesCoordinator(hass, mock_entry)

    # 2. Mock Storage with corrupted date
    # Phase 4: enforce_known_list is always-on, so the valid packet must
    # contain a device ID that exists in the schema-derived known_list.
    now: dt = dt_util.now()
    timestamp: str = now.isoformat()
    mock_storage_data = {
        SZ_CLIENT_STATE: {
            SZ_PACKETS: {
                timestamp: "00 18:006402 01:145038 ... valid packet ...",
                "INVALID-DATE-STRING": "00 ... corrupted packet ...",
            }
        }
    }

    coordinator.store = MagicMock()  # Ensure store is mocked
    coordinator.store.async_load = AsyncMock(return_value=mock_storage_data)

    # Ensure _create_client returns the mock that we check later
    mock_client = MagicMock(spec=Gateway)
    mock_client.start = AsyncMock()
    coordinator._create_client = MagicMock(return_value=mock_client)

    # 3. Run async_setup
    await coordinator.async_setup()

    # 4. Yield to the event loop
    await asyncio.sleep(0)

    # 5. Verify client started
    assert mock_client.start.called

    # 6. Verify only valid packet was passed to start
    call_args = mock_client.start.call_args
    cached_packets = call_args.kwargs.get("cached_packets", {})

    assert len(cached_packets) == 1
    assert "INVALID-DATE-STRING" not in cached_packets
    await asyncio.sleep(0)


async def test_save_client_state_remotes(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test saving remote commands to persistent storage."""
    # Type Guard for Pyright
    assert mock_coordinator.client is not None

    # Cast methods to MagicMock to access test attributes
    cast(MagicMock, mock_coordinator.client.get_state).return_value = ({}, {})
    mock_coordinator._remotes = {REM_ID: {"boost": "packet_data"}}

    # Reset mocks to clear any setup calls
    cast(MagicMock, mock_coordinator.store.async_save).reset_mock()

    await mock_coordinator.async_save_client_state()

    # Verify remotes were included in the save payload
    assert cast(MagicMock, mock_coordinator.store.async_save).called
    args = cast(MagicMock, mock_coordinator.store.async_save).call_args[0]
    saved_remotes = args[2]

    assert saved_remotes == mock_coordinator._remotes


async def test_setup_packet_filtering(
    hass: HomeAssistant, mock_entry: MagicMock
) -> None:
    """Test logic for filtering cached packets based on age and known list."""
    coordinator = RamsesCoordinator(hass, mock_entry)

    mock_client = MagicMock(spec=Gateway)
    mock_client.start = AsyncMock()
    coordinator._create_client = MagicMock(return_value=mock_client)

    now: dt = dt_util.now()
    old_date = (now - td(days=2)).isoformat()
    recent_date = (now - td(hours=1)).isoformat()

    # Phase 4: known_list is derived from schema, enforce_known_list is always-on
    coordinator.options[CONF_SCHEMA] = {"01:123456": {}}
    coordinator.options[CONF_RAMSES_RF] = {}

    padding = " " * 11
    valid_packet = f"{padding}01:123456" + (" " * 20)
    unknown_packet = f"{padding}99:999999" + (" " * 20)

    mock_storage_data = {
        SZ_CLIENT_STATE: {
            SZ_PACKETS: {
                old_date: valid_packet,
                recent_date: valid_packet,
                (now - td(minutes=1)).isoformat(): unknown_packet,
            }
        }
    }
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value=mock_storage_data)

    await coordinator.async_setup()

    mock_client.start.assert_called_once()
    packets = mock_client.start.call_args.kwargs["cached_packets"]

    assert recent_date in packets
    assert old_date not in packets
    assert len(packets) == 1

    await asyncio.sleep(0)

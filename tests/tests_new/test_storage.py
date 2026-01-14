"""Tests for the storage aspect of RamsesBroker (Persistence)."""

import asyncio
from datetime import datetime as dt, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ramses_cc.broker import SZ_CLIENT_STATE, SZ_PACKETS, RamsesBroker
from custom_components.ramses_cc.const import CONF_RAMSES_RF, CONF_SCHEMA, DOMAIN
from ramses_tx.schemas import SZ_KNOWN_LIST

REM_ID = "32:111111"


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a mock Home Assistant instance."""
    hass = MagicMock()
    hass.loop = MagicMock()

    # FIX: Define a helper that schedules the coroutine on the REAL event loop
    def _create_task(coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)

    # Apply this to both loop.create_task AND async_create_task
    # This ensures any coroutine passed by broker.py gets scheduled (and awaited)
    hass.loop.create_task.side_effect = _create_task
    hass.async_create_task = MagicMock(side_effect=_create_task)

    return hass


@pytest.fixture
def mock_entry() -> MagicMock:
    """Return a mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {},
        CONF_RAMSES_RF: {},
        "serial_port": "/dev/ttyUSB0",
    }
    entry.async_on_unload = MagicMock()
    return entry


@pytest.fixture
def mock_broker(mock_hass: MagicMock, mock_entry: MagicMock) -> RamsesBroker:
    """Return a mock broker for storage tests."""
    broker = RamsesBroker(mock_hass, mock_entry)
    broker.client = MagicMock()
    broker._store = MagicMock()
    broker._store.async_load = AsyncMock()
    broker._store.async_save = AsyncMock()

    # Mock hass.data
    mock_hass.data = {DOMAIN: {mock_entry.entry_id: broker}}
    return broker


async def test_setup_with_corrupted_storage_dates(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that startup survives invalid date strings in storage."""
    # 1. Setup Broker
    broker = RamsesBroker(mock_hass, mock_entry)

    # 2. Mock Storage with corrupted date
    # Valid date: 2023-01-01T12:00:00
    # Invalid date: "INVALID-DATE-STRING"
    mock_storage_data = {
        SZ_CLIENT_STATE: {
            SZ_PACKETS: {
                dt.now().isoformat(): "00 ... valid packet ...",
                "INVALID-DATE-STRING": "00 ... corrupted packet ...",
            }
        }
    }

    broker._store.async_load = AsyncMock(return_value=mock_storage_data)

    # FIX: Ensure _create_client returns the mock that we check later
    mock_client = MagicMock()
    mock_client.start = AsyncMock()
    broker._create_client = MagicMock(return_value=mock_client)

    # 3. Run async_setup
    # This should NOT raise ValueError
    await broker.async_setup()

    # 4. Verify client started
    assert mock_client.start.called

    # 5. Verify only valid packet was passed to start
    call_args = mock_client.start.call_args
    cached_packets = call_args.kwargs.get("cached_packets", {})

    assert len(cached_packets) == 1
    assert "INVALID-DATE-STRING" not in cached_packets


async def test_save_client_state_remotes(mock_broker: RamsesBroker) -> None:
    """Test saving remote commands to persistent storage."""
    mock_broker.client.get_state.return_value = ({}, {})
    mock_broker._remotes = {REM_ID: {"boost": "packet_data"}}

    # Reset mocks to clear any setup calls
    mock_broker._store.async_save.reset_mock()

    await mock_broker.async_save_client_state()

    # Verify remotes were included in the save payload
    assert mock_broker._store.async_save.called
    save_data = mock_broker._store.async_save.call_args[0][0]
    assert "remotes" in save_data
    assert save_data["remotes"][REM_ID]["boost"] == "packet_data"


async def test_setup_packet_filtering(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test logic for filtering cached packets based on age and known list."""
    broker = RamsesBroker(mock_hass, mock_entry)

    # FIX: Wire up mock_client to be returned by _create_client
    mock_client = MagicMock()
    mock_client.start = AsyncMock()
    broker._create_client = MagicMock(return_value=mock_client)
    # Also set broker.client for convenience, though async_setup overwrites it
    broker.client = mock_client

    now = dt.now()
    old_date = (now - timedelta(days=2)).isoformat()
    recent_date = (now - timedelta(hours=1)).isoformat()

    # Known list contains a device 01:123456
    broker.options[SZ_KNOWN_LIST] = {"01:123456": {}}
    broker.options[CONF_RAMSES_RF] = {"enforce_known_list": True}

    # Helper to construct a packet where ID matches [11:20]
    # Indices 0-10 (11 chars) are padding.
    # [11:20] is 9 chars -> "01:123456"
    padding = " " * 11
    valid_packet = f"{padding}01:123456" + (" " * 20)
    unknown_packet = f"{padding}99:999999" + (" " * 20)

    mock_storage_data = {
        SZ_CLIENT_STATE: {
            SZ_PACKETS: {
                old_date: valid_packet,  # Too old
                recent_date: valid_packet,  # Good
                (now - timedelta(minutes=1)).isoformat(): unknown_packet,  # Unknown
            }
        }
    }
    broker._store.async_load = AsyncMock(return_value=mock_storage_data)

    await broker.async_setup()

    # Check which packets survived
    call_kwargs = mock_client.start.call_args.kwargs
    packets = call_kwargs["cached_packets"]

    # Verify recent known packet is present
    assert recent_date in packets
    # Verify old packet is gone
    assert old_date not in packets
    # Verify unknown device packet is gone
    # Note: unknown_packet timestamp key was dynamically generated, so we check count
    assert len(packets) == 1

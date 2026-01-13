"""Tests for the storage aspect of RamsesBroker (Persistence)."""

from datetime import datetime as dt
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
    hass.loop = AsyncMock()
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
    broker._create_client = MagicMock()
    broker.client = MagicMock()
    broker.client.start = AsyncMock()

    # 3. Run async_setup
    # This should NOT raise ValueError
    await broker.async_setup()

    # 4. Verify client started
    assert broker.client.start.called

    # 5. Verify only valid packet was passed to start
    call_args = broker.client.start.call_args
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

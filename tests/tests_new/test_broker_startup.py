"""Tests for the ramses_cc broker startup resilience."""

from datetime import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from custom_components.ramses_cc.broker import SZ_CLIENT_STATE, SZ_PACKETS, RamsesBroker
from custom_components.ramses_cc.const import CONF_RAMSES_RF, CONF_SCHEMA
from ramses_tx.schemas import SZ_KNOWN_LIST


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a mock Home Assistant instance.

    :return: A mock Home Assistant object.
    :rtype: MagicMock
    """
    hass = MagicMock()
    hass.loop = AsyncMock()
    return hass


@pytest.fixture
def mock_entry() -> MagicMock:
    """Return a mock ConfigEntry.

    :return: A mock ConfigEntry object.
    :rtype: MagicMock
    """
    entry = MagicMock()
    entry.options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {},
        CONF_RAMSES_RF: {},  # Added missing config key
        "serial_port": "/dev/ttyUSB0",
    }
    entry.async_on_unload = MagicMock()
    return entry


async def test_setup_with_corrupted_storage_dates(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that startup survives invalid date strings in storage.

    :param mock_hass: Mock Home Assistant instance.
    :param mock_entry: Mock ConfigEntry.
    """
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


async def test_setup_fails_gracefully_on_bad_config(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that startup catches client creation errors and logs them.

    :param mock_hass: Mock Home Assistant instance.
    :param mock_entry: Mock ConfigEntry.
    """
    broker = RamsesBroker(mock_hass, mock_entry)
    broker._store.async_load = AsyncMock(return_value={})

    # Force _create_client to raise vol.Invalid (simulation of bad schema)
    broker._create_client = MagicMock(side_effect=vol.Invalid("Invalid config"))

    # Verify it raises a clean ValueError with helpful message
    with pytest.raises(ValueError, match="Failed to initialise RAMSES client"):
        await broker.async_setup()

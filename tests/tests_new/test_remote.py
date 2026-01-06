"""Tests for the ramses_cc remote platform features.

This module targets command database management, learning, sending,
and fan parameter coordination in remote.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.remote import (
    RamsesRemote,
    RamsesRemoteEntityDescription,
)
from ramses_tx.command import Command

# Constants
REM_ID = "32:111111"
FAN_ID = "30:222222"
VALID_PKT = "RQ --- 32:111111 30:222222 --:------ 22F1 003 000030"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> MagicMock:
    """Return a mock broker.

    :param hass: The Home Assistant instance.
    :return: A mock broker object.
    """
    broker = MagicMock()
    broker.hass = hass
    broker._remotes = {}
    broker._fan_bound_to_remote = {REM_ID: FAN_ID}

    # Mock the semaphore as an async context manager
    broker._sem = MagicMock()
    broker._sem.__aenter__ = AsyncMock()
    broker._sem.__aexit__ = AsyncMock()

    # Methods that are awaited must be AsyncMock
    broker.client.async_send_cmd = AsyncMock()
    broker.async_get_fan_param = AsyncMock()
    broker.async_set_fan_param = AsyncMock()
    broker.async_update = AsyncMock()

    return broker


@pytest.fixture
def mock_remote_device() -> MagicMock:
    """Return a mock HVAC remote device.

    :return: A mock device object.
    """
    device = MagicMock()
    device.id = REM_ID
    device.is_faked = True
    return device


async def test_remote_command_db_management(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test adding and deleting commands manually.

    This targets async_add_command and async_delete_command.
    """
    desc = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_broker, mock_remote_device, desc)

    # 1. Add a command
    await remote.async_add_command("boost", VALID_PKT)
    assert "boost" in remote.extra_state_attributes["commands"]
    assert remote.extra_state_attributes["commands"]["boost"] == VALID_PKT

    # 2. Delete the command
    await remote.async_delete_command("boost")
    assert "boost" not in remote.extra_state_attributes["commands"]


async def test_remote_send_command_logic(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test sending a stored command.

    This targets async_send_command and repeat logic.
    """
    desc = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_broker, mock_remote_device, desc)
    await remote.async_add_command("boost", VALID_PKT)

    # Execute send with 2 repeats
    await remote.async_send_command("boost", num_repeats=2, delay_secs=0.01)

    assert mock_broker.client.async_send_cmd.call_count == 2
    assert mock_broker.async_update.called
    sent_cmd = mock_broker.client.async_send_cmd.call_args[0][0]
    assert isinstance(sent_cmd, Command)


async def test_remote_fan_parameter_services(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test remote-to-fan parameter service coordination.

    This targets async_get_fan_rem_param and async_set_fan_rem_param.
    """
    desc = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_broker, mock_remote_device, desc)

    # 1. Test Get
    await remote.async_get_fan_rem_param(param_id="01")
    mock_broker.async_get_fan_param.assert_called_once()
    args = mock_broker.async_get_fan_param.call_args[0][0]
    assert args["device_id"] == FAN_ID
    assert args["from_id"] == REM_ID

    # 2. Test Set
    await remote.async_set_fan_rem_param(param_id="01", value=20.0)
    mock_broker.async_set_fan_param.assert_called_once()
    args = mock_broker.async_set_fan_param.call_args[0][0]
    assert args["device_id"] == FAN_ID
    assert args["value"] == 20.0


async def test_remote_learn_command_success(
    hass: HomeAssistant, mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test the successful learning of a command via event bus listener.

    This targets the async_learn_command loop and event listeners by patching
    the bus listener registration.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    :param mock_remote_device: The mock remote device fixture.
    """
    remote = RamsesRemote(
        mock_broker, mock_remote_device, RamsesRemoteEntityDescription()
    )
    remote.hass = hass

    # Prepare real data dictionary
    learn_payload = {"src": REM_ID, "code": "22F1", "packet": "learned_pkt_123"}
    mock_event = MagicMock()
    mock_event.data = learn_payload

    # Patch async_listen on the bus instance specifically for this test
    # instead of using patch.object which fails on read-only attributes.
    with patch("homeassistant.core.EventBus.async_listen") as mock_listen:
        # Start learning task
        task = asyncio.create_task(remote.async_learn_command("test_cmd", timeout=1))

        # Give the task a moment to register the listener
        await asyncio.sleep(0.1)

        # Verify and trigger the captured listener/filter
        assert mock_listen.called
        # async_listen call: (event_type, listener, event_filter)
        _, listener, event_filter = mock_listen.call_args[0]

        # Simulate a bus event matching the criteria
        if event_filter(mock_event):
            listener(mock_event)

        await task

    # Verify command was captured as a string
    assert remote._commands.get("test_cmd") == "learned_pkt_123"

"""Tests for the ramses_cc remote platform features.

This module targets command database management, learning, sending,
and fan parameter coordination in remote.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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

    # Prepare the payload dictionary
    learn_payload = {"src": REM_ID, "code": "22F1", "packet": "learned_pkt_123"}

    # Patch async_listen on the bus instance to intercept listener registration
    with patch("homeassistant.core.EventBus.async_listen") as mock_listen:
        task = asyncio.create_task(remote.async_learn_command("test_cmd", timeout=1))

        # Allow the task to register the listener
        await asyncio.sleep(0.1)

        assert mock_listen.called
        # Retrieve the registered listener and filter from the call args
        # async_listen signature: (event_type, listener, event_filter)
        _, listener, event_filter = mock_listen.call_args[0]

        # Simulate a bus event: the filter accepts a dict, the listener expects an Event
        if event_filter(learn_payload):
            mock_event = MagicMock()
            mock_event.data = learn_payload
            listener(mock_event)

        await task

    # Verify command was captured as a string
    assert remote._commands.get("test_cmd") == "learned_pkt_123"


async def test_remote_learn_filter_logic(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Thoroughly test the event_filter logic for various packet scenarios.

    This ensures the filter only allows specific HVAC codes from the correct source.
    """
    remote = RamsesRemote(
        mock_broker, mock_remote_device, RamsesRemoteEntityDescription()
    )

    # We use a patch to capture the event_filter function from inside async_learn_command
    with patch("homeassistant.core.EventBus.async_listen") as mock_listen:
        # Start learning task briefly to register the listener
        task = asyncio.create_task(remote.async_learn_command("test_cmd", timeout=1))
        await asyncio.sleep(0.1)

        # Capture the filter from the async_listen call
        _, _, event_filter = mock_listen.call_args[0]

        # 1. Valid packet (HVAC code 22F1 from correct source)
        valid_data = {"src": REM_ID, "code": "22F1"}
        assert event_filter(valid_data) is True

        # 2. Invalid Source
        wrong_src = {"src": "99:999999", "code": "22F1"}
        assert event_filter(wrong_src) is False

        # 3. Invalid Code (e.g., a temperature code 30C9)
        wrong_code = {"src": REM_ID, "code": "30C9"}
        assert event_filter(wrong_code) is False

        # Clean up the task using contextlib.suppress to ignore the CancelledError
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_fan_param_methods(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test fan parameter methods for bound and unbound scenarios (PR 1 verification)."""
    entity_id = "remote.test_remote"
    device_id = "12:123456"
    fan_id = "18:654321"

    # 1. Setup Mock Broker first
    mock_broker = MagicMock()
    mock_broker._fan_bound_to_remote = {device_id: fan_id}

    mock_broker.async_get_fan_param = AsyncMock()
    mock_broker.async_set_fan_param = AsyncMock()
    mock_broker.get_all_fan_params = MagicMock()

    # 2. Setup Remote Entity
    mock_device = MagicMock()
    mock_device.id = device_id
    mock_device.unique_id = "unique_id"

    remote = RamsesRemote(
        mock_broker,
        mock_device,
        MagicMock(),
    )
    remote.entity_id = entity_id
    remote.hass = hass

    kwargs = {"key": "value"}

    # --- Test 1: Async Get ---
    await remote.async_get_fan_rem_param(**kwargs)
    mock_broker.async_get_fan_param.assert_awaited()
    call_args = mock_broker.async_get_fan_param.call_args[0][0]
    assert call_args["device_id"] == fan_id

    # --- Test 2: Async Set ---
    await remote.async_set_fan_rem_param(**kwargs)
    mock_broker.async_set_fan_param.assert_awaited()

    # --- Test 3: Update Params  ---
    # Create a completed Future to simulate the return value of async_add_executor_job
    future: asyncio.Future[None] = asyncio.Future()
    future.set_result(None)

    with patch.object(hass, "async_add_executor_job", return_value=future) as mock_exec:
        await remote.async_update_fan_rem_params(**kwargs)

        # VERIFICATION: Check that async_add_executor_job was called with the sync method
        expected_kwargs = {"key": "value", "device_id": fan_id, "from_id": device_id}
        mock_exec.assert_called_with(mock_broker.get_all_fan_params, expected_kwargs)

    # --- Test 4: Unbound Scenarios ---
    mock_broker._fan_bound_to_remote = {}
    mock_broker.get_all_fan_params.reset_mock()

    with caplog.at_level(logging.WARNING):
        await remote.async_update_fan_rem_params(**kwargs)

    assert f"REM {device_id} not bound to a FAN" in caplog.text
    mock_broker.get_all_fan_params.assert_not_called()

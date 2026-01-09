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
from ramses_tx.const import Priority

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
    """Test sending a stored command using native QoS arguments.

    This verifies that async_send_command delegates repeats to the client
    rather than looping manually.
    """
    desc = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_broker, mock_remote_device, desc)
    await remote.async_add_command("boost", VALID_PKT)

    # Execute send with 2 repeats and specific delay
    # Note: 2 repeats means send once + repeat twice = 3 transmissions total in ramses_rf logic?
    # Actually, num_repeats in async_send_cmd usually means "extra" sends.
    # But here we just verify the arguments are passed through.
    await remote.async_send_command("boost", num_repeats=2, delay_secs=0.05)

    # Expectation: Called ONCE, with QoS parameters passed in kwargs
    assert mock_broker.client.async_send_cmd.call_count == 1

    call_args = mock_broker.client.async_send_cmd.call_args
    sent_cmd = call_args[0][0]
    kwargs = call_args[1]

    assert isinstance(sent_cmd, Command)
    assert kwargs["priority"] == Priority.HIGH
    assert kwargs["num_repeats"] == 2
    assert kwargs["gap_duration"] == 0.05  # delay_secs mapped to gap_duration

    assert mock_broker.async_update.called


async def test_remote_send_command_exception_handling(
    caplog: pytest.LogCaptureFixture,
    mock_broker: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test that exceptions during send do not bubble up and stop execution.

    This ensures that even if ramses_rf raises a TimeoutError, async_update
    is still called and the automation flow isn't aborted.
    """
    desc = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_broker, mock_remote_device, desc)
    await remote.async_add_command("boost", VALID_PKT)

    # Simulate a TimeoutError from the underlying client
    mock_broker.client.async_send_cmd.side_effect = TimeoutError("Simulated Timeout")

    # Capture logs to verify the warning
    with caplog.at_level(logging.WARNING):
        # This should NOT raise an exception
        await remote.async_send_command("boost")

    # Verify the exception was logged
    assert "Error sending command" in caplog.text
    assert "Simulated Timeout" in caplog.text

    # Verify async_update was still called
    mock_broker.async_update.assert_called_once()


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
    """Test the successful learning of a command via event bus listener."""
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
        _, listener, event_filter = mock_listen.call_args[0]

        # Simulate a bus event
        if event_filter(learn_payload):
            mock_event = MagicMock()
            mock_event.data = learn_payload
            listener(mock_event)

        await task

    # Verify command was captured
    assert remote._commands.get("test_cmd") == "learned_pkt_123"


async def test_remote_learn_filter_logic(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Thoroughly test the event_filter logic for various packet scenarios."""
    remote = RamsesRemote(
        mock_broker, mock_remote_device, RamsesRemoteEntityDescription()
    )

    with patch("homeassistant.core.EventBus.async_listen") as mock_listen:
        task = asyncio.create_task(remote.async_learn_command("test_cmd", timeout=1))
        await asyncio.sleep(0.1)

        _, _, event_filter = mock_listen.call_args[0]

        # 1. Valid packet
        valid_data = {"src": REM_ID, "code": "22F1"}
        assert event_filter(valid_data) is True

        # 2. Invalid Source
        wrong_src = {"src": "99:999999", "code": "22F1"}
        assert event_filter(wrong_src) is False

        # 3. Invalid Code
        wrong_code = {"src": REM_ID, "code": "30C9"}
        assert event_filter(wrong_code) is False

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_remote_learn_cleanup_on_timeout(
    hass: HomeAssistant, mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test that the event listener is removed even if learning times out.

    This targets the cleanup logic (try...finally) in async_learn_command.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    :param mock_remote_device: The mock remote device fixture.
    """
    remote = RamsesRemote(
        mock_broker, mock_remote_device, RamsesRemoteEntityDescription()
    )
    remote.hass = hass

    # Mock the unsubscribe callback returned by async_listen
    mock_unsubscribe = MagicMock()

    with patch(
        "homeassistant.core.EventBus.async_listen", return_value=mock_unsubscribe
    ):
        # Run learn command with a very short timeout
        await remote.async_learn_command("timeout_cmd", timeout=0.01)

    # Assert that the unsubscribe callback was called
    mock_unsubscribe.assert_called_once()

    # Verify that the command was NOT added
    assert "timeout_cmd" not in remote._commands

    # Verify broker state was reset
    assert mock_broker.learn_device_id is None


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

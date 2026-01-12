"""Tests for the ramses_cc remote platform."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.remote import (
    RamsesRemote,
    RamsesRemoteEntityDescription,
    async_setup_entry,
)
from ramses_tx.command import Command
from ramses_tx.const import Priority

# Constants for testing
REMOTE_ID = "30:123456"
MOCK_DEV_ID = "12:123456"
# Valid packet string for ramses_tx validation
VALID_PKT = "RQ --- 30:123456 18:111111 --:------ 22F1 003 000030"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> MagicMock:
    """Return a mock broker with required internal structures."""
    broker = MagicMock()
    broker._remotes = {REMOTE_ID: {"boost": VALID_PKT}}
    broker._fan_bound_to_remote = {REMOTE_ID: "18:654321"}

    # Mock semaphore to support sync 'with' block as used in remote.py
    mock_sem = MagicMock()
    mock_sem.__enter__ = MagicMock(return_value=None)
    mock_sem.__exit__ = MagicMock(return_value=None)
    broker._sem = mock_sem

    broker.client = MagicMock()
    broker.client.async_send_cmd = AsyncMock()
    broker.async_update = AsyncMock()

    # Async methods for fan params
    broker.async_get_fan_param = AsyncMock()
    broker.async_set_fan_param = AsyncMock()
    broker.get_all_fan_params = MagicMock()

    return broker


@pytest.fixture
def mock_remote_device() -> MagicMock:
    """Return a mock HvacRemote device."""
    device = MagicMock()
    device.id = REMOTE_ID
    device.is_faked = True
    return device


@pytest.fixture
def remote_entity(
    hass: HomeAssistant, mock_broker: MagicMock, mock_remote_device: MagicMock
) -> RamsesRemote:
    """Return a RamsesRemote entity."""
    desc = RamsesRemoteEntityDescription()
    entity = RamsesRemote(mock_broker, mock_remote_device, desc)
    entity.hass = hass
    return entity


async def test_async_setup_entry(hass: HomeAssistant, mock_broker: MagicMock) -> None:
    """Test the setup entry logic."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data[DOMAIN] = {entry.entry_id: mock_broker}

    async_add_entities = MagicMock()

    with patch(
        "custom_components.ramses_cc.remote.async_get_current_platform",
        return_value=MagicMock(),
    ):
        await async_setup_entry(hass, entry, async_add_entities)

    assert mock_broker.async_register_platform.called
    call_args = mock_broker.async_register_platform.call_args
    # Handle both positional and keyword arguments for the callback
    add_devices_cb = (
        call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["add_devices"]
    )

    # Trigger entity creation logic
    mock_device = MagicMock()
    mock_device.id = "30:999999"
    add_devices_cb([mock_device])
    assert async_add_entities.called


async def test_remote_entity_unique_id(
    mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test the RamsesRemote unique ID logic."""
    description = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_broker, mock_remote_device, description)

    assert remote.unique_id == REMOTE_ID
    assert REMOTE_ID in remote.unique_id


async def test_remote_validation_errors(remote_entity: RamsesRemote) -> None:
    """Test TypeError branches for command handling."""
    with pytest.raises(TypeError, match="exactly one command to learn"):
        await remote_entity.async_learn_command(["c1", "c2"])

    with pytest.raises(TypeError, match="exactly one command to send"):
        await remote_entity.async_send_command(["c1", "c2"])

    with pytest.raises(TypeError, match="exactly one command to add"):
        await remote_entity.async_add_command(["c1", "c2"], VALID_PKT)


async def test_kwargs_assertions(remote_entity: RamsesRemote) -> None:
    """Test that unexpected kwargs raise AssertionError (covering assert not kwargs)."""
    # async_delete_command
    with pytest.raises(AssertionError):
        await remote_entity.async_delete_command("cmd", unexpected_arg=True)

    # async_learn_command
    with pytest.raises(AssertionError):
        await remote_entity.async_learn_command("cmd", timeout=1, unexpected_arg=True)

    # async_send_command
    with pytest.raises(AssertionError):
        await remote_entity.async_send_command("boost", unexpected_arg=True)

    # async_add_command
    with pytest.raises(AssertionError):
        await remote_entity.async_add_command("cmd", VALID_PKT, unexpected_arg=True)


async def test_remote_send_command_exceptions(remote_entity: RamsesRemote) -> None:
    """Test exception branches in async_send_command."""
    # hold_secs is not supported
    with pytest.raises(TypeError, match="hold_secs is not supported"):
        await remote_entity.async_send_command("boost", hold_secs=1)

    # command not known
    with pytest.raises(LookupError, match="command 'unknown' is not known"):
        await remote_entity.async_send_command("unknown")

    # device not configured for faking
    remote_entity._device.is_faked = False
    with pytest.raises(TypeError, match="is not configured for faking"):
        await remote_entity.async_send_command("boost")
    remote_entity._device.is_faked = True


async def test_remote_add_command(remote_entity: RamsesRemote) -> None:
    """Test async_add_command logic."""
    # Invalid packet string raises ValueError
    # Fix SIM117: Combined nested with statements
    with (
        patch(
            "custom_components.ramses_cc.remote.Command",
            side_effect=Exception("Bad Pkt"),
        ),
        pytest.raises(ValueError, match="packet_string invalid"),
    ):
        await remote_entity.async_add_command("new_cmd", "INVALID_PKT")

    # Success case
    with patch("custom_components.ramses_cc.remote.Command"):
        # Add new command
        await remote_entity.async_add_command("new_cmd", VALID_PKT)
        assert remote_entity._commands["new_cmd"] == VALID_PKT

        # Overwrite existing
        await remote_entity.async_add_command("new_cmd", "PKT_2")
        assert remote_entity._commands["new_cmd"] == "PKT_2"


async def test_remote_send_command_logic(
    remote_entity: RamsesRemote, mock_broker: MagicMock
) -> None:
    """Test send loop, delay, and broker calls."""
    with patch("asyncio.sleep", AsyncMock()):
        await remote_entity.async_send_command("boost", num_repeats=2, delay_secs=0.5)

    # Expectation: Called ONCE, with QoS parameters passed in kwargs
    # The broker client is responsible for the repeats, not the remote entity loop
    assert mock_broker.client.async_send_cmd.call_count == 1

    call_args = mock_broker.client.async_send_cmd.call_args
    sent_cmd = call_args[0][0]
    kwargs = call_args[1]

    assert isinstance(sent_cmd, Command)
    assert kwargs["priority"] == Priority.HIGH
    assert kwargs["num_repeats"] == 2
    assert kwargs["gap_duration"] == 0.5  # delay_secs mapped 1-to-1 to gap_duration

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


async def test_remote_learn_command_success(
    remote_entity: RamsesRemote,
    hass: HomeAssistant,
    mock_broker: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test the successful learning of a command via event bus listener."""
    remote = RamsesRemote(
        mock_broker, mock_remote_device, RamsesRemoteEntityDescription()
    )
    remote.hass = hass

    # Prepare the payload dictionary
    # Assuming REM_ID is intended to be REMOTE_ID based on the fixture logic
    learn_payload = {
        "src": mock_remote_device.id,
        "code": "22F1",
        "packet": "learned_pkt_123",
    }

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
    mock_broker: MagicMock, mock_remote_device: MagicMock, hass: HomeAssistant
) -> None:
    """Thoroughly test the event_filter logic for various packet scenarios."""
    remote = RamsesRemote(
        mock_broker, mock_remote_device, RamsesRemoteEntityDescription()
    )
    remote.hass = hass

    with patch("homeassistant.core.EventBus.async_listen") as mock_listen:
        task = asyncio.create_task(remote.async_learn_command("test_cmd", timeout=1))
        await asyncio.sleep(0.1)

        _, _, event_filter = mock_listen.call_args[0]

        # 1. Valid packet (HVAC code 22F1 from correct source)
        valid_data = {"src": mock_remote_device.id, "code": "22F1"}
        assert event_filter(valid_data) is True

        # 2. Invalid Source
        wrong_src = {"src": "99:999999", "code": "22F1"}
        assert event_filter(wrong_src) is False

        # 3. Invalid Code (e.g., a temperature code 30C9)
        wrong_code = {"src": mock_remote_device.id, "code": "30C9"}
        assert event_filter(wrong_code) is False

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_remote_services(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test remote services (turn_on, turn_off, send_command)."""
    entity_id = "remote.test_device"

    # Create a mock remote entity
    remote = RamsesRemote(
        MagicMock(id=MOCK_DEV_ID),
        MagicMock(unique_id="unique_id"),
        MagicMock(),
    )
    remote.entity_id = entity_id
    remote.hass = hass

    # Setup the broker mock structure for awaitable calls
    remote._broker = MagicMock()
    remote._broker.client.async_send_cmd = AsyncMock()
    remote._broker.async_update = AsyncMock()

    # Mock the internal send_command method provided by the backend library
    remote._commands = {"cmd_1": "mock_hex_packet"}

    # Test turn_on
    with caplog.at_level(logging.DEBUG):
        await remote.async_turn_on()
        assert "Turning on REM device" in caplog.text

    # Test turn_off
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await remote.async_turn_off()
        assert "Turning off REM device" in caplog.text

    with patch("custom_components.ramses_cc.remote.Command", side_effect=lambda x: x):
        # Test send_command with simple valid command
        await remote.async_send_command(["cmd_1"], num_repeats=1, delay_secs=0)

    remote._broker.client.async_send_cmd.assert_awaited()


async def test_send_command_edge_cases(hass: HomeAssistant) -> None:
    """Test send_command with various parameters and edge cases."""
    remote = RamsesRemote(
        MagicMock(id=MOCK_DEV_ID),
        MagicMock(unique_id="unique_id"),
        MagicMock(),
    )
    remote.entity_id = "remote.test_remote"
    remote.hass = hass

    # Setup the broker mock structure
    remote._broker = MagicMock()
    remote._broker.client.async_send_cmd = AsyncMock()
    remote._broker.async_update = AsyncMock()

    remote._commands = {"cmd_1": "mock_hex_packet"}

    # Case 1: Multiple repeats and delay
    # The implementation passes parameters to the client, it does NOT loop itself.
    with patch("custom_components.ramses_cc.remote.Command", side_effect=lambda x: x):
        await remote.async_send_command(["cmd_1"], num_repeats=2, delay_secs=0.1)

    # Expect exactly 1 call to the broker client
    assert remote._broker.client.async_send_cmd.call_count == 1
    # Verify the arguments passed to that call
    call_kwargs = remote._broker.client.async_send_cmd.call_args[1]
    assert call_kwargs["num_repeats"] == 2
    assert call_kwargs["gap_duration"] == 0.1  # delay_secs mapped 1-to-1


async def test_send_command_failure(hass: HomeAssistant) -> None:
    """Test handling of failures during send_command."""
    remote = RamsesRemote(
        MagicMock(id=MOCK_DEV_ID),
        MagicMock(unique_id="unique_id"),
        MagicMock(),
    )
    remote.entity_id = "remote.test_remote"
    remote.hass = hass

    # Setup the broker mock structure
    remote._broker = MagicMock()
    remote._broker.client.async_send_cmd = AsyncMock()
    remote._broker.async_update = AsyncMock()

    # Mock sending to raise an exception (e.g. timeout or validation error)
    remote._commands = {"cmd_fail": "mock_hex_packet"}
    remote._broker.client.async_send_cmd.side_effect = TimeoutError("Timeout")

    with patch("custom_components.ramses_cc.remote.Command", side_effect=lambda x: x):
        # This should NOT raise an exception (it is caught and logged)
        await remote.async_send_command(["cmd_fail"])

    # Ensure async_update is still called even after failure
    remote._broker.async_update.assert_awaited()


async def test_learn_command(hass: HomeAssistant) -> None:
    """Test the learn_command service."""
    remote = RamsesRemote(
        MagicMock(id=MOCK_DEV_ID),
        MagicMock(unique_id="unique_id"),
        MagicMock(),
    )
    remote.entity_id = "remote.test_remote"
    # Use a standalone mock for hass to avoid "Event loop is closed" errors
    remote.hass = MagicMock()
    remote._commands = {}
    remote._broker = MagicMock()

    # The implementation likely returns silently on timeout rather than raising.
    # We assert that the command was NOT added to the commands list.
    await remote.async_learn_command(command=["fail_cmd"], timeout=0.001)

    assert "fail_cmd" not in remote._commands


async def test_learn_command_failure(hass: HomeAssistant) -> None:
    """Test the learn_command service failure."""
    remote = RamsesRemote(
        MagicMock(id=MOCK_DEV_ID),
        MagicMock(unique_id="unique_id"),
        MagicMock(),
    )
    remote.entity_id = "remote.test_remote"
    # Use a standalone mock for hass
    remote.hass = MagicMock()
    remote._commands = {}
    remote._broker = MagicMock()

    # The implementation returns silently on timeout.
    # We assert that the command was NOT added to the commands list.
    await remote.async_learn_command(command=["fail_cmd"], timeout=0.001)

    assert "fail_cmd" not in remote._commands


async def test_setup_entry_platform(hass: HomeAssistant) -> None:
    """Test platform setup."""
    mock_broker = MagicMock()
    mock_broker.devices = []

    # Create a mock config entry with an ID
    entry = MagicMock()
    entry.entry_id = "test_entry_id"

    # Populate hass.data with the broker
    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][entry.entry_id] = mock_broker

    with (
        patch("custom_components.ramses_cc.remote.RamsesRemote", autospec=True),
        patch(
            "custom_components.ramses_cc.remote.async_get_current_platform",
            return_value=MagicMock(entities={}),
        ),
    ):
        from custom_components.ramses_cc.remote import async_setup_entry

        # Setup with no devices
        await async_setup_entry(hass, entry, MagicMock())


def test_extra_state_attributes(remote_entity: RamsesRemote) -> None:
    """Test that extra state attributes include the command list."""
    # Populate commands
    remote_entity._commands = {"cmd1": "pkt1", "cmd2": "pkt2"}

    attrs = remote_entity.extra_state_attributes

    # Assert 'commands' is merged into attributes
    assert "commands" in attrs
    assert attrs["commands"] == {"cmd1": "pkt1", "cmd2": "pkt2"}


async def test_learn_command_overwrite(
    remote_entity: RamsesRemote, hass: HomeAssistant
) -> None:
    """Test that an existing command is deleted before learning."""
    remote_entity.hass = hass

    # Pre-populate a command to trigger the delete logic
    remote_entity._commands = {"test_cmd": "old_packet"}

    # We use a patch to verify async_delete_command is called.
    with (
        patch.object(
            remote_entity,
            "async_delete_command",
            wraps=remote_entity.async_delete_command,
        ) as mock_delete,
        patch("homeassistant.core.EventBus.async_listen"),
    ):
        # Create a task for learning
        task = asyncio.create_task(
            remote_entity.async_learn_command("test_cmd", timeout=1)
        )

        # Yield to event loop to let the task start and reach the delete call
        await asyncio.sleep(0.001)

        # Cancel task as we only care about the pre-check
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Verify async_delete_command was called with the command list
        mock_delete.assert_awaited_with(["test_cmd"])


async def test_fan_param_methods(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test fan parameter methods for bound and unbound scenarios.

    This test consolidates bound, unbound, set, get, and update verifications,
    including the recent thread-safety fix for async_update_fan_rem_params.
    """
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

    # --- Test 1: Async Get (Bound) ---
    await remote.async_get_fan_rem_param(**kwargs)
    mock_broker.async_get_fan_param.assert_awaited()
    call_args = mock_broker.async_get_fan_param.call_args[0][0]
    assert call_args["device_id"] == fan_id

    # --- Test 2: Async Set (Bound) ---
    await remote.async_set_fan_rem_param(**kwargs)
    mock_broker.async_set_fan_param.assert_awaited()

    # --- Test 3: Update Params (Bound + Thread Safety Check) ---
    # Create a completed Future to simulate the return value of async_add_executor_job
    # if it were improperly used. We assert that it is NOT used.
    future: asyncio.Future[None] = asyncio.Future()
    future.set_result(None)

    with patch.object(hass, "async_add_executor_job", return_value=future) as mock_exec:
        await remote.async_update_fan_rem_params(**kwargs)

        # VERIFICATION: Ensure async_add_executor_job was NOT called.
        # The method should now run directly on the event loop.
        mock_exec.assert_not_called()

        # Check that broker.get_all_fan_params was called directly
        expected_kwargs = {"key": "value", "device_id": fan_id, "from_id": device_id}
        mock_broker.get_all_fan_params.assert_called_with(expected_kwargs)

    # --- Test 4: Unbound Scenarios ---
    mock_broker._fan_bound_to_remote = {}
    mock_broker.get_all_fan_params.reset_mock()
    mock_broker.async_get_fan_param.reset_mock()
    mock_broker.async_set_fan_param.reset_mock()

    with caplog.at_level(logging.WARNING):
        # Update
        await remote.async_update_fan_rem_params(**kwargs)
        assert f"REM {device_id} not bound to a FAN" in caplog.text

        # Get
        await remote.async_get_fan_rem_param(**kwargs)
        # Set
        await remote.async_set_fan_rem_param(**kwargs)

    # Verify broker methods were NOT called for unbound remote
    mock_broker.get_all_fan_params.assert_not_called()
    mock_broker.async_get_fan_param.assert_not_called()
    mock_broker.async_set_fan_param.assert_not_called()


async def test_remote_learn_cleanup_on_timeout(
    hass: HomeAssistant, mock_broker: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test that the event listener is removed even if learning times out."""
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

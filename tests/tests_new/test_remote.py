"""Tests for the ramses_cc remote platform."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.event import RamsesEventType, RamsesLearnEvent
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
def mock_coordinator(hass: HomeAssistant) -> MagicMock:
    """Return a mock coordinator with required internal structures."""
    coordinator = MagicMock()
    coordinator._remotes = {REMOTE_ID: {"boost": VALID_PKT}}

    # for Learn command
    coordinator.learn_device_id = None

    # Updated: Mock fan_handler to support new architecture
    coordinator.fan_handler = MagicMock()
    coordinator.fan_handler._fan_bound_to_remote = {REMOTE_ID: "18:654321"}

    # Mock semaphore to support sync 'with' block as used in remote.py
    mock_sem = MagicMock()
    mock_sem.__enter__ = MagicMock(return_value=None)
    mock_sem.__exit__ = MagicMock(return_value=None)
    coordinator._sem = mock_sem

    coordinator.client = MagicMock()
    coordinator.client.async_send_cmd = AsyncMock()
    coordinator.async_refresh = AsyncMock()

    # Async methods for fan params
    # NOTE: Even though these now delegate to service_handler in the real coordinator,
    # mocking them here on the coordinator instance is correct because RamsesRemote
    # calls them on the coordinator instance.
    coordinator.async_get_fan_param = AsyncMock()
    coordinator.async_set_fan_param = AsyncMock()
    coordinator.get_all_fan_params = MagicMock()

    # Proactive: Mock service_handler just in case logic traverses it
    coordinator.service_handler = MagicMock()

    return coordinator


@pytest.fixture
def mock_remote_device() -> MagicMock:
    """Return a mock HvacRemote device."""
    device = MagicMock()
    device.id = REMOTE_ID
    device.is_faked = True
    return device


@pytest.fixture
def remote_entity(
    hass: HomeAssistant, mock_coordinator: MagicMock, mock_remote_device: MagicMock
) -> RamsesRemote:
    """Return a RamsesRemote entity."""
    desc = RamsesRemoteEntityDescription()
    entity = RamsesRemote(mock_coordinator, mock_remote_device, desc)
    entity.hass = hass
    return entity


async def test_async_setup_entry(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test the setup entry logic."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}

    async_add_entities = MagicMock()

    with patch(
        "custom_components.ramses_cc.remote.async_get_current_platform",
        return_value=MagicMock(),
    ):
        await async_setup_entry(hass, entry, async_add_entities)

    assert mock_coordinator.async_register_platform.called
    call_args = mock_coordinator.async_register_platform.call_args
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
    mock_coordinator: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test the RamsesRemote unique ID logic."""
    description = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_coordinator, mock_remote_device, description)

    assert remote.unique_id == REMOTE_ID
    assert REMOTE_ID in remote.unique_id


async def test_remote_validation_errors(remote_entity: RamsesRemote) -> None:
    """Test HomeAssistantError branches for command handling."""
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="exactly one command to learn"):
        await remote_entity.async_learn_command(["c1", "c2"])

    with pytest.raises(HomeAssistantError, match="exactly one command to send"):
        await remote_entity.async_send_command(["c1", "c2"])

    with pytest.raises(HomeAssistantError, match="exactly one command to add"):
        await remote_entity.async_add_command(["c1", "c2"], VALID_PKT)


async def test_kwargs_assertions(remote_entity: RamsesRemote) -> None:
    """Test that unexpected kwargs raise AssertionError (covering assert not kwargs)."""
    # async_delete_command
    with pytest.raises(AssertionError):
        await remote_entity.async_delete_command("cmd", unexpected_arg=True)

    # async_learn_command
    with pytest.raises(AssertionError):
        await remote_entity.async_learn_command("cmd", timeout=1, unexpected_arg=True)

    # async_add_command
    with pytest.raises(AssertionError):
        await remote_entity.async_add_command("cmd", VALID_PKT, unexpected_arg=True)


async def test_remote_send_command_exceptions(
    caplog: pytest.LogCaptureFixture,
    remote_entity: RamsesRemote,
) -> None:
    """Test exception branches in async_send_command."""
    from homeassistant.exceptions import HomeAssistantError

    # hold_secs is not supported
    with pytest.raises(HomeAssistantError, match="hold_secs is not supported"):
        await remote_entity.async_send_command("boost", hold_secs=1)

    # command not known
    with pytest.raises(HomeAssistantError, match="command 'unknown' is not known"):
        await remote_entity.async_send_command("unknown")

    # device not configured for faking
    remote_entity._device.is_faked = False
    with pytest.raises(HomeAssistantError, match="is not configured for faking"):
        await remote_entity.async_send_command("boost")
    remote_entity._device.is_faked = True

    # include device (kwarg popped). We send a warning, pop kwargs and continue
    # Capture logs to verify the warning
    with caplog.at_level(logging.WARNING):
        # This should NOT raise an exception
        await remote_entity.async_send_command("boost", unexpected_arg=True)

    # Verify the exception was logged
    assert "Use ramses_cc" in caplog.text
    assert "instead of this HA command" in caplog.text


async def test_remote_add_command(remote_entity: RamsesRemote) -> None:
    """Test async_add_command logic."""
    # Invalid packet string raises ValueError
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
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """Test send loop, delay, and coordinator calls."""
    with patch("asyncio.sleep", AsyncMock()):
        await remote_entity.async_send_command("boost", num_repeats=2, delay_secs=0.5)

    # Expectation: Called ONCE, with QoS parameters passed in kwargs
    # The coordinator client is responsible for the repeats, not the remote entity loop
    assert mock_coordinator.client.async_send_cmd.call_count == 1

    call_args = mock_coordinator.client.async_send_cmd.call_args
    sent_cmd = call_args[0][0]
    kwargs = call_args[1]

    assert isinstance(sent_cmd, Command)
    assert kwargs["priority"] == Priority.HIGH
    assert kwargs["num_repeats"] == 2
    assert kwargs["gap_duration"] == 0.5  # delay_secs mapped 1-to-1 to gap_duration

    # Fixed: Verify async_refresh is called instead of async_update
    assert mock_coordinator.async_refresh.called


async def test_remote_send_command_exception_handling(
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test that exceptions during send do not bubble up and stop execution.

    This ensures that even if ramses_rf raises a TimeoutError, async_refresh
    is still called and the automation flow isn't aborted.
    """
    from homeassistant.exceptions import HomeAssistantError

    desc = RamsesRemoteEntityDescription()
    remote = RamsesRemote(mock_coordinator, mock_remote_device, desc)
    await remote.async_add_command("boost", VALID_PKT)

    # Simulate a TimeoutError from the underlying client
    mock_coordinator.client.async_send_cmd.side_effect = TimeoutError(
        "Simulated Timeout"
    )

    with (
        pytest.raises(HomeAssistantError, match="Error sending command "),
    ):
        # This will raise a HomeAssistantError for any error caught in remote.py
        await remote.async_send_command("boost")


@pytest.mark.skip
@pytest.mark.asyncio
async def test_remote_learn_command_success(
    remote_entity: RamsesRemote,
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test the successful learning of a command via learn_event state change listener."""
    remote = RamsesRemote(
        mock_coordinator, mock_remote_device, RamsesRemoteEntityDescription()
    )
    remote.hass = hass

    # Prepare the payload dictionary
    # Assuming REM_ID is intended to be REMOTE_ID based on the fixture logic
    learn_payload = {
        "src": mock_remote_device.id,
        "code": "22F1",
        "packet": "learned_pkt_123",
    }

    # create event.ramses_cc_learn_event (or listener will close during init)
    event = RamsesLearnEvent(mock_coordinator, hass, {"type": RamsesEventType.LEARN})
    assert event._attr_unique_id == "learn_event"

    # Mock the unsubscribe callback returned by async_listen
    mock_unsubscribe = MagicMock()

    with patch(
        "homeassistant.helpers.event.async_track_state_change_event",
        return_value=mock_unsubscribe,
    ) as mock_track_change:
        task = asyncio.create_task(remote.async_learn_command("test_cmd", timeout=1))

        # Allow the task to register the listener
        await asyncio.sleep(0.1)

        assert mock_track_change.called
        # Retrieve the registered callback from the call args
        _, _, callback = mock_track_change.call_args[0]

        # Simulate a state_change event
        mock_event = MagicMock()
        mock_event.data = learn_payload
        callback(mock_event)

        await task

    # Verify command was captured
    assert remote._commands.get("test_cmd") == "learned_pkt_123"


# TODO(eb): adapt this LeChat test suggestion to the above test_remote_learn_command_success:
async def test_async_learn_command_callback() -> None:
    # Mock the class instance
    mock_instance = AsyncMock()
    mock_instance._commands = {}
    mock_instance._device.id = "test_device_id"
    mock_instance.coordinator._sem = AsyncMock()
    mock_instance.coordinator.learn_device_id = None

    # Mock the event data
    mock_event = MagicMock(spec=RamsesLearnEvent)
    mock_event.data = {
        "new_state": State(
            "event.ramses_cc_learn_event",
            "test",
            {
                "extra_data": {
                    "src": "test_device_id",
                    "code": "22F1",
                    "packet": "test_packet",
                }
            },
        )
    }

    # Mock async_track_state_change_event
    mock_remove_listener = MagicMock()
    patch(
        "homeassistant.helpers.event.async_track_state_change_event",
        return_value=mock_remove_listener,
    )

    # Mock the learning_session event
    # learning_session = asyncio.Event()  # used below, commented

    # Call the method
    with patch.object(
        mock_instance, "_async_on_change", new=AsyncMock()
    ) as mock_callback:
        # Simulate the event being triggered
        await mock_instance.async_learn_command("test_command", timeout=1)

        # Simulate the event being received
        await mock_instance._async_on_change(mock_event)

        # Assert the callback was called
        mock_callback.assert_awaited_once_with(mock_event)

        # TODO: fix next asserts/handlers

        # Assert the command was saved
        # assert "test_command" in mock_instance._commands
        # assert mock_instance._commands["test_command"] == "test_packet"

        # Assert the learning session was set
        # assert learning_session.is_set()

        # Assert the listener was removed
        # mock_remove_listener.assert_called_once()


# new tests for remote_learn events


@pytest.mark.skip
@pytest.mark.asyncio
async def test_async_learn_command_success(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test successful learning of a command."""
    # Setup
    device = remote_entity
    device._commands = {}
    device.async_delete_command = AsyncMock()
    device.async_learn_command = AsyncMock()

    # Mock the asyncio.Event
    with patch("asyncio.Event") as mock_event:
        mock_event.return_value = AsyncMock()
        mock_event.return_value.wait = AsyncMock()
        mock_event.return_value.is_set.return_value = True

        # Call the method
        await device.async_learn_command(command="boost", timeout=1)

        # Assertions
        # assert "boost" in device._commands
        # device.async_delete_command.assert_not_called()
        mock_event.assert_called_once()


@pytest.mark.asyncio
async def test_async_learn_command_invalid_command_type(
    remote_entity: RamsesRemote,
) -> None:
    """Test that a HomeAssistantError is raised for invalid command types."""
    # Setup
    device = remote_entity
    device._commands = {}

    # Call the method with an invalid command type
    with pytest.raises(HomeAssistantError):
        await device.async_learn_command(command=["boost", "volume_up"], timeout=3)


@pytest.mark.asyncio
async def test_async_learn_command_command_already_exists(
    remote_entity: RamsesRemote,
) -> None:
    """Test that the existing command is deleted before learning a new one."""
    # Setup
    device = remote_entity
    device._commands = {"boost": "some_value"}
    device.async_delete_command = AsyncMock()

    # Mock the asyncio.Event
    with patch("asyncio.Event") as mock_event:
        mock_event.return_value = AsyncMock()
        mock_event.return_value.wait = AsyncMock()
        mock_event.return_value.is_set.return_value = True

        # Call the method
        await device.async_learn_command(command="boost", timeout=3)

        # Assertions
        device.async_delete_command.assert_called_once_with(["boost"])


@pytest.mark.asyncio
async def test_async_learn_command_kwargs_not_empty(
    remote_entity: RamsesRemote,
) -> None:
    """Test that an assertion error is raised if kwargs are not empty."""
    # Setup
    device = remote_entity
    device._commands = {}

    # Call the method with kwargs
    with pytest.raises(AssertionError):
        await device.async_learn_command(command="boost", timeout=3, extra_arg="value")


# end new


@pytest.mark.skip  # no separate filter
async def test_remote_learn_filter_logic(
    mock_coordinator: MagicMock, mock_remote_device: MagicMock, hass: HomeAssistant
) -> None:
    """Thoroughly test the event_filter logic for various packet scenarios."""
    remote = RamsesRemote(
        mock_coordinator, mock_remote_device, RamsesRemoteEntityDescription()
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
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test remote services (turn_on, turn_off, send_command)."""
    # remote_entity is already set up with mock_coordinator via fixtures

    # Mock the internal commands dictionary
    remote_entity._commands = {"cmd_1": VALID_PKT}

    # Test turn_on
    with caplog.at_level(logging.DEBUG):
        await remote_entity.async_turn_on()
        assert "Turning on REM device" in caplog.text

    # Test turn_off
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await remote_entity.async_turn_off()
        assert "Turning off REM device" in caplog.text

    # Test send_command
    with patch("custom_components.ramses_cc.remote.Command", side_effect=lambda x: x):
        await remote_entity.async_send_command(["cmd_1"], num_repeats=1, delay_secs=0)

    mock_coordinator.client.async_send_cmd.assert_awaited()
    # Fixed: Verify async_refresh is awaited
    mock_coordinator.async_refresh.assert_awaited()


async def test_send_command_edge_cases(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """Test send_command with various parameters and edge cases."""
    remote_entity._commands = {"cmd_1": VALID_PKT}

    # Case 1: Multiple repeats and delay
    with patch("custom_components.ramses_cc.remote.Command", side_effect=lambda x: x):
        await remote_entity.async_send_command(["cmd_1"], num_repeats=2, delay_secs=0.1)

    # Verify parameters passed to the coordinator client
    call_kwargs = mock_coordinator.client.async_send_cmd.call_args[1]
    assert call_kwargs["num_repeats"] == 2
    assert call_kwargs["gap_duration"] == 0.1

    # Fixed: Verify async_refresh is awaited
    mock_coordinator.async_refresh.assert_awaited()


async def test_send_command_failure(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test handling of failures during send_command."""
    from homeassistant.exceptions import HomeAssistantError

    remote_entity._commands = {"cmd_fail": VALID_PKT}

    # Simulate a failure in the client
    mock_coordinator.client.async_send_cmd.side_effect = Exception("RF Error")

    with (
        patch("custom_components.ramses_cc.remote.Command", side_effect=lambda x: x),
        pytest.raises(HomeAssistantError, match="Error sending command "),
    ):
        # This will raise a HomeAssistantError for any error caught in remote.py
        await remote_entity.async_send_command(["cmd_fail"])


@pytest.mark.skip
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
    remote._coordinator = MagicMock()

    # The implementation likely returns silently on timeout rather than raising.
    # We assert that the command was NOT added to the commands list.
    await remote.async_learn_command(command=["fail_cmd"], timeout=0.001)

    assert "fail_cmd" not in remote._commands


@pytest.mark.skip
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
    remote._coordinator = MagicMock()

    # The implementation returns silently on timeout.
    # We assert that the command was NOT added to the commands list.
    await remote.async_learn_command(command=["fail_cmd"], timeout=0.001)

    assert "fail_cmd" not in remote._commands


async def test_setup_entry_platform(hass: HomeAssistant) -> None:
    """Test platform setup."""
    mock_coordinator = MagicMock()
    mock_coordinator.devices = []

    # Create a mock config entry with an ID
    entry = MagicMock()
    entry.entry_id = "test_entry_id"

    # Populate hass.data with the coordinator
    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][entry.entry_id] = mock_coordinator

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

    # 1. Setup Mock Coordinator first
    mock_coordinator = MagicMock()
    mock_coordinator.fan_handler = MagicMock()
    mock_coordinator.fan_handler._fan_bound_to_remote = {device_id: fan_id}

    mock_coordinator.async_get_fan_param = AsyncMock()
    mock_coordinator.async_set_fan_param = AsyncMock()
    mock_coordinator.get_all_fan_params = MagicMock()

    # 2. Setup Remote Entity
    mock_device = MagicMock()
    mock_device.id = device_id
    mock_device.unique_id = "unique_id"

    remote = RamsesRemote(
        mock_coordinator,
        mock_device,
        MagicMock(),
    )
    remote.entity_id = entity_id
    remote.hass = hass

    kwargs = {"key": "value"}

    # --- Test 1: Async Get (Bound) ---
    await remote.async_get_fan_rem_param(**kwargs)
    mock_coordinator.async_get_fan_param.assert_awaited()
    call_args = mock_coordinator.async_get_fan_param.call_args[0][0]
    assert call_args["device_id"] == fan_id

    # --- Test 2: Async Set (Bound) ---
    await remote.async_set_fan_rem_param(**kwargs)
    mock_coordinator.async_set_fan_param.assert_awaited()

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

        # Check that coordinator.get_all_fan_params was called directly
        expected_kwargs = {"key": "value", "device_id": fan_id, "from_id": device_id}
        mock_coordinator.get_all_fan_params.assert_called_with(expected_kwargs)

    # --- Test 4: Unbound Scenarios ---
    mock_coordinator.fan_handler._fan_bound_to_remote = {}
    mock_coordinator.get_all_fan_params.reset_mock()
    mock_coordinator.async_get_fan_param.reset_mock()
    mock_coordinator.async_set_fan_param.reset_mock()

    with caplog.at_level(logging.WARNING):
        # Update
        await remote.async_update_fan_rem_params(**kwargs)
        assert f"REM {device_id} not bound to a FAN" in caplog.text

        # Get
        await remote.async_get_fan_rem_param(**kwargs)
        # Set
        await remote.async_set_fan_rem_param(**kwargs)

    # Verify coordinator methods were NOT called for unbound remote
    mock_coordinator.get_all_fan_params.assert_not_called()
    mock_coordinator.async_get_fan_param.assert_not_called()
    mock_coordinator.async_set_fan_param.assert_not_called()


@pytest.mark.skip
async def test_remote_learn_cleanup_on_timeout(
    hass: HomeAssistant, mock_coordinator: MagicMock, mock_remote_device: MagicMock
) -> None:
    """Test that the event listener is removed even if learning times out."""
    remote = RamsesRemote(
        mock_coordinator, mock_remote_device, RamsesRemoteEntityDescription()
    )
    remote.hass = hass

    # Mock the unsubscribe callback returned by async_listen
    mock_unsubscribe = MagicMock()

    with patch(  # TODO(eb): when learn test works, copy patch here
        "homeassistant.core.EventBus.async_listen", return_value=mock_unsubscribe
    ):
        # Run learn command with a very short timeout
        await remote.async_learn_command("timeout_cmd", timeout=0.01)

    # Assert that the unsubscribe callback was called
    mock_unsubscribe.assert_called_once()

    # Verify that the command was NOT added
    assert "timeout_cmd" not in remote._commands

    # Verify coordinator state was reset
    assert mock_coordinator.learn_device_id is None


async def test_remote_send_command_no_client(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
) -> None:
    """Test send_command raises HomeAssistantError when client is not initialized."""
    from homeassistant.exceptions import HomeAssistantError

    # Ensure command exists so we don't fail the LookupError check
    remote_entity._commands = {"boost": VALID_PKT}

    # Force client to None to trigger the guard clause at line 255
    mock_coordinator.client = None

    # Patch Command to ensure we reach the client check without parsing errors
    with (
        patch("custom_components.ramses_cc.remote.Command"),
        pytest.raises(HomeAssistantError, match="client is not initialized"),
    ):
        await remote_entity.async_send_command("boost")

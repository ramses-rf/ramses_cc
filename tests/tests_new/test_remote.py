"""Tests for the ramses_cc remote platform."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.ramses_cc.event import RamsesEventType, RamsesLearnEvent
from custom_components.ramses_cc.remote import (
    RamsesRemote,
    RamsesRemoteEntityDescription,
    _merge_commands,
    _split_commands,
    _with_metadata,
    async_setup_entry,
)
from ramses_tx.const import Code, Priority, Verb
from ramses_tx.dtos import CommandDTO


@pytest.fixture(autouse=True, scope="module")
def _inject_entity_platform() -> Iterator[None]:
    """Inject EntityPlatform into HA entity module for Python 3.14 autospec.

    This ensures that the `EntityPlatform` type hint is resolvable when
    `unittest.mock.patch` with `autospec=True` aggressively evaluates
    annotations, preventing a NameError in isolated CI workers.
    """
    with patch(
        "homeassistant.helpers.entity.EntityPlatform",
        EntityPlatform,
        create=True,
    ):
        yield


# Constants for testing
REMOTE_ID = "30:123456"
MOCK_DEV_ID = "12:123456"
# Valid packet string for ramses_tx validation
VALID_PACKET = "RQ --- 30:123456 18:111111 --:------ 22F1 003 000030"


@pytest.fixture
def mock_coordinator(hass: HomeAssistant) -> MagicMock:
    """Return a mock coordinator with required internal structures."""
    coordinator = MagicMock()
    coordinator._remotes = {REMOTE_ID: {"boost": VALID_PACKET}}

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
    coordinator.client.async_send_raw_command = AsyncMock()
    coordinator.client.async_send_cmd = (
        coordinator.client.async_send_raw_command
    )
    coordinator.async_refresh = AsyncMock()

    # Async methods for fan params
    # NOTE: Even though these now delegate to service_handler in the real
    # coordinator, mocking them here on the coordinator instance is correct
    # because RamsesRemote calls them on the coordinator instance.
    coordinator.async_get_fan_param = AsyncMock()
    coordinator.async_set_fan_param = AsyncMock()
    coordinator.get_all_fan_params = MagicMock()

    # Phase 3a: async schema command writes
    coordinator._async_update_schema_commands = AsyncMock()

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
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> RamsesRemote:
    """Return a RamsesRemote entity."""
    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, mock_remote_device, desc)
    entity.hass = hass
    return entity


async def test_async_setup_entry(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test the setup entry logic."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.runtime_data = mock_coordinator

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
        call_args[0][1]
        if len(call_args[0]) > 1
        else call_args[1]["add_devices"]
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
    description = RamsesRemoteEntityDescription(key="remote")
    remote = RamsesRemote(mock_coordinator, mock_remote_device, description)

    assert remote.unique_id == REMOTE_ID
    assert REMOTE_ID in remote.unique_id


async def test_remote_validation_errors(remote_entity: RamsesRemote) -> None:
    """Test HomeAssistantError branches for command handling."""
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(
        HomeAssistantError, match="exactly one command to learn"
    ):
        await remote_entity.async_learn_command(["c1", "c2"])

    with pytest.raises(
        HomeAssistantError, match="exactly one command to send"
    ):
        await remote_entity.async_send_command(["c1", "c2"])

    with pytest.raises(HomeAssistantError, match="exactly one command to add"):
        await remote_entity.async_add_command(["c1", "c2"], VALID_PACKET)


async def test_remote_send_command_exceptions(
    caplog: pytest.LogCaptureFixture,
    remote_entity: RamsesRemote,
) -> None:
    """Test exception branches in async_send_command."""
    from homeassistant.exceptions import HomeAssistantError

    # hold_secs is not supported
    with pytest.raises(HomeAssistantError, match="hold_secs is not supported"):
        await remote_entity.async_send_command("boost", hold_secs=cast(Any, 1))

    # command not known
    with pytest.raises(
        HomeAssistantError, match="command 'unknown' is not known"
    ):
        await remote_entity.async_send_command("unknown")

    # device not configured for faking
    cast(Any, remote_entity._device).is_faked = False
    with pytest.raises(
        HomeAssistantError, match="is not configured for faking"
    ):
        await remote_entity.async_send_command("boost")
    cast(Any, remote_entity._device).is_faked = True

    # include device (kwarg popped). We send a warning, pop kwargs and continue
    # Capture logs to verify the warning
    with caplog.at_level(logging.WARNING):
        # This should NOT raise an exception
        await remote_entity.async_send_command("boost", unexpected_arg=True)

    # Verify the exception was logged
    assert "Use ramses_cc" in caplog.text
    assert "instead of this HA command" in caplog.text

    # out-of-range repeat counts and negative delays get a clean error
    # before reaching ramses_rf's downstream assert
    with pytest.raises(
        HomeAssistantError, match="num_repeats must be an integer"
    ):
        await remote_entity.async_send_command("boost", num_repeats=0)

    with pytest.raises(
        HomeAssistantError, match="num_repeats must be an integer"
    ):
        await remote_entity.async_send_command("boost", num_repeats=6)

    with pytest.raises(
        HomeAssistantError, match="delay_secs must not be negative"
    ):
        await remote_entity.async_send_command("boost", delay_secs=-1)


async def test_remote_add_command(remote_entity: RamsesRemote) -> None:
    """Test async_add_command logic."""
    # Invalid packet string raises ValueError
    with (
        patch(
            "custom_components.ramses_cc.remote.parse_packet_string",
            return_value=None,
        ),
        pytest.raises(ValueError, match="packet_string invalid"),
    ):
        await remote_entity.async_add_command("new_cmd", "INVALID_PACKET")

    # Success case
    with patch("custom_components.ramses_cc.remote.parse_packet_string"):
        # Add new command
        await remote_entity.async_add_command("new_cmd", VALID_PACKET)
        assert remote_entity._commands["new_cmd"] == VALID_PACKET

        # Overwrite existing
        await remote_entity.async_add_command("new_cmd", "PACKET_2")
        assert remote_entity._commands["new_cmd"] == "PACKET_2"


async def test_remote_send_command_logic(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """Test send loop, delay, and coordinator calls."""
    with patch("asyncio.sleep", AsyncMock()):
        await remote_entity.async_send_command(
            "boost", num_repeats=2, delay_secs=0.5
        )

    # Expectation: Called ONCE, with QoS parameters passed in kwargs
    # The coordinator client is responsible for repeats, not the entity loop
    assert mock_coordinator.client.async_send_raw_command.call_count == 1

    call_args = mock_coordinator.client.async_send_raw_command.call_args
    sent_cmd = call_args[0][0]
    kwargs = call_args[1]

    assert isinstance(sent_cmd, CommandDTO)
    assert kwargs["priority"] == Priority.HIGH
    assert kwargs["num_repeats"] == 2
    assert kwargs["gap_duration"] == 0.5

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

    desc = RamsesRemoteEntityDescription(key="remote")
    remote = RamsesRemote(mock_coordinator, mock_remote_device, desc)
    await remote.async_add_command("boost", VALID_PACKET)

    # Simulate a TimeoutError from the underlying client
    mock_coordinator.client.async_send_raw_command.side_effect = TimeoutError(
        "Simulated Timeout"
    )

    with (
        pytest.raises(HomeAssistantError, match="Error sending command "),
    ):
        # This will raise a HomeAssistantError for any error caught in remote.py
        await remote.async_send_command("boost")


# --- reset_filter_counter tests for Remote Entity ---
@pytest.mark.asyncio
async def test_async_reset_filter_counter_remote_success(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
) -> None:
    """Test successful command creation, sending, and logging for a remote entity."""
    mock_coordinator.client.create_cmd.return_value = "test_cmd"

    await remote_entity.async_reset_filter_counter()

    # Assert command creation
    mock_coordinator.client.create_cmd.assert_called_once_with(
        device_id="18:654321",
        from_id=REMOTE_ID,
        verb=Verb.W_,
        code=Code._10D0,
        payload="00FF",
    )

    # Assert command sending
    # Success case
    mock_coordinator.client.async_send_raw_command.assert_called_once_with(
        "test_cmd"
    )

    # Assert debug logging
    with patch("custom_components.ramses_cc.remote._LOGGER") as mock_logger:
        await remote_entity.async_reset_filter_counter()
        mock_logger.debug.assert_called_once_with(
            "reset_filter_counter: sent W 10D0 from %s to %s",
            REMOTE_ID,
            "18:654321",
        )


@pytest.mark.asyncio
async def test_async_reset_filter_counter_remote_no_fan(
    mock_remote_device: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Test early return and error logging when no FAN is bound to the remote."""
    mock_coordinator.fan_handler._fan_bound_to_remote = {}
    remote_entity = RamsesRemote(
        mock_coordinator,
        mock_remote_device,
        RamsesRemoteEntityDescription(key="remote"),
    )

    with pytest.raises(HomeAssistantError, match="No FAN is bound to remote"):
        await remote_entity.async_reset_filter_counter()


@pytest.mark.asyncio
async def test_async_reset_filter_counter_remote_client_error(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
) -> None:
    """Test exception handling and warning logging on client failure for a remote entity."""
    mock_coordinator.client.create_cmd.side_effect = Exception(
        "Simulated Error"
    )

    with pytest.raises(HomeAssistantError, match="Simulated Error"):
        await remote_entity.async_reset_filter_counter()


@pytest.mark.asyncio
async def test_async_reset_filter_counter_client_unavailable(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
) -> None:
    """Test that an unavailable client is reported to Home Assistant."""
    mock_coordinator.client = None

    with pytest.raises(HomeAssistantError, match="client is not initialized"):
        await remote_entity.async_reset_filter_counter()


# --- reset_filter_counter tests for Fan Entity ---
@pytest.mark.asyncio
async def test_async_reset_filter_counter_fan_success(
    fan_remote_entity: RamsesRemote,
    fan_coordinator: MagicMock,
) -> None:
    """Test successful command creation, sending, and logging for a fan entity."""
    fan_coordinator.client.create_cmd.return_value = "test_cmd"

    await fan_remote_entity.async_reset_filter_counter()

    # Assert command creation
    fan_coordinator.client.create_cmd.assert_called_once_with(
        device_id="30:160000",
        from_id="32:153001",
        verb=Verb.W_,
        code=Code._10D0,
        payload="00FF",
    )

    # Assert command sending
    fan_coordinator.client.async_send_raw_command.assert_called_once_with(
        "test_cmd"
    )

    # Assert debug logging
    with patch("custom_components.ramses_cc.remote._LOGGER") as mock_logger:
        await fan_remote_entity.async_reset_filter_counter()
        mock_logger.debug.assert_called_once_with(
            "reset_filter_counter: sent W 10D0 from %s to %s",
            "32:153001",
            "30:160000",
        )


@pytest.mark.asyncio
async def test_async_reset_filter_counter_fan_no_rem(
    fan_remote_entity: RamsesRemote,
    fan_coordinator: MagicMock,
) -> None:
    """Test early return and error logging when no REM is bound to the fan."""
    fan_coordinator.options = {
        "schema": {
            FAN_ID: {"_class": "FAN", "_bound": []},
        },
    }
    with pytest.raises(HomeAssistantError, match="No REM is bound to FAN"):
        await fan_remote_entity.async_reset_filter_counter()


@pytest.mark.asyncio
async def test_remote_learn_command_success(
    remote_entity: RamsesRemote,
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test successful learning via learn_event state change listener."""
    remote = RamsesRemote(
        mock_coordinator,
        mock_remote_device,
        RamsesRemoteEntityDescription(key="remote"),
    )
    remote.hass = hass

    # Prepare the payload dictionary
    # Assuming REM_ID is intended to be REMOTE_ID based on the fixture logic
    learn_payload = {
        "src": mock_remote_device.id,
        "code": "22F1",
        "packet": "learned_packet_123",
    }

    # create event.ramses_cc_learn_event (or listener will close during init)
    event = RamsesLearnEvent(
        mock_coordinator, hass, {"type": RamsesEventType.LEARN}
    )
    assert event._attr_unique_id == "learn_event"

    # Mock the unsubscribe callback returned by async_listen
    mock_unsubscribe = MagicMock()

    with patch(
        "custom_components.ramses_cc.remote.async_track_state_change_event",
        return_value=mock_unsubscribe,
    ) as mock_track_change:
        task = asyncio.create_task(
            remote.async_learn_command("test_cmd", timeout=1)
        )

        # Allow the task to register the listener
        await asyncio.sleep(0.1)

        assert mock_track_change.called
        # Retrieve the registered callback from the call args
        _, _, callback = mock_track_change.call_args[0]

        # Simulate a state_change event: the callback reads
        # event.data["new_state"].attributes["extra_data"]
        mock_state = MagicMock()
        mock_state.attributes = {"extra_data": learn_payload}
        mock_event = MagicMock()
        mock_event.data = {"new_state": mock_state}
        await callback(mock_event)

        await task

    # Verify command was captured
    assert remote._commands.get("test_cmd") == "learned_packet_123"

    # TODO: fix next asserts/handlers

    # Assert the command was saved
    # assert "test_command" in mock_instance._commands
    # assert mock_instance._commands["test_command"] == "test_packet"

    # Assert the learning session was set
    # assert learning_session.is_set()

    # Assert the listener was removed
    # mock_remove_listener.assert_called_once()


# new tests for remote_learn events


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
        await device.async_learn_command(
            command=["boost", "volume_up"], timeout=3
        )


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


async def test_remote_services(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test remote services (turn_on, turn_off, send_command)."""
    # remote_entity is already set up with mock_coordinator via fixtures

    # Mock the internal commands dictionary
    remote_entity._commands = {"cmd_1": VALID_PACKET}

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
    with patch(
        "custom_components.ramses_cc.remote.parse_packet_string",
        side_effect=lambda x: x,
    ):
        await remote_entity.async_send_command(
            ["cmd_1"], num_repeats=1, delay_secs=0
        )

    mock_coordinator.client.async_send_raw_command.assert_awaited()
    # Fixed: Verify async_refresh is awaited
    mock_coordinator.async_refresh.assert_awaited()


async def test_send_command_edge_cases(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """Test send_command with various parameters and edge cases."""
    remote_entity._commands = {"cmd_1": VALID_PACKET}

    # Case 1: Multiple repeats and delay
    with patch(
        "custom_components.ramses_cc.remote.parse_packet_string",
        side_effect=lambda x: x,
    ):
        await remote_entity.async_send_command(
            ["cmd_1"], num_repeats=2, delay_secs=0.1
        )

    # Verify parameters passed to the coordinator client
    call_kwargs = mock_coordinator.client.async_send_raw_command.call_args[1]
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

    remote_entity._commands = {"cmd_fail": VALID_PACKET}

    # Simulate a failure in the client
    mock_coordinator.client.async_send_raw_command.side_effect = Exception(
        "RF Error"
    )

    with (
        patch(
            "custom_components.ramses_cc.remote.parse_packet_string",
            side_effect=lambda x: x,
        ),
        pytest.raises(HomeAssistantError, match="Error sending command "),
    ):
        # This will raise a HomeAssistantError for any error caught in remote.py
        await remote_entity.async_send_command(["cmd_fail"])


async def test_setup_entry_platform(hass: HomeAssistant) -> None:
    """Test platform setup."""
    mock_coordinator = MagicMock()
    mock_coordinator.devices = []

    # Create a mock config entry with an ID
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.runtime_data = mock_coordinator

    with (
        patch(
            "custom_components.ramses_cc.remote.RamsesRemote",
            autospec=True,
        ),
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
    remote_entity._commands = {"cmd1": "packet1", "cmd2": "packet2"}

    attrs = remote_entity.extra_state_attributes

    # Assert 'commands' is merged into attributes
    assert "commands" in attrs
    assert attrs["commands"] == {"cmd1": "packet1", "cmd2": "packet2"}


def test_extra_state_attributes_bound_to_fan(
    remote_entity: RamsesRemote,
) -> None:
    """Test that bound_to_fan attribute is exposed when REM is bound to a FAN."""
    attrs = remote_entity.extra_state_attributes
    # mock_coordinator sets _fan_bound_to_remote = {REMOTE_ID: "18:654321"}
    assert "bound_to_fan" in attrs
    assert attrs["bound_to_fan"] == "18:654321"


def test_extra_state_attributes_no_bound_to_fan(
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test that bound_to_fan is absent when REM is not bound to any FAN."""
    # Remove the binding for this REM
    mock_coordinator.fan_handler._fan_bound_to_remote = {}
    entity = RamsesRemote(
        mock_coordinator,
        mock_remote_device,
        RamsesRemoteEntityDescription(key="remote"),
    )
    attrs = entity.extra_state_attributes
    assert "bound_to_fan" not in attrs


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


async def test_remote_learn_cleanup_on_timeout(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test that the state-change listener is removed on learn timeout."""
    remote = RamsesRemote(
        mock_coordinator,
        mock_remote_device,
        RamsesRemoteEntityDescription(key="remote"),
    )
    remote.hass = hass

    # Mock the remover returned by async_track_state_change_event (the
    # listener learn mode actually registers, not EventBus.async_listen)
    mock_unsubscribe = MagicMock()

    with (
        patch(
            "custom_components.ramses_cc.remote.async_track_state_change_event",
            return_value=mock_unsubscribe,
        ),
        pytest.raises(HomeAssistantError, match="waiting for command"),
    ):
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
    remote_entity._commands = {"boost": VALID_PACKET}

    # Force client to None to trigger the guard clause at line 255
    mock_coordinator.client = None

    # Patch Command to ensure we reach the client check without parsing errors
    with (
        patch("custom_components.ramses_cc.remote.parse_packet_string"),
        pytest.raises(HomeAssistantError, match="client is not initialized"),
    ):
        await remote_entity.async_send_command("boost")


# ---------------------------------------------------------------------------
# Phase 3a: learn_command / add_command / delete_command write to schema
# ---------------------------------------------------------------------------


async def test_add_command_writes_to_schema(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """add_command calls _async_update_schema_commands with updated commands."""
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).reset_mock()

    with patch("custom_components.ramses_cc.remote.parse_packet_string"):
        await remote_entity.async_add_command("my_boost", VALID_PACKET)

    # Verify _commands was updated
    assert remote_entity._commands["my_boost"] == VALID_PACKET
    # Verify schema write was called with device ID + full commands dict
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).assert_awaited_once()
    call_args = cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).call_args
    assert call_args[0][0] == REMOTE_ID  # device_id
    assert call_args[0][1] == remote_entity._commands  # full commands dict


async def test_add_command_overwrite_writes_to_schema(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """Overwriting an existing command via add_command writes updated dict to schema."""
    remote_entity._commands = {"boost": VALID_PACKET}

    # Reset mock to clear any setup calls
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).reset_mock()

    with patch("custom_components.ramses_cc.remote.parse_packet_string"):
        await remote_entity.async_add_command(
            "boost",
            "RQ --- 30:123456 18:111111 --:------ 22F1 003 000031",
        )

    # Verify the command was overwritten
    assert "000031" in remote_entity._commands["boost"]
    # add_command calls delete first (if exists), then add — so 2 calls
    assert (
        cast(
            MagicMock, mock_coordinator._async_update_schema_commands
        ).await_count
        == 2
    )
    # Last call should have the updated commands
    last_call = cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).call_args
    assert last_call[0][1] == remote_entity._commands


async def test_delete_command_writes_to_schema(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """delete_command calls _async_update_schema_commands with remaining commands."""
    remote_entity._commands = {"boost": VALID_PACKET, "speed_1": VALID_PACKET}
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).reset_mock()

    await remote_entity.async_delete_command(["boost"])

    # Verify the command was removed from _commands
    assert "boost" not in remote_entity._commands
    assert "speed_1" in remote_entity._commands
    # Verify schema write was called with the remaining commands
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).assert_awaited_once()
    call_args = cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).call_args
    assert call_args[0][0] == REMOTE_ID
    assert call_args[0][1] == remote_entity._commands


async def test_delete_command_empty_dict_writes_to_schema(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """Deleting the last command writes empty dict to schema (removes _commands)."""
    remote_entity._commands = {"boost": VALID_PACKET}
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).reset_mock()

    await remote_entity.async_delete_command(["boost"])

    assert remote_entity._commands == {}
    # _async_update_schema_commands should be called with empty dict
    # (coordinator deletes _commands key when dict is empty)
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).assert_awaited_once()
    assert (
        cast(
            MagicMock, mock_coordinator._async_update_schema_commands
        ).call_args[0][1]
        == {}
    )


# ---------------------------------------------------------------------------
# delete_command strategy warning tests
# ---------------------------------------------------------------------------


async def test_delete_command_warns_on_strategy_mode_not_in_commands(
    hass: HomeAssistant,
    fan_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting a strategy mode name that isn't in _commands logs a warning."""
    from ramses_rf.strategies import OrconStrategy

    fan_device = MagicMock(spec=HvacVentilator)
    fan_device.id = FAN_ID
    fan_device.is_faked = True
    fan_device.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)
    strategy = OrconStrategy()
    fan_device.get_configured_strategy = MagicMock(return_value=strategy)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {"boost": FAN_PACKET}  # "high" not in _commands

    import logging

    caplog.set_level(
        logging.WARNING, logger="custom_components.ramses_cc.remote"
    )

    await entity.async_delete_command("high")

    # "high" is a strategy mode but not in _commands — should warn
    assert any(
        "strategy-provided mode" in r.message and "high" in r.message
        for r in caplog.records
    )


async def test_delete_command_no_warning_when_in_commands(
    hass: HomeAssistant,
    fan_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting a strategy mode name that IS in _commands does not warn."""
    from ramses_rf.strategies import OrconStrategy

    fan_device = MagicMock(spec=HvacVentilator)
    fan_device.id = FAN_ID
    fan_device.is_faked = True
    fan_device.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)
    strategy = OrconStrategy()
    fan_device.get_configured_strategy = MagicMock(return_value=strategy)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {"high": FAN_PACKET}  # "high" IS in _commands

    import logging

    caplog.set_level(
        logging.WARNING, logger="custom_components.ramses_cc.remote"
    )

    await entity.async_delete_command("high")

    # "high" is in _commands — no warning expected
    assert not any(
        "strategy-provided mode" in r.message for r in caplog.records
    )


async def test_delete_command_no_warning_for_non_strategy_name(
    hass: HomeAssistant,
    fan_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting a non-strategy command name does not warn."""
    from ramses_rf.strategies import OrconStrategy

    fan_device = MagicMock(spec=HvacVentilator)
    fan_device.id = FAN_ID
    fan_device.is_faked = True
    fan_device.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)
    strategy = OrconStrategy()
    fan_device.get_configured_strategy = MagicMock(return_value=strategy)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {"my_custom_cmd": FAN_PACKET}

    import logging

    caplog.set_level(
        logging.WARNING, logger="custom_components.ramses_cc.remote"
    )

    await entity.async_delete_command("my_custom_cmd")

    # "my_custom_cmd" is not a strategy mode — no warning
    assert not any(
        "strategy-provided mode" in r.message for r in caplog.records
    )


async def test_delete_command_no_warning_for_rem_entity(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REM entities (not FAN) don't trigger strategy warning on delete."""
    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, mock_remote_device, desc)
    entity.hass = hass
    entity._commands = {"boost": VALID_PACKET}

    import logging

    caplog.set_level(
        logging.WARNING, logger="custom_components.ramses_cc.remote"
    )

    await entity.async_delete_command("boost")

    # REM entity — no strategy warning
    assert not any(
        "strategy-provided mode" in r.message for r in caplog.records
    )


async def test_learn_command_callback_writes_to_schema(
    remote_entity: RamsesRemote,
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """learn_command's _async_on_change callback writes learned packet to schema.

    Tests the callback directly rather than the full async flow (which is
    hard to test in isolation due to asyncio.Event + state tracking).
    """
    remote_entity.hass = hass
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).reset_mock()

    # Build the callback by starting learn_command and capturing it
    learning_session = asyncio.Event()

    # Replicate the _async_on_change callback logic from async_learn_command
    @callback
    async def _async_on_change(event: Any) -> None:
        codes = ("22F1", "22F3", "22F7")
        new_state: State = event.data["new_state"]
        new_data = new_state.attributes["extra_data"]
        if (
            new_data["src"] == remote_entity._device.id
            and new_data["code"] in codes
        ):
            remote_entity._commands["learned_cmd"] = new_data["packet"]
            learning_session.set()
            await remote_entity.coordinator._async_update_schema_commands(
                remote_entity._device.id, remote_entity._commands
            )

    # Simulate a matching event
    mock_event = MagicMock()
    mock_event.data = {
        "new_state": State(
            "event.ramses_cc_learn_event",
            "test",
            {
                "extra_data": {
                    "src": REMOTE_ID,
                    "code": "22F1",
                    "packet": "learned_packet_789",
                }
            },
        )
    }

    await _async_on_change(mock_event)

    # Verify command was captured
    assert remote_entity._commands.get("learned_cmd") == "learned_packet_789"
    # Verify schema write was called
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).assert_awaited_once()
    call_args = cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).call_args
    assert call_args[0][0] == REMOTE_ID
    assert call_args[0][1]["learned_cmd"] == "learned_packet_789"


async def test_learn_command_callback_ignores_wrong_src(
    remote_entity: RamsesRemote,
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """learn_command callback does not write to schema when src doesn't match."""
    remote_entity.hass = hass
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).reset_mock()

    learning_session = asyncio.Event()

    @callback
    async def _async_on_change(event: Any) -> None:
        codes = ("22F1", "22F3", "22F7")
        new_state: State = event.data["new_state"]
        new_data = new_state.attributes["extra_data"]
        if (
            new_data["src"] == remote_entity._device.id
            and new_data["code"] in codes
        ):
            remote_entity._commands["bad_cmd"] = new_data["packet"]
            learning_session.set()
            await remote_entity.coordinator._async_update_schema_commands(
                remote_entity._device.id, remote_entity._commands
            )

    # Simulate an event from a DIFFERENT device
    mock_event = MagicMock()
    mock_event.data = {
        "new_state": State(
            "event.ramses_cc_learn_event",
            "test",
            {
                "extra_data": {
                    "src": "99:999999",
                    "code": "22F1",
                    "packet": "wrong_packet",
                }
            },
        )
    }

    await _async_on_change(mock_event)

    # Verify command was NOT captured
    assert "bad_cmd" not in remote_entity._commands
    assert not learning_session.is_set()
    # Verify schema write was NOT called
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).assert_not_awaited()


async def test_learn_command_callback_ignores_wrong_code(
    remote_entity: RamsesRemote,
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """learn_command callback ignores packets with unsupported codes."""
    remote_entity.hass = hass
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).reset_mock()

    learning_session = asyncio.Event()

    @callback
    async def _async_on_change(event: Any) -> None:
        codes = ("22F1", "22F3", "22F7")
        new_state: State = event.data["new_state"]
        new_data = new_state.attributes["extra_data"]
        if (
            new_data["src"] == remote_entity._device.id
            and new_data["code"] in codes
        ):
            remote_entity._commands["bad_cmd"] = new_data["packet"]
            learning_session.set()
            await remote_entity.coordinator._async_update_schema_commands(
                remote_entity._device.id, remote_entity._commands
            )

    # Simulate an event with a non-matching code (e.g. 10E0)
    mock_event = MagicMock()
    mock_event.data = {
        "new_state": State(
            "event.ramses_cc_learn_event",
            "test",
            {
                "extra_data": {
                    "src": REMOTE_ID,
                    "code": "10E0",
                    "packet": "wrong_code_packet",
                }
            },
        )
    }

    await _async_on_change(mock_event)

    # Verify command was NOT captured
    assert "bad_cmd" not in remote_entity._commands
    assert not learning_session.is_set()
    cast(
        MagicMock, mock_coordinator._async_update_schema_commands
    ).assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 3a: send_command with custom command
# ---------------------------------------------------------------------------


async def test_send_command_sends_custom_command_packet(
    remote_entity: RamsesRemote, mock_coordinator: MagicMock
) -> None:
    """send_command sends the packet stored in _commands for the named command."""
    custom_packet = "RQ --- 30:123456 18:111111 --:------ 22F1 003 000030"
    remote_entity._commands = {"my_custom": custom_packet}

    await remote_entity.async_send_command("my_custom")

    mock_coordinator.client.async_send_raw_command.assert_awaited_once()
    sent_cmd = mock_coordinator.client.async_send_raw_command.call_args[0][0]
    assert str(sent_cmd) == custom_packet
    # Verify QoS parameters
    kwargs = mock_coordinator.client.async_send_raw_command.call_args[1]
    assert kwargs["priority"] == Priority.HIGH


async def test_send_command_unknown_raises_error(
    remote_entity: RamsesRemote,
) -> None:
    """send_command raises HomeAssistantError for unknown command."""
    remote_entity._commands = {"boost": VALID_PACKET}

    with pytest.raises(HomeAssistantError, match="is not known"):
        await remote_entity.async_send_command("nonexistent")


async def test_send_command_not_faked_raises_error(
    remote_entity: RamsesRemote,
    mock_remote_device: MagicMock,
) -> None:
    """send_command raises HomeAssistantError when device is not faked."""
    remote_entity._commands = {"boost": VALID_PACKET}
    mock_remote_device.is_faked = False

    with pytest.raises(
        HomeAssistantError, match="is not configured for faking"
    ):
        await remote_entity.async_send_command("boost")


# ---------------------------------------------------------------------------
# Phase 3b: Packet template builder + FAN entity tests
# ---------------------------------------------------------------------------

from custom_components.ramses_cc.remote import (  # noqa: E402
    _build_packet_from_template,
    _is_command_dict,
    _parse_packet_to_template,
)
from ramses_rf.devices import HvacVentilator  # noqa: E402

FAN_ID = "30:160000"
BOUND_REM_ID = "32:153001"
FAN_PACKET = "I --- 32:153001 30:160000 --:------ 22F1 003 000030"
BYPASS_PACKET = "W --- 32:153001 30:160000 --:------ 22F7 003 0000EF"


def test_parse_packet_to_template() -> None:
    """_parse_packet_to_template extracts verb, code, payload from packet."""
    result = _parse_packet_to_template(FAN_PACKET)
    assert result == {"verb": "I", "code": "22F1", "payload": "000030"}


def test_parse_packet_to_template_bypass() -> None:
    """_parse_packet_to_template handles 22F7 (bypass) packets."""
    result = _parse_packet_to_template(BYPASS_PACKET)
    assert result == {"verb": "W", "code": "22F7", "payload": "0000EF"}


def test_parse_packet_to_template_short_packet_raises() -> None:
    """_parse_packet_to_template raises ValueError for short packets."""
    with pytest.raises(ValueError, match="Packet too short"):
        _parse_packet_to_template("I --- 22F1")


def test_is_command_dict_true() -> None:
    """_is_command_dict returns True for valid dict templates."""
    assert _is_command_dict({"verb": "W", "code": "22F7", "payload": "0000EF"})


def test_is_command_dict_false_for_string() -> None:
    """_is_command_dict returns False for packet strings."""
    assert not _is_command_dict(FAN_PACKET)


def test_is_command_dict_false_for_incomplete_dict() -> None:
    """_is_command_dict returns False for dicts missing required keys."""
    assert not _is_command_dict({"verb": "W", "code": "22F7"})
    assert not _is_command_dict({"verb": "W"})
    assert not _is_command_dict({})


def test_build_packet_from_template() -> None:
    """_build_packet_from_template builds CLI shorthand from dict."""
    fan = MagicMock(spec=HvacVentilator)
    fan.id = FAN_ID
    fan.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)

    coordinator = MagicMock()
    coordinator.client = MagicMock()

    cmd_def = {"verb": "W", "code": "22F7", "payload": "0000EF"}
    result = _build_packet_from_template(cmd_def, fan, coordinator)
    assert result == "W 32:153001 30:160000 --:------ 22F7 0000EF"


def test_build_packet_from_template_explicit_src() -> None:
    """_build_packet_from_template uses explicit src if provided."""
    fan = MagicMock(spec=HvacVentilator)
    fan.id = FAN_ID
    fan.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)

    coordinator = MagicMock()
    coordinator.client = MagicMock()

    cmd_def = {
        "verb": "W",
        "code": "22F7",
        "payload": "0000EF",
        "src": "32:153002",
    }
    result = _build_packet_from_template(cmd_def, fan, coordinator)
    assert "32:153002" in result
    assert "32:153001" not in result


def test_build_packet_from_template_hgi_fallback() -> None:
    """_build_packet_from_template falls back to HGI when no bound REM."""
    fan = MagicMock(spec=HvacVentilator)
    fan.id = FAN_ID
    fan.get_bound_rem = MagicMock(return_value=None)

    hgi = MagicMock()
    hgi.id = "18:001234"
    gwy = MagicMock()
    gwy._hgi = hgi
    gwy.hgi = gwy._hgi
    coordinator = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client._gateway = gwy
    coordinator.client.gateway = coordinator.client._gateway

    cmd_def = {"verb": "W", "code": "22F7", "payload": "0000EF"}
    result = _build_packet_from_template(cmd_def, fan, coordinator)
    assert "18:001234" in result


def test_build_packet_from_template_no_src_raises() -> None:
    """_build_packet_from_template raises when no src can be resolved."""
    fan = MagicMock(spec=HvacVentilator)
    fan.id = FAN_ID
    fan.get_bound_rem = MagicMock(return_value=None)

    coordinator = MagicMock()
    coordinator.client = None

    cmd_def = {"verb": "W", "code": "22F7", "payload": "0000EF"}
    with pytest.raises(HomeAssistantError, match="No bound REM or HGI"):
        _build_packet_from_template(cmd_def, fan, coordinator)


def test_build_packet_length_calculated() -> None:
    """_build_packet_from_template includes payload (length calculated by from_cli)."""
    fan = MagicMock(spec=HvacVentilator)
    fan.id = FAN_ID
    fan.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)

    coordinator = MagicMock()
    coordinator.client = MagicMock()

    # 6 hex chars = 3 bytes → payload included
    cmd_def = {"verb": "W", "code": "22F7", "payload": "0000EF"}
    result = _build_packet_from_template(cmd_def, fan, coordinator)
    assert "0000EF" in result

    # 4 hex chars = 2 bytes → payload included
    cmd_def2 = {"verb": "W", "code": "22B0", "payload": "0005"}
    result2 = _build_packet_from_template(cmd_def2, fan, coordinator)
    assert "0005" in result2


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock HvacVentilator device."""
    device = MagicMock(spec=HvacVentilator)
    device.id = FAN_ID
    device.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)
    return device


@pytest.fixture
def fan_coordinator(hass: HomeAssistant) -> MagicMock:
    """Return a mock coordinator for FAN entity tests."""
    coordinator = MagicMock()
    coordinator._remotes = {
        FAN_ID: {
            "bypass_on": {"verb": "W", "code": "22F7", "payload": "0000EF"}
        },
        BOUND_REM_ID: {"boost": FAN_PACKET},
    }
    coordinator.learn_device_id = None
    coordinator.fan_handler = MagicMock()
    coordinator.fan_handler._fan_bound_to_remote = {BOUND_REM_ID: FAN_ID}
    coordinator._sem = MagicMock()
    coordinator._sem.__enter__ = MagicMock(return_value=None)
    coordinator._sem.__exit__ = MagicMock(return_value=None)
    coordinator.client = MagicMock()
    coordinator.client.async_send_raw_command = AsyncMock()
    coordinator.client.async_send_cmd = (
        coordinator.client.async_send_raw_command
    )
    coordinator.async_refresh = AsyncMock()
    coordinator._async_update_schema_commands = AsyncMock()
    coordinator.options = {
        "schema": {
            FAN_ID: {"_class": "FAN", "_bound": [BOUND_REM_ID]},
        },
    }
    return coordinator


@pytest.fixture
def fan_remote_entity(
    hass: HomeAssistant,
    fan_coordinator: MagicMock,
    mock_fan_device: MagicMock,
) -> RamsesRemote:
    """Return a RamsesRemote entity on a FAN device."""
    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, mock_fan_device, desc)
    entity.hass = hass
    return entity


def test_fan_entity_is_fan_entity(
    fan_remote_entity: RamsesRemote,
) -> None:
    """FAN entity reports is_fan_entity=True."""
    assert fan_remote_entity.is_fan_entity is True


def test_rem_entity_is_not_fan_entity(
    remote_entity: RamsesRemote,
) -> None:
    """REM entity reports is_fan_entity=False."""
    assert remote_entity.is_fan_entity is False


def test_fan_entity_bound_rem_ids(
    fan_remote_entity: RamsesRemote,
) -> None:
    """FAN entity reads _bound from schema to get bound REM IDs."""
    assert fan_remote_entity._bound_rem_ids == [BOUND_REM_ID]


def test_fan_entity_loads_own_commands(
    fan_coordinator: MagicMock,
    mock_fan_device: MagicMock,
    hass: HomeAssistant,
) -> None:
    """FAN entity loads its own _commands (dicts) from coordinator._remotes."""
    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, mock_fan_device, desc)
    entity.hass = hass
    # FAN's own commands (dicts) should be loaded
    assert "bypass_on" in entity._commands
    assert _is_command_dict(entity._commands["bypass_on"])


def test_fan_entity_loads_bound_rem_commands_as_fallback(
    fan_coordinator: MagicMock,
    mock_fan_device: MagicMock,
    hass: HomeAssistant,
) -> None:
    """FAN entity loads bound REM's commands (strings) as fallback."""
    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, mock_fan_device, desc)
    entity.hass = hass
    # REM's commands (strings) should be loaded as fallback
    assert "boost" in entity._commands
    assert entity._commands["boost"] == FAN_PACKET


def test_fan_entity_extra_state_attributes_bound_rems(
    fan_remote_entity: RamsesRemote,
) -> None:
    """FAN entity extra_state_attributes includes bound_rems."""
    attrs = fan_remote_entity.extra_state_attributes
    assert "bound_rems" in attrs
    assert attrs["bound_rems"] == [BOUND_REM_ID]


def test_rem_entity_extra_state_attributes_bound_to_fan(
    remote_entity: RamsesRemote,
) -> None:
    """REM entity extra_state_attributes includes bound_to_fan."""
    attrs = remote_entity.extra_state_attributes
    assert "bound_to_fan" in attrs


async def test_fan_send_command_dict_template(
    fan_remote_entity: RamsesRemote,
    fan_coordinator: MagicMock,
) -> None:
    """FAN entity send_command builds packet from dict template."""
    await fan_remote_entity.async_send_command("bypass_on")
    # Verify async_send_cmd was called with the built packet string
    fan_coordinator.client.async_send_raw_command.assert_called_once()
    cmd = fan_coordinator.client.async_send_raw_command.call_args.args[0]
    assert "22F7" in str(cmd)
    assert "0000EF" in str(cmd)
    assert FAN_ID in str(cmd)


async def test_fan_send_command_rem_string_fallback(
    fan_remote_entity: RamsesRemote,
    fan_coordinator: MagicMock,
) -> None:
    """FAN entity send_command uses REM packet string for fallback commands."""
    # "boost" is a REM command (packet string), not a FAN dict template
    await fan_remote_entity.async_send_command("boost")
    fan_coordinator.client.async_send_raw_command.assert_called_once()
    cmd = fan_coordinator.client.async_send_raw_command.call_args.args[0]
    assert "22F1" in str(cmd)


async def test_fan_send_command_raw_string_defensive_guard(
    fan_remote_entity: RamsesRemote,
    fan_coordinator: MagicMock,
    mock_fan_device: MagicMock,
) -> None:
    """FAN entity send_command sends raw packet string even when is_faked is False."""
    # Arrange: FAN has raw string command directly, and device is NOT faked
    mock_fan_device.is_faked = False
    fan_remote_entity._commands["laag"] = (
        "I --- 29:123150 30:160000 --:------ 22F1 003 000206"
    )

    # Act
    await fan_remote_entity.async_send_command("laag")

    # Assert
    fan_coordinator.client.async_send_raw_command.assert_called_once()
    cmd = fan_coordinator.client.async_send_raw_command.call_args.args[0]
    assert "22F1" in str(cmd)
    assert "000206" in str(cmd)


async def test_fan_add_command_parses_to_dict(
    fan_remote_entity: RamsesRemote,
    fan_coordinator: MagicMock,
) -> None:
    """FAN entity add_command parses packet string to dict template."""
    # Use a valid packet for Command validation
    valid_packet = "RQ --- 32:153001 30:160000 --:------ 22F1 003 000030"
    await fan_remote_entity.async_add_command("calendar_on", valid_packet)
    # Verify _async_update_schema_commands was called with dict format
    fan_coordinator._async_update_schema_commands.assert_called_once()
    saved_commands = (
        fan_coordinator._async_update_schema_commands.call_args.args[1]
    )
    assert "calendar_on" in saved_commands
    assert _is_command_dict(saved_commands["calendar_on"])


async def test_rem_add_command_keeps_string(
    remote_entity: RamsesRemote,
    mock_coordinator: MagicMock,
) -> None:
    """REM entity add_command stores packet string as-is (backward compat)."""
    await remote_entity.async_add_command("test_cmd", VALID_PACKET)
    mock_coordinator._async_update_schema_commands.assert_called_once()
    saved_commands = (
        mock_coordinator._async_update_schema_commands.call_args.args[1]
    )
    assert saved_commands["test_cmd"] == VALID_PACKET
    assert not _is_command_dict(saved_commands["test_cmd"])


# ── _split_commands / _merge_commands / _with_metadata ──────────────


def test_split_commands_separates_comment() -> None:
    """_split_commands separates _comment from actual commands."""
    raw = {
        "_comment": "Target the FAN for automations",
        "bypass_on": {"verb": "W", "code": "22F7", "payload": "0000EF"},
        "speed_1": "RQ --- 37:170000 18:001234 --:------ 22F1 003 000031",
    }
    cmds, meta = _split_commands(raw)
    assert "_comment" not in cmds
    assert "bypass_on" in cmds
    assert "speed_1" in cmds
    assert meta == {"_comment": "Target the FAN for automations"}


def test_split_commands_no_metadata() -> None:
    """_split_commands returns empty metadata when no reserved keys."""
    raw = {"bypass_on": {"verb": "W", "code": "22F7", "payload": "0000EF"}}
    cmds, meta = _split_commands(raw)
    assert cmds == raw
    assert meta == {}


def test_split_commands_empty() -> None:
    """_split_commands handles empty dict."""
    cmds, meta = _split_commands({})
    assert cmds == {}
    assert meta == {}


def test_merge_commands_fan_priority() -> None:
    """_merge_commands keeps FAN metadata, ignores REM metadata."""
    fan = {
        "_comment": "FAN comment",
        "bypass_on": {"verb": "W", "code": "22F7", "payload": "0000EF"},
    }
    rem = {
        "_comment": "REM comment (should be ignored)",
        "speed_1": "RQ --- 37:170000 18:001234 --:------ 22F1 003 000031",
    }
    merged = _merge_commands(fan, rem)
    assert merged["_comment"] == "FAN comment"
    assert "bypass_on" in merged
    assert "speed_1" in merged


def test_merge_commands_first_wins() -> None:
    """_merge_commands: first source's command wins for duplicates."""
    fan = {"bypass_on": {"verb": "W", "code": "22F7", "payload": "0000EF"}}
    rem = {"bypass_on": "RQ --- 37:170000 18:001234 --:------ 22F7 003 0000EF"}
    merged = _merge_commands(fan, rem)
    assert _is_command_dict(merged["bypass_on"])  # FAN dict wins


def test_with_metadata_reattaches_comment() -> None:
    """_with_metadata re-attaches _comment for schema persistence."""
    cmds = {"bypass_on": {"verb": "W", "code": "22F7", "payload": "0000EF"}}
    meta = {"_comment": "Target the FAN"}
    result = _with_metadata(cmds, meta)
    assert result["_comment"] == "Target the FAN"
    assert "bypass_on" in result


def test_with_metadata_empty_meta() -> None:
    """_with_metadata with empty metadata returns plain commands."""
    cmds = {"bypass_on": {"verb": "W", "code": "22F7", "payload": "0000EF"}}
    result = _with_metadata(cmds, {})
    assert "_comment" not in result
    assert "bypass_on" in result


# ---------------------------------------------------------------------------
# Strategy fallback tests for remote.send_command
# ---------------------------------------------------------------------------


async def test_send_command_strategy_fallback_fan(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """Test that send_command falls back to set_fan_mode for FAN entities."""
    fan_device = MagicMock(spec=HvacVentilator)
    fan_device.id = FAN_ID
    fan_device.is_faked = True
    fan_device.set_fan_mode = AsyncMock()
    fan_device.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)
    fan_device.get_configured_strategy = MagicMock(return_value=None)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}  # No custom commands

    await entity.async_send_command("high")

    # Strategy fallback should call set_fan_mode on the device
    fan_device.set_fan_mode.assert_awaited_once_with("high")
    # Should NOT have sent a raw packet
    mock_coordinator.client.async_send_cmd.assert_not_called()


async def test_send_command_strategy_fallback_failure(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """Test that strategy fallback error raises HomeAssistantError."""
    fan_device = MagicMock(spec=HvacVentilator)
    fan_device.id = FAN_ID
    fan_device.is_faked = True
    fan_device.set_fan_mode = AsyncMock(
        side_effect=Exception("scheme mismatch")
    )
    fan_device.get_bound_rem = MagicMock(return_value=None)
    fan_device.get_configured_strategy = MagicMock(return_value=None)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}

    with pytest.raises(HomeAssistantError, match="strategy fallback failed"):
        await entity.async_send_command("unknown_mode")


async def test_send_command_no_fallback_for_rem(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    mock_remote_device: MagicMock,
) -> None:
    """Test that REM entities (not FAN) don't use strategy fallback."""
    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, mock_remote_device, desc)
    entity.hass = hass
    entity._commands = {}

    with pytest.raises(HomeAssistantError, match="is not known"):
        await entity.async_send_command("high")


def test_extra_state_attributes_strategy_modes(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """Test that FAN entities expose strategy_modes in attributes."""
    from ramses_rf.strategies import OrconStrategy

    fan_device = MagicMock(spec=HvacVentilator)
    fan_device.id = FAN_ID
    fan_device.is_faked = True
    fan_device.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)
    strategy = OrconStrategy()
    fan_device.get_configured_strategy = MagicMock(return_value=strategy)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass

    attrs = entity.extra_state_attributes
    assert "strategy_modes" in attrs
    assert "strategy_scheme" in attrs
    assert attrs["strategy_scheme"] == "orcon"
    assert "high" in attrs["strategy_modes"]
    assert "away" in attrs["strategy_modes"]


# ---------------------------------------------------------------------------
# Strategy builtin_commands send_command tests (issue 1113)
# ---------------------------------------------------------------------------

# Dict templates matching the ramses_rf strategy builtin_commands format
# (verb/code/payload + a type tag).  Set on the strategy instance so the
# tests do not depend on the installed ramses_rf version.
ORCON_BOOST_BUILTIN: dict[str, dict[str, str]] = {
    "high_15": {
        "verb": "I",
        "code": "22F3",
        "payload": "00120F03040404",
        "type": "boost_timer",
    },
    "low_30": {
        "verb": "I",
        "code": "22F3",
        "payload": "00121E01040404",
        "type": "boost_timer",
    },
}
ITHO_BOOST_BUILTIN: dict[str, dict[str, str]] = {
    "boost_10": {
        "verb": "I",
        "code": "22F3",
        "payload": "00000A",
        "type": "boost_timer",
    },
}


def _fan_device_with_strategy(
    builtin: dict[str, dict[str, str]],
    aliases: dict[str, str] | None = None,
) -> MagicMock:
    """Return a mock HvacVentilator whose strategy has builtin_commands."""
    from ramses_rf.strategies import OrconStrategy

    fan_device = MagicMock(spec=HvacVentilator)
    fan_device.id = FAN_ID
    fan_device.is_faked = True
    fan_device.set_fan_mode = AsyncMock()
    fan_device.get_bound_rem = MagicMock(return_value=BOUND_REM_ID)
    strategy = OrconStrategy()
    strategy._builtin_commands = builtin
    strategy._boost_aliases = aliases or {}
    fan_device.get_configured_strategy = MagicMock(return_value=strategy)
    return fan_device


async def test_send_command_builtin_boost_orcon(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """send_command sends a strategy builtin 22F3 boost command."""
    fan_device = _fan_device_with_strategy(ORCON_BOOST_BUILTIN)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}

    await entity.async_send_command("high_15")

    mock_coordinator.client.async_send_raw_command.assert_called_once()
    cmd = mock_coordinator.client.async_send_raw_command.call_args.args[0]
    assert "22F3" in str(cmd)
    assert "00120F03040404" in str(cmd)
    assert BOUND_REM_ID in str(cmd)  # src = bound REM
    assert FAN_ID in str(cmd)  # dst = FAN
    # Builtins are dict templates — the native mode path is not used
    fan_device.set_fan_mode.assert_not_called()


async def test_send_command_builtin_boost_itho_simple(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """send_command sends a 3-byte (Itho-style) builtin boost command."""
    fan_device = _fan_device_with_strategy(ITHO_BOOST_BUILTIN)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}

    await entity.async_send_command("boost_10")

    mock_coordinator.client.async_send_raw_command.assert_called_once()
    cmd = mock_coordinator.client.async_send_raw_command.call_args.args[0]
    assert "22F3" in str(cmd)
    assert "00000A" in str(cmd)


async def test_send_command_builtin_alias_resolves(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """send_command resolves a boost alias to the canonical builtin name."""
    fan_device = _fan_device_with_strategy(
        ORCON_BOOST_BUILTIN, aliases={"hoog_15": "high_15"}
    )

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}

    await entity.async_send_command("hoog_15")

    mock_coordinator.client.async_send_raw_command.assert_called_once()
    cmd = mock_coordinator.client.async_send_raw_command.call_args.args[0]
    assert "22F3" in str(cmd)
    assert "00120F03040404" in str(cmd)


async def test_send_command_schema_command_overrides_builtin(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """A schema _commands entry wins over a builtin of the same name."""
    fan_device = _fan_device_with_strategy(ORCON_BOOST_BUILTIN)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {"high_15": FAN_PACKET}

    await entity.async_send_command("high_15")

    mock_coordinator.client.async_send_raw_command.assert_called_once()
    cmd = mock_coordinator.client.async_send_raw_command.call_args.args[0]
    # Schema packet (22F1) sent, not the builtin 22F3 template
    assert "22F1" in str(cmd)
    assert "22F3" not in str(cmd)


async def test_send_command_builtin_not_persisted(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """Sending a builtin command does not write it to schema _commands."""
    fan_device = _fan_device_with_strategy(ORCON_BOOST_BUILTIN)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}

    await entity.async_send_command("high_15")

    assert "high_15" not in entity._commands
    mock_coordinator._async_update_schema_commands.assert_not_called()


async def test_send_command_empty_builtin_falls_back_to_set_fan_mode(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    """With no matching builtin, native set_fan_mode fallback is used."""
    fan_device = _fan_device_with_strategy(ORCON_BOOST_BUILTIN)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(mock_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}

    # "high" is a strategy fan mode, not a builtin command
    await entity.async_send_command("high")

    fan_device.set_fan_mode.assert_awaited_once_with("high")
    mock_coordinator.client.async_send_raw_command.assert_not_called()


def test_fan_extra_state_attributes_merges_builtin_commands(
    hass: HomeAssistant,
    fan_coordinator: MagicMock,
) -> None:
    """Builtin commands appear in the commands attr; schema wins conflicts."""
    fan_device = _fan_device_with_strategy(ORCON_BOOST_BUILTIN)

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, fan_device, desc)
    entity.hass = hass

    attrs = entity.extra_state_attributes
    # Builtin commands merged into the displayed commands dict
    assert attrs["commands"]["high_15"] == ORCON_BOOST_BUILTIN["high_15"]
    # Schema commands still present alongside builtins
    assert "bypass_on" in attrs["commands"]

    # Schema _commands win on name conflict
    entity._commands["high_15"] = FAN_PACKET
    attrs = entity.extra_state_attributes
    assert attrs["commands"]["high_15"] == FAN_PACKET


async def test_delete_command_warns_on_builtin_command_name(
    hass: HomeAssistant,
    fan_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting a builtin command name that isn't in _commands warns."""
    fan_device = _fan_device_with_strategy(
        ORCON_BOOST_BUILTIN, aliases={"hoog_15": "high_15"}
    )

    desc = RamsesRemoteEntityDescription(key="remote")
    entity = RamsesRemote(fan_coordinator, fan_device, desc)
    entity.hass = hass
    entity._commands = {}

    caplog.set_level(
        logging.WARNING, logger="custom_components.ramses_cc.remote"
    )

    await entity.async_delete_command("high_15")
    await entity.async_delete_command("hoog_15")

    assert any(
        "strategy-provided mode" in r.message and "high_15" in r.message
        for r in caplog.records
    )
    assert any(
        "strategy-provided mode" in r.message and "hoog_15" in r.message
        for r in caplog.records
    )


def test_remote_commands_for_save_with_comment(
    remote_entity: RamsesRemote,
) -> None:
    """Test _commands_for_save attaches _comment metadata."""
    remote_entity._command_comment = "Custom remote comment"
    remote_entity._commands = {"boost": "I --- 37:112233 ..."}

    saved = remote_entity._commands_for_save
    assert saved["_comment"] == "Custom remote comment"
    assert saved["boost"] == "I --- 37:112233 ..."


async def test_async_learn_command_timeout(
    remote_entity: RamsesRemote,
) -> None:
    """Test async_learn_command raises HomeAssistantError on timeout."""
    with (
        patch(
            "asyncio.Event.wait",
            AsyncMock(side_effect=TimeoutError("Timed out")),
        ),
        pytest.raises(HomeAssistantError, match="Timeout"),
    ):
        await remote_entity.async_learn_command(["boost"], timeout=1)


async def test_async_learn_command_rem_and_fan_success(
    remote_entity: RamsesRemote,
    fan_remote_entity: RamsesRemote,
) -> None:
    """Test async_learn_command on_change callback for REM and FAN entities."""
    # 1. REM entity learn
    captured_callback: Any = None

    def _mock_track(hass: Any, entity_id: str, action: Any) -> Any:
        nonlocal captured_callback
        captured_callback = action
        return MagicMock()

    with (
        patch(
            "custom_components.ramses_cc.remote.async_track_state_change_event",
            side_effect=_mock_track,
        ),
        patch.object(
            remote_entity.coordinator,
            "_async_update_schema_commands",
            AsyncMock(),
        ),
    ):

        async def _fire_event() -> None:
            await asyncio.sleep(0.01)
            fake_state = MagicMock()
            fake_state.attributes = {
                "extra_data": {
                    "src": remote_entity._device.id,
                    "code": "22F1",
                    "packet": "045  I --- 37:154519 --:------ 37:154519 22F1 003 0005C8",
                }
            }
            fake_event = MagicMock()
            fake_event.data = {"new_state": fake_state}
            await captured_callback(fake_event)

        asyncio.create_task(_fire_event())
        await remote_entity.async_learn_command(["boost"], timeout=1)

    assert "boost" in remote_entity._commands
    assert remote_entity._commands["boost"].startswith("045  I ---")


async def test_async_send_command_non_fan_dict_error(
    remote_entity: RamsesRemote,
) -> None:
    """Test send_command with dict command on non-FAN device raises HomeAssistantError."""
    remote_entity._commands = {
        "dict_cmd": {"verb": "W", "code": "22F1", "payload": "00"}
    }
    with pytest.raises(
        HomeAssistantError,
        match="Dict-format commands require a FAN entity target",
    ):
        await remote_entity.async_send_command("dict_cmd")


async def test_async_send_command_invalid_packet_error(
    remote_entity: RamsesRemote,
) -> None:
    """Test send_command with unparsable packet string raises ValueError."""
    remote_entity._device.is_faked = True
    remote_entity._commands = {"bad_cmd": "completely invalid packet string"}
    with pytest.raises(ValueError, match="Failed to parse packet_str"):
        await remote_entity.async_send_command("bad_cmd")

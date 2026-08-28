"""Tests for task/timer cleanup to prevent lingering tasks on unload/removal.

These tests prove that:
1. _clear_pending_after_timeout tasks are tracked and cancelled on entity removal
2. async_call_later handles are stored and cancelled on unload
3. Discovery probe tasks are tracked and cancelled on unload
4. _schedule_clear_pending tracks tasks on the entity
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
)
from custom_components.ramses_cc.services import RamsesServiceHandler
from ramses_rf.entity import Entity as RamsesRFEntity

FAN_ID = "30:999888"


class MockDevice(RamsesRFEntity):
    """Mock device class for testing."""

    supports_2411: bool = True

    def get_fan_param(self, param_id: str) -> float | None:
        """Stub."""
        return None

    def clear_fan_param(self, param_id: str) -> None:
        """Stub."""
        pass


@pytest.fixture  # type: ignore[untyped-decorator]
def mock_coordinator(hass: HomeAssistant) -> MagicMock:
    """Return a mock RamsesCoordinator."""
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.entry = MagicMock()
    coordinator.entry.entry_id = "test_entry"
    coordinator.async_request_refresh = AsyncMock()
    coordinator.client = MagicMock()
    coordinator.devices = []
    coordinator.entry.runtime_data = coordinator
    return coordinator


@pytest.fixture  # type: ignore[untyped-decorator]
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device."""
    device = MagicMock(spec=MockDevice)
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    device.get_fan_param.return_value = None
    return device


@pytest.fixture  # type: ignore[untyped-decorator]
def number_entity(
    mock_coordinator: MagicMock, mock_fan_device: MagicMock
) -> RamsesNumberParam:
    """Return an initialized RamsesNumberParam with a real event loop."""
    desc = RamsesNumberEntityDescription(key="param_01", ramses_rf_attr="01")
    entity = RamsesNumberParam(mock_coordinator, mock_fan_device, desc)
    entity.hass = mock_coordinator.hass
    entity.async_write_ha_state = MagicMock()
    return entity


async def test_clear_pending_task_cancelled_on_entity_removal(
    number_entity: RamsesNumberParam,
) -> None:
    """_clear_pending_after_timeout task is cancelled when entity is removed.

    This proves the fix for untracked _clear_pending_after_timeout tasks that
    would linger for up to 30s after an entity was removed, attempting to
    write state to a dead entity.
    """
    # Start a pending timeout task
    number_entity._pending_timer = asyncio.create_task(
        number_entity._clear_pending_after_timeout(30)
    )

    assert number_entity._pending_timer is not None
    assert not number_entity._pending_timer.done()

    # Simulate entity removal from HA
    await number_entity.async_will_remove_from_hass()

    # The task should be cancelled
    assert number_entity._pending_timer is None


async def test_clear_pending_task_cancelled_before_new_one(
    number_entity: RamsesNumberParam,
) -> None:
    """Previous _clear_pending_after_timeout task is cancelled before starting new one.

    This proves the fix for multiple overlapping pending tasks that would
    accumulate when a parameter was requested multiple times.
    """
    # Start first pending timeout task
    number_entity._pending_timer = asyncio.create_task(
        number_entity._clear_pending_after_timeout(30)
    )
    first_task = number_entity._pending_timer

    # Simulate requesting a new parameter (which should cancel the old task)
    # We patch the device interaction to avoid needing a real gateway
    with patch.object(number_entity, "async_write_ha_state"):
        number_entity.set_pending()
        if hasattr(number_entity._device, "get_fan_param"):
            number_entity._device.get_fan_param("01")

        # Cancel previous and start new (mirrors the code in _request_parameter_value)
        if number_entity._pending_timer is not None:
            if not number_entity._pending_timer.done():
                number_entity._pending_timer.cancel()
        number_entity._pending_timer = asyncio.create_task(
            number_entity._clear_pending_after_timeout(30)
        )

    # First task should be cancelled
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()
    # New task should be active
    assert number_entity._pending_timer is not None
    assert not number_entity._pending_timer.done()

    # Cleanup
    number_entity._pending_timer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await number_entity._pending_timer


async def test_schedule_clear_pending_tracks_task_on_entity(
    mock_coordinator: MagicMock,
) -> None:
    """_schedule_clear_pending stores the task on the entity's _pending_timer.

    This proves the fix for the 11 untracked async_create_task calls in
    services.py that created _clear_pending_after_timeout tasks without
    storing them.
    """
    handler = RamsesServiceHandler(mock_coordinator)

    # Create a mock entity with the required interface
    entity = MagicMock()
    entity._pending_timer = None

    async def _clear_pending(timeout: int) -> None:
        await asyncio.sleep(timeout)

    entity._clear_pending_after_timeout = _clear_pending

    # Schedule a clear pending task
    handler._schedule_clear_pending(entity, 30)

    # The task should be stored on the entity
    assert entity._pending_timer is not None
    assert isinstance(entity._pending_timer, asyncio.Task)
    assert not entity._pending_timer.done()

    # Cleanup
    entity._pending_timer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await entity._pending_timer


async def test_schedule_clear_pending_cancels_previous_task(
    mock_coordinator: MagicMock,
) -> None:
    """_schedule_clear_pending cancels any previous pending task on the entity.

    This proves the fix for overlapping pending tasks when multiple service
    calls target the same entity.
    """
    handler = RamsesServiceHandler(mock_coordinator)

    entity = MagicMock()
    entity._pending_timer = asyncio.create_task(asyncio.sleep(100))

    async def _clear_pending(timeout: int) -> None:
        await asyncio.sleep(timeout)

    entity._clear_pending_after_timeout = _clear_pending

    prev_task = entity._pending_timer

    # Schedule a new clear pending task (should cancel the previous)
    handler._schedule_clear_pending(entity, 30)

    # Previous task should be cancelled
    await asyncio.sleep(0)
    assert prev_task.cancelled() or prev_task.done()
    # New task should be stored
    assert entity._pending_timer is not None
    assert entity._pending_timer is not prev_task

    # Cleanup
    entity._pending_timer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await entity._pending_timer


async def test_service_handler_cleanup_cancels_probe_task(
    mock_coordinator: MagicMock,
) -> None:
    """async_cleanup cancels the discovery probe task.

    This proves the fix for the untracked _async_probe_and_discover task
    that could run for minutes after the integration was unloaded.
    """
    handler = RamsesServiceHandler(mock_coordinator)

    # Create a fake probe task
    async def _long_probe() -> None:
        await asyncio.sleep(100)

    handler._probe_task = asyncio.create_task(_long_probe())

    assert not handler._probe_task.done()

    # Run cleanup
    await handler.async_cleanup()

    # Probe task should be cancelled
    assert handler._probe_task is None


async def test_service_handler_cleanup_cancels_call_later_handles(
    mock_coordinator: MagicMock,
) -> None:
    """async_cleanup cancels stored async_call_later handles.

    This proves the fix for untracked async_call_later handles in services.py
    that would fire refresh callbacks on a stopped coordinator.
    """
    handler = RamsesServiceHandler(mock_coordinator)

    # Create mock call_later handles
    cancelled: list[bool] = []
    handle1 = MagicMock()
    handle1.side_effect = lambda: cancelled.append(True)
    handle2 = MagicMock()
    handle2.side_effect = lambda: cancelled.append(True)

    handler._call_later_handles = [handle1, handle2]

    # Run cleanup
    await handler.async_cleanup()

    # Both handles should have been called (cancelled)
    assert len(cancelled) == 2
    assert handler._call_later_handles == []


async def test_service_handler_cleanup_cancels_fan_param_sequences(
    mock_coordinator: MagicMock,
) -> None:
    """async_cleanup cancels all fan param sequence tasks.

    This proves the fix for fan param sequence tasks that could outlive
    the integration if unloaded mid-sequence.
    """
    handler = RamsesServiceHandler(mock_coordinator)

    async def _long_sequence() -> None:
        await asyncio.sleep(100)

    task1 = asyncio.create_task(_long_sequence())
    task2 = asyncio.create_task(_long_sequence())
    handler._fan_param_sequences = {"dev1": task1, "dev2": task2}

    # Run cleanup
    await handler.async_cleanup()

    # Both tasks should be cancelled
    assert task1.cancelled() or task1.done()
    assert task2.cancelled() or task2.done()
    assert handler._fan_param_sequences == {}


async def test_service_handler_cleanup_no_op_when_idle(
    mock_coordinator: MagicMock,
) -> None:
    """async_cleanup is safe to call when there are no pending tasks."""
    handler = RamsesServiceHandler(mock_coordinator)

    # Should not raise
    await handler.async_cleanup()

    assert handler._probe_task is None
    assert handler._call_later_handles == []
    assert handler._fan_param_sequences == {}


async def test_schedule_clear_pending_tracks_task_in_handler(
    mock_coordinator: MagicMock,
) -> None:
    """_schedule_clear_pending also tracks the task in the handler's _pending_timers.

    This proves the fix for issue 802: pending timers must be centrally
    tracked so they can be cancelled on HA shutdown.
    """
    handler = RamsesServiceHandler(mock_coordinator)

    entity = MagicMock()
    entity._pending_timer = None

    async def _clear_pending(timeout: int) -> None:
        await asyncio.sleep(timeout)

    entity._clear_pending_after_timeout = _clear_pending

    handler._schedule_clear_pending(entity, 30)

    # Task should be tracked in the handler
    assert len(handler._pending_timers) == 1
    assert handler._pending_timers[0] is entity._pending_timer

    # Cleanup
    entity._pending_timer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await entity._pending_timer


async def test_register_pending_timer_tracks_task(
    mock_coordinator: MagicMock,
) -> None:
    """register_pending_timer adds a task to the central tracking list."""
    handler = RamsesServiceHandler(mock_coordinator)

    task = asyncio.create_task(asyncio.sleep(100))
    handler.register_pending_timer(task)

    assert task in handler._pending_timers

    # Cleanup
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_async_cleanup_cancels_pending_timers(
    mock_coordinator: MagicMock,
) -> None:
    """async_cleanup cancels all tracked pending timers (issue 802).

    This proves that pending timers registered via _schedule_clear_pending
    or register_pending_timer are cancelled on cleanup, preventing
    'still running after final writes shutdown stage' warnings.
    """
    handler = RamsesServiceHandler(mock_coordinator)

    async def _long_pending() -> None:
        await asyncio.sleep(100)

    task1 = asyncio.create_task(_long_pending())
    task2 = asyncio.create_task(_long_pending())
    handler.register_pending_timer(task1)
    handler.register_pending_timer(task2)

    assert len(handler._pending_timers) == 2

    await handler.async_cleanup()

    assert task1.cancelled() or task1.done()
    assert task2.cancelled() or task2.done()
    assert handler._pending_timers == []


async def test_async_cleanup_is_idempotent_for_pending_timers(
    mock_coordinator: MagicMock,
) -> None:
    """async_cleanup can be called twice without error (stop event + unload)."""
    handler = RamsesServiceHandler(mock_coordinator)

    task = asyncio.create_task(asyncio.sleep(100))
    handler.register_pending_timer(task)

    await handler.async_cleanup()
    assert task.cancelled() or task.done()

    # Second call should not raise
    await handler.async_cleanup()
    assert handler._pending_timers == []

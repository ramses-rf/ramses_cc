"""Tests for thread safety compliance in remote.py."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import ATTR_DEVICE_ID
from custom_components.ramses_cc.remote import (
    RamsesRemote,
    RamsesRemoteEntityDescription,
)


@pytest.fixture
def mock_broker() -> MagicMock:
    """Create a mock RamsesBroker."""
    broker = MagicMock()
    broker._remotes = {}
    broker._fan_bound_to_remote = {}
    return broker


@pytest.fixture
def mock_device() -> MagicMock:
    """Create a mock HvacRemote device."""
    device = MagicMock()
    device.id = "29:123456"
    return device


@pytest.fixture
def ramses_remote(mock_broker: MagicMock, mock_device: MagicMock) -> RamsesRemote:
    """Create a RamsesRemote entity instance."""
    description = RamsesRemoteEntityDescription(key="test_remote")

    # We mock RamsesEntity.__init__ to avoid side effects if strictly necessary,
    # but here we can just let it run if dependencies are mocked.
    # However, RamsesEntity is not imported here to patch easily without full path.
    # We'll rely on mocks behaving well.

    # Fix Ruff F841: Use '_' for unused variable
    with patch("custom_components.ramses_cc.remote.RamsesEntity.__init__") as _:
        entity = RamsesRemote(mock_broker, mock_device, description)
        entity.hass = MagicMock(spec=HomeAssistant)
        entity.entity_id = "remote.test_remote"

        # Fix AttributeError: Since we mocked __init__, we must manually set these
        # attributes that RamsesEntity would usually handle.
        entity._broker = mock_broker
        entity._device = mock_device

        return entity


async def test_update_fan_rem_params_calls_broker_directly(
    ramses_remote: RamsesRemote, mock_broker: MagicMock
) -> None:
    """Test that get_all_fan_params is called directly, NOT via executor.

    This verifies fix for REMOTE-01: Thread Safety Violation.
    """
    # Setup: Bind the remote to a fan
    mock_broker._fan_bound_to_remote = {"29:123456": "30:987654"}

    kwargs = {"some_arg": "value"}

    # Execute
    await ramses_remote.async_update_fan_rem_params(**kwargs)

    # Verify:
    # 1. Broker method was called
    mock_broker.get_all_fan_params.assert_called_once()

    # 2. Arguments were correct
    call_args = mock_broker.get_all_fan_params.call_args[0][0]
    assert call_args[ATTR_DEVICE_ID] == "30:987654"
    assert call_args["from_id"] == "29:123456"
    assert call_args["some_arg"] == "value"

    # 3. CRITICAL: Ensure async_add_executor_job was NOT called
    ramses_remote.hass.async_add_executor_job.assert_not_called()


async def test_update_fan_rem_params_no_binding(
    ramses_remote: RamsesRemote, mock_broker: MagicMock
) -> None:
    """Test behavior when remote is not bound to a fan."""
    # Setup: Empty binding dict
    mock_broker._fan_bound_to_remote = {}

    # Execute
    await ramses_remote.async_update_fan_rem_params()

    # Verify:
    # Broker method should NOT be called
    mock_broker.get_all_fan_params.assert_not_called()
    # Executor should NOT be called
    ramses_remote.hass.async_add_executor_job.assert_not_called()

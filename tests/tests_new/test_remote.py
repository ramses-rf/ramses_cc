"""Tests for the ramses_cc remote platform features.

This module targets command database management, learning, sending,
and fan parameter coordination in remote.py.
"""

from unittest.mock import AsyncMock, MagicMock

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
    broker._sem = MagicMock()
    broker.client.async_send_cmd = AsyncMock()
    broker.async_get_fan_param = AsyncMock()
    broker.async_set_fan_param = AsyncMock()
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

"""Extended tests for RamsesBroker to reach 100% coverage."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from ramses_rf.entity_base import Child
from ramses_rf.system import Zone


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> RamsesBroker:
    """Return a mock broker with an entry attached."""
    entry = MagicMock()
    entry.entry_id = "service_test_entry"
    entry.options = {"ramses_rf": {}, "serial_port": "/dev/ttyUSB0"}

    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()
    broker.client.async_send_cmd = AsyncMock()
    broker.platforms = {}

    hass.data[DOMAIN] = {entry.entry_id: broker}
    return broker


async def test_async_force_update(mock_broker: RamsesBroker) -> None:
    """Test the async_force_update service call."""
    # Mock async_update to verify it gets called
    mock_broker.async_update = AsyncMock()

    call = ServiceCall(DOMAIN, "force_update", {})
    await mock_broker.async_force_update(call)

    mock_broker.async_update.assert_called_once()


async def test_get_device_and_from_id_propagates_exceptions(
    mock_broker: RamsesBroker,
) -> None:
    """Test that exceptions during device lookup are propagated (not swallowed)."""
    # Mock _resolve_device_id to raise an arbitrary exception
    # (Simulating a serious failure in lookup logic)
    mock_broker._resolve_device_id = MagicMock(
        side_effect=ValueError("Critical Lookup Failure")
    )

    # We now expect the exception to bubble up, so the caller can handle it
    with pytest.raises(ValueError, match="Critical Lookup Failure"):
        mock_broker._get_device_and_from_id({"device_id": "30:111111"})


async def test_update_device_via_device_logic(
    mock_broker: RamsesBroker, hass: HomeAssistant
) -> None:
    """Test the via_device logic in _update_device for Zones and Children."""
    # We need to test the specific branches for Zone and Child

    # 1. Test Zone with TCS
    mock_tcs = MagicMock()
    mock_tcs.id = "01:123456"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "04:111111"
    mock_zone.tcs = mock_tcs
    mock_zone._msg_value_code.return_value = None  # No model description
    mock_zone._SLUG = "ZN"

    # 2. Test Child with Parent
    mock_parent = MagicMock()
    mock_parent.id = "02:222222"

    mock_child = MagicMock(spec=Child)
    mock_child.id = "03:333333"
    mock_child._parent = mock_parent
    mock_child._msg_value_code.return_value = None
    mock_child._SLUG = "DHW"

    # We need to mock device_registry.async_get to ensure we hit the logic
    # The code calls dr.async_get(hass)

    mock_dr = MagicMock()
    # Mock async_get to return the registry mock
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr):
        # Trigger update for Zone
        mock_broker._update_device(mock_zone)

        # Verify call to async_get_or_create has correct via_device for Zone (TCS ID)
        # Check the most recent call [-1]
        call_args_zone = mock_dr.async_get_or_create.call_args_list[-1][1]
        assert call_args_zone["via_device"] == (DOMAIN, "01:123456")

        # Trigger update for Child
        mock_broker._update_device(mock_child)

        # Verify call to async_get_or_create has correct via_device for Child (Parent ID)
        # Check the most recent call [-1]
        call_args_child = mock_dr.async_get_or_create.call_args_list[-1][1]
        assert call_args_child["via_device"] == (DOMAIN, "02:222222")


async def test_adjust_sentinel_packet_early_return(mock_broker: RamsesBroker) -> None:
    """Test _adjust_sentinel_packet returns early if src/dst don't match."""
    # Ensure client HGI is set
    mock_broker.client.hgi.id = "18:006402"

    # Mock command with non-matching IDs
    cmd = MagicMock()
    cmd.src.id = "18:999999"  # Not sentinel (18:000730)
    cmd.dst.id = "01:000000"  # Not HGI

    # If it returns early, it won't access cmd._frame or cmd._addrs inside the try/except
    # We can check this by ensuring pkt_addrs is NOT called
    with patch("custom_components.ramses_cc.broker.pkt_addrs") as mock_pkt_addrs:
        mock_broker._adjust_sentinel_packet(cmd)
        mock_pkt_addrs.assert_not_called()


async def test_set_fan_param_generic_exception(mock_broker: RamsesBroker) -> None:
    """Explicitly test the generic exception handler coverage in set_fan_param."""
    # Mock send_cmd to raise a generic Exception (not ValueError)
    mock_broker.client.async_send_cmd.side_effect = Exception("Generic Transport Error")

    # Mock entity find so we don't error out earlier
    mock_entity = MagicMock()
    # IMPORTANT: Must be AsyncMock because it is passed to asyncio.create_task
    mock_entity._clear_pending_after_timeout = AsyncMock()

    mock_broker._find_param_entity = MagicMock(return_value=mock_entity)

    call_data = {
        "device_id": "30:111111",
        "param_id": "0A",
        "value": 1,
        "from_id": "18:000000",
    }

    # We patch _get_device_and_from_id to bypass the lookup failure.
    # We also mock Command to ensure it doesn't fail validation before sending.
    with (
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
        patch("custom_components.ramses_cc.broker.Command") as mock_cmd,
    ):
        mock_cmd.set_fan_param.return_value = MagicMock()

        with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
            await mock_broker.async_set_fan_param(call_data)

        # Ensure the cleanup was attempted
        mock_entity._clear_pending_after_timeout.assert_called_with(0)

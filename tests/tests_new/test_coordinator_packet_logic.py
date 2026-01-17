"""Tests for packet injection logic and address manipulation."""

from unittest.mock import MagicMock, patch

from custom_components.ramses_cc.services import RamsesServiceHandler
from ramses_tx.exceptions import PacketAddrSetInvalid

# Constants
HGI_ID = "18:006402"
SENTINEL_ID = "18:000730"


def test_adjust_sentinel_packet_swaps_on_invalid() -> None:
    """Test that addresses are swapped when validation fails for sentinel packet."""
    # Setup
    # Use generic MagicMock because 'client' is an instance attribute
    coordinator = MagicMock()
    # Logic Update: The handler expects self._coordinator.client..., so we self-reference
    coordinator._coordinator = coordinator
    coordinator.client.hgi.id = HGI_ID

    # Mock Command
    # Use generic MagicMock to avoid attribute issues with 'src'/'dst'
    cmd = MagicMock()
    cmd.src.id = SENTINEL_ID
    cmd.dst.id = HGI_ID
    cmd._frame = "X" * 40  # Dummy frame data
    cmd._addrs = ["addr0", "addr1", "addr2"]

    # Patch pkt_addrs to raise PacketAddrSetInvalid
    with patch("custom_components.ramses_cc.services.pkt_addrs") as mock_validate:
        mock_validate.side_effect = PacketAddrSetInvalid("Invalid structure")

        # Execute using the unbound method call, passing our mock as 'self'
        RamsesServiceHandler._adjust_sentinel_packet(coordinator, cmd)

        # Verify swap occurred
        assert cmd._addrs[1] == "addr2"
        assert cmd._addrs[2] == "addr1"
        assert cmd._repr is None


def test_adjust_sentinel_packet_no_swap_on_valid() -> None:
    """Test that addresses are NOT swapped when validation passes."""
    # Setup
    coordinator = MagicMock()
    coordinator._coordinator = coordinator
    coordinator.client.hgi.id = HGI_ID

    cmd = MagicMock()
    cmd.src.id = SENTINEL_ID
    cmd.dst.id = HGI_ID
    cmd._frame = "X" * 40
    cmd._addrs = ["addr0", "addr1", "addr2"]

    with patch("custom_components.ramses_cc.services.pkt_addrs") as mock_validate:
        mock_validate.return_value = True  # Validation passes

        RamsesServiceHandler._adjust_sentinel_packet(coordinator, cmd)

        # Verify NO swap
        assert cmd._addrs[1] == "addr1"
        assert cmd._addrs[2] == "addr2"


def test_adjust_sentinel_packet_ignores_other_devices() -> None:
    """Test that logic is skipped for non-sentinel devices."""
    coordinator = MagicMock()
    coordinator._coordinator = coordinator
    coordinator.client.hgi.id = HGI_ID

    cmd = MagicMock()
    cmd.src.id = "01:123456"  # Not sentinel
    cmd.dst.id = HGI_ID
    cmd._addrs = ["addr0", "addr1", "addr2"]

    # Execute
    RamsesServiceHandler._adjust_sentinel_packet(coordinator, cmd)

    # Verify no swap and repr is untouched
    assert cmd._addrs == ["addr0", "addr1", "addr2"]
    assert cmd._repr is not None  # Should not be reset

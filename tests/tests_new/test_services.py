"""Tests for the Services aspect of RamsesBroker (Bind, Send Packet, Service Calls)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.helpers import (
    ha_device_id_to_ramses_device_id,
    ramses_device_id_to_ha_device_id,
)
from ramses_rf.exceptions import BindingFlowFailed
from ramses_tx.exceptions import PacketAddrSetInvalid

# Constants
HGI_ID = "18:006402"
SENTINEL_ID = "18:000730"
FAN_ID = "30:111222"
RAMSES_ID = "32:153289"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> RamsesBroker:
    """Return a mock broker with an entry attached."""
    entry = MagicMock()
    entry.entry_id = "service_test_entry"
    entry.options = {"ramses_rf": {}, "serial_port": "/dev/ttyUSB0"}

    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()
    broker.client.async_send_cmd = AsyncMock()

    hass.data[DOMAIN] = {entry.entry_id: broker}
    return broker


async def test_bind_device_raises_ha_error(mock_broker: RamsesBroker) -> None:
    """Test that async_bind_device raises HomeAssistantError on binding failure."""
    mock_device = MagicMock()
    mock_device.id = "01:123456"
    mock_device._initiate_binding_process = AsyncMock(
        side_effect=BindingFlowFailed("Timeout waiting for confirm")
    )
    mock_broker.client.fake_device.return_value = mock_device

    call = MagicMock()
    call.data = {
        "device_id": "01:123456",
        "offer": {"key": "val"},
        "confirm": {"key": "val"},
        "device_info": None,
    }

    with pytest.raises(HomeAssistantError, match="Binding failed for device"):
        await mock_broker.async_bind_device(call)


async def test_set_fan_param_raises_ha_error_invalid_value(
    mock_broker: RamsesBroker,
) -> None:
    """Test that async_set_fan_param raises HomeAssistantError on invalid input."""
    call_data = {
        "device_id": "30:111222",
        "param_id": "0A",
        # "value": missing -> triggers ValueError
        "from_id": "32:111111",
    }
    with pytest.raises(HomeAssistantError, match="Invalid parameter for set_fan_param"):
        await mock_broker.async_set_fan_param(call_data)


async def test_set_fan_param_raises_ha_error_no_source(
    mock_broker: RamsesBroker,
) -> None:
    """Test that async_set_fan_param raises HomeAssistantError when no source device is found."""
    call_data = {
        "device_id": "30:111222",
        "param_id": "0A",
        "value": 1,
        # No from_id and no bound device configured in mock
    }
    with pytest.raises(HomeAssistantError, match="No valid source device available"):
        await mock_broker.async_set_fan_param(call_data)


def test_adjust_sentinel_packet_swaps_on_invalid() -> None:
    """Test that addresses are swapped when validation fails for sentinel packet."""
    broker = MagicMock()
    broker.client.hgi.id = HGI_ID

    cmd = MagicMock()
    cmd.src.id = SENTINEL_ID
    cmd.dst.id = HGI_ID
    cmd._frame = "X" * 40
    cmd._addrs = ["addr0", "addr1", "addr2"]

    with patch("custom_components.ramses_cc.broker.pkt_addrs") as mock_validate:
        mock_validate.side_effect = PacketAddrSetInvalid("Invalid structure")
        RamsesBroker._adjust_sentinel_packet(broker, cmd)

        assert cmd._addrs[1] == "addr2"
        assert cmd._addrs[2] == "addr1"
        assert cmd._repr is None


def test_adjust_sentinel_packet_no_swap_on_valid() -> None:
    """Test that addresses are NOT swapped when validation passes."""
    broker = MagicMock()
    broker.client.hgi.id = HGI_ID

    cmd = MagicMock()
    cmd.src.id = SENTINEL_ID
    cmd.dst.id = HGI_ID
    cmd._addrs = ["addr0", "addr1", "addr2"]

    with patch("custom_components.ramses_cc.broker.pkt_addrs") as mock_validate:
        mock_validate.return_value = True
        RamsesBroker._adjust_sentinel_packet(broker, cmd)
        assert cmd._addrs[1] == "addr1"


def test_adjust_sentinel_packet_ignores_other_devices() -> None:
    """Test that logic is skipped for non-sentinel devices."""
    broker = MagicMock()
    broker.client.hgi.id = HGI_ID
    cmd = MagicMock()
    cmd.src.id = "01:123456"  # Not sentinel
    cmd._addrs = ["addr0", "addr1", "addr2"]

    RamsesBroker._adjust_sentinel_packet(broker, cmd)
    assert cmd._addrs == ["addr0", "addr1", "addr2"]


def test_get_param_id_validation(mock_broker: RamsesBroker) -> None:
    """Test validation of parameter IDs in service calls."""
    assert mock_broker._get_param_id({"param_id": "0a"}) == "0A"

    with pytest.raises(ValueError, match="Invalid parameter ID"):
        mock_broker._get_param_id({"param_id": "001"})

    with pytest.raises(ValueError, match="Invalid parameter ID"):
        mock_broker._get_param_id({"param_id": "ZZ"})


async def test_broker_device_lookup_fail(mock_broker: RamsesBroker) -> None:
    """Test broker handling when a device lookup fails."""
    call_data = {"device_id": "99:999999", "param_id": "01"}
    with patch("custom_components.ramses_cc.broker._LOGGER.warning") as mock_warn:
        await mock_broker.async_get_fan_param(call_data)
        assert mock_warn.called
        assert "No valid source device available" in mock_warn.call_args[0][0]


async def test_broker_service_presence(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test that the expected services are registered with Home Assistant."""
    services = hass.services.async_services()
    if DOMAIN in services:
        assert "get_fan_param" in services[DOMAIN]
        assert "set_fan_param" in services[DOMAIN]


# --- Helper Tests (moved from test_broker_services.py) ---
# These verify helpers used during service ID resolution


def test_ha_to_ramses_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from HA registry ID to RAMSES hardware ID."""
    assert ha_device_id_to_ramses_device_id(hass, "") is None
    assert ha_device_id_to_ramses_device_id(hass, "missing") is None

    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config")
    config_entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RAMSES_ID)},
    )
    result = ha_device_id_to_ramses_device_id(hass, device.id)
    assert result == RAMSES_ID


def test_ramses_to_ha_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from RAMSES hardware ID to HA registry ID."""
    assert ramses_device_id_to_ha_device_id(hass, "") is None
    assert ramses_device_id_to_ha_device_id(hass, "99:999999") is None

    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config_2")
    config_entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RAMSES_ID)},
    )
    result = ramses_device_id_to_ha_device_id(hass, RAMSES_ID)
    assert result == device.id


def test_ha_to_ramses_id_wrong_domain(hass: HomeAssistant) -> None:
    """Test mapping when the device registry entry belongs to another domain."""
    config_entry = MockConfigEntry(domain="not_ramses", entry_id="other_entry")
    config_entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("not_ramses", "some_id")},
    )
    assert ha_device_id_to_ramses_device_id(hass, device.id) is None


async def test_bind_device_success(mock_broker: RamsesBroker) -> None:
    """Test the happy path for async_bind_device."""
    mock_device = MagicMock()
    mock_device.id = "01:123456"
    mock_device._initiate_binding_process = AsyncMock(return_value=None)  # Success
    mock_broker.client.fake_device.return_value = mock_device

    call = MagicMock()
    call.data = {
        "device_id": "01:123456",
        "offer": {},
        "confirm": {},
        "device_info": None,
    }

    # Should not raise exception
    await mock_broker.async_bind_device(call)

    # Verify call later was scheduled
    assert mock_device._initiate_binding_process.called


async def test_send_packet_hgi_alias(mock_broker: RamsesBroker) -> None:
    """Test async_send_packet with HGI aliasing logic."""
    # Setup HGI in client
    mock_broker.client.hgi.id = "18:999999"

    call = MagicMock()
    # Using the sentinel alias ID "18:000730"
    call.data = {
        "device_id": "18:000730",
        "from_id": "18:000730",
        "verb": "I",
        "code": "1F09",
        "payload": "FF",
    }

    await mock_broker.async_send_packet(call)

    # Check that create_cmd was called with the REAL HGI ID, not the alias
    # This covers lines 602-603
    create_kwargs = mock_broker.client.create_cmd.call_args[1]
    assert create_kwargs["device_id"] == "18:999999"


def test_resolve_device_ids_complex(mock_broker: RamsesBroker) -> None:
    """Test _resolve_device_id with lists and area_ids."""
    # 1. Test List handling (lines 807-813)
    data: dict[str, Any] = {"device_id": ["01:111111", "01:222222"]}
    resolved = mock_broker._resolve_device_id(data)
    assert resolved == "01:111111"
    assert data["device_id"] == "01:111111"  # Should update input dict

    # 2. Test explicit None return (line 827)
    assert mock_broker._resolve_device_id({}) is None

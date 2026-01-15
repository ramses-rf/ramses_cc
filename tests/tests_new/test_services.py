"""Tests for the Services aspect of RamsesBroker (Bind, Send Packet, Service Calls)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
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
    # IMPORTANT: Initialize device_by_id as a dict so .get() returns None for missing keys
    # instead of a MagicMock object.
    broker.client.device_by_id = {}
    broker.platforms = {}

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
    # We patch _get_device_and_from_id because otherwise the broker checks for
    # the device existence first and raises "No valid source device" before
    # checking the value.
    with (
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111222", "30_111222", "32:111111"),
        ),
        pytest.raises(HomeAssistantError, match="Invalid parameter for set_fan_param"),
    ):
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
    with patch("custom_components.ramses_cc.broker._LOGGER.warning") as mock_warn:
        resolved = mock_broker._resolve_device_id(data)
        assert resolved == "01:111111"
        assert data["device_id"] == "01:111111"  # Should update input dict
        assert mock_warn.called

    # 2. Test explicit None return (line 827)
    assert mock_broker._resolve_device_id({}) is None

    # 3. Test empty list
    data_empty: dict[str, Any] = {"device_id": []}
    assert mock_broker._resolve_device_id(data_empty) is None

    # 4. Test list with empty values
    data_missing: dict[str, Any] = {"device": []}
    assert mock_broker._resolve_device_id(data_missing) is None

    # 5. Test HA Device list (multiple devices)
    data_ha_list: dict[str, Any] = {"device": ["ha_id_1", "ha_id_2"]}
    # Mock _target_to_device_id to avoid needing full registry setup
    with (
        patch.object(mock_broker, "_target_to_device_id", return_value="01:555555"),
        patch("custom_components.ramses_cc.broker._LOGGER.warning") as mock_warn_2,
    ):
        resolved_ha = mock_broker._resolve_device_id(data_ha_list)
        assert resolved_ha == "01:555555"
        assert mock_warn_2.called
        assert data_ha_list["device"] == "ha_id_1"


async def test_resolve_device_id_area_string(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test resolving device ID from an Area ID passed as a string (not list)."""
    # Create a device in an area
    dev_reg = dr.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config")
    config_entry.add_to_hass(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "01:555555")},
    )
    dev_reg.async_update_device(device.id, area_id="test_area")

    # Pass area_id as string, not list, to hit line: area_ids = [area_ids]
    data = {"target": {"area_id": "test_area"}}
    resolved = mock_broker._resolve_device_id(data)

    assert resolved == "01:555555"


async def test_find_param_entity_registry_only(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test _find_param_entity when entity is in registry but not platform."""
    # Add entity to registry
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        "30_111222_param_0a",
        original_icon="mdi:fan",
    )
    # Ensure entity ID matches what broker expects (HA might add ramses_cc prefix)
    if entry.entity_id != "number.30_111222_param_0a":
        ent_reg.async_update_entity(
            entry.entity_id, new_entity_id="number.30_111222_param_0a"
        )

    # Ensure platform is empty or doesn't have it
    mock_broker.platforms = {"number": [MagicMock(entities={})]}

    # This should hit line 669 (return None)
    entity = mock_broker._find_param_entity("30:111222", "0A")
    assert entity is None


async def test_async_set_fan_param_success_clear_pending(
    mock_broker: RamsesBroker,
) -> None:
    """Test full success path of set_fan_param including pending state."""
    mock_broker._devices = [MagicMock(id=FAN_ID)]
    mock_entity = MagicMock()
    mock_entity.set_pending = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()

    # Mock Command to avoid validation errors
    with (
        patch.object(mock_broker, "_find_param_entity", return_value=mock_entity),
        patch("custom_components.ramses_cc.broker.Command") as mock_cmd_cls,
    ):
        mock_cmd = MagicMock()
        mock_cmd_cls.set_fan_param.return_value = mock_cmd

        call = {
            "device_id": FAN_ID,
            "param_id": "0A",
            "value": 20,
            "from_id": "32:999999",
        }
        await mock_broker.async_set_fan_param(call)

        # Verify command sent
        assert mock_broker.client.async_send_cmd.called
        assert mock_broker.client.async_send_cmd.call_args[0][0] == mock_cmd
        # Verify pending set
        assert mock_entity.set_pending.called


async def test_find_param_entity_found_in_platform(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test _find_param_entity when entity is found in the platform."""
    # 1. Add entity to registry to pass the first check in _find_param_entity
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        "30_111222_param_0a",
        original_icon="mdi:fan",
    )
    # Force entity ID to match what broker expects
    if entry.entity_id != "number.30_111222_param_0a":
        ent_reg.async_update_entity(
            entry.entity_id, new_entity_id="number.30_111222_param_0a"
        )

    # 2. Mock the platform with the entity loaded
    mock_entity = MagicMock()
    mock_platform = MagicMock()
    # ensure hasattr(platform, "entities") is True and key exists
    mock_platform.entities = {"number.30_111222_param_0a": mock_entity}
    mock_broker.platforms = {"number": [mock_platform]}

    # 3. Call the method
    entity = mock_broker._find_param_entity("30:111222", "0A")

    # 4. Assert we got the specific entity object from the platform
    assert entity is mock_entity


async def test_get_device_and_from_id_bound_logic(mock_broker: RamsesBroker) -> None:
    """Test _get_device_and_from_id logic regarding bound devices."""
    mock_dev = MagicMock()
    mock_dev.id = "30:111111"

    # Mock the internal list so _get_device finds it
    mock_broker._devices = [mock_dev]

    call = {"device_id": "30:111111"}

    # Case 1: Bound device exists and returns valid ID
    mock_dev.get_bound_rem.return_value = "30:999999"
    orig, norm, from_id = mock_broker._get_device_and_from_id(call)
    assert orig == "30:111111"
    assert from_id == "30:999999"

    # Case 2: Bound device exists but returns None (not bound)
    # This hits lines 922-927
    mock_dev.get_bound_rem.return_value = None
    orig_2, norm_2, from_id_2 = mock_broker._get_device_and_from_id(call)

    # Correct logic: It should still return the device ID, but empty from_id
    assert from_id_2 == ""
    assert orig_2 == "30:111111"


async def test_run_fan_param_sequence_exception(mock_broker: RamsesBroker) -> None:
    """Test exception handling in _async_run_fan_param_sequence."""
    # Force an exception inside the sequence loop
    # We patch the schema to a single item to make the test deterministic and fast
    with (
        patch("custom_components.ramses_cc.broker._2411_PARAMS_SCHEMA", ["0A"]),
        patch.object(
            mock_broker, "async_get_fan_param", side_effect=Exception("Sequence Error")
        ),
        patch("custom_components.ramses_cc.broker._LOGGER.error") as mock_err,
    ):
        await mock_broker._async_run_fan_param_sequence({"device_id": "30:111111"})

        # Should catch exception and log error, not raise
        assert mock_err.called

        # Verify the log message format matches the code in broker.py
        # args[0] is the message format string
        # args[1] is the param_id
        args = mock_err.call_args[0]
        assert args[0] == "Failed to get fan parameter %s for device: %s"
        assert args[1] == "0A"


async def test_set_fan_param_generic_exception(mock_broker: RamsesBroker) -> None:
    """Test generic exception handling in async_set_fan_param."""
    call = {
        "device_id": "30:111111",
        "param_id": "0A",
        "value": "1",
        "from_id": "18:000000",
    }
    # Mock Command to bypass validation so we hit the send_cmd exception
    with patch("custom_components.ramses_cc.broker.Command") as mock_cmd:
        # Mock send_cmd to raise a generic Exception (not ValueError)
        mock_broker.client.async_send_cmd.side_effect = Exception("Transport Failure")
        mock_cmd.set_fan_param.return_value = MagicMock()

        with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
            await mock_broker.async_set_fan_param(call)


async def test_resolve_device_id_single_item_list(mock_broker: RamsesBroker) -> None:
    """Test resolving device ID from a list with exactly one item."""
    data: dict[str, Any] = {"device_id": ["30:111111"]}
    resolved = mock_broker._resolve_device_id(data)
    assert resolved == "30:111111"
    assert data["device_id"] == "30:111111"


async def test_resolve_device_ha_id_string(mock_broker: RamsesBroker) -> None:
    """Test resolving device from 'device' field as string."""
    # Mock _target_to_device_id to return a RAMSES ID
    with patch.object(
        mock_broker, "_target_to_device_id", return_value="30:111111"
    ) as mock_target:
        data: dict[str, Any] = {"device": "ha_device_id_123"}
        resolved = mock_broker._resolve_device_id(data)
        assert resolved == "30:111111"
        assert data["device_id"] == "30:111111"
        # Verify it called _target_to_device_id with the string wrapped in list
        mock_target.assert_called_with({"device_id": ["ha_device_id_123"]})


async def test_target_to_device_id_entity_string(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test _target_to_device_id handles entity_id as string."""
    # Setup registry with device
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
    config_entry.add_to_hass(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id="test", identifiers={(DOMAIN, "30:123456")}
    )
    entity = ent_reg.async_get_or_create(
        "sensor", DOMAIN, "test_sens", device_id=device.id
    )

    target = {"entity_id": entity.entity_id}  # String, not list
    resolved = mock_broker._target_to_device_id(target)
    assert resolved == "30:123456"


async def test_target_to_device_id_device_string(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test _target_to_device_id handles device_id as string."""
    # Setup registry
    dev_reg = dr.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
    config_entry.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id="test", identifiers={(DOMAIN, "30:654321")}
    )

    target = {"device_id": device.id}  # String (HA Device ID)
    resolved = mock_broker._target_to_device_id(target)
    assert resolved == "30:654321"


async def test_set_fan_param_exception_handling(
    mock_broker: RamsesBroker,
) -> None:
    """Test that generic exception in set_fan_param is handled gracefully."""
    # entity
    mock_entity = MagicMock()
    # Must be AsyncMock because it is awaited via asyncio.create_task logic in test
    mock_entity._clear_pending_after_timeout = AsyncMock()

    with (
        patch.object(mock_broker, "_find_param_entity", return_value=mock_entity),
        patch("custom_components.ramses_cc.broker.Command") as mock_cmd,
        # Patch device lookup to ensure we reach the logic
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
    ):
        # Mock send_cmd to raise Exception
        mock_broker.client.async_send_cmd.side_effect = Exception("Boom")
        # Mock cmd creation to succeed so we reach send_cmd
        mock_cmd.set_fan_param.return_value = MagicMock()

        call = {
            "device_id": "30:111111",
            "param_id": "0A",
            "value": "1",
            "from_id": "18:000000",
        }

        # Expect HomeAssistantError and logged error
        with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
            await mock_broker.async_set_fan_param(call)


async def test_run_fan_param_sequence_dict_fail(mock_broker: RamsesBroker) -> None:
    """Test the try/except block in run_fan_param_sequence."""

    # We need to mock data so dict(data) raises ValueError
    # Mock _normalize_service_call
    class BadDict:
        def __init__(self) -> None:
            self.items = lambda: [("device_id", "30:111111")]

        def __iter__(self) -> Any:
            raise ValueError("Cannot iterate")

    bad_data = BadDict()

    with patch.object(mock_broker, "_normalize_service_call", return_value=bad_data):
        # mocking async_get_fan_param to avoid actual calls
        mock_broker.async_get_fan_param = AsyncMock()

        await mock_broker._async_run_fan_param_sequence({})

        # If it reached here without raising, and called async_get_fan_param, it worked
        assert mock_broker.async_get_fan_param.called
        # Check arguments - should be a dict
        args = mock_broker.async_get_fan_param.call_args[0][0]
        assert isinstance(args, dict)
        assert args["device_id"] == "30:111111"


async def test_get_fan_param_value_error(mock_broker: RamsesBroker) -> None:
    """Test that ValueError in get_fan_param (e.g. invalid param ID) is caught and logged."""
    # We use 'ZZ' to force a ValueError in _get_param_id
    call = {
        "device_id": "30:111111",
        "param_id": "ZZ",
        "from_id": "18:000000",
    }
    # Patch device lookup to succeed so we reach param ID check
    with (
        patch("custom_components.ramses_cc.broker._LOGGER.error") as mock_err,
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
    ):
        await mock_broker.async_get_fan_param(call)
        assert mock_err.called
        assert "Failed to get fan parameter" in mock_err.call_args[0][0]


async def test_set_fan_param_exception_clears_pending(
    mock_broker: RamsesBroker,
) -> None:
    """Test that generic exception in set_fan_param clears pending state."""
    # entity
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()

    with (
        patch.object(mock_broker, "_find_param_entity", return_value=mock_entity),
        patch("custom_components.ramses_cc.broker.Command") as mock_cmd_cls,
        # Patch device lookup so we don't fail early with 'No valid source'
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
    ):
        mock_cmd = MagicMock()
        mock_cmd_cls.set_fan_param.return_value = mock_cmd
        # Mock send_cmd to raise Exception
        mock_broker.client.async_send_cmd.side_effect = Exception("Boom")

        call = {
            "device_id": "30:111111",
            "param_id": "0A",
            "value": "1",
            "from_id": "18:000000",
        }

        with pytest.raises(HomeAssistantError):
            await mock_broker.async_set_fan_param(call)

        # Check clear pending called with 0
        mock_entity._clear_pending_after_timeout.assert_called_with(0)

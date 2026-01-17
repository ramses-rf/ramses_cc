"""Tests for the Services aspect of RamsesBroker (Bind, Send Packet, Service Calls)."""

from datetime import datetime as dt, timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import (
    CONF_RAMSES_RF,
    DOMAIN,
    SIGNAL_UPDATE,
    SZ_BOUND_TO,
    SZ_CLIENT_STATE,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    SZ_PACKETS,
    SZ_SCHEMA,
)
from custom_components.ramses_cc.helpers import (
    ha_device_id_to_ramses_device_id,
    ramses_device_id_to_ha_device_id,
)
from ramses_rf.device import Device
from ramses_rf.device.hvac import HvacVentilator
from ramses_rf.entity_base import Child
from ramses_rf.exceptions import BindingFlowFailed
from ramses_rf.system import System, Zone
from ramses_tx.exceptions import PacketAddrSetInvalid

# Constants
HGI_ID = "18:006402"
SENTINEL_ID = "18:000730"
FAN_ID = "30:111222"
RAMSES_ID = "32:153289"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> RamsesBroker:
    """Return a mock broker with an entry attached.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :return: A mocked RamsesBroker instance configured for testing.
    :rtype: RamsesBroker
    """
    entry = MagicMock()
    entry.entry_id = "service_test_entry"
    entry.options = {
        "ramses_rf": {},
        "serial_port": "/dev/ttyUSB0",
        SZ_KNOWN_LIST: {},
    }

    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()
    broker.client.async_send_cmd = AsyncMock()
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
    # Patch _get_device_and_from_id because otherwise the broker checks for
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


# --- Helper Tests (verify helpers used during service ID resolution) ---


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
    # Patch the schema to a single item to make the test deterministic and fast
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
    """Test the generic exception handler coverage in async_set_fan_param.

    This test verifies that when the transport layer raises a generic Exception,
    the broker correctly catches it, raises a HomeAssistantError, and
    executes the necessary entity cleanup.

    :param mock_broker: The mocked Ramses broker fixture.
    :type mock_broker: RamsesBroker
    """
    # 1. Setup the transport failure
    mock_broker.client.async_send_cmd.side_effect = Exception("Transport Failure")

    # 2. Setup the entity and its cleanup mock
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    mock_broker._find_param_entity = MagicMock(return_value=mock_entity)

    call_data = {
        "device_id": "30:111111",
        "param_id": "0A",
        "value": 1,
        "from_id": "18:000000",
    }

    # 3. Patch necessary internal methods and the Command builder
    with (
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
        patch("custom_components.ramses_cc.broker.Command") as mock_cmd,
    ):
        mock_cmd.set_fan_param.return_value = MagicMock()

        # 4. Verify that HomeAssistantError is raised with the correct message
        with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
            await mock_broker.async_set_fan_param(call_data)

        # 5. Verify the cleanup mechanism was triggered (from the first test file)
        mock_entity._clear_pending_after_timeout.assert_called_with(0)


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

    # Mock data so dict(data) raises ValueError
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

    with pytest.raises(ValueError, match="Critical Lookup Failure"):
        mock_broker._get_device_and_from_id({"device_id": "30:111111"})


async def test_update_device_via_device_logic(
    mock_broker: RamsesBroker, hass: HomeAssistant
) -> None:
    """Test the via_device logic in _update_device for Zones and Children."""
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

    mock_dr = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr):
        # Trigger update for Zone
        mock_broker._update_device(mock_zone)
        # Check zone via_device (most recent call)
        call_args_zone = mock_dr.async_get_or_create.call_args_list[-1][1]
        assert call_args_zone["via_device"] == (DOMAIN, "01:123456")

        # Trigger update for Child
        mock_broker._update_device(mock_child)
        # Check child via_device (most recent call)
        call_args_child = mock_dr.async_get_or_create.call_args_list[-1][1]
        assert call_args_child["via_device"] == (DOMAIN, "02:222222")


async def test_adjust_sentinel_packet_early_return(mock_broker: RamsesBroker) -> None:
    """Test _adjust_sentinel_packet returns early if src/dst don't match."""
    mock_broker.client.hgi.id = "18:006402"
    cmd = MagicMock()
    cmd.src.id = "18:999999"  # Not sentinel
    cmd.dst.id = "01:000000"  # Not HGI

    with patch("custom_components.ramses_cc.broker.pkt_addrs") as mock_pkt_addrs:
        mock_broker._adjust_sentinel_packet(cmd)
        mock_pkt_addrs.assert_not_called()


async def test_find_param_entity_missing_in_platform(
    hass: HomeAssistant, mock_broker: RamsesBroker
) -> None:
    """Test _find_param_entity returns None if entity in registry but not in platform."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "number", DOMAIN, "30_111111_param_0a", original_icon="mdi:fan"
    )
    if entry.entity_id != "number.30_111111_param_0a":
        ent_reg.async_update_entity(
            entry.entity_id, new_entity_id="number.30_111111_param_0a"
        )

    mock_platform = MagicMock()
    mock_platform.entities = {}
    mock_broker.platforms = {"number": [mock_platform]}

    entity = mock_broker._find_param_entity("30:111111", "0A")
    assert entity is None


async def test_resolve_device_id_list_warning(mock_broker: RamsesBroker) -> None:
    """Test that passing a list to device_id logs a warning."""
    with patch("custom_components.ramses_cc.broker._LOGGER.warning") as mock_warn:
        mock_broker._resolve_device_id({"device_id": ["30:111111", "30:222222"]})

        assert mock_warn.called
        # Verify the call was made with the format string and specific arguments
        mock_warn.assert_called_with(
            "Multiple values for '%s' provided, using first one: %s",
            "device_id",
            "30:111111",
        )


async def test_get_device_client_fallback(mock_broker: RamsesBroker) -> None:
    """Test _get_device falls back to client.device_by_id."""
    mock_broker._devices = []
    mock_dev = MagicMock()
    mock_dev.id = "30:999999"

    # Configure client.device_by_id to work as a dict
    mock_broker.client.device_by_id = {"30:999999": mock_dev}

    dev = mock_broker._get_device("30:999999")
    assert dev == mock_dev


async def test_update_device_valid_child_type(mock_broker: RamsesBroker) -> None:
    """Test _update_device with a valid Child class to ensure fallback logic."""
    # Use spec=Child so isinstance(dev, Child) returns True
    mock_child = MagicMock(spec=Child)
    mock_child.id = "03:999999"
    mock_child._parent = MagicMock()
    mock_child._parent.id = "02:888888"
    mock_child._SLUG = "CHI"
    mock_child._msg_value_code.return_value = None

    mock_dr = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr):
        mock_broker._update_device(mock_child)

        # Check that it used the parent for via_device
        call_args = mock_dr.async_get_or_create.call_args[1]
        assert call_args["via_device"] == (DOMAIN, "02:888888")


async def test_get_fan_param_generic_exception(mock_broker: RamsesBroker) -> None:
    """Test generic exception in async_get_fan_param (lines 1142+)."""
    call_data = {"device_id": "30:111111", "param_id": "0A", "from_id": "18:000000"}

    # Setup the entity with AsyncMock for the cleanup task
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()

    with (
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
        patch.object(mock_broker, "_find_param_entity", return_value=mock_entity),
        patch("custom_components.ramses_cc.broker.Command") as mock_cmd_cls,
        patch("custom_components.ramses_cc.broker._LOGGER.error") as mock_err,
    ):
        # Configure the side effect on the method, not the class constructor
        mock_cmd_cls.get_fan_param.side_effect = Exception("Unexpected Error")

        # Now we expect HomeAssistantError because broker wraps the generic exception
        with pytest.raises(HomeAssistantError, match="Failed to get fan parameter"):
            await mock_broker.async_get_fan_param(call_data)

        # Assert error was logged
        assert mock_err.called
        assert "Failed to get fan parameter" in mock_err.call_args[0][0]

        # Verify cleanup was called
        mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_set_fan_param_value_error_in_command(mock_broker: RamsesBroker) -> None:
    """Test ValueError raised during command creation in set_fan_param (line 1373)."""
    call_data = {
        "device_id": "30:111111",
        "param_id": "0A",
        "value": 1,
        "from_id": "18:000000",
    }

    with (
        patch.object(
            mock_broker,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
        patch("custom_components.ramses_cc.broker.Command") as mock_cmd,
    ):
        mock_cmd.set_fan_param.side_effect = ValueError("Value out of range")

        with pytest.raises(
            HomeAssistantError, match="Invalid parameter for set_fan_param"
        ):
            await mock_broker.async_set_fan_param(call_data)


async def test_cached_packets_filtering(mock_broker: RamsesBroker) -> None:
    """Test the packet caching logic in async_setup."""
    # Setup storage with valid, old, and invalid packets
    dt_now = dt.now()
    dt_old = dt_now - timedelta(days=2)
    valid_dt = dt_now.isoformat()
    old_dt = dt_old.isoformat()

    # Construct packet string that actually places 313F at index 41
    # 01234567890123456789012345678901234567890 (41 chars)
    padding = "X" * 41
    filtered_pkt = f"{padding}313F"
    filtered_dt = (dt_now - timedelta(minutes=1)).isoformat()

    # Mock store load
    mock_broker.store.async_load = AsyncMock(
        return_value={
            SZ_CLIENT_STATE: {
                SZ_PACKETS: {
                    valid_dt: "0000 000 000000 000000 000000 000000 0000 00",
                    old_dt: "0000 000 000000 000000 000000 000000 0000 00",
                    filtered_dt: filtered_pkt,
                    "invalid_dt": "...",
                },
                SZ_SCHEMA: {},
            }
        }
    )

    # Configure options
    mock_broker.options[CONF_RAMSES_RF] = {SZ_ENFORCE_KNOWN_LIST: False}

    # Mock client creation to avoid actual startup logic
    mock_broker._create_client = MagicMock()
    mock_client = AsyncMock()
    # Explicitly make start an AsyncMock so it can be awaited
    mock_client.start = AsyncMock()
    mock_broker._create_client.return_value = mock_client

    # IMPORTANT: Ensure self.client is None so logic tries to create a new one
    mock_broker.client = None

    await mock_broker.async_setup()

    # Verify client.start was called with filtered packets
    # Should include valid_dt, exclude old_dt and invalid_dt
    assert mock_client.start.called
    cached = mock_client.start.call_args[1]["cached_packets"]
    assert valid_dt in cached
    assert old_dt not in cached
    assert "invalid_dt" not in cached
    # The filtered packet should NOT be in cached because '313F' is in filter list
    assert filtered_dt not in cached


async def test_target_to_device_id_lists(
    mock_broker: RamsesBroker, hass: HomeAssistant
) -> None:
    """Test _target_to_device_id with lists of entity_ids and area_ids."""
    # Setup registry
    dr.async_get(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
    config_entry.add_to_hass(hass)

    # Create device 1 in area 1
    dev1 = dev_reg.async_get_or_create(
        config_entry_id="test", identifiers={(DOMAIN, "01:111111")}
    )
    dev_reg.async_update_device(dev1.id, area_id="area1")

    # Create device 2 with entity
    dev2 = dev_reg.async_get_or_create(
        config_entry_id="test", identifiers={(DOMAIN, "02:222222")}
    )
    ent2 = ent_reg.async_get_or_create(
        "sensor", DOMAIN, "sensor_dev2", device_id=dev2.id
    )

    # Test entity_id list
    target_ent = {"entity_id": [ent2.entity_id]}
    assert mock_broker._target_to_device_id(target_ent) == "02:222222"

    # Test area_id list
    target_area = {"area_id": ["area1"]}
    assert mock_broker._target_to_device_id(target_area) == "01:111111"


async def test_fan_bound_device_bad_config(mock_broker: RamsesBroker) -> None:
    """Test _setup_fan_bound_devices with invalid bound_to type."""
    mock_fan = MagicMock(spec=HvacVentilator)
    mock_fan.id = "30:111111"
    mock_fan.type = "FAN"

    # Setup known_list with bad type (int instead of str)
    mock_broker.options[SZ_KNOWN_LIST] = {"30:111111": {SZ_BOUND_TO: 12345}}

    with patch("custom_components.ramses_cc.broker._LOGGER.warning") as mock_warn:
        await mock_broker._setup_fan_bound_devices(mock_fan)
        assert mock_warn.called
        assert "invalid bound device id type" in mock_warn.call_args[0][0]


async def test_bind_device_generic_exception(mock_broker: RamsesBroker) -> None:
    """Test async_bind_device handles generic exceptions."""
    # We must mock _initiate_binding_process on the device object itself,
    # NOT on the client.fake_device method (which only raises LookupError).
    mock_device = MagicMock()
    mock_broker.client.fake_device.return_value = mock_device
    mock_device._initiate_binding_process = AsyncMock(
        side_effect=Exception("Surprise!")
    )

    call = MagicMock()
    # Provide device_info to avoid KeyError in early stages
    call.data = {
        "device_id": "01:123456",
        "offer": {},
        "confirm": {},
        "device_info": {},
    }

    with pytest.raises(HomeAssistantError, match="Unexpected error during binding"):
        await mock_broker.async_bind_device(call)


async def test_update_completed_dispatcher(
    mock_broker: RamsesBroker, hass: HomeAssistant
) -> None:
    """Test async_update sends signal at the end."""
    mock_broker.client.systems = []
    mock_broker.client.devices = []

    # Mock dispatcher send
    with patch("custom_components.ramses_cc.broker.async_dispatcher_send") as mock_send:
        await mock_broker.async_update()
        # Check last call was SIGNAL_UPDATE
        assert mock_send.call_args_list[-1][0][1] == SIGNAL_UPDATE


async def test_update_device_simple_device(mock_broker: RamsesBroker) -> None:
    """Test _update_device for a simple device (not Zone, not Child) sets via_device=None."""
    # A plain device (not Zone, not Child) should fall through to via_device = None
    mock_dev = MagicMock()
    mock_dev.id = "63:111111"
    mock_dev._SLUG = "SEN"
    mock_dev._msg_value_code.return_value = None

    mock_dr = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr):
        mock_broker._update_device(mock_dev)

        # Check that via_device is None
        call_args = mock_dr.async_get_or_create.call_args[1]
        assert call_args["via_device"] is None


async def test_run_fan_param_sequence_errors(mock_broker: RamsesBroker) -> None:
    """Test exception handlers in _async_run_fan_param_sequence loop."""
    # Patch the schema so the loop runs exactly twice
    # We patch the import in the BROKER module, not the test module
    with (
        patch("custom_components.ramses_cc.broker._2411_PARAMS_SCHEMA", ["0A", "0B"]),
        patch("custom_components.ramses_cc.broker._LOGGER.error") as mock_err,
    ):
        # Mock async_get_fan_param to raise errors
        # First call: HomeAssistantError
        # Second call: Generic Exception
        mock_broker.async_get_fan_param = AsyncMock(
            side_effect=[
                HomeAssistantError("Known error"),
                Exception("Unknown error"),
            ]
        )

        await mock_broker._async_run_fan_param_sequence({"device_id": "30:111111"})

        # Check that BOTH errors were logged (meaning the loop continued)
        assert mock_err.call_count == 2

        # Verify first error log args: (msg, param_id, error)
        # call_args_list[i][0] contains the positional args tuple
        args0 = mock_err.call_args_list[0][0]
        assert args0[0] == "Failed to get fan parameter %s for device: %s"
        assert args0[1] == "0A"

        # Verify second error log args
        args1 = mock_err.call_args_list[1][0]
        assert args1[0] == "Failed to get fan parameter %s for device: %s"
        assert args1[1] == "0B"


async def test_setup_schema_merge_failure(hass: HomeAssistant) -> None:
    """Test setup behavior when merged schema fails validation."""
    entry = MagicMock()
    entry.options = {
        "serial_port": "/dev/ttyUSB0",
        "packet_log": {},
        "ramses_rf": {},
        "known_list": {},
        "config_schema": {},
    }

    broker = RamsesBroker(hass, entry)

    # Mock store load to return a cached schema
    broker.store.async_load = AsyncMock(
        return_value={"client_state": {"schema": {"mock": "schema"}, "packets": {}}}
    )

    # Mock schema handling
    with (
        patch(
            "custom_components.ramses_cc.broker.merge_schemas",
            return_value={"merged": "schema"},
        ),
        patch(
            "custom_components.ramses_cc.broker.schema_is_minimal", return_value=True
        ),
        patch.object(broker, "_create_client") as mock_create_client,
        patch(
            "custom_components.ramses_cc.broker.extract_serial_port",
            return_value=("/dev/ttyUSB0", {}),
        ),
    ):
        # Setup the mock client to be awaitable
        mock_client = MagicMock()
        mock_client.start = AsyncMock()

        # First call fails (merged schema), second call succeeds (config schema)
        mock_create_client.side_effect = [
            vol.MultipleInvalid("Invalid schema"),
            mock_client,
        ]

        await broker.async_setup()

        # Verify _create_client was called twice (fallback occurred)
        assert mock_create_client.call_count == 2
        # Verify the client start was awaited
        mock_client.start.assert_awaited()


def test_get_device_returns_none(hass: HomeAssistant) -> None:
    """Test _get_device returns None when device not found and client not ready (Line 322)."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Ensure client is None (default behavior on init)
    broker.client = None
    broker._devices = []

    # Should hit the final return None
    assert broker._get_device("01:123456") is None


def test_update_device_relationships(hass: HomeAssistant) -> None:
    """Test _update_device for Child with Parent and generic Device."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    broker = RamsesBroker(hass, entry)

    # Mock Device Registry
    dev_reg = MagicMock()
    dev_reg.async_get_or_create = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg):
        # Case 1: Child Device with Parent
        parent = MagicMock(spec=System)
        parent.id = "01:123456"

        child_device = MagicMock(spec=Child)
        child_device.id = "04:123456"
        child_device._parent = parent
        child_device.name = "Test Child"
        child_device._msg_value_code.return_value = {"description": "Test Model"}

        broker._update_device(child_device)

        # Verify via_device is set to parent
        dev_reg.async_get_or_create.assert_called_with(
            config_entry_id="test_entry",
            identifiers={(DOMAIN, "04:123456")},
            name="Test Child",
            manufacturer=None,
            model="Test Model",
            via_device=(DOMAIN, "01:123456"),
            serial_number="04:123456",
        )

        # Case 2: Generic Device
        generic_device = MagicMock(spec=Device)
        generic_device.id = "18:000000"
        generic_device.name = "HGI"
        generic_device._SLUG = "HGI"
        # Explicitly set _parent to None to avoid AttributeError if strict spec is used
        generic_device._parent = None
        generic_device._msg_value_code.return_value = None

        # Reset mock
        broker._device_info = {}

        broker._update_device(generic_device)

        # Verify via_device is None
        args, kwargs = dev_reg.async_get_or_create.call_args
        assert kwargs["via_device"] is None


async def test_bind_device_lookup_error(hass: HomeAssistant) -> None:
    """Test async_bind_device raises HomeAssistantError on LookupError."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # Mock fake_device to raise LookupError
    broker.client.fake_device.side_effect = LookupError("Device not found")

    call = MagicMock()
    call.data = {"device_id": "99:999999"}

    with pytest.raises(HomeAssistantError, match="Device not found"):
        await broker.async_bind_device(call)


def test_find_param_entity_registry_miss(hass: HomeAssistant) -> None:
    """Test _find_param_entity when entity is in registry but not platform."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Mock Entity Registry to return an entry
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = MagicMock(device_id="device_id")

    # Mock Platforms (empty entities dict)
    platform = MagicMock()
    platform.entities = {}
    broker.platforms = {"number": [platform]}

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        entity = broker._find_param_entity("01:123456", "01")

        # Should return None and log the debug message
        assert entity is None


def test_resolve_device_id_edge_cases(hass: HomeAssistant) -> None:
    """Test _resolve_device_id with empty lists and lists of IDs."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Test 1: device_id is an empty list
    data: dict[str, Any] = {"device_id": []}
    assert broker._resolve_device_id(data) is None

    # Test 2: device (HA ID) is an empty list
    data = {"device": []}
    assert broker._resolve_device_id(data) is None

    # Test 3: device (HA ID) is a list with multiple items (Logs warning)
    # Mock _target_to_device_id to return something valid
    with patch.object(broker, "_target_to_device_id", return_value="18:123456"):
        # Explicitly annotate data as dict[str, Any] to avoid Mypy overlap error
        # when comparing data["device"] (initially list) with a string.
        data = {"device": ["ha_id_1", "ha_id_2"]}
        result = broker._resolve_device_id(data)
        assert result == "18:123456"
        assert data["device"] == "ha_id_1"  # Should be flattened

    # Test 4: Simple string ID (Line 1052)
    data_str = {"device_id": "01:123456"}
    assert broker._resolve_device_id(data_str) == "01:123456"

    # Test 5: Target dictionary (Lines 1075-1081)
    with patch.object(broker, "_target_to_device_id", return_value="02:222222"):
        data_target: dict[str, Any] = {"target": {"entity_id": "climate.test"}}
        assert broker._resolve_device_id(data_target) == "02:222222"
        assert data_target["device_id"] == "02:222222"

    # Test 6: No matching data (Line 1081)
    assert broker._resolve_device_id({}) is None


async def test_get_fan_param_no_source(hass: HomeAssistant) -> None:
    """Test get_fan_param returns early when from_id cannot be resolved."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # Mock a device that returns None for get_bound_rem()
    device = MagicMock()
    device.id = "32:123456"
    device.get_bound_rem.return_value = None
    broker._get_device = MagicMock(return_value=device)

    # Call without explicit from_id
    call = {"device_id": "32:123456", "param_id": "01"}

    # This should return None and log a warning, not raise
    await broker.async_get_fan_param(call)

    # Verify client.async_send_cmd was NOT called
    broker.client.async_send_cmd.assert_not_called()


async def test_get_fan_param_sets_pending(hass: HomeAssistant) -> None:
    """Test get_fan_param sets entity to pending state."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()
    broker.client.async_send_cmd = AsyncMock()

    # Setup happy path for IDs using valid RAMSES ID format (XX:YYYYYY)
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # Mock Entity - _clear_pending_after_timeout must be awaitable
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    call = {"device_id": "32:111111", "param_id": "01"}

    await broker.async_get_fan_param(call)

    # Verify set_pending was called
    mock_entity.set_pending.assert_called_once()
    # Verify cleanup was scheduled
    mock_entity._clear_pending_after_timeout.assert_called()


async def test_run_fan_param_sequence_dict_failure(hass: HomeAssistant) -> None:
    """Test _async_run_fan_param_sequence handles dict conversion failure."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Create an object that fails dict() conversion
    class BadData:
        def keys(self) -> None:
            raise ValueError("Boom")

    # Mock normalize to return bad data
    broker._normalize_service_call = MagicMock(return_value=BadData())

    await broker._async_run_fan_param_sequence({})

    # If it didn't raise, the exception was caught.
    # We can assume success if we reached here without crash.


async def test_set_fan_param_errors(hass: HomeAssistant) -> None:
    """Test set_fan_param error handling."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # 1. Missing Source (from_id)
    device = MagicMock()
    device.id = "32:123456"
    device.get_bound_rem.return_value = None
    broker._get_device = MagicMock(return_value=device)

    call = {"device_id": "32:123456", "param_id": "01", "value": 1}

    with pytest.raises(HomeAssistantError, match="Cannot set parameter"):
        await broker.async_set_fan_param(call)

    # 2. Generic Exception during send
    # Setup valid IDs
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )
    # Mock Send to raise generic Exception
    broker.client.async_send_cmd.side_effect = RuntimeError("Transport fail")

    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    # Patch Command.set_fan_param to skip validation for this test
    # This prevents 'value out of range' errors before we reach the send_cmd call
    with (
        patch(
            "custom_components.ramses_cc.broker.Command.set_fan_param",
            return_value="MOCK_CMD",
        ),
        pytest.raises(HomeAssistantError, match="Failed to set fan parameter"),
    ):
        await broker.async_set_fan_param(call)

    # Verify pending was cleared (Line 1391 context)
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


def test_update_device_already_registered(hass: HomeAssistant) -> None:
    """Test _update_device returns early if device is already registered (Line 678)."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    broker = RamsesBroker(hass, entry)

    # Mock Device Registry
    dev_reg = MagicMock()
    dev_reg.async_get_or_create = MagicMock()

    with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg):
        # Create a simple device mock
        device = MagicMock(spec=Device)
        device.id = "13:123456"
        device.name = "Test Device"
        device._SLUG = "BDR"
        device._msg_value_code.return_value = None
        # Ensure it doesn't trigger Child/Zone logic for via_device
        device._parent = None

        # First call - should register the device
        broker._update_device(device)
        assert dev_reg.async_get_or_create.call_count == 1

        # Check internal cache was updated
        assert "13:123456" in broker._device_info

        # Second call with identical state - should return early (hitting line 678)
        broker._update_device(device)

        # Call count should remain 1 (proving the early return worked)
        assert dev_reg.async_get_or_create.call_count == 1


def test_get_param_id_missing_param(hass: HomeAssistant) -> None:
    """Test _get_param_id raises ValueError when param_id is missing (Lines 964-965)."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Call with empty data -> Missing param_id
    with pytest.raises(
        ValueError, match=r"required key not provided @ data\['param_id'\]"
    ):
        broker._get_param_id({})


def test_resolve_device_id_from_ha_registry_id(hass: HomeAssistant) -> None:
    """Test _resolve_device_id resolves HA Registry ID to RAMSES ID."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Input data with an HA Device Registry ID (no colons/underscores)
    # Using hyphens instead of underscores to ensure we bypass the RAMSES ID check
    data = {"device_id": "ha-registry-uuid-123"}

    # Mock successful resolution
    # The code calls _target_to_device_id({"device_id": [device_id]})
    with patch.object(broker, "_target_to_device_id", return_value="18:999999"):
        result = broker._resolve_device_id(data)

        # Verify return value is the resolved RAMSES ID
        assert result == "18:999999"

        # Verify data dictionary was updated in place
        assert data["device_id"] == "18:999999"


def test_get_device_and_from_id_resolve_failure(hass: HomeAssistant) -> None:
    """Test _get_device_and_from_id returns empty tuple if resolution fails (Line ~1113)."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Mock _resolve_device_id to return None, forcing the code to hit 'return "", "", ""'
    with patch.object(broker, "_resolve_device_id", return_value=None):
        result = broker._get_device_and_from_id({})

        # Verify the "magic" empty tuple is returned
        assert result == ("", "", "")


def test_normalize_service_call_variants(hass: HomeAssistant) -> None:
    """Test _normalize_service_call with objects having .data, iterables, and targets."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # 1. Test object with 'data' attribute (Hits 'elif hasattr(call, "data")')
    class MockCall:
        data = {"key": "value_from_attr"}

    result_attr = broker._normalize_service_call(MockCall())
    assert result_attr == {"key": "value_from_attr"}

    # 2. Test iterable/list of tuples (Hits 'else: data = dict(call)')
    call_iterable = [("key", "value_from_iter")]
    result_iter = broker._normalize_service_call(call_iterable)
    assert result_iter == {"key": "value_from_iter"}

    # 3. Test object with target having .as_dict() (Hits 'if hasattr(target, "as_dict")')
    class MockTarget:
        def as_dict(self) -> dict[str, str]:
            return {"entity_id": "climate.test"}

    class MockCallWithTarget:
        data = {"key": "val"}
        target = MockTarget()

    result_target_method = broker._normalize_service_call(MockCallWithTarget())
    assert result_target_method["key"] == "val"
    assert result_target_method["target"] == {"entity_id": "climate.test"}

    # 4. Test object with target as dict (Hits 'elif isinstance(target, dict)')
    class MockCallWithDictTarget:
        data = {"key": "val"}
        target = {"area_id": "living_room"}

    result_target_dict = broker._normalize_service_call(MockCallWithDictTarget())
    assert result_target_dict["key"] == "val"
    assert result_target_dict["target"] == {"area_id": "living_room"}


async def test_get_fan_param_value_error_clears_pending(hass: HomeAssistant) -> None:
    """Test get_fan_param clears pending state when ValueError occurs after entity found."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # 1. Setup valid IDs to ensure we get past initial checks
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # 2. Setup Mock Entity with the required method
    mock_entity = MagicMock()
    # The method must be an AsyncMock so it can be awaited/scheduled
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch Command.get_fan_param to raise ValueError
    # This ensures 'entity' is already assigned before the exception is raised
    with patch(
        "custom_components.ramses_cc.broker.Command.get_fan_param",
        side_effect=ValueError("Simulated Error"),
    ):
        call = {"device_id": "32:111111", "param_id": "01"}

        await broker.async_get_fan_param(call)

    # 4. Verify _clear_pending_after_timeout(0) was called in the except block
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_run_fan_param_sequence_normalization_error(hass: HomeAssistant) -> None:
    """Test _async_run_fan_param_sequence handles exception during normalization."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Patch _normalize_service_call to raise an exception immediately
    with (
        patch.object(
            broker,
            "_normalize_service_call",
            side_effect=ValueError("Normalization failed"),
        ),
        patch("custom_components.ramses_cc.broker._LOGGER.error") as mock_error,
    ):
        await broker._async_run_fan_param_sequence({})

        # Verify the error was logged
        assert mock_error.called
        mock_error.assert_called_with("Invalid service call data: %s", ANY)
        # Verify the exception message was passed as the argument
        assert str(mock_error.call_args[0][1]) == "Normalization failed"


async def test_set_fan_param_value_error_clears_pending(hass: HomeAssistant) -> None:
    """Test set_fan_param clears pending state when ValueError occurs after entity found."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # 1. Setup valid IDs so execution proceeds past initial checks
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # 2. Setup Mock Entity with the required async method
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch Command.set_fan_param to raise ValueError
    # This ensures 'entity' is already assigned before the exception is raised
    with patch(
        "custom_components.ramses_cc.broker.Command.set_fan_param",
        side_effect=ValueError("Simulated Validation Error"),
    ):
        call = {"device_id": "32:111111", "param_id": "01", "value": 10}

        # The broker catches ValueError and re-raises it as HomeAssistantError
        with pytest.raises(
            HomeAssistantError, match="Invalid parameter for set_fan_param"
        ):
            await broker.async_set_fan_param(call)

    # 4. Verify _clear_pending_after_timeout(0) was called in the except block
    mock_entity._clear_pending_after_timeout.assert_called_with(0)

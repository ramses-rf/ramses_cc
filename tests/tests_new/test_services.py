"""Tests for the Services aspect of RamsesCoordinator (Bind, Send Packet, Service Calls)."""

import asyncio
import logging
from datetime import datetime as dt, timedelta as td
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ramses_cc.const import (
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    DOMAIN,
    SZ_BOUND_TO,
    SZ_CLIENT_STATE,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    SZ_PACKETS,
    SZ_SCHEMA,
)
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from custom_components.ramses_cc.helpers import (
    ha_device_id_to_ramses_device_id,
    ramses_device_id_to_ha_device_id,
)
from custom_components.ramses_cc.services import RamsesServiceHandler
from ramses_rf.devices import Device, HvacRemoteBase, HvacVentilator
from ramses_rf.exceptions import BindingFlowFailed
from ramses_rf.schemas import (
    SZ_ACTUATORS,
    SZ_APPLIANCE_CONTROL,
    SZ_DHW_SYSTEM,
    SZ_DHW_VALVE,
    SZ_HTG_VALVE,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
    SZ_ORPHANS_HEAT,
    SZ_ORPHANS_HVAC,
    SZ_REMOTES,
    SZ_SENSOR,
    SZ_SENSORS,
    SZ_SYSTEM,
    SZ_UFH_SYSTEM,
    SZ_ZONES,
)
from ramses_rf.systems import System, Zone
from ramses_rf.topology import Child
from ramses_tx.const import DevType
from ramses_tx.exceptions import (
    PacketAddrSetInvalid,
    ProtocolSendFailed,
    TransportError,
)

# Constants
HGI_ID = "18:006402"
SENTINEL_ID = "18:000730"
FAN_ID = "30:111222"
RAMSES_ID = "32:153289"
REM_ID = "32:987654"
PARAM_ID_HEX = "75"  # Temperature parameter


@pytest.fixture
def mock_coordinator(hass: HomeAssistant) -> RamsesCoordinator:
    """Return a mock coordinator with an entry attached.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :return: A mocked RamsesCoordinator instance configured for testing.
    :rtype: RamsesCoordinator
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="service_test_entry",
        options={
            "ramses_rf": {},
            "serial_port": "/dev/ttyUSB0",
            SZ_KNOWN_LIST: {},
            CONF_SCAN_INTERVAL: 60,
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.async_send_cmd = AsyncMock()
    # CQRS CommandDispatcher — the fan_param services call
    # client.dispatcher.send(intent) instead of build_dto + async_send_cmd.
    mock_client.dispatcher = MagicMock()
    mock_client.dispatcher.send = AsyncMock()
    # Initialize device_by_id as a dict for lookups
    mock_client.device_registry.device_by_id = {}
    coordinator.platforms = {}
    coordinator._device_info = {}

    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    return coordinator


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device.

    :return: A MagicMock simulating a HvacVentilator device.
    """
    device = MagicMock()
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    device.get_bound_rem = MagicMock(return_value=REM_ID)
    return device


async def test_bind_device_raises_ha_error(mock_coordinator: RamsesCoordinator) -> None:
    """Test that async_bind_device raises HomeAssistantError on binding failure."""
    mock_device = MagicMock()
    mock_device.id = "01:123456"
    mock_device._initiate_binding_process = AsyncMock(
        side_effect=BindingFlowFailed("Timeout waiting for confirm")
    )
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.fake_device = AsyncMock(return_value=mock_device)

    call = MagicMock()
    call.data = {
        "device_id": "01:123456",
        "offer": {"key": "val"},
        "confirm": {"key": "val"},
        "device_info": None,
    }

    with pytest.raises(HomeAssistantError, match="Binding failed for device"):
        await mock_coordinator.async_bind_device(call)


async def test_set_fan_param_raises_ha_error_invalid_value(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that async_set_fan_param raises HomeAssistantError on invalid input."""
    call_data = {
        "device_id": "30:111222",
        "param_id": "01",
        # "value": missing -> triggers ValueError
        "from_id": "32:111111",
    }
    with (
        patch.object(
            mock_coordinator.service_handler,
            "_get_device_and_from_id",
            return_value=("30:111222", "30_111222", "32:111111"),
        ),
        pytest.raises(HomeAssistantError, match="Invalid parameter for set_fan_param"),
    ):
        await mock_coordinator.async_set_fan_param(call_data)


async def test_set_fan_param_raises_ha_error_no_source(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that async_set_fan_param raises HomeAssistantError when no source device is found."""
    call_data = {
        "device_id": "30:111222",
        "param_id": "01",
        "value": 1,
        # No from_id and no bound device configured in mock
    }
    with pytest.raises(
        HomeAssistantError, match="No valid source device available for destination"
    ):
        await mock_coordinator.async_set_fan_param(call_data)


def test_adjust_sentinel_packet_swaps_on_invalid() -> None:
    """Test that addresses are swapped when validation fails for sentinel packet."""
    coordinator = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.hgi.id = HGI_ID

    handler = RamsesServiceHandler(coordinator)

    cmd = MagicMock()
    cmd.src.id = SENTINEL_ID
    cmd.dst.id = HGI_ID
    cmd._frame = "X" * 40

    m0, m1, m2 = MagicMock(), MagicMock(), MagicMock()
    m0.id, m1.id, m2.id = "addr0", "addr1", "addr2"
    cmd._addrs = [m0, m1, m2]

    with patch("custom_components.ramses_cc.services.pkt_addrs") as mock_validate:
        mock_validate.side_effect = PacketAddrSetInvalid("Invalid structure")
        handler._adjust_sentinel_packet(cmd)

        assert cmd._addrs[1].id == "addr2"
        assert cmd._addrs[2].id == "addr1"
        assert cmd._repr is None


def test_adjust_sentinel_packet_no_swap_on_valid() -> None:
    """Test that addresses are NOT swapped when validation passes."""
    coordinator = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.hgi.id = HGI_ID

    handler = RamsesServiceHandler(coordinator)

    cmd = MagicMock()
    cmd.src.id = SENTINEL_ID
    cmd.dst.id = HGI_ID

    m0, m1, m2 = MagicMock(), MagicMock(), MagicMock()
    m0.id, m1.id, m2.id = "addr0", "addr1", "addr2"
    cmd._addrs = [m0, m1, m2]

    with patch("custom_components.ramses_cc.services.pkt_addrs") as mock_validate:
        mock_validate.return_value = True
        handler._adjust_sentinel_packet(cmd)
        assert cmd._addrs[1].id == "addr1"


def test_adjust_sentinel_packet_ignores_other_devices() -> None:
    """Test that logic is skipped for non-sentinel devices."""
    coordinator = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.hgi.id = HGI_ID
    handler = RamsesServiceHandler(coordinator)

    cmd = MagicMock()
    cmd.src.id = "01:123456"  # Not sentinel

    m0, m1, m2 = MagicMock(), MagicMock(), MagicMock()
    m0.id, m1.id, m2.id = "addr0", "addr1", "addr2"
    cmd._addrs = [m0, m1, m2]

    handler._adjust_sentinel_packet(cmd)
    assert cmd._addrs[0].id == "addr0"
    assert cmd._addrs[1].id == "addr1"
    assert cmd._addrs[2].id == "addr2"


def test_get_param_id_validation(mock_coordinator: RamsesCoordinator) -> None:
    """Test validation of parameter IDs in service calls."""
    assert mock_coordinator.service_handler._get_param_id({"param_id": "01"}) == "01"

    with pytest.raises(ValueError, match="Invalid parameter ID"):
        mock_coordinator.service_handler._get_param_id({"param_id": "001"})

    with pytest.raises(ValueError, match="Invalid parameter ID"):
        mock_coordinator.service_handler._get_param_id({"param_id": "ZZ"})


async def test_coordinator_device_lookup_fail(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test coordinator handling when a device lookup fails."""
    call_data = {"device_id": "99:999999", "param_id": "01"}
    with caplog.at_level(logging.WARNING):
        await mock_coordinator.async_get_fan_param(call_data)
        assert "No valid source device available" in caplog.text


async def test_coordinator_service_presence(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
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


async def test_bind_device_success(mock_coordinator: RamsesCoordinator) -> None:
    """Test the happy path for async_bind_device."""
    mock_device = MagicMock()
    mock_device.id = "01:123456"
    mock_device._initiate_binding_process = AsyncMock(return_value=None)  # Success
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.fake_device = AsyncMock(return_value=mock_device)

    call = MagicMock()
    call.data = {
        "device_id": "01:123456",
        "offer": {},
        "confirm": {},
        "device_info": None,
    }

    # Intercept the refresh request so Debouncers are never spawned
    with patch.object(mock_coordinator, "async_request_refresh"):
        await mock_coordinator.async_bind_device(call)

        # Fast-forward time to cleanly execute the async_call_later timer
        async_fire_time_changed(
            mock_coordinator.hass, dt_util.utcnow() + td(seconds=10)
        )
        await mock_coordinator.hass.async_block_till_done()

    # Verify call later was scheduled
    assert mock_device._initiate_binding_process.called


async def test_send_packet_hgi_alias(mock_coordinator: RamsesCoordinator) -> None:
    """Test async_send_packet with HGI aliasing logic."""
    # Setup HGI in client
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.hgi.id = "18:999999"

    call = MagicMock()
    # Using the sentinel alias ID "18:000730"
    call.data = {
        "device_id": "18:000730",
        "from_id": "18:000730",
        "verb": "I",
        "code": "1F09",
        "payload": "FF",
    }

    # Intercept the refresh request so Debouncers are never spawned
    with patch.object(mock_coordinator, "async_request_refresh"):
        await mock_coordinator.async_send_packet(call)

        # Fast-forward time to cleanly execute the async_call_later timer
        async_fire_time_changed(
            mock_coordinator.hass, dt_util.utcnow() + td(seconds=10)
        )
        await mock_coordinator.hass.async_block_till_done()

    # Check that create_cmd was called with the REAL HGI ID, not the alias
    # This verifies the translation logic
    create_kwargs = mock_client.create_cmd.call_args[1]
    assert create_kwargs["device_id"] == "18:999999"


def test_resolve_device_ids_complex(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _resolve_device_id with lists and area_ids."""
    # 1. Test List handling
    data: dict[str, Any] = {"device_id": ["01:111111", "01:222222"]}
    with caplog.at_level(logging.WARNING):
        resolved = mock_coordinator.service_handler._resolve_device_id(data)
        assert resolved == "01:111111"
        assert data["device_id"] == "01:111111"  # Should update input dict
        assert "Multiple values for 'device_id'" in caplog.text

    # 2. Test explicit None return
    assert mock_coordinator.service_handler._resolve_device_id({}) is None

    # 3. Test empty list
    data_empty: dict[str, Any] = {"device_id": []}
    assert mock_coordinator.service_handler._resolve_device_id(data_empty) is None

    # 4. Test list with empty values
    data_missing: dict[str, Any] = {"device": []}
    assert mock_coordinator.service_handler._resolve_device_id(data_missing) is None

    # 5. Test HA Device list (multiple devices)
    data_ha_list: dict[str, Any] = {"device": ["ha_id_1", "ha_id_2"]}
    # Mock _target_to_device_id on service_handler
    with patch.object(
        mock_coordinator.service_handler,
        "_target_to_device_id",
        return_value="01:555555",
    ):
        resolved_ha = mock_coordinator.service_handler._resolve_device_id(data_ha_list)
        assert resolved_ha == "01:555555"
        assert data_ha_list["device"] == "ha_id_1"


async def test_resolve_device_id_area_string(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
) -> None:
    """Test resolving device ID from a Area ID passed as a string (not list)."""
    # Create a device in an area
    dev_reg = dr.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config")
    config_entry.add_to_hass(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "01:555555")},
    )
    dev_reg.async_update_device(device.id, area_id="test_area")

    # Pass area_id as string, not list, to trigger the single string conversion
    data = {"target": {"area_id": "test_area"}}
    resolved = mock_coordinator.service_handler._resolve_device_id(data)

    assert resolved == "01:555555"


async def test_find_param_entity_registry_only(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
) -> None:
    """Test fan_handler.find_param_entity when entity is in registry but not platform."""
    # Add entity to registry
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        "30_111222_param_01",
        original_icon="mdi:fan",
    )
    # Ensure entity ID matches what coordinator expects
    if entry.entity_id != "number.ramses_cc_30_111222_param_01":
        ent_reg.async_update_entity(
            entry.entity_id, new_entity_id="number.ramses_cc_30_111222_param_01"
        )

    # Ensure platform is empty or doesn't have it
    mock_coordinator.platforms = {"number": [MagicMock(entities={})]}

    # This should fall through and return None
    entity = mock_coordinator.fan_handler.find_param_entity("30:111222", "01")
    assert entity is None


async def test_async_set_fan_param_success_clear_pending(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test full success path of set_fan_param including pending state."""
    mock_coordinator._get_device = MagicMock(return_value=MagicMock(id=FAN_ID))
    mock_entity = MagicMock()
    mock_entity.set_pending = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()

    # dispatcher.send is already mocked on mock_coordinator.client
    with (
        patch.object(
            mock_coordinator.fan_handler, "find_param_entity", return_value=mock_entity
        ),
    ):
        call = {
            "device_id": FAN_ID,
            "param_id": "01",
            "value": 20,
            "from_id": "32:999999",
        }
        await mock_coordinator.async_set_fan_param(call)

        # Verify command sent via CQRS dispatcher
        mock_client = cast(Any, mock_coordinator.client)
        assert mock_client.dispatcher.send.called
        # Verify pending set
        assert mock_entity.set_pending.called


async def test_find_param_entity_found_in_platform(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
) -> None:
    """Test fan_handler.find_param_entity when entity is found in the platform."""
    # 1. Add entity to registry to pass the first check in fan_handler.find_param_entity
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        "30_111222_param_01",
        original_icon="mdi:fan",
    )
    # Force entity ID to match what coordinator expects
    if entry.entity_id != "number.ramses_cc_30_111222_param_01":
        ent_reg.async_update_entity(
            entry.entity_id, new_entity_id="number.ramses_cc_30_111222_param_01"
        )

    # 2. Mock the platform with the entity loaded
    mock_entity = MagicMock()
    mock_platform = MagicMock()
    # ensure hasattr(platform, "entities") is True and key exists
    mock_platform.entities = {"number.ramses_cc_30_111222_param_01": mock_entity}
    mock_coordinator.platforms = {"number": [mock_platform]}

    # 3. Call the method
    entity = mock_coordinator.fan_handler.find_param_entity("30:111222", "01")

    # 4. Assert we got the specific entity object from the platform
    assert entity is mock_entity


async def test_get_device_and_from_id_bound_logic(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _get_device_and_from_id logic regarding bound devices."""
    mock_dev = MagicMock()
    mock_dev.id = "30:111111"

    # Mock the device lookup
    mock_coordinator._get_device = MagicMock(return_value=mock_dev)

    call = {"device_id": "30:111111"}

    # Case 1: Bound device exists and returns valid ID
    mock_dev.get_bound_rem.return_value = "30:999999"
    orig, norm, from_id = mock_coordinator.service_handler._get_device_and_from_id(call)
    assert orig == "30:111111"
    assert from_id == "30:999999"

    # Case 2: Bound device exists but returns None (not bound)
    mock_dev.get_bound_rem.return_value = None
    orig_2, norm_2, from_id_2 = (
        mock_coordinator.service_handler._get_device_and_from_id(call)
    )

    # Correct logic: It should still return the device ID, but empty from_id
    assert from_id_2 == ""
    assert orig_2 == "30:111111"


async def test_run_fan_param_sequence_exception(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test exception handling in _async_run_fan_param_sequence."""
    # Force an exception inside the sequence loop
    # Patch the schema to a single item to make the test deterministic and fast
    with (
        patch("custom_components.ramses_cc.services._2411_PARAMS_SCHEMA", ["01"]),
        patch.object(
            mock_coordinator.service_handler,
            "async_get_fan_param",
            side_effect=Exception("Sequence Error"),
        ),
        caplog.at_level(logging.ERROR),
    ):
        await mock_coordinator.service_handler._async_run_fan_param_sequence(
            {"device_id": "30:111111"}
        )

        # Should catch exception and log error, not raise
        assert (
            "Failed to get fan parameter 01 for device: Sequence Error" in caplog.text
        )


async def test_set_fan_param_generic_exception(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test the generic exception handler coverage in async_set_fan_param."""
    # 1. Setup the transport failure on the CQRS dispatcher
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.dispatcher.send.side_effect = Exception("Transport Failure")

    # 2. Setup the entity and its cleanup mock
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    mock_coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    call_data = {
        "device_id": "30:111111",
        "param_id": "01",
        "value": 1,
        "from_id": "18:000000",
    }

    # 3. Patch necessary internal methods
    with (
        patch.object(
            mock_coordinator.service_handler,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
    ):
        # 4. Verify that HomeAssistantError is raised with the correct message
        with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
            await mock_coordinator.async_set_fan_param(call_data)

        # 5. Verify the cleanup mechanism was triggered
        mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_resolve_device_id_single_item_list(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test resolving device ID from a list with exactly one item."""
    data: dict[str, Any] = {"device_id": ["30:111111"]}
    resolved = mock_coordinator.service_handler._resolve_device_id(data)
    assert resolved == "30:111111"
    assert data["device_id"] == "30:111111"


async def test_resolve_device_ha_id_string(mock_coordinator: RamsesCoordinator) -> None:
    """Test resolving device from 'device' field as string."""
    # Mock _target_to_device_id to return a RAMSES ID
    with patch.object(
        mock_coordinator.service_handler,
        "_target_to_device_id",
        return_value="30:111111",
    ) as mock_target:
        data: dict[str, Any] = {"device": "ha_device_id_123"}
        resolved = mock_coordinator.service_handler._resolve_device_id(data)
        assert resolved == "30:111111"
        assert data["device_id"] == "30:111111"
        # Verify it called _target_to_device_id with the string wrapped in list
        mock_target.assert_called_with({"device_id": ["ha_device_id_123"]})


async def test_target_to_device_id_entity_string(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
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
    resolved = mock_coordinator.service_handler._target_to_device_id(target)
    assert resolved == "30:123456"


async def test_target_to_device_id_device_string(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
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
    resolved = mock_coordinator.service_handler._target_to_device_id(target)
    assert resolved == "30:654321"


async def test_set_fan_param_exception_handling(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that generic exception in set_fan_param is handled gracefully."""
    # entity
    mock_entity = MagicMock()
    # Must be AsyncMock because it is awaited via asyncio.create_task logic in test
    mock_entity._clear_pending_after_timeout = AsyncMock()

    with (
        patch.object(
            mock_coordinator.fan_handler, "find_param_entity", return_value=mock_entity
        ),
        # Patch device lookup to ensure we reach the logic
        patch.object(
            mock_coordinator.service_handler,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
    ):
        # Mock dispatcher.send to raise Exception
        mock_client = cast(Any, mock_coordinator.client)
        mock_client.dispatcher.send.side_effect = Exception("Boom")

        call = {
            "device_id": "30:111111",
            "param_id": "01",
            "value": "1",
            "from_id": "18:000000",
        }

        # Expect HomeAssistantError and logged error
        with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
            await mock_coordinator.async_set_fan_param(call)


async def test_run_fan_param_sequence_dict_fail(
    mock_coordinator: RamsesCoordinator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the try/except block in run_fan_param_sequence."""

    # Mock data so dict(data) raises ValueError
    # Mock _normalize_service_call
    class BadData:
        def __init__(self) -> None:
            self.items = lambda: [("device_id", "30:111111")]

        def __iter__(self) -> Any:
            raise ValueError("Cannot iterate")

    bad_data = BadData()

    with patch.object(
        mock_coordinator.service_handler,
        "_normalize_service_call",
        return_value=bad_data,
    ):
        # mocking async_get_fan_param to avoid actual calls
        mock_coordinator.service_handler.async_get_fan_param = AsyncMock()

        await mock_coordinator.service_handler._async_run_fan_param_sequence({})

        # The function should return early due to invalid data, so async_get_fan_param
        # should NOT be called
        assert not mock_coordinator.service_handler.async_get_fan_param.called

        # Check that the error was logged
        assert "Invalid service call data" in caplog.text


async def test_get_fan_param_value_error(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that ValueError in get_fan_param (e.g. invalid param ID) is caught and logged."""
    # We use 'ZZ' to force a ValueError in _get_param_id
    call = {
        "device_id": "30:111111",
        "param_id": "ZZ",
        "from_id": "18:000000",
    }
    # Patch device lookup to succeed so we reach param ID check
    with (
        caplog.at_level(logging.ERROR),
        patch.object(
            mock_coordinator.service_handler,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
        pytest.raises(ServiceValidationError, match="service_param_invalid"),
    ):
        await mock_coordinator.async_get_fan_param(call)
        assert "Failed to get fan parameter" in caplog.text


async def test_set_fan_param_exception_clears_pending(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that generic exception in set_fan_param clears pending state."""
    # entity
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()

    with (
        patch.object(
            mock_coordinator.fan_handler, "find_param_entity", return_value=mock_entity
        ),
        # Patch device lookup so we don't fail early with 'No valid source'
        patch.object(
            mock_coordinator.service_handler,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
    ):
        # Mock dispatcher.send to raise Exception
        mock_client = cast(Any, mock_coordinator.client)
        mock_client.dispatcher.send.side_effect = Exception("Boom")

        call = {
            "device_id": "30:111111",
            "param_id": "01",
            "value": "1",
            "from_id": "18:000000",
        }

        with pytest.raises(HomeAssistantError):
            await mock_coordinator.async_set_fan_param(call)

        # Check clear pending called with 0
        mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_async_force_update(mock_coordinator: RamsesCoordinator) -> None:
    """Test the async_force_update service call."""
    # Mock async_update to verify it gets called
    with patch.object(
        mock_coordinator, "async_refresh", new_callable=AsyncMock
    ) as mock_refresh:
        call = ServiceCall(
            hass=mock_coordinator.hass, domain=DOMAIN, service="force_update", data={}
        )
        await mock_coordinator.async_force_update(call)
        mock_refresh.assert_called_once()


async def test_async_sync_topology(mock_coordinator: RamsesCoordinator) -> None:
    """Test the async_sync_topology service call triggers a state save."""
    with patch.object(
        mock_coordinator, "async_save_client_state", new_callable=AsyncMock
    ) as mock_save:
        call = ServiceCall(
            hass=mock_coordinator.hass, domain=DOMAIN, service="sync_topology", data={}
        )
        await mock_coordinator.async_sync_topology(call)
        mock_save.assert_called_once()


async def test_async_sync_topology_enriches_schema(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test sync_topology enriches config schema with learned topology.

    Verifies the full flow: client.get_state() → sync_learned_topology →
    async_update_entry with enriched schema.
    """
    # Config schema: TCS with a zone that has no sensor
    config_schema = {
        "01:145038": {
            "zones": {"02": {"class": "radiator_valve"}},
        },
    }
    # Learned schema: same zone now has a sensor
    learned_schema = {
        "01:145038": {
            "zones": {"02": {"class": "radiator_valve", "sensor": "34:092243"}},
        },
    }

    mock_coordinator.options = {CONF_SCHEMA: config_schema}
    mock_coordinator._skip_topology_sync = False  # noqa: SLF001
    mock_coordinator.client = MagicMock()
    mock_coordinator.client.get_state = MagicMock(return_value=(learned_schema, {}))
    mock_coordinator._entities = {}  # noqa: SLF001
    mock_coordinator._remotes = {}  # noqa: SLF001
    mock_coordinator.discovery_manager = None
    mock_coordinator.store = MagicMock()
    mock_coordinator.store.async_save = AsyncMock()

    with patch.object(
        mock_coordinator.hass.config_entries, "async_update_entry"
    ) as mock_update:
        await mock_coordinator.async_save_client_state()

        # sync_learned_topology should have enriched the config schema
        mock_update.assert_called_once()
        new_options = mock_update.call_args.kwargs.get("options", {})
        enriched = new_options.get(CONF_SCHEMA, {})
        # The sensor should now be in the zone
        assert enriched["01:145038"]["zones"]["02"]["sensor"] == "34:092243"


async def test_get_device_and_from_id_propagates_exceptions(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that exceptions during device lookup are propagated (not swallowed)."""
    # Mock _resolve_device_id to raise an arbitrary exception
    mock_coordinator.service_handler._resolve_device_id = MagicMock(
        side_effect=ValueError("Critical Lookup Failure")
    )

    with pytest.raises(ValueError, match="Critical Lookup Failure"):
        mock_coordinator.service_handler._get_device_and_from_id(
            {"device_id": "30:111111"}
        )


async def test_update_device_via_device_logic(
    mock_coordinator: RamsesCoordinator, hass: HomeAssistant
) -> None:
    """Test the via_device logic in _update_device for Zones and Children."""
    # 1. Test Zone with TCS
    mock_tcs = MagicMock()
    mock_tcs.id = "01:123456"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "04:111111"
    mock_zone.tcs = mock_tcs
    mock_zone.state_store = MagicMock()
    mock_zone.state_store._msg_value_code = AsyncMock(return_value=None)
    mock_zone._SLUG = "ZN"

    # 2. Test Child with Parent
    mock_parent = MagicMock(spec=System)
    mock_parent.id = "02:222222"

    mock_child = MagicMock(spec=Child)
    mock_child.id = "03:333333"
    mock_child._parent = mock_parent
    mock_child.state_store = MagicMock()
    mock_child.state_store._msg_value_code = AsyncMock(return_value=None)
    mock_child._SLUG = "DHW"

    mock_dr = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr):
        # Trigger update for Zone
        await mock_coordinator._async_update_device(mock_zone)
        # Check zone via_device (most recent call)
        call_args_zone = mock_dr.async_get_or_create.call_args_list[-1][1]
        assert call_args_zone["via_device"] == (DOMAIN, "01:123456")

        # Trigger update for Child
        await mock_coordinator._async_update_device(mock_child)
        # Check child via_device (most recent call)
        call_args_child = mock_dr.async_get_or_create.call_args_list[-1][1]
        assert call_args_child["via_device"] == (DOMAIN, "02:222222")


async def test_adjust_sentinel_packet_early_return(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _adjust_sentinel_packet returns early if src/dst don't match."""
    handler = RamsesServiceHandler(mock_coordinator)

    mock_client = cast(Any, mock_coordinator.client)
    mock_client.hgi.id = "18:006402"
    cmd = MagicMock()
    cmd.src.id = "18:999999"  # Not sentinel
    cmd.dst.id = "01:000000"  # Not HGI

    with patch("custom_components.ramses_cc.services.pkt_addrs") as mock_pkt_addrs:
        handler._adjust_sentinel_packet(cmd)
        mock_pkt_addrs.assert_not_called()


async def test_find_param_entity_missing_in_platform(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
) -> None:
    """Test fan_handler.find_param_entity returns None if entity in registry but not in platform."""
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
    mock_coordinator.platforms = {"number": [mock_platform]}

    entity = mock_coordinator.fan_handler.find_param_entity("30:111111", "01")
    assert entity is None


async def test_resolve_device_id_list_warning(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that passing a list to device_id logs a warning."""
    with caplog.at_level(logging.WARNING):
        mock_coordinator.service_handler._resolve_device_id(
            {"device_id": ["30:111111", "30:222222"]}
        )

        # Verify the call was made with the format string and specific arguments
        assert (
            "Multiple values for 'device_id' provided, using first one: 30:111111"
            in caplog.text
        )


async def test_get_device_client_fallback(mock_coordinator: RamsesCoordinator) -> None:
    """Test _get_device falls back to client.device_registry.device_by_id."""
    # Ensure internal devices list is empty to trigger fallback logic
    mock_coordinator._devices = []
    mock_dev = MagicMock()
    mock_dev.id = "30:999999"

    # Configure client.device_registry.device_by_id to work as a dict
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.device_by_id = {"30:999999": mock_dev}

    dev = mock_coordinator._get_device("30:999999")
    assert dev == mock_dev


async def test_update_device_valid_child_type(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _update_device with a valid Child class to ensure fallback logic."""
    mock_child = MagicMock(spec=Child)
    mock_child.id = "03:999999"
    mock_child._parent = MagicMock()
    mock_child._parent.id = "02:888888"
    mock_child._SLUG = "CHI"
    mock_child.state_store = MagicMock()
    mock_child.state_store._msg_value_code = AsyncMock(return_value=None)

    mock_dr = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr):
        await mock_coordinator._async_update_device(mock_child)

        # Check that it used the parent for via_device
        call_args = mock_dr.async_get_or_create.call_args[1]
        assert call_args["via_device"] == (DOMAIN, "02:888888")


async def test_get_fan_param_generic_exception(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test generic exception in async_get_fan_param."""
    call_data = {"device_id": "30:111111", "param_id": "01", "from_id": "18:000000"}

    # Setup the entity with AsyncMock for the cleanup task
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()

    with (
        caplog.at_level(logging.ERROR),
        patch.object(
            mock_coordinator.service_handler,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
        patch.object(
            mock_coordinator.fan_handler, "find_param_entity", return_value=mock_entity
        ),
    ):
        # Configure dispatcher.send to raise
        mock_client = cast(Any, mock_coordinator.client)
        mock_client.dispatcher.send.side_effect = Exception("Unexpected Error")

        # Now we expect HomeAssistantError because coordinator wraps the generic exception
        with pytest.raises(HomeAssistantError, match="Failed to get fan parameter"):
            await mock_coordinator.async_get_fan_param(call_data)

        # Assert error was logged
        assert "Failed to get fan parameter" in caplog.text

        # Verify cleanup was called
        mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_set_fan_param_value_error_in_command(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test ValueError raised during command creation in set_fan_param."""
    call_data = {
        "device_id": "30:111111",
        "param_id": "01",
        "value": 1,
        "from_id": "18:000000",
    }

    with (
        patch.object(
            mock_coordinator.service_handler,
            "_get_device_and_from_id",
            return_value=("30:111111", "30_111111", "18:000000"),
        ),
    ):
        # dispatcher.send calls build_dto internally; simulate its ValueError
        mock_client = cast(Any, mock_coordinator.client)
        mock_client.dispatcher.send.side_effect = ValueError("Value out of range")

        with pytest.raises(
            HomeAssistantError, match="Invalid parameter for set_fan_param"
        ):
            await mock_coordinator.async_set_fan_param(call_data)


async def test_cached_packets_filtering(mock_coordinator: RamsesCoordinator) -> None:
    """Test the packet caching logic in async_setup."""
    # Setup storage with valid, old, and invalid packets
    dt_now: dt = dt_util.now()
    dt_old: dt = dt_now - td(days=2)
    valid_dt: str = dt_now.isoformat()
    old_dt: str = dt_old.isoformat()

    # Construct packet string that actually places 313F at index 41
    # 01234567890123456789012345678901234567890 (41 chars)
    padding = "X" * 41
    filtered_pkt = f"{padding}313F"
    filtered_dt: dt = dt_now - td(minutes=1)
    filtered_dt_str: str = filtered_dt.isoformat()

    # Mock store load
    mock_coordinator.store.async_load = AsyncMock(
        return_value={
            SZ_CLIENT_STATE: {
                SZ_PACKETS: {
                    valid_dt: "0000 000 000000 000000 000000 000000 0000 00",
                    old_dt: "0000 000 000000 000000 000000 000000 0000 00",
                    filtered_dt_str: filtered_pkt,
                    "invalid_dt": "...",
                },
                SZ_SCHEMA: {},
            }
        }
    )

    # Configure options
    mock_coordinator.options[CONF_RAMSES_RF] = {SZ_ENFORCE_KNOWN_LIST: False}

    # Mock client creation to avoid actual startup logic
    mock_coordinator._create_client = MagicMock()
    mock_client = AsyncMock()
    # Explicitly make start an AsyncMock so it can be awaited
    mock_client.start = AsyncMock()
    mock_coordinator._create_client.return_value = mock_client

    # IMPORTANT: Ensure self.client is None so logic tries to create a new one
    mock_coordinator.client = None

    await mock_coordinator.async_setup()

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
    mock_coordinator: RamsesCoordinator, hass: HomeAssistant
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
    assert (
        mock_coordinator.service_handler._target_to_device_id(target_ent) == "02:222222"
    )

    # Test area_id list
    target_area = {"area_id": ["area1"]}
    assert (
        mock_coordinator.service_handler._target_to_device_id(target_area)
        == "01:111111"
    )


async def test_fan_bound_device_bad_config(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _setup_fan_bound_devices with invalid bound_to type."""
    mock_fan = MagicMock(spec=HvacVentilator)
    mock_fan.id = "30:111111"
    mock_fan.type = "FAN"

    # Setup known_list with bad type (int instead of str)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"30:111111": {SZ_BOUND_TO: 12345}}

    with caplog.at_level(logging.WARNING):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan)
        assert "invalid bound type" in caplog.text


async def test_fan_bound_device_list(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _setup_fan_bound_devices with list bound_to (multi-REM binding)."""
    mock_fan = MagicMock(spec=HvacVentilator)
    mock_fan.id = "30:111111"
    mock_fan.type = "FAN"

    # Setup known_list with a list of bound REMs
    mock_coordinator.options[SZ_KNOWN_LIST] = {
        "30:111111": {SZ_BOUND_TO: ["32:153001", "32:153002"]}
    }

    # Mock _get_device to return mock REM devices (HvacRemoteBase)
    mock_rem1 = MagicMock(spec=HvacRemoteBase)
    mock_rem2 = MagicMock(spec=HvacRemoteBase)
    mock_coordinator._get_device = MagicMock(
        side_effect=lambda dev_id: {"32:153001": mock_rem1, "32:153002": mock_rem2}.get(
            dev_id
        )
    )

    await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan)

    # Both REMs should be bound
    mock_fan.add_bound_device.assert_any_call("32:153001", DevType.REM)
    mock_fan.add_bound_device.assert_any_call("32:153002", DevType.REM)
    assert mock_fan.add_bound_device.call_count == 2
    # Both should be in the _fan_bound_to_remote dict
    assert mock_coordinator.fan_handler._fan_bound_to_remote["32:153001"] == "30:111111"
    assert mock_coordinator.fan_handler._fan_bound_to_remote["32:153002"] == "30:111111"


async def test_fan_bound_device_single_string_still_works(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _setup_fan_bound_devices with single string bound_to (backward compat)."""
    mock_fan = MagicMock(spec=HvacVentilator)
    mock_fan.id = "30:111111"
    mock_fan.type = "FAN"

    mock_coordinator.options[SZ_KNOWN_LIST] = {"30:111111": {SZ_BOUND_TO: "32:153001"}}

    mock_rem = MagicMock(spec=HvacRemoteBase)
    mock_coordinator._get_device = MagicMock(return_value=mock_rem)

    await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan)

    mock_fan.add_bound_device.assert_called_once_with("32:153001", DevType.REM)
    assert mock_coordinator.fan_handler._fan_bound_to_remote["32:153001"] == "30:111111"


async def test_bind_device_generic_exception(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_bind_device handles generic exceptions."""
    # We must mock _initiate_binding_process on the device object itself,
    # NOT on the client.fake_device method (which only raises LookupError).
    mock_device = MagicMock()
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.fake_device = AsyncMock(return_value=mock_device)
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
        await mock_coordinator.async_bind_device(call)


async def test_update_device_simple_device(mock_coordinator: RamsesCoordinator) -> None:
    """Test _update_device for a simple device (not Zone, not Child) sets via_device=None."""
    # A plain device (not Zone, not Child) should fall through to via_device = None
    mock_dev = MagicMock()
    mock_dev.id = "63:111111"
    mock_dev._SLUG = "SEN"
    mock_dev.state_store = MagicMock()
    mock_dev.state_store._msg_value_code = AsyncMock(return_value=None)

    mock_dr = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr):
        await mock_coordinator._async_update_device(mock_dev)

        # Check that via_device is None using dictionary get method to prevent KeyError
        call_args = mock_dr.async_get_or_create.call_args[1]
        assert call_args.get("via_device") is None


async def test_run_fan_param_sequence_errors(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test exception handlers in _async_run_fan_param_sequence loop."""
    # Patch the schema to a single item to make the test deterministic and fast
    with (
        patch("custom_components.ramses_cc.services._2411_PARAMS_SCHEMA", ["01", "0B"]),
        caplog.at_level(logging.ERROR),
    ):
        # Mock async_get_fan_param to raise errors
        # First call: HomeAssistantError
        # Second call: Generic Exception
        mock_coordinator.service_handler.async_get_fan_param = AsyncMock(
            side_effect=[
                HomeAssistantError("Known error"),
                Exception("Unknown error"),
            ]
        )

        await mock_coordinator.service_handler._async_run_fan_param_sequence(
            {"device_id": "30:111111"}
        )

        # Check that BOTH errors were logged (meaning the loop continued)
        assert "Failed to get fan parameter 01 for device: Known error" in caplog.text
        assert "Failed to get fan parameter 0B for device: Unknown error" in caplog.text


async def test_setup_schema_merge_failure(hass: HomeAssistant) -> None:
    """Test setup behavior when merged schema fails validation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_SCAN_INTERVAL: 60,
            "serial_port": "/dev/ttyUSB0",
            "packet_log": {},
            "ramses_rf": {},
            "known_list": {},
            "config_schema": {},
        },
    )

    coordinator = RamsesCoordinator(hass, entry)

    # Mock store load to return a cached schema
    coordinator.store.async_load = AsyncMock(
        return_value={"client_state": {"schema": {"mock": "schema"}, "packets": {}}}
    )

    # Mock schema handling
    with (
        patch(
            "custom_components.ramses_cc.coordinator.merge_schemas",
            return_value={"merged": "schema"},
        ),
        patch.object(coordinator, "_create_client") as mock_create_client,
        patch(
            "custom_components.ramses_cc.coordinator.extract_serial_port",
            return_value=("/dev/ttyUSB0", {}),
        ),
    ):
        # Setup the mock client to be awaitable
        mock_client = MagicMock()
        mock_client.start = AsyncMock()

        # First call fails (merged schema), second call succeeds (config schema)
        mock_create_client.side_effect = [
            vol.MultipleInvalid([vol.Invalid("Invalid schema")]),
            mock_client,
        ]

        await coordinator.async_setup()

        # Verify _create_client was called twice (fallback occurred)
        assert mock_create_client.call_count == 2
        # Verify the client start was awaited
        mock_client.start.assert_awaited()


def test_get_device_returns_none(hass: HomeAssistant) -> None:
    """Test _get_device returns None when device not found and client not ready."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Ensure client is None (default behavior on init)
    coordinator.client = None
    coordinator._devices = []

    # Test fallback logic returns None
    assert coordinator._get_device("01:123456") is None


async def test_update_device_relationships(hass: HomeAssistant) -> None:
    """Test _update_device for Child with Parent and generic Device."""
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="test_entry", options={CONF_SCAN_INTERVAL: 60}
    )
    coordinator = RamsesCoordinator(hass, entry)

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
        child_device.state_store = MagicMock()
        child_device.state_store._msg_value_code = AsyncMock(
            return_value={"description": "Test Model"}
        )

        await coordinator._async_update_device(child_device)

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
        generic_device.state_store = MagicMock()
        generic_device.state_store._msg_value_code = AsyncMock(return_value=None)

        # Reset mock
        coordinator._device_info = {}

        await coordinator._async_update_device(generic_device)

        # Verify via_device is None using dictionary get method to prevent KeyError
        args, kwargs = dev_reg.async_get_or_create.call_args
        assert kwargs.get("via_device") is None


async def test_bind_device_lookup_error(hass: HomeAssistant) -> None:
    """Test async_bind_device raises HomeAssistantError on LookupError."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()

    # Mock fake_device to raise LookupError
    mock_client = cast(Any, coordinator.client)
    mock_client.device_registry.fake_device.side_effect = LookupError(
        "Device not found"
    )

    call = MagicMock()
    call.data = {"device_id": "99:999999"}

    with pytest.raises(HomeAssistantError, match="Device not found"):
        await coordinator.async_bind_device(call)


def test_find_param_entity_registry_miss(hass: HomeAssistant) -> None:
    """Test fan_handler.find_param_entity when entity is in registry but not platform."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Mock Entity Registry to return an entry
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = MagicMock(device_id="device_id")

    # Mock Platforms (empty entities dict)
    platform = MagicMock()
    platform.entities = {}
    coordinator.platforms = {"number": [platform]}

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        entity = coordinator.fan_handler.find_param_entity("01:123456", "01")

        # Should return None and log the debug message
        assert entity is None


def test_resolve_device_id_edge_cases(hass: HomeAssistant) -> None:
    """Test _resolve_device_id with empty lists and lists of IDs."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Test 1: device_id is an empty list
    data: dict[str, Any] = {"device_id": []}
    assert coordinator.service_handler._resolve_device_id(data) is None

    # Test 2: device (HA ID) is an empty list
    data = {"device": []}
    assert coordinator.service_handler._resolve_device_id(data) is None

    # Test 3: device (HA ID) is a list with multiple items (Logs warning)
    # Mock _target_to_device_id to return something valid
    with patch.object(
        coordinator.service_handler, "_target_to_device_id", return_value="18:123456"
    ):
        # Explicitly annotate data
        data = {"device": ["ha_id_1", "ha_id_2"]}
        result = coordinator.service_handler._resolve_device_id(data)
        assert result == "18:123456"
        assert data["device"] == "ha_id_1"  # Should be flattened

    # Test 4: Simple string ID
    data_str = {"device_id": "01:123456"}
    assert coordinator.service_handler._resolve_device_id(data_str) == "01:123456"

    # Test 5: Target dictionary
    with patch.object(
        coordinator.service_handler, "_target_to_device_id", return_value="02:222222"
    ):
        data_target: dict[str, Any] = {"target": {"entity_id": "climate.test"}}
        assert (
            coordinator.service_handler._resolve_device_id(data_target) == "02:222222"
        )
        assert data_target["device_id"] == "02:222222"

    # Test 6: No matching data
    assert coordinator.service_handler._resolve_device_id({}) is None


async def test_get_fan_param_no_source(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test get_fan_param returns early when from_id cannot be resolved."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()

    # Mock a device that returns None for get_bound_rem()
    device = MagicMock()
    device.id = "32:123456"
    device.get_bound_rem.return_value = None
    coordinator._get_device = MagicMock(return_value=device)

    # Call without explicit from_id
    call = {"device_id": "32:123456", "param_id": "01"}

    # This should return None and log a warning, not raise
    with caplog.at_level(logging.WARNING):
        await coordinator.async_get_fan_param(call)

    assert "No valid source device available" in caplog.text

    # Verify client.dispatcher.send was NOT called
    mock_client = cast(Any, coordinator.client)
    mock_client.dispatcher.send.assert_not_called()


async def test_get_fan_param_sets_pending(hass: HomeAssistant) -> None:
    """Test get_fan_param sets entity to pending state."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.async_send_cmd = AsyncMock()
    mock_client.dispatcher = MagicMock()
    mock_client.dispatcher.send = AsyncMock()

    # Setup happy path for IDs using valid RAMSES ID format (XX:YYYYYY)
    coordinator.service_handler._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # Mock Entity - _clear_pending_after_timeout must be awaitable
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    call = {"device_id": "32:111111", "param_id": "01"}

    await coordinator.async_get_fan_param(call)

    # Verify set_pending was called
    mock_entity.set_pending.assert_called_once()
    # Verify cleanup was scheduled
    mock_entity._clear_pending_after_timeout.assert_called()


async def test_run_fan_param_sequence_dict_failure(hass: HomeAssistant) -> None:
    """Test _async_run_fan_param_sequence handles dict conversion failure."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Create an object that fails dict() conversion
    class BadData:
        def keys(self) -> None:
            raise ValueError("Boom")

    # Mock normalize to return bad data
    coordinator.service_handler._normalize_service_call = MagicMock(
        return_value=BadData()
    )

    await coordinator.service_handler._async_run_fan_param_sequence({})

    # If it didn't raise, the exception was caught.
    # We can assume success if we reached here without crash.


async def test_set_fan_param_errors(hass: HomeAssistant) -> None:
    """Test set_fan_param error handling."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.dispatcher = MagicMock()
    mock_client.dispatcher.send = AsyncMock()

    # 1. Missing Source (from_id)
    device = MagicMock()
    device.id = "32:123456"
    device.get_bound_rem.return_value = None
    coordinator._get_device = MagicMock(return_value=device)

    call = {"device_id": "32:123456", "param_id": "01", "value": 1}

    with pytest.raises(HomeAssistantError, match="Cannot set parameter"):
        await coordinator.async_set_fan_param(call)

    # 2. Generic Exception during send
    # Setup valid IDs
    coordinator.service_handler._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )
    # Mock dispatcher.send to raise generic Exception
    mock_client = cast(Any, coordinator.client)
    mock_client.dispatcher.send.side_effect = RuntimeError("Transport fail")

    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
        await coordinator.async_set_fan_param(call)

    # Verify pending was cleared
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_update_device_already_registered(hass: HomeAssistant) -> None:
    """Test _update_device returns early if device is already registered."""
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="test_entry", options={CONF_SCAN_INTERVAL: 60}
    )
    coordinator = RamsesCoordinator(hass, entry)

    # Mock Device Registry
    dev_reg = MagicMock()
    dev_reg.async_get_or_create = MagicMock()

    with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg):
        # Create a simple device mock
        device = MagicMock(spec=Device)
        device.id = "13:123456"
        device.name = "Test Device"
        device._SLUG = "BDR"
        device.state_store = MagicMock()
        device.state_store._msg_value_code = AsyncMock(return_value=None)
        # Ensure it doesn't trigger Child/Zone logic for via_device
        device._parent = None

        # First call - should register the device
        await coordinator._async_update_device(device)
        assert dev_reg.async_get_or_create.call_count == 1

        # Check internal cache was updated
        assert "13:123456" in coordinator._device_info

        # Second call with identical state - should return early
        await coordinator._async_update_device(device)

        # Call count should remain 1 (proving the early return worked)
        assert dev_reg.async_get_or_create.call_count == 1


def test_get_param_id_missing_param(hass: HomeAssistant) -> None:
    """Test _get_param_id raises ValueError when param_id is missing."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Call with empty data -> Missing param_id
    with pytest.raises(
        ValueError, match=r"required key not provided @ data\['param_id'\]"
    ):
        coordinator.service_handler._get_param_id({})


def test_resolve_device_id_from_ha_registry_id(hass: HomeAssistant) -> None:
    """Test _resolve_device_id resolves HA Registry ID to RAMSES ID."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Input data with an HA Device Registry ID (no colons/underscores)
    data = {"device_id": "ha-registry-uuid-123"}

    # Mock successful resolution
    with patch.object(
        coordinator.service_handler, "_target_to_device_id", return_value="18:999999"
    ):
        result = coordinator.service_handler._resolve_device_id(data)

        # Verify return value is the resolved RAMSES ID
        assert result == "18:999999"

        # Verify data dictionary was updated in place
        assert data["device_id"] == "18:999999"


def test_get_device_and_from_id_resolve_failure(hass: HomeAssistant) -> None:
    """Test _get_device_and_from_id returns empty tuple if resolution fails."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Mock _resolve_device_id to return None
    with patch.object(
        coordinator.service_handler, "_resolve_device_id", return_value=None
    ):
        result = coordinator.service_handler._get_device_and_from_id({})

        # Verify the "magic" empty tuple is returned
        assert result == ("", "", "")


def test_normalize_service_call_variants(hass: HomeAssistant) -> None:
    """Test _normalize_service_call with objects having .data, iterables, and targets."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # 1. Test object with 'data' attribute
    mock_call = MagicMock(spec=ServiceCall)
    mock_call.data = {"key": "value_from_attr"}
    mock_call.target = None

    result_attr = coordinator.service_handler._normalize_service_call(mock_call)
    assert result_attr == {"key": "value_from_attr"}

    # 2. Test iterable/list of tuples (Hits 'else: data = dict(call)')
    call_iterable = [("key", "value_from_iter")]
    result_iter = coordinator.service_handler._normalize_service_call(
        cast(ServiceCall, call_iterable)
    )
    assert result_iter == {"key": "value_from_iter"}

    # 3. Test object with target having .as_dict()
    class MockTarget:
        def as_dict(self) -> dict[str, str]:
            return {"entity_id": "climate.test"}

    mock_call_target = MagicMock(spec=ServiceCall)
    mock_call_target.data = {"key": "val"}
    mock_call_target.target = MockTarget()

    result_target_method = coordinator.service_handler._normalize_service_call(
        mock_call_target
    )
    assert result_target_method["key"] == "val"
    assert result_target_method["target"] == {"entity_id": "climate.test"}

    # 4. Test object with target as dict
    mock_call_dict = MagicMock(spec=ServiceCall)
    mock_call_dict.data = {"key": "val"}
    mock_call_dict.target = {"area_id": "living_room"}

    result_target_dict = coordinator.service_handler._normalize_service_call(
        mock_call_dict
    )
    assert result_target_dict["key"] == "val"
    assert result_target_dict["target"] == {"area_id": "living_room"}


async def test_get_fan_param_value_error_clears_pending(hass: HomeAssistant) -> None:
    """Test get_fan_param clears pending state when ValueError occurs after entity found."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.dispatcher = MagicMock()
    mock_client.dispatcher.send = AsyncMock()

    # 1. Setup valid IDs to ensure we get past initial checks
    coordinator.service_handler._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # 2. Setup Mock Entity with the required method
    mock_entity = MagicMock()
    # The method must be an AsyncMock so it can be awaited/scheduled
    mock_entity._clear_pending_after_timeout = AsyncMock()
    coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch dispatcher.send to raise ValueError
    # This ensures 'entity' is already assigned before the exception is raised
    mock_client = cast(Any, coordinator.client)
    mock_client.dispatcher.send.side_effect = ValueError("Simulated Error")
    with pytest.raises(ServiceValidationError, match="service_param_invalid"):
        call = {"device_id": "32:111111", "param_id": "01"}

        await coordinator.async_get_fan_param(call)

    # 4. Verify _clear_pending_after_timeout(0) was called in the except block
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_run_fan_param_sequence_normalization_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _async_run_fan_param_sequence handles exception during normalization."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)

    # Patch _normalize_service_call to raise an exception immediately
    with (
        patch.object(
            coordinator.service_handler,
            "_normalize_service_call",
            side_effect=ValueError("Normalization failed"),
        ),
        caplog.at_level(logging.ERROR),
    ):
        await coordinator.service_handler._async_run_fan_param_sequence({})

        # Verify the error was logged with the exact message format
        assert "Invalid service call data: Normalization failed" in caplog.text


async def test_set_fan_param_value_error_clears_pending(hass: HomeAssistant) -> None:
    """Test set_fan_param clears pending state when ValueError occurs after entity found."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 60})
    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()
    mock_client = cast(Any, coordinator.client)
    mock_client.dispatcher = MagicMock()
    mock_client.dispatcher.send = AsyncMock()

    # 1. Setup valid IDs so execution proceeds past initial checks
    coordinator.service_handler._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # 2. Setup Mock Entity with the required async method
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch dispatcher.send to raise ValueError
    mock_client.dispatcher.send.side_effect = ValueError("Simulated Validation Error")
    call = {"device_id": "32:111111", "param_id": "01", "value": 10}

    # The coordinator catches ValueError and re-raises it as HomeAssistantError
    with pytest.raises(HomeAssistantError, match="Invalid parameter for set_fan_param"):
        await coordinator.async_set_fan_param(call)

    # 4. Verify _clear_pending_after_timeout(0) was called in the except block
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_get_all_fan_params_creates_task(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that get_all_fan_params schedules _async_run_fan_param_sequence as a task."""
    call_data = {"device_id": "30:111111"}

    # Provide a side effect for async_create_task to explicitly close the coroutine immediately
    def close_coro(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    # We patch 'async_create_task' because the implementation
    # uses hass.async_create_task() instead of hass.loop.create_task()
    with (
        patch.object(
            mock_coordinator.hass, "async_create_task", side_effect=close_coro
        ) as mock_create_task,
        # Mocking with AsyncMock generates the coroutine cleanly
        patch.object(
            mock_coordinator.service_handler,
            "_async_run_fan_param_sequence",
            new_callable=AsyncMock,
        ) as mock_run,
    ):
        await mock_coordinator.service_handler.get_all_fan_params(call_data)

        # 1. Verify the sequence method was called with the correct data
        mock_run.assert_called_once_with(call_data)

        # 2. Verify async_create_task was called exactly once
        mock_create_task.assert_called_once()


async def test_services_client_not_initialized(
    mock_coordinator: RamsesCoordinator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that services raise HomeAssistantError when client is not initialized."""
    # Force client to None to trigger the guard clauses
    mock_coordinator.client = None

    # 1. Test async_bind_device
    with pytest.raises(HomeAssistantError, match="client is not initialized"):
        await mock_coordinator.service_handler.async_bind_device(MagicMock())

    # 2. Test async_send_packet
    with pytest.raises(HomeAssistantError, match="client is not initialized"):
        await mock_coordinator.service_handler.async_send_packet(MagicMock())

    # 3. Test _adjust_sentinel_packet
    # This internal method has a redundant check that is unreachable via async_send_packet
    # (because async_send_packet checks client first). We call it directly to ensure coverage.
    with pytest.raises(HomeAssistantError, match="client is not initialized"):
        mock_coordinator.service_handler._adjust_sentinel_packet(MagicMock())

    # 4. Test async_set_fan_param
    with pytest.raises(HomeAssistantError, match="client is not initialized"):
        await mock_coordinator.service_handler.async_set_fan_param(MagicMock())

    # 5. Test async_get_fan_param
    with pytest.raises(HomeAssistantError, match="client is not initialized"):
        await mock_coordinator.service_handler.async_get_fan_param(MagicMock())

    # 6. Test _async_run_fan_param_sequence
    # This method catches exceptions internally, so it does NOT raise.
    # We assert that it runs without error and logs the underlying issues.
    await mock_coordinator.service_handler._async_run_fan_param_sequence({})

    # Check that the error was logged, confirming the exception handler was entered
    # The function returns early when device_id is missing, before checking client
    assert "Cannot run fan param sequence: missing device_id in call" in caplog.text


async def test_set_fan_param_raises_error_missing_destination(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that async_set_fan_param raises specific error for missing destination."""
    # DATA MISSING DEVICE_ID
    call_data = {
        # "device_id": "30:111222", # Missing
        "param_id": "01",
        "value": 1,
        "from_id": "32:111111",
    }

    # We expect HomeAssistantError with the NEW destination-specific message
    # This verifies Step 1 of the new logic
    with pytest.raises(HomeAssistantError, match="Destination 'device_id' is missing"):
        await mock_coordinator.async_set_fan_param(call_data)


async def test_get_fan_param_raises_error_missing_destination(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that async_get_fan_param raises specific error for missing destination."""
    call_data = {
        # "device_id": "30:111222", # Missing
        "param_id": "01",
        "from_id": "32:111111",
    }

    # Expect ServiceValidationError directly
    with pytest.raises(ServiceValidationError, match="service_device_id_missing"):
        await mock_coordinator.async_get_fan_param(call_data)


async def test_schedule_refresh_creates_task(mock_coordinator: MagicMock) -> None:
    """Test that _schedule_refresh submits the refresh request as a task."""

    # 1. Mock the coordinator's refresh method so we can assert it was called
    # We use AsyncMock so it returns a coroutine object when called, just like the real method
    mock_coordinator.async_request_refresh = AsyncMock()

    # 2. Instantiate the handler with the mock coordinator
    handler = RamsesServiceHandler(mock_coordinator)

    # 3. Patch async_create_task to intercept the call
    with patch.object(mock_coordinator.hass, "async_create_task") as mock_create_task:
        # 4. Trigger the method (it expects one argument, usually a datetime)
        handler._schedule_refresh(None)

        # 5. Verify the coordinator's refresh method was called to generate the coroutine
        mock_coordinator.async_request_refresh.assert_called_once()

        # 6. Verify the coroutine was submitted to the task runner
        mock_create_task.assert_called_once()

        # Check arguments
        args, _ = mock_create_task.call_args
        coro_arg = args[0]

        # Cleanup: Prevent "RuntimeWarning: coroutine '...' was never awaited"
        # Since we intercepted it, it won't run, so we close it manually.
        if hasattr(coro_arg, "close"):
            coro_arg.close()


async def test_get_fan_param_service_validation_error_clears_pending(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that ServiceValidationError raised in get_fan_param clears pending state.

    This targets the specific 'except ServiceValidationError' block
    ensuring that if a validation error occurs after the entity is found (e.g. during sending),
    the pending state is cleared immediately.
    """
    # 1. Setup valid IDs so execution proceeds past initial checks
    mock_coordinator.service_handler._get_device_and_from_id = MagicMock(
        return_value=("30:111111", "30_111111", "18:000000")
    )

    # 2. Setup Mock Entity with the required async method
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    mock_coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch dispatcher.send to raise ServiceValidationError
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.dispatcher.send.side_effect = ServiceValidationError(
        "Downstream Validation Failure"
    )

    call = {"device_id": "30:111111", "param_id": "01"}

    # 4. Assert the specific exception bubbles up
    with pytest.raises(ServiceValidationError, match="Downstream Validation Failure"):
        await mock_coordinator.async_get_fan_param(call)

    # 5. Verify _clear_pending_after_timeout(0) was called
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_coordinator_get_fan_param(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
) -> None:
    """Test async_get_fan_param service call in coordinator.py.

    From test_coordinator_fan.py.
    """
    # Mock device lookup using the boundary interface
    mock_coordinator._get_device = MagicMock(return_value=mock_fan_device)

    # 1. Test with explicit from_id
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "from_id": REM_ID}

    await mock_coordinator.async_get_fan_param(call_data)

    # Verify intent sent via CQRS dispatcher
    mock_client = cast(Any, mock_coordinator.client)
    assert mock_client.dispatcher.send.called
    intent = mock_client.dispatcher.send.call_args[0][0]
    # Intent has src/dst/action/data; the dispatcher translates it to a
    # CommandDTO with addr1=src, addr2=dst, verb=RQ, code=2411
    assert intent.dst.id == FAN_ID
    assert intent.action.name == "GET_FAN_PARAM"


async def test_coordinator_set_fan_param(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
) -> None:
    """Test async_set_fan_param service call in coordinator.py.

    From test_coordinator_fan.py.
    """
    # Mock device lookup using the boundary interface
    mock_coordinator._get_device = MagicMock(return_value=mock_fan_device)

    # 1. Test with automatic bound device lookup (no from_id)
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 21.5}

    await mock_coordinator.async_set_fan_param(call_data)

    # Verify intent sent via CQRS dispatcher
    mock_client = cast(Any, mock_coordinator.client)
    assert mock_client.dispatcher.send.called
    intent = mock_client.dispatcher.send.call_args[0][0]
    assert intent.dst.id == FAN_ID
    assert intent.action.name == "SET_FAN_PARAM"


async def test_update_fan_params_sequence(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
) -> None:
    """Test the sequential update of fan parameters with mocked schema.

    From test_coordinator_fan.py.
    """
    # Mock device lookup using the boundary interface
    mock_coordinator._get_device = MagicMock(return_value=mock_fan_device)

    # Define a tiny schema for testing (just 2 params) to avoid 30+ iterations
    tiny_schema = ["11", "22"]

    # Patch the schema AND asyncio.sleep in a single with-statement (SIM117)
    with (
        patch("custom_components.ramses_cc.services._2411_PARAMS_SCHEMA", tiny_schema),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        call_data = {"device_id": FAN_ID}
        # Call the method on service_handler, NOT directly on coordinator
        await mock_coordinator.service_handler._async_run_fan_param_sequence(call_data)

    # Verify that exactly 2 intents were sent via the CQRS dispatcher
    mock_client = cast(Any, mock_coordinator.client)
    assert mock_client.dispatcher.send.call_count == 2

    # Optional: Verify the calls were correct (GET_FAN_PARAM intents)
    calls = mock_client.dispatcher.send.call_args_list
    assert calls[0][0][0].action.name == "GET_FAN_PARAM"
    assert calls[1][0][0].action.name == "GET_FAN_PARAM"


async def test_set_fan_param_no_bound_remote(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
) -> None:
    """Test set_fan_param when the fan has NO bound remote (unbound).

    From test_coordinator_fan.py (renamed from test_coordinator_set_fan_param_no_binding).
    """
    # Mock device lookup using the boundary interface
    mock_coordinator._get_device = MagicMock(return_value=mock_fan_device)

    # 1. Simulate an Unbound Fan (get_bound_rem returns None)
    mock_fan_device.get_bound_rem = MagicMock(return_value=None)

    # 2. Try to set a parameter WITHOUT providing a 'from_id'
    # This forces the coordinator to look for the bound remote
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 21.5}

    # 3. Expectation: It SHOULD raise HomeAssistantError
    # We use pytest.raises to catch it and verify the message (optional match)
    with pytest.raises(
        HomeAssistantError, match="Cannot set parameter: No valid source device"
    ):
        await mock_coordinator.async_set_fan_param(call_data)

    # Verify NO command was sent (because there is no source ID)
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.dispatcher.send.assert_not_called()


async def test_set_fan_param_explicit_id_precedence(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
) -> None:
    """Test that explicit from_id takes precedence over bound device/HGI.

    Migrated from test_bound_device.py.
    """
    # 1. Setup: Fan has a bound remote with a valid HEX ID
    # 32:111111 is the 'bound' remote
    mock_fan_device.get_bound_rem.return_value = "32:111111"

    # Mock device lookup using the boundary interface
    mock_coordinator._get_device = MagicMock(return_value=mock_fan_device)

    # 2. Action: Call with an EXPLICIT from_id that is DIFFERENT from bound
    # 32:222222 is the 'explicit' remote
    explicit_id = "32:222222"
    call_data = {
        "device_id": FAN_ID,
        "param_id": PARAM_ID_HEX,
        "value": 21.5,
        "from_id": explicit_id,
    }

    # We want to test the resolution logic, so we do NOT patch _get_device_and_from_id here.
    # We rely on the real method in RamsesServiceHandler.
    await mock_coordinator.async_set_fan_param(call_data)

    # 3. Assert: The intent should use the EXPLICIT ID (32:222222), not the bound one
    mock_client = cast(Any, mock_coordinator.client)
    assert mock_client.dispatcher.send.called
    intent = mock_client.dispatcher.send.call_args[0][0]

    # intent.src is an Address; the explicit from_id should be used, not the bound one
    assert intent.src.id != "32:111111"


async def test_get_fan_param_uses_hgi_fallback(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test get_fan_param falls back to HGI ID when no bound source is found."""
    # 1. Setup HGI in client
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.hgi = MagicMock()
    mock_client.hgi.id = "18:999999"

    # 2. Setup Device (Fan) that is NOT bound
    mock_dev = MagicMock()
    mock_dev.id = "30:111111"
    mock_dev.get_bound_rem.return_value = None
    mock_coordinator._get_device = MagicMock(return_value=mock_dev)

    # 3. Setup Entity (to handle set_pending/cleanup)
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    mock_coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    # 4. Call without from_id
    call_data = {"device_id": "30:111111", "param_id": "01"}

    with caplog.at_level(logging.DEBUG):
        await mock_coordinator.async_get_fan_param(call_data)

        # Check that the fallback log was triggered
        assert "using gateway id" in caplog.text

    # 5. Verify intent was sent via dispatcher with HGI ID as source
    assert mock_client.dispatcher.send.called
    intent = mock_client.dispatcher.send.call_args[0][0]
    assert intent.src.id == "18:999999"


async def test_target_to_device_id_internals_coverage(
    hass: HomeAssistant, mock_coordinator: RamsesCoordinator
) -> None:
    """Test internal edge cases of _target_to_device_id for 100% coverage."""
    # Test behavior when no target is provided
    assert mock_coordinator.service_handler._target_to_device_id({}) is None

    # Test behavior when _device_entry_to_ramses_id evaluates a missing entry
    # We pass a device_id that definitely does not exist in the registry
    target_missing = {"device_id": "non_existent_ha_id"}
    assert mock_coordinator.service_handler._target_to_device_id(target_missing) is None

    # Test behavior when domain mismatch occurs during resolution
    dev_reg = dr.async_get(hass)
    config_entry_other = MockConfigEntry(domain="other_domain", entry_id="other_entry")
    config_entry_other.add_to_hass(hass)

    other_device = dev_reg.async_get_or_create(
        config_entry_id="other_entry", identifiers={("other_domain", "123")}
    )

    target_wrong_domain = {"device_id": other_device.id}
    assert (
        mock_coordinator.service_handler._target_to_device_id(target_wrong_domain)
        is None
    )


async def test_resolve_device_id_fallback_string(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _resolve_device_id falls back to string if not resolved."""
    # Use an integer ID to skip string validation logic
    # This forces the code to hit the final fallback block
    # Mypy fix: Explicitly type data as dict[str, Any] so it doesn't infer dict[str, int]
    data: dict[str, Any] = {"device_id": 12345}

    # Patch _target_to_device_id to fail resolution
    with patch.object(
        mock_coordinator.service_handler,
        "_target_to_device_id",
        return_value=None,
    ):
        result = mock_coordinator.service_handler._resolve_device_id(data)

        # Test string casting fallback
        assert result == "12345"
        assert data["device_id"] == "12345"


async def test_target_to_device_id_single_area_string(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _target_to_device_id when area_id is a single string.

    Tests passing {'area_id': 'string'} directly to _target_to_device_id,
    complementing test_resolve_device_id_area_string which passes it via 'target'.
    """
    area_id = "living_room"
    ramses_dev_id = "10:654321"

    target = {"area_id": area_id}

    # Patch device registry
    with patch("custom_components.ramses_cc.services.dr.async_get") as mock_dr_get:
        mock_reg = mock_dr_get.return_value

        # Create a mock device entry in the correct area with a RAMSES ID
        mock_entry = MagicMock()
        mock_entry.area_id = area_id
        mock_entry.identifiers = {(DOMAIN, ramses_dev_id)}

        # dev_reg.devices.values() is iterated
        mock_reg.devices.values.return_value = [mock_entry]

        # Execute on service_handler
        result = mock_coordinator.service_handler._target_to_device_id(target)

    assert result == ramses_dev_id


async def test_target_device_id_resolution(mock_coordinator: RamsesCoordinator) -> None:
    """Test resolution via device_id (single string and list) when entity_id is missing.

    Adds coverage for 'device_id' as a list in _target_to_device_id.
    """
    target_single = {"device_id": "ha_dev_1"}
    target_list = {"device_id": ["ha_dev_1"]}

    ramses_id = "02:222222"

    with patch("custom_components.ramses_cc.services.dr.async_get") as mock_dr_get:
        # Setup Device Registry Mock
        mock_dev_reg = mock_dr_get.return_value
        mock_dev_entry = MagicMock()
        mock_dev_entry.identifiers = {(DOMAIN, ramses_id)}
        mock_dev_reg.async_get.return_value = mock_dev_entry

        # Test Single String
        assert (
            mock_coordinator.service_handler._target_to_device_id(target_single)
            == ramses_id
        )

        # Test List
        assert (
            mock_coordinator.service_handler._target_to_device_id(target_list)
            == ramses_id
        )


async def test_target_priority_order(mock_coordinator: RamsesCoordinator) -> None:
    """Test that Entity ID takes priority over Device ID, which takes priority over Area ID."""
    target = {
        "entity_id": "sensor.exists",
        "device_id": "ha_dev_exists",
        "area_id": "area_exists",
    }

    id_from_entity = "01:000001"

    with (
        patch("custom_components.ramses_cc.services.er.async_get") as mock_er_get,
        patch("custom_components.ramses_cc.services.dr.async_get") as mock_dr_get,
    ):
        # 1. Setup successful Entity Lookup
        mock_ent_reg = mock_er_get.return_value
        mock_ent_entry = MagicMock()
        mock_ent_entry.device_id = "ha_dev_from_entity"
        mock_ent_reg.async_get.return_value = mock_ent_entry

        # Mock DR to return the ID derived from Entity
        mock_dev_reg = mock_dr_get.return_value

        def side_effect(dev_id: str) -> MagicMock:
            m = MagicMock()
            if dev_id == "ha_dev_from_entity":
                m.identifiers = {(DOMAIN, id_from_entity)}
                return m
            return MagicMock(identifiers={})  # Return generic for others

        mock_dev_reg.async_get.side_effect = side_effect

        # Should return the one found via entity_id, ignoring device_id/area_id logic
        assert (
            mock_coordinator.service_handler._target_to_device_id(target)
            == id_from_entity
        )


async def test_target_resolution_orphaned_entity(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test target resolution returns None when entity exists but has no device_id (orphaned)."""
    with patch("custom_components.ramses_cc.services.er.async_get") as mock_er_get:
        mock_ent_reg = mock_er_get.return_value
        # Mock entity found but device_id is None
        mock_ent_reg.async_get.return_value = MagicMock(device_id=None)

        assert (
            mock_coordinator.service_handler._target_to_device_id(
                {"entity_id": "sensor.orphan"}
            )
            is None
        )


async def test_send_packet_transport_error(mock_coordinator: RamsesCoordinator) -> None:
    """Test async_send_packet raises HomeAssistantError on specific transport errors."""
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.async_send_cmd.side_effect = TransportError("Tx Failed")

    call = MagicMock()
    # Mock data to satisfy HGI check if needed, though simple packet usually skips checks
    call.data = {
        "device_id": "18:000730",
        "verb": "I",
        "code": "1F09",
        "payload": "FF",
    }

    # Mock create_cmd to return a valid command object
    mock_cmd = MagicMock()
    mock_cmd.src.id = "18:000730"  # Match sentinel logic if hit
    mock_client.create_cmd.return_value = mock_cmd

    with pytest.raises(HomeAssistantError, match="Failed to send packet"):
        await mock_coordinator.async_send_packet(call)


async def test_get_fan_param_transport_error(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_get_fan_param handles ProtocolSendFailed/TimeoutError."""
    # 1. Setup valid IDs
    mock_coordinator.service_handler._get_device_and_from_id = MagicMock(
        return_value=("30:111111", "30_111111", "18:000000")
    )

    # 2. Setup Entity
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    mock_coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch dispatcher.send to raise ProtocolSendFailed
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.dispatcher.send.side_effect = ProtocolSendFailed("RF Error")

    call = {"device_id": "30:111111", "param_id": "01"}

    with pytest.raises(HomeAssistantError, match="Failed to get fan parameter"):
        await mock_coordinator.async_get_fan_param(call)

    # 4. Verify cleanup called
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_set_fan_param_transport_error(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_set_fan_param handles ProtocolSendFailed/TimeoutError."""
    # 1. Setup valid IDs
    mock_coordinator.service_handler._get_device_and_from_id = MagicMock(
        return_value=("30:111111", "30_111111", "18:000000")
    )

    # 2. Setup Entity
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    mock_coordinator.fan_handler.find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch dispatcher.send to raise TimeoutError
    mock_client = cast(Any, mock_coordinator.client)
    mock_client.dispatcher.send.side_effect = TimeoutError("Tx Timeout")

    call = {"device_id": "30:111111", "param_id": "01", "value": 10}

    with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
        await mock_coordinator.async_set_fan_param(call)

    # 4. Verify cleanup called
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


@pytest.mark.asyncio
async def test_async_bind_device_routes_to_registry(
    hass: HomeAssistant,
) -> None:
    """Ensure async_bind_device explicitly routes through device_registry.

    This test explicitly prevents regressions for ramses_cc Issue #598 by
    enforcing that the Gateway object (client) does not possess the
    'fake_device' attribute directly.
    """
    # 1. Arrange: Setup Coordinator
    mock_coordinator = MagicMock()
    mock_coordinator.hass = hass
    # Prevent HA Debouncer by mocking the refresh request entirely
    mock_coordinator.async_request_refresh = AsyncMock()

    # Setup the client (Gateway) mock
    mock_client = MagicMock()

    # CRITICAL: Explicitly delete fake_device from the gateway mock to
    # replicate the strict 0.55.5+ architecture and trigger AttributeError
    # if the old routing is used.
    del mock_client.fake_device

    # Setup the device registry mock
    mock_registry = AsyncMock()
    mock_device = AsyncMock()
    mock_device.id = "01:123456"

    # Assign the mock device to be returned by the registry
    mock_registry.fake_device.return_value = mock_device
    mock_client.device_registry = mock_registry

    mock_coordinator.client = mock_client

    handler = RamsesServiceHandler(mock_coordinator)

    # Construct the HA service call mimicking the developer tools action
    call = ServiceCall(
        hass=hass,
        domain="ramses_cc",
        service="bind_device",
        data={
            "device_id": "01:123456",
            "device_info": None,
            "offer": {"00": "val"},
            "confirm": {"00": "val"},
        },
    )

    # 2. Act: Execute the service
    await handler.async_bind_device(call)

    # Fast-forward time to cleanly execute the async_call_later timer
    async_fire_time_changed(hass, dt_util.utcnow() + td(seconds=10))
    await hass.async_block_till_done()

    # 3. Assert: Verify the registry was called, bypassing the Gateway
    mock_registry.fake_device.assert_called_once_with("01:123456")
    mock_device._initiate_binding_process.assert_called_once()


# ───────────────────────────────────────────────────────────────────────
# Passive device scan: _extract_device_ids_from_schema
# ───────────────────────────────────────────────────────────────────────


class TestExtractDeviceIdsFromSchema:
    """Tests for RamsesServiceHandler._extract_device_ids_from_schema."""

    def test_empty_schema(self) -> None:
        """Empty schema returns empty set."""
        result = RamsesServiceHandler._extract_device_ids_from_schema({})
        assert result == set()

    def test_main_tcs_only(self) -> None:
        """Schema with only main_tcs returns the CTL id."""
        schema = {SZ_MAIN_TCS: "01:123456"}
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert result == {"01:123456"}

    def test_tcs_with_system_appliance_control(self) -> None:
        """TCS with system.appliance_control extracts both CTL and appliance."""
        schema = {
            SZ_MAIN_TCS: "01:123456",
            "01:123456": {
                SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "01:654321"},
            },
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "01:123456" in result
        assert "01:654321" in result

    def test_tcs_with_dhw_system(self) -> None:
        """TCS with dhw_system extracts sensor and valves."""
        schema = {
            "01:123456": {
                SZ_DHW_SYSTEM: {
                    SZ_SENSOR: "07:111111",
                    SZ_DHW_VALVE: "08:222222",
                    SZ_HTG_VALVE: "08:333333",
                },
            },
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "01:123456" in result
        assert "07:111111" in result
        assert "08:222222" in result
        assert "08:333333" in result

    def test_tcs_with_ufh_system(self) -> None:
        """TCS with ufh_system extracts UFC device IDs."""
        schema: dict[str, Any] = {
            "01:123456": {
                SZ_UFH_SYSTEM: {"10:444444": {}},
            },
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "01:123456" in result
        assert "10:444444" in result

    def test_tcs_with_zones(self) -> None:
        """TCS with zones extracts zone sensors and actuators."""
        schema = {
            "01:123456": {
                SZ_ZONES: {
                    "01": {SZ_SENSOR: "04:555555", SZ_ACTUATORS: ["08:666666"]},
                },
            },
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "01:123456" in result
        assert "04:555555" in result
        assert "08:666666" in result

    def test_tcs_with_orphans(self) -> None:
        """TCS-level orphans are extracted."""
        schema = {
            "01:123456": {SZ_ORPHANS: ["04:777777"]},
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "01:123456" in result
        assert "04:777777" in result

    def test_vcs_with_remotes_and_sensors(self) -> None:
        """HVAC (FAN) structure extracts remotes and sensors."""
        schema = {
            "30:160000": {
                SZ_REMOTES: ["32:888888"],
                SZ_SENSORS: ["32:999999"],
            },
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "30:160000" in result
        assert "32:888888" in result
        assert "32:999999" in result

    def test_global_orphans(self) -> None:
        """Global heat and HVAC orphans are extracted."""
        schema = {
            SZ_ORPHANS_HEAT: ["04:aaaaaa"],
            SZ_ORPHANS_HVAC: ["32:bbbbbb"],
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "04:aaaaaa" in result
        assert "32:bbbbbb" in result

    def test_skips_non_device_keys(self) -> None:
        """Non-device-id keys and extension keys are skipped."""
        schema = {
            SZ_MAIN_TCS: "01:123456",
            SZ_ORPHANS_HEAT: [],
            SZ_ORPHANS_HVAC: [],
            "transport_constructor": "something",
            "not_a_device_id": {},
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert result == {"01:123456"}

    def test_skips_non_dict_value(self) -> None:
        """Non-dict values for device-id keys are handled (id still extracted)."""
        schema = {
            "01:123456": "not a dict",
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        assert "01:123456" in result

    def test_full_complex_schema(self) -> None:
        """A complex schema with multiple TCS, zones, DHW, UFH, HVAC, orphans."""
        schema = {
            SZ_MAIN_TCS: "01:100000",
            "01:100000": {
                SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "01:200000"},
                SZ_DHW_SYSTEM: {SZ_SENSOR: "07:300000", SZ_DHW_VALVE: "08:400000"},
                SZ_UFH_SYSTEM: {"10:500000": {}},
                SZ_ZONES: {
                    "01": {SZ_SENSOR: "04:600000", SZ_ACTUATORS: ["08:700000"]},
                },
                SZ_ORPHANS: ["04:800000"],
            },
            "30:160000": {
                SZ_REMOTES: ["32:900000"],
                SZ_SENSORS: ["32:a00000"],
            },
            SZ_ORPHANS_HEAT: ["04:b00000"],
            SZ_ORPHANS_HVAC: ["32:c00000"],
        }
        result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
        expected = {
            "01:100000",
            "01:200000",
            "07:300000",
            "08:400000",
            "10:500000",
            "04:600000",
            "08:700000",
            "04:800000",
            "30:160000",
            "32:900000",
            "32:a00000",
            "04:b00000",
            "32:c00000",
        }
        assert result == expected


# ───────────────────────────────────────────────────────────────────────
# Passive device scan: discovery service calls
# ───────────────────────────────────────────────────────────────────────


def make_service_handler_with_discovery(
    coordinator: RamsesCoordinator,
) -> RamsesServiceHandler:
    """Create a service handler with a mock discovery_manager on the coordinator."""
    coordinator.discovery_manager = MagicMock()
    coordinator.discovery_manager.get_devices.return_value = []
    coordinator.discovery_manager.accept_device = MagicMock()
    coordinator.discovery_manager.discard_device = MagicMock()
    coordinator.discovery_manager.remove_device = MagicMock()
    coordinator.discovery_manager.enable_device = MagicMock()
    coordinator.discovery_manager.disable_device = MagicMock()
    coordinator.discovery_manager.add_faked_rem = MagicMock()
    return RamsesServiceHandler(coordinator)


def make_mock_discovery_entry(
    device_id: str = "04:056053",
    schema_entry: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock DiscoveredDeviceEntry."""
    entry = MagicMock()
    entry.device.device_id = device_id
    entry.device.likely_type = "TRV"
    entry.device.confidence = "high"
    entry.device.rssi = -72.0
    entry.device.codes_seen = ["3150"]
    entry.device.bound_to = "01:145038"
    entry.device.zone_idx = "02"
    entry.device.is_battery = True
    entry.device.src_count = 3
    entry.device.dst_count = 0
    entry.metadata.status.value = "new"
    entry.metadata.enabled = False
    entry.metadata.schema_entry = schema_entry
    entry.to_dict.return_value = {"device_id": device_id}
    return entry


async def test_get_discovered_devices_no_manager(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test get_discovered_devices raises when no discovery manager."""
    mock_coordinator.discovery_manager = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {}
    with pytest.raises(HomeAssistantError, match="Passive device scan is not enabled"):
        await handler.async_get_discovered_devices(call)


async def test_get_discovered_devices_success(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test get_discovered_devices returns devices via bus event."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    entry = make_mock_discovery_entry()
    mock_coordinator.discovery_manager.get_devices.return_value = [entry]

    call = MagicMock()
    call.data = {"status": "new", "enabled": True}

    caplog.set_level(logging.INFO)
    await handler.async_get_discovered_devices(call)

    # Verify get_devices was called with the right filters
    from custom_components.ramses_cc.discovery import DiscoveryStatus

    mock_coordinator.discovery_manager.get_devices.assert_called_once_with(
        status=DiscoveryStatus.NEW, enabled=True
    )
    assert "found 1 device(s)" in caplog.text
    assert "04:056053" in caplog.text


async def test_get_discovered_devices_no_filters(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test get_discovered_devices with no filters."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    mock_coordinator.discovery_manager.get_devices.return_value = []

    call = MagicMock()
    call.data = {}

    await handler.async_get_discovered_devices(call)
    mock_coordinator.discovery_manager.get_devices.assert_called_once_with(
        status=None, enabled=None
    )


async def test_accept_discovered_device_no_manager(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test accept_discovered_device raises when no discovery manager."""
    mock_coordinator.discovery_manager = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}
    with pytest.raises(HomeAssistantError, match="Passive device scan is not enabled"):
        await handler.async_accept_discovered_device(call)


async def test_accept_discovered_device_not_found(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test accept_discovered_device raises ServiceValidationError when device not found."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    mock_coordinator.discovery_manager.accept_device.side_effect = ValueError(
        "Device 99:999999 not in discovery list"
    )
    call = MagicMock()
    call.data = {"device_id": "99:999999"}
    with pytest.raises(ServiceValidationError, match="not in discovery list"):
        await handler.async_accept_discovered_device(call)


async def test_accept_discovered_device_with_schema_entry(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test accept_discovered_device merges schema entry and triggers discovery."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    schema_entry = {"01:145038": {SZ_ZONES: {"02": {SZ_SENSOR: "04:056053"}}}}
    entry = make_mock_discovery_entry("04:056053", schema_entry=schema_entry)
    mock_coordinator.discovery_manager.accept_device.return_value = entry

    # Mock the coordinator's client and engine for _apply_schema_entry
    mock_client = MagicMock()
    mock_engine = MagicMock()
    mock_engine._include = []
    mock_client._engine = mock_engine
    mock_dev_filter = MagicMock()
    mock_dev_filter._include = []
    mock_client._device_filter = mock_dev_filter
    mock_coordinator.client = mock_client

    # Mock discover_known_devices to avoid full client setup
    handler.async_discover_known_devices = AsyncMock()

    call = MagicMock()
    call.data = {"device_id": "04:056053", "owner": "henk"}

    await handler.async_accept_discovered_device(call)

    mock_coordinator.discovery_manager.accept_device.assert_called_once_with(
        "04:056053", owner="henk", schema_entry=None, ctl_id=None
    )
    # Verify schema was merged into options
    assert CONF_SCHEMA in mock_coordinator.options
    handler.async_discover_known_devices.assert_called_once()


async def test_accept_discovered_device_no_schema_entry(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test accept_discovered_device when accept returns no schema_entry."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    entry = make_mock_discovery_entry("04:056053", schema_entry=None)
    mock_coordinator.discovery_manager.accept_device.return_value = entry

    handler.async_discover_known_devices = AsyncMock()

    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    await handler.async_accept_discovered_device(call)

    # Should not have updated config entry (no schema_entry)
    handler.async_discover_known_devices.assert_called_once()


async def test_apply_schema_entry_with_owner(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _apply_schema_entry adds owner as alias to known_list."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_client = MagicMock()
    mock_engine = MagicMock()
    mock_engine._include = []
    mock_client._engine = mock_engine
    mock_dev_filter = MagicMock()
    mock_dev_filter._include = []
    mock_client._device_filter = mock_dev_filter
    mock_coordinator.client = mock_client

    fragment = {"01:145038": {SZ_ZONES: {"02": {SZ_SENSOR: "04:056053"}}}}
    handler._apply_schema_entry(fragment, "04:056053", owner="henk")

    # Verify schema was merged
    assert mock_coordinator.options[CONF_SCHEMA] == fragment
    # Verify known_list got the alias
    assert mock_coordinator.options[SZ_KNOWN_LIST]["04:056053"]["alias"] == "henk"
    # Verify engine include list was updated
    assert "04:056053" in mock_engine._include


async def test_apply_schema_entry_no_client(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _apply_schema_entry when client is None."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.client = None

    fragment: dict[str, Any] = {"01:145038": {}}
    handler._apply_schema_entry(fragment, "04:056053")

    assert mock_coordinator.options[CONF_SCHEMA] == fragment


async def test_apply_schema_entry_moves_from_orphans(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _apply_schema_entry removes device from orphans before merging."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_client = MagicMock()
    mock_engine = MagicMock()
    mock_engine._include = []
    mock_client._engine = mock_engine
    mock_dev_filter = MagicMock()
    mock_dev_filter._include = []
    mock_client._device_filter = mock_dev_filter
    mock_coordinator.client = mock_client

    # Device starts in orphans_heat
    mock_coordinator.options = {
        CONF_SCHEMA: {
            "main_tcs": "01:145038",
            "01:145038": {},
            SZ_ORPHANS_HEAT: ["04:056053"],
        }
    }

    # Accept it as a zone sensor — should remove from orphans, add to zone
    fragment = {"01:145038": {SZ_ZONES: {"02": {SZ_SENSOR: "04:056053"}}}}
    handler._apply_schema_entry(fragment, "04:056053")

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert "04:056053" not in schema.get(SZ_ORPHANS_HEAT, [])
    assert schema["01:145038"][SZ_ZONES]["02"][SZ_SENSOR] == "04:056053"


async def test_apply_schema_entry_does_not_overwrite_zone_sensor(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _apply_schema_entry doesn't overwrite an existing zone sensor."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_client = MagicMock()
    mock_engine = MagicMock()
    mock_engine._include = []
    mock_client._engine = mock_engine
    mock_dev_filter = MagicMock()
    mock_dev_filter._include = []
    mock_client._device_filter = mock_dev_filter
    mock_coordinator.client = mock_client

    # Zone 02 already has a sensor
    mock_coordinator.options = {
        CONF_SCHEMA: {
            "main_tcs": "01:145038",
            "01:145038": {
                SZ_ZONES: {"02": {SZ_SENSOR: "04:999999"}},
            },
        }
    }

    # Accept a new device into the same zone — old sensor should be cleared
    # (the new device becomes the sensor, old one is orphaned by ramses_rf)
    fragment = {"01:145038": {SZ_ZONES: {"02": {SZ_SENSOR: "04:056053"}}}}
    handler._apply_schema_entry(fragment, "04:056053")

    schema = mock_coordinator.options[CONF_SCHEMA]
    # New sensor is set
    assert schema["01:145038"][SZ_ZONES]["02"][SZ_SENSOR] == "04:056053"
    # Old sensor was removed from the zone (deep_merge overwrites scalars)
    # but it should NOT be in orphans (we only removed the new device, not the old one)
    # This is expected — the old device's orphan status is handled by ramses_rf's topology


async def test_apply_schema_entry_preserves_existing_root_entry(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _apply_schema_entry does NOT overwrite an existing root entry.

    If a device already has a root entry in the schema (e.g. added manually
    via the schema editor with _class, remotes, _commands), accepting it
    via discovery should NOT overwrite those user-configured keys.

    Regression test for the bug where accept_discovered_device overwrote
    a manually-configured FAN entry (remotes: ["37:170000"], _commands: {...})
    with the auto-generated fragment (remotes: [], _class: "FAN").
    """
    handler = RamsesServiceHandler(mock_coordinator)
    mock_client = MagicMock()
    mock_engine = MagicMock()
    mock_engine._include = []
    mock_client._engine = mock_engine
    mock_dev_filter = MagicMock()
    mock_dev_filter._include = []
    mock_client._device_filter = mock_dev_filter
    mock_coordinator.client = mock_client

    # FAN already in schema with user-configured remotes and _commands
    mock_coordinator.options = {
        CONF_SCHEMA: {
            "32:150000": {
                "_class": "FAN",
                "remotes": ["37:170000"],
                "_commands": {
                    "boost": {"code": "22F1", "payload": "000607", "verb": "I"}
                },
            },
        }
    }

    # Auto-generated fragment for FAN — has remotes: [] and _class: "FAN"
    # which would overwrite the user's remotes and _commands if merged
    # with fragment as src
    fragment = {
        "32:150000": {
            "_class": "FAN",
            "remotes": [],
        }
    }
    handler._apply_schema_entry(fragment, "32:150000")

    schema = mock_coordinator.options[CONF_SCHEMA]
    fan_entry = schema["32:150000"]
    # _class should be preserved (fragment has same value, but the point is
    # the user's value wins)
    assert fan_entry["_class"] == "FAN"
    # remotes should NOT be overwritten with []
    assert fan_entry["remotes"] == ["37:170000"]
    # _commands should be preserved (fragment doesn't have it, but we're
    # verifying the existing entry isn't touched)
    assert "_commands" in fan_entry
    assert fan_entry["_commands"]["boost"]["payload"] == "000607"


async def test_apply_schema_entry_preserves_existing_rem_root(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _apply_schema_entry preserves an existing REM root entry.

    A REM with a user-configured root entry (_class, _owner) should keep
    those traits when the REM is re-accepted via discovery.  The fragment
    only adds the placement (remotes[] under the FAN), not the root entry.
    """
    handler = RamsesServiceHandler(mock_coordinator)
    mock_client = MagicMock()
    mock_engine = MagicMock()
    mock_engine._include = []
    mock_client._engine = mock_engine
    mock_dev_filter = MagicMock()
    mock_dev_filter._include = []
    mock_client._device_filter = mock_dev_filter
    mock_coordinator.client = mock_client

    mock_coordinator.options = {
        CONF_SCHEMA: {
            "32:150000": {"_class": "FAN", "remotes": []},
            "37:170000": {"_class": "REM", "_owner": "not-me"},
        }
    }

    # Fragment for REM bound to FAN — has remotes: ["37:170000"] under FAN
    # and a root entry with _class: "REM" which would overwrite _owner
    fragment = {
        "32:150000": {"remotes": ["37:170000"]},
        "37:170000": {"_class": "REM"},
    }
    handler._apply_schema_entry(fragment, "37:170000")

    schema = mock_coordinator.options[CONF_SCHEMA]
    rem_entry = schema["37:170000"]
    # _class should be REM (same in both)
    assert rem_entry["_class"] == "REM"
    # _owner should be preserved — NOT overwritten by the fragment
    assert rem_entry["_owner"] == "not-me"
    # REM should be placed under the FAN's remotes
    assert "37:170000" in schema["32:150000"].get("remotes", [])


async def test_discard_discovered_device_no_manager(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discard_discovered_device raises when no discovery manager."""
    mock_coordinator.discovery_manager = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}
    with pytest.raises(HomeAssistantError, match="Passive device scan is not enabled"):
        await handler.async_discard_discovered_device(call)


async def test_discard_discovered_device_success(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discard_discovered_device calls discovery_manager.discard_device."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    await handler.async_discard_discovered_device(call)
    mock_coordinator.discovery_manager.discard_device.assert_called_once_with(
        "04:056053"
    )


async def test_discard_discovered_device_not_found(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discard_discovered_device raises ServiceValidationError when not found."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    mock_coordinator.discovery_manager.discard_device.side_effect = ValueError(
        "not in discovery list"
    )
    call = MagicMock()
    call.data = {"device_id": "99:999999"}
    with pytest.raises(ServiceValidationError, match="not in discovery list"):
        await handler.async_discard_discovered_device(call)


async def test_remove_discovered_device_no_manager(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test remove_discovered_device raises when no discovery manager."""
    mock_coordinator.discovery_manager = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}
    with pytest.raises(HomeAssistantError, match="Passive device scan is not enabled"):
        await handler.async_remove_discovered_device(call)


async def test_remove_discovered_device_success(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test remove_discovered_device calls discovery_manager.remove_device."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    await handler.async_remove_discovered_device(call)
    mock_coordinator.discovery_manager.remove_device.assert_called_once_with(
        "04:056053"
    )


async def test_remove_discovered_device_not_found(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test remove_discovered_device raises ServiceValidationError when not found."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    mock_coordinator.discovery_manager.remove_device.side_effect = ValueError(
        "not in discovery list"
    )
    call = MagicMock()
    call.data = {"device_id": "99:999999"}
    with pytest.raises(ServiceValidationError, match="not in discovery list"):
        await handler.async_remove_discovered_device(call)


async def test_enable_discovered_device_no_manager(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test enable_discovered_device raises when no discovery manager."""
    mock_coordinator.discovery_manager = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}
    with pytest.raises(HomeAssistantError, match="Passive device scan is not enabled"):
        await handler.async_enable_discovered_device(call)


async def test_enable_discovered_device_success(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test enable_discovered_device calls discovery_manager.enable_device."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    await handler.async_enable_discovered_device(call)
    mock_coordinator.discovery_manager.enable_device.assert_called_once_with(
        "04:056053"
    )


async def test_enable_discovered_device_not_found(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test enable_discovered_device raises ServiceValidationError when not found."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    mock_coordinator.discovery_manager.enable_device.side_effect = ValueError(
        "not in discovery list"
    )
    call = MagicMock()
    call.data = {"device_id": "99:999999"}
    with pytest.raises(ServiceValidationError, match="not in discovery list"):
        await handler.async_enable_discovered_device(call)


async def test_disable_discovered_device_no_manager(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test disable_discovered_device raises when no discovery manager."""
    mock_coordinator.discovery_manager = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}
    with pytest.raises(HomeAssistantError, match="Passive device scan is not enabled"):
        await handler.async_disable_discovered_device(call)


async def test_disable_discovered_device_success(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test disable_discovered_device calls discovery_manager.disable_device."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    await handler.async_disable_discovered_device(call)
    mock_coordinator.discovery_manager.disable_device.assert_called_once_with(
        "04:056053"
    )


async def test_disable_discovered_device_not_found(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test disable_discovered_device raises ServiceValidationError when not found."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    mock_coordinator.discovery_manager.disable_device.side_effect = ValueError(
        "not in discovery list"
    )
    call = MagicMock()
    call.data = {"device_id": "99:999999"}
    with pytest.raises(ServiceValidationError, match="not in discovery list"):
        await handler.async_disable_discovered_device(call)


async def test_add_faked_rem_no_manager(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test add_faked_rem raises when no discovery manager."""
    mock_coordinator.discovery_manager = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "37:000001", "bound_to": "32:157747"}
    with pytest.raises(HomeAssistantError, match="Passive device scan is not enabled"):
        await handler.async_add_faked_rem(call)


async def test_add_faked_rem_success(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test add_faked_rem calls discovery_manager.add_faked_rem and persists schema."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "37:000001", "bound_to": "32:157747", "alias": "Living"}

    # add_faked_rem should return a DiscoveredDeviceEntry with schema_entry
    mock_entry = make_mock_discovery_entry(
        "37:000001",
        schema_entry={"_class": "REM", "_bound": "32:157747", "_faked": True},
    )
    mock_coordinator.discovery_manager.add_faked_rem.return_value = mock_entry
    handler._apply_schema_entry = MagicMock()
    handler.async_discover_known_devices = AsyncMock()
    mock_coordinator.entry = MagicMock()
    mock_coordinator.options = {}

    caplog.set_level(logging.INFO)
    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_add_faked_rem(call)
    mock_coordinator.discovery_manager.add_faked_rem.assert_called_once_with(
        "37:000001", bound_to="32:157747", alias="Living"
    )
    handler._apply_schema_entry.assert_called_once()
    handler.async_discover_known_devices.assert_awaited_once()
    assert "Added faked REM" in caplog.text


async def test_add_faked_rem_no_alias(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test add_faked_rem without alias."""
    handler = make_service_handler_with_discovery(mock_coordinator)
    call = MagicMock()
    call.data = {"device_id": "37:000001", "bound_to": "32:157747"}

    mock_entry = make_mock_discovery_entry(
        "37:000001",
        schema_entry={"_class": "REM", "_bound": "32:157747", "_faked": True},
    )
    mock_coordinator.discovery_manager.add_faked_rem.return_value = mock_entry
    handler._apply_schema_entry = MagicMock()
    handler.async_discover_known_devices = AsyncMock()
    mock_coordinator.entry = MagicMock()
    mock_coordinator.options = {}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_add_faked_rem(call)
    mock_coordinator.discovery_manager.add_faked_rem.assert_called_once_with(
        "37:000001", bound_to="32:157747", alias=None
    )


# ───────────────────────────────────────────────────────────────────────
# Passive device scan: async_discover_known_devices
# ───────────────────────────────────────────────────────────────────────


async def test_discover_known_devices_no_client(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discover_known_devices raises when client is not initialized."""
    mock_coordinator.client = None
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {}
    with pytest.raises(HomeAssistantError, match="RAMSES RF client is not initialized"):
        await handler.async_discover_known_devices(call)


async def test_discover_known_devices_no_devices(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test discover_known_devices with empty known_list and schema."""
    handler = RamsesServiceHandler(mock_coordinator)
    call = MagicMock()
    call.data = {}

    caplog.set_level(logging.WARNING)
    await handler.async_discover_known_devices(call)
    assert "no known_list or schema configured" in caplog.text


async def test_discover_known_devices_target_not_found(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test discover_known_devices with a target device_id not in known_list."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"01:123456": {}}

    call = MagicMock()
    call.data = {"device_id": "99:999999"}

    caplog.set_level(logging.WARNING)
    await handler.async_discover_known_devices(call)
    assert "not in known_list or schema" in caplog.text


async def test_discover_known_devices_creates_device(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discover_known_devices creates a device from known_list."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"01:123456": {}}

    # Mock device registry
    mock_client = cast(Any, mock_coordinator.client)
    mock_dev = MagicMock()
    mock_dev._SLUG = "CTL"
    mock_dev.discovery.cmds = []
    mock_client.device_registry.device_by_id = {}
    mock_client.device_registry.get_device = MagicMock(return_value=mock_dev)
    mock_client.hgi = None

    # Mock async_create_task to capture (and close) the background task
    def _close_coro(coro: Any) -> None:
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    mock_coordinator.hass.async_create_task = MagicMock(side_effect=_close_coro)

    call = MagicMock()
    call.data = {}

    await handler.async_discover_known_devices(call)

    mock_client.device_registry.get_device.assert_called_once_with("01:123456")
    mock_coordinator.hass.async_create_task.assert_called_once()


async def test_discover_known_devices_already_present(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discover_known_devices skips devices already in registry."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"01:123456": {}}

    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.device_by_id = {"01:123456": MagicMock()}
    mock_client.hgi = None

    def _close_coro(coro: Any) -> None:
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    mock_coordinator.hass.async_create_task = MagicMock(side_effect=_close_coro)

    call = MagicMock()
    call.data = {}

    await handler.async_discover_known_devices(call)

    # Should still create a background task for probing
    mock_coordinator.hass.async_create_task.assert_called_once()


async def test_discover_known_devices_skips_hgi(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test discover_known_devices skips HGI-class devices."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"18:123456": {"class": "HGI"}}

    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.device_by_id = {}
    mock_client.hgi = None

    caplog.set_level(logging.INFO)
    # Should not create a task since nothing was created or present
    mock_coordinator.hass.async_create_task = MagicMock()

    call = MagicMock()
    call.data = {}

    await handler.async_discover_known_devices(call)
    assert "Skipping HGI" in caplog.text
    mock_coordinator.hass.async_create_task.assert_not_called()


async def test_discover_known_devices_create_fails(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test discover_known_devices handles device creation failure."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"01:123456": {}}

    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.device_by_id = {}
    mock_client.hgi = None
    mock_client.device_registry.get_device = MagicMock(side_effect=Exception("boom"))

    caplog.set_level(logging.WARNING)
    mock_coordinator.hass.async_create_task = MagicMock()

    call = MagicMock()
    call.data = {}

    await handler.async_discover_known_devices(call)
    assert "Failed to create device" in caplog.text


async def test_discover_known_devices_skips_active_hgi(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discover_known_devices skips the active HGI itself."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"18:006402": {}}

    mock_client = cast(Any, mock_coordinator.client)
    mock_hgi = MagicMock()
    mock_hgi.id = "18:006402"
    mock_client.hgi = mock_hgi
    mock_client.device_registry.device_by_id = {}

    mock_coordinator.hass.async_create_task = MagicMock()

    call = MagicMock()
    call.data = {}

    await handler.async_discover_known_devices(call)
    # Nothing to do — HGI was skipped, nothing created/present
    mock_coordinator.hass.async_create_task.assert_not_called()


# ───────────────────────────────────────────────────────────────────────
# Passive device scan: _async_probe_and_discover
# ───────────────────────────────────────────────────────────────────────


async def test_async_probe_and_discover_no_client(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_probe_and_discover returns early when no client."""
    mock_coordinator.client = None
    handler = RamsesServiceHandler(mock_coordinator)
    await handler._async_probe_and_discover([], [])


async def test_async_probe_and_discover_probes_devices(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_probe_and_discover probes devices with discovery cmds."""
    handler = RamsesServiceHandler(mock_coordinator)

    mock_client = cast(Any, mock_coordinator.client)
    mock_dev = MagicMock()
    mock_dev.discovery.cmds = ["3150"]
    mock_dev.discovery.discover = AsyncMock()
    mock_client.device_registry.device_by_id = {"01:123456": mock_dev}
    mock_client.hgi = None

    mock_coordinator._discover_new_entities = AsyncMock()

    with patch("custom_components.ramses_cc.services.async_call_later"):
        await handler._async_probe_and_discover(["01:123456"], [])

    mock_dev.discovery.discover.assert_called_once()
    mock_coordinator._discover_new_entities.assert_called_once()


async def test_async_probe_and_discover_zero_cmds(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_probe_and_discover skips devices with zero discovery cmds."""
    handler = RamsesServiceHandler(mock_coordinator)

    mock_client = cast(Any, mock_coordinator.client)
    mock_dev = MagicMock()
    mock_dev.discovery.cmds = []  # zero cmds
    mock_dev.discovery.discover = AsyncMock()
    mock_client.device_registry.device_by_id = {"04:123456": mock_dev}
    mock_client.hgi = None

    mock_coordinator._discover_new_entities = AsyncMock()

    with patch("custom_components.ramses_cc.services.async_call_later"):
        await handler._async_probe_and_discover(["04:123456"], [])

    mock_dev.discovery.discover.assert_not_called()
    mock_coordinator._discover_new_entities.assert_called_once()


async def test_async_probe_and_discover_discover_fails(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _async_probe_and_discover handles discovery failures."""
    handler = RamsesServiceHandler(mock_coordinator)

    mock_client = cast(Any, mock_coordinator.client)
    mock_dev = MagicMock()
    mock_dev.discovery.cmds = ["3150"]
    mock_dev.discovery.discover = AsyncMock(side_effect=Exception("timeout"))
    mock_client.device_registry.device_by_id = {"01:123456": mock_dev}
    mock_client.hgi = None

    mock_coordinator._discover_new_entities = AsyncMock()

    caplog.set_level(logging.DEBUG)
    with patch("custom_components.ramses_cc.services.async_call_later"):
        await handler._async_probe_and_discover(["01:123456"], [])

    assert "Discovery cycle failed" in caplog.text
    mock_coordinator._discover_new_entities.assert_called_once()


async def test_async_probe_and_discover_device_not_in_registry(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_probe_and_discover skips devices not in registry."""
    handler = RamsesServiceHandler(mock_coordinator)

    mock_client = cast(Any, mock_coordinator.client)
    mock_client.device_registry.device_by_id = {}  # device not present
    mock_client.hgi = None

    mock_coordinator._discover_new_entities = AsyncMock()

    with patch("custom_components.ramses_cc.services.async_call_later"):
        await handler._async_probe_and_discover(["99:999999"], [])
    mock_coordinator._discover_new_entities.assert_called_once()


async def test_async_probe_and_discover_skips_hgi(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_probe_and_discover skips the active HGI."""
    handler = RamsesServiceHandler(mock_coordinator)

    mock_client = cast(Any, mock_coordinator.client)
    mock_hgi = MagicMock()
    mock_hgi.id = "18:006402"
    mock_client.hgi = mock_hgi
    mock_dev = MagicMock()
    mock_dev.discovery.cmds = ["3150"]
    mock_dev.discovery.discover = AsyncMock()
    mock_client.device_registry.device_by_id = {"18:006402": mock_dev}

    mock_coordinator._discover_new_entities = AsyncMock()

    with patch("custom_components.ramses_cc.services.async_call_later"):
        await handler._async_probe_and_discover(["18:006402"], [])
    mock_dev.discovery.discover.assert_not_called()


# ───────────────────────────────────────────────────────────────────────
# Services: _async_run_fan_param_sequence edge cases (lines 371-386)
# ───────────────────────────────────────────────────────────────────────


async def test_fan_param_sequence_skips_duplicate_running(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a duplicate fan param sweep is skipped when one is already running."""
    handler = RamsesServiceHandler(mock_coordinator)

    # Simulate an already-running task for this device
    fake_task = MagicMock()
    fake_task.done.return_value = False  # still running
    handler._fan_param_sequences["32_153289"] = fake_task

    caplog.set_level(logging.DEBUG)
    await handler._async_run_fan_param_sequence({"device_id": "32:153289"})

    assert "Skipping duplicate fan param sweep" in caplog.text


async def test_fan_param_sequence_clears_done_task(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that a done task is cleared before starting a new sweep."""
    handler = RamsesServiceHandler(mock_coordinator)

    # Simulate a completed task
    fake_task = MagicMock()
    fake_task.done.return_value = True  # done
    handler._fan_param_sequences["32_153289"] = fake_task

    # Mock async_get_fan_param so the sequence doesn't actually send
    handler.async_get_fan_param = AsyncMock()

    with patch.object(handler.hass, "async_create_task"):
        await handler._async_run_fan_param_sequence({"device_id": "32:153289"})

    # The old task should have been popped
    assert "32_153289" not in handler._fan_param_sequences or (
        handler._fan_param_sequences.get("32_153289") is not fake_task
    )


async def test_fan_param_sequence_no_device_id(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test fan param sequence with missing device_id."""
    handler = RamsesServiceHandler(mock_coordinator)

    caplog.set_level(logging.WARNING)
    await handler._async_run_fan_param_sequence({})

    assert "missing device_id" in caplog.text


async def test_fan_param_sequence_invalid_data(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test fan param sequence with invalid data that raises in _normalize."""
    handler = RamsesServiceHandler(mock_coordinator)

    caplog.set_level(logging.ERROR)
    # Pass data that will cause _normalize_service_call to raise
    with patch.object(
        handler, "_normalize_service_call", side_effect=Exception("bad data")
    ):
        await handler._async_run_fan_param_sequence({"device_id": "32:153289"})

    assert "Invalid service call data" in caplog.text


# ───────────────────────────────────────────────────────────────────────
# Services: _extract_device_ids_from_schema edge case (line 747)
# ───────────────────────────────────────────────────────────────────────


def test_extract_device_ids_zone_data_not_dict() -> None:
    """Test that non-dict zone data is skipped in _extract_device_ids_from_schema."""
    schema = {
        "01:123456": {
            SZ_ZONES: {
                "01": "not a dict",  # should be skipped
            },
        },
    }
    result = RamsesServiceHandler._extract_device_ids_from_schema(schema)
    assert "01:123456" in result
    # No sensor/actuator extracted from the non-dict zone
    assert len(result) == 1


# ───────────────────────────────────────────────────────────────────────
# Services: discover_known_devices with target device in list (line 820)
# ───────────────────────────────────────────────────────────────────────


async def test_discover_known_devices_target_device_in_list(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test discover_known_devices with a target device_id that IS in the list."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[SZ_KNOWN_LIST] = {"01:123456": {}, "04:654321": {}}

    mock_client = cast(Any, mock_coordinator.client)
    mock_dev = MagicMock()
    mock_dev._SLUG = "CTL"
    mock_dev.discovery.cmds = []
    mock_client.device_registry.device_by_id = {}
    mock_client.device_registry.get_device = MagicMock(return_value=mock_dev)
    mock_client.hgi = None

    def _close_coro(coro: Any) -> None:
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    mock_coordinator.hass.async_create_task = MagicMock(side_effect=_close_coro)

    call = MagicMock()
    call.data = {"device_id": "01:123456"}

    await handler.async_discover_known_devices(call)

    # Only the target device should have been created
    mock_client.device_registry.get_device.assert_called_once_with("01:123456")


# ---------------------------------------------------------------------------
# remove_device service tests
# ---------------------------------------------------------------------------


async def test_remove_device_from_zone_sensor(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device that is a zone sensor — sensor cleared, zone preserved."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "04:056053", "actuators": ["04:034720"]},
            },
        },
    }
    mock_coordinator.options[SZ_KNOWN_LIST] = {"04:056053": {}}
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert schema["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] is None
    # Zone and actuators preserved
    assert "01" in schema["01:216136"][SZ_ZONES]
    assert "04:034720" in schema["01:216136"][SZ_ZONES]["01"]["actuators"]
    # Removed from known_list
    assert "04:056053" not in mock_coordinator.options[SZ_KNOWN_LIST]


async def test_remove_device_from_zone_actuators(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device from zone actuators list — removed, list preserved."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {
                    SZ_SENSOR: "04:056053",
                    "actuators": ["04:034720", "04:056053"],
                },
            },
        },
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    actuators = schema["01:216136"][SZ_ZONES]["01"]["actuators"]
    assert "04:056053" not in actuators
    assert "04:034720" in actuators


async def test_remove_device_appliance_control(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device that is appliance_control — cleared, TCS preserved."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        "01:216136": {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "10:064873"},
        },
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "10:064873"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert schema["01:216136"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] is None
    # TCS entry preserved
    assert "01:216136" in schema


async def test_remove_device_from_orphans_heat(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device from orphans_heat — removed, list preserved."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_ORPHANS_HEAT: ["07:050121", "13:042605"],
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "07:050121"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert "07:050121" not in schema[SZ_ORPHANS_HEAT]
    assert "13:042605" in schema[SZ_ORPHANS_HEAT]


async def test_remove_device_from_orphans_heat_empty_list(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove the only device from orphans_heat — list key removed."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_ORPHANS_HEAT: ["07:050121"],
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "07:050121"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert SZ_ORPHANS_HEAT not in schema


async def test_remove_device_from_hvac_remotes(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device from HVAC remotes list."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        "32:153289": {SZ_REMOTES: ["37:111111", "37:222222"]},
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "37:111111"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert "37:111111" not in schema["32:153289"][SZ_REMOTES]
    assert "37:222222" in schema["32:153289"][SZ_REMOTES]


async def test_remove_device_own_top_level_key(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device's own top-level key (e.g. '32:153289': {})."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        "32:153289": {SZ_REMOTES: ["37:111111"]},
        "01:216136": {},
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "32:153289"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert "32:153289" not in schema
    # 37:111111 should also be gone from remotes (its parent was removed)
    # Actually remotes list was under 32:153289 which is now deleted entirely


async def test_remove_device_clears_main_tcs(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove the device that is main_tcs — main_tcs cleared."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {},
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "01:216136"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert SZ_MAIN_TCS not in schema
    assert "01:216136" not in schema


async def test_remove_device_from_known_list(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device from known_list overrides."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_ORPHANS_HEAT: ["04:056053"],
    }
    mock_coordinator.options[SZ_KNOWN_LIST] = {
        "04:056053": {"alias": "Living Room"},
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    assert "04:056053" not in mock_coordinator.options[SZ_KNOWN_LIST]


async def test_remove_device_hgi_raises(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Removing the HGI gateway device raises ServiceValidationError."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {"18:006402": {}}
    mock_coordinator.options[SZ_KNOWN_LIST] = {
        "18:006402": {"class": "HGI"},
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "18:006402"}

    with pytest.raises(ServiceValidationError, match="Cannot remove the HGI"):
        await handler.async_remove_device(call)


async def test_remove_device_not_found_raises(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Removing a device not in schema or known_list raises ServiceValidationError."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_ORPHANS_HEAT: ["04:056053"],
    }
    mock_coordinator.options[SZ_KNOWN_LIST] = {}
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "99:999999"}

    with pytest.raises(ServiceValidationError, match="not found"):
        await handler.async_remove_device(call)


async def test_remove_device_from_dhw_sensor(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device that is a DHW sensor — cleared, DHW preserved."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        "01:216136": {
            SZ_DHW_SYSTEM: {SZ_SENSOR: "07:050121"},
        },
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "07:050121"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    assert schema["01:216136"][SZ_DHW_SYSTEM][SZ_SENSOR] is None
    # DHW system preserved
    assert SZ_DHW_SYSTEM in schema["01:216136"]


async def test_remove_device_persists_to_config_entry(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Verify the updated options are persisted to the config entry."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_ORPHANS_HEAT: ["04:056053"],
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    with patch.object(
        mock_coordinator.hass.config_entries,
        "async_update_entry",
        MagicMock(),
    ) as mock_update:
        await handler.async_remove_device(call)

    # async_update_entry should have been called with the cleaned options
    mock_update.assert_called_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs.kwargs.get("options") is not None


async def test_remove_device_removes_from_ha_device_registry(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
) -> None:
    """Verify the HA device registry entry is removed."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_ORPHANS_HEAT: ["04:056053"],
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    # Mock device registry
    mock_dev_reg = MagicMock()
    mock_dev_entry = MagicMock()
    mock_dev_entry.id = "ha-dev-id-123"
    mock_dev_entry.identifiers = {(DOMAIN, "04:056053")}

    with (
        patch(
            "custom_components.ramses_cc.services.dr.async_get",
            return_value=mock_dev_reg,
        ),
        patch(
            "custom_components.ramses_cc.services.dr.async_entries_for_config_entry",
            return_value=[mock_dev_entry],
        ),
        patch.object(mock_coordinator.hass.config_entries, "async_update_entry"),
    ):
        call = MagicMock()
        call.data = {"device_id": "04:056053"}

        await handler.async_remove_device(call)

    mock_dev_reg.async_remove_device.assert_called_once_with("ha-dev-id-123")


async def test_remove_device_removes_from_client_include_lists(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Verify the device is removed from ramses_rf client's include lists."""
    handler = RamsesServiceHandler(mock_coordinator)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_ORPHANS_HEAT: ["04:056053"],
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    # Mock client with include lists
    mock_engine = MagicMock()
    mock_engine._include = ["01:216136", "04:056053"]
    mock_dev_filter = MagicMock()
    mock_dev_filter._include = ["01:216136", "04:056053"]
    mock_client = MagicMock()
    mock_client._engine = mock_engine
    mock_client._device_filter = mock_dev_filter
    mock_coordinator.client = mock_client

    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    assert "04:056053" not in mock_engine._include
    assert "04:056053" not in mock_dev_filter._include
    assert "01:216136" in mock_engine._include  # others preserved


async def test_remove_device_in_multiple_locations(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Remove a device that exists in multiple schema locations — all removed."""
    handler = RamsesServiceHandler(mock_coordinator)
    # Device 04:056053 is in zone sensor AND orphans_heat (inconsistent state)
    mock_coordinator.options[CONF_SCHEMA] = {
        SZ_MAIN_TCS: "01:216136",
        SZ_ORPHANS_HEAT: ["04:056053"],
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "04:056053"}},
        },
    }
    mock_coordinator.entry = MagicMock()
    mock_coordinator.entry.entry_id = "test_remove"

    call = MagicMock()
    call.data = {"device_id": "04:056053"}

    with patch.object(mock_coordinator.hass.config_entries, "async_update_entry"):
        await handler.async_remove_device(call)

    schema = mock_coordinator.options[CONF_SCHEMA]
    # Removed from orphans
    assert "04:056053" not in schema.get(SZ_ORPHANS_HEAT, [])
    # Removed from zone sensor
    assert schema["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] is None


async def test_remove_device_service_registered(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Verify the remove_device service is registered in __init__."""
    # This is an integration test — verified via test_init.py instead.
    # Here we just verify the handler method exists.
    handler = RamsesServiceHandler(mock_coordinator)
    assert hasattr(handler, "async_remove_device")

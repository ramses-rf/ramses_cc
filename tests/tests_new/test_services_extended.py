"""Extended tests for RamsesBroker to reach 100% coverage."""

from datetime import datetime as dt, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
from ramses_rf.device.hvac import HvacVentilator
from ramses_rf.entity_base import Child
from ramses_rf.system import Zone


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> RamsesBroker:
    """Return a mock broker with an entry attached."""
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
    # IMPORTANT: Initialize device_by_id as a dict so .get() returns None for missing keys
    broker.client.device_by_id = {}
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


async def test_set_fan_param_generic_exception(mock_broker: RamsesBroker) -> None:
    """Explicitly test the generic exception handler coverage in set_fan_param."""
    mock_broker.client.async_send_cmd.side_effect = Exception("Generic Transport Error")

    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    mock_broker._find_param_entity = MagicMock(return_value=mock_entity)

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
        mock_cmd.set_fan_param.return_value = MagicMock()

        with pytest.raises(HomeAssistantError, match="Failed to set fan parameter"):
            await mock_broker.async_set_fan_param(call_data)

        mock_entity._clear_pending_after_timeout.assert_called_with(0)


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
    mock_broker._store.async_load = AsyncMock(
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

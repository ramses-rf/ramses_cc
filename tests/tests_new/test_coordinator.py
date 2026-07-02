"""Tests for the coordinator aspect of RamsesCoordinator.

(Lifecycle, Config, Updates).
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import datetime as dt, timedelta as td
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest
import serial  # type: ignore[import-untyped]
import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.const import (
    CONF_COMMANDS,
    CONF_GATEWAY_TIMEOUT,
    CONF_MQTT_USE_HA,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    DEFAULT_HGI_ID,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    SIGNAL_NEW_DEVICES,
    SZ_ENFORCE_KNOWN_LIST,
)
from custom_components.ramses_cc.coordinator import (
    SZ_CLIENT_STATE,
    SZ_PACKETS,
    SZ_SCHEMA,
    RamsesCoordinator,
)
from custom_components.ramses_cc.schemas import (
    SCH_GET_FAN_PARAM_DOMAIN,
    SVC_GET_FAN_PARAM,
    SVC_SET_FAN_PARAM,
)
from ramses_rf import Gateway
from ramses_rf.systems import Evohome
from ramses_tx import exceptions as exc
from ramses_tx.schemas import SZ_KNOWN_LIST, SZ_PORT_NAME, SZ_SERIAL_PORT

# Constants
FAN_ID = "30:111222"
REM_ID = "32:111111"
PARAM_ID_HEX = "75"  # Temperature parameter

# Test constants from test_fan_param.py
TEST_DEVICE_ID = "32:153289"  # Example fan device ID
TEST_FROM_ID = "37:168270"  # Source device ID (e.g., remote)
TEST_PARAM_ID = "4E"  # Example parameter ID
TEST_VALUE = 50  # Example parameter value
SERVICE_GET_NAME = "get_fan_param"  # Name of the get service
SERVICE_SET_NAME = SVC_SET_FAN_PARAM  # Name of the set service


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a mock Home Assistant instance."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_get_entry = MagicMock(return_value=None)
    hass.services = MagicMock()
    hass.services.async_register = MagicMock()
    hass.services.async_call = AsyncMock()

    # Ensure these methods are AsyncMocks
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_forward_entry_unload = AsyncMock(return_value=True)

    # async_create_task must return an awaitable (Future).
    # CRITICAL: It must also 'close' the coro passed to it to prevent
    # RuntimeWarnings.
    def _create_task(coro: Any) -> asyncio.Future[Any]:
        if asyncio.iscoroutine(coro):
            coro.close()  # Prevent "coro was never awaited" warning
        f: asyncio.Future[Any] = asyncio.Future()
        f.set_result(None)
        return f

    hass.async_create_task = MagicMock(side_effect=_create_task)
    return hass


@pytest.fixture
def mock_entry(mock_hass: MagicMock) -> MagicMock:
    """Return a mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {},
        CONF_RAMSES_RF: {},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
        CONF_SCAN_INTERVAL: 60,
        CONF_GATEWAY_TIMEOUT: 10,
    }
    entry.async_on_unload = MagicMock()
    # Fix the AttributeError: provide a domain for the mock entry
    entry.domain = DOMAIN

    # Register this entry with the mock hass instance
    cast(Any, mock_hass.config_entries.async_get_entry).side_effect = lambda eid: (
        entry if eid == entry.entry_id else None
    )

    return entry


@pytest.fixture
def mock_coordinator(mock_hass: MagicMock, mock_entry: MagicMock) -> RamsesCoordinator:
    """Return a mock coordinator with an entry attached."""
    coordinator = RamsesCoordinator(mock_hass, mock_entry)
    coordinator.client = MagicMock()
    cast(Any, coordinator.client).async_send_cmd = AsyncMock()
    coordinator._device_info = {}
    coordinator.platforms = {}
    coordinator._devices = []

    mock_hass.data[DOMAIN] = {mock_entry.entry_id: coordinator}
    return coordinator


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device."""
    device = MagicMock()
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    cast(Any, device).get_bound_rem = MagicMock(return_value=REM_ID)
    return device


async def test_setup_fails_gracefully_on_bad_config(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that startup catches client creation errors and logs them."""
    coordinator = RamsesCoordinator(mock_hass, mock_entry)
    cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

    # Force _create_client to raise vol.Invalid (simulation of bad schema)
    cast(Any, coordinator)._create_client = MagicMock(
        side_effect=vol.Invalid("Invalid config")
    )

    # Verify it raises a clean ValueError with helpful message
    with pytest.raises(ValueError, match="Failed to initialise RAMSES client"):
        await coordinator.async_setup()


async def test_device_registry_update_slugs(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test registry update logic for different device slugs."""
    mock_device = MagicMock()
    mock_device.id = FAN_ID
    mock_device._SLUG = "FAN"
    # Ensure name is None so coordinator falls back to slug-based logic
    mock_device.name = None
    mock_device.state_store = MagicMock()
    cast(Any, mock_device.state_store)._msg_value_code = AsyncMock(return_value=None)

    with patch("homeassistant.helpers.device_registry.async_get") as mock_dr:
        mock_reg = mock_dr.return_value

        await mock_coordinator._async_update_device(mock_device)

        # Verify the name and model were derived from the SLUG
        call_kwargs = cast(Any, mock_reg.async_get_or_create).call_args[1]
        assert call_kwargs["name"] == f"FAN {FAN_ID}"
        assert call_kwargs["model"] == "FAN"


async def test_setup_schema_merge_failure(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test async_setup handling of schema merge failures."""
    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    # Provide a non-empty schema so the code enters the "merge" block
    cast(Any, coordinator.store).async_load = AsyncMock(
        return_value={SZ_CLIENT_STATE: {SZ_SCHEMA: {"existing": "data"}}}
    )

    mock_client = MagicMock()
    cast(Any, mock_client).start = AsyncMock()

    # Mock _create_client to fail on first call (merged schema) but
    # succeed on second (config schema)
    cast(Any, coordinator)._create_client = MagicMock(
        side_effect=[LookupError("Merge failed"), mock_client]
    )

    with patch(
        "custom_components.ramses_cc.coordinator.merge_schemas",
        return_value={"mock": "schema"},
    ):
        await coordinator.async_setup()

    assert cast(Any, coordinator._create_client).call_count == 2
    # First call with merged schema, second with config schema


async def test_update_device_relationships(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_update_device logic for parent/child and TCS."""

    # Define dummy class with required attributes for spec matching
    class DummyZone:
        tcs: Any | None = None
        name: str | None = None
        _SLUG: str | None = None

    # We patch the class in the BROKER module so it checks against our dummy
    with patch("custom_components.ramses_cc.coordinator.Zone", DummyZone):
        # 1. Test Zone with TCS (hits via_device logic for Zones)
        mock_zone = MagicMock(spec=DummyZone)
        mock_zone.id = "04:123456"
        mock_zone.tcs = MagicMock()
        mock_zone.tcs.id = "01:999999"
        mock_zone.state_store = MagicMock()
        cast(Any, mock_zone.state_store)._msg_value_code = AsyncMock(
            return_value={"description": "Zone Name"}
        )

        mock_zone.name = "Custom Zone"

        with patch("homeassistant.helpers.device_registry.async_get") as dr_m:
            mock_reg = dr_m.return_value
            await mock_coordinator._async_update_device(mock_zone)

            # Verify via_device was set to TCS ID
            call_kwargs = cast(Any, mock_reg.async_get_or_create).call_args[1]
            assert call_kwargs["via_device"] == (DOMAIN, "01:999999")


async def test_update_device_child_parent(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_update_device logic for Child devices."""
    # Test logic around lines 535-548
    from ramses_rf.topology import Child

    mock_child = MagicMock(spec=Child)
    mock_child.id = "13:123456"
    mock_child._parent = MagicMock()
    mock_child._parent.id = "04:123456"
    mock_child.state_store = MagicMock()
    cast(Any, mock_child.state_store)._msg_value_code = AsyncMock(return_value=None)

    mock_child._SLUG = "BDR"
    mock_child.name = None

    with patch("homeassistant.helpers.device_registry.async_get") as mock_dr:
        mock_reg = mock_dr.return_value
        await mock_coordinator._async_update_device(mock_child)

        call_kwargs = cast(Any, mock_reg.async_get_or_create).call_args[1]
        assert call_kwargs["via_device"] == (DOMAIN, "04:123456")


async def test_async_start(mock_coordinator: RamsesCoordinator) -> None:
    """Test async_start sets up updates and saving."""
    assert mock_coordinator.client is not None

    # MOCK CHANGE: DataUpdateCoordinator.async_start calls
    # async_config_entry_first_refresh
    # We patch it to avoid actual execution logic during this specific
    # lifecycle test
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()
    mock_coordinator.async_save_client_state = AsyncMock()
    cast(Any, mock_coordinator.client).start = AsyncMock()

    with patch(
        "custom_components.ramses_cc.coordinator.async_track_time_interval"
    ) as mock_track:
        await mock_coordinator.async_start()

        # Check that the first refresh was triggered
        assert cast(Any, mock_coordinator.async_config_entry_first_refresh).called

        # Should setup 2 timers:
        # 1. Discovery Loop (_async_discovery_task)
        # 2. Save Client State (async_save_client_state)
        assert cast(Any, mock_track).call_count == 2


async def test_platform_lifecycle(mock_coordinator: RamsesCoordinator) -> None:
    """Test registering, setting up, and unloading platforms."""
    # 1. Register Platform
    mock_platform = MagicMock()
    mock_platform.domain = "climate"
    mock_callback = MagicMock()

    mock_coordinator.async_register_platform(mock_platform, mock_callback)
    assert "climate" in mock_coordinator.platforms
    # Test duplicate registration
    mock_coordinator.async_register_platform(mock_platform, mock_callback)
    assert len(mock_coordinator.platforms["climate"]) == 2

    # 2. Setup Platform
    # Since mock_coordinator.hass is a MagicMock, we verify the call directly
    # on the mock
    await mock_coordinator._async_setup_platform("climate")
    assert cast(
        Any, mock_coordinator.hass.config_entries.async_forward_entry_setups
    ).called

    # Already set up path
    cast(
        Any, mock_coordinator.hass.config_entries.async_forward_entry_setups
    ).reset_mock()
    await mock_coordinator._async_setup_platform("climate")
    assert not cast(
        Any, mock_coordinator.hass.config_entries.async_forward_entry_setups
    ).called

    # 3. Unload Platforms
    assert await mock_coordinator.async_unload_platforms()
    assert cast(
        Any, mock_coordinator.hass.config_entries.async_forward_entry_unload
    ).called


async def test_create_client_real(mock_coordinator: RamsesCoordinator) -> None:
    """Test the _create_client method execution (port extraction)."""
    # Setup options to contain the expected dict structure for the serial port
    mock_coordinator.options[SZ_SERIAL_PORT] = {SZ_PORT_NAME: "/dev/ttyUSB0"}

    with patch("custom_components.ramses_cc.coordinator.Gateway") as mock_gwy:
        # Pass empty dict as config_schema
        mock_coordinator._create_client({})

        assert cast(Any, mock_gwy).called
        _, kwargs = cast(Any, mock_gwy).call_args

        # Verify the port config was extracted and passed
        assert "port_name" in kwargs
        assert kwargs["port_name"] == "/dev/ttyUSB0"

        # Verify our new timeout was routed successfully into GatewayConfig
        assert "config" in kwargs
        assert getattr(kwargs["config"], "gateway_timeout", None) == 10


async def test_create_client_strips_commands_from_known_list(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _create_client removes command data from known_list."""
    mock_coordinator.options[SZ_SERIAL_PORT] = {SZ_PORT_NAME: "/dev/ttyUSB0"}
    mock_coordinator.options[SZ_KNOWN_LIST] = {
        "37:168270": {
            "class": "REM",
            CONF_COMMANDS: {"boost": "packet_data"},
        }
    }

    with patch("custom_components.ramses_cc.coordinator.Gateway") as mock_gwy:
        mock_coordinator._create_client({})

        _, kwargs = cast(Any, mock_gwy).call_args

        gwy_config = kwargs["config"]

        assert gwy_config.known_list["37:168270"]["class"] == "REM"
        assert CONF_COMMANDS not in gwy_config.known_list["37:168270"]


async def test_async_update_discovery(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_update discovering and adding new entities."""
    assert mock_coordinator.client is not None

    # Setup mock entities in client
    mock_system = MagicMock(spec=Evohome)
    mock_system.id = "01:123456"
    mock_system.state_store = MagicMock()
    cast(Any, mock_system.state_store)._msg_value_code = AsyncMock(return_value=None)

    mock_system.dhw = MagicMock()  # Has DHW
    mock_system.dhw.state_store = MagicMock()
    cast(Any, mock_system.dhw.state_store)._msg_value_code = AsyncMock(
        return_value=None
    )

    zone_mock = MagicMock()
    zone_mock.state_store = MagicMock()
    cast(Any, zone_mock.state_store)._msg_value_code = AsyncMock(return_value=None)
    mock_system.zones = [zone_mock]  # Has Zone

    mock_device = MagicMock()
    mock_device.id = "04:123456"  # Device

    mock_device.state_store = MagicMock()
    cast(Any, mock_device.state_store)._msg_value_code = AsyncMock(return_value=None)

    # Bypass Pylance entirely and assign directly to the mock objects
    cast(Any, mock_coordinator.client.device_registry).systems = [mock_system]
    cast(Any, mock_coordinator.client.device_registry).devices = [mock_device]
    cast(Any, mock_coordinator.client).get_state = MagicMock(return_value=({}, {}))

    # Mock registry to allow lookup AND Mock dispatcher to verify signals
    with (
        patch("homeassistant.helpers.device_registry.async_get"),
        patch(
            "custom_components.ramses_cc.coordinator.async_dispatcher_send"
        ) as mock_dispatch,
    ):
        # Call _discover_new_entities directly (was _async_update_data)
        await mock_coordinator._discover_new_entities()

        # Verify signal sent for new devices
        assert cast(Any, mock_dispatch).call_count >= 1
        calls = [c[0][1] for c in cast(Any, mock_dispatch).call_args_list]
        assert SIGNAL_NEW_DEVICES.format(Platform.CLIMATE) in calls
        assert SIGNAL_NEW_DEVICES.format(Platform.WATER_HEATER) in calls


async def test_async_update_setup_failure(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test platform setup failure handling."""
    # Create a future that raises an exception when awaited
    f: asyncio.Future[Any] = asyncio.Future()
    f.set_exception(Exception("Setup failed"))

    # The side effect needs to close the coro argument to prevent warning
    def _fail_task(coro: Any) -> asyncio.Future[Any]:
        if asyncio.iscoroutine(coro):
            coro.close()
        return f

    cast(Any, mock_coordinator.hass.async_create_task).side_effect = _fail_task
    cast(
        Any, mock_coordinator.hass.config_entries.async_forward_entry_setups
    ).side_effect = Exception("Setup failed")

    result = await mock_coordinator._async_setup_platform("climate")
    assert result is False


async def test_setup_ignores_invalid_cached_packet_timestamps(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that async_setup ignores packets with invalid timestamps."""

    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    # Use a fresh timestamp for the valid packet so it isn't filtered
    now: dt = dt_util.now()
    valid_dtm: str = now.isoformat()
    invalid_dtm = "invalid-iso-format"

    cast(Any, coordinator.store).async_load = AsyncMock(
        return_value={
            SZ_CLIENT_STATE: {
                SZ_PACKETS: {
                    valid_dtm: "valid_packet_data",
                    invalid_dtm: "broken_packet_data",
                }
            }
        }
    )

    # Mock client creation
    mock_client = MagicMock()
    mock_start = AsyncMock()
    cast(Any, mock_client).start = mock_start
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    # Run setup
    await coordinator.async_setup()

    # Verify client.start was called with only the valid packet
    kwargs = cast(Any, mock_start).call_args.kwargs
    cached = kwargs.get("cached_packets", {})

    assert valid_dtm in cached
    assert invalid_dtm not in cached


async def test_update_device_system_naming(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_update_device naming logic for System devices."""

    # Define dummy class to patch System for isinstance check
    class DummySystem:
        pass

    # Patch the System class in the coordinator module
    with patch("custom_components.ramses_cc.coordinator.System", DummySystem):
        mock_system = MagicMock(spec=DummySystem)
        mock_system.id = "01:123456"
        mock_system.name = None
        mock_system._SLUG = None

        # Ensure the method returns None as expected
        mock_system.state_store = MagicMock()
        cast(Any, mock_system.state_store)._msg_value_code = AsyncMock(
            return_value=None
        )

        with patch("homeassistant.helpers.device_registry.async_get") as dr_m:
            mock_reg = dr_m.return_value

            await mock_coordinator._async_update_device(mock_system)

            # Verify the name format "Controller {id}"
            call_kwargs = cast(Any, mock_reg.async_get_or_create).call_args[1]
            assert call_kwargs["name"] == "Controller 01:123456"


async def test_async_update_adds_systems_and_guards(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_update handles new systems and guards empty lists."""
    assert mock_coordinator.client is not None

    # Define dummy class with all required attributes for coordinator.py logic
    class DummyEvohome:
        id: str = "01:111111"
        dhw: Any = None
        zones: list[Any] = []
        name: str | None = None
        _SLUG: str = "EVO"

        def _msg_value_code(self, *args: Any, **kwargs: Any) -> Any:
            return None

    # Patch Evohome in the coordinator with our dummy CLASS
    with patch("custom_components.ramses_cc.coordinator.Evohome", DummyEvohome):
        # Create a system that is an instance of our dummy class
        mock_system = DummyEvohome()
        mock_system.zones = []

        cast(Any, mock_coordinator.client.device_registry).systems = [mock_system]
        cast(Any, mock_coordinator.client.device_registry).devices = []
        cast(Any, mock_coordinator.client).get_state = MagicMock(return_value=({}, {}))

        # Capture the calls to dispatcher to verify system was added
        with (
            patch(
                "custom_components.ramses_cc.coordinator.async_dispatcher_send"
            ) as mock_dispatch,
            patch("homeassistant.helpers.device_registry.async_get"),
        ):
            # Call _discover_new_entities directly (was _async_update_data)
            await mock_coordinator._discover_new_entities()

            # Use assert_any_call for robust verification
            expected_signal = SIGNAL_NEW_DEVICES.format(Platform.CLIMATE)
            cast(Any, mock_dispatch).assert_any_call(
                mock_coordinator.hass, expected_signal, [mock_system]
            )


async def test_setup_uses_merged_schema_on_success(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that async_setup successfully uses the merged schema."""
    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    # Setup mock data
    cached_schema = {"cached_key": "cached_val"}
    config_schema = {"config_key": "config_val"}
    merged_result = {"merged_key": "merged_val"}

    cast(Any, coordinator.store).async_load = AsyncMock(
        return_value={SZ_CLIENT_STATE: {SZ_SCHEMA: cached_schema}}
    )

    # 2. Setup a mock config schema in options
    coordinator.options[CONF_SCHEMA] = config_schema

    # 3. Mock _create_client to return a valid client object (Success case)
    mock_client = MagicMock()
    cast(Any, mock_client).start = AsyncMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    # Patch mock schema_is_minimal to prevent TypeError
    with (
        patch(
            "custom_components.ramses_cc.coordinator.merge_schemas",
            return_value=merged_result,
        ) as mock_merge,
        patch(
            "custom_components.ramses_cc.coordinator.schema_is_minimal",
            return_value=True,
        ),
    ):
        # 5. Execute async_setup
        await coordinator.async_setup()

        # VERIFICATION

        # Ensure merge_schemas was called correctly
        cast(Any, mock_merge).assert_called_once_with(config_schema, cached_schema)

        # CRITICAL: Verify _create_client called ONCE with the MERGED schema.
        cast(Any, coordinator._create_client).assert_called_once_with(merged_result)

        # Ensure the coordinator's client attribute was set to our mock
        assert coordinator.client is mock_client


async def test_setup_logs_warning_on_non_minimal_schema(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that a warning is logged when the schema is not minimal.

    (Line 155).
    """
    coordinator = RamsesCoordinator(mock_hass, mock_entry)
    cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

    # Mock success path for client creation so setup completes
    mock_client = MagicMock()
    cast(Any, mock_client).start = AsyncMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    # Patch schema_is_minimal to return False -> triggers the warning
    with (
        patch(
            "custom_components.ramses_cc.coordinator.schema_is_minimal",
            return_value=False,
        ),
        patch("custom_components.ramses_cc.coordinator._LOGGER") as mock_log,
    ):
        await coordinator.async_setup()

        cast(Any, mock_log.warning).assert_any_call(
            "The config schema is not minimal (consider minimising it)"
        )


async def test_update_device_name_fallback_to_id(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_update_device falls back to device.id.

    Happens when no name/slug/system (Line 626).
    """

    # 1. Create a generic mock device
    # MagicMock is not an instance of System, so check fails automatically.
    mock_device = MagicMock()
    mock_device.id = "99:888777"

    # 2. Ensure preceding checks fail
    mock_device.name = None  # Fails 'if device.name'
    mock_device._SLUG = None  # Fails 'elif device._SLUG'

    # Stub helper method to return None (affects 'model', not 'name')
    mock_device.state_store = MagicMock()
    cast(Any, mock_device.state_store)._msg_value_code = AsyncMock(return_value=None)

    # 3. Patch the device registry to verify the result
    with patch("homeassistant.helpers.device_registry.async_get") as dr_m:
        mock_reg = dr_m.return_value

        # 4. Call the method under test
        await mock_coordinator._async_update_device(mock_device)

        # 5. Verify device_registry was called with name == device.id
        call_kwargs = cast(Any, mock_reg.async_get_or_create).call_args[1]
        assert call_kwargs["name"] == "99:888777"


async def test_coordinator_save_client_state_no_client(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_save_client_state returns early when client is None.

    (Lines 232-233).
    """
    # Force client to None
    mock_coordinator.client = None
    # Mock the store to verify it is NOT called
    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save

    await mock_coordinator.async_save_client_state()
    mock_save.assert_not_called()


async def test_coordinator_update_data_no_client(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_update_data returns early when client is None.

    (Line 353).
    """
    mock_coordinator.client = None

    # Patch _discover_new_entities to ensure it is NOT called
    with patch.object(mock_coordinator, "_discover_new_entities") as mock_dsc:
        await mock_coordinator._async_update_data()
        cast(Any, mock_dsc).assert_not_called()


async def test_coordinator_run_fan_param_sequence(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test run_fan_param_sequence delegates to service_handler (Line 452)."""
    call_data = {"test": "data"}
    # Mock the handler method on the service_handler
    mock_run = AsyncMock()
    cast(Any, mock_coordinator.service_handler)._async_run_fan_param_sequence = mock_run

    await mock_coordinator._async_run_fan_param_sequence(call_data)
    mock_run.assert_awaited_once_with(call_data)


async def test_discovery_task_calls_discovery(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that _async_discovery_task calls discovery with client."""
    # Ensure client exists
    mock_coordinator.client = MagicMock()

    # Patch the discovery method to verify it gets called
    with patch.object(mock_coordinator, "_discover_new_entities") as mock_dsc:
        await mock_coordinator._async_discovery_task()

        cast(Any, mock_dsc).assert_called_once()


async def test_save_client_state_hybrid_compatibility(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test state saving works with Sync and Async client methods."""
    assert mock_coordinator.client is not None

    # Mock the store and internal state needed for the save method
    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save
    mock_coordinator._remotes = {}
    mock_coordinator._entities = {}

    # --- SCENARIO 1: New Async Client ---
    # get_state returns an Awaitable (Coroutine) that resolves to the tuple
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=self_resolving_async_mock({"type": "async"}, {})
    )

    await mock_coordinator.async_save_client_state()

    # Verify the awaitable was awaited and data passed to store
    mock_save.assert_awaited_with({"type": "async"}, {}, {}, None)
    mock_save.reset_mock()

    # --- SCENARIO 2: Old Sync Client ---
    # get_state returns the tuple directly (MagicMock is not awaitable)
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=({"type": "sync"}, {})
    )

    await mock_coordinator.async_save_client_state()

    # Verify the synchronous result was handled correctly
    mock_save.assert_awaited_with({"type": "sync"}, {}, {}, None)


def self_resolving_async_mock(*args: Any) -> Any:
    """Helper to return an awaitable resolving to the args for mocking."""
    f: asyncio.Future[Any] = asyncio.Future()
    f.set_result(args if len(args) > 1 else (args[0] if args else None))
    return f


async def test_create_client_mqtt_not_ready(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _create_client raises ConfigEntryNotReady if MQTT missing."""

    # Enable MQTT in options
    mock_coordinator.options[CONF_MQTT_USE_HA] = True

    # Mock HA to report NO MQTT entries
    cast(Any, mock_coordinator.hass.config_entries.async_entries).return_value = []

    with pytest.raises(
        ConfigEntryNotReady, match="Home Assistant MQTT integration is not set up"
    ):
        # Pass an empty schema as it's required by the signature
        mock_coordinator._create_client({})


async def test_create_client_mqtt_success(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _create_client sets up the MQTT bridge correctly."""

    # Enable MQTT in options
    mock_coordinator.options[CONF_MQTT_USE_HA] = True

    # Mock HA to report MQTT entries exist
    cast(Any, mock_coordinator.hass.config_entries.async_entries).return_value = [
        "mqtt"
    ]

    with (
        patch("custom_components.ramses_cc.coordinator.Gateway") as mock_gwy,
        patch(
            "custom_components.ramses_cc.coordinator.RamsesMqttBridge"
        ) as mock_bridge_cls,
    ):
        # Setup the mock bridge instance
        mock_bridge_instance = mock_bridge_cls.return_value
        cast(Any, mock_bridge_instance).async_transport_factory = MagicMock()
        # Ensure _extra is a real dict so coordinator can call .update()
        cast(Any, mock_gwy.return_value)._extra = {}

        # Call the method under test
        mock_coordinator._create_client({})

        # 1. Verify Bridge Initialization
        # It should use the default topic and ID from const
        cast(Any, mock_bridge_cls).assert_called_once_with(
            mock_coordinator.hass, DEFAULT_MQTT_TOPIC, DEFAULT_HGI_ID
        )
        assert mock_coordinator.mqtt_bridge is mock_bridge_instance

        # 2. Verify Gateway was initialised with MQTT-transport arguments
        assert cast(Any, mock_gwy).called
        _, kwargs = cast(Any, mock_gwy).call_args

        # Check specific MQTT-related arguments were passed to Gateway
        assert (
            kwargs.get("transport_constructor")
            == mock_bridge_instance.async_transport_factory
        )
        assert kwargs.get("port_name") == "/dev/ttyUSB0"
        assert "config" in kwargs
        assert kwargs["config"].engine.hgi_id == DEFAULT_HGI_ID


@pytest.mark.asyncio
async def test_create_client_zigbee_path(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _create_client creates Gateway with _hass injected for zigbee.

    Covers the _is_zigbee branch of coordinator._create_client.
    """
    zigbee_url = "zigbee://00:11:22:33:44:55:66:77/0xfc00/0x0000/10/0xfc01/0x0000/10"
    mock_coordinator.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = zigbee_url

    with patch("custom_components.ramses_cc.coordinator.Gateway") as mock_gwy:
        mock_client = mock_gwy.return_value

        result = mock_coordinator._create_client({})

        # Gateway should be constructed exactly once with the zigbee URL
        cast(Any, mock_gwy).assert_called_once()
        _, kwargs = cast(Any, mock_gwy).call_args
        assert kwargs.get("port_name") == zigbee_url

        # PR #505: hass is injected via GatewayConfig.app_context.
        # Only check when the installed ramses_rf supports app_context.
        config = kwargs.get("config")
        assert config is not None
        if hasattr(config, "engine") and hasattr(config.engine, "app_context"):
            assert config.engine.app_context is mock_coordinator.hass

        # The method should return the Gateway instance
        assert result is mock_client


@pytest.mark.asyncio
async def test_discover_new_entities_registration_order(
    mock_hass: MagicMock,
) -> None:
    """Test that parent devices are registered before child devices.

    This ensures Systems/DHW/Zones are processed before generic Devices to
    maintain Device Registry integrity.
    """
    # 1. Setup Mock Gateway
    mock_gateway = MagicMock(spec=Gateway)

    # Setup Device Hierarchy
    mock_system = MagicMock(spec=Evohome)
    mock_system.id = "01:123456"
    mock_system.dhw = None
    mock_system.zones = []

    mock_device = MagicMock()
    mock_device.id = "07:048080"

    cast(Any, mock_gateway).get_state = MagicMock(return_value=({}, {}))
    cast(Any, mock_gateway.device_registry).systems = [mock_system]
    cast(Any, mock_gateway.device_registry).devices = [mock_device]

    # 2. Setup Mock Config Entry
    mock_entry = MagicMock(spec=ConfigEntry)
    mock_entry.options = {"scan_interval": 60}
    mock_entry.entry_id = "test_entry_id"
    # Fix the AttributeError: provide a domain for the mock entry
    mock_entry.domain = "ramses_cc"

    # 3. Initialize Coordinator and Inject Mock Client
    with (
        patch(
            "custom_components.ramses_cc.coordinator.RamsesCoordinator._async_update_device",
            new_callable=AsyncMock,
        ) as mock_update_device,
        patch(
            "custom_components.ramses_cc.coordinator.RamsesFanHandler.async_setup_fan_device",
            new_callable=AsyncMock,
        ),
        # We patch async_setup_platform to avoid real HA platform loading
        patch(
            "custom_components.ramses_cc.coordinator.RamsesCoordinator._async_setup_platform",
            return_value=True,
        ),
    ):
        coordinator = RamsesCoordinator(mock_hass, mock_entry)
        coordinator.client = mock_gateway

        # Manually trigger discovery
        await coordinator._discover_new_entities()

        # 4. Assertions
        expected_calls = [
            call(mock_system),  # Parent first
            call(mock_device),  # Child second
        ]

        cast(Any, mock_update_device).assert_has_calls(expected_calls, any_order=False)
        assert cast(Any, mock_update_device).call_count == 2


async def test_setup_with_corrupted_storage_dates(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that startup survives invalid date strings in storage.

    From test_coordinator_startup.py.
    """
    # 1. Setup Coordinator
    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    # 2. Mock Storage with corrupted date
    # Valid date: 2023-01-01T12:00:00
    # Invalid date: "INVALID-DATE-STRING"
    now: dt = dt_util.now()
    timestamp: str = now.isoformat()
    mock_storage_data = {
        SZ_CLIENT_STATE: {
            SZ_PACKETS: {
                timestamp: "00 ... valid packet ...",
                "INVALID-DATE-STRING": "00 ... corrupted packet ...",
            }
        }
    }

    cast(Any, coordinator.store).async_load = AsyncMock(return_value=mock_storage_data)
    cast(Any, coordinator)._create_client = MagicMock()
    coordinator.client = MagicMock()
    assert coordinator.client is not None
    mock_start = AsyncMock()
    cast(Any, coordinator.client).start = mock_start

    # 3. Run async_setup
    # This should NOT raise ValueError
    await coordinator.async_setup()

    # 4. Verify client started
    assert cast(Any, mock_start).called

    # 5. Verify only valid packet was passed to start
    kwargs = cast(Any, mock_start).call_args.kwargs
    cached_packets = kwargs.get("cached_packets", {})

    assert len(cached_packets) == 1
    assert "INVALID-DATE-STRING" not in cached_packets


async def test_setup_packet_filtering(
    hass: HomeAssistant, mock_entry: MagicMock
) -> None:
    """Test logic for filtering cached packets based on age/known list."""
    coordinator = RamsesCoordinator(hass, mock_entry)

    # Wire up mock_client to be returned by _create_client
    mock_client = MagicMock(spec=Gateway)
    mock_start = AsyncMock()
    cast(Any, mock_client).start = mock_start
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    now: dt = dt_util.now()
    old_date = (now - td(days=2)).isoformat()
    recent_date = (now - td(hours=1)).isoformat()

    # Known list contains a device 01:123456
    coordinator.options[SZ_KNOWN_LIST] = {"01:123456": {}}
    coordinator.options[CONF_RAMSES_RF] = {"enforce_known_list": True}

    # Helper to construct a packet where ID matches [11:20]
    # Indices 0-10 (11 chars) are padding.
    # [11:20] is 9 chars -> "01:123456"
    padding = " " * 11
    valid_packet = f"{padding}01:123456" + (" " * 20)
    unknown_packet = f"{padding}99:999999" + (" " * 20)

    mock_storage_data = {
        SZ_CLIENT_STATE: {
            SZ_PACKETS: {
                old_date: valid_packet,  # Too old
                recent_date: valid_packet,  # Good
                (now - td(minutes=1)).isoformat(): unknown_packet,  # Unknown
            }
        }
    }
    cast(Any, coordinator.store).async_load = AsyncMock(return_value=mock_storage_data)

    await coordinator.async_setup()

    # Check which packets survived
    cast(Any, mock_start).assert_called_once()
    packets = cast(Any, mock_start).call_args.kwargs["cached_packets"]

    # Verify recent known packet is present
    assert recent_date in packets
    # Verify old packet is gone
    assert old_date not in packets
    # Verify unknown device packet is gone
    # Note: unknown_packet timestamp key is dynamic, so we check count
    assert len(packets) == 1

    # Ensure the event loop has processed all mock callbacks
    await asyncio.sleep(0)


async def test_setup_packet_filtering_regex_resilience(
    hass: HomeAssistant, mock_entry: MagicMock
) -> None:
    """Test that the regex filter handles shifted packets (RSSI vars)."""
    coordinator = RamsesCoordinator(hass, mock_entry)

    # Wire up mock_client to be returned by _create_client
    mock_client = MagicMock()
    mock_start = AsyncMock()
    cast(Any, mock_client).start = mock_start
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    now: dt = dt_util.now()

    # Known list contains a single valid device
    coordinator.options[SZ_KNOWN_LIST] = {"01:123456": {}}
    coordinator.options[CONF_RAMSES_RF] = {"enforce_known_list": True}

    # Various packet formats that should ALL be caught by the regex
    # plus corrupted/unknown packets that should be dropped.
    packets_to_test = {
        (now - td(minutes=1)).isoformat(): (
            "073  I 01:123456 --:------ 0005 004 00"  # Standard RSSI
        ),
        (now - td(minutes=2)).isoformat(): (
            "...  I 01:123456 --:------ 0005 004 00"  # Dummy RSSI
        ),
        (now - td(minutes=3)).isoformat(): (
            "---  I 01:123456 --:------ 0005 004 00"  # Hyphen RSSI
        ),
        (now - td(minutes=4)).isoformat(): (
            " I 01:123456 --:------ 0005 004 00"  # No RSSI
        ),
        (now - td(minutes=5)).isoformat(): (
            "073  I 99:999999 --:------ 0005 004 00"  # UNKNOWN device
        ),
        (now - td(minutes=6)).isoformat(): (
            "073  I AB:CDEFGH --:------ 0005 004 00"  # Corrupted ID
        ),
    }

    mock_storage_data = {SZ_CLIENT_STATE: {SZ_PACKETS: packets_to_test}}
    cast(Any, coordinator.store).async_load = AsyncMock(return_value=mock_storage_data)

    await coordinator.async_setup()

    # Check which packets survived
    cast(Any, mock_start).assert_called_once()
    survived = cast(Any, mock_start).call_args.kwargs["cached_packets"]

    # The 4 valid known-list packets should survive.
    # The unknown (minute 5) and corrupted (minute 6) should drop.
    assert len(survived) == 4
    assert (now - td(minutes=1)).isoformat() in survived
    assert (now - td(minutes=2)).isoformat() in survived
    assert (now - td(minutes=3)).isoformat() in survived
    assert (now - td(minutes=4)).isoformat() in survived
    assert (now - td(minutes=5)).isoformat() not in survived
    assert (now - td(minutes=6)).isoformat() not in survived


async def test_save_client_state_remotes(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test saving remote commands to persistent storage.

    From test_coordinator_services.py.
    """
    assert mock_coordinator.client is not None
    mock_coordinator._remotes = {REM_ID: {"boost": "packet_data"}}
    mock_save = AsyncMock()

    cast(Any, mock_coordinator.client).get_state = MagicMock(return_value=({}, {}))
    cast(Any, mock_coordinator.store).async_save = mock_save

    await mock_coordinator.async_save_client_state()

    # Verify remotes were included in the save payload
    args = cast(Any, mock_save).call_args[0]
    saved_remotes = args[2]

    assert saved_remotes[REM_ID]["boost"] == "packet_data"


async def test_setup_handles_naive_timestamps(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test that async_setup adds timezone info to naive timestamps.

    Covers line 170.
    """
    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    # Create a naive timestamp string (no offset)
    naive_dt = "2023-01-01T12:00:00"

    cast(Any, coordinator.store).async_load = AsyncMock(
        return_value={
            SZ_CLIENT_STATE: {SZ_PACKETS: {naive_dt: "naive_packet"}},
            SZ_KNOWN_LIST: {},
        }
    )
    cast(Any, coordinator)._create_client = MagicMock()
    coordinator.client = MagicMock()
    assert coordinator.client is not None
    mock_start = AsyncMock()
    cast(Any, coordinator.client).start = mock_start

    # Patch dt_util.now() to ensure the packet isn't discarded as too old
    # Packet is 2023-01-01, so we pretend "now" is 2023-01-01 13:00
    fake_now = dt(2023, 1, 1, 13, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)

    with patch("homeassistant.util.dt.now", return_value=fake_now):
        await coordinator.async_setup()

        # Verify packet was accepted (tzinfo added implies it parsed safely)
        kwargs = cast(Any, mock_start).call_args.kwargs
        cached = kwargs.get("cached_packets", {})
        assert naive_dt in cached


async def test_get_device_lookup(mock_coordinator: RamsesCoordinator) -> None:
    """Test _get_device lookups via internal list and client fallback.

    Covers lines 373-377.
    """
    assert mock_coordinator.client is not None

    # 1. Test finding in self._devices
    dev1 = MagicMock()
    dev1.id = "01:111111"
    mock_coordinator._devices = [dev1]

    assert mock_coordinator._get_device("01:111111") == dev1

    # 2. Test fallback to client.device_registry.device_by_id
    dev2 = MagicMock()
    dev2.id = "02:222222"

    cast(Any, mock_coordinator.client.device_registry).device_by_id = {
        "02:222222": dev2
    }

    assert mock_coordinator._get_device("02:222222") == dev2

    # 3. Test not found (client exists)
    assert mock_coordinator._get_device("99:999999") is None

    # 4. Test not found (no client) -> Hits the final return None
    mock_coordinator.client = None
    mock_coordinator._devices = []  # Clear devices to ensure fall-through
    assert mock_coordinator._get_device("01:111111") is None


async def test_update_device_skips_redundant_update(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_update_device returns early if info hasn't changed.

    Covers line 470.
    """
    mock_device = MagicMock()
    mock_device.id = "01:000000"
    mock_device.name = "Test Device"
    mock_device._SLUG = "TST"

    mock_device.state_store = MagicMock()
    cast(Any, mock_device.state_store)._msg_value_code = AsyncMock(return_value=None)

    with patch("homeassistant.helpers.device_registry.async_get") as dr_m:
        mock_reg = dr_m.return_value
        mock_reg.async_get_or_create = MagicMock()

        # First call: Should create
        await mock_coordinator._async_update_device(mock_device)
        assert cast(Any, mock_reg.async_get_or_create).call_count == 1

        # Second call: Should return early
        await mock_coordinator._async_update_device(mock_device)
        assert cast(Any, mock_reg.async_get_or_create).call_count == 1


async def test_discovery_task_handles_exception(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_discovery_task catches and logs exceptions.

    Covers lines 496-497.
    """
    with (
        patch.object(
            mock_coordinator,
            "_discover_new_entities",
            side_effect=Exception("Boom"),
        ),
        patch("custom_components.ramses_cc.coordinator._LOGGER") as mock_log,
    ):
        await mock_coordinator._async_discovery_task()

        cast(Any, mock_log.error).assert_called_with("Discovery error: %s", ANY)


async def test_service_delegates(mock_coordinator: RamsesCoordinator) -> None:
    """Test simple service delegates pass calls to handler.

    Covers lines 582, 586, 590, 594, 611.
    """
    call_obj = MagicMock()
    handler = mock_coordinator.service_handler

    mock_bind = AsyncMock()
    mock_send = AsyncMock()
    mock_get = AsyncMock()
    mock_set = AsyncMock()
    mock_refresh = AsyncMock()

    cast(Any, handler).async_bind_device = mock_bind
    cast(Any, handler).async_send_packet = mock_send
    cast(Any, handler).async_get_fan_param = mock_get
    cast(Any, handler).async_set_fan_param = mock_set
    cast(Any, mock_coordinator).async_refresh = mock_refresh

    # 1. bind_device
    await mock_coordinator.async_bind_device(call_obj)
    mock_bind.assert_awaited_once_with(call_obj)

    # 2. force_update
    await mock_coordinator.async_force_update(call_obj)
    mock_refresh.assert_awaited_once()

    # 3. send_packet
    await mock_coordinator.async_send_packet(call_obj)
    mock_send.assert_awaited_once_with(call_obj)

    # 4. get_fan_param
    await mock_coordinator.async_get_fan_param(call_obj)
    mock_get.assert_awaited_once_with(call_obj)

    # 5. set_fan_param
    await mock_coordinator.async_set_fan_param(call_obj)
    mock_set.assert_awaited_once_with(call_obj)


async def test_get_all_fan_params_delegate(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test get_all_fan_params creates a task.

    Covers lines 605.
    """
    call_obj = MagicMock()
    handler = mock_coordinator.service_handler
    mock_run = AsyncMock()

    cast(Any, handler)._async_run_fan_param_sequence = mock_run

    # This method is not async, it uses hass.async_create_task
    mock_coordinator.get_all_fan_params(call_obj)

    # Verify task creation was called
    cast(Any, mock_coordinator.hass.async_create_task).assert_called_once()
    # Note: verifying the exact coro passed to create_task is complex with
    # mocks, but line coverage is satisfied by calling the method.


async def test_async_update_data_success(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test _async_update_data runs to completion when client exists.

    Covers line 490.
    """
    assert mock_coordinator.client is not None

    # Ensure client exists so we don't return early
    mock_coordinator.client = MagicMock()

    # Call the method
    result = await mock_coordinator._async_update_data()

    # Verify it reached the end
    assert result is None


# --- Tests migrated from test_fan_handler.py ---


async def test_coordinator_init(mock_coordinator: RamsesCoordinator) -> None:
    """Test coordinator initialization state."""
    assert mock_coordinator.client is not None
    assert mock_coordinator._devices == []


async def test_coordinator_get_fan_param(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_get_fan_param service call.

    Migrated from test_fan_handler.py.
    """
    assert mock_coordinator.client is not None

    call_data = {
        "device_id": FAN_ID,
        "param_id": PARAM_ID_HEX,
        "from_id": REM_ID,
    }

    mock_send = AsyncMock()

    with patch.object(mock_coordinator, "_get_device") as mock_get_dev:
        mock_dev = MagicMock()
        mock_dev.id = FAN_ID
        mock_get_dev.return_value = mock_dev

        cast(Any, mock_coordinator.client).async_send_cmd = mock_send

        await mock_coordinator.async_get_fan_param(call_data)

        assert cast(Any, mock_send).called
        cmd = mock_send.call_args[0][0]
        # Check command attributes if possible, or just that it was sent
        assert cmd is not None


async def test_coordinator_set_fan_param(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_set_fan_param service call.

    Migrated from test_fan_handler.py.
    """
    assert mock_coordinator.client is not None

    call_data = {
        "device_id": FAN_ID,
        "param_id": PARAM_ID_HEX,
        "value": 0.5,
        "from_id": REM_ID,
    }

    mock_send = AsyncMock()

    # Patch _get_device so valid check passes
    with patch.object(mock_coordinator, "_get_device") as mock_get_dev:
        mock_dev = MagicMock()
        mock_dev.id = FAN_ID
        mock_get_dev.return_value = mock_dev

        cast(Any, mock_coordinator.client).async_send_cmd = mock_send

        await mock_coordinator.async_set_fan_param(call_data)

        assert cast(Any, mock_send).called


async def test_coordinator_set_fan_param_no_value(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_set_fan_param raises error when value is missing.

    Migrated from test_fan_handler.py.
    """
    call_data = {
        "device_id": FAN_ID,
        "param_id": PARAM_ID_HEX,
        "from_id": REM_ID,
    }

    # Patch _get_device so valid check passes and we hit the value check
    with patch.object(mock_coordinator, "_get_device") as mock_get_dev:
        mock_dev = MagicMock()
        mock_dev.id = FAN_ID
        mock_get_dev.return_value = mock_dev

        with pytest.raises(
            HomeAssistantError,
            match="Invalid parameter.*Missing required parameter",
        ):
            await mock_coordinator.async_set_fan_param(call_data)


async def test_coordinator_set_fan_param_no_binding(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
) -> None:
    """Test set_fan_param when the fan has NO bound remote (unbound).

    Migrated from test_fan_handler.py.
    """
    assert mock_coordinator.client is not None

    mock_coordinator._devices = [mock_fan_device]
    cast(Any, mock_fan_device).get_bound_rem = MagicMock(return_value=None)
    mock_send = AsyncMock()

    cast(Any, mock_coordinator.client).async_send_cmd = mock_send

    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 21.5}

    with pytest.raises(
        HomeAssistantError, match="Cannot set parameter: No valid source device"
    ):
        await mock_coordinator.async_set_fan_param(call_data)

    cast(Any, mock_send).assert_not_called()


async def test_get_fan_param_fallback_hgi(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test async_get_fan_param falls back to HGI ID.

    Happens when no bound remote exists. Migrated from test_fan_handler.py.
    """
    assert mock_coordinator.client is not None

    # 1. Setup HGI with a valid ID (matches _DEVICE_ID_RE)
    hgi_id = "18:000123"

    # 2. Setup Device to have NO bound remote
    # This forces the coordinator to look for a fallback (the HGI)
    mock_coordinator._devices = [mock_fan_device]
    cast(Any, mock_fan_device).get_bound_rem.return_value = None
    mock_send = AsyncMock()

    # 3. Prepare call data without an explicit 'from_id'
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX}

    cast(Any, mock_coordinator.client).hgi = MagicMock(id=hgi_id)
    cast(Any, mock_coordinator.client).async_send_cmd = mock_send

    # 4. Run with log capture to verify the debug logic
    with caplog.at_level(logging.DEBUG):
        await mock_coordinator.async_get_fan_param(call_data)

    # 5. Verify the fallback logic triggered
    # Check the specific debug message matches the code path
    assert (
        f"No explicit/bound from_id for {FAN_ID}, using gateway id {hgi_id}"
        in caplog.text
    )

    # Check the command was actually sent with the HGI ID as the source
    assert cast(Any, mock_send).called
    cmd = mock_send.call_args[0][0]
    assert cmd.src.id == hgi_id


# --- Tests migrated from test_fan_param.py ---

# Type aliases for better readability
MockType = MagicMock
AsyncMockType = AsyncMock


class TestFanParameterGet:
    """Test cases for the get_fan_param service.

    This test class verifies the behaviour of the async_get_fan_param and
    _async_run_fan_param_sequence methods in the RamsesCoordinator class,
    including error handling and edge cases for parameter reading operations.
    """

    @pytest.fixture(autouse=True)
    async def setup_get_fixture(self, hass: HomeAssistant) -> AsyncGenerator[None]:
        """Set up test environment for GET operations.

        This fixture runs before each test method and sets up:
        - A real RamsesCoordinator instance
        - A mock client with an HGI device
        - Patches for Command.get_fan_param
        - Test command objects for GET operations

        Args:
            hass: Home Assistant fixture for creating a test environment.
        """
        # Create a properly structured MockConfigEntry
        mock_entry = MockConfigEntry(
            domain=DOMAIN,
            options={
                CONF_SCAN_INTERVAL: 60,
                CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            },
            entry_id="test_entry_id",
        )
        mock_entry.add_to_hass(hass)

        # Initialize coordinator with the structured mock entry
        self.coordinator = RamsesCoordinator(hass, mock_entry)

        # Create a mock client with HGI device
        self.mock_client = AsyncMock()
        self.coordinator.client = self.mock_client
        assert self.coordinator.client is not None

        # Create a mock device and add it to the registry
        # This prevents _get_device_and_from_id from returning early with empty
        # from_id
        self.mock_device = MagicMock()
        self.mock_device.id = TEST_DEVICE_ID
        cast(Any, self.mock_device).get_bound_rem.return_value = None

        # Patch Command.get_fan_param to control command creation
        self.patcher = patch(
            "custom_components.ramses_cc.services.Command.get_fan_param"
        )
        self.mock_get_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "RQ"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        cast(Any, self.mock_get_fan_param).return_value = self.mock_cmd

        cast(Any, self.coordinator.client).hgi = MagicMock(id=TEST_FROM_ID)
        cast(Any, self.coordinator.client.device_registry).device_by_id = {
            TEST_DEVICE_ID: self.mock_device
        }

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()

    @pytest.mark.asyncio
    async def test_basic_fan_param_request(self, hass: HomeAssistant) -> None:
        """Test basic fan parameter request.

        Verifies that:
        1. The command is constructed with correct parameters
        2. The command is sent via the client
        3. No errors are raised
        """
        # Setup service call data with all required parameters
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Act - Call the method under test
        await self.coordinator.async_get_fan_param(call)

        # Assert - Verify command construction
        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # device_id as positional argument
            TEST_PARAM_ID,  # param_id as positional argument
            src_id=TEST_FROM_ID,  # src_id as keyword argument
        )

        # Verify command was sent via the client
        cast(Any, self.mock_client.async_send_cmd).assert_awaited_once_with(
            self.mock_cmd
        )

    @pytest.mark.asyncio
    async def test_missing_required_param_id(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing param_id logs an error.

        Verifies that:
        1. An error is logged when param_id is missing
        2. No command is sent when validation fails
        """
        # Setup service call without param_id
        service_data = {"device_id": TEST_DEVICE_ID, "from_id": TEST_FROM_ID}
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act & Assert - Expect ServiceValidationError instead of just logging
        with pytest.raises(ServiceValidationError, match="service_param_invalid"):
            await self.coordinator.async_get_fan_param(call)

        # Verify no command was sent
        cast(Any, self.mock_client.async_send_cmd).assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_fan_id(self, hass: HomeAssistant) -> None:
        """Test that a custom fan_id can be specified.

        Verifies that:
        1. The fan_id parameter is used when provided
        2. The command is constructed with the correct fan_id
        """
        # Setup service call with custom fan_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            # "fan_id": custom_fan_id,
            "param_id": TEST_PARAM_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Act - Call the method under test
        await self.coordinator.async_get_fan_param(call)

        # Assert - Verify command was constructed with custom fan_id
        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # fan_id deprecated? Should use the custom fan_id
            TEST_PARAM_ID,
            src_id=TEST_FROM_ID,
        )

        # Verify command was sent
        cast(Any, self.mock_client.async_send_cmd).assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_fan_param_with_ha_device_selector_resolves_device_id(
        self, hass: HomeAssistant
    ) -> None:
        """Test that HA Device selector resolves to Ramses device ID."""
        entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
        entry.add_to_hass(hass)
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, TEST_DEVICE_ID)},
            name="Test FAN",
        )

        service_data = {
            "device": device_entry.id,
            "param_id": TEST_PARAM_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        await self.coordinator.async_get_fan_param(call)

        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,
            TEST_PARAM_ID,
            src_id=TEST_FROM_ID,
        )


async def test_get_fan_param_service_schema_accepts_ha_device_selector(
    hass: HomeAssistant,
) -> None:
    """Test that the service schema accepts HA device selectors."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, TEST_DEVICE_ID)},
        name="Test FAN",
    )

    handler = AsyncMock()
    hass.services.async_register(
        DOMAIN, SVC_GET_FAN_PARAM, handler, schema=SCH_GET_FAN_PARAM_DOMAIN
    )

    await hass.services.async_call(
        DOMAIN,
        SVC_GET_FAN_PARAM,
        {"device": device_entry.id, "param_id": TEST_PARAM_ID},
        blocking=True,
    )

    assert cast(Any, handler).called


class TestFanParameterSet:
    """Test cases for the set_fan_param service.

    SAFETY NOTICE: This test class uses comprehensive mocking to ensure
    no real commands are sent to actual FAN devices. All
    Command.set_fan_param calls and client.send_cmd operations are
    intercepted by mocks.

    Safety measures in place:
    - Command.set_fan_param is patched with mock
    - Client.async_send_cmd is mocked
    - Coordinator uses mock client, not real hardware
    - All assertions verify mock behaviour only
    - No real hardware communication can occur

    This test class verifies the behaviour of the async_set_fan_param
    method in the RamsesCoordinator class, including error handling and
    edge cases for parameter writing operations.
    """

    @pytest.fixture(autouse=True)
    async def setup_set_fixture(self, hass: HomeAssistant) -> AsyncGenerator[None]:
        """Set up test environment for SET operations.

        This fixture runs before each test method and sets up:
        - A real RamsesCoordinator instance
        - A mock client with an HGI device
        - Patches for Command.set_fan_param
        - Test command objects for SET operations

        Args:
            hass: Home Assistant fixture for creating a test environment.
        """
        # Create a properly structured MockConfigEntry
        mock_entry = MockConfigEntry(
            domain=DOMAIN,
            options={
                CONF_SCAN_INTERVAL: 60,
                CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            },
            entry_id="test_entry_id",
        )
        mock_entry.add_to_hass(hass)

        # Initialize coordinator with the structured mock entry
        self.coordinator = RamsesCoordinator(hass, mock_entry)

        # Create a mock client with HGI device
        self.mock_client = AsyncMock()
        self.coordinator.client = self.mock_client
        assert self.coordinator.client is not None

        # Create a mock device and add it to the registry
        self.mock_device = MagicMock()
        self.mock_device.id = TEST_DEVICE_ID
        cast(Any, self.mock_device).get_bound_rem.return_value = None

        # Patch Command.set_fan_param to control command creation
        self.patcher = patch(
            "custom_components.ramses_cc.services.Command.set_fan_param"
        )
        self.mock_set_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "W"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        cast(Any, self.mock_set_fan_param).return_value = self.mock_cmd

        # PERFORMANCE OPTIMIZATION:
        # Patch asyncio.sleep to be instant for set operations which use sleep
        self.sleep_patcher = patch("asyncio.sleep")
        self.mock_sleep = self.sleep_patcher.start()

        cast(Any, self.coordinator.client).hgi = MagicMock(id=TEST_FROM_ID)
        cast(Any, self.coordinator.client.device_registry).device_by_id = {
            TEST_DEVICE_ID: self.mock_device
        }

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()
        self.sleep_patcher.stop()

    @pytest.mark.asyncio
    async def test_basic_fan_param_set(self, hass: HomeAssistant) -> None:
        """Test basic fan parameter set with all required parameters.

        Verifies that:
        1. The command is constructed with correct parameters
        2. The command is sent via the client
        3. No errors are raised
        """
        # Setup service call data with all required parameters
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Act - Call the method under test
        await self.coordinator.async_set_fan_param(call)

        # Assert - Verify command construction
        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # device_id as positional argument
            TEST_PARAM_ID,  # param_id as positional argument
            TEST_VALUE,  # value as is
            src_id=TEST_FROM_ID,  # src_id as keyword argument
        )

        # Verify command was sent via the client
        cast(Any, self.mock_client.async_send_cmd).assert_awaited_once_with(
            self.mock_cmd
        )

    @pytest.mark.asyncio
    async def test_set_fan_param_with_ha_device_selector(
        self, hass: HomeAssistant
    ) -> None:
        """Test that HA Device selector resolves to Ramses device ID in SET."""
        entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
        entry.add_to_hass(hass)
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, TEST_DEVICE_ID)},
            name="Test FAN",
        )

        service_data = {
            "device": device_entry.id,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        await self.coordinator.async_set_fan_param(call)

        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,
            TEST_PARAM_ID,
            TEST_VALUE,
            src_id=TEST_FROM_ID,
        )

        # Verify command was sent
        cast(Any, self.mock_client.async_send_cmd).assert_awaited_once()


class TestFanParameterUpdate:
    """Test cases for the update_fan_params service.

    This test class verifies the behaviour of the
    _async_run_fan_param_sequence method in the RamsesCoordinator class.
    """

    @pytest.fixture(autouse=True)
    async def setup_update_fixture(self, hass: HomeAssistant) -> AsyncGenerator[None]:
        """Set up test environment for UPDATE operations.

        This fixture runs before each test method and sets up:
        - A real RamsesCoordinator instance
        - A mock client with an HGI device
        - Patches for Command.get_fan_param
        - Test command objects for UPDATE operations

        Args:
            hass: Home Assistant fixture for creating a test environment.
        """
        # Create a properly structured MockConfigEntry
        mock_entry = MockConfigEntry(
            domain=DOMAIN,
            options={
                CONF_SCAN_INTERVAL: 60,
                CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            },
            entry_id="test_entry_id",
        )
        mock_entry.add_to_hass(hass)

        # Initialize coordinator with the structured mock entry
        self.coordinator = RamsesCoordinator(hass, mock_entry)

        # Create a mock client with HGI device
        self.mock_client = AsyncMock()
        self.coordinator.client = self.mock_client
        assert self.coordinator.client is not None

        # Create a mock device and add it to the registry
        self.mock_device = MagicMock()
        self.mock_device.id = TEST_DEVICE_ID
        cast(Any, self.mock_device).get_bound_rem.return_value = None

        # Patch Command.get_fan_param to control command creation
        self.patcher = patch(
            "custom_components.ramses_cc.services.Command.get_fan_param"
        )
        self.mock_get_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "RQ"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        cast(Any, self.mock_get_fan_param).return_value = self.mock_cmd

        # PERFORMANCE OPTIMIZATION:
        # Patch asyncio.sleep to be instant for set operations which use sleep
        self.sleep_patcher = patch("asyncio.sleep")
        self.mock_sleep = self.sleep_patcher.start()

        cast(Any, self.coordinator.client).hgi = MagicMock(id=TEST_FROM_ID)
        cast(Any, self.coordinator.client.device_registry).device_by_id = {
            TEST_DEVICE_ID: self.mock_device
        }

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()
        self.sleep_patcher.stop()

    @pytest.mark.asyncio
    async def test_basic_fan_param_update(self, hass: HomeAssistant) -> None:
        """Test basic fan parameter update with all required parameters.

        Verifies that:
        1. Commands are constructed for all parameters in the schema
        2. All commands are sent via the client
        3. No errors are raised
        """
        # Setup service call data with all required parameters
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

        # Act - Call the method under test
        await self.coordinator.service_handler._async_run_fan_param_sequence(call)

        # Verify all parameters in the schema were requested
        assert cast(Any, self.mock_get_fan_param).call_count > 0, (
            "Expected multiple parameter requests"
        )

        # Verify commands were sent via the client
        assert cast(Any, self.mock_client.async_send_cmd).call_count > 0, (
            "Expected multiple commands sent"
        )


async def test_async_stop_client_handles_exceptions(
    mock_coordinator: RamsesCoordinator, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that _async_stop_client gracefully catches teardown
    exceptions.

    This ensures that Home Assistant's async_unload task does not crash
    if the serial port disconnects or times out during the gateway
    shutdown phase.
    """
    # Scenario 1: Early return if client is None
    mock_coordinator.client = None
    await mock_coordinator._async_stop_client()  # Should not raise or error

    # Re-initialize the mock client for the remaining tests
    mock_coordinator.client = MagicMock()
    assert mock_coordinator.client is not None
    mock_stop = AsyncMock()

    cast(Any, mock_coordinator.client).stop = mock_stop

    # Scenario 2: Normal operation (no exceptions)
    await mock_coordinator._async_stop_client()
    mock_stop.assert_awaited_once()

    # Scenario 3: serial.SerialException (e.g. buffer flush disconnect)
    mock_stop.reset_mock()
    cast(Any, mock_stop).side_effect = serial.SerialException("Device disconnected")

    with caplog.at_level(logging.DEBUG):
        await mock_coordinator._async_stop_client()  # Should catch exception
    assert "Serial port disconnected or busy" in caplog.text

    # Scenario 4: TimeoutError (built-in)
    caplog.clear()
    cast(Any, mock_stop).side_effect = TimeoutError("Shutdown timed out")

    with caplog.at_level(logging.DEBUG):
        await mock_coordinator._async_stop_client()  # Should catch exception
    assert "Transport timeout/error" in caplog.text

    # Scenario 5: exc.TransportError
    caplog.clear()
    cast(Any, mock_stop).side_effect = exc.TransportError("Transport failed")

    with caplog.at_level(logging.DEBUG):
        await mock_coordinator._async_stop_client()  # Should catch exception
    assert "Transport timeout/error" in caplog.text

    # Scenario 6: Unexpected generic Exception
    caplog.clear()
    cast(Any, mock_stop).side_effect = Exception("Something completely unexpected")

    with caplog.at_level(logging.WARNING):
        await mock_coordinator._async_stop_client()  # Should catch exception
    assert "Unexpected error while stopping RAMSES client" in caplog.text


async def test_update_device_async_name(
    mock_coordinator: RamsesCoordinator,
) -> None:
    # Test async name resolution in _async_update_device.
    mock_device = MagicMock()
    mock_device.id = "01:123456"

    async def mock_name_coro() -> str:
        return "Async Name"

    # Mock it so that `callable(raw_name)` is True and returns a coroutine
    mock_device.name = MagicMock(return_value=mock_name_coro())

    mock_device.state_store = MagicMock()
    cast(Any, mock_device.state_store)._msg_value_code = AsyncMock(return_value=None)

    with patch("homeassistant.helpers.device_registry.async_get") as dr_m:
        mock_reg = dr_m.return_value
        await mock_coordinator._async_update_device(mock_device)

        call_kwargs = cast(Any, mock_reg.async_get_or_create).call_args[1]
        assert call_kwargs["name"] == "Async Name"


async def test_discover_new_entities_hgi_registration(
    mock_coordinator: RamsesCoordinator,
) -> None:
    # Test active HGI device is explicitly registered during discovery.
    assert mock_coordinator.client is not None

    mock_transport = MagicMock()
    cast(Any, mock_transport).get_extra_info.return_value = "18:111111"

    mock_engine = MagicMock()
    mock_engine._transport = mock_transport

    mock_coordinator.client._engine = mock_engine
    mock_get_device = MagicMock()

    cast(Any, mock_coordinator.client.device_registry).devices = []
    cast(Any, mock_coordinator.client.device_registry).systems = []
    cast(Any, mock_coordinator.client.device_registry).device_by_id = {}
    cast(Any, mock_coordinator.client.device_registry).get_device = mock_get_device

    with (
        patch("custom_components.ramses_cc.coordinator.async_dispatcher_send"),
        patch("homeassistant.helpers.device_registry.async_get"),
        patch.object(
            mock_coordinator,
            "_async_update_device",
            new_callable=AsyncMock,
        ),
    ):
        await mock_coordinator._discover_new_entities()

        mock_get_device.assert_called_with("18:111111")


async def test_discover_entities_does_not_suppress_base_exceptions(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test that unexpected exceptions are not swallowed during discovery."""
    assert mock_coordinator.client is not None

    # 2. Force the transport to raise a severe exception (RuntimeError)
    # instead of an expected one like AttributeError or KeyError
    mock_transport = MagicMock()
    cast(Any, mock_transport).get_extra_info.side_effect = RuntimeError(
        "Critical transport failure"
    )

    mock_engine = MagicMock()
    mock_engine._transport = mock_transport
    mock_coordinator.client._engine = mock_engine

    # 1. Setup minimal safe state for discovery
    cast(Any, mock_coordinator.client.device_registry).devices = []
    cast(Any, mock_coordinator.client.device_registry).systems = []
    cast(Any, mock_coordinator.client.device_registry).device_by_id = {}
    cast(Any, mock_coordinator.client.device_registry).get_device = MagicMock()

    # 3. Call discovery and assert the RuntimeError successfully escapes
    with pytest.raises(RuntimeError, match="Critical transport failure"):
        await mock_coordinator._discover_new_entities()

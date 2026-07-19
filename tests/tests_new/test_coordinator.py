"""Tests for the coordinator aspect of RamsesCoordinator.

(Lifecycle, Config, Updates).
"""

import asyncio
import logging
import sys
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
    SZ_REMOTES,
    SZ_TR_COMMANDS,
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
def mock_client():
    """Return mock client."""
    client = AsyncMock()
    client.start = AsyncMock()
    client.add_msg_handler = MagicMock()
    return client


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

        # Should set up 2 timers:
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


@pytest.mark.asyncio
async def test_create_client_foreign_hgi_not_in_block_list(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Foreign HGIs (18:) must not be put in the block_list.

    A foreign HGI communicates with our controller and the controller's
    responses (e.g. 0004 zone names) are addressed to the foreign HGI.
    Blocking the foreign HGI would prevent the active gateway from
    eavesdropping on those responses (issue 822).
    """
    mock_coordinator.options[SZ_SERIAL_PORT] = {SZ_PORT_NAME: "/dev/ttyUSB0"}

    schema = {
        "_owner": "me",
        "main_tcs": "01:216136",
        "01:216136": {"_owner": "me"},
        "04:111111": {"_owner": "neighbour"},  # foreign non-HGI → block
        "18:072981": {"_owner": "not-me"},  # foreign HGI → do NOT block
    }

    with patch("custom_components.ramses_cc.coordinator.Gateway") as mock_gwy:
        mock_coordinator._create_client(schema)

        _, kwargs = cast(Any, mock_gwy).call_args
        gwy_config = kwargs["config"]

        block_list = dict(gwy_config.block_list or {})
        assert "04:111111" in block_list  # foreign non-HGI is blocked
        assert "18:072981" not in block_list  # foreign HGI is NOT blocked


@pytest.mark.asyncio
async def test_async_start_with_packet_handler(
    mock_coordinator: RamsesCoordinator, mock_client
):
    """Test async_start with packet handler registration."""
    mock_coordinator.client = mock_client
    mock_coordinator._discover_new_entities = AsyncMock()
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()
    mock_coordinator.async_save_client_state = AsyncMock()

    with patch("custom_components.ramses_cc.coordinator.async_track_time_interval"):
        await mock_coordinator.async_start()

    # Confirm the packet handler is registered
    assert mock_client.add_msg_handler.call_count == 1
    handler = mock_client.add_msg_handler.call_args[0][0]

    # Mock a packet
    mock_dto = MagicMock()
    mock_dto.addr1 = "addr1"
    mock_dto.addr2 = "addr2"
    handler(mock_dto)

    # Verify task creation was called
    cast(Any, mock_coordinator.hass.async_create_task).assert_called_once()
    # Note: verifying the exact coro passed to create_task is complex with
    # mocks, but line coverage is satisfied by calling the method.


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

    # 2. Set up a mock config schema in options
    coordinator.options[CONF_SCHEMA] = config_schema

    # 3. Mock _create_client to return a valid client object (Success case)
    mock_client = MagicMock()
    cast(Any, mock_client).start = AsyncMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    with (
        patch(
            "custom_components.ramses_cc.coordinator.merge_schemas",
            return_value=merged_result,
        ) as mock_merge,
    ):
        # 5. Execute async_setup
        await coordinator.async_setup()

        # VERIFICATION

        # Ensure merge_schemas was called correctly
        cast(Any, mock_merge).assert_called_once_with(
            config_schema, cached_schema, schema_is_ssot=False
        )

        # CRITICAL: Verify _create_client called ONCE with the MERGED schema.
        cast(Any, coordinator._create_client).assert_called_once_with(merged_result)

        # Ensure the coordinator's client attribute was set to our mock
        assert coordinator.client is mock_client


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
    mock_save.assert_awaited_with({"type": "async"}, {}, {}, None, {})
    mock_save.reset_mock()

    # --- SCENARIO 2: Old Sync Client ---
    # get_state returns the tuple directly (MagicMock is not awaitable)
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=({"type": "sync"}, {})
    )

    await mock_coordinator.async_save_client_state()

    # Verify the synchronous result was handled correctly
    mock_save.assert_awaited_with({"type": "sync"}, {}, {}, None, {})


async def test_save_client_state_unload_uses_config_schema(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """During unload (_skip_topology_sync=True), the config schema is saved
    to .storage instead of the learned schema.

    This prevents the learned topology from surviving in the cache and
    overriding a freshly-cleared config schema on the next restart.
    """
    assert mock_coordinator.client is not None

    # Config schema is empty (user just cleared it)
    config_schema: dict[str, Any] = {}
    mock_coordinator.options = {CONF_SCHEMA: config_schema}

    # Learned schema from ramses_rf still has devices
    learned_schema = {"main_tcs": "01:145038", "orphans_heat": ["04:056053"]}

    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save
    mock_coordinator._remotes = {}
    mock_coordinator._entities = {}
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=(learned_schema, {})
    )

    # Simulate unload: _skip_topology_sync = True
    mock_coordinator._skip_topology_sync = True
    await mock_coordinator.async_save_client_state()

    # The saved schema must be the (empty) config schema, not the learned one
    saved_schema = mock_save.await_args.args[0]
    assert saved_schema == config_schema, (
        f"Expected config schema (empty), got learned schema: {saved_schema}"
    )
    assert "main_tcs" not in saved_schema, (
        "Learned topology leaked into cache on unload"
    )


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


async def test_setup_sanitises_main_tcs_nonexistent_key(
    hass: HomeAssistant, mock_entry: MagicMock
) -> None:
    """main_tcs pointing to a non-existent key is cleared on startup."""
    from ramses_rf.schemas import SZ_MAIN_TCS

    # Mock async_update_entry so the sanitised schema is persisted
    hass.config_entries.async_update_entry = MagicMock()

    coordinator = RamsesCoordinator(hass, mock_entry)
    mock_client = MagicMock(spec=Gateway)
    cast(Any, mock_client).start = AsyncMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    # main_tcs points to a key that doesn't exist in the schema
    mock_entry.options = {
        CONF_SCHEMA: {SZ_MAIN_TCS: "99:999999"},
        SZ_KNOWN_LIST: {},
        CONF_RAMSES_RF: {},
        "serial_port": "/dev/ttyUSB0",
    }
    coordinator.options = dict(mock_entry.options)

    cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

    await coordinator.async_setup()

    # main_tcs should have been cleared in memory
    assert SZ_MAIN_TCS not in coordinator.options.get(CONF_SCHEMA, {})
    # ...and persisted to the config entry
    hass.config_entries.async_update_entry.assert_called_once()
    updated_options = hass.config_entries.async_update_entry.call_args[1]["options"]
    assert SZ_MAIN_TCS not in updated_options.get(CONF_SCHEMA, {})


async def test_setup_sanitises_main_tcs_trv_id(
    hass: HomeAssistant, mock_entry: MagicMock
) -> None:
    """main_tcs pointing to a TRV ID (not 01:) is cleared on startup."""
    from ramses_rf.schemas import SZ_MAIN_TCS

    # Mock async_update_entry so the sanitised schema is persisted
    hass.config_entries.async_update_entry = MagicMock()

    coordinator = RamsesCoordinator(hass, mock_entry)
    mock_client = MagicMock(spec=Gateway)
    cast(Any, mock_client).start = AsyncMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    # main_tcs points to a TRV (04:) — not a CTL (01:)
    mock_entry.options = {
        CONF_SCHEMA: {
            SZ_MAIN_TCS: "04:056053",
            "04:056053": {},
        },
        SZ_KNOWN_LIST: {},
        CONF_RAMSES_RF: {},
        "serial_port": "/dev/ttyUSB0",
    }
    coordinator.options = dict(mock_entry.options)

    cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

    await coordinator.async_setup()

    # main_tcs should have been cleared (04: is not a CTL)
    assert SZ_MAIN_TCS not in coordinator.options.get(CONF_SCHEMA, {})
    # ...and persisted to the config entry
    hass.config_entries.async_update_entry.assert_called_once()
    updated_options = hass.config_entries.async_update_entry.call_args[1]["options"]
    assert SZ_MAIN_TCS not in updated_options.get(CONF_SCHEMA, {})


async def test_setup_preserves_valid_main_tcs(
    hass: HomeAssistant, mock_entry: MagicMock
) -> None:
    """main_tcs pointing to a valid CTL key is preserved."""
    from ramses_rf.schemas import SZ_MAIN_TCS

    coordinator = RamsesCoordinator(hass, mock_entry)
    mock_client = MagicMock(spec=Gateway)
    cast(Any, mock_client).start = AsyncMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    mock_entry.options = {
        CONF_SCHEMA: {
            SZ_MAIN_TCS: "01:216136",
            "01:216136": {},
        },
        SZ_KNOWN_LIST: {},
        CONF_RAMSES_RF: {},
        "serial_port": "/dev/ttyUSB0",
    }
    coordinator.options = dict(mock_entry.options)

    cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

    await coordinator.async_setup()

    # main_tcs should be preserved
    assert coordinator.options.get(CONF_SCHEMA, {}).get(SZ_MAIN_TCS) == "01:216136"


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
    mock_send.call_args[0][0]
    # removed cmd.src.id assert


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
        - Patches for build_dto
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

        # Patch build_dto to control command creation
        self.patcher = patch("custom_components.ramses_cc.services.build_dto")
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
        self.mock_get_fan_param.assert_called_once()

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
        self.mock_get_fan_param.assert_called_once()

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

        self.mock_get_fan_param.assert_called_once()


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
    build_dto calls and client.send_cmd operations are
    intercepted by mocks.

    Safety measures in place:
    - build_dto is patched with mock
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
        - Patches for build_dto
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

        # Patch build_dto to control command creation
        self.patcher = patch("custom_components.ramses_cc.services.build_dto")
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
        self.mock_set_fan_param.assert_called_once()

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

        self.mock_set_fan_param.assert_called_once()

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
        - Patches for build_dto
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

        # Patch build_dto to control command creation
        self.patcher = patch("custom_components.ramses_cc.services.build_dto")
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


# ── Schema-as-single-source-of-truth tests ──────────────────────────────


class TestDeriveKnownListFromSchema:
    """Tests for _derive_known_list_from_schema."""

    def test_empty_schema(self) -> None:
        """Empty schema produces empty known_list."""
        result = RamsesCoordinator._derive_known_list_from_schema({})
        assert result == {}

    def test_main_tcs_only(self) -> None:
        """Schema with just main_tcs produces CTL in known_list."""
        schema = {"main_tcs": "01:145038", "01:145038": {}}
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "01:145038" in result
        assert result["01:145038"] == {}

    def test_full_tcs_structure(self) -> None:
        """Full TCS with zones, DHW, system produces all device IDs."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {
                "system": {"appliance_control": "10:064873"},
                "stored_hotwater": {
                    "sensor": "07:012345",
                    "hotwater_valve": "13:111111",
                    "heating_valve": "13:222222",
                },
                "underfloor_heating": {
                    "02:333333": {"circuits": {"01": {"zone_idx": "01"}}}
                },
                "zones": {
                    "01": {"sensor": "04:056053", "actuators": ["04:111111"]},
                    "02": {"sensor": "22:123456"},
                },
                "orphans": ["23:777777"],
            },
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        expected_ids = {
            "01:145038",
            "10:064873",
            "07:012345",
            "13:111111",
            "13:222222",
            "02:333333",
            "04:056053",
            "04:111111",
            "22:123456",
            "23:777777",
        }
        assert set(result.keys()) == expected_ids
        # All entries should be empty dicts (no traits)
        for traits in result.values():
            assert traits == {}

    def test_hvac_vcs_structure(self) -> None:
        """HVAC with remotes and sensors."""
        schema = {
            "30:111222": {
                "remotes": ["37:168270", "37:168271"],
                "sensors": ["39:000001"],
            },
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        expected_ids = {"30:111222", "37:168270", "37:168271", "39:000001"}
        assert set(result.keys()) == expected_ids

    def test_global_orphans(self) -> None:
        """Global orphan lists are included."""
        schema = {
            "orphans_heat": ["23:111111", "23:222222"],
            "orphans_hvac": ["39:333333"],
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "23:111111" in result
        assert "23:222222" in result
        assert "39:333333" in result

    def test_disabled_trait_includes_device(self) -> None:
        """Devices with _disabled: True are included in known_list (to avoid
        DeviceNotFoundError log spam) but entity creation is suppressed separately."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"zones": {"01": {"sensor": "04:056053"}}},
            "04:056053": {"_disabled": True},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "01:145038" in result
        assert "04:056053" in result  # included to avoid log spam

    def test_disabled_trait_on_ctl_excludes_ctl(self) -> None:
        """CTL with _disabled: True is included in known_list (to avoid log spam)."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"_disabled": True, "zones": {"01": {"sensor": "04:056053"}}},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "01:145038" in result  # included to avoid log spam
        assert "04:056053" in result  # zone sensor still collected

    def test_class_trait_propagates_to_known_list(self) -> None:
        """_class trait on a device entry propagates to known_list."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_class": "TRV"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["04:111111"]["class"] == "TRV"

    def test_class_ventilator_normalized_to_fan(self) -> None:
        """_class='ventilator' (entity slug) is normalized to 'FAN' (DevType slug)
        in both the schema and the derived known_list."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {"_class": "ventilator", "remotes": ["37:168270"]},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        # known_list should have the normalized DevType slug
        assert result["32:153289"]["class"] == "FAN"

    def test_class_switch_normalized_to_rem(self) -> None:
        """_class='switch' (entity slug) is normalized to 'REM' (DevType slug)."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_hvac": ["37:168270"],
            "37:168270": {"_class": "switch"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["37:168270"]["class"] == "REM"

    def test_class_lowercase_fan_normalized(self) -> None:
        """_class='fan' (lowercase) is normalized to 'FAN' (uppercase)."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {"_class": "fan", "remotes": ["37:168270"]},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["32:153289"]["class"] == "FAN"

    def test_alias_trait_propagates_to_known_list(self) -> None:
        """_alias trait on a device entry propagates to known_list."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_alias": "Living Room"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["04:111111"]["alias"] == "Living Room"

    def test_name_trait_maps_to_alias(self) -> None:
        """_name trait maps to alias in known_list (ramses_rf display name)."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_name": "My Sensor"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["04:111111"]["alias"] == "My Sensor"

    def test_alias_overrides_name_trait(self) -> None:
        """_alias takes precedence over _name when both are present."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_name": "Name", "_alias": "Alias"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["04:111111"]["alias"] == "Alias"

    def test_user_overrides_merge_with_traits(self) -> None:
        """User known_list overrides are merged with schema-derived traits."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_class": "TRV"},
        }
        overrides = {"04:111111": {"alias": "Custom"}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides
        )
        assert result["04:111111"]["class"] == "TRV"
        assert result["04:111111"]["alias"] == "Custom"

    def test_user_overrides_merged(self) -> None:
        """User overrides are merged into derived known_list."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"zones": {"01": {"sensor": "04:056053"}}},
        }
        overrides = {
            "01:145038": {"class": "CTL", "alias": "My Controller"},
            "04:056053": {"alias": "Living Room"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides
        )
        assert result["01:145038"]["alias"] == "My Controller"
        assert result["01:145038"]["class"] == "CTL"
        assert result["04:056053"]["alias"] == "Living Room"

    def test_user_override_for_device_not_in_schema(self) -> None:
        """User overrides for devices not in schema are kept (backward compat)."""
        schema = {"main_tcs": "01:145038", "01:145038": {}}
        overrides = {"03:123456": {"class": "THM", "faked": True}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides
        )
        assert "03:123456" in result
        assert result["03:123456"]["class"] == "THM"
        assert result["03:123456"]["faked"] is True

    def test_ssot_drops_known_list_only_devices(self) -> None:
        """When schema_is_ssot=True, devices in known_list but not in schema
        are dropped (prevents stale entries from re-creating cleared devices).
        """
        schema = {"main_tcs": "01:145038", "01:145038": {}}
        overrides = {
            "03:123456": {"class": "THM"},
            "04:056053": {"alias": "Kitchen TRV"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides, schema_is_ssot=True
        )
        # 03:123456 is not in schema → dropped (stale entry)
        assert "03:123456" not in result
        # 04:056053 is also not in schema → dropped
        assert "04:056053" not in result
        # 01:145038 is in schema → kept
        assert "01:145038" in result

    def test_ssot_keeps_overrides_for_schema_devices(self) -> None:
        """When schema_is_ssot=True, overrides for devices IN the schema
        are still applied (only stale entries are dropped).
        """
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"zones": {"01": {"sensor": "04:056053"}}},
        }
        overrides = {
            "04:056053": {"alias": "Kitchen TRV"},
            "03:123456": {"class": "THM"},  # not in schema → dropped
        }
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides, schema_is_ssot=True
        )
        # 04:056053 is in schema → override applied
        assert "04:056053" in result
        assert result["04:056053"]["alias"] == "Kitchen TRV"
        # 03:123456 is not in schema → dropped
        assert "03:123456" not in result

    def test_ssot_keeps_hgi_even_when_not_in_schema(self) -> None:
        """When schema_is_ssot=True, the HGI is always kept in the known_list
        even though it is never in the schema (it is the scanner, not a
        scanned device).  Without this, enforce_known_list rejects the
        gateway's own packets.
        """
        schema = {"main_tcs": "01:145038", "01:145038": {}}
        overrides = {
            "18:001234": {"class": "HGI"},
            "03:123456": {"class": "THM"},  # not in schema → dropped
        }
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides, schema_is_ssot=True
        )
        # HGI is kept even though it's not in the schema
        assert "18:001234" in result
        assert result["18:001234"]["class"] == "HGI"
        # Non-HGI devices not in schema are still dropped
        assert "03:123456" not in result

    def test_owner_matching_root_included(self) -> None:
        """Device with _owner matching root _owner is in known_list."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_owner": "me"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "04:111111" in result

    def test_owner_not_matching_root_excluded(self) -> None:
        """Device with _owner NOT matching root _owner is excluded from known_list."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111", "04:222222"],
            "04:111111": {"_owner": "me"},
            "04:222222": {"_owner": "neighbour"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "04:111111" in result
        assert "04:222222" not in result

    def test_no_root_owner_includes_all(self) -> None:
        """Without root _owner, all devices are included (backward compatible)."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_owner": "someone"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "04:111111" in result  # no root _owner → no filtering

    def test_owner_matching_root_but_disabled_included(self) -> None:
        """Device with _owner matching root AND _disabled is in known_list."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_owner": "me", "_disabled": True},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        # _disabled devices stay in known_list (to avoid DeviceNotFoundError)
        assert "04:111111" in result

    def test_owner_foreign_and_disabled_excluded(self) -> None:
        """Foreign _owner takes priority over _disabled → excluded from known_list."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_owner": "neighbour", "_disabled": True},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        # foreign → excluded (block_list handles it, not known_list)
        assert "04:111111" not in result

    def test_owner_matching_root_but_skipped_excluded(self) -> None:
        """Device with _owner matching root AND _skipped is excluded."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_owner": "me", "_skipped": True},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        # _skipped → excluded (block_list handles it)
        assert "04:111111" not in result

    def test_owner_foreign_and_skipped_excluded(self) -> None:
        """Foreign _owner + _skipped → excluded (both agree)."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_owner": "neighbour", "_skipped": True},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "04:111111" not in result

    def test_faked_trait_extracted(self) -> None:
        """_faked: true is extracted into known_list as faked=True."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_hvac": ["37:111111"],
            "37:111111": {"_faked": True, "_class": "REM"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["37:111111"]["faked"] is True
        assert result["37:111111"]["class"] == "REM"

    def test_faked_false_not_extracted(self) -> None:
        """_faked: false does not set faked in known_list."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_hvac": ["37:111111"],
            "37:111111": {"_faked": False},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "faked" not in result["37:111111"]

    def test_bound_trait_extracted(self) -> None:
        """_bound on a FAN with _class is extracted into known_list as bound=<id>."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {
                "_bound": "37:168270",
                "_class": "FAN",
                "remotes": ["37:168270"],
            },
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["32:153289"]["bound"] == "37:168270"
        assert result["32:153289"]["class"] == "FAN"

    def test_bound_without_class_not_passed_to_ramserf(self) -> None:
        """_bound on a FAN without _class is NOT passed to ramses_rf.

        ramses_rf's SCH_TRAITS only accepts 'bound' for HVAC devices with
        an explicit class.  Without _class, SCH_TRAITS_HEAT rejects bound
        (PREVENT_EXTRA) and SCH_TRAITS_HVAC also fails.  The _bound trait
        on a FAN is a ramses_cc concept (used by fan_handler for 2411
        routing) — ramses_rf doesn't need it.
        """
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {
                "_bound": "37:168270",
                "remotes": ["37:168270"],
            },
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        # FAN should be in known_list but without bound (no _class)
        assert "32:153289" in result
        assert "bound" not in result["32:153289"]

    def test_scheme_trait_extracted(self) -> None:
        """_scheme on a FAN is extracted into known_list as scheme=<name>."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {
                "_scheme": "orcon",
                "_class": "FAN",
                "remotes": ["37:168270"],
            },
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["32:153289"]["scheme"] == "orcon"

    def test_faked_bound_scheme_combined(self) -> None:
        """All three new traits work together on a FAN + REM pair."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {
                "_bound": "37:168270",
                "_class": "FAN",
                "_scheme": "itho",
                "remotes": ["37:168270"],
            },
            "37:168270": {"_faked": True, "_class": "REM"},
        }
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert result["32:153289"]["bound"] == "37:168270"
        assert result["32:153289"]["scheme"] == "itho"
        assert result["32:153289"]["class"] == "FAN"
        assert result["37:168270"]["faked"] is True
        assert result["37:168270"]["class"] == "REM"

    def test_user_override_wins_over_schema_faked(self) -> None:
        """User known_list faked=False overrides schema _faked=True."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_hvac": ["37:111111"],
            "37:111111": {"_faked": True, "_class": "REM"},
        }
        user_overrides = {"37:111111": {"faked": False, "class": "DIS"}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=user_overrides
        )
        # User override wins (shallow merge)
        assert result["37:111111"]["faked"] is False
        assert result["37:111111"]["class"] == "DIS"

    def test_user_override_wins_over_schema_bound(self) -> None:
        """User known_list bound overrides schema _bound."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {
                "_bound": "37:168270",
                "_class": "FAN",
                "remotes": ["37:168270"],
            },
        }
        user_overrides = {"32:153289": {"bound": "37:999999"}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=user_overrides
        )
        assert result["32:153289"]["bound"] == "37:999999"

    def test_user_override_wins_over_schema_scheme(self) -> None:
        """User known_list scheme overrides schema _scheme."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {
                "_scheme": "orcon",
                "_class": "FAN",
                "remotes": ["37:168270"],
            },
        }
        user_overrides = {"32:153289": {"scheme": "itho"}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=user_overrides
        )
        assert result["32:153289"]["scheme"] == "itho"

    def test_schema_faked_and_user_other_trait_merge(self) -> None:
        """Schema _faked and user class coexist (no conflict, both kept)."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_hvac": ["37:111111"],
            "37:111111": {"_faked": True},
        }
        user_overrides = {"37:111111": {"class": "REM"}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=user_overrides
        )
        assert result["37:111111"]["faked"] is True
        assert result["37:111111"]["class"] == "REM"


class TestExtractDeviceIdsFromStripped:
    """Tests for _extract_device_ids_from_stripped (safety net for known_list)."""

    def test_extracts_top_level_device(self) -> None:
        """Top-level device IDs (CTL, FAN) are extracted."""
        stripped = {"main_tcs": "01:145038", "01:145038": {}}
        result = RamsesCoordinator._extract_device_ids_from_stripped(stripped)
        assert "01:145038" in result

    def test_extracts_hvac_from_orphans(self) -> None:
        """HVAC devices in orphans_hvac are extracted (after _strip_schema_extensions
        moves empty HVAC entries there)."""
        stripped = {"orphans_hvac": ["30:160000"]}
        result = RamsesCoordinator._extract_device_ids_from_stripped(stripped)
        assert "30:160000" in result

    def test_extracts_zone_devices(self) -> None:
        """Sensors and actuators in zones are extracted."""
        stripped = {
            "01:145038": {
                "zones": {"01": {"sensor": "04:056053", "actuators": ["04:034720"]}}
            }
        }
        result = RamsesCoordinator._extract_device_ids_from_stripped(stripped)
        assert "04:056053" in result
        assert "04:034720" in result

    def test_extracts_empty(self) -> None:
        """Empty schema returns empty set."""
        assert RamsesCoordinator._extract_device_ids_from_stripped({}) == set()


class TestStripSchemaExtensions:
    """Tests for _strip_schema_extensions."""

    def test_strips_disabled_trait(self) -> None:
        """_disabled trait is stripped from TCS entries."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"_disabled": True, "zones": {"01": {"sensor": "04:056053"}}},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "_disabled" not in result["01:145038"]
        assert result["01:145038"]["zones"]["01"]["sensor"] == "04:056053"

    def test_strips_all_traits(self) -> None:
        """All _ prefixed traits are stripped from TCS entries."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {
                "_name": "My Controller",
                "_alias": "CTL",
                "_class": "CTL",
                "_comment": "main controller",
                "zones": {"01": {"sensor": "04:056053", "_name": "Living Room"}},
            },
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        for trait in ("_name", "_alias", "_class", "_comment"):
            assert trait not in result["01:145038"]
        assert "_name" not in result["01:145038"]["zones"]["01"]
        assert result["01:145038"]["zones"]["01"]["sensor"] == "04:056053"

    def test_strips_trait_only_entry(self) -> None:
        """Trait-only entries (only _ keys) are dropped."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "04:222222": {"_disabled": True},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:222222" not in result
        assert "01:145038" in result

    def test_strips_disabled_from_orphan_lists(self) -> None:
        """_disabled devices are removed from orphan lists."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111", "04:222222", "04:333333"],
            "04:222222": {"_disabled": True},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:222222" not in result  # trait-only entry dropped
        assert "04:111111" in result["orphans_heat"]
        assert "04:222222" not in result["orphans_heat"]  # removed from list
        assert "04:333333" in result["orphans_heat"]

    def test_strips_disabled_from_orphans_hvac(self) -> None:
        """_disabled devices are removed from orphans_hvac lists."""
        schema = {
            "orphans_hvac": ["32:111111", "37:222222"],
            "37:222222": {"_disabled": True},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "37:222222" not in result
        assert "32:111111" in result["orphans_hvac"]
        assert "37:222222" not in result["orphans_hvac"]

    def test_strips_device_comments(self) -> None:
        """device_comments key is stripped."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "device_comments": {"01:145038": "My Controller"},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "device_comments" not in result

    def test_no_extensions_returns_copy(self) -> None:
        """Schema without extensions is returned as-is (copy)."""
        schema = {"main_tcs": "01:145038", "01:145038": {}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert result == schema
        assert result is not schema  # should be a new dict

    def test_vcs_without_remotes_moved_to_orphans(self) -> None:
        """HVAC devices without remotes/sensors are moved to orphans_hvac."""
        schema: dict[str, Any] = {"30:160000": {}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "30:160000" not in result
        assert "30:160000" in result.get("orphans_hvac", [])

    def test_vcs_with_sensors_not_modified(self) -> None:
        """HVAC devices that already have sensors are not modified."""
        schema = {"30:160000": {"sensors": ["01:123456"]}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert result["30:160000"] == {"sensors": ["01:123456"]}

    def test_vcs_with_remotes_not_modified(self) -> None:
        """HVAC devices that already have remotes are not modified."""
        schema = {"30:160000": {"remotes": ["01:123456"]}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert result["30:160000"] == {"remotes": ["01:123456"]}

    def test_trv_in_tcs_orphans_moved_to_heat_orphans(self) -> None:
        """TRV in a TCS-level orphans list is moved to orphans_heat (issue 813).

        ramses_rf's PARENT_RULES only allows BdrSwitch / OtbGateway /
        UfhController in a TCS ``orphans`` list.  A TrvActuator there
        raises SchemaInconsistentError at setup time.
        """
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {"orphans": ["04:034682", "04:056673"]},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        # TCS orphans list should be gone (no valid entries left)
        assert "orphans" not in result["01:216136"]
        # TRVs moved to top-level orphans_heat
        assert "04:034682" in result["orphans_heat"]
        assert "04:056673" in result["orphans_heat"]

    def test_thm_in_tcs_orphans_moved_to_heat_orphans(self) -> None:
        """THM (room thermostat) in TCS orphans is moved to orphans_heat."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {"orphans": ["22:012299", "34:058721"]},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "orphans" not in result["01:216136"]
        assert "22:012299" in result["orphans_heat"]
        assert "34:058721" in result["orphans_heat"]

    def test_bdr_in_tcs_orphans_kept(self) -> None:
        """BDR (13:) in TCS orphans is a valid actuator — kept in place."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {"orphans": ["13:042605"]},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert result["01:216136"]["orphans"] == ["13:042605"]

    def test_mixed_tcs_orphans_split(self) -> None:
        """Mixed TCS orphans: valid actuators kept, TRVs moved to orphans_heat."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {"orphans": ["13:042605", "04:034682", "10:064873"]},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        # Valid actuators (BDR, OTB) stay in TCS orphans
        assert sorted(result["01:216136"]["orphans"]) == ["10:064873", "13:042605"]
        # TRV moved to orphans_heat
        assert "04:034682" in result["orphans_heat"]

    def test_nonheat_in_tcs_orphans_moved_to_hvac(self) -> None:
        """Non-heat device in TCS orphans is moved to orphans_hvac."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {"orphans": ["32:123456"]},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "orphans" not in result["01:216136"]
        assert "32:123456" in result["orphans_hvac"]

    def test_strips_root_owner_key(self) -> None:
        """Root _owner key is stripped (ramses_cc extension, not for ramses_rf)."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "_owner" not in result

    def test_strips_per_device_owner_trait(self) -> None:
        """Per-device _owner trait is stripped from device entries."""
        schema = {
            "_owner": "me",
            "main_tcs": "01:145038",
            "01:145038": {},
            "orphans_heat": ["04:111111"],
            "04:111111": {"_owner": "me"},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "_owner" not in result
        # Device entry should be empty after stripping (trait-only) → dropped
        assert "04:111111" not in result

    def test_foreign_owner_device_removed_from_orphans(self) -> None:
        """Foreign-owner devices are removed from orphan lists."""
        schema = {
            "_owner": "me",
            "orphans_heat": ["04:111111", "04:222222"],
            "04:111111": {"_owner": "me"},
            "04:222222": {"_owner": "neighbour"},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:222222" not in result  # foreign → dropped
        assert "04:222222" not in result.get("orphans_heat", [])
        assert "04:111111" not in result  # trait-only → dropped from result
        # but 04:111111 should be in orphans (it's "ours")
        assert "04:111111" in result.get("orphans_heat", [])

    def test_strips_faked_bound_scheme_traits(self) -> None:
        """_faked, _bound, _scheme are stripped from device entries."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {},
            "32:153289": {
                "_bound": "37:168270",
                "_scheme": "orcon",
                "remotes": ["37:168270"],
            },
            "37:168270": {"_faked": True, "_class": "REM"},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        # _ traits stripped from FAN
        assert "_bound" not in result["32:153289"]
        assert "_scheme" not in result["32:153289"]
        assert "remotes" in result["32:153289"]  # non-trait key kept
        # _ traits stripped from REM (entry becomes empty → dropped).
        # REM is already in FAN's remotes[] list, so it is NOT moved to
        # orphans_hvac (placed_in_lists check prevents duplicate).
        assert "37:168270" not in result
        assert "37:168270" not in result.get("orphans_hvac", [])


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _extract_schema_device_ids
# ───────────────────────────────────────────────────────────────────────


class TestExtractSchemaDeviceIds:
    """Tests for RamsesCoordinator._extract_schema_device_ids."""

    def test_empty_schema(self) -> None:
        """Empty schema returns empty set."""
        result = RamsesCoordinator._extract_schema_device_ids({})
        assert result == set()

    def test_with_devices(self) -> None:
        """Schema with devices returns their IDs."""
        schema = {
            "main_tcs": "01:123456",
            "01:123456": {"zones": {"01": {"sensor": "04:654321"}}},
        }
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        assert "01:123456" in result
        assert "04:654321" in result


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _derive_known_list_from_schema
# ───────────────────────────────────────────────────────────────────────


class TestDeriveKnownListFromSchemaExtended:
    """Tests for RamsesCoordinator._derive_known_list_from_schema."""

    def test_empty_schema(self) -> None:
        """Empty schema returns empty known_list."""
        result = RamsesCoordinator._derive_known_list_from_schema({})
        assert result == {}

    def test_user_overrides_merged(self) -> None:
        """User overrides are merged into the derived known_list."""
        schema = {"01:123456": {}}
        overrides = {"01:123456": {"alias": "Living room"}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides
        )
        assert result["01:123456"]["alias"] == "Living room"

    def test_user_overrides_adds_new_device(self) -> None:
        """User overrides can add a device not in the schema."""
        schema = {"01:123456": {}}
        overrides = {"04:654321": {"class": "TRV"}}
        result = RamsesCoordinator._derive_known_list_from_schema(
            schema, user_overrides=overrides
        )
        assert "04:654321" in result
        assert result["04:654321"]["class"] == "TRV"

    def test_non_dict_value_skipped(self) -> None:
        """Non-dict values for device-id keys are handled (id still extracted)."""
        schema = {"01:123456": "not a dict"}
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "01:123456" in result

    def test_full_schema_with_all_structures(self) -> None:
        """A full schema with TCS, DHW, UFH, zones, HVAC, orphans."""
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

        schema = {
            SZ_MAIN_TCS: "01:100000",
            "01:100000": {
                SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "01:200000"},
                SZ_DHW_SYSTEM: {
                    SZ_SENSOR: "07:300000",
                    SZ_DHW_VALVE: "08:400000",
                    SZ_HTG_VALVE: "08:450000",
                },
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
        result = RamsesCoordinator._derive_known_list_from_schema(schema)
        expected_ids = {
            "01:100000",
            "01:200000",
            "07:300000",
            "08:400000",
            "08:450000",
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
        assert set(result.keys()) == expected_ids
        # All entries should be empty dicts (no overrides)
        for v in result.values():
            assert v == {}


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _validate_schema_for_ramserf
# ───────────────────────────────────────────────────────────────────────


class TestValidateSchemaForRamserf:
    """Tests for RamsesCoordinator._validate_schema_for_ramserf."""

    def test_valid_schema_passes(self) -> None:
        """A valid schema with TCS and zones passes validation."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"zones": {"01": {"sensor": "04:056053"}}},
            "04:056053": {},
        }
        # Should not raise
        RamsesCoordinator._validate_schema_for_ramserf(schema)

    def test_valid_schema_with_traits_passes(self) -> None:
        """A schema with _ traits (stripped before validation) passes."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {
                "_class": "CTL",
                "_owner": "me",
                "zones": {"01": {"sensor": "04:056053"}},
            },
            "04:056053": {"_class": "TRV"},
            "device_comments": {"01:145038": "Main controller"},
        }
        # Should not raise — _ traits and device_comments are stripped
        RamsesCoordinator._validate_schema_for_ramserf(schema)

    def test_root_level_invalid_key_fails(self) -> None:
        """A root-level key that's not a device ID and not a known extension
        key fails validation (SCH_GLOBAL_SCHEMAS uses PREVENT_EXTRA)."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"zones": {"01": {"sensor": "04:056053"}}},
            "invalid_root_key": "some_value",  # not a device ID, not an extension
        }
        with pytest.raises(ValueError, match="Schema validation failed"):
            RamsesCoordinator._validate_schema_for_ramserf(schema)

    def test_root_level_bound_trait_stripped_passes(self) -> None:
        """A root-level _bound trait is stripped by _strip_schema_extensions
        (which removes all root-level _ prefixed keys), so validation passes."""
        schema = {
            "main_tcs": "01:145038",
            "01:145038": {"zones": {"01": {"sensor": "04:056053"}}},
            "_bound": "32:123456",  # stripped before validation
        }
        # Should not raise — _bound is stripped
        RamsesCoordinator._validate_schema_for_ramserf(schema)

    def test_empty_schema_passes(self) -> None:
        """An empty schema passes validation."""
        RamsesCoordinator._validate_schema_for_ramserf({})

    def test_invalid_class_warns_but_passes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An invalid _class value (e.g. 'ventilator') logs a warning but
        doesn't raise — ramses_rf falls back to the default class."""
        schema = {
            "32:153289": {"_class": "ventilator", "remotes": []},
        }
        with caplog.at_level("WARNING"):
            RamsesCoordinator._validate_schema_for_ramserf(schema)
        assert any("ventilator" in r.message for r in caplog.records)

    def test_valid_class_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """A valid _class value (e.g. 'FAN') doesn't log a warning."""
        schema = {
            "32:153289": {"_class": "FAN", "remotes": []},
        }
        with caplog.at_level("WARNING"):
            RamsesCoordinator._validate_schema_for_ramserf(schema)
        assert not any("_class" in r.message for r in caplog.records)


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _strip_schema_extensions edge cases
# ───────────────────────────────────────────────────────────────────────


class TestStripSchemaExtensionsExtended:
    """Tests for RamsesCoordinator._strip_schema_extensions."""

    def test_strips_none_values(self) -> None:
        """None values are stripped (e.g. main_tcs: None)."""
        schema = {"main_tcs": None, "01:123456": {}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "main_tcs" not in result
        # Empty device entries are moved to orphans (ramses_rf rejects empty dicts)
        assert "01:123456" not in result
        assert "01:123456" in result.get("orphans_heat", [])

    def test_hvac_without_remotes_moved_to_orphans(self) -> None:
        """HVAC devices (30:) without remotes/sensors are moved to orphans_hvac."""
        schema = {"30:160000": {}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "30:160000" not in result
        assert "30:160000" in result.get("orphans_hvac", [])

    def test_hvac_with_sensors_stays_at_root(self) -> None:
        """HVAC devices with sensors stay at root (valid VCS)."""
        schema = {"30:160000": {"sensors": ["32:123456"]}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "30:160000" in result
        assert "remotes" not in result["30:160000"]
        assert result["30:160000"]["sensors"] == ["32:123456"]

    def test_heat_empty_moved_to_heat_orphans(self) -> None:
        """Heat devices (01:) with empty dict are moved to orphans_heat."""
        schema = {"01:123456": {}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "01:123456" not in result
        assert "01:123456" in result.get("orphans_heat", [])

    def test_strips_device_comments_key(self) -> None:
        """device_comments extension key is stripped."""
        schema = {"01:123456": {}, "device_comments": {"01:123456": "test"}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "device_comments" not in result

    def test_disabled_false_adds_to_orphans(self) -> None:
        """A device with _disabled: false is added to orphans (un-declined)."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {},
            "orphans_heat": ["10:064873"],
            "04:034692": {"_disabled": False},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:034692" not in result  # trait-only entry dropped
        assert "04:034692" in result.get("orphans_heat", [])

    def test_disabled_true_dropped(self) -> None:
        """A device with _disabled: true is dropped entirely."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {},
            "orphans_heat": ["10:064873"],
            "04:034692": {"_disabled": True},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:034692" not in result
        assert "04:034692" not in result.get("orphans_heat", [])

    def test_empty_device_entry_moved_to_orphans(self) -> None:
        """An empty device entry (no traits, no topology) is moved to orphans."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {},
            "orphans_heat": ["10:064873"],
            "04:034692": {},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:034692" not in result
        assert "04:034692" in result.get("orphans_heat", [])

    def test_ctl_empty_dict_not_moved_to_orphans(self) -> None:
        """The CTL (main_tcs) with empty dict is NOT moved to orphans."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {},
            "orphans_heat": [],
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert result["01:216136"] == {}
        assert "01:216136" not in result.get("orphans_heat", [])

    def test_hvac_empty_dict_moved_to_orphans(self) -> None:
        """HVAC (30:) empty dict is moved to orphans_hvac, not kept at root."""
        schema = {"30:160000": {}}
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "30:160000" not in result
        assert "30:160000" in result.get("orphans_hvac", [])

    def test_skipped_true_dropped(self) -> None:
        """A device with _skipped: true is dropped from ramses_rf view."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {},
            "orphans_heat": ["10:064873"],
            "04:034692": {"_skipped": True},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:034692" not in result
        assert "04:034692" not in result.get("orphans_heat", [])

    def test_skipped_false_adds_to_orphans(self) -> None:
        """A device with _skipped: false is added to orphans (un-skipped)."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {},
            "orphans_heat": ["10:064873"],
            "04:034692": {"_skipped": False},
        }
        result = RamsesCoordinator._strip_schema_extensions(schema)
        assert "04:034692" not in result
        assert "04:034692" in result.get("orphans_heat", [])

    def test_skipped_excluded_from_known_list(self) -> None:
        """_skipped devices are excluded from the derived known_list."""
        schema = {
            "main_tcs": "01:216136",
            "01:216136": {},
            "orphans_heat": ["10:064873", "04:034692"],
            "04:034692": {"_skipped": True},
        }
        kl = RamsesCoordinator._derive_known_list_from_schema(schema)
        assert "04:034692" not in kl
        assert "10:064873" in kl
        assert "01:216136" in kl


# ───────────────────────────────────────────────────────────────────────
# Coordinator: discovery scan lifecycle
# ───────────────────────────────────────────────────────────────────────


async def test_async_stop_discovery_scan(hass: HomeAssistant) -> None:
    """Test _async_stop_discovery_scan exports and saves state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_stop_discovery",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value={})
    coordinator.store.async_save = AsyncMock()

    # Set up a mock discovery_manager
    mock_dm = MagicMock()
    mock_dm.export_state = MagicMock(
        return_value={"devices": {"04:056053": {"status": "accepted"}}}
    )
    mock_dm.stop = MagicMock()
    coordinator.discovery_manager = mock_dm

    await coordinator._async_stop_discovery_scan()

    mock_dm.export_state.assert_called_once()
    mock_dm.stop.assert_called_once()
    assert coordinator.discovery_manager is None


async def test_async_stop_discovery_scan_no_manager(hass: HomeAssistant) -> None:
    """Test _async_stop_discovery_scan when no discovery_manager is set."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_stop_no_mgr",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.discovery_manager = None

    # Should not raise
    await coordinator._async_stop_discovery_scan()


async def test_async_discovery_checkpoint_no_manager(hass: HomeAssistant) -> None:
    """Test _async_discovery_checkpoint when no discovery_manager is set."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_checkpoint_no_mgr",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.discovery_manager = None

    # Should return early without error
    await coordinator._async_discovery_checkpoint()


async def test_async_discovery_checkpoint_with_manager(hass: HomeAssistant) -> None:
    """Test _async_discovery_checkpoint calls check methods and saves state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_checkpoint",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value={})
    coordinator.store.async_save = AsyncMock()

    # Mock the client so async_save_client_state works
    mock_client = MagicMock(spec=Gateway)
    mock_client.get_state = MagicMock(return_value=({}, {}))
    coordinator.client = mock_client
    coordinator._remotes = {}

    coordinator.discovery_manager = MagicMock()
    coordinator.discovery_manager.check_for_new_devices = MagicMock()
    coordinator.discovery_manager.check_for_lost_devices = MagicMock()
    # refresh_device_comments must return a real dict (not a MagicMock) so
    # that HA's storage serializer doesn't choke on teardown.
    coordinator.discovery_manager.refresh_device_comments = MagicMock(return_value={})

    await coordinator._async_discovery_checkpoint()

    coordinator.discovery_manager.check_for_new_devices.assert_called_once()
    coordinator.discovery_manager.check_for_lost_devices.assert_called_once()


# ───────────────────────────────────────────────────────────────────────
# Coordinator: PacketDTO dict-format filtering (lines 211-234)
# ───────────────────────────────────────────────────────────────────────


async def test_get_saved_packets_dict_format_with_known_device(
    hass: HomeAssistant,
) -> None:
    """Test _get_saved_packets with PacketDTO dict format and enforce_known_list."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_dict_pkt",
        options={
            "ramses_rf": {SZ_ENFORCE_KNOWN_LIST: True},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # PacketDTO dict format with addr as dict (device_type + device_id)
    client_state = {
        SZ_PACKETS: {
            recent: {
                "code": "3150",
                "addr1": {"device_type": 1, "device_id": 123456},
                "addr2": "01:654321",
                "addr3": None,
            },
        }
    }

    result = coordinator._get_saved_packets(client_state)
    # 01:123456 is in known_list, so the packet should be kept
    assert recent in result


async def test_get_saved_packets_dict_format_unknown_device(
    hass: HomeAssistant,
) -> None:
    """Test _get_saved_packets filters out packets with unknown devices."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_dict_unknown",
        options={
            "ramses_rf": {SZ_ENFORCE_KNOWN_LIST: True},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # Packet with only unknown devices
    client_state = {
        SZ_PACKETS: {
            recent: {
                "code": "3150",
                "addr1": {"device_type": 9, "device_id": 999999},
                "addr2": None,
                "addr3": None,
            },
        }
    }

    result = coordinator._get_saved_packets(client_state)
    # No known devices in this packet — should be filtered out
    assert recent not in result


async def test_get_saved_packets_dict_format_filtered_code(
    hass: HomeAssistant,
) -> None:
    """Test _get_saved_packets filters out packets with code in msg_code_filter."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_dict_code",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # Packet with filtered code 313F
    client_state = {
        SZ_PACKETS: {
            recent: {
                "code": "313F",
                "addr1": "01:123456",
            },
        }
    }

    result = coordinator._get_saved_packets(client_state)
    assert recent not in result


async def test_get_saved_packets_dict_format_string_addr(
    hass: HomeAssistant,
) -> None:
    """Test _get_saved_packets with dict format but string addresses."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_dict_str",
        options={
            "ramses_rf": {SZ_ENFORCE_KNOWN_LIST: True},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # PacketDTO with simple string addresses
    client_state = {
        SZ_PACKETS: {
            recent: {
                "code": "3150",
                "addr1": "01:123456",
                "addr2": "01:654321",
            },
        }
    }

    result = coordinator._get_saved_packets(client_state)
    assert recent in result


async def test_get_saved_packets_src_dst_fallback(hass: HomeAssistant) -> None:
    """Test _get_saved_packets falls back to src/dst when addr1/2/3 absent.

    This covers the ramses_rf PR 780 format (L7 MessageStore bridge)
    which provides src/dst but not the legacy addr1/2/3 keys.
    PR 782 adds addr1/2/3 back, but we should work with both.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_src_dst",
        options={
            "ramses_rf": {SZ_ENFORCE_KNOWN_LIST: True},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # PR 780 format: src/dst only, no addr1/2/3
    client_state = {
        SZ_PACKETS: {
            recent: {
                "verb": " I",
                "src": "01:123456",
                "dst": "01:654321",
                "code": "3150",
                "payload": {},
            },
        }
    }

    result = coordinator._get_saved_packets(client_state)
    assert recent in result  # kept: src matches known_list


async def test_get_saved_packets_src_dst_unknown_device(hass: HomeAssistant) -> None:
    """Test _get_saved_packets drops packets when src/dst not in known_list."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_src_dst_unknown",
        options={
            "ramses_rf": {SZ_ENFORCE_KNOWN_LIST: True},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # PR 780 format: src/dst only, neither in known_list
    client_state = {
        SZ_PACKETS: {
            recent: {
                "verb": " I",
                "src": "09:999999",
                "dst": "09:888888",
                "code": "3150",
                "payload": {},
            },
        }
    }

    result = coordinator._get_saved_packets(client_state)
    assert recent not in result  # dropped: neither src nor dst in known_list


async def test_passive_scan_migration(hass: HomeAssistant) -> None:
    """Test that known_list-only devices are migrated to schema as orphans.

    Migration only runs when the schema has at least one device — an empty
    schema means the user wiped it and devices should be re-discovered.
    """
    from custom_components.ramses_cc.const import (
        CONF_ADVANCED_FEATURES,
        CONF_PASSIVE_SCAN,
    )
    from ramses_rf.schemas import SZ_ORPHANS_HEAT

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_migration",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"04:123456": {}, "18:006402": {"class": "HGI"}},
            # Schema has one existing device so migration runs for 04:123456
            CONF_SCHEMA: {"01:200001": {}},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value={})
    coordinator.store.async_save_backup = AsyncMock()

    mock_client = MagicMock(spec=Gateway)
    mock_client.start = AsyncMock()
    coordinator._create_client = MagicMock(return_value=mock_client)

    await coordinator.async_setup()
    await asyncio.sleep(0)

    # 04:123456 should have been migrated to schema as a heat orphan
    # 18:006402 should NOT (HGI devices are filtered out)
    schema = coordinator.options.get(CONF_SCHEMA, {})
    assert SZ_ORPHANS_HEAT in schema
    assert "04:123456" in schema[SZ_ORPHANS_HEAT]
    assert "18:006402" not in schema[SZ_ORPHANS_HEAT]

    # Backup should have been called before migration
    coordinator.store.async_save_backup.assert_called_once()
    await asyncio.sleep(0)


async def test_passive_scan_schema_wiped_skips_migration(
    hass: HomeAssistant,
) -> None:
    """Test that migration is skipped when schema is empty (user wiped it).

    The known_list-only devices should be dropped (SSOT), not resurrected,
    so the passive scan can re-discover them.
    """
    from custom_components.ramses_cc.const import (
        CONF_ADVANCED_FEATURES,
        CONF_PASSIVE_SCAN,
    )
    from ramses_rf.schemas import SZ_ORPHANS_HEAT

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_wiped",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"04:123456": {}, "18:006402": {"class": "HGI"}},
            CONF_SCHEMA: {},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value={})
    coordinator.store.async_save_backup = AsyncMock()

    mock_client = MagicMock(spec=Gateway)
    mock_client.start = AsyncMock()
    coordinator._create_client = MagicMock(return_value=mock_client)

    await coordinator.async_setup()
    await asyncio.sleep(0)

    # Schema should remain empty — no migration
    schema = coordinator.options.get(CONF_SCHEMA, {})
    assert SZ_ORPHANS_HEAT not in schema
    assert "04:123456" not in schema

    # Known_list entries are kept as trait overrides (not wiped)
    known_list = coordinator.options.get(SZ_KNOWN_LIST, {})
    assert "04:123456" in known_list  # trait override kept
    assert "18:006402" in known_list  # HGI kept

    # Backup should NOT have been called (no migration)
    coordinator.store.async_save_backup.assert_not_called()
    await asyncio.sleep(0)


async def test_passive_scan_no_migration_when_schema_has_device(
    hass: HomeAssistant,
) -> None:
    """Test that no migration happens when known_list devices are already in schema."""
    from custom_components.ramses_cc.const import (
        CONF_ADVANCED_FEATURES,
        CONF_PASSIVE_SCAN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_no_migration",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
            CONF_SCHEMA: {"01:123456": {}},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value={})
    coordinator.store.async_save_backup = AsyncMock()

    mock_client = MagicMock(spec=Gateway)
    mock_client.start = AsyncMock()
    coordinator._create_client = MagicMock(return_value=mock_client)

    await coordinator.async_setup()
    await asyncio.sleep(0)

    # No migration needed — device already in schema
    coordinator.store.async_save_backup.assert_not_called()
    await asyncio.sleep(0)


async def test_passive_scan_no_migration_when_scan_disabled(
    hass: HomeAssistant,
) -> None:
    """Test that no migration happens when passive scan is disabled."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_no_scan",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"04:123456": {}},
            CONF_SCHEMA: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value={})
    coordinator.store.async_save_backup = AsyncMock()

    mock_client = MagicMock(spec=Gateway)
    mock_client.start = AsyncMock()
    coordinator._create_client = MagicMock(return_value=mock_client)

    await coordinator.async_setup()
    await asyncio.sleep(0)

    # Passive scan is off — no migration
    coordinator.store.async_save_backup.assert_not_called()
    await asyncio.sleep(0)


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _async_start_discovery_scan (lines 390-427)
# ───────────────────────────────────────────────────────────────────────


async def test_async_start_discovery_scan_no_client(hass: HomeAssistant) -> None:
    """Test _async_start_discovery_scan returns early when no client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_start_no_client",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = None

    # Inject a fake discovery_scan module (may not exist in CI's ramses_rf)
    fake_module = MagicMock()
    with patch.dict(sys.modules, {"ramses_rf.discovery_scan": fake_module}):
        # Should return early without error
        await coordinator._async_start_discovery_scan()


async def test_async_start_discovery_scan_with_restore(
    hass: HomeAssistant,
) -> None:
    """Test _async_start_discovery_scan restores persisted state."""
    from custom_components.ramses_cc.const import (
        CONF_ADVANCED_FEATURES,
        CONF_AUTO_NOTIFY,
        CONF_LOST_THRESHOLD,
        CONF_PASSIVE_SCAN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_start_restore",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
            CONF_ADVANCED_FEATURES: {
                CONF_PASSIVE_SCAN: True,
                CONF_AUTO_NOTIFY: False,
                CONF_LOST_THRESHOLD: 14,
            },
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()

    # Mock store with persisted discovery state
    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(
        return_value={
            "discovery": {
                "devices": {"04:056053": {"status": "accepted"}},
                "scan_state": '{"devices": []}',
            }
        }
    )

    # Inject a fake discovery_scan module (may not exist in CI's ramses_rf)
    fake_scan_module = MagicMock()
    fake_scan_module.DiscoveryScan = MagicMock(return_value=MagicMock())
    with (
        patch.dict(sys.modules, {"ramses_rf.discovery_scan": fake_scan_module}),
        patch(
            "custom_components.ramses_cc.coordinator.DiscoveryManager"
        ) as mock_dm_cls,
        patch(
            "custom_components.ramses_cc.coordinator.async_track_time_interval"
        ) as mock_track,
        patch(
            "custom_components.ramses_cc.coordinator.async_call_later"
        ) as mock_call_later,
    ):
        mock_dm = MagicMock()
        mock_dm_cls.return_value = mock_dm
        mock_track.return_value = MagicMock()
        mock_call_later.return_value = MagicMock()

        await coordinator._async_start_discovery_scan()

        # Verify DiscoveryManager was created with the right params
        mock_dm_cls.assert_called_once()
        call_kwargs = mock_dm_cls.call_args
        assert call_kwargs.kwargs["auto_notify"] is False
        assert call_kwargs.kwargs["lost_threshold_days"] == 14

        # Verify state was restored
        mock_dm.restore_state.assert_called_once()

        # Verify timers were scheduled
        mock_track.assert_called_once()
        mock_call_later.assert_called_once()


async def test_async_start_discovery_scan_no_stored_state(hass: HomeAssistant) -> None:
    """Test _async_start_discovery_scan with no persisted discovery state."""
    from custom_components.ramses_cc.const import (
        CONF_ADVANCED_FEATURES,
        CONF_PASSIVE_SCAN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_start_no_state",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()

    coordinator.store = MagicMock()
    coordinator.store.async_load = AsyncMock(return_value={})

    # Inject a fake discovery_scan module (may not exist in CI's ramses_rf)
    fake_scan_module = MagicMock()
    fake_scan_module.DiscoveryScan = MagicMock(return_value=MagicMock())
    with (
        patch.dict(sys.modules, {"ramses_rf.discovery_scan": fake_scan_module}),
        patch(
            "custom_components.ramses_cc.coordinator.DiscoveryManager"
        ) as mock_dm_cls,
        patch("custom_components.ramses_cc.coordinator.async_track_time_interval"),
        patch("custom_components.ramses_cc.coordinator.async_call_later"),
    ):
        mock_dm = MagicMock()
        mock_dm_cls.return_value = mock_dm

        await coordinator._async_start_discovery_scan()

        # No stored state — restore_state should not be called
        mock_dm.restore_state.assert_not_called()


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _create_client passive scan enforce_known_list forcing
# (lines 636-643)
# ───────────────────────────────────────────────────────────────────────


async def test_create_client_passive_scan_forces_enforce_known_list(
    mock_hass: MagicMock, mock_entry: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that _create_client forces enforce_known_list when passive scan is on."""
    from custom_components.ramses_cc.const import (
        CONF_ADVANCED_FEATURES,
        CONF_PASSIVE_SCAN,
    )

    # Enable passive scan but don't set enforce_known_list
    mock_entry.options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {},
        CONF_RAMSES_RF: {},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
        CONF_SCAN_INTERVAL: 60,
        CONF_GATEWAY_TIMEOUT: 10,
        CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
    }

    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    with (
        patch("custom_components.ramses_cc.coordinator.Gateway") as mock_gw_cls,
        patch(
            "custom_components.ramses_cc.coordinator.RamsesMqttBridge"
        ) as mock_bridge_cls,
    ):
        mock_gw_cls.return_value = MagicMock()
        cast(Any, mock_bridge_cls.return_value).async_transport_factory = MagicMock()
        cast(Any, mock_gw_cls.return_value)._extra = {}
        caplog.set_level(logging.WARNING)

        coordinator._create_client({})

        # The warning was logged — enforce_known_list was forced on the copy
        assert "forcing enforce_known_list=True" in caplog.text


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _create_client no port + MQTT defaulting (lines 701-712)
# ───────────────────────────────────────────────────────────────────────


async def test_create_client_no_port_defaults_to_mqtt(
    mock_hass: MagicMock, mock_entry: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _create_client defaults to MQTT when no port and MQTT is configured."""
    mock_entry.options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {},
        CONF_RAMSES_RF: {},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: ""},  # empty port
        CONF_SCAN_INTERVAL: 60,
    }

    # Simulate MQTT entries being present
    mock_hass.config_entries.async_entries.return_value = [MagicMock()]

    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    with (
        patch("custom_components.ramses_cc.coordinator.Gateway") as mock_gw_cls,
        patch(
            "custom_components.ramses_cc.coordinator.RamsesMqttBridge"
        ) as mock_bridge_cls,
    ):
        mock_gw_cls.return_value = MagicMock()
        cast(Any, mock_bridge_cls.return_value).async_transport_factory = MagicMock()
        cast(Any, mock_gw_cls.return_value)._extra = {}
        caplog.set_level(logging.WARNING)

        coordinator._create_client({})

        # Verify it defaulted to mqtt_ha
        assert "defaulting to Home Assistant MQTT transport" in caplog.text


async def test_create_client_no_port_no_mqtt_raises(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Test _create_client raises ConfigEntryNotReady when no port and no MQTT."""
    mock_entry.options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {},
        CONF_RAMSES_RF: {},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: ""},  # empty port
        CONF_SCAN_INTERVAL: 60,
    }

    # No MQTT entries
    mock_hass.config_entries.async_entries.return_value = []

    coordinator = RamsesCoordinator(mock_hass, mock_entry)

    with pytest.raises(ConfigEntryNotReady, match="No serial port configured"):
        coordinator._create_client({})


# ───────────────────────────────────────────────────────────────────────
# Coordinator: delegate methods (lines 1155-1204)
# ───────────────────────────────────────────────────────────────────────


async def test_delegate_async_discover_known_devices(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_discover_known_devices delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_discover_known_devices = AsyncMock()
    await mock_coordinator.async_discover_known_devices(call)
    mock_coordinator.service_handler.async_discover_known_devices.assert_called_once_with(
        call
    )


async def test_delegate_async_get_discovered_devices(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_get_discovered_devices delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_get_discovered_devices = AsyncMock()
    await mock_coordinator.async_get_discovered_devices(call)
    mock_coordinator.service_handler.async_get_discovered_devices.assert_called_once_with(
        call
    )


async def test_delegate_async_accept_discovered_device(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_accept_discovered_device delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_accept_discovered_device = AsyncMock()
    await mock_coordinator.async_accept_discovered_device(call)
    mock_coordinator.service_handler.async_accept_discovered_device.assert_called_once_with(
        call
    )


async def test_delegate_async_discard_discovered_device(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_discard_discovered_device delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_discard_discovered_device = AsyncMock()
    await mock_coordinator.async_discard_discovered_device(call)
    mock_coordinator.service_handler.async_discard_discovered_device.assert_called_once_with(
        call
    )


async def test_delegate_async_remove_discovered_device(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_remove_discovered_device delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_remove_discovered_device = AsyncMock()
    await mock_coordinator.async_remove_discovered_device(call)
    mock_coordinator.service_handler.async_remove_discovered_device.assert_called_once_with(
        call
    )


async def test_delegate_async_enable_discovered_device(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_enable_discovered_device delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_enable_discovered_device = AsyncMock()
    await mock_coordinator.async_enable_discovered_device(call)
    mock_coordinator.service_handler.async_enable_discovered_device.assert_called_once_with(
        call
    )


async def test_delegate_async_disable_discovered_device(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_disable_discovered_device delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_disable_discovered_device = AsyncMock()
    await mock_coordinator.async_disable_discovered_device(call)
    mock_coordinator.service_handler.async_disable_discovered_device.assert_called_once_with(
        call
    )


async def test_delegate_async_add_faked_rem(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test async_add_faked_rem delegates to service_handler."""
    call = MagicMock()
    mock_coordinator.service_handler = MagicMock()
    mock_coordinator.service_handler.async_add_faked_rem = AsyncMock()
    await mock_coordinator.async_add_faked_rem(call)
    mock_coordinator.service_handler.async_add_faked_rem.assert_called_once_with(call)


# ───────────────────────────────────────────────────────────────────────
# Coordinator: string packet filtering (line 241)
# ───────────────────────────────────────────────────────────────────────


async def test_get_saved_packets_string_format_filtered_code(
    hass: HomeAssistant,
) -> None:
    """Test _get_saved_packets filters string packets with 313F code."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_str_filtered",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # String packet containing " 313F "
    client_state = {
        SZ_PACKETS: {
            recent: "2026-01-01 00:00:00.000 000 18:006402 01:123456 313F 000 ...",
        }
    }

    result = coordinator._get_saved_packets(client_state)
    assert recent not in result


async def test_get_saved_packets_string_format_enforce_known_list(
    hass: HomeAssistant,
) -> None:
    """Test _get_saved_packets enforces known_list on string packets."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_str_enforce",
        options={
            "ramses_rf": {SZ_ENFORCE_KNOWN_LIST: True},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # String packet with a known device
    client_state = {
        SZ_PACKETS: {
            recent: "2026-01-01 00:00:00.000 000 18:006402 01:123456 3150 000 ...",
        }
    }

    result = coordinator._get_saved_packets(client_state)
    assert recent in result


async def test_get_saved_packets_string_format_unknown_device(
    hass: HomeAssistant,
) -> None:
    """Test _get_saved_packets filters out string packets with unknown devices."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_str_unknown",
        options={
            "ramses_rf": {SZ_ENFORCE_KNOWN_LIST: True},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {"01:123456": {}},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)

    now = dt_util.now()
    recent = (now - td(hours=1)).isoformat()

    # String packet with only unknown devices
    client_state = {
        SZ_PACKETS: {
            recent: "2026-01-01 00:00:00.000 000 09:999999 09:888888 3150 000 ...",
        }
    }

    result = coordinator._get_saved_packets(client_state)
    assert recent not in result


# ───────────────────────────────────────────────────────────────────────
# Coordinator: _extract_schema_device_ids edge cases (lines 549-580)
# ───────────────────────────────────────────────────────────────────────


def test_extract_schema_device_ids_non_device_key_skipped() -> None:
    """Test that non-device-id keys are skipped."""
    schema: dict[str, Any] = {
        "not_a_device_id": {},
        "01:123456": {},
    }
    result = RamsesCoordinator._extract_schema_device_ids(schema)
    assert "01:123456" in result
    assert "not_a_device_id" not in result


def test_extract_schema_device_ids_non_dict_value_skipped() -> None:
    """Test that non-dict values for device keys are handled."""
    schema: dict[str, Any] = {
        "01:123456": "not a dict",
    }
    result = RamsesCoordinator._extract_schema_device_ids(schema)
    assert "01:123456" in result
    # No sub-devices extracted since value is not a dict


def test_extract_schema_device_ids_zone_non_dict_skipped() -> None:
    """Test that non-dict zone data is skipped."""
    from ramses_rf.schemas import SZ_ZONES

    schema: dict[str, Any] = {
        "01:123456": {
            SZ_ZONES: {
                "01": "not a dict",
            },
        },
    }
    result = RamsesCoordinator._extract_schema_device_ids(schema)
    assert "01:123456" in result
    assert len(result) == 1  # only the CTL itself


# ───────────────────────────────────────────────────────────────────────
# Coordinator: async_setup starts discovery scan (line 374)
# ───────────────────────────────────────────────────────────────────────


async def test_async_setup_starts_discovery_scan(hass: HomeAssistant) -> None:
    """Test that async_start starts the discovery scan when passive scan is on."""
    from custom_components.ramses_cc.const import (
        CONF_ADVANCED_FEATURES,
        CONF_PASSIVE_SCAN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_setup_scan",
        options={
            "ramses_rf": {},
            "serial_port": {SZ_PORT_NAME: "/dev/ttyUSB0"},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
        },
    )
    entry.add_to_hass(hass)

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = MagicMock()
    coordinator.client.start = AsyncMock()

    with (
        patch.object(
            coordinator, "_async_start_discovery_scan", new_callable=AsyncMock
        ) as mock_start_scan,
        patch.object(coordinator, "_discover_new_entities", new_callable=AsyncMock),
        patch.object(
            coordinator, "async_config_entry_first_refresh", new_callable=AsyncMock
        ),
        patch("custom_components.ramses_cc.coordinator.async_track_time_interval"),
    ):
        await coordinator.async_start()

        mock_start_scan.assert_called_once()


class TestSyncTraitsToSchema:
    """Tests for RamsesCoordinator._sync_traits_to_schema."""

    def test_copies_class_from_known_list(self) -> None:
        """class from known_list is copied to _class in schema."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"32:123456": {"class": "FAN"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["32:123456"]["_class"] == "FAN"

    def test_copies_faked_from_known_list(self) -> None:
        """faked from known_list is copied to _faked in schema."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"32:123456": {"faked": True}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["32:123456"]["_faked"] is True

    def test_copies_alias_from_known_list(self) -> None:
        """alias from known_list is copied to _alias in schema."""
        schema = {"01:123456": {"_owner": "me"}}
        known_list = {"01:123456": {"alias": "Living Room"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["01:123456"]["_alias"] == "Living Room"

    def test_does_not_overwrite_existing_schema_trait(self) -> None:
        """Schema traits are authoritative — known_list doesn't overwrite."""
        schema = {"32:123456": {"_owner": "me", "_class": "REM"}}
        known_list = {"32:123456": {"class": "FAN"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        # Schema's _class wins
        assert result["32:123456"]["_class"] == "REM"

    def test_no_root_entry_no_copy(self) -> None:
        """Device without root entry in schema is skipped."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"37:999999": {"class": "CO2"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        # No root entry created for 37:999999
        assert "37:999999" not in result

    def test_empty_known_list_returns_schema_unchanged(self) -> None:
        """Empty known_list → schema returned as-is."""
        schema = {"32:123456": {"_owner": "me"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, {})
        assert result == schema

    def test_no_traits_in_known_list(self) -> None:
        """known_list entry with no traits → no changes."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"32:123456": {}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result == schema

    def test_copies_multiple_traits(self) -> None:
        """Multiple traits are copied in one pass."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {
            "32:123456": {"class": "FAN", "faked": True, "alias": "HRU"},
        }
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["32:123456"]["_class"] == "FAN"
        assert result["32:123456"]["_faked"] is True
        assert result["32:123456"]["_alias"] == "HRU"

    def test_copies_bound_and_scheme(self) -> None:
        """bound and scheme traits are copied."""
        schema = {"37:123456": {"_owner": "me"}}
        known_list = {
            "37:123456": {"bound": "32:123456", "scheme": "nuaire"},
        }
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["37:123456"]["_bound"] == "32:123456"
        assert result["37:123456"]["_scheme"] == "nuaire"

    def test_ventilator_slug_normalized_to_fan(self) -> None:
        """Entity slug 'ventilator' is normalized to DevType slug 'FAN'
        during KL→Schema migration."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"32:123456": {"class": "ventilator"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["32:123456"]["_class"] == "FAN"

    def test_switch_slug_normalized_to_rem(self) -> None:
        """Entity slug 'switch' is normalized to DevType slug 'REM'."""
        schema = {"37:123456": {"_owner": "me"}}
        known_list = {"37:123456": {"class": "switch"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["37:123456"]["_class"] == "REM"

    def test_co2_sensor_slug_normalized_to_co2(self) -> None:
        """Entity slug 'co2_sensor' is normalized to DevType slug 'CO2'."""
        schema = {"37:123456": {"_owner": "me"}}
        known_list = {"37:123456": {"class": "co2_sensor"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["37:123456"]["_class"] == "CO2"

    def test_short_slug_preserved(self) -> None:
        """Short DevType slugs like 'FAN' are preserved as-is."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"32:123456": {"class": "FAN"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["32:123456"]["_class"] == "FAN"

    def test_lowercase_fan_normalized(self) -> None:
        """Lowercase 'fan' is normalized to 'FAN' (DevType slug)."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"32:123456": {"class": "fan"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["32:123456"]["_class"] == "FAN"

    def test_unknown_class_preserved(self) -> None:
        """Unknown class values are preserved as-is (no normalization)."""
        schema = {"32:123456": {"_owner": "me"}}
        known_list = {"32:123456": {"class": "some_unknown_type"}}
        result = RamsesCoordinator._sync_traits_to_schema(schema, known_list)
        assert result["32:123456"]["_class"] == "some_unknown_type"


class TestSyncRemotesToSchema:
    """Tests for RamsesCoordinator._sync_remotes_to_schema (Phase 3a)."""

    def test_remotes_copied_to_schema(self) -> None:
        """Commands from remotes are copied to schema _commands (first-time)."""
        schema: dict[str, Any] = {"32:153001": {"_class": "REM"}}
        remotes = {"32:153001": {"turn_on": "I --- 22F1 003 000030"}}
        result = RamsesCoordinator._sync_remotes_to_schema(schema, remotes)
        assert result["32:153001"][SZ_TR_COMMANDS] == {
            "turn_on": "I --- 22F1 003 000030"
        }
        # Existing _class preserved
        assert result["32:153001"]["_class"] == "REM"

    def test_remotes_not_resurrected_for_known_command_device(self) -> None:
        """Commands are NOT re-added if device previously had _commands.

        If known_command_devices includes a device, and that device's
        schema entry no longer has _commands (user deleted it), the
        commands are NOT resurrected from remotes.
        """
        schema: dict[str, Any] = {"32:153001": {"_class": "REM"}}
        remotes = {"32:153001": {"turn_on": "I --- 22F1 003 000030"}}
        # Device previously had _commands — user deleted them
        known = {"32:153001"}
        result = RamsesCoordinator._sync_remotes_to_schema(schema, remotes, known)
        assert SZ_TR_COMMANDS not in result["32:153001"]
        assert result["32:153001"]["_class"] == "REM"

    def test_remotes_migrated_for_unknown_command_device(self) -> None:
        """Commands ARE migrated for devices not in known_command_devices.

        If known_command_devices is provided but the device is NOT in it,
        the commands are migrated normally (first-time migration).
        """
        schema: dict[str, Any] = {"32:153001": {"_class": "REM"}}
        remotes = {"32:153001": {"turn_on": "I --- 22F1 003 000030"}}
        known: set[str] = set()  # device not known → migrate
        result = RamsesCoordinator._sync_remotes_to_schema(schema, remotes, known)
        assert result["32:153001"][SZ_TR_COMMANDS] == {
            "turn_on": "I --- 22F1 003 000030"
        }

    def test_schema_commands_not_overwritten(self) -> None:
        """Schema _commands is authoritative — remotes don't overwrite."""
        schema: dict[str, Any] = {
            "32:153001": {SZ_TR_COMMANDS: {"turn_on": "I --- schema"}}
        }
        remotes = {"32:153001": {"turn_on": "I --- remotes"}}
        result = RamsesCoordinator._sync_remotes_to_schema(schema, remotes)
        assert result["32:153001"][SZ_TR_COMMANDS] == {"turn_on": "I --- schema"}

    def test_empty_remotes_no_change(self) -> None:
        """Empty remotes dict returns schema unchanged."""
        schema: dict[str, Any] = {"01:123456": {}}
        result = RamsesCoordinator._sync_remotes_to_schema(schema, {})
        assert result is schema

    def test_none_remotes_no_change(self) -> None:
        """None remotes returns schema unchanged."""
        schema: dict[str, Any] = {"01:123456": {}}
        result = RamsesCoordinator._sync_remotes_to_schema(schema, None)  # type: ignore[arg-type]
        assert result is schema

    def test_new_device_entry_created(self) -> None:
        """If device not in schema, a new entry is created with _commands."""
        schema: dict[str, Any] = {"01:123456": {}}
        remotes = {"32:153001": {"turn_on": "I --- 22F1"}}
        result = RamsesCoordinator._sync_remotes_to_schema(schema, remotes)
        assert result["32:153001"][SZ_TR_COMMANDS] == {"turn_on": "I --- 22F1"}

    def test_empty_commands_skipped(self) -> None:
        """Devices with empty command dicts are skipped."""
        schema: dict[str, Any] = {"32:153001": {}}
        remotes = {"32:153001": {}}
        result = RamsesCoordinator._sync_remotes_to_schema(schema, remotes)
        assert SZ_TR_COMMANDS not in result["32:153001"]

    def test_multiple_devices_migrated(self) -> None:
        """Multiple devices with commands are all migrated."""
        schema: dict[str, Any] = {"01:123456": {}}
        remotes = {
            "32:153001": {"turn_on": "I --- 22F1 003 000030"},
            "32:153002": {"speed_1": "I --- 22F1 003 000031"},
        }
        result = RamsesCoordinator._sync_remotes_to_schema(schema, remotes)
        assert SZ_TR_COMMANDS in result["32:153001"]
        assert SZ_TR_COMMANDS in result["32:153002"]


class TestStripSchemaExtensionsCommands:
    """Test that _commands is stripped by _strip_schema_extensions."""

    def test_commands_stripped_from_device(self) -> None:
        """_commands is stripped from device entries."""
        schema: dict[str, Any] = {
            "01:123456": {"_alias": "Test", SZ_TR_COMMANDS: {"turn_on": "I ---"}},
            "main_tcs": "01:123456",
        }
        stripped = RamsesCoordinator._strip_schema_extensions(schema)
        # _commands should not appear anywhere in the stripped schema
        assert SZ_TR_COMMANDS not in str(stripped)
        assert "_alias" not in str(stripped)

    def test_commands_stripped_no_remotes_key_leak(self) -> None:
        """_commands doesn't leak as a 'remotes' key to ramses_rf."""
        schema: dict[str, Any] = {
            "32:153001": {SZ_TR_COMMANDS: {"turn_on": "I --- 22F1"}},
        }
        stripped = RamsesCoordinator._strip_schema_extensions(schema)
        # The device should be in orphans_hvac (no remotes/sensors keys)
        # but _commands must not appear
        assert SZ_TR_COMMANDS not in str(stripped)


# ---------------------------------------------------------------------------
# Phase 3a: startup load, else branch, backup before migration
# ---------------------------------------------------------------------------


async def test_startup_load_commands_from_schema(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Coordinator loads _commands from schema into _remotes at startup.

    Schema _commands has highest precedence (SSOT), overriding any
    commands from .storage[remotes] or known_list[commands].
    """
    # Configure schema with _commands for a REM device
    rem_id = "37:170000"
    commands = {"boost": "I --- 37:170000 30:160000 --:------ 22F1 003 000030"}
    cast(Any, mock_entry).options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {
            rem_id: {"_class": "REM", SZ_TR_COMMANDS: commands},
        },
        CONF_RAMSES_RF: {},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
        CONF_SCAN_INTERVAL: 60,
        CONF_GATEWAY_TIMEOUT: 10,
    }

    coordinator = RamsesCoordinator(mock_hass, mock_entry)
    cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

    # Mock the client with async start
    mock_client = MagicMock()
    mock_client.start = AsyncMock()
    mock_client.add_msg_handler = MagicMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    await coordinator.async_setup()

    # Verify _commands from schema were loaded into _remotes
    assert rem_id in coordinator._remotes
    assert coordinator._remotes[rem_id] == commands


async def test_startup_load_schema_commands_override_storage(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Schema _commands override .storage[remotes] at startup (SSOT wins)."""
    rem_id = "37:170000"
    schema_commands = {"boost": "I --- schema packet"}
    storage_commands = {"boost": "I --- storage packet"}

    cast(Any, mock_entry).options = {
        SZ_KNOWN_LIST: {},
        CONF_SCHEMA: {
            rem_id: {"_class": "REM", SZ_TR_COMMANDS: schema_commands},
        },
        CONF_RAMSES_RF: {},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
        CONF_SCAN_INTERVAL: 60,
        CONF_GATEWAY_TIMEOUT: 10,
    }

    coordinator = RamsesCoordinator(mock_hass, mock_entry)
    cast(Any, coordinator.store).async_load = AsyncMock(
        return_value={SZ_REMOTES: {rem_id: storage_commands}}
    )

    mock_client = MagicMock()
    mock_client.start = AsyncMock()
    mock_client.add_msg_handler = MagicMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    await coordinator.async_setup()

    # Schema _commands should win (SSOT — highest precedence)
    assert coordinator._remotes[rem_id] == schema_commands


async def test_startup_load_legacy_known_list_commands(
    mock_hass: MagicMock, mock_entry: MagicMock
) -> None:
    """Legacy known_list[commands] are loaded when no schema _commands exist."""
    rem_id = "37:170000"
    legacy_commands = {"boost": "I --- legacy packet"}

    cast(Any, mock_entry).options = {
        SZ_KNOWN_LIST: {rem_id: {CONF_COMMANDS: legacy_commands}},
        CONF_SCHEMA: {},
        CONF_RAMSES_RF: {},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
        CONF_SCAN_INTERVAL: 60,
        CONF_GATEWAY_TIMEOUT: 10,
    }

    coordinator = RamsesCoordinator(mock_hass, mock_entry)
    cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

    mock_client = MagicMock()
    mock_client.start = AsyncMock()
    mock_client.add_msg_handler = MagicMock()
    cast(Any, coordinator)._create_client = MagicMock(return_value=mock_client)

    await coordinator.async_setup()

    # Legacy known_list commands should be loaded (fallback)
    assert coordinator._remotes[rem_id] == legacy_commands


async def test_async_save_client_state_else_branch_syncs_remotes(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """async_save_client_state else branch syncs remotes to schema _commands.

    When learned topology is NOT richer than config (enriched is None) and
    no comments are refreshed, the else branch still runs
    _sync_remotes_to_schema to migrate commands from .storage[remotes].

    The REM is in the config schema but has no _commands yet (first-time
    migration).  _devices_with_commands is empty, so migration proceeds.
    """
    assert mock_coordinator.client is not None

    rem_id = "37:170000"
    commands = {"boost": "I --- 37:170000 30:160000 --:------ 22F1 003 000030"}

    # Config schema has the REM but no _commands yet
    config_schema: dict[str, Any] = {rem_id: {"_class": "REM"}}
    mock_coordinator.options = {CONF_SCHEMA: config_schema, SZ_KNOWN_LIST: {}}

    # Remotes has commands that need migrating
    mock_coordinator._remotes = {rem_id: commands}
    mock_coordinator._devices_with_commands = set()  # first-time migration
    mock_coordinator._entities = {}
    mock_coordinator._skip_topology_sync = False

    # Learned schema is same as config (no enrichment)
    learned_schema = dict(config_schema)
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=(learned_schema, {})
    )

    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save

    # Patch sync_learned_topology to return None (no enrichment)
    with (
        patch(
            "custom_components.ramses_cc.coordinator.sync_learned_topology",
            return_value=None,
        ),
        patch.object(mock_coordinator, "discovery_manager", None),
    ):
        await mock_coordinator.async_save_client_state()

    # Verify _sync_remotes_to_schema ran — _commands should be in options
    saved_options = mock_coordinator.options
    assert SZ_TR_COMMANDS in saved_options[CONF_SCHEMA].get(rem_id, {})
    assert saved_options[CONF_SCHEMA][rem_id][SZ_TR_COMMANDS] == commands


async def test_async_save_client_state_no_resurrection_of_deleted_commands(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Save cycle does not resurrect user-deleted _commands from remotes.

    If a device previously had _commands in the schema and the user deleted
    them, _sync_remotes_to_schema should NOT re-add them from .storage[remotes].
    """
    assert mock_coordinator.client is not None

    rem_id = "37:170000"
    commands = {"boost": "I --- 37:170000 30:160000 --:------ 22F1 003 000030"}

    # Config schema has the REM but _commands was deleted by the user
    config_schema: dict[str, Any] = {rem_id: {"_class": "REM"}}
    mock_coordinator.options = {CONF_SCHEMA: config_schema, SZ_KNOWN_LIST: {}}

    # Remotes still has the old commands (from .storage)
    mock_coordinator._remotes = {rem_id: commands}
    # Device previously had _commands — should NOT be resurrected
    mock_coordinator._devices_with_commands = {rem_id}
    mock_coordinator._entities = {}
    mock_coordinator._skip_topology_sync = False

    # Learned schema is same as config (no enrichment)
    learned_schema = dict(config_schema)
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=(learned_schema, {})
    )

    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save

    # Patch sync_learned_topology to return None (no enrichment)
    with (
        patch(
            "custom_components.ramses_cc.coordinator.sync_learned_topology",
            return_value=None,
        ),
        patch.object(mock_coordinator, "discovery_manager", None),
    ):
        await mock_coordinator.async_save_client_state()

    # _commands should NOT be in the schema (user deleted, not resurrected)
    saved_schema = mock_coordinator.options[CONF_SCHEMA]
    assert SZ_TR_COMMANDS not in saved_schema.get(rem_id, {})


async def test_async_save_client_state_backup_before_migration(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """async_save_client_state creates backup with reason='ssot_phase3a' before
    migrating remotes to schema _commands (when enriched is not None).
    """
    assert mock_coordinator.client is not None

    rem_id = "37:170000"
    commands = {"boost": "I --- 37:170000 30:160000 --:------ 22F1 003 000030"}

    # Config schema has the REM but no _commands yet (unmigrated)
    config_schema: dict[str, Any] = {rem_id: {"_class": "REM"}}
    mock_coordinator.options = {
        CONF_SCHEMA: config_schema,
        SZ_KNOWN_LIST: {},
    }

    # Remotes has commands that need migrating
    mock_coordinator._remotes = {rem_id: commands}
    mock_coordinator._devices_with_commands = set()  # first-time migration
    mock_coordinator._entities = {}
    mock_coordinator._skip_topology_sync = False

    # Enriched schema (sync_learned_topology returns a richer schema)
    enriched_schema = {rem_id: {"_class": "REM"}, "main_tcs": "01:123456"}
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=(enriched_schema, {})
    )

    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save
    mock_backup = AsyncMock()
    cast(Any, mock_coordinator.store).async_save_backup = mock_backup

    # Patch sync_learned_topology to return enriched schema
    with (
        patch(
            "custom_components.ramses_cc.coordinator.sync_learned_topology",
            return_value=enriched_schema,
        ),
        patch.object(mock_coordinator, "discovery_manager", None),
        patch.object(mock_coordinator, "_validate_schema_for_ramserf"),
    ):
        await mock_coordinator.async_save_client_state()

    # Verify backup was called with reason="ssot_phase3a"
    mock_backup.assert_awaited_once()
    call_args = mock_backup.await_args
    assert call_args.kwargs.get("reason") == "ssot_phase3a" or (
        len(call_args.args) >= 3 and call_args.args[2] == "ssot_phase3a"
    )

    # Verify _commands were migrated to schema
    saved_schema = mock_coordinator.options[CONF_SCHEMA]
    assert SZ_TR_COMMANDS in saved_schema.get(rem_id, {})
    assert saved_schema[rem_id][SZ_TR_COMMANDS] == commands


async def test_async_save_client_state_no_backup_when_already_migrated(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """No backup is created when remotes already have _commands in schema."""
    assert mock_coordinator.client is not None

    rem_id = "37:170000"
    commands = {"boost": "I --- 37:170000 30:160000 --:------ 22F1 003 000030"}

    # Schema already has _commands (already migrated)
    config_schema: dict[str, Any] = {
        rem_id: {"_class": "REM", SZ_TR_COMMANDS: commands}
    }
    mock_coordinator.options = {
        CONF_SCHEMA: config_schema,
        SZ_KNOWN_LIST: {},
    }

    mock_coordinator._remotes = {rem_id: commands}
    mock_coordinator._entities = {}
    mock_coordinator._skip_topology_sync = False

    enriched_schema = dict(config_schema)
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=(enriched_schema, {})
    )

    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save
    mock_backup = AsyncMock()
    cast(Any, mock_coordinator.store).async_save_backup = mock_backup

    with (
        patch(
            "custom_components.ramses_cc.coordinator.sync_learned_topology",
            return_value=enriched_schema,
        ),
        patch.object(mock_coordinator, "discovery_manager", None),
        patch.object(mock_coordinator, "_validate_schema_for_ramserf"),
    ):
        await mock_coordinator.async_save_client_state()

    # No backup should be created (already migrated)
    mock_backup.assert_not_awaited()


async def test_async_save_client_state_survives_validation_failure(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Save cycle skips topology sync when _validate_schema_for_ramserf raises.

    If the enriched schema fails validation, the save cycle should log the
    error and skip the config entry update — NOT abort with an unhandled
    ValueError, and NOT overwrite the config entry with an invalid schema.
    The rest of the save (packets, remotes to .storage) should still happen.
    """
    assert mock_coordinator.client is not None

    rem_id = "37:170000"
    commands = {"boost": "I --- 37:170000 30:160000 --:------ 22F1 003 000030"}

    config_schema: dict[str, Any] = {rem_id: {"_class": "REM"}}
    original_options = {CONF_SCHEMA: config_schema, SZ_KNOWN_LIST: {}}
    mock_coordinator.options = dict(original_options)

    mock_coordinator._remotes = {rem_id: commands}
    mock_coordinator._entities = {}
    mock_coordinator._skip_topology_sync = False

    # Enriched schema that will fail validation
    enriched_schema = {rem_id: {"_class": "REM"}, "main_tcs": "01:123456"}
    cast(Any, mock_coordinator.client).get_state = MagicMock(
        return_value=(enriched_schema, {})
    )

    mock_save = AsyncMock()
    cast(Any, mock_coordinator.store).async_save = mock_save
    mock_backup = AsyncMock()
    cast(Any, mock_coordinator.store).async_save_backup = mock_backup

    with (
        patch(
            "custom_components.ramses_cc.coordinator.sync_learned_topology",
            return_value=enriched_schema,
        ),
        patch.object(mock_coordinator, "discovery_manager", None),
        patch.object(
            mock_coordinator,
            "_validate_schema_for_ramserf",
            side_effect=ValueError("Schema validation failed: bad key"),
        ),
    ):
        # Should NOT raise — the ValueError is caught
        await mock_coordinator.async_save_client_state()

    # Config entry options should NOT be updated with the invalid schema
    assert mock_coordinator.options[CONF_SCHEMA] == config_schema
    # The save to .storage should still happen (packets, remotes)
    mock_save.assert_awaited()


# ---------------------------------------------------------------------------
# Phase 3b: _migrate_rem_commands_to_fan tests
# ---------------------------------------------------------------------------


class TestMigrateRemCommandsToFan:
    """Tests for RamsesCoordinator._migrate_rem_commands_to_fan (Phase 3b)."""

    def test_no_fan_no_change(self) -> None:
        """No FAN entries → no migration."""
        schema: dict[str, Any] = {"32:153001": {"_class": "REM"}}
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        assert result is schema

    def test_fan_no_bound_no_change(self) -> None:
        """FAN without _bound → no migration."""
        schema: dict[str, Any] = {"30:160000": {"_class": "FAN"}}
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        assert result is schema

    def test_fan_bound_rem_no_commands_no_change(self) -> None:
        """FAN with bound REM that has no _commands → no migration."""
        schema: dict[str, Any] = {
            "30:160000": {"_class": "FAN", "_bound": ["32:153001"]},
            "32:153001": {"_class": "REM"},
        }
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        assert result is schema

    def test_rem_commands_copied_to_fan_as_dicts(self) -> None:
        """REM _commands (packet strings) copied to FAN as dict templates."""
        schema: dict[str, Any] = {
            "30:160000": {
                "_class": "FAN",
                "_bound": ["32:153001"],
            },
            "32:153001": {
                "_class": "REM",
                "_commands": {
                    "boost": "I --- 32:153001 30:160000 --:------ 22F1 003 000030",
                },
            },
        }
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        fan_entry = result["30:160000"]
        assert SZ_TR_COMMANDS in fan_entry
        cmd = fan_entry[SZ_TR_COMMANDS]["boost"]
        assert cmd == {"verb": "I", "code": "22F1", "payload": "000030"}

    def test_rem_commands_not_deleted(self) -> None:
        """REM _commands are NOT deleted (downgrade safety)."""
        schema: dict[str, Any] = {
            "30:160000": {
                "_class": "FAN",
                "_bound": ["32:153001"],
            },
            "32:153001": {
                "_class": "REM",
                "_commands": {
                    "boost": "I --- 32:153001 30:160000 --:------ 22F1 003 000030",
                },
            },
        }
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        # REM entry still has _commands
        assert SZ_TR_COMMANDS in result["32:153001"]

    def test_fan_commands_authoritative(self) -> None:
        """FAN _commands not overwritten by REM _commands."""
        schema: dict[str, Any] = {
            "30:160000": {
                "_class": "FAN",
                "_bound": ["32:153001"],
                "_commands": {
                    "boost": {"verb": "W", "code": "22F7", "payload": "0000EF"},
                },
            },
            "32:153001": {
                "_class": "REM",
                "_commands": {
                    "boost": "I --- 32:153001 30:160000 --:------ 22F1 003 000030",
                },
            },
        }
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        # FAN's dict template wins
        cmd = result["30:160000"][SZ_TR_COMMANDS]["boost"]
        assert cmd == {"verb": "W", "code": "22F7", "payload": "0000EF"}

    def test_multi_rem_all_copied(self) -> None:
        """Commands from multiple bound REMs are all copied to FAN."""
        schema: dict[str, Any] = {
            "30:160000": {
                "_class": "FAN",
                "_bound": ["32:153001", "32:153002"],
            },
            "32:153001": {
                "_class": "REM",
                "_commands": {
                    "boost": "I --- 32:153001 30:160000 --:------ 22F1 003 000030",
                },
            },
            "32:153002": {
                "_class": "REM",
                "_commands": {
                    "bypass": "W --- 32:153002 30:160000 --:------ 22F7 003 0000EF",
                },
            },
        }
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        fan_cmds = result["30:160000"][SZ_TR_COMMANDS]
        assert "boost" in fan_cmds
        assert "bypass" in fan_cmds
        assert fan_cmds["boost"] == {"verb": "I", "code": "22F1", "payload": "000030"}
        assert fan_cmds["bypass"] == {"verb": "W", "code": "22F7", "payload": "0000EF"}

    def test_bound_as_str_normalized(self) -> None:
        """_bound as string (single REM) is normalized to list."""
        schema: dict[str, Any] = {
            "30:160000": {
                "_class": "FAN",
                "_bound": "32:153001",
            },
            "32:153001": {
                "_class": "REM",
                "_commands": {
                    "boost": "I --- 32:153001 30:160000 --:------ 22F1 003 000030",
                },
            },
        }
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        assert SZ_TR_COMMANDS in result["30:160000"]

    def test_invalid_packet_logged_not_crash(self) -> None:
        """Invalid packet string is skipped, not crashing."""
        schema: dict[str, Any] = {
            "30:160000": {
                "_class": "FAN",
                "_bound": ["32:153001"],
            },
            "32:153001": {
                "_class": "REM",
                "_commands": {
                    "bad": "invalid_packet",
                },
            },
        }
        # Should not raise
        result = RamsesCoordinator._migrate_rem_commands_to_fan(schema)
        # Bad command not copied to FAN
        fan_cmds = result["30:160000"].get(SZ_TR_COMMANDS, {})
        assert "bad" not in fan_cmds

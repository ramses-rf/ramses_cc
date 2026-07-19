"""Tests for the ramses_cc config flow.

This module contains tests for the configuration wizard (ConfigFlow) and the
options menu (OptionsFlow).
"""

from collections.abc import Callable, Iterator
from datetime import timedelta as td
from importlib.metadata import version
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.usb.models import USBDevice
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.config_flow import (
    CONF_HA_MQTT_PATH,
    CONF_MANUAL_PATH,
    CONF_MQTT_PATH,
    CONF_ZIGBEE_DEVICE,
    RamsesConfigFlow,
    get_usb_ports,
)
from custom_components.ramses_cc.const import (
    CONF_FRESH_START,
    CONF_GATEWAY_TIMEOUT,
    CONF_MESSAGE_EVENTS,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    DEFAULT_HGI_ID,
    DOMAIN,
    SZ_OWNER,
    SZ_TR_CLASS,
    SZ_TR_COMMANDS,
    SZ_TR_OWNER,
)
from ramses_tx.schemas import (
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    SZ_LOG_ALL_MQTT,
    SZ_PACKET_LOG,
    SZ_PORT_NAME,
    SZ_SERIAL_PORT,
)

HOMEASSISTANT_VERSION = version("homeassistant")


def _get_schema_default(schema_key: Any) -> Any:
    """Robustly extract the default value from a voluptuous Marker.

    Home Assistant strips defaults and moves them to 'suggested_value'
    in the description placeholder during form processing.
    """
    desc = getattr(schema_key, "description", {}) or {}
    if "suggested_value" in desc:
        return desc["suggested_value"]
    d = getattr(schema_key, "default", vol.UNDEFINED)
    return d() if callable(d) else d


@pytest.fixture(autouse=True)
def bypass_setup_fixture() -> Iterator[None]:
    """Prevent actual setup of the integration during config flow tests."""
    with (
        patch(
            "custom_components.ramses_cc.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.ramses_cc.async_unload_entry",
            return_value=True,
        ),
    ):
        yield


async def test_full_user_flow(hass: HomeAssistant) -> None:
    """Test the full user configuration flow with manual port selection."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    # Choose Serial Port Step - Select Manual
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_MANUAL_PATH},
        )

    # Configure Serial Port (Manual Text Entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_PORT_NAME: "/dev/ttyUSB0"},
    )

    # Gateway Config
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL: 60, CONF_GATEWAY_TIMEOUT: 15},
    )

    # Schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_ENFORCE_KNOWN_LIST: False},
    )

    # Advanced
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={},
    )

    # Packet Log
    # Testing the new Flight Recorder fields and coercion
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "packet_log_prefix": "test_flight_recorder",
            "buffer_capacity": 50,
            "flush_interval": 2.5,
            "flush_level": "30",  # Simulate UI Dropdown returning a string
        },
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    options = result.get("options")
    assert options is not None

    assert options[SZ_SERIAL_PORT][SZ_PORT_NAME] == "/dev/ttyUSB0"
    assert options[CONF_GATEWAY_TIMEOUT] == 15

    # Assert flight recorder inputs are casted correctly
    assert options[SZ_PACKET_LOG]["packet_log_prefix"] == "test_flight_recorder"
    assert options[SZ_PACKET_LOG]["buffer_capacity"] == 50
    assert options[SZ_PACKET_LOG]["flush_interval"] == 2.5
    assert options[SZ_PACKET_LOG]["flush_level"] == 30  # Should cast to int


async def test_flow_with_discovered_port(hass: HomeAssistant) -> None:
    """Test the flow when selecting a discovered USB port."""
    # Patch must be active during init so schema has the discovered port
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={"/dev/ttyUSB_DISCOVERED": "My Device"},
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

        # Select the discovered port (covers lines 141-142)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: "/dev/ttyUSB_DISCOVERED"},
        )

    # Configure Serial Port step.
    # Since it is a discovered port, _manual_serial_port is False.
    # The form schema will NOT include SZ_PORT_NAME.
    # We submit the form (empty or with other config).
    # Forces code to look up port_name in self.options (covers 292-297).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_SERIAL_PORT: {}},
    )

    assert result.get("step_id") == "config"
    # Ensure the option was preserved
    # We can't easily check internal state, success means it found port name.


async def test_mqtt_flow_edge_cases(hass: HomeAssistant) -> None:
    """Test MQTT flow pre-fill logic and auth string generation."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@127.0.0.1:1883"}},
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "choose_serial_port"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={SZ_PORT_NAME: CONF_MQTT_PATH}
    )

    assert result.get("step_id") == "mqtt_config"

    # Submit with auth to cover line 202-204
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "host": "localhost",
            "port": 1883,
            "username": "user",
            "password": "pass",
        },
    )
    assert result.get("step_id") == "configure_serial_port"


async def test_mqtt_malformed_and_no_auth(hass: HomeAssistant) -> None:
    """Test MQTT flow with malformed URL and no authentication."""
    # 1. Malformed URL (Covers lines 232-233)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://[invalid"}},
    )
    config_entry.add_to_hass(hass)

    # Navigate: Init -> Menu -> Choose Serial Port -> MQTT Broker -> MQTT Config
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "choose_serial_port"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={SZ_PORT_NAME: CONF_MQTT_PATH}
    )

    # Now at 'mqtt_config', fields should be blank/defaults due to parse fail
    assert result.get("step_id") == "mqtt_config"

    # 2. No Auth (Covers line 206)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "host": "192.168.1.5",
            "port": 1883,
            # No credentials provided
        },
    )
    assert result.get("step_id") == "configure_serial_port"


async def test_validation_errors(hass: HomeAssistant) -> None:
    """Test validation error branches for all major steps."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    # 1. Choose Serial Port (Manual)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={SZ_PORT_NAME: CONF_MANUAL_PATH}
    )

    # 2. Serial Port Validation (Line 298-299)
    with patch(
        "custom_components.ramses_cc.config_flow.SCH_SERIAL_PORT_CONFIG",
        side_effect=vol.Invalid("Invalid Config"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: "/dev/ttyUSB0", SZ_SERIAL_PORT: {}},
        )

    errors = result.get("errors")
    assert errors is not None
    assert errors[SZ_SERIAL_PORT] == "invalid_port_config"

    # Move to Gateway config
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={SZ_PORT_NAME: "/dev/ttyUSB0"}
    )

    # 3. Gateway Config Error (Line 367-369)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 60,
            CONF_GATEWAY_TIMEOUT: 10,
            CONF_RAMSES_RF: {"invalid": "key"},
        },
    )

    errors = result.get("errors")
    assert errors is not None
    assert errors[CONF_RAMSES_RF] == "invalid_gateway_config"

    # 4. Schema/Traits Errors (Line 432-434, 440-442, 458)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SCAN_INTERVAL: 60}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_SCHEMA: "not_a_dict", SZ_KNOWN_LIST: "not_a_dict"},
    )

    errors = result.get("errors")
    assert errors is not None
    assert errors[CONF_SCHEMA] == "invalid_schema"
    assert errors[SZ_KNOWN_LIST] == "invalid_traits"

    # 5. Regex Error (Line 519-523)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={SZ_ENFORCE_KNOWN_LIST: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_MESSAGE_EVENTS: "[Unclosed"}
    )

    errors = result.get("errors")
    assert errors is not None
    assert errors[CONF_MESSAGE_EVENTS] == "invalid_regex"


async def test_options_flow_reload_logic(hass: HomeAssistant) -> None:
    """Test reload logic and cache clearing branches."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    try:
        config_entry.mock_state(hass, ConfigEntryState.SETUP_ERROR)
    except AttributeError:
        object.__setattr__(config_entry, "_state", ConfigEntryState.SETUP_ERROR)
        config_entry.__dict__["state"] = ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    # Guarantee config_entry instance is firmly linked so get_options works
    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "config"}
    )
    with patch.object(hass.config_entries, "async_reload") as mock_rl:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_SCAN_INTERVAL: 120, CONF_GATEWAY_TIMEOUT: 10},
        )
        await hass.async_block_till_done()
        assert result.get("type") == FlowResultType.CREATE_ENTRY
        mock_rl.assert_called_once()

    # Test cache clearing and packet filtering (Lines 692-730)
    try:
        config_entry.mock_state(hass, ConfigEntryState.LOADED)
    except AttributeError:
        object.__setattr__(config_entry, "_state", ConfigEntryState.LOADED)
        config_entry.__dict__["state"] = ConfigEntryState.LOADED

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    # Guarantee config_entry instance is firmly linked for cache clear step
    flow_handler2 = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler2, OptionsFlow)
    cast(Any, flow_handler2).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "clear_cache"}
    )

    with (
        patch.object(hass.config_entries, "async_unload") as mock_un,
        patch.object(hass.config_entries, "async_setup") as mock_setup,
        patch.object(hass.config_entries, "async_update_entry") as mock_update,
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_entries_for_config_entry",
            return_value=[],
        ) as mock_dr_entries,
        patch("custom_components.ramses_cc.config_flow.Store") as mock_store,
    ):
        mock_instance = MagicMock()
        mock_store.return_value = mock_instance
        # Configure AsyncMocks for Store methods
        mock_instance.async_load = AsyncMock(
            return_value={
                "client_state": {
                    "schema": {},
                    "packets": {"2024-01-01": "000 ... 0004 ..."},
                }
            }
        )
        mock_instance.async_save = AsyncMock()

        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "clear_schema": True,
                "clear_packets": True,
            },
        )
        mock_un.assert_called_once()
        # Ensure the background task setup is called
        mock_setup.assert_called_once()
        mock_instance.async_save.assert_called_once()
        # Device registry cleanup should have been called for clear_schema
        mock_dr_entries.assert_called_once()
        # CONF_FRESH_START should have been set via async_update_entry
        mock_update.assert_called_once()
        update_kwargs = mock_update.call_args.kwargs
        assert update_kwargs.get("options", {}).get(CONF_FRESH_START) is True


async def test_options_flow_defaults_and_branches(hass: HomeAssistant) -> None:
    """Test various options flow branches including defaults & finish steps."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB_SAVED"}},
    )
    config_entry.add_to_hass(hass)

    # 1. Test Line 162: Stored port not in discovered ports
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={"/dev/ttyUSB_OTHER": "Other"},
    ):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

        flow_handler = hass.config_entries.options._progress[result["flow_id"]]
        assert isinstance(flow_handler, OptionsFlow)
        cast(Any, flow_handler).config_entry = config_entry

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "choose_serial_port"},
        )

        # Verify default falls back to Manual
        schema = result.get("data_schema")
        assert schema is not None
        port_key = next(
            k for k in schema.schema if getattr(k, "schema", k) == SZ_PORT_NAME
        )
        assert _get_schema_default(port_key) == CONF_MANUAL_PATH

    # 2. Test Line 458: async_step_schema finishes in Options Flow
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: False,
        },
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY

    # 3. Test Line 529: async_step_advanced_features finishes in Options Flow
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "advanced_features"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY


async def test_options_flow_serial_port_save(hass: HomeAssistant) -> None:
    """Test options flow serial port triggers save (Line 308)."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB_OLD"}},
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "choose_serial_port"}
    )

    # Select manual to go to configure step
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={SZ_PORT_NAME: CONF_MANUAL_PATH}
    )

    # Enter new port
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={SZ_PORT_NAME: "/dev/ttyUSB_NEW"},
    )

    # Should save (create entry)
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    assert data[SZ_SERIAL_PORT][SZ_PORT_NAME] == "/dev/ttyUSB_NEW"


async def test_options_flow_schema_save_preserves_serial_port(
    hass: HomeAssistant,
) -> None:
    """Ensure schema-step saves do not drop an existing serial_port."""

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCHEMA: {},
            SZ_KNOWN_LIST: {},
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: True,
        },
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    assert data[SZ_SERIAL_PORT][SZ_PORT_NAME] == "mqtt://user:pass@broker:1883"


async def test_options_flow_schema_owner_backfill(hass: HomeAssistant) -> None:
    """Schema step sets root _owner and backfills devices without _owner."""

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {
                "main_tcs": "01:145038",
                "01:145038": {},
                "orphans_heat": ["04:111111"],
                "04:111111": {"_class": "TRV"},
                "04:222222": {"_owner": "neighbour"},
            },
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCHEMA: config_entry.options[CONF_SCHEMA],
            "owner_name": "myhome",
            SZ_KNOWN_LIST: {},
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: False,
        },
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    schema = data[CONF_SCHEMA]
    # Root _owner is set
    assert schema["_owner"] == "myhome"
    # Devices without _owner are backfilled
    assert schema["01:145038"]["_owner"] == "myhome"
    assert schema["04:111111"]["_owner"] == "myhome"
    # Foreign devices keep their existing _owner
    assert schema["04:222222"]["_owner"] == "neighbour"


async def test_options_flow_schema_owner_rename(hass: HomeAssistant) -> None:
    """Changing root _owner renames devices with the old owner name."""

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {
                "_owner": "me",
                "main_tcs": "01:145038",
                "01:145038": {"_owner": "me"},
                "orphans_heat": ["04:111111"],
                "04:111111": {"_owner": "me", "_class": "TRV"},
                "04:222222": {"_owner": "neighbour"},
            },
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCHEMA: config_entry.options[CONF_SCHEMA],
            "owner_name": "myhome",
            SZ_KNOWN_LIST: {},
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: False,
        },
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    schema = data[CONF_SCHEMA]
    # Root _owner is updated
    assert schema["_owner"] == "myhome"
    # Devices with old owner "me" are renamed to "myhome"
    assert schema["01:145038"]["_owner"] == "myhome"
    assert schema["04:111111"]["_owner"] == "myhome"
    # Foreign devices keep their existing _owner
    assert schema["04:222222"]["_owner"] == "neighbour"


async def test_options_flow_schema_owner_rename_with_disabled(
    hass: HomeAssistant,
) -> None:
    """Rename updates _owner on disabled devices (they're still ours)."""

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {
                "_owner": "me",
                "main_tcs": "01:145038",
                "01:145038": {"_owner": "me"},
                "orphans_heat": ["04:111111", "04:333333"],
                "04:111111": {"_owner": "me", "_disabled": True, "_class": "TRV"},
                "04:222222": {"_owner": "neighbour"},
                "04:333333": {"_owner": "neighbour", "_disabled": True},
            },
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCHEMA: config_entry.options[CONF_SCHEMA],
            "owner_name": "myhome",
            SZ_KNOWN_LIST: {},
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: False,
        },
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    schema = data[CONF_SCHEMA]
    assert schema["_owner"] == "myhome"
    # Disabled device with old owner "me" → renamed
    assert schema["04:111111"]["_owner"] == "myhome"
    assert schema["04:111111"]["_disabled"] is True
    # Foreign disabled device → left untouched
    assert schema["04:333333"]["_owner"] == "neighbour"
    assert schema["04:333333"]["_disabled"] is True


async def test_options_flow_schema_owner_rename_with_skipped(
    hass: HomeAssistant,
) -> None:
    """Rename updates _owner on skipped devices (they're still ours)."""

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {
                "_owner": "me",
                "main_tcs": "01:145038",
                "01:145038": {"_owner": "me"},
                "orphans_heat": ["04:111111", "04:333333"],
                "04:111111": {"_owner": "me", "_skipped": True, "_class": "TRV"},
                "04:222222": {"_owner": "neighbour"},
                "04:333333": {"_owner": "neighbour", "_skipped": True},
            },
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCHEMA: config_entry.options[CONF_SCHEMA],
            "owner_name": "myhome",
            SZ_KNOWN_LIST: {},
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: False,
        },
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    schema = data[CONF_SCHEMA]
    assert schema["_owner"] == "myhome"
    # Skipped device with old owner "me" → renamed
    assert schema["04:111111"]["_owner"] == "myhome"
    assert schema["04:111111"]["_skipped"] is True
    # Foreign skipped device → left untouched
    assert schema["04:333333"]["_owner"] == "neighbour"
    assert schema["04:333333"]["_skipped"] is True


async def test_options_flow_enforce_known_list_clears_cache(
    hass: HomeAssistant,
) -> None:
    """Test that enabling enforce_known_list in options clears cached schema/packets."""
    from homeassistant.helpers.storage import Store

    from custom_components.ramses_cc.config_flow import STORAGE_KEY, STORAGE_VERSION
    from custom_components.ramses_cc.const import SZ_CLIENT_STATE, SZ_PACKETS
    from ramses_rf.schemas import SZ_SCHEMA

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    # Mock async_unload to return True
    with patch.object(
        hass.config_entries, "async_unload", new_callable=AsyncMock
    ) as mock_unload:
        mock_unload.return_value = True

        # Pre-populate store with cached client state
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        await store.async_save(
            {
                SZ_CLIENT_STATE: {
                    SZ_SCHEMA: {"01:123456": {}},
                    SZ_PACKETS: {"2026-01-01": "some packet"},
                }
            }
        )
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "schema"}
        )

        # Enable enforce_known_list (was False, now True)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_SCHEMA: {},
                SZ_KNOWN_LIST: {},
                SZ_ENFORCE_KNOWN_LIST: True,
                SZ_LOG_ALL_MQTT: False,
            },
        )

        # Should have unloaded and cleared caches
        assert mock_unload.called

        # Verify cache was cleared
        stored = await store.async_load()
        if stored and SZ_CLIENT_STATE in stored:
            assert SZ_SCHEMA not in stored[SZ_CLIENT_STATE]
            assert SZ_PACKETS not in stored[SZ_CLIENT_STATE]


async def test_choose_serial_port_defaults(hass: HomeAssistant) -> None:
    """Test choose_serial_port defaults to stored port if present."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB_EXISTING"}},
    )
    config_entry.add_to_hass(hass)

    # Discovered ports include the existing one
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={
            "/dev/ttyUSB_EXISTING": "Existing Device",
            "/dev/ttyUSB_OTHER": "Other",
        },
    ):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)

        flow_handler = hass.config_entries.options._progress[result["flow_id"]]
        assert isinstance(flow_handler, OptionsFlow)
        cast(Any, flow_handler).config_entry = config_entry

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "choose_serial_port"},
        )

        # Verify default is the existing port
        schema = result.get("data_schema")
        assert schema is not None
        port_key = next(
            k for k in schema.schema if getattr(k, "schema", k) == SZ_PORT_NAME
        )
        assert _get_schema_default(port_key) == "/dev/ttyUSB_EXISTING"


async def test_import_flow(hass: HomeAssistant) -> None:
    """Test the import flow from configuration.yaml (Lines 630-639)."""
    with patch("custom_components.ramses_cc.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "import"},
            data={
                CONF_SCAN_INTERVAL: td(seconds=60),
                SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
                CONF_RAMSES_RF: {},
                "restore_cache": True,  # Should be popped
            },
        )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    options = result.get("options")
    assert options is not None
    assert options[CONF_SCAN_INTERVAL] == 60
    assert "restore_cache" not in options


async def test_single_instance_allowed(hass: HomeAssistant) -> None:
    """Test that only one instance is allowed (Integration Style)."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result.get("type") == FlowResultType.ABORT
    assert result.get("reason") == "single_instance_allowed"


async def test_single_instance_allowed_direct(hass: HomeAssistant) -> None:
    """Test the single instance check by invoking the method directly.

    This ensures coverage for line 623 is properly recorded by avoiding
    FlowManager overhead.
    """
    # 1. Setup existing entry
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    # 2. Instantiate Flow manually
    flow = RamsesConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_USER}

    # 3. Execute Step
    result = await flow.async_step_user()

    assert result.get("type") == FlowResultType.ABORT
    assert result.get("reason") == "single_instance_allowed"


async def test_configure_serial_port_error_logic(hass: HomeAssistant) -> None:
    """Test the defensive error path in configure_serial_port."""

    # 1. Start flow and pick a port (discovered)
    # Patch needs to be active during init for the port to be valid in schema
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={"/dev/ttyUSB1": "Found Device"},
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: "/dev/ttyUSB1"},
        )

    # 2. Now in configure_serial_port.
    # To trigger the error 'port_name is None', we must manipulate the stored
    # options on the flow handler instance before submitting the next step.
    flow_instance = hass.config_entries.flow._progress[result["flow_id"]]
    assert isinstance(flow_instance, RamsesConfigFlow)
    flow_instance.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = None

    # Enable manual mode so SZ_PORT_NAME appears in the schema
    flow_instance._manual_serial_port = True

    # 3. Submit empty input (triggers lookup in options)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_SERIAL_PORT: {}},
    )

    # Assert that the flow halted at the form step.
    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "configure_serial_port"


@pytest.mark.skipif(HOMEASSISTANT_VERSION < "2026.5.0", reason="requires HA 2026.5.0+")
def test_get_usb_ports_full_new() -> None:
    """Test get_usb_ports with VID/PID present, HA Core 2026.5.0."""
    usb_device = USBDevice(
        device="/dev/ttyUSB0",
        vid="1234",
        pid="5678",
        serial_number="SN123",
        manufacturer="Acme",
        description="Acme Device",
    )
    with (
        patch(
            "homeassistant.components.usb.scan_serial_ports",
            return_value=[usb_device],
        ),
        patch(
            "homeassistant.components.usb.human_readable_device_name",
            return_value="USB Device",
        ),
    ):
        ports = get_usb_ports()
        assert ports == {"/dev/ttyUSB0": "USB Device"}


@pytest.mark.skipif(HOMEASSISTANT_VERSION < "2026.5.0", reason="requires HA 2026.5.0+")
def test_get_usb_ports_logic_edge_case_new() -> None:
    """Test get_usb_ports when VID is missing, HA Core 2026.5.0."""
    from homeassistant.components.usb import (
        SerialDevice,  # pyright: ignore[reportAttributeAccessIssue]
    )

    serial_device = SerialDevice(
        device="/dev/serial/by-id/usb-Acme_Device_123",
        serial_number=None,
        manufacturer=None,
        description=None,
    )
    with (
        patch(
            "homeassistant.components.usb.scan_serial_ports",
            return_value=[serial_device],
        ),
        patch(
            "homeassistant.components.usb.human_readable_device_name",
            return_value="USB Device",
        ),
    ):
        ports = get_usb_ports()
        assert ports == {"/dev/serial/by-id/usb-Acme_Device_123": "USB Device"}


# TODO: remove Q3 2026
@pytest.mark.skipif(
    HOMEASSISTANT_VERSION >= "2026.5.0", reason="requires HA < 2026.5.0"
)
def test_get_usb_ports_full_old() -> None:
    """Test get_usb_ports with VID/PID present (Lines 76-78), older Core."""
    with (
        patch("serial.tools.list_ports.comports") as mock_ports,
        patch("homeassistant.components.usb.usb_device_from_port") as mock_usb_dev,
        patch(
            "homeassistant.components.usb.get_serial_by_id",
            return_value="/dev/ttyUSB0",
        ),
        patch(
            "homeassistant.components.usb.human_readable_device_name",
            return_value="USB Device",
        ),
    ):
        mock_port = MagicMock()
        mock_port.vid = "1234"
        mock_port.pid = "5678"
        mock_port.device = "/dev/ttyUSB0"
        mock_ports.return_value = [mock_port]

        mock_device = MagicMock()
        mock_device.vid = "1234"
        mock_device.pid = "5678"
        mock_usb_dev.return_value = mock_device

        ports = get_usb_ports()
        assert "/dev/ttyUSB0" in ports
        mock_usb_dev.assert_called_once()


# TODO: remove Q3 2026
@pytest.mark.skipif(
    HOMEASSISTANT_VERSION >= "2026.5.0", reason="requires HA < 2026.5.0"
)
def test_get_usb_ports_logic_edge_case_old() -> None:
    """Test get_usb_ports when VID is missing (Lines 161-164), older Core."""
    with (
        patch("serial.tools.list_ports.comports") as mock_ports,
        patch(
            "homeassistant.components.usb.get_serial_by_id",
            return_value="/dev/serial/by-id/usb-Acme_Device_123",
        ),
        patch(
            "homeassistant.components.usb.human_readable_device_name",
            return_value="USB Device",
        ),
    ):
        mock_port = MagicMock()
        mock_port.vid = None  # Forces skip of line 78-81
        mock_port.device = "/dev/ttyUSB0"
        mock_ports.return_value = [mock_port]

        ports = get_usb_ports()
        assert "/dev/serial/by-id/usb-Acme_Device_123" in ports
        assert ports["/dev/serial/by-id/usb-Acme_Device_123"] == "USB Device"


async def test_configure_serial_port_validation_error(
    hass: HomeAssistant,
) -> None:
    """Test invalid serial port config stays on the same step with errors.

    This specifically tests the fix in lines 306-308 of config_flow.py.
    """
    # 1. Start the flow and get to the configure_serial_port step
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_MANUAL_PATH},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "configure_serial_port"

    # 2. Submit an invalid configuration
    invalid_input = {
        SZ_PORT_NAME: "/dev/ttyUSB0",
        SZ_SERIAL_PORT: {"baudrate": "not_an_int"},
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=invalid_input,
    )

    # 3. Assert that we are still on the same step and have an error
    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "configure_serial_port"
    errors = result.get("errors")
    assert errors is not None
    assert SZ_SERIAL_PORT in errors
    assert errors[SZ_SERIAL_PORT] == "invalid_port_config"


async def test_configure_serial_port_missing_port_name(
    hass: HomeAssistant,
) -> None:
    """Test that the flow handles a missing port_name in options correctly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    # 1. Reach the configure_serial_port step
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_MANUAL_PATH},
        )

    # 2. Access the actual flow handler
    flow_handler = hass.config_entries.flow._progress.get(result["flow_id"])
    assert isinstance(flow_handler, RamsesConfigFlow)
    # Corrupt internal options so retrieved port_name is None
    flow_handler.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = None

    # 3. Modify the flow's current step schema to make port_name optional
    current_step = flow_handler.cur_step
    assert current_step is not None
    old_schema = current_step.get("data_schema")
    assert old_schema is not None
    # Create a new schema where everything is optional
    new_schema = vol.Schema({vol.Optional(k): v for k, v in old_schema.schema.items()})
    current_step["data_schema"] = new_schema

    # 4. Submit without port_name to trigger the 'else' branch (line 321)
    with patch(
        "custom_components.ramses_cc.config_flow._LOGGER.error"
    ) as mock_log_error:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_SERIAL_PORT: {}},
        )

    # 5. Assertions
    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "configure_serial_port"
    errors = result.get("errors")
    assert errors is not None
    assert errors[SZ_PORT_NAME] == "port_name_required"
    mock_log_error.assert_called_with("ERROR: port_name is None!")


async def test_options_flow_configure_serial_port(hass: HomeAssistant) -> None:
    """Test the serial port configuration via the options flow."""
    port_path = "/dev/ttyUSB0"
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: port_path}},
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    # 1. Open Menu and Choose Serial Port
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={port_path: "USB Device"},
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "choose_serial_port"},
        )

        # 2. Submit Choice
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: port_path},
        )

    # 3. Submit valid data in configure_serial_port
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={SZ_SERIAL_PORT: {}},
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY


async def test_ha_mqtt_flow(hass: HomeAssistant) -> None:
    """Test selecting Home Assistant MQTT in the user flow."""
    # Mock MQTT entry existing and LOADED to avoid mqtt_missing error
    mock_mqtt_entry = MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.LOADED,
    )
    mock_mqtt_entry.add_to_hass(hass)

    # Patch discover to succeed to avoid "discovery_failed" error
    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.BaseRamsesFlow._discover_mqtt_hgi",
            return_value="18:123456",
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

        # 1. Verify HA MQTT is an option
        assert result.get("step_id") == "choose_serial_port"

        # Select HA MQTT
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

        # Skip configure_serial_port and go to config
        assert result.get("step_id") == "config"

        # Finish flow to ensure options are saved correctly
        test_hgi_id = "18:123456"
        test_topic = "ramses_cc"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_SCAN_INTERVAL: 60,
                CONF_MQTT_HGI_ID: test_hgi_id,
                CONF_MQTT_TOPIC: test_topic,
            },
        )

        assert result.get("step_id") == "schema"
        schema = result.get("data_schema")
        assert schema is not None
        key = next(k for k in schema.schema if k == SZ_KNOWN_LIST)
        suggested = getattr(key, "description", {}).get("suggested_value", "not found")
        assert test_hgi_id in suggested

        # Submit Schema Step
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                SZ_ENFORCE_KNOWN_LIST: False,
                SZ_KNOWN_LIST: suggested,
            },
        )

        # Advanced Features
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={},
        )
        # Packet Log
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={},
        )

        assert result.get("type") == FlowResultType.CREATE_ENTRY
        options = result.get("options")
        assert options is not None
        assert options[CONF_MQTT_USE_HA] is True
        assert options[SZ_SERIAL_PORT][SZ_PORT_NAME] == "mqtt_ha"
        assert options[CONF_MQTT_HGI_ID] == test_hgi_id
        assert options[CONF_MQTT_TOPIC] == test_topic

        known_list = options.get(SZ_KNOWN_LIST, {})
        assert test_hgi_id in known_list
        assert known_list[test_hgi_id]["class"] == "HGI"


async def test_options_flow_ha_mqtt_defaults(hass: HomeAssistant) -> None:
    """Test that HA MQTT is pre selected in options flow if active."""

    # Create config entry with HA MQTT enabled
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_MQTT_USE_HA: True,
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt_ha"},
        },
    )
    config_entry.add_to_hass(hass)

    # Need an MQTT entry in HA so it shows up in list.
    mock_mqtt_entry = MockConfigEntry(
        domain="mqtt", data={}, state=ConfigEntryState.LOADED
    )
    mock_mqtt_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    # Go to choose_serial_port
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "choose_serial_port"},
        )

    # Verify default is HA MQTT
    schema = result.get("data_schema")
    assert schema is not None
    port_key = next(k for k in schema.schema if getattr(k, "schema", k) == SZ_PORT_NAME)
    assert _get_schema_default(port_key) == CONF_HA_MQTT_PATH

    # Select it again
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    assert data[CONF_MQTT_USE_HA] is True


async def test_ha_mqtt_discovery_failure(hass: HomeAssistant) -> None:
    """Test HA MQTT discovery failure triggers error and default value."""
    # Ensure MQTT integration exists
    mock_mqtt_entry = MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.LOADED,
    )
    mock_mqtt_entry.add_to_hass(hass)

    # Initialize the flow
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    # Select HA MQTT, but force discovery to fail
    with patch(
        "custom_components.ramses_cc.config_flow.BaseRamsesFlow._discover_mqtt_hgi",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    # Verify we are at the 'config' step
    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "config"

    # Verify the discovery_failed error is present
    errors = result.get("errors")
    assert errors is not None
    assert errors["base"] == "discovery_failed"

    # Verify the correct placeholder is set
    placeholders = result.get("description_placeholders")
    assert placeholders is not None
    assert placeholders["default_id"] == DEFAULT_HGI_ID


async def test_ha_mqtt_discovery_success(hass: HomeAssistant) -> None:
    """Test successful MQTT discovery."""
    MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.LOADED,
    ).add_to_hass(hass)

    msg = MagicMock()
    msg.topic = "ramses_cc/18:123456/status"
    msg.payload = "payload"

    async def mock_subscribe(
        hass: HomeAssistant,
        topic: str,
        msg_callback: Callable[[Any], None],
        *args: Any,
        **kwargs: Any,
    ) -> MagicMock:
        bad_msg = MagicMock()
        bad_msg.topic = None
        msg_callback(bad_msg)
        msg_callback(msg)
        msg_callback(msg)
        return MagicMock()

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.mqtt.async_subscribe",
            side_effect=mock_subscribe,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    assert result.get("step_id") == "config"
    errors = result.get("errors") or {}
    assert "base" not in errors

    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert isinstance(flow, RamsesConfigFlow)
    assert flow.options[CONF_MQTT_HGI_ID] == "18:123456"


async def test_ha_mqtt_missing_integration(hass: HomeAssistant) -> None:
    """Test selecting HA MQTT when MQTT integration is not set up."""
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "choose_serial_port"
    errors = result.get("errors")
    assert errors is not None
    assert errors["base"] == "mqtt_missing"


async def test_ha_mqtt_discovery_exception(hass: HomeAssistant) -> None:
    """Test exception handling during MQTT discovery subscription."""
    MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.LOADED,
    ).add_to_hass(hass)

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.mqtt.async_subscribe",
            side_effect=Exception("MQTT Error"),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    assert result.get("step_id") == "config"
    errors = result.get("errors")
    assert errors is not None
    assert errors["base"] == "discovery_failed"


async def test_ha_mqtt_discovery_timeout_logic(hass: HomeAssistant) -> None:
    """Test timeout logic in MQTT discovery."""
    MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.LOADED,
    ).add_to_hass(hass)

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.mqtt.async_subscribe",
            return_value=MagicMock(),
        ),
        patch("asyncio.wait_for", side_effect=TimeoutError),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    assert result.get("step_id") == "config"
    errors = result.get("errors")
    assert errors is not None
    assert errors["base"] == "discovery_failed"


async def test_ha_mqtt_not_loaded_error(hass: HomeAssistant) -> None:
    """Test error when MQTT integration exists but is not loaded."""
    MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.NOT_LOADED,
    ).add_to_hass(hass)

    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "choose_serial_port"
    errors = result.get("errors")
    assert errors is not None
    assert errors["base"] == "mqtt_missing"


# ---------------------------------------------------------------------------
# Zigbee device selection tests
# ---------------------------------------------------------------------------


def _make_zigbee_device(
    device_id: str,
    name: str = "RAMSES ESP32-C6",
    ieee: str | None = "00:11:22:33:44:55:66:77",
) -> MagicMock:
    """Return a MagicMock that looks like a ZHA DeviceEntry."""
    dev = MagicMock()
    dev.model = "ramses_esp32c6"
    dev.id = device_id
    dev.name = name
    dev.name_by_user = None
    if ieee:
        dev.identifiers = {("zha", ieee)}
    else:
        dev.identifiers = {("zha", "not_an_ieee_address")}
    return dev


async def test_zigbee_no_devices_found(hass: HomeAssistant) -> None:
    """Test zigbee_device step shows error when no devices exist."""
    mock_registry = MagicMock()
    mock_registry.devices = {}

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_get",
            return_value=mock_registry,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "zigbee_device"
    errors = result.get("errors")
    assert errors is not None
    assert errors["base"] == "no_ramses_device_found"


async def test_zigbee_single_device_auto_configure(
    hass: HomeAssistant,
) -> None:
    """Test zigbee_device auto-configures when exactly one device is found."""
    device = _make_zigbee_device("dev1", ieee="00:11:22:33:44:55:66:77")
    mock_registry = MagicMock()
    mock_registry.devices = {"dev1": device}

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_get",
            return_value=mock_registry,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "configure_serial_port"


async def test_zigbee_single_device_no_ieee(hass: HomeAssistant) -> None:
    """Test zigbee_device step shows error when single device has no IEEE."""
    device = _make_zigbee_device("dev1", ieee=None)
    mock_registry = MagicMock()
    mock_registry.devices = {"dev1": device}

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_get",
            return_value=mock_registry,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "zigbee_device"
    errors = result.get("errors")
    assert errors is not None
    assert errors["base"] == "no_ieee_identifier"


async def test_zigbee_multiple_devices_shows_selector(
    hass: HomeAssistant,
) -> None:
    """Test zigbee_device step shows SelectSelector with multiple devices."""
    devices = {
        "dev1": _make_zigbee_device("dev1", name="Device One"),
        "dev2": _make_zigbee_device("dev2", name="Device Two"),
    }
    mock_registry = MagicMock()
    mock_registry.devices = devices

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_get",
            return_value=mock_registry,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "zigbee_device"
    assert result.get("errors") in ({}, None)


async def test_zigbee_user_selects_device_from_selector(
    hass: HomeAssistant,
) -> None:
    """Test user picks a device from the multi-device SelectSelector."""
    dev1 = _make_zigbee_device(
        "dev1", name="Device One", ieee="00:11:22:33:44:55:66:77"
    )
    dev2 = _make_zigbee_device(
        "dev2", name="Device Two", ieee="aa:bb:cc:dd:ee:ff:00:11"
    )
    mock_registry = MagicMock()
    mock_registry.devices = {"dev1": dev1, "dev2": dev2}
    mock_registry.async_get.return_value = dev1

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_get",
            return_value=mock_registry,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )
        assert result.get("step_id") == "zigbee_device"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"device": "dev1"},
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "configure_serial_port"


async def test_zigbee_port_picker_shows_zigbee_option(
    hass: HomeAssistant,
) -> None:
    """Test that CONF_ZIGBEE_DEVICE is a valid port choice in the picker."""
    mock_registry = MagicMock()
    mock_registry.devices = {}

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_get",
            return_value=mock_registry,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result.get("type") == FlowResultType.FORM
        assert result.get("step_id") == "choose_serial_port"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )

    assert result.get("step_id") == "zigbee_device"


async def test_zigbee_single_device_label_in_port_picker(
    hass: HomeAssistant,
) -> None:
    """Test port picker shows device-specific label for single device."""
    device = _make_zigbee_device("dev1", name="RAMSES esp32c6 My Sensor")
    mock_registry = MagicMock()
    mock_registry.devices = {"dev1": device}

    with (
        patch(
            "custom_components.ramses_cc.config_flow.async_get_usb_ports",
            return_value={},
        ),
        patch(
            "custom_components.ramses_cc.config_flow.dr.async_get",
            return_value=mock_registry,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "choose_serial_port"

    schema = result.get("data_schema")
    assert schema is not None
    port_name_key = next(
        (k for k in schema.schema if getattr(k, "schema", None) == SZ_PORT_NAME),
        None,
    )
    assert port_name_key is not None

    selector_obj = schema.schema[port_name_key]
    config = getattr(selector_obj, "config", {})
    options: list[dict[str, Any]] = (
        config.get("options", []) if isinstance(config, dict) else []
    )
    opts_by_value = {opt.get("value"): opt.get("label", "") for opt in options}

    zigbee_label = opts_by_value.get(CONF_ZIGBEE_DEVICE, "")
    assert "Zigbee device:" in zigbee_label


# ───────────────────────────────────────────────────────────────────────
# Options flow: review_discovered step (passive device scan)
# ───────────────────────────────────────────────────────────────────────


async def test_review_discovered_no_coordinator(hass: HomeAssistant) -> None:
    """Test review_discovered step when no coordinator with discovery_manager is found."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "review_discovered"
    placeholders = result.get("description_placeholders", {})
    assert "not enabled" in placeholders.get("message", "")


async def test_review_discovered_no_manager(hass: HomeAssistant) -> None:
    """Test review_discovered step when coordinator has no discovery_manager."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    # Put a coordinator without discovery_manager in hass.data
    mock_coord = MagicMock()
    mock_coord.discovery_manager = None
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM
    placeholders = result.get("description_placeholders", {})
    assert "not enabled" in placeholders.get("message", "")


async def test_review_discovered_no_new_devices(hass: HomeAssistant) -> None:
    """Test review_discovered step when there are no new devices."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    # Coordinator with discovery_manager but no new devices
    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = []
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM
    placeholders = result.get("description_placeholders", {})
    assert "No new devices" in placeholders.get("message", "")


async def test_review_discovered_shows_form_with_devices(hass: HomeAssistant) -> None:
    """Test review_discovered step shows form with device selectors."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    # Create a mock discovered device entry
    mock_entry = MagicMock()
    mock_entry.device.device_id = "04:056053"
    mock_entry.device.likely_type = "TRV"
    mock_entry.device.confidence = "high"
    mock_entry.device.rssi = -72.0
    mock_entry.device.codes_seen = ["3150", "10e0"]
    mock_entry.device.bound_to = "01:145038"
    mock_entry.device.zone_idx = "02"
    mock_entry.device.is_battery = True
    mock_entry.device.src_count = 3
    mock_entry.device.dst_count = 1

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "review_discovered"
    # Verify the form has device selector fields
    data_schema = result.get("data_schema")
    assert data_schema is not None
    schema_dict = data_schema.schema
    field_names = {
        str(k) if hasattr(k, "schema") else k for k, _ in schema_dict.items()
    }
    assert "device_04:056053" in field_names
    # Verify summary table in placeholders
    placeholders = result.get("description_placeholders", {})
    assert "04:056053" in placeholders.get("message", "")


async def test_review_discovered_accept_device(hass: HomeAssistant) -> None:
    """Test review_discovered step accepting a device."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    # Create a mock discovered device entry
    mock_entry = MagicMock()
    mock_entry.device.device_id = "04:056053"
    mock_entry.device.likely_type = "TRV"
    mock_entry.device.confidence = "high"
    mock_entry.device.rssi = -72.0
    mock_entry.device.codes_seen = ["3150"]
    mock_entry.device.bound_to = "01:145038"
    mock_entry.device.zone_idx = "02"
    mock_entry.device.is_battery = True
    mock_entry.device.src_count = 3
    mock_entry.device.dst_count = 0

    # Mock accept_device to return an entry with a schema_entry.
    # Include a root-level entry for the device so the config flow can
    # set traits (_owner, etc.) on it — generate_schema_entry always
    # ensures one via _merge().
    accepted_entry = MagicMock()
    accepted_entry.metadata.schema_entry = {
        "04:056053": {},
        "01:145038": {"zones": {"02": {"sensor": "04:056053"}}},
    }
    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry]
    mock_coord.discovery_manager.accept_device.return_value = accepted_entry
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    # Navigate to review_discovered
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM

    # Submit form with accept action and a per-device owner
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "device_04:056053": "accept",
            "owner_04:056053": "henk",
        },
    )
    # Should create entry (save)
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    mock_coord.discovery_manager.accept_device.assert_called_once_with(
        "04:056053", owner="henk", ctl_id=None
    )
    # Per-device owner must be written to the schema entry, not overridden
    # by the root owner (regression: owner was silently replaced with
    # root_owner "me" — see issue with 37:154519 accept + owner: not-me).
    saved_schema = config_entry.options.get(CONF_SCHEMA, {})
    assert saved_schema.get("04:056053", {}).get(SZ_TR_OWNER) == "henk"


async def test_review_discovered_decline_device(hass: HomeAssistant) -> None:
    """Test review_discovered step declining a device."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry = MagicMock()
    mock_entry.device.device_id = "04:056053"
    mock_entry.device.likely_type = "TRV"
    mock_entry.device.confidence = "high"
    mock_entry.device.rssi = -72.0
    mock_entry.device.codes_seen = ["3150"]
    mock_entry.device.bound_to = "01:145038"
    mock_entry.device.zone_idx = "02"
    mock_entry.device.is_battery = True
    mock_entry.device.src_count = 3
    mock_entry.device.dst_count = 0

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    # Submit form with decline action
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"device_04:056053": "decline"},
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    mock_coord.discovery_manager.discard_device.assert_called_once_with("04:056053")


async def test_review_discovered_skip_device(hass: HomeAssistant) -> None:
    """Test review_discovered step skipping a device (no change)."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry = MagicMock()
    mock_entry.device.device_id = "04:056053"
    mock_entry.device.likely_type = "TRV"
    mock_entry.device.confidence = "high"
    mock_entry.device.rssi = -72.0
    mock_entry.device.codes_seen = ["3150"]
    mock_entry.device.bound_to = "01:145038"
    mock_entry.device.zone_idx = "02"
    mock_entry.device.is_battery = True
    mock_entry.device.src_count = 3
    mock_entry.device.dst_count = 0

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    # Submit form with skip action (default)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"device_04:056053": "skip"},
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    # Neither accept nor discard should have been called
    mock_coord.discovery_manager.accept_device.assert_not_called()
    mock_coord.discovery_manager.discard_device.assert_not_called()


async def test_review_discovered_missing_class_add_class(hass: HomeAssistant) -> None:
    """Test review_discovered step adding _class to a device that lacks it.

    A device already in the schema (accepted) but without a _class trait
    should appear in the review form when check_missing_class flags it.
    Choosing "add_class" sets _class to the discovery suggestion.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {
                "37:154519": {"remotes": []},
            },
        },
    )
    config_entry.add_to_hass(hass)

    # Mock a missing_class device entry (no new devices, no mismatches)
    mock_entry = MagicMock()
    mock_entry.device.device_id = "37:154519"
    mock_entry.device.likely_type = "FAN"
    mock_entry.device.confidence = "medium"
    mock_entry.metadata.missing_class = "discovery=FAN"
    mock_entry.metadata.class_mismatch = None

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = []  # no NEW devices
    mock_coord.discovery_manager.get_mismatched_devices.return_value = []
    mock_coord.discovery_manager.get_missing_class_devices.return_value = [mock_entry]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    # Navigate to review_discovered
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM
    # Verify the form has the missing_class field
    data_schema = result.get("data_schema")
    assert data_schema is not None
    schema_dict = data_schema.schema
    field_names = {
        str(k) if hasattr(k, "schema") else k for k, _ in schema_dict.items()
    }
    assert "missing_class_37:154519" in field_names
    # Verify summary mentions missing _class
    placeholders = result.get("description_placeholders", {})
    assert "missing _class" in placeholders.get("message", "")

    # Submit form with add_class action
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"missing_class_37:154519": "add_class"},
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    # Verify _class was set in the saved schema
    saved_schema = config_entry.options.get(CONF_SCHEMA, {})
    assert saved_schema.get("37:154519", {}).get(SZ_TR_CLASS) == "FAN"


async def test_review_discovered_missing_class_per_device_owner(
    hass: HomeAssistant,
) -> None:
    """Test per-device owner overrides root owner for missing_class devices.

    The user sets a per-device owner of "not-me" on a missing_class device
    while keeping the root owner as "me".  The device should get
    _owner=not-me, not _owner=me.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {
                SZ_OWNER: "me",
                "37:154519": {"remotes": []},
            },
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry = MagicMock()
    mock_entry.device.device_id = "37:154519"
    mock_entry.device.likely_type = "FAN"
    mock_entry.device.confidence = "medium"
    mock_entry.metadata.missing_class = "discovery=FAN"
    mock_entry.metadata.class_mismatch = None

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = []
    mock_coord.discovery_manager.get_mismatched_devices.return_value = []
    mock_coord.discovery_manager.get_missing_class_devices.return_value = [mock_entry]
    mock_coord.discovery_manager._metadata = {
        "37:154519": mock_entry.metadata,
    }
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM
    # Verify the form has a per-device owner field for the missing_class device
    data_schema = result.get("data_schema")
    schema_dict = data_schema.schema
    field_names = {
        str(k) if hasattr(k, "schema") else k for k, _ in schema_dict.items()
    }
    assert "owner_37:154519" in field_names

    # Submit with add_class + per-device owner "not-me"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "missing_class_37:154519": "add_class",
            "owner_37:154519": "not-me",
        },
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    saved_schema = config_entry.options.get(CONF_SCHEMA, {})
    # _class should be set
    assert saved_schema.get("37:154519", {}).get(SZ_TR_CLASS) == "FAN"
    # Per-device owner should be "not-me", NOT root owner "me"
    assert saved_schema.get("37:154519", {}).get(SZ_TR_OWNER) == "not-me"
    # Root owner should still be "me"
    assert saved_schema.get(SZ_OWNER) == "me"


async def test_review_discovered_missing_class_skip(hass: HomeAssistant) -> None:
    """Test review_discovered step skipping a missing_class device.

    Skipping should not modify the schema (no _class added) but should
    clear the missing_class flag so the notification doesn't re-fire
    immediately.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {
                "37:154519": {"remotes": []},
            },
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry = MagicMock()
    mock_entry.device.device_id = "37:154519"
    mock_entry.device.likely_type = "FAN"
    mock_entry.device.confidence = "medium"
    mock_entry.metadata.missing_class = "discovery=FAN"
    mock_entry.metadata.class_mismatch = None

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = []
    mock_coord.discovery_manager.get_mismatched_devices.return_value = []
    mock_coord.discovery_manager.get_missing_class_devices.return_value = [mock_entry]
    # _metadata is accessed directly to clear the missing_class flag
    mock_coord.discovery_manager._metadata = {
        "37:154519": mock_entry.metadata,
    }
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    # Submit form with skip action
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"missing_class_37:154519": "skip"},
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    # _class should NOT be set in the schema
    saved_schema = config_entry.options.get(CONF_SCHEMA, {})
    assert SZ_TR_CLASS not in saved_schema.get("37:154519", {})
    # The missing_class flag should be cleared
    assert mock_entry.metadata.missing_class is None


async def test_review_discovered_bulk_accept_all(hass: HomeAssistant) -> None:
    """Test bulk_action=accept accepts all devices."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry1 = MagicMock()
    mock_entry1.device.device_id = "04:056053"
    mock_entry1.device.likely_type = "TRV"
    mock_entry1.device.confidence = "high"
    mock_entry1.device.rssi = -72.0
    mock_entry1.device.codes_seen = ["3150"]
    mock_entry1.device.bound_to = "01:145038"
    mock_entry1.device.zone_idx = "02"
    mock_entry1.device.is_battery = True
    mock_entry1.device.src_count = 3
    mock_entry1.device.dst_count = 0

    mock_entry2 = MagicMock()
    mock_entry2.device.device_id = "04:056054"
    mock_entry2.device.likely_type = "TRV"
    mock_entry2.device.confidence = "high"
    mock_entry2.device.rssi = -70.0
    mock_entry2.device.codes_seen = ["3150"]
    mock_entry2.device.bound_to = "01:145038"
    mock_entry2.device.zone_idx = "03"
    mock_entry2.device.is_battery = True
    mock_entry2.device.src_count = 2
    mock_entry2.device.dst_count = 0

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry1, mock_entry2]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    cast(Any, flow_handler).config_entry = config_entry
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    # Submit with bulk_action=accept, per-device left as skip
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "bulk_action": "accept",
            "device_04:056053": "skip",
            "device_04:056054": "skip",
        },
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert mock_coord.discovery_manager.accept_device.call_count == 2


async def test_review_discovered_bulk_decline_all(hass: HomeAssistant) -> None:
    """Test bulk_action=decline declines all devices."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry1 = MagicMock()
    mock_entry1.device.device_id = "04:056053"
    mock_entry1.device.likely_type = "TRV"
    mock_entry1.device.confidence = "high"
    mock_entry1.device.rssi = -72.0
    mock_entry1.device.codes_seen = ["3150"]
    mock_entry1.device.bound_to = None
    mock_entry1.device.zone_idx = None
    mock_entry1.device.is_battery = True
    mock_entry1.device.src_count = 3
    mock_entry1.device.dst_count = 0

    mock_entry2 = MagicMock()
    mock_entry2.device.device_id = "04:056054"
    mock_entry2.device.likely_type = "TRV"
    mock_entry2.device.confidence = "high"
    mock_entry2.device.rssi = -70.0
    mock_entry2.device.codes_seen = ["3150"]
    mock_entry2.device.bound_to = None
    mock_entry2.device.zone_idx = None
    mock_entry2.device.is_battery = True
    mock_entry2.device.src_count = 2
    mock_entry2.device.dst_count = 0

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry1, mock_entry2]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    cast(Any, flow_handler).config_entry = config_entry
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "bulk_action": "decline",
            "device_04:056053": "skip",
            "device_04:056054": "skip",
        },
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert mock_coord.discovery_manager.discard_device.call_count == 2


async def test_review_discovered_per_device_overrides_bulk(hass: HomeAssistant) -> None:
    """Test per-device choice overrides bulk action."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry1 = MagicMock()
    mock_entry1.device.device_id = "04:056053"
    mock_entry1.device.likely_type = "TRV"
    mock_entry1.device.confidence = "high"
    mock_entry1.device.rssi = -72.0
    mock_entry1.device.codes_seen = ["3150"]
    mock_entry1.device.bound_to = "01:145038"
    mock_entry1.device.zone_idx = "02"
    mock_entry1.device.is_battery = True
    mock_entry1.device.src_count = 3
    mock_entry1.device.dst_count = 0

    mock_entry2 = MagicMock()
    mock_entry2.device.device_id = "04:056054"
    mock_entry2.device.likely_type = "TRV"
    mock_entry2.device.confidence = "high"
    mock_entry2.device.rssi = -70.0
    mock_entry2.device.codes_seen = ["3150"]
    mock_entry2.device.bound_to = None
    mock_entry2.device.zone_idx = None
    mock_entry2.device.is_battery = True
    mock_entry2.device.src_count = 2
    mock_entry2.device.dst_count = 0

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry1, mock_entry2]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    cast(Any, flow_handler).config_entry = config_entry
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    # bulk=accept, but device 2 is explicitly declined
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "bulk_action": "accept",
            "device_04:056053": "skip",  # uses bulk → accept
            "device_04:056054": "decline",  # overrides bulk → decline
        },
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    mock_coord.discovery_manager.accept_device.assert_called_once_with(
        "04:056053", owner=None, ctl_id=None
    )
    mock_coord.discovery_manager.discard_device.assert_called_once_with("04:056054")


async def test_review_discovered_bulk_none_no_action(hass: HomeAssistant) -> None:
    """Test bulk_action=none leaves all devices as skip (no action taken)."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry1 = MagicMock()
    mock_entry1.device.device_id = "04:056053"
    mock_entry1.device.likely_type = "TRV"
    mock_entry1.device.confidence = "high"
    mock_entry1.device.rssi = -72.0
    mock_entry1.device.codes_seen = ["3150"]
    mock_entry1.device.bound_to = None
    mock_entry1.device.zone_idx = None
    mock_entry1.device.is_battery = True
    mock_entry1.device.src_count = 3
    mock_entry1.device.dst_count = 0

    mock_entry2 = MagicMock()
    mock_entry2.device.device_id = "04:056054"
    mock_entry2.device.likely_type = "TRV"
    mock_entry2.device.confidence = "high"
    mock_entry2.device.rssi = -70.0
    mock_entry2.device.codes_seen = ["3150"]
    mock_entry2.device.bound_to = None
    mock_entry2.device.zone_idx = None
    mock_entry2.device.is_battery = True
    mock_entry2.device.src_count = 2
    mock_entry2.device.dst_count = 0

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry1, mock_entry2]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    cast(Any, flow_handler).config_entry = config_entry
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    # bulk=none, per-device left as skip → no action taken
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "bulk_action": "none",
            "device_04:056053": "skip",
            "device_04:056054": "skip",
        },
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    mock_coord.discovery_manager.accept_device.assert_not_called()
    mock_coord.discovery_manager.discard_device.assert_not_called()


async def test_review_discovered_bulk_none_per_device_still_works(
    hass: HomeAssistant,
) -> None:
    """Test bulk_action=none still allows per-device accept/decline."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    mock_entry1 = MagicMock()
    mock_entry1.device.device_id = "04:056053"
    mock_entry1.device.likely_type = "TRV"
    mock_entry1.device.confidence = "high"
    mock_entry1.device.rssi = -72.0
    mock_entry1.device.codes_seen = ["3150"]
    mock_entry1.device.bound_to = "01:145038"
    mock_entry1.device.zone_idx = "02"
    mock_entry1.device.is_battery = True
    mock_entry1.device.src_count = 3
    mock_entry1.device.dst_count = 0

    mock_entry2 = MagicMock()
    mock_entry2.device.device_id = "04:056054"
    mock_entry2.device.likely_type = "TRV"
    mock_entry2.device.confidence = "high"
    mock_entry2.device.rssi = -70.0
    mock_entry2.device.codes_seen = ["3150"]
    mock_entry2.device.bound_to = None
    mock_entry2.device.zone_idx = None
    mock_entry2.device.is_battery = True
    mock_entry2.device.src_count = 2
    mock_entry2.device.dst_count = 0

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry1, mock_entry2]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    cast(Any, flow_handler).config_entry = config_entry
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )

    # bulk=none, but per-device choices are explicit
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "bulk_action": "none",
            "device_04:056053": "accept",
            "device_04:056054": "decline",
        },
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    mock_coord.discovery_manager.accept_device.assert_called_once_with(
        "04:056053", owner=None, ctl_id=None
    )
    mock_coord.discovery_manager.discard_device.assert_called_once_with("04:056054")


# ───────────────────────────────────────────────────────────────────────
# Options flow: clear_cache step with clear_discovery option
# ───────────────────────────────────────────────────────────────────────


async def test_clear_cache_with_clear_discovery(hass: HomeAssistant) -> None:
    """Test clear_cache step with the clear_discovery option."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    try:
        config_entry.mock_state(hass, ConfigEntryState.LOADED)
    except AttributeError:
        object.__setattr__(config_entry, "_state", ConfigEntryState.LOADED)
        config_entry.__dict__["state"] = ConfigEntryState.LOADED

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "clear_cache"}
    )

    with (
        patch.object(hass.config_entries, "async_unload") as mock_un,
        patch.object(hass.config_entries, "async_setup") as mock_setup,
        patch("custom_components.ramses_cc.config_flow.Store") as mock_store,
    ):
        mock_instance = MagicMock()
        mock_store.return_value = mock_instance
        mock_instance.async_load = AsyncMock(
            return_value={
                "client_state": {
                    "schema": {},
                    "packets": {},
                },
                "discovery": {"devices": {"04:056053": {"status": "new"}}},
            }
        )
        mock_instance.async_save = AsyncMock()

        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "clear_schema": False,
                "clear_packets": False,
                "clear_discovery": True,
            },
        )
        mock_un.assert_called_once()
        mock_setup.assert_called_once()
        mock_instance.async_save.assert_called_once()
        # Verify discovery state was removed from saved data
        saved_data = mock_instance.async_save.call_args[0][0]
        assert "discovery" not in saved_data


async def test_review_discovered_many_codes_and_no_rssi(hass: HomeAssistant) -> None:
    """Test review_discovered summary table with >4 codes and None rssi."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    # Device with >4 codes and no rssi to cover lines 1414-1415
    mock_entry = MagicMock()
    mock_entry.device.device_id = "04:056053"
    mock_entry.device.likely_type = "TRV"
    mock_entry.device.confidence = "high"
    mock_entry.device.rssi = None  # covers the "—" branch
    mock_entry.device.codes_seen = ["3150", "10e0", "0008", "2309", "1f09", "30c9"]
    mock_entry.device.bound_to = None  # covers the "—" branch
    mock_entry.device.zone_idx = None  # covers the "—" branch
    mock_entry.device.is_battery = False
    mock_entry.device.src_count = 5
    mock_entry.device.dst_count = 2

    mock_coord = MagicMock()
    mock_coord.discovery_manager = MagicMock()
    mock_coord.discovery_manager.get_devices.return_value = [mock_entry]
    hass.data[DOMAIN] = {config_entry.entry_id: mock_coord}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    flow_handler = hass.config_entries.options._progress[result["flow_id"]]
    assert isinstance(flow_handler, OptionsFlow)
    cast(Any, flow_handler).config_entry = config_entry

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "review_discovered"}
    )
    assert result.get("type") == FlowResultType.FORM
    placeholders = result.get("description_placeholders", {})
    # Verify the summary includes the "(+2)" for extra codes
    assert "+2" in placeholders.get("message", "")
    # Verify the em-dash for None rssi/bound_to/zone_idx
    assert "—" in placeholders.get("message", "")


# ---------------------------------------------------------------------------
# Phase 3a: _commands in config flow schema step
# ---------------------------------------------------------------------------


async def test_options_flow_schema_preserves_commands(hass: HomeAssistant) -> None:
    """Schema step preserves _commands when user saves the schema.

    _commands is a _ prefixed key that should survive the config flow
    round-trip: it's stripped for validation but saved in the original schema.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {
                "37:153001": {
                    "_class": "REM",
                    SZ_TR_COMMANDS: {
                        "boost": "I --- 37:153001 30:160000 --:------ 22F1 003 000030",
                    },
                },
                "30:160000": {
                    "_class": "FAN",
                    "_bound": "37:153001",
                },
            },
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )

    # Submit the schema step with the same schema (including _commands)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCHEMA: {
                "37:153001": {
                    "_class": "REM",
                    SZ_TR_COMMANDS: {
                        "boost": "I --- 37:153001 30:160000 --:------ 22F1 003 000030",
                        "speed_1": "I --- 37:153001 30:160000 --:------ 22F1 003 000031",
                    },
                },
                "30:160000": {
                    "_class": "FAN",
                    "_bound": "37:153001",
                },
            },
            SZ_KNOWN_LIST: {},
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: True,
        },
    )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    schema = data[CONF_SCHEMA]

    # _commands should be preserved on the REM entry
    rem_entry = schema.get("37:153001", {})
    assert isinstance(rem_entry, dict)
    assert SZ_TR_COMMANDS in rem_entry
    commands = rem_entry[SZ_TR_COMMANDS]
    assert "boost" in commands
    assert "speed_1" in commands
    assert commands["boost"] == "I --- 37:153001 30:160000 --:------ 22F1 003 000030"


async def test_options_flow_schema_strips_commands_for_validation(
    hass: HomeAssistant,
) -> None:
    """Schema step strips _commands before ramses_rf validation.

    ramses_rf's SCH_GLOBAL_SCHEMAS rejects _ prefixed keys. The config flow
    strips them via strip_traits_for_validation, validates the clean schema,
    then saves the original (with _commands) to options.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "mqtt://user:pass@broker:1883"},
            CONF_RAMSES_RF: {SZ_ENFORCE_KNOWN_LIST: False},
            SZ_KNOWN_LIST: {},
            CONF_SCHEMA: {},
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "schema"}
    )

    # Submit schema with _commands — should NOT raise invalid_schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCHEMA: {
                "37:153001": {
                    "_class": "REM",
                    SZ_TR_COMMANDS: {
                        "boost": "I --- 37:153001 30:160000 --:------ 22F1 003 000030"
                    },
                },
            },
            SZ_KNOWN_LIST: {},
            SZ_ENFORCE_KNOWN_LIST: False,
            SZ_LOG_ALL_MQTT: True,
        },
    )

    # Should succeed (not return errors with invalid_schema)
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None
    assert SZ_TR_COMMANDS in data[CONF_SCHEMA].get("37:153001", {})

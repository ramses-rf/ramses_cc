"""Tests for the ramses_cc config flow.

This module contains tests for the configuration wizard (ConfigFlow) and the
options menu (OptionsFlow).
"""

from collections.abc import Callable, Iterator
from datetime import timedelta as td
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
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
    CONF_GATEWAY_TIMEOUT,
    CONF_MESSAGE_EVENTS,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    DEFAULT_HGI_ID,
    DOMAIN,
)
from ramses_tx.schemas import (
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    SZ_LOG_ALL_MQTT,
    SZ_PACKET_LOG,
    SZ_PORT_NAME,
    SZ_SERIAL_PORT,
)


@pytest.fixture(autouse=True)
def bypass_setup_fixture() -> Iterator[None]:
    """Prevent actual setup of the integration during config flow tests."""
    with (
        patch("custom_components.ramses_cc.async_setup_entry", return_value=True),
        patch("custom_components.ramses_cc.async_unload_entry", return_value=True),
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

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][SZ_SERIAL_PORT][SZ_PORT_NAME] == "/dev/ttyUSB0"
    assert result["options"][CONF_GATEWAY_TIMEOUT] == 15

    # Assert flight recorder inputs are casted correctly
    assert (
        result["options"][SZ_PACKET_LOG]["packet_log_prefix"] == "test_flight_recorder"
    )
    assert result["options"][SZ_PACKET_LOG]["buffer_capacity"] == 50
    assert result["options"][SZ_PACKET_LOG]["flush_interval"] == 2.5
    assert result["options"][SZ_PACKET_LOG]["flush_level"] == 30  # Should cast to int


async def test_flow_with_discovered_port(hass: HomeAssistant) -> None:
    """Test the flow when selecting a discovered USB port."""

    # Patch must be active during init so the schema is generated with the discovered port
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
    # This forces the code to look up port_name in self.options (covers lines 292-297).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_SERIAL_PORT: {}},
    )

    assert result["step_id"] == "config"
    # Ensure the option was preserved
    # We can't easily check internal state, but success means it found the port name.


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

    assert result["step_id"] == "mqtt_config"

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
    assert result["step_id"] == "configure_serial_port"


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

    # Now we are at 'mqtt_config', fields should be blank/defaults because parsing failed
    assert result["step_id"] == "mqtt_config"

    # 2. No Auth (Covers line 206)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "host": "192.168.1.5",
            "port": 1883,
            # No credentials provided
        },
    )
    assert result["step_id"] == "configure_serial_port"


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
    assert result["errors"][SZ_SERIAL_PORT] == "invalid_port_config"

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
    assert result["errors"][CONF_RAMSES_RF] == "invalid_gateway_config"

    # 4. Schema/Traits Errors (Line 432-434, 440-442, 458)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_SCAN_INTERVAL: 60}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_SCHEMA: "not_a_dict", SZ_KNOWN_LIST: "not_a_dict"},
    )
    assert result["errors"][CONF_SCHEMA] == "invalid_schema"
    assert result["errors"][SZ_KNOWN_LIST] == "invalid_traits"

    # 5. Regex Error (Line 519-523)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={SZ_ENFORCE_KNOWN_LIST: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_MESSAGE_EVENTS: "[Unclosed"}
    )
    assert result["errors"][CONF_MESSAGE_EVENTS] == "invalid_regex"


async def test_options_flow_reload_logic(hass: HomeAssistant) -> None:
    """Test reload logic and cache clearing branches."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"}},
    )
    config_entry.add_to_hass(hass)

    # Bypass frozen attribute check for coverage of line 679
    config_entry.__dict__["state"] = ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "config"}
    )
    with patch("homeassistant.config_entries.ConfigEntries.async_reload") as mock_rl:
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_SCAN_INTERVAL: 120}
        )
        mock_rl.assert_called_once()

    # Test cache clearing and packet filtering (Lines 692-730)
    config_entry.__dict__["state"] = ConfigEntryState.LOADED
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "clear_cache"}
    )

    with (
        patch("homeassistant.config_entries.ConfigEntries.async_unload") as mock_un,
        patch("homeassistant.config_entries.ConfigEntries.async_setup") as mock_setup,
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
            user_input={"clear_schema": True, "clear_packets": True},
        )
        mock_un.assert_called_once()
        # Ensure the background task setup is called (since we mock it, it's safe)
        mock_setup.assert_called_once()
        mock_instance.async_save.assert_called_once()


async def test_options_flow_defaults_and_branches(hass: HomeAssistant) -> None:
    """Test various options flow branches including defaults and finish steps."""
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "choose_serial_port"}
        )

        # Verify default falls back to Manual
        # We must find the schema key for SZ_PORT_NAME
        port_key = next(k for k in result["data_schema"].schema if k == SZ_PORT_NAME)
        assert port_key.default() == CONF_MANUAL_PATH

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
            "sqlite_index": False,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # 3. Test Line 529: async_step_advanced_features finishes in Options Flow
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "advanced_features"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_options_flow_serial_port_save(hass: HomeAssistant) -> None:
    """Test that configuring serial port in options flow triggers save (Line 308)."""
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
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][SZ_SERIAL_PORT][SZ_PORT_NAME] == "/dev/ttyUSB_NEW"


async def test_choose_serial_port_defaults(hass: HomeAssistant) -> None:
    """Test that choose_serial_port defaults to stored port if present (Line 162)."""
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "choose_serial_port"}
        )

        # Verify default is the existing port
        port_key = next(k for k in result["data_schema"].schema if k == SZ_PORT_NAME)
        assert port_key.default() == "/dev/ttyUSB_EXISTING"


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

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_SCAN_INTERVAL] == 60
    assert "restore_cache" not in result["options"]


async def test_single_instance_allowed(hass: HomeAssistant) -> None:
    """Test that only one instance is allowed (Integration Style)."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


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

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_configure_serial_port_error_logic(hass: HomeAssistant) -> None:
    """Test the defensive error path in configure_serial_port (lines 299-301)."""

    # 1. Start flow and pick a port (discovered)
    # Patch needs to be active during init for the port to be valid in the schema
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
    # To trigger the error 'port_name is None', we must manipulate the stored options
    # on the flow handler instance before submitting the next step.
    # The flow handler is stored in hass.config_entries.flow._progress[flow_id]
    flow_instance = hass.config_entries.flow._progress[result["flow_id"]]
    flow_instance.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = None

    # Enable manual mode so SZ_PORT_NAME appears in the schema, allowing errors to attach
    flow_instance._manual_serial_port = True

    # 3. Submit empty input (triggers lookup in options)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_SERIAL_PORT: {}},
    )

    # Assert that the flow halted at the form step.
    # We do NOT assert result['errors'] is populated because Home Assistant's
    # data_entry_flow implementation may filter errors for fields that were not
    # submitted in the user_input or handle schema errors differently.
    # The key behavior we are testing is that it DID NOT proceed to success (CREATE_ENTRY)
    # or the next step ('config').
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"


def test_get_usb_ports_full() -> None:
    """Test get_usb_ports with VID/PID present (Lines 76-78)."""
    with (
        patch("serial.tools.list_ports.comports") as mock_ports,
        patch("homeassistant.components.usb.usb_device_from_port") as mock_usb_dev,
        patch(
            "homeassistant.components.usb.get_serial_by_id", return_value="/dev/ttyUSB0"
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


def test_get_usb_ports_logic_edge_case() -> None:
    """Test get_usb_ports when VID is missing (Lines 161-164)."""
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


async def test_configure_serial_port_validation_error(hass: HomeAssistant) -> None:
    """Test that an invalid serial port configuration stays on the same step with errors.

    This specifically tests the fix in lines 306-308 of config_flow.py, ensuring
    the flow does not proceed if validation fails.
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

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"

    # 2. Submit an invalid configuration (e.g., baudrate as a string instead of int)
    # This should trigger a vol.Invalid error in SCH_SERIAL_PORT_CONFIG
    invalid_input = {
        SZ_PORT_NAME: "/dev/ttyUSB0",
        SZ_SERIAL_PORT: {"baudrate": "not_an_int"},
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=invalid_input,
    )

    # 3. Assert that we are still on the same step and have an error
    # Because of the indentation fix, it should NOT return async_step_config()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"
    assert SZ_SERIAL_PORT in result["errors"]
    assert result["errors"][SZ_SERIAL_PORT] == "invalid_port_config"


async def test_configure_serial_port_missing_port_name(hass: HomeAssistant) -> None:
    """Test that the flow handles a missing port_name in options correctly.

    This targets the 'if port_name is None' block and ensures the flow stays
    on the current step due to the indentation fix.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    # 1. Reach the configure_serial_port step
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports", return_value={}
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_MANUAL_PATH},
        )

    # 2. Access the actual flow handler
    flow_handler = hass.config_entries.flow._progress.get(result["flow_id"])
    # Corrupt internal options so retrieved port_name is None
    flow_handler.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = None

    # 3. Modify the flow's current step schema to make port_name optional
    # This bypasses the 'required key not provided' error in async_configure
    import voluptuous as vol

    current_step = flow_handler.cur_step
    old_schema = current_step["data_schema"]
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
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"
    assert result["errors"][SZ_PORT_NAME] == "port_name_required"
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
    # We patch here so the FORM returned by async_configure has the correct schema options
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

    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_ha_mqtt_flow(hass: HomeAssistant) -> None:
    """Test selecting Home Assistant MQTT in the user flow."""
    from custom_components.ramses_cc.config_flow import CONF_HA_MQTT_PATH
    from custom_components.ramses_cc.const import CONF_MQTT_USE_HA

    # Mock MQTT entry existing and LOADED to avoid mqtt_missing error
    mock_mqtt_entry = MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.LOADED,
    )
    mock_mqtt_entry.add_to_hass(hass)

    # Patch discover to succeed (return an ID) to avoid the "discovery_failed" error screen
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
        # Note: We can inspect the schema to ensure the option is present,
        # but successfully selecting it proves it was accepted.
        assert result["step_id"] == "choose_serial_port"

        # Select HA MQTT
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

        # Should skip configure_serial_port and go to config because it is initial setup
        assert result["step_id"] == "config"

        # Finish flow to ensure options are saved correctly
        # We provide a specific HGI ID and Topic to test full option handling
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

        # Verify known_list injection logic:
        # The logic in async_step_config injects the ID into the options.
        # However, async_step_schema (the next step) overwrites options[SZ_KNOWN_LIST]
        # with what is submitted in user_input (defaults to empty dict if missing).
        # To verify injection happened, we check that it is offered as a suggested_value.
        assert result["step_id"] == "schema"
        schema = result["data_schema"]
        # Find the key for SZ_KNOWN_LIST
        key = next(k for k in schema.schema if k == SZ_KNOWN_LIST)
        suggested = key.description["suggested_value"]
        assert test_hgi_id in suggested

        # Submit Schema Step (MUST submit the suggested value to preserve it)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                SZ_ENFORCE_KNOWN_LIST: False,
                SZ_KNOWN_LIST: suggested,  # Submit back the suggested list
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

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["options"][CONF_MQTT_USE_HA] is True
        # The port name is set to dummy "mqtt_ha"
        assert result["options"][SZ_SERIAL_PORT][SZ_PORT_NAME] == "mqtt_ha"
        assert result["options"][CONF_MQTT_HGI_ID] == test_hgi_id
        assert result["options"][CONF_MQTT_TOPIC] == test_topic

        # Now verify it persisted
        known_list = result["options"].get(SZ_KNOWN_LIST, {})
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

    # Also need an MQTT entry in HA so it shows up in list.
    # IMPORTANT: State must be LOADED for the options flow to show the option.
    mock_mqtt_entry = MockConfigEntry(
        domain="mqtt", data={}, state=ConfigEntryState.LOADED
    )
    mock_mqtt_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

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
    port_key = next(k for k in result["data_schema"].schema if k == SZ_PORT_NAME)
    assert port_key.default() == CONF_HA_MQTT_PATH

    # Select it again (should save immediately in options flow)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MQTT_USE_HA] is True


async def test_ha_mqtt_discovery_failure(hass: HomeAssistant) -> None:
    """Test that HA MQTT discovery failure triggers the error and default value."""

    # Ensure MQTT integration exists so the option appears
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

    # Select HA MQTT, but force discovery to fail (return None)
    with patch(
        "custom_components.ramses_cc.config_flow.BaseRamsesFlow._discover_mqtt_hgi",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    # Verify we are at the 'config' step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "config"

    # Verify the discovery_failed error is present
    assert result["errors"]["base"] == "discovery_failed"

    # Verify the correct placeholder is set for the error message
    assert result["description_placeholders"]["default_id"] == DEFAULT_HGI_ID


async def test_ha_mqtt_discovery_success(hass: HomeAssistant) -> None:
    """Test successful MQTT discovery."""
    # Ensure MQTT entry exists
    MockConfigEntry(
        domain="mqtt",
        data={"broker": "mock_broker"},
        state=ConfigEntryState.LOADED,
    ).add_to_hass(hass)

    # Prepare the message that triggers discovery
    msg = MagicMock()
    msg.topic = "ramses_cc/18:123456/status"
    msg.payload = "payload"

    # Side effect to simulate immediate message receipt upon subscription
    async def mock_subscribe(
        hass: HomeAssistant,
        topic: str,
        msg_callback: Callable[[Any], None],
        *args: Any,
        **kwargs: Any,
    ) -> MagicMock:
        # 1. Send Bad message first to cover exception handling in loop
        bad_msg = MagicMock()
        bad_msg.topic = None  # Causes AttributeError on split
        msg_callback(bad_msg)

        # 2. Send Good message
        msg_callback(msg)
        # 3. Send again to cover 'if done(): return' check
        msg_callback(msg)
        return MagicMock()

    # NOTE: We patch the mqtt reference *inside* the config_flow module
    # to ensure the mock is used even if the module is already imported.
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

        # Select HA MQTT
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_HA_MQTT_PATH},
        )

    # Check that we proceeded to config step without error
    assert result["step_id"] == "config"
    assert "base" not in result.get("errors", {})

    # Check that the ID was populated in the flow options
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow.options[CONF_MQTT_HGI_ID] == "18:123456"


async def test_ha_mqtt_missing_integration(hass: HomeAssistant) -> None:
    """Test selecting HA MQTT when MQTT integration is not set up."""
    # Do NOT add MQTT MockConfigEntry

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

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "choose_serial_port"
    assert result["errors"]["base"] == "mqtt_missing"


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

    # Should proceed to config but with error (discovery failed returns None)
    assert result["step_id"] == "config"
    assert result["errors"]["base"] == "discovery_failed"


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

    assert result["step_id"] == "config"
    assert result["errors"]["base"] == "discovery_failed"


async def test_ha_mqtt_not_loaded_error(hass: HomeAssistant) -> None:
    """Test error when MQTT integration exists but is not loaded.

    This ensures complete coverage of the MQTT state check logic.
    """
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

    # Should stay on the same step with an error
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "choose_serial_port"
    assert result["errors"]["base"] == "mqtt_missing"


# ---------------------------------------------------------------------------
# Zigbee device selection tests
# ---------------------------------------------------------------------------


def _make_zigbee_device(
    device_id: str,
    name: str = "RAMSES ESP32-C6",
    ieee: str | None = "00:11:22:33:44:55:66:77",
) -> MagicMock:
    """Return a MagicMock that looks like a ZHA DeviceEntry for ramses_esp32c6."""
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
    """Test zigbee_device step shows error when no ramses_esp32c6 devices exist."""
    mock_registry = MagicMock()
    mock_registry.devices = {}  # empty → no matches

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

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zigbee_device"
    assert result["errors"]["base"] == "no_ramses_device_found"


async def test_zigbee_single_device_auto_configure(hass: HomeAssistant) -> None:
    """Test zigbee_device auto-configures when exactly one device with IEEE is found."""
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

    # Single device with IEEE → auto-configured, advances to configure_serial_port
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"


async def test_zigbee_single_device_no_ieee(hass: HomeAssistant) -> None:
    """Test zigbee_device step shows error when single device has no IEEE address."""
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

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zigbee_device"
    assert result["errors"]["base"] == "no_ieee_identifier"


async def test_zigbee_multiple_devices_shows_selector(hass: HomeAssistant) -> None:
    """Test zigbee_device step shows SelectSelector when multiple devices are present."""
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

    # Multiple devices → show selector form with no errors
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zigbee_device"
    assert result["errors"] == {}


async def test_zigbee_user_selects_device_from_selector(hass: HomeAssistant) -> None:
    """Test user picks a device from the multi-device SelectSelector."""
    dev1 = _make_zigbee_device(
        "dev1", name="Device One", ieee="00:11:22:33:44:55:66:77"
    )
    dev2 = _make_zigbee_device(
        "dev2", name="Device Two", ieee="aa:bb:cc:dd:ee:ff:00:11"
    )
    mock_registry = MagicMock()
    mock_registry.devices = {"dev1": dev1, "dev2": dev2}
    # async_get("dev1") will return dev1 when the user submits their selection
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
        # Step 1: navigate to zigbee_device (multiple devices → shows selector)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )
        assert result["step_id"] == "zigbee_device"

        # Step 2: user selects dev1 from the selector
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"device": "dev1"},
        )

    # After selection with valid IEEE → advance to configure_serial_port
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"


async def test_zigbee_port_picker_shows_zigbee_option(hass: HomeAssistant) -> None:
    """Test that CONF_ZIGBEE_DEVICE is a valid port choice in the picker.

    Verifies that the choose_serial_port form accepts CONF_ZIGBEE_DEVICE as a
    valid submission value (i.e. it is registered in the SelectSelector options).
    If it were not, voluptuous would reject the submission before our handler runs.
    """
    mock_registry = MagicMock()
    mock_registry.devices = {}  # no devices → generic label

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
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "choose_serial_port"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_ZIGBEE_DEVICE},
        )

    # If CONF_ZIGBEE_DEVICE were not in the selector options, the form
    # would return an "invalid_select_option" error here instead.
    assert result["step_id"] == "zigbee_device"


async def test_zigbee_single_device_label_in_port_picker(hass: HomeAssistant) -> None:
    """Test port picker shows device-specific label when one matching device is found.

    The choose_serial_port step builds a custom label "Zigbee device: <name>"
    when exactly one ramses_esp32c6 device is present in the device registry.
    """
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

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "choose_serial_port"

    # Extract the options list from the SelectSelector for SZ_PORT_NAME.
    # vol.Required("key").schema == "key", and SelectSelector.config is TypedDict.
    schema = result["data_schema"].schema
    port_name_key = next(
        (k for k in schema if getattr(k, "schema", None) == SZ_PORT_NAME), None
    )
    assert port_name_key is not None, "SZ_PORT_NAME key not found in schema"

    selector_obj = schema[port_name_key]
    # SelectSelectorConfig is a TypedDict (plain dict at runtime)
    config = getattr(selector_obj, "config", {})
    options: list[dict[str, Any]] = (
        config.get("options", []) if isinstance(config, dict) else []
    )
    opts_by_value = {opt.get("value"): opt.get("label", "") for opt in options}

    zigbee_label = opts_by_value.get(CONF_ZIGBEE_DEVICE, "")
    assert "Zigbee device:" in zigbee_label, (
        f"Expected 'Zigbee device:' in label but got: {zigbee_label!r}"
    )

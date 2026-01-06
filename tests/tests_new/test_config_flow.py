"""Tests for the ramses_cc config flow.

This module contains tests for the configuration wizard (ConfigFlow) and the
options menu (OptionsFlow), ensuring that user inputs are correctly processed
and converted into configuration entries.
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry  # type: ignore

from custom_components.ramses_cc.config_flow import CONF_MQTT_PATH, get_usb_ports
from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.schemas import SZ_CONFIG, SZ_SERIAL_PORT
from ramses_tx.schemas import SZ_PORT_NAME

# Constants used in the tests
CONF_MANUAL_PATH = "Enter Manually..."


@pytest.fixture(autouse=True)
def bypass_setup_fixture() -> Iterator[None]:
    """Prevent actual setup of the integration during config flow tests."""
    with patch(
        "custom_components.ramses_cc.async_setup_entry",
        return_value=True,
    ):
        yield


async def test_full_user_flow(hass: HomeAssistant) -> None:
    """Test the full user configuration flow from start to finish.

    This test simulates a user:
    1. Starting the flow.
    2. Selecting 'Enter Manually...' for the serial port.
    3. Entering the serial port path.
    4. Configuring gateway options.
    5. Configuring schema options.
    6. Configuring advanced features.
    7. Configuring packet logging.
    8. Finally creating the entry.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    # 1. Choose Serial Port Step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "choose_serial_port"

    # Select "Enter Manually..."
    with patch(
        "custom_components.ramses_cc.config_flow.async_get_usb_ports",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={SZ_PORT_NAME: CONF_MANUAL_PATH},
        )

    # 2. Configure Serial Port Step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_PORT_NAME: "/dev/ttyUSB0"},
    )

    # 3. Gateway Config Step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "config"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL: 60},
    )

    # 4. Schema Step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "schema"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"enforce_known_list": False},
    )

    # 5. Advanced Features Step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "advanced_features"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={},
    )

    # 6. Packet Log Step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "packet_log"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={},
    )

    # 7. Create Entry
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "RAMSES RF"
    assert result["data"] == {}
    assert result["options"][SZ_SERIAL_PORT][SZ_PORT_NAME] == "/dev/ttyUSB0"


async def test_mqtt_flow(hass: HomeAssistant) -> None:
    """Test the MQTT configuration flow.

    This targets lines 192-263 in config_flow.py.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    # 1. Select "MQTT Broker..."
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={SZ_PORT_NAME: CONF_MQTT_PATH},
    )

    # 2. MQTT Config Form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "mqtt_config"

    # 3. Enter MQTT Details
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "host": "localhost",
            "port": 1883,
            "username": "user",
            "password": "pass",
        },
    )

    # 4. Returns to configure_serial_port (with MQTT URL pre-filled internally)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "configure_serial_port"

    # 5. Continue through the rest of the flow
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={},
    )
    assert result["step_id"] == "config"


async def test_options_flow(hass: HomeAssistant) -> None:
    """Test the options flow (re-configuration).

    This test simulates a user:
    1. Having an existing config entry.
    2. Opening the options menu.
    3. Changing the scan interval.
    """
    # Create a mock config entry using the official test helper
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ramses_cc_test",
        data={},
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCAN_INTERVAL: 60,
            SZ_CONFIG: {},
        },
    )
    config_entry.add_to_hass(hass)

    # Initialize options flow
    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    # 1. Main Menu
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "init"

    # Select 'config' step
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "config"},
    )

    # 2. Config Step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "config"

    # Change scan interval to 120
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL: 120},
    )

    # 3. Create Entry (Save)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCAN_INTERVAL] == 120


async def test_options_flow_clear_cache(hass: HomeAssistant) -> None:
    """Test clearing the cache via options flow.

    This targets lines 689-730 in config_flow.py.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ramses_cc_test_cache",
        data={},
        options={
            SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
            CONF_SCAN_INTERVAL: 60,
            SZ_CONFIG: {},
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    # 1. Select Clear Cache
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "clear_cache"},
    )

    # 2. Confirm Clear
    # We mock the Store to avoid file I/O errors and simulate data present
    with patch("custom_components.ramses_cc.config_flow.Store") as mock_store:
        mock_instance = mock_store.return_value
        # Use AsyncMock for async methods so they can be awaited
        mock_instance.async_load = AsyncMock(
            return_value={"client_state": {"schema": {}, "packets": {}}}
        )
        mock_instance.async_save = AsyncMock()

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"clear_schema": True, "clear_packets": True},
        )

    # 3. Assert Flow Aborts (Cache Cleared)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "cache_cleared"


def test_get_usb_ports_logic() -> None:
    """Test the synchronous get_usb_ports helper.

    This targets lines 73-88 in config_flow.py.
    """
    with (
        patch(
            "custom_components.ramses_cc.config_flow.list_ports.comports"
        ) as mock_comports,
        patch(
            "custom_components.ramses_cc.config_flow.usb.usb_device_from_port"
        ) as mock_usb_dev,
        patch(
            "custom_components.ramses_cc.config_flow.usb.get_serial_by_id"
        ) as mock_get_serial,
        patch(
            "custom_components.ramses_cc.config_flow.usb.human_readable_device_name"
        ) as mock_human,
    ):
        mock_port = MagicMock()
        mock_port.vid = 1234
        mock_port.pid = 5678
        mock_port.device = "/dev/ttyUSB0"
        mock_port.serial_number = "123"
        mock_port.manufacturer = "Acme"
        mock_port.description = "Device"

        mock_comports.return_value = [mock_port]
        mock_usb_dev.return_value.vid = "1234"
        mock_usb_dev.return_value.pid = "5678"
        mock_get_serial.return_value = "/dev/serial/by-id/usb-Acme_Device_123"
        mock_human.return_value = "Acme Device"

        ports = get_usb_ports()
        assert "/dev/serial/by-id/usb-Acme_Device_123" in ports
        assert ports["/dev/serial/by-id/usb-Acme_Device_123"] == "Acme Device"

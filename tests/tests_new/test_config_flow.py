"""Tests for the ramses_cc config flow.

This module contains tests for the configuration wizard (ConfigFlow) and the
options menu (OptionsFlow), ensuring that user inputs are correctly processed
and converted into configuration entries.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry  # type: ignore

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
        # Removed "restore_cache": False as it is not part of this flow step schema
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

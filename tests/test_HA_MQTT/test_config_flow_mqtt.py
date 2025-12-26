"""Test the Ramses CC config flow for MQTT selection."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ramses_cc import config_flow
from custom_components.ramses_cc.const import CONF_MQTT_TOPIC, CONF_MQTT_USE_HA, DOMAIN


# Helper function to mock setup calls with real coroutines
async def mock_async_setup_component(hass: Any, domain: str, config: Any) -> bool:
    """Return True for async_setup_component."""
    return True


@pytest.mark.asyncio
async def test_flow_selects_mqtt_ha_success(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    """Test selecting HA MQTT when the MQTT integration is present."""

    # Combine all patches into one context manager
    with (
        patch.dict(config_entries.HANDLERS, {DOMAIN: config_flow.RamsesConfigFlow}),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_entries",
            return_value=[MagicMock(domain="mqtt")],
        ),
        # FIX: Patch async_setup_component with a real async function
        patch(
            "homeassistant.setup.async_setup_component",
            side_effect=mock_async_setup_component,
        ),
        # CRITICAL FIX: Prevent the config entry from actually starting up.
        # This stops the code from reaching the lines that cause the TypeError crash.
        # We verify the result object instead.
        patch.object(hass.config_entries, "async_add", return_value=True),
    ):
        # Initialise the flow
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.MENU

        # 2. User selects "mqtt_ha" from the menu
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "mqtt_ha"}
        )

        # Should show the form to ask for the TOPIC
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "mqtt_ha"

        # 3. User enters topic and submits
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MQTT_TOPIC: "RAMSES/TEST"}
        )

        # 4. Verify Entry Creation
        # Because we mocked async_add, the flow finishes successfully and returns the creation data
        assert result["type"] == FlowResultType.CREATE_ENTRY

        # FIX: Check "options" instead of "data" because _async_save writes to options
        assert result["options"][CONF_MQTT_USE_HA] is True
        assert result["options"][CONF_MQTT_TOPIC] == "RAMSES/TEST"


@pytest.mark.asyncio
async def test_flow_mqtt_ha_missing_integration(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    """Test selecting HA MQTT when the MQTT integration is MISSING."""

    # Combine all patches into one context manager
    with (
        patch.dict(config_entries.HANDLERS, {DOMAIN: config_flow.RamsesConfigFlow}),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_entries",
            return_value=[],
        ),
        patch(
            "homeassistant.setup.async_setup_component",
            side_effect=mock_async_setup_component,
        ),
    ):
        # Initialize
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        # 2. User selects "mqtt_ha"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "mqtt_ha"}
        )

        # 3. Verify Error
        assert result["type"] == FlowResultType.ABORT

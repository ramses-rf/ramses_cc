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
        # FIX: Use side_effect with a real async function so it is awaitable
        patch(
            "homeassistant.setup.async_setup_component",
            side_effect=mock_async_setup_component,
        ),
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
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_MQTT_USE_HA] is True
        assert result["data"][CONF_MQTT_TOPIC] == "RAMSES/TEST"


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
        # FIX: Use side_effect with a real async function
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
        # Implementation returns ABORT when MQTT is missing
        assert result["type"] == FlowResultType.ABORT

        # Optional: Check the abort reason if you know what key your code uses
        # assert result["reason"] == "mqtt_missing"

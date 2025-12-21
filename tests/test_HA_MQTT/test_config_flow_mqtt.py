"""Test the Ramses CC config flow for MQTT selection."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ramses_cc.const import CONF_MQTT_TOPIC, CONF_MQTT_USE_HA, DOMAIN


@pytest.mark.asyncio
async def test_flow_selects_mqtt_ha_success(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    """Test selecting HA MQTT when the MQTT integration is present."""

    # 1. Simulate that the 'mqtt' integration is set up
    with (
        patch(
            "homeassistant.config_entries.ConfigEntries.async_entries",
            return_value=[MagicMock(domain="mqtt")],
        ),
        # FIX: Patch the top-level HA setup manager to return True immediately.
        # This bypasses the actual component initialization (which we don't need for flow tests)
        # and avoids the Coroutine/Mock TypeErrors.
        patch("homeassistant.setup.async_setup_component", return_value=True),
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

    # 1. Simulate that 'mqtt' is NOT in async_entries (Empty list)
    with (
        patch(
            "homeassistant.config_entries.ConfigEntries.async_entries", return_value=[]
        ),
        # FIX: Patch top-level setup here as well
        patch("homeassistant.setup.async_setup_component", return_value=True),
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
        # It should return the menu again (re-show form) but with an error
        assert result["type"] == FlowResultType.MENU

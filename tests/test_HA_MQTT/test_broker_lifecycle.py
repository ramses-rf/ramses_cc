"""Unit tests for the RamsesBroker lifecycle management."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import CONF_MQTT_USE_HA


@pytest.mark.asyncio
async def test_broker_waits_for_mqtt_client() -> None:
    """Verify RamsesBroker waits for MQTT before starting the bridge."""

    # 1. Setup Mock HASS and Config Entry
    hass = MagicMock()
    hass.loop = asyncio.get_event_loop()

    entry = MagicMock()
    entry.options = {CONF_MQTT_USE_HA: True}

    broker = RamsesBroker(hass, entry)

    # 2. Mock Dependencies
    # We mock the storage load to return empty so setup proceeds
    with (
        patch("custom_components.ramses_cc.broker.Store.async_load", return_value={}),
        patch("custom_components.ramses_cc.broker.RamsesMqttBridge") as mock_bridge_cls,
        patch("custom_components.ramses_cc.broker.Gateway") as mock_gateway_cls,
        patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt,
    ):
        # Setup Mocks
        mock_bridge_instance = mock_bridge_cls.return_value
        mock_gateway_instance = mock_gateway_cls.return_value

        # KEY: Mock the wait function
        mock_mqtt.async_wait_for_mqtt_client = AsyncMock(return_value=True)

        # 3. Run Setup
        await broker.async_setup()

        # 4. Verifications
        # Ensure we actually waited
        mock_mqtt.async_wait_for_mqtt_client.assert_awaited_once_with(hass)

        # Ensure bridge was started AFTER the wait
        mock_bridge_instance.async_start.assert_awaited_once_with(mock_gateway_instance)


@pytest.mark.asyncio
async def test_broker_aborts_if_mqtt_fails() -> None:
    """Verify RamsesBroker aborts if MQTT client never becomes ready."""

    hass = MagicMock()
    hass.loop = asyncio.get_event_loop()
    entry = MagicMock()
    entry.options = {CONF_MQTT_USE_HA: True}
    broker = RamsesBroker(hass, entry)

    with (
        patch("custom_components.ramses_cc.broker.Store.async_load", return_value={}),
        patch("custom_components.ramses_cc.broker.RamsesMqttBridge") as mock_bridge_cls,
        patch("custom_components.ramses_cc.broker.Gateway"),
        patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt,
    ):
        mock_bridge_instance = mock_bridge_cls.return_value

        # KEY: Simulate timeout/failure
        mock_mqtt.async_wait_for_mqtt_client = AsyncMock(return_value=False)

        await broker.async_setup()

        # Verification: We waited, but because it failed...
        mock_mqtt.async_wait_for_mqtt_client.assert_awaited()
        # ...the bridge should NOT have started
        mock_bridge_instance.async_start.assert_not_awaited()

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

    # Create a config entry with MQTT enabled options
    entry = MagicMock()
    entry.options = {CONF_MQTT_USE_HA: True}

    # Initialize the Broker
    broker = RamsesBroker(hass, entry)

    # 2. Mock Dependencies
    # We mock the Store class so we can control what async_load returns
    with patch("custom_components.ramses_cc.broker.Store") as mock_store_cls:
        mock_store_instance = mock_store_cls.return_value
        mock_store_instance.async_load = AsyncMock(return_value={})  # Return empty dict

        with (
            patch(
                "custom_components.ramses_cc.broker.RamsesMqttBridge"
            ) as mock_bridge_cls,
            patch("custom_components.ramses_cc.broker.Gateway") as mock_gateway_cls,
            patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt,
        ):
            # Setup Bridge Mock
            mock_bridge_instance = mock_bridge_cls.return_value
            mock_bridge_instance.async_start = AsyncMock()

            # Setup Gateway Mock
            mock_gateway_instance = mock_gateway_cls.return_value
            mock_gateway_instance.start = AsyncMock()

            # KEY: Mock the wait function to return True (connected)
            mock_mqtt.async_wait_for_mqtt_client = AsyncMock(return_value=True)

            # 3. Run Setup
            await broker.async_setup()

            # 4. Verifications

            # Ensure we waited for the client
            mock_mqtt.async_wait_for_mqtt_client.assert_awaited_once_with(hass)

            # Ensure bridge was started AFTER the wait
            mock_bridge_instance.async_start.assert_awaited_once_with(
                mock_gateway_instance
            )

            # Ensure the client was started
            mock_gateway_instance.start.assert_awaited()


@pytest.mark.asyncio
async def test_broker_aborts_if_mqtt_fails() -> None:
    """Verify RamsesBroker aborts if MQTT client never becomes ready."""

    hass = MagicMock()
    hass.loop = asyncio.get_event_loop()
    entry = MagicMock()
    entry.options = {CONF_MQTT_USE_HA: True}
    broker = RamsesBroker(hass, entry)

    with patch("custom_components.ramses_cc.broker.Store") as mock_store_cls:
        mock_store_instance = mock_store_cls.return_value
        mock_store_instance.async_load = AsyncMock(return_value={})

        with (
            patch(
                "custom_components.ramses_cc.broker.RamsesMqttBridge"
            ) as mock_bridge_cls,
            patch("custom_components.ramses_cc.broker.Gateway"),
            patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt,
        ):
            mock_bridge_instance = mock_bridge_cls.return_value
            mock_bridge_instance.async_start = AsyncMock()

            # KEY: Simulate timeout/failure (return False)
            mock_mqtt.async_wait_for_mqtt_client = AsyncMock(return_value=False)

            # 3. Run Setup
            await broker.async_setup()

            # Verification: We waited...
            mock_mqtt.async_wait_for_mqtt_client.assert_awaited()

            # ...but because it failed, the bridge should NOT have started
            mock_bridge_instance.async_start.assert_not_awaited()

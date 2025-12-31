"""Unit tests for the RamsesBroker lifecycle management."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import CONF_MQTT_USE_HA


@pytest.mark.asyncio
async def test_broker_waits_for_mqtt_client() -> None:
    """Verify RamsesBroker waits for MQTT before starting the bridge."""

    hass = MagicMock()
    hass.loop = asyncio.get_event_loop()
    entry = MagicMock()
    entry.options = {CONF_MQTT_USE_HA: True}

    with patch("custom_components.ramses_cc.broker.Store") as mock_store_cls:
        mock_store_instance = mock_store_cls.return_value
        mock_store_instance.async_load = AsyncMock(return_value={})

        with (
            patch(
                "custom_components.ramses_cc.broker.RamsesMqttBridge"
            ) as mock_bridge_cls,
            patch("custom_components.ramses_cc.broker.Gateway") as mock_gateway_cls,
            # KEY FIX: Point to the REAL Home Assistant MQTT component
            patch(
                "homeassistant.components.mqtt.async_wait_for_mqtt_client",
                new_callable=AsyncMock,
            ) as mock_wait,
        ):
            mock_bridge_instance = mock_bridge_cls.return_value
            mock_bridge_instance.async_start = AsyncMock()

            mock_gateway_instance = mock_gateway_cls.return_value
            mock_gateway_instance.start = AsyncMock()

            # Configure the mock to return True (Connected)
            mock_wait.return_value = True

            broker = RamsesBroker(hass, entry)
            await broker.async_setup()

            # Verify we waited
            mock_wait.assert_awaited_once_with(hass)

            # Verify bridge started
            mock_bridge_instance.async_start.assert_awaited_once_with(
                mock_gateway_instance
            )


@pytest.mark.asyncio
async def test_broker_aborts_if_mqtt_fails() -> None:
    """Verify RamsesBroker aborts if MQTT client never becomes ready."""

    hass = MagicMock()
    hass.loop = asyncio.get_event_loop()
    entry = MagicMock()
    entry.options = {CONF_MQTT_USE_HA: True}

    with patch("custom_components.ramses_cc.broker.Store") as mock_store_cls:
        mock_store_instance = mock_store_cls.return_value
        mock_store_instance.async_load = AsyncMock(return_value={})

        with (
            patch(
                "custom_components.ramses_cc.broker.RamsesMqttBridge"
            ) as mock_bridge_cls,
            patch("custom_components.ramses_cc.broker.Gateway"),
            # KEY FIX: Point to the REAL Home Assistant MQTT component
            patch(
                "homeassistant.components.mqtt.async_wait_for_mqtt_client",
                new_callable=AsyncMock,
            ) as mock_wait,
        ):
            mock_bridge_instance = mock_bridge_cls.return_value
            mock_bridge_instance.async_start = AsyncMock()

            # Simulate failure/timeout
            mock_wait.return_value = False

            broker = RamsesBroker(hass, entry)
            await broker.async_setup()

            # Verify we waited
            mock_wait.assert_awaited()

            # Verify bridge did NOT start
            mock_bridge_instance.async_start.assert_not_awaited()

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

    # 2. Mock Dependencies
    # CRITICAL FIX: Patch Store BEFORE instantiating RamsesBroker
    with patch("custom_components.ramses_cc.broker.Store") as mock_store_cls:
        mock_store_instance = mock_store_cls.return_value
        mock_store_instance.async_load = AsyncMock(return_value={})

        with (
            patch(
                "custom_components.ramses_cc.broker.RamsesMqttBridge"
            ) as mock_bridge_cls,
            patch("custom_components.ramses_cc.broker.Gateway") as mock_gateway_cls,
            patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt_module,
        ):
            # Setup Bridge Mock
            mock_bridge_instance = mock_bridge_cls.return_value
            mock_bridge_instance.async_start = AsyncMock()

            # Setup Gateway Mock
            mock_gateway_instance = mock_gateway_cls.return_value
            mock_gateway_instance.start = AsyncMock()

            # KEY FIX: Explicitly use AsyncMock so it can be awaited
            mock_mqtt_module.async_wait_for_mqtt_client = AsyncMock(return_value=True)

            # 3. Initialize the Broker (INSIDE the patch context)
            broker = RamsesBroker(hass, entry)

            # 4. Run Setup
            await broker.async_setup()

            # 5. Verifications
            # Ensure we waited for the client
            mock_mqtt_module.async_wait_for_mqtt_client.assert_awaited_once_with(hass)

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

    with patch("custom_components.ramses_cc.broker.Store") as mock_store_cls:
        mock_store_instance = mock_store_cls.return_value
        mock_store_instance.async_load = AsyncMock(return_value={})

        with (
            patch(
                "custom_components.ramses_cc.broker.RamsesMqttBridge"
            ) as mock_bridge_cls,
            patch("custom_components.ramses_cc.broker.Gateway"),
            patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt_module,
        ):
            mock_bridge_instance = mock_bridge_cls.return_value
            mock_bridge_instance.async_start = AsyncMock()

            # KEY FIX: Explicitly use AsyncMock (returning False) so it can be awaited
            mock_mqtt_module.async_wait_for_mqtt_client = AsyncMock(return_value=False)

            # Initialize the Broker
            broker = RamsesBroker(hass, entry)

            # Run Setup
            await broker.async_setup()

            # Verification: We waited...
            mock_mqtt_module.async_wait_for_mqtt_client.assert_awaited()

            # ...but because it failed, the bridge should NOT have started
            mock_bridge_instance.async_start.assert_not_awaited()

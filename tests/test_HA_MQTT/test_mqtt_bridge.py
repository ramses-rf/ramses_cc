#!/usr/bin/env python3
"""Unit tests for the RamsesMqttBridge."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ramses_cc.broker import RamsesMqttBridge
from ramses_tx.const import SZ_ACTIVE_HGI
from ramses_tx.transport import CallbackTransport


# Mock Home Assistant ReceiveMessage object
class MockMQTTMessage:
    def __init__(self, topic: str, payload: str | bytes) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = 0


class TestMqttBridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hass = MagicMock()
        self.hass.loop = asyncio.get_event_loop()

        # Mock the generic transport factory since we are testing the Bridge logic
        with patch(
            "custom_components.ramses_cc.broker.CallbackTransport"
        ) as mock_transport_cls:
            self.mock_transport = AsyncMock(spec=CallbackTransport)
            self.mock_transport._extra = {}  # Emulate the extra dict
            self.mock_transport.get_extra_info.side_effect = (
                lambda k: self.mock_transport._extra.get(k)
            )
            mock_transport_cls.return_value = self.mock_transport

            self.bridge = RamsesMqttBridge(self.hass, topic_root="RAMSES/TEST")

            # Mock the Protocol and Gateway for full integration testing
            self.protocol = MagicMock()
            self.gateway = MagicMock()
            self.gateway._protocol = self.protocol

            # Manually trigger the constructor logic to bind the transport
            await self.bridge.async_transport_constructor(self.protocol)

            # Link the gateway so circuit breaker tests work
            await self.bridge.async_start(self.gateway)

    async def test_incoming_mqtt_parsing(self) -> None:
        """Verify JSON payloads are unpacked and injected into transport."""
        # Payload mimicking ramses_esp
        payload = json.dumps(
            {"ts": "2023-01-01T12:00:00", "msg": " I --- 18:123456 ..."}
        )
        msg = MockMQTTMessage("RAMSES/TEST/18:123456/rx", payload)

        self.bridge._handle_mqtt_message(msg)

        # Verify transport received the RAW string, not the JSON
        self.mock_transport.receive_frame.assert_called_with(
            " I --- 18:123456 ...", dtm="2023-01-01T12:00:00"
        )

    async def test_gateway_id_injection(self) -> None:
        """Verify the Gateway ID is extracted from topic and injected."""
        # Initial state: No HGI known
        self.assertIsNone(self.mock_transport.get_extra_info(SZ_ACTIVE_HGI))

        payload = json.dumps({"msg": "..."})
        msg = MockMQTTMessage("RAMSES/TEST/18:999999/rx", payload)

        self.bridge._handle_mqtt_message(msg)

        # Verify HGI ID was set in the transport extras
        self.assertEqual(self.mock_transport._extra[SZ_ACTIVE_HGI], "18:999999")

    async def test_outgoing_mqtt_formatting(self) -> None:
        """Verify outgoing frames are wrapped in JSON."""
        # Setup the Gateway ID lookup
        self.gateway.hgi.id = "18:123456"

        # We need to mock mqtt.async_publish inside the bridge module
        with patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt:
            mock_mqtt.async_publish = AsyncMock()

            await self.bridge._async_mqtt_publish("RQ --- ...")

            # Check the arguments passed to HA MQTT publish
            # Topic should end in /tx, Payload should be JSON {"msg": "..."}
            mock_mqtt.async_publish.assert_awaited()
            call_args = mock_mqtt.async_publish.call_args

            topic = call_args[0][1]
            payload = call_args[0][2]

            self.assertEqual(topic, "RAMSES/TEST/18:123456/tx")
            self.assertEqual(json.loads(payload), {"msg": "RQ --- ..."})

    def test_circuit_breaker_logic(self) -> None:
        """Verify connection status toggles BOTH transport reading AND protocol writing."""
        # 1. Disconnect
        self.bridge._handle_connection_status(False)

        # Verify Transport (Read Path) is paused
        self.mock_transport.pause_reading.assert_called()
        # Verify Protocol (Write Path) is paused -- NEW CHECK
        self.protocol.pause_writing.assert_called()

        self.mock_transport.reset_mock()
        self.protocol.reset_mock()

        # 2. Connect
        self.bridge._handle_connection_status(True)

        # Verify Transport (Read Path) is resumed
        self.mock_transport.resume_reading.assert_called()
        # Verify Protocol (Write Path) is resumed -- NEW CHECK
        self.protocol.resume_writing.assert_called()

    async def test_ioc_constructor_arguments(self) -> None:
        """Verify disable_sending and extra args are passed to the transport."""
        # Create a new bridge instance to test construction cleanly
        bridge = RamsesMqttBridge(self.hass, topic_root="RAMSES/TEST")

        # We need to spy on the CallbackTransport class creation
        with patch("custom_components.ramses_cc.broker.CallbackTransport") as mock_cls:
            await bridge.async_transport_constructor(
                self.protocol, disable_sending=True, extra={"test_flag": 123}
            )

            # Verify the class was instantiated with the right args
            call_kwargs = mock_cls.call_args[1]
            self.assertTrue(call_kwargs["disable_sending"])
            self.assertEqual(call_kwargs["extra"]["test_flag"], 123)

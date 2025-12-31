#!/usr/bin/env python3
"""Unit tests for the RamsesMqttBridge."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ramses_cc.broker import RamsesMqttBridge
from ramses_tx.const import SZ_ACTIVE_HGI
from ramses_tx.transport import CallbackTransport


# Mock Home Assistant ReceiveMessage object
class MockMQTTMessage:
    def __init__(self, topic: str, payload: str | bytes) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = 0


@pytest.fixture
async def bridge_env() -> dict[str, Any]:
    """Fixture to setup the bridge environment for each test."""
    hass = MagicMock()
    # We must ensure the mock has a loop attribute, as code checks hass.loop
    hass.loop = asyncio.get_event_loop()

    # Mock the generic transport factory
    with patch(
        "custom_components.ramses_cc.broker.CallbackTransport"
    ) as mock_transport_cls:
        # Create a mock that behaves like an instance of CallbackTransport
        mock_transport = AsyncMock(spec=CallbackTransport)

        # Manually attach the extra dict and the get_extra_info method to the instance mock
        mock_transport._extra = {}
        mock_transport.get_extra_info.side_effect = (
            lambda k, default=None: mock_transport._extra.get(k, default)
        )

        mock_transport_cls.return_value = mock_transport

        bridge = RamsesMqttBridge(hass, topic_root="RAMSES/TEST")

        # Mock the Protocol and Gateway
        protocol = MagicMock()
        gateway = MagicMock()
        gateway._protocol = protocol

        # Manually trigger the constructor logic to bind the transport
        await bridge.async_transport_constructor(protocol)

        # Link the gateway so circuit breaker tests work
        await bridge.async_start(gateway)

        # Return a dictionary of useful objects for the test functions
        return {
            "bridge": bridge,
            "mock_transport": mock_transport,
            "gateway": gateway,
            "protocol": protocol,
            "hass": hass,
        }


@pytest.mark.asyncio
async def test_incoming_mqtt_parsing(bridge_env: dict[str, Any]) -> None:
    """Verify JSON payloads are unpacked and injected into transport."""
    bridge = bridge_env["bridge"]
    mock_transport = bridge_env["mock_transport"]

    # Payload mimicking ramses_esp
    payload = json.dumps({"ts": "2023-01-01T12:00:00", "msg": " I --- 18:123456 ..."})
    msg = MockMQTTMessage("RAMSES/TEST/18:123456/rx", payload)

    bridge._handle_mqtt_message(msg)

    # Verify transport received the RAW string, not the JSON
    mock_transport.receive_frame.assert_called_with(
        " I --- 18:123456 ...", dtm="2023-01-01T12:00:00"
    )


@pytest.mark.asyncio
async def test_gateway_id_injection(bridge_env: dict[str, Any]) -> None:
    """Verify the Gateway ID is extracted from topic and injected."""
    bridge = bridge_env["bridge"]
    mock_transport = bridge_env["mock_transport"]

    # Initial state: No HGI known
    assert mock_transport.get_extra_info(SZ_ACTIVE_HGI) is None

    payload = json.dumps({"msg": "..."})
    msg = MockMQTTMessage("RAMSES/TEST/18:999999/rx", payload)

    bridge._handle_mqtt_message(msg)

    # Verify HGI ID was set in the transport extras
    assert mock_transport._extra[SZ_ACTIVE_HGI] == "18:999999"


@pytest.mark.asyncio
async def test_outgoing_mqtt_formatting(bridge_env: dict[str, Any]) -> None:
    """Verify outgoing frames are wrapped in JSON."""
    bridge = bridge_env["bridge"]
    gateway = bridge_env["gateway"]

    # Setup the Gateway ID lookup
    gateway.hgi.id = "18:123456"

    # We need to mock mqtt.async_publish inside the bridge module
    with patch("custom_components.ramses_cc.broker.mqtt") as mock_mqtt:
        mock_mqtt.async_publish = AsyncMock()

        await bridge._async_mqtt_publish("RQ --- ...")

        # Check the arguments passed to HA MQTT publish
        mock_mqtt.async_publish.assert_awaited()
        call_args = mock_mqtt.async_publish.call_args

        topic = call_args[0][1]
        payload = call_args[0][2]

        assert topic == "RAMSES/TEST/18:123456/tx"
        assert json.loads(payload) == {"msg": "RQ --- ..."}


@pytest.mark.asyncio
async def test_circuit_breaker_logic(bridge_env: dict[str, Any]) -> None:
    """Verify connection status toggles BOTH transport reading AND protocol writing."""
    bridge = bridge_env["bridge"]
    mock_transport = bridge_env["mock_transport"]
    protocol = bridge_env["protocol"]

    # 1. Disconnect
    bridge._handle_connection_status(False)

    # Verify Transport (Read Path) is paused
    mock_transport.pause_reading.assert_called()
    # Verify Protocol (Write Path) is paused
    protocol.pause_writing.assert_called()

    mock_transport.reset_mock()
    protocol.reset_mock()

    # 2. Connect
    bridge._handle_connection_status(True)

    # Verify Transport (Read Path) is resumed
    mock_transport.resume_reading.assert_called()
    # Verify Protocol (Write Path) is resumed
    protocol.resume_writing.assert_called()


@pytest.mark.asyncio
async def test_ioc_constructor_arguments(bridge_env: dict[str, Any]) -> None:
    """Verify disable_sending and extra args are passed to the transport."""
    # We grab hass and protocol from the fixture to reuse mocks
    hass = bridge_env["hass"]
    protocol = bridge_env["protocol"]

    # Create a new bridge instance to test construction cleanly
    bridge = RamsesMqttBridge(hass, topic_root="RAMSES/TEST")

    # We need to spy on the CallbackTransport class creation
    with patch("custom_components.ramses_cc.broker.CallbackTransport") as mock_cls:
        await bridge.async_transport_constructor(
            protocol, disable_sending=True, extra={"test_flag": 123}
        )

        # Verify the class was instantiated with the right args
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["disable_sending"] is True
        assert call_kwargs["extra"]["test_flag"] == 123


@pytest.mark.asyncio
async def test_incoming_mqtt_malformed_json(bridge_env: dict[str, Any]) -> None:
    """Verify malformed JSON is handled gracefully (logged, not crashed)."""
    bridge = bridge_env["bridge"]
    mock_transport = bridge_env["mock_transport"]

    # Malformed payload (missing closing brace)
    msg = MockMQTTMessage("RAMSES/TEST/18:123456/rx", '{"msg": "incomplete..."')

    # This should NOT raise an exception
    bridge._handle_mqtt_message(msg)

    # Verify transport was NOT called
    mock_transport.receive_frame.assert_not_called()

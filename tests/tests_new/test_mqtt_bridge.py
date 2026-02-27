"""Tests for the RamsesMqttBridge."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.config_flow import SZ_PORT_NAME, SZ_SERIAL_PORT
from custom_components.ramses_cc.const import (
    CONF_MQTT_HGI_ID,
    CONF_MQTT_USE_HA,
    CONF_RAMSES_RF,
    DOMAIN,
)
from custom_components.ramses_cc.mqtt_bridge import RamsesMqttBridge

TEST_DEVICE_ID = "18:123456"


@pytest.fixture
def mock_protocol() -> MagicMock:
    """Mock an asyncio.Protocol."""
    return MagicMock(spec=asyncio.Protocol)


@pytest.fixture
def mock_mqtt(hass: HomeAssistant) -> Iterator[dict[str, Any]]:
    """Mock the HA MQTT integration methods used by the bridge."""
    # We patch the 'mqtt' module IMPORTED inside mqtt_bridge.py.
    # This ensures we intercept calls even if the real HA MQTT component is loaded.
    with patch("custom_components.ramses_cc.mqtt_bridge.mqtt") as mock_mqtt_module:
        # 1. Setup async_subscribe
        # It must be an AsyncMock (awaitable) that returns a Mock (the unsub callback)
        mock_sub = AsyncMock(return_value=MagicMock())
        mock_mqtt_module.async_subscribe = mock_sub

        # 2. Setup async_publish
        mock_pub = AsyncMock()
        mock_mqtt_module.async_publish = mock_pub

        # 3. Setup connection status
        # This is a standard function (not async) in HA, returns an unsub callback
        mock_conn_status = MagicMock(return_value=MagicMock())
        mock_mqtt_module.async_subscribe_connection_status = mock_conn_status

        yield {
            "subscribe": mock_sub,
            "connection_status": mock_conn_status,
            "publish": mock_pub,
        }


async def test_bridge_init(hass: HomeAssistant) -> None:
    """Test the bridge initialization."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)
    assert bridge.device_id == TEST_DEVICE_ID


async def test_bridge_flow(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test the full flow: Factory -> Subscribe -> Rx -> Tx."""

    # 1. Setup Mock MQTT Config Entry
    mqtt_entry = MockConfigEntry(domain="mqtt", data={"broker": "mock_broker"})
    mqtt_entry.add_to_hass(hass)

    # 2. Setup Ramses Config Entry
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="central_controller",
        data={},
        options={
            CONF_RAMSES_RF: {
                "enforce_known_list": True,
            },
            SZ_SERIAL_PORT: {
                SZ_PORT_NAME: "mqtt://mqtt_host:1883",
            },
            CONF_MQTT_USE_HA: True,
            CONF_MQTT_HGI_ID: TEST_DEVICE_ID,
        },
    )
    entry.add_to_hass(hass)

    # 3. Mock classes
    with (
        patch(
            "custom_components.ramses_cc.coordinator.MqttGateway"
        ) as mock_gateway_cls,
        patch(
            "custom_components.ramses_cc.mqtt_bridge.CallbackTransport"
        ) as mock_transport_cls,
    ):
        # 4. Setup the Gateway Mock
        mock_gateway = mock_gateway_cls.return_value
        mock_gateway.start = AsyncMock()
        mock_gateway.get_state.return_value = ({}, {})

        # Setup the Transport Mock
        mock_transport = mock_transport_cls.return_value

        # 5. Initialize the Integration
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # 6. Get the active coordinator and bridge
        coordinator = hass.data[DOMAIN][entry.entry_id]
        bridge = coordinator.mqtt_bridge
        assert bridge is not None

        # 7. Simulate the wiring
        transport = await bridge.async_transport_factory(mock_protocol)

        # Verify the transport was created
        assert transport == mock_transport
        mock_transport_cls.assert_called_once()

        # 8. Verify Subscriptions
        mock_mqtt["subscribe"].assert_any_call(
            hass,
            f"RAMSES/GATEWAY/{TEST_DEVICE_ID}/rx",
            bridge._handle_rx_message,
            qos=0,
        )
        mock_mqtt["subscribe"].assert_any_call(
            hass,
            f"RAMSES/GATEWAY/{TEST_DEVICE_ID}/cmd/result",
            bridge._handle_cmd_message,
            qos=0,
        )

        # 9. Test INBOUND (MQTT -> Transport)
        rx_call = next(
            call
            for call in mock_mqtt["subscribe"].call_args_list
            if call[0][1].endswith("/rx")
        )
        rx_callback = rx_call[0][2]

        # Simulate an incoming MQTT message
        msg = MagicMock()
        msg.payload = json.dumps(
            {"msg": "RQ --- 18:123456 01:000000 --:------ 0005 002 0000"}
        )

        rx_callback(msg)

        # Verify it was unwrapped and passed to the transport
        expected_frame = "RQ --- 18:123456 01:000000 --:------ 0005 002 0000\r\n"
        mock_transport.receive_frame.assert_called_with(expected_frame)

        # 10. Test OUTBOUND (Transport Writer -> MQTT)
        call_args = mock_transport_cls.call_args[0]
        io_writer = call_args[1]

        # A. Test TX Packet
        tx_frame = "RP --- 01:000000 18:123456 --:------ 0005 002 0000"
        await io_writer(tx_frame)

        expected_topic_tx = f"RAMSES/GATEWAY/{TEST_DEVICE_ID}/tx"
        expected_payload_tx = json.dumps({"msg": tx_frame + "\r\n"})
        mock_mqtt["publish"].assert_called_with(
            hass, expected_topic_tx, expected_payload_tx
        )

        # B. Test Command
        cmd_frame = "!V"
        await io_writer(cmd_frame)

        expected_topic_cmd = f"RAMSES/GATEWAY/{TEST_DEVICE_ID}/cmd/cmd"
        mock_mqtt["publish"].assert_called_with(hass, expected_topic_cmd, cmd_frame)


async def test_bridge_subscriptions_and_errors(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test subscription idempotency and error handling."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)

    # Test 1: Successful subscription
    await bridge.async_transport_factory(mock_protocol)
    assert mock_mqtt["subscribe"].call_count == 2
    mock_mqtt["subscribe"].reset_mock()

    # Test 2: Double subscription (should be ignored due to guards)
    await bridge.async_transport_factory(mock_protocol)
    mock_mqtt["subscribe"].assert_not_called()

    # Test 3: Subscription failure
    # Reset bridge internals to force re-subscription attempt
    bridge._sub_rx = None
    bridge._sub_cmd = None
    mock_mqtt["subscribe"].side_effect = Exception("MQTT Boom")

    with patch("custom_components.ramses_cc.mqtt_bridge._LOGGER") as mock_logger:
        await bridge.async_transport_factory(mock_protocol)
        assert mock_logger.error.call_count >= 1
        assert "Failed to subscribe to MQTT" in mock_logger.error.call_args[0][0]


async def test_bridge_rx_edge_cases(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test RX message handling edge cases."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)
    await bridge.async_transport_factory(mock_protocol)
    # We need the transport mock to verify receive_frame calls
    mock_transport = bridge._transport
    mock_transport.receive_frame = MagicMock()

    # Get the callback
    rx_call = next(
        call
        for call in mock_mqtt["subscribe"].call_args_list
        if call[0][1].endswith("/rx")
    )
    rx_callback = rx_call[0][2]

    # Case 1: Transport is None (should drop message)
    bridge._transport = None
    msg = MagicMock()
    msg.payload = json.dumps({"msg": "test"})
    rx_callback(msg)
    # Restore transport for subsequent tests
    bridge._transport = mock_transport

    # Case 2: Bad JSON
    msg.payload = "Not JSON"
    rx_callback(msg)
    mock_transport.receive_frame.assert_not_called()

    # Case 3: JSON without "msg" key
    msg.payload = json.dumps({"other": "data"})
    rx_callback(msg)
    mock_transport.receive_frame.assert_not_called()

    # Case 4: Payload as bytes (valid)
    msg.payload = json.dumps({"msg": "BYTES"}).encode("utf-8")
    rx_callback(msg)
    mock_transport.receive_frame.assert_called_with("BYTES\r\n")

    # Case 5: Unicode error
    # FIX: UnicodeEncodeError 2nd arg must be str, not bytes
    with patch(
        "custom_components.ramses_cc.mqtt_bridge.json.loads",
        side_effect=UnicodeEncodeError("utf-8", "", 0, 1, "ouch"),
    ):
        rx_callback(msg)  # Should log error, not crash

    # Case 6: Generic Exception
    with patch(
        "custom_components.ramses_cc.mqtt_bridge.json.loads",
        side_effect=ValueError("Boom"),
    ):
        rx_callback(msg)  # Should log exception, not crash

    # Case 7: Empty Payload (Covers line 140)
    msg.payload = b""
    rx_callback(msg)
    mock_transport.receive_frame.assert_called_with("BYTES\r\n")  # Call from Case 4
    mock_transport.receive_frame.reset_mock()
    mock_transport.receive_frame.assert_not_called()


async def test_bridge_cmd_edge_cases(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test CMD message handling edge cases."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)
    await bridge.async_transport_factory(mock_protocol)
    mock_transport = bridge._transport
    mock_transport.receive_frame = MagicMock()

    # Get the callback
    cmd_call = next(
        call
        for call in mock_mqtt["subscribe"].call_args_list
        if call[0][1].endswith("/cmd/result")
    )
    cmd_callback = cmd_call[0][2]
    msg = MagicMock()

    # Case 1: Transport is None
    bridge._transport = None
    msg.payload = json.dumps({"return": "ok"})
    cmd_callback(msg)
    bridge._transport = mock_transport

    # Case 2: Bad JSON
    msg.payload = "{"
    cmd_callback(msg)
    mock_transport.receive_frame.assert_not_called()

    # Case 3: "ramses_esp_eth" replacement
    msg.payload = json.dumps({"return": "# ramses_esp_eth 1.0"})
    cmd_callback(msg)
    mock_transport.receive_frame.assert_called_with("# evofw3 1.0\r\n")

    # Case 4: Missing "#" prefix
    msg.payload = json.dumps({"return": "evofw3 1.0"})
    cmd_callback(msg)
    mock_transport.receive_frame.assert_called_with("# evofw3 1.0\r\n")

    # Case 5: Generic Exception
    with patch(
        "custom_components.ramses_cc.mqtt_bridge.json.loads",
        side_effect=RuntimeError("General Failure"),
    ):
        cmd_callback(msg)  # Should handle gracefully

    # Case 6: Empty Payload (Covers line 178)
    msg.payload = ""
    cmd_callback(msg)
    # Should simply return without doing anything

    # Case 7: Unicode Error (Covers line 205)
    # Force the transport to raise the error, ensuring the try block completes parsing
    mock_transport.receive_frame.side_effect = UnicodeEncodeError(
        "utf-8", "", 0, 1, "ouch"
    )
    msg.payload = json.dumps({"return": "valid"})
    cmd_callback(msg)
    mock_transport.receive_frame.side_effect = None  # Reset


async def test_bridge_writer_errors(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test errors during packet writing."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)

    # We need to capture the io_writer defined inside the factory
    # Pass a valid protocol so we don't crash before assignment
    with patch("custom_components.ramses_cc.mqtt_bridge.CallbackTransport"):
        await bridge.async_transport_factory(mock_protocol)

    # Access the closure via the stored transport or by inspecting the call
    # Since we patched CallbackTransport, we can inspect call_args
    transport_cls = (
        "custom_components.ramses_cc.mqtt_bridge.CallbackTransport"  # For clarity
    )
    with patch(transport_cls) as mock_transport_cls:
        await bridge.async_transport_factory(mock_protocol)
        call_args = mock_transport_cls.call_args[0]
        io_writer = call_args[1]

        # Test TypeError during JSON encoding
        # We patch json.dumps specifically in the mqtt_bridge module
        with patch(
            "custom_components.ramses_cc.mqtt_bridge.json.dumps", side_effect=TypeError
        ) as mock_json:
            await io_writer("TEST_FRAME")
            mock_json.assert_called()


async def test_bridge_connection_status(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test connection status changes."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)
    await bridge.async_transport_factory(mock_protocol)

    status_call = mock_mqtt["connection_status"].call_args
    status_callback = status_call[0][1]

    # Test Online
    status_callback("online")
    # Should publish handshake !V
    expected_topic = f"RAMSES/GATEWAY/{TEST_DEVICE_ID}/cmd/cmd"
    mock_mqtt["publish"].assert_called_with(hass, expected_topic, "!V")

    # Test Offline
    mock_mqtt["publish"].reset_mock()
    status_callback("offline")
    # Should just log, no publish
    mock_mqtt["publish"].assert_not_called()


async def test_bridge_cleanup(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test cleanup and unsubscriptions."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)
    await bridge.async_transport_factory(mock_protocol)

    # Ensure we have mock unsub functions
    unsub_rx = MagicMock()
    unsub_cmd = MagicMock()
    unsub_status = MagicMock()

    bridge._sub_rx = unsub_rx
    bridge._sub_cmd = unsub_cmd
    bridge._sub_status = unsub_status

    bridge.close()

    unsub_rx.assert_called_once()
    unsub_cmd.assert_called_once()
    unsub_status.assert_called_once()


async def test_bridge_handle_cmd_result_int(
    hass: HomeAssistant, mock_mqtt: dict[str, Any], mock_protocol: MagicMock
) -> None:
    """Test handling of integer return codes (maintainer firmware style)."""
    bridge = RamsesMqttBridge(hass, "RAMSES/GATEWAY", TEST_DEVICE_ID)
    await bridge.async_transport_factory(mock_protocol)

    # Mock the transport instance so we can check calls
    bridge._transport = MagicMock()

    # Simulate subscribing to grab the callback
    msg = MagicMock()

    # Scenario 1: !V command returns 0 (int) -> Handled by IF block
    msg.payload = json.dumps({"cmd": "!V", "return": 0})
    bridge._handle_cmd_message(msg)

    # It should have synthesized a fake handshake response
    expected_response = "# evofw3 0.1.0\r\n"
    bridge._transport.receive_frame.assert_called_with(expected_response)

    # Scenario 2: Other command returns int -> Handled by ELSE block (lines 194-197)
    msg.payload = json.dumps({"cmd": "!C", "return": 0})
    bridge._handle_cmd_message(msg)

    # It should convert int to string and wrap it
    # 0 -> "0" -> "# 0" -> "# 0\r\n"
    expected_response_2 = "# 0\r\n"
    bridge._transport.receive_frame.assert_called_with(expected_response_2)

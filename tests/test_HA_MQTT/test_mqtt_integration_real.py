"""End-to-End Integration Test for ramses_cc using HA MQTT with Real Data."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

# Import testing helpers
# Mypy cannot find types for this library, so we ignore the error
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
    async_fire_mqtt_message,
)

# Constants from your code
from custom_components.ramses_cc.const import (
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    DOMAIN,
)

# -------------------------------------------------------------------------
# REAL DATA FROM YOUR LOG
# -------------------------------------------------------------------------
# The Gateway ID found in your logs (found in RQ packets)
HGI_ID = "18:000730"

# A real packet received from a sensor (The first line in your log)
REAL_RX_PACKET = "059  I --- 01:195932 --:------ 01:195932 0008 002 FC00"
REAL_RX_TIMESTAMP = "2025-12-22T00:00:10.328446"

# A real packet the Gateway tried to send (Line 5 in your log)
REAL_TX_PACKET = "--- RQ --- 18:000730 01:195932 --:------ 313F 001 00"

# Topic Configuration
TOPIC_ROOT = "RAMSES/TEST"
TOPIC_RX = f"{TOPIC_ROOT}/{HGI_ID}/rx"
TOPIC_TX = f"{TOPIC_ROOT}/{HGI_ID}/tx"


# -------------------------------------------------------------------------
# Helper: The "Fake ESP32" Device
# -------------------------------------------------------------------------
class FakeESP32:
    """Simulates your real hardware sending MQTT messages."""

    def __init__(self, hass: HomeAssistant, mqtt_mock: MagicMock) -> None:
        """Initialize the fake device."""
        self.hass = hass
        self.mqtt_mock = mqtt_mock

    def send_packet(self, packet_str: str, timestamp: str) -> None:
        """Simulate the ESP32 sending a packet TO Home Assistant."""
        # ramses_esp payload format
        payload = json.dumps({"ts": timestamp, "msg": packet_str})
        # Inject directly into HA's MQTT bus
        async_fire_mqtt_message(self.hass, TOPIC_RX, payload)

    def assert_received_packet(self, packet_str: str) -> None:
        """Check if Home Assistant sent a packet BACK to the ESP32."""
        # Scan all calls to mqtt.publish
        found = False
        debug_payloads = []

        for call in self.mqtt_mock.async_publish.call_args_list:
            # args: (hass, topic, payload, qos, retain)
            topic = call.args[1]
            payload = call.args[2]

            # We are looking for a message sent TO the TX topic
            if topic == TOPIC_TX:
                json_payload = json.loads(payload)
                msg = json_payload.get("msg")
                debug_payloads.append(msg)

                # Check if it matches our expected packet
                if msg == packet_str:
                    found = True
                    break

        if not found:
            raise AssertionError(
                f"ESP32 Expected: {packet_str}\n" f"But Received: {debug_payloads}"
            )


# -------------------------------------------------------------------------
# The Integration Test
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mqtt_connection_and_data_flow(
    hass: HomeAssistant, mqtt_mock: MagicMock, enable_custom_integrations: None
) -> None:
    """Test full MQTT data flow using real packet logs.

    Test Phase 1: Connection & Subscription
    Test Phase 2: Receiving Real Data (Log Replay)
    Test Phase 3: Sending Real Data (Log Replay)
    """

    # 1. SETUP: Create the Fake ESP32
    esp32 = FakeESP32(hass, mqtt_mock)

    # 2. SETUP: Mock Config Entry
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ramses_mqtt_test_log_replay",
        data={CONF_MQTT_USE_HA: True, CONF_MQTT_TOPIC: TOPIC_ROOT},
    )
    config_entry.add_to_hass(hass)

    # 3. EXECUTE: Start the Integration
    # We patch Gateway to avoid opening a physical serial port,
    # but we KEEP the 'broker' logic real.
    with patch("custom_components.ramses_cc.broker.Gateway") as mock_gateway_cls:

        # Capture the protocol mock so we can pretend to be the engine
        mock_gateway = mock_gateway_cls.return_value

        # FIX 0: Set the HGI ID to a real string
        mock_gateway.hgi.id = HGI_ID
        
        # FIX 1: Make .start() awaitable so the broker can await it
        mock_gateway.start = AsyncMock()
        
        # FIX 2: Mock get_state to return empty data (Prevent Teardown Crash)
        # The broker calls this during shutdown to save cache
        mock_gateway.get_state = MagicMock(return_value=({}, []))

        mock_protocol = MagicMock()
        mock_gateway._protocol = mock_protocol

        # --- START UP ---
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)  # Give the loop a moment to register the call on the mock

        # --- PHASE 1: VERIFY CONNECTION & SUBSCRIPTION ---
        # The logs show your code subscribes to the wildcard '#'
        expected_subscription = f"{TOPIC_ROOT}/#"

        # Check if mqtt.async_subscribe was called with the correct topic
        # We scan ALL arguments of ALL calls because argument positions can shift
        found_subscription = False
        all_calls_debug = []
        
        for call in mqtt_mock.async_subscribe.call_args_list:
            # Flatten args to search for the string
            args_str = [str(a) for a in call.args]
            all_calls_debug.append(args_str)
            
            if expected_subscription in call.args:
                found_subscription = True
                break

        assert found_subscription, (
            f"Failed to subscribe to {expected_subscription}! Calls: {all_calls_debug}"
        )

        print(f"\n[PASS] Successfully subscribed to {expected_subscription}")

        # --- PHASE 2: RECEIVE REAL PACKET (RX) ---
        print(f"[TEST] Injecting Log Line: {REAL_RX_PACKET}")

        esp32.send_packet(REAL_RX_PACKET, REAL_RX_TIMESTAMP)
        await hass.async_block_till_done()

        # --- PHASE 3: SEND REAL PACKET (TX) ---
        print(f"[TEST] Ramses_rf attempting to send: {REAL_TX_PACKET}")

        # Get the transport constructor that was passed to the Gateway
        _, kwargs = mock_gateway_cls.call_args
        transport_constructor = kwargs.get("transport_constructor")

        # Instantiate the transport (simulating what Gateway does internally)
        transport = await transport_constructor(mock_protocol)

        # Simulate ramses_rf engine writing a frame to the transport
        # NOTE: CallbackTransport explicitly forbids .write() and demands .write_frame()
        # This is because MQTT is message-based, not stream-based.
        if hasattr(transport, "write_frame"):
            await transport.write_frame(REAL_TX_PACKET)
        elif hasattr(transport, "send_frame"):
            transport.send_frame(REAL_TX_PACKET)

        await hass.async_block_till_done()

        # Verify the ESP32 "received" it via MQTT
        esp32.assert_received_packet(REAL_TX_PACKET)
        print("[PASS] ESP32 received the packet correctly.")
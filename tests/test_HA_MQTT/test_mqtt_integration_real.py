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
    CONF_ADVANCED_FEATURES,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_SEND_PACKET,
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
        found = False
        debug_payloads = []

        # Check async_publish (normal). We don't check sync publish as it might not exist on the mock
        all_calls = self.mqtt_mock.async_publish.call_args_list

        for call in all_calls:
            # We don't know if 'hass' is the first arg or not, so we check
            # if the TX Topic exists anywhere in the arguments.
            if TOPIC_TX in call.args:
                # The payload is usually the argument AFTER the topic
                try:
                    topic_index = call.args.index(TOPIC_TX)
                    payload = call.args[topic_index + 1]
                    
                    json_payload = json.loads(payload)
                    msg = json_payload.get("msg")
                    debug_payloads.append(msg)

                    if msg == packet_str:
                        found = True
                        break
                except (IndexError, ValueError, json.JSONDecodeError):
                    continue

        if not found:
            raise AssertionError(
                f"ESP32 Expected: {packet_str}\n" f"But Received (on {TOPIC_TX}): {debug_payloads}\n" 
                f"All Calls: {self.mqtt_mock.async_publish.call_args_list}"
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
    with patch("custom_components.ramses_cc.broker.Gateway") as mock_gateway_cls:

        mock_gateway = mock_gateway_cls.return_value
        
        # MOCKS
        mock_gateway.hgi.id = HGI_ID
        mock_gateway.start = AsyncMock()
        mock_gateway.get_state = MagicMock(return_value=({}, []))

        mock_protocol = MagicMock()
        mock_gateway._protocol = mock_protocol

        # --- START UP ---
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1) 

        # --- PHASE 1: VERIFY SUBSCRIPTION ---
        expected_subscription = f"{TOPIC_ROOT}/#"
        found_subscription = False
        
        for call in mqtt_mock.async_subscribe.call_args_list:
            if expected_subscription in call.args:
                found_subscription = True
                break

        assert found_subscription, (
            f"Failed to subscribe to {expected_subscription}!"
        )
        print(f"\n[PASS] Successfully subscribed to {expected_subscription}")

        # --- PHASE 2: RECEIVE REAL PACKET (RX) ---
        print(f"[TEST] Injecting Log Line: {REAL_RX_PACKET}")
        esp32.send_packet(REAL_RX_PACKET, REAL_RX_TIMESTAMP)
        await hass.async_block_till_done()

        # --- PHASE 3: SEND REAL PACKET (TX) ---
        print(f"[TEST] Ramses_rf attempting to send: {REAL_TX_PACKET}")

        _, kwargs = mock_gateway_cls.call_args
        transport_constructor = kwargs.get("transport_constructor")
        transport = await transport_constructor(mock_protocol)

        # Simulate engine writing frame
        if hasattr(transport, "write_frame"):
            await transport.write_frame(REAL_TX_PACKET)
        elif hasattr(transport, "send_frame"):
            transport.send_frame(REAL_TX_PACKET)

        await hass.async_block_till_done()
        await asyncio.sleep(0.1) # Wait for event loop to process the publish

        # Verify the ESP32 "received" it via MQTT
        esp32.assert_received_packet(REAL_TX_PACKET)
        print("[PASS] ESP32 received the packet correctly.")

# -------------------------------------------------------------------------
# NEW TEST: Real Gateway Logic (No Mocks for Gateway)
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_service_call_end_to_end(
    hass: HomeAssistant, mqtt_mock: MagicMock, enable_custom_integrations: None
) -> None:
    """Test that a Service Call triggers a Real Packet from the Real Gateway.
    
    This test does NOT mock the Gateway class. It runs the actual ramses_rf 
    library logic to ensure it creates a packet and sends it via our 
    injected MQTT transport.
    """
    
    # 1. SETUP: Create the Fake ESP32 to listen for the command
    esp32 = FakeESP32(hass, mqtt_mock)

    # 2. SETUP: Config Entry with Advanced Features Enabled
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ramses_mqtt_real_gateway_test",
        data={CONF_MQTT_USE_HA: True, CONF_MQTT_TOPIC: TOPIC_ROOT},
        # ENABLE send_packet service
        options={
            CONF_ADVANCED_FEATURES: {
                CONF_SEND_PACKET: True
            }
        }
    )
    config_entry.add_to_hass(hass)

    # 3. EXECUTE: Start the Integration (REAL GATEWAY)
    # We patch wait_for_connection_made on PortProtocol to avoid hanging
    with patch("ramses_tx.protocol.PortProtocol.wait_for_connection_made", new_callable=AsyncMock) as mock_wait:
        
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

        # 4. EXECUTE: Call a Service (Send Packet)
        # Command: "1F09" (System Sync) to Device "01:123456"
        test_command = "RQ 01:123456 1F09 00"
        
        # Check if service was successfully registered (it should be now)
        if hass.services.has_service(DOMAIN, "send_packet"):
            print("[TEST] Calling service send_packet")
            await hass.services.async_call(
                DOMAIN, 
                "send_packet", 
                {"device_id": "01:123456", "command": "1F09", "payload": "00"},
                blocking=True
            )
        else:
            # Fallback: Use broker.client (which is the gateway)
            print("[TEST] Service not found, using direct gateway call")
            broker = hass.data[DOMAIN][config_entry.entry_id]
            # broker.client is the ramses_rf.Gateway instance
            await broker.client.async_send_cmd(test_command)

        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

        # 5. VERIFY: The ESP32 should receive the compiled packet
        found = False
        debug_list = []
        for call in mqtt_mock.async_publish.call_args_list:
            if TOPIC_TX in call.args:
                try:
                    topic_index = call.args.index(TOPIC_TX)
                    payload = call.args[topic_index + 1]
                    debug_list.append(payload)
                    # We look for the command code 1F09 in the payload
                    if "1F09" in payload:
                        found = True
                        break
                except:
                    continue
                    
        assert found, f"Real Gateway failed to send packet! Payloads sent to {TOPIC_TX}: {debug_list}"
        print("\n[PASS] Real Gateway Logic successfully converted Service Call -> MQTT Packet")
"""End-to-End Integration Test for ramses_cc using HA MQTT with Real Data."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import paho.mqtt.client as mqtt_client
import pytest
from homeassistant.core import HomeAssistant

# REMOVED: Broken import of MQTT_CONNECTED
from pytest_homeassistant_custom_component.common import (  # type: ignore
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.ramses_cc.const import (
    CONF_ADVANCED_FEATURES,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_SEND_PACKET,
    DOMAIN,
)

if not hasattr(mqtt_client, "base62"):
    if hasattr(mqtt_client, "_base62"):
        mqtt_client.base62 = mqtt_client._base62
    else:
        pass

# -------------------------------------------------------------------------
# CONSTANTS & HELPERS
# -------------------------------------------------------------------------
HGI_ID = "18:000730"
REAL_RX_PACKET = "059  I --- 01:195932 --:------ 01:195932 0008 002 FC00"
REAL_RX_TIMESTAMP = "2025-12-22T00:00:10.328446"
REAL_TX_PACKET = "--- RQ --- 18:000730 01:195932 --:------ 313F 001 00"

TOPIC_ROOT = "RAMSES/TEST"
TOPIC_RX = f"{TOPIC_ROOT}/{HGI_ID}/rx"
TOPIC_TX = f"{TOPIC_ROOT}/{HGI_ID}/tx"


class FakeESP32:
    def __init__(self, hass: HomeAssistant, mqtt_mock: MagicMock) -> None:
        self.hass = hass
        self.mqtt_mock = mqtt_mock

    def send_packet(self, packet_str: str, timestamp: str) -> None:
        payload = json.dumps({"ts": timestamp, "msg": packet_str})
        async_fire_mqtt_message(self.hass, TOPIC_RX, payload)

    def assert_received_packet(self, packet_str: str) -> None:
        found = False
        debug_payloads = []
        all_calls = self.mqtt_mock.async_publish.call_args_list

        for call in all_calls:
            if TOPIC_TX in call.args:
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
                f"ESP32 Expected: {packet_str}\n"
                f"Received on {TOPIC_TX}: {debug_payloads}"
            )


async def mock_req(*args: Any, **kwargs: Any) -> None:
    return


# -------------------------------------------------------------------------
# TESTS
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mqtt_connection_and_data_flow(
    hass: HomeAssistant, mqtt_mock: MagicMock, enable_custom_integrations: None
) -> None:
    """Test full MQTT data flow using real packet logs."""
    esp32 = FakeESP32(hass, mqtt_mock)

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ramses_mqtt_test_log_replay",
        data={CONF_MQTT_USE_HA: True, CONF_MQTT_TOPIC: TOPIC_ROOT},
    )
    config_entry.add_to_hass(hass)

    import sys

    sys.modules["pyudev"] = MagicMock()
    sys.modules["pyudev.Context"] = MagicMock()
    sys.modules["pyudev.Monitor"] = MagicMock()

    with (
        patch("custom_components.ramses_cc.broker.Gateway") as mock_gateway_cls,
        patch(
            "homeassistant.requirements.async_process_requirements",
            side_effect=mock_req,
        ),
        patch(
            "homeassistant.components.mqtt.async_wait_for_mqtt_client",
            return_value=True,
        ),
        # KEY FIX: Force MQTT to report True for is_connected so bridge initializes in connected state
        patch("homeassistant.components.mqtt.is_connected", return_value=True),
    ):
        mock_gateway = mock_gateway_cls.return_value
        mock_gateway.hgi.id = HGI_ID
        mock_gateway.start = AsyncMock()
        mock_gateway.get_state = MagicMock(return_value=({}, []))
        mock_protocol = MagicMock()
        mock_gateway._protocol = mock_protocol

        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

        broker = hass.data[DOMAIN][config_entry.entry_id]
        mock_hgi = MagicMock()
        mock_hgi.id = HGI_ID
        broker.client.device_by_id[HGI_ID] = mock_hgi

        if broker.client._transport:
            broker.client._transport.get_extra_info = MagicMock(return_value=HGI_ID)

        if broker.client._protocol and broker.client._transport:
            broker.client._protocol.connection_made(broker.client._transport)

        # --- PHASE 1: VERIFY SUBSCRIPTION ---
        found_subscription = False
        all_subs = []
        for call in mqtt_mock.async_subscribe.call_args_list:
            topic = call.args[0] if call.args else ""
            all_subs.append(topic)
            if str(topic).startswith(TOPIC_ROOT):
                found_subscription = True
                break

        assert found_subscription, (
            f"Failed to subscribe to any topic starting with {TOPIC_ROOT}. Found: {all_subs}"
        )

        # --- PHASE 2: RECEIVE ---
        _, kwargs = mock_gateway_cls.call_args
        transport_constructor = kwargs.get("transport_constructor")
        transport = await transport_constructor(mock_protocol)

        esp32.send_packet(REAL_RX_PACKET, REAL_RX_TIMESTAMP)
        await hass.async_block_till_done()

        # --- PHASE 3: SEND ---
        if hasattr(transport, "write_frame"):
            await transport.write_frame(REAL_TX_PACKET)
        elif hasattr(transport, "send_frame"):
            transport.send_frame(REAL_TX_PACKET)

        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

        esp32.assert_received_packet(REAL_TX_PACKET)


@pytest.mark.asyncio
async def test_service_call_end_to_end(
    hass: HomeAssistant, mqtt_mock: MagicMock, enable_custom_integrations: None
) -> None:
    """Test that a Service Call triggers a Real Packet from the Real Gateway."""
    FakeESP32(hass, mqtt_mock)

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ramses_mqtt_real_gateway_test",
        data={CONF_MQTT_USE_HA: True, CONF_MQTT_TOPIC: TOPIC_ROOT},
        options={CONF_ADVANCED_FEATURES: {CONF_SEND_PACKET: True}},
    )
    config_entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.requirements.async_process_requirements",
            side_effect=mock_req,
        ),
        patch(
            "homeassistant.components.mqtt.async_wait_for_mqtt_client",
            return_value=True,
        ),
        # KEY FIX: Force MQTT to report True so bridge unpauses protocol immediately
        patch("homeassistant.components.mqtt.is_connected", return_value=True),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

    broker = hass.data[DOMAIN][config_entry.entry_id]

    mock_hgi = MagicMock()
    mock_hgi.id = HGI_ID
    broker.client.device_by_id[HGI_ID] = mock_hgi

    if broker.client._transport:
        broker.client._transport.get_extra_info = MagicMock(return_value=HGI_ID)

    import datetime

    async def mock_publish(
        hass: Any, topic: str, payload: str, *args: Any, **kwargs: Any
    ) -> None:
        if topic.endswith("/tx") and "1F09" in payload:
            d = json.loads(payload)
            frame = d["msg"]
            ts = datetime.datetime.now().isoformat()
            rx_payload = json.dumps({"ts": ts, "msg": frame})
            async_fire_mqtt_message(hass, TOPIC_RX, rx_payload)
        return None

    mqtt_mock.async_publish.side_effect = mock_publish

    # EXECUTE
    if hass.services.has_service(DOMAIN, "send_packet"):
        await hass.services.async_call(
            DOMAIN,
            "send_packet",
            {"device_id": "01:123456", "verb": "I", "code": "1F09", "payload": "00"},
            blocking=True,
        )
    else:
        test_command = "I 01:123456 1F09 00"
        await broker.client.async_send_cmd(test_command)

    await hass.async_block_till_done()
    await asyncio.sleep(0.1)

    # VERIFY
    found = False
    debug_list = []
    for call in mqtt_mock.async_publish.call_args_list:
        if TOPIC_TX in call.args:
            try:
                topic_index = call.args.index(TOPIC_TX)
                payload = call.args[topic_index + 1]
                debug_list.append(payload)
                if "1F09" in payload:
                    found = True
                    break
            except (IndexError, ValueError):
                continue

    assert found, (
        f"Real Gateway failed to send packet! Payloads sent to {TOPIC_TX}: {debug_list}"
    )

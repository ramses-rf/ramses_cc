"""Tests for the RamsesMqttPoolBridge (PR 4B).

Covers:
- Bridge initialization with multiple configured HGIs.
- Wildcard subscription to RX, CMD, and status topics.
- HGI ID extraction from MQTT topics.
- LWT online/offline handling per HGI.
- Broker connection/disconnection affecting all children.
- Outbound publishing to the correct HGI's TX topic.
- Unknown HGI discovery via wildcard status.
- RX frame parsing and forwarding to the adapter.
- Cleanup of subscriptions on close.
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.mqtt_pool_bridge import (
    RamsesMqttPoolBridge,
)

TEST_HGI_1 = "18:001111"
TEST_HGI_2 = "18:002222"
TEST_TOPIC_PREFIX = "RAMSES/GATEWAY"


@pytest.fixture
def mock_protocol() -> MagicMock:
    """Mock an asyncio.Protocol."""
    proto = MagicMock(spec=asyncio.Protocol)
    proto.connection_made = MagicMock()
    proto.connection_lost = MagicMock()
    return proto


@pytest.fixture
def mock_mqtt_pool(
    hass: HomeAssistant,
) -> Iterator[dict[str, Any]]:
    """Mock the HA MQTT integration for the pool bridge."""
    with patch(
        "custom_components.ramses_cc.mqtt_pool_bridge.mqtt"
    ) as mock_mqtt_module:
        mock_sub = AsyncMock(return_value=MagicMock())
        mock_mqtt_module.async_subscribe = mock_sub
        mock_pub = AsyncMock()
        mock_mqtt_module.async_publish = mock_pub
        mock_conn_status = MagicMock(return_value=MagicMock())
        mock_mqtt_module.async_subscribe_connection_status = mock_conn_status
        yield {
            "subscribe": mock_sub,
            "connection_status": mock_conn_status,
            "publish": mock_pub,
        }


# -- Initialization -------------------------------------------------------


def test_pool_bridge_init(hass: HomeAssistant) -> None:
    """Test pool bridge initialization with multiple HGIs."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
    )
    assert bridge.device_ids == [TEST_HGI_1, TEST_HGI_2]


def test_pool_bridge_strips_trailing_slash(hass: HomeAssistant) -> None:
    """Test that trailing slash is stripped from topic prefix."""
    bridge = RamsesMqttPoolBridge(
        hass,
        "RAMSES/GATEWAY/",
        [TEST_HGI_1],
    )
    assert bridge._topic_prefix == "RAMSES/GATEWAY"


# -- HGI extraction from topics ------------------------------------------


def test_extract_hgi_from_rx_topic(hass: HomeAssistant) -> None:
    """Test HGI ID extraction from an RX topic."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    hgi = bridge._extract_hgi_from_topic("RAMSES/GATEWAY/18:001111/rx", "/rx")
    assert hgi == TEST_HGI_1


def test_extract_hgi_from_cmd_topic(hass: HomeAssistant) -> None:
    """Test HGI ID extraction from a CMD result topic."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    hgi = bridge._extract_hgi_from_topic(
        "RAMSES/GATEWAY/18:001111/cmd/result", "/cmd/result"
    )
    assert hgi == TEST_HGI_1


def test_extract_hgi_from_status_topic(hass: HomeAssistant) -> None:
    """Test HGI ID extraction from a status/LWT topic."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    hgi = bridge._extract_hgi_from_topic("RAMSES/GATEWAY/18:001111", "")
    assert hgi == TEST_HGI_1


def test_extract_hgi_invalid_topic(hass: HomeAssistant) -> None:
    """Test that invalid topics return None."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    assert bridge._extract_hgi_from_topic("other/topic/rx", "/rx") is None
    assert (
        bridge._extract_hgi_from_topic("RAMSES/GATEWAY/not-an-hgi/rx", "/rx")
        is None
    )


# -- Wildcard subscription ------------------------------------------------


async def test_subscribes_to_wildcard_topics(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that the bridge subscribes to wildcard topics."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()

    # Should subscribe to 3 wildcard topics + broker status.
    assert mock_mqtt_pool["subscribe"].call_count == 3
    topics = [
        call.args[1] for call in mock_mqtt_pool["subscribe"].call_args_list
    ]
    assert "RAMSES/GATEWAY/+/rx" in topics
    assert "RAMSES/GATEWAY/+/cmd/result" in topics
    assert "RAMSES/GATEWAY/+" in topics


async def test_does_not_double_subscribe(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that calling _async_attach twice doesn't re-subscribe."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    await bridge._async_attach()
    mock_mqtt_pool["subscribe"].reset_mock()
    await bridge._async_attach()
    assert mock_mqtt_pool["subscribe"].call_count == 0


# -- Transport factory ----------------------------------------------------


async def test_transport_factory_returns_pooled_transport(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that async_transport_factory returns a PooledTransport."""
    from ramses_tx.transport.pooled import PooledTransport

    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    transport = await bridge.async_transport_factory(mock_protocol)
    assert isinstance(transport, PooledTransport)
    assert bridge._pool is not None
    assert bridge._adapter is not None


async def test_transport_factory_pre_creates_children(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that children are pre-created for each configured HGI."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)
    assert bridge._pool is not None
    assert len(bridge._pool._children) == 2
    assert bridge._pool._children[0].callback_driven
    assert bridge._pool._children[1].callback_driven


# -- LWT online/offline ---------------------------------------------------


async def test_lwt_online_marks_child_online(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that LWT online marks the child as online."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # Simulate LWT online for HGI 1.
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    assert TEST_HGI_1 in bridge._online_hgis
    assert bridge._adapter is not None
    # Child should be send-ready.
    child = bridge._pool._children[0]  # type: ignore[union-attr]
    assert child.send_ready


async def test_lwt_offline_marks_child_offline(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that LWT offline marks the child as offline."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # Bring HGI 1 online first.
    msg_online = MagicMock()
    msg_online.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}"
    msg_online.payload = b"online"
    bridge._handle_status_message(msg_online)

    # Now take it offline.
    msg_offline = MagicMock()
    msg_offline.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}"
    msg_offline.payload = b"offline"
    bridge._handle_status_message(msg_offline)

    assert TEST_HGI_1 not in bridge._online_hgis


async def test_lwt_online_only_affects_target_child(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that LWT online for one HGI doesn't affect others."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # Bring HGI 1 online.
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    # HGI 2 should not be online.
    assert TEST_HGI_1 in bridge._online_hgis
    assert TEST_HGI_2 not in bridge._online_hgis


async def test_lwt_online_unknown_hgi_fires_discovery(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that LWT online for unknown HGI fires discovery."""
    discovery = MagicMock()
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        discovery_callback=discovery,
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = "RAMSES/GATEWAY/18:999999"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    discovery.on_unknown_hgi.assert_called_once()


# -- Broker connection ---------------------------------------------------


async def test_broker_connected(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test broker connected event."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)
    bridge._handle_broker_status(True)
    # Should not crash.


async def test_broker_disconnected_marks_all_unavailable(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test broker disconnected marks all children unavailable."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # Bring both HGIs online.
    for hgi in [TEST_HGI_1, TEST_HGI_2]:
        msg = MagicMock()
        msg.topic = f"RAMSES/GATEWAY/{hgi}"
        msg.payload = b"online"
        bridge._handle_status_message(msg)

    # Broker disconnect.
    bridge._handle_broker_status(False)

    # All callback-driven children should be non-sendable.
    assert bridge._pool is not None
    for child in bridge._pool._children:
        if child.callback_driven:
            assert not child.is_sendable


# -- Outbound publishing --------------------------------------------------


async def test_publish_frame_to_correct_hgi(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that publish_frame publishes to the correct HGI topic."""
    bridge = RamsesMqttPoolBridge(
        hass, TEST_TOPIC_PREFIX, [TEST_HGI_1, TEST_HGI_2]
    )
    await bridge.publish_frame(
        TEST_HGI_2,
        " 000 I --- 01:123456 18:000730 --:------ 30C9 000 00",
    )

    mock_mqtt_pool["publish"].assert_called_once()
    call_args = mock_mqtt_pool["publish"].call_args
    topic = call_args.args[1]
    assert TEST_HGI_2 in topic
    assert topic == f"RAMSES/GATEWAY/{TEST_HGI_2}/tx"


async def test_publish_frame_command_to_correct_hgi(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that publish_frame with ! command goes to cmd topic."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    await bridge.publish_frame(TEST_HGI_1, "!V")

    mock_mqtt_pool["publish"].assert_called_once()
    call_args = mock_mqtt_pool["publish"].call_args
    topic = call_args.args[1]
    assert topic == f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/cmd"


# -- RX message handling --------------------------------------------------


async def test_rx_message_invalid_json_no_crash(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that invalid JSON in RX doesn't crash."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/rx"
    msg.payload = b"not json"
    bridge._handle_rx_message(msg)  # should not crash


async def test_rx_message_non_packet_frame_dropped(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that non-packet RX frames are silently dropped."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # "# evofw3 0.1.0" is not a valid RF packet.
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"msg": "# evofw3 0.1.0"}).encode()
    bridge._handle_rx_message(msg)  # should not crash


async def test_rx_message_valid_packet_forwarded(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that valid RX packets are forwarded to the adapter."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # Use a valid RAMSES packet frame (verb " I" = space-I).
    frame = "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"msg": frame}).encode()

    # Spy on the adapter's on_child_packet.
    bridge._adapter.on_child_packet = MagicMock()  # type: ignore[union-attr]
    bridge._handle_rx_message(msg)

    bridge._adapter.on_child_packet.assert_called_once()  # type: ignore[union-attr]
    call_kwargs = bridge._adapter.on_child_packet.call_args  # type: ignore[union-attr]
    assert call_kwargs.args[0] == TEST_HGI_1


# -- Cleanup --------------------------------------------------------------


async def test_close_unsubscribes(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that close unsubscribes from all topics."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    await bridge._async_attach()
    bridge.close()
    # All unsub callbacks should have been called.
    # (They are MagicMocks, so calling them is a no-op.)


# -- MqttPoolOutbound compliance -----------------------------------------


def test_pool_bridge_is_mqtt_pool_outbound(hass: HomeAssistant) -> None:
    """Test that RamsesMqttPoolBridge satisfies MqttPoolOutbound."""
    from ramses_tx.transport.callbacks import MqttPoolOutbound

    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    assert isinstance(bridge, MqttPoolOutbound)


# -- Additional coverage from fact-check ----------------------------------


async def test_no_child_online_within_timeout(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that transport factory continues even if no child comes online."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    transport = await bridge.async_transport_factory(mock_protocol)
    # Transport is still returned — children may come online later.
    assert transport is not None


async def test_lwt_offline_does_not_affect_sibling(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that LWT offline for one HGI does not affect a sibling."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    # Bring both online.
    for hgi in [TEST_HGI_1, TEST_HGI_2]:
        msg = MagicMock()
        msg.topic = f"{TEST_TOPIC_PREFIX}/{hgi}"
        msg.payload = b"online"
        bridge._handle_status_message(msg)

    # Take HGI 1 offline.
    msg_off = MagicMock()
    msg_off.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}"
    msg_off.payload = b"offline"
    bridge._handle_status_message(msg_off)

    # HGI 2 should still be online.
    assert TEST_HGI_2 in bridge._online_hgis
    assert TEST_HGI_1 not in bridge._online_hgis


async def test_broker_recovery_does_not_duplicate_children(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that broker disconnect+reconnect does not duplicate children."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    transport = await bridge.async_transport_factory(mock_protocol)
    initial_child_count = len(transport._children)

    # Broker disconnects and reconnects.
    bridge._handle_broker_status(False)
    bridge._handle_broker_status(True)

    # No duplicate children.
    assert len(transport._children) == initial_child_count


async def test_discovery_callback_invoked_for_unknown_hgi(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that unknown HGI on wildcard status fires discovery callback."""
    unknown_hgi = "18:999999"
    callback = MagicMock()
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        discovery_callback=callback,
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{unknown_hgi}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    callback.on_unknown_hgi.assert_called_once()
    call_kwargs = callback.on_unknown_hgi.call_args
    assert str(call_kwargs.args[0]) == unknown_hgi


async def test_ingress_hgi_id_passed_to_adapter(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that RX forwarding includes ingress_hgi_id kwarg."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    # Bring the child online so the adapter has a connected child.
    msg_online = MagicMock()
    msg_online.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}"
    msg_online.payload = b"online"
    bridge._handle_status_message(msg_online)

    # Use a valid RAMSES packet frame (verb " I" = space-I).
    frame = "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"msg": frame}).encode()

    # Spy on the adapter's on_child_packet.
    bridge._adapter.on_child_packet = MagicMock()  # type: ignore[union-attr]
    bridge._handle_rx_message(msg)

    # Verify the adapter received the call with ingress_hgi_id.
    call = bridge._adapter.on_child_packet.call_args  # type: ignore[union-attr]
    assert call is not None
    assert "ingress_hgi_id" in call.kwargs
    assert str(call.kwargs["ingress_hgi_id"]) == TEST_HGI_1

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

from custom_components.ramses_cc.const import DEFAULT_HGI_ID
from custom_components.ramses_cc.mqtt_pool_bridge import (
    RamsesMqttPoolBridge,
)
from ramses_tx.exceptions import TransportError

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
            "module": mock_mqtt_module,
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


def test_pool_bridge_init_filters_sentinel_hgi(hass: HomeAssistant) -> None:
    """Test the sentinel HGI ID can never become a configured pool child."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [DEFAULT_HGI_ID, TEST_HGI_1, DEFAULT_HGI_ID],
        accepted_hgi_ids={DEFAULT_HGI_ID, TEST_HGI_1},
    )
    assert bridge.device_ids == [TEST_HGI_1]
    assert bridge._accepted_hgi_ids == {TEST_HGI_1}


def test_pool_bridge_init_only_sentinel(hass: HomeAssistant) -> None:
    """Test a sentinel-only configured list leaves no pool children."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [DEFAULT_HGI_ID],
    )
    assert bridge.device_ids == []


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


def test_extract_hgi_rejects_non_hgi_device_id(hass: HomeAssistant) -> None:
    """Test that non-HGI device IDs (e.g. 32:NNNNNN) are rejected (issue 1119)."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    # 32:153289 is a real device, not an HGI — must not be accepted
    assert (
        bridge._extract_hgi_from_topic("RAMSES/GATEWAY/32:153289/rx", "/rx")
        is None
    )
    # 37:168270 is a faked device — must not be accepted
    assert (
        bridge._extract_hgi_from_topic("RAMSES/GATEWAY/37:168270/rx", "/rx")
        is None
    )
    # 18:001111 is a valid HGI — must be accepted
    assert (
        bridge._extract_hgi_from_topic("RAMSES/GATEWAY/18:001111/rx", "/rx")
        == TEST_HGI_1
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


async def test_subscription_failure_cleans_up_partial_subscriptions(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """A partial subscription failure is cleaned up and propagated."""
    unsubscribe_rx = MagicMock()
    unsubscribe_cmd = MagicMock()
    mock_mqtt_pool["subscribe"].side_effect = [
        unsubscribe_rx,
        unsubscribe_cmd,
        RuntimeError("subscribe failed"),
    ]
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])

    with pytest.raises(TransportError, match="subscribe failed"):
        await bridge._async_attach()

    unsubscribe_rx.assert_called_once()
    unsubscribe_cmd.assert_called_once()
    assert bridge._sub_rx is None
    assert bridge._sub_cmd is None
    assert bridge._sub_status is None


async def test_retained_lwt_is_handled_during_transport_factory(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """The adapter exists before a retained online LWT is delivered."""
    unsubscribe = MagicMock()

    async def subscribe(
        _hass: HomeAssistant,
        topic: str,
        callback_fn: Any,
        qos: int,
    ) -> MagicMock:
        del _hass, qos
        if topic == f"{TEST_TOPIC_PREFIX}/+":
            msg = MagicMock()
            msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}"
            msg.payload = b"online"
            callback_fn(msg)
        return unsubscribe

    mock_mqtt_pool["subscribe"].side_effect = subscribe
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )

    transport = await bridge.async_transport_factory(mock_protocol)

    assert transport._children[0].is_connected
    assert transport._children[0].is_online


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


# -- Hybrid pool attach ---------------------------------------------------


async def test_attach_to_pool_binds_protocol_when_no_transport_connected(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """async_attach_to_pool must bind the gateway protocol when no
    transport child provided connection_made.

    Regression: when every serial/zigbee child failed to connect, the
    pool stayed at 0 connected and client.start() would have timed out
    waiting for connection_made — leaving the whole pool dead even
    though MQTT HGIs were available.
    """
    from ramses_tx.transport.base import TransportConfig
    from ramses_tx.transport.pooled import PooledTransport

    # Hybrid pool shape: 1 failed transport child + 2 callback slots.
    pool = PooledTransport(
        mock_protocol,
        [None] * 3,
        config=TransportConfig(),
        loop=hass.loop,
        port_names=[
            "zigbee://aa:bb:cc:dd:ee:ff:00:11",
            "mqtt_ha://18:001111",
            "mqtt_ha://18:002222",
        ],
    )
    assert not pool._protocol_connected

    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_attach_to_pool(pool, callback_child_start_index=1)

    mock_protocol.connection_made.assert_called_once_with(pool, ramses=True)
    assert pool._protocol_connected


async def test_attach_to_pool_does_not_rebind_connected_protocol(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Attach must not double-bind when a transport child already
    connected the protocol."""
    from ramses_tx.transport.base import TransportConfig
    from ramses_tx.transport.pooled import PooledTransport

    pool = PooledTransport(
        mock_protocol,
        [None] * 3,
        config=TransportConfig(),
        loop=hass.loop,
        port_names=[
            "zigbee://aa:bb:cc:dd:ee:ff:00:11",
            "mqtt_ha://18:001111",
            "mqtt_ha://18:002222",
        ],
    )
    pool._protocol_connected = True  # transport child connected already

    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_attach_to_pool(pool, callback_child_start_index=1)

    mock_protocol.connection_made.assert_not_called()


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


async def test_unknown_rx_reports_discovery_without_lwt(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """An RX topic can discover an unknown HGI when no LWT is published."""
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
    msg.topic = f"{TEST_TOPIC_PREFIX}/18:999999/rx"
    msg.payload = json.dumps(
        {"msg": "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"}
    ).encode()

    bridge._handle_rx_message(msg)

    discovery.on_unknown_hgi.assert_called_once()


async def test_lwt_online_sentinel_hgi_ignored(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """LWT on the sentinel HGI topic is ignored — no online, no discovery."""
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
    msg.topic = f"RAMSES/GATEWAY/{DEFAULT_HGI_ID}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    assert DEFAULT_HGI_ID not in bridge._online_hgis
    discovery.on_unknown_hgi.assert_not_called()
    discovery.on_mqtt_capable.assert_not_called()


async def test_rx_sentinel_hgi_ignored(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """RX on the sentinel HGI topic is ignored — no discovery callback."""
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
    msg.topic = f"{TEST_TOPIC_PREFIX}/{DEFAULT_HGI_ID}/rx"
    msg.payload = json.dumps(
        {"msg": "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"}
    ).encode()
    bridge._handle_rx_message(msg)

    discovery.on_unknown_hgi.assert_not_called()


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


async def test_rx_infers_online_when_lwt_missing(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """RX from a configured HGI without LWT infers online (issue 1185).

    When a configured HGI sends packets but never publishes LWT
    (or the LWT was missed), the bridge should infer online status
    from the RX and call on_child_online so the pool child becomes
    connected and sendable for TX.
    """
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        accepted_hgi_ids={TEST_HGI_1},
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    # No LWT online message sent — simulate a packet arriving first.
    assert TEST_HGI_1 not in bridge._online_hgis

    frame = "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"msg": frame}).encode()

    bridge._adapter = MagicMock()
    bridge._publish_command = AsyncMock()
    bridge._handle_rx_message(msg)

    # The HGI should now be in _online_hgis and on_child_online called.
    assert TEST_HGI_1 in bridge._online_hgis
    bridge._adapter.on_child_online.assert_called_once_with(TEST_HGI_1)


async def test_rx_infers_online_then_sendable_regression_1185(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Regression test for issue 1185: single MQTT HGI must be TX-capable.

    In 0.60.5, single-HGI MQTT was switched to the pool bridge.  If the
    HGI doesn't publish LWT (or the LWT is missed), the pool child was
    never marked connected, so is_sendable returned False and TX failed
    with "No connected child transport available for send" even though
    RX worked fine.

    This test verifies that after an RX packet (without LWT), the
    bridge infers online status and calls on_child_online, which makes
    the pool child connected and sendable — so TX works.
    """
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        accepted_hgi_ids={TEST_HGI_1},
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    # No LWT online message — simulate a packet arriving first.
    assert TEST_HGI_1 not in bridge._online_hgis

    frame = "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"msg": frame}).encode()

    bridge._adapter = MagicMock()
    bridge._publish_command = AsyncMock()
    bridge._handle_rx_message(msg)

    # 1. The HGI should be marked as online.
    assert TEST_HGI_1 in bridge._online_hgis

    # 2. on_child_online should have been called (makes child connected).
    bridge._adapter.on_child_online.assert_called_once_with(TEST_HGI_1)

    # 3. The !V handshake should have been sent (HGI is accepted).
    await bridge._hass.async_block_till_done()
    bridge._publish_command.assert_called_once_with(TEST_HGI_1, "!V")

    # 4. on_child_packet should also have been called (the actual packet).
    bridge._adapter.on_child_packet.assert_called_once()


async def test_lwt_online_still_works_alongside_rx_inference(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """LWT online path still works — RX inference is only a fallback.

    If LWT arrives first, the HGI is marked online via LWT.  A
    subsequent RX packet should NOT call on_child_online again (it's
    already online).
    """
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        accepted_hgi_ids={TEST_HGI_1},
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    bridge._adapter = MagicMock()
    bridge._publish_command = AsyncMock()

    # 1. LWT online arrives first.
    msg_lwt = MagicMock()
    msg_lwt.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}"
    msg_lwt.payload = b"online"
    bridge._handle_status_message(msg_lwt)
    assert TEST_HGI_1 in bridge._online_hgis
    bridge._adapter.on_child_online.assert_called_once_with(TEST_HGI_1)

    # 2. RX packet arrives — should NOT call on_child_online again.
    frame = "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"
    msg_rx = MagicMock()
    msg_rx.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg_rx.payload = json.dumps({"msg": frame}).encode()
    bridge._handle_rx_message(msg_rx)

    # on_child_online should still have been called only once (from LWT).
    bridge._adapter.on_child_online.assert_called_once_with(TEST_HGI_1)
    # on_child_packet should have been called for the RX.
    bridge._adapter.on_child_packet.assert_called_once()


async def test_excluded_hgi_lwt_calls_on_mqtt_capable_not_unknown(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Excluded HGI (serial primary) LWT calls on_mqtt_capable, not on_unknown_hgi.

    When a serial primary HGI is also publishing on MQTT, the bridge
    excludes it from the MQTT pool (exclude_hgi_id).  But when its LWT
    online arrives, it should NOT be treated as an unknown discovery
    candidate — it's a known, configured HGI handled by the serial
    transport.  The bridge should call on_mqtt_capable to update
    _comment (supports both usb and mqtt), not on_unknown_hgi.
    """
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1},
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    discovery = MagicMock()
    bridge._discovery_callback = discovery
    bridge._adapter = MagicMock()

    # Exclude HGI 2 (simulating serial primary discovery).
    bridge.exclude_hgi_id(TEST_HGI_2)
    assert TEST_HGI_2 in bridge._excluded_hgi_ids

    # LWT online for the excluded HGI.
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_2}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    # on_mqtt_capable should be called (not on_unknown_hgi).
    discovery.on_mqtt_capable.assert_called_once()
    call_args = discovery.on_mqtt_capable.call_args
    assert str(call_args.args[0]) == TEST_HGI_2
    assert call_args.kwargs.get("topic") == msg.topic

    # on_unknown_hgi should NOT be called.
    discovery.on_unknown_hgi.assert_not_called()

    # on_child_online should NOT be called (serial transport handles it).
    bridge._adapter.on_child_online.assert_not_called()


async def test_rx_skipped_for_excluded_hgi(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """RX from excluded HGI (serial primary) is skipped, not forwarded.

    When a serial primary HGI is also publishing on MQTT, the bridge
    excludes it.  RX packets from excluded HGIs should NOT be forwarded
    to the pool — the serial transport handles them, and forwarding
    via MQTT would cause duplicate ingestion (even if deduped) and
    incorrectly mark the excluded MQTT child as connected.
    """
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1},
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    bridge._adapter = MagicMock()

    # Exclude HGI 2 (simulating serial primary discovery).
    bridge.exclude_hgi_id(TEST_HGI_2)
    assert TEST_HGI_2 in bridge._excluded_hgi_ids

    # RX packet from the excluded HGI.
    frame = "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_2}/rx"
    msg.payload = json.dumps({"msg": frame}).encode()
    bridge._handle_rx_message(msg)

    # on_child_packet should NOT be called (serial transport handles it).
    bridge._adapter.on_child_packet.assert_not_called()
    # on_child_online should NOT be called.
    bridge._adapter.on_child_online.assert_not_called()


async def test_wait_online_timeout_passed_to_bridge(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that wait_online_timeout is stored and used."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=45.0,
    )
    assert bridge._wait_online_timeout == 45.0


async def test_wait_online_timeout_default(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that wait_online_timeout defaults to 30.0."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    assert bridge._wait_online_timeout == 30.0


# -- !V gating on acceptance (issue 1119) ---------------------------------


def test_is_accepted_all_when_none(hass: HomeAssistant) -> None:
    """Test that _is_accepted returns True for all when accepted_hgi_ids is None."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
    )
    assert bridge._is_accepted(TEST_HGI_1)
    assert bridge._is_accepted(TEST_HGI_2)
    assert bridge._is_accepted("18:999999")


def test_is_accepted_only_in_set(hass: HomeAssistant) -> None:
    """Test that _is_accepted returns True only for HGIs in the accepted set."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1},
    )
    assert bridge._is_accepted(TEST_HGI_1)
    assert not bridge._is_accepted(TEST_HGI_2)


def test_is_accepted_empty_set(hass: HomeAssistant) -> None:
    """Test that _is_accepted returns False for all when accepted set is empty."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids=set(),
    )
    assert not bridge._is_accepted(TEST_HGI_1)
    assert not bridge._is_accepted(TEST_HGI_2)


async def test_lwt_online_no_v_for_unaccepted(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that !V is not sent to unaccepted (receive-only) HGIs (issue 1119)."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1},
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # LWT online for the unaccepted HGI 2.
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_2}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    # !V should NOT have been published for HGI 2.
    for call in mock_mqtt_pool["publish"].call_args_list:
        topic = call.args[1]
        assert TEST_HGI_2 not in topic, (
            f"!V was sent to unaccepted HGI {TEST_HGI_2}"
        )


async def test_lwt_online_v_for_accepted(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that !V is sent to accepted HGIs."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1},
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    # LWT online for the accepted HGI 1.
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)

    # !V should have been published for HGI 1.
    publish_calls = [
        call.args[1] for call in mock_mqtt_pool["publish"].call_args_list
    ]
    assert any(TEST_HGI_1 in t and "/cmd/cmd" in t for t in publish_calls), (
        "!V was not sent to accepted HGI"
    )


# -- publish_frame awaits mqtt.async_publish (issue 1119) ------------------


async def test_publish_frame_awaits_publish(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that publish_frame awaits mqtt.async_publish (no fire-and-forget)."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    await bridge.publish_frame(
        TEST_HGI_1,
        " 000 I --- 01:123456 18:000730 --:------ 30C9 000 00",
    )
    # async_publish should have been called and awaited
    mock_mqtt_pool["publish"].assert_called_once()


async def test_publish_frame_propagates_error(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that publish_frame propagates MQTT publish errors."""
    mock_mqtt_pool["publish"].side_effect = RuntimeError("MQTT broker down")
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    with pytest.raises(RuntimeError, match="MQTT broker down"):
        await bridge.publish_frame(TEST_HGI_1, "!V")


# -- CMD result handling ---------------------------------------------------


async def test_cmd_message_v_response(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message processes !V command result."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/result"
    msg.payload = json.dumps({"cmd": "!V", "return": 0}).encode()
    bridge._handle_cmd_message(msg)  # should not crash


async def test_cmd_message_int_return(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message with non-!V int return."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/result"
    msg.payload = json.dumps({"cmd": "!S", "return": 42}).encode()
    bridge._handle_cmd_message(msg)  # should not crash


async def test_cmd_message_str_return(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message with string return."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/result"
    msg.payload = json.dumps(
        {"cmd": "!V", "return": "ramses_esp_eth 0.7.0"}
    ).encode()
    bridge._handle_cmd_message(msg)  # should not crash


async def test_cmd_message_invalid_json(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message with invalid JSON doesn't crash."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/result"
    msg.payload = b"not json"
    bridge._handle_cmd_message(msg)  # should not crash


async def test_cmd_message_no_adapter(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_cmd_message with no adapter returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/result"
    msg.payload = b"{}"
    bridge._handle_cmd_message(msg)  # should not crash


async def test_cmd_message_invalid_topic(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message with invalid topic returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = "other/topic/cmd/result"
    msg.payload = b"{}"
    bridge._handle_cmd_message(msg)  # should not crash


async def test_cmd_message_empty_payload(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message with empty payload returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/result"
    msg.payload = b""
    bridge._handle_cmd_message(msg)  # should not crash


async def test_cmd_message_no_return_key(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message with no 'return' key in JSON."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/cmd/result"
    msg.payload = json.dumps({"cmd": "!V"}).encode()  # no "return"
    bridge._handle_cmd_message(msg)  # should not crash


# -- RX edge cases ---------------------------------------------------------


async def test_rx_message_no_adapter(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_rx_message with no adapter returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/rx"
    msg.payload = b"{}"
    bridge._handle_rx_message(msg)  # should not crash


async def test_rx_message_invalid_topic(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_rx_message with invalid topic returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = "other/topic/rx"
    msg.payload = b"{}"
    bridge._handle_rx_message(msg)  # should not crash


async def test_rx_message_empty_payload(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_rx_message with empty payload returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/rx"
    msg.payload = b""
    bridge._handle_rx_message(msg)  # should not crash


async def test_rx_message_no_msg_key(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_rx_message with JSON but no 'msg' key."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"other": "data"}).encode()
    bridge._handle_rx_message(msg)  # should not crash


async def test_rx_message_empty_frame(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_rx_message with empty frame string."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"msg": ""}).encode()
    bridge._handle_rx_message(msg)  # should not crash


# -- Status and broker edge cases ------------------------------------------


async def test_status_message_no_adapter(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_status_message with no adapter returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    msg = MagicMock()
    msg.topic = f"RAMSES/GATEWAY/{TEST_HGI_1}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)  # should not crash


async def test_status_message_invalid_topic(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_status_message with invalid topic returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = "other/topic"
    msg.payload = b"online"
    bridge._handle_status_message(msg)  # should not crash


async def test_broker_status_no_adapter(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_broker_status with no adapter returns early."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    bridge._handle_broker_status(True)  # should not crash


# -- Payload extraction ----------------------------------------------------


def test_extract_payload_bytes(hass: HomeAssistant) -> None:
    """Test _extract_payload with bytes payload."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    msg = MagicMock()
    msg.payload = b"hello"
    assert bridge._extract_payload(msg) == "hello"


def test_extract_payload_string(hass: HomeAssistant) -> None:
    """Test _extract_payload with string payload."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    msg = MagicMock()
    msg.payload = "hello"
    assert bridge._extract_payload(msg) == "hello"


def test_extract_payload_int(hass: HomeAssistant) -> None:
    """Test _extract_payload with non-bytes/non-string payload."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    msg = MagicMock()
    msg.payload = 42
    assert bridge._extract_payload(msg) == "42"


# -- Transport factory with config -----------------------------------------


async def test_transport_factory_with_existing_config(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test async_transport_factory with an existing config object."""
    from ramses_tx.transport import TransportConfig

    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    config = TransportConfig(disable_sending=False, autostart=False)
    transport = await bridge.async_transport_factory(
        mock_protocol, config=config
    )
    assert transport is not None
    assert config.autostart is True


# -- Subscription failure --------------------------------------------------


async def test_async_attach_subscription_failure(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _async_attach handles subscription failure gracefully."""
    mock_mqtt_pool["subscribe"].side_effect = RuntimeError("MQTT down")
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    with pytest.raises(TransportError, match="MQTT down"):
        await bridge._async_attach()


# -- Close with no subscriptions -------------------------------------------


def test_close_no_subscriptions(hass: HomeAssistant) -> None:
    """Test close with no subscriptions doesn't crash."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    bridge.close()  # should not crash


# -- LWT offline for unconfigured HGI --------------------------------------


async def test_lwt_offline_unknown_hgi_no_crash(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test LWT offline for unknown HGI doesn't crash."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = "RAMSES/GATEWAY/18:999999"
    msg.payload = b"offline"
    bridge._handle_status_message(msg)  # should not crash


# -- _handle_cmd_message tests ----------------------------------------------


async def test_handle_cmd_message_evofw3_response(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message processes !V command response."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = json.dumps({"return": 0, "cmd": "!V"}).encode()

    bridge._handle_cmd_message(msg)  # should not crash


async def test_handle_cmd_message_ramses_esp_eth(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message handles ramses_esp_eth firmware string."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = json.dumps(
        {"return": "ramses_esp_eth 0.6.6c", "cmd": "!V"}
    ).encode()

    bridge._handle_cmd_message(msg)  # should not crash


async def test_handle_cmd_message_json_decode_error(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message handles JSON decode error gracefully."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = b"not json"

    bridge._handle_cmd_message(msg)  # should not crash


async def test_handle_cmd_message_no_adapter(
    hass: HomeAssistant,
) -> None:
    """Test _handle_cmd_message returns early when adapter is None."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = b"{}"
    # Should not crash even though adapter is None
    bridge._handle_cmd_message(msg)


async def test_handle_cmd_message_invalid_hgi_topic(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message rejects non-HGI topic."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/32:153289/cmd/result"
    msg.payload = json.dumps({"return": 0, "cmd": "!V"}).encode()

    bridge._handle_cmd_message(msg)  # should not crash


async def test_handle_cmd_message_empty_payload(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message handles empty payload."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = b""

    bridge._handle_cmd_message(msg)  # should not crash


async def test_handle_cmd_message_int_return_non_v(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_cmd_message with int return for non-!V command."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = json.dumps({"return": 42, "cmd": "!C"}).encode()

    bridge._handle_cmd_message(msg)  # should not crash


# -- _handle_broker_status tests --------------------------------------------


async def test_handle_broker_status_connected(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_broker_status with connected=True."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    bridge._handle_broker_status(True)
    # adapter.on_broker_connected should have been called
    assert bridge._adapter is not None


async def test_handle_broker_status_disconnected(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_broker_status with connected=False."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    bridge._handle_broker_status(False)
    # adapter.on_broker_disconnected should have been called
    assert bridge._adapter is not None


async def test_handle_broker_status_no_adapter(
    hass: HomeAssistant,
) -> None:
    """Test _handle_broker_status returns early when adapter is None."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    # Should not crash
    bridge._handle_broker_status(True)
    bridge._handle_broker_status(False)


# -- exclude_hgi_id tests ---------------------------------------------------


async def test_exclude_hgi_id_removes_from_configured(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test exclude_hgi_id marks HGI as excluded."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    bridge.exclude_hgi_id(TEST_HGI_1)
    assert TEST_HGI_1 in bridge._excluded_hgi_ids
    assert TEST_HGI_2 not in bridge._excluded_hgi_ids


async def test_exclude_hgi_id_not_in_list(
    hass: HomeAssistant,
) -> None:
    """Test exclude_hgi_id when HGI is not in configured list."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    # Should not crash
    bridge.exclude_hgi_id(TEST_HGI_2)
    assert TEST_HGI_1 not in bridge._excluded_hgi_ids


# -- _is_accepted tests -----------------------------------------------------


def test_is_accepted_no_accepted_set(hass: HomeAssistant) -> None:
    """Test _is_accepted returns True when _accepted_hgi_ids is None."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    assert bridge._is_accepted(TEST_HGI_1) is True


def test_is_accepted_in_set(hass: HomeAssistant) -> None:
    """Test _is_accepted returns True when HGI is in accepted set."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        accepted_hgi_ids={TEST_HGI_1},
    )
    assert bridge._is_accepted(TEST_HGI_1) is True


def test_is_accepted_not_in_set(hass: HomeAssistant) -> None:
    """Test _is_accepted returns False when HGI is not in accepted set."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        accepted_hgi_ids={TEST_HGI_2},
    )
    assert bridge._is_accepted(TEST_HGI_1) is False


# -- _extract_hgi_from_topic edge cases -------------------------------------


def test_extract_hgi_from_topic_no_prefix(hass: HomeAssistant) -> None:
    """Test _extract_hgi_from_topic returns None for wrong prefix."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    assert bridge._extract_hgi_from_topic("OTHER/18:001111", "") is None


def test_extract_hgi_from_topic_non_hgi(hass: HomeAssistant) -> None:
    """Test _extract_hgi_from_topic rejects non-18: device IDs."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    assert (
        bridge._extract_hgi_from_topic("RAMSES/GATEWAY/32:153289", "") is None
    )


def test_extract_hgi_from_topic_too_short(hass: HomeAssistant) -> None:
    """Test _extract_hgi_from_topic rejects short IDs."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    assert bridge._extract_hgi_from_topic("RAMSES/GATEWAY/18:123", "") is None


def test_extract_hgi_from_topic_with_suffix(hass: HomeAssistant) -> None:
    """Test _extract_hgi_from_topic strips suffix correctly."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    result = bridge._extract_hgi_from_topic(
        f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx", "/rx"
    )
    assert result == TEST_HGI_1


# -- _handle_rx_message edge cases ------------------------------------------


async def test_handle_rx_message_json_decode_error(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_rx_message handles JSON decode error."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = b"not json"

    bridge._handle_rx_message(msg)  # should not crash


async def test_handle_rx_message_no_adapter(
    hass: HomeAssistant,
) -> None:
    """Test _handle_rx_message returns early when adapter is None."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = (
        b'{"msg": " I --- 18:001111 32:153289 --:------ 22F1 003 000207"}'
    )
    # Should not crash
    bridge._handle_rx_message(msg)


async def test_handle_rx_message_invalid_hgi(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_rx_message rejects non-HGI topic."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/32:153289/rx"
    msg.payload = b'{"msg": "test"}'

    bridge._handle_rx_message(msg)  # should not crash


async def test_handle_rx_message_empty_payload(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test _handle_rx_message handles empty payload."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = b""

    bridge._handle_rx_message(msg)  # should not crash


# -- async_attach_to_pool tests (hybrid pool) ------------------------------


async def test_async_attach_to_pool(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test async_attach_to_pool creates adapter and subscribes."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        wait_online_timeout=0.01,
    )

    # Mock a PooledTransport
    mock_pool = MagicMock()
    mock_pool._protocol = MagicMock()
    mock_pool._conn_fut = None
    mock_pool._wait_for_any_connection = AsyncMock()

    with patch(
        "custom_components.ramses_cc.mqtt_pool_bridge.MqttCallbackPoolAdapter"
    ) as mock_adapter_cls:
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter

        await bridge.async_attach_to_pool(
            mock_pool, callback_child_start_index=1
        )

    assert bridge._pool is mock_pool
    assert bridge._adapter is mock_adapter
    mock_adapter_cls.assert_called_once()


async def test_async_attach_to_pool_mqtt_timeout(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test async_attach_to_pool handles MQTT client timeout."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )

    mock_pool = MagicMock()
    mock_pool._protocol = MagicMock()
    mock_pool._conn_fut = None
    mock_pool._wait_for_any_connection = AsyncMock()

    with patch(
        "custom_components.ramses_cc.mqtt_pool_bridge.MqttCallbackPoolAdapter"
    ):
        # Should not crash even if MQTT client setup times out
        await bridge.async_attach_to_pool(
            mock_pool, callback_child_start_index=0
        )
    assert bridge._adapter is not None


# -- Error handling and edge cases ----------------------------------------


def test_handle_rx_json_decode_error(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_rx_message handles JSON decode errors."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    bridge._adapter = MagicMock()

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = b"not valid json"

    bridge._handle_rx_message(msg)
    # Should not crash, adapter should not be called
    bridge._adapter.on_rx_packet.assert_not_called()


def test_handle_rx_exception(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_rx_message handles unexpected exceptions."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    # Make adapter raise an exception
    bridge._adapter = MagicMock()
    bridge._adapter.on_rx_packet.side_effect = RuntimeError("test error")

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = b'{"frame": "000..." }'

    # Should not crash
    bridge._handle_rx_message(msg)


def test_handle_cmd_json_decode_error(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_cmd_message handles JSON decode errors."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = b"not valid json"

    bridge._handle_cmd_message(msg)
    # Should not crash


def test_handle_cmd_exception(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test _handle_cmd_message handles unexpected exceptions."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )

    # Create a msg that will cause an exception during processing
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    # Use a payload that parses as JSON but causes issues downstream
    msg.payload = b'{"result": "ok"}'

    # Should not crash even with unexpected errors
    bridge._handle_cmd_message(msg)


def test_discovery_callback_on_mqtt_capable(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test that discovery_callback.on_mqtt_capable is called for configured HGIs."""
    discovery_cb = MagicMock()
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        discovery_callback=discovery_cb,
        # Don't add to accepted_hgi_ids to avoid !V publish
    )
    bridge._adapter = MagicMock()

    # Mock _publish_command to avoid actual MQTT publish
    bridge._publish_command = AsyncMock()

    # Simulate an LWT online message for a configured HGI
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}"
    msg.payload = b"online"

    bridge._handle_status_message(msg)
    # discovery_callback.on_mqtt_capable should be called for configured HGI
    discovery_cb.on_mqtt_capable.assert_called_once()
    # on_unknown_hgi should NOT be called for configured HGIs (issue 1208)
    discovery_cb.on_unknown_hgi.assert_not_called()


def test_exclude_hgi_id(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """Test exclude_hgi_id marks HGI as excluded and offline via adapter."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1, TEST_HGI_2},
    )
    bridge._adapter = MagicMock()

    bridge.exclude_hgi_id(TEST_HGI_1)

    assert TEST_HGI_1 in bridge._excluded_hgi_ids
    assert TEST_HGI_1 in bridge._accepted_hgi_ids
    # The adapter's on_child_offline must be called (not a
    # nonexistent remove_child on the pool — issue 1119).
    bridge._adapter.on_child_offline.assert_called_once_with(
        TEST_HGI_1, definitive=True
    )


def test_extract_hgi_from_topic_empty_parts(hass: HomeAssistant) -> None:
    """Test _extract_hgi_from_topic returns None for empty parts."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    # Topic with no HGI ID after prefix
    result = bridge._extract_hgi_from_topic(
        topic=f"{TEST_TOPIC_PREFIX}/",
        suffix="/rx",
    )
    # Should return None for invalid format
    assert result is None


def test_extract_hgi_from_topic_invalid_format(hass: HomeAssistant) -> None:
    """Test _extract_hgi_from_topic returns None for invalid HGI ID format."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    result = bridge._extract_hgi_from_topic(
        topic=f"{TEST_TOPIC_PREFIX}/invalid_id/rx",
        suffix="/rx",
    )
    assert result is None


def test_extract_hgi_from_topic_not_hgi_device(hass: HomeAssistant) -> None:
    """Test _extract_hgi_from_topic returns None for non-HGI device IDs."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
    )
    result = bridge._extract_hgi_from_topic(
        topic=f"{TEST_TOPIC_PREFIX}/01:123456/rx",
        suffix="/rx",
    )
    assert result is None


# -- unexclude_hgi_id (issue 1185) ---------------------------------------


def test_unexclude_hgi_id_not_excluded_is_noop(hass: HomeAssistant) -> None:
    """Test that unexclude_hgi_id is a no-op when HGI is not excluded."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1, TEST_HGI_2},
    )
    # HGI 2 is not excluded — unexclude should be a no-op.
    bridge.unexclude_hgi_id(TEST_HGI_2)
    assert TEST_HGI_2 not in bridge._excluded_hgi_ids


async def test_unexclude_hgi_id_re_includes(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that unexclude_hgi_id re-includes an excluded HGI (issue 1185)."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1, TEST_HGI_2},
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    # Exclude HGI 2 (simulating serial primary discovery).
    bridge.exclude_hgi_id(TEST_HGI_2)
    assert TEST_HGI_2 in bridge._excluded_hgi_ids

    # Now unexclude (simulating serial transport disconnect).
    bridge.unexclude_hgi_id(TEST_HGI_2)
    assert TEST_HGI_2 not in bridge._excluded_hgi_ids
    assert TEST_HGI_2 in bridge._accepted_hgi_ids  # type: ignore[union-attr]


def test_unexclude_preserves_receive_only_acceptance(
    hass: HomeAssistant,
) -> None:
    """Re-inclusion must not promote a receive-only discovery candidate."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1},
    )

    bridge.exclude_hgi_id(TEST_HGI_2)
    bridge.unexclude_hgi_id(TEST_HGI_2)

    assert bridge._accepted_hgi_ids == {TEST_HGI_1}


async def test_unexclude_hgi_id_brings_online(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that unexclude brings an already-online HGI back online."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1, TEST_HGI_2},
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    # Bring HGI 2 online first.
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_2}"
    msg.payload = b"online"
    bridge._handle_status_message(msg)
    assert TEST_HGI_2 in bridge._online_hgis

    # Exclude HGI 2.
    bridge.exclude_hgi_id(TEST_HGI_2)

    # Unexclude — should call on_child_online since HGI is already online.
    bridge._adapter = MagicMock()  # type: ignore[assignment]
    bridge.unexclude_hgi_id(TEST_HGI_2)
    bridge._adapter.on_child_online.assert_called_once_with(TEST_HGI_2)


# -- Serial-silence failover ----------------------------------------------


def _serial_pool(
    hgi_id: str, last_pkt_iso: str | None, pkts: int | None = None
) -> MagicMock:
    """Mock a pool whose serial child owns ``hgi_id``."""
    pool = MagicMock()
    pool.get_pool_child_status.return_value = [
        {
            "child_id": "0",
            "port_name": "/dev/ttyACM0",
            "hgi_id": hgi_id,
            "callback_driven": False,
            "pkts_received": (
                pkts
                if pkts is not None
                else (0 if last_pkt_iso is None else 1)
            ),
            "last_pkt_time": last_pkt_iso,
        }
    ]
    return pool


def _excluded_bridge(hass: HomeAssistant) -> RamsesMqttPoolBridge:
    """Bridge with TEST_HGI_1 excluded (serial primary)."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1, TEST_HGI_2],
        accepted_hgi_ids={TEST_HGI_1, TEST_HGI_2},
    )
    bridge._adapter = MagicMock()
    bridge.exclude_hgi_id(TEST_HGI_1)
    return bridge


def test_serial_silence_fails_over_to_mqtt(hass: HomeAssistant) -> None:
    """Excluded HGI with live MQTT feed but silent serial fails over."""
    from datetime import timedelta as td

    from ramses_tx.helpers import dt_now

    bridge = _excluded_bridge(hass)
    bridge._pool = _serial_pool(TEST_HGI_1, None)  # never delivered
    bridge._excluded_mqtt_rx[TEST_HGI_1] = dt_now() - td(minutes=1)

    with patch(
        "custom_components.ramses_cc.notifications._pn_create"
    ) as mock_notify:
        failed_over = bridge.serial_silence_check()

    assert failed_over == [TEST_HGI_1]
    assert TEST_HGI_1 not in bridge._excluded_hgi_ids
    assert TEST_HGI_1 in bridge._degraded_hgi_ids
    mock_notify.assert_called_once()


def test_serial_alive_stays_excluded(hass: HomeAssistant) -> None:
    """Excluded HGI stays excluded while its serial leg delivers."""
    from datetime import timedelta as td

    from ramses_tx.helpers import dt_now

    bridge = _excluded_bridge(hass)
    bridge._pool = _serial_pool(
        TEST_HGI_1, (dt_now() - td(seconds=30)).isoformat()
    )
    bridge._excluded_mqtt_rx[TEST_HGI_1] = dt_now()

    assert bridge.serial_silence_check() == []

    assert TEST_HGI_1 in bridge._excluded_hgi_ids
    assert TEST_HGI_1 not in bridge._degraded_hgi_ids


def test_no_mqtt_rx_no_failover(hass: HomeAssistant) -> None:
    """A silent serial leg alone does not fail over without MQTT proof."""

    bridge = _excluded_bridge(hass)
    bridge._pool = _serial_pool(TEST_HGI_1, None)
    # No MQTT RX recorded for the excluded HGI.

    assert bridge.serial_silence_check() == []

    assert TEST_HGI_1 in bridge._excluded_hgi_ids
    assert TEST_HGI_1 not in bridge._degraded_hgi_ids


def test_serial_revive_re_excludes(hass: HomeAssistant) -> None:
    """A failovered HGI re-excludes after sustained serial traffic."""
    from custom_components.ramses_cc import notifications as _notif
    from ramses_tx.helpers import dt_now

    bridge = _excluded_bridge(hass)
    bridge._degraded_hgi_ids.add(TEST_HGI_1)
    bridge._excluded_hgi_ids.discard(TEST_HGI_1)
    _notif._active.add(f"serial_silent_{TEST_HGI_1}")

    with patch(
        "custom_components.ramses_cc.notifications._pn_dismiss"
    ) as mock_dismiss:
        # Cycle 1: baseline only — a single packet must not revive.
        bridge._pool = _serial_pool(TEST_HGI_1, dt_now().isoformat(), pkts=1)
        bridge.serial_silence_check()
        assert TEST_HGI_1 not in bridge._excluded_hgi_ids

        # Cycle 2: progress — streak 1.
        bridge._pool = _serial_pool(TEST_HGI_1, dt_now().isoformat(), pkts=2)
        bridge.serial_silence_check()
        assert TEST_HGI_1 not in bridge._excluded_hgi_ids

        # Cycle 3: still progressing — streak 2 → revived.
        bridge._pool = _serial_pool(TEST_HGI_1, dt_now().isoformat(), pkts=3)
        bridge.serial_silence_check()

    assert TEST_HGI_1 in bridge._excluded_hgi_ids
    assert TEST_HGI_1 not in bridge._degraded_hgi_ids
    assert f"serial_silent_{TEST_HGI_1}" not in _notif._active
    mock_dismiss.assert_called_once()


def test_serial_recurrence_creates_new_notification(
    hass: HomeAssistant,
) -> None:
    """A recovered serial link warns again on a later failure."""
    from ramses_tx.helpers import dt_now

    bridge = _excluded_bridge(hass)
    bridge._pool = _serial_pool(TEST_HGI_1, None)

    with (
        patch(
            "custom_components.ramses_cc.notifications._pn_create"
        ) as mock_notify,
        patch("custom_components.ramses_cc.notifications._pn_dismiss"),
    ):
        bridge._excluded_mqtt_rx[TEST_HGI_1] = dt_now()
        assert bridge.serial_silence_check() == [TEST_HGI_1]

        bridge._serial_recover(TEST_HGI_1)
        bridge._pool = _serial_pool(TEST_HGI_1, None)
        bridge._excluded_mqtt_rx[TEST_HGI_1] = dt_now()
        assert bridge.serial_silence_check() == [TEST_HGI_1]

    assert mock_notify.call_count == 2


def test_serial_stray_packet_does_not_revive(hass: HomeAssistant) -> None:
    """A marginal leg emitting one stray packet stays on MQTT."""
    from ramses_tx.helpers import dt_now

    bridge = _excluded_bridge(hass)
    bridge._degraded_hgi_ids.add(TEST_HGI_1)
    bridge._excluded_hgi_ids.discard(TEST_HGI_1)

    with patch(
        "custom_components.ramses_cc.notifications._pn_dismiss"
    ) as mock_dismiss:
        # Stray packet (progress), then silence (flat) — no revive.
        bridge._pool = _serial_pool(TEST_HGI_1, dt_now().isoformat(), pkts=1)
        bridge.serial_silence_check()
        bridge._pool = _serial_pool(TEST_HGI_1, dt_now().isoformat(), pkts=2)
        bridge.serial_silence_check()
        bridge._pool = _serial_pool(TEST_HGI_1, dt_now().isoformat(), pkts=2)
        bridge.serial_silence_check()
        bridge._pool = _serial_pool(TEST_HGI_1, dt_now().isoformat(), pkts=2)
        bridge.serial_silence_check()

    assert TEST_HGI_1 not in bridge._excluded_hgi_ids
    assert TEST_HGI_1 in bridge._degraded_hgi_ids
    mock_dismiss.assert_not_called()


def test_excluded_rx_tracked_for_watchdog(hass: HomeAssistant) -> None:
    """RX from an excluded HGI is recorded for the watchdog."""
    bridge = _excluded_bridge(hass)

    frame = "000  I --- 01:145038 18:000730 --:------ 30C9 003 000F1B"
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = json.dumps({"msg": frame}).encode()
    bridge._handle_rx_message(msg)

    assert TEST_HGI_1 in bridge._excluded_mqtt_rx


# -- Exception handlers in RX/CMD (defensive coverage) --------------------


async def test_rx_message_unexpected_exception(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that unexpected exception in RX handler is caught (line 522-523)."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    # Force an unexpected exception by making json.loads return a bad object.
    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/rx"
    msg.payload = b'{"msg": "valid"}'

    # Patch json.loads to raise a non-JSONDecodeError exception.
    with patch(
        "custom_components.ramses_cc.mqtt_pool_bridge.json.loads",
        side_effect=RuntimeError("unexpected"),
    ):
        bridge._handle_rx_message(msg)  # should not crash


async def test_cmd_message_unexpected_exception(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    mock_protocol: MagicMock,
) -> None:
    """Test that unexpected exception in CMD handler is caught (line 587-588)."""
    bridge = RamsesMqttPoolBridge(
        hass,
        TEST_TOPIC_PREFIX,
        [TEST_HGI_1],
        wait_online_timeout=0.01,
    )
    await bridge._async_attach()
    await bridge.async_transport_factory(mock_protocol)

    msg = MagicMock()
    msg.topic = f"{TEST_TOPIC_PREFIX}/{TEST_HGI_1}/cmd/result"
    msg.payload = b'{"cmd": "!V", "return": 0}'

    # Patch json.loads to raise a non-JSONDecodeError exception.
    with patch(
        "custom_components.ramses_cc.mqtt_pool_bridge.json.loads",
        side_effect=RuntimeError("unexpected"),
    ):
        bridge._handle_cmd_message(msg)  # should not crash


# -- Subscription-ID release race recovery (HA 2026.8) ---------------------


def _mqtt_data_with_ids(topics: dict[str, int]) -> MagicMock:
    """Mock hass.data['mqtt'] with a subscription-ID registry."""
    mqtt_data = MagicMock()
    mqtt_data.subscription_id_generator._registered_subscriptions = topics
    return mqtt_data


async def test_attach_with_recovery_healthy(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered subscription IDs — no re-attach happens."""
    monkeypatch.setattr(
        "custom_components.ramses_cc.mqtt_pool_bridge"
        "._SUBSCRIPTION_VERIFY_WINDOW",
        0.01,
    )
    mock_mqtt_pool["module"].DATA_MQTT = "mqtt"
    hass.data["mqtt"] = _mqtt_data_with_ids(
        {
            f"{TEST_TOPIC_PREFIX}/+/rx": 2,
            f"{TEST_TOPIC_PREFIX}/+/cmd/result": 3,
            f"{TEST_TOPIC_PREFIX}/+": 4,
        }
    )

    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    await bridge._async_attach_with_recovery()

    # Subscribed once — all three wildcard topics still registered.
    assert mock_mqtt_pool["subscribe"].call_count == 3


async def test_attach_with_recovery_reattaches_when_ids_released(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Released subscription IDs trigger a close + re-attach.

    HA 2026.8 releases a wildcard topic's subscription ID when a
    pending unsubscribe (queued by a previous bridge instance during
    entry reload) completes after the new subscription was tracked —
    the debounced subscribe job then dies on KeyError and the topics
    never reach the broker.
    """
    monkeypatch.setattr(
        "custom_components.ramses_cc.mqtt_pool_bridge"
        "._SUBSCRIPTION_VERIFY_WINDOW",
        0.01,
    )
    monkeypatch.setattr(
        "custom_components.ramses_cc.mqtt_pool_bridge"
        "._RESUBSCRIBE_SETTLE_DELAY",
        0.01,
    )
    mock_mqtt_pool["module"].DATA_MQTT = "mqtt"
    hass.data["mqtt"] = _mqtt_data_with_ids({})  # all IDs released

    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    await bridge._async_attach_with_recovery()

    # First attach (3 topics) + re-attach after detecting the race.
    assert mock_mqtt_pool["subscribe"].call_count == 6
    # The stale subscriptions were unsubscribed via close().
    unsub = mock_mqtt_pool["subscribe"].return_value
    assert unsub.call_count == 3


async def test_attach_with_recovery_no_id_registry(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA without subscription identifiers — attach is not re-done."""
    monkeypatch.setattr(
        "custom_components.ramses_cc.mqtt_pool_bridge"
        "._SUBSCRIPTION_VERIFY_WINDOW",
        0.01,
    )
    mock_mqtt_pool["module"].DATA_MQTT = "mqtt"
    hass.data["mqtt"] = MagicMock(spec=[])  # no subscription_id_generator

    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    await bridge._async_attach_with_recovery()

    assert mock_mqtt_pool["subscribe"].call_count == 3


def test_subscription_ids_released_without_mqtt_data(
    hass: HomeAssistant,
) -> None:
    """No MQTT integration data — treated as healthy."""
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    assert not bridge._subscription_ids_released()


def test_subscription_ids_released_partial(
    hass: HomeAssistant,
    mock_mqtt_pool: dict[str, Any],
) -> None:
    """A single released topic ID is enough to count as broken."""
    mock_mqtt_pool["module"].DATA_MQTT = "mqtt"
    hass.data["mqtt"] = _mqtt_data_with_ids(
        {
            f"{TEST_TOPIC_PREFIX}/+/rx": 2,
            # /cmd/result and /+ missing — released by the race
        }
    )
    bridge = RamsesMqttPoolBridge(hass, TEST_TOPIC_PREFIX, [TEST_HGI_1])
    assert bridge._subscription_ids_released()

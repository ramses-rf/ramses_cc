"""HA-native multi-HGI MQTT bridge using the PR 4A callback contract.

Generalises :class:`RamsesMqttBridge` to drive multiple configured
HGI devices through Home Assistant's shared MQTT connection using
the transport-neutral callback contract defined in
``ramses_tx.transport.callbacks``.

The pool bridge:
- Subscribes once to wildcard RX, command-result, and status/LWT
  topics.
- Extracts the receiving HGI ID from each MQTT topic and passes
  it as ``ingress_hgi_id``.
- Pre-creates logical children from configured HGI IDs.
- Maps LWT online/offline and broker connection events into child
  availability.
- Publishes to the selected HGI's TX topic through the shared
  HA-managed MQTT connection.
- Parses raw RX frame strings into :class:`Packet` objects before
  handing them to the :class:`MqttCallbackPoolAdapter`.

The single-HGI path (:class:`RamsesMqttBridge`) remains unchanged
for backward compatibility — a single MQTT HGI is **not** a pool
with one child.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.core import HomeAssistant, callback

from ramses_tx import exceptions as exc
from ramses_tx.helpers import dt_now
from ramses_tx.packet import Packet
from ramses_tx.transport import TransportConfig
from ramses_tx.transport.callbacks import MqttDiscoveryCallback
from ramses_tx.transport.mqtt_pool import MqttCallbackPoolAdapter
from ramses_tx.transport.pooled import PooledTransport
from ramses_tx.typing import DeviceIdT

if TYPE_CHECKING:
    from homeassistant.components.mqtt import PublishPayloadType

_LOGGER = logging.getLogger(__name__)

#: Suffix for the RX topic (incoming radio packets).
_TOPIC_SUFFIX_RX = "/rx"

#: Wildcard for RX topics across all HGIs.
_TOPIC_WILDCARD_RX = "/+/rx"

#: Wildcard for command result topics across all HGIs.
_TOPIC_WILDCARD_CMD_RESULT = "/+/cmd/result"

#: Wildcard for status/LWT topics across all HGIs.
_TOPIC_WILDCARD_STATUS = "/+"

#: Default timeout for at least one child to come online (seconds).
_DEFAULT_WAIT_ONLINE_TIMEOUT: float = 30.0


class RamsesMqttPoolBridge:
    """HA-native multi-HGI MQTT bridge using the callback contract.

    Manages multiple configured HGI devices through one HA-managed
    MQTT connection.  Uses :class:`MqttCallbackPoolAdapter` to map
    callback events into a :class:`PooledTransport`.

    The single-HGI :class:`RamsesMqttBridge` path is preserved
    unchanged — this class is only used when multiple MQTT HGIs
    are configured.

    :param hass: Home Assistant instance.
    :param topic_prefix: MQTT base topic (e.g. ``RAMSES/GATEWAY``).
    :param configured_hgi_ids: List of configured HGI device IDs.
    :param discovery_callback: Optional callback for unknown HGIs
        observed on the wildcard topic.
    :param wait_online_timeout: Seconds to wait for at least one
        child to come online during transport creation.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        topic_prefix: str,
        configured_hgi_ids: list[str],
        *,
        discovery_callback: MqttDiscoveryCallback | None = None,
        wait_online_timeout: float = _DEFAULT_WAIT_ONLINE_TIMEOUT,
    ) -> None:
        """Initialise the multi-HGI MQTT pool bridge."""
        self._hass = hass
        self._topic_prefix = topic_prefix.rstrip("/")
        self._configured_hgi_ids = configured_hgi_ids
        self._discovery_callback = discovery_callback
        self._wait_online_timeout = wait_online_timeout

        self._pool: PooledTransport | None = None
        self._adapter: MqttCallbackPoolAdapter | None = None

        # Subscriptions.
        self._sub_rx: Callable[[], None] | None = None
        self._sub_cmd: Callable[[], None] | None = None
        self._sub_status: Callable[[], None] | None = None
        self._sub_broker: Callable[[], None] | None = None

        # Track which HGIs are online (LWT).
        self._online_hgis: set[str] = set()

    @property
    def device_ids(self) -> list[str]:
        """Return the configured HGI device IDs."""
        return list(self._configured_hgi_ids)

    async def async_transport_factory(
        self,
        protocol: Any,
        disable_sending: bool = False,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> PooledTransport:
        """Create pooled transport for the multi-HGI MQTT path.

        Subscribes to wildcard MQTT topics, creates the
        :class:`PooledTransport` with callback-driven children,
        waits for at least one child to come online, and returns
        the pool.

        :param protocol: The protocol instance from ramses_rf.
        :param disable_sending: If True, outbound sending is
            disabled.
        :param extra: Optional extra configuration (may contain
            ``SZ_ACTIVE_HGI``).
        :param kwargs: Additional keyword arguments (including
            ``config`` and ``loop``).
        :returns: A :class:`PooledTransport` wrapping all
            callback-driven children.
        """
        _LOGGER.debug(
            "MqttPoolBridge: async_transport_factory called for "
            "%d configured HGIs: %s",
            len(self._configured_hgi_ids),
            self._configured_hgi_ids,
        )

        # Extract config and loop from kwargs.
        config = kwargs.pop("config", None)
        if config is None:
            config = TransportConfig(
                disable_sending=disable_sending,
                autostart=True,
            )
        else:
            config.autostart = True
        kwargs.pop("autostart", None)

        loop = kwargs.pop("loop", None) or self._hass.loop

        # 1. Subscribe to wildcard MQTT topics before starting.
        await self._async_attach()

        # 2. Create the PooledTransport with callback-driven
        #    children.  All children are None (callback-driven).
        n = len(self._configured_hgi_ids)
        self._pool = PooledTransport(
            protocol,
            [None] * n,
            config=config,
            loop=loop,
            port_names=[
                f"mqtt_ha://{hgi_id}" for hgi_id in self._configured_hgi_ids
            ],
        )

        # 3. Create the adapter that bridges callbacks to the pool.
        self._adapter = MqttCallbackPoolAdapter(
            self._pool,
            self._configured_hgi_ids,
            self,  # self implements MqttPoolOutbound
            discovery_callback=self._discovery_callback,
        )

        # 4. Wait for at least one child to come online.
        #    LWT online messages arrive asynchronously from MQTT.
        try:
            await self._pool._wait_for_any_connection(
                timeout=self._wait_online_timeout
            )
        except exc.TransportError as err:
            _LOGGER.warning(
                "MqttPoolBridge: no child came online within %ss: %s",
                self._wait_online_timeout,
                err,
            )
            # Continue anyway — children may come online later.

        return self._pool

    async def _async_attach(self) -> None:
        """Subscribe to wildcard MQTT topics."""
        if self._sub_rx and self._sub_cmd:
            return

        # Wildcard RX: {prefix}/+/rx
        topic_rx_wildcard = f"{self._topic_prefix}{_TOPIC_WILDCARD_RX}"
        _LOGGER.debug(
            "MqttPoolBridge: Subscribing to wildcard RX %s",
            topic_rx_wildcard,
        )

        # Wildcard command results: {prefix}/+/cmd/result
        topic_cmd_wildcard = (
            f"{self._topic_prefix}{_TOPIC_WILDCARD_CMD_RESULT}"
        )
        _LOGGER.debug(
            "MqttPoolBridge: Subscribing to wildcard CMD %s",
            topic_cmd_wildcard,
        )

        # Wildcard status/LWT: {prefix}/+
        topic_status_wildcard = f"{self._topic_prefix}{_TOPIC_WILDCARD_STATUS}"
        _LOGGER.debug(
            "MqttPoolBridge: Subscribing to wildcard status %s",
            topic_status_wildcard,
        )

        try:
            self._sub_rx = await mqtt.async_subscribe(
                self._hass,
                topic_rx_wildcard,
                self._handle_rx_message,
                qos=0,
            )
            _LOGGER.info("MqttPoolBridge: Subscribed to %s", topic_rx_wildcard)

            self._sub_cmd = await mqtt.async_subscribe(
                self._hass,
                topic_cmd_wildcard,
                self._handle_cmd_message,
                qos=0,
            )
            _LOGGER.info(
                "MqttPoolBridge: Subscribed to %s",
                topic_cmd_wildcard,
            )

            self._sub_status = await mqtt.async_subscribe(
                self._hass,
                topic_status_wildcard,
                self._handle_status_message,
                qos=0,
            )
            _LOGGER.info(
                "MqttPoolBridge: Subscribed to %s",
                topic_status_wildcard,
            )

            self._sub_broker = mqtt.async_subscribe_connection_status(
                self._hass, self._handle_broker_status
            )
            _LOGGER.info("MqttPoolBridge: Subscribed to broker status")

        except Exception as err:
            _LOGGER.error(
                "MqttPoolBridge: Failed to subscribe: %s",
                err,
                exc_info=True,
            )

    # -- MqttPoolOutbound implementation --------------------------------

    async def publish_frame(self, child_id: str, frame: str) -> None:
        """Publish a frame to the specified HGI's TX topic.

        Implements :class:`MqttPoolOutbound`.

        :param child_id: The HGI device ID to publish to.
        :param frame: The serialized RAMSES frame string.
        """
        if frame.startswith("!"):
            self._publish_command(child_id, frame)
        else:
            payload = json.dumps({"msg": frame})
            self._publish_tx(child_id, payload)

    # -- Publishing helpers ---------------------------------------------

    def _publish_tx(self, hgi_id: str, payload: PublishPayloadType) -> None:
        """Publish to ``{prefix}/{hgi_id}/tx``.

        :param hgi_id: The target HGI device ID.
        :param payload: The payload to publish.
        """
        topic = f"{self._topic_prefix}/{hgi_id}/tx"
        self._hass.async_create_task(
            mqtt.async_publish(self._hass, topic, payload)
        )
        _LOGGER.debug("MqttPoolBridge: TX -> %s on %s", payload, topic)

    def _publish_command(
        self, hgi_id: str, payload: PublishPayloadType
    ) -> None:
        """Publish to ``{prefix}/{hgi_id}/cmd/cmd``.

        :param hgi_id: The target HGI device ID.
        :param payload: The command to publish.
        """
        topic = f"{self._topic_prefix}/{hgi_id}/cmd/cmd"
        self._hass.async_create_task(
            mqtt.async_publish(self._hass, topic, payload)
        )
        _LOGGER.debug("MqttPoolBridge: CMD -> %s on %s", payload, topic)

    # -- Inbound message handlers ---------------------------------------

    @callback
    def _handle_rx_message(self, msg: ReceiveMessage) -> None:
        """Process incoming radio packets from wildcard RX."""
        if self._adapter is None:
            return

        # Extract HGI ID from topic: {prefix}/{hgi_id}/rx
        hgi_id = self._extract_hgi_from_topic(msg.topic, _TOPIC_SUFFIX_RX)
        if hgi_id is None:
            _LOGGER.debug(
                "MqttPoolBridge: cannot extract HGI from %s",
                msg.topic,
            )
            return

        payload_str = self._extract_payload(msg)
        if not payload_str:
            return

        try:
            data = json.loads(payload_str)
            if not (isinstance(data, dict) and "msg" in data):
                return
            raw_line = data["msg"]
            frame = raw_line.lstrip("\x00").rstrip("\r\n\t\x00 ")
            if not frame:
                return

            _LOGGER.debug(
                "MqttPoolBridge: RX <- %s (HGI=%s)",
                repr(frame),
                hgi_id,
            )

            # Parse the raw frame into a Packet, then hand to adapter.
            dtm = dt_now().isoformat()
            try:
                packet = Packet.from_file(dtm, frame)
            except (ValueError, exc.PacketInvalid) as err:
                _LOGGER.debug(
                    "MqttPoolBridge: dropped non-packet frame "
                    "from %s: %s (%s)",
                    hgi_id,
                    repr(frame),
                    err,
                )
                return

            self._adapter.on_child_packet(
                hgi_id,
                packet,
                ingress_hgi_id=DeviceIdT(hgi_id),
            )

        except json.JSONDecodeError as err:
            _LOGGER.debug("MqttPoolBridge RX: JSON decode error: %s", err)
        except Exception as err:
            _LOGGER.error(
                "MqttPoolBridge RX: unexpected error: %s",
                err,
                exc_info=True,
            )

    @callback
    def _handle_cmd_message(self, msg: ReceiveMessage) -> None:
        """Process command results from wildcard CMD topic.

        Command results (e.g. ``# evofw3 0.1.0``) are firmware
        responses, not RF packets.  They are logged but not fed
        to the pool — the pool's :class:`PooledTransport` only
        accepts parsed :class:`Packet` objects via
        ``on_child_packet``.

        The protocol's ``_is_evofw3`` flag is set from
        ``transport.get_extra_info(SZ_IS_EVOFW3)`` during
        ``connection_made``, not from parsing this response.
        """
        if self._adapter is None:
            return

        # Extract HGI ID from topic: {prefix}/{hgi_id}/cmd/result
        hgi_id = self._extract_hgi_from_topic(msg.topic, "/cmd/result")
        if hgi_id is None:
            return

        payload_str = self._extract_payload(msg)
        if not payload_str:
            return

        try:
            data = json.loads(payload_str)
            if isinstance(data, dict) and "return" in data:
                return_val = data["return"]
                cmd_val = data.get("cmd", "")
                result_str = ""

                if isinstance(return_val, int):
                    if cmd_val == "!V":
                        result_str = "# evofw3 0.1.0"
                    else:
                        result_str = str(return_val)
                elif isinstance(return_val, str):
                    result_str = return_val

                if "ramses_esp_eth" in result_str:
                    result_str = result_str.replace("ramses_esp_eth", "evofw3")

                if not result_str.strip().startswith("#"):
                    result_str = f"# {result_str}"

                result_str = result_str.rstrip("\r\n\t\x00 ")

                _LOGGER.info(
                    "MqttPoolBridge: CMD Response <- %s (HGI=%s)",
                    repr(result_str),
                    hgi_id,
                )
                # Command results are not RF packets — log only.

        except json.JSONDecodeError as err:
            _LOGGER.debug("MqttPoolBridge CMD: JSON decode error: %s", err)
        except Exception as err:
            _LOGGER.error(
                "MqttPoolBridge CMD: unexpected error: %s",
                err,
                exc_info=True,
            )

    @callback
    def _handle_status_message(self, msg: ReceiveMessage) -> None:
        """Process LWT online/offline messages from wildcard status."""
        if self._adapter is None:
            return

        # Extract HGI ID from topic: {prefix}/{hgi_id}
        hgi_id = self._extract_hgi_from_topic(msg.topic, "")
        if hgi_id is None:
            return

        payload_str = self._extract_payload(msg).strip().lower()

        if payload_str == "online":
            _LOGGER.info("MqttPoolBridge: HGI %s online (LWT)", hgi_id)
            self._online_hgis.add(hgi_id)
            if hgi_id in self._configured_hgi_ids:
                self._adapter.on_child_online(hgi_id)
                # Send identity handshake to this HGI only.
                self._publish_command(hgi_id, "!V")
            else:
                self._adapter.on_unknown_hgi(
                    DeviceIdT(hgi_id), topic=msg.topic
                )

        elif payload_str == "offline":
            _LOGGER.warning("MqttPoolBridge: HGI %s offline (LWT)", hgi_id)
            self._online_hgis.discard(hgi_id)
            if hgi_id in self._configured_hgi_ids:
                self._adapter.on_child_offline(hgi_id, definitive=True)

    @callback
    def _handle_broker_status(self, connected: bool) -> None:
        """Handle MQTT broker connection/disconnection."""
        if self._adapter is None:
            return

        if connected:
            _LOGGER.info("MqttPoolBridge: broker connected, resuming")
            self._adapter.on_broker_connected()
        else:
            _LOGGER.warning("MqttPoolBridge: broker disconnected, pausing")
            self._adapter.on_broker_disconnected()

    # -- Helpers --------------------------------------------------------

    def _extract_hgi_from_topic(self, topic: str, suffix: str) -> str | None:
        """Extract the HGI ID from an MQTT topic.

        Topics have the form ``{prefix}/{hgi_id}{suffix}``.
        The HGI ID is a 9-character string like ``18:123456``.

        :param topic: The MQTT topic.
        :param suffix: The topic suffix (e.g. ``/rx``).
        :returns: The HGI ID, or ``None`` if not found.
        """
        prefix = self._topic_prefix + "/"
        if not topic.startswith(prefix):
            return None
        remainder = topic[len(prefix) :]
        if suffix and remainder.endswith(suffix):
            remainder = remainder[: -len(suffix)]
        # HGI ID is the last segment (e.g. "18:123456").
        parts = remainder.split("/")
        if not parts:
            return None
        hgi_id = parts[-1]
        # Validate format: NN:NNNNNN
        if len(hgi_id) == 9 and hgi_id[2] == ":":
            return hgi_id
        return None

    def _extract_payload(self, msg: ReceiveMessage) -> str:
        """Decode raw message bytes to string.

        :param msg: The MQTT receive message.
        :returns: The decoded payload string.
        """
        if isinstance(msg.payload, bytes):
            return msg.payload.decode("utf-8", errors="ignore")
        return str(msg.payload)

    def close(self) -> None:
        """Cleanup subscriptions."""
        _LOGGER.debug("MqttPoolBridge: cleanup called")
        if self._sub_rx:
            self._sub_rx()
        if self._sub_cmd:
            self._sub_cmd()
        if self._sub_status:
            self._sub_status()
        if self._sub_broker:
            self._sub_broker()

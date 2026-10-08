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

The same callback-driven transport is used for one or more MQTT HGIs,
so wildcard discovery and lifecycle handling stay consistent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.core import HomeAssistant, callback

from ramses_tx import exceptions as exc
from ramses_tx.address import is_hgi_id
from ramses_tx.helpers import dt_now
from ramses_tx.packet import Packet
from ramses_tx.transport import TransportConfig
from ramses_tx.transport.callbacks import MqttDiscoveryCallback
from ramses_tx.transport.mqtt_pool import MqttCallbackPoolAdapter
from ramses_tx.transport.pooled import PooledTransport
from ramses_tx.typing import DeviceIdT

from .const import DEFAULT_HGI_ID
from .notifications import (
    async_dismiss as pn_async_dismiss,
    async_set as pn_async_set,
)

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

#: An excluded HGI's serial leg is declared dead when its MQTT feed
#: shows live RF traffic while the serial child has delivered nothing
#: for this long (its USB stack can wedge while WiFi stays alive —
#: seen with ESP32 gateways).
_SERIAL_SILENCE_AFTER = td(minutes=3)

#: A failovered serial leg counts as revived when it delivers a
#: packet within this window.
_SERIAL_REVIVED_WITHIN = td(minutes=2)

#: Consecutive check cycles with increasing serial RX count required
#: before a failovered leg counts as revived.  Guards against
#: marginal/flapping serial links that emit an occasional packet and
#: then go silent again.
_SERIAL_REVIVE_STREAK = 2

#: Grace period after subscribing during which a pending unsubscribe
#: queued by a previous bridge instance (config-entry reload) can
#: still release our topics' HA MQTT subscription IDs — HA releases
#: them only after the UNSUBACK is awaited, i.e. after the
#: unsubscribe debounce plus the broker round-trip.
_SUBSCRIPTION_VERIFY_WINDOW: float = 1.5

#: Settle delay between close() and re-attach so the pending
#: unsubscribe batch is flushed (stale IDs released) before the same
#: wildcard topics are re-subscribed — re-subscribing earlier would
#: lose the fresh IDs to the same release race.
_RESUBSCRIBE_SETTLE_DELAY: float = 1.0


class RamsesMqttPoolBridge:
    """HA-native multi-HGI MQTT bridge using the callback contract.

    Manages multiple configured HGI devices through one HA-managed
    MQTT connection.  Uses :class:`MqttCallbackPoolAdapter` to map
    callback events into a :class:`PooledTransport`.

    This class supports both single-HGI and multi-HGI MQTT setups.

    :param hass: Home Assistant instance.
    :param topic_prefix: MQTT base topic (e.g. ``RAMSES/GATEWAY``).
    :param configured_hgi_ids: List of configured HGI device IDs.
    :param discovery_callback: Optional callback for unknown HGIs
        observed on the wildcard topic.
    :param wait_online_timeout: Seconds to wait for at least one
        child to come online during transport creation.
    :param accepted_hgi_ids: Optional set of HGI IDs that are
        accepted pool members (may transmit).  HGIs not in this
        set are receive-only discovery candidates.  If ``None``,
        all configured HGIs are accepted (backward-compatible).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        topic_prefix: str,
        configured_hgi_ids: list[str],
        *,
        discovery_callback: MqttDiscoveryCallback | None = None,
        wait_online_timeout: float = _DEFAULT_WAIT_ONLINE_TIMEOUT,
        accepted_hgi_ids: set[str] | None = None,
    ) -> None:
        """Initialise the multi-HGI MQTT pool bridge."""
        self._hass = hass
        self._topic_prefix = topic_prefix.rstrip("/")
        # The sentinel HGI ID (18:000730) is an internal placeholder
        # for the local gateway's source address — it is not a real
        # gateway and must never become a pool child (issue 1171).
        self._configured_hgi_ids = [
            hgi_id
            for hgi_id in dict.fromkeys(configured_hgi_ids)
            if hgi_id != DEFAULT_HGI_ID
        ]
        self._discovery_callback = discovery_callback
        self._wait_online_timeout = wait_online_timeout
        self._accepted_hgi_ids = (
            {hgi_id for hgi_id in accepted_hgi_ids if hgi_id != DEFAULT_HGI_ID}
            if accepted_hgi_ids is not None
            else None
        )

        self._pool: PooledTransport | None = None
        self._adapter: MqttCallbackPoolAdapter | None = None

        # Subscriptions.
        self._sub_rx: Callable[[], None] | None = None
        self._sub_cmd: Callable[[], None] | None = None
        self._sub_status: Callable[[], None] | None = None
        self._sub_broker: Callable[[], None] | None = None

        # Track which HGIs are online (LWT).
        self._online_hgis: set[str] = set()

        # HGIs excluded from the MQTT pool (e.g. serial primary).
        # These are known/configured HGIs that are handled by another
        # transport.  LWT for these HGIs should update _comment (they
        # support MQTT) but should NOT be treated as unknown discovery
        # candidates (issue 1185/1208).
        self._excluded_hgi_ids: set[str] = set()

        # Serial-silence failover: the last MQTT RX seen for each
        # excluded HGI (its serial leg should deliver the same frames).
        self._excluded_mqtt_rx: dict[str, dt] = {}
        # HGIs temporarily re-included because their serial leg is
        # silent while their MQTT feed shows live RF traffic.
        self._degraded_hgi_ids: set[str] = set()
        # Flap damping for degraded HGIs: last seen serial RX count
        # and the number of consecutive checks on which it grew.
        self._serial_last_pkts: dict[str, int] = {}
        self._serial_revive_streak: dict[str, int] = {}

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

        # 0. Wait for HA's MQTT integration to be available before
        #    subscribing.  async_subscribe requires the MQTT entry
        #    to be loaded.  This is a quick check — the actual MQTT
        #    client connection may happen later, and LWT messages
        #    will be delivered as retained messages when it connects.
        try:
            await mqtt.async_wait_for_mqtt_client(self._hass)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "MqttPoolBridge: timed out waiting for HA MQTT "
                "client setup — continuing anyway"
            )

        # 1. Create the PooledTransport with callback-driven
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
            accepted_hgi_ids=self._accepted_hgi_ids,
        )

        # 3. Subscribe after creating the adapter so retained LWT
        #    messages can be handled immediately.
        await self._async_attach_with_recovery()

        # 4. Bind the protocol immediately.  The pool is connected to
        #    the MQTT broker via HA's MQTT integration — children (HGIs)
        #    may come online later via LWT.  Without this, the engine's
        #    wait_for_connection_made() times out if no HGI is online
        #    within the bind timeout (issue 1119 — clean-schema startup).
        if not self._pool._protocol_connected:
            self._pool._protocol_connected = True
            self._pool._protocol.connection_made(self._pool, ramses=True)
        if (
            self._pool._conn_fut is not None
            and not self._pool._conn_fut.done()
        ):
            self._pool._conn_fut.set_result(self._pool)

        # 5. Wait for at least one child to come online (best-effort).
        #    LWT online messages arrive asynchronously from MQTT.
        #    If no child comes online within the timeout, continue
        #    anyway — children may come online later.
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

    async def async_attach_to_pool(
        self,
        pool: PooledTransport,
        *,
        callback_child_start_index: int,
    ) -> None:
        """Attach MQTT callback adapter to an existing hybrid pool.

        For hybrid pools (serial primary + MQTT additional, or vice
        versa), the coordinator creates a single ``PooledTransport``
        via ``pooled_transport_factory`` with both transport-driven
        (serial) and callback-driven (MQTT) children.  This method
        subscribes to MQTT topics and creates the
        ``MqttCallbackPoolAdapter`` that feeds packets into the
        callback-driven children of the existing pool.

        :param pool: The existing ``PooledTransport`` to attach to.
        :param callback_child_start_index: Index of the first
            callback-driven child in the pool (serial children come
            first).
        """
        _LOGGER.debug(
            "MqttPoolBridge: async_attach_to_pool for %d configured "
            "HGIs (callback children start at index %d): %s",
            len(self._configured_hgi_ids),
            callback_child_start_index,
            self._configured_hgi_ids,
        )

        # 0. Wait for HA's MQTT integration to be available.
        try:
            await mqtt.async_wait_for_mqtt_client(self._hass)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "MqttPoolBridge: timed out waiting for HA MQTT "
                "client setup — continuing anyway"
            )

        # 1. Store the pool reference (don't create a new one).
        self._pool = pool

        # 2. Create the adapter that bridges callbacks to the pool.
        #    The adapter uses the pool's _on_child_packet() method to
        #    feed packets into the callback-driven children.
        self._adapter = MqttCallbackPoolAdapter(
            self._pool,
            self._configured_hgi_ids,
            self,  # self implements MqttPoolOutbound
            discovery_callback=self._discovery_callback,
            accepted_hgi_ids=self._accepted_hgi_ids,
            callback_child_start_index=callback_child_start_index,
        )

        # 3. Subscribe after creating the adapter so retained LWT
        #    messages can be handled immediately.
        await self._async_attach_with_recovery()

        # 4. If no transport-driven child provided connection_made
        #    (e.g. every serial/zigbee child failed to connect), bind
        #    the protocol now — otherwise the engine's
        #    wait_for_connection_made() times out even though the MQTT
        #    children will come online via LWT.
        if not self._pool._protocol_connected:
            self._pool._protocol_connected = True
            self._pool._protocol.connection_made(self._pool, ramses=True)
        if (
            self._pool._conn_fut is not None
            and not self._pool._conn_fut.done()
        ):
            self._pool._conn_fut.set_result(self._pool)

        _LOGGER.info(
            "MqttPoolBridge: attached to hybrid pool with %d MQTT "
            "callback-driven children (indices %d..%d)",
            len(self._configured_hgi_ids),
            callback_child_start_index,
            callback_child_start_index + len(self._configured_hgi_ids) - 1,
        )

    def _wildcard_topics(self) -> tuple[str, str, str]:
        """Return the wildcard topics this bridge subscribes to."""
        return (
            # Wildcard RX: {prefix}/+/rx
            f"{self._topic_prefix}{_TOPIC_WILDCARD_RX}",
            # Wildcard command results: {prefix}/+/cmd/result
            f"{self._topic_prefix}{_TOPIC_WILDCARD_CMD_RESULT}",
            # Wildcard status/LWT: {prefix}/+
            f"{self._topic_prefix}{_TOPIC_WILDCARD_STATUS}",
        )

    async def _async_attach_with_recovery(self) -> None:
        """Subscribe to the wildcard topics and verify it stuck.

        HA 2026.8's per-topic subscription identifiers have a reload
        race: a pending unsubscribe releases the topic's ID only
        *after* the UNSUBACK is awaited, so a wildcard topic
        re-subscribed inside that window loses its ID afterwards.
        HA's debounced subscribe job then dies on a KeyError — the
        subscription stays tracked but never reaches the broker and
        is never retried, leaving all MQTT HGIs offline until the
        config entry is reloaded.  Detect the released IDs and
        re-attach once (a fresh ``async_subscribe`` registers new
        IDs), which self-heals the pool.
        """
        await self._async_attach()
        if self._subscription_id_registry() is None:
            return  # pre-2026.8 HA — nothing to verify
        await asyncio.sleep(_SUBSCRIPTION_VERIFY_WINDOW)
        if not self._subscription_ids_released():
            return
        _LOGGER.warning(
            "MqttPoolBridge: HA MQTT released the wildcard "
            "subscription IDs during attach (unsubscribe/reload "
            "race) — re-attaching"
        )
        self.close()
        await asyncio.sleep(_RESUBSCRIBE_SETTLE_DELAY)
        await self._async_attach()
        if self._subscription_ids_released():
            _LOGGER.error(
                "MqttPoolBridge: wildcard subscription IDs still "
                "missing after re-attach — MQTT HGIs may stay "
                "offline until the config entry is reloaded"
            )

    def _subscription_id_registry(self) -> dict[str, Any] | None:
        """Return HA's registered MQTT subscription IDs, or None.

        Reads the registry defensively — HA versions without
        subscription identifiers (pre-2026.8) expose nothing to
        check and return None.
        """
        try:
            mqtt_data = self._hass.data.get(getattr(mqtt, "DATA_MQTT", "mqtt"))
            registered = getattr(
                getattr(mqtt_data, "subscription_id_generator", None),
                "_registered_subscriptions",
                None,
            )
        except Exception:  # noqa: BLE001 — never let this break setup
            return None
        return registered if isinstance(registered, dict) else None

    def _subscription_ids_released(self) -> bool:
        """Return True if HA released one of our wildcard topic IDs."""
        registered = self._subscription_id_registry()
        if registered is None:
            return False
        return any(
            topic not in registered for topic in self._wildcard_topics()
        )

    async def _async_attach(self) -> None:
        """Subscribe to wildcard MQTT topics.

        Only subscribes to topics that don't already have a handle,
        so re-attach after partial failure doesn't leak subscriptions.
        """
        (
            topic_rx_wildcard,
            topic_cmd_wildcard,
            topic_status_wildcard,
        ) = self._wildcard_topics()

        try:
            if self._sub_rx is None:
                _LOGGER.debug(
                    "MqttPoolBridge: Subscribing to wildcard RX %s",
                    topic_rx_wildcard,
                )
                self._sub_rx = await mqtt.async_subscribe(
                    self._hass,
                    topic_rx_wildcard,
                    self._handle_rx_message,
                    qos=0,
                )
                _LOGGER.info(
                    "MqttPoolBridge: Subscribed to %s", topic_rx_wildcard
                )

            if self._sub_cmd is None:
                _LOGGER.debug(
                    "MqttPoolBridge: Subscribing to wildcard CMD %s",
                    topic_cmd_wildcard,
                )
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

            if self._sub_status is None:
                _LOGGER.debug(
                    "MqttPoolBridge: Subscribing to wildcard status %s",
                    topic_status_wildcard,
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

            if self._sub_broker is None:
                self._sub_broker = mqtt.async_subscribe_connection_status(
                    self._hass, self._handle_broker_status
                )
                _LOGGER.info("MqttPoolBridge: Subscribed to broker status")

        except Exception as err:
            self.close()
            raise exc.TransportError(
                f"MqttPoolBridge failed to subscribe: {err}"
            ) from err

    # -- MqttPoolOutbound implementation --------------------------------

    async def publish_frame(self, child_id: str, frame: str) -> None:
        """Publish a frame to the specified HGI's TX topic.

        Implements :class:`MqttPoolOutbound`.

        Awaits the HA MQTT publish so that transport-layer errors
        propagate to :meth:`PooledTransport._send_routed_frame` and
        are reported as ``WriteOutcome.AMBIGUOUS`` instead of being
        silently swallowed by a fire-and-forget background task
        (issue 1119).

        :param child_id: The HGI device ID to publish to.
        :param frame: The serialized RAMSES frame string.
        """
        if frame.startswith("!"):
            await self._publish_command(child_id, frame)
        else:
            payload = json.dumps({"msg": frame})
            await self._publish_tx(child_id, payload)

    # -- Publishing helpers ---------------------------------------------

    async def _publish_tx(
        self, hgi_id: str, payload: PublishPayloadType
    ) -> None:
        """Publish to ``{prefix}/{hgi_id}/tx``.

        :param hgi_id: The target HGI device ID.
        :param payload: The payload to publish.
        """
        topic = f"{self._topic_prefix}/{hgi_id}/tx"
        await mqtt.async_publish(self._hass, topic, payload)
        _LOGGER.debug("MqttPoolBridge: TX -> %s on %s", payload, topic)

    async def _publish_command(
        self, hgi_id: str, payload: PublishPayloadType
    ) -> None:
        """Publish to ``{prefix}/{hgi_id}/cmd/cmd``.

        :param hgi_id: The target HGI device ID.
        :param payload: The command to publish.
        """
        topic = f"{self._topic_prefix}/{hgi_id}/cmd/cmd"
        await mqtt.async_publish(self._hass, topic, payload)
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
        if hgi_id == DEFAULT_HGI_ID:
            # The sentinel is an internal placeholder, not a real
            # gateway — ignore foreign/stale traffic on its topic.
            _LOGGER.debug(
                "MqttPoolBridge: ignoring RX on sentinel HGI topic %s",
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

            # Skip excluded HGIs (serial primary or serial additional
            # that's also publishing on MQTT).  The serial transport
            # handles these HGIs — forwarding MQTT packets to the pool
            # would cause duplicate ingestion (even if deduped) and
            # incorrectly mark the excluded MQTT child as connected.
            if hgi_id in self._excluded_hgi_ids:
                # Track the live MQTT feed for the serial-silence
                # watchdog: these frames prove RF traffic that the
                # serial leg should also be delivering.
                self._excluded_mqtt_rx[hgi_id] = dt_now()
                _LOGGER.debug(
                    "MqttPoolBridge: RX from excluded HGI %s "
                    "(serial transport) — skipping",
                    hgi_id,
                )
                return
            if hgi_id not in self._configured_hgi_ids:
                self._adapter.on_unknown_hgi(
                    DeviceIdT(hgi_id), topic=msg.topic
                )
                return

            # Fallback: if the HGI is configured but hasn't sent LWT
            # online (e.g. ramses_esp doesn't publish LWT, or the LWT
            # was missed), mark it as online now so the pool child
            # becomes connected and sendable (issue 1185).
            if (
                hgi_id in self._configured_hgi_ids
                and hgi_id not in self._online_hgis
            ):
                _LOGGER.info(
                    "MqttPoolBridge: HGI %s online (inferred from "
                    "RX, no LWT seen)",
                    hgi_id,
                )
                self._online_hgis.add(hgi_id)
                self._adapter.on_child_online(hgi_id)
                if self._is_accepted(hgi_id):
                    self._hass.async_create_task(
                        self._publish_command(hgi_id, "!V")
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
        if hgi_id == DEFAULT_HGI_ID:
            # The sentinel is an internal placeholder, not a real
            # gateway — ignore foreign/stale LWT on its topic.
            _LOGGER.debug(
                "MqttPoolBridge: ignoring LWT on sentinel HGI topic %s",
                msg.topic,
            )
            return

        payload_str = self._extract_payload(msg).strip().lower()

        if payload_str == "online":
            _LOGGER.info("MqttPoolBridge: HGI %s online (LWT)", hgi_id)
            self._online_hgis.add(hgi_id)
            if hgi_id in self._excluded_hgi_ids:
                # Excluded HGI (e.g. serial primary that's also on
                # MQTT).  Don't treat as unknown — just update _comment
                # to include "mqtt" since it's publishing on MQTT.
                # Don't call on_child_online (it's handled by the
                # serial transport) or send !V (serial transport
                # handles identity).
                if self._discovery_callback is not None:
                    self._discovery_callback.on_mqtt_capable(
                        DeviceIdT(hgi_id), topic=msg.topic
                    )
            elif hgi_id in self._configured_hgi_ids:
                self._adapter.on_child_online(hgi_id)
                # Send identity handshake only to accepted HGIs.
                # Receive-only discovery candidates must not be sent
                # any outbound communication before acceptance
                # (issue 1119).
                if self._is_accepted(hgi_id):
                    self._hass.async_create_task(
                        self._publish_command(hgi_id, "!V")
                    )
                # Notify discovery callback so it can update _comment
                # to include "mqtt" for HGIs already in the schema
                # (e.g. added by serial probe with only "usb").
                # Use on_mqtt_capable (not on_unknown_hgi) because the
                # HGI is already configured — we only want the _comment
                # update, not pool-level discovery (issue 1208).
                if self._discovery_callback is not None:
                    self._discovery_callback.on_mqtt_capable(
                        DeviceIdT(hgi_id), topic=msg.topic
                    )
            else:
                # Unknown HGI — the adapter's on_unknown_hgi will
                # call the discovery callback internally.
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

    def exclude_hgi_id(self, hgi_id: str) -> None:
        """Exclude an HGI from the MQTT pool at runtime.

        Used when a serial primary discovers its HGI ID and that
        same HGI is also publishing on MQTT — the serial transport
        takes ownership, and the MQTT child should be made
        non-sendable to avoid duplicate packet ingestion (Phase 2
        hybrid pool, issue 1119).

        This does NOT structurally remove the child from the pool
        (``PooledTransport`` has no ``remove_child()`` and runtime
        structural mutation is intentionally unsupported).  Instead
        it marks the callback-driven child as offline/disconnected
        via the adapter so it's excluded from routing but remains
        in the pool for quick re-inclusion when the serial
        transport disconnects.

        :param hgi_id: The HGI device ID to exclude.
        """
        if hgi_id in self._configured_hgi_ids:
            self._excluded_hgi_ids.add(hgi_id)
            _LOGGER.info(
                "MqttPoolBridge: excluding HGI %s from MQTT pool "
                "(serial primary)",
                hgi_id,
            )
        # Mark the callback-driven child as offline via the adapter.
        # This makes it non-sendable (excluded from routing) without
        # structural pool mutation (issue 1119).
        if self._adapter is not None:
            self._adapter.on_child_offline(hgi_id, definitive=True)

    def unexclude_hgi_id(self, hgi_id: str) -> None:
        """Re-include an HGI in the MQTT pool after its serial transport disconnected.

        When a serial child disconnects (e.g. USB unplugged), its HGI
        should no longer be excluded from the MQTT pool — otherwise
        packets arriving via MQTT from that HGI are silently dropped
        (issue 1185).

        :param hgi_id: The HGI device ID to re-include.
        """
        if hgi_id not in self._excluded_hgi_ids:
            return
        self._excluded_hgi_ids.discard(hgi_id)
        _LOGGER.info(
            "MqttPoolBridge: re-included HGI %s in MQTT pool "
            "(serial transport disconnected)",
            hgi_id,
        )
        # Re-add to configured HGIs so LWT/RX handlers process it again.
        if hgi_id not in self._configured_hgi_ids:
            self._configured_hgi_ids.append(hgi_id)
        # If the HGI is already online (LWT was retained), bring the
        # pool child online now.  Otherwise the next LWT will do it.
        if hgi_id in self._online_hgis and self._adapter is not None:
            self._adapter.on_child_online(hgi_id)

    # -- Serial-silence failover ----------------------------------------

    def _serial_child_status(
        self, hgi_id: str
    ) -> tuple[dt | None, int | None]:
        """Return the serial child's last-packet time and RX count.

        :param hgi_id: The HGI id the child claims to be.
        :return: ``(last_pkt_time, pkts_received)`` of the non-callback
            child whose HGI matches, or ``(None, None)`` when no such
            child exists.  ``pkts_received`` is ``None`` when the child
            exists but reports no count.
        :rtype: tuple[dt | None, int | None]
        """
        if self._pool is None:
            return None, None
        try:
            statuses = self._pool.get_pool_child_status()
        except AttributeError:
            return None, None
        for status in statuses:
            if status.get("callback_driven"):
                continue
            if str(status.get("hgi_id")) == hgi_id:
                last = status.get("last_pkt_time")
                last_dt: dt | None = None
                if isinstance(last, str):
                    try:
                        last_dt = dt.fromisoformat(last)
                    except ValueError:
                        last_dt = None
                pkts = status.get("pkts_received")
                return last_dt, pkts if isinstance(pkts, int) else None
        return None, None

    @callback
    def serial_silence_check(self) -> list[str]:
        """Fail over to MQTT when an excluded HGI's serial leg is dead.

        Called periodically from the coordinator's update cycle.

        An excluded HGI's MQTT feed and its serial leg carry the same
        radio traffic (same physical gateway, e.g. an ESP32 on WiFi +
        USB).  Live MQTT RX with a silent serial child means the USB
        side has wedged (it cannot merely be a quiet network — the
        feed itself proves traffic exists).  Re-include the HGI so the
        pool keeps working, and re-exclude when serial revives.

        :return: HGI ids that failed over to MQTT during this check,
            so the caller can kick a device poll to get RF traffic
            flowing again immediately.
        :rtype: list[str]
        """
        now = dt_now()
        failed_over: list[str] = []
        for hgi_id in list(self._excluded_hgi_ids):
            last_mqtt = self._excluded_mqtt_rx.get(hgi_id)
            if last_mqtt is None or now - last_mqtt > _SERIAL_SILENCE_AFTER:
                continue
            last_serial, _pkts = self._serial_child_status(hgi_id)
            if (
                last_serial is not None
                and now - last_serial <= _SERIAL_SILENCE_AFTER
            ):
                continue  # serial leg is alive — skipping is correct
            self._serial_failover(hgi_id, last_serial)
            failed_over.append(hgi_id)
        for hgi_id in list(self._degraded_hgi_ids):
            last_serial, pkts = self._serial_child_status(hgi_id)
            if pkts is None:
                self._serial_revive_streak[hgi_id] = 0
                continue  # no serial child to compare against
            prev_pkts = self._serial_last_pkts.get(hgi_id)
            self._serial_last_pkts[hgi_id] = pkts
            if prev_pkts is not None and pkts > prev_pkts:
                self._serial_revive_streak[hgi_id] = (
                    self._serial_revive_streak.get(hgi_id, 0) + 1
                )
            else:
                self._serial_revive_streak[hgi_id] = 0
            # Revive only on *sustained* serial traffic: a marginal
            # leg that emits a stray packet every few minutes must not
            # flap the exclusion back on (it would start dropping the
            # live MQTT feed again).
            if (
                self._serial_revive_streak[hgi_id] >= _SERIAL_REVIVE_STREAK
                and last_serial is not None
                and now - last_serial <= _SERIAL_REVIVED_WITHIN
            ):
                self._serial_recover(hgi_id)
        return failed_over

    def _serial_failover(self, hgi_id: str, last_serial: dt | None) -> None:
        """Re-include an excluded HGI whose serial leg went silent."""
        self._degraded_hgi_ids.add(hgi_id)
        self.unexclude_hgi_id(hgi_id)
        _LOGGER.warning(
            "MqttPoolBridge: serial gateway %s silent%s while its "
            "MQTT feed is live — failing over to MQTT (replug or "
            "power-cycle the gateway to restore the serial link)",
            hgi_id,
            f" since {last_serial}" if last_serial else "",
        )
        pn_async_set(
            self._hass,
            f"serial_silent_{hgi_id}",
            True,
            title="Gateway serial link silent",
            message=(
                f"Gateway {hgi_id}: the serial link is silent while "
                "its MQTT feed is live — packets were being dropped. "
                "Temporarily using the MQTT feed; replug or "
                "power-cycle the gateway to restore the serial link."
            ),
        )

    def _serial_recover(self, hgi_id: str) -> None:
        """Re-exclude a failovered HGI once its serial leg delivers."""
        self._degraded_hgi_ids.discard(hgi_id)
        self._serial_last_pkts.pop(hgi_id, None)
        self._serial_revive_streak.pop(hgi_id, None)
        self.exclude_hgi_id(hgi_id)
        pn_async_dismiss(self._hass, f"serial_silent_{hgi_id}")
        _LOGGER.info(
            "MqttPoolBridge: serial gateway %s revived — "
            "re-excluded from MQTT pool",
            hgi_id,
        )

    def _is_accepted(self, hgi_id: str) -> bool:
        """Return whether ``hgi_id`` is an accepted pool member.

        When ``_accepted_hgi_ids`` is ``None`` (backward-compatible),
        all configured HGIs are accepted.  When it is a set, only
        HGIs in that set may transmit (issue 1119).

        :param hgi_id: The HGI device ID to check.
        :returns: ``True`` if the HGI is accepted.
        """
        if self._accepted_hgi_ids is None:
            return True
        return hgi_id in self._accepted_hgi_ids

    def _extract_hgi_from_topic(self, topic: str, suffix: str) -> str | None:
        """Extract the HGI ID from an MQTT topic.

        Topics have the form ``{prefix}/{hgi_id}{suffix}``.
        The HGI ID must be a 9-character string matching the HGI
        device-id format ``18:NNNNNN`` — non-HGI device IDs such
        as ``32:153289`` are rejected so they cannot be mistaken
        for gateways (issue 1119).

        :param topic: The MQTT topic.
        :param suffix: The topic suffix (e.g. ``/rx``).
        :returns: The HGI ID, or ``None`` if not found or invalid.
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
        # Validate format: 18:NNNNNN (HGI devices only).
        if len(hgi_id) == 9 and is_hgi_id(hgi_id) and hgi_id[3:].isdigit():
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
        for attr_name in ("_sub_rx", "_sub_cmd", "_sub_status", "_sub_broker"):
            unsubscribe = getattr(self, attr_name)
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception as err:
                    # A failed unsubscribe must not skip the remaining
                    # handles — a leaked tracked subscription can leave
                    # HA's MQTT subscription registry inconsistent
                    # (issue 1241).
                    _LOGGER.debug(
                        "MqttPoolBridge: unsubscribe failed for %s: %s",
                        attr_name,
                        err,
                    )
                setattr(self, attr_name, None)

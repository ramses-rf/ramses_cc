#!/usr/bin/env python3
"""RAMSES RF - MQTT-based packet transport."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime as dt, timedelta as td
from logging.handlers import RotatingFileHandler
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, unquote, urlparse

from paho.mqtt import MQTTException, client as mqtt

try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    # Fallback for Paho MQTT < 2.0.0 (Home Assistant compatibility)
    CallbackAPIVersion = None  # type: ignore[assignment, misc]

from .. import exceptions as exc
from ..const import (
    DUTY_CYCLE_DURATION,
    MAX_TRANSMIT_RATE_TOKENS,
    SZ_ACTIVE_HGI,
    SZ_IS_EVOFW3,
    SZ_RAMSES_GATEWAY,
)
from .base import TransportConfig, _FullTransport
from .helpers import _normalise

if TYPE_CHECKING:
    from ..protocol import RamsesProtocolT

_LOGGER = logging.getLogger(__name__)

# --- Raw MQTT frame logger (before any parsing/validation) ---
_RAW_LOG_PATH = "/config/ramses_raw_mqtt.log"
_RAW_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

_raw_logger = logging.getLogger("ramses_rf.raw_mqtt_log")
_raw_logger.setLevel(logging.DEBUG)
_raw_logger.propagate = False  # don't send to root logger
try:
    _raw_handler = RotatingFileHandler(
        _RAW_LOG_PATH, maxBytes=_RAW_LOG_MAX_BYTES, backupCount=1
    )
    _raw_handler.setFormatter(logging.Formatter("%(message)s"))
    _raw_logger.addHandler(_raw_handler)
except OSError:
    pass  # can't write to log file, fail silently


def _log_raw_mqtt(frame: str) -> None:
    """Log every raw MQTT frame to a file before any parsing."""
    _raw_logger.debug(f"{dt.now().isoformat(timespec='seconds')} {frame}")


# NOTE: All debug flags should be False for deployment to end-users
_DBG_FORCE_FRAME_LOGGING: Final[bool] = False


def validate_topic_path(path: str) -> str:
    """Test the topic path and normalize it.

    :param path: The candidate topic path.
    :type path: str
    :return: The valid, normalized path.
    :rtype: str
    :raises TransportMqttError: If the path format is invalid.
    """
    new_path = path or SZ_RAMSES_GATEWAY
    if new_path.startswith("/"):
        new_path = new_path[1:]
    if not new_path.startswith(SZ_RAMSES_GATEWAY):
        raise exc.TransportMqttError(f"Invalid topic path: {path}")
    if new_path == SZ_RAMSES_GATEWAY:
        new_path += "/+"
    if len(new_path.split("/")) != 3:
        raise exc.TransportMqttError(f"Invalid topic path: {path}")
    return new_path


class _MqttTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying class."""

    def __init__(
        self,
        broker_url: str,
        protocol: RamsesProtocolT,
        /,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the MQTT transport abstractor."""
        self._broker_url = urlparse(broker_url)
        self._protocol = protocol
        self._loop = loop or asyncio.get_event_loop()


class MqttTransport(_FullTransport, _MqttTransportAbstractor):
    """Send/receive packets to/from ramses_esp via MQTT.
    For full RX logging, turn on debug logging.

    See: https://github.com/IndaloTech/ramses_esp
    """

    # used in .write_frame() to rate-limit the number of writes
    _MAX_TOKENS: Final[int] = MAX_TRANSMIT_RATE_TOKENS
    _TIME_WINDOW: Final[int] = DUTY_CYCLE_DURATION
    _TOKEN_RATE: Final[float] = _MAX_TOKENS / _TIME_WINDOW

    def __init__(
        self,
        broker_url: str,
        protocol: RamsesProtocolT,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the MQTT transport."""
        _MqttTransportAbstractor.__init__(self, broker_url, protocol, loop=loop)
        _FullTransport.__init__(self, config=config, extra=extra, loop=loop)

        self._username = unquote(self._broker_url.username or "")
        self._password = unquote(self._broker_url.password or "")

        self._topic_base = validate_topic_path(self._broker_url.path)
        self._topic_pub = ""
        self._topic_sub = ""
        self._data_wildcard_topic = ""

        qos_val = parse_qs(self._broker_url.query).get("qos", ["0"])[0]
        self._mqtt_qos = int(qos_val)

        self._connected = False
        self._connecting = False
        self._connection_established = False
        self._extra[SZ_IS_EVOFW3] = True

        # Reconnection settings
        self._reconnect_interval = 5.0  # seconds
        self._max_reconnect_interval = 300.0  # 5 minutes max
        self._reconnect_backoff = 1.5
        self._current_reconnect_interval = self._reconnect_interval
        self._reconnect_task: asyncio.Task[None] | None = None

        self._timestamp = perf_counter()
        self._max_tokens: float = self._MAX_TOKENS * 2
        self._num_tokens: float = self._MAX_TOKENS * 2

        self._log_all = config.log_all

        self.client = mqtt.Client(
            protocol=mqtt.MQTTv5,
            callback_api_version=CallbackAPIVersion.VERSION2,
        )
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.username_pw_set(self._username, self._password)

        self._attempt_connection()

    def _attempt_connection(self) -> None:
        """Attempt to connect to the MQTT broker."""
        if self._connecting or self._connected:
            return

        self._connecting = True
        try:
            self.client.connect_async(
                str(self._broker_url.hostname or "localhost"),
                self._broker_url.port or 1883,
                60,
            )
            self.client.loop_start()
        except (ValueError, OSError, MQTTException) as err:
            _LOGGER.exception("Failed to initiate MQTT connection: %s", err)
            self._connecting = False
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt with exponential backoff."""
        if self._closing or self._reconnect_task:
            return

        _LOGGER.info(
            "Scheduling MQTT reconnect in %s seconds",
            self._current_reconnect_interval,
        )
        self._reconnect_task = self._loop.create_task(
            self._reconnect_after_delay(),
            name="MqttTransport._reconnect_after_delay()",
        )

    async def _reconnect_after_delay(self) -> None:
        """Wait and then attempt to reconnect."""
        try:
            await asyncio.sleep(self._current_reconnect_interval)

            self._current_reconnect_interval = min(
                self._current_reconnect_interval * self._reconnect_backoff,
                self._max_reconnect_interval,
            )

            _LOGGER.info("Attempting MQTT reconnection...")
            self._attempt_connection()
        except asyncio.CancelledError:
            pass
        finally:
            self._reconnect_task = None

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: Any,
        properties: Any | None,
    ) -> None:
        """Handle MQTT connection success."""
        self._connecting = False

        if reason_code.is_failure:
            _LOGGER.error("MQTT connection failed: %s", reason_code.getName())
            self._schedule_reconnect()
            return

        _LOGGER.info("MQTT connected: %s", reason_code.getName())

        self._current_reconnect_interval = self._reconnect_interval

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        self.client.subscribe(self._topic_base)

        if self._topic_base.endswith("/+") and not (
            hasattr(self, "_topic_sub") and self._topic_sub
        ):
            data_wildcard = self._topic_base.replace("/+", "/+/rx")
            self.client.subscribe(data_wildcard, qos=self._mqtt_qos)
            self._data_wildcard_topic = data_wildcard
            _LOGGER.debug("Subscribed to data wildcard: %s", data_wildcard)

        if hasattr(self, "_topic_sub") and self._topic_sub:
            self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)
            _LOGGER.debug("Re-subscribed to specific topic: %s", self._topic_sub)
            if getattr(self, "_data_wildcard_topic", ""):
                try:
                    self.client.unsubscribe(self._data_wildcard_topic)
                    _LOGGER.debug(
                        "Unsubscribed data wildcard after specific subscribe: %s",
                        self._data_wildcard_topic,
                    )
                except (ValueError, MQTTException) as err:
                    _LOGGER.exception("Error unsubscribing data wildcard: %s", err)
                finally:
                    self._data_wildcard_topic = ""

    def _on_connect_fail(
        self,
        client: mqtt.Client,
        userdata: Any,
    ) -> None:
        """Handle MQTT connection failure."""
        _LOGGER.error("MQTT connection failed")

        self._connecting = False
        self._connected = False

        if not self._closing:
            self._schedule_reconnect()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Handle MQTT disconnection."""
        reason_code = args[0] if len(args) >= 1 else None

        reason_name = (
            reason_code.getName()
            if reason_code is not None and hasattr(reason_code, "getName")
            else str(reason_code)
        )
        _LOGGER.warning("MQTT disconnected: %s", reason_name)

        was_connected = self._connected
        self._connected = False

        if was_connected and hasattr(self, "_topic_sub") and self._topic_sub:
            device_topic = self._topic_sub[:-3]
            _LOGGER.warning("%s: the MQTT device is offline: %s", self, device_topic)

            if hasattr(self, "_protocol"):
                self._protocol.pause_writing()

        if not self._closing:
            self._schedule_reconnect()

    def _create_connection(self, msg: mqtt.MQTTMessage) -> None:
        """Invoke the Protocols's connection_made() callback MQTT is
        established.
        """
        assert msg.payload == b"online", "Coding error"

        if self._connected:
            _LOGGER.info("MQTT device came back online - resuming writing")
            self._loop.call_soon_threadsafe(self._protocol.resume_writing)
            return

        _LOGGER.info("MQTT device is online - establishing connection")
        self._connected = True

        self._extra[SZ_ACTIVE_HGI] = msg.topic[-9:]

        self._topic_pub = msg.topic + "/tx"
        self._topic_sub = msg.topic + "/rx"

        self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)

        if getattr(self, "_data_wildcard_topic", ""):
            try:
                self.client.unsubscribe(self._data_wildcard_topic)
                _LOGGER.debug(
                    "Unsubscribed data wildcard after device online: %s",
                    self._data_wildcard_topic,
                )
            except (ValueError, MQTTException) as err:
                _LOGGER.exception("Error unsubscribing data wildcard: %s", err)
            finally:
                self._data_wildcard_topic = ""

        if not self._connection_established:
            self._connection_established = True
            self._make_connection(gwy_id=msg.topic[-9:])  # type: ignore[arg-type]
        else:
            _LOGGER.info("MQTT reconnected - protocol connection already established")

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        """Make a Frame from the MQTT message and process it."""
        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning("Rx: %s", msg.payload)
        elif self._log_all and _LOGGER.getEffectiveLevel() == logging.INFO:
            _LOGGER.info("mq Rx: %s", msg.payload)

        if msg.topic[-3:] != "/rx":
            if msg.payload == b"offline":
                if (
                    self._topic_sub and msg.topic == self._topic_sub[:-3]
                ) or not self._topic_sub:
                    _LOGGER.warning(
                        "%s: the ESP device is offline (via LWT): %s",
                        self,
                        msg.topic,
                    )
                    if hasattr(self, "_protocol"):
                        self._protocol.pause_writing()

            elif msg.payload == b"online":
                _LOGGER.info(
                    "%s: the ESP device is online (via status): %s",
                    self,
                    msg.topic,
                )
                self._create_connection(msg)

            return

        if not self._connection_established and msg.topic.endswith("/rx"):
            topic_parts = msg.topic.split("/")
            if len(topic_parts) >= 3 and topic_parts[-2] not in ("+", "*"):
                gateway_id = topic_parts[-2]
                _LOGGER.info(
                    "Inferring gateway connection from data topic: %s",
                    gateway_id,
                )

                self._topic_pub = f"{'/'.join(topic_parts[:-1])}/tx"
                self._topic_sub = msg.topic
                self._extra[SZ_ACTIVE_HGI] = gateway_id

                self._connected = True
                self._connection_established = True
                self._make_connection(gwy_id=gateway_id)  # type: ignore[arg-type]

                try:
                    self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)
                except (ValueError, MQTTException) as err:
                    _LOGGER.exception("Error subscribing specific topic: %s", err)
                if getattr(self, "_data_wildcard_topic", ""):
                    try:
                        self.client.unsubscribe(self._data_wildcard_topic)
                        _LOGGER.debug(
                            "Unsubscribed data wildcard after inferring device: %s",
                            self._data_wildcard_topic,
                        )
                    except (ValueError, MQTTException) as err:
                        _LOGGER.exception("Error unsubscribing wildcard: %s", err)
                    finally:
                        self._data_wildcard_topic = ""

        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            _LOGGER.warning("%s < Can't decode JSON (ignoring)", msg.payload)
            return

        dtm = dt.fromisoformat(payload["ts"])
        if dtm.tzinfo is not None:
            dtm = dtm.astimezone().replace(tzinfo=None)
        if dtm < dt.now() - td(days=90):
            _LOGGER.warning(
                "%s: Have you configured the SNTP settings on the ESP?",
                self,
            )

        try:
            _log_raw_mqtt(payload["msg"])
            self._frame_read(dtm.isoformat(), _normalise(payload["msg"]))
        except exc.TransportError:
            if not self._closing:
                raise

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler (e.g. serial port, MQTT)."""
        if not self._connected:
            raise exc.TransportStateError("Cannot write frame: MQTT not connected")

        timestamp = perf_counter()
        elapsed, self._timestamp = timestamp - self._timestamp, timestamp
        self._num_tokens = min(
            self._num_tokens + elapsed * self._TOKEN_RATE, self._max_tokens
        )

        if self._num_tokens < 1.0 - self._TOKEN_RATE and not disable_tx_limits:
            _LOGGER.warning(
                "%s: Discarding write (tokens=%.2f)",
                self,
                self._num_tokens,
            )
            return

        self._num_tokens -= 1.0
        if self._max_tokens > self._MAX_TOKENS:
            self._max_tokens = min(self._max_tokens, self._num_tokens)
            self._max_tokens = max(self._max_tokens, self._MAX_TOKENS)

        if self._num_tokens < 0.0 and not disable_tx_limits:
            delay = (0 - self._num_tokens) / self._TOKEN_RATE
            _LOGGER.debug("%s: Sleeping (seconds=%s)", self, delay)
            await asyncio.sleep(delay)

        await super().write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport."""
        data = json.dumps({"msg": frame})

        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning("Tx: %s", data)
        elif _LOGGER.getEffectiveLevel() == logging.INFO or self._log_all:
            _LOGGER.info("mq Tx: %s", data)

        try:
            self._publish(data)
        except (ValueError, MQTTException) as err:
            _LOGGER.exception("MQTT publish failed: %s", err)
            self._connected = False
            if not self._closing:
                self._schedule_reconnect()
            return

    def _publish(self, payload: str) -> None:
        """Publish the payload to the MQTT broker."""
        if not self._connected:
            raise exc.TransportStateError("Cannot publish: MQTT not connected")

        info: mqtt.MQTTMessageInfo = self.client.publish(
            self._topic_pub, payload=payload, qos=self._mqtt_qos
        )

        if not info:
            _LOGGER.warning("MQTT publish returned no info")
        elif info.rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning("MQTT publish failed with code: %s", info.rc)
            if info.rc in (mqtt.MQTT_ERR_NO_CONN, mqtt.MQTT_ERR_CONN_LOST):
                self._connected = False
                if not self._closing:
                    self._schedule_reconnect()

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Close the transport (disconnect from the broker and stop its poller)."""
        super()._close(exc)

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if not self._connected:
            return
        self._connected = False

        try:
            self.client.unsubscribe(self._topic_sub)
            self.client.disconnect()
            self.client.loop_stop()
        except (ValueError, MQTTException) as err:
            _LOGGER.exception("Error during MQTT cleanup: %s", err)

"""The Bridge between Home Assistant MQTT and ramses_rf."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from ramses_tx.transport import CallbackTransport, TransportConfig

if TYPE_CHECKING:
    from homeassistant.components.mqtt import PublishPayloadType

_LOGGER = logging.getLogger(__name__)


class RamsesMqttBridge:
    """Isolates all MQTT translation logic."""

    def __init__(self, hass: HomeAssistant, topic_prefix: str, device_id: str) -> None:
        """Initialize the bridge."""
        self._hass = hass
        self._topic_prefix = topic_prefix.rstrip("/")
        self._device_id = device_id
        self._protocol: asyncio.Protocol | None = None
        self._transport: CallbackTransport | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._unsubscribe_status: Callable[[], None] | None = None

        # Subscriptions
        self._sub_rx: Callable[[], None] | None = None
        self._sub_cmd: Callable[[], None] | None = None
        self._sub_status: Callable[[], None] | None = None

    @property
    def device_id(self) -> str:
        """Return the configured device ID."""
        return self._device_id

    async def async_transport_factory(
        self,
        protocol: asyncio.Protocol,
        disable_sending: bool = False,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> CallbackTransport:
        """The factory method passed to ramses_rf.Gateway.

        This method initializes the transport layer, attaching it to the
        MQTT protocol and defining the IO writer for sending packets.

        :param protocol: The protocol instance to be used by the transport.
        :param disable_sending: If True, the transport will not send data.
        :param extra: Optional dictionary of extra attributes/configuration.
        :param kwargs: Additional keyword arguments passed to the transport.
        :return: An instantiated CallbackTransport.
        """
        _LOGGER.debug(
            "MqttBridge: async_transport_factory called for protocol %s", type(protocol)
        )
        self._protocol = protocol

        # 1. Ensure we are subscribed to MQTT *before* starting the transport
        #    This prevents missing the response to the initial handshake (!V)
        await self._async_attach()

        # 2. Define the IO Writer (Step A in API Guide)
        async def mqtt_packet_sender(frame: str) -> None:
            """Callback for ramses_rf to send data via MQTT."""
            # The firmware separates "Commands" (!V, !C) from "Radio Packets".
            # If the data starts with '!', send to cmd topic. Otherwise, tx topic.
            if frame.startswith("!"):
                _LOGGER.debug("MqttTransport: Sending Command -> %s", frame)
                self.publish_command(frame)
            else:
                # Wrap in JSON for the /tx topic as per ramses_esp expectation
                try:
                    json_payload = json.dumps({"msg": frame + "\r\n"})
                    _LOGGER.debug("MqttTransport: TX (frame) -> %s", json_payload)
                    self.publish_tx(json_payload)
                except TypeError as err:
                    _LOGGER.error("MqttTransport: Failed to JSON encode frame: %s", err)

        # 3. Instantiate CallbackTransport (Step B in API Guide)

        # Extract config from kwargs if provided (e.g. by ramses_rf.Gateway),
        # otherwise create a default one (e.g. during standalone tests).
        config = kwargs.pop("config", None)
        if config is None:
            config = TransportConfig(
                disable_sending=disable_sending,
            )

        # Remove old kwargs that CallbackTransport.__init__ no longer accepts
        kwargs.pop("autostart", None)

        self._transport = CallbackTransport(
            protocol,
            mqtt_packet_sender,  # <-- Passed as POSITIONAL argument
            config=config,  # <-- Passed via the new config object
            extra=extra,
            **kwargs,
        )

        return self._transport

    async def _async_attach(self) -> None:
        """Start listening to MQTT."""
        # Prevent double subscription
        if self._sub_rx and self._sub_cmd:
            return

        # Topic 1: Radio Packets (RAMSES/GATEWAY/ID/rx)
        topic_rx = f"{self._topic_prefix}/{self._device_id}/rx"
        _LOGGER.debug("MqttBridge: Starting subscription to %s", topic_rx)

        # Topic 2: Command Results (RAMSES/GATEWAY/ID/cmd/result)
        # Matches main.cpp: snprintf(cmd_result_topic, ..., "%s/cmd/result", base_topic);
        topic_cmd = f"{self._topic_prefix}/{self._device_id}/cmd/result"
        _LOGGER.debug("MqttBridge: Starting subscription to %s", topic_cmd)

        try:
            # Subscribe to RX
            self._sub_rx = await mqtt.async_subscribe(
                self._hass, topic_rx, self._handle_rx_message, qos=0
            )
            _LOGGER.info("MqttBridge: Successfully subscribed to %s", topic_rx)
            # Subscribe to Command Results
            self._sub_cmd = await mqtt.async_subscribe(
                self._hass, topic_cmd, self._handle_cmd_message, qos=0
            )
            _LOGGER.info("MqttBridge: Successfully subscribed to %s", topic_cmd)

            self._sub_status = mqtt.async_subscribe_connection_status(
                self._hass, self._handle_connection_status
            )
            _LOGGER.info("MqttBridge: Successfully subscribed to connection status")

        except Exception as err:
            _LOGGER.error("MqttBridge: Failed to subscribe to MQTT: %s", err)

    @callback
    def _handle_rx_message(self, msg: Any) -> None:
        """Process incoming radio packets."""
        if self._transport is None:
            _LOGGER.warning("MqttBridge RX: Transport is None, dropping message")
            return

        payload_str = self._extract_payload(msg)
        if not payload_str:
            return

        # ramses_esp wraps RX in JSON: {"msg": "..."}
        try:
            data = json.loads(payload_str)
            if isinstance(data, dict) and "msg" in data:
                raw_line = data["msg"]
                # PACKET STRUCTURE RULE (from Packet Structure Wiki):
                # The Verb field is strictly 2 characters wide.
                # - "RQ", "RP", " W" (space W), " I" (space I).
                # - We must preserve internal whitespace (e.g. "059  I") to maintain this alignment.
                # - However, we MUST strip leading/trailing garbage (newlines, nulls) to avoid parser errors.
                # ramses_rf expects a serial stream ending in exactly \r\n
                frame = raw_line.strip() + "\r\n"
                # Log exact repr() to reveal hidden characters or malformed line endings
                _LOGGER.debug("MqttBridge: RX <- %s", repr(frame))

                # Feed inbound data (Step D in API Guide)
                self._transport.receive_frame(frame)

        except json.JSONDecodeError as err:
            _LOGGER.debug("MqttBridge RX: Failed to decode JSON payload: %s", err)
        except UnicodeEncodeError as err:
            _LOGGER.error("MqttBridge RX: Encoding error in frame: %s", err)
        except Exception as err:
            _LOGGER.exception(
                "MqttBridge RX: Unexpected error processing MQTT message: %s", err
            )

    @callback
    def _handle_cmd_message(self, msg: Any) -> None:
        """Process incoming MQTT messages and inject into ramses_rf."""
        if self._transport is None:
            _LOGGER.warning("MqttBridge CMD: Transport is None, dropping message")
            return

        payload_str = self._extract_payload(msg)
        if not payload_str:
            return

        try:
            # Unwrap JSON if present (standard ramses_esp format)
            data = json.loads(payload_str)
            if isinstance(data, dict) and "return" in data:
                return_val = data["return"]
                cmd_val = data.get("cmd", "")
                result_str = ""

                # Handle Integer vs String return types
                # Scenario A: Firmware returns an int (e.g. 0)
                # We must convert to string and synthesize a handshake response if needed.
                if isinstance(return_val, int):
                    # If this was a handshake request (!V) and it succeeded (0),
                    # we MUST return a valid evofw3 signature or ramses_rf will abort.
                    if cmd_val == "!V" and return_val == 0:
                        result_str = "# evofw3 0.1.0"  # Fake response for compatibility
                    else:
                        result_str = str(return_val)

                # Scenario B: Firmware returns the actual response string (Your version)
                elif isinstance(return_val, str):
                    result_str = return_val

                # Compatibility: ramses_rf requires 'evofw3' to transition FSM
                if "ramses_esp_eth" in result_str:
                    result_str = result_str.replace("ramses_esp_eth", "evofw3")

                # Re-add the hash if missing, because ramses_rf expects "# evofw3..."
                if not result_str.strip().startswith("#"):
                    result_str = f"# {result_str}"

                # Ensure CRLF
                result_str = result_str.strip() + "\r\n"

                _LOGGER.info("MqttBridge: CMD Response <- %s", repr(result_str))

                # Feed directly to transport
                self._transport.receive_frame(result_str)

        except json.JSONDecodeError as err:
            _LOGGER.debug("MqttBridge CMD: Failed to decode JSON payload: %s", err)
        except UnicodeEncodeError as err:
            _LOGGER.error("MqttBridge CMD: Encoding error in frame: %s", err)
        except Exception as err:
            _LOGGER.exception(
                "MqttBridge CMD: Unexpected error processing MQTT message: %s", err
            )

    def _extract_payload(self, msg: Any) -> str:
        """Helper to decode bytes to string."""
        if isinstance(msg.payload, bytes):
            return msg.payload.decode("utf-8", errors="ignore")
        return str(msg.payload)

    def publish_tx(self, payload: PublishPayloadType) -> None:
        """Publish a radio packet to the /tx topic.

        :param payload: The packet data to publish (string or bytes).
        """
        # Publish to TX topic: {prefix}/{device_id}/tx
        topic = f"{self._topic_prefix}/{self._device_id}/tx"
        self._hass.async_create_task(mqtt.async_publish(self._hass, topic, payload))
        _LOGGER.debug("MqttBridge: TX -> %s, on topic: %s", payload, topic)

    def publish_command(self, payload: PublishPayloadType) -> None:
        """Publish a command to the /cmd/cmd topic.

        :param payload: The command data to publish (string or bytes).
        """
        # Publish to CMD topic: {prefix}/{device_id}/cmd/cmd
        topic = f"{self._topic_prefix}/{self._device_id}/cmd/cmd"
        self._hass.async_create_task(mqtt.async_publish(self._hass, topic, payload))
        _LOGGER.debug("MqttBridge: CMD -> %s, on topic: %s", payload, topic)

    @callback
    def _handle_connection_status(self, status: str) -> None:
        """Handle MQTT broker connection/disconnection."""
        _LOGGER.debug("MqttBridge: Connection status changed to %s", status)
        if status == "online":
            _LOGGER.info("MQTT Broker connected. Resuming ramses_rf.")
            # Send handshake immediately when MQTT comes online
            self.publish_command("!V")
        elif status == "offline":
            _LOGGER.warning("MQTT Broker disconnected. Pausing ramses_rf.")

    def close(self) -> None:
        """Cleanup subscriptions."""
        _LOGGER.debug("MqttBridge: Cleanup called")
        if self._sub_rx:
            self._sub_rx()
        if self._sub_cmd:
            self._sub_cmd()
        if self._sub_status:
            self._sub_status()

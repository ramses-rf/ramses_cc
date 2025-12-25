# Home Assistant MQTT Integration Python API Reference

Reference [Home Assistant MQTT component](https://github.com/home-assistant/core/tree/dev/homeassistant/components/mqtt)

This document provides a reference for the Python API exposed by the Home Assistant MQTT integration (`homeassistant.components.mqtt`). This API allows custom components and scripts to interact with the MQTT broker configured in Home Assistant.

## Import
```
from homeassistant.components import mqtt

```

## 1. Data Models

Data structures used for message handling and type hinting.

### `ReceiveMessage`

The object passed to the callback function when a message is received.

**Source:** `homeassistant.components.mqtt.models.ReceiveMessage`

| **Attribute** | **Type** | **Description** |
| --- | --- | --- |
| **`topic`** | `str` | The topic the message was received on. |
| **`payload`** | `str` | `bytes` |
| **`qos`** | `int` | The Quality of Service level (0, 1, or 2). |
| **`retain`** | `bool` | `True` if the message was a retained message. |
| **`subscribed_topic`** | `str` | The topic subscription that matched this message (useful when using wildcards like `+` or `#`). |
| **`timestamp`** | `float` | The timestamp when the message was received. |

### `PublishPayloadType`

Accepted types for publishing payloads: `str | bytes | int | float | None`

### `PayloadSentinel`

Enum used as a sentinel value when rendering templates to distinguish between a "missing" value and a "default" value.

**Source:** `homeassistant.components.mqtt.models.PayloadSentinel`
- `PayloadSentinel.NONE`
- `PayloadSentinel.DEFAULT`

## 2. Publishing Methods

Methods to send messages to the MQTT broker.

### `async_publish`

Asynchronously publishes a message to a topic. This is the preferred method for modern async components.
```
async def async_publish(
    hass: HomeAssistant,
    topic: str,
    payload: PublishPayloadType,
    qos: int = 0,
    retain: bool = False,
    encoding: str | None = "utf-8"
) -> None

```

**Parameters:**
- **`hass`** (`HomeAssistant`): The Home Assistant instance.
- **`topic`** (`str`): The topic to publish to.
- **`payload`** (`PublishPayloadType`): The message content.
  - If `bytes`, it is sent as-is.
  - If `str` (and starts with `b'` or `b"`), it may be evaluated to bytes.
  - Other types are converted to strings.
- **`qos`** (`int`, optional): Quality of Service level (0, 1, or 2). Defaults to `0`.
- **`retain`** (`bool`, optional): If `True`, the broker will retain the message. Defaults to `False`.
- **`encoding`** (`str`, optional): Encoding to use if payload is a string. Defaults to `"utf-8"`.

**Raises:**
- `HomeAssistantError`: If MQTT is not enabled or set up.

### `publish`

The synchronous version of `async_publish`. Use this only if you are running in a synchronous context (e.g., inside a standard `def` method).
```
def publish(
    hass: HomeAssistant,
    topic: str,
    payload: PublishPayloadType,
    qos: int = 0,
    retain: bool = False,
    encoding: str | None = "utf-8"
) -> None

```

## 3. Subscription Methods

Methods to listen for messages on specific topics.

### `async_subscribe`

Asynchronously subscribes to a topic.
```
async def async_subscribe(
    hass: HomeAssistant,
    topic: str,
    msg_callback: Callable[[ReceiveMessage], Coroutine[Any, Any, None] | None],
    qos: int = 0,
    encoding: str | None = "utf-8"
) -> Callable[[], None]

```

**Parameters:**
- **`hass`** (`HomeAssistant`): The Home Assistant instance.
- **`topic`** (`str`): The topic to subscribe to. Supports wildcards (`+`, `#`).
- **`msg_callback`** (`Callable`): The function to call when a message is received.
  - **Signature:** `callback(msg: ReceiveMessage)`
- **`qos`** (`int`, optional): The requested QoS level. Defaults to `0`.
- **`encoding`** (`str`, optional): The encoding used to decode the received payload.
  - Defaults to `"utf-8"`.
  - Set to `None` to receive the payload as raw `bytes`.

**Returns:**
- A callable function (unsubscriber). Call this function to remove the subscription.

### `subscribe`

The synchronous version of `async_subscribe`. Thread-safe.
```
def subscribe(
    hass: HomeAssistant,
    topic: str,
    msg_callback: Callable[[ReceiveMessage], None],
    qos: int = 0,
    encoding: str = "utf-8"
) -> Callable[[], None]

```

### `async_on_subscribe_done`

Registers a callback that is called when a subscription for a specific topic is acknowledged by the broker.

**Source:** `homeassistant.components.mqtt.client`
```
@callback
def async_on_subscribe_done(
    hass: HomeAssistant,
    topic: str,
    qos: int,
    on_subscribe_status: Callable[[], None]
) -> Callable[[], None]

```

**Parameters:**
- **`hass`** (`HomeAssistant`): The Home Assistant instance.
- **`topic`** (`str`): The topic subscription to track.
- **`qos`** (`int`): The QoS level of the subscription.
- **`on_subscribe_status`** (`Callable[[], None]`): The callback to execute when the subscription is done.

**Returns:**
- A callable function (unsubscriber) to cancel the listener.

## 4. Template Helpers

Helper classes for components that need to handle MQTT templates for values or commands.

### `MqttValueTemplate`

Used for rendering incoming MQTT payloads (e.g., extracting values from JSON).

**Source:** `homeassistant.components.mqtt.models`
```
class MqttValueTemplate:
    def __init__(
        self,
        value_template: template.Template | None,
        *,
        entity: Entity | None = None,
        config_attributes: TemplateVarsType = None
    )

```

**Main Method:**
- **`async_render_with_possible_json_value(payload, default=..., variables=...)`**: Renders the template with the received payload. If the payload is JSON, it attempts to parse it so it can be accessed in the template (e.g., `value_json.foo`).

### `MqttCommandTemplate`

Used for rendering outgoing MQTT payloads.

**Source:** `homeassistant.components.mqtt.models`
```
class MqttCommandTemplate:
    def __init__(
        self,
        command_template: template.Template | None,
        *,
        entity: Entity | None = None
    )

```

**Main Method:**
- **`async_render(value, variables=...)`**: Renders the template using the provided value. Returns a valid `PublishPayloadType`.

## 5. Utility & Connection Status

### `convert_outgoing_mqtt_payload`

Ensures the correct raw MQTT payload is passed as bytes for publishing. Useful for sanitizing inputs.

**Source:** `homeassistant.components.mqtt.models`
```
def convert_outgoing_mqtt_payload(
    payload: PublishPayloadType
) -> PublishPayloadType

```

### `is_connected`

Checks if the MQTT client is currently connected to the broker.
```
def is_connected(hass: HomeAssistant) -> bool

```

### `async_wait_for_mqtt_client`

Awaitable helper that returns `True` when the MQTT client is connected and available.
```
async def async_wait_for_mqtt_client(hass: HomeAssistant) -> bool

```

### `async_subscribe_connection_status`

Registers a callback to be notified when the MQTT connection state changes.
```
def async_subscribe_connection_status(
    hass: HomeAssistant,
    connection_status_callback: Callable[[bool], None]
) -> Callable[[], None]

```

## 6. Validators

These are `voluptuous` validators exposed by the MQTT component. They are useful when defining configuration schemas (`CONFIG_SCHEMA` or `PLATFORM_SCHEMA`) for your own components.

**Source:** `homeassistant.components.mqtt`
- **`valid_publish_topic`**: Validates that a topic is valid for publishing (no wildcards allowed).
- **`valid_subscribe_topic`**: Validates that a topic is valid for subscribing (wildcards `+` and `#` allowed).
- **`valid_qos_schema`**: Validates that the QoS is an integer between 0 and 2.

## 7. Service Calls

These services are exposed to the Home Assistant Event Bus and can be called via `hass.services.call` or from automations.

| Service | Description | Arguments |

| mqtt.publish | Publishes a message. | topic (required), payload, qos, retain, evaluate_payload |

| mqtt.dump | Listens to a topic for a duration and dumps messages to mqtt_dump.txt in the config folder. | topic (required), duration (default: 5s) |

| mqtt.reload | Reloads the MQTT YAML configuration and entities. | None |
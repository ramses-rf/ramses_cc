# Design Document: Home Assistant MQTT Support for RAMSES-RF

## 1. Objective

Enable `ramses_rf` (and by extension `ramses_cc`) to utilize the native Home Assistant MQTT service without creating a hard dependency on Home Assistant within the core library.

Key Philosophy: Inversion of Control (IoC)

Instead of ramses_rf deciding how to connect to MQTT, it will simply accept a generic "Connection Builder" (Transport Factory) injected by the external integration (ramses_cc). This ensures:
1. **No Breaking Changes:** `ramses_rf` remains standalone and platform-agnostic.
2. **Decoupling:** `ramses_cc` handles all HA-specific logic (JSON unpacking, topic monitoring, entity management).
3. **Stability:** The core library stops/starts operations based on the external service's availability (Circuit Breaker pattern).

## 2. Architecture Overview

### 2.1. Inversion of Control Pattern
- **Current Behavior:** `ramses_rf` inspects its configuration to decide whether to instantiate a Serial transport or a standard Paho MQTT transport.
- **New Behavior:** `ramses_rf` will first check if a `transport_constructor` argument has been passed. If present, it delegates the transport creation entirely to that external callable, bypassing its internal logic.

### 2.2. Component Responsibilities

| **Component** | **Responsibility** |
| --- | --- |
| `ramses_rf` | Defines the generic interface (CallbackTransport) for sending/receiving data. It is passive regarding the connection source. |
| `ramses_cc` | Acts as the Bridge. It creates the transport, translates HA MQTT payloads (JSON) into raw packet strings, ensures thread safety with the Event Loop, and manages the "Circuit Breaker" (Pause/Resume). |

## 3. Implementation Specification: `ramses_rf` (The Core)

### 3.1. The Generic Interface: `CallbackTransport`

**Target File:** `src/ramses_tx/transport.py`

A new transport class must be implemented that acts as a "Virtual Serial Port". It must:
- Inherit from the standard transport base classes (`_FullTransport`, `_ReadTransport`).
- Accept an `io_writer` callback during initialization.
- **Write Path:** When the protocol wants to send a frame, this transport must invoke the `io_writer` callback with the raw byte data.
- **Read Path:** Expose a public method (`receive_frame`) that allows an external caller to inject raw packet strings into the protocol engine.
- **Initial State:** Default to a **PAUSED** state upon instantiation. It should only start processing when explicitly told to resume by the external integrator.

### 3.2. Injecting the Transport

**Target File:** `src/ramses_rf/gateway.py` & `src/ramses_tx/transport.py`

The `Gateway` class and the underlying `transport_factory` must be updated to accept a `transport_constructor` parameter in their `**kwargs`.
- If this parameter is detected, the factory must execute it immediately and return the result.
- This mechanism replaces the need for complex URL parsing or port name validation when using external transports.

## 4. Implementation Specification: `ramses_cc` (The Integration)

### 4.1. The Bridge Class

**Target File:** `custom_components/ramses_cc/broker.py`

A "Bridge" class (or set of functions) must be created to handle the translation between Home Assistant and `ramses_rf`.
- **Dependency Check:** The bridge must verify that `homeassistant.components.mqtt` is loaded before attempting to import it. If missing, it must log a warning and abort the specific MQTT setup (falling back to a disconnected state or error message) rather than crashing the integration.
- **Topic Subscription:** The bridge must listen to user-configured topics (e.g., `RAMSES/GATEWAY/#`) rather than hardcoded defaults.
- **JSON Unpacking:** Parse incoming JSON payloads (standard for `ramses_esp` devices) to extract the raw packet string (e.g., `msg`) and timestamp.
  - **Resilience:** The unpacking logic must be wrapped in `try/except` blocks to catch `json.JSONDecodeError` or `UnicodeDecodeError`. Malformed packets must be logged as warnings but **must not** crash the bridge or stop the event loop.
- **Gateway Identification:** Parse the MQTT topic string to extract the Gateway ID (e.g., `18:xxxxxx`) and inject this into the `ramses_rf` configuration (`SZ_ACTIVE_HGI`).
- **Event Loop Sync:** Ensure all callbacks (RX and TX) are executed on the main Home Assistant Event Loop (`hass.loop`) to prevent thread safety issues.

### 4.2. Lifecycle Management (The Circuit Breaker)

To prevent "Entity Unavailable" errors and "Ghost" commands, the integration must strictly manage the library's lifecycle:
1. **Delayed Start:** The `ramses_rf` Gateway must not be initialized until the Home Assistant MQTT client is confirmed to be connected and available.
2. **Circuit Breaker:** The integration must subscribe to the MQTT Status topic (`homeassistant/status`).
  - **On "offline" (Will Message):** Immediately call `pause_writing()` on the protocol and `pause_reading()` on the transport.
  - **On "online" (Birth Message):** Call `resume_writing()` and `resume_reading()`.

### 4.3. Configuration & Manifest

**Target File:** `custom_components/ramses_cc/config_flow.py` & `custom_components/ramses_cc/manifest.json`
- **Manifest (Soft Dependency):**
  - Do **NOT** add `"dependencies": ["mqtt"]`. This would prevent the integration from loading for USB/Serial users if MQTT is missing.
  - **Instead**, use `"after_dependencies": ["mqtt"]`. This ensures that *if* MQTT is present, it is loaded before `ramses_cc`.
- **Config Flow (Runtime Check):**
  - Add a user-selectable option for "Home Assistant MQTT".
  - **Validation:** When this option is selected, the config flow must check if the MQTT integration is loaded (`if "mqtt" not in self.hass.config.components:`).
  - **Error Handling:** If MQTT is missing, display a user-friendly error ("MQTT integration not found. Please set up the official MQTT integration first.") and prevent the user from proceeding with this specific option.
  - **Topic Configuration:** Provide a field for the user to specify the **Topic Root** (default: `RAMSES/GATEWAY`).

## 5. Development & Diagnostics Standards

This project involves complex interaction between three systems (HA Core, `ramses_cc`, `ramses_rf`). The following standards are mandatory for all development.

### 5.1. Logging & Observability

Detailed logging is required to trace data flow across system boundaries.
- **Traceability:** Significant functions in the "Bridge" layer must log their entry, exit, and parameters at `DEBUG` level.
- **Boundary Logging:**
  - **Incoming:** Log the exact MQTT payload received from Home Assistant before decoding.
  - **Outgoing:** Log the exact bytes being handed to the `io_writer` callback.
- **Asyncio Debugging:** During the development phase, developers should enable Python's asyncio debug mode (`PYTHONASYNCIODEBUG=1`) to detect if the Bridge is blocking the main event loop for too long.
- **Object Lifecycle:** Log the creation and destruction of the `CallbackTransport` and `Gateway` instances at `INFO` level to detect restart loops.
- **Implementation:** Use a dedicated logger for the bridge (e.g., `logging.getLogger("custom_components.ramses_cc.mqtt_bridge")`) to allow targeted debugging.

### 5.2. Git Workflow

To ensure a clean history and easy rollback:
- **Atomic Commits:** Changes should be broken down into small, logical units (e.g., "Add CallbackTransport class" vs. "Update Config Flow").
- **Commit Messages:** Every code update provided by the AI assistant must include a suggested Git commit message.
- **Format:** Use Conventional Commits format:
  - `feat: ...` for new features.
  - `fix: ...` for bug fixes.
  - `docs: ...` for documentation changes.
  - `refactor: ...` for code restructuring without behavior change.

### 5.3. Code Documentation Standards

All code artifacts must include docstrings adhering to the **Strict Sphinx (reStructuredText)** standard. This ensures compatibility with auto-documentation tools and maintains high maintainability.
- **Format:** Use standard Sphinx syntax (`:param`, `:type`, etc.).
- **Structure:**
  1. **Summary:** A concise summary on the first line.
  2. **Spacer:** A mandatory blank line.
  3. **Detail:** A detailed explanation of logic (if complex).
  4. **Fields:** Parameter and return value definitions.
- **Required Fields:**
  - `:param <name>:` Description of the parameter.
  - `:type <name>:` The data type (must match Python type hints).
  - `:returns:` Description of what the function returns.
  - `:rtype:` The return data type.
  - `:raises <Exception>:` Document any specific exceptions raised.
- **Type Hinting:** Docstrings must reflect and align with the Python type hints used in the function signature.

### 5.4. Testing Strategy
- **Mocking:** Since `ramses_rf` will now accept an injected transport, unit tests should pass a **Mock Transport Constructor**. This allows testing the `ramses_rf` logic without requiring a real running Home Assistant instance.

## 6. References

The following documents contain detailed research, API references, and specific strategies used to derive this design.
- **High-Level Strategy; Inversion of Control for ramses_rf.md**: Explains the rationale behind using Dependency Injection to avoid coupling the library to Home Assistant.
- **HA's mqtt Service.md**: Details the "Reliable Handshake" workflow (Birth/Will messages) required for robust HACS integrations.
- **Home Assistant MQTT Integration Python API Reference.md**: Technical documentation for `homeassistant.components.mqtt`, including `async_publish`, `async_subscribe`, and `async_wait_for_mqtt_client`.
- **To configure MQTT topics compatible with ramses_rf.md**: Describes the expected topic hierarchy (`RAMSES/GATEWAY/...`) and JSON payload format used by the existing `ramses_rf` ecosystem.
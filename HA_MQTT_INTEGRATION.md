# Home Assistant MQTT Integration for RAMSES RF

The `ramses_cc` integration now supports using Home Assistant's native MQTT service to communicate with your hardware (evofw3/RAMSES_ESP). This eliminates the need to configure a separate MQTT broker connection inside the library and ensures better stability by leveraging Home Assistant's existing connection management.

## Key Benefits
* **One Connection:** Reuses the existing MQTT connection configured in Home Assistant.
* **Stability:** Automatically pauses communication if the broker disconnects, preventing errors.
* **Simplicity:** No need to enter broker IP, port, username, or password again.

## Prerequisites
1.  **MQTT Integration:** The official [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) must be installed and running in Home Assistant.
2.  **Hardware:** A compatible gateway (e.g., RAMSES_ESP running on an ESP32) publishing packet data.

## Configuration

### Method 1: New Installation
1.  Add the **RAMSES RF** integration from the devices menu.
2.  Select **MQTT (Home Assistant)** from the connection menu.
    * *Note: This option is only visible if the official MQTT integration is set up.*
3.  Enter your **Topic Root** (Default: `RAMSES/GATEWAY`).
    * This must match the topic your hardware stick uses (e.g., if your stick publishes to `home/heating/18:123456/...`, your root is `home/heating`).

### Method 2: Migrating from Direct MQTT
If you are currently using the "Direct MQTT" option (where you manually entered the broker IP/User/Pass):
1.  Go to **Settings > Devices & Services > RAMSES RF**.
2.  Click **Configure**.
3.  Select **Reconfigure Connection**.
4.  Choose **MQTT (Home Assistant)**.

## Topic Structure
The integration expects the standard `ramses_esp` topic format:

* **RX (Incoming):** `{root}/{device_id}/rx`
    * Payload: JSON `{"ts": "...", "msg": "..."}`
* **TX (Outgoing):** `{root}/{device_id}/tx`
    * Payload: JSON `{"msg": "..."}`

**Example:**
If your root is `RAMSES/GATEWAY` and your gateway ID is `18:123456`:
* Listen: `RAMSES/GATEWAY/18:123456/rx`
* Publish: `RAMSES/GATEWAY/18:123456/tx`

## Troubleshooting

### "Integration Failed to Start"
* **Cause:** The Home Assistant MQTT integration is not loaded or is not connected to the broker.
* **Fix:** Check **Settings > Devices & Services > MQTT**. Ensure it shows as "Connected". The RAMSES integration waits for this connection before starting.

### "Device Unavailable"
* **Cause:** The integration monitors the connection status. If Home Assistant loses connection to the broker, RAMSES RF entities will become unavailable to prevent "ghost" commands.
* **Fix:** Restore your broker connection. The entities will automatically recover when the connection is restored.

### Gateway Not Discovered
* **Cause:** The integration relies on receiving data to identify the active gateway.
* **Fix:** Ensure your hardware stick is powered on and publishing to the correct topic. You can use an MQTT client (like MQTT Explorer) to verify that messages are appearing under your configured **Topic Root**.
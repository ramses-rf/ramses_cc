# Usage

## Installation

To use ramses_cc, install it in Home Assistant either via HACS or using the `update.install` Action:

```yaml
   action: update.install
   target:
      entity_id: update.ramses_cc_update
   data:
      version: 0.52.1
```

### Supported Transceivers & Installation Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :---: | :--- |
| **Transceiver Hardware** | Hardware | Yes | Honeywell HGI80, SSM-D2 / evofw3 dongle, or Indalo Tech ramses_esp (ESP32-S3). |
| **Local USB Port** | String / Path | Optional | Path to local serial port (e.g. `/dev/serial/by-id/usb-...`, `COM3`). Default baud rate `115200` (evofw3/ramses_esp) or `57600` (HGI80). |
| **Network Serial Port** | URI / URL | Optional | Remote serial socket using `rfc2217://<host>:<port>`, `socket://<host>:<port>`, or `tcp://<host>:<port>`. |
| **Home Assistant MQTT** | Service | Optional | Built-in Home Assistant MQTT integration bridge with broker topic subscription. |
| **Standalone MQTT** | URI / URL | Optional | Custom Paho MQTT broker connection string (`mqtt://[user:pass@]<host>[:port]`). |
| **Zigbee Bridge** | URI / URL | Optional | Zigbee bridge URI (`zigbee://<ieee>/<rx_cluster>/<rx_attribute>/<rx_endpoint>/<tx_cluster>/<tx_attribute>/<tx_endpoint>`). |

### RAMSES ESP firmware detection and Zigbee acknowledgements

Select the paired RAMSES ESP in the Zigbee device picker; manual URL entry
is unnecessary. The picker reads the installed firmware from Home Assistant's
ZHA device registry. It recognizes both `v0.6.0` and ZHA's packed
`0x00060000` notation. Public firmware v0.6.0 and newer uses native APS
acknowledgements when the installed `ramses_rf` transport and the applied
ELECRAM quirk both support native commands. Older, unknown or development
firmware, missing transport support, and unavailable or older quirks retain
legacy text acknowledgements. An advertised newer release does not count
as installed firmware.

The same detection applies when adding a Zigbee device to an HGI pool.
Selecting the same pool bridge again updates its URL without adding a second
connection. After upgrading firmware or installing the matching transport or
quirk, reselect the device to detect its current capabilities. Existing saved
URLs are not silently migrated.

Check **Use legacy Zigbee acknowledgements** in the serial-port configuration
or pool device selector to force the older method. Unchecking it restores
automatic selection. The choice is saved in the URL as `?ack=legacy`.
Manually entered URLs retain the mode explicitly requested by the user.

For supported devices the picker constructs this URL automatically:

```text
zigbee://<ieee>/0xfc00/0x0000/10/0xfc01/0x0000/10?ack=aps
```

The transport uses command 2 for native chunks carrying a message ID, sequence,
total and text; command 3 selects this mode on the bridge. Install the updated
ELECRAM quirk defining these commands as well. Native chunks have at most 30
body bytes. The transport waits for APS delivery instead of generating ASCII
ACK commands. It keeps complete outgoing frames in order, rejects mismatched
or missing chunks, and aborts a frame after a delivery failure. Host waits are
bounded, and the firmware reboots if its stack stops delivering confirmations.
Messages can still be dropped on failure, queue overflow or restart.
URLs without `ack=aps` retain legacy behaviour. Do not manually enable APS
with firmware that requires ASCII ACKs.

The released `ramses-rf` version pinned in `manifest.json` does not contain
these development changes. Updating this integration alone therefore does
not enable APS mode; automatic selection safely retains legacy mode in that
case. Deployment must first package the modified transport and update the
manifest requirement to its published version or available immutable Git
commit. The existing dependency pin is retained until then.

APS confirms delivery to the remote Zigbee stack, not execution by the RF
device. Validate RF echoes/replies and capture actual Zigbee traffic before
deploying this mode. The quirk retains legacy commands for older firmware.

### Configuration & Options Parameters

| Option Parameter | Type | Default | Description |
| :--- | :--- | :---: | :--- |
| `scan_interval` | Integer (seconds) | `1200` | Periodic polling interval (in seconds) for supported passive parameter queries. |
| `packet_log` | File Path | None | Absolute path to a file where raw RAMSES II protocol packet logs are recorded. |
| `restore_cache` | Boolean | `True` | Restore learned device traits and schema from cache upon startup. |
| `advanced_features` | Boolean | `False` | Enable advanced entity creation (e.g. packet debug event entities). |
| `message_events` | Boolean | `False` | Dispatch RAMSES RF packet message events on Home Assistant's event bus. |
| `known_list` | Dictionary | `{}` | Explicit list of authorized device IDs and optional device class / faked traits. |
| `block_list` | Dictionary | `{}` | Explicit list of device IDs to ignore/block from discovery and packet processing. |
| `system_schema` | Dictionary | `{}` | Declarative schema defining controller (CTL), heating/cooling zones, and ventilation topology. |


## Documentation

We use [sphinx](https://www.sphinx-doc.org/en/master/usage/markdown.html) and
MyST [markup](https://myst-parser.readthedocs.io/en/latest/syntax/organising_content.html) to automatically create this code documentation from `docstr` annotations in our python code.

- Activate your virtual environment for ramses_cc as described in the [Wiki](https://github.com/ramses-rf/ramses_cc/wiki).

- Install the extra required dependencies by running ``pip install -r requirements_docs.txt`` so you can build a local set.

- Then, in a Terminal, enter `cd docs/` and run `sphinx-build -b html source build/html`.

- When the operation finishes, you can open the generated files from the `docs/build/html/` folder in a web browser.

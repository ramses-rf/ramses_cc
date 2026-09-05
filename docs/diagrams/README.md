# PooledTransport Flow Diagrams (issue 1119)

Mermaid flow diagrams for the multi-HGI gateway pool support.

These diagrams illustrate the **target architecture** from the new plan
(`multi-hgi-plan.md`), not the current PR implementation. The plan is
phased: Phase 1 (MQTT-only pool) is the first release, Phase 2 adds
serial/hybrid, Phase 3 adds Zigbee. The diagrams show the complete target
state across all phases. Key differences from the current implementation:

- **Typed pre-serialization routing** instead of ASCII frame parsing
- **`SourcePolicy.GATEWAY` vs `PRESERVE`** instead of `addr1.startswith("18:")` heuristic
- **`PoolChild` object** instead of parallel arrays
- **Schema ownership** as canonical acceptance authority instead of separate `accepted_hgis`
- **Config-entry reload** for membership changes instead of runtime `add_child()`/`remove_child()`
- **RSSI recorded before dedup but after loopback exclusion** — loopback frames never enter route RSSI
- **RSSI TTL of 5 minutes** (resolved from captured fixtures)
- **Deterministic primary fallback** instead of round-robin as default cold-start
- **HA-native MQTT** via `RamsesMqttBridge` (`homeassistant.components.mqtt`) inside Home Assistant — no direct paho clients for pooled MQTT

## Diagrams

1. **[Inbound: dedup + RSSI tracking](01_inbound_dedup.md)** — device sends
   a packet, two HGIs hear it with different RSSI; pool checks loopback,
   deduplicates, then records per-device RSSI (loopback excluded).

2. **[Outbound: typed pre-serialization routing](02_outbound_rssi_routing.md)** —
   sending a command via `prepare_command()` with `SourcePolicy.GATEWAY` or
   `PRESERVE`; route selection on `CommandDTO`, not on serialized frame.

3. **[Discovery: new HGI or REM detected](03_discovery_new_hgi.md)** —
   how a new HGI is discovered from RF traffic, reviewed by the user,
   and added via config-entry reload.

4. **[Transport transparency](03b_transport_transparency.md)** — serial,
   MQTT, and Zigbee transports all converge at the same pool interface;
   each child is a `PoolChild` with distinct state dimensions.

5. **[Adding a serial USB gateway](03c_adding_serial_usb.md)** — the
   two-phase bootstrapping problem for serial: detect from RF first,
   then add the physical port via config-entry reload after handshake.

6. **[RSSI routing lifecycle](04_rssi_add_remove.md)** — cold-start
   deterministic primary fallback, RSSI warmup, TTL expiry, and
   child offline quarantine (no runtime add/remove).

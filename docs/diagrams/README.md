# PooledTransport Flow Diagrams (issue 1119)

Mermaid flow diagrams for the multi-HGI gateway pool support.

These diagrams were originally posted as comments on
[issue 1119](https://github.com/ramses-rf/ramses_cc/issues/1119)
and are saved here for persistence (GitHub's mermaid renderer can be
unreliable for large diagrams).

## Diagrams

1. **[Inbound: dedup + RSSI tracking](01_inbound_dedup.md)** — device sends
   a packet, two HGIs hear it with different RSSI; pool deduplicates and
   records per-device RSSI.

2. **[Outbound: RSSI routing + source re-patching](02_outbound_rssi_routing.md)** —
   sending a command to a device; pool selects the child with best RSSI
   and re-patches the source ID. Covers normal (HGI source) and faked
   (REM source) cases.

3. **[Discovery: new HGI or REM detected](03_discovery_new_hgi.md)** —
   how a new HGI is discovered from RF traffic, reviewed by the user,
   and hot-reloaded into the pool.

4. **[Transport transparency](03b_transport_transparency.md)** — serial,
   MQTT, and Zigbee transports all converge at the same pool interface.

5. **[Adding a serial USB gateway](03c_adding_serial_usb.md)** — the
   two-phase bootstrapping problem for serial: detect from RF first,
   then add the physical port after handshake reveals the HGI ID.

6. **[RSSI on add/remove](04_rssi_add_remove.md)** — how RSSI routing
   adapts when a child is added (round-robin fallback → RSSI-based)
   or removed (tracker cleared, child excluded from selection).

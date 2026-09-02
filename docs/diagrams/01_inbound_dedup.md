# Inbound: device sends a packet, two HGIs hear it

```mermaid
flowchart TD
    FAN["FAN 32:000001<br/>broadcasts state packet"]

    subgraph RF["RF frequency 868 MHz"]
        FAN -->|"RSSI -045 weak"| HGI1["HGI 18:001234<br/>child 0, MQTT"]
        FAN -->|"RSSI -082 strong"| HGI2["HGI 18:005678<br/>child 1, USB"]
    end

    HGI1 -->|"MQTT publish<br/>RAMSES/GATEWAY/18:001234/rx"| MQTT["MQTT broker"]
    MQTT --> MqttTransport0["MqttTransport child 0"]
    HGI2 -->|"serial read"| PortTransport1["PortTransport child 1"]

    MqttTransport0 -->|"packet_received"| Proxy0["ChildProtocolProxy index 0"]
    PortTransport1 -->|"packet_received"| Proxy1["ChildProtocolProxy index 1"]

    Proxy0 -->|"_on_child_packet 0"| Pool["PooledTransport._on_child_packet"]
    Proxy1 -->|"_on_child_packet 1"| Pool

    Pool -->|"step 1: accepted_hgis check<br/>child_hgi 0 = 18:001234<br/>in accepted set? YES"| AcceptCheck["Accepted"]
    Pool -->|"step 1: accepted_hgis check<br/>child_hgi 1 = 18:005678<br/>in accepted set? YES"| AcceptCheck

    AcceptCheck -->|"step 2: record RSSI<br/>tracker 0 record 32:000001, -045<br/>tracker 1 record 32:000001, -082"| RSSI["Per-device RSSI tracking<br/>RssiTracker per child"]

    RSSI -->|"step 3: dedup check"| Dedup["Dedup cache"]
    Dedup -->|"first arrival child 0<br/>NOT duplicate - forward"| Proto1["Protocol.packet_received"]
    Dedup -->|"second arrival child 1<br/>DUPLICATE - drop"| DedupDrop["Dropped deduped"]

    Proto1 -->|"raw handlers fire first"| Scan["DiscoveryScan raw handler"]
    Proto1 -->|"device ID filter"| Filter["Device ID filter"]
    Filter -->|"allowed 32: is known"| Engine["Engine / Gateway"]

    style DedupDrop fill:#fdd,stroke:#c00
    style RSSI fill:#dfd,stroke:#0a0
```

## Key points

- Both HGIs hear the same RF packet with different RSSI due to distance
- Pool records per-device RSSI via `RssiTracker.record()` — one tracker per child
- `accepted_hgis` checks the **child's** HGI, not the packet source
- Dedup: first arrival forwarded, second dropped
- Scan engine sees it via raw handler before device filter
- Only one packet reaches the engine — no duplicate processing

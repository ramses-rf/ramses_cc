# Inbound: device sends a packet, two HGIs hear it

```mermaid
flowchart TD
    FAN["FAN 32:000001<br/>broadcasts state packet"]

    subgraph RF["RF frequency 868 MHz"]
        FAN -->|"RSSI -045"| HGI1["HGI 18:001234<br/>child 0, MQTT"]
        FAN -->|"RSSI -082"| HGI2["HGI 18:005678<br/>child 1, USB"]
    end

    HGI1 -->|"MQTT publish<br/>RAMSES/GATEWAY/18:001234/rx"| MQTT["MQTT broker"]
    MQTT --> MqttTransport0["MqttTransport child 0"]
    HGI2 -->|"serial read"| PortTransport1["PortTransport child 1"]

    MqttTransport0 -->|"packet_received"| Pool["PooledTransport._on_child_packet"]
    PortTransport1 -->|"packet_received"| Pool

    Pool -->|"step 1: loopback check<br/>src is active pool HGI?"| LoopCheck{"src in<br/>pool_hgi_ids?"}
    LoopCheck -->|"NO - normal traffic"| AcceptCheck{"child accepted?<br/>schema ownership check"}
    LoopCheck -->|"YES - loopback frame"| LoopTag["Tag as loopback<br/>exclude from route RSSI"]

    AcceptCheck -->|"YES"| Dedup["Dedup cache<br/>dict-backed, O(1) lookup"]
    AcceptCheck -->|"NO"| Drop["Dropped: not accepted"]

    Dedup -->|"first arrival<br/>key = verb,src,addr1,addr2,addr3,<br/>code,length,payload,seq?"| Forward["Forward to protocol"]
    Dedup -->|"duplicate within 500ms window"| DedupDrop["Dropped: deduped"]

    Forward -->|"record RSSI AFTER dedup<br/>child.rssi.record src, rssi, now<br/>(loopback excluded)"| RSSI["Per-device RSSI tracking<br/>RssiTracker per PoolChild<br/>TTL: 5 minutes"]
    Forward --> Proto["Protocol.packet_received"]
    Proto -->|"raw handlers fire first"| Scan["DiscoveryScan raw handler"]
    Proto -->|"device ID filter"| Filter["Device ID filter"]
    Filter -->|"allowed"| Engine["Engine / Gateway"]

    style DedupDrop fill:#fdd,stroke:#c00
    style Drop fill:#fdd,stroke:#c00
    style RSSI fill:#dfd,stroke:#0a0
    style LoopTag fill:#ffd,stroke:#aa0
```

## Key points (new plan)

- Both HGIs hear the same RF packet with different RSSI due to distance
- **Loopback check first**: if src is an active pool HGI, tag as loopback and exclude from route RSSI (invariant 15)
- **Schema ownership** is canonical for acceptance (not a separate `accepted_hgis` set)
- **Dedup is dict-backed** (O(1) lookup), key includes sequence when present (resolved from fixtures: sequence is stable across HGIs, 50/50)
- **RSSI recorded after dedup** — only for non-loopback packets, preventing aggregate contamination
- **RSSI TTL: 5 minutes** — stale samples expire automatically (resolved from fixtures)
- **500 ms dedup window** confirmed from fixtures (median delta 8.4 ms)
- Inbound frames retain receiving-HGI provenance independently from `addr1` (invariant 8)

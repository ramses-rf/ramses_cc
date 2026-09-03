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

    Pool -->|"step 1: preserve ingress_hgi_id<br/>with the frame"| Provenance["IngressFrame<br/>frame + ingress_hgi_id"]
    Provenance -->|"step 2: resolve RF transmitter<br/>via packet_addrs"| LoopCheck{"src is active<br/>pool HGI?"}
    LoopCheck -->|"NO - normal traffic"| AcceptCheck{"child accepted?<br/>schema ownership check"}
    LoopCheck -->|"YES - loopback frame"| LoopTag["Exclude from route RSSI<br/>(do not record as<br/>target-device evidence)"]

    AcceptCheck -->|"YES"| RecordRSSI["Record RSSI for non-loopback<br/>child.rssi.record src, rssi, now<br/>TTL: 5 minutes"]
    AcceptCheck -->|"NO"| Drop["Dropped: not accepted"]

    LoopTag --> EchoCheck{"QoS echo match?<br/>compare canonical fingerprint<br/>with pending final routed command"}
    RecordRSSI --> EchoCheck

    EchoCheck -->|"match: local echo or over-air copy<br/>satisfy QoS, then dedup"| Dedup["Dedup cache<br/>dict-backed, O(1) lookup"]
    EchoCheck -->|"no match: normal traffic<br/>proceed to dedup"| Dedup

    Dedup -->|"first arrival<br/>key = verb,src,addr1,addr2,addr3,<br/>code,length,payload,seq?"| Forward["Forward to protocol"]
    Dedup -->|"duplicate within 500ms window"| DedupDrop["Dropped: deduped"]

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
- **Inbound processing order** (plan section "Ingress provenance and cross-dongle loopback"):
  1. Preserve `ingress_hgi_id` with the frame
  2. Resolve RF transmitter; if src is an active pool HGI, exclude from route RSSI (not `addr1` heuristic)
  3. Compare canonical echo fingerprint with pending final routed command
  4. Normalize transport-assigned sequence for echo matching
  5. Treat exact match from selected child as local echo; from other children as over-air copy
  6. Satisfy QoS, then deduplicate remaining copies
  7. Do not blanket-suppress unrelated frames whose `addr1` is an active HGI
- **RSSI recorded before dedup but after loopback exclusion** — loopback frames never enter route RSSI
- **Schema ownership** is canonical for acceptance (not a separate `accepted_hgis` set)
- **Dedup is dict-backed** (O(1) lookup), key includes sequence when present (resolved from fixtures: sequence is stable across HGIs, 50/50)
- **RSSI TTL: 5 minutes** — stale samples expire automatically (resolved from fixtures)
- **500 ms dedup window** confirmed from fixtures (median delta 8.4 ms)
- Inbound frames retain receiving-HGI provenance independently from `addr1` (invariant 8)

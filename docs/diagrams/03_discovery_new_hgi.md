# Discovery: new HGI or REM detected

```mermaid
flowchart TD
    subgraph RF["RF frequency 868 MHz"]
        NewHGI["New HGI 18:009999<br/>neighbours or users 2nd"]
        NewREM["New REM 37:001234<br/>unknown remote"]
        ActiveHGI["Active HGI 18:001234<br/>pool member, child 0, ACCEPTED"]
    end

    NewHGI -->|"broadcasts 0001 puzzle<br/>RSSI -060"| ActiveHGI
    NewREM -->|"broadcasts 22F1 state<br/>RSSI -075"| ActiveHGI

    ActiveHGI -->|"MQTT rx topic"| MqttTransport["MqttTransport child 0"]
    MqttTransport -->|"packet_received"| Pool["PooledTransport._on_child_packet"]

    Pool -->|"loopback check: src 18:009999<br/>NOT in pool_hgi_ids - normal traffic"| AcceptCheck{"child accepted?<br/>schema ownership"}
    AcceptCheck -->|"YES - forward"| Forward["Forward to dedup + protocol"]

    Forward -->|"dedup: first time - forward"| Proto["Protocol._packet_received"]
    Proto -->|"raw handlers fire FIRST<br/>before device filter"| Scan["DiscoveryScan._on_packet"]

    Scan -->|"src = 18:009999<br/>is_hgi = True<br/>not known - NEW"| NewDevice1["Discovery entry:<br/>18:009999, type HGI"]
    Scan -->|"src = 37:001234<br/>is_hgi = False<br/>not known - NEW"| NewDevice2["Discovery entry:<br/>37:001234, type REM"]

    NewDevice1 --> Notify["ramses_cc DiscoveryManager<br/>check_for_new_devices"]
    NewDevice2 --> Notify

    Notify --> Review["review_discovered_devices<br/>config flow step"]

    Review -->|"User accepts 18:009999"| AcceptHGI["1. Set _owner: me in schema<br/>2. Config-entry RELOAD<br/>3. Pool recreated with new child<br/>4. New child connects, handshake<br/>5. RSSI routing active after warmup"]
    Review -->|"User rejects 18:009999"| RejectHGI["Set _owner: not-me<br/>excluded from pool"]
    Review -->|"User accepts 37:001234"| AcceptREM["Set _owner: me<br/>device entities created<br/>no pool change"]

    style Forward fill:#dfd,stroke:#0a0
    style NewDevice1 fill:#ffd,stroke:#aa0
    style NewDevice2 fill:#ffd,stroke:#aa0
    style AcceptHGI fill:#dfd,stroke:#0a0
```

## Key points (new plan)

- **Schema ownership is canonical** for acceptance — no separate `accepted_hgis` authority
- Packets from a new HGI are forwarded if heard by an accepted pool member
- Scan engine sees them via raw handler and creates discovery entries
- **Config-entry reload** is the membership-change mechanism (invariant 19) — no runtime `add_child()`
- Newly wildcard-discovered MQTT IDs remain non-routable discovery records until acceptance and reload (invariant 21)
- Cold-start after reload: new child uses deterministic primary fallback until RSSI samples accumulate

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
    MqttTransport -->|"packet_received"| Proxy["ChildProtocolProxy index 0"]
    Proxy -->|"_on_child_packet 0"| Pool["PooledTransport._on_child_packet"]

    Pool -->|"accepted_hgis check<br/>child_hgi 0 = 18:001234<br/>in accepted? YES - forward"| Forward["Forward to dedup + protocol"]

    Forward -->|"dedup: first time - forward"| Proto["Protocol._packet_received"]
    Proto -->|"raw handlers fire FIRST<br/>before device filter"| Scan["DiscoveryScan._on_packet"]

    Scan -->|"src = 18:009999<br/>is_hgi = True<br/>not known - NEW"| NewDevice1["Discovery entry:<br/>18:009999, type HGI"]
    Scan -->|"src = 37:001234<br/>is_hgi = False<br/>not known - NEW"| NewDevice2["Discovery entry:<br/>37:001234, type REM"]

    NewDevice1 -->|"check _is_own_gateway<br/>not active_hgi 18:001234<br/>not in pool_hgi_ids<br/>NOT own gateway"| Confirm1["Confirmed new device"]
    NewDevice2 -->|"check _is_own_gateway<br/>37: not 18: - not a gateway"| Confirm2["Confirmed new device"]

    Confirm1 --> Notify["ramses_cc DiscoveryManager<br/>check_for_new_devices"]
    Confirm2 --> Notify

    Notify --> Review["review_discovered_devices<br/>config flow step"]

    Review -->|"User accepts 18:009999"| AcceptHGI["1. Set _owner: me in schema<br/>2. _on_rf_schema_updated fires<br/>3. pool.add_child<br/>4. pool.set_accepted_hgis<br/>5. HOT RELOAD no restart"]
    Review -->|"User rejects 18:009999"| RejectHGI["Set _owner: not-me<br/>added to block_list<br/>not in pool"]
    Review -->|"User accepts 37:001234"| AcceptREM["Set _owner: me<br/>device entities created<br/>no pool change"]

    style Forward fill:#dfd,stroke:#0a0
    style NewDevice1 fill:#ffd,stroke:#aa0
    style NewDevice2 fill:#ffd,stroke:#aa0
    style AcceptHGI fill:#dfd,stroke:#0a0
```

## Key points

- `accepted_hgis` filters by which child received the packet, not by packet source
- Packets from a new HGI are forwarded if heard by an accepted pool member
- Scan engine sees them via raw handler and creates discovery entries
- `_is_own_gateway` checks `pool_hgi_ids` — existing members not re-discovered
- Accept triggers hot-reload via `_on_rf_schema_updated`

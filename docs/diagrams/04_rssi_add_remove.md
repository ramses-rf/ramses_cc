# RSSI when a new HGI is added or removed

```mermaid
flowchart TD
    AddChild["pool.add_child /dev/ttyUSB1"]
    AddChild --> Init["Initialize:<br/>child_rssi_trackers 1 = RssiTracker empty<br/>child_hgi 1 = None until handshake"]

    Init --> Connected["Child connects<br/>_on_child_connected 1<br/>child_hgi 1 = 18:009999"]

    Connected --> Select["_select_transport target_device"]

    Select --> Check1{"Per-device RSSI<br/>tracker.best_rssi_for target?"}
    Check1 -->|"None no samples<br/>returns _RSSI_UNKNOWN"| Check2{"Aggregate best RSSI<br/>across all known devices?"}
    Check2 -->|"No known devices<br/>returns _RSSI_UNKNOWN"| Check3{"All candidates<br/>return _RSSI_UNKNOWN?"}
    Check3 -->|"YES round-robin"| RR["Round-robin selection<br/>new child gets fair share"]
    Check3 -->|"NO some have RSSI"| Best["Select best RSSI child<br/>new child excluded until<br/>it has samples"]

    RR --> UseNew["New child immediately usable<br/>via round-robin fallback"]
    Best --> UseExisting["Existing children preferred<br/>they have RSSI data"]

    UseNew --> Packets["New child receives packets"]
    UseExisting --> Packets

    Packets --> Accumulate["RSSI samples accumulate<br/>tracker 1 record src_id, rssi, now"]

    Accumulate --> Transition["After a few packets:<br/>tracker 1 best_rssi_for returns data<br/>selected when best signal"]

    subgraph Remove["When a child is removed"]
        RemoveChild["pool.remove_child 1"]
        RemoveChild --> Clear["tracker 1 clear<br/>transports 1 = None<br/>child_connected 1 = False"]
        Clear --> Excluded["select_transport filters<br/>transports i is not None - False<br/>removed child never selected"]
    end

    style RR fill:#dfd,stroke:#0a0
    style Transition fill:#dfd,stroke:#0a0
    style Excluded fill:#fdd,stroke:#c00
```

## Key points

- New child gets a fresh `RssiTracker` — no initialization needed
- Immediately usable via round-robin fallback
- Naturally transitions to RSSI-based selection as samples accumulate
- Removed child: `tracker.clear()` frees memory, `is not None` check excludes from selection
- Fallback chain: per-device `best_rssi_for` → aggregate best → round-robin

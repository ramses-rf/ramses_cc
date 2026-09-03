# RSSI routing: cold-start, warmup, and child lifecycle

```mermaid
flowchart TD
    subgraph ColdStart["Cold-start after config-entry reload"]
        Reload["Config-entry reload<br/>Pool recreated"]
        Reload --> Init["Each PoolChild:<br/>rssi = RssiTracker empty<br/>connection_state: CONNECTING"]
        Init --> Connected["Child connects<br/>connection_state: CONNECTED<br/>hgi_id discovered"]
        Connected --> Select["_select_transport target_device"]
    end

    Select --> Check1{"Fresh per-device RSSI?<br/>TTL: 5 minutes<br/>at least 1 sample?"}
    Check1 -->|"NO - cold start"| Check2{"Fresh aggregate RSSI?<br/>excluding pool HGI sources"}
    Check2 -->|"NO - no data at all"| Primary["Deterministic primary<br/>first eligible child in<br/>stable config order"]
    Check1 -->|"YES"| Best["Select child with<br/>best fresh per-device RSSI"]
    Check2 -->|"YES"| BestAgg["Select child with<br/>best fresh aggregate RSSI"]

    Primary --> CheckRR{"Round-robin<br/>explicitly configured?"}
    CheckRR -->|"NO - use primary"| UseChild["Transmit via selected child"]
    CheckRR -->|"YES"| RR["Round-robin selection"]
    RR --> UseChild
    Best --> UseChild
    BestAgg --> UseChild

    UseChild --> CheckSend{"Any child<br/>send-ready?"}
    CheckSend -->|"YES"| Transmit["Transmit"]
    CheckSend -->|"NO - fail clearly"| Fail["Fail: no send-ready child"]

    Transmit --> Packets["Child receives packets"]
    Packets --> Accumulate["RSSI samples accumulate<br/>child.rssi.record src, rssi, now<br/>loopback excluded"]

    Accumulate --> Transition["After warmup:<br/>per-device RSSI available<br/>routing becomes RSSI-driven"]

    subgraph Offline["Child goes offline (LWT / disconnect)"]
        LWT["MQTT LWT offline<br/>or serial disconnect"]
        LWT --> Avail["node_availability: OFFLINE<br/>send_ready: False"]
        Avail --> Quarantine["Quarantine child's RSSI samples<br/>exclude from selection"]
        Quarantine --> Excluded["Child excluded from<br/>outbound selection"]
    end

    subgraph ConfigChange["Config-entry reload removes child"]
        RemoveReload["Config-entry reload<br/>without this child"]
        RemoveReload --> Recreate["Pool recreated<br/>child simply absent"]
        Recreate --> NoClear["No runtime remove_child<br/>no tracker.clear needed"]
    end

    style Primary fill:#dfd,stroke:#0a0
    style Transition fill:#dfd,stroke:#0a0
    style Excluded fill:#fdd,stroke:#c00
    style Quarantine fill:#ffd,stroke:#aa0
```

## Key points (new plan)

- **Cold-start routing is deterministic**: first eligible child in stable config order (invariant 16) — never round-robin unless explicitly configured
- **RSSI TTL: 5 minutes** — stale samples expire automatically (resolved from fixtures)
- **Loopback excluded** from route RSSI, including aggregate fallback (invariant 15)
- **Fallback chain** (plan section "RSSI routing"): fresh per-device RSSI → fresh aggregate RSSI (excluding pool HGIs) → first eligible child in stable config order → round-robin only if explicitly configured → fail clearly if no child is send-ready
- **Never multicast**: exactly one HGI transmits per attempt (invariant 16)
- **No runtime add/remove**: config-entry reload is the only membership-change mechanism (invariant 19)
- **Node availability** is distinct from connection state: LWT offline sets `node_availability=OFFLINE` and quarantines RSSI without dropping the connection
- **Health timeout ≥ 120s**: packet silence expires route evidence but does not itself mark a connected serial radio offline (observed: 60s was too aggressive)

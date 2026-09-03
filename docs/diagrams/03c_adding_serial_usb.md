# Adding a new serial USB gateway to the pool

Serial has a bootstrapping problem: the scan engine knows a new HGI ID
exists from RF, but not which physical port it is on.

```mermaid
flowchart TD
    subgraph Phase1["Phase 1: Detection - same as MQTT"]
        NewHGI["New HGI 18:009999<br/>broadcasts on RF"]
        ExistingHGI["Existing HGI 18:001234<br/>child 0, /dev/ttyUSB0"]
        NewHGI -->|"RF signal"| ExistingHGI
        ExistingHGI --> PortTransport0["PortTransport child 0"]
        PortTransport0 --> Pool1["Pool._on_child_packet"]
        Pool1 --> Proto1["Protocol._packet_received"]
        Proto1 --> Scan1["DiscoveryScan raw handler"]
        Scan1 -->|"src 18:009999 not in pool_hgi_ids"| Entry1["Discovery entry: 18:009999"]
        Entry1 --> Review1["review_discovered_devices"]
        Review1 --> Accept1["User accepts<br/>_owner: me in schema"]
        Accept1 --> Note1["BUT: unknown which port<br/>18:009999 is on<br/>Cannot add to pool yet"]
    end

    subgraph Phase2["Phase 2: Adding to pool - serial specific"]
        Plug["User plugs in USB stick<br/>at /dev/ttyUSB1"]
        Plug --> Config["User adds port to config-entry<br/>additional_ports: /dev/ttyUSB1"]
        Config --> Reload["Config-entry RELOAD<br/>Pool recreated with new child"]
        Reload --> Factory["PoolChild created<br/>transport: PortTransport /dev/ttyUSB1<br/>connection_state: CONNECTING"]

        Factory --> Handshake["Signature handshake<br/>send 0001 puzzle<br/>wait for echo"]
        Handshake -->|"HGI echoes back<br/>0001 with its ID as src"| Echo["Echo received<br/>packet.src.id = 18:009999"]
        Echo --> SetHgi["PoolChild.hgi_id = 18:009999<br/>connection_state: CONNECTED<br/>node_availability: ONLINE<br/>send_ready: True (after HW gate)"]
        SetHgi --> CrossRef["Cross-reference:<br/>18:009999 accepted in Phase 1<br/>schema ownership = me"]
        CrossRef --> Done["Pool has 2 children:<br/>child 0: /dev/ttyUSB0 to 18:001234<br/>child 1: /dev/ttyUSB1 to 18:009999<br/>RSSI routing active after warmup"]
    end

    Accept1 --> Plug

    style Note1 fill:#fdd,stroke:#c00
    style Done fill:#dfd,stroke:#0a0
    style Echo fill:#ffd,stroke:#aa0
    style Reload fill:#dfd,stroke:#0a0
```

## MQTT vs Serial comparison

| Aspect | MQTT | Serial |
|---|---|---|
| HGI ID known before adding? | Yes from scan engine | No, only after handshake |
| URL format | mqtt://broker/.../18:009999 | /dev/ttyUSB1 |
| How HGI ID discovered | ramses_esp topic path | 0001 puzzle echo src.id |
| Can accept directly? | Yes, construct URL | No, need physical port first |
| Membership change mechanism | Config-entry reload | Config-entry reload |

## Key points (new plan)

- **Phase 2 feature:** serial USB gateways are gated in the config flow during Phase 1 (MQTT-only). This diagram shows the Phase 2 target after serial is un-gated.
- **Config-entry reload** is the only membership-change mechanism (invariant 19) — no runtime `add_child()`
- Serial children are created with `send_ready=False` until the hardware feasibility gate is passed
- The ESP USB reset behavior must be characterized before the serial PR starts (hardware feasibility gate, Phase 2 prerequisite)
- After reload, the new child follows the same cold-start path: deterministic primary fallback → RSSI-based selection as samples accumulate
- **HA USB consumer listing (issue 1143):** the port picker relies on HA detecting ramses_cc as a USB consumer. HA 2026.9+ checks flat key paths only; if the nested `("serial_port", "port_name")` path is not supported by HA core, PR 5 must flatten the key or add a compatibility shim

# Outbound: send a command via typed pre-serialization routing

The new plan moves route selection to a typed pre-serialization boundary.
The pool no longer parses ASCII frames with `.split()` to extract addresses.

```mermaid
flowchart TD
    subgraph CaseA["Case A: gateway-source command (SourcePolicy.GATEWAY)"]
        Cmd1["CommandDTO<br/>addr1 = 18:000730 placeholder"]
        Cmd1 --> Prep1["prepare_command RouteRequest<br/>command, source_policy=GATEWAY"]
        Prep1 --> Route1["Router selects child<br/>via packet_addrs for target<br/>fresh per-device RSSI"]
        Route1 --> Sub1{"SourcePolicy.GATEWAY<br/>and child is evofw3?"}
        Sub1 -->|"YES - substitute source"| Replace1["dataclasses.replace<br/>addr1 = selected child HGI ID"]
        Sub1 -->|"NO - HGI80 placeholder<br/>keep 18:000730"| Keep1["DTO unchanged<br/>HGI80 firmware substitutes ID"]
        Replace1 --> Final1["Final routed CommandDTO<br/>addr1 = 18:005678"]
        Keep1 --> Final1
    end

    subgraph CaseB["Case B: faked device (SourcePolicy.PRESERVE)"]
        Cmd2["CommandDTO<br/>addr1 = 37:001234 faked REM"]
        Cmd2 --> Prep2["prepare_command RouteRequest<br/>command, source_policy=PRESERVE"]
        Prep2 --> Route2["Router selects child<br/>via packet_addrs for target<br/>fresh per-device RSSI"]
        Route2 --> NoSub["SourcePolicy.PRESERVE<br/>source NEVER rewritten"]
        NoSub --> Final2["Final routed CommandDTO<br/>addr1 = 37:001234 unchanged"]
    end

    Final1 --> QoS1["Set pending QoS command<br/>from final routed DTO"]
    Final2 --> QoS2["Set pending QoS command<br/>from final routed DTO"]

    QoS1 --> Serial1["Serialize once<br/>derive canonical echo fingerprint<br/>from re-parsed wire frame"]
    QoS2 --> Serial2["Serialize once<br/>derive canonical echo fingerprint<br/>from re-parsed wire frame"]

    Serial1 --> Write1["write_routed child_id, frame<br/>dispatch to pinned child"]
    Serial2 --> Write2["write_routed child_id, frame<br/>dispatch to pinned child"]

    Write1 --> HGI1["HGI 18:005678 transmits<br/>RF source: 18:005678"]
    Write2 --> HGI2["HGI 18:005678 transmits<br/>RF source: 37:001234 faked REM"]

    style Replace1 fill:#ffd,stroke:#aa0
    style NoRepatch fill:#dfd,stroke:#0a0
    style QoS1 fill:#dfd,stroke:#0a0
    style QoS2 fill:#dfd,stroke:#0a0
```

## Key points (new plan)

- **Typed pre-serialization routing**: route selection happens on `CommandDTO`, not on a serialized ASCII frame string
- **SourcePolicy.GATEWAY vs PRESERVE**: explicit intent, not an `addr1.startswith("18:")` heuristic
- **`dataclasses.replace()`** for source substitution — no string manipulation
- **QoS echo fingerprint** derived from the final routed wire command (invariant 9, 15)
- **Serialize once** per attempt; QoS retry is a new routed attempt that may select a different child
- **No double-patching**: `_patch_cmd_if_needed` is folded into the preparation step
- HGI80 exception: placeholder `18:000730` kept for HGI80 children (firmware substitutes its own ID)
- Faked-device sources (`37:001234`) are preserved — no `18:` prefix heuristic

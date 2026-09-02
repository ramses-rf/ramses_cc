# Outbound: send a command to the FAN

Normal command (HGI as source) vs faked device (REM as source).

```mermaid
flowchart TD
    subgraph CaseA["Case A: normal command, HGI as source"]
        Cmd1["CommandDTO<br/>addr1 = 18:000730 placeholder"]
        Cmd1 --> Patch1["_patch_cmd_if_needed<br/>evofw3: 18:000730 to 18:001234<br/>pools active HGI = first child"]
        Patch1 --> Frame1["Frame: I --- 18:001234 01:123456 ..."]
        Frame1 --> Pool1["Pool.write_frame"]
        Pool1 -->|"src = 18:001234<br/>child = 18:005678<br/>src starts with 18 - re-patch"| Repatch1["Frame: I --- 18:005678 01:123456 ..."]
        Repatch1 --> Send1["Transmit via child 1<br/>best RSSI for 01:123456"]
    end

    subgraph CaseB["Case B: faked device, REM as source"]
        Cmd2["CommandDTO<br/>addr1 = 37:001234 faked REM"]
        Cmd2 --> Patch2["_patch_cmd_if_needed<br/>addr1 not 18:000730 - no patch<br/>addr1 not hgi_id - no patch"]
        Patch2 --> Frame2["Frame: I --- 37:001234 32:000001 ... 2411 ..."]
        Frame2 --> Pool2["Pool.write_frame"]
        Pool2 -->|"src = 37:001234<br/>child = 18:005678<br/>src does NOT start with 18 - skip"| NoRepatch["Frame unchanged:<br/>I --- 37:001234 32:000001 ..."]
        NoRepatch --> Send2["Transmit via child 1<br/>best RSSI for 32:000001"]
    end

    Send1 --> HGI1["HGI 18:005678 transmits<br/>RF source: 18:005678"]
    Send2 --> HGI2["HGI 18:005678 transmits<br/>RF source: 37:001234 faked REM"]

    HGI1 --> Dev1["Device 01:123456 receives<br/>command from HGI 18:005678"]
    HGI2 --> Dev2["FAN 32:000001 receives<br/>2411 from bound REM 37:001234"]

    style Repatch1 fill:#ffd,stroke:#aa0
    style NoRepatch fill:#dfd,stroke:#0a0
```

## Key points

- Protocol patches addr1 to pool's active HGI (first child) — first patch
- Pool parses frame via `.split()`, target = parts[3] (addr2)
- Pool looks up `tracker.best_rssi_for(target)` — max of last N readings
- Pool re-patches addr1 to selected child's HGI — second patch
- Re-patch only when `src_addr starts with 18` (HGI source)
- Faked devices preserved: 37:001234 (REM) is NOT re-patched
- HGI80 exception: 18:000730 placeholder is NOT re-patched
- Double-patch is not optimal but overhead is negligible — see future optimization comment

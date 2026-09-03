# Transport transparency: serial vs MQTT vs Zigbee

```mermaid
flowchart TD
    subgraph RF["RF frequency 868 MHz"]
        NewHGI["New HGI 18:009999<br/>broadcasts on RF"]
    end

    subgraph SerialPath["Serial path - USB stick"]
        USB["HGI 18:001234<br/>USB stick, child 0"]
        USB -->|"hears RF signal"| SerialRx["Serial read /dev/ttyUSB0"]
        SerialRx -->|"bytes to frame"| PortFrame["PortTransport._frame_read<br/>Packet.from_file"]
        PortFrame --> PortPkt["PortTransport._packet_read<br/>sets SZ_ACTIVE_HGI"]
    end

    subgraph MqttPath["MQTT path - ramses_esp + broker"]
        ESP["HGI 18:001234<br/>ramses_esp, child 0"]
        ESP -->|"hears RF signal"| EspPub["ramses_esp publishes<br/>to .../18:001234/rx"]
        EspPub --> Broker["MQTT broker"]
        Broker -->|"delivers to subscriber"| MqttSub["MqttTransport._on_message"]
        MqttSub --> MqttFrame["_frame_read<br/>Packet.from_file"]
        MqttFrame --> MqttPkt["_packet_read"]
    end

    USB -.->|"same HGI different transport<br/>not both at once"| ESP

    PortPkt --> Pool["PooledTransport._on_child_packet<br/>via PoolChild.transport callback"]
    MqttPkt --> Pool

    Pool --> Proto["Protocol._packet_received"]
    Proto --> Scan["DiscoveryScan raw handler"]
    Scan --> NewDev["Discovery entry created"]

    style Pool fill:#dfd,stroke:#0a0
```

## Transport comparison

| Transport | RF receiver | Delivery to pool | Send-ready |
|---|---|---|---|
| Serial USB | USB stick | Serial bytes to frame to Packet | After hardware feasibility gate |
| MQTT ramses_esp | ramses_esp radio | MQTT publish to broker to subscriber to Packet | After ESP online (LWT) |
| Zigbee | Zigbee coordinator radio | Zigbee cluster attr to Packet | Not advertised until IEEE identity separated |

All three converge at the `PoolChild.transport` callback to `pool._on_child_packet`
to `Protocol._packet_received` to raw handlers to `DiscoveryScan._on_packet`.
The pool treats all children the same via the transport-neutral child interface.

## Key points (new plan)

- Each child is a `PoolChild` object with distinct `connection_state`, `node_availability`, `send_ready`, and `rssi` (invariant 20)
- Serial children remain `send_ready=False` until the hardware feasibility gate is passed
- MQTT LWT/offline propagates into `node_availability`, not just `connection_state`
- Zigbee is not advertised as supported until IEEE transport identity and RAMSES HGI identity are separated (invariant 13)

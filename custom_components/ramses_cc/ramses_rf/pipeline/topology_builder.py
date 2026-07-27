"""RAMSES RF - The Asynchronous Topology Builder Engine."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from ..const import (
    I_,
    RQ,
    SZ_DOMAIN_ID,
    SZ_UFH_IDX,
    SZ_ZONE_IDX,
    SZ_ZONE_TYPE,
    W_,
    ZON_ROLE_MAP,
    Code,
    DevType,
)
from ..enums import TopologyAction
from ..messages.core import Message
from ..models import TopologyChangedEvent
from ..protocol.ramses import CODES_ONLY_FROM_CTL, HVAC_KLASS_BY_VC_PAIR
from ...ramses_tx import DeviceIdT

_LOGGER = logging.getLogger(__name__)


class TopologyBuilder:
    """Centralised engine for heuristic eavesdropping and graph mutation.

    INDUSTRY ARCHITECTURE NOTE: The Rule Engine Pattern
    ---------------------------------------------------
    Because this project relies on reverse-engineered RF protocols, new
    heuristics and manufacturer quirks are discovered frequently. To
    prevent this class from becoming a massive, unmaintainable
    "God Object", we register independent heuristic rules in a list.

    When a message arrives, the engine simply feeds the message to every
    rule in the list. To add a new quirk for a new device, a developer
    simply writes a new method and appends it to `self._rules`. The core
    ingestion logic never needs to be modified.
    """

    def __init__(
        self,
        emit_event_cb: Callable[[TopologyChangedEvent], None],
        enable_eavesdrop: bool = False,
    ) -> None:
        """Initialize the TopologyBuilder.

        :param emit_event_cb: Callback to emit topology events back
            onto the central event bus or directly to the registry.
        :param enable_eavesdrop: If False, heuristic class promotions
            are disabled. Explicit bindings (e.g., 000C) still process.
        """
        self._emit = emit_event_cb
        self._enable_eavesdrop = enable_eavesdrop

        # FIXME: LEGACY DEBT - State Cache for Flawed Temporal Rules
        # The legacy architecture relied on comparing current packets
        # against previously observed packets to deduce bindings
        # (e.g., 30C9 temperature matching, 3B00 sequential matching).
        # This cache isolates that fragile state until Phase 3 rewrites.
        self._legacy_debt_cache: dict[str, Any] = {
            "prev_3b00": None,
            "trv_temps": {},
            "zone_temps": {},
        }

        # The active list of heuristic rules. Order does not matter,
        # as each rule independently yields its own isolated events.
        self._rules: list[Callable[[Message], None]] = [
            self._evaluate_evohome_rules,
            self._evaluate_zone_binding_rules,
            self._evaluate_directed_telemetry_rules,
            self._evaluate_ufh_rules,
            self._evaluate_hvac_rules,
            self._evaluate_dhw_opentherm_rules,
            self._evaluate_heating_prefix_rules,
            self._evaluate_appliance_control_sync_rules,
            self._evaluate_appliance_eavesdrop_rules,
            self._evaluate_zone_sensor_matching_rules,
            self._evaluate_zone_type_eavesdrop_rules,
            self._evaluate_eavesdrop_rules,
            self._evaluate_implicit_binding_rules,
            self._evaluate_third_address_broadcast_rules,
        ]

    async def consume(self, msg: Message) -> None:
        """Ingest a message and evaluate it against all registered rules.

        :param msg: The immutable Message L7 envelope to evaluate.
        """
        for rule in self._rules:
            try:
                rule(msg)
            except Exception as err:
                # Do not spam the logs with topology rejection errors when
                # enforcing strict schemas or ignoring missing/poison devices.
                if "changed app_cntrl" in str(err) or "Can't create" in str(err):
                    _LOGGER.debug("Topology rule %s bypassed: %s", rule.__name__, err)
                else:
                    _LOGGER.error(
                        "Error evaluating topology rule %s: %s", rule.__name__, err
                    )

    def _get_payloads(self, msg: Message) -> list[Any]:
        """Safely extract the array or standard dictionary payload.

        Seamlessly bridges `core.Message` data structures whether they
        contain a single dictionary or an array of payloads.
        """
        raw: Any = msg.data
        if isinstance(raw, dict):
            return cast("list[Any]", raw.get("_array", [raw]))
        if isinstance(raw, list):
            return raw
        return []

    def _evaluate_evohome_rules(self, msg: Message) -> None:
        """Evaluate rules specific to the Evohome CH/DHW ecosystem.

        Historically, entities intercepted CODES_ONLY_FROM_CTL to
        dynamically promote themselves to Controllers. We now extract
        that logic into this explicit, trackable rule.
        """
        if not self._enable_eavesdrop:
            return

        if msg.header.verb == I_ and msg.header.code in CODES_ONLY_FROM_CTL:
            event = TopologyChangedEvent(
                action=TopologyAction.CREATE_CONTROLLER,
                device_id=msg.src.id,
                causation="Rule_Evohome_Controller_Broadcast",
            )
            self._emit(event)

    def _evaluate_zone_binding_rules(self, msg: Message) -> None:
        """Evaluate 000C and heuristic packets to bind actuators to zones."""

        # EXPLICIT BINDING: Controllers (01) broadcasting 000C device maps
        if msg.header.code == Code._000C and getattr(msg.src, "type", None) == "01":
            for p in self._get_payloads(msg):
                if not isinstance(p, dict):
                    continue

                zone_idx = p.get("zone_idx")
                domain_id = p.get("domain_id")
                device_role = p.get("device_role")
                devices = p.get("devices", [])

                if not devices:
                    continue

                # Prepare the base metadata dict, correctly flagging
                # all types of sensors (e.g., 'sensor', 'dhw_sensor')
                metadata: dict[str, Any] = {}
                if device_role is not None:
                    metadata["is_sensor"] = "sensor" in str(device_role)
                    metadata["device_role"] = str(
                        device_role
                    )  # Explicit DHW preservation

                if zone_idx is not None:
                    # Clone metadata to avoid cross-iteration pollution
                    event_meta = dict(metadata)
                    event_meta["zone_idx"] = str(zone_idx)
                    # Bridging quirk: DeviceRegistry expects domain index under child_id
                    event_meta["child_id"] = str(zone_idx)
                    for child_id in devices:
                        event = TopologyChangedEvent(
                            action=TopologyAction.BIND_DEVICE,
                            parent_id=msg.src.id,  # The Controller
                            child_id=child_id,  # The Device
                            metadata=event_meta,
                            causation="Rule_000C_Zone_Binding",
                        )
                        self._emit(event)

                elif domain_id is not None:
                    event_meta = dict(metadata)
                    event_meta["domain_id"] = str(domain_id)
                    event_meta["child_id"] = str(domain_id)
                    for child_id in devices:
                        event = TopologyChangedEvent(
                            action=TopologyAction.BIND_DEVICE,
                            parent_id=msg.src.id,  # The Controller
                            child_id=child_id,  # The Device
                            metadata=event_meta,
                            causation="Rule_000C_Domain_Binding",
                        )
                        self._emit(event)

    def _evaluate_directed_telemetry_rules(self, msg: Message) -> None:
        """Evaluate implicit bindings from directed telemetry broadcasts.

        Devices (TRVs, Thermostats, DHW sensors) explicitly declare their
        topological relationships by broadcasting telemetry (e.g., 30C9,
        3150, 1060, 1260) directly to their parent Controller (01).
        """
        if not self._enable_eavesdrop:
            return

        # Broaden the net: Intercept ANY directed telemetry to a Controller,
        # but strictly prevent the Controller from binding to itself.
        # Identify the Controller ID whether it is a directed target or a
        # broadcast addr3 target.
        ctl_id = None
        if getattr(msg.dst, "type", None) == "01":
            ctl_id = msg.dst.id
        elif getattr(msg.addr3, "type", None) == "01":
            ctl_id = msg.addr3.id

        if msg.header.verb == I_ and ctl_id and msg.src.id != ctl_id:
            # Bypass hardcoded Code limitations. If the parsed payload contains
            # a zone_idx, and is routed to the controller, it implies a binding.
            for p in self._get_payloads(msg):
                if not isinstance(p, dict):
                    continue

                zone_idx = p.get(SZ_ZONE_IDX)
                domain_id = p.get(SZ_DOMAIN_ID)

                if zone_idx is None and domain_id is None:
                    continue

                metadata: dict[str, Any] = {}

                # Determine Device Role (Fallback to hardware prefix inference)
                is_actuator = getattr(msg.src, "type", None) in ("04", "08", "13", "02")
                is_sensor = getattr(msg.src, "type", None) in (
                    "00",
                    "03",
                    "07",  # DHW Sensor
                    "12",
                    "22",
                    "34",
                )

                if msg.header.code in (Code._3150, Code._0008, Code._2309, Code._000A):
                    metadata["device_role"] = "actuator"
                elif msg.header.code in (
                    Code._30C9,
                    Code._1260,
                    Code._10A0,
                    Code._12B0,
                ):
                    metadata["device_role"] = "sensor" if is_sensor else "actuator"
                    if is_sensor:
                        metadata["is_sensor"] = "True"
                else:
                    metadata["device_role"] = "actuator" if is_actuator else "sensor"
                    if is_sensor:
                        metadata["is_sensor"] = "True"

                if zone_idx is not None:
                    metadata["zone_idx"] = str(zone_idx)
                    metadata["child_id"] = str(zone_idx)
                elif domain_id is not None:
                    if domain_id in ("F9", "FA", "FC"):
                        metadata["domain_id"] = str(domain_id)
                        metadata["child_id"] = str(domain_id)
                    else:
                        metadata["zone_idx"] = str(domain_id)
                        metadata["domain_id"] = str(domain_id)
                        metadata["child_id"] = str(domain_id)

                event = TopologyChangedEvent(
                    action=TopologyAction.BIND_DEVICE,
                    parent_id=ctl_id,
                    child_id=msg.src.id,
                    metadata=metadata,
                    causation=f"Rule_Telemetry_Eavesdrop_{msg.header.code}",
                )
                self._emit(event)

    def _evaluate_ufh_rules(self, msg: Message) -> None:
        """Evaluate rules specific to Underfloor Heating (UFH).

        UFCs broadcast their circuit mappings via 000C messages.
        We intercept these to bind the UFC to the Controller and map
        the individual circuits to their corresponding zones.
        Note: This is explicit configuration data, not a heuristic,
        so it is processed regardless of the enable_eavesdrop flag.
        """
        is_ufc_src = getattr(msg.src, "type", None) == "02"
        is_ufc_dst = getattr(msg.dst, "type", None) == "02"

        if not (is_ufc_src or is_ufc_dst):
            return

        ufc_id = msg.src.id if is_ufc_src else msg.dst.id

        # Identify the Controller ID if present in the conversation
        ctl_id = None
        if getattr(msg.src, "type", None) == "01":
            ctl_id = msg.src.id
        elif getattr(msg.dst, "type", None) == "01":
            ctl_id = msg.dst.id
        elif getattr(msg.addr3, "type", None) == "01":
            ctl_id = msg.addr3.id

        # 1. Conversational Binding: Promote and bind if communicating with a Controller
        if ctl_id and ctl_id != ufc_id:
            # Explicitly promote to UFC, This prevents HVAC devices from being
            # falsely flagged and dropped by the strict parser before they can
            # be routed.
            event_promote = TopologyChangedEvent(
                action=TopologyAction.PROMOTE_CLASS,
                device_id=ufc_id,
                metadata={"device_class": DevType.UFC},
                causation="Rule_UFH_Communication_Promotion",
            )
            self._emit(event_promote)

            # Bind the UFC to the parent Controller
            event_bind = TopologyChangedEvent(
                action=TopologyAction.BIND_DEVICE,
                parent_id=ctl_id,
                child_id=ufc_id,
                metadata={"device_role": "ufc"},
                causation="Rule_UFH_Communication_Binding",
            )
            self._emit(event_bind)

        # 2. Extract specific circuit topology from 000C configuration packets
        if is_ufc_src and msg.header.code == Code._000C:
            # Fallback to direct property check first for legacy compatibility
            # Bypassing strict typing evaluation by casting to Any
            raw_data: Any = msg.data
            zone_type = (
                raw_data.get(SZ_ZONE_TYPE) if isinstance(raw_data, dict) else None
            )
            if zone_type and zone_type not in (ZON_ROLE_MAP.ACT, ZON_ROLE_MAP.UFH):
                return

            for p in self._get_payloads(msg):
                if not isinstance(p, dict):
                    continue

                ufh_idx = p.get(SZ_UFH_IDX)
                zone_idx = p.get(SZ_ZONE_IDX)

                if ufh_idx is not None:
                    event_circuit = TopologyChangedEvent(
                        action=TopologyAction.CREATE_CIRCUIT,
                        device_id=ufc_id,
                        metadata={
                            "ufh_idx": str(ufh_idx),
                            "zone_idx": str(zone_idx) if zone_idx else "None",
                            "child_id": str(zone_idx) if zone_idx else "None",
                        },
                        causation="Rule_UFH_000C_Circuit",
                    )
                    self._emit(event_circuit)

    def _evaluate_hvac_rules(self, msg: Message) -> None:
        """Evaluate rules specific to Ventilation and HVAC.

        HVAC devices share prefixes (e.g., 32: can be a Fan, CO2, etc.).
        Therefore, we promote classes dynamically using the central protocol schema.
        """
        if not self._enable_eavesdrop:
            return

        # Safely convert the Code Enum to a string for schema dictionary lookups
        msg_verb = msg.header.verb
        msg_code = str(msg.header.code)

        # Iterate through the dictionary and strictly evaluate both verb and code
        # to correctly assign dual-role devices (e.g., 31D9 is fan on RQ, co2 on I).
        # Note: A schema_verb of None indicates the opcode applies across all verbs.
        dev_class = None
        for (schema_verb, schema_code), dev_class_name in HVAC_KLASS_BY_VC_PAIR.items():
            if (schema_verb is None or schema_verb == msg_verb) and str(
                schema_code
            ) == msg_code:
                dev_class = dev_class_name
                break

        if dev_class:
            # Promote Source (if the device is transmitting, and NOT a controller)
            if msg.src.id != "--:------" and getattr(msg.src, "type", None) != "01":
                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.PROMOTE_CLASS,
                        device_id=msg.src.id,
                        metadata={"device_class": dev_class},
                        causation="Rule_HVAC_Signature_Source",
                    )
                )

            # Promote Target (if the controller is querying/commanding the device)
            if (
                msg.dst.id != "--:------"
                and msg.dst.id != msg.src.id
                and getattr(msg.dst, "type", None) != "01"
            ):
                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.PROMOTE_CLASS,
                        device_id=msg.dst.id,
                        metadata={"device_class": dev_class},
                        causation="Rule_HVAC_Signature_Target",
                    )
                )

    def _evaluate_dhw_opentherm_rules(self, msg: Message) -> None:
        """Evaluate rules specific to DHW and OpenTherm Bridges.

        OpenTherm Bridges exclusively use 3220. DHW sensors are deduced
        via 1260 and 10A0 packets.
        """
        if not self._enable_eavesdrop:
            return

        # Prefix Guard: Prevent cross-promotion (e.g., OTB sending 1260)
        if msg.header.code == Code._3220 and getattr(msg.src, "type", None) == "10":
            event = TopologyChangedEvent(
                action=TopologyAction.PROMOTE_CLASS,
                device_id=msg.src.id,
                metadata={"device_class": DevType.OTB},
                causation="Rule_OTB_3220_Signature",
            )
            self._emit(event)

        elif (
            msg.header.code in (Code._1260, Code._10A0)
            and getattr(msg.src, "type", None) == "07"
        ):
            event = TopologyChangedEvent(
                action=TopologyAction.PROMOTE_CLASS,
                device_id=msg.src.id,
                metadata={"device_class": DevType.DHW},
                causation="Rule_DHW_Signature",
            )
            self._emit(event)

    def _evaluate_heating_prefix_rules(self, msg: Message) -> None:
        """Evaluate passive heuristics based purely on hardware prefixes.

        Legacy architecture automatically promoted generic devices into specific
        heating domain subtypes (e.g., TRV, UFC, BDR) the moment their address
        was observed anywhere in the packet (src, dst, or embedded in payload).
        """
        if not self._enable_eavesdrop:
            return

        prefix_map = {
            # REMOVED: "02": DevType.UFC, - This greedy assumption breaks HVAC validation
            "03": DevType.HCW,
            "04": DevType.TRV,
            "12": DevType.THM,
            "13": DevType.BDR,
            "22": DevType.THM,
            "34": DevType.THM,
        }

        # Safe L7 extraction (dropping the legacy _pkt._addrs shim).
        addrs = [msg.src]
        if msg.dst.id != "--:------" and msg.dst.id != msg.src.id:
            addrs.append(msg.dst)

        for addr in addrs:
            if getattr(addr, "type", None) in prefix_map:
                event = TopologyChangedEvent(
                    action=TopologyAction.PROMOTE_CLASS,
                    device_id=addr.id,
                    metadata={"device_class": prefix_map[addr.type]},
                    causation="Rule_Heating_Prefix_Heuristic",
                )
                self._emit(event)

    def _evaluate_appliance_control_sync_rules(self, msg: Message) -> None:
        """Evaluate direct configuration syncs to map the System Relay.

        :param msg: The immutable Message L7 envelope to evaluate.
        :type msg: Message
        :return: None
        :rtype: None
        """
        if not self._enable_eavesdrop:
            return

        # Guard: Catch direct commands from the Controller (01) to a Relay (13)
        if (
            getattr(msg.src, "type", None) == "01"
            and msg.dst.id != "--:------"
            and getattr(msg.dst, "type", None) == "13"
        ):
            # 1100 (Boiler Params) or 10E0/1FC9 (Binding) are direct links
            if msg.header.code in (Code._1100, Code._10E0, Code._1FC9):
                event = TopologyChangedEvent(
                    action=TopologyAction.BIND_DEVICE,
                    parent_id=msg.src.id,
                    child_id=msg.dst.id,
                    metadata={
                        "domain_id": "FC",
                        "child_id": "FC",
                        "device_role": "appliance_control",
                    },
                    causation="Rule_Direct_Relay_Sync",
                )
                self._emit(event)

    def _evaluate_appliance_eavesdrop_rules(self, msg: Message) -> None:
        """Evaluate the legacy passive eavesdropping of the System Relay.

        # TODO: PARITY FLAW - This replicates legacy `eavesdrop_appliance_control`.
        It relies on fragile assumptions regarding typical message flows
        (e.g., 3220, 3EF0, 3B00) between the Controller and the Heat Relay.
        """
        if not self._enable_eavesdrop:
            return

        if msg.header.code not in (Code._3220, Code._3B00, Code._3EF0):
            return

        app_cntrl_id: DeviceIdT | None = None

        if msg.header.code == Code._3220 and msg.header.verb == RQ:
            if (
                getattr(msg.src, "type", None) == "01"
                and msg.dst.id != "--:------"
                and getattr(msg.dst, "type", None) == "10"
            ):
                app_cntrl_id = msg.dst.id

        elif msg.header.code == Code._3EF0 and msg.header.verb == RQ:
            if (
                getattr(msg.src, "type", None) == "01"
                and msg.dst.id != "--:------"
                and getattr(msg.dst, "type", None) in ("10", "13")
            ):
                app_cntrl_id = msg.dst.id

        elif msg.header.code == Code._3B00 and msg.header.verb == I_:
            # Sequence matching: 13: broadcasts 3B00, followed by 01: broadcasting 3B00
            if getattr(msg.src, "type", None) == "13":
                self._legacy_debt_cache["prev_3b00"] = msg
            elif getattr(msg.src, "type", None) == "01":
                prev = self._legacy_debt_cache.get("prev_3b00")
                if prev and prev.data == msg.data:
                    app_cntrl_id = prev.src.id

        if app_cntrl_id is not None:
            event = TopologyChangedEvent(
                action=TopologyAction.BIND_DEVICE,
                parent_id=msg.src.id,  # Assume src is the Controller
                child_id=app_cntrl_id,
                metadata={"domain_id": "FC", "device_role": "appliance_control"},
                causation="Rule_Legacy_Appliance_Eavesdrop",
            )
            self._emit(event)

    def _evaluate_zone_sensor_matching_rules(self, msg: Message) -> None:
        """Evaluate the legacy collision-abstention temperature matching."""
        # DISABLED FOR PARITY: This legacy heuristic is highly prone to false
        # positives and cross-zone contamination (e.g., binding TRVs to wrong
        # zones). It is intentionally bypassed here to ensure schema parity.
        return

    def _evaluate_zone_type_eavesdrop_rules(self, msg: Message) -> None:
        """Evaluate the legacy passive promotion of zone classes.

        # TODO: PARITY FLAW - This replicates `eavesdrop_zone_type` from `zones.py`.
        It arbitrarily maps zones to VAL, ELE, RAD, or UFH based purely on
        telemetry packet signatures.
        """
        if not self._enable_eavesdrop:
            return

        for p in self._get_payloads(msg):
            if not isinstance(p, dict):
                continue

            zone_idx = p.get(SZ_ZONE_IDX)
            if zone_idx is None:
                continue

            zone_class: str | None = None

            # 0008/0009 packets denote Electric or Valve configurations
            if msg.header.code in (Code._0008, Code._0009):
                zone_class = ZON_ROLE_MAP["ELE"]

            # 3150 Demand mappings denote specific actuator types
            elif msg.header.code == Code._3150:
                src_type = getattr(msg.src, "type", None)
                if src_type == "04":
                    zone_class = ZON_ROLE_MAP["RAD"]
                elif src_type == "13":
                    zone_class = ZON_ROLE_MAP["VAL"]
                elif src_type == "02":
                    zone_class = ZON_ROLE_MAP["UFH"]

            if zone_class is not None:
                # We emit a BIND_DEVICE action targeting the controller,
                # passing the deduced zone_class as metadata for the projection.
                event = TopologyChangedEvent(
                    action=TopologyAction.BIND_DEVICE,
                    parent_id=msg.src.id,  # Typically the Controller
                    child_id=msg.src.id,  # Self-referential to update the zone metadata
                    metadata={
                        "zone_idx": str(zone_idx),
                        "zone_class": zone_class,
                    },
                    causation="Rule_Legacy_Zone_Type_Eavesdrop",
                )
                self._emit(event)

    def _evaluate_eavesdrop_rules(self, msg: Message) -> None:
        """Evaluate broadcast telemetry for heuristic sensor correlation.

        :param msg: The immutable Message L7 envelope to evaluate.
        """
        if not self._enable_eavesdrop:
            return

        # Break Mypy strict typing explicitly
        raw_payload: Any = msg.data

        # Catch Controller Sync Array (30C9 from 01 to --:------ or specific)
        if (
            msg.header.verb == I_
            and msg.header.code == Code._30C9
            and getattr(msg.src, "type", None) == "01"
        ):
            event = TopologyChangedEvent(
                action=TopologyAction.UPDATE_TRAITS,
                device_id=msg.src.id,
                metadata={
                    "eavesdrop": "controller_sync",
                    "payload": raw_payload,
                },
                causation="Rule_30C9_Controller_Sync",
            )
            self._emit(event)

        # Catch Orphan Sensor Broadcast (30C9 from sensors to themselves)
        elif (
            msg.header.verb == I_
            and msg.header.code == Code._30C9
            and msg.dst.id != "--:------"
            and msg.dst.id == msg.src.id
        ):
            event = TopologyChangedEvent(
                action=TopologyAction.UPDATE_TRAITS,
                device_id=msg.src.id,
                metadata={
                    "eavesdrop": "orphan_broadcast",
                    "payload": raw_payload,
                },
                causation="Rule_30C9_Orphan_Broadcast",
            )
            self._emit(event)

    def _evaluate_implicit_binding_rules(self, msg: Message) -> None:
        """Evaluate implicit bindings from directed controller polls.

        If a Controller (01:) explicitly sends a direct command (RQ, W) to a
        heating device (e.g., 04: TRV, 00: Zone Sensor, 08: Relay), it implies
        the controller believes that device belongs to its network.

        :param msg: The immutable Message L7 envelope to evaluate.
        :type msg: Message
        :return: None
        :rtype: None
        """
        if not self._enable_eavesdrop:
            return

        # 1. We only care about explicit, directed requests/writes
        if msg.header.verb not in (RQ, W_):
            return

        # 2. The source MUST be a Controller
        if getattr(msg.src, "type", None) != "01":
            return

        # 3. The target MUST be a valid Heating Domain device
        # (00 = Zone Sensor, 04 = TRV, 08 = Relay/BDR91)
        if msg.dst.id == "--:------":
            return

        dst_type = getattr(msg.dst, "type", None)
        if dst_type not in ("00", "04", "08"):
            return

        # Emit the topology mutation event. The downstream Registry
        # will safely process this and ignore it if already bound.
        event = TopologyChangedEvent(
            action=TopologyAction.BIND_DEVICE,
            parent_id=msg.src.id,
            child_id=msg.dst.id,
            metadata={
                "device_role": "actuator" if dst_type in ("04", "08") else "sensor"
            },
            causation="Rule_Implicit_Poll_Binding",
        )
        self._emit(event)

    def _evaluate_third_address_broadcast_rules(self, msg: Message) -> None:
        """Evaluate bindings from the third address field of broadcasts.

        Many heating devices broadcast telemetry (I ---) to no particular
        address (--:------), but explicitly declare their parent
        Controller in the third address slot of the RF frame.

        :param msg: The immutable Message L7 envelope to evaluate.
        :type msg: Message
        :return: None
        :rtype: None
        """
        if not self._enable_eavesdrop:
            return

        if msg.header.verb != I_:
            return

        # Pure L7 architectural access using the new Domain property
        src_type = getattr(msg.src, "type", None)
        if getattr(msg.addr3, "type", None) == "01" and src_type in ("00", "04", "08"):
            event = TopologyChangedEvent(
                action=TopologyAction.BIND_DEVICE,
                parent_id=msg.addr3.id,
                child_id=msg.src.id,
                metadata={
                    "device_role": "actuator" if src_type in ("04", "08") else "sensor"
                },
                causation="Rule_3rd_Address_Declaration",
            )
            self._emit(event)

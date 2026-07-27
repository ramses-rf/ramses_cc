"""RAMSES RF - Asynchronous CQRS State Ingestion Engine.

Consumes messages from the central dispatcher queues and translates
decoded telemetry payloads into frozen, observable StateUpdatedEvents.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from typing import Any, Final

from .. import quirks
from ..const import (
    SZ_ACTIVE,
    SZ_ACTUATOR_COUNTDOWN,
    SZ_ACTUATOR_ENABLED,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BATTERY_LEVEL,
    SZ_BATTERY_LOW,
    SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_CH_SETPOINT,
    SZ_CO2_LEVEL,
    SZ_COOL_ACTIVE,
    SZ_CYCLE_COUNTDOWN,
    SZ_DATETIME,
    SZ_DEWPOINT_TEMP,
    SZ_DHW_ACTIVE,
    SZ_DHW_FLOW_RATE,
    SZ_DIFFERENTIAL,
    SZ_DOMAIN_ID,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FLAME_ON,
    SZ_ZONE_DEMAND,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_LANGUAGE,
    SZ_MAX_REL_MODULATION,
    SZ_MINUTES,
    SZ_MODE,
    SZ_MODULATION_LEVEL,
    SZ_NAME,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_OVERRUN,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_PRESSURE,
    SZ_REL_MODULATION_LEVEL,
    SZ_RELAY_DEMAND,
    SZ_RELAY_FAILSAFE,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT,
    SZ_REQ_REASON,
    SZ_REQ_SPEED,
    SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_UFH_IDX,
    SZ_UNTIL,
    SZ_WINDOW_OPEN,
    SZ_ZONE_IDX,
    Code,
)
from ..messages import Message
from ..models import (
    ActuatorState,
    DemandState,
    DhwState,
    HvacState,
    OpenThermState,
    PowerState,
    StateUpdatedEvent,
    SystemState,
    TemperatureState,
    TrvState,
    UfhState,
    ZoneState,
)
from ..protocol.opentherm import OtDataId

# --- Translation Maps (Static Constant Blocks) ---

RAMSES_HEATING_MAP: Final[dict[Code, tuple[str, str, str]]] = {
    Code._3200: (SZ_TEMPERATURE, "temperatures", "boiler_output"),
    Code._3210: (SZ_TEMPERATURE, "temperatures", "boiler_return"),
    Code._22D9: (SZ_SETPOINT, "temperatures", "boiler_setpoint"),
    Code._1081: (SZ_SETPOINT, "temperatures", "ch_max_setpoint"),
    Code._1300: (SZ_PRESSURE, "base", "ch_water_pressure"),
    Code._12F0: (SZ_DHW_FLOW_RATE, "base", "dhw_flow_rate"),
    Code._10A0: (SZ_SETPOINT, "temperatures", "dhw_setpoint"),
    Code._1260: (SZ_TEMPERATURE, "temperatures", "dhw"),
    Code._1290: (SZ_TEMPERATURE, "temperatures", "outside"),
}

OPENTHERM_FIELD_MAP: Final[dict[OtDataId, tuple[str, str]]] = {
    OtDataId.BOILER_OUTPUT_TEMP: ("temperatures", "boiler_output"),
    OtDataId.BOILER_RETURN_TEMP: ("temperatures", "boiler_return"),
    OtDataId.CONTROL_SETPOINT: ("temperatures", "boiler_setpoint"),
    OtDataId.CH_MAX_SETPOINT: ("temperatures", "ch_max_setpoint"),
    OtDataId.CH_WATER_PRESSURE: ("base", "ch_water_pressure"),
    OtDataId.DHW_FLOW_RATE: ("base", "dhw_flow_rate"),
    OtDataId.DHW_SETPOINT: ("temperatures", "dhw_setpoint"),
    OtDataId.DHW_TEMP: ("temperatures", "dhw"),
    OtDataId.OEM_CODE: ("base", "oem_code"),
    OtDataId.OUTSIDE_TEMP: ("temperatures", "outside"),
    OtDataId.REL_MODULATION_LEVEL: ("base", "rel_modulation_level"),
    OtDataId._0E: ("base", "max_rel_modulation"),
    OtDataId.BURNER_HOURS: ("counters", "burner_hours"),
    OtDataId.BURNER_STARTS: ("counters", "burner_starts"),
    OtDataId.BURNER_FAILED_STARTS: ("counters", "burner_failed_starts"),
    OtDataId.CH_PUMP_HOURS: ("counters", "ch_pump_hours"),
    OtDataId.CH_PUMP_STARTS: ("counters", "ch_pump_starts"),
    OtDataId.DHW_BURNER_HOURS: ("counters", "dhw_burner_hours"),
    OtDataId.DHW_BURNER_STARTS: ("counters", "dhw_burner_starts"),
    OtDataId.DHW_PUMP_HOURS: ("counters", "dhw_pump_hours"),
    OtDataId.DHW_PUMP_STARTS: ("counters", "dhw_pump_starts"),
    OtDataId.FLAME_LOW_SIGNALS: ("counters", "flame_signal_low"),
}

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class StateProjector:
    """Projector task that transforms incoming telemetry into immutable
    states.
    """

    def __init__(self, gwy: Any, ssot_queue: asyncio.Queue[Message]) -> None:
        """Initialize the state projector background worker.

        :param gwy: The active Gateway facade instance.
        :type gwy: Any
        :param ssot_queue: Single Source of Truth Queue from
            CentralDispatcher.
        :type ssot_queue: asyncio.Queue[Message]
        """
        self._gwy = gwy
        self._queue = ssot_queue
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background consumer projector loop.

        :return: None
        :rtype: None
        """
        if self._task is None:
            self._task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stop the background consumer projector loop cleanly.

        :return: None
        :rtype: None
        """
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _worker_loop(self) -> None:
        """Continuously pop messages from the queue for state processing.

        :return: None
        :rtype: None
        """
        while True:
            msg = await self._queue.get()
            try:
                self.process_message_state(msg)
            except Exception as err:
                _LOGGER.error("Failed to ingest state payload: %s", err)
            finally:
                self._queue.task_done()

    def process_message_state(self, msg: Message) -> None:
        """Route valid inbound message envelopes to their respective
        engines.

        :param msg: The message envelope containing raw telemetry.
        :type msg: Message
        :return: None
        :rtype: None
        """
        if getattr(msg, "verb", "") == "RQ" or not isinstance(
            msg.payload, (dict, list)
        ):
            return

        payloads = msg.payload if isinstance(msg.payload, list) else [msg.payload]

        # Unfold dict-of-dicts arrays (e.g. {'00': {'temp_low': 10}})
        unfolded_payloads: list[dict[str, Any]] = []
        for p in payloads:
            if not isinstance(p, dict):
                continue

            if (
                SZ_UFH_IDX not in p
                and SZ_ZONE_IDX not in p
                and "ufx_idx" not in p
                and SZ_DOMAIN_ID not in p
                and all(isinstance(v, dict) for v in p.values())
            ):
                for k, v in p.items():
                    if isinstance(v, dict):
                        # Inject the outer index key so it isn't lost during unfold
                        v_copy = dict(v)
                        v_copy[SZ_UFH_IDX] = k
                        unfolded_payloads.append(v_copy)
            else:
                unfolded_payloads.append(p)

        registry = getattr(self._gwy, "device_registry", None)
        if not registry:
            return

        systems = getattr(registry, "systems", [])
        system_by_id = {s.id: s for s in systems}

        for p in unfolded_payloads:
            # Hexagonal Boundary Enforcement: Route telemetry to Source
            src_dev = registry.device_by_id.get(msg.src.id)
            if src_dev:
                try:
                    self._update_opentherm_state(src_dev, p, msg)
                    self._update_hvac_state(src_dev, p, msg)
                    self._update_power_state(src_dev, p, msg)
                    self._update_dhw_state(src_dev, p, msg)
                    self._update_system_state(src_dev, p, msg)
                    self._update_temperature_state(src_dev, p, msg)
                    self._update_demand_state(src_dev, p, msg)
                    self._update_ufh_state(src_dev, p, msg)
                    self._update_actuator_state(src_dev, p, msg)
                except Exception as err:
                    _LOGGER.error(
                        "CQRS extraction failed for src %s: %s",
                        src_dev.id,
                        err,
                    )

            # Route to Destination Device (Aggregation)
            if msg.dst.id != "--:------" and msg.dst.id != msg.src.id:
                dst_dev = registry.device_by_id.get(msg.dst.id)
                if dst_dev:
                    try:
                        self._update_opentherm_state(dst_dev, p, msg)
                        self._update_hvac_state(dst_dev, p, msg)
                        self._update_power_state(dst_dev, p, msg)
                        self._update_dhw_state(dst_dev, p, msg)
                        self._update_system_state(dst_dev, p, msg)
                        self._update_temperature_state(dst_dev, p, msg)
                        self._update_demand_state(dst_dev, p, msg)
                        self._update_ufh_state(dst_dev, p, msg)
                        self._update_actuator_state(dst_dev, p, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for dst %s: %s",
                            dst_dev.id,
                            err,
                        )

            # Route CQRS state to Systems (TCS) and Zones
            if SZ_ZONE_IDX in p and msg.src.id in system_by_id:
                tcs = system_by_id[msg.src.id]
                zone = tcs.zone_by_idx.get(str(p[SZ_ZONE_IDX]))
                if zone:
                    try:
                        self._update_zone_state(zone, p, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for zone %s: %s",
                            zone.id,
                            err,
                        )

    def _update_opentherm_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate OpenTherm frames or parallel opcodes into
        OpenThermState.
        """
        current_state = getattr(target, "opentherm_state", None)
        if current_state is None:
            if getattr(target, "_SLUG", "") == "OTB":
                current_state = OpenThermState()
            else:
                return

        upd_base: dict[str, Any] = {}
        upd_flag: dict[str, Any] = {}
        upd_temp: dict[str, Any] = {}
        upd_count: dict[str, Any] = {}

        if msg.code == Code._3220:
            raw_id = p.get("msg_id")
            val = p.get("value")

            if raw_id is None:
                return

            try:
                msg_id = OtDataId(raw_id)
            except ValueError:
                return

            if (
                msg_id == OtDataId.STATUS
                and isinstance(val, (list, tuple))
                and len(val) >= 13
            ):
                upd_flag.update(
                    {
                        "ch_enabled": bool(val[0]),
                        "dhw_enabled": bool(val[1]),
                        "cooling_enabled": bool(val[2]),
                        "otc_active": bool(val[3]),
                        "summer_mode": bool(val[5]),
                        "dhw_blocking": bool(val[6]),
                        "fault_present": bool(val[8]),
                        "ch_active": bool(val[9]),
                        "dhw_active": bool(val[10]),
                        "flame_active": bool(val[11]),
                        "cooling_active": bool(val[12]),
                    }
                )
            elif val is not None and msg_id in OPENTHERM_FIELD_MAP:
                category, field_key = OPENTHERM_FIELD_MAP[msg_id]
                if category == "base":
                    upd_base[field_key] = val
                elif category == "temperatures":
                    upd_temp[field_key] = val
                elif category == "counters":
                    upd_count[field_key] = val
                elif category == "flags":
                    upd_flag[field_key] = val
        else:
            if msg.code in RAMSES_HEATING_MAP:
                data = RAMSES_HEATING_MAP[msg.code]
                payload_key, category, state_field = data
                if payload_key in p:
                    if category == "base":
                        upd_base[state_field] = p[payload_key]
                    elif category == "temperatures":
                        upd_temp[state_field] = p[payload_key]
            elif msg.code in (Code._3EF0, Code._3EF1):
                if SZ_REL_MODULATION_LEVEL in p:
                    upd_base["rel_modulation_level"] = p[SZ_REL_MODULATION_LEVEL]
                if SZ_MAX_REL_MODULATION in p:
                    upd_base["max_rel_modulation"] = p[SZ_MAX_REL_MODULATION]
                if SZ_CH_SETPOINT in p:
                    upd_temp["ch_setpoint"] = p[SZ_CH_SETPOINT]
                if SZ_CH_ACTIVE in p:
                    upd_flag["ch_active"] = p[SZ_CH_ACTIVE]
                if SZ_CH_ENABLED in p:
                    upd_flag["ch_enabled"] = p[SZ_CH_ENABLED]
                if SZ_DHW_ACTIVE in p:
                    upd_flag["dhw_active"] = p[SZ_DHW_ACTIVE]
                if SZ_FLAME_ON in p:
                    # NOTE: semantic parser maps this specifically
                    upd_flag["flame_active"] = p[SZ_FLAME_ON]

        if not any((upd_base, upd_flag, upd_temp, upd_count)):
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            upd_base["last_updated"] = dtm

        new_flags = current_state.flags
        if upd_flag:
            new_flags = dataclasses.replace(new_flags, **upd_flag)

        new_temps = current_state.temperatures
        if upd_temp:
            new_temps = dataclasses.replace(new_temps, **upd_temp)

        new_counters = current_state.counters
        if upd_count:
            new_counters = dataclasses.replace(new_counters, **upd_count)

        new_state = dataclasses.replace(
            current_state,
            flags=new_flags,
            temperatures=new_temps,
            counters=new_counters,
            **upd_base,
        )
        target.opentherm_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_hvac_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate complex multi-opcode ventilation payloads into
        HvacState.

        Applies hardware-specific stateful FSM rules (via the Quirks
        middleware) prior to hydration.
        """
        if getattr(target, "_SLUG", "") in (
            "CTL",
            "BDR",
            "TRV",
            "OTB",
            "UFC",
            "DHW",
        ):
            return

        current_state = getattr(target, "hvac_state", None) or HvacState()
        p = quirks.apply_hvac_quirks(p, current_state, msg.code)

        updates: dict[str, Any] = {}

        fields = [
            SZ_CO2_LEVEL,
            SZ_AIR_QUALITY,
            SZ_AIR_QUALITY_BASIS,
            SZ_BYPASS_MODE,
            SZ_BYPASS_POSITION,
            SZ_BYPASS_STATE,
            SZ_EXHAUST_FAN_SPEED,
            SZ_EXHAUST_FLOW,
            SZ_EXHAUST_TEMP,
            SZ_FAN_RATE,
            SZ_FAN_MODE,
            SZ_FAN_INFO,
            SZ_INDOOR_HUMIDITY,
            SZ_INDOOR_TEMP,
            SZ_OUTDOOR_HUMIDITY,
            SZ_OUTDOOR_TEMP,
            SZ_POST_HEAT,
            SZ_PRE_HEAT,
            SZ_PRESENCE_DETECTED,
            SZ_REMAINING_MINS,
            SZ_SPEED_CAPABILITIES,
            SZ_SUPPLY_FAN_SPEED,
            SZ_SUPPLY_FLOW,
            SZ_SUPPLY_TEMP,
            SZ_TEMPERATURE,
            SZ_DEWPOINT_TEMP,
        ]

        # Filter out null-marker values that 31DA/31D9 snapshots emit for
        # sensors the device does not have.  Without this, every polling cycle
        # (~10 min) overwrites good telemetry from 22F1/12A0/22F7 with null
        # markers, causing sensors to bounce to None/FF/0.  See issue #742.
        # This must mirror the filtering in dispatcher._update_hvac_state.
        _NULL_HUMIDITY_FIELDS = frozenset({SZ_INDOOR_HUMIDITY, SZ_OUTDOOR_HUMIDITY})

        for f in fields:
            if f not in p:
                continue
            val = p[f]
            # None = "not implemented" (e.g. EF in bypass_position)
            if val is None:
                continue
            # Raw hex (e.g. "FF", "04") = non-semantic fan_mode from 31D9
            # long-payload devices; the quirk normalises these to None, but
            # filter here as belt-and-suspenders.  See ramses_cc issue 723.
            if f == SZ_FAN_MODE and isinstance(val, str) and len(val) == 2:
                try:
                    int(val, 16)
                    continue
                except ValueError:
                    pass
            # 0.0 for humidity = "no sensor" (00 parses as 0%, physically impossible)
            if f in _NULL_HUMIDITY_FIELDS and val == 0:
                continue
            updates[f] = val

        # Handle non-standard names passed by the semantic parsers
        if SZ_REMAINING_DAYS in p:
            updates["filter_remaining_days"] = p[SZ_REMAINING_DAYS]
        if SZ_REMAINING_PERCENT in p:
            updates["filter_remaining_percent"] = p[SZ_REMAINING_PERCENT]
        if SZ_MINUTES in p and msg.code == Code._22F3:
            updates["boost_timer_mins"] = p[SZ_MINUTES]
        if SZ_REQ_SPEED in p:
            updates["request_fan_speed"] = p[SZ_REQ_SPEED]
        if SZ_REQ_REASON in p:
            updates["request_reason"] = p[SZ_REQ_REASON]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        new_state = dataclasses.replace(current_state, **updates)
        target.hvac_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_power_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate battery opcodes into PowerState."""
        updates: dict[str, Any] = {}
        if msg.code == Code._1060:
            if SZ_BATTERY_LOW in p:
                updates["battery_low"] = p[SZ_BATTERY_LOW]
            if SZ_BATTERY_LEVEL in p:
                updates["battery_level"] = p[SZ_BATTERY_LEVEL]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "power_state", None) or PowerState()
        new_state = dataclasses.replace(current_state, **updates)
        target.power_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_dhw_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate DHW opcodes into DhwState."""
        if msg.code not in (Code._10A0, Code._1260, Code._1F41):
            return

        updates: dict[str, Any] = {}
        if msg.code == Code._10A0:
            if SZ_SETPOINT in p:
                updates["setpoint"] = p[SZ_SETPOINT]
            if SZ_OVERRUN in p:
                updates["overrun"] = p[SZ_OVERRUN]
            if SZ_DIFFERENTIAL in p:
                updates["differential"] = p[SZ_DIFFERENTIAL]
        elif msg.code == Code._1260:
            if SZ_TEMPERATURE in p:
                updates["temperature"] = p[SZ_TEMPERATURE]
        elif msg.code == Code._1F41:
            if SZ_MODE in p:
                updates["mode"] = p[SZ_MODE]
            if SZ_ACTIVE in p:
                updates["active"] = p[SZ_ACTIVE]
            if SZ_UNTIL in p:
                updates["until"] = p[SZ_UNTIL]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "dhw_state", None) or DhwState()
        new_state = dataclasses.replace(current_state, **updates)
        target.dhw_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_system_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate system configuration opcodes into SystemState."""
        if msg.code not in (Code._0100, Code._2E04, Code._313F):
            return

        updates: dict[str, Any] = {}
        if msg.code == Code._0100:
            if SZ_LANGUAGE in p:
                updates["language"] = p[SZ_LANGUAGE]
        elif msg.code == Code._2E04:
            if SZ_SYSTEM_MODE in p:
                updates["system_mode"] = p[SZ_SYSTEM_MODE]
            if SZ_UNTIL in p:
                updates["until"] = p[SZ_UNTIL]
        elif msg.code == Code._313F:
            if SZ_DATETIME in p:
                updates["datetime"] = p[SZ_DATETIME]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "system_state", None) or SystemState()
        new_state = dataclasses.replace(current_state, **updates)
        target.system_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_temperature_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate temperature/TRV opcodes into TrvState &
        TemperatureState.
        """
        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))

        if msg.code == Code._12B0 and SZ_WINDOW_OPEN in p:
            current_trv = getattr(target, "trv_state", None) or TrvState()
            trv_updates = {"window_open": p[SZ_WINDOW_OPEN]}
            if dtm:
                trv_updates["last_updated"] = dtm

            new_trv = dataclasses.replace(current_trv, **trv_updates)
            target.trv_state = new_trv

            event = StateUpdatedEvent(
                entity_id=getattr(target, "id", "unknown"),
                state=new_trv,
                correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
                causation_id=getattr(msg, "message_id", uuid.uuid4()),
            )
            if hasattr(target, "apply_state_update"):
                target.apply_state_update(event)

        if msg.code in (Code._30C9, Code._1260, Code._0002):
            updates: dict[str, Any] = {}
            if SZ_TEMPERATURE in p:
                updates["temperature"] = p[SZ_TEMPERATURE]
            if SZ_SETPOINT in p:
                updates["setpoint"] = p[SZ_SETPOINT]

            if updates:
                if dtm:
                    updates["last_updated"] = dtm

                current_temp = getattr(target, "temp_state", None) or TemperatureState()
                new_temp = dataclasses.replace(current_temp, **updates)
                target.temp_state = new_temp

                event = StateUpdatedEvent(
                    entity_id=getattr(target, "id", "unknown"),
                    state=new_temp,
                    correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
                    causation_id=getattr(msg, "message_id", uuid.uuid4()),
                )
                if hasattr(target, "apply_state_update"):
                    target.apply_state_update(event)

    def _update_demand_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate demand opcodes into DemandState."""
        if msg.code not in (Code._3150, Code._0008, Code._0009):
            return

        updates: dict[str, Any] = {}
        slug = getattr(target, "_SLUG", "")

        if msg.code == Code._3150 and SZ_ZONE_DEMAND in p:
            # Prevent flattened array payloads (e.g., UFH circuit demands)
            # from overwriting the controller's aggregate FC heat demand.
            if slug in ("CTL", "UFC"):
                if p.get(SZ_DOMAIN_ID) == "FC":
                    updates["zone_demand"] = p[SZ_ZONE_DEMAND]
            elif SZ_UFH_IDX not in p and "ufx_idx" not in p:
                updates["zone_demand"] = p[SZ_ZONE_DEMAND]

        elif msg.code == Code._0008 and SZ_RELAY_DEMAND in p:
            # Prevent FA (UFH) relay demands from overwriting FC relay demand
            if slug == "UFC" and p.get(SZ_DOMAIN_ID) != "FC":
                pass
            else:
                updates["relay_demand"] = p[SZ_RELAY_DEMAND]

        elif msg.code == Code._0009 and SZ_RELAY_FAILSAFE in p:
            updates["relay_failsafe"] = p[SZ_RELAY_FAILSAFE]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "demand_state", None) or DemandState()
        new_state = dataclasses.replace(current_state, **updates)
        target.demand_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_ufh_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate UFH circuit arrays and bounds into UfhState."""
        if msg.code not in (Code._3150, Code._0008, Code._22C9):
            return

        if getattr(target, "_SLUG", "") != "UFC":
            return

        current_state = getattr(target, "ufh_state", None) or UfhState()
        updates: dict[str, Any] = {}

        # Safely extract index matching legacy typo "ufx_idx"
        idx = p.get("ufx_idx") or p.get(SZ_UFH_IDX) or p.get(SZ_ZONE_IDX)

        if msg.code == Code._3150 and idx is not None and SZ_ZONE_DEMAND in p:
            new_demands = dict(current_state.zone_demands)
            new_demands[str(idx)] = p[SZ_ZONE_DEMAND]
            updates["zone_demands"] = new_demands
        elif (
            msg.code == Code._0008
            and p.get(SZ_DOMAIN_ID) == "FA"
            and SZ_RELAY_DEMAND in p
        ):
            updates["relay_demand_fa"] = p[SZ_RELAY_DEMAND]
        elif msg.code == Code._22C9 and idx is not None:
            new_sp = dict(current_state.setpoints)
            sp_data = dict(new_sp.get(str(idx), {}))

            # Legacy parsers return an empty dict if no bounds exist.
            # Only populate the bounds if they are explicitly present.
            bounds = p.get(SZ_SETPOINT_BOUNDS)
            if isinstance(bounds, tuple) and len(bounds) == 2:
                sp_data["temp_low"] = bounds[0]
                sp_data["temp_high"] = bounds[1]

            new_sp[str(idx)] = sp_data
            updates["setpoints"] = new_sp

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        new_state = dataclasses.replace(current_state, **updates)
        target.ufh_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_actuator_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate actuator state opcodes into ActuatorState."""
        if msg.code not in (Code._3EF0, Code._3EF1):
            return

        updates: dict[str, Any] = {}
        if SZ_MODULATION_LEVEL in p:
            # NOTE: semantic parser custom keys
            updates["modulation_level"] = p[SZ_MODULATION_LEVEL]
        elif SZ_REL_MODULATION_LEVEL in p:
            updates["modulation_level"] = p[SZ_REL_MODULATION_LEVEL]

        if SZ_ACTUATOR_ENABLED in p:
            updates["actuator_enabled"] = p[SZ_ACTUATOR_ENABLED]
        if SZ_CH_ACTIVE in p:
            updates["ch_active"] = p[SZ_CH_ACTIVE]
        if SZ_CH_ENABLED in p:
            updates["ch_enabled"] = p[SZ_CH_ENABLED]
        if SZ_DHW_ACTIVE in p:
            updates["dhw_active"] = p[SZ_DHW_ACTIVE]
        if SZ_FLAME_ON in p:
            # NOTE: semantic parser maps this specifically
            updates["flame_active"] = p[SZ_FLAME_ON]
            updates["flame_on"] = p[SZ_FLAME_ON]

        # Legacy diagnostic payloads restored for backwards compatibility
        if SZ_CH_SETPOINT in p:
            updates["ch_setpoint"] = p[SZ_CH_SETPOINT]
        if SZ_MAX_REL_MODULATION in p:
            updates["max_rel_modulation"] = p[SZ_MAX_REL_MODULATION]
        if SZ_COOL_ACTIVE in p:
            updates["cool_active"] = p[SZ_COOL_ACTIVE]
        if SZ_ACTUATOR_COUNTDOWN in p:
            updates["actuator_countdown"] = p[SZ_ACTUATOR_COUNTDOWN]
        if SZ_CYCLE_COUNTDOWN in p:
            updates["cycle_countdown"] = p[SZ_CYCLE_COUNTDOWN]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "act_state", None) or ActuatorState()
        new_state = dataclasses.replace(current_state, **updates)
        target.act_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_zone_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate zone configuration opcodes into ZoneState."""
        if msg.code != Code._0004:
            return

        updates: dict[str, Any] = {}
        if SZ_NAME in p:
            updates["name"] = str(p[SZ_NAME])

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "zone_state", None) or ZoneState()
        new_state = dataclasses.replace(current_state, **updates)
        target.zone_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

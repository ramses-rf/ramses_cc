#!/usr/bin/env python3
"""RAMSES RF - Decode/process a message (payload into JSON)."""

# TODO:
# - fix dispatching - what devices (some are Addr) are sent packets, esp. 1FC9s

from __future__ import annotations

import contextlib
import dataclasses
import logging
import uuid
from datetime import timedelta as td
from typing import TYPE_CHECKING, Any, Final

from ..ramses_tx import ALL_DEV_ADDR

from . import exceptions as exc
from .const import (
    DEV_TYPE_MAP,
    DONT_CREATE_ENTITIES,
    DONT_UPDATE_ENTITIES,
    I_,
    RP,
    RQ,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE,
    SZ_CO2_LEVEL,
    SZ_DEVICES,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FILTER_DIRTY,
    SZ_FROST_CYCLE,
    SZ_HAS_FAULT,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_MINUTES,
    SZ_OFFER,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_PHASE,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_REMAINING_MINS,
    SZ_REQ_REASON,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_TEMPERATURE,
    W_,
    Code,
    DevType,
)
from .messages import Message
from .models import StateUpdatedEvent
from .protocol.ramses import (
    CODES_OF_HEAT_DOMAIN,
    CODES_OF_HEAT_DOMAIN_ONLY,
    CODES_OF_HVAC_DOMAIN_ONLY,
)
from .protocol_schema import CODES_BY_DEV_SLUG

if TYPE_CHECKING:
    from .gateway import Gateway

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_FORCE_LOG_MESSAGES: Final[bool] = False  # useful for dev/test
_DBG_INCREASE_LOG_LEVELS: Final[bool] = (
    False  # set True for developer-friendly log spam
)

_LOGGER = logging.getLogger(__name__)


__all__ = [
    "detect_array_fragment",
    "instantiate_devices",
    "process_msg",
    "route_payload",
    "validate_addresses",
    "validate_slugs",
]


MSG_FORMAT_18 = "|| {:18s} | {:18s} | {:2s} | {:16s} | {:^4s} || {}"

_TD_SECONDS_003 = td(seconds=3)


def _log_message(gwy: Gateway, msg: Message) -> None:
    """Log msg according to src, code, log.debug setting.

    :param gwy: The gateway handling the message.
    :type gwy: Gateway
    :param msg: the Message being processed.
    :type msg: Message
    """
    if _DBG_FORCE_LOG_MESSAGES:
        _LOGGER.warning(msg)
    elif msg.src != gwy.hgi or (msg.code != Code._PUZZ and msg.verb != RQ):
        _LOGGER.info(msg)
    elif msg.src != gwy.hgi or msg.verb != RQ:
        _LOGGER.info(msg)
    elif _LOGGER.getEffectiveLevel() == logging.DEBUG:
        _LOGGER.info(msg)


def validate_addresses(gwy: Gateway, msg: Message) -> bool:
    """Validate the packet's address set for basic structural rules.

    This is Stage 1 of the processing pipeline. It evaluates the raw addressing
    metadata. If the addresses violate domain-specific rules, an exception is
    raised and caught by the pipeline executor.

    :param gwy: The gateway handling the message.
    :type gwy: Gateway
    :param msg: The message containing source/destination addresses.
    :type msg: Message
    :raises exc.PacketAddrSetInvalid: If the address pair is invalid.
    :return: True if the pipeline should proceed, False if processing
             is configured to halt before entity creation.
    :rtype: bool
    """
    # TODO: needs work: doesn't take into account device's (non-HVAC) class
    if (
        msg.src.id != msg.dst.id
        and msg.src.type == msg.dst.type
        and msg.src.type in DEV_TYPE_MAP.HEAT_DEVICES  # could still be HVAC domain
    ):
        # .I --- 18:013393 18:000730 --:------ 0001 005 00FFFF0200     # invalid
        # .I --- 01:078710 --:------ 01:144246 1F09 003 FF04B5         # invalid
        # .I --- 29:151550 29:237552 --:------ 22F3 007 00023C03040000 # valid? HVAC

        # 🚨 CQRS Bypass: Permit UFCs (02:) to communicate directly (e.g. Autotemp)
        if msg.src.type == "02":
            pass
        elif msg.code in CODES_OF_HEAT_DOMAIN_ONLY:
            raise exc.PacketAddrSetInvalid(
                f"Invalid addr pair: {msg.src!r}/{msg.dst!r}"
            )
        elif msg.code in CODES_OF_HEAT_DOMAIN:
            _LOGGER.warning(
                f"{msg!r} < Invalid addr pair: {msg.src!r}/{msg.dst!r}, is it HVAC?"
            )
        elif msg.code not in CODES_OF_HVAC_DOMAIN_ONLY:
            _LOGGER.info(
                f"{msg!r} < Invalid addr pair: {msg.src!r}/{msg.dst!r}, is it HVAC?"
            )

    # TODO: any use in creating a device only if the payload is valid?
    return gwy.config.reduce_processing < DONT_CREATE_ENTITIES


def instantiate_devices(gwy: Gateway, msg: Message) -> bool:
    """Ensure the source and destination devices exist in the registry.

    This is Stage 2 of the processing pipeline. It attempts to discover or
    map the addresses to actual Device objects. If a required device cannot be
    found, it logs a warning and halts the pipeline.

    :param gwy: The gateway containing the device registry.
    :type gwy: Gateway
    :param msg: The message to inject discovered devices into.
    :type msg: Message
    :return: True if devices were mapped/created successfully, False otherwise.
    :rtype: bool
    """
    try:
        # FIXME: changing Address to Devices is messy: ? Protocol for same
        # method signatures. prefer Devices but can continue with Addresses...
        src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
        dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)

        # Devices need to know their controller, ?and their location ('parent' domain)
        # NB: only addrs processed here, packet metadata is processed elsewhere

        # Determining bindings to a controller:
        #  - configury; As per any schema      # codespell:ignore configury
        #  - discovery: If in 000C pkt, or pkt *to* device where src is a controller
        #  - eavesdrop: If pkt *from* device where dst is a controller

        # Determining location in a schema (domain/DHW/zone):
        #  - configury; As per any schema      # codespell:ignore configury
        #  - discovery: If in 000C pkt - unable for 10: & 00: (TRVs)
        #  - discovery: from packet fingerprint, excl. payloads (only for 10:)
        #  - eavesdrop: from packet fingerprint, incl. payloads

        if src_dev is None:
            # may: DeviceNotFoundError, but don't suppress
            src_dev = gwy.device_registry.get_device(msg.src.id)
            if msg.dst.id == msg.src.id:
                return True

        if not gwy.config.enable_eavesdrop:
            return True

        hgi_id = gwy.hgi.id if gwy.hgi else None
        if dst_dev is None and msg.src.id != hgi_id:
            with contextlib.suppress(exc.DeviceNotFoundError):
                gwy.device_registry.get_device(msg.dst.id)

        # Eavesdrop: Instantiate implicitly referenced devices (e.g., parent
        # controller in addr2)
        if hasattr(msg._pkt, "_addrs"):
            for addr in msg._pkt._addrs:
                if addr.id not in (msg.src.id, getattr(msg.dst, "id", None)):
                    with contextlib.suppress(exc.DeviceNotFoundError):
                        gwy.device_registry.get_device(addr.id)

    except exc.DeviceNotFoundError as err:
        (_LOGGER.error if _DBG_INCREASE_LOG_LEVELS else _LOGGER.warning)(
            "%s < %s(%s)", msg._pkt, err.__class__.__name__, err
        )
        return False

    return True


def validate_slugs(gwy: Gateway, msg: Message) -> bool:
    """Validate the device classes against the transmitted code/verb.

    This is Stage 3 of the processing pipeline. It verifies whether the
    source is permitted to Tx this payload, and if the destination is
    permitted to Rx it, based on protocol schemas.

    :param gwy: The gateway handling the message.
    :type gwy: Gateway
    :param msg: The message containing the verb and code to validate.
    :type msg: Message
    :raises exc.PacketInvalid: If either slug cannot process the verb/code.
    :return: True if slugs are valid, False if processing limits dictate halting.
    :rtype: bool
    """
    # 1. Check Source Slug
    src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
    slug = getattr(src_dev, "_SLUG", None)

    if slug not in (None, DevType.HGI, DevType.DEV, DevType.HEA, DevType.HVC):
        # TODO: use DEV_TYPE_MAP.PROMOTABLE_SLUGS
        if slug not in CODES_BY_DEV_SLUG:
            raise exc.PacketInvalid(f"{msg!r} < Unknown src slug ({slug}), is it HVAC?")

        if msg.code not in CODES_BY_DEV_SLUG[slug]:
            raise exc.PacketInvalid(f"{msg!r} < Unexpected code for src ({slug}) to Tx")

        if msg.verb not in CODES_BY_DEV_SLUG[slug][msg.code]:
            raise exc.PacketInvalid(
                f"{msg!r} < Unexpected verb/code for src ({slug}) to Tx"
            )

    # 2. Check Destination Slug
    if (
        slug != DevType.HGI  # avoid: msg.src.id != gwy.hgi.id
        and msg.verb != I_
        and msg.dst.id != msg.src.id
    ):
        # HGI80 can do what it likes
        # receiving an I_ isn't currently in the schema & so can't yet be tested
        dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)
        dst_slug = getattr(dst_dev, "_SLUG", None)

        if dst_slug not in (None, DevType.HGI, DevType.DEV, DevType.HEA, DevType.HVC):
            if dst_slug not in CODES_BY_DEV_SLUG:
                raise exc.PacketInvalid(
                    f"{msg!r} < Unknown dst slug ({dst_slug}), is it HVAC?"
                )

            if f"{dst_slug}/{msg.verb}/{msg.code}" not in (f"CTL/{RQ}/{Code._3EF1}",):
                # HACK: an exception-to-the-rule that need sorting
                if msg.code not in CODES_BY_DEV_SLUG[dst_slug]:
                    raise exc.PacketInvalid(
                        f"{msg!r} < Unexpected code for dst ({dst_slug}) to Rx"
                    )

                if f"{msg.verb}/{msg.code}" not in (f"{W_}/{Code._0001}",):
                    # HACK: an exception-to-the-rule that need sorting
                    if f"{dst_slug}/{msg.verb}/{msg.code}" not in (
                        f"{DevType.BDR}/{RQ}/{Code._3EF0}",
                    ):
                        # HACK: an exception-to-the-rule that need sorting
                        if {RQ: RP, RP: RQ, W_: I_}[msg.verb] not in CODES_BY_DEV_SLUG[
                            dst_slug
                        ][msg.code]:
                            raise exc.PacketInvalid(
                                f"{msg!r} < Unexpected verb/code for dst "
                                f"({dst_slug}) to Rx"
                            )

    return gwy.config.reduce_processing < DONT_UPDATE_ENTITIES


def _resolve_logical_targets(
    gwy: Gateway, msg: Message, p: dict[str, Any]
) -> list[Any]:
    """Resolve all logical software twins that should ingest this payload."""
    targets = []
    src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
    dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)

    # 1. Fault logs strictly target the TCS (if it exists) or the source device
    if msg.code == "0418":
        if src_dev and hasattr(src_dev, "tcs") and src_dev.tcs:
            targets.append(getattr(src_dev.tcs, "faultlog", src_dev))
        elif src_dev:
            targets.append(src_dev)
        return targets

    # 2. Hardware twin (Sender) always gets the update UNLESS it's a Controller/UFC
    # actively broadcasting an array of children's states (e.g., a 30C9 sync).
    src_type = getattr(src_dev, "type", None)
    has_arr = getattr(msg, "_has_array", False)
    if src_type not in ("01", "02") or not has_arr:
        if src_dev:
            targets.append(src_dev)

    # 3. Hardware twin (Destination) gets the update.
    # Legacy routes packets to the destination device's cache. To maintain
    # strict parity, we mirror this.
    # HVAC packets (e.g. 22F1 fan_mode from REM→FAN) target the destination
    # device's hvac_state directly, so we also accept devices that have
    # hvac_state even if they lack apply_state_update.
    if msg.dst.id != msg.src.id and getattr(msg.dst, "id", "") != "63:262142":
        if (
            dst_dev
            and (
                hasattr(dst_dev, "apply_state_update") or hasattr(dst_dev, "hvac_state")
            )
            and dst_dev not in targets
        ):
            targets.append(dst_dev)

    # 4. Virtual twins (Zones) get updates if explicitly addressed by idx.
    if "zone_idx" in p and src_dev and hasattr(src_dev, "tcs") and src_dev.tcs:
        if zone := src_dev.tcs.zone_by_idx.get(p["zone_idx"]):
            if zone not in targets:
                targets.append(zone)

    # 5. Domain twins (TCS, DHW) get updates.
    if "domain_id" in p and src_dev and hasattr(src_dev, "tcs") and src_dev.tcs:
        domain_id = p["domain_id"]
        if domain_id == "FC" and src_dev.tcs not in targets:
            targets.append(src_dev.tcs)
        elif domain_id in ("FA", "F9") and hasattr(src_dev.tcs, "dhw"):
            if src_dev.tcs.dhw not in targets:
                targets.append(src_dev.tcs.dhw)

    return targets


def _update_temperature_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate temperature data into a frozen StateUpdatedEvent."""
    if not hasattr(target, "temp_state"):
        return

    updates: dict[str, Any] = {}

    if "temperature" in p:
        # Legacy Parity: Physical sensors only track their own local sensor readings.
        # We must ignore Zone temperature syncs sent TO them by the Controller.
        target_id = getattr(target, "id", str(target))
        src_id = getattr(msg.src, "id", str(msg.src))

        if getattr(target, "_SLUG", "") in ("TRV", "THM") and src_id != target_id:
            pass
        else:
            updates["temperature"] = p["temperature"]

    if "setpoint" in p:
        updates["setpoint"] = p["setpoint"]

    if not updates:
        return

    new_state = dataclasses.replace(target.temp_state, **updates)
    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    target.apply_state_update(event)


def _update_demand_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate demand data into a frozen StateUpdatedEvent."""
    if not hasattr(target, "demand_state"):
        return

    updates: dict[str, Any] = {}
    if "zone_demand" in p:
        updates["zone_demand"] = p["zone_demand"]
    if "relay_demand" in p:
        updates["zone_demand"] = p["relay_demand"]
        updates["relay_active"] = float(p["relay_demand"]) > 0.0

    if not updates:
        return

    new_state = dataclasses.replace(target.demand_state, **updates)
    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    target.apply_state_update(event)


def _update_faultlog_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate 0418 fault log data into a frozen StateUpdatedEvent.

    This handles the immutable tuple appending tracking required by the
    CQRS FaultLogState read-model container.

    :param target: The target entity software twin to update.
    :type target: Any
    :param p: The parsed message payload dictionary.
    :type p: dict[str, Any]
    :param msg: The immutable Message L7 envelope.
    :type msg: Message
    :return: None
    :rtype: None
    """
    if msg.code != "0418" or not hasattr(target, "state"):
        return
    if type(target.state).__name__ != "FaultLogState":
        return

    # Guard: Ensure the entry index exists in the parsed payload
    if "log_idx" not in p:
        return

    from .systems.faultlog import FaultLogEntry

    with contextlib.suppress(Exception):
        entry = FaultLogEntry.from_msg(msg)

        # Append to the immutable tuple, safely removing stale matching timestamps
        current_entries = getattr(target.state, "entries", ())
        filtered = [e for e in current_entries if e.timestamp != entry.timestamp]
        new_entries = tuple(filtered) + (entry,)

        latest = getattr(target.state, "latest_fault", None)
        if getattr(entry.fault_state, "value", str(entry.fault_state)) == "fault":
            latest = entry

        new_state = dataclasses.replace(
            target.state, entries=new_entries, latest_fault=latest
        )

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        target.apply_state_update(event)


def _update_hvac_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate HVAC ventilation payloads into a frozen HvacState.

    Handles 31D9/31DA/22F1/22F3/10D0/12A0/1298 and related opcodes,
    porting the logic from ``pipeline/ingestion.py`` into the dispatcher's
    CQRS ingestion engine.  See issue #649 / #547.
    """
    if getattr(target, "_SLUG", "") in ("CTL", "BDR", "TRV", "OTB", "UFC", "DHW"):
        return

    if not hasattr(target, "hvac_state"):
        return

    from . import quirks

    p = quirks.apply_hvac_quirks(p, target.hvac_state, msg.code)

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
        SZ_FILTER_DIRTY,
        SZ_FROST_CYCLE,
        SZ_HAS_FAULT,
        "dewpoint_temp",
    ]

    # Filter out null-marker values that 31DA/31D9 snapshots emit for
    # sensors the device does not have.  Without this, every polling cycle
    # (~10 min) overwrites good telemetry from 22F1/12A0/22F7 with null
    # markers, causing sensors to bounce to None/FF/0.  See issue #742.
    _NULL_HUMIDITY_FIELDS = frozenset({SZ_INDOOR_HUMIDITY, SZ_OUTDOOR_HUMIDITY})

    updates: dict[str, Any] = {}
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
    if "days_remaining" in p and p["days_remaining"] is not None:
        updates["filter_remaining_days"] = p["days_remaining"]
    if "percent_remaining" in p and p["percent_remaining"] is not None:
        updates["filter_remaining_percent"] = p["percent_remaining"]
    if SZ_MINUTES in p and msg.code == Code._22F3 and p[SZ_MINUTES] is not None:
        updates["boost_timer_mins"] = p[SZ_MINUTES]
    if "req_speed" in p and p["req_speed"] is not None:
        updates["request_fan_speed"] = p["req_speed"]
    if SZ_REQ_REASON in p and p[SZ_REQ_REASON] is not None:
        updates["request_reason"] = p[SZ_REQ_REASON]

    if not updates:
        return

    new_state = dataclasses.replace(target.hvac_state, **updates)
    target.hvac_state = new_state

    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    if hasattr(target, "apply_state_update"):
        target.apply_state_update(event)


def _cqrs_ingestion_engine(gwy: Gateway, msg: Message) -> None:
    """Parallel ingestion engine to populate immutable CQRS read-models.

    This acts as a Strangler Fig, intercepting decoded payloads and mapping
    them directly into the new `StateUpdatedEvent` structures.
    """
    # Legacy Parity: Request packets do not contain authoritative telemetry.
    if getattr(msg, "verb", "") == "RQ":
        return

    if not isinstance(msg.payload, (dict, list)):
        return

    payloads = msg.payload if isinstance(msg.payload, list) else [msg.payload]

    for p in payloads:
        if not isinstance(p, dict):
            continue

        targets = _resolve_logical_targets(gwy, msg, p)
        for target in targets:
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                _update_hvac_state(target, p, msg)
                _update_temperature_state(target, p, msg)
                _update_demand_state(target, p, msg)
                _update_faultlog_state(target, p, msg)


def route_payload(gwy: Gateway, msg: Message) -> None:
    """Determine target entities and deliver the payload to them.

    This is the final stage (Stage 4) of the pipeline. It routes messages to
    the source device (for internal state updates) and constructs a list of
    destination devices based on binding offers, eavesdropping rules, and
    faked device states.

    :param gwy: The gateway handling the message routing.
    :type gwy: Gateway
    :param msg: The fully validated message to be dispatched.
    :type msg: Message
    """
    # NOTE: here, msgs are routed only to devices: routing to other entities (i.e.
    # systems, zones, circuits) is done by those devices (e.g. UFC to UfhCircuit)

    src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
    if src_dev is not None:
        gwy._engine._loop.call_soon(src_dev._handle_msg, msg)

    devices: list[Any] = []

    if (
        msg.code == Code._1FC9
        and isinstance(msg.payload, dict)  # 1. Ensure it's a dict (not bytes)
        and msg.payload.get(SZ_PHASE) == SZ_OFFER  # 2. Safely check for key
    ):
        devices = [
            d
            for d in gwy.device_registry.devices
            if d.id != msg.src.id and d._is_binding
        ]

    elif msg.dst.id == ALL_DEV_ADDR.id:  # some offers use dst=63:, so after 1FC9
        devices = [
            d for d in gwy.device_registry.devices if d.id != msg.src.id and d.is_faked
        ]

    else:
        dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)
        if msg.dst.id != msg.src.id and dst_dev is not None:
            devices.append(dst_dev)

        if src_dev and hasattr(src_dev, SZ_DEVICES) and src_dev.devices:
            for d in src_dev.devices:
                if d.id != msg.src.id and d not in devices:
                    devices.append(d)

    # Add Eavesdropping Correlation Routing
    if gwy.config.enable_eavesdrop and hasattr(msg._pkt, "_addrs"):
        for addr in msg._pkt._addrs:
            if addr.id != msg.src.id and addr.id != getattr(msg.dst, "id", None):
                if dev := gwy.device_registry.device_by_id.get(addr.id):
                    if dev not in devices:
                        devices.append(dev)

    for d in devices:
        if d.id != msg.src.id:
            gwy._engine._loop.call_soon(d._handle_msg, msg)


async def process_msg(gwy: Gateway, msg: Message) -> None:
    """Decode the packet payload and route it through the message pipeline.

    This executor acts as a Chain of Responsibility, routing the message
    through sequential, mathematically isolated validation and dispatch stages.

    :param gwy: The gateway instance handling the routing.
    :type gwy: Gateway
    :param msg: The processed message to route.
    :type msg: Message
    """
    # All methods require msg with a valid payload, except instantiate_devices(),
    # which requires a valid payload only for 000C.
    try:
        if not validate_addresses(gwy, msg):
            _log_message(gwy, msg)
            return

        if not instantiate_devices(gwy, msg):
            return

        if not validate_slugs(gwy, msg):
            _log_message(gwy, msg)
            return

        # Store the message BEFORE routing/ingestion so that any signal
        # handlers (e.g. SIGNAL_UPDATE) that trigger entity_state reads
        # will find the message already in the store.  Previously this
        # was in the `else` block (after route_payload), creating a race
        # where the async _msg_handler Task hadn't written the message
        # before entity state cursors advanced past it.
        if gwy.message_store:
            gwy.message_store.add(msg)

        _cqrs_ingestion_engine(gwy, msg)

        route_payload(gwy, msg)

    except (AssertionError, exc.RamsesException, NotImplementedError) as err:
        (_LOGGER.error if _DBG_INCREASE_LOG_LEVELS else _LOGGER.warning)(
            "%s < %s(%s)", msg._pkt, err.__class__.__name__, err
        )

    except (AttributeError, LookupError, TypeError, ValueError) as err:
        if getattr(gwy.config, "enforce_strict_handling", False):
            raise
        _LOGGER.warning(
            "%s < %s(%s)", msg._pkt, err.__class__.__name__, err, exc_info=True
        )

    else:
        _log_message(gwy, msg)


# TODO: this needs cleaning up (e.g. handle intervening packet)
def detect_array_fragment(this: Message, prev: Message) -> bool:  # _PayloadT
    """Return True if this pkt is the latter half of an array.

    :param this: The current message being evaluated.
    :type this: Message
    :param prev: The previously received message.
    :type prev: Message
    :return: True if the packet is part of a merged array, False otherwise.
    :rtype: bool
    """
    # This will work, even if the 2nd pkt._is_array == False as 1st == True
    # .I --- 01:158182 --:------ 01:158182 000A 048 001201F409C4011101F409C40...
    # .I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4

    return bool(
        prev._has_array
        and this.code in (Code._000A, Code._22C9)  # TODO: not a complete list
        and this.code == prev.code
        and this.verb == prev.verb == I_
        and this.src == prev.src
        and this.dtm < prev.dtm + _TD_SECONDS_003
    )

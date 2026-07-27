#!/usr/bin/env python3
"""RAMSES RF - Passive device scan engine.

A read-only observer that listens to RF traffic, classifies unknown devices
by prefix and verb/code pairs, and maintains an in-memory discovery list.

This module is the scan engine only — it does NOT:
  - create devices in the registry (no `get_device()` calls)
  - mutate topology (no `TopologyChangedEvent`s)
  - write to disk (everything in-memory, consumer calls `export_json()`)
  - depend on Home Assistant (plain Python, works in CLI)

The consumer (ramses_cc or the CLI) is responsible for persistence,
notifications, and user-facing accept/discard workflow.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any

from .const import DevType
from .protocol.ramses import HVAC_KLASS_BY_VC_PAIR
from ..ramses_tx.const import SZ_ACTIVE_HGI

if TYPE_CHECKING:
    from .gateway import Gateway
    from ..ramses_tx.dtos import PacketDTO


_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification maps
# ---------------------------------------------------------------------------

# Prefix → likely DevType (CH + HVAC domain).
# Not reused from DEV_TYPE_MAP because that maps prefix→human-readable string
# (e.g. "controller"), not prefix→DevType enum (e.g. CTL). Also, DEV_TYPE_MAP
# lacks HVAC prefixes (32:=FAN, 37:=REM). TopologyBuilder has a similar local
# dict but smaller (only eavesdrop-promotable types). Keeping our own is
# the simplest correct approach.
_PREFIX_TO_TYPE: dict[str, DevType] = {
    "00": DevType.TRV,  # radiator_valve (rare, same class as 04:)
    "01": DevType.CTL,
    "02": DevType.UFC,
    "03": DevType.THM,
    "04": DevType.TRV,
    "07": DevType.DHW,
    "08": DevType.JIM,
    "10": DevType.BDR,
    "12": DevType.THM,
    "13": DevType.OTB,
    "17": DevType.OUT,
    "18": DevType.HGI,
    "22": DevType.THM,
    "23": DevType.PRG,
    "30": DevType.RFG,
    "31": DevType.JST,
    "32": DevType.FAN,
    "34": DevType.RND,
    "37": DevType.REM,
}

# Verb+code → DevType (HVAC domain, from HVAC_KLASS_BY_VC_PAIR).
# Keys are (verb_value, code_value) tuples for fast lookup.
_VC_TO_TYPE: dict[tuple[str, str], DevType] = {
    (v.value, str(c)): dt for (v, c), dt in HVAC_KLASS_BY_VC_PAIR.items()
}

# Codes that only a CTL sends (from CODES_ONLY_FROM_CTL).
# If a device sends one of these, it's definitely a CTL.
_CTL_ONLY_CODES: frozenset[str] = frozenset({"1030", "1F09", "313F"})

# Codes that indicate battery-powered devices.
_BATTERY_CODES: frozenset[str] = frozenset({"1060", "1FC9"})

# Codes that carry zone_idx in the payload (binding telemetry).
# Used to extract zone assignment from traffic.
_ZONE_BINDING_CODES: frozenset[str] = frozenset(
    {"3150", "30C9", "000C", "2309", "2349", "10A0", "1260", "12B0", "1F09"}
)

# 32: is unambiguous — always a FAN. A FAN sends 22F1 (which maps to REM in
# HVAC_KLASS_BY_VC_PAIR) but is still a FAN, so the prefix must win.
# 37: is ambiguous (REM, CO2, or HUM all use 37:) — needs the VC pair to
# distinguish, so it's NOT in this set.
_UNAMBIGUOUS_HVAC_PREFIXES: frozenset[str] = frozenset({"32"})


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredDevice:
    """A device seen on RF, classified but not yet created in the registry.

    This dataclass is the scan engine's view of a device. The consumer
    (ramses_cc) extends it with status/enabled/owner/faked fields stored
    in HA's .storage/.
    """

    device_id: str
    first_seen: str  # ISO timestamp
    last_seen: str  # ISO timestamp
    likely_type: str  # DevType value (e.g. "CTL", "TRV")
    codes_seen: list[str] = field(default_factory=list)  # sorted, deduplicated
    bound_to: str | None = None  # parent device ID (CTL for TRV, FAN for REM)
    zone_idx: str | None = None  # zone index if known from payload
    rssi: float | None = None  # running average
    confidence: str = "low"  # high, medium, low
    is_battery: bool = False  # seen sending battery info
    src_count: int = 0  # number of packets where this device was src
    dst_count: int = 0  # number of packets where this device was dst

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (for JSON export)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveredDevice:
        """Deserialize from a plain dict (for JSON import/resume)."""
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Scan engine
# ---------------------------------------------------------------------------


class DiscoveryScan:
    """Passive device scanner. Read-only, no topology mutation.

    Register as a msg_handler on the gateway. Every packet is examined:
    - src, dst, addr3 device IDs are extracted
    - Unknown devices are classified and added to the in-memory dict
    - Known devices are enriched with new codes/binding info

    The scan never calls ``get_device()`` or emits topology events.
    """

    def __init__(self, gwy: Gateway) -> None:
        self._gwy = gwy
        self._devices: dict[str, DiscoveredDevice] = {}
        self._dirty: bool = False
        self._remove_handler: Callable[[], None] | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Register as a raw packet handler on the gateway.

        Uses ``add_raw_pkt_handler`` (not ``add_msg_handler``) so the scan
        sees packets from unknown devices even when ``enforce_known_list=True``.
        The raw handler fires before the device ID filter.
        """
        if self._remove_handler is not None:
            _LOGGER.warning("DiscoveryScan.start(): already running")
            return
        self._remove_handler = self._gwy.add_raw_pkt_handler(self._on_packet)
        _LOGGER.info("DiscoveryScan: started (passive observer)")

    def stop(self) -> None:
        """Unregister from gateway."""
        if self._remove_handler:
            self._remove_handler()
            self._remove_handler = None
            _LOGGER.info("DiscoveryScan: stopped")

    @property
    def is_running(self) -> bool:
        """Whether the scan is currently listening to traffic."""
        return self._remove_handler is not None

    @property
    def is_dirty(self) -> bool:
        """Whether the in-memory state has changed since last export/import."""
        return self._dirty

    def clear_dirty(self) -> None:
        """Reset the dirty flag (call after successful persistence)."""
        self._dirty = False

    # -- known device check --------------------------------------------------

    def _is_known(self, dev_id: str) -> bool:
        """Check if a device is already known to the gateway.

        A device is "known" if it's the gateway itself, in the known_list,
        schema, or already in the device registry.
        """
        # The gateway's own HGI is never a "discovered" device.
        # Check the active HGI ID from the transport directly — the device
        # may not be in the device_registry yet when the first packets arrive.
        # TODO: when multiple HGI gateways are supported, this must check
        # against all gateway IDs, not just the single active one.
        engine = getattr(self._gwy, "_engine", None)
        transport = getattr(engine, "_transport", None) if engine else None
        if transport is not None:
            active_hgi = transport.get_extra_info(SZ_ACTIVE_HGI)
            if active_hgi == dev_id:
                return True
        # Also check via the hgi property (covers the case where the device
        # is in the registry but the transport extra_info is not set)
        if self._gwy.hgi and self._gwy.hgi.id == dev_id:
            return True

        # Check device registry (already created devices)
        if dev_id in self._gwy.device_registry.device_by_id:
            return True

        # Check known_list
        if dev_id in self._gwy._gwy_config.known_list:
            return True

        # Check schema keys (CTL IDs are top-level keys)
        return dev_id in self._gwy._gwy_config.schema

    # -- packet handler ------------------------------------------------------

    async def _on_packet(self, dto: PacketDTO) -> None:
        """Async wrapper for the gateway msg_handler interface.

        Delegates to the sync ``_process_packet`` so tests can call it
        directly without an event loop.
        """
        self._process_packet(dto)

    def _process_packet(self, dto: PacketDTO) -> None:
        """Classify packet, update in-memory dict. No disk I/O.

        Called for every valid packet from the gateway. Must be fast —
        just dict lookups and updates.
        """
        # Extract device IDs from the packet
        src = dto.addr1.strip()
        dst = dto.addr2.strip()
        addr3 = dto.addr3.strip() if dto.addr3 else ""
        code = str(dto.code).strip()
        verb = dto.verb if dto.verb else ""

        # Parse RSSI (stored as string in PacketDTO)
        rssi: float | None = None
        if dto.rssi:
            with contextlib.suppress(ValueError, TypeError):
                rssi = float(dto.rssi)

        # Extract zone_idx from payload if this is a binding code
        zone_idx = (
            _extract_zone_idx(dto.payload) if code in _ZONE_BINDING_CODES else None
        )

        # Process each address in the packet
        # src: high-confidence (device is actively sending)
        if src and _is_valid_address(src):
            self._process_device(
                src,
                code=code,
                verb=verb,
                rssi=rssi,
                zone_idx=zone_idx,
                is_src=True,
                dst=dst,
            )

        # dst: lower-confidence (device is being talked to)
        if dst and _is_valid_address(dst):
            self._process_device(
                dst,
                code=code,
                verb=verb,
                rssi=None,  # RSSI is for the sender, not the receiver
                zone_idx=zone_idx,
                is_src=False,
                dst=None,
            )

        # addr3: lowest-confidence (broadcast target or relay)
        if addr3 and _is_valid_address(addr3) and addr3 not in (src, dst):
            self._process_device(
                addr3,
                code=code,
                verb=verb,
                rssi=None,
                zone_idx=None,
                is_src=False,
                dst=None,
            )

    def _process_device(
        self,
        dev_id: str,
        *,
        code: str,
        verb: str,
        rssi: float | None,
        zone_idx: str | None,
        is_src: bool,
        dst: str | None,
    ) -> None:
        """Update or create a discovery entry for a single device."""
        # Skip if already known to the gateway
        if self._is_known(dev_id):
            return

        now = dt.now().isoformat(timespec="seconds")
        dev = self._devices.get(dev_id)

        if dev is None:
            # New device — classify and create entry
            likely_type = _classify(dev_id, code, verb, is_src=is_src)
            dev = DiscoveredDevice(
                device_id=dev_id,
                first_seen=now,
                last_seen=now,
                likely_type=likely_type,
                codes_seen=[code] if code else [],
                rssi=rssi,
                confidence=_initial_confidence(is_src, code, verb),
                is_battery=code in _BATTERY_CODES,
                src_count=1 if is_src else 0,
                dst_count=0 if is_src else 1,
            )
            # Set binding info if available
            if zone_idx and dst and _is_valid_address(dst):
                dev.zone_idx = zone_idx
                dev.bound_to = dst
                dev.confidence = "high"  # binding telemetry = high confidence
            self._devices[dev_id] = dev
            self._dirty = True
            _LOGGER.info(
                "DiscoveryScan: new device %s (%s, %s)",
                dev_id,
                likely_type,
                dev.confidence,
            )
            return

        # Existing device — enrich
        changed = False

        dev.last_seen = now
        if is_src:
            dev.src_count += 1
        else:
            dev.dst_count += 1

        # Add code to codes_seen (deduplicated, keep sorted)
        if code and code not in dev.codes_seen:
            dev.codes_seen.append(code)
            dev.codes_seen.sort()
            changed = True

        # Update RSSI as running average (only from src packets)
        if rssi is not None and is_src:
            if dev.rssi is None:
                dev.rssi = rssi
            else:
                dev.rssi = (dev.rssi + rssi) / 2
            changed = True

        # Update battery flag
        if code in _BATTERY_CODES and not dev.is_battery:
            dev.is_battery = True
            changed = True

        # Update zone binding (prefer src packets with zone_idx)
        if zone_idx and is_src and dst and _is_valid_address(dst):
            if dev.zone_idx != zone_idx or dev.bound_to != dst:
                dev.zone_idx = zone_idx
                dev.bound_to = dst
                dev.confidence = "high"
                changed = True

        # Upgrade confidence based on accumulated evidence
        new_conf = _recompute_confidence(dev)
        if new_conf != dev.confidence:
            dev.confidence = new_conf
            changed = True

        # Re-classify if we have more info now
        new_type = _classify(dev_id, code, verb, is_src=is_src, dev=dev)
        if new_type != dev.likely_type and new_type != DevType.DEV:
            dev.likely_type = new_type
            changed = True

        if changed:
            self._dirty = True

    # -- public API ----------------------------------------------------------

    def get_devices(
        self,
        *,
        status: str | None = None,
        likely_type: str | None = None,
        min_confidence: str | None = None,
    ) -> list[DiscoveredDevice]:
        """Return discovered devices, optionally filtered.

        :param status: Not used by the engine (ramses_cc concern). Accepted
            for API compatibility — filtering by status is done by the consumer.
        :param likely_type: Filter by DevType value (e.g. "TRV").
        :param min_confidence: Only return devices with at least this confidence
            level ("low" < "medium" < "high").
        """
        result = list(self._devices.values())

        if likely_type:
            result = [d for d in result if d.likely_type == likely_type]

        if min_confidence:
            order = {"low": 0, "medium": 1, "high": 2}
            min_val = order.get(min_confidence, 0)
            result = [d for d in result if order.get(d.confidence, 0) >= min_val]

        return result

    def get_device(self, dev_id: str) -> DiscoveredDevice | None:
        """Return a single discovered device by ID, or None."""
        return self._devices.get(dev_id)

    def remove_device(self, dev_id: str) -> bool:
        """Remove a device from the in-memory list.

        Returns True if the device was present and removed.
        """
        if dev_id in self._devices:
            del self._devices[dev_id]
            self._dirty = True
            return True
        return False

    def export_json(self) -> str:
        """Export the full device list as JSON (for CLI, persistence).

        Returns a JSON string with a ``version`` key and a ``devices`` list.
        """
        data = {
            "version": 1,
            "exported_at": dt.now().isoformat(timespec="seconds"),
            "devices": [
                d.to_dict()
                for d in sorted(self._devices.values(), key=lambda d: d.device_id)
            ],
        }
        return json.dumps(data, indent=2, sort_keys=False)

    def import_json(self, data: str) -> None:
        """Load a previously exported list (for resume after restart).

        Replaces the current in-memory dict.
        """
        parsed = json.loads(data)
        self._devices = {
            d["device_id"]: DiscoveredDevice.from_dict(d)
            for d in parsed.get("devices", [])
        }
        self._dirty = False
        _LOGGER.info("DiscoveryScan: imported %d devices", len(self._devices))

    def device_count(self) -> int:
        """Return the number of discovered devices."""
        return len(self._devices)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _is_valid_address(dev_id: str) -> bool:
    """Quick check if a device ID looks valid (N.N:NNNNNN or N:NNNNNN).

    Filters out broadcast addresses (18:73030, 18:14803), placeholder
    addresses (--:------), and corrupt IDs.
    """
    if not dev_id or len(dev_id) < 8:
        return False
    # Skip broadcast/multicast addresses
    if dev_id in ("18:73030", "18:14803", "18:000730"):
        return False
    # Skip placeholder/empty addresses (e.g. "--:------")
    if dev_id.startswith("-") or dev_id.startswith("00:------"):
        return False
    # Basic format check: should contain a colon
    return ":" in dev_id


def _classify(
    dev_id: str,
    code: str,
    verb: str,
    *,
    is_src: bool,
    dev: DiscoveredDevice | None = None,
) -> DevType:
    """Classify a device based on prefix, verb/code, and accumulated evidence.

    Priority:
    1. HVAC prefix (32:=FAN, 37:=REM) — unambiguous, takes precedence over
       verb/code pairs (a FAN sends 22F1, but that doesn't make it a REM)
    2. CTL-only codes — if device sends these, it's a CTL
    3. Verb+code pair (HVAC) — for non-HVAC prefixes that send HVAC codes
    4. CH prefix — fallback for heating domain devices
    5. Accumulated codes — re-evaluate with full evidence
    """
    prefix = dev_id[:2]

    # 1. Unambiguous HVAC prefixes (32:=FAN) — check first
    if prefix in _UNAMBIGUOUS_HVAC_PREFIXES:
        return _PREFIX_TO_TYPE[prefix]

    # 2. CTL-only codes (only if this device is the sender)
    if is_src and code in _CTL_ONLY_CODES:
        return DevType.CTL

    # 3. Check verb+code pair (HVAC domain, for non-HVAC prefixes)
    vc_key = (verb, code)
    if vc_key in _VC_TO_TYPE:
        return _VC_TO_TYPE[vc_key]

    # 4. Check accumulated codes if we have a dev
    if dev and is_src:
        for c in dev.codes_seen:
            if c in _CTL_ONLY_CODES:
                return DevType.CTL
        # Check HVAC codes from accumulated data
        for c in dev.codes_seen:
            for v in (" I", "RP", "RQ", " W"):
                if (v, c) in _VC_TO_TYPE:
                    return _VC_TO_TYPE[(v, c)]

    # 5. CH prefix fallback
    if prefix in _PREFIX_TO_TYPE:
        return _PREFIX_TO_TYPE[prefix]

    return DevType.DEV


def _initial_confidence(is_src: bool, code: str, verb: str) -> str:
    """Determine initial confidence for a newly seen device."""
    if is_src and code in _ZONE_BINDING_CODES:
        return "high"  # binding telemetry from src = high confidence
    if is_src:
        return "medium"  # device is actively sending
    return "low"  # only seen as dst/addr3


def _recompute_confidence(dev: DiscoveredDevice) -> str:
    """Recompute confidence based on accumulated evidence."""
    # High: has binding info (zone_idx + bound_to)
    if dev.zone_idx and dev.bound_to:
        return "high"

    # High: sends CTL-only codes
    if any(c in _CTL_ONLY_CODES for c in dev.codes_seen):
        return "high"

    # Medium: seen as src multiple times
    if dev.src_count >= 2:
        return "medium"

    # Medium: seen as src at least once with known codes
    if dev.src_count >= 1 and len(dev.codes_seen) >= 2:
        return "medium"

    # Low: only seen as dst, or seen once as src
    return "low"


def _extract_zone_idx(payload: str) -> str | None:
    """Extract zone_idx from a payload string.

    Zone index is typically the first 2 hex chars of the payload.
    Returns None if payload is empty or too short.
    """
    if not payload or len(payload) < 2:
        return None
    idx = payload[:2]
    # Validate: should be hex chars
    try:
        int(idx, 16)
    except ValueError:
        return None
    return idx

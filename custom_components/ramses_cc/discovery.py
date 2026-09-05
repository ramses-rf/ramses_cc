"""RAMSES CC - Passive device scan integration.

Wraps the ramses_rf DiscoveryScan engine with HA-specific concerns:
- status/enabled/owner/faked metadata (stored in HA .storage/)
- persistent notifications for new/lost devices
- service calls for accept/discard/remove/enable/disable
- schema auto-generation from accepted devices
- periodic checkpoint to .storage/

The scan engine itself (ramses_rf.discovery_scan) is read-only and
HA-agnostic. This module adds the user-facing layer.

HGI gateway handling (issue 1119):
  HGI gateways (18: devices) are RF receivers, not remote devices.
  The scan engine sees their traffic but they don't have zone bindings,
  class mismatches, weak signals, or orphaned/lost status like real
  devices do.  Most check methods skip them early via ``_is_hgi()``.
  The exception is MQTT discovery: a second HGI on the same broker
  appears in the schema without ``_owner`` and must be flagged as a
  discovery candidate for the user to review — this is handled by the
  HGI loop in ``check_for_new_devices()`` and the ``_schema_no_owner_ids``
  set populated by ``sync_with_schema()``.

  TODO: Consider moving HGI-specific knowledge (prefix, class, stub
  creation) into a small HgiGateway helper class or into ramses_rf's
  device model, so discovery.py doesn't need prefix checks at all.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime as dt, timedelta as td
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
    async_dismiss as async_dismiss_notification,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SZ_DEVICE_COMMENTS,
    SZ_TR_BOUND,
    SZ_TR_CLASS,
    SZ_TR_COMMENT,
    SZ_TR_FAKED,
    SZ_TR_NAME,
    SZ_TR_OWNER,
)

if TYPE_CHECKING:
    from ramses_rf.discovery_scan import DiscoveredDevice, DiscoveryScan

_LOGGER = logging.getLogger(__name__)

# Storage keys
SZ_DISCOVERY: Final = "discovery"
SZ_DISCOVERY_DEVICES: Final = "devices"
SZ_DISCOVERY_SCAN_STATE: Final = "scan_state"

# Defaults
CHECKPOINT_INTERVAL_MINUTES: Final[int] = 30
LOST_DEVICE_THRESHOLD_DAYS: Final[int] = 7


class DiscoveryStatus(StrEnum):
    """Discovery state of a device."""

    NEW = "new"
    ACCEPTED = "accepted"
    DISCARDED = "discarded"
    REMOVED = "removed"
    LOST = "lost"


@dataclass(slots=True)
class DeviceMetadata:
    """ramses_cc-specific metadata for a discovered device.

    Stored in HA .storage/, separate from the ramses_rf scan engine's
    in-memory DiscoveredDevice dataclass.
    """

    status: DiscoveryStatus = DiscoveryStatus.NEW
    enabled: bool = False
    faked: bool = False
    owner: str | None = None
    accepted_at: str | None = None
    schema_entry: dict[str, Any] | None = None
    # Set when the scan engine's likely_type differs from the schema's
    # _class.  Cleared when the mismatch is resolved (user updates _class
    # or the scan engine re-classifies to match).
    class_mismatch: str | None = None
    # Set when the user explicitly chose "Keep" in the review_discovered
    # step, dismissing the mismatch.  Prevents check_class_mismatches
    # from re-flagging the same device every checkpoint cycle.
    class_mismatch_dismissed: bool = False
    # Set when the scan engine's bound_to differs from the schema's
    # _bound.  Cleared when the mismatch is resolved.
    bound_mismatch: str | None = None
    # Set when the scan engine has a likely_type but the schema entry
    # has no _class at all.  Cleared when the user adds a _class.
    missing_class: str | None = None
    # Set when the user explicitly chose "Skip" in the review_discovered
    # step, dismissing the missing_class suggestion.  Prevents
    # check_missing_class from re-flagging the same device every cycle.
    missing_class_dismissed: bool = False
    # Set when a device is in the schema but has not been seen by the
    # scan engine for longer than the orphan threshold.  Cleared when
    # traffic is seen again.
    orphaned: str | None = None
    # ISO timestamp of the last INFO log for a suppressed-orphaned device.
    # Used when the schema entry has ``_suppress_not_seen: True``: the
    # persistent notification is suppressed, but an INFO log is emitted
    # once every ``orphan_threshold_days`` as a gentle reminder (e.g. in
    # case batteries died).  Cleared when traffic is seen again.
    last_orphaned_log: str | None = None
    # Set when a zone's schema _name differs from the runtime name
    # reported by the controller via 0004 packets.  The controller's
    # name is authoritative for _name — the user can set _alias for a
    # custom display name.  No dismiss option: the schema _name should
    # be updated to match the controller (issue 947).
    name_mismatch: str | None = None
    # Set when a device's communication quality is poor (weak RSSI or
    # stale).  Cleared when quality recovers.  See check_communication_quality.
    weak_signal: str | None = None
    # Set when the user explicitly chose "Keep (dismiss)" in the
    # review_device_health step for a weak-signal device.  Prevents
    # check_communication_quality from re-flagging the same device.
    weak_signal_dismissed: bool = False
    # ISO timestamp of the last WARNING log for a weak-signal device.
    # Used to throttle logs to once per hour per device so the HA log
    # is not spammed.  Cleared when quality recovers.
    last_weak_signal_log: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return {
            "status": self.status.value,
            "enabled": self.enabled,
            "faked": self.faked,
            "owner": self.owner,
            "accepted_at": self.accepted_at,
            "schema_entry": self.schema_entry,
            "class_mismatch": self.class_mismatch,
            "class_mismatch_dismissed": self.class_mismatch_dismissed,
            "bound_mismatch": self.bound_mismatch,
            "missing_class": self.missing_class,
            "missing_class_dismissed": self.missing_class_dismissed,
            "orphaned": self.orphaned,
            "last_orphaned_log": self.last_orphaned_log,
            "name_mismatch": self.name_mismatch,
            "weak_signal": self.weak_signal,
            "weak_signal_dismissed": self.weak_signal_dismissed,
            "last_weak_signal_log": self.last_weak_signal_log,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceMetadata:
        """Deserialize from dict (loaded from JSON storage)."""
        try:
            status = DiscoveryStatus(data.get("status", "new"))
        except ValueError:
            status = DiscoveryStatus.NEW
        return cls(
            status=status,
            enabled=data.get("enabled", False),
            faked=data.get("faked", False),
            owner=data.get("owner"),
            accepted_at=data.get("accepted_at"),
            schema_entry=data.get("schema_entry"),
            class_mismatch=data.get("class_mismatch"),
            class_mismatch_dismissed=data.get(
                "class_mismatch_dismissed", False
            ),
            bound_mismatch=data.get("bound_mismatch"),
            missing_class=data.get("missing_class"),
            missing_class_dismissed=data.get("missing_class_dismissed", False),
            orphaned=data.get("orphaned"),
            last_orphaned_log=data.get("last_orphaned_log"),
            name_mismatch=data.get("name_mismatch"),
            weak_signal=data.get("weak_signal"),
            weak_signal_dismissed=data.get("weak_signal_dismissed", False),
            last_weak_signal_log=data.get("last_weak_signal_log"),
        )


@dataclass(slots=True)
class DiscoveredDeviceEntry:
    """Full discovery entry: engine data + ramses_cc metadata."""

    device: DiscoveredDevice
    metadata: DeviceMetadata = field(default_factory=DeviceMetadata)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for service responses and storage."""
        return {
            **asdict(self.device),
            **self.metadata.to_dict(),
        }


@dataclass(slots=True)
class _SchemaOnlyDevice:
    """Lightweight stand-in for a scan-engine ``DiscoveredDevice``.

    Used by ``refresh_device_comments`` when a device has a schema-declared
    ``_bound`` trait (HVAC parent) but is not yet tracked by the scan engine.
    Provides the attributes that ``_build_comment`` reads via ``getattr``.
    """

    device_id: str
    likely_type: str
    bound_to: str | None = None
    zone_index: str | None = None
    domain_id: str | None = None
    is_authoritative_domain: bool = False
    codes_seen: list[str] = field(default_factory=list)
    is_battery: bool = False
    rssi: float | None = None
    confidence: str | None = None


class DiscoveryManager:
    """Manages the passive device scan for ramses_cc.

    Wraps the ramses_rf DiscoveryScan engine with:
    - Metadata tracking (status, enabled, owner, faked)
    - Persistence to HA .storage/
    - Persistent notifications
    - Schema auto-generation from accepted devices
    """

    def __init__(
        self,
        hass: HomeAssistant,
        scan: DiscoveryScan,
        *,
        auto_notify: bool = True,
        lost_threshold_days: int = LOST_DEVICE_THRESHOLD_DAYS,
        active_hgi_id: str | None = None,
    ) -> None:
        """Initialize the discovery manager.

        :param hass: Home Assistant instance.
        :param scan: The ramses_rf DiscoveryScan engine instance.
        :param auto_notify: Whether to send persistent notifications for
            new devices.
        :param lost_threshold_days: Days without traffic before marking
            a device lost.
        :param active_hgi_id: Optional device ID of the active local HGI.
        """
        self._hass = hass
        self._scan = scan
        self._auto_notify = auto_notify
        self._lost_threshold_days = lost_threshold_days
        self._active_hgi_id = active_hgi_id

        # device_id → metadata (persisted to .storage/)
        self._metadata: dict[str, DeviceMetadata] = {}

        # Track notified device IDs to avoid duplicate notifications
        self._notified: set[str] = set()

        # Last-known schema device IDs (stashed by sync_with_schema so
        # check_for_new_devices can suppress notifications for devices
        # that are already in the schema but lost their metadata, e.g.
        # after a reload where .storage/ wasn't updated before teardown
        # (issue 917).
        self._schema_device_ids: set[str] = set()
        self._foreign_device_ids: set[str] = set()
        # Devices in schema without _owner — need review (issue 1119).
        self._schema_no_owner_ids: set[str] = set()

        # Track which mismatches we've already warned about (to avoid
        # repeating the WARNING every checkpoint cycle).  Cleared when
        # a mismatch is resolved or changes.
        self._warned_mismatches: set[str] = set()
        # Separate set for name mismatches (zone IDs, not device IDs).
        self._warned_name_mismatches: set[str] = set()

        # Notification ID for the "new devices" notification
        self._notification_id = f"{DOMAIN}_discovery"
        # Notification ID for the "schema mismatches" notification
        self._mismatch_notification_id = f"{DOMAIN}_discovery_mismatches"

        self._scan.start()
        _LOGGER.info("DiscoveryManager: started (passive scan running)")

    @property
    def active_hgi_id(self) -> str | None:
        """Return the active local HGI gateway device ID."""
        return self._active_hgi_id

    @active_hgi_id.setter
    def active_hgi_id(self, value: str | None) -> None:
        """Set the active local HGI gateway device ID."""
        self._active_hgi_id = value

    @property
    def scan(self) -> DiscoveryScan:
        """Return the underlying scan engine."""
        return self._scan

    # ── HGI helpers ──────────────────────────────────────────────
    # HGI gateways (18:) are RF receivers, not remote devices.  Most
    # check methods skip them early.  See the module docstring for the
    # full rationale and the issue 1119 exception for MQTT discovery.

    @staticmethod
    def _is_hgi(device_id: str) -> bool:
        """Return True if ``device_id`` is an HGI gateway (18: prefix)."""
        return device_id.startswith("18:")

    @staticmethod
    def _hgi_likely_type(device_id: str, likely_type: str | None) -> str:
        """Return the authoritative likely_type for an HGI.

        HGIs discovered via MQTT are not in the scan engine, so
        ``likely_type`` may be ``"unknown"`` or ``None``.  The 18:
        prefix is authoritative — always use ``"HGI"``.
        """
        if device_id.startswith("18:") and (
            not likely_type or likely_type.lower() == "unknown"
        ):
            return "HGI"
        return likely_type or "unknown"

    def get_scan_codes(self) -> dict[str, list[str]]:
        """Return a mapping of device_id → codes_seen from the scan engine.

        Used by sync_learned_topology to infer DHW valves (13: devices
        that send 1100 are boiler relays, not zone actuators).
        """
        result: dict[str, list[str]] = {}
        for dev_id, dev in self._scan._devices.items():
            if dev.codes_seen:
                result[dev_id] = list(dev.codes_seen)
        return result

    def get_scan_domain_ids(self) -> dict[str, tuple[str | None, bool]]:
        """Return a mapping of device_id → (domain_id, is_authoritative).

        The domain_id (FC/FA/F9) is the authoritative tag from the
        scan engine: ``FC`` = appliance_control, ``FA`` = hotwater_valve,
        ``F9`` = heating_valve.  ``is_authoritative`` is True when the
        domain_id was set from a ``000C`` binding table entry (confident),
        False when set from a ``3B00``/``3EF0`` fallback hint (hedged).

        Used by sync_learned_topology to place BDR relays based on
        authoritative domain evidence rather than the ambiguous ``1100``
        code heuristic.  Issue 931.

        :return: A dict mapping device_id to a ``(domain_id,
            is_authoritative)`` tuple.  Devices with no domain_id are
            included with ``(None, False)``.
        """
        result: dict[str, tuple[str | None, bool]] = {}
        for dev_id, dev in self._scan._devices.items():
            result[dev_id] = (dev.domain_id, dev.is_authoritative_domain)
        return result

    def refresh_device_comments(
        self,
        existing_comments: dict[str, str],
        config_schema: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Update device comments with the latest scan engine data.

        For each device in the scan engine that has zone_index or bound_to,
        update the corresponding comment in *existing_comments* to include
        the binding info.  Devices not in the scan engine are left unchanged.

        :param existing_comments: The current device_comments dict from the
            config schema.
        :param config_schema: The full config schema (used to check if a
            device is already placed as hotwater_valve, which overrides the
            scan engine's FC domain hint).  Issue 834.
        :return: A new dict with updated comments (or the original dict if
            no changes were made).
        """
        engine_devices = {d.device_id: d for d in self._scan.get_devices()}
        changed = False
        result = dict(existing_comments)

        # Build a set of device IDs that are placed as hotwater_valve in
        # the schema.  The scan engine's domain_id=FC hint (from 3B00/3EF0)
        # is ambiguous — a hotwater_valve relay also broadcasts these codes.
        # If the device is already placed as hotwater_valve in the schema,
        # we suppress the "domain FC" comment to avoid misleading the user.
        # See issue 834 comment 5044906835.
        hotwater_valves: set[str] = set()
        if config_schema:
            for ctl_entry in config_schema.values():
                if not isinstance(ctl_entry, dict):
                    continue
                dhw = ctl_entry.get("stored_hotwater", {})
                if isinstance(dhw, dict):
                    valve = dhw.get("hotwater_valve")
                    if isinstance(valve, str):
                        hotwater_valves.add(valve)

        for dev_id, dev in engine_devices.items():
            schema_role = (
                "hotwater_valve" if dev_id in hotwater_valves else None
            )
            # HGI gateways (18:) are tracked but don't have zone bindings.
            # Still create comments for them (without zone/bound info).
            if self._is_hgi(dev_id):
                if (
                    dev_id in result
                    and result[dev_id]
                    and self._COMMENT_SUFFIX in result[dev_id]
                ):
                    continue  # already has a comment with suffix
                likely_type = dev.likely_type or "HGI"
                new_comment = self._build_comment(
                    dev, likely_type, None, None, schema_role=schema_role
                )
                if new_comment != result.get(dev_id, ""):
                    result[dev_id] = new_comment
                    changed = True
                continue
            # For non-HGI devices, always ensure a comment exists.
            # Previously only updated comments for devices with zone_index
            # or bound_to, leaving new devices without comments.
            comment = result.get(dev_id, "")
            if not dev.zone_index and not dev.bound_to:
                # No binding info — still create a basic comment if missing
                # or if it lacks the auto-generated suffix.
                # Exceptions that force a rebuild:
                # - schema_role is set (e.g. hotwater_valve): suppress the
                #   scan engine's FC domain hint.  Issue 834.
                # - is_authoritative_domain is True: a 000C binding has
                #   been captured since the comment was last built — the
                #   comment must be rebuilt to show the confident domain
                #   classification instead of the hedged hint.  Issue 931.
                if (
                    comment
                    and self._COMMENT_SUFFIX in comment
                    and schema_role is None
                    and not dev.is_authoritative_domain
                ):
                    continue  # existing comment, no new info to add
                likely_type = dev.likely_type or "unknown"
                new_comment = self._build_comment(
                    dev, likely_type, None, None, schema_role=schema_role
                )
                if new_comment != comment:
                    result[dev_id] = new_comment
                    changed = True
                continue
            # Check if comment already has the correct zone/bound info
            has_zone = dev.zone_index and f"zone {dev.zone_index}" in comment
            # "belongs to" for FAN (32:), "bound to" for heat TCS
            bound_phrase = (
                f"belongs to {dev.bound_to}"
                if dev.bound_to and dev.bound_to.startswith("32:")
                else f"bound to {dev.bound_to}"
            )
            has_bound = dev.bound_to and bound_phrase in comment
            if has_zone and has_bound and self._COMMENT_SUFFIX in comment:
                continue
            # Rebuild the comment from the scan engine data
            likely_type = dev.likely_type or "unknown"
            new_comment = self._build_comment(
                dev,
                likely_type,
                dev.bound_to,
                dev.zone_index,
                schema_role=schema_role,
            )
            if new_comment != comment:
                result[dev_id] = new_comment
                changed = True

        # Fallback: backfill "belongs to" comments from the schema's _bound
        # trait for HVAC devices (37:, 29:) that are placed under a FAN
        # (32:) but whose scan engine bound_to hasn't been detected yet.
        # The _bound trait is the user-declared / profile-preloaded binding
        # for 2411 routing — it's the canonical HVAC parent declaration.
        # Without this fallback, the device is in remotes[]/sensors[] (via
        # step 1i in sync_learned_topology) but has no "belongs to" comment,
        # causing a comment/schema inconsistency.  See issue 767.
        if config_schema:
            for dev_id, dev_entry in config_schema.items():
                if not isinstance(dev_entry, dict):
                    continue
                # Only HVAC device prefixes can "belong to" a FAN
                if not dev_id.startswith(("37:", "29:")):
                    continue
                # Skip if already has a "belongs to" comment
                existing = result.get(dev_id, "")
                if "belongs to" in existing:
                    continue
                # Skip if the scan engine already has bound_to for this device
                engine_dev = engine_devices.get(dev_id)
                if engine_dev and engine_dev.bound_to:
                    continue
                # Check the device's own _bound trait (points to FAN)
                bound = dev_entry.get(SZ_TR_BOUND)
                if not isinstance(bound, str) or not bound.startswith("32:"):
                    continue
                # Build a comment with "belongs to" from the _bound trait
                engine_dev = engine_devices.get(dev_id)
                likely_type = (
                    engine_dev.likely_type
                    if engine_dev
                    else dev_entry.get(SZ_TR_CLASS, "unknown")
                )
                likely_type = likely_type or "unknown"
                zone_index = engine_dev.zone_index if engine_dev else None
                new_comment = self._build_comment(
                    engine_dev or _SchemaOnlyDevice(dev_id, likely_type),
                    likely_type,
                    bound,
                    zone_index,
                    schema_role=None,
                )
                if new_comment != existing:
                    result[dev_id] = new_comment
                    changed = True

        return result if changed else existing_comments

    def restore_state(self, data: dict[str, Any]) -> None:
        """Restore metadata and scan state from persisted data.

        Called on startup to resume after HA restart.

        :param data: The persisted discovery data from .storage/.
        """
        devices_data = data.get(SZ_DISCOVERY_DEVICES, {})
        for device_id, meta_dict in devices_data.items():
            self._metadata[device_id] = DeviceMetadata.from_dict(meta_dict)

        # Restore scan engine state (in-memory device list)
        scan_state = data.get(SZ_DISCOVERY_SCAN_STATE)
        if scan_state:
            self._scan.import_json(scan_state)

        _LOGGER.info(
            "DiscoveryManager: restored %d device metadata entries",
            len(self._metadata),
        )

    def sync_with_schema(
        self,
        schema_device_ids: set[str],
        foreign_device_ids: set[str] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> None:
        """Sync discovery metadata with the current schema.

        Compares the scan's device list (what the system actually sees)
        with the schema (what the user configured). Devices in the scan
        but not in the schema are marked as NEW for review.

        :param schema_device_ids: Set of device IDs currently in the schema.
        :param foreign_device_ids: Set of device IDs with a foreign _owner
            (neighbour's devices).  These are excluded from discovery —
            they appear in the scan engine (it sees all RF traffic) but
            should not be offered for review/acceptance.
        :param schema: The full config schema dict (with _ traits).
            Used to identify devices that are in the schema but have no
            ``_owner`` — these need review (issue 1119: HGI discovery
            candidates).
        """
        # Stash for check_for_new_devices (issue 917: prevents re-notifying
        # devices that are already in the schema but lost their metadata).
        self._schema_device_ids = schema_device_ids
        self._foreign_device_ids = foreign_device_ids or set()
        # Devices in the schema that have no _owner — these are discovery
        # candidates that need review (e.g. HGIs discovered via MQTT).
        # check_for_new_devices should NOT suppress them (issue 1119).
        self._schema_no_owner_ids = set()
        if schema and isinstance(schema, dict):
            import re

            device_id_re = re.compile(r"^[0-9]{2}:[0-9]{6}$")
            for dev_id, entry in schema.items():
                if (
                    isinstance(entry, dict)
                    and SZ_TR_OWNER not in entry
                    and device_id_re.match(str(dev_id))
                ):
                    self._schema_no_owner_ids.add(dev_id)

        _LOGGER.info(
            "DiscoveryManager: sync_with_schema with schema_device_ids=%s",
            schema_device_ids,
        )
        # Get all devices from the scan (what the system actually sees)
        scan_devices = {d.device_id: d for d in self._scan.get_devices()}
        _LOGGER.info(
            "DiscoveryManager: scan has %d devices: %s",
            len(scan_devices),
            list(scan_devices.keys()),
        )

        # Mark devices as REMOVED if in discovery but not in schema
        for device_id, meta in list(self._metadata.items()):
            # Skip local active HGI gateway
            if self._active_hgi_id and device_id == self._active_hgi_id:
                continue
            # Remove foreign-owner devices from discovery metadata —
            # they should not appear in the discovery UI at all.
            if device_id in self._foreign_device_ids:
                if meta.status != DiscoveryStatus.REMOVED:
                    meta.status = DiscoveryStatus.REMOVED
                    meta.enabled = False
                    self._metadata[device_id] = meta
                    self._notified.discard(device_id)
                    _LOGGER.info(
                        "DiscoveryManager: foreign-owner device %s "
                        "removed from discovery",
                        device_id,
                    )
                continue
            if device_id not in schema_device_ids and meta.status in (
                DiscoveryStatus.ACCEPTED,
                DiscoveryStatus.NEW,
            ):
                meta.status = DiscoveryStatus.REMOVED
                meta.enabled = False
                self._metadata[device_id] = meta
                self._notified.discard(device_id)
                _LOGGER.info(
                    "DiscoveryManager: device %s not in schema, marked "
                    "as REMOVED",
                    device_id,
                )
            elif (
                device_id in schema_device_ids
                and meta.status == DiscoveryStatus.NEW
            ):
                # Device is in the schema (e.g. added manually via the schema
                # editor) but discovery status is still NEW.  Mark it as
                # ACCEPTED so it doesn't appear in the "new devices" section
                # of the review form — it's already in the schema.  If there's
                # a class mismatch, the review form will show it in the
                # mismatch section where the user can resolve it.
                meta.status = DiscoveryStatus.ACCEPTED
                meta.enabled = True
                self._metadata[device_id] = meta
                _LOGGER.info(
                    "DiscoveryManager: device %s is in schema but had NEW "
                    "status, marked as ACCEPTED",
                    device_id,
                )

        # Second, add devices from the scan that aren't in discovery metadata
        # (e.g., devices seen by the system but not yet in discovery).
        # Skip devices that are already in the schema — they don't need
        # discovery metadata (they're already configured).  Creating NEW
        # metadata for them would cause check_for_new_devices to re-notify
        # them after every reload where metadata was lost (issue 917).
        for device_id in scan_devices:
            # Skip local active HGI gateway
            if self._active_hgi_id and device_id == self._active_hgi_id:
                continue
            # Skip foreign-owner devices (neighbour's devices) — the
            # scan engine sees all RF traffic, but foreign devices
            # should not be offered for discovery/review.
            if device_id in self._foreign_device_ids:
                continue
            if (
                device_id not in self._metadata
                and device_id not in schema_device_ids
            ):
                self._metadata[device_id] = DeviceMetadata()
                _LOGGER.info(
                    "DiscoveryManager: device %s added to discovery metadata "
                    "(from scan)",
                    device_id,
                )

    def check_class_mismatches(self, schema: dict[str, Any]) -> int:
        """Check for class mismatches between scan engine and schema.

        For each device that is in both the scan engine and the schema,
        compares the scan's ``likely_type`` with the schema's ``_class``.
        If they differ, logs a WARNING and sets ``class_mismatch`` on the
        device's metadata so the discovery UI can flag it.

        **HVAC devices are skipped** — the scan engine's ``likely_type``
        for HVAC prefixes (29:, 32:, 37:, 63:) is unreliable because
        these prefixes are ambiguous (REM, CO2, HUM, DIS, FAN all share
        them) and classification depends on which packet was seen first.
        HVAC class mismatches are detected by ``_check_rf_contradictions``
        instead, which reads from ramses_rf's known_list (updated by the
        HvacTopologyHandler with proper threshold-based contradiction
        detection).

        The schema is authoritative — this method does NOT modify the
        schema.  It only warns the user that discovery suggests a
        different class.

        :param schema: The current config entry schema (with _ traits).
        :return: Number of mismatches found.
        """
        from .coordinator import _normalize_class_slug

        scan_devices = {d.device_id: d for d in self._scan.get_devices()}
        mismatches: list[tuple[str, str, str]] = []

        # HVAC device prefixes — likely_type is unreliable for these
        # because 37:/29: are ambiguous (REM, CO2, HUM, DIS, FAN all
        # share these prefixes) and the scan engine's classification
        # depends on which packet was seen first.  HVAC class mismatches
        # are detected by _check_rf_contradictions instead.
        _hvac_prefixes = ("29:", "32:", "37:", "63:")
        _hvac_class_slugs = {"FAN", "REM", "CO2", "HUM", "DIS", "VCS"}

        for device_id, dev in scan_devices.items():
            # Skip HGI gateways — they're not classified by the scan engine
            if self._is_hgi(device_id):
                continue

            # Get the schema's _class for this device
            schema_entry = schema.get(device_id)
            if not isinstance(schema_entry, dict):
                continue  # no root entry — nothing to compare
            schema_class = schema_entry.get(SZ_TR_CLASS)
            if not isinstance(schema_class, str) or not schema_class:
                continue  # no _class in schema — nothing to compare

            # Normalize schema class to DevType slug for comparison
            # (e.g. 'ventilator' -> 'FAN')
            schema_class_norm = _normalize_class_slug(schema_class)

            # Skip if the user already dismissed this mismatch ("Keep")
            existing_meta = self._metadata.get(device_id)
            if existing_meta and existing_meta.class_mismatch_dismissed:
                continue  # user decided — don't re-flag

            # Skip _locked devices — user has pinned the class
            if schema_entry.get("_locked") is True:
                continue

            # Skip HVAC devices with low/medium confidence — the scan
            # engine's likely_type for HVAC prefixes (29:, 32:, 37:, 63:)
            # is unreliable when based on a prefix fallback (e.g. 37: →
            # REM is a guess, not evidence).  HVAC devices with "high"
            # confidence are NOT skipped — either a VC pair matched
            # (specific evidence) or the scan engine re-classified after
            # 3+ contradictions (threshold-based, reliable).
            # HVAC class mismatches are also detected by
            # _check_rf_contradictions (reads from ramses_rf's known_list).
            is_hvac = (
                device_id[:3] in _hvac_prefixes
                or schema_class_norm.upper() in _hvac_class_slugs
            )
            if is_hvac and dev.confidence != "high":
                continue

            # Get the scan engine's likely_type
            scan_type = str(dev.likely_type) if dev.likely_type else ""
            if not scan_type or scan_type == "DEV":
                continue  # unknown/generic — not a meaningful mismatch

            # Compare (both should be DevType slugs like 'FAN', 'REM', etc.)
            if scan_type.upper() != schema_class_norm.upper():
                meta = self._metadata.get(device_id, DeviceMetadata())
                mismatch_desc = (
                    f"schema={schema_class_norm}, discovery={scan_type}"
                )
                meta.class_mismatch = mismatch_desc
                self._metadata[device_id] = meta
                mismatches.append((device_id, schema_class_norm, scan_type))
                _LOGGER.debug(
                    "DiscoveryManager: class mismatch for %s — "
                    "schema has _class=%s but discovery suggests %s. "
                    "Schema is authoritative; update _class in the schema "
                    "if the discovery classification is correct.",
                    device_id,
                    schema_class_norm,
                    scan_type,
                )
            else:
                # Mismatch resolved — clear the flag
                existing_meta = self._metadata.get(device_id)
                if existing_meta and existing_meta.class_mismatch:
                    existing_meta.class_mismatch = None
                    self._metadata[device_id] = existing_meta

        if mismatches:
            # Only WARN once per device — subsequent checks log at DEBUG.
            # This avoids log spam every 5 min for persistent mismatches.
            new_mismatches = [
                (d, s, t)
                for d, s, t in mismatches
                if d not in self._warned_mismatches
            ]
            if new_mismatches:
                _LOGGER.warning(
                    "DiscoveryManager: %d device(s) have class mismatches "
                    "between discovery and schema: %s",
                    len(new_mismatches),
                    ", ".join(f"{d} ({s}→{t})" for d, s, t in new_mismatches),
                )
                self._warned_mismatches.update(d for d, _, _ in new_mismatches)
            else:
                _LOGGER.debug(
                    "DiscoveryManager: %d persistent class mismatch(s) "
                    "(already warned): %s",
                    len(mismatches),
                    ", ".join(d for d, _, _ in mismatches),
                )
        else:
            # All mismatches resolved — clear the warned set
            if self._warned_mismatches:
                _LOGGER.info("DiscoveryManager: all class mismatches resolved")
                self._warned_mismatches.clear()

        return len(mismatches)

    def flag_class_mismatch(self, device_id: str, description: str) -> None:
        """Flag a device as having a class mismatch (external source).

        Used by the coordinator when ramses_rf's contradiction detection
        suggests a different class than the schema's _class.  This sets
        the ``class_mismatch`` metadata so the existing persistent
        notification and review_discovered flow can surface it to the
        user.

        :param device_id: The device ID with the mismatch.
        :param description: Human-readable description of the mismatch.
        """
        meta = self._metadata.get(device_id, DeviceMetadata())
        if meta.class_mismatch_dismissed:
            return  # user already dismissed this
        if meta.class_mismatch != description:
            meta.class_mismatch = description
            self._metadata[device_id] = meta
            if device_id not in self._warned_mismatches:
                _LOGGER.warning(
                    "DiscoveryManager: class mismatch for %s — %s. "
                    "Schema is authoritative; update _class in the "
                    "schema if the suggested classification is correct.",
                    device_id,
                    description,
                )
                self._warned_mismatches.add(device_id)

    def get_mismatched_devices(self) -> list[DiscoveredDeviceEntry]:
        """Get devices that have a class mismatch flag set.

        These are ACCEPTED devices whose scan engine likely_type differs
        from the schema's _class.  The review_discovered step shows them
        so the user can update _class or dismiss the mismatch.

        :return: List of device entries with class_mismatch set.
        """
        result: list[DiscoveredDeviceEntry] = []
        for entry in self.get_devices():
            if entry.metadata.class_mismatch:
                result.append(entry)
        return result

    def get_missing_class_devices(self) -> list[DiscoveredDeviceEntry]:
        """Get devices that have a missing_class flag set.

        These are ACCEPTED devices whose schema entry has no ``_class``
        but the scan engine has a ``likely_type``.  The review_discovered
        step shows them so the user can add ``_class`` from the discovery
        suggestion.

        :return: List of device entries with missing_class set.
        """
        result: list[DiscoveredDeviceEntry] = []
        for entry in self.get_devices():
            if entry.metadata.missing_class:
                result.append(entry)
        return result

    def get_orphaned_devices(self) -> list[DiscoveredDeviceEntry]:
        """Get devices that have an orphaned flag set.

        These are schema devices that haven't been seen by the scan
        engine for longer than the orphan threshold (default 7 days).
        The ``review_device_health`` config flow step shows them so the
        user can remove them if truly gone, or dismiss the flag if the
        device is just quiet.

        :return: List of device entries with orphaned set.
        """
        result: list[DiscoveredDeviceEntry] = []
        for entry in self.get_devices():
            if entry.metadata.orphaned:
                result.append(entry)
        return result

    def get_lost_devices(self) -> list[DiscoveredDeviceEntry]:
        """Get devices that have LOST status.

        These are ACCEPTED devices that haven't been seen for the
        configured threshold (default 7 days).  The
        ``review_device_health`` config flow step shows them so the user
        can remove them if truly gone.

        :return: List of device entries with LOST status.
        """
        result: list[DiscoveredDeviceEntry] = []
        for entry in self.get_devices():
            # HGI gateways are never "lost" — they are the receiver, not
            # remote devices.  This also cleans up any pre-existing LOST
            # status that may have been set before the check_for_lost_devices
            # skip was added.
            if self._is_hgi(entry.device.device_id):
                continue
            if entry.metadata.status == DiscoveryStatus.LOST:
                result.append(entry)
        return result

    def check_bound_mismatches(self, schema: dict[str, Any]) -> int:
        """Check for _bound mismatches between scan engine and schema.

        For each device that is in both the scan engine and the schema,
        compares the scan's ``bound_to`` with the schema's ``_bound``.
        If they differ, sets ``bound_mismatch`` on the device's metadata.

        The schema is authoritative — this method does NOT modify the
        schema.  It only warns the user that discovery suggests a
        different binding.

        :param schema: The current config entry schema (with _ traits).
        :return: Number of mismatches found.
        """
        scan_devices = {d.device_id: d for d in self._scan.get_devices()}
        mismatches: list[tuple[str, str, str]] = []

        for device_id, dev in scan_devices.items():
            if self._is_hgi(device_id):
                continue

            schema_entry = schema.get(device_id)
            if not isinstance(schema_entry, dict):
                continue

            # _bound is str (REM/DIS → their FAN) or list[str] (FAN →
            # its bound REMs, Phase 3b multi-REM format)
            schema_bound = schema_entry.get(SZ_TR_BOUND)
            if isinstance(schema_bound, str):
                schema_bound_ids = [schema_bound] if schema_bound else []
            elif isinstance(schema_bound, list):
                schema_bound_ids = [
                    b for b in schema_bound if isinstance(b, str) and b
                ]
            else:
                schema_bound_ids = []
            if not schema_bound_ids:
                continue  # no _bound in schema — nothing to compare

            scan_bound = dev.bound_to or ""
            if not scan_bound:
                continue  # scan doesn't know — not a meaningful mismatch

            # Normalize for comparison (case-insensitive).  For a list,
            # the scan's single bound_to must be one of the schema's
            # bound IDs — otherwise it's a mismatch.
            schema_bound_str = (
                schema_bound
                if isinstance(schema_bound, str)
                else ", ".join(schema_bound_ids)
            )
            if scan_bound.upper() not in {b.upper() for b in schema_bound_ids}:
                meta = self._metadata.get(device_id, DeviceMetadata())
                meta.bound_mismatch = (
                    f"schema={schema_bound_str}, discovery={scan_bound}"
                )
                self._metadata[device_id] = meta
                mismatches.append((device_id, schema_bound_str, scan_bound))
                _LOGGER.debug(
                    "DiscoveryManager: bound mismatch for %s — "
                    "schema has _bound=%s but discovery suggests %s",
                    device_id,
                    schema_bound,
                    scan_bound,
                )
            else:
                # Mismatch resolved — clear the flag
                existing_meta = self._metadata.get(device_id)
                if existing_meta and existing_meta.bound_mismatch:
                    existing_meta.bound_mismatch = None
                    self._metadata[device_id] = existing_meta

        if mismatches:
            _LOGGER.warning(
                "DiscoveryManager: %d device(s) have bound mismatches "
                "between discovery and schema: %s",
                len(mismatches),
                ", ".join(f"{d} ({s}→{t})" for d, s, t in mismatches),
            )

        return len(mismatches)

    def check_missing_class(self, schema: dict[str, Any]) -> int:
        """Check for schema devices that have no _class but discovery has one.

        For each device that is in both the scan engine and the schema,
        if the scan engine has a ``likely_type`` but the schema entry has
        no ``_class``, sets ``missing_class`` on the device's metadata.

        :param schema: The current config entry schema (with _ traits).
        :return: Number of missing-class flags set.
        """
        scan_devices = {d.device_id: d for d in self._scan.get_devices()}
        missing: list[str] = []

        for device_id, dev in scan_devices.items():
            if self._is_hgi(device_id):
                continue

            schema_entry = schema.get(device_id)
            if not isinstance(schema_entry, dict):
                continue

            schema_class = schema_entry.get(SZ_TR_CLASS)
            if isinstance(schema_class, str) and schema_class:
                # Has _class — clear any previous missing_class flag
                existing_meta = self._metadata.get(device_id)
                if existing_meta and existing_meta.missing_class:
                    existing_meta.missing_class = None
                    self._metadata[device_id] = existing_meta
                continue

            scan_type = str(dev.likely_type) if dev.likely_type else ""
            if not scan_type or scan_type == "DEV":
                continue  # scan doesn't know either — not actionable

            # Skip if the user already dismissed this missing_class ("Skip")
            existing_meta = self._metadata.get(device_id)
            if existing_meta and existing_meta.missing_class_dismissed:
                continue  # user decided — don't re-flag

            meta = self._metadata.get(device_id, DeviceMetadata())
            meta.missing_class = f"discovery={scan_type}"
            self._metadata[device_id] = meta
            missing.append(device_id)
            _LOGGER.debug(
                "DiscoveryManager: missing _class for %s — "
                "discovery suggests %s but schema has no _class",
                device_id,
                scan_type,
            )

        if missing:
            _LOGGER.info(
                "DiscoveryManager: %d device(s) in schema have no _class "
                "but discovery has a suggestion: %s",
                len(missing),
                ", ".join(missing),
            )

        return len(missing)

    def check_orphaned_devices(
        self, schema: dict[str, Any], *, threshold_days: int | None = None
    ) -> int:
        """Check for schema devices not seen by discovery for a long time.

        For each device that is in the schema AND in the scan engine's
        device list, checks ``last_seen``.  If the device hasn't been
        seen for longer than the threshold, sets ``orphaned`` on the
        device's metadata.

        Devices that are in the schema but NOT in the scan engine are
        skipped — the scan may simply not have seen them yet, so
        flagging them would be a false positive.  This mirrors the
        behaviour of ``check_for_lost_devices``.

        :param schema: The current config entry schema (with _ traits).
        :param threshold_days: Days without traffic before flagging.
            Defaults to ``self._lost_threshold_days``.
        :return: Number of orphaned flags set.
        """
        if threshold_days is None:
            threshold_days = self._lost_threshold_days

        scan_devices = {d.device_id: d for d in self._scan.get_devices()}
        now = dt_util.now()  # tz-aware (HA default timezone)
        threshold = now - td(days=threshold_days)
        orphaned: list[str] = []

        for device_id, schema_entry in schema.items():
            if not isinstance(schema_entry, dict):
                continue
            if self._is_hgi(device_id):
                continue  # HGI — not a real device
            # Skip structural keys (main_tcs, _owner, etc.)
            if device_id.startswith("_") or device_id in ("main_tcs",):
                continue

            dev = scan_devices.get(device_id)
            if dev is None:
                # Not in scan — skip (scan may not have seen it yet).
                # Same logic as check_for_lost_devices.
                continue

            # In scan — check last_seen
            last_seen_str = getattr(dev, "last_seen", None)
            if not last_seen_str:
                continue
            try:
                last_seen = dt.fromisoformat(last_seen_str)
            except (ValueError, TypeError):
                continue

            # Ensure tz-aware for comparison (ramses_rf may store naive
            # or tz-aware datetimes depending on version/config).
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(
                    tzinfo=dt_util.get_default_time_zone()
                )

            if last_seen < threshold:
                existing_meta = self._metadata.get(device_id)
                meta = existing_meta or DeviceMetadata()

                # Check if the user has suppressed notifications for
                # this device via the schema key ``_suppress_not_seen``.
                # Accepted values:
                #   True  → suppress forever (INFO log every threshold_days)
                #   N(int)→ suppress for N days from last_seen, then re-notify
                # Suppressed devices are not added to the orphaned list
                # (so no persistent notification), but an INFO log is
                # emitted once every ``threshold_days`` as a gentle
                # reminder (e.g. in case batteries died).  Issue 988.
                suppress_val = schema_entry.get("_suppress_not_seen")
                suppress_forever = suppress_val is True
                suppress_days: int | None = None
                if isinstance(suppress_val, (int, float)) and not isinstance(
                    suppress_val, bool
                ):
                    suppress_days = int(suppress_val)

                if suppress_forever or suppress_days is not None:
                    # Check if suppress period has expired (for int mode)
                    if suppress_days is not None:
                        suppress_threshold = now - td(days=suppress_days)
                        if last_seen < suppress_threshold:
                            # Suppress expired — remove key from schema so
                            # normal orphaned handling takes over (notify
                            # every cycle until user re-dismisses or device
                            # returns).  For True (forever) we never reach
                            # this branch.
                            schema_entry.pop("_suppress_not_seen", None)
                            meta.orphaned = (
                                f"last seen {last_seen_str} "
                                f"(>{threshold_days} days, suppress expired)"
                            )
                            meta.last_orphaned_log = None
                            self._metadata[device_id] = meta
                            orphaned.append(device_id)
                            _LOGGER.info(
                                "DiscoveryManager: suppress expired for %s "
                                "(not seen > %d days), re-notifying",
                                device_id,
                                suppress_days,
                            )
                            continue

                    # Still within suppress period
                    meta.orphaned = None  # no notification
                    # Check if we should log a periodic reminder
                    last_log = meta.last_orphaned_log
                    should_log = True
                    if last_log:
                        try:
                            last_log_dt = dt.fromisoformat(last_log)
                            if last_log_dt.tzinfo is None:
                                last_log_dt = last_log_dt.replace(
                                    tzinfo=dt_util.get_default_time_zone()
                                )
                            if last_log_dt > threshold:
                                should_log = False
                        except (ValueError, TypeError):
                            pass  # invalid timestamp → log again
                    if should_log:
                        meta.last_orphaned_log = now.isoformat()
                        suppress_desc = (
                            "forever"
                            if suppress_forever
                            else f"{suppress_days}d"
                        )
                        _LOGGER.info(
                            "DiscoveryManager: device %s not seen since %s "
                            "(> %d days, suppressed %s)",
                            device_id,
                            last_seen_str,
                            threshold_days,
                            suppress_desc,
                        )
                    self._metadata[device_id] = meta
                    continue

                # Not suppressed — flag as orphaned (triggers notification)
                meta.orphaned = (
                    f"last seen {last_seen_str} (>{threshold_days} days)"
                )
                meta.last_orphaned_log = None
                self._metadata[device_id] = meta
                orphaned.append(device_id)
                _LOGGER.debug(
                    "DiscoveryManager: orphaned device %s — last seen %s",
                    device_id,
                    last_seen_str,
                )
            else:
                # Seen recently — clear orphaned flag and last_orphaned_log.
                # Also remove _suppress_not_seen from the schema so that
                # if the device goes quiet again later, the user gets a
                # fresh orphaned notification (issue 988).
                existing_meta = self._metadata.get(device_id)
                if existing_meta and (
                    existing_meta.orphaned or existing_meta.last_orphaned_log
                ):
                    existing_meta.orphaned = None
                    existing_meta.last_orphaned_log = None
                    self._metadata[device_id] = existing_meta
                if schema_entry.get("_suppress_not_seen"):
                    schema_entry.pop("_suppress_not_seen", None)
                    _LOGGER.info(
                        "DiscoveryManager: device %s seen again, "
                        "cleared _suppress_not_seen from schema",
                        device_id,
                    )

        if orphaned:
            _LOGGER.info(
                "DiscoveryManager: %d device(s) in schema appear orphaned "
                "(not seen > %d days): %s",
                len(orphaned),
                threshold_days,
                ", ".join(orphaned),
            )

        return len(orphaned)

    def check_name_mismatches(
        self, schema: dict[str, Any], zones: list[Any] | None = None
    ) -> int:
        """Check for zone name mismatches between schema and controller.

        For each zone in the ramses_rf client, compares the schema's
        ``_name`` with the runtime name reported by the controller via
        0004 packets (``zone.zone_state.name``).  If they differ, sets
        ``name_mismatch`` on the zone's metadata.

        The controller's 0004 name is authoritative for ``_name``.
        Users who want a custom display name should use ``_alias``
        (which overrides ``_name`` for display).  There is no dismiss
        option — the schema ``_name`` should be updated to match the
        controller (issue 947).

        :param schema: The current config entry schema (with _ traits).
        :param zones: The coordinator's list of ramses_rf Zone objects.
            If ``None``, the check is skipped (no zones to compare).
        :return: Number of name mismatches found.
        """
        if zones is None:
            return 0

        mismatches: list[tuple[str, str, str]] = []

        for zone in zones:
            zone_id = str(zone.id)  # e.g. "01:150000_03"
            runtime_name = getattr(zone.zone_state, "name", None)
            if not runtime_name:
                continue  # no 0004 name received yet — nothing to compare

            # Find the schema's _name for this zone.
            # zone_id is "<ctl_id>_<zone_index>", e.g. "01:150000_03".
            # Schema stores zones under schema[ctl_id]["zones"][zone_index].
            parts = zone_id.rsplit("_", 1)
            if len(parts) != 2:
                continue
            ctl_id, zone_index = parts
            ctl_entry = schema.get(ctl_id)
            if not isinstance(ctl_entry, dict):
                continue
            ctl_zones = ctl_entry.get("zones")
            if not isinstance(ctl_zones, dict):
                continue
            zone_entry = ctl_zones.get(zone_index)
            if not isinstance(zone_entry, dict):
                continue
            schema_name = zone_entry.get(SZ_TR_NAME)
            if not isinstance(schema_name, str) or not schema_name:
                continue  # no _name in schema — nothing to compare

            if schema_name != runtime_name:
                meta = self._metadata.get(zone_id, DeviceMetadata())
                meta.name_mismatch = (
                    f"schema={schema_name}, controller={runtime_name}"
                )
                self._metadata[zone_id] = meta
                mismatches.append((zone_id, schema_name, runtime_name))
                _LOGGER.debug(
                    "DiscoveryManager: name mismatch for zone %s — "
                    "schema has _name=%r but controller reports %r. "
                    "Update _name in the schema to match, or use _alias "
                    "for a custom display name.",
                    zone_id,
                    schema_name,
                    runtime_name,
                )
            else:
                # Mismatch resolved — clear the flag
                existing_meta = self._metadata.get(zone_id)
                if existing_meta and existing_meta.name_mismatch:
                    existing_meta.name_mismatch = None
                    self._metadata[zone_id] = existing_meta

        if mismatches:
            # Only WARN once per zone — subsequent checks log at DEBUG.
            # This avoids log spam every checkpoint cycle for persistent
            # mismatches.  The notification (via _send_mismatch_notification)
            # uses a fixed notification_id so HA updates it in-place rather
            # than creating duplicates.
            new_mismatches = [
                (d, s, t)
                for d, s, t in mismatches
                if d not in self._warned_name_mismatches
            ]
            if new_mismatches:
                _LOGGER.warning(
                    "DiscoveryManager: %d zone(s) have name mismatches "
                    "between schema and controller: %s",
                    len(new_mismatches),
                    ", ".join(f"{d} ({s}→{t})" for d, s, t in new_mismatches),
                )
                self._warned_name_mismatches.update(
                    d for d, _, _ in new_mismatches
                )
            else:
                _LOGGER.debug(
                    "DiscoveryManager: %d persistent name mismatch(s) "
                    "(already warned): %s",
                    len(mismatches),
                    ", ".join(d for d, _, _ in mismatches),
                )
        else:
            # All name mismatches resolved — clear the warned set
            if self._warned_name_mismatches:
                _LOGGER.info("DiscoveryManager: all name mismatches resolved")
                self._warned_name_mismatches.clear()

        return len(mismatches)

    def get_name_mismatch_devices(self) -> list[DiscoveredDeviceEntry]:
        """Get zones that have a name mismatch flag set.

        These are zones whose schema ``_name`` differs from the
        controller's 0004-reported name.  The review_discovered step
        shows them so the user can update ``_name`` or add ``_alias``.

        :return: List of device entries with name_mismatch set.
        """
        result: list[DiscoveredDeviceEntry] = []
        for entry in self.get_devices():
            if entry.metadata.name_mismatch:
                result.append(entry)
        return result

    # ── Communication quality (issue 1047) ───────────────────────

    # Log throttle: minimum seconds between WARNING logs for the same
    # device's weak-signal condition.  Prevents HA log spam when a
    # device is borderline and oscillates around the threshold.
    _WEAK_SIGNAL_LOG_INTERVAL: Final[int] = 3600  # 1 hour

    def check_communication_quality(
        self,
        schema: dict[str, Any],
        devices: list[Any] | None = None,
    ) -> int:
        """Check device communication quality (RSSI only).

        For each ramses_rf device that has a ``communication_quality``
        property, evaluates the snapshot.  If the RSSI quality is
        ``"weak"`` or ``"very_weak"``, sets ``weak_signal`` on the
        device's metadata so the review_device_health step can surface
        it.

        Staleness (time since last transmission) is **not** checked
        here.  Battery devices (TRVs, sensors, DHW) transmit every
        10-30 minutes by design, so a flat staleness threshold would
        falsely flag them.  "Device gone silent" is handled by:
        - ``is_available`` (entity state in HA UI)
        - ``check_for_orphaned_devices`` (7-day persistent notification)

        A WARNING is logged at most once per hour per device (throttled
        via ``last_weak_signal_log``) to avoid spamming the HA log.

        The user can suppress future warnings for a device by setting
        ``_suppress_weak_signal: True`` in the schema entry (e.g. for
        a device that is known to be far from the HGI and works fine).

        :param schema: The current config entry schema (with _ traits).
        :param devices: The coordinator's list of ramses_rf device
            objects.  If ``None``, the check is skipped.
        :return: Number of weak-signal flags set.
        """
        if devices is None:
            _LOGGER.debug(
                "check_communication_quality: devices is None, skipping"
            )
            return 0

        now = dt_util.now()
        log_threshold = now - td(seconds=self._WEAK_SIGNAL_LOG_INTERVAL)
        flagged: list[str] = []

        _LOGGER.debug(
            "check_communication_quality: checking %d device(s)", len(devices)
        )
        for device in devices:
            device_id = str(device.id)
            # Skip HGI gateway — it is the receiver, not a remote device.
            if self._is_hgi(device_id):
                continue
            # Skip foreign-owner devices.
            if device_id in self._foreign_device_ids:
                continue

            quality = getattr(device, "communication_quality", None)
            if quality is None:
                _LOGGER.debug(
                    "check_communication_quality: %s has no "
                    "communication_quality (getattr returned None)",
                    device_id,
                )
                continue  # no RSSI tracker (e.g. tests without a gateway)

            # If we have RSSI data, the device is being heard — clear
            # any stale LOST status (a weak device is not lost).
            if quality.best_rssi is not None:
                existing = self._metadata.get(device_id)
                if existing and existing.status == DiscoveryStatus.LOST:
                    existing.status = DiscoveryStatus.ACCEPTED
                    self._metadata[device_id] = existing
                    _LOGGER.info(
                        "DiscoveryManager: %s was LOST but is now "
                        "being heard (rssi=%s), cleared LOST status",
                        device_id,
                        quality.best_rssi,
                    )

            # Determine if the device has weak signal (RSSI only).
            # Staleness is not checked — see docstring above.
            is_weak = quality.rssi_quality in ("weak", "very_weak")
            _LOGGER.debug(
                "check_communication_quality: %s rssi=%s quality=%s "
                "is_weak=%s",
                device_id,
                quality.best_rssi,
                quality.rssi_quality,
                is_weak,
            )
            if not is_weak:
                # Quality is good — clear any previous flag.
                existing = self._metadata.get(device_id)
                if existing and (
                    existing.weak_signal
                    or existing.last_weak_signal_log
                    or existing.weak_signal_dismissed
                ):
                    existing.weak_signal = None
                    existing.last_weak_signal_log = None
                    # Clear dismissed so the user gets a fresh warning
                    # if the device degrades again after recovering.
                    existing.weak_signal_dismissed = False
                    self._metadata[device_id] = existing
                    _LOGGER.debug(
                        "DiscoveryManager: communication quality "
                        "recovered for %s (rssi=%s, quality=%s)",
                        device_id,
                        quality.best_rssi,
                        quality.rssi_quality,
                    )
                continue

            # Build a human-readable description.
            description = (
                f"RSSI {quality.best_rssi} dBm ({quality.rssi_quality})"
            )

            # Check if the user has suppressed warnings for this device.
            schema_entry = schema.get(device_id)
            if isinstance(schema_entry, dict):
                if schema_entry.get("_suppress_weak_signal") is True:
                    # Suppressed — clear flag, no notification.
                    existing = self._metadata.get(device_id)
                    if existing and existing.weak_signal:
                        existing.weak_signal = None
                        self._metadata[device_id] = existing
                    continue

            # Check if the user has dismissed this via review_device_health.
            existing = self._metadata.get(device_id)
            if existing and existing.weak_signal_dismissed:
                continue  # user decided — don't re-flag

            # Set the flag.
            meta = existing or DeviceMetadata()
            meta.weak_signal = description
            self._metadata[device_id] = meta
            flagged.append(device_id)

            # Throttle the WARNING log to once per hour per device.
            should_log = True
            if meta.last_weak_signal_log:
                try:
                    last_log = dt.fromisoformat(meta.last_weak_signal_log)
                    if last_log.tzinfo is None:
                        last_log = last_log.replace(
                            tzinfo=dt_util.get_default_time_zone()
                        )
                    if last_log > log_threshold:
                        should_log = False
                except (ValueError, TypeError):
                    pass  # invalid timestamp → log again

            if should_log:
                meta.last_weak_signal_log = now.isoformat()
                self._metadata[device_id] = meta
                _LOGGER.warning(
                    "DiscoveryManager: weak signal for %s — %s. "
                    "Check RF range/batteries, or set "
                    "_suppress_weak_signal: true in the schema "
                    "to dismiss.",
                    device_id,
                    description,
                )

        if flagged:
            _LOGGER.info(
                "DiscoveryManager: %d device(s) have weak signal: %s",
                len(flagged),
                ", ".join(flagged),
            )

        return len(flagged)

    def get_weak_signal_devices(self) -> list[DiscoveredDeviceEntry]:
        """Get devices that have a weak_signal flag set.

        These are devices whose communication quality (RSSI or
        staleness) is poor.  The ``review_device_health`` config flow
        step shows them so the user can dismiss the flag or suppress
        future warnings.

        :return: List of device entries with weak_signal set.
        """
        result: list[DiscoveredDeviceEntry] = []
        for entry in self.get_devices():
            if entry.metadata.weak_signal:
                result.append(entry)
        return result

    def check_all_mismatches(
        self,
        schema: dict[str, Any],
        zones: list[Any] | None = None,
        devices: list[Any] | None = None,
    ) -> dict[str, int]:
        """Run all mismatch checks & send persistent notification if needed.

        Convenience method that calls all six checks:
        - check_class_mismatches
        - check_bound_mismatches
        - check_missing_class
        - check_orphaned_devices
        - check_name_mismatches (requires *zones*)
        - check_communication_quality (requires *devices*)

        :param schema: The current config entry schema (with _ traits).
        :param zones: The coordinator's list of ramses_rf Zone objects.
            Passed to :meth:`check_name_mismatches`.  If ``None``, the
            name mismatch check is skipped.
        :param devices: The coordinator's list of ramses_rf device
            objects.  Passed to :meth:`check_communication_quality`.
            If ``None``, the communication quality check is skipped.
        :return: Dict with counts per check type.
        """
        counts = {
            "class_mismatch": self.check_class_mismatches(schema),
            "bound_mismatch": self.check_bound_mismatches(schema),
            "missing_class": self.check_missing_class(schema),
            "orphaned": self.check_orphaned_devices(schema),
            "name_mismatch": self.check_name_mismatches(schema, zones),
            "weak_signal": self.check_communication_quality(schema, devices),
        }
        # Also count rf-flagged mismatches (from _check_rf_contradictions)
        # that check_class_mismatches may have missed (scan engine doesn't
        # re-classify known devices, so likely_type may agree with schema
        # even when ramses_rf's known_list suggests a different class).
        rf_flagged = [
            d_id
            for d_id, meta in self._metadata.items()
            if meta.class_mismatch and d_id not in self._warned_mismatches
        ]
        if rf_flagged:
            counts["class_mismatch"] += len(rf_flagged)
        total = sum(counts.values())
        if total > 0:
            self._send_mismatch_notification(counts)
        else:
            # All clear — dismiss any existing mismatch notification
            async_dismiss_notification(
                self._hass, self._mismatch_notification_id
            )

        return counts

    def _send_mismatch_notification(self, counts: dict[str, int]) -> None:
        """Send a persistent notification about schema/discovery mismatches."""
        _LOGGER.debug(
            "DiscoveryManager: _send_mismatch_notification counts=%s",
            counts,
        )
        lines: list[str] = []

        class_mm = self.get_mismatched_devices()
        if counts["class_mismatch"] and class_mm:
            lines.append(
                f"**{counts['class_mismatch']} class mismatch(es):**\n"
            )
            for entry in class_mm:
                mm = entry.metadata.class_mismatch or ""
                lines.append(f"- `{entry.device.device_id}` — {mm}")
            lines.append("")

        bound_mm = [e for e in self.get_devices() if e.metadata.bound_mismatch]
        if counts["bound_mismatch"] and bound_mm:
            lines.append(
                f"**{counts['bound_mismatch']} bound mismatch(es):**\n"
            )
            for entry in bound_mm:
                lines.append(
                    f"- `{entry.device.device_id}` — "
                    f"{entry.metadata.bound_mismatch}"
                )
            lines.append("")

        missing_cls = [
            e for e in self.get_devices() if e.metadata.missing_class
        ]
        if counts["missing_class"] and missing_cls:
            lines.append(f"**{counts['missing_class']} missing _class:**\n")
            for entry in missing_cls:
                lines.append(
                    f"- `{entry.device.device_id}` — "
                    f"{entry.metadata.missing_class}"
                )
            lines.append("")

        orphaned = [e for e in self.get_devices() if e.metadata.orphaned]
        if counts["orphaned"] and orphaned:
            lines.append(f"**{counts['orphaned']} orphaned device(s):**\n")
            for entry in orphaned:
                lines.append(
                    f"- `{entry.device.device_id}` — {entry.metadata.orphaned}"
                )
            lines.append("")

        name_mm = [e for e in self.get_devices() if e.metadata.name_mismatch]
        if counts.get("name_mismatch") and name_mm:
            lines.append(
                f"**{counts['name_mismatch']} zone name mismatch(es):**\n"
            )
            for entry in name_mm:
                lines.append(
                    f"- `{entry.device.device_id}` — "
                    f"{entry.metadata.name_mismatch} "
                    "(update _name, or use _alias for display name)"
                )
            lines.append("")

        weak = self.get_weak_signal_devices()
        if counts.get("weak_signal") and weak:
            lines.append(
                f"**{counts['weak_signal']} weak signal device(s):**\n"
            )
            for entry in weak:
                lines.append(
                    f"- `{entry.device.device_id}` — "
                    f"{entry.metadata.weak_signal}"
                )
            lines.append("")

        if not lines:
            return

        lines.append(
            "[Review device health]"
            "(/config/integrations/integration/ramses_cc)"
            " — open **Configure → Review device health** to resolve."
        )

        async_create_notification(
            self._hass,
            message="\n".join(lines),
            title="RAMSES CC: Schema mismatches detected",
            notification_id=self._mismatch_notification_id,
        )

    def export_state(self) -> dict[str, Any]:
        """Export full state for persistence.

        Called on shutdown/checkpoint to save to .storage/.

        :return: Dict with device metadata and scan engine state.
        """
        return {
            SZ_DISCOVERY_DEVICES: {
                device_id: meta.to_dict()
                for device_id, meta in self._metadata.items()
            },
            SZ_DISCOVERY_SCAN_STATE: self._scan.export_json(),
        }

    def get_devices(
        self,
        *,
        status: DiscoveryStatus | None = None,
        enabled: bool | None = None,
    ) -> list[DiscoveredDeviceEntry]:
        """Get discovered devices, optionally filtered.

        Merges the scan engine's in-memory device list with ramses_cc metadata.

        :param status: Filter by discovery status.
        :param enabled: Filter by enabled flag.
        :return: List of device entries with engine data + metadata.
        """
        engine_devices = {d.device_id: d for d in self._scan.get_devices()}
        entries: list[DiscoveredDeviceEntry] = []

        # Include devices from both the engine and metadata (faked devices
        # may not be in the engine since they don't broadcast)
        all_ids = set(engine_devices.keys()) | set(self._metadata.keys())

        for device_id in all_ids:
            # Skip local active HGI gateway — it is managed directly by the
            # coordinator and auto-registered in the schema.
            if self._active_hgi_id and device_id == self._active_hgi_id:
                continue
            meta = self._metadata.get(device_id, DeviceMetadata())

            if status is not None and meta.status != status:
                continue
            if enabled is not None and meta.enabled != enabled:
                continue

            # For devices not in the engine (faked, or restored from
            # storage but not yet seen in this session), create a stub
            if device_id in engine_devices:
                entries.append(
                    DiscoveredDeviceEntry(
                        device=engine_devices[device_id], metadata=meta
                    )
                )
            else:
                # Stub for faked/restored devices with no engine data
                from ramses_rf.discovery_scan import DiscoveredDevice

                entries.append(
                    DiscoveredDeviceEntry(
                        device=DiscoveredDevice(
                            device_id=device_id,
                            first_seen="",
                            last_seen="",
                            likely_type=(
                                "REM"
                                if meta.faked
                                else self._hgi_likely_type(
                                    device_id, "unknown"
                                )
                            ),
                            codes_seen=[],
                            bound_to=None,
                            zone_index=None,
                            rssi=None,
                            confidence="high" if meta.faked else "low",
                            is_battery=False,
                            source_count=0,
                            destination_count=0,
                        ),
                        metadata=meta,
                    )
                )

        return entries

    def get_device(self, device_id: str) -> DiscoveredDeviceEntry | None:
        """Get a single device entry by ID.

        :param device_id: The device ID to look up.
        :return: The device entry, or None if not found.
        """
        for entry in self.get_devices():
            if entry.device.device_id == device_id:
                return entry
        return None

    # Suffix appended to every auto-generated comment to warn users
    # not to edit the structured portions (zone, bound_to, codes, RSSI).
    _COMMENT_SUFFIX: str = "(auto-generated — do not edit)"

    # Types that the scan engine may confuse with each other.
    # 31DA (fan_status) is sent by both FANs and DIS devices.
    _AMBIGUOUS_TYPES: dict[str, str] = {
        "FAN": "may also be DIS (31DA is sent by both)",
        "DIS": "may also be FAN (31DA is sent by both)",
    }

    @staticmethod
    def _build_comment(
        dev: Any,
        likely_type: str,
        bound_to: str | None,
        zone_index: str | None = None,
        *,
        schema_role: str | None = None,
    ) -> str:
        """Build a descriptive comment from scan engine data.

        Includes: likely type, confidence, ambiguity notes, binding info,
        domain_id (appliance_control), packet codes seen, and battery/RSSI
        if available.

        :param schema_role: The device's actual role in the schema (e.g.
            ``"hotwater_valve"``).  When set, this overrides the scan
            engine's domain_id hint — the schema is the SSOT.  See issue 834.
        """
        parts: list[str] = []
        resolved_zone = zone_index if zone_index is not None else zone_index

        # Type + confidence
        confidence = getattr(dev, "confidence", None) if dev else None
        if confidence and confidence != "high":
            parts.append(f"Likely {likely_type} (confidence: {confidence})")
        else:
            parts.append(f"Likely {likely_type}")

        # Ambiguity note
        ambiguity = DiscoveryManager._AMBIGUOUS_TYPES.get(likely_type.upper())
        if ambiguity:
            parts.append(ambiguity)

        # Binding info — use "belongs to" for HVAC (FAN parent) to
        # distinguish from hardware handshake "_bound" trait (1FC9 pairing).
        # "bound to" is kept for heat-domain TCS bindings (sensors/actuators).
        if bound_to:
            if bound_to.startswith("32:"):
                parts.append(f"belongs to {bound_to}")
            else:
                parts.append(f"bound to {bound_to}")
        if resolved_zone:
            parts.append(f"zone {resolved_zone}")

        # Domain ID (FC = appliance_control, FA = hotwater_valve,
        # F9 = heating_valve, issue 834/931).
        # The scan engine's domain_id can be authoritative (from 000C
        # binding table) or a non-authoritative hint (from 3B00/3EF0).
        # Both appliance_control and hotwater_valve relays send 3B00/3EF0,
        # so the hint is ambiguous.  If the schema has already placed this
        # device as hotwater_valve, suppress the FC hint — the schema is
        # the SSOT and the user has confirmed the placement.
        domain_id = getattr(dev, "domain_id", None) if dev else None
        is_auth = (
            getattr(dev, "is_authoritative_domain", False) if dev else False
        )
        if domain_id and schema_role != "hotwater_valve":
            if is_auth:
                # Authoritative — from 000C binding table
                domain_names = {
                    "FC": "appliance_control",
                    "FA": "hotwater_valve",
                    "F9": "heating_valve",
                }
                name = domain_names.get(domain_id, domain_id)
                parts.append(f"domain {domain_id} ({name})")
            elif domain_id == "FC":
                # Non-authoritative hint from 3B00/3EF0 — ambiguous
                parts.append(
                    "domain FC hint from 3B00/3EF0 — could be "
                    "appliance_control or hotwater_valve; awaiting "
                    "000C binding"
                )

        # Packet codes seen
        codes = getattr(dev, "codes_seen", None) if dev else None
        if codes:
            parts.append(f"codes: {', '.join(codes[:5])}")

        # Battery
        is_battery = getattr(dev, "is_battery", False) if dev else False
        if is_battery:
            parts.append("battery")

        # RSSI
        rssi = getattr(dev, "rssi", None) if dev else None
        if rssi is not None:
            parts.append(f"RSSI {rssi:.0f}")

        return ". ".join(parts) + f". {DiscoveryManager._COMMENT_SUFFIX}"

    @staticmethod
    def generate_schema_entry(
        device_id: str,
        likely_type: str,
        *,
        bound_to: str | None = None,
        zone_index: str | None = None,
        ctl_id: str | None = None,
        comment: str | None = None,
        domain_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate a schema fragment for a discovered device.

        Maps the scan engine's ``likely_type`` to the appropriate
        ramses_rf global schema structure.  Returns a *fragment* —
        the caller merges it into the full schema dict.

        If ``comment`` is provided, a ``_comment`` trait is injected into
        the device's own schema entry (for devices that get a dict entry,
        like CTL and FAN).  For list-based devices (REM, CO2, TRV, etc. that
        end up as strings in lists), the comment is added to a top-level
        ``device_comments`` dict instead.

        Both ``_comment`` and ``device_comments`` are stripped by
        ``_strip_schema_extensions`` before ramses_rf sees the schema,
        so they survive cache loss (lives in the config entry) but do
        not pollute the ramses_rf schema.

        :param device_id: The device ID (e.g. ``04:056053``).
        :param likely_type: One of CTL, TRV, DHW, OTB, BDR, FAN, REM, CO2, THM.
        :param bound_to: Optional parent device ID (for REM → FAN).
        :param zone_index: Optional zone index (for TRV/THM in a TCS).
        :param zone_index: Deprecated alias for zone_index.
        :param ctl_id: Optional CTL device ID (for placing devices in a TCS).
        :param comment: Optional comment for the ``_comment`` trait.
        :param domain_id: Optional domain ID (FC=appliance_control).
        :return: A dict that can be deep-merged into the global schema.
        """
        from ramses_rf.schemas import (
            SZ_ACTUATORS,
            SZ_APPLIANCE_CONTROL,
            SZ_DHW_SYSTEM,
            SZ_DHW_VALVE,
            SZ_MAIN_TCS,
            SZ_ORPHANS_HEAT,
            SZ_ORPHANS_HVAC,
            SZ_REMOTES,
            SZ_SENSOR,
            SZ_SYSTEM,
            SZ_ZONES,
        )

        likely_type_normalized = likely_type.upper()
        resolved_zone = zone_index if zone_index is not None else zone_index

        # Helper: inject _comment into a device's own dict entry
        def _with_comment(entry: dict[str, Any]) -> dict[str, Any]:
            if comment:
                entry[SZ_TR_COMMENT] = comment
            return entry

        # Helper: for list devices, add comment to top-level device_comments
        def _list_comment() -> dict[str, Any]:
            if comment:
                return {SZ_DEVICE_COMMENTS: {device_id: comment}}
            return {}

        # Helper: merge a fragment with optional list comment.
        # Always ensures a root-level entry for the device so that traits
        # (_owner, _faked, _class, etc.) can be set by the config flow or
        # by the user via the schema editor.  Without this, list-based
        # devices (REM/CO2 in remotes[], TRV in zones[], etc.) would have
        # no root entry and _owner could never be set — breaking SSOT.
        #
        # Also injects _class on the root entry so the scan engine's
        # likely_type is persisted as the schema identity trait.  This
        # matches the architecture intent (accept_discovered_device writes
        # _class to schema) and prevents check_missing_class from flagging
        # every accepted device on the next checkpoint.  Uses setdefault so
        # a caller that already set _class (e.g. add_faked_rem) wins.
        def _merge(fragment: dict[str, Any]) -> dict[str, Any]:
            root = fragment.setdefault(device_id, {})
            root.setdefault(SZ_TR_CLASS, likely_type_normalized)
            fragment.update(_list_comment())
            return fragment

        # ── CTL: Temperature Control System controller ──────────────
        if likely_type_normalized == "CTL":
            return {
                SZ_MAIN_TCS: device_id,
                device_id: _with_comment(
                    {SZ_TR_CLASS: likely_type_normalized}
                ),
            }

        # ── FAN: HVAC controller ────────────────────────────────────
        if likely_type_normalized == "FAN":
            return {
                device_id: _with_comment(
                    {SZ_TR_CLASS: likely_type_normalized, SZ_REMOTES: []}
                ),
            }

        # ── HGI: Hardware Gateway Interface ─────────────────────────
        if likely_type_normalized == "HGI":
            return {
                device_id: _with_comment(
                    {SZ_TR_CLASS: likely_type_normalized}
                ),
            }

        # ── REM / CO2: HVAC remote or sensor — add to parent FAN ─────
        #  37: devices are classified as CO2 or REM depending on which
        #  packet arrived last (they send both I 1298 and I 22F1).  Both
        #  are HVAC devices that belong under a FAN parent.  We put them
        #  in remotes[] for now — the sensors[] list is reserved for the
        #  future when load_fan is implemented and the Builder pattern
        #  can distinguish dual-role devices (CO2 sensor + REM).
        #
        #  remotes[] is only valid under a FAN/VCS entry (ramses_rf's
        #  SCH_VCS).  It must NEVER be placed under a CTL/TCS entry
        #  (SCH_TCS rejects 'remotes' with PREVENT_EXTRA), so we must not
        #  fall back to ctl_id (the TCS) when bound_to (the FAN) is
        #  unknown — that corrupts the schema and breaks setup (issue
        #  825).  CTL device IDs use the 01:/23: prefixes (see
        #  ramses_tx DEVICE_ID_REGEX.CTL), so a bound_to pointing at a CTL
        #  is treated as unknown and the device is orphaned to
        #  orphans_hvac rather than incorrectly nested under the TCS.
        _CTL_PREFIXES = ("01:", "23:")
        if likely_type_normalized in ("REM", "CO2"):
            if bound_to and not bound_to.startswith(_CTL_PREFIXES):
                return _merge({bound_to: {SZ_REMOTES: [device_id]}})
            return _merge({SZ_ORPHANS_HVAC: [device_id]})

        # ── OTB: OpenTherm Bridge — appliance_control for a CTL ─────
        if likely_type_normalized == "OTB":
            if ctl_id:
                return _merge(
                    {
                        ctl_id: {SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: device_id}},
                    }
                )
            return _merge({SZ_ORPHANS_HEAT: [device_id]})

        # ── BDR: relay — appliance_control, DHW valve, or zone actuator
        if likely_type_normalized == "BDR":
            if ctl_id and resolved_zone:
                return _merge(
                    {
                        ctl_id: {
                            SZ_ZONES: {
                                resolved_zone: {SZ_ACTUATORS: [device_id]},
                            },
                        },
                    }
                )
            # domain_id=FC means the BDR broadcasts 3B00/3EF0 (TPI loop) —
            # it's the boiler relay, not a DHW valve.  See issue 834.
            if ctl_id and domain_id == "FC":
                return _merge(
                    {
                        ctl_id: {SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: device_id}},
                    }
                )
            if ctl_id:
                # No zone, no FC domain — assume DHW valve (htg_valve)
                return _merge(
                    {
                        ctl_id: {SZ_DHW_SYSTEM: {SZ_DHW_VALVE: device_id}},
                    }
                )
            return _merge({SZ_ORPHANS_HEAT: [device_id]})

        # ── DHW: stored hot water sensor ────────────────────────────
        if likely_type_normalized == "DHW":
            if ctl_id:
                return _merge(
                    {
                        ctl_id: {SZ_DHW_SYSTEM: {SZ_SENSOR: device_id}},
                    }
                )
            return _merge({SZ_ORPHANS_HEAT: [device_id]})

        # ── TRV / THM / RND: zone sensor ───────────────────────────
        if likely_type_normalized in ("TRV", "THM", "RND"):
            if ctl_id and resolved_zone:
                return _merge(
                    {
                        ctl_id: {
                            SZ_ZONES: {
                                resolved_zone: {SZ_SENSOR: device_id},
                            },
                        },
                    }
                )
            # No zone (with or without a ctl_id) — put in top-level
            # orphans_heat.  The TCS-level ``orphans`` list is validated by
            # ramses_rf's PARENT_RULES and only accepts BdrSwitch /
            # OtbGateway / UfhController actuators, so a TrvActuator or
            # Thermostat placed there raises SchemaInconsistentError at
            # setup time (see issue 813).
            return _merge({SZ_ORPHANS_HEAT: [device_id]})

        # ── DIS / HUM: HVAC display or humidity sensor — orphan ──────
        if likely_type_normalized in ("DIS", "HUM"):
            return _merge({SZ_ORPHANS_HVAC: [device_id]})

        # ── Default: orphan ─────────────────────────────────────────
        # Non-heat prefixes (29:, 32:, 37:, 63:, etc.) go to orphans_hvac.
        _HEAT_PREFIXES = frozenset(
            ("01:", "04:", "07:", "08:", "10:", "13:", "22:", "34:")
        )
        if device_id[:3] not in _HEAT_PREFIXES:
            return _merge({SZ_ORPHANS_HVAC: [device_id]})
        return _merge({SZ_ORPHANS_HEAT: [device_id]})

    def accept_device(
        self,
        device_id: str,
        *,
        owner: str | None = None,
        schema_entry: dict[str, Any] | None = None,
        ctl_id: str | None = None,
    ) -> DiscoveredDeviceEntry:
        """Accept a discovered device — add to schema.

        Sets status=accepted, enabled=true.  If no ``schema_entry`` is
        provided, one is auto-generated from the scan engine's
        ``likely_type`` / ``bound_to`` / ``zone_index`` data.

        The caller is still responsible for merging the schema entry
        into the config entry and calling ``discover_known_devices``.

        :param device_id: The device ID to accept.
        :param owner: Optional owner label.
        :param schema_entry: Optional schema entry override (skips auto-gen).
        :param ctl_id: Optional CTL device ID for placing devices in a TCS.
        :return: The updated device entry.
        :raise ValueError: If the device is not in the discovery list.
        """
        if (
            device_id not in {d.device_id for d in self._scan.get_devices()}
            and device_id not in self._metadata
        ):
            raise ValueError(f"Device {device_id} not in discovery list")

        meta = self._metadata.get(device_id, DeviceMetadata())
        meta.status = DiscoveryStatus.ACCEPTED
        meta.enabled = True
        meta.accepted_at = dt.now().isoformat()
        if owner is not None:
            meta.owner = owner

        # Auto-generate schema entry if not explicitly provided
        if schema_entry is not None:
            meta.schema_entry = schema_entry
        else:
            entry = self.get_device(device_id)
            dev = entry.device if entry else None
            likely_type = dev.likely_type if dev else "unknown"
            # HGI discovery candidates (from MQTT) are not in the scan
            # engine, so get_device returns a stub with likely_type=
            # "unknown".  But 18: devices are always HGIs — use the
            # prefix as the authoritative class (issue 1119).
            likely_type = self._hgi_likely_type(device_id, likely_type)
            bound_to = dev.bound_to if dev else None
            zone_index = dev.zone_index if dev else None
            domain_id = getattr(dev, "domain_id", None) if dev else None

            # Build a descriptive comment from scan engine data so the user
            # can see what the scan engine found and any ambiguity.
            # TODO(Phase 2/4): The scan engine is a passive observer that
            # guesses types from packet codes — e.g. 31DA can come from both
            # FANs and DIS devices, leading to misclassification.  The proper
            # fix is for ramses_rf's HvacVentilator.schema() to expose
            # remotes/sensors, and for _class to become a schema trait
            # (Phase 3).  Until then, the _comment trait documents the scan
            # engine's guess and the user can manually fix the schema entry.
            comment = self._build_comment(
                dev, likely_type, bound_to, zone_index
            )
            meta.schema_entry = self.generate_schema_entry(
                device_id,
                likely_type,
                bound_to=bound_to,
                zone_index=zone_index,
                ctl_id=ctl_id,
                comment=comment,
                domain_id=domain_id,
            )

        self._metadata[device_id] = meta
        _LOGGER.info("DiscoveryManager: accepted device %s", device_id)

        result = self.get_device(device_id)
        assert result is not None  # just added/updated metadata
        return result

    def discard_device(self, device_id: str) -> DiscoveredDeviceEntry:
        """Discard a discovered device — keep for spam prevention.

        Sets status=discarded, enabled=false. Device stays in the list
        so it won't trigger another notification.

        :param device_id: The device ID to discard.
        :return: The updated device entry.
        :raise ValueError: If the device is not in the discovery list.
        """
        if device_id not in self._metadata and device_id not in {
            d.device_id for d in self._scan.get_devices()
        }:
            raise ValueError(f"Device {device_id} not in discovery list")

        meta = self._metadata.get(device_id, DeviceMetadata())
        meta.status = DiscoveryStatus.DISCARDED
        meta.enabled = False
        self._metadata[device_id] = meta

        _LOGGER.info("DiscoveryManager: discarded device %s", device_id)
        result = self.get_device(device_id)
        assert result is not None  # just updated metadata
        return result

    def remove_device(self, device_id: str) -> DiscoveredDeviceEntry:
        """Remove a previously accepted device — it no longer exists.

        Sets status=removed, enabled=false. Discovery info is kept so
        it won't be re-notified if traffic is still seen.

        :param device_id: The device ID to remove.
        :return: The updated device entry.
        :raise ValueError: If the device is not in the discovery list.
        """
        if device_id not in self._metadata and device_id not in {
            d.device_id for d in self._scan.get_devices()
        }:
            raise ValueError(f"Device {device_id} not in discovery list")

        meta = self._metadata.get(device_id, DeviceMetadata())
        meta.status = DiscoveryStatus.REMOVED
        meta.enabled = False
        self._metadata[device_id] = meta
        # Clear from notified so it can be re-discovered if still present
        self._notified.discard(device_id)

        _LOGGER.info("DiscoveryManager: removed device %s", device_id)
        result = self.get_device(device_id)
        assert result is not None  # just updated metadata
        return result

    def enable_device(self, device_id: str) -> DiscoveredDeviceEntry:
        """Enable a disabled/discarded/removed device.

        Sets enabled=true without changing status. The caller is
        responsible for updating the schema and calling discover_known_devices.

        :param device_id: The device ID to enable.
        :return: The updated device entry.
        :raise ValueError: If the device is not in the discovery list.
        """
        if device_id not in self._metadata:
            raise ValueError(f"Device {device_id} not in discovery list")

        self._metadata[device_id].enabled = True
        _LOGGER.info("DiscoveryManager: enabled device %s", device_id)
        result = self.get_device(device_id)
        assert result is not None  # just updated metadata
        return result

    def disable_device(self, device_id: str) -> DiscoveredDeviceEntry:
        """Disable an accepted device — temporary exclusion.

        Sets enabled=false without changing status. Device stays in
        the schema but is excluded from device creation.

        :param device_id: The device ID to disable.
        :return: The updated device entry.
        :raise ValueError: If the device is not in the discovery list.
        """
        if device_id not in self._metadata:
            raise ValueError(f"Device {device_id} not in discovery list")

        self._metadata[device_id].enabled = False
        _LOGGER.info("DiscoveryManager: disabled device %s", device_id)
        result = self.get_device(device_id)
        assert result is not None  # just updated metadata
        return result

    def add_faked_rem(
        self,
        device_id: str,
        *,
        bound_to: str,
        alias: str | None = None,
    ) -> DiscoveredDeviceEntry:
        """Add a faked REM entry — no traffic needed.

        Creates a virtual REM device for sending commands to a FAN.
        Sets faked=true, status=accepted, enabled=true.

        :param device_id: Device ID for faked REM (valid 37: address).
        :param bound_to: The FAN device ID this REM is bound to.
        :param alias: Optional friendly name.
        :return: The created device entry.
        """
        # Build a schema fragment that:
        # 1. Creates a root entry for the REM with traits (_class, _bound,
        #    _faked, _owner)
        # 2. Adds the REM to the FAN's remotes[] list so ramses_rf knows
        #    the topology (REM → FAN binding)
        # 3. Sets _bound on the FAN pointing to the REM (canonical place
        #    for the binding — a FAN can have multiple bound REMs)
        # The REM's _bound trait tells ramses_cc which FAN this REM can
        # send 2411 commands to.  The FAN's _bound trait is the canonical
        # binding (copied from known_list's bound trait).  The remotes[]
        # list tells ramses_rf the FAN-REM topology so it creates the
        # devices correctly.
        # deep_merge(fragment, existing_schema) will union the remotes list
        # with any existing remotes — no need to read the current schema.
        fragment: dict[str, Any] = {
            device_id: {
                SZ_TR_CLASS: "REM",
                SZ_TR_BOUND: bound_to,
                SZ_TR_FAKED: True,
                SZ_TR_OWNER: "me",
            },
            bound_to: {
                "remotes": [device_id],
                SZ_TR_BOUND: device_id,
            },
        }

        meta = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            enabled=True,
            faked=True,
            owner=alias,
            accepted_at=dt.now().isoformat(),
            schema_entry=fragment,
        )
        self._metadata[device_id] = meta

        _LOGGER.info(
            "DiscoveryManager: added faked REM %s bound to %s",
            device_id,
            bound_to,
        )
        result = self.get_device(device_id)
        assert result is not None  # just added faked REM
        return result

    def check_for_new_devices(self) -> list[str]:
        """Check for new devices and send notifications if enabled.

        Called periodically by the coordinator. Returns the list of
        newly discovered device IDs (status=NEW, not yet checked).

        HGI discovery candidates (18: devices in the schema without
        ``_owner``, added by ``_MqttHgiDiscoveryCallback``) are also
        flagged as NEW here so the user gets a persistent notification
        to review and accept them (issue 1119).

        :return: List of new device IDs that were found this round.
        """
        engine_devices = {d.device_id: d for d in self._scan.get_devices()}
        new_ids: list[str] = []

        # HGI discovery candidates from MQTT are not in the scan engine
        # (HGIs are gateways, not RF devices).  Flag them as NEW so
        # they appear in the review form and trigger a notification.
        for dev_id in self._schema_no_owner_ids:
            if not self._is_hgi(dev_id):
                continue
            # Skip the active HGI — it's managed by the coordinator
            if self._active_hgi_id and dev_id == self._active_hgi_id:
                continue
            meta = self._metadata.get(dev_id)
            if meta is None:
                self._metadata[dev_id] = DeviceMetadata()
                new_ids.append(dev_id)
                _LOGGER.info(
                    "check_for_new_devices: HGI discovery candidate %s "
                    "flagged for review (issue 1119)",
                    dev_id,
                )
            elif (
                meta.status == DiscoveryStatus.NEW
                and dev_id not in self._notified
            ):
                new_ids.append(dev_id)
            elif (
                meta.status == DiscoveryStatus.ACCEPTED
                and dev_id not in self._notified
            ):
                # Previously accepted but lost its _owner (e.g. user
                # cleared _owner, or config was cleaned) — re-flag for
                # review so the user can re-accept (issue 1119).
                _LOGGER.info(
                    "check_for_new_devices: HGI %s was accepted but "
                    "has no _owner — re-flagging for review (issue 1119)",
                    dev_id,
                )
                meta.status = DiscoveryStatus.NEW
                self._metadata[dev_id] = meta
                new_ids.append(dev_id)

        for device_id in engine_devices:
            # Skip local active HGI gateway — it is managed directly by the
            # coordinator and auto-registered in the schema.  Foreign HGIs
            # (device_id != active_hgi_id) are discoverable devices.
            if self._active_hgi_id and device_id == self._active_hgi_id:
                continue
            # Skip foreign-owner devices (neighbour's devices) — the
            # scan engine sees all RF traffic, but foreign devices
            # should not be offered for discovery/review.
            if device_id in self._foreign_device_ids:
                continue
            meta = self._metadata.get(device_id)
            if meta is None:
                # If the device is already in the schema but has no metadata
                # (e.g. metadata lost during reload because .storage/ wasn't
                # updated before teardown), do NOT flag it as NEW — it's
                # already configured, not a new discovery (issue 917).
                if device_id in self._schema_device_ids:
                    _LOGGER.info(
                        "check_for_new_devices: %s is in schema but has no"
                        " metadata — suppressing NEW notification (issue 917)",
                        device_id,
                    )
                    continue
                # Brand new device — create metadata
                self._metadata[device_id] = DeviceMetadata()
                new_ids.append(device_id)
            elif (
                meta.status == DiscoveryStatus.NEW
                and device_id not in self._notified
            ):
                new_ids.append(device_id)
            elif meta.status == DiscoveryStatus.REMOVED:
                # Re-mark REMOVED devices as NEW if they're still seen
                # (e.g., user removed from schema but device is still present)
                meta.status = DiscoveryStatus.NEW
                self._metadata[device_id] = meta
                new_ids.append(device_id)

        # Mark all reported devices as notified, regardless of whether
        # a notification was actually sent (prevents re-reporting)
        self._notified.update(new_ids)

        if new_ids and self._auto_notify:
            self._send_notification(new_ids)

        return new_ids

    def check_for_lost_devices(
        self, schema: dict[str, Any] | None = None
    ) -> list[str]:
        """Check for accepted devices that haven't been seen recently.

        Marks devices as LOST if they haven't been seen for the
        configured threshold. Returns the list of newly lost device IDs.

        If *schema* is provided, devices with ``_suppress_not_seen: True``
        in their schema entry are skipped (issue 988 — the user has
        explicitly dismissed the "not seen" notification for this device).

        :param schema: The current config entry schema (with _ traits).
            If ``None``, the ``_suppress_not_seen`` check is skipped
            (backward compatibility for callers that don't pass it).
        :return: List of device IDs that were marked as lost.
        """
        now = dt.now()
        lost_ids: list[str] = []

        for device_id, meta in self._metadata.items():
            if meta.status != DiscoveryStatus.ACCEPTED or not meta.enabled:
                continue

            # Skip HGI gateways — they are not remote devices and must
            # never be flagged as lost or offered for removal (the
            # remove_device service blocks gateway removal with a
            # ServiceValidationError).  Mirrors check_orphaned_devices.
            if self._is_hgi(device_id):
                continue

            # Respect _suppress_not_seen from schema (issue 988)
            # Accepted values: True (suppress forever) or N int (suppress
            # for N days from last_seen, then re-notify).  This mirrors
            # the logic in check_orphaned_devices.
            if schema is not None:
                schema_entry = schema.get(device_id)
                if isinstance(schema_entry, dict):
                    suppress_val = schema_entry.get("_suppress_not_seen")
                    suppress_forever = suppress_val is True
                    suppress_days: int | None = None
                    if isinstance(
                        suppress_val, (int, float)
                    ) and not isinstance(suppress_val, bool):
                        suppress_days = int(suppress_val)

                    if suppress_forever or suppress_days is not None:
                        # For int mode, check if suppress period has expired
                        if suppress_days is not None:
                            engine_dev_check = next(
                                (
                                    d
                                    for d in self._scan.get_devices()
                                    if d.device_id == device_id
                                ),
                                None,
                            )
                            if engine_dev_check and engine_dev_check.last_seen:
                                try:
                                    ls = dt.fromisoformat(
                                        engine_dev_check.last_seen
                                    )
                                    if ls.tzinfo is None:
                                        ls = ls.replace(
                                            tzinfo=dt_util.get_default_time_zone()
                                        )
                                    suppress_threshold = now - td(
                                        days=suppress_days
                                    )
                                    if ls < suppress_threshold:
                                        # Suppress expired — allow lost check
                                        pass
                                    else:
                                        continue
                                except (ValueError, TypeError):
                                    pass
                        else:
                            continue

            engine_dev = next(
                (
                    d
                    for d in self._scan.get_devices()
                    if d.device_id == device_id
                ),
                None,
            )
            if engine_dev is None or not engine_dev.last_seen:
                continue

            try:
                last_seen = dt.fromisoformat(engine_dev.last_seen)
            except (ValueError, TypeError):
                continue

            days_since = (now - last_seen).days
            if days_since >= self._lost_threshold_days:
                meta.status = DiscoveryStatus.LOST
                lost_ids.append(device_id)
                _LOGGER.warning(
                    "DiscoveryManager: device %s marked lost (not seen "
                    "for %d days)",
                    device_id,
                    days_since,
                )

        if lost_ids and self._auto_notify:
            self._send_lost_notification(lost_ids)

        return lost_ids

    def stop(self) -> None:
        """Stop the scan engine and dismiss notifications."""
        self._scan.stop()
        async_dismiss_notification(self._hass, self._notification_id)
        async_dismiss_notification(self._hass, self._mismatch_notification_id)
        _LOGGER.info("DiscoveryManager: stopped")

    def _send_notification(self, new_ids: list[str]) -> None:
        """Send a persistent notification about new devices."""
        self._notified.update(new_ids)

        devices = self.get_devices()
        new_devices = [d for d in devices if d.device.device_id in new_ids]
        # HGI discovery candidates (issue 1119) may not be in the scan
        # engine, so get_devices() might not return them.  Add synthetic
        # lines for any new_ids that are missing.
        known_ids = {d.device.device_id for d in new_devices}
        missing_ids = sorted(did for did in new_ids if did not in known_ids)

        lines = [f"Found {len(new_ids)} new device(s):\n"]
        for entry in sorted(new_devices, key=lambda e: e.device.device_id):
            dev = entry.device
            line = f"- `{dev.device_id}` ({dev.likely_type}"
            if dev.confidence:
                line += f", {dev.confidence}"
            if dev.zone_index:
                line += f", zone={dev.zone_index}"
            if dev.bound_to:
                line += f", bound to {dev.bound_to}"
            if dev.is_battery:
                line += ", battery"
            line += ")"
            lines.append(line)
        for dev_id in missing_ids:
            if self._is_hgi(dev_id):
                lines.append(f"- `{dev_id}` (HGI — discovery candidate)")
            else:
                lines.append(f"- `{dev_id}`")

        lines.append(
            "\n[Review discovered devices]"
            "(/config/integrations/integration/ramses_cc)"
            " — open **Configure → Review discovered devices** to accept, "
            "decline, or skip for now."
        )
        lines.append(
            "Or call `ramses_cc.accept_discovered_device` / "
            "`ramses_cc.discard_discovered_device` services."
        )

        async_create_notification(
            self._hass,
            message="\n".join(lines),
            title="RAMSES CC: New devices discovered",
            notification_id=self._notification_id,
        )

    def _send_lost_notification(self, lost_ids: list[str]) -> None:
        """Send a persistent notification about lost devices."""
        lines = [f"{len(lost_ids)} device(s) have not been seen recently:\n"]
        for device_id in lost_ids:
            entry = self.get_device(device_id)
            if entry:
                lines.append(
                    f"- `{device_id}` ({entry.device.likely_type})"
                    f" — last seen: {entry.device.last_seen}"
                )

        lines.append("\nCheck battery or RF range, or call")
        lines.append(
            "`ramses_cc.remove_discovered_device` if the device is gone."
        )

        async_create_notification(
            self._hass,
            message="\n".join(lines),
            title="RAMSES CC: Lost devices",
            notification_id=f"{DOMAIN}_discovery_lost",
        )

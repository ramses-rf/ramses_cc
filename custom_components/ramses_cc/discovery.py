"""RAMSES CC - Passive device scan integration.

Wraps the ramses_rf DiscoveryScan engine with HA-specific concerns:
- status/enabled/owner/faked metadata (stored in HA .storage/)
- persistent notifications for new/lost devices
- service calls for accept/discard/remove/enable/disable
- schema auto-generation from accepted devices
- periodic checkpoint to .storage/

The scan engine itself (ramses_rf.discovery_scan) is read-only and
HA-agnostic. This module adds the user-facing layer.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime as dt
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
    async_dismiss as async_dismiss_notification,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN

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


@dataclass
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return {
            "status": self.status.value,
            "enabled": self.enabled,
            "faked": self.faked,
            "owner": self.owner,
            "accepted_at": self.accepted_at,
            "schema_entry": self.schema_entry,
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
        )


@dataclass
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
    ) -> None:
        """Initialize the discovery manager.

        :param hass: Home Assistant instance.
        :param scan: The ramses_rf DiscoveryScan engine instance.
        :param auto_notify: Whether to send persistent notifications for new devices.
        :param lost_threshold_days: Days without traffic before marking a device lost.
        """
        self._hass = hass
        self._scan = scan
        self._auto_notify = auto_notify
        self._lost_threshold_days = lost_threshold_days

        # device_id → metadata (persisted to .storage/)
        self._metadata: dict[str, DeviceMetadata] = {}

        # Track notified device IDs to avoid duplicate notifications
        self._notified: set[str] = set()

        # Notification ID for the "new devices" notification
        self._notification_id = f"{DOMAIN}_discovery"

        self._scan.start()
        _LOGGER.info("DiscoveryManager: started (passive scan running)")

    @property
    def scan(self) -> DiscoveryScan:
        """Return the underlying scan engine."""
        return self._scan

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

    def export_state(self) -> dict[str, Any]:
        """Export full state for persistence.

        Called on shutdown/checkpoint to save to .storage/.

        :return: Dict with device metadata and scan engine state.
        """
        return {
            SZ_DISCOVERY_DEVICES: {
                device_id: meta.to_dict() for device_id, meta in self._metadata.items()
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
                            likely_type="REM" if meta.faked else "unknown",
                            codes_seen=[],
                            bound_to=None,
                            zone_idx=None,
                            rssi=None,
                            confidence="high" if meta.faked else "low",
                            is_battery=False,
                            src_count=0,
                            dst_count=0,
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

    @staticmethod
    def generate_schema_entry(
        device_id: str,
        likely_type: str,
        *,
        bound_to: str | None = None,
        zone_idx: str | None = None,
        ctl_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate a schema fragment for a discovered device.

        Maps the scan engine's ``likely_type`` to the appropriate
        ramses_rf global schema structure.  Returns a *fragment* —
        the caller merges it into the full schema dict.

        :param device_id: The device ID (e.g. ``04:056053``).
        :param likely_type: One of CTL, TRV, DHW, OTB, BDR, FAN, REM, THM.
        :param bound_to: Optional parent device ID (for REM → FAN).
        :param zone_idx: Optional zone index (for TRV/THM in a TCS).
        :param ctl_id: Optional CTL device ID (for placing devices in a TCS).
        :return: A dict that can be deep-merged into the global schema.
        """
        from ramses_rf.schemas import (
            SZ_ACTUATORS,
            SZ_APPLIANCE_CONTROL,
            SZ_DHW_SYSTEM,
            SZ_DHW_VALVE,
            SZ_MAIN_TCS,
            SZ_ORPHANS,
            SZ_ORPHANS_HEAT,
            SZ_ORPHANS_HVAC,
            SZ_REMOTES,
            SZ_SENSOR,
            SZ_SYSTEM,
            SZ_ZONES,
        )

        lt = likely_type.upper()

        # ── CTL: Temperature Control System controller ──────────────
        if lt == "CTL":
            return {
                SZ_MAIN_TCS: device_id,
                device_id: {},
            }

        # ── FAN: HVAC VCS controller ────────────────────────────────
        if lt == "FAN":
            return {
                device_id: {SZ_REMOTES: []},
            }

        # ── REM: HVAC remote — add to parent FAN's remotes list ─────
        if lt == "REM":
            parent = bound_to or ctl_id
            if parent:
                return {
                    parent: {SZ_REMOTES: [device_id]},
                }
            return {SZ_ORPHANS_HVAC: [device_id]}

        # ── OTB: OpenTherm Bridge — appliance_control for a CTL ─────
        if lt == "OTB":
            if ctl_id:
                return {
                    ctl_id: {SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: device_id}},
                }
            return {SZ_ORPHANS_HEAT: [device_id]}

        # ── BDR: relay — DHW valve or zone actuator ─────────────────
        if lt == "BDR":
            if ctl_id and zone_idx:
                return {
                    ctl_id: {
                        SZ_ZONES: {
                            zone_idx: {SZ_ACTUATORS: [device_id]},
                        },
                    },
                }
            if ctl_id:
                # No zone — put in DHW as htg_valve
                return {
                    ctl_id: {SZ_DHW_SYSTEM: {SZ_DHW_VALVE: device_id}},
                }
            return {SZ_ORPHANS_HEAT: [device_id]}

        # ── DHW: stored hot water sensor ────────────────────────────
        if lt == "DHW":
            if ctl_id:
                return {
                    ctl_id: {SZ_DHW_SYSTEM: {SZ_SENSOR: device_id}},
                }
            return {SZ_ORPHANS_HEAT: [device_id]}

        # ── TRV / THM: zone sensor ──────────────────────────────────
        if lt in ("TRV", "THM"):
            if ctl_id and zone_idx:
                return {
                    ctl_id: {
                        SZ_ZONES: {
                            zone_idx: {SZ_SENSOR: device_id},
                        },
                    },
                }
            if ctl_id:
                # No zone — put in orphans of this TCS
                return {
                    ctl_id: {SZ_ORPHANS: [device_id]},
                }
            return {SZ_ORPHANS_HEAT: [device_id]}

        # ── Default: orphan ─────────────────────────────────────────
        return {SZ_ORPHANS_HEAT: [device_id]}

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
        ``likely_type`` / ``bound_to`` / ``zone_idx`` data.

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
            bound_to = dev.bound_to if dev else None
            zone_idx = dev.zone_idx if dev else None
            meta.schema_entry = self.generate_schema_entry(
                device_id,
                likely_type,
                bound_to=bound_to,
                zone_idx=zone_idx,
                ctl_id=ctl_id,
            )

        self._metadata[device_id] = meta
        _LOGGER.info("DiscoveryManager: accepted device %s", device_id)

        return self.get_device(device_id)  # type: ignore[return-value]

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
        return self.get_device(device_id)  # type: ignore[return-value]

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

        _LOGGER.info("DiscoveryManager: removed device %s", device_id)
        return self.get_device(device_id)  # type: ignore[return-value]

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
        return self.get_device(device_id)  # type: ignore[return-value]

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
        return self.get_device(device_id)  # type: ignore[return-value]

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

        :param device_id: The device ID for the faked REM (any valid 37: address).
        :param bound_to: The FAN device ID this REM is bound to.
        :param alias: Optional friendly name.
        :return: The created device entry.
        """
        meta = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            enabled=True,
            faked=True,
            owner=alias,
            accepted_at=dt.now().isoformat(),
            schema_entry={"class": "REM", "bound": bound_to, "faked": True},
        )
        self._metadata[device_id] = meta

        _LOGGER.info(
            "DiscoveryManager: added faked REM %s bound to %s",
            device_id,
            bound_to,
        )
        return self.get_device(device_id)  # type: ignore[return-value]

    def check_for_new_devices(self) -> list[str]:
        """Check for new devices and send notifications if enabled.

        Called periodically by the coordinator. Returns the list of
        newly discovered device IDs (status=NEW, not yet checked).

        :return: List of new device IDs that were found this round.
        """
        engine_devices = {d.device_id: d for d in self._scan.get_devices()}
        new_ids: list[str] = []

        for device_id in engine_devices:
            meta = self._metadata.get(device_id)
            if meta is None:
                # Brand new device — create metadata
                self._metadata[device_id] = DeviceMetadata()
                new_ids.append(device_id)
            elif meta.status == DiscoveryStatus.NEW and device_id not in self._notified:
                new_ids.append(device_id)

        # Mark all reported devices as notified, regardless of whether
        # a notification was actually sent (prevents re-reporting)
        self._notified.update(new_ids)

        if new_ids and self._auto_notify:
            self._send_notification(new_ids)

        return new_ids

    def check_for_lost_devices(self) -> list[str]:
        """Check for accepted devices that haven't been seen recently.

        Marks devices as LOST if they haven't been seen for the
        configured threshold. Returns the list of newly lost device IDs.

        :return: List of device IDs that were marked as lost.
        """
        now = dt.now()
        lost_ids: list[str] = []

        for device_id, meta in self._metadata.items():
            if meta.status != DiscoveryStatus.ACCEPTED or not meta.enabled:
                continue

            engine_dev = next(
                (d for d in self._scan.get_devices() if d.device_id == device_id),
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
                    "DiscoveryManager: device %s marked as lost (not seen for %d days)",
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
        _LOGGER.info("DiscoveryManager: stopped")

    def _send_notification(self, new_ids: list[str]) -> None:
        """Send a persistent notification about new devices."""
        self._notified.update(new_ids)

        devices = self.get_devices()
        new_devices = [d for d in devices if d.device.device_id in new_ids]

        lines = [f"Found {len(new_ids)} new device(s):\n"]
        for entry in sorted(new_devices, key=lambda e: e.device.device_id):
            dev = entry.device
            line = f"- `{dev.device_id}` ({dev.likely_type}"
            if dev.confidence:
                line += f", {dev.confidence}"
            if dev.zone_idx:
                line += f", zone={dev.zone_idx}"
            line += ")"
            lines.append(line)

        lines.append("\nCall `ramses_cc.get_discovered_devices` for details.")
        lines.append("Call `ramses_cc.accept_discovered_device` to add to schema.")

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
        lines.append("`ramses_cc.remove_discovered_device` if the device is gone.")

        async_create_notification(
            self._hass,
            message="\n".join(lines),
            title="RAMSES CC: Lost devices",
            notification_id=f"{DOMAIN}_discovery_lost",
        )

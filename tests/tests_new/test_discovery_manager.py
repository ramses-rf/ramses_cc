"""Tests for the ramses_cc discovery manager (passive device scan integration).

Tests verify:
- DeviceMetadata serialization/deserialization
- DiscoveryManager lifecycle (accept/discard/remove/enable/disable)
- Faked REM creation
- State export/import for persistence
- New device detection and notification
- Lost device detection
"""

from __future__ import annotations

from datetime import datetime as dt, timedelta as td
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ramses_cc.discovery import (
    DeviceMetadata,
    DiscoveryManager,
    DiscoveryStatus,
)
from ramses_rf.discovery_scan import DiscoveredDevice


def make_discovered_device(
    device_id: str = "04:056053",
    likely_type: str = "TRV",
    last_seen: str | None = None,
) -> DiscoveredDevice:
    """Create a DiscoveredDevice for testing."""
    return DiscoveredDevice(
        device_id=device_id,
        first_seen="2026-01-01T00:00:00",
        last_seen=last_seen or "2026-01-01T00:00:01",
        likely_type=likely_type,
        codes_seen=["3150"],
        bound_to="01:145038",
        zone_idx="02",
        rssi=-72.0,
        confidence="high",
        is_battery=True,
        src_count=3,
        dst_count=0,
    )


def make_mock_scan(devices: list[DiscoveredDevice] | None = None) -> MagicMock:
    """Create a mock DiscoveryScan."""
    scan = MagicMock()
    scan.get_devices.return_value = devices or []
    scan.export_json.return_value = '{"devices": []}'
    scan.import_json = MagicMock()
    scan.start = MagicMock()
    scan.stop = MagicMock()
    return scan


def make_mock_hass() -> MagicMock:
    """Create a mock HomeAssistant."""
    hass = MagicMock()
    return hass


class TestDeviceMetadata:
    """Tests for DeviceMetadata serialization."""

    def test_to_dict_defaults(self) -> None:
        meta = DeviceMetadata()
        d = meta.to_dict()
        assert d["status"] == "new"
        assert d["enabled"] is False
        assert d["faked"] is False
        assert d["owner"] is None
        assert d["accepted_at"] is None
        assert d["schema_entry"] is None

    def test_to_dict_with_values(self) -> None:
        meta = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            enabled=True,
            faked=False,
            owner="henk",
            accepted_at="2026-01-01T00:00:00",
            schema_entry={"class": "TRV"},
        )
        d = meta.to_dict()
        assert d["status"] == "accepted"
        assert d["enabled"] is True
        assert d["owner"] == "henk"
        assert d["schema_entry"] == {"class": "TRV"}

    def test_from_dict_defaults(self) -> None:
        meta = DeviceMetadata.from_dict({})
        assert meta.status == DiscoveryStatus.NEW
        assert meta.enabled is False

    def test_from_dict_with_values(self) -> None:
        meta = DeviceMetadata.from_dict(
            {
                "status": "accepted",
                "enabled": True,
                "owner": "henk",
            }
        )
        assert meta.status == DiscoveryStatus.ACCEPTED
        assert meta.enabled is True
        assert meta.owner == "henk"

    def test_from_dict_invalid_status(self) -> None:
        meta = DeviceMetadata.from_dict({"status": "invalid"})
        assert meta.status == DiscoveryStatus.NEW

    def test_round_trip(self) -> None:
        meta = DeviceMetadata(
            status=DiscoveryStatus.DISCARDED,
            enabled=False,
            owner="neighbor",
        )
        restored = DeviceMetadata.from_dict(meta.to_dict())
        assert restored.status == DiscoveryStatus.DISCARDED
        assert restored.owner == "neighbor"


class TestDiscoveryManagerLifecycle:
    """Tests for DiscoveryManager lifecycle methods."""

    def test_start_calls_scan_start(self) -> None:
        scan = make_mock_scan()
        hass = make_mock_hass()
        DiscoveryManager(hass, scan)
        assert scan.start.called

    def test_stop_calls_scan_stop(self) -> None:
        scan = make_mock_scan()
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan)
        manager.stop()
        assert scan.stop.called

    def test_accept_device(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        entry = manager.accept_device("04:056053", owner="henk")
        assert entry.metadata.status == DiscoveryStatus.ACCEPTED
        assert entry.metadata.enabled is True
        assert entry.metadata.owner == "henk"
        assert entry.metadata.accepted_at is not None

    def test_accept_device_not_found(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        with pytest.raises(ValueError, match="not in discovery list"):
            manager.accept_device("99:999999")

    def test_discard_device(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        entry = manager.discard_device("04:056053")
        assert entry.metadata.status == DiscoveryStatus.DISCARDED
        assert entry.metadata.enabled is False

    def test_remove_device(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        entry = manager.remove_device("04:056053")
        assert entry.metadata.status == DiscoveryStatus.REMOVED
        assert entry.metadata.enabled is False

    def test_enable_device(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        manager.disable_device("04:056053")
        entry = manager.enable_device("04:056053")
        assert entry.metadata.enabled is True
        assert entry.metadata.status == DiscoveryStatus.ACCEPTED

    def test_disable_device(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        entry = manager.disable_device("04:056053")
        assert entry.metadata.enabled is False
        assert entry.metadata.status == DiscoveryStatus.ACCEPTED

    def test_enable_device_not_found(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        with pytest.raises(ValueError, match="not in discovery list"):
            manager.enable_device("99:999999")


class TestFakedRem:
    """Tests for faked REM creation."""

    def test_add_faked_rem(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        entry = manager.add_faked_rem(
            "37:000001", bound_to="32:157747", alias="Living room"
        )
        assert entry.metadata.faked is True
        assert entry.metadata.status == DiscoveryStatus.ACCEPTED
        assert entry.metadata.enabled is True
        assert entry.metadata.owner == "Living room"
        assert entry.metadata.schema_entry == {
            "class": "REM",
            "bound": "32:157747",
            "faked": True,
        }

    def test_faked_rem_appears_in_get_devices(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.add_faked_rem("37:000001", bound_to="32:157747")
        devices = manager.get_devices()
        assert len(devices) == 1
        assert devices[0].device.device_id == "37:000001"
        assert devices[0].metadata.faked is True


class TestStateExportImport:
    """Tests for state export/import (persistence)."""

    def test_export_state_empty(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        state = manager.export_state()
        assert "devices" in state
        assert "scan_state" in state
        assert state["devices"] == {}

    def test_export_state_with_metadata(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        manager.accept_device("04:056053")

        state = manager.export_state()
        assert "04:056053" in state["devices"]
        assert state["devices"]["04:056053"]["status"] == "accepted"

    def test_restore_state(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        state = {
            "devices": {
                "04:056053": {
                    "status": "accepted",
                    "enabled": True,
                    "faked": False,
                    "owner": "henk",
                }
            },
            "scan_state": '{"devices": []}',
        }
        manager.restore_state(state)

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.ACCEPTED
        assert entry.metadata.owner == "henk"

    def test_restore_state_imports_scan_engine(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.restore_state(
            {
                "devices": {},
                "scan_state": '{"devices": ["test"]}',
            }
        )
        assert scan.import_json.called


class TestGetDevices:
    """Tests for get_devices filtering."""

    def test_get_all_devices(self) -> None:
        dev1 = make_discovered_device("04:056053", "TRV")
        dev2 = make_discovered_device("07:046947", "DHW")
        scan = make_mock_scan([dev1, dev2])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        devices = manager.get_devices()
        assert len(devices) == 2

    def test_filter_by_status(self) -> None:
        dev1 = make_discovered_device("04:056053")
        dev2 = make_discovered_device("07:046947")
        scan = make_mock_scan([dev1, dev2])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        manager.discard_device("07:046947")

        accepted = manager.get_devices(status=DiscoveryStatus.ACCEPTED)
        assert len(accepted) == 1
        assert accepted[0].device.device_id == "04:056053"

        discarded = manager.get_devices(status=DiscoveryStatus.DISCARDED)
        assert len(discarded) == 1
        assert discarded[0].device.device_id == "07:046947"

    def test_filter_by_enabled(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        enabled = manager.get_devices(enabled=True)
        assert len(enabled) == 1

        manager.disable_device("04:056053")
        enabled = manager.get_devices(enabled=True)
        assert len(enabled) == 0

    def test_get_single_device(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.device.device_id == "04:056053"

    def test_get_nonexistent_device(self) -> None:
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        assert manager.get_device("99:999999") is None


class TestNewDeviceDetection:
    """Tests for new device detection and notifications."""

    def test_check_for_new_devices(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        new_ids = manager.check_for_new_devices()
        assert "04:056053" in new_ids

    def test_check_no_new_devices_after_first_check(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.check_for_new_devices()
        new_ids = manager.check_for_new_devices()
        assert new_ids == []

    def test_notification_sent_when_auto_notify(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan, auto_notify=True)

        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_notify:
            manager.check_for_new_devices()
            assert mock_notify.called

    def test_no_notification_when_auto_notify_disabled(self) -> None:
        dev = make_discovered_device()
        scan = make_mock_scan([dev])
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan, auto_notify=False)

        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_notify:
            manager.check_for_new_devices()
            assert not mock_notify.called


class TestLostDeviceDetection:
    """Tests for lost device detection."""

    def test_device_marked_lost_after_threshold(self) -> None:
        old_date = (dt.now() - td(days=10)).isoformat()
        dev = make_discovered_device(last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(
            make_mock_hass(), scan, auto_notify=False, lost_threshold_days=7
        )

        manager.accept_device("04:056053")
        lost_ids = manager.check_for_lost_devices()
        assert "04:056053" in lost_ids

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.LOST

    def test_recent_device_not_lost(self) -> None:
        recent_date = (dt.now() - td(days=2)).isoformat()
        dev = make_discovered_device(last_seen=recent_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(
            make_mock_hass(), scan, auto_notify=False, lost_threshold_days=7
        )

        manager.accept_device("04:056053")
        lost_ids = manager.check_for_lost_devices()
        assert lost_ids == []

    def test_non_accepted_device_not_checked(self) -> None:
        old_date = (dt.now() - td(days=10)).isoformat()
        dev = make_discovered_device(last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(
            make_mock_hass(), scan, auto_notify=False, lost_threshold_days=7
        )

        # Don't accept — just discard
        manager.discard_device("04:056053")
        lost_ids = manager.check_for_lost_devices()
        assert lost_ids == []

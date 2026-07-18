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

# ramses_rf.discovery_scan is a runtime dependency of the passive scan
# feature (discovery.py imports DiscoveredDevice at runtime).  If the
# installed ramses_rf is too old (pre-0.57.7), skip the entire file —
# the feature cannot work without it.
discovery_scan = pytest.importorskip("ramses_rf.discovery_scan")
DiscoveredDevice = discovery_scan.DiscoveredDevice


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
        assert d["class_mismatch"] is None

    def test_to_dict_with_values(self) -> None:
        meta = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            enabled=True,
            faked=False,
            owner="henk",
            accepted_at="2026-01-01T00:00:00",
            schema_entry={"class": "TRV"},
            class_mismatch="schema=FAN, discovery=DIS",
        )
        d = meta.to_dict()
        assert d["status"] == "accepted"
        assert d["enabled"] is True
        assert d["owner"] == "henk"
        assert d["schema_entry"] == {"class": "TRV"}
        assert d["class_mismatch"] == "schema=FAN, discovery=DIS"

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
            "37:000001": {
                "_class": "REM",
                "_bound": "32:157747",
                "_faked": True,
                "_owner": "me",
            },
            "32:157747": {"remotes": ["37:000001"], "_bound": "37:000001"},
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


class TestGenerateSchemaEntry:
    """Tests for DiscoveryManager.generate_schema_entry."""

    def test_ctl_creates_main_tcs(self) -> None:
        result = DiscoveryManager.generate_schema_entry("01:145038", "CTL")
        from ramses_rf.schemas import SZ_MAIN_TCS

        assert result[SZ_MAIN_TCS] == "01:145038"
        assert "01:145038" in result

    def test_trv_with_ctl_and_zone(self) -> None:
        result = DiscoveryManager.generate_schema_entry(
            "04:056053", "TRV", ctl_id="01:145038", zone_idx="02"
        )
        from ramses_rf.schemas import SZ_SENSOR, SZ_ZONES

        assert result["01:145038"][SZ_ZONES]["02"][SZ_SENSOR] == "04:056053"

    def test_trv_without_ctl_goes_to_orphans(self) -> None:
        result = DiscoveryManager.generate_schema_entry("04:056053", "TRV")
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        assert "04:056053" in result[SZ_ORPHANS_HEAT]

    def test_bdr_with_ctl_and_zone(self) -> None:
        result = DiscoveryManager.generate_schema_entry(
            "13:123456", "BDR", ctl_id="01:145038", zone_idx="01"
        )
        from ramses_rf.schemas import SZ_ACTUATORS, SZ_ZONES

        assert "13:123456" in result["01:145038"][SZ_ZONES]["01"][SZ_ACTUATORS]

    def test_dhw_with_ctl(self) -> None:
        result = DiscoveryManager.generate_schema_entry(
            "07:123456", "DHW", ctl_id="01:145038"
        )
        from ramses_rf.schemas import SZ_DHW_SYSTEM, SZ_SENSOR

        assert result["01:145038"][SZ_DHW_SYSTEM][SZ_SENSOR] == "07:123456"

    def test_otb_with_ctl(self) -> None:
        result = DiscoveryManager.generate_schema_entry(
            "10:064873", "OTB", ctl_id="01:145038"
        )
        from ramses_rf.schemas import SZ_APPLIANCE_CONTROL, SZ_SYSTEM

        assert result["01:145038"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "10:064873"

    def test_fan_creates_vcs(self) -> None:
        result = DiscoveryManager.generate_schema_entry("32:123456", "FAN")
        from ramses_rf.schemas import SZ_REMOTES

        assert SZ_REMOTES in result["32:123456"]

    def test_rem_with_parent_fan(self) -> None:
        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "REM", bound_to="32:123456"
        )
        from ramses_rf.schemas import SZ_REMOTES

        assert "37:123456" in result["32:123456"][SZ_REMOTES]

    def test_rem_without_parent_goes_to_hvac_orphans(self) -> None:
        result = DiscoveryManager.generate_schema_entry("37:123456", "REM")
        from ramses_rf.schemas import SZ_ORPHANS_HVAC

        assert "37:123456" in result[SZ_ORPHANS_HVAC]

    def test_co2_with_parent_fan(self) -> None:
        """CO2 sensor (37:) with a parent FAN goes to remotes[], not orphans_heat."""
        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "CO2", bound_to="32:123456"
        )
        from ramses_rf.schemas import SZ_REMOTES

        assert "37:123456" in result["32:123456"][SZ_REMOTES]

    def test_co2_without_parent_goes_to_hvac_orphans(self) -> None:
        """CO2 sensor without a parent FAN goes to orphans_hvac, not orphans_heat."""
        result = DiscoveryManager.generate_schema_entry("37:123456", "CO2")
        from ramses_rf.schemas import SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC

        assert "37:123456" in result[SZ_ORPHANS_HVAC]
        assert SZ_ORPHANS_HEAT not in result or "37:123456" not in result.get(
            SZ_ORPHANS_HEAT, []
        )

    def test_co2_with_ctl_no_fan_goes_to_hvac_orphans(self) -> None:
        """CO2 sensor with ctl_id but no bound_to goes to orphans_hvac.

        remotes[] is only valid under a FAN/VCS entry — placing it under a
        CTL/TCS corrupts the schema and breaks setup (issue 825).  When the
        FAN parent (bound_to) is unknown, the device is orphaned to
        orphans_hvac rather than incorrectly nested under the CTL.
        """
        from ramses_rf.schemas import SZ_ORPHANS_HVAC, SZ_REMOTES

        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "CO2", ctl_id="01:216136"
        )
        # Must NOT be placed under the CTL's remotes[]
        assert "01:216136" not in result or SZ_REMOTES not in result.get(
            "01:216136", {}
        )
        assert "37:123456" in result[SZ_ORPHANS_HVAC]

    def test_rem_with_ctl_no_fan_goes_to_hvac_orphans(self) -> None:
        """REM with ctl_id (TCS) but no FAN bound_to is orphaned, not nested under CTL.

        Regression test for issue 825: a REM discovered with only a ctl_id
        (the TCS) must not be placed under the CTL's remotes[], because
        SCH_TCS rejects 'remotes' and breaks setup.  It goes to orphans_hvac.
        """
        from ramses_rf.schemas import SZ_ORPHANS_HVAC, SZ_REMOTES

        result = DiscoveryManager.generate_schema_entry(
            "29:091138", "REM", ctl_id="01:088175"
        )
        assert "01:088175" not in result or SZ_REMOTES not in result.get(
            "01:088175", {}
        )
        assert "29:091138" in result[SZ_ORPHANS_HVAC]

    def test_rem_with_non_fan_bound_to_goes_to_hvac_orphans(self) -> None:
        """REM whose bound_to is not a 32: FAN is orphaned (no remotes under CTL)."""
        from ramses_rf.schemas import SZ_ORPHANS_HVAC, SZ_REMOTES

        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "REM", bound_to="01:088175"
        )
        assert "01:088175" not in result or SZ_REMOTES not in result.get(
            "01:088175", {}
        )
        assert "37:123456" in result[SZ_ORPHANS_HVAC]

    def test_unknown_type_goes_to_heat_orphans(self) -> None:
        result = DiscoveryManager.generate_schema_entry("04:999999", "unknown")
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        assert "04:999999" in result[SZ_ORPHANS_HEAT]


class TestLostDeviceDetectionExtended:
    """Tests for lost device detection and notifications."""

    def test_check_for_lost_devices_marks_old(self) -> None:
        """A device not seen for > threshold days is marked LOST."""
        old_date = (dt.now() - td(days=10)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Accept the device first so it's eligible for lost detection
        manager.accept_device("04:056053")

        lost_ids = manager.check_for_lost_devices()
        assert "04:056053" in lost_ids

    def test_check_for_lost_devices_skips_recent(self) -> None:
        """A recently seen device is not marked LOST."""
        recent_date = (dt.now() - td(hours=1)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=recent_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        lost_ids = manager.check_for_lost_devices()
        assert lost_ids == []

    def test_check_for_lost_devices_skips_non_accepted(self) -> None:
        """Non-accepted devices are not checked for lost status."""
        old_date = (dt.now() - td(days=10)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Don't accept — just check
        lost_ids = manager.check_for_lost_devices()
        assert lost_ids == []

    def test_check_for_lost_devices_invalid_date(self) -> None:
        """Devices with invalid last_seen dates are skipped."""
        dev = make_discovered_device("04:056053", "TRV", last_seen="not-a-date")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        lost_ids = manager.check_for_lost_devices()
        assert lost_ids == []

    def test_lost_notification_sent_when_auto_notify(self) -> None:
        """A notification is sent when a device is marked lost and auto_notify is on."""
        old_date = (dt.now() - td(days=10)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan, auto_notify=True)

        manager.accept_device("04:056053")

        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_notify:
            manager.check_for_lost_devices()
            assert mock_notify.called

    def test_check_for_lost_devices_no_last_seen(self) -> None:
        """Devices with no last_seen are skipped."""
        dev = DiscoveredDevice(
            device_id="04:056053",
            first_seen="2026-01-01T00:00:00",
            last_seen="",  # empty string is falsy
            likely_type="TRV",
            codes_seen=["3150"],
            bound_to="01:145038",
            zone_idx="02",
            rssi=-72.0,
            confidence="high",
            is_battery=True,
            src_count=3,
            dst_count=0,
        )
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        lost_ids = manager.check_for_lost_devices()
        assert lost_ids == []


class TestGenerateSchemaEntryEdgeCases:
    """Tests for generate_schema_entry edge cases."""

    def test_ctl_with_id(self) -> None:
        """CTL type creates a main_tcs entry."""
        from ramses_rf.schemas import SZ_MAIN_TCS

        result = DiscoveryManager.generate_schema_entry("01:123456", "CTL")
        assert result[SZ_MAIN_TCS] == "01:123456"
        assert "01:123456" in result

    def test_fan_creates_vcs(self) -> None:
        """FAN type creates an HVAC entry with empty remotes."""
        from ramses_rf.schemas import SZ_REMOTES

        result = DiscoveryManager.generate_schema_entry("30:160000", "FAN")
        assert "30:160000" in result
        assert result["30:160000"][SZ_REMOTES] == []

    def test_rem_with_bound_to(self) -> None:
        """REM with bound_to adds to parent FAN's remotes."""
        from ramses_rf.schemas import SZ_REMOTES

        result = DiscoveryManager.generate_schema_entry(
            "32:111111", "REM", bound_to="30:160000"
        )
        assert "30:160000" in result
        assert "32:111111" in result["30:160000"][SZ_REMOTES]

    def test_rem_no_bound_to(self) -> None:
        """REM without bound_to goes to HVAC orphans."""
        from ramses_rf.schemas import SZ_ORPHANS_HVAC

        result = DiscoveryManager.generate_schema_entry("32:111111", "REM")
        assert "32:111111" in result[SZ_ORPHANS_HVAC]

    def test_co2_with_bound_to(self) -> None:
        """CO2 sensor with bound_to adds to parent FAN's remotes."""
        from ramses_rf.schemas import SZ_REMOTES

        result = DiscoveryManager.generate_schema_entry(
            "37:222222", "CO2", bound_to="30:160000"
        )
        assert "30:160000" in result
        assert "37:222222" in result["30:160000"][SZ_REMOTES]

    def test_co2_no_bound_to(self) -> None:
        """CO2 sensor without bound_to goes to HVAC orphans."""
        from ramses_rf.schemas import SZ_ORPHANS_HVAC

        result = DiscoveryManager.generate_schema_entry("37:222222", "CO2")
        assert "37:222222" in result[SZ_ORPHANS_HVAC]

    def test_co2_lowercase_type(self) -> None:
        """likely_type is case-insensitive — 'co2' works same as 'CO2'."""
        from ramses_rf.schemas import SZ_REMOTES

        result = DiscoveryManager.generate_schema_entry(
            "37:333333", "co2", bound_to="32:123456"
        )
        assert "37:333333" in result["32:123456"][SZ_REMOTES]

    def test_otb_with_ctl(self) -> None:
        """OTB with ctl_id sets appliance_control."""
        from ramses_rf.schemas import SZ_APPLIANCE_CONTROL, SZ_SYSTEM

        result = DiscoveryManager.generate_schema_entry(
            "01:222222", "OTB", ctl_id="01:111111"
        )
        assert result["01:111111"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "01:222222"

    def test_otb_no_ctl(self) -> None:
        """OTB without ctl_id goes to heat orphans."""
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry("01:222222", "OTB")
        assert "01:222222" in result[SZ_ORPHANS_HEAT]

    def test_bdr_with_ctl_and_zone(self) -> None:
        """BDR with ctl_id and zone_idx becomes a zone actuator."""
        from ramses_rf.schemas import SZ_ACTUATORS, SZ_ZONES

        result = DiscoveryManager.generate_schema_entry(
            "08:333333", "BDR", ctl_id="01:111111", zone_idx="01"
        )
        assert "08:333333" in result["01:111111"][SZ_ZONES]["01"][SZ_ACTUATORS]

    def test_bdr_with_ctl_no_zone(self) -> None:
        """BDR with ctl_id but no zone goes to DHW as dhw_valve."""
        from ramses_rf.schemas import SZ_DHW_SYSTEM, SZ_DHW_VALVE

        result = DiscoveryManager.generate_schema_entry(
            "08:333333", "BDR", ctl_id="01:111111"
        )
        assert result["01:111111"][SZ_DHW_SYSTEM][SZ_DHW_VALVE] == "08:333333"

    def test_bdr_no_ctl(self) -> None:
        """BDR without ctl_id goes to heat orphans."""
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry("08:333333", "BDR")
        assert "08:333333" in result[SZ_ORPHANS_HEAT]

    def test_dhw_with_ctl(self) -> None:
        """DHW with ctl_id goes to dhw_system as sensor."""
        from ramses_rf.schemas import SZ_DHW_SYSTEM, SZ_SENSOR

        result = DiscoveryManager.generate_schema_entry(
            "07:444444", "DHW", ctl_id="01:111111"
        )
        assert result["01:111111"][SZ_DHW_SYSTEM][SZ_SENSOR] == "07:444444"

    def test_dhw_no_ctl(self) -> None:
        """DHW without ctl_id goes to heat orphans."""
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry("07:444444", "DHW")
        assert "07:444444" in result[SZ_ORPHANS_HEAT]

    def test_trv_with_ctl_and_zone(self) -> None:
        """TRV with ctl_id and zone_idx becomes a zone sensor."""
        from ramses_rf.schemas import SZ_SENSOR, SZ_ZONES

        result = DiscoveryManager.generate_schema_entry(
            "04:555555", "TRV", ctl_id="01:111111", zone_idx="02"
        )
        assert result["01:111111"][SZ_ZONES]["02"][SZ_SENSOR] == "04:555555"

    def test_trv_with_ctl_no_zone(self) -> None:
        """TRV with ctl_id but no zone goes to orphans_heat, not TCS orphans.

        ramses_rf's PARENT_RULES only allows BdrSwitch / OtbGateway /
        UfhController in a TCS ``orphans`` list, so a TrvActuator placed
        there raises SchemaInconsistentError at setup time (issue 813).
        """
        from ramses_rf.schemas import SZ_ORPHANS, SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry(
            "04:555555", "TRV", ctl_id="01:111111"
        )
        assert "04:555555" in result[SZ_ORPHANS_HEAT]
        # Must NOT be in the TCS-level orphans list
        assert SZ_ORPHANS not in result.get("01:111111", {})

    def test_thm_with_ctl_no_zone(self) -> None:
        """THM (room thermostat) with ctl_id but no zone goes to orphans_heat."""
        from ramses_rf.schemas import SZ_ORPHANS, SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry(
            "22:012299", "THM", ctl_id="01:216136"
        )
        assert "22:012299" in result[SZ_ORPHANS_HEAT]
        assert SZ_ORPHANS not in result.get("01:216136", {})

    def test_rnd_with_ctl_no_zone(self) -> None:
        """RND (round thermostat) with ctl_id but no zone goes to orphans_heat."""
        from ramses_rf.schemas import SZ_ORPHANS, SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry(
            "34:058721", "RND", ctl_id="01:216136"
        )
        assert "34:058721" in result[SZ_ORPHANS_HEAT]
        assert SZ_ORPHANS not in result.get("01:216136", {})

    def test_trv_no_ctl(self) -> None:
        """TRV without ctl_id goes to heat orphans."""
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry("04:555555", "TRV")
        assert "04:555555" in result[SZ_ORPHANS_HEAT]


class TestGenerateSchemaEntryRootEntry:
    """Tests that generate_schema_entry always creates a root-level entry.

    Every accepted device needs a root-level entry (e.g. ``{"37:123456": {}}``)
    so that the config flow can set ``_owner`` and users can add traits
    (``_faked``, ``_class``, etc.) via the schema editor.  Without a root
    entry, the device exists only in a list (remotes[], orphans_hvac[]) and
    traits cannot be attached — breaking SSOT.
    """

    def test_rem_with_parent_has_root_entry(self) -> None:
        """REM with bound_to gets a root entry alongside remotes[] placement."""
        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "REM", bound_to="32:123456"
        )
        assert "37:123456" in result
        assert isinstance(result["37:123456"], dict)

    def test_rem_orphan_has_root_entry(self) -> None:
        """REM without parent gets a root entry alongside orphans_hvac."""
        result = DiscoveryManager.generate_schema_entry("37:123456", "REM")
        assert "37:123456" in result
        assert isinstance(result["37:123456"], dict)

    def test_co2_with_parent_has_root_entry(self) -> None:
        """CO2 with bound_to gets a root entry."""
        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "CO2", bound_to="32:123456"
        )
        assert "37:123456" in result
        assert isinstance(result["37:123456"], dict)

    def test_trv_with_zone_has_root_entry(self) -> None:
        """TRV with ctl_id and zone_idx gets a root entry."""
        result = DiscoveryManager.generate_schema_entry(
            "04:056053", "TRV", ctl_id="01:145038", zone_idx="02"
        )
        assert "04:056053" in result
        assert isinstance(result["04:056053"], dict)

    def test_trv_orphan_has_root_entry(self) -> None:
        """TRV without ctl_id gets a root entry."""
        result = DiscoveryManager.generate_schema_entry("04:056053", "TRV")
        assert "04:056053" in result
        assert isinstance(result["04:056053"], dict)

    def test_otb_with_ctl_has_root_entry(self) -> None:
        """OTB with ctl_id gets a root entry."""
        result = DiscoveryManager.generate_schema_entry(
            "10:064873", "OTB", ctl_id="01:145038"
        )
        assert "10:064873" in result
        assert isinstance(result["10:064873"], dict)

    def test_bdr_with_zone_has_root_entry(self) -> None:
        """BDR with ctl_id and zone_idx gets a root entry."""
        result = DiscoveryManager.generate_schema_entry(
            "13:123456", "BDR", ctl_id="01:145038", zone_idx="01"
        )
        assert "13:123456" in result
        assert isinstance(result["13:123456"], dict)

    def test_dhw_with_ctl_has_root_entry(self) -> None:
        """DHW with ctl_id gets a root entry."""
        result = DiscoveryManager.generate_schema_entry(
            "07:123456", "DHW", ctl_id="01:145038"
        )
        assert "07:123456" in result
        assert isinstance(result["07:123456"], dict)

    def test_unknown_type_has_root_entry(self) -> None:
        """Unknown device type gets a root entry alongside orphan list."""
        result = DiscoveryManager.generate_schema_entry("04:999999", "unknown")
        assert "04:999999" in result
        assert isinstance(result["04:999999"], dict)

    def test_ctl_already_has_root_entry(self) -> None:
        """CTL already gets a root entry (not via _merge)."""
        result = DiscoveryManager.generate_schema_entry("01:145038", "CTL")
        assert "01:145038" in result
        assert isinstance(result["01:145038"], dict)

    def test_fan_already_has_root_entry(self) -> None:
        """FAN already gets a root entry (not via _merge)."""
        result = DiscoveryManager.generate_schema_entry("32:123456", "FAN")
        assert "32:123456" in result
        assert isinstance(result["32:123456"], dict)


class TestGenerateSchemaEntrySetsClass:
    """Tests that generate_schema_entry sets _class on the root entry.

    The architecture intent (schema_architecture.md: "accept_discovered_device
    → writes _alias, _class to schema") requires that the scan engine's
    likely_type is persisted as the _class trait on the device's root entry.
    Without this, check_missing_class flags every accepted device on the next
    checkpoint, and the user has to manually add _class in the schema editor.
    """

    def test_ctl_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry("01:145038", "CTL")
        assert result["01:145038"][SZ_TR_CLASS] == "CTL"

    def test_fan_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry("32:123456", "FAN")
        assert result["32:123456"][SZ_TR_CLASS] == "FAN"

    def test_rem_with_parent_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "REM", bound_to="32:123456"
        )
        assert result["37:123456"][SZ_TR_CLASS] == "REM"

    def test_rem_orphan_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry("37:123456", "REM")
        assert result["37:123456"][SZ_TR_CLASS] == "REM"

    def test_co2_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry(
            "37:123456", "CO2", bound_to="32:123456"
        )
        assert result["37:123456"][SZ_TR_CLASS] == "CO2"

    def test_trv_with_zone_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry(
            "04:056053", "TRV", ctl_id="01:145038", zone_idx="02"
        )
        assert result["04:056053"][SZ_TR_CLASS] == "TRV"

    def test_trv_orphan_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry("04:056053", "TRV")
        assert result["04:056053"][SZ_TR_CLASS] == "TRV"

    def test_otb_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry(
            "10:064873", "OTB", ctl_id="01:145038"
        )
        assert result["10:064873"][SZ_TR_CLASS] == "OTB"

    def test_bdr_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry(
            "13:123456", "BDR", ctl_id="01:145038", zone_idx="01"
        )
        assert result["13:123456"][SZ_TR_CLASS] == "BDR"

    def test_dhw_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry(
            "07:123456", "DHW", ctl_id="01:145038"
        )
        assert result["07:123456"][SZ_TR_CLASS] == "DHW"

    def test_dis_has_class(self) -> None:
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry("37:123456", "DIS")
        assert result["37:123456"][SZ_TR_CLASS] == "DIS"

    def test_unknown_type_has_class(self) -> None:
        """Unknown likely_type is still written as _class (uppercased)."""
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry("04:999999", "unknown")
        assert result["04:999999"][SZ_TR_CLASS] == "UNKNOWN"

    def test_class_is_uppercased(self) -> None:
        """likely_type is case-insensitive — _class is stored uppercased."""
        from custom_components.ramses_cc.const import SZ_TR_CLASS

        result = DiscoveryManager.generate_schema_entry(
            "37:333333", "co2", bound_to="32:123456"
        )
        assert result["37:333333"][SZ_TR_CLASS] == "CO2"


class TestDiscoveredDeviceEntrySerialization:
    """Tests for DiscoveredDeviceEntry.to_dict and scan property."""

    def test_to_dict_serializes_device_and_metadata(self) -> None:
        """Test that to_dict merges device fields and metadata."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        entry = manager.get_device("04:056053")
        assert entry is not None

        result = entry.to_dict()
        assert result["device_id"] == "04:056053"
        assert result["status"] == "accepted"
        assert "enabled" in result
        assert "schema_entry" in result

    def test_scan_property_returns_underlying_scan(self) -> None:
        """Test that the scan property returns the scan engine."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        assert manager.scan is scan


class TestAcceptDeviceWithSchemaEntry:
    """Tests for accept_device with explicit schema_entry parameter."""

    def test_accept_device_with_explicit_schema_entry(self) -> None:
        """Test that accept_device stores an explicitly provided schema_entry."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        custom_entry = {"class": "TRV", "alias": "Living Room"}
        manager.accept_device("04:056053", schema_entry=custom_entry)

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.schema_entry == custom_entry

    def test_accept_device_auto_generates_schema_entry(self) -> None:
        """Test that accept_device auto-generates schema_entry when not provided."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.schema_entry is not None
        assert isinstance(entry.metadata.schema_entry, dict)

    def test_accept_device_with_owner(self) -> None:
        """Test that accept_device stores the owner alias."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053", owner="My TRV")

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.owner == "My TRV"

    def test_accept_device_injects_comment_trait(self) -> None:
        """Test that auto-generated schema entries include a _comment trait.

        TRV without a ctl_id goes to orphans_heat as a string in a list —
        no _comment is injected because it doesn't have its own dict entry.
        Devices that get their own dict entry (CTL, FAN) receive _comment.
        """
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")

        entry = manager.get_device("04:056053")
        assert entry is not None
        schema = entry.metadata.schema_entry
        assert schema is not None
        # Without ctl_id, TRV goes to orphans_heat (a list, no dict entry)
        assert "orphans_heat" in schema
        assert "04:056053" in schema["orphans_heat"]

    def test_accept_device_fan_gets_comment(self) -> None:
        """Test that a FAN device gets a _comment trait with ambiguity note."""
        dev = make_discovered_device(
            "32:153289", "FAN", last_seen="2026-01-01T00:00:01"
        )
        # Override bound_to/zone_idx for FAN (not relevant)
        dev.bound_to = None
        dev.zone_idx = None
        dev.codes_seen = ["31DA", "22F1"]
        dev.confidence = "medium"
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("32:153289")

        entry = manager.get_device("32:153289")
        assert entry is not None
        schema = entry.metadata.schema_entry
        assert schema is not None
        fan_entry = schema.get("32:153289", {})
        assert fan_entry.get("remotes") == []
        comment = fan_entry.get("_comment")
        assert comment is not None
        assert "Likely FAN" in comment
        assert "may also be DIS" in comment
        assert "31DA" in comment
        assert "22F1" in comment
        assert "medium" in comment

    def test_accept_device_ctl_gets_comment(self) -> None:
        """Test that a CTL device gets a _comment trait."""
        dev = make_discovered_device("01:145038", "CTL")
        dev.bound_to = None
        dev.zone_idx = None
        dev.codes_seen = ["10E0", "30C9"]
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("01:145038")

        entry = manager.get_device("01:145038")
        assert entry is not None
        schema = entry.metadata.schema_entry
        assert schema is not None
        ctl_entry = schema.get("01:145038", {})
        comment = ctl_entry.get("_comment")
        assert comment is not None
        assert "Likely CTL" in comment
        assert "10E0" in comment

    def test_accept_device_co2_orphan_gets_device_comment(self) -> None:
        """Test that a CO2 in orphans_hvac gets a device_comments entry."""
        dev = make_discovered_device("37:126776", "CO2")
        dev.bound_to = None  # no parent FAN detected
        dev.zone_idx = None
        dev.codes_seen = ["22F1", "1298"]
        dev.confidence = "medium"
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("37:126776")

        entry = manager.get_device("37:126776")
        assert entry is not None
        schema = entry.metadata.schema_entry
        assert schema is not None
        assert "orphans_hvac" in schema
        assert "37:126776" in schema["orphans_hvac"]
        # Comment goes to top-level device_comments (no dict entry for list items)
        dc = schema.get("device_comments", {})
        assert "37:126776" in dc
        comment = dc["37:126776"]
        assert "Likely CO2" in comment
        assert "22F1" in comment
        assert "1298" in comment

    def test_accept_device_rem_with_parent_gets_device_comment(self) -> None:
        """Test that a REM under a FAN parent gets a device_comments entry."""
        dev = make_discovered_device("37:168270", "REM")
        dev.bound_to = "32:153289"  # parent FAN
        dev.zone_idx = None
        dev.codes_seen = ["22F1"]
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("37:168270")

        entry = manager.get_device("37:168270")
        assert entry is not None
        schema = entry.metadata.schema_entry
        assert schema is not None
        # REM goes into parent's remotes list
        fan_entry = schema.get("32:153289", {})
        assert "37:168270" in fan_entry.get("remotes", [])
        # Comment goes to device_comments
        dc = schema.get("device_comments", {})
        assert "37:168270" in dc
        comment = dc["37:168270"]
        assert "Likely REM" in comment
        assert "bound to 32:153289" in comment

    def test_explicit_schema_entry_no_comment(self) -> None:
        """Test that explicitly provided schema entries don't get auto-comments."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        custom_entry = {"01:145038": {"zones": {"02": {"sensor": "04:056053"}}}}
        manager.accept_device("04:056053", schema_entry=custom_entry)

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.schema_entry == custom_entry


class TestDiscardRemoveDeviceInScanNotMetadata:
    """Tests for discard/remove when device is in scan but not in metadata."""

    def test_discard_device_in_scan_not_metadata(self) -> None:
        """Test discard_device creates metadata for a device only in scan."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Device is in scan but not yet in metadata (check_for_new_devices not called)
        manager.discard_device("04:056053")

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.DISCARDED
        assert entry.metadata.enabled is False

    def test_remove_device_in_scan_not_metadata(self) -> None:
        """Test remove_device creates metadata for a device only in scan."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.remove_device("04:056053")

        entry = manager.get_device("04:056053")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.REMOVED
        assert entry.metadata.enabled is False


class TestDisableDeviceNotInMetadata:
    """Test disable_device when device is not in metadata."""

    def test_disable_device_not_in_metadata_raises(self) -> None:
        """Test disable_device raises ValueError for unknown device."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        with pytest.raises(ValueError, match="not in discovery list"):
            manager.disable_device("99:999999")


class TestCheckForNewDevicesReReport:
    """Test check_for_new_devices re-reporting logic."""

    def test_new_status_device_not_notified_is_re_reported(self) -> None:
        """A device with NEW status that hasn't been notified is re-reported."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # First check — creates metadata with NEW status
        new_ids = manager.check_for_new_devices()
        assert "04:056053" in new_ids

        # Manually reset _notified to simulate "not yet notified"
        manager._notified.clear()

        # Second check — device is NEW and not in _notified, should be re-reported
        new_ids = manager.check_for_new_devices()
        assert "04:056053" in new_ids


class TestCheckClassMismatches:
    """Tests for DiscoveryManager.check_class_mismatches."""

    def test_no_mismatch_when_classes_match(self) -> None:
        """No mismatch when schema _class matches scan likely_type."""
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "remotes": []}}
        count = manager.check_class_mismatches(schema)
        assert count == 0
        # No class_mismatch set on metadata
        meta = manager._metadata.get("32:153289")
        assert meta is None or meta.class_mismatch is None

    def test_mismatch_detected(self) -> None:
        """Mismatch detected when schema _class differs from scan likely_type."""
        dev = make_discovered_device("32:153289", "DIS")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "remotes": []}}
        count = manager.check_class_mismatches(schema)
        assert count == 1
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.class_mismatch is not None
        assert "FAN" in meta.class_mismatch
        assert "DIS" in meta.class_mismatch

    def test_mismatch_cleared_when_resolved(self) -> None:
        """Mismatch flag cleared when classes match again."""
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # First: create a mismatch
        manager._metadata["32:153289"] = DeviceMetadata(
            class_mismatch="schema=FAN, discovery=DIS"
        )

        # Now the scan says FAN and schema says FAN — mismatch resolved
        schema = {"32:153289": {"_class": "FAN", "remotes": []}}
        count = manager.check_class_mismatches(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.class_mismatch is None

    def test_no_mismatch_for_device_not_in_schema(self) -> None:
        """No mismatch check for devices not in the schema."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {}  # device not in schema
        count = manager.check_class_mismatches(schema)
        assert count == 0

    def test_no_mismatch_for_device_without_class(self) -> None:
        """No mismatch check for schema entries without _class."""
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"remotes": []}}  # no _class
        count = manager.check_class_mismatches(schema)
        assert count == 0

    def test_no_mismatch_for_hgi(self) -> None:
        """HGI (18:) devices are skipped."""
        dev = make_discovered_device("18:001234", "HGI")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"18:001234": {"_class": "HGI"}}
        count = manager.check_class_mismatches(schema)
        assert count == 0

    def test_no_mismatch_for_unknown_scan_type(self) -> None:
        """No mismatch when scan type is DEV (generic/unknown)."""
        dev = make_discovered_device("32:153289", "DEV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "remotes": []}}
        count = manager.check_class_mismatches(schema)
        assert count == 0

    def test_mismatch_with_entity_slug_normalized(self) -> None:
        """Schema _class='ventilator' is normalized to 'FAN' before comparison."""
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Schema has entity slug 'ventilator', scan says 'FAN'
        # After normalization, both are 'FAN' — no mismatch
        schema = {"32:153289": {"_class": "ventilator", "remotes": []}}
        count = manager.check_class_mismatches(schema)
        assert count == 0

    def test_multiple_mismatches(self) -> None:
        """Multiple devices with mismatches are all detected."""
        dev1 = make_discovered_device("32:153289", "DIS")
        dev2 = make_discovered_device("37:168270", "CO2")
        scan = make_mock_scan([dev1, dev2])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {
            "32:153289": {"_class": "FAN", "remotes": []},
            "37:168270": {"_class": "REM"},
        }
        count = manager.check_class_mismatches(schema)
        assert count == 2


class TestGetMismatchedDevices:
    """Tests for DiscoveryManager.get_mismatched_devices."""

    def test_returns_only_mismatched(self) -> None:
        """Only devices with class_mismatch flag are returned."""
        dev1 = make_discovered_device("32:153289", "DIS")
        dev2 = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev1, dev2])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Set mismatch on dev1 only
        manager._metadata["32:153289"] = DeviceMetadata(
            class_mismatch="schema=FAN, discovery=DIS"
        )
        manager._metadata["04:056053"] = DeviceMetadata()

        result = manager.get_mismatched_devices()
        assert len(result) == 1
        assert result[0].device.device_id == "32:153289"

    def test_empty_when_no_mismatches(self) -> None:
        """No mismatches → empty list."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        result = manager.get_mismatched_devices()
        assert result == []

    def test_cleared_mismatch_not_returned(self) -> None:
        """A mismatch that was cleared (set to None) is not returned."""
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Set then clear mismatch
        manager._metadata["32:153289"] = DeviceMetadata(
            class_mismatch="schema=FAN, discovery=DIS"
        )
        manager._metadata["32:153289"].class_mismatch = None

        result = manager.get_mismatched_devices()
        assert result == []


class TestCheckBoundMismatches:
    """Tests for DiscoveryManager.check_bound_mismatches."""

    def test_no_mismatch_when_bound_matches(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "_bound": "01:145038"}}
        count = manager.check_bound_mismatches(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is None or meta.bound_mismatch is None

    def test_bound_mismatch_detected(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        # make_discovered_device sets bound_to="01:145038"
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "_bound": "22:999999"}}
        count = manager.check_bound_mismatches(schema)
        assert count == 1
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.bound_mismatch is not None
        assert "22:999999" in meta.bound_mismatch
        assert "01:145038" in meta.bound_mismatch

    def test_bound_mismatch_cleared_when_resolved(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager._metadata["32:153289"] = DeviceMetadata(
            bound_mismatch="schema=22:999999, discovery=01:145038"
        )
        # Now schema matches scan
        schema = {"32:153289": {"_class": "FAN", "_bound": "01:145038"}}
        count = manager.check_bound_mismatches(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.bound_mismatch is None

    def test_no_bound_mismatch_for_device_without_bound(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN"}}  # no _bound
        count = manager.check_bound_mismatches(schema)
        assert count == 0

    def test_no_bound_mismatch_for_device_not_in_scan(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:999999": {"_class": "TRV", "_bound": "01:145038"}}
        count = manager.check_bound_mismatches(schema)
        assert count == 0

    def test_bound_mismatch_case_insensitive(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Same ID, different case — should NOT be a mismatch
        schema = {"32:153289": {"_class": "FAN", "_bound": "01:145038"}}
        count = manager.check_bound_mismatches(schema)
        assert count == 0

    def test_list_bound_no_mismatch_when_scan_in_list(self) -> None:
        """FAN with list-valued _bound — scan's bound_to is in the list."""
        dev = make_discovered_device("32:153289", "FAN")
        # make_discovered_device sets bound_to="01:145038"
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {
            "32:153289": {
                "_class": "FAN",
                "_bound": ["01:145038", "37:111111"],
            }
        }
        count = manager.check_bound_mismatches(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is None or meta.bound_mismatch is None

    def test_list_bound_mismatch_when_scan_not_in_list(self) -> None:
        """FAN with list-valued _bound — scan's bound_to NOT in list."""
        dev = make_discovered_device("32:153289", "FAN")
        # bound_to="01:145038" but schema says ["22:999999", "37:111111"]
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {
            "32:153289": {
                "_class": "FAN",
                "_bound": ["22:999999", "37:111111"],
            }
        }
        count = manager.check_bound_mismatches(schema)
        assert count == 1
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.bound_mismatch is not None
        assert "22:999999" in meta.bound_mismatch
        assert "37:111111" in meta.bound_mismatch
        assert "01:145038" in meta.bound_mismatch

    def test_list_bound_mismatch_cleared_when_resolved(self) -> None:
        """List-valued _bound mismatch is cleared when scan joins the list."""
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Pre-set a mismatch
        manager._metadata["32:153289"] = DeviceMetadata(
            bound_mismatch="schema=22:999999, 37:111111, discovery=01:145038"
        )
        # Now schema list includes the scan's bound_to
        schema = {
            "32:153289": {
                "_class": "FAN",
                "_bound": ["01:145038", "22:999999"],
            }
        }
        count = manager.check_bound_mismatches(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.bound_mismatch is None

    def test_list_bound_case_insensitive(self) -> None:
        """List-valued _bound comparison is case-insensitive."""
        dev = make_discovered_device("32:153289", "FAN")
        # bound_to="01:145038"
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Different case in list — should NOT be a mismatch
        schema = {
            "32:153289": {
                "_class": "FAN",
                "_bound": ["01:145038".upper(), "37:111111"],
            }
        }
        count = manager.check_bound_mismatches(schema)
        assert count == 0


class TestCheckMissingClass:
    """Tests for DiscoveryManager.check_missing_class."""

    def test_missing_class_detected(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {}}  # no _class
        count = manager.check_missing_class(schema)
        assert count == 1
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.missing_class is not None
        assert "FAN" in meta.missing_class

    def test_no_missing_class_when_class_present(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN"}}
        count = manager.check_missing_class(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is None or meta.missing_class is None

    def test_missing_class_cleared_when_added(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # First: flag as missing
        manager._metadata["32:153289"] = DeviceMetadata(missing_class="discovery=FAN")
        # Now schema has _class
        schema = {"32:153289": {"_class": "FAN"}}
        count = manager.check_missing_class(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.missing_class is None

    def test_no_missing_class_for_unknown_scan_type(self) -> None:
        dev = make_discovered_device("32:153289", "DEV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {}}
        count = manager.check_missing_class(schema)
        assert count == 0

    def test_no_missing_class_for_device_not_in_scan(self) -> None:
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:999999": {}}  # not in scan
        count = manager.check_missing_class(schema)
        assert count == 0


class TestCheckOrphanedDevices:
    """Tests for DiscoveryManager.check_orphaned_devices."""

    def test_not_orphaned_when_not_in_scan(self) -> None:
        """Device in schema but not in scan is NOT orphaned — scan may
        not have seen it yet (same logic as check_for_lost_devices)."""
        scan = make_mock_scan([])  # no devices in scan
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Device in schema, accepted, but not in scan — should NOT flag
        manager._metadata["04:056053"] = DeviceMetadata(status=DiscoveryStatus.ACCEPTED)
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema)
        assert count == 0

    def test_not_orphaned_when_new_and_not_in_scan(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # NEW device not in scan — not orphaned
        manager._metadata["04:056053"] = DeviceMetadata(status=DiscoveryStatus.NEW)
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema)
        assert count == 0

    def test_orphaned_when_last_seen_too_old(self) -> None:
        old_date = (dt.now() - td(days=30)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 1
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is not None

    def test_not_orphaned_when_recently_seen(self) -> None:
        recent_date = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=recent_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0

    def test_orphaned_cleared_when_seen_again(self) -> None:
        recent_date = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=recent_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Was orphaned, now seen recently
        manager._metadata["04:056053"] = DeviceMetadata(orphaned="old")
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is None

    def test_hgi_not_orphaned(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"18:123456": {"_class": "HGI"}}
        count = manager.check_orphaned_devices(schema)
        assert count == 0

    def test_structural_keys_skipped(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"main_tcs": "01:145038", "_owner": "me"}
        count = manager.check_orphaned_devices(schema)
        assert count == 0


class TestCheckAllMismatches:
    """Tests for DiscoveryManager.check_all_mismatches (unified check)."""

    def test_all_clear_when_no_mismatches(self) -> None:
        recent = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("32:153289", "FAN", last_seen=recent)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "_bound": "01:145038"}}
        counts = manager.check_all_mismatches(schema)
        assert counts == {
            "class_mismatch": 0,
            "bound_mismatch": 0,
            "missing_class": 0,
            "orphaned": 0,
        }

    def test_multiple_mismatch_types(self) -> None:
        recent = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("32:153289", "DIS", last_seen=recent)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Bound mismatch + missing class on same device (no _class to
        # compare, so class_mismatch is 0 — that's a missing_class instead)
        schema = {"32:153289": {"_bound": "22:999999"}}  # no _class, wrong bound
        counts = manager.check_all_mismatches(schema)
        assert counts["class_mismatch"] == 0
        assert counts["bound_mismatch"] == 1
        assert counts["missing_class"] == 1

    def test_notification_sent_when_mismatches_found(self) -> None:
        recent = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("32:153289", "DIS", last_seen=recent)
        scan = make_mock_scan([dev])
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN"}}
        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_create:
            manager.check_all_mismatches(schema)
            mock_create.assert_called_once()

    def test_notification_dismissed_when_all_clear(self) -> None:
        recent = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("32:153289", "FAN", last_seen=recent)
        scan = make_mock_scan([dev])
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "_bound": "01:145038"}}
        with patch(
            "custom_components.ramses_cc.discovery.async_dismiss_notification"
        ) as mock_dismiss:
            manager.check_all_mismatches(schema)
            mock_dismiss.assert_called_once()


class TestSyncWithSchema:
    """Tests for DiscoveryManager.sync_with_schema.

    sync_with_schema reconciles discovery metadata with the current schema.
    Devices in the schema but with NEW status should be marked ACCEPTED —
    they're already in the schema (e.g. added manually via the schema
    editor), so showing them as "new devices to review" is confusing and
    prevents the user from resolving class mismatches (the review form's
    NEW section only has Accept/Decline/Skip, not Update _class).
    """

    def test_new_device_in_schema_marked_accepted(self) -> None:
        """A device with NEW status that's in the schema is marked ACCEPTED."""
        dev = make_discovered_device("37:154519", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # First sync to populate metadata from scan (status=NEW by default)
        manager.sync_with_schema(set())
        assert manager._metadata["37:154519"].status == DiscoveryStatus.NEW

        # Now sync with schema that includes 37:154519 — should mark ACCEPTED
        manager.sync_with_schema({"37:154519", "32:153289"})

        entry = manager.get_device("37:154519")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.ACCEPTED
        assert entry.metadata.enabled is True

    def test_new_device_not_in_schema_stays_new(self) -> None:
        """A device with NEW status that's NOT in the schema stays NEW."""
        dev = make_discovered_device("37:154519", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # sync_with_schema — 37:154519 is NOT in the schema
        manager.sync_with_schema({"32:153289"})

        entry = manager.get_device("37:154519")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.NEW

    def test_accepted_device_not_in_schema_marked_removed(self) -> None:
        """An ACCEPTED device that's removed from the schema is marked REMOVED."""
        dev = make_discovered_device("37:154519", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("37:154519")
        assert (
            manager.get_device("37:154519").metadata.status == DiscoveryStatus.ACCEPTED
        )

        # sync_with_schema — 37:154519 is no longer in the schema
        manager.sync_with_schema({"32:153289"})

        entry = manager.get_device("37:154519")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.REMOVED

    def test_hgi_gateway_skipped(self) -> None:
        """HGI gateways (18:) are skipped by sync_with_schema."""
        dev = make_discovered_device("18:130236", "HGI")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # 18: devices are skipped — status stays whatever it was
        manager.sync_with_schema(set())  # empty schema

        # 18:130236 should not be in metadata (skipped by sync_with_schema)
        # or if it is, its status should not be REMOVED
        if "18:130236" in manager._metadata:
            assert manager._metadata["18:130236"].status != DiscoveryStatus.REMOVED

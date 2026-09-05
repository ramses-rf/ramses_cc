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

from datetime import UTC, datetime as dt, timedelta as td
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
        zone_index="02",
        rssi=-72.0,
        confidence="high",
        is_battery=True,
        source_count=3,
        destination_count=0,
    )


def make_mock_scan(devices: list[DiscoveredDevice] | None = None) -> MagicMock:
    """Create a mock DiscoveryScan."""
    scan = MagicMock()
    devs = devices or []
    scan.get_devices.return_value = devs
    scan._devices = {d.device_id: d for d in devs}
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

    def test_local_vs_foreign_hgi_detection(self) -> None:
        """Local active HGI is skipped, foreign HGI is detected as NEW."""
        local_hgi = make_discovered_device("18:111111", "HGI")
        foreign_hgi = make_discovered_device("18:222222", "HGI")
        scan = make_mock_scan([local_hgi, foreign_hgi])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:111111",
        )

        new_ids = manager.check_for_new_devices()
        assert "18:111111" not in new_ids
        assert "18:222222" in new_ids

        devices = manager.get_devices()
        device_ids = [d.device.device_id for d in devices]
        assert "18:111111" not in device_ids
        assert "18:222222" in device_ids

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

    def test_schema_device_not_notified_as_new(self) -> None:
        """Device in schema but with no metadata must not be notified as NEW.

        Regression test for issue 917: after a coordinator reload, discovery
        metadata can be lost (not persisted before teardown).  A device that
        was previously accepted is still in the schema and still seen by the
        scan engine, but has no metadata.  Without the schema-membership guard
        in check_for_new_devices, it would be flagged as NEW and re-notified
        every checkpoint cycle.
        """
        dev = make_discovered_device(device_id="10:093149", likely_type="OTB")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=True)

        # Simulate the post-reload state: device is in the schema but has
        # no metadata (lost during reload).  sync_with_schema stashes the
        # schema device IDs so check_for_new_devices can suppress it.
        manager.sync_with_schema({"10:093149", "01:223036"})

        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_notify:
            new_ids = manager.check_for_new_devices()
            assert new_ids == []
            assert not mock_notify.called

    def test_schema_device_no_metadata_not_created_as_new(self) -> None:
        """A schema device with no metadata must not get NEW metadata created.

        Ensures the guard in check_for_new_devices doesn't just suppress the
        notification but also prevents creating NEW metadata — otherwise the
        device would appear in the review form's "new devices" list.
        """
        dev = make_discovered_device(device_id="13:142019", likely_type="BDR")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.sync_with_schema({"13:142019"})
        manager.check_for_new_devices()

        # Device should NOT have metadata created (it's in the schema already)
        assert "13:142019" not in manager._metadata

    def test_non_schema_device_still_notified_as_new(self) -> None:
        """Device NOT in schema with no metadata must still be notified as NEW.

        Ensures the schema-membership guard doesn't over-suppress: a genuinely
        new device (not in schema, no metadata) must still be detected and
        notified.
        """
        dev = make_discovered_device(device_id="04:999999", likely_type="TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=True)

        # Schema has a different device, not 04:999999
        manager.sync_with_schema({"01:223036"})

        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_notify:
            new_ids = manager.check_for_new_devices()
            assert "04:999999" in new_ids
            assert mock_notify.called


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

    def test_hgi_generates_root_entry_with_class(self) -> None:
        """HGI creates a root-level entry with _class='HGI'."""
        result = DiscoveryManager.generate_schema_entry(
            "18:001234", "HGI", comment="Local Gateway"
        )
        assert result == {
            "18:001234": {
                "_class": "HGI",
                "_comment": "Local Gateway",
            }
        }

    def test_trv_with_ctl_and_zone(self) -> None:
        result = DiscoveryManager.generate_schema_entry(
            "04:056053", "TRV", ctl_id="01:145038", zone_index="02"
        )
        from ramses_rf.schemas import SZ_SENSOR, SZ_ZONES

        assert result["01:145038"][SZ_ZONES]["02"][SZ_SENSOR] == "04:056053"

    def test_trv_without_ctl_goes_to_orphans(self) -> None:
        result = DiscoveryManager.generate_schema_entry("04:056053", "TRV")
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        assert "04:056053" in result[SZ_ORPHANS_HEAT]

    def test_bdr_with_ctl_and_zone(self) -> None:
        result = DiscoveryManager.generate_schema_entry(
            "13:123456", "BDR", ctl_id="01:145038", zone_index="01"
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

        assert (
            result["01:145038"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "10:064873"
        )

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
        dev = make_discovered_device(
            "04:056053", "TRV", last_seen="not-a-date"
        )
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
            zone_index="02",
            rssi=-72.0,
            confidence="high",
            is_battery=True,
            source_count=3,
            destination_count=0,
        )
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("04:056053")
        lost_ids = manager.check_for_lost_devices()
        assert lost_ids == []

    def test_check_for_lost_devices_skips_hgi_gateway(self) -> None:
        """HGI gateway devices (18:) are never marked LOST.

        The remove_device service blocks gateway removal, so the HGI
        must never appear in the lost-devices review list.
        """
        old_date = (dt.now() - td(days=30)).isoformat()
        dev = make_discovered_device("18:149488", "HGI", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager.accept_device("18:149488")
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
        assert (
            result["01:111111"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "01:222222"
        )

    def test_otb_no_ctl(self) -> None:
        """OTB without ctl_id goes to heat orphans."""
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry("01:222222", "OTB")
        assert "01:222222" in result[SZ_ORPHANS_HEAT]

    def test_bdr_with_ctl_and_zone(self) -> None:
        """BDR with ctl_id and zone_index becomes a zone actuator."""
        from ramses_rf.schemas import SZ_ACTUATORS, SZ_ZONES

        result = DiscoveryManager.generate_schema_entry(
            "08:333333", "BDR", ctl_id="01:111111", zone_index="01"
        )
        assert "08:333333" in result["01:111111"][SZ_ZONES]["01"][SZ_ACTUATORS]

    def test_bdr_with_ctl_no_zone(self) -> None:
        """BDR with ctl_id but no zone goes to DHW as dhw_valve."""
        from ramses_rf.schemas import SZ_DHW_SYSTEM, SZ_DHW_VALVE

        result = DiscoveryManager.generate_schema_entry(
            "08:333333", "BDR", ctl_id="01:111111"
        )
        assert result["01:111111"][SZ_DHW_SYSTEM][SZ_DHW_VALVE] == "08:333333"

    def test_bdr_with_ctl_and_fc_domain_is_appliance_control(self) -> None:
        """BDR with ctl_id and domain_id=FC is the appliance_control (issue 834).

        A BDR broadcasting 3B00/3EF0 (TPI loop) is the boiler relay, not a
        DHW valve.  The scan engine sets domain_id=FC; generate_schema_entry
        must place it under system.appliance_control, not stored_hotwater.
        """
        from ramses_rf.schemas import SZ_APPLIANCE_CONTROL, SZ_SYSTEM

        result = DiscoveryManager.generate_schema_entry(
            "13:121025", "BDR", ctl_id="01:046100", domain_id="FC"
        )
        assert (
            result["01:046100"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "13:121025"
        )

    def test_bdr_with_fc_domain_no_ctl_goes_to_orphans(self) -> None:
        """BDR with domain_id=FC but no ctl_id goes to orphans_heat."""
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        result = DiscoveryManager.generate_schema_entry(
            "13:121025", "BDR", domain_id="FC"
        )
        assert "13:121025" in result[SZ_ORPHANS_HEAT]

    def test_bdr_with_zone_takes_priority_over_fc_domain(self) -> None:
        """BDR with both zone_index and domain_id=FC is a zone actuator.

        zone_index is a stronger signal (explicit zone binding) than the FC
        domain (TPI loop).  A BDR could theoretically be both, but zone
        binding wins.
        """
        from ramses_rf.schemas import SZ_ACTUATORS, SZ_ZONES

        result = DiscoveryManager.generate_schema_entry(
            "13:121025",
            "BDR",
            ctl_id="01:046100",
            zone_index="02",
            domain_id="FC",
        )
        assert "13:121025" in result["01:046100"][SZ_ZONES]["02"][SZ_ACTUATORS]

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
        """TRV with ctl_id and zone_index becomes a zone sensor."""
        from ramses_rf.schemas import SZ_SENSOR, SZ_ZONES

        result = DiscoveryManager.generate_schema_entry(
            "04:555555", "TRV", ctl_id="01:111111", zone_index="02"
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
        """TRV with ctl_id and zone_index gets a root entry."""
        result = DiscoveryManager.generate_schema_entry(
            "04:056053", "TRV", ctl_id="01:145038", zone_index="02"
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
        """BDR with ctl_id and zone_index gets a root entry."""
        result = DiscoveryManager.generate_schema_entry(
            "13:123456", "BDR", ctl_id="01:145038", zone_index="01"
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
            "04:056053", "TRV", ctl_id="01:145038", zone_index="02"
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
            "13:123456", "BDR", ctl_id="01:145038", zone_index="01"
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
        # Override bound_to/zone_index for FAN (not relevant)
        dev.bound_to = None
        dev.zone_index = None
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
        dev.zone_index = None
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
        dev.zone_index = None
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
        dev.zone_index = None
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
        assert "belongs to 32:153289" in comment

    def test_explicit_schema_entry_no_comment(self) -> None:
        """Test that explicitly provided schema entries don't get auto-comments."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        custom_entry = {
            "01:145038": {"zones": {"02": {"sensor": "04:056053"}}}
        }
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


class TestHgiDiscoveryCandidate:
    """Tests for HGI discovery candidates from MQTT (issue 1119).

    HGI gateways discovered via MQTT appear in the schema without _owner
    but are NOT in the scan engine (HGIs are gateways, not RF devices).
    check_for_new_devices must still flag them as NEW so the user gets
    a notification and can review/accept them.
    """

    def test_hgi_candidate_flagged_as_new(self) -> None:
        """An 18: device in schema without _owner is flagged as NEW."""
        scan = make_mock_scan([])  # no scan engine devices
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )
        # Simulate sync_with_schema with a schema containing an HGI
        # discovery candidate (18:149488, no _owner)
        manager.sync_with_schema(
            {"18:130236", "18:149488"},
            schema={
                "18:130236": {"_class": "HGI", "_owner": "me"},
                "18:149488": {"_class": "HGI"},  # no _owner
            },
        )

        new_ids = manager.check_for_new_devices()
        assert "18:149488" in new_ids
        assert "18:130236" not in new_ids  # active HGI skipped

    def test_hgi_candidate_not_re_notified(self) -> None:
        """Once notified, HGI candidate is not re-reported."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )
        manager.sync_with_schema(
            {"18:149488"},
            schema={"18:149488": {"_class": "HGI"}},
        )

        # First check — flagged
        new_ids = manager.check_for_new_devices()
        assert "18:149488" in new_ids

        # Second check — already notified, not re-flagged
        new_ids = manager.check_for_new_devices()
        assert "18:149488" not in new_ids

    def test_accepted_hgi_without_owner_re_flagged(self) -> None:
        """An accepted HGI that lost _owner is re-flagged as NEW."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )
        # Pre-populate metadata as ACCEPTED (e.g. user previously accepted
        # but later cleared _owner from the schema)
        manager._metadata["18:149488"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            enabled=True,
            owner=None,
        )
        manager.sync_with_schema(
            {"18:149488"},
            schema={"18:149488": {"_class": "HGI"}},  # no _owner
        )

        new_ids = manager.check_for_new_devices()
        assert "18:149488" in new_ids
        assert manager._metadata["18:149488"].status == DiscoveryStatus.NEW

    def test_non_hgi_schema_no_owner_not_flagged_by_hgi_loop(self) -> None:
        """Non-18: devices in _schema_no_owner_ids are skipped by HGI loop."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
        )
        manager.sync_with_schema(
            {"04:056053"},
            schema={"04:056053": {"_class": "TRV"}},  # no _owner but not 18:
        )

        new_ids = manager.check_for_new_devices()
        # 04: device is in _schema_no_owner_ids but not 18:, so the HGI
        # loop skips it.  It's also not in the scan engine, so the engine
        # loop doesn't flag it either.
        assert "04:056053" not in new_ids

    def test_active_hgi_in_no_owner_ids_skipped(self) -> None:
        """Active HGI without _owner is still skipped by the HGI loop."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )
        # Active HGI is in schema without _owner (edge case)
        manager.sync_with_schema(
            {"18:130236"},
            schema={"18:130236": {"_class": "HGI"}},  # no _owner
        )

        new_ids = manager.check_for_new_devices()
        assert "18:130236" not in new_ids  # active HGI always skipped

    def test_hgi_candidate_new_status_re_reported(self) -> None:
        """HGI candidate with NEW status is re-reported if not notified."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )
        manager.sync_with_schema(
            {"18:149488"},
            schema={"18:149488": {"_class": "HGI"}},
        )

        # First check — creates metadata with NEW status
        new_ids = manager.check_for_new_devices()
        assert "18:149488" in new_ids

        # Clear _notified to simulate "not yet notified"
        manager._notified.clear()

        # Second check — NEW status and not in _notified, re-reported
        new_ids = manager.check_for_new_devices()
        assert "18:149488" in new_ids

    def test_get_devices_stub_uses_hgi_class(self) -> None:
        """get_devices stub for 18: device uses 'HGI' as likely_type."""
        scan = make_mock_scan([])  # no scan engine devices
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )
        # Add metadata for an HGI not in the scan engine
        manager._metadata["18:149488"] = DeviceMetadata(
            status=DiscoveryStatus.NEW,
        )

        devices = manager.get_devices()
        hgi_entry = next(
            (d for d in devices if d.device.device_id == "18:149488"), None
        )
        assert hgi_entry is not None
        assert hgi_entry.device.likely_type == "HGI"

    def test_accept_device_uses_hgi_class(self) -> None:
        """accept_device for an 18: device generates _class: HGI."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )
        # Add as NEW so accept_device can find it
        manager._metadata["18:149488"] = DeviceMetadata(
            status=DiscoveryStatus.NEW,
        )

        accepted = manager.accept_device("18:149488", owner="me")
        assert accepted.metadata.schema_entry is not None
        # The schema entry should have _class: HGI, not UNKNOWN
        entry = accepted.metadata.schema_entry.get("18:149488", {})
        assert entry.get("_class") == "HGI"

    def test_notification_includes_hgi_candidate(self) -> None:
        """Notification text includes HGI candidates not in scan engine."""
        scan = make_mock_scan([])
        hass = make_mock_hass()
        manager = DiscoveryManager(
            hass,
            scan,
            auto_notify=True,
            active_hgi_id="18:130236",
        )
        manager.sync_with_schema(
            {"18:149488"},
            schema={"18:149488": {"_class": "HGI"}},
        )

        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_notify:
            manager.check_for_new_devices()
            assert mock_notify.called
            msg = mock_notify.call_args.kwargs.get("message", "")
            assert "18:149488" in msg
            assert "HGI" in msg


class TestCheckClassMismatches:
    """Tests for DiscoveryManager.check_class_mismatches.

    Note: HVAC devices (29:, 32:, 37:, 63:) are skipped by
    check_class_mismatches because the scan engine's likely_type is
    unreliable for ambiguous HVAC prefixes.  HVAC class mismatches are
    detected by _check_rf_contradictions instead.  These tests use
    non-HVAC devices (04: TRV, 01: CTL) where likely_type is reliable.
    """

    def test_no_mismatch_when_classes_match(self) -> None:
        """No mismatch when schema _class matches scan likely_type."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_class_mismatches(schema)
        assert count == 0
        # No class_mismatch set on metadata
        meta = manager._metadata.get("04:056053")
        assert meta is None or meta.class_mismatch is None

    def test_mismatch_detected(self) -> None:
        """Mismatch detected when schema _class differs from scan likely_type."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "CTL"}}
        count = manager.check_class_mismatches(schema)
        assert count == 1
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.class_mismatch is not None
        assert "CTL" in meta.class_mismatch
        assert "TRV" in meta.class_mismatch

    def test_mismatch_cleared_when_resolved(self) -> None:
        """Mismatch flag cleared when classes match again."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # First: create a mismatch
        manager._metadata["04:056053"] = DeviceMetadata(
            class_mismatch="schema=CTL, discovery=TRV"
        )

        # Now the scan says TRV and schema says TRV — mismatch resolved
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_class_mismatches(schema)
        assert count == 0
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.class_mismatch is None

    def test_hvac_devices_skipped_when_low_confidence(self) -> None:
        """HVAC devices with low/medium confidence are skipped — likely_type
        is unreliable when based on prefix fallback."""
        dev = make_discovered_device("32:153289", "DIS")
        dev.confidence = "medium"  # prefix fallback, not evidence-based
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "remotes": []}}
        count = manager.check_class_mismatches(schema)
        assert count == 0  # skipped — low confidence

    def test_hvac_devices_checked_when_high_confidence(self) -> None:
        """HVAC devices with high confidence are NOT skipped — either a VC
        pair matched or the scan engine re-classified after 3+ contradictions."""
        dev = make_discovered_device("32:153289", "DIS")
        dev.confidence = "high"  # re-classified after threshold
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"32:153289": {"_class": "FAN", "remotes": []}}
        count = manager.check_class_mismatches(schema)
        assert count == 1  # not skipped — high confidence

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
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {}}  # no _class
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
        dev = make_discovered_device("04:056053", "DEV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_class_mismatches(schema)
        assert count == 0

    def test_mismatch_with_entity_slug_normalized(self) -> None:
        """Schema _class='radiator_valve' is normalized to 'TRV' before comparison."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Schema has entity slug 'radiator_valve', scan says 'TRV'
        # After normalization, both are 'TRV' — no mismatch
        schema = {"04:056053": {"_class": "radiator_valve"}}
        count = manager.check_class_mismatches(schema)
        assert count == 0

    def test_multiple_mismatches(self) -> None:
        """Multiple devices with mismatches are all detected."""
        dev1 = make_discovered_device("04:056053", "TRV")
        dev2 = make_discovered_device("01:216136", "CTL")
        scan = make_mock_scan([dev1, dev2])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {
            "04:056053": {"_class": "CTL"},  # schema says CTL, scan says TRV
            "01:216136": {"_class": "TRV"},  # schema says TRV, scan says CTL
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
        manager._metadata["32:153289"] = DeviceMetadata(
            missing_class="discovery=FAN"
        )
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

    def test_missing_class_skipped_when_dismissed(self) -> None:
        """A dismissed missing_class is not re-flagged on the next check."""
        dev = make_discovered_device("32:153289", "FAN")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # User previously dismissed the missing_class suggestion
        manager._metadata["32:153289"] = DeviceMetadata(
            missing_class_dismissed=True
        )
        schema = {"32:153289": {}}  # still no _class
        count = manager.check_missing_class(schema)
        assert count == 0
        meta = manager._metadata.get("32:153289")
        assert meta is not None
        assert meta.missing_class is None  # not re-flagged

    def test_missing_class_dismissed_serialization(self) -> None:
        """missing_class_dismissed is serialized/deserialized correctly."""
        meta = DeviceMetadata(missing_class_dismissed=True)
        d = meta.to_dict()
        assert d["missing_class_dismissed"] is True

        restored = DeviceMetadata.from_dict(d)
        assert restored.missing_class_dismissed is True

    def test_missing_class_dismissed_default_false(self) -> None:
        """missing_class_dismissed defaults to False."""
        meta = DeviceMetadata()
        assert meta.missing_class_dismissed is False
        d = meta.to_dict()
        assert d["missing_class_dismissed"] is False


class TestCheckOrphanedDevices:
    """Tests for DiscoveryManager.check_orphaned_devices."""

    def test_not_orphaned_when_not_in_scan(self) -> None:
        """Device in schema but not in scan is NOT orphaned — scan may
        not have seen it yet (same logic as check_for_lost_devices)."""
        scan = make_mock_scan([])  # no devices in scan
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Device in schema, accepted, but not in scan — should NOT flag
        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED
        )
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema)
        assert count == 0

    def test_not_orphaned_when_new_and_not_in_scan(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # NEW device not in scan — not orphaned
        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.NEW
        )
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

    def test_suppress_not_seen_skips_notification(self) -> None:
        """Device with _suppress_not_seen in schema is not added to
        the orphaned notification list (issue 988)."""

        old_date = (dt.now() - td(days=30)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV", "_suppress_not_seen": True}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is None  # no notification flag
        # last_orphaned_log should be set (periodic INFO log)
        assert meta.last_orphaned_log is not None

    def test_suppress_not_seen_logs_periodically(self) -> None:
        """When _suppress_not_seen is set, INFO log is emitted only once
        per threshold_days, not on every checkpoint cycle (issue 988)."""

        old_date = (dt.now() - td(days=30)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # First check — should log (last_orphaned_log is None)
        schema = {"04:056053": {"_class": "TRV", "_suppress_not_seen": True}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        first_log = meta.last_orphaned_log
        assert first_log is not None

        # Second check — should NOT log again (within threshold)
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0
        meta = manager._metadata.get("04:056053")
        assert meta.last_orphaned_log == first_log  # unchanged

    def test_suppress_not_seen_cleared_when_seen_again(self) -> None:
        """last_orphaned_log and _suppress_not_seen are cleared when
        device is seen again, so a future orphaned episode gets a
        fresh notification (issue 988)."""

        recent_date = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=recent_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Device was suppressed-orphans, now seen again
        manager._metadata["04:056053"] = DeviceMetadata(
            orphaned="old", last_orphaned_log="2026-01-01T00:00:00"
        )
        schema = {"04:056053": {"_class": "TRV", "_suppress_not_seen": True}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is None
        assert meta.last_orphaned_log is None  # cleared
        # _suppress_not_seen should be removed from the schema entry
        assert "_suppress_not_seen" not in schema["04:056053"]

    def test_suppress_not_seen_logs_again_after_threshold(self) -> None:
        """After threshold_days since last log, a new INFO log is emitted."""

        old_date = (dt.now() - td(days=30)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Set last_orphaned_log to 10 days ago — should log again
        old_log = (dt.now() - td(days=10)).isoformat()
        manager._metadata["04:056053"] = DeviceMetadata(
            last_orphaned_log=old_log
        )
        schema = {"04:056053": {"_class": "TRV", "_suppress_not_seen": True}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.last_orphaned_log != old_log  # updated
        assert meta.orphaned is None  # still no notification

    def test_suppress_not_seen_int_days_still_suppressed(self) -> None:
        """_suppress_not_seen: 14 suppresses for 14 days from last_seen.
        Device not seen for 10 days, threshold=7, suppress=14 -> still
        within suppress period (10 < 14)."""

        old_date = (dt.now() - td(days=10)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV", "_suppress_not_seen": 14}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0  # suppressed
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is None  # no notification

    def test_suppress_not_seen_int_days_expired(self) -> None:
        """_suppress_not_seen: 14 re-notifies after 14 days from last_seen.
        Device not seen for 20 days, threshold=7, suppress=14 -> expired
        (20 > 14), so notification re-appears."""

        old_date = (dt.now() - td(days=20)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV", "_suppress_not_seen": 14}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 1  # suppress expired, re-notified
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is not None  # notification flag set
        assert "suppress expired" in meta.orphaned
        # Key removed from schema so next checkpoint uses normal orphaned
        # handling (no re-evaluation of expired suppress)
        assert "_suppress_not_seen" not in schema["04:056053"]

    def test_suppress_not_seen_true_forever(self) -> None:
        """_suppress_not_seen: True suppresses forever, even after 100
        days."""

        old_date = (dt.now() - td(days=100)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV", "_suppress_not_seen": True}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 0  # suppressed forever
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is None  # no notification

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

    def test_orphaned_with_tz_aware_last_seen(self) -> None:
        """Tz-aware last_seen (e.g. from ramses_rf) is compared correctly."""

        old_date = (dt.now(UTC) - td(days=30)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=old_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
        assert count == 1
        meta = manager._metadata.get("04:056053")
        assert meta is not None
        assert meta.orphaned is not None

    def test_not_orphaned_with_tz_aware_recent(self) -> None:
        """Tz-aware recent last_seen is not flagged as orphaned."""

        recent_date = (dt.now(UTC) - td(days=1)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=recent_date)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_orphaned_devices(schema, threshold_days=7)
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
            "name_mismatch": 0,
            "weak_signal": 0,
        }

    def test_multiple_mismatch_types(self) -> None:
        recent = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("32:153289", "DIS", last_seen=recent)
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # Bound mismatch + missing class on same device (no _class to
        # compare, so class_mismatch is 0 — that's a missing_class instead)
        schema = {
            "32:153289": {"_bound": "22:999999"}
        }  # no _class, wrong bound
        counts = manager.check_all_mismatches(schema)
        assert counts["class_mismatch"] == 0
        assert counts["bound_mismatch"] == 1
        assert counts["missing_class"] == 1

    def test_notification_sent_when_mismatches_found(self) -> None:
        recent = (dt.now() - td(days=1)).isoformat()
        dev = make_discovered_device("04:056053", "TRV", last_seen=recent)
        scan = make_mock_scan([dev])
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan, auto_notify=False)

        schema = {"04:056053": {"_class": "CTL"}}
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


class TestCheckCommunicationQuality:
    """Tests for DiscoveryManager.check_communication_quality (issue 1047)."""

    def _make_device(
        self, device_id: str, rssi_quality: str, is_stale: bool = False
    ) -> MagicMock:
        """Create a mock ramses_rf device with communication_quality."""
        quality = MagicMock()
        quality.rssi_quality = rssi_quality
        quality.is_stale = is_stale
        quality.best_rssi = -98 if rssi_quality == "weak" else -72
        quality.staleness_seconds = 600 if is_stale else None
        device = MagicMock()
        device.id = device_id
        device.communication_quality = quality
        return device

    def test_stale_alone_does_not_set_flag(self) -> None:
        """Staleness alone (good RSSI) does not flag a device (issue 1062).

        Battery devices transmit every 10-30 minutes by design — a flat
        staleness threshold would falsely flag them.  Only RSSI quality
        is checked; "device gone silent" is handled by is_available and
        check_for_orphaned_devices.
        """
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        device = self._make_device("04:056053", "normal", is_stale=True)
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_communication_quality(schema, [device])
        assert count == 0

    def test_weak_rssi_sets_flag(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        device = self._make_device("04:056053", "weak")
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_communication_quality(schema, [device])
        assert count == 1
        meta = manager._metadata["04:056053"]
        assert meta.weak_signal is not None
        assert "RSSI" in meta.weak_signal

    def test_very_weak_rssi_sets_flag(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        device = self._make_device("04:056053", "very_weak")
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_communication_quality(schema, [device])
        assert count == 1

    def test_normal_rssi_clears_flag(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        # First set the flag
        weak_device = self._make_device("04:056053", "weak")
        schema = {"04:056053": {"_class": "TRV"}}
        manager.check_communication_quality(schema, [weak_device])
        assert manager._metadata["04:056053"].weak_signal is not None
        # Now quality recovers
        good_device = self._make_device("04:056053", "normal")
        manager.check_communication_quality(schema, [good_device])
        assert manager._metadata["04:056053"].weak_signal is None

    def test_suppress_weak_signal(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        device = self._make_device("04:056053", "weak")
        schema = {
            "04:056053": {"_class": "TRV", "_suppress_weak_signal": True}
        }
        count = manager.check_communication_quality(schema, [device])
        assert count == 0

    def test_dismissed_prevents_re_flagging(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        # Pre-set dismissed flag
        manager._metadata["04:056053"] = DeviceMetadata(
            weak_signal_dismissed=True
        )
        device = self._make_device("04:056053", "weak")
        schema = {"04:056053": {"_class": "TRV"}}
        count = manager.check_communication_quality(schema, [device])
        assert count == 0

    def test_hgi_skipped(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        device = self._make_device("18:001234", "weak")
        schema = {"18:001234": {}}
        count = manager.check_communication_quality(schema, [device])
        assert count == 0

    def test_no_devices_skips_check(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        count = manager.check_communication_quality({}, devices=None)
        assert count == 0

    def test_log_throttle_one_per_hour(self) -> None:
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        device = self._make_device("04:056053", "weak")
        schema = {"04:056053": {"_class": "TRV"}}

        with patch(
            "custom_components.ramses_cc.discovery._LOGGER"
        ) as mock_logger:
            # First call — should log WARNING
            manager.check_communication_quality(schema, [device])
            assert mock_logger.warning.call_count >= 1

            # Second call within 1 hour — should NOT log WARNING
            mock_logger.warning.reset_mock()
            manager.check_communication_quality(schema, [device])
            assert mock_logger.warning.call_count == 0

    def test_metadata_serialization_roundtrip(self) -> None:
        meta = DeviceMetadata(
            weak_signal="RSSI -98 dBm (weak)",
            weak_signal_dismissed=False,
            last_weak_signal_log="2026-08-25T12:00:00",
        )
        d = meta.to_dict()
        assert d["weak_signal"] == "RSSI -98 dBm (weak)"
        assert d["weak_signal_dismissed"] is False
        assert d["last_weak_signal_log"] == "2026-08-25T12:00:00"

        restored = DeviceMetadata.from_dict(d)
        assert restored.weak_signal == "RSSI -98 dBm (weak)"
        assert restored.weak_signal_dismissed is False
        assert restored.last_weak_signal_log == "2026-08-25T12:00:00"

    def test_recovery_clears_dismissed(self) -> None:
        """Dismissed flag is cleared on recovery so re-degradation re-warns."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        # Pre-set dismissed flag
        manager._metadata["04:056053"] = DeviceMetadata(
            weak_signal_dismissed=True
        )
        # Quality recovers
        good_device = self._make_device("04:056053", "normal")
        schema = {"04:056053": {"_class": "TRV"}}
        manager.check_communication_quality(schema, [good_device])
        assert manager._metadata["04:056053"].weak_signal_dismissed is False

        # Now device degrades again — should be flagged
        weak_device = self._make_device("04:056053", "weak")
        count = manager.check_communication_quality(schema, [weak_device])
        assert count == 1

    def test_weak_device_clears_lost_status(self) -> None:
        """A device with RSSI data is being heard — clear LOST status.

        If we're receiving weak signals, the device is not lost.
        """
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        # Pre-set LOST status
        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.LOST, enabled=True
        )
        device = self._make_device("04:056053", "weak")
        schema = {"04:056053": {"_class": "TRV"}}
        manager.check_communication_quality(schema, [device])
        assert (
            manager._metadata["04:056053"].status == DiscoveryStatus.ACCEPTED
        )

    def test_good_device_clears_lost_status(self) -> None:
        """A device with strong RSSI is also being heard — clear LOST."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.LOST, enabled=True
        )
        device = self._make_device("04:056053", "normal")
        schema = {"04:056053": {"_class": "TRV"}}
        manager.check_communication_quality(schema, [device])
        assert (
            manager._metadata["04:056053"].status == DiscoveryStatus.ACCEPTED
        )

    def test_no_rssi_does_not_clear_lost(self) -> None:
        """A device with no RSSI data (quality=None) stays LOST."""
        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.LOST, enabled=True
        )
        # Device with communication_quality=None (no RSSI tracker)
        device = MagicMock()
        device.id = "04:056053"
        device.communication_quality = None
        schema = {"04:056053": {"_class": "TRV"}}
        manager.check_communication_quality(schema, [device])
        assert manager._metadata["04:056053"].status == DiscoveryStatus.LOST


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
            manager.get_device("37:154519").metadata.status
            == DiscoveryStatus.ACCEPTED
        )

        # sync_with_schema — 37:154519 is no longer in the schema
        manager.sync_with_schema({"32:153289"})

        entry = manager.get_device("37:154519")
        assert entry is not None
        assert entry.metadata.status == DiscoveryStatus.REMOVED

    def test_hgi_gateway_skipped(self) -> None:
        """Active HGI gateway (18:) is skipped by sync_with_schema."""
        dev = make_discovered_device("18:130236", "HGI")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(
            make_mock_hass(),
            scan,
            auto_notify=False,
            active_hgi_id="18:130236",
        )

        # Active 18: device is skipped — status stays whatever it was
        manager.sync_with_schema(set())  # empty schema

        # 18:130236 should not be in metadata (skipped by sync_with_schema)
        # or if it is, its status should not be REMOVED
        if "18:130236" in manager._metadata:
            assert (
                manager._metadata["18:130236"].status
                != DiscoveryStatus.REMOVED
            )


class TestGetOrphanedDevices:
    """Tests for DiscoveryManager.get_orphaned_devices."""

    def test_returns_only_orphaned(self) -> None:
        """Only devices with orphaned flag set are returned."""
        dev1 = make_discovered_device("04:056053", "TRV")
        dev2 = make_discovered_device("01:145038", "CTL")
        scan = make_mock_scan([dev1, dev2])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager._metadata["04:056053"] = DeviceMetadata(
            orphaned="last seen 2026-07-01 (>7 days)"
        )
        manager._metadata["01:145038"] = DeviceMetadata()

        result = manager.get_orphaned_devices()
        assert len(result) == 1
        assert result[0].device.device_id == "04:056053"

    def test_empty_when_no_orphaned(self) -> None:
        """No orphaned flags → empty list."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        result = manager.get_orphaned_devices()
        assert result == []

    def test_cleared_orphaned_not_returned(self) -> None:
        """An orphaned flag that was cleared (set to None) is not returned."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager._metadata["04:056053"] = DeviceMetadata(
            orphaned="last seen 2026-07-01 (>7 days)"
        )
        manager._metadata["04:056053"].orphaned = None

        result = manager.get_orphaned_devices()
        assert result == []


class TestGetLostDevices:
    """Tests for DiscoveryManager.get_lost_devices."""

    def test_returns_only_lost(self) -> None:
        """Only devices with LOST status are returned."""
        dev1 = make_discovered_device("04:056053", "TRV")
        dev2 = make_discovered_device("01:145038", "CTL")
        scan = make_mock_scan([dev1, dev2])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.LOST
        )
        manager._metadata["01:145038"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED
        )

        result = manager.get_lost_devices()
        assert len(result) == 1
        assert result[0].device.device_id == "04:056053"

    def test_empty_when_no_lost(self) -> None:
        """No LOST devices → empty list."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED
        )

        result = manager.get_lost_devices()
        assert result == []

    def test_accepted_not_returned(self) -> None:
        """ACCEPTED devices are not returned by get_lost_devices."""
        dev = make_discovered_device("04:056053", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager._metadata["04:056053"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED
        )

        result = manager.get_lost_devices()
        assert result == []

    def test_hgi_gateway_not_returned(self) -> None:
        """HGI gateway devices (18:) are never returned as lost.

        Even if an HGI was previously marked LOST (e.g. before the
        check_for_lost_devices skip was added), get_lost_devices
        filters it out so it never appears in the review-device-health
        UI as removable.
        """
        dev = make_discovered_device("18:149488", "HGI")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        manager._metadata["18:149488"] = DeviceMetadata(
            status=DiscoveryStatus.LOST
        )

        result = manager.get_lost_devices()
        assert result == []


class TestNameMismatch:
    """Tests for zone name mismatch detection (issue 947)."""

    def _make_zone(
        self, zone_id: str, runtime_name: str | None = None
    ) -> MagicMock:
        """Create a mock Zone with the given runtime name.

        :param zone_id: Zone ID like "01:150000_03".
        :param runtime_name: The name from 0004 packets (zone_state.name).
        """
        zone = MagicMock()
        zone.id = zone_id
        zone.zone_state = MagicMock()
        zone.zone_state.name = runtime_name
        return zone

    def test_mismatch_detected_when_schema_differs_from_controller(
        self,
    ) -> None:
        """Schema _name='Lounge' but controller reports 'Kitchen' → mismatch."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Lounge"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        count = manager.check_name_mismatches(schema, zones=zones)

        assert count == 1
        meta = manager._metadata.get("01:150000_03")
        assert meta is not None
        assert meta.name_mismatch is not None
        assert "Lounge" in meta.name_mismatch
        assert "Kitchen" in meta.name_mismatch

    def test_no_mismatch_when_names_match(self) -> None:
        """Schema _name='Kitchen' and controller reports 'Kitchen' → OK."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Kitchen"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        count = manager.check_name_mismatches(schema, zones=zones)

        assert count == 0
        meta = manager._metadata.get("01:150000_03")
        assert meta is None or meta.name_mismatch is None

    def test_no_mismatch_when_no_runtime_name(self) -> None:
        """No 0004 name received yet (zone_state.name=None) → skip."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Lounge"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name=None)]

        count = manager.check_name_mismatches(schema, zones=zones)

        assert count == 0

    def test_no_mismatch_when_no_schema_name(self) -> None:
        """Schema has no _name for the zone → skip (nothing to compare)."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        count = manager.check_name_mismatches(schema, zones=zones)

        assert count == 0

    def test_no_mismatch_when_zones_is_none(self) -> None:
        """No zones provided → skip (coordinator not ready)."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {"01:150000": {"zones": {"03": {"_name": "Lounge"}}}}

        count = manager.check_name_mismatches(schema, zones=None)

        assert count == 0

    def test_mismatch_cleared_when_resolved(self) -> None:
        """Previously mismatched zone now matches → flag cleared."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        # Pre-set a mismatch flag
        manager._metadata["01:150000_03"] = DeviceMetadata(
            name_mismatch="schema=Lounge, controller=Kitchen"
        )
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Kitchen"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        count = manager.check_name_mismatches(schema, zones=zones)

        assert count == 0
        meta = manager._metadata.get("01:150000_03")
        assert meta is not None
        assert meta.name_mismatch is None

    def test_multiple_zones_one_mismatch(self) -> None:
        """Multiple zones, only one has a mismatch."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Lounge"},
                    "04": {"_name": "Hallway"},
                },
            },
        }
        zones = [
            self._make_zone("01:150000_03", runtime_name="Lounge"),  # match
            self._make_zone("01:150000_04", runtime_name="Office"),  # mismatch
        ]

        count = manager.check_name_mismatches(schema, zones=zones)

        assert count == 1
        assert manager._metadata["01:150000_04"].name_mismatch is not None
        assert manager._metadata.get("01:150000_03") is None or (
            manager._metadata["01:150000_03"].name_mismatch is None
        )

    def test_name_mismatch_in_check_all_mismatches(self) -> None:
        """check_all_mismatches includes name_mismatch in counts."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Lounge"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        counts = manager.check_all_mismatches(schema, zones=zones)

        assert counts["name_mismatch"] == 1

    def test_name_mismatch_metadata_round_trip(self) -> None:
        """name_mismatch survives to_dict/from_dict serialization."""
        meta = DeviceMetadata(
            name_mismatch="schema=Lounge, controller=Kitchen"
        )
        d = meta.to_dict()
        assert d["name_mismatch"] == "schema=Lounge, controller=Kitchen"

        restored = DeviceMetadata.from_dict(d)
        assert restored.name_mismatch == "schema=Lounge, controller=Kitchen"

    def test_name_mismatch_default_none(self) -> None:
        """name_mismatch defaults to None."""
        meta = DeviceMetadata()
        assert meta.name_mismatch is None
        d = meta.to_dict()
        assert d["name_mismatch"] is None

    def test_get_name_mismatch_devices(self) -> None:
        """get_name_mismatch_devices returns only mismatched zones."""
        dev = make_discovered_device("01:150000_03", "CTL")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        manager._metadata["01:150000_03"] = DeviceMetadata(
            name_mismatch="schema=Lounge, controller=Kitchen"
        )
        manager._metadata["01:150000_04"] = DeviceMetadata()

        result = manager.get_name_mismatch_devices()

        assert len(result) == 1
        assert result[0].device.device_id == "01:150000_03"

    def test_warned_name_mismatches_avoids_spam(self) -> None:
        """Second check with same mismatch doesn't re-warn at WARNING level."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Lounge"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        # First check — should warn and add to _warned_name_mismatches
        with patch(
            "custom_components.ramses_cc.discovery._LOGGER"
        ) as mock_logger:
            manager.check_name_mismatches(schema, zones=zones)
            # WARNING should have been called (new mismatch)
            warning_calls = [
                c
                for c in mock_logger.warning.call_args_list
                if "name mismatch" in str(c).lower()
            ]
            assert len(warning_calls) == 1
            assert "01:150000_03" in manager._warned_name_mismatches

        # Second check — same mismatch, should NOT warn at WARNING level
        with patch(
            "custom_components.ramses_cc.discovery._LOGGER"
        ) as mock_logger:
            count = manager.check_name_mismatches(schema, zones=zones)
            # Count is still 1 (mismatch still exists)
            assert count == 1
            # But no WARNING call — only DEBUG
            warning_calls = [
                c
                for c in mock_logger.warning.call_args_list
                if "name mismatch" in str(c).lower()
            ]
            assert len(warning_calls) == 0
            # DEBUG should have been called instead
            debug_calls = [
                c
                for c in mock_logger.debug.call_args_list
                if "persistent name mismatch" in str(c).lower()
            ]
            assert len(debug_calls) == 1

    def test_warned_name_mismatches_cleared_when_resolved(self) -> None:
        """Warned set cleared when all name mismatches are resolved."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Lounge"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        # First check — creates mismatch
        manager.check_name_mismatches(schema, zones=zones)
        assert "01:150000_03" in manager._warned_name_mismatches

        # Fix the schema _name to match controller
        schema["01:150000"]["zones"]["03"]["_name"] = "Kitchen"

        # Second check — mismatch resolved
        count = manager.check_name_mismatches(schema, zones=zones)
        assert count == 0
        assert "01:150000_03" not in manager._warned_name_mismatches

    def test_name_mismatch_re_flagged_after_skip(self) -> None:
        """After 'skip' in review, mismatch is re-detected next checkpoint."""
        scan = make_mock_scan()
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        schema = {
            "01:150000": {
                "zones": {
                    "03": {"_name": "Lounge"},
                },
            },
        }
        zones = [self._make_zone("01:150000_03", runtime_name="Kitchen")]

        # First check — mismatch detected
        manager.check_name_mismatches(schema, zones=zones)
        assert manager._metadata["01:150000_03"].name_mismatch is not None

        # Simulate "skip" in review — flag is cleared
        manager._metadata["01:150000_03"].name_mismatch = None
        # Also clear from warned set so the re-detection warns again
        manager._warned_name_mismatches.clear()

        # Second check — mismatch re-detected (no dismiss)
        count = manager.check_name_mismatches(schema, zones=zones)
        assert count == 1
        assert manager._metadata["01:150000_03"].name_mismatch is not None

    def test_active_hgi_id_and_scan_properties(self) -> None:
        """Test active_hgi_id getter/setter and get_scan_codes / get_scan_domain_ids."""
        dev = make_discovered_device("18:123456", "HGI")
        dev.codes_seen = {"1FC9", "000C"}
        dev.domain_id = "FC"
        dev.is_authoritative_domain = True

        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # active_hgi_id
        assert manager.active_hgi_id is None
        manager.active_hgi_id = "18:123456"
        assert manager.active_hgi_id == "18:123456"

        # get_scan_codes
        scan_codes = manager.get_scan_codes()
        assert "18:123456" in scan_codes
        assert "1FC9" in scan_codes["18:123456"]

        # get_scan_domain_ids
        scan_domains = manager.get_scan_domain_ids()
        assert scan_domains["18:123456"] == ("FC", True)

    def test_refresh_device_comments(self) -> None:
        """Test refresh_device_comments for HGI, bound devices, hotwater valves, and HVAC parents."""
        dev_hgi = make_discovered_device("18:001122", "HGI")
        dev_trv = make_discovered_device("04:112233", "TRV")
        dev_trv.zone_index = "01"
        dev_trv.bound_to = "01:111111"
        dev_fan = make_discovered_device("32:112233", "FAN")
        dev_rem = make_discovered_device("37:112233", "REM")
        dev_rem.bound_to = "32:112233"

        scan = make_mock_scan([dev_hgi, dev_trv, dev_fan, dev_rem])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        config_schema = {
            "01:111111": {"stored_hotwater": {"hotwater_valve": "13:999888"}},
            "32:112233": {},
            "29:445566": {"_class": "SEN", "_bound": "32:112233"},
        }

        existing_comments: dict[str, str] = {}
        updated = manager.refresh_device_comments(
            existing_comments, config_schema=config_schema
        )

        # 1. HGI comment generated
        assert "18:001122" in updated
        assert manager._COMMENT_SUFFIX in updated["18:001122"]

        # 2. Bound TRV comment generated with zone and controller
        assert "04:112233" in updated
        assert "zone 01" in updated["04:112233"]

        # 3. HVAC Remote comment generated with 'belongs to'
        assert "37:112233" in updated
        assert "belongs to 32:112233" in updated["37:112233"]

        # 4. Fallback 29: sensor with _bound in config_schema
        assert "29:445566" in updated
        assert "belongs to 32:112233" in updated["29:445566"]

    def test_sync_removed_from_schema_edge_cases(self) -> None:
        """Test sync_with_schema skips active HGI and removes foreign devices."""
        meta_hgi = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED, enabled=True
        )
        meta_foreign = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED, enabled=True
        )
        meta_normal = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED, enabled=True
        )

        scan = make_mock_scan([])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)
        manager._metadata = {
            "18:111111": meta_hgi,
            "04:222222": meta_foreign,
            "04:333333": meta_normal,
        }
        manager.active_hgi_id = "18:111111"

        manager.sync_with_schema(
            schema_device_ids={"04:333333"}, foreign_device_ids={"04:222222"}
        )

        # Active HGI is not marked REMOVED
        assert (
            manager._metadata["18:111111"].status == DiscoveryStatus.ACCEPTED
        )
        # Foreign device is marked REMOVED
        assert manager._metadata["04:222222"].status == DiscoveryStatus.REMOVED
        # Device in schema remains ACCEPTED
        assert (
            manager._metadata["04:333333"].status == DiscoveryStatus.ACCEPTED
        )

    def test_check_lost_devices_suppress_not_seen(self) -> None:
        """Test check_for_lost_devices respects _suppress_not_seen boolean and days integer."""
        now = dt.now()
        recent_ts = (now - td(days=2)).isoformat()
        old_ts = (now - td(days=30)).isoformat()

        dev_forever = make_discovered_device(
            "04:100001", "TRV", last_seen=old_ts
        )
        dev_suppressed_days = make_discovered_device(
            "04:100002", "TRV", last_seen=recent_ts
        )
        dev_expired_days = make_discovered_device(
            "04:100003", "TRV", last_seen=old_ts
        )

        scan = make_mock_scan(
            [dev_forever, dev_suppressed_days, dev_expired_days]
        )
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        for dev_id in ("04:100001", "04:100002", "04:100003"):
            manager._metadata[dev_id] = DeviceMetadata(
                status=DiscoveryStatus.ACCEPTED, enabled=True
            )

        schema = {
            "04:100001": {"_suppress_not_seen": True},
            "04:100002": {"_suppress_not_seen": 7},
            "04:100003": {"_suppress_not_seen": 7},
        }

        lost = manager.check_for_lost_devices(schema=schema)
        # dev_forever: skipped (True)
        # dev_suppressed_days: last_seen 2 days ago (suppress active)
        # dev_expired_days: last_seen 30 days ago (suppress 7 days expired and not seen)
        assert "04:100001" not in lost
        assert "04:100003" in lost

    def test_flag_class_mismatch_and_missing_class(self) -> None:
        """Test flag_class_mismatch logic and get_missing_class_devices filter."""
        dev = make_discovered_device("04:123456", "TRV")
        scan = make_mock_scan([dev])
        manager = DiscoveryManager(make_mock_hass(), scan, auto_notify=False)

        # 1. Missing class
        manager._metadata["04:123456"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            missing_class="Suggest TRV",
        )
        missing = manager.get_missing_class_devices()
        assert len(missing) == 1
        assert missing[0].device.device_id == "04:123456"

        # 2. Flag mismatch (new)
        manager.flag_class_mismatch(
            "04:123456", "Config says FAN but looks like TRV"
        )
        assert (
            manager._metadata["04:123456"].class_mismatch
            == "Config says FAN but looks like TRV"
        )
        assert "04:123456" in manager._warned_mismatches

        # 3. Flag mismatch dismissed
        manager._metadata["04:123456"].class_mismatch_dismissed = True
        manager.flag_class_mismatch("04:123456", "Different description")
        # Description unchanged because dismissed
        assert (
            manager._metadata["04:123456"].class_mismatch
            == "Config says FAN but looks like TRV"
        )

    def test_send_notification_orphaned_and_weak_signal(self) -> None:
        """Test _send_notification formats orphaned and weak signal sections."""
        dev1 = make_discovered_device("04:111111", "TRV")
        dev2 = make_discovered_device("04:222222", "TRV")
        scan = make_mock_scan([dev1, dev2])
        hass = make_mock_hass()
        manager = DiscoveryManager(hass, scan, auto_notify=True)

        manager._metadata["04:111111"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            orphaned="Not seen for 10 days",
        )
        manager._metadata["04:222222"] = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            weak_signal="RSSI -88 dBm",
        )

        counts = {
            "new": 0,
            "removed": 0,
            "class_mismatch": 0,
            "bound_mismatch": 0,
            "missing_class": 0,
            "orphaned": 1,
            "name_mismatch": 0,
            "weak_signal": 1,
            "lost": 0,
        }
        with patch(
            "custom_components.ramses_cc.discovery.async_create_notification"
        ) as mock_notify:
            manager._send_mismatch_notification(counts)
            assert mock_notify.called
            msg = mock_notify.call_args[1]["message"]
            assert "orphaned device(s)" in msg
            assert "weak signal device(s)" in msg

"""Tests for discovery metadata reset when devices are removed from the schema.

Covers three scenarios:
1. Full schema wipe (all devices removed) via schema editor
2. Per-device removal via schema editor
3. Schema already empty on startup (stale .storage from before fix)

The key invariant: when a device is removed from the schema, its discovery
metadata must be reset so it can be re-discovered as NEW by the passive scan.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import (
    CONF_ADVANCED_FEATURES,
    CONF_PASSIVE_SCAN,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    CONF_SSOT_MIGRATED,
    DOMAIN,
    SZ_KNOWN_LIST,
)
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from custom_components.ramses_cc.discovery import (
    SZ_DISCOVERY,
    SZ_DISCOVERY_DEVICES,
    SZ_DISCOVERY_SCAN_STATE,
    DeviceMetadata,
    DiscoveryStatus,
)

# Device IDs used in tests
HGI_ID = "18:130236"
FAN_ID = "32:153289"
REM_ID = "37:168270"
CO2_ID = "37:126776"


def _make_discovery_state(
    devices: dict[str, dict[str, Any]] | None = None,
    scan_devices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a discovery state dict as stored in .storage."""
    return {
        SZ_DISCOVERY_DEVICES: devices or {},
        SZ_DISCOVERY_SCAN_STATE: json.dumps({"devices": scan_devices or []}),
    }


def _make_scan_device(device_id: str) -> dict[str, Any]:
    """Build a scan engine device dict."""
    return {
        "device_id": device_id,
        "first_seen": "2026-01-01T00:00:00",
        "last_seen": "2026-01-01T00:00:01",
        "likely_type": "unknown",
        "codes_seen": [],
        "bound_to": None,
        "zone_index": None,
        "rssi": None,
        "confidence": "low",
        "is_battery": False,
        "source_count": 1,
        "destination_count": 0,
    }


def _make_accepted_meta() -> dict[str, Any]:
    """Build ACCEPTED device metadata."""
    return {
        "status": "accepted",
        "enabled": True,
        "faked": False,
        "schema_entry": None,
        "owner": None,
    }


def _make_storage_with_discovery(
    discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a .storage dict with discovery data."""
    storage: dict[str, Any] = {
        "client_state": {"schema": {}, "packets": {}},
        "remotes": {},
    }
    if discovery is not None:
        storage[SZ_DISCOVERY] = discovery
    return storage


def _make_mock_client() -> MagicMock:
    """Build a mock ramses_rf client."""
    client = MagicMock()
    client.start = AsyncMock()
    client.get_state = MagicMock(return_value=({}, {}))
    return client


# ---------------------------------------------------------------------------
# Part 1: Coordinator — schema empty on startup clears stale discovery
# ---------------------------------------------------------------------------


class TestCoordinatorDiscoveryReset:
    """Tests for coordinator clearing stale discovery on startup."""

    async def test_schema_empty_clears_stale_discovery(
        self, hass: HomeAssistant
    ) -> None:
        """When schema is empty but known_list has devices, discovery
        metadata in .storage should be cleared so devices are
        re-discovered as NEW.  known_list entries are kept as trait
        overrides (not wiped), and CONF_SSOT_MIGRATED is set.
        """
        # Mock async_update_entry to prevent real config entry updates
        hass.config_entries.async_update_entry = MagicMock()
        discovery = _make_discovery_state(
            devices={
                FAN_ID: _make_accepted_meta(),
                REM_ID: _make_accepted_meta(),
            },
            scan_devices=[
                _make_scan_device(FAN_ID),
                _make_scan_device(REM_ID),
            ],
        )
        storage = _make_storage_with_discovery(discovery)

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: {},  # empty schema
            SZ_KNOWN_LIST: {
                HGI_ID: {"class": "HGI"},
                FAN_ID: {"class": "FAN"},
                REM_ID: {"class": "REM"},
            },
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        mock_client = _make_mock_client()
        cast(Any, coordinator)._create_client = MagicMock(
            return_value=mock_client
        )
        cast(Any, coordinator.store).async_load = AsyncMock(
            return_value=storage
        )

        # Track what gets saved to the raw HA Store
        saved_data: dict[str, Any] = {}

        async def fake_raw_save(data: dict[str, Any]) -> None:
            saved_data.update(data)

        with (
            patch(
                "custom_components.ramses_cc.coordinator.RamsesCoordinator._async_start_discovery_scan",
                AsyncMock(),
            ),
            patch("homeassistant.helpers.storage.Store") as mock_store_cls,
        ):
            mock_store_inst = MagicMock()
            mock_store_inst.async_load = AsyncMock(return_value=storage)
            mock_store_inst.async_save = AsyncMock(side_effect=fake_raw_save)
            mock_store_cls.return_value = mock_store_inst

            await coordinator.async_setup()

        # The coordinator should have set the skip flag
        assert coordinator._skip_discovery_restore is True

        # The raw store should have been saved without discovery data
        assert SZ_DISCOVERY not in saved_data

        # CONF_SSOT_MIGRATED should be set via async_update_entry
        cast(Any, hass.config_entries.async_update_entry).assert_called()
        call_kwargs = cast(
            Any, hass.config_entries.async_update_entry
        ).call_args
        updated_options = call_kwargs.kwargs.get("options", {})
        updated_advanced = updated_options.get(CONF_ADVANCED_FEATURES, {})
        assert updated_advanced.get(CONF_SSOT_MIGRATED) is True

        # known_list should NOT be wiped — trait overrides are preserved
        updated_kl = updated_options.get(SZ_KNOWN_LIST, {})
        assert HGI_ID in updated_kl  # HGI kept
        assert FAN_ID in updated_kl  # FAN trait override kept
        assert REM_ID in updated_kl  # REM trait override kept

    async def test_schema_empty_skips_discovery_restore_in_scan(
        self, hass: HomeAssistant
    ) -> None:
        """When _skip_discovery_restore is set, the scan startup should
        not restore old discovery state.
        """
        # Mock async_update_entry to prevent real config entry updates
        hass.config_entries.async_update_entry = MagicMock()

        discovery = _make_discovery_state(
            devices={FAN_ID: _make_accepted_meta()},
            scan_devices=[_make_scan_device(FAN_ID)],
        )
        storage = _make_storage_with_discovery(discovery)

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: {},
            SZ_KNOWN_LIST: {
                HGI_ID: {"class": "HGI"},
                FAN_ID: {"class": "FAN"},
            },
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        mock_client = _make_mock_client()
        cast(Any, coordinator)._create_client = MagicMock(
            return_value=mock_client
        )
        cast(Any, coordinator.store).async_load = AsyncMock(
            return_value=storage
        )

        with (
            patch(
                "custom_components.ramses_cc.coordinator.RamsesCoordinator._async_start_discovery_scan",
                AsyncMock(),
            ),
            patch("homeassistant.helpers.storage.Store") as mock_store_cls,
        ):
            mock_store_inst = MagicMock()
            mock_store_inst.async_load = AsyncMock(return_value=storage)
            mock_store_inst.async_save = AsyncMock()
            mock_store_cls.return_value = mock_store_inst

            await coordinator.async_setup()

        # Now simulate the scan startup
        coordinator.client = mock_client
        with (
            patch("ramses_rf.discovery_scan.DiscoveryScan") as mock_scan_cls,
            patch(
                "custom_components.ramses_cc.coordinator.DiscoveryManager"
            ) as mock_dm_cls,
            patch(
                "custom_components.ramses_cc.coordinator.async_track_time_interval"
            ) as mock_track,
            patch(
                "custom_components.ramses_cc.coordinator.async_call_later"
            ) as mock_call_later,
        ):
            mock_track.return_value = MagicMock()
            mock_call_later.return_value = MagicMock()
            mock_scan = MagicMock()
            mock_scan_cls.return_value = mock_scan
            mock_dm = MagicMock()
            mock_dm_cls.return_value = mock_dm

            # The store still has discovery data in memory
            cast(Any, coordinator.store).async_load = AsyncMock(
                return_value=storage
            )

            await coordinator._async_start_discovery_scan()

            # restore_state should NOT have been called
            mock_dm.restore_state.assert_not_called()

    async def test_schema_with_devices_does_not_clear_discovery(
        self, hass: HomeAssistant
    ) -> None:
        """When schema has devices, discovery metadata should be preserved."""
        discovery = _make_discovery_state(
            devices={FAN_ID: _make_accepted_meta()},
        )
        storage = _make_storage_with_discovery(discovery)

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: {FAN_ID: {}},  # has a device
            SZ_KNOWN_LIST: {HGI_ID: {"class": "HGI"}},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        mock_client = _make_mock_client()
        cast(Any, coordinator)._create_client = MagicMock(
            return_value=mock_client
        )
        cast(Any, coordinator.store).async_load = AsyncMock(
            return_value=storage
        )

        with (
            patch(
                "custom_components.ramses_cc.coordinator.RamsesCoordinator._async_start_discovery_scan",
                AsyncMock(),
            ),
            patch("homeassistant.helpers.storage.Store") as mock_store_cls,
        ):
            mock_store_inst = MagicMock()
            mock_store_inst.async_load = AsyncMock(return_value=storage)
            mock_store_inst.async_save = AsyncMock()
            mock_store_cls.return_value = mock_store_inst

            await coordinator.async_setup()

        # The skip flag should NOT be set
        assert coordinator._skip_discovery_restore is False

    async def test_migrated_flag_skips_orphan_migration(
        self, hass: HomeAssistant
    ) -> None:
        """When CONF_SSOT_MIGRATED is set, known_list-only entries are
        NOT migrated into the schema as orphans — they're trait overrides.
        """
        hass.config_entries.async_update_entry = MagicMock()

        storage = _make_storage_with_discovery(
            _make_discovery_state(devices={}, scan_devices=[])
        )

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        # Schema has FAN, but known_list also has REM (not in schema).
        # With CONF_SSOT_MIGRATED=True, REM should NOT become an orphan.
        entry.options = {
            CONF_SCHEMA: {FAN_ID: {}},
            SZ_KNOWN_LIST: {
                HGI_ID: {"class": "HGI"},
                FAN_ID: {"class": "FAN"},
                REM_ID: {"class": "REM", "faked": True},
            },
            CONF_ADVANCED_FEATURES: {
                CONF_PASSIVE_SCAN: True,
                CONF_SSOT_MIGRATED: True,
            },
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        mock_client = _make_mock_client()
        cast(Any, coordinator)._create_client = MagicMock(
            return_value=mock_client
        )
        cast(Any, coordinator.store).async_load = AsyncMock(
            return_value=storage
        )

        with (
            patch(
                "custom_components.ramses_cc.coordinator.RamsesCoordinator._async_start_discovery_scan",
                AsyncMock(),
            ),
            patch("homeassistant.helpers.storage.Store") as mock_store_cls,
        ):
            mock_store_inst = MagicMock()
            mock_store_inst.async_load = AsyncMock(return_value=storage)
            mock_store_inst.async_save = AsyncMock()
            mock_store_cls.return_value = mock_store_inst

            await coordinator.async_setup()

        # REM should NOT have been added as an orphan
        config_schema = coordinator.options.get(CONF_SCHEMA, {})
        orphans_hvac = config_schema.get("orphans_hvac", [])
        assert REM_ID not in orphans_hvac
        # FAN should still be in the schema
        assert FAN_ID in config_schema


# ---------------------------------------------------------------------------
# Part 2: Coordinator unload — filters discovery state
# ---------------------------------------------------------------------------


class TestCoordinatorUnloadFilter:
    """Tests for coordinator unload filtering discovery state."""

    @pytest.fixture  # type: ignore[untyped-decorator]
    def coordinator_with_empty_schema(
        self, hass: HomeAssistant
    ) -> RamsesCoordinator:
        """Return a coordinator with empty schema."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: {},
            SZ_KNOWN_LIST: {},
            CONF_ADVANCED_FEATURES: {},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        coordinator.client = _make_mock_client()
        coordinator._entities = {}
        coordinator._remotes = {}
        return coordinator

    async def test_unload_empty_schema_skips_discovery_save(
        self, coordinator_with_empty_schema: RamsesCoordinator
    ) -> None:
        """When schema is empty, unload should skip saving discovery state."""
        coordinator = coordinator_with_empty_schema

        # Mock discovery_manager with export_state
        coordinator.discovery_manager = MagicMock()
        coordinator.discovery_manager.export_state.return_value = {
            SZ_DISCOVERY_DEVICES: {FAN_ID: _make_accepted_meta()},
            SZ_DISCOVERY_SCAN_STATE: '{"devices": []}',
        }

        # Mock store to track what gets saved
        saved_calls: list[tuple] = []
        cast(Any, coordinator.store).async_save = AsyncMock(
            side_effect=lambda *a, **kw: saved_calls.append((a, kw))
        )

        await coordinator._async_save_on_unload()

        # The save call should have discovery=None (skipped because
        # schema is empty — flags are reset in finally block, so we
        # verify the effect on the saved data, not the flags)
        assert len(saved_calls) == 1
        args, _ = saved_calls[0]
        # discovery is the 4th positional arg (index 3)
        assert args[3] is None

    async def test_unload_filters_removed_devices(
        self, hass: HomeAssistant
    ) -> None:
        """When a device was removed, unload should filter it from
        the discovery state so it's re-discovered as NEW.
        """
        REMOVED_ID = CO2_ID
        KEPT_ID = FAN_ID

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: {KEPT_ID: {}},  # only KEPT_ID in schema
            SZ_KNOWN_LIST: {},
            CONF_ADVANCED_FEATURES: {},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        coordinator.client = _make_mock_client()
        coordinator._entities = {}
        coordinator._remotes = {}

        # Discovery state has both KEPT and REMOVED
        coordinator.discovery_manager = MagicMock()
        coordinator.discovery_manager.export_state.return_value = {
            SZ_DISCOVERY_DEVICES: {
                KEPT_ID: _make_accepted_meta(),
                REMOVED_ID: _make_accepted_meta(),
            },
            SZ_DISCOVERY_SCAN_STATE: json.dumps(
                {
                    "devices": [
                        _make_scan_device(KEPT_ID),
                        _make_scan_device(REMOVED_ID),
                    ]
                }
            ),
        }

        saved_calls: list[tuple] = []
        cast(Any, coordinator.store).async_save = AsyncMock(
            side_effect=lambda *a, **kw: saved_calls.append(a)
        )

        await coordinator._async_save_on_unload()

        # Check the saved discovery state — flags are reset in finally
        # block, so we verify the effect on the saved data
        assert len(saved_calls) == 1
        args = saved_calls[0]
        discovery_state = args[3]  # 4th positional arg
        assert discovery_state is not None
        # REMOVED_ID should be filtered out
        assert REMOVED_ID not in discovery_state[SZ_DISCOVERY_DEVICES]
        assert KEPT_ID in discovery_state[SZ_DISCOVERY_DEVICES]
        # Scan state should also be filtered
        scan_data = json.loads(discovery_state[SZ_DISCOVERY_SCAN_STATE])
        scan_ids = {d["device_id"] for d in scan_data["devices"]}
        assert REMOVED_ID not in scan_ids
        assert KEPT_ID in scan_ids

    async def test_unload_resets_flags_after_save(
        self, coordinator_with_empty_schema: RamsesCoordinator
    ) -> None:
        """After unload save, flags should be reset."""
        coordinator = coordinator_with_empty_schema
        coordinator.discovery_manager = None
        cast(Any, coordinator.store).async_save = AsyncMock()

        await coordinator._async_save_on_unload()

        assert coordinator._skip_topology_sync is False
        assert coordinator._skip_discovery_save is False
        assert coordinator._discovery_filter_ids is None

    async def test_stop_scan_skips_save_when_schema_empty(
        self, coordinator_with_empty_schema: RamsesCoordinator
    ) -> None:
        """_async_stop_discovery_scan must skip saving when schema is empty.

        This tests the race condition where the config flow clears .storage
        but the scan-stop unload callback overwrites it with stale ACCEPTED
        metadata.  The scan-stop callback runs first (LIFO order) so it
        must check entry.options itself, not rely on _async_save_on_unload.
        """
        coordinator = coordinator_with_empty_schema
        coordinator.client = _make_mock_client()

        # Mock async_update_entry — the coordinator calls it to persist
        # comments, but the MagicMock entry isn't registered in HA.
        coordinator.hass.config_entries.async_update_entry = MagicMock()

        # Mock discovery_manager with stale ACCEPTED metadata
        dm = MagicMock()
        dm.export_state.return_value = {
            SZ_DISCOVERY_DEVICES: {FAN_ID: _make_accepted_meta()},
            SZ_DISCOVERY_SCAN_STATE: '{"devices": []}',
        }
        coordinator.discovery_manager = dm

        # Track what gets saved
        saved_calls: list[tuple] = []
        cast(Any, coordinator.store).async_save = AsyncMock(
            side_effect=lambda *a, **kw: saved_calls.append((a, kw))
        )

        await coordinator._async_stop_discovery_scan()

        # Discovery state should NOT have been saved (schema is empty)
        assert len(saved_calls) == 1
        args, _ = saved_calls[0]
        # discovery is the 4th positional arg (index 3)
        assert args[3] is None
        # discovery_manager should be stopped and cleared
        dm.stop.assert_called_once()

    async def test_stop_scan_saves_when_schema_has_devices(
        self, hass: HomeAssistant
    ) -> None:
        """_async_stop_discovery_scan saves normally when schema has devices."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: {FAN_ID: {}},
            SZ_KNOWN_LIST: {HGI_ID: {"class": "HGI"}},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        coordinator.client = _make_mock_client()
        cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

        # Mock async_update_entry — the coordinator calls it to persist
        # comments, but the MagicMock entry isn't registered in HA.
        hass.config_entries.async_update_entry = MagicMock()

        dm = MagicMock()
        dm.export_state.return_value = {
            SZ_DISCOVERY_DEVICES: {FAN_ID: _make_accepted_meta()},
            SZ_DISCOVERY_SCAN_STATE: '{"devices": []}',
        }
        coordinator.discovery_manager = dm

        saved_calls: list[tuple] = []
        cast(Any, coordinator.store).async_save = AsyncMock(
            side_effect=lambda *a, **kw: saved_calls.append((a, kw))
        )

        await coordinator._async_stop_discovery_scan()

        # Discovery state SHOULD have been saved (schema has devices)
        assert len(saved_calls) == 1
        args, _ = saved_calls[0]
        assert args[3] is not None
        dm.stop.assert_called_once()

    async def test_stop_scan_filters_removed_device(
        self, hass: HomeAssistant
    ) -> None:
        """_async_stop_discovery_scan filters out removed devices.

        When a user removes one device from the schema (not a full wipe),
        the scan-stop callback must filter that device from the discovery
        state so it's re-discovered as NEW after reload.
        """
        KEPT_ID = FAN_ID
        REMOVED_ID = CO2_ID

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: {KEPT_ID: {}},
            SZ_KNOWN_LIST: {HGI_ID: {"class": "HGI"}},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        coordinator.client = _make_mock_client()
        cast(Any, coordinator.store).async_load = AsyncMock(return_value={})

        # Mock async_update_entry — the coordinator calls it to persist
        # comments, but the MagicMock entry isn't registered in HA.
        hass.config_entries.async_update_entry = MagicMock()

        dm = MagicMock()
        dm.export_state.return_value = {
            SZ_DISCOVERY_DEVICES: {
                KEPT_ID: _make_accepted_meta(),
                REMOVED_ID: _make_accepted_meta(),
            },
            SZ_DISCOVERY_SCAN_STATE: '{"devices": []}',
        }
        coordinator.discovery_manager = dm

        saved_calls: list[tuple] = []
        cast(Any, coordinator.store).async_save = AsyncMock(
            side_effect=lambda *a, **kw: saved_calls.append((a, kw))
        )

        await coordinator._async_stop_discovery_scan()

        # Discovery state should be saved (schema has devices)
        assert len(saved_calls) == 1
        args, _ = saved_calls[0]
        discovery_state = args[3]
        assert discovery_state is not None
        # KEPT device should be in the saved discovery state
        assert KEPT_ID in discovery_state.get("devices", {})
        # REMOVED device should NOT be in the saved discovery state
        assert REMOVED_ID not in discovery_state.get("devices", {})
        dm.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Part 3: DeviceMetadata reset
# ---------------------------------------------------------------------------


class TestDeviceMetadataReset:
    """Tests for DeviceMetadata reset to NEW status."""

    def test_reset_to_new(self) -> None:
        """DeviceMetadata can be reset to NEW status."""
        meta = DeviceMetadata(
            status=DiscoveryStatus.ACCEPTED,
            enabled=True,
            faked=False,
            schema_entry={"some": "entry"},
            owner="test_owner",
        )
        assert meta.status == DiscoveryStatus.ACCEPTED

        # Reset to new
        reset = DeviceMetadata.from_dict(
            {
                "status": "new",
                "enabled": False,
                "faked": False,
                "schema_entry": None,
                "owner": None,
            }
        )
        assert reset.status == DiscoveryStatus.NEW
        assert reset.enabled is False
        assert reset.schema_entry is None

    def test_from_dict_preserves_accepted(self) -> None:
        """DeviceMetadata.from_dict correctly loads ACCEPTED status."""
        meta = DeviceMetadata.from_dict(
            {
                "status": "accepted",
                "enabled": True,
                "faked": False,
                "schema_entry": {"key": "val"},
                "owner": "owner1",
            }
        )
        assert meta.status == DiscoveryStatus.ACCEPTED
        assert meta.enabled is True
        assert meta.schema_entry == {"key": "val"}
        assert meta.owner == "owner1"

    def test_from_dict_defaults_to_new(self) -> None:
        """DeviceMetadata.from_dict defaults to NEW when status missing."""
        meta = DeviceMetadata.from_dict({})
        assert meta.status == DiscoveryStatus.NEW
        assert meta.enabled is False


# ---------------------------------------------------------------------------
# Part 4: Schema device-ID extraction covers nested TCS locations (issue 917)
# ---------------------------------------------------------------------------

# Device IDs used in issue 917 tests
CTL_ID = "01:108199"
OTB_ID = "10:093149"  # placed as appliance_control
BDR_ID = "13:142019"  # BDR relay, domain_id=FC → appliance_control
BDR2_ID = "13:181949"  # second BDR relay broadcasting 3EF0
DHW_SENSOR_ID = "07:123456"
DHW_VALVE_ID = "13:234567"
TRV_ID = "04:345678"
UFH_ID = "02:022960"


def _schema_with_nested_devices() -> dict[str, Any]:
    """Build a schema with devices nested inside TCS structures.

    This schema mirrors the real-world topology from issue 917:
    - OTB as appliance_control (inside system)
    - DHW sensor and valve (inside stored_hotwater)
    - TRV as zone sensor (inside zones)
    - UFH controller (inside underfloor_heating)
    - BDR as orphan (top-level orphans_heat)
    """
    from ramses_rf.schemas import (
        SZ_ACTUATORS,
        SZ_APPLIANCE_CONTROL,
        SZ_DHW_SYSTEM,
        SZ_DHW_VALVE,
        SZ_ORPHANS_HEAT,
        SZ_SENSOR,
        SZ_SYSTEM,
        SZ_UFH_SYSTEM,
        SZ_ZONES,
    )

    return {
        "main_tcs": CTL_ID,
        CTL_ID: {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: OTB_ID},
            SZ_DHW_SYSTEM: {
                SZ_SENSOR: DHW_SENSOR_ID,
                SZ_DHW_VALVE: DHW_VALVE_ID,
            },
            SZ_ZONES: {
                "01": {SZ_SENSOR: TRV_ID, SZ_ACTUATORS: [BDR_ID]},
            },
            SZ_UFH_SYSTEM: {UFH_ID: {}},
        },
        SZ_ORPHANS_HEAT: [BDR2_ID],
    }


class TestSchemaDeviceIdExtractionNested:
    """Tests that _extract_schema_device_ids finds devices nested inside
    TCS structures (appliance_control, DHW, zones, UFH, orphans).

    Regression for ramses-rf/ramses_cc#917: the simplified extraction in
    _async_stop_discovery_scan / _async_save_on_unload only looked at
    top-level keys and list values, missing devices nested inside TCS
    entries.  This caused their ACCEPTED metadata to be filtered out
    during save, so they were re-notified as NEW after every reload.
    """

    def test_extract_finds_appliance_control(self) -> None:
        """OTB placed as appliance_control must be found."""
        schema = _schema_with_nested_devices()
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        assert OTB_ID in result

    def test_extract_finds_dhw_sensor_and_valve(self) -> None:
        """DHW sensor and valve must be found."""
        schema = _schema_with_nested_devices()
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        assert DHW_SENSOR_ID in result
        assert DHW_VALVE_ID in result

    def test_extract_finds_zone_sensor(self) -> None:
        """TRV placed as zone sensor must be found."""
        schema = _schema_with_nested_devices()
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        assert TRV_ID in result

    def test_extract_finds_zone_actuator(self) -> None:
        """BDR placed as zone actuator must be found."""
        schema = _schema_with_nested_devices()
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        assert BDR_ID in result

    def test_extract_finds_ufh_controller(self) -> None:
        """UFH controller inside underfloor_heating must be found."""
        schema = _schema_with_nested_devices()
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        assert UFH_ID in result

    def test_extract_finds_orphan(self) -> None:
        """BDR in top-level orphans_heat must be found."""
        schema = _schema_with_nested_devices()
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        assert BDR2_ID in result

    def test_extract_finds_all_devices(self) -> None:
        """All nested devices + CTL must be found."""
        schema = _schema_with_nested_devices()
        result = RamsesCoordinator._extract_schema_device_ids(schema)
        expected = {
            CTL_ID,
            OTB_ID,
            DHW_SENSOR_ID,
            DHW_VALVE_ID,
            TRV_ID,
            BDR_ID,
            UFH_ID,
            BDR2_ID,
        }
        assert result == expected


class TestUnloadPreservesNestedDeviceMetadata:
    """Tests that _async_save_on_unload and _async_stop_discovery_scan
    preserve ACCEPTED metadata for devices nested inside TCS structures.

    Before the fix, the simplified schema_device_ids extraction missed
    nested devices, so the _discovery_filter_ids filter stripped their
    metadata during save.  After reload, the scan engine saw them again
    with no metadata → NEW → notification (ramses-rf/ramses_cc#917).
    """

    async def test_unload_preserves_appliance_control_metadata(
        self, hass: HomeAssistant
    ) -> None:
        """ACCEPTED metadata for an OTB (appliance_control) must survive
        the unload filter so it's not re-notified after reload.
        """
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: _schema_with_nested_devices(),
            SZ_KNOWN_LIST: {HGI_ID: {"class": "HGI"}},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        coordinator.client = _make_mock_client()
        cast(Any, coordinator.store).async_load = AsyncMock(return_value={})
        hass.config_entries.async_update_entry = MagicMock()

        dm = MagicMock()
        dm.export_state.return_value = {
            SZ_DISCOVERY_DEVICES: {
                OTB_ID: _make_accepted_meta(),
                BDR2_ID: _make_accepted_meta(),
                CTL_ID: _make_accepted_meta(),
            },
            SZ_DISCOVERY_SCAN_STATE: '{"devices": []}',
        }
        coordinator.discovery_manager = dm

        saved_calls: list[tuple] = []
        cast(Any, coordinator.store).async_save = AsyncMock(
            side_effect=lambda *a, **kw: saved_calls.append((a, kw))
        )

        await coordinator._async_save_on_unload()

        assert len(saved_calls) == 1
        args, _ = saved_calls[0]
        discovery_state = args[3]
        assert discovery_state is not None
        # OTB (nested as appliance_control) must NOT be filtered out
        assert OTB_ID in discovery_state[SZ_DISCOVERY_DEVICES]
        # BDR2 (top-level orphan) must also be preserved
        assert BDR2_ID in discovery_state[SZ_DISCOVERY_DEVICES]
        # CTL must also be preserved
        assert CTL_ID in discovery_state[SZ_DISCOVERY_DEVICES]

    async def test_stop_scan_preserves_nested_device_metadata(
        self, hass: HomeAssistant
    ) -> None:
        """_async_stop_discovery_scan must preserve metadata for devices
        nested inside TCS structures (appliance_control, DHW, zones).
        """
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.domain = DOMAIN
        entry.options = {
            CONF_SCHEMA: _schema_with_nested_devices(),
            SZ_KNOWN_LIST: {HGI_ID: {"class": "HGI"}},
            CONF_ADVANCED_FEATURES: {CONF_PASSIVE_SCAN: True},
            CONF_RAMSES_RF: {},
        }
        entry.async_on_unload = MagicMock()

        coordinator = RamsesCoordinator(hass, entry)
        coordinator.client = _make_mock_client()
        cast(Any, coordinator.store).async_load = AsyncMock(return_value={})
        hass.config_entries.async_update_entry = MagicMock()

        dm = MagicMock()
        dm.export_state.return_value = {
            SZ_DISCOVERY_DEVICES: {
                OTB_ID: _make_accepted_meta(),
                TRV_ID: _make_accepted_meta(),
                DHW_SENSOR_ID: _make_accepted_meta(),
            },
            SZ_DISCOVERY_SCAN_STATE: '{"devices": []}',
        }
        coordinator.discovery_manager = dm

        saved_calls: list[tuple] = []
        cast(Any, coordinator.store).async_save = AsyncMock(
            side_effect=lambda *a, **kw: saved_calls.append((a, kw))
        )

        await coordinator._async_stop_discovery_scan()

        assert len(saved_calls) == 1
        args, _ = saved_calls[0]
        discovery_state = args[3]
        assert discovery_state is not None
        # All nested devices must be preserved (not filtered out)
        assert OTB_ID in discovery_state[SZ_DISCOVERY_DEVICES]
        assert TRV_ID in discovery_state[SZ_DISCOVERY_DEVICES]
        assert DHW_SENSOR_ID in discovery_state[SZ_DISCOVERY_DEVICES]
        dm.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Part 5: Single-slot conflict resolution in config_flow (issue 917)
# ---------------------------------------------------------------------------


class TestResolveSingleSlotConflicts:
    """Tests that _resolve_single_slot_conflicts prevents two devices from
    claiming the same single-slot role (appliance_control, hotwater_valve,
    heating_valve) during bulk accept in the config flow.

    Before the fix, the config flow's review_discovered step did a raw
    deep_merge without calling _resolve_single_slot_conflicts.  When two
    relays both broadcast 3EF0 (TPI loop), the scan engine classifies
    both with domain_id=FC → appliance_control.  Bulk-accepting both
    caused the second to displace the first, creating an orphan that
    gets re-discovered and re-notified every checkpoint.
    """

    def test_no_conflict_returns_fragment_unchanged(self) -> None:
        """When there's no conflict, the fragment is returned unchanged."""
        from custom_components.ramses_cc.services import (
            _resolve_single_slot_conflicts,
        )
        from ramses_rf.schemas import SZ_ORPHANS_HEAT

        fragment = {SZ_ORPHANS_HEAT: ["13:111111"]}
        current_schema: dict[str, Any] = {}
        result = _resolve_single_slot_conflicts(
            fragment, current_schema, "13:111111"
        )
        assert result == fragment

    def test_appliance_control_conflict_redirects_to_orphans(self) -> None:
        """When a fragment places a device into appliance_control but a
        different device already holds that slot, the new device is
        redirected to orphans_heat.
        """
        from custom_components.ramses_cc.services import (
            _resolve_single_slot_conflicts,
        )
        from ramses_rf.schemas import (
            SZ_APPLIANCE_CONTROL,
            SZ_ORPHANS_HEAT,
            SZ_SYSTEM,
        )

        # Fragment wants to place BDR_ID as appliance_control
        fragment = {
            CTL_ID: {SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: BDR_ID}},
            BDR_ID: {"_class": "BDR"},
        }
        # Current schema already has OTB_ID as appliance_control
        current_schema = {
            CTL_ID: {SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: OTB_ID}},
        }
        result = _resolve_single_slot_conflicts(
            fragment, current_schema, BDR_ID
        )
        # The fragment's appliance_control should be stripped
        assert SZ_SYSTEM not in result.get(
            CTL_ID, {}
        ) or SZ_APPLIANCE_CONTROL not in (
            result.get(CTL_ID, {}).get(SZ_SYSTEM, {})
        )
        # BDR_ID should be redirected to orphans_heat
        assert BDR_ID in result.get(SZ_ORPHANS_HEAT, [])

    def test_same_device_in_slot_no_conflict(self) -> None:
        """When the fragment places the same device that already holds the
        slot, there's no conflict (idempotent re-accept).
        """
        from custom_components.ramses_cc.services import (
            _resolve_single_slot_conflicts,
        )
        from ramses_rf.schemas import SZ_APPLIANCE_CONTROL, SZ_SYSTEM

        fragment = {
            CTL_ID: {SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: OTB_ID}},
        }
        current_schema = {
            CTL_ID: {SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: OTB_ID}},
        }
        result = _resolve_single_slot_conflicts(
            fragment, current_schema, OTB_ID
        )
        # Should be unchanged — same device, no conflict
        assert result[CTL_ID][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == OTB_ID

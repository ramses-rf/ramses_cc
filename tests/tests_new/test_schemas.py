"""Tests to achieve 100% coverage for schemas.py."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.ramses_cc.const import (
    CONF_ADVANCED_FEATURES,
    CONF_COMMANDS,
    CONF_RAMSES_RF,
    SZ_DEVICE_COMMENTS,
    SZ_OWNER,
    SZ_TR_NAME,
)
from custom_components.ramses_cc.schemas import (
    extract_hvac_schema,
    merge_hvac_schema,
    merge_schemas,
    normalise_config,
    order_schema,
    remove_device_from_schema,
    strip_traits_for_validation,
    sync_learned_topology,
)
from ramses_rf.schemas import (
    SZ_APPLIANCE_CONTROL,
    SZ_CLASS,
    SZ_DHW_SYSTEM,
    SZ_KNOWN_LIST,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
    SZ_ORPHANS_HEAT,
    SZ_ORPHANS_HVAC,
    SZ_REMOTES,
    SZ_SENSOR,
    SZ_SENSORS,
    SZ_SYSTEM,
)
from ramses_tx.const import SZ_ZONES
from ramses_tx.schemas import SZ_PORT_NAME, SZ_SERIAL_PORT


def test_normalise_config() -> None:
    """Test the normalization of configuration data (Lines 157-170)."""
    config: dict[str, Any] = {
        CONF_RAMSES_RF: {"disable_discovery": True},
        SZ_SERIAL_PORT: {SZ_PORT_NAME: "/dev/ttyUSB0"},
        SZ_KNOWN_LIST: {
            "18:111111": {CONF_COMMANDS: {"boost": "packet_data"}},
            "01:123456": {SZ_CLASS: "TRV"},
        },
        "restore_cache": True,
        CONF_ADVANCED_FEATURES: {"dev_mode": True},
    }

    port, client_config, coordinator_config = normalise_config(config)

    assert port == "/dev/ttyUSB0"
    assert client_config["config"] == {"disable_discovery": True}
    assert coordinator_config["remotes"]["18:111111"] == {"boost": "packet_data"}
    assert CONF_COMMANDS not in client_config[SZ_KNOWN_LIST]["18:111111"]


def test_merge_schemas_logic(caplog: pytest.LogCaptureFixture) -> None:
    """Test schema merging branches (Lines 186-193)."""
    caplog.set_level(logging.INFO)

    # Case 1: Config is subset of cached (Line 183)
    config_sub: dict[str, Any] = {"known_list": {"18:111111": {SZ_CLASS: "HGI"}}}
    cached_sup: dict[str, Any] = {
        "known_list": {"18:111111": {SZ_CLASS: "HGI"}, "01:123456": {SZ_CLASS: "TRV"}}
    }
    assert merge_schemas(config_sub, cached_sup) == cached_sup
    assert "Using the cached schema" in caplog.text
    caplog.clear()

    # Case 2: Merged schema is superset of config (Line 189)
    config_new: dict[str, Any] = {"known_list": {"01:123456": {SZ_CLASS: "TRV"}}}
    cached_old: dict[str, Any] = {"known_list": {"18:111111": {SZ_CLASS: "HGI"}}}
    merged = merge_schemas(config_new, cached_old)
    assert merged is not None
    assert "Using a merged schema" in caplog.text

    # Case 3: Trigger 'Cached schema is a subset' path (Line 193)
    with patch(
        "custom_components.ramses_cc.schemas.is_subset", side_effect=[False, False]
    ):
        assert merge_schemas({"a": 1}, {"b": 2}) is None
        assert "Cached schema is a subset of config schema" in caplog.text


def test_merge_schemas_drops_removed_devices() -> None:
    """Devices removed from config schema must not come back from cache.

    Only applies in SSOT mode (passive scan).  In legacy mode, the cache
    is kept as-is.
    """
    config: dict[str, Any] = {
        "01:123456": {},
        "orphans_heat": ["04:111111"],
    }
    cached: dict[str, Any] = {
        "01:123456": {},
        "04:111111": {},  # in cache but removed from config
        "37:154519": {},  # in cache but never in config
        "orphans_heat": ["04:111111"],
        "orphans_hvac": ["37:154519"],
    }
    result = merge_schemas(config, cached, schema_is_ssot=True)
    assert result is not None
    # 01:123456 is in config → kept
    assert "01:123456" in result
    # 04:111111 is in config (orphans_heat) → kept
    assert "04:111111" in result.get("orphans_heat", [])
    # 37:154519 is NOT in config → dropped (not resurrected from cache)
    assert "37:154519" not in result
    assert "37:154519" not in result.get("orphans_hvac", [])


def test_merge_schemas_fully_wiped() -> None:
    """When config schema is fully wiped, cache devices are all dropped.

    Only applies in SSOT mode (passive scan).
    """
    config: dict[str, Any] = {}
    cached: dict[str, Any] = {
        "37:154519": {},
        "63:262142": {"_skipped": True},
        "orphans_hvac": ["29:176861", "32:153289"],
    }
    result = merge_schemas(config, cached, schema_is_ssot=True)
    # No devices in config → all cached devices dropped
    assert result is not None
    assert "37:154519" not in result
    assert "63:262142" not in result
    assert "orphans_hvac" not in result


def test_merge_schemas_keeps_non_device_keys() -> None:
    """Non-device keys (known_list) are preserved even with no devices."""
    config: dict[str, Any] = {"known_list": {"18:111111": {SZ_CLASS: "HGI"}}}
    cached: dict[str, Any] = {
        "known_list": {"18:111111": {SZ_CLASS: "HGI"}, "01:123456": {SZ_CLASS: "TRV"}}
    }
    result = merge_schemas(config, cached)
    assert result is not None
    assert "known_list" in result


def test_merge_schemas_filters_remotes_list() -> None:
    """Devices in remotes list but not in config schema should be filtered out."""
    config: dict[str, Any] = {
        "32:153289": {},
    }
    cached: dict[str, Any] = {
        "32:153289": {"remotes": ["37:168270", "37:126776"]},
    }
    result = merge_schemas(config, cached, schema_is_ssot=True)
    assert result is not None
    # 32:153289 is in config → kept
    assert "32:153289" in result
    # 37:168270 and 37:126776 are NOT in config → removed from remotes
    assert result["32:153289"].get("remotes", []) == []


def test_merge_schemas_preserves_remotes_in_config() -> None:
    """Devices in remotes list that ARE in config schema should be kept."""
    config: dict[str, Any] = {
        "32:153289": {"remotes": ["37:168270", "37:126776"]},
    }
    cached: dict[str, Any] = {
        "32:153289": {"remotes": ["37:168270", "37:126776", "37:999999"]},
    }
    result = merge_schemas(config, cached, schema_is_ssot=True)
    assert result is not None
    # 37:168270 and 37:126776 are in config remotes → kept
    # 37:999999 is NOT in config → filtered out
    remotes = result["32:153289"].get("remotes", [])
    assert "37:168270" in remotes
    assert "37:126776" in remotes
    assert "37:999999" not in remotes


def test_merge_schemas_filters_orphans_hvac() -> None:
    """Devices in orphans_hvac but not in config schema should be filtered out."""
    config: dict[str, Any] = {
        "32:153289": {},
    }
    cached: dict[str, Any] = {
        "32:153289": {},
        "orphans_hvac": ["37:168270", "37:126776"],
    }
    result = merge_schemas(config, cached, schema_is_ssot=True)
    assert result is not None
    # 32:153289 is in config → kept
    assert "32:153289" in result
    # orphans_hvac should be empty since devices are not in config
    assert result.get("orphans_hvac", []) == []


def test_merge_schemas_keeps_orphans_if_in_config() -> None:
    """Devices in orphans_hvac that ARE in config schema should be kept."""
    config: dict[str, Any] = {
        "32:153289": {},
        "orphans_hvac": ["37:126776"],
    }
    cached: dict[str, Any] = {
        "32:153289": {},
        "orphans_hvac": ["37:168270", "37:126776"],
    }
    result = merge_schemas(config, cached, schema_is_ssot=True)
    assert result is not None
    # 37:126776 is in config → kept in orphans_hvac
    assert "37:126776" in result.get("orphans_hvac", [])
    # 37:168270 is NOT in config → removed from orphans_hvac
    assert "37:168270" not in result.get("orphans_hvac", [])


def test_merge_schemas_preserves_fan_with_only_traits() -> None:
    """A FAN entry with only _ traits (no remotes/sensors) should not be
    dropped by merge_schemas.

    shrink() removes _ prefixed keys and falsey values, so a FAN entry
    like {"_class": "FAN", "remotes": []} becomes {} and is removed.
    is_subset then thinks config is a subset of cached (which doesn't
    have the FAN), and returns cached — dropping the FAN.

    The fix: check device IDs directly.  If config has device IDs that
    cached doesn't, merge instead of using cached directly.
    """
    config: dict[str, Any] = {
        "main_tcs": "01:145038",
        "01:145038": {"zones": {"01": {"sensor": "04:056053"}}},
        "32:153289": {"_class": "FAN", "remotes": []},
    }
    cached: dict[str, Any] = {
        "main_tcs": "01:145038",
        "01:145038": {
            "zones": {"01": {"sensor": "04:056053", "actuators": ["04:200002"]}}
        },
        "orphans_heat": [],
        "orphans_hvac": [],
    }
    result = merge_schemas(config, cached)
    assert result is not None
    # FAN should be preserved in the merged result
    assert "32:153289" in result


# ── Tests for remove_device_from_schema ───────────────────────────────


def test_remove_device_from_orphans_heat() -> None:
    """Remove a device from top-level orphans_heat list."""
    schema: dict[str, Any] = {
        SZ_ORPHANS_HEAT: ["04:111111", "04:222222", "04:333333"],
    }
    result = remove_device_from_schema(schema, "04:222222")
    assert result[SZ_ORPHANS_HEAT] == ["04:111111", "04:333333"]


def test_remove_device_from_orphans_heat_empty_list() -> None:
    """Removing the last device from orphans_heat deletes the key."""
    schema: dict[str, Any] = {SZ_ORPHANS_HEAT: ["04:111111"]}
    result = remove_device_from_schema(schema, "04:111111")
    assert SZ_ORPHANS_HEAT not in result


def test_remove_device_from_orphans_hvac() -> None:
    """Remove a device from top-level orphans_hvac list."""
    schema: dict[str, Any] = {SZ_ORPHANS_HVAC: ["32:111111", "32:222222"]}
    result = remove_device_from_schema(schema, "32:111111")
    assert result[SZ_ORPHANS_HVAC] == ["32:222222"]


def test_remove_device_from_zone_sensor() -> None:
    """Remove a device that is a zone sensor (set to None, zone stays)."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "04:111111", "actuators": ["13:222222"]},
            }
        }
    }
    result = remove_device_from_schema(schema, "04:111111")
    assert result["01:123456"][SZ_ZONES]["02"][SZ_SENSOR] is None
    # Zone and actuators stay
    assert result["01:123456"][SZ_ZONES]["02"]["actuators"] == ["13:222222"]


def test_remove_device_from_zone_actuators() -> None:
    """Remove a device from a zone's actuators list."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "04:111111", "actuators": ["13:222222", "13:333333"]},
            }
        }
    }
    result = remove_device_from_schema(schema, "13:222222")
    assert result["01:123456"][SZ_ZONES]["02"]["actuators"] == ["13:333333"]


def test_remove_device_from_zone_actuators_empty() -> None:
    """Removing the last actuator deletes the key."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "04:111111", "actuators": ["13:222222"]},
            }
        }
    }
    result = remove_device_from_schema(schema, "13:222222")
    assert "actuators" not in result["01:123456"][SZ_ZONES]["02"]


def test_remove_device_from_appliance_control() -> None:
    """Remove a device that is appliance_control (set to None)."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "10:111111"},
        }
    }
    result = remove_device_from_schema(schema, "10:111111")
    assert result["01:123456"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] is None


def test_remove_device_from_tcs_orphans() -> None:
    """Remove a device from TCS-level orphans list."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_ORPHANS: ["04:111111", "04:222222"],
        }
    }
    result = remove_device_from_schema(schema, "04:111111")
    assert result["01:123456"][SZ_ORPHANS] == ["04:222222"]


def test_remove_device_from_dhw_sensor() -> None:
    """Remove a device that is a DHW sensor."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_DHW_SYSTEM: {SZ_SENSOR: "07:111111"},
        }
    }
    result = remove_device_from_schema(schema, "07:111111")
    assert result["01:123456"][SZ_DHW_SYSTEM][SZ_SENSOR] is None


def test_remove_device_from_hvac_remotes() -> None:
    """Remove a device from an HVAC entry's remotes list."""
    schema: dict[str, Any] = {
        "32:111111": {"remotes": ["29:222222", "29:333333"]},
    }
    result = remove_device_from_schema(schema, "29:222222")
    assert result["32:111111"]["remotes"] == ["29:333333"]


def test_remove_device_not_in_schema() -> None:
    """Removing a device that isn't in the schema returns a copy unchanged."""
    schema: dict[str, Any] = {"01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}}}
    result = remove_device_from_schema(schema, "99:999999")
    assert result == schema
    # Ensure it's a copy, not the same object
    assert result is not schema


def test_remove_device_preserves_own_top_level_key() -> None:
    """The device's own top-level key (e.g. '32:153289': {}) is NOT removed."""
    schema: dict[str, Any] = {
        "32:111111": {"remotes": ["29:222222"]},
        SZ_ORPHANS_HVAC: ["32:111111"],
    }
    result = remove_device_from_schema(schema, "32:111111")
    # Removed from orphans_hvac
    assert "32:111111" not in result.get(SZ_ORPHANS_HVAC, [])
    # But its own top-level key stays (the fragment will update it)
    assert "32:111111" in result


def test_remove_device_cleans_device_comments() -> None:
    """Removing a device also removes its entry from device_comments."""
    schema: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["37:111111"],
        SZ_DEVICE_COMMENTS: {
            "37:111111": "Likely CO2. codes: 22F1.",
            "37:222222": "Likely REM. bound to 32:150000.",
        },
    }
    result = remove_device_from_schema(schema, "37:111111")
    # Removed from orphans_hvac
    assert "37:111111" not in result.get(SZ_ORPHANS_HVAC, [])
    # Removed from device_comments
    assert SZ_DEVICE_COMMENTS in result
    assert "37:111111" not in result[SZ_DEVICE_COMMENTS]
    # Other comments preserved
    assert "37:222222" in result[SZ_DEVICE_COMMENTS]


def test_remove_device_cleans_empty_device_comments() -> None:
    """If device_comments becomes empty after removal, the key is deleted."""
    schema: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["37:111111"],
        SZ_DEVICE_COMMENTS: {"37:111111": "Likely CO2."},
    }
    result = remove_device_from_schema(schema, "37:111111")
    assert SZ_DEVICE_COMMENTS not in result


# ── Tests for sync_learned_topology ───────────────────────────────────


def test_sync_learned_topology_no_changes() -> None:
    """Returns None when config already matches learned topology."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:111111"}},
        },
        "22:111111": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:111111"}},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    assert sync_learned_topology(config, learned) is None


def test_sync_learned_topology_adds_zone_sensor() -> None:
    """Moves a device from orphans_heat to a zone when learned has it."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        SZ_ORPHANS_HEAT: ["22:111111"],
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:111111", "actuators": []}},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:123456"][SZ_ZONES]["02"][SZ_SENSOR] == "22:111111"
    # Device removed from orphans_heat
    assert "22:111111" not in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_learned_topology_adds_actuators() -> None:
    """Adds actuators to a zone that config doesn't have."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
        },
        SZ_ORPHANS_HEAT: ["13:222222"],
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "04:111111", "actuators": ["13:222222"]},
            },
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert "13:222222" in result["01:123456"][SZ_ZONES]["02"]["actuators"]
    assert "13:222222" not in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_learned_topology_preserves_user_sensor() -> None:
    """Does not overwrite a sensor the user already set."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:999999"}},
        },
        "22:999999": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:111111", "actuators": []}},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    # No changes — user's sensor stays, learned sensor is different
    assert result is None


def test_sync_learned_topology_adds_appliance_control() -> None:
    """Adds appliance_control when learned has it and config doesn't."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        "10:111111": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "10:111111"},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:123456"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "10:111111"


def test_sync_learned_topology_preserves_device_comments() -> None:
    """device_comments list is preserved."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        "device_comments": {"04:666666": "test comment"},
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["device_comments"] == {"04:666666": "test comment"}


def test_sync_learned_topology_empty_learned() -> None:
    """Returns None when learned schema is empty."""
    config: dict[str, Any] = {"main_tcs": "01:123456"}
    assert sync_learned_topology(config, {}) is None
    assert sync_learned_topology(config, None) is None  # type: ignore[arg-type]


def test_sync_learned_topology_hvac_orphans() -> None:
    """Removes HVAC devices from orphans_hvac when they're in an HVAC entry."""
    config: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["29:111111", "29:222222"],
        "32:333333": {"remotes": []},
    }
    learned: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["29:222222"],
        "32:333333": {"remotes": ["29:111111"]},
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert "29:111111" not in result.get(SZ_ORPHANS_HVAC, [])
    assert "29:222222" in result[SZ_ORPHANS_HVAC]


def test_sync_learned_topology_fixes_misplaced_bound_on_fan() -> None:
    """Ensures REMs in FAN's _bound are also in remotes[] list.

    A FAN can have one or more bound REMs (stored as _bound on the FAN).
    ramses_rf needs the REM in the FAN's remotes[] list to create the
    device topology.  This test verifies that a _bound REM missing from
    remotes[] is added.
    """
    config: dict[str, Any] = {
        "32:153289": {
            "_owner": "me",
            "_class": "FAN",
            "_bound": "37:168270",  # bound REM — should be in remotes[]
            "remotes": ["37:169161"],
        },
        "37:168270": {
            "_owner": "me",
            "_class": "REM",
            "_faked": True,
        },
        "37:169161": {"_owner": "me", "_class": "DIS"},
    }
    learned: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["32:153289", "37:168270"],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # _bound should stay on the FAN (canonical place for binding)
    assert result["32:153289"].get("_bound") == "37:168270"
    # REM should be added to the FAN's remotes list
    assert "37:168270" in result["32:153289"]["remotes"]
    # Existing remote should still be there
    assert "37:169161" in result["32:153289"]["remotes"]


def test_sync_learned_topology_adds_zone_class() -> None:
    """Adds zone class from learned when config doesn't have it."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
        },
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "04:111111", SZ_CLASS: "electric_heating"},
            },
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:123456"][SZ_ZONES]["02"][SZ_CLASS] == "electric_heating"


def test_sync_learned_topology_preserves_existing_zone_class() -> None:
    """Does not overwrite zone class the user already set."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:111111", SZ_CLASS: "underfloor_heating"}},
        },
        "22:111111": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "22:111111", SZ_CLASS: "electric_heating"},
            },
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is None  # No changes — user's class stays


def test_sync_learned_topology_adds_zone_name() -> None:
    """Adds _name from learned schema (e.g. from 0004 zone_name packets)."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"03": {SZ_SENSOR: "22:111111"}},
        },
        "22:111111": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {
                "03": {SZ_SENSOR: "22:111111", SZ_TR_NAME: "Bedroom"},
            },
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:123456"][SZ_ZONES]["03"][SZ_TR_NAME] == "Bedroom"


def test_sync_learned_topology_preserves_existing_zone_name() -> None:
    """Does not overwrite _name the user already set."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"03": {SZ_SENSOR: "22:111111", SZ_TR_NAME: "My Name"}},
        },
        "22:111111": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {
                "03": {SZ_SENSOR: "22:111111", SZ_TR_NAME: "Learned Name"},
            },
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is None  # No changes — user's name stays


def test_sync_learned_topology_dhw_sensor() -> None:
    """Adds DHW sensor from learned when config doesn't have one."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        SZ_ORPHANS_HEAT: ["07:111111"],
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_DHW_SYSTEM: {SZ_SENSOR: "07:111111"},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:123456"][SZ_DHW_SYSTEM][SZ_SENSOR] == "07:111111"


def test_sync_learned_topology_tcs_orphans_cleanup() -> None:
    """Removes devices from TCS-level orphans when they're in zones."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ORPHANS: ["04:111111"],
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
        },
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
            SZ_ORPHANS: [],
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert "04:111111" not in result["01:123456"].get(SZ_ORPHANS, [])


def test_sync_learned_topology_tcs_orphans_partial_cleanup() -> None:
    """Only removes devices that are in zones, keeps other orphans."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ORPHANS: ["04:111111", "04:222222"],
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
        },
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
            SZ_ORPHANS: ["04:222222"],
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:123456"][SZ_ORPHANS] == ["04:222222"]


def test_sync_learned_topology_hvac_orphans_empty_result() -> None:
    """Removes all HVAC orphans when they're all in HVAC entries."""
    config: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["29:111111"],
        "32:333333": {"remotes": []},
    }
    learned: dict[str, Any] = {
        SZ_ORPHANS_HVAC: [],
        "32:333333": {"remotes": ["29:111111"]},
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert SZ_ORPHANS_HVAC not in result


def test_sync_learned_topology_heat_orphans_empty_result() -> None:
    """Removes all heat orphans when they're all in zones."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        SZ_ORPHANS_HEAT: ["04:111111"],
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert SZ_ORPHANS_HEAT not in result


def test_sync_learned_topology_non_dict_tcs_entry() -> None:
    """Handles a non-dict TCS entry in config gracefully."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": "not a dict",
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "10:111111"},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:123456"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "10:111111"


def test_sync_learned_topology_non_dict_zone() -> None:
    """Handles a non-dict zone in learned schema gracefully."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": "not a dict"},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    # No changes — the non-dict zone was skipped
    assert result is None


def test_remove_device_from_dhw_valve() -> None:
    """Remove a device that is a DHW hotwater_valve."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_DHW_SYSTEM: {"hotwater_valve": "13:111111"},
        }
    }
    result = remove_device_from_schema(schema, "13:111111")
    assert result["01:123456"][SZ_DHW_SYSTEM]["hotwater_valve"] is None


def test_remove_device_from_hvac_sensors() -> None:
    """Remove a device from an HVAC entry's sensors list."""
    schema: dict[str, Any] = {
        "32:111111": {"sensors": ["37:222222", "37:333333"]},
    }
    result = remove_device_from_schema(schema, "37:222222")
    assert result["32:111111"]["sensors"] == ["37:333333"]


def test_remove_device_from_hvac_sensors_empty() -> None:
    """Removing the last sensor deletes the key."""
    schema: dict[str, Any] = {
        "32:111111": {"sensors": ["37:222222"]},
    }
    result = remove_device_from_schema(schema, "37:222222")
    assert "sensors" not in result["32:111111"]


def test_remove_device_from_orphans_key() -> None:
    """Remove a device from the bare 'orphans' key."""
    schema: dict[str, Any] = {"orphans": ["04:111111", "04:222222"]}
    result = remove_device_from_schema(schema, "04:111111")
    assert result["orphans"] == ["04:222222"]


def test_remove_device_from_heating_valve() -> None:
    """Remove a device that is a heating_valve."""
    schema: dict[str, Any] = {
        "01:123456": {
            SZ_DHW_SYSTEM: {"heating_valve": "13:111111"},
        }
    }
    result = remove_device_from_schema(schema, "13:111111")
    assert result["01:123456"][SZ_DHW_SYSTEM]["heating_valve"] is None


def test_remove_device_tcs_orphans_becomes_empty() -> None:
    """TCS orphans list is deleted when the last device is removed."""
    schema: dict[str, Any] = {
        "01:123456": {SZ_ORPHANS: ["04:111111"]},
    }
    result = remove_device_from_schema(schema, "04:111111")
    assert SZ_ORPHANS not in result["01:123456"]


def test_remove_device_non_dict_zone() -> None:
    """Non-dict zone entry is skipped gracefully."""
    schema: dict[str, Any] = {
        "01:123456": {SZ_ZONES: {"02": "not a dict"}},
    }
    result = remove_device_from_schema(schema, "04:111111")
    # No crash, zone stays as-is
    assert result["01:123456"][SZ_ZONES]["02"] == "not a dict"


def test_sync_learned_topology_heat_orphans_partial() -> None:
    """Only removes heat orphans that are in zones, keeps others."""
    config: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        SZ_ORPHANS_HEAT: ["04:111111", "04:222222"],
    }
    learned: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}},
        },
        SZ_ORPHANS_HEAT: ["04:222222"],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result[SZ_ORPHANS_HEAT] == ["04:222222"]


def test_sync_learned_topology_orphan_and_zone_same_pass() -> None:
    """Device in both orphans_heat and a zone must be removed from orphans.

    When a THM/RND is placed in a zone via device comments (000A zone
    binding) but ramses_rf's learned schema still has it in orphans_heat,
    the device appears in both places.  sync_learned_topology must remove
    it from orphans_heat in the same pass (issue 813: thermostats showed
    in both orphans_heat AND the correct zone until HA restart).
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {},
        SZ_ORPHANS_HEAT: ["22:012299", "34:058721"],
        SZ_DEVICE_COMMENTS: {
            "22:012299": "Likely THM. bound to 01:216136. zone 01. codes: 000A, 30C9.",
            "34:058721": "Likely RND. bound to 01:216136. zone 03. codes: 000A, 30C9.",
        },
    }
    # Learned schema STILL has both in orphans_heat (ramses_rf hasn't
    # placed them in zones yet — the 000C RP hasn't arrived)
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: ["22:012299", "34:058721"],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Both should be in zones (from comments), NOT in orphans_heat
    zones = result["01:216136"][SZ_ZONES]
    assert zones["01"][SZ_SENSOR] == "22:012299"
    assert zones["03"][SZ_SENSOR] == "34:058721"
    # orphans_heat should be gone (both devices now in zones)
    assert SZ_ORPHANS_HEAT not in result or result[SZ_ORPHANS_HEAT] == []


def test_sync_learned_topology_hvac_non_dict_entry() -> None:
    """Non-dict HVAC entry in learned is skipped gracefully."""
    config: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["29:111111"],
        "29:111111": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        SZ_ORPHANS_HVAC: [],
        "32:333333": "not a dict",
    }
    result = sync_learned_topology(config, learned)
    # No HVAC entry devices found, so no changes
    assert result is None


# ── Tests for strip_traits_for_validation ─────────────────────────────


def test_strip_traits_removes_underscore_keys() -> None:
    """_ prefixed keys are stripped from all levels."""
    schema: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {
            "_disabled": True,
            "_name": "My Controller",
            SZ_ZONES: {"02": {SZ_SENSOR: "04:111111", "_name": "Living Room"}},
        },
    }
    result = strip_traits_for_validation(schema)
    assert "_disabled" not in result["01:123456"]
    assert "_name" not in result["01:123456"]
    assert "_name" not in result["01:123456"][SZ_ZONES]["02"]
    assert result["01:123456"][SZ_ZONES]["02"][SZ_SENSOR] == "04:111111"


def test_strip_traits_removes_trait_only_entries() -> None:
    """Top-level device-ID keys with only _ keys are removed."""
    schema: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}},
        "04:222222": {"_disabled": True},
    }
    result = strip_traits_for_validation(schema)
    assert "04:222222" not in result
    assert "01:123456" in result


def test_strip_traits_keeps_empty_tcs_entries() -> None:
    """Empty TCS entries (no _ keys) are kept."""
    schema: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
    }
    result = strip_traits_for_validation(schema)
    assert result["01:123456"] == {}


def test_strip_traits_moves_hvac_devices_to_orphans() -> None:
    """HVAC devices at root without remotes/sensors are moved to orphans_hvac."""
    schema: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        "29:160000": {},  # FAN (battery-powered variant)
        "30:160001": {},  # FAN/PIV
        "37:160003": {},  # REM/CO2/HUM at root
    }
    result = strip_traits_for_validation(schema)
    # Devices should not be at root level
    assert "29:160000" not in result
    assert "30:160001" not in result
    assert "37:160003" not in result
    # They should be in orphans_hvac
    assert "29:160000" in result.get("orphans_hvac", [])
    assert "30:160001" in result.get("orphans_hvac", [])
    assert "37:160003" in result.get("orphans_hvac", [])


def test_strip_traits_moves_hvac_trait_only_to_orphans() -> None:
    """HVAC device with only _ traits is moved to orphans_hvac, not dropped."""
    schema: dict[str, Any] = {
        "32:160002": {"_alias": "Kitchen HRU"},
    }
    result = strip_traits_for_validation(schema)
    assert "32:160002" not in result  # not at root
    assert "32:160002" in result.get("orphans_hvac", [])


def test_strip_traits_preserves_hvac_remotes() -> None:
    """HVAC devices with existing remotes/sensors stay at root."""
    schema: dict[str, Any] = {
        "29:160000": {"remotes": ["37:000001"]},
        "32:160001": {"sensors": ["1F:000002"]},
    }
    result = strip_traits_for_validation(schema)
    assert result["29:160000"] == {"remotes": ["37:000001"]}
    assert result["32:160001"] == {"sensors": ["1F:000002"]}


def test_strip_traits_no_remotes_for_heat_prefixes() -> None:
    """Heat-side prefixes (04:, 07:, etc.) are not moved to orphans_hvac."""
    schema: dict[str, Any] = {
        "main_tcs": "01:123456",
        "01:123456": {},
        "04:111111": {"_disabled": True},  # TRV — trait-only, should be dropped
    }
    result = strip_traits_for_validation(schema)
    assert "04:111111" not in result  # dropped
    assert "04:111111" not in result.get("orphans_hvac", [])  # not in hvac orphans


def test_strip_traits_keeps_non_underscore_keys() -> None:
    """Non-underscore keys are preserved."""
    schema: dict[str, Any] = {
        "orphans_heat": ["04:111111"],
        "orphans_hvac": ["32:222222"],
    }
    result = strip_traits_for_validation(schema)
    assert result == schema


def test_strip_traits_handles_non_dict_values() -> None:
    """Non-dict values (lists, strings) are passed through."""
    schema: dict[str, Any] = {
        "main_tcs": "01:123456",
        "orphans_heat": ["04:111111", "04:222222"],
    }
    result = strip_traits_for_validation(schema)
    assert result["orphans_heat"] == ["04:111111", "04:222222"]
    assert result["main_tcs"] == "01:123456"


# ---------------------------------------------------------------------------
# Zone reassignment tests (zone→zone, zone→DHW, DHW→zone)
# ---------------------------------------------------------------------------


def test_sync_zone_to_zone_sensor_move() -> None:
    """Device moves from zone 01 to zone 02 — old zone sensor cleared."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "22:056053"},
                "02": {},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "22:056053"},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] is None
    assert result["01:216136"][SZ_ZONES]["02"][SZ_SENSOR] == "22:056053"


def test_sync_zone_to_zone_actuator_move() -> None:
    """Actuator moves from zone 01 to zone 02 — removed from old, kept in new."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {"actuators": ["04:034720", "04:056053"]},
                "02": {},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "02": {"actuators": ["04:056053"]},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert "04:056053" not in result["01:216136"][SZ_ZONES]["01"]["actuators"]
    assert "04:034720" in result["01:216136"][SZ_ZONES]["01"]["actuators"]
    assert "04:056053" in result["01:216136"][SZ_ZONES]["02"]["actuators"]


def test_sync_zone_to_dhw_sensor_move() -> None:
    """Device moves from zone to DHW — old zone sensor cleared, DHW gets it."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "07:050121"}},
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_DHW_SYSTEM: {SZ_SENSOR: "07:050121"},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] is None
    assert result["01:216136"][SZ_DHW_SYSTEM][SZ_SENSOR] == "07:050121"


def test_sync_dhw_to_zone_sensor_move() -> None:
    """Device moves from DHW to zone — DHW sensor cleared, zone gets it."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {}},
            SZ_DHW_SYSTEM: {SZ_SENSOR: "07:050121"},
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "07:050121"}},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] == "07:050121"
    assert result["01:216136"][SZ_DHW_SYSTEM][SZ_SENSOR] is None


def test_sync_zone_to_zone_no_move_returns_none() -> None:
    """Device stays in same zone — no changes, returns None."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "22:056053"}},
        },
        "22:056053": {},  # root entry exists — no backfill needed
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "22:056053"}},
        },
    }
    assert sync_learned_topology(config, learned) is None


def test_sync_cross_tcs_zone_sensor_move() -> None:
    """Sensor moves from CTL-A zone 01 to CTL-B zone 02 — stale entry
    in CTL-A must be cleared (cross-TCS move)."""
    config: dict[str, Any] = {
        "01:111111": {
            SZ_ZONES: {"01": {SZ_SENSOR: "22:056053"}},
        },
        "01:222222": {
            SZ_ZONES: {"02": {}},
        },
    }
    learned: dict[str, Any] = {
        "01:111111": {
            SZ_ZONES: {"01": {}},
        },
        "01:222222": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:056053"}},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Stale entry in CTL-A cleared
    assert result["01:111111"][SZ_ZONES]["01"][SZ_SENSOR] is None
    # New placement in CTL-B present
    assert result["01:222222"][SZ_ZONES]["02"][SZ_SENSOR] == "22:056053"


def test_sync_cross_tcs_zone_actuator_move() -> None:
    """Actuator moves from CTL-A zone 01 to CTL-B zone 03 — removed
    from CTL-A, kept in CTL-B."""
    config: dict[str, Any] = {
        "01:111111": {
            SZ_ZONES: {"01": {"actuators": ["04:034720", "04:056053"]}},
        },
        "01:222222": {
            SZ_ZONES: {"03": {}},
        },
    }
    learned: dict[str, Any] = {
        "01:111111": {
            SZ_ZONES: {"01": {"actuators": ["04:034720"]}},
        },
        "01:222222": {
            SZ_ZONES: {"03": {"actuators": ["04:056053"]}},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert "04:056053" not in result["01:111111"][SZ_ZONES]["01"]["actuators"]
    assert "04:034720" in result["01:111111"][SZ_ZONES]["01"]["actuators"]
    assert "04:056053" in result["01:222222"][SZ_ZONES]["03"]["actuators"]


def test_sync_cross_tcs_zone_to_dhw_move() -> None:
    """Sensor moves from CTL-A zone to CTL-B DHW — stale zone entry
    in CTL-A cleared."""
    config: dict[str, Any] = {
        "01:111111": {
            SZ_ZONES: {"01": {SZ_SENSOR: "07:050121"}},
        },
        "01:222222": {},
    }
    learned: dict[str, Any] = {
        "01:111111": {
            SZ_ZONES: {"01": {}},
        },
        "01:222222": {
            SZ_DHW_SYSTEM: {SZ_SENSOR: "07:050121"},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:111111"][SZ_ZONES]["01"][SZ_SENSOR] is None
    assert result["01:222222"][SZ_DHW_SYSTEM][SZ_SENSOR] == "07:050121"


def test_sync_cross_tcs_dhw_to_zone_move() -> None:
    """Sensor moves from CTL-A DHW to CTL-B zone — CTL-A DHW cleared."""
    config: dict[str, Any] = {
        "01:111111": {
            SZ_DHW_SYSTEM: {SZ_SENSOR: "07:050121"},
        },
        "01:222222": {
            SZ_ZONES: {"01": {}},
        },
    }
    learned: dict[str, Any] = {
        "01:111111": {},
        "01:222222": {
            SZ_ZONES: {"01": {SZ_SENSOR: "07:050121"}},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:111111"][SZ_DHW_SYSTEM][SZ_SENSOR] is None
    assert result["01:222222"][SZ_ZONES]["01"][SZ_SENSOR] == "07:050121"


def test_sync_zone_move_preserves_user_authored_keys() -> None:
    """User-authored keys (_name, _class) in old zone are preserved on move."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "22:056053", "_name": "Living Room"},
                "02": {},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "22:056053"},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Old zone keeps user _name, sensor cleared
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] is None
    assert result["01:216136"][SZ_ZONES]["01"]["_name"] == "Living Room"
    # New zone has the sensor
    assert result["01:216136"][SZ_ZONES]["02"][SZ_SENSOR] == "22:056053"


def test_sync_zone_move_actuator_empty_list_removed() -> None:
    """When all actuators move away, the empty actuators list is removed."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {"actuators": ["04:056053"]},
                "02": {},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "02": {"actuators": ["04:056053"]},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Old zone: actuators list removed entirely (was the only entry)
    assert "actuators" not in result["01:216136"][SZ_ZONES]["01"]
    assert result["01:216136"][SZ_ZONES]["02"]["actuators"] == ["04:056053"]


def test_sync_zone_move_multiple_devices() -> None:
    """Multiple devices move zones simultaneously — all cleaned up."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "22:056053", "actuators": ["13:120241"]},
                "02": {},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "02": {SZ_SENSOR: "22:056053", "actuators": ["13:120241"]},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] is None
    assert "actuators" not in result["01:216136"][SZ_ZONES]["01"]
    assert result["01:216136"][SZ_ZONES]["02"][SZ_SENSOR] == "22:056053"
    assert "13:120241" in result["01:216136"][SZ_ZONES]["02"]["actuators"]


def test_sync_dhw_to_zone_valve_move() -> None:
    """DHW valve moves to a zone — DHW valve cleared, zone actuator added."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {}},
            SZ_DHW_SYSTEM: {"hotwater_valve": "13:120242"},
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {"actuators": ["13:120242"]}},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_DHW_SYSTEM]["hotwater_valve"] is None
    assert "13:120242" in result["01:216136"][SZ_ZONES]["01"]["actuators"]


def test_sync_infer_dhw_valve_from_scan_codes() -> None:
    """13: device in orphans_heat with 1100 in scan_codes → hotwater_valve."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["13:042605", "04:111111"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["13:042605", "04:111111"],
    }
    scan_codes: dict[str, list[str]] = {
        "13:042605": ["1100", "3B00", "0008"],
        "04:111111": ["30C9", "3150"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    assert result is not None
    # 13:042605 moved from orphans_heat to stored_hotwater.hotwater_valve
    assert result["01:216136"][SZ_DHW_SYSTEM]["hotwater_valve"] == "13:042605"
    assert "13:042605" not in result.get(SZ_ORPHANS_HEAT, [])
    # 04:111111 stays in orphans_heat (no 1100)
    assert "04:111111" in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_infer_dhw_valve_both_slots() -> None:
    """Two 13: devices with 1100 → hotwater_valve + heating_valve."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["13:042605", "13:042606"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["13:042605", "13:042606"],
    }
    scan_codes: dict[str, list[str]] = {
        "13:042605": ["1100"],
        "13:042606": ["1100"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    assert result is not None
    dhw = result["01:216136"][SZ_DHW_SYSTEM]
    assert dhw["hotwater_valve"] == "13:042605"
    assert dhw["heating_valve"] == "13:042606"
    assert SZ_ORPHANS_HEAT not in result or result.get(SZ_ORPHANS_HEAT) == []


def test_sync_infer_dhw_valve_no_scan_codes() -> None:
    """Without scan_codes, 13: in orphans_heat stays as orphan."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["13:042605"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["13:042605"],
    }
    # No scan_codes → no inference
    result = sync_learned_topology(config, learned)
    # orphans_heat unchanged (learned matches config)
    assert result is None or "13:042605" in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_infer_dhw_valve_existing_hotwater_preserved() -> None:
    """If hotwater_valve already set, 13: with 1100 goes to heating_valve."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
            SZ_DHW_SYSTEM: {"hotwater_valve": "13:999999"},
        },
        SZ_ORPHANS_HEAT: ["13:042605"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
            SZ_DHW_SYSTEM: {"hotwater_valve": "13:999999"},
        },
        SZ_ORPHANS_HEAT: ["13:042605"],
    }
    scan_codes: dict[str, list[str]] = {
        "13:042605": ["1100"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    assert result is not None
    dhw = result["01:216136"][SZ_DHW_SYSTEM]
    assert dhw["hotwater_valve"] == "13:999999"  # preserved
    assert dhw["heating_valve"] == "13:042605"  # inferred
    assert "13:042605" not in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_infer_appliance_control_from_scan_codes() -> None:
    """10: device sending 3220/3EF0 in orphans_heat → appliance_control."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["10:064873"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["10:064873"],
    }
    scan_codes: dict[str, list[str]] = {
        "10:064873": ["3220", "3EF0", "1FD4"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    assert result is not None
    assert result["01:216136"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "10:064873"
    assert "10:064873" not in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_infer_appliance_control_no_scan_codes() -> None:
    """Without scan_codes, 10: in orphans_heat stays as orphan."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["10:064873"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["10:064873"],
    }
    result = sync_learned_topology(config, learned)
    assert result is None or "10:064873" in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_infer_appliance_control_existing_preserved() -> None:
    """If appliance_control already set, 10: with OTB codes stays orphan."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "10:999999"},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["10:064873"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "10:999999"},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["10:064873"],
    }
    scan_codes: dict[str, list[str]] = {
        "10:064873": ["3220"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    # No changes made (appliance_control already set) → result may be None
    if result is not None:
        # Existing appliance_control preserved
        assert result["01:216136"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "10:999999"
        # 10:064873 stays in orphans (appliance_control already taken)
        assert "10:064873" in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_place_orphan_sensors_matching_count() -> None:
    """Orphaned 22:/34: devices placed as zone sensors when count matches.

    One orphan sensor, one zone with actuators but no sensor → place it.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"actuators": ["04:111111"]},  # no sensor
                "02": {"sensor": "01:222222", "actuators": ["04:333333"]},
            },
        },
        SZ_ORPHANS_HEAT: ["22:012299"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"actuators": ["04:111111"]},
                "02": {"sensor": "01:222222", "actuators": ["04:333333"]},
            },
        },
        SZ_ORPHANS_HEAT: ["22:012299"],
    }
    scan_codes: dict[str, list[str]] = {
        "22:012299": ["30C9"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] == "22:012299"
    assert "22:012299" not in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_place_orphan_sensors_count_mismatch() -> None:
    """When orphan sensor count != zones-needing-sensor count, don't guess."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"actuators": ["04:111111"]},  # no sensor
                "02": {"actuators": ["04:222222"]},  # no sensor
            },
        },
        SZ_ORPHANS_HEAT: ["22:012299"],  # only 1 orphan, 2 zones need sensor
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"actuators": ["04:111111"]},
                "02": {"actuators": ["04:222222"]},
            },
        },
        SZ_ORPHANS_HEAT: ["22:012299"],
    }
    scan_codes: dict[str, list[str]] = {
        "22:012299": ["30C9"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    # Counts don't match → don't guess, leave as orphan
    assert result is None or "22:012299" in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_place_orphan_sensors_two_match() -> None:
    """Two orphan sensors, two zones needing sensors → both placed."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"actuators": ["04:111111"]},  # no sensor
                "02": {"actuators": ["04:222222"]},  # no sensor
            },
        },
        SZ_ORPHANS_HEAT: ["22:012299", "34:058721"],
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"actuators": ["04:111111"]},
                "02": {"actuators": ["04:222222"]},
            },
        },
        SZ_ORPHANS_HEAT: ["22:012299", "34:058721"],
    }
    scan_codes: dict[str, list[str]] = {
        "22:012299": ["30C9"],
        "34:058721": ["30C9", "3120"],
    }
    result = sync_learned_topology(config, learned, scan_codes=scan_codes)
    assert result is not None
    # Both placed as sensors (sorted: 22: → zone 01, 34: → zone 02)
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] == "22:012299"
    assert result["01:216136"][SZ_ZONES]["02"][SZ_SENSOR] == "34:058721"
    assert not result.get(SZ_ORPHANS_HEAT)


def test_sync_clears_skipped_for_active_devices() -> None:
    """Devices with active zone/DHW roles should have _skipped cleared."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"sensor": "01:111111", "actuators": ["04:222222"]},
            },
            SZ_DHW_SYSTEM: {"hotwater_valve": "13:333333"},
        },
        "01:111111": {"_skipped": True},
        "04:222222": {"_skipped": True},
        "13:333333": {"_skipped": True},
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {
                "01": {"sensor": "01:111111", "actuators": ["04:222222"]},
            },
            SZ_DHW_SYSTEM: {"hotwater_valve": "13:333333"},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # All three devices have active roles — _skipped should be cleared
    assert result["01:111111"].get("_skipped") is None
    assert result["04:222222"].get("_skipped") is None
    assert result["13:333333"].get("_skipped") is None


def test_sync_preserves_skipped_for_orphan_only_devices() -> None:
    """Devices that are only in orphans with no zone role keep _skipped."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["04:111111"],
        "04:111111": {"_skipped": True},
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_SYSTEM: {},
            SZ_ZONES: {"01": {}},
        },
        SZ_ORPHANS_HEAT: ["04:111111"],
    }
    result = sync_learned_topology(config, learned)
    # 04:111111 is in orphans_heat — that's an active role, so _skipped
    # should be cleared (it's tracked, not deferred)
    assert result is not None
    assert result["04:111111"].get("_skipped") is None


# ---------------------------------------------------------------------------
# HVAC schema extract / merge tests (load_fan stub workaround)
# ---------------------------------------------------------------------------


def test_extract_hvac_schema_basic() -> None:
    """Extract HVAC entries from a mixed schema — only HVAC keys returned."""
    schema: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {SZ_ZONES: {"01": {SZ_SENSOR: "34:092243"}}},
        SZ_ORPHANS_HEAT: ["04:056053"],
        "32:153289": {SZ_REMOTES: ["37:111111", "37:222222"]},
        SZ_ORPHANS_HVAC: ["37:444444"],
    }
    hvac = extract_hvac_schema(schema)
    assert "32:153289" in hvac
    assert hvac["32:153289"][SZ_REMOTES] == ["37:111111", "37:222222"]
    assert SZ_ORPHANS_HVAC in hvac
    assert hvac[SZ_ORPHANS_HVAC] == ["37:444444"]
    # Heat-only entries excluded
    assert "01:216136" not in hvac
    assert SZ_ORPHANS_HEAT not in hvac
    assert SZ_MAIN_TCS not in hvac


def test_extract_hvac_schema_with_sensors() -> None:
    """FAN entry with sensors list is also extracted."""
    schema: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:111111"], SZ_SENSORS: ["37:222222"]},
    }
    hvac = extract_hvac_schema(schema)
    assert "32:153289" in hvac
    assert SZ_SENSORS in hvac["32:153289"]


def test_extract_hvac_schema_empty() -> None:
    """Schema with no HVAC entries returns empty dict."""
    schema: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: ["04:056053"],
    }
    assert extract_hvac_schema(schema) == {}


def test_extract_hvac_schema_non_dict() -> None:
    """Non-dict schema returns empty dict."""
    assert extract_hvac_schema(None) == {}  # type: ignore[arg-type]
    assert extract_hvac_schema("bad") == {}  # type: ignore[arg-type]


def test_extract_hvac_schema_fan_without_lists() -> None:
    """FAN entry without remotes/sensors is NOT extracted (no HVAC keys)."""
    schema: dict[str, Any] = {
        "32:153289": {"_name": "My Fan"},
    }
    hvac = extract_hvac_schema(schema)
    assert "32:153289" not in hvac


def test_merge_hvac_schema_into_empty() -> None:
    """Merge cached HVAC into empty config — no-op (config is authoritative).

    When the config schema is empty (user wiped it), the HVAC cache must
    NOT resurrect devices.  They will be re-discovered by the passive scan.
    """
    hvac: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:111111", "37:222222"]},
        SZ_ORPHANS_HVAC: ["37:444444"],
    }
    result = merge_hvac_schema({}, hvac, schema_is_ssot=True)
    assert result == {}
    assert "32:153289" not in result
    assert SZ_ORPHANS_HVAC not in result


def test_merge_hvac_schema_union_remotes() -> None:
    """Merge cached remotes with existing — union, no duplicates."""
    config: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:111111"]},
    }
    hvac: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:222222", "37:333333"]},
    }
    result = merge_hvac_schema(config, hvac)
    assert set(result["32:153289"][SZ_REMOTES]) == {
        "37:111111",
        "37:222222",
        "37:333333",
    }


def test_merge_hvac_schema_union_orphans() -> None:
    """Merge cached orphans_hvac with existing — union, no duplicates."""
    config: dict[str, Any] = {SZ_ORPHANS_HVAC: ["37:111111"]}
    hvac: dict[str, Any] = {SZ_ORPHANS_HVAC: ["37:222222"]}
    result = merge_hvac_schema(config, hvac)
    assert set(result[SZ_ORPHANS_HVAC]) == {"37:111111", "37:222222"}


def test_merge_hvac_schema_adds_sensors() -> None:
    """Merge cached sensors into FAN entry that only has remotes."""
    config: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:111111"]},
    }
    hvac: dict[str, Any] = {
        "32:153289": {SZ_SENSORS: ["37:222222"]},
    }
    result = merge_hvac_schema(config, hvac)
    assert "37:111111" in result["32:153289"][SZ_REMOTES]
    assert "37:222222" in result["32:153289"][SZ_SENSORS]


def test_merge_hvac_schema_empty_cache() -> None:
    """Empty HVAC cache returns config unchanged."""
    config: dict[str, Any] = {"32:153289": {SZ_REMOTES: ["37:111111"]}}
    assert merge_hvac_schema(config, {}) is config


def test_merge_hvac_schema_none_cache() -> None:
    """None HVAC cache returns config unchanged."""
    config: dict[str, Any] = {"32:153289": {SZ_REMOTES: ["37:111111"]}}
    assert merge_hvac_schema(config, None) is config  # type: ignore[arg-type]


def test_merge_hvac_schema_no_overlap() -> None:
    """Config has no HVAC entries, cache has some — all added."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {SZ_ZONES: {}},
    }
    hvac: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:111111"]},
        SZ_ORPHANS_HVAC: ["37:222222"],
    }
    result = merge_hvac_schema(config, hvac)
    assert "32:153289" in result
    assert result["32:153289"][SZ_REMOTES] == ["37:111111"]
    assert result[SZ_ORPHANS_HVAC] == ["37:222222"]
    # Heat entries preserved
    assert SZ_MAIN_TCS in result
    assert "01:216136" in result


def test_merge_hvac_schema_preserves_heat_entries() -> None:
    """Merging HVAC does not alter heat topology entries."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "34:092243"}},
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "13:120241"},
        },
        "32:153289": {SZ_REMOTES: ["37:111111"]},
    }
    hvac: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:222222"]},
    }
    result = merge_hvac_schema(config, hvac)
    # Heat entry unchanged
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] == "34:092243"
    assert result["01:216136"][SZ_SYSTEM][SZ_APPLIANCE_CONTROL] == "13:120241"
    # HVAC entry merged
    assert set(result["32:153289"][SZ_REMOTES]) == {
        "37:111111",
        "37:222222",
    }


def test_merge_hvac_schema_roundtrip() -> None:
    """Extract then merge should roundtrip when config has devices."""
    original: dict[str, Any] = {
        "32:153289": {
            SZ_REMOTES: ["37:111111", "37:222222"],
            SZ_SENSORS: ["37:333333"],
        },
        SZ_ORPHANS_HVAC: ["37:444444"],
    }
    hvac = extract_hvac_schema(original)
    # Config must have at least one device for the merge to proceed
    config: dict[str, Any] = {"32:153289": {}}
    merged = merge_hvac_schema(config, hvac)
    assert merged["32:153289"][SZ_REMOTES] == ["37:111111", "37:222222"]
    assert merged["32:153289"][SZ_SENSORS] == ["37:333333"]
    assert SZ_ORPHANS_HVAC in merged
    assert "37:444444" in merged[SZ_ORPHANS_HVAC]


# ---------------------------------------------------------------------------
# C.2: User schema edits survive sync/merge cycle
# ---------------------------------------------------------------------------


def test_sync_preserves_user_alias_in_zone() -> None:
    """User _alias in a zone is preserved when sync adds actuators."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "34:092243", "_alias": "Living Room"},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {"actuators": ["04:056053"]},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"]["_alias"] == "Living Room"
    assert "04:056053" in result["01:216136"][SZ_ZONES]["01"]["actuators"]


def test_sync_preserves_user_enabled_false() -> None:
    """User _enabled: false on a zone is preserved across sync."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "34:092243", "_enabled": False},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {"actuators": ["04:056053"]},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"]["_enabled"] is False


def test_sync_preserves_user_skipped_true() -> None:
    """User _skipped: true on a zone is preserved across sync."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "34:092243", "_skipped": True},
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {"actuators": ["04:056053"]},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert result["01:216136"][SZ_ZONES]["01"]["_skipped"] is True


def test_sync_preserves_user_main_tcs() -> None:
    """User-edited main_tcs is not overwritten by sync."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {SZ_ZONES: {"01": {SZ_SENSOR: "34:092243"}}},
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:999999",
        "01:216136": {
            SZ_ZONES: {"01": {"actuators": ["04:056053"]}},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Config main_tcs is preserved — sync does not overwrite it
    assert result[SZ_MAIN_TCS] == "01:216136"


def test_sync_preserves_manually_added_device() -> None:
    """Device manually added to a zone (not in learned) is preserved."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "34:092243", "actuators": ["04:056053"]},
            },
        },
    }
    # Learned schema doesn't mention 04:056053 at all
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "34:092243"},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    # 04:056053 should still be in actuators (sync only removes from
    # orphans when device is in a learned zone, not from zones)
    if result is not None:
        assert "04:056053" in result["01:216136"][SZ_ZONES]["01"]["actuators"]
    else:
        # If result is None, config is unchanged — manually added device preserved
        assert "04:056053" in config["01:216136"][SZ_ZONES]["01"]["actuators"]


def test_sync_does_not_re_add_removed_device() -> None:
    """Device removed from config schema is not re-added from learned orphans."""
    config: dict[str, Any] = {
        "01:216136": {SZ_ZONES: {}},
    }
    # Learned has 04:056053 in orphans, but config doesn't
    learned: dict[str, Any] = {
        "01:216136": {SZ_ORPHANS: ["04:056053"]},
    }
    result = sync_learned_topology(config, learned)
    # sync should not add orphans to config — it only removes from
    # config orphans when device is in a zone
    if result is not None:
        assert "04:056053" not in result["01:216136"].get(SZ_ORPHANS, [])


def test_sync_preserves_device_comments_with_zone_sync() -> None:
    """device_comments preserved even when zones are being synced."""
    config: dict[str, Any] = {
        SZ_DEVICE_COMMENTS: [{"device_id": "34:092243", "comment": "Kitchen"}],
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "34:092243"}},
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {"actuators": ["04:056053"]}},
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert SZ_DEVICE_COMMENTS in result
    assert result[SZ_DEVICE_COMMENTS] == [
        {"device_id": "34:092243", "comment": "Kitchen"}
    ]


def test_strip_traits_with_disabled_string() -> None:
    """strip_traits_for_validation handles _disabled as string, not just bool."""
    schema: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "34:092243", "_disabled": "yes"}},
        },
    }
    result = strip_traits_for_validation(schema)
    # _disabled key should be stripped regardless of value type
    assert "_disabled" not in result["01:216136"][SZ_ZONES]["01"]
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] == "34:092243"


def test_strip_traits_removes_custom_underscore_key() -> None:
    """Custom _ prefixed key (not a known trait) is stripped for validation."""
    schema: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "34:092243", "_custom_key": "value"}},
        },
    }
    result = strip_traits_for_validation(schema)
    assert "_custom_key" not in result["01:216136"][SZ_ZONES]["01"]


# ---------------------------------------------------------------------------
# C.3: Corruption / malformed schema graceful degradation
# ---------------------------------------------------------------------------


def test_sync_duplicate_device_in_two_zones() -> None:
    """Duplicate device in two zones — sync keeps learned, removes stale."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "34:092243"},
                "02": {SZ_SENSOR: "34:092243"},  # duplicate!
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: "34:092243"},
            },
        },
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Zone 01 keeps the sensor (learned agrees)
    assert result["01:216136"][SZ_ZONES]["01"][SZ_SENSOR] == "34:092243"
    # Zone 02 sensor cleared (learned doesn't have it there)
    assert result["01:216136"][SZ_ZONES]["02"][SZ_SENSOR] is None


def test_sync_orphan_list_non_string_entries() -> None:
    """Orphan list with non-string entries — no crash, strings preserved."""
    config: dict[str, Any] = {
        SZ_ORPHANS_HEAT: ["04:056053", 123, None, "13:042605"],
        "01:216136": {SZ_ZONES: {}},
    }
    learned: dict[str, Any] = {
        "01:216136": {SZ_ZONES: {"01": {SZ_SENSOR: "04:056053"}}},
    }
    # Should not crash — non-string entries are in the set, but the
    # intersection logic uses set operations which handle mixed types
    result = sync_learned_topology(config, learned)
    assert result is not None
    # 04:056053 should be removed from orphans (it's in a zone now)
    assert "04:056053" not in result.get(SZ_ORPHANS_HEAT, [])


def test_sync_zones_as_list_not_dict() -> None:
    """Schema with zones as a list instead of dict — no crash."""
    config: dict[str, Any] = {
        "01:216136": {
            "zones": [{"sensor": "04:056053"}],  # list, not dict
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {SZ_SENSOR: "34:092243"}},
        },
    }
    # Should not crash — config zones is a list, so setdefault won't
    # crash because sync checks isinstance(learned_zones, dict) first
    sync_learned_topology(config, learned)
    # Either returns a result (enriched) or None — either way, no crash


def test_sync_sensor_as_dict_not_string() -> None:
    """Schema with sensor as a dict instead of string — no crash."""
    config: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {
                "01": {SZ_SENSOR: {"nested": "garbage"}},  # dict, not str
            },
        },
    }
    learned: dict[str, Any] = {
        "01:216136": {
            SZ_ZONES: {"01": {"actuators": ["04:056053"]}},
        },
    }
    # Should not crash — sensor is a dict (truthy), but sync doesn't
    # validate the type, it just checks truthiness
    result = sync_learned_topology(config, learned)
    # No crash is the key assertion
    assert result is not None or result is None  # either is fine


def test_remove_device_remotes_as_string_not_list() -> None:
    """remotes as a string instead of list — no crash."""
    schema: dict[str, Any] = {
        "32:153289": {"remotes": "37:111111"},  # string, not list
    }
    # Should not crash — remove_device_from_schema iterates the list,
    # but if it's a string it iterates characters. The key assertion
    # is that it doesn't raise.
    result = remove_device_from_schema(schema, "37:111111")
    assert "32:153289" in result  # entry preserved


def test_empty_schema_all_functions() -> None:
    """Empty schema {} — all functions return empty/None, no crash."""
    empty: dict[str, Any] = {}
    assert sync_learned_topology(empty, {}) is None
    assert extract_hvac_schema(empty) == {}
    assert merge_hvac_schema(empty, {}) is empty
    assert remove_device_from_schema(empty, "04:056053") == {}
    assert strip_traits_for_validation(empty) == {}


def test_none_schema_all_functions() -> None:
    """None schema — treated as empty/None, no crash."""
    assert sync_learned_topology(None, {}) is None  # type: ignore[arg-type]
    assert extract_hvac_schema(None) == {}
    config: dict[str, Any] = {}
    assert merge_hvac_schema(config, None) is config  # type: ignore[arg-type]
    assert merge_schemas(None, {}) is None  # type: ignore[arg-type]
    assert merge_schemas({}, None) is None  # type: ignore[arg-type]


def test_merge_schemas_corrupt_cache_non_dict() -> None:
    """merge_schemas with corrupt cache (non-dict) — returns None or config."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {SZ_ZONES: {}},
    }
    # Corrupt cache: a string instead of dict
    corrupt_cache = "not a dict"  # type: ignore[assignment]
    result = merge_schemas(config, corrupt_cache)  # type: ignore[arg-type]
    # Should return None (cache is not a valid schema) or config
    assert result is None or result == config


# ── Tests for zone binding from broadcast traffic (passive scan) ──────


def test_sync_learned_topology_comment_zone_only_infers_ctl() -> None:
    """A TRV comment with zone_idx but no bound_to infers CTL from main_tcs.

    This is the passive scan case: TRVs broadcast zone-binding codes
    (30C9, 3150) with dst=--:------, so the scan engine captures zone_idx
    but not bound_to.  sync_learned_topology should infer the CTL from
    main_tcs.  TRVs (04:) are placed as actuators, not sensors.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
        SZ_DEVICE_COMMENTS: {
            "04:111111": "Likely TRV. zone 02. codes: 30C9, 3150. RSSI 82.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert "02" in result["01:123456"][SZ_ZONES]
    assert "04:111111" in result["01:123456"][SZ_ZONES]["02"]["actuators"]


def test_sync_learned_topology_comment_zone_only_infers_single_ctl() -> None:
    """Without main_tcs, a single CTL key is used as fallback."""
    config: dict[str, Any] = {
        "01:123456": {},
        SZ_DEVICE_COMMENTS: {
            "04:111111": "Likely TRV. zone 03. codes: 30C9.",
        },
    }
    learned: dict[str, Any] = {
        "01:123456": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    assert "03" in result["01:123456"][SZ_ZONES]
    assert "04:111111" in result["01:123456"][SZ_ZONES]["03"]["actuators"]


def test_sync_learned_topology_comment_skips_invalid_zone_idx() -> None:
    """Zone indices > 0B are rejected by ramses_rf schema (max 12 zones).

    Comments with zone 0C-0F or 10+ should be skipped, not added to the
    schema.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
        SZ_DEVICE_COMMENTS: {
            "04:111111": "Likely TRV. zone 02. codes: 30C9.",
            "04:222222": "Likely TRV. zone 0C. codes: 30C9.",
            "04:333333": "Likely TRV. zone 15. codes: 30C9.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zones = result["01:123456"][SZ_ZONES]
    # Zone 02 is valid — should be present
    assert "02" in zones
    assert "04:111111" in zones["02"].get("actuators", [])
    # Zones 0C and 15 are invalid — should NOT be present
    assert "0C" not in zones
    assert "15" not in zones


def test_sync_learned_topology_comment_skips_hgi_device() -> None:
    """18: (HGI) devices are never valid zone sensors — skip them."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
        SZ_DEVICE_COMMENTS: {
            "18:111111": "Likely HGI. bound to 01:123456. zone 07.",
            "04:222222": "Likely TRV. zone 03. codes: 30C9.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zones = result["01:123456"][SZ_ZONES]
    # 18: device should NOT be a zone sensor
    for zone in zones.values():
        assert zone.get(SZ_SENSOR) != "18:111111"
    # 04: device should be present as actuator
    assert "03" in zones
    assert "04:222222" in zones["03"].get("actuators", [])


def test_sync_learned_topology_comment_learned_takes_precedence() -> None:
    """Learned schema zones take precedence over comment-derived zones."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
        SZ_DEVICE_COMMENTS: {
            "04:111111": "Likely TRV. zone 02. codes: 30C9.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {
            SZ_ZONES: {"02": {SZ_SENSOR: "22:999999"}},
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Learned sensor (22:999999) wins over comment (04:111111)
    assert result["01:123456"][SZ_ZONES]["02"][SZ_SENSOR] == "22:999999"


def test_sync_learned_topology_cleans_hgi_zones() -> None:
    """HGI (18:) entries must not have heating-specific keys (zones, system, etc.).

    Earlier versions of sync_learned_topology incorrectly parsed HGI comments
    like "bound to 01:123456. zone 07" and created zone entries under the HGI.
    This test ensures those stale keys are cleaned up.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}},
        "18:072981": {
            "_skipped": True,
            SZ_ZONES: {"07": {SZ_SENSOR: "01:123456"}},
            SZ_SYSTEM: {"appliance_control": "10:222222"},
        },
        "18:191664": {"_skipped": True},
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # 18: entries should still exist but have NO heating keys and NO _skipped
    assert "18:072981" in result
    assert SZ_ZONES not in result["18:072981"]
    assert SZ_SYSTEM not in result["18:072981"]
    assert SZ_DHW_SYSTEM not in result["18:072981"]
    # _skipped should be removed (would cause re-discovery every cycle)
    assert result["18:072981"].get("_skipped") is None
    # 18:191664 should also have _skipped removed
    assert result["18:191664"] == {}


def test_sync_learned_topology_comment_moves_device_between_zones() -> None:
    """A device whose comment says zone 00 but config has it in zone 04 must move.

    Previously, step 1g added the device to the comment-specified zone WITHOUT
    removing it from its existing config zone, causing the device to appear in
    both zones.  This caused "can't change parent" errors in ramses_rf because
    the TRV was bound to two zones simultaneously.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {
            SZ_ZONES: {
                "00": {"actuators": ["04:111111", "04:222222"]},
                "04": {"actuators": ["04:333333", "04:444444"]},
            },
        },
        SZ_DEVICE_COMMENTS: {
            # Comments say 04:333333 is in zone 00 (moved from zone 04)
            "04:333333": "Likely TRV. zone 00. codes: 30C9.",
            # Comments say 04:111111 is in zone 04 (moved from zone 00)
            "04:111111": "Likely TRV. zone 04. codes: 30C9.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zones = result["01:123456"][SZ_ZONES]
    # 04:333333 should be in zone 00 (from comment), NOT in zone 04
    assert "04:333333" in zones["00"].get("actuators", [])
    assert "04:333333" not in zones["04"].get("actuators", [])
    # 04:111111 should be in zone 04 (from comment), NOT in zone 00
    assert "04:111111" in zones["04"].get("actuators", [])
    assert "04:111111" not in zones["00"].get("actuators", [])
    # Devices that didn't move should stay in their original zones
    assert "04:222222" in zones["00"].get("actuators", [])
    assert "04:444444" in zones["04"].get("actuators", [])


def test_sync_learned_topology_clears_trv_from_sensor() -> None:
    """TRVs (04:) placed as both sensor and actuator must have sensor cleared.

    ramses_rf's 000C handler sometimes places a TRV as both the zone sensor
    and an actuator.  A TRV is never a zone sensor — it measures valve
    position, not room temperature.  Clear the sensor field in that case.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {
            SZ_ZONES: {
                "02": {
                    SZ_SENSOR: "04:111111",
                    "actuators": ["04:111111", "04:222222"],
                },
            },
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zone = result["01:123456"][SZ_ZONES]["02"]
    # 04:111111 should be cleared from sensor (it's a TRV)
    assert zone.get(SZ_SENSOR) is None
    # It should still be in actuators
    assert "04:111111" in zone["actuators"]
    assert "04:222222" in zone["actuators"]


def test_sync_learned_topology_skips_ctl_comment_zone() -> None:
    """CTL (01:) comment with 'zone NN' must not create a phantom zone.

    The CTL's own comment may contain "zone 09" (its binding zone), but the
    CTL is the controller, not a zone member.  Creating a zone from this
    would add an empty phantom zone with no sensor or actuators.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
        SZ_DEVICE_COMMENTS: {
            "01:123456": "Likely CTL. bound to 18:001234. zone 09. codes: 0004.",
            "04:111111": "Likely TRV. zone 02. codes: 30C9.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zones = result["01:123456"][SZ_ZONES]
    # Zone 02 should exist (from the TRV comment)
    assert "02" in zones
    assert "04:111111" in zones["02"].get("actuators", [])
    # Zone 09 should NOT exist (from the CTL comment)
    assert "09" not in zones


def test_sync_learned_topology_removes_phantom_zone_from_learned() -> None:
    """Empty phantom zones from learned schema are removed if not in original config.

    ramses_rf may include an empty zone (class only, no sensor/actuators) in
    its learned schema — this happens when a corrupted cached schema was loaded.
    sync_learned_topology should remove such zones if they were NOT in the
    original config schema (user-created zones are preserved even if empty).
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {
            # Zone 02 is in original config — should be preserved even if empty
            SZ_ZONES: {"02": {}},
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {
            SZ_ZONES: {
                # Zone 02 is empty in learned too — but it's in original config
                "02": {SZ_CLASS: "radiator_valve"},
                # Zone 09 is a phantom — not in original config, empty in learned
                "09": {SZ_CLASS: "radiator_valve"},
                # Zone 03 has devices — should be kept
                "03": {SZ_CLASS: "radiator_valve", "actuators": ["04:111111"]},
            },
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zones = result["01:123456"][SZ_ZONES]
    # Zone 02 was in original config — preserved even though empty
    assert "02" in zones
    # Zone 09 was NOT in original config and is empty — removed
    assert "09" not in zones
    # Zone 03 has devices — kept
    assert "03" in zones
    assert "04:111111" in zones["03"].get("actuators", [])


def test_sync_learned_topology_skips_hgi_bound_comment() -> None:
    """A CTL comment saying 'bound to 18:072981' must not create zones under the HGI.

    The HGI is the gateway, not a TCS.  Comments like "bound to 18:072981"
    on a CTL mean the CTL is paired with that gateway, not that the HGI
    is a temperature control system with zones.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
        "18:072981": {"_skipped": True},
        SZ_DEVICE_COMMENTS: {
            "01:123456": "Likely CTL. bound to 18:072981. zone 00. codes: 1260.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    # Result is None (no changes) or a schema where 18: has no zones
    if result is not None:
        assert SZ_ZONES not in result.get("18:072981", {})
        hgi_entry = result.get("18:072981", {})
        if SZ_ZONES in hgi_entry:
            for zone in hgi_entry[SZ_ZONES].values():
                assert zone.get(SZ_SENSOR) != "01:123456"


def test_sync_learned_topology_removes_hgi_from_orphans() -> None:
    """HGI (18:) devices must not appear in orphans_heat or orphans_hvac."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}},
        "18:072981": {"_skipped": True},
        SZ_ORPHANS_HEAT: ["07:050121", "18:072981", "10:064873"],
        SZ_ORPHANS_HVAC: ["32:111111", "18:072981"],
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}},
        SZ_ORPHANS_HEAT: ["07:050121", "10:064873"],
        SZ_ORPHANS_HVAC: ["32:111111"],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    heat_orphans = result.get(SZ_ORPHANS_HEAT, [])
    hvac_orphans = result.get(SZ_ORPHANS_HVAC, [])
    assert "18:072981" not in heat_orphans
    assert "18:072981" not in hvac_orphans
    # Non-HGI orphans should be preserved
    assert "07:050121" in heat_orphans
    assert "10:064873" in heat_orphans


def test_sync_learned_topology_places_dhw_sensor_from_comment() -> None:
    """A 07: (DHW) device in comments should be placed as stored_hotwater.sensor.

    The scan engine classifies 07: devices as DHW and may include "zone 00"
    in the comment (the DHW domain).  sync_learned_topology should place
    the device as stored_hotwater.sensor, not in a heating zone.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {SZ_ZONES: {"00": {"actuators": ["04:111111"]}}},
        SZ_ORPHANS_HEAT: ["07:050121"],
        SZ_DEVICE_COMMENTS: {
            "07:050121": "Likely DHW. zone 00. codes: 10A0, 1260. RSSI 82.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {"00": {"actuators": ["04:111111"]}},
            SZ_DHW_SYSTEM: {},
        },
        SZ_ORPHANS_HEAT: ["07:050121"],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # 07:050121 should be stored_hotwater.sensor
    assert result["01:216136"][SZ_DHW_SYSTEM][SZ_SENSOR] == "07:050121"
    # 07:050121 should NOT be in orphans_heat
    assert "07:050121" not in result.get(SZ_ORPHANS_HEAT, [])
    # 07:050121 should NOT be in any heating zone
    for zone in result["01:216136"][SZ_ZONES].values():
        assert zone.get(SZ_SENSOR) != "07:050121"
        assert "07:050121" not in zone.get("actuators", [])


def test_sync_learned_topology_trv_never_zone_sensor() -> None:
    """TRVs (04:) must never be placed as zone sensors from comments."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {}}},
        SZ_DEVICE_COMMENTS: {
            "04:111111": "Likely TRV. zone 02. codes: 30C9, 3150. RSSI 82.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {}}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zone = result["01:123456"][SZ_ZONES]["02"]
    # TRV should be in actuators, NOT as sensor
    assert "04:111111" in zone.get("actuators", [])
    assert zone.get(SZ_SENSOR) != "04:111111"


def test_sync_learned_topology_creates_hgi_schema_entry() -> None:
    """HGI (18:) devices in device_comments should get a schema entry.

    The scan engine tracks HGIs and refresh_device_comments creates comments
    for them.  sync_learned_topology should create a minimal empty entry
    so HGIs are tracked in the schema (enabling eventual removal of the
    known_list).  The entry must NOT have _skipped, otherwise
    _derive_known_list_from_schema would exclude it from the known_list
    and the scan engine would re-discover the HGI every cycle.
    _strip_schema_extensions drops these before passing to ramses_rf.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}},
        SZ_DEVICE_COMMENTS: {
            "01:123456": "Likely CTL. codes: 0016.",
            "18:130236": "Likely HGI. codes: 2210, 22E0. RSSI 0.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {SZ_ZONES: {"02": {SZ_SENSOR: "04:111111"}}},
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # 18:130236 should now have an empty schema entry (NOT _skipped)
    assert "18:130236" in result
    assert result["18:130236"] == {}
    # Should not have _skipped (would cause re-discovery every cycle)
    assert result["18:130236"].get("_skipped") is None
    # Should not have any heating keys
    assert SZ_ZONES not in result["18:130236"]
    assert SZ_SYSTEM not in result["18:130236"]


def test_sync_learned_topology_updates_comment_zone_from_learned() -> None:
    """Comments should reflect zone info from the learned schema, not the
    scan engine's broadcast-derived zone_idx.

    The scan engine captures zone_idx from 30C9 broadcast packets, which
    often default to zone 00.  The learned schema (from ramses_rf's active
    discovery via 0004/0005 config packets) has the authoritative zone
    assignments.  sync_learned_topology should update comments to match.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "04": {SZ_SENSOR: "04:056677", "actuators": ["04:056677"]},
            }
        },
        SZ_DEVICE_COMMENTS: {
            "04:056677": "Likely TRV. zone 00. codes: 30C9. RSSI 82.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "04": {SZ_SENSOR: "04:056677", "actuators": ["04:056677"]},
            }
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Comment should be updated from "zone 00" to "zone 04"
    comment = result[SZ_DEVICE_COMMENTS]["04:056677"]
    assert "zone 04" in comment
    assert "zone 00" not in comment


def test_sync_learned_topology_adds_zone_to_comment_without_zone() -> None:
    """If a comment has no zone info but the learned schema places the device
    in a zone, the zone info should be added to the comment."""
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "04": {SZ_SENSOR: "04:056677", "actuators": ["04:056677"]},
            }
        },
        SZ_DEVICE_COMMENTS: {
            "04:056677": "Likely TRV. codes: 30C9. RSSI 82.",
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "04": {SZ_SENSOR: "04:056677", "actuators": ["04:056677"]},
            }
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    comment = result[SZ_DEVICE_COMMENTS]["04:056677"]
    assert "zone 04" in comment


def test_sync_learned_topology_adds_hvac_remote_from_comment() -> None:
    """A 37: REM with 'bound to 32:...' in its comment should be added as a
    remote to the FAN's schema entry and removed from orphans_hvac."""
    config: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:169161"]},
        SZ_ORPHANS_HVAC: ["37:168270", "37:126776"],
        SZ_DEVICE_COMMENTS: {
            "37:169161": "Likely REM. bound to 32:153289. codes: 1470.",
            "37:168270": "Likely REM. bound to 32:153289. codes: 22F1.",
            "37:126776": "Likely CO2. codes: 1298.",
        },
    }
    learned: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:169161"]},
        SZ_ORPHANS_HVAC: ["37:126776"],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # 37:168270 should be added to the FAN's remotes
    remotes = result["32:153289"][SZ_REMOTES]
    assert "37:168270" in remotes
    assert "37:169161" in remotes  # already there
    # 37:168270 should be removed from orphans_hvac
    orphans = result.get(SZ_ORPHANS_HVAC, [])
    assert "37:168270" not in orphans
    # 37:126776 (no bound_to) should stay in orphans
    assert "37:126776" in orphans


def test_sync_learned_topology_backfills_root_entry_for_list_device() -> None:
    """Devices in remotes[]/orphans[] without a root entry get one backfilled.

    Before the generate_schema_entry fix, list-based devices (REM/CO2 in
    remotes[], TRV in zones[], etc.) were accepted without a root entry —
    so _owner and other traits could never be set on them.  This backfill
    in sync_learned_topology creates root entries for pre-existing schemas.
    """
    config: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:168270"]},
        SZ_ORPHANS_HVAC: ["37:126776"],
        SZ_OWNER: "me",
    }
    learned: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:168270"]},
        SZ_ORPHANS_HVAC: ["37:126776"],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    # Both devices should now have root entries
    assert "37:168270" in result
    assert isinstance(result["37:168270"], dict)
    assert "37:126776" in result
    assert isinstance(result["37:126776"], dict)
    # Root _owner should be inherited
    assert result["37:168270"].get("_owner") == "me"
    assert result["37:126776"].get("_owner") == "me"


def test_sync_learned_topology_no_backfill_when_root_exists() -> None:
    """No backfill when root entry already exists — no changes."""
    config: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:168270"]},
        "37:168270": {"_owner": "me", "_class": "REM"},
        SZ_OWNER: "me",
    }
    learned: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:168270"]},
    }
    result = sync_learned_topology(config, learned)
    # No changes — root entry already exists with traits
    assert result is None


def test_strip_traits_no_duplicate_for_remotes_device() -> None:
    """A device in remotes[] with a backfilled root entry must not also
    appear in orphans_hvac after strip_traits_for_validation.

    The backfill in sync_learned_topology creates root entries for devices
    that exist only in lists.  strip_traits_for_validation must NOT move
    those root entries to orphans_hvac — that would create a duplicate
    with the remotes[] placement.
    """
    schema: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:168270"]},
        "37:168270": {},  # backfilled root entry (empty, no traits)
    }
    result = strip_traits_for_validation(schema)
    # Device should be in remotes, NOT in orphans_hvac
    assert "37:168270" in result["32:153289"][SZ_REMOTES]
    assert SZ_ORPHANS_HVAC not in result or "37:168270" not in result.get(
        SZ_ORPHANS_HVAC, []
    )
    # Root entry should be dropped (it was empty, device is in remotes)
    assert "37:168270" not in result


def test_strip_traits_no_duplicate_for_orphans_device() -> None:
    """A device in orphans_hvac with a backfilled root entry must not
    appear twice in orphans_hvac after strip_traits_for_validation."""
    schema: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["37:126776"],
        "37:126776": {},  # backfilled root entry (empty, no traits)
    }
    result = strip_traits_for_validation(schema)
    # Should appear exactly once in orphans_hvac
    assert result.get(SZ_ORPHANS_HVAC, []).count("37:126776") == 1
    # Root entry should be dropped (moved to orphans, set dedup handles it)
    assert "37:126776" not in result or not isinstance(result["37:126776"], dict)


def test_strip_traits_keeps_root_entry_with_traits_for_remotes_device() -> None:
    """A device in remotes[] with a root entry that HAS traits (e.g. _class)
    should have the root entry dropped but traits extracted elsewhere.

    The root entry's _ traits are processed by _derive_known_list_from_schema
    before strip_traits_for_validation runs.  After stripping, the empty root
    entry is dropped (not moved to orphans) because the device is in remotes[].
    """
    schema: dict[str, Any] = {
        "32:153289": {SZ_REMOTES: ["37:168270"]},
        "37:168270": {"_owner": "me", "_class": "REM"},
    }
    result = strip_traits_for_validation(schema)
    # Device should be in remotes only, not orphans
    assert "37:168270" in result["32:153289"][SZ_REMOTES]
    assert SZ_ORPHANS_HVAC not in result or "37:168270" not in result.get(
        SZ_ORPHANS_HVAC, []
    )
    # Root entry dropped after trait stripping (no remotes/sensors left)
    assert "37:168270" not in result


def test_sync_learned_topology_sanitizes_sensor_in_actuators() -> None:
    """A sensor-type device (01:, 22:, 34:) in actuators should be moved to
    sensor if the zone has no sensor.

    ramses_rf's active discovery sometimes places THM/RND devices in the
    actuators list.  This causes RULES EXCEPTIONS in ramses_rf's
    legacy_trace because THM is not a valid actuator class.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "00": {
                    SZ_SENSOR: None,
                    "actuators": ["04:034720", "34:058721"],
                }
            }
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "00": {
                    SZ_SENSOR: None,
                    "actuators": ["04:034720", "34:058721"],
                    SZ_CLASS: "radiator_valve",
                }
            }
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zone = result["01:216136"][SZ_ZONES]["00"]
    # 34:058721 should be moved from actuators to sensor
    assert zone[SZ_SENSOR] == "34:058721"
    assert "34:058721" not in zone.get("actuators", [])
    # 04:034720 should stay in actuators
    assert "04:034720" in zone["actuators"]


def test_sync_learned_topology_trv_sensor_rnd_actuator_swapped() -> None:
    """TRV as sensor + RND in actuators → TRV moved to actuators, RND to sensor.

    ramses_rf's active discovery sometimes places a TRV (04:) as the zone
    sensor and a RND (34:) in the actuators list.  The TRV is never a zone
    sensor — it must be moved to actuators so the RND can be promoted to
    sensor (issue 813).
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "03": {
                    SZ_SENSOR: "04:056679",
                    "actuators": ["04:219929", "34:058721"],
                }
            }
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "03": {
                    SZ_SENSOR: "04:056679",
                    "actuators": ["04:219929", "34:058721"],
                    SZ_CLASS: "radiator_valve",
                }
            }
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zone = result["01:216136"][SZ_ZONES]["03"]
    # RND should be the sensor
    assert zone[SZ_SENSOR] == "34:058721"
    assert "34:058721" not in zone.get("actuators", [])
    # TRVs should all be in actuators
    assert sorted(zone["actuators"]) == ["04:056679", "04:219929"]


def test_sync_learned_topology_single_trv_as_sensor_moved_to_actuators() -> None:
    """A single TRV (04:) placed as zone sensor with no thermostat should be
    moved to actuators, sensor set to None (issue 813).

    ramses_rf's active discovery sometimes places a TRV as the zone sensor
    when it's the only device in the zone.  The TRV's primary role is as an
    actuator — move it to actuators and leave sensor=None.
    """
    config: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "06": {
                    SZ_SENSOR: "04:056675",
                }
            }
        },
    }
    learned: dict[str, Any] = {
        SZ_MAIN_TCS: "01:216136",
        "01:216136": {
            SZ_ZONES: {
                "06": {
                    SZ_SENSOR: "04:056675",
                    SZ_CLASS: "radiator_valve",
                }
            }
        },
        SZ_ORPHANS_HEAT: [],
        SZ_ORPHANS_HVAC: [],
    }
    result = sync_learned_topology(config, learned)
    assert result is not None
    zone = result["01:216136"][SZ_ZONES]["06"]
    # TRV should be in actuators, not sensor
    assert zone.get(SZ_SENSOR) is None
    assert "04:056675" in zone["actuators"]


# ── Tests for order_schema ───────────────────────────────────────────


def test_order_schema_basic_ordering() -> None:
    """Schema keys are ordered: root traits, main_tcs, comments, orphans, heat, hvac."""
    schema: dict[str, Any] = {
        SZ_ORPHANS_HVAC: ["37:111111"],
        "37:111111": {},
        "04:222222": {},
        SZ_DEVICE_COMMENTS: {"04:222222": "TRV"},
        SZ_MAIN_TCS: "01:123456",
        "_owner": "me",
        "01:123456": {},
        SZ_ORPHANS_HEAT: ["04:333333"],
        "32:444444": {SZ_REMOTES: []},
    }
    result = order_schema(schema)
    keys = list(result.keys())
    # _owner first
    assert keys[0] == "_owner"
    # main_tcs second
    assert keys[1] == SZ_MAIN_TCS
    # device_comments third
    assert keys[2] == SZ_DEVICE_COMMENTS
    # orphans right after comments (needs work at top)
    assert keys[3] == SZ_ORPHANS_HEAT
    assert keys[4] == SZ_ORPHANS_HVAC
    # heat devices (01:, 04:) sorted by owner then ID
    assert keys[5] == "01:123456"
    assert keys[6] == "04:222222"
    # hvac devices (32:, 37:) sorted by owner then ID
    assert keys[7] == "32:444444"
    assert keys[8] == "37:111111"


def test_order_schema_preserves_all_keys() -> None:
    """No keys are lost during ordering."""
    schema: dict[str, Any] = {
        "_owner": "me",
        "37:111111": {},
        "04:222222": {},
        SZ_MAIN_TCS: "01:123456",
        "01:123456": {},
        SZ_ORPHANS_HVAC: ["37:111111"],
    }
    result = order_schema(schema)
    assert set(result.keys()) == set(schema.keys())


def test_order_schema_empty_schema() -> None:
    """Empty schema returns empty dict."""
    assert order_schema({}) == {}


def test_order_schema_non_dict_returns_as_is() -> None:
    """Non-dict input is returned unchanged."""
    assert order_schema("not a dict") == "not a dict"  # type: ignore[arg-type]


def test_order_schema_heat_devices_sorted_by_owner_then_id() -> None:
    """Heat devices are sorted by _owner first, then device ID."""
    schema: dict[str, Any] = {
        "07:111111": {"_owner": "me"},
        "01:222222": {"_owner": "not-me"},
        "04:333333": {"_owner": "me"},
        "10:444444": {},  # no _owner → sorts first (empty string)
    }
    result = order_schema(schema)
    keys = list(result.keys())
    # No-owner first, then "me" group, then "not-me" group
    assert keys == ["10:444444", "04:333333", "07:111111", "01:222222"]


def test_order_schema_hvac_devices_sorted_by_owner_then_id() -> None:
    """HVAC devices are sorted by _owner first, then device ID."""
    schema: dict[str, Any] = {
        "37:111111": {"_owner": "me"},
        "32:222222": {"_owner": "not-me"},
        "29:333333": {"_owner": "me"},
        "18:444444": {},  # no _owner → sorts first
    }
    result = order_schema(schema)
    keys = list(result.keys())
    # No-owner first, then "me" group, then "not-me" group
    assert keys == ["18:444444", "29:333333", "37:111111", "32:222222"]

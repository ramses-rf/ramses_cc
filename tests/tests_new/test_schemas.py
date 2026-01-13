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
)
from custom_components.ramses_cc.schemas import (
    merge_schemas,
    normalise_config,
    schema_is_minimal,
)
from ramses_rf.schemas import (
    SZ_APPLIANCE_CONTROL,
    SZ_BLOCK_LIST,
    SZ_CLASS,
    SZ_KNOWN_LIST,
    SZ_SENSOR,
    SZ_SYSTEM,
    SZ_ZONES,
)
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

    port, client_config, broker_config = normalise_config(config)

    assert port == "/dev/ttyUSB0"
    assert client_config["config"] == {"disable_discovery": True}
    assert broker_config["remotes"]["18:111111"] == {"boost": "packet_data"}
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


def test_schema_is_minimal_logic() -> None:
    """Test minimal schema validation branches (Lines 203-214)."""
    # Case 1: Valid minimal schema (Line 203 & 214)
    # Note: To pass line 211, the top-level key must match the zone sensor ID
    minimal_schema: dict[str, Any] = {
        "01:123456": {
            SZ_ZONES: {"01": {SZ_SENSOR: "01:123456"}},
        }
    }
    assert schema_is_minimal(minimal_schema) is True

    # Case 2: Excluded keys branch (Line 204)
    excluded_keys: dict[str, Any] = {
        SZ_BLOCK_LIST: ["01:111111"],
        SZ_KNOWN_LIST: {"18:111111": {SZ_CLASS: "HGI"}},
        "01:123456": {SZ_ZONES: {"01": {SZ_SENSOR: "01:123456"}}},
    }
    assert schema_is_minimal(excluded_keys) is True

    # Case 3: Invalid content for SCH_MINIMUM_TCS (Line 208)
    not_minimal: dict[str, Any] = {
        "10:123456": {
            SZ_SYSTEM: {SZ_APPLIANCE_CONTROL: "10:123456"},
            "extra_key": "not_allowed",
        }
    }
    assert schema_is_minimal(not_minimal) is False

    # Case 4: Mismatched zone sensor (Line 211)
    mismatched: dict[str, Any] = {
        "01:111111": {
            SZ_ZONES: {"01": {SZ_SENSOR: "01:222222"}},
        }
    }
    assert schema_is_minimal(mismatched) is False

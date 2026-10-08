"""Unit tests for custom_components.ramses_cc.helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime as dt
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.helpers import (
    add_to_include_lists,
    as_iso,
    clear_async_attr_cache,
    device_filter_include,
    device_gateway,
    device_parent_fan,
    device_slug,
    engine_include_list,
    engine_transport,
    extract_demand,
    fields_to_aware,
    gateway_engine,
    ha_device_id_to_ramses_device_id,
    parse_packet_string,
    ramses_device_id_to_ha_device_id,
    remove_from_include_lists,
    resolve_async_attr,
    resolve_demand_attr,
)

RAMSES_ID = "01:145038"


def test_ha_to_ramses_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from HA registry ID to RAMSES hardware ID."""
    # 1. Handle empty input
    assert ha_device_id_to_ramses_device_id(hass, "") is None

    # 2. Handle non-existent device
    assert ha_device_id_to_ramses_device_id(hass, "non_existent_id") is None

    # 3. Create a valid ConfigEntry
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config_1")
    config_entry.add_to_hass(hass)

    # 4. Create device in registry
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RAMSES_ID)},
    )

    # 5. Verify successful mapping
    result = ha_device_id_to_ramses_device_id(hass, device.id)
    assert result == RAMSES_ID


def test_ramses_to_ha_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from RAMSES hardware ID to HA registry ID."""
    # 1. Handle empty input
    assert ramses_device_id_to_ha_device_id(hass, "") is None

    # 2. Handle non-existent hardware
    assert ramses_device_id_to_ha_device_id(hass, "99:999999") is None

    # 3. Create a valid ConfigEntry
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config_2")
    config_entry.add_to_hass(hass)

    # 4. Handle valid mapping
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RAMSES_ID)},
    )

    # 5. Verify successful mapping - deprecated  # TODO: remove Q2 2027
    result = ramses_device_id_to_ha_device_id(hass, RAMSES_ID)
    assert result == device.id

    # 6. Verify successful mapping - current
    result = ramses_device_id_to_ha_device_id(
        hass, RAMSES_ID, entry_id="test_config_2"
    )
    assert result == device.id


def test_ha_to_ramses_id_wrong_domain(hass: HomeAssistant) -> None:
    """Test mapping when the device registry entry belongs to another domain."""
    config_entry = MockConfigEntry(domain="not_ramses", entry_id="other_entry")
    config_entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("not_ramses", "some_id")},
    )
    assert ha_device_id_to_ramses_device_id(hass, device.id) is None


def test_fields_to_aware_none() -> None:
    """Test fields_to_aware with None input."""
    assert fields_to_aware(None) is None


def test_fields_to_aware_parsing() -> None:
    """Test fields_to_aware with strings and invalid inputs."""
    # Test valid ISO string
    iso_str = "2024-01-20T12:00:00"
    result = fields_to_aware(iso_str)
    assert isinstance(result, dt)
    assert result.year == 2024

    # Test invalid string that fails parsing
    assert fields_to_aware("not-a-date") is None


def test_fields_to_aware_logic() -> None:
    """Test fields_to_aware logic for aware and naive datetimes."""
    # Test already aware datetime
    aware_dt = dt_util.now()
    assert fields_to_aware(aware_dt) == aware_dt

    # Test naive datetime conversion
    naive_dt = dt(2024, 1, 20, 12, 0, 0)
    result = fields_to_aware(naive_dt)
    assert result is not None
    assert result.tzinfo is not None
    # dt_util.as_local makes it aware based on HA's configured timezone
    assert result.year == 2024


def test_as_iso_conversion() -> None:
    """Test as_iso helper for both datetime and string inputs."""
    # Test datetime input
    current_dt = dt_util.now()
    # as_iso strips tzinfo, so we compare to a naive version of the expected string
    assert as_iso(current_dt) == current_dt.replace(tzinfo=None).isoformat()

    # Test string input (pass-through)
    iso_str = "2024-01-01T10:00:00"
    assert as_iso(iso_str) == iso_str

    # Test None - implementation converts None to "None"
    assert as_iso(None) == "None"


def test_extract_demand_variants() -> None:
    """Test extract_demand with various object structures."""
    assert extract_demand(None) is None
    assert extract_demand(0.75) == 0.75
    assert extract_demand(1) == 1.0

    # Object with heat_demand
    obj_heat = type("HeatObj", (), {"heat_demand": 0.65})()
    assert extract_demand(obj_heat) == 0.65

    # Object with demand
    obj_demand = type("DemandObj", (), {"demand": 0.42})()
    assert extract_demand(obj_demand) == 0.42

    # Object with invalid demand
    obj_invalid = type("InvalidObj", (), {"other": "value"})()
    assert extract_demand(obj_invalid) is None


def test_resolve_demand_attr_fallback() -> None:
    """Test resolve_demand_attr with primary and fallback attributes."""
    entity = MagicMock()
    dev = MagicMock()
    dev.thermal_demand = None
    dev.heat_demand = 0.88

    res = resolve_demand_attr(entity, dev, "thermal_demand", "heat_demand")
    assert res == 0.88


# ---------------------------------------------------------------------------
# Public ramses_rf accessors with private fallbacks (ramses_rf PR 1236)
#
# SimpleNamespace is used so attribute absence is real — a MagicMock would
# auto-create any attribute accessed, which can shadow the member under
# test (the recurring mock-vs-property trap).
# ---------------------------------------------------------------------------


def test_gateway_engine_accessor() -> None:
    """gateway_engine reads the public engine property."""
    public = SimpleNamespace(engine="pub_engine")
    assert gateway_engine(public) == "pub_engine"

    assert gateway_engine(SimpleNamespace(_engine="priv_engine")) is None
    assert gateway_engine(SimpleNamespace()) is None


def test_engine_transport_accessor() -> None:
    """engine_transport reads engine.transport or obj.transport."""
    # Gateway-style: public chain
    gwy = SimpleNamespace(engine=SimpleNamespace(transport="pub_t"))
    assert engine_transport(gwy) == "pub_t"

    # Engine passed directly
    eng = SimpleNamespace(transport="direct_t")
    assert engine_transport(eng) == "direct_t"

    # private members are no longer consulted
    gwy_priv = SimpleNamespace(_engine=SimpleNamespace(_transport="priv_t"))
    assert engine_transport(gwy_priv) is None
    assert engine_transport(SimpleNamespace(_transport="deep_t")) is None

    assert engine_transport(SimpleNamespace()) is None


def test_device_gateway_accessor() -> None:
    """device_gateway reads the public gateway property."""
    assert device_gateway(SimpleNamespace(gateway="pub_g")) == "pub_g"

    assert device_gateway(SimpleNamespace(_gateway="priv_g")) is None
    assert device_gateway(SimpleNamespace()) is None


def test_device_slug_accessor() -> None:
    """device_slug reads the public slug property."""
    assert device_slug(SimpleNamespace(slug="FAN")) == "FAN"

    assert device_slug(SimpleNamespace(_SLUG="REM")) is None
    assert device_slug(SimpleNamespace()) is None


def test_device_parent_fan_accessor() -> None:
    """device_parent_fan reads the public parent_fan property."""
    assert (
        device_parent_fan(SimpleNamespace(parent_fan="pub_fan")) == "pub_fan"
    )

    assert device_parent_fan(SimpleNamespace(_parent_fan="priv_fan")) is None
    assert device_parent_fan(SimpleNamespace()) is None


def test_device_filter_include_accessor() -> None:
    """device_filter_include reads device_filter.include_list."""
    pub = SimpleNamespace(
        device_filter=SimpleNamespace(include_list=["18:000001"])
    )
    assert device_filter_include(pub) == ["18:000001"]

    priv = SimpleNamespace(
        _device_filter=SimpleNamespace(_include=["18:000002"])
    )
    assert device_filter_include(priv) is None

    assert device_filter_include(SimpleNamespace()) is None


def test_engine_include_list_accessor() -> None:
    """engine_include_list reads engine.include_list or obj.include_list."""
    pub = SimpleNamespace(engine=SimpleNamespace(include_list=["pub"]))
    assert engine_include_list(pub) == ["pub"]

    # Engine-less object carrying its own public include list
    assert engine_include_list(SimpleNamespace(include_list=["g"])) == ["g"]

    # private members are no longer consulted
    priv_engine = SimpleNamespace(_engine=SimpleNamespace(_include=["e"]))
    assert engine_include_list(priv_engine) is None
    assert engine_include_list(SimpleNamespace(_include=["g"])) is None

    assert engine_include_list(SimpleNamespace()) is None


def test_add_to_include_lists_directions() -> None:
    """add_to_include_lists prefers mutators, falls back to list mutation."""

    def _adder(lst: list[str]) -> Any:
        return lambda d: lst.append(d) if d not in lst else None

    # public mutators (engine + device_filter)
    engine = SimpleNamespace(_include=[])
    engine.add_to_include = _adder(engine._include)
    dev_filter = SimpleNamespace(_include=[])
    dev_filter.add_to_include = _adder(dev_filter._include)
    client = SimpleNamespace(engine=engine, device_filter=dev_filter)
    add_to_include_lists(client, "01:000001")
    add_to_include_lists(client, "01:000001")  # idempotent
    assert engine._include == ["01:000001"]
    assert dev_filter._include == ["01:000001"]

    # fallback: mutate the live lists via the public accessors
    client_no_mut = SimpleNamespace(
        engine=SimpleNamespace(include_list=[]),
        device_filter=SimpleNamespace(include_list=[]),
    )
    add_to_include_lists(client_no_mut, "01:000002")
    assert client_no_mut.engine.include_list == ["01:000002"]
    assert client_no_mut.device_filter.include_list == ["01:000002"]

    # nothing to mutate — must not raise
    add_to_include_lists(SimpleNamespace(), "01:000003")


def test_remove_from_include_lists_directions() -> None:
    """remove_from_include_lists prefers mutators, falls back to lists."""

    def _remover(lst: list[str]) -> Any:
        return lambda d: lst.remove(d) if d in lst else None

    engine = SimpleNamespace(_include=["01:000001"])
    engine.remove_from_include = _remover(engine._include)
    dev_filter = SimpleNamespace(_include=["01:000001"])
    dev_filter.remove_from_include = _remover(dev_filter._include)
    client = SimpleNamespace(engine=engine, device_filter=dev_filter)
    remove_from_include_lists(client, "01:000001")
    remove_from_include_lists(client, "01:000001")  # idempotent
    assert engine._include == []
    assert dev_filter._include == []

    # fallback: mutate the live lists via the public accessors
    client_no_mut = SimpleNamespace(
        engine=SimpleNamespace(include_list=["01:000002"]),
        device_filter=SimpleNamespace(include_list=["01:000002"]),
    )
    remove_from_include_lists(client_no_mut, "01:000002")
    assert client_no_mut.engine.include_list == []
    assert client_no_mut.device_filter.include_list == []

    remove_from_include_lists(SimpleNamespace(), "01:000003")


def test_clear_async_attr_cache_empty_state() -> None:
    """Test clear_async_attr_cache handles entity without state_map."""
    entity = MagicMock(spec=[])
    clear_async_attr_cache(entity)  # Must not raise


def test_clear_async_attr_cache_cancels_tasks() -> None:
    """Test clear_async_attr_cache cancels in-flight resolving tasks."""
    mock_task = MagicMock()
    mock_task.done.return_value = False

    state = MagicMock()
    state.resolving_task = mock_task

    entity = MagicMock()
    entity.async_attr_state = {(123, "prop"): state}

    clear_async_attr_cache(entity)
    mock_task.cancel.assert_called_once()


def test_parse_packet_string_raw_frame() -> None:
    """Test parse_packet_string on raw RF packet frame."""
    raw_packet = "045  I --- 01:145038 --:------ 01:145038 1F09 003 0005C8"
    cmd = parse_packet_string(raw_packet)
    assert cmd is not None
    assert cmd.verb.strip() == "I"
    assert cmd.code == "1F09"

    # Invalid frame returns None
    assert parse_packet_string("invalid raw frame") is None


@pytest.mark.asyncio
async def test_resolve_async_attr_sync_and_async(hass: HomeAssistant) -> None:
    """Test resolve_async_attr helper with sync and async targets."""
    # 1. Sync value test
    entity = SimpleNamespace(hass=hass, entity_id="sensor.test")
    obj = SimpleNamespace(sync_prop="sync_val")

    val = resolve_async_attr(entity, obj, "sync_prop")
    assert val == "sync_val"
    assert not hasattr(entity, "async_attr_state")

    # 2. Async target resolution test
    async def _async_getter() -> str:
        await asyncio.sleep(0.01)
        return "async_resolved"

    obj.async_prop = _async_getter
    res = resolve_async_attr(entity, obj, "async_prop", default="default_val")
    assert res == "default_val"
    assert hasattr(entity, "async_attr_state")

    # Wait for background task to resolve
    state_map = entity.async_attr_state
    state = list(state_map.values())[0]
    if state.resolving_task:
        await state.resolving_task

    # Next call returns cached value
    res_cached = resolve_async_attr(entity, obj, "async_prop")
    assert res_cached == "async_resolved"


def test_fields_to_aware_and_as_iso_edge_cases() -> None:
    """Test fields_to_aware and as_iso date conversion edge cases."""
    aware_dt = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert fields_to_aware(aware_dt) == aware_dt
    assert as_iso("2026-01-01T12:00:00") == "2026-01-01T12:00:00"
    assert as_iso(aware_dt) == "2026-01-01T12:00:00"

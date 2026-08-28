"""Tests for ramses_cc helper utilities.

This module targets 100% coverage for helpers.py by testing device ID
mappings between Home Assistant and RAMSES RF hardware IDs, as well as
datetime utility functions.
"""

import asyncio
from datetime import datetime as dt
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.helpers import (
    as_iso,
    clear_async_attr_cache,
    fields_to_aware,
    ha_device_id_to_ramses_device_id,
    ramses_device_id_to_ha_device_id,
    resolve_async_attr,
)

# Constants
RAMSES_ID = "32:153289"


def test_ha_to_ramses_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from HA registry ID to RAMSES hardware ID."""
    # 1. Handle empty input
    assert ha_device_id_to_ramses_device_id(hass, "") is None

    # 2. Handle non-existent device
    assert ha_device_id_to_ramses_device_id(hass, "missing") is None

    # 3. Create a valid ConfigEntry to satisfy the DeviceRegistry requirement
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="test_config")
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

    result = ramses_device_id_to_ha_device_id(hass, RAMSES_ID)
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
    dt = dt_util.now()
    # as_iso strips tzinfo, so we compare to a naive version of the expected string
    assert as_iso(dt) == dt.replace(tzinfo=None).isoformat()

    # Test string input (pass-through)
    iso_str = "2024-01-01T10:00:00"
    assert as_iso(iso_str) == iso_str

    # Test None - implementation converts None to "None"
    assert as_iso(None) == "None"


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_resolve_async_attr_sync_and_async(hass: HomeAssistant) -> None:
    """Test resolve_async_attr helper with sync and async targets."""
    # 1. Sync value test
    entity = SimpleNamespace(hass=hass)
    obj = SimpleNamespace(sync_prop="sync_val")

    val = resolve_async_attr(entity, obj, "sync_prop")
    assert val == "sync_val"
    assert not hasattr(entity, "_async_attr_state")

    # 2. Async target resolution test
    async def _async_getter() -> str:
        await asyncio.sleep(0.01)
        return "async_resolved"

    obj.async_prop = _async_getter
    res = resolve_async_attr(entity, obj, "async_prop", default="default_val")
    assert res == "default_val"
    assert hasattr(entity, "_async_attr_state")

    # Wait for background task to resolve
    state_map = entity._async_attr_state
    state = list(state_map.values())[0]
    if state.resolving_task:
        await state.resolving_task

    # Next call returns cached value
    res_cached = resolve_async_attr(entity, obj, "async_prop")
    assert res_cached == "async_resolved"


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_clear_async_attr_cache(hass: HomeAssistant) -> None:
    """Test clearing async attribute resolution cache and cancelling tasks."""
    entity = SimpleNamespace(hass=hass)
    obj = SimpleNamespace()

    async def _slow_getter() -> str:
        await asyncio.sleep(10)
        return "slow"

    obj.slow_prop = _slow_getter
    resolve_async_attr(entity, obj, "slow_prop", default="default")

    state_map = getattr(entity, "_async_attr_state", {})
    assert len(state_map) == 1

    clear_async_attr_cache(entity)
    assert len(state_map) == 0

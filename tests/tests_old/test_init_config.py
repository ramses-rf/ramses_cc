"""Test the setup of ramses_cc with different configurations, but no data."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Final
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc import CONFIG_SCHEMA, DOMAIN, RamsesBroker
from custom_components.ramses_cc.config_flow import SZ_RESTORE_CACHE
from ramses_rf.gateway import Gateway

from ..virtual_rf import VirtualRf

# patched constants
_CALL_LATER_DELAY: Final = 0  # from: custom_components.ramses_cc.services.py

NUM_SVCS_AFTER = 35  # proxy for success, platform services included since 0.51.8


TEST_CONFIGS = {
    "config_00": {
        "serial_port": {"port_name": None},
        "ramses_rf": {"disable_discovery": True},
    },
    "config_10": {
        "serial_port": {"port_name": None},
        "ramses_rf": {"disable_discovery": True},
        "packet_log": None,
    },
    "config_11": {
        "serial_port": {"port_name": None},
        "ramses_rf": {"disable_discovery": True},
        "packet_log": "packet.log",
    },
    "config_12": {
        "serial_port": {"port_name": None},
        "ramses_rf": {"disable_discovery": True},
        "packet_log": {"file_name": "packet.log"},
    },
    "config_13": {
        "serial_port": {"port_name": None},
        "ramses_rf": {"disable_discovery": True},
        "packet_log": {"file_name": "packet.log", "rotate_backups": 7},
    },
    "config_fan_unbind": {
        "serial_port": {"port_name": None},
        "ramses_rf": {"disable_discovery": True},
        "known_list": {
            "30:123456": {  # A Fan Device
                "class": "FAN",
                "bound": None,  # <--- This is what your PR allows
            }
        },
    },
}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    metafunc.parametrize("config", TEST_CONFIGS.values(), ids=TEST_CONFIGS.keys())


@pytest.fixture()  # add hass fixture to ensure hass/rf use same event loop
async def rf(hass: HomeAssistant) -> AsyncGenerator[Any]:
    """Utilize a virtual evofw3-compatible gateway."""

    rf = VirtualRf(2)
    rf.set_gateway(rf.ports[0], "18:006402")

    with patch("ramses_tx.transport.comports", rf.comports):
        try:
            yield rf
        finally:
            await rf.stop()


async def _test_common(hass: HomeAssistant, entry: ConfigEntry = None) -> None:
    """The main tests are here."""

    # hass.data["custom_components"][DOMAIN]  # homeassistant.loader.Integration
    assert isinstance(list(hass.data[DOMAIN].values())[0], RamsesBroker)
    assert isinstance(list(hass.data[DOMAIN].values())[0].client, Gateway)

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1

    assert entry is None or entry is entries[0]

    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    assert hass.data["setup_tasks"] == {}
    assert isinstance(hass.data[DOMAIN][entry.entry_id], RamsesBroker)

    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    assert len(broker._devices) == 1  # 18_000730

    assert (
        len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 1
    )  # binary_sensor.18_000730_status

    assert len(hass.services.async_services_for_domain(DOMAIN)) == NUM_SVCS_AFTER


@patch("custom_components.ramses_cc.services._CALL_LATER_DELAY", _CALL_LATER_DELAY)
async def test_services_entry_(
    hass: HomeAssistant, rf: VirtualRf, config: dict[str, Any]
) -> None:
    """Test ramses_cc via config entry."""

    config["serial_port"]["port_name"] = rf.ports[0]
    config = CONFIG_SCHEMA({DOMAIN: config})[DOMAIN]
    config[CONF_SCAN_INTERVAL] = config[CONF_SCAN_INTERVAL].total_seconds()
    config.pop(SZ_RESTORE_CACHE, None)

    assert len(hass.config_entries.async_entries(DOMAIN)) == 0
    entry = MockConfigEntry(domain=DOMAIN, options=config)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    # await hass.async_block_till_done()  # ?clear hass._tasks

    #
    try:
        await _test_common(hass, entry)
    finally:
        assert await hass.config_entries.async_remove(entry.entry_id)  # will unload


@patch("custom_components.ramses_cc.services._CALL_LATER_DELAY", _CALL_LATER_DELAY)
async def test_services_import(
    hass: HomeAssistant, rf: VirtualRf, config: dict[str, Any]
) -> None:
    """Test ramses_cc via importing a configuration."""

    config["serial_port"]["port_name"] = rf.ports[0]

    assert await async_setup_component(hass, DOMAIN, {DOMAIN: config})
    # await hass.async_block_till_done()  # ?clear hass._tasks

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    try:
        await _test_common(hass, entry)
    finally:
        assert await hass.config_entries.async_remove(entry.entry_id)  # will unload

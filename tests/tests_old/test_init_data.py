"""Test the setup of ramses_cc with data (vanilla configuration)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Final
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc import DOMAIN, RamsesEntity
from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.climate import RamsesController, RamsesHvac, RamsesZone
from ramses_rf.gateway import Gateway

from ..virtual_rf import VirtualRf
from .helpers import TEST_DIR, cast_packets_to_rf

# patched constants
_CALL_LATER_DELAY: Final = 0  # from: custom_components.ramses_cc.broker.py

# fmt: off
EXPECTED_ENTITIES = [  # TODO: add OTB entities, adjust list when adding sensors etc
    "18:006402-status",
    "01:145038-status", "01:145038", "01:145038-heat_demand", "01:145038-active_fault",

    "01:145038_02", "01:145038_02-heat_demand", "01:145038_02-window_open",
    "01:145038_0A", "01:145038_0A-heat_demand", "01:145038_0A-window_open",
    "01:145038_HW", "01:145038_HW-heat_demand", "01:145038_HW-relay_demand",

    "04:056053-battery_low", "04:056053-heat_demand", "04:056053-temperature", "04:056053-window_open",
    "04:189082-battery_low", "04:189082-heat_demand", "04:189082-temperature", "04:189082-window_open",

    "07:046947-battery_low", "07:046947-temperature",

    "13:081775-active", "13:081775-relay_demand",  # missing?
    "13:120241-active", "13:120241-relay_demand",
    "13:120242-active", "13:120242-relay_demand",
    "13:202850-active", "13:202850-relay_demand",  # missing?

    "22:140285-battery_low", "22:140285-temperature",
    "34:092243-battery_low", "34:092243-temperature",
]
# fmt: on

NUM_DEVS_SETUP = 1  # HGI (before casting packets to RF)
NUM_DEVS_AFTER = 13  # proxy for success of cast_packets_to_rf()
# adjust NUM_DEVS_AFTER when adding sensors etc. was: 9
NUM_SVCS_AFTER = 31  # proxy for success, platform services included since 0.51.8
NUM_ENTS_AFTER = 43  # proxy for success


TEST_CONFIG = {
    "serial_port": {"port_name": None},
    "ramses_rf": {"disable_discovery": True},
}


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


async def _test_common(hass: HomeAssistant, entry: ConfigEntry, rf: VirtualRf) -> None:
    """The main tests are here."""

    gwy: Gateway = list(hass.data[DOMAIN].values())[0].client
    assert len(gwy.devices) == NUM_DEVS_SETUP
    assert gwy.config.disable_discovery is True

    await cast_packets_to_rf(
        rf, f"{TEST_DIR}/system_1.log", gwy=gwy
    )  # <<< issue 249 happens here
    assert len(gwy.devices) == NUM_DEVS_AFTER  # adjust when adding sensors etc

    assert len(hass.services.async_services_for_domain(DOMAIN)) == NUM_SVCS_AFTER

    broker: RamsesBroker = list(hass.data[DOMAIN].values())[0]
    assert len(broker._entities) == 1

    await broker.async_update()
    await hass.async_block_till_done()

    # for x in broker._entities:  # debug issue 278, 249
    #     if x not in EXPECTED_ENTITIES:
    #         print("_test_common extra: " + str(x))
    assert not [x for x in broker._entities if x not in EXPECTED_ENTITIES]  # extras
    # for x in EXPECTED_ENTITIES:  # debug issue 278, 249
    #     if x not in broker._entities:
    #         print("_test_common missing: " + str(x))
    assert not [x for x in EXPECTED_ENTITIES if x not in broker._entities]  # missing

    # ramses_rf entities
    assert len(broker._devices) == NUM_DEVS_AFTER  # adjust when adding sensors etc
    assert len(broker._dhws) == 1
    assert len(broker._remotes) == 0
    assert len(broker._systems) == 1
    assert len(broker._zones) == 2


def find_entities(hass: HomeAssistant, platform: Platform) -> list[RamsesEntity]:
    return list(hass.data["domain_platform_entities"][platform, DOMAIN].values())


async def _test_names(hass: HomeAssistant, entry: ConfigEntry, rf: VirtualRf) -> None:
    """The main tests are here."""

    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    await broker.async_update()

    for entity in find_entities(hass, Platform.CLIMATE):
        if isinstance(entity, RamsesController):
            assert entity.name == f"Controller {entity._device.id}"
        elif isinstance(entity, RamsesZone):
            assert entity.name == entity._device.name
        elif isinstance(entity, RamsesHvac):
            assert entity.name
        else:
            raise AssertionError()

    for entity in find_entities(hass, Platform.WATER_HEATER):
        assert entity.name

    for entity in find_entities(hass, Platform.REMOTE):
        assert entity.name

    for entity in find_entities(hass, Platform.BINARY_SENSOR):
        assert entity.name

    for entity in find_entities(hass, Platform.SENSOR):
        assert entity.name


@patch("custom_components.ramses_cc.broker._CALL_LATER_DELAY", _CALL_LATER_DELAY)
async def test_services_entry_(
    hass: HomeAssistant, rf: VirtualRf, config: dict[str, Any] = TEST_CONFIG
) -> None:
    """Test ramses_cc via config entry."""

    config["serial_port"]["port_name"] = rf.ports[0]

    assert len(hass.config_entries.async_entries(DOMAIN)) == 0
    entry = MockConfigEntry(domain=DOMAIN, options=config)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    # await hass.async_block_till_done()  # ?clear hass._tasks

    #
    try:
        pass
        await _test_common(hass, entry, rf)
        # await _test_names(hass, entry, rf)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@patch("custom_components.ramses_cc.broker._CALL_LATER_DELAY", _CALL_LATER_DELAY)
async def test_services_import(
    hass: HomeAssistant, rf: VirtualRf, config: dict[str, Any] = TEST_CONFIG
) -> None:
    """Test ramses_cc via importing a configuration."""

    config["serial_port"]["port_name"] = rf.ports[0]

    #
    #
    #

    assert await async_setup_component(hass, DOMAIN, {DOMAIN: config})
    # await hass.async_block_till_done()  # ?clear hass._tasks

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    try:
        await _test_common(hass, entry, rf)
        # await _test_names(hass, entry, rf)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)

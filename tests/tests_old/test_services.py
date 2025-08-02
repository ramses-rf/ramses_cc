"""Test the services of ramses_cc."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime as dt, timedelta as td
from typing import Any, Final
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc import (
    DOMAIN,
    SCH_BIND_DEVICE,
    SCH_NO_SVC_PARAMS,
    SCH_SEND_PACKET,
    SVC_BIND_DEVICE,
    SVC_FORCE_UPDATE,
    SVC_SEND_PACKET,
)
from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.climate import SVCS_RAMSES_CLIMATE
from custom_components.ramses_cc.remote import SVCS_RAMSES_REMOTE
from custom_components.ramses_cc.schemas import (
    SCH_DELETE_COMMAND,
    SCH_LEARN_COMMAND,
    SCH_NO_ENTITY_SVC_PARAMS,
    SCH_PUT_CO2_LEVEL,
    SCH_PUT_DHW_TEMP,
    SCH_PUT_INDOOR_HUMIDITY,
    SCH_PUT_ROOM_TEMP,
    SCH_SEND_COMMAND,
    SCH_SET_DHW_MODE,
    SCH_SET_DHW_PARAMS,
    SCH_SET_DHW_SCHEDULE,
    SCH_SET_SYSTEM_MODE,
    SCH_SET_ZONE_CONFIG,
    SCH_SET_ZONE_MODE,
    SCH_SET_ZONE_SCHEDULE,
    SVC_DELETE_COMMAND,
    SVC_FAKE_DHW_TEMP,
    SVC_FAKE_ZONE_TEMP,
    SVC_GET_DHW_SCHEDULE,
    SVC_GET_ZONE_SCHEDULE,
    SVC_LEARN_COMMAND,
    SVC_PUT_CO2_LEVEL,
    SVC_PUT_DHW_TEMP,
    SVC_PUT_INDOOR_HUMIDITY,
    SVC_PUT_ROOM_TEMP,
    SVC_RESET_DHW_MODE,
    SVC_RESET_DHW_PARAMS,
    SVC_RESET_SYSTEM_MODE,
    SVC_RESET_ZONE_CONFIG,
    SVC_RESET_ZONE_MODE,
    SVC_SEND_COMMAND,
    SVC_SET_DHW_BOOST,
    SVC_SET_DHW_MODE,
    SVC_SET_DHW_PARAMS,
    SVC_SET_DHW_SCHEDULE,
    SVC_SET_SYSTEM_MODE,
    SVC_SET_ZONE_CONFIG,
    SVC_SET_ZONE_MODE,
    SVC_SET_ZONE_SCHEDULE,
)
from custom_components.ramses_cc.sensor import SVCS_RAMSES_SENSOR
from custom_components.ramses_cc.water_heater import SVCS_RAMSES_WATER_HEATER
from ramses_rf.gateway import Gateway
from ramses_tx.const import Priority
from ramses_tx.exceptions import CommandInvalid

from ..virtual_rf import VirtualRf
from .helpers import TEST_DIR, cast_packets_to_rf

# patched constants
_CALL_LATER_DELAY: Final = 0  # from: custom_components.ramses_cc.broker.py


NUM_DEVS_BEFORE = 3  # HGI, faked THM, faked REM
NUM_DEVS_AFTER = 15  # proxy for success of cast_packets_to_rf()
NUM_SVCS_AFTER = 10  # proxy for success
NUM_ENTS_AFTER = 45  # proxy for success


# until an hour from now as "2024-03-16 14:00:00"
_UNTIL = (dt.now().replace(minute=0, second=0, microsecond=0) + td(hours=2)).strftime(
    "%Y-%m-%d %H:%M:%S"
)


TEST_CONFIG: Final = {
    "serial_port": {"port_name": None},
    "ramses_rf": {"disable_discovery": True},
    "advanced_features": {"send_packet": True},
    "known_list": {
        "03:123456": {"class": "THM", "faked": True},
        "32:097710": {"class": "CO2"},
        "32:139773": {"class": "HUM"},
        "37:123456": {"class": "FAN"},
        "40:123456": {"class": "REM", "faked": True},
    },
}


SERVICES = {
    SVC_BIND_DEVICE: (
        "custom_components.ramses_cc.broker.RamsesBroker.async_bind_device",
        SCH_BIND_DEVICE,
    ),
    SVC_DELETE_COMMAND: (
        "custom_components.ramses_cc.remote.RamsesRemote.async_delete_command",
        SCH_DELETE_COMMAND,
    ),
    SVC_FAKE_DHW_TEMP: (
        "custom_components.ramses_cc.water_heater.RamsesWaterHeater.async_fake_dhw_temp",
        SCH_PUT_DHW_TEMP,
    ),
    SVC_FAKE_ZONE_TEMP: (
        "custom_components.ramses_cc.climate.RamsesZone.async_fake_zone_temp",
        SCH_PUT_ROOM_TEMP,
    ),
    SVC_FORCE_UPDATE: (
        "custom_components.ramses_cc.broker.RamsesBroker.async_force_update",
        SCH_NO_SVC_PARAMS,
    ),
    SVC_GET_DHW_SCHEDULE: (
        "custom_components.ramses_cc.water_heater.RamsesWaterHeater.async_get_dhw_schedule",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_GET_ZONE_SCHEDULE: (
        "custom_components.ramses_cc.climate.RamsesZone.async_get_zone_schedule",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_LEARN_COMMAND: (
        "custom_components.ramses_cc.remote.RamsesRemote.async_learn_command",
        SCH_LEARN_COMMAND,
    ),
    SVC_PUT_CO2_LEVEL: (
        "custom_components.ramses_cc.sensor.RamsesSensor.async_put_co2_level",
        SCH_PUT_CO2_LEVEL,
    ),
    SVC_PUT_DHW_TEMP: (
        "custom_components.ramses_cc.sensor.RamsesSensor.async_put_dhw_temp",
        SCH_PUT_DHW_TEMP,
    ),
    SVC_PUT_INDOOR_HUMIDITY: (
        "custom_components.ramses_cc.sensor.RamsesSensor.async_put_indoor_humidity",
        SCH_PUT_INDOOR_HUMIDITY,
    ),
    SVC_PUT_ROOM_TEMP: (
        "custom_components.ramses_cc.sensor.RamsesSensor.async_put_room_temp",
        SCH_PUT_ROOM_TEMP,
    ),
    SVC_SEND_COMMAND: (
        "custom_components.ramses_cc.remote.RamsesRemote.async_send_command",
        SCH_SEND_COMMAND,
    ),
    SVC_SEND_PACKET: (
        "custom_components.ramses_cc.broker.RamsesBroker.async_send_packet",
        SCH_SEND_PACKET,
    ),
    SVC_RESET_DHW_MODE: (
        "custom_components.ramses_cc.water_heater.RamsesWaterHeater.async_reset_dhw_mode",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_RESET_DHW_PARAMS: (
        "custom_components.ramses_cc.water_heater.RamsesWaterHeater.async_reset_dhw_params",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_RESET_SYSTEM_MODE: (
        "custom_components.ramses_cc.climate.RamsesController.async_reset_system_mode",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_RESET_ZONE_CONFIG: (
        "custom_components.ramses_cc.climate.RamsesZone.async_reset_zone_config",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_RESET_ZONE_MODE: (
        "custom_components.ramses_cc.climate.RamsesZone.async_reset_zone_mode",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_SET_DHW_BOOST: (
        "custom_components.ramses_cc.water_heater.RamsesWaterHeater.async_set_dhw_boost",
        SCH_NO_ENTITY_SVC_PARAMS,
    ),
    SVC_SET_DHW_MODE: (
        # Use ramses_rf built-in validation, by mocking
        "ramses_rf.gateway.Gateway.send_cmd",
        # to catch nested entry schema, uses dedicated asserts then other services
        SCH_SET_DHW_MODE,
    ),
    SVC_SET_DHW_PARAMS: (
        "custom_components.ramses_cc.water_heater.RamsesWaterHeater.async_set_dhw_params",
        SCH_SET_DHW_PARAMS,
    ),
    SVC_SET_DHW_SCHEDULE: (
        "custom_components.ramses_cc.water_heater.RamsesWaterHeater.async_set_dhw_schedule",
        SCH_SET_DHW_SCHEDULE,
    ),
    SVC_SET_SYSTEM_MODE: (
        # Use ramses_rf built-in validation, by mocking
        "ramses_rf.gateway.Gateway.send_cmd",
        # to catch nested entry schema, uses dedicated asserts then other services
        SCH_SET_SYSTEM_MODE,
    ),
    SVC_SET_ZONE_CONFIG: (
        "custom_components.ramses_cc.climate.RamsesZone.async_set_zone_config",
        SCH_SET_ZONE_CONFIG,
    ),
    SVC_SET_ZONE_MODE: (
        # Use ramses_rf built-in validation, by mocking
        "ramses_rf.gateway.Gateway.send_cmd",
        # to catch nested entry schema, uses dedicated asserts then other services
        SCH_SET_ZONE_MODE,
    ),
    SVC_SET_ZONE_SCHEDULE: (
        "custom_components.ramses_cc.climate.RamsesZone.async_set_zone_schedule",
        SCH_SET_ZONE_SCHEDULE,
    ),
}


async def _cast_packets_to_rf(hass: HomeAssistant, rf: VirtualRf) -> None:
    """Load packets from a CH/DHW system."""

    gwy: Gateway = list(hass.data[DOMAIN].values())[0].client
    assert len(gwy.devices) == NUM_DEVS_BEFORE

    await cast_packets_to_rf(rf, f"{TEST_DIR}/system_1.log", gwy=gwy)

    try:
        assert len(gwy.devices) == NUM_DEVS_AFTER  # proxy for success of above
    except AssertionError:
        assert len(gwy.devices) == NUM_DEVS_AFTER - 4

    assert len(hass.services.async_services_for_domain(DOMAIN)) == NUM_SVCS_AFTER


async def _setup_via_entry_(
    hass: HomeAssistant, rf: VirtualRf, config: dict[str, Any] = TEST_CONFIG
) -> ConfigEntry:
    """Test ramses_cc via config entry."""

    config["serial_port"]["port_name"] = rf.ports[0]

    assert len(hass.config_entries.async_entries(DOMAIN)) == 0
    entry = MockConfigEntry(domain=DOMAIN, options=config)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    # await hass.async_block_till_done()  # ?clear hass._tasks

    await _cast_packets_to_rf(hass, rf)

    broker: RamsesBroker = list(hass.data[DOMAIN].values())[0]

    await broker.async_update()
    await hass.async_block_till_done()

    try:
        assert len(broker._entities) == NUM_ENTS_AFTER  # proxy for success of above
    except AssertionError:
        assert len(broker._entities) == NUM_ENTS_AFTER - 9

    return entry


@pytest.fixture()  # need hass fixture to ensure hass/rf use same event loop
async def entry(hass: HomeAssistant) -> AsyncGenerator[ConfigEntry]:
    """Set up the test bed."""

    # Utilize a virtual evofw3-compatible gateway
    rf = VirtualRf(2)
    rf.set_gateway(rf.ports[0], "18:006402")

    with patch(
        "custom_components.ramses_cc.broker._CALL_LATER_DELAY", _CALL_LATER_DELAY
    ):
        entry: ConfigEntry = None
        try:
            entry = await _setup_via_entry_(hass, rf, TEST_CONFIG)
            yield entry

        finally:
            if entry:
                await hass.config_entries.async_unload(entry.entry_id)
                # await hass.async_block_till_done()
            await rf.stop()


async def _test_entity_service_call(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
    asserts: dict[str, Any] | None = None,
    *,
    schemas: dict[str, vol.Schema] | None = None,
) -> None:
    """Test an entity service call."""

    # should check that the entity exists, and is available

    assert not schemas or schemas[service] == SERVICES[service][1]

    with patch(SERVICES[service][0]) as mock_method:
        _ = await hass.services.async_call(
            DOMAIN, service=service, service_data=data, blocking=True
        )

        mock_method.assert_called_once()

        if asserts is None:
            assert mock_method.call_args.kwargs == {
                k: v for k, v in SERVICES[service][1](data).items() if k != "entity_id"
            }
        else:
            assert mock_method.call_args.kwargs == asserts


async def _test_service_call(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
    *,
    schemas: dict[str, vol.Schema] | None = None,
) -> None:
    """Test a service call."""

    # should check that referenced entity, if any, exists and is available

    assert not schemas or schemas[service] == SERVICES[service][1]

    with patch(SERVICES[service][0]) as mock_method:
        _ = await hass.services.async_call(
            DOMAIN, service=service, service_data=data, blocking=True
        )

        mock_method.assert_called_once()

        service_call: ServiceCall = mock_method.call_args[0][0]
        assert service_call.data == SERVICES[service][1](data)


########################################################################################


async def test_delete_command(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the ramses_cc.delete_command service call."""

    data = {
        "entity_id": "remote.40_123456",
        "command": "boost",
    }

    await _test_entity_service_call(
        hass, SVC_DELETE_COMMAND, data, schemas=SVCS_RAMSES_REMOTE
    )


# TODO: extended test of underlying method
async def test_learn_command(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the ramses_cc.learn_command service call."""

    data = {
        "entity_id": "remote.40_123456",
        "command": "boost",
        "timeout": 60,
    }

    await _test_entity_service_call(
        hass, SVC_LEARN_COMMAND, data, schemas=SVCS_RAMSES_REMOTE
    )


TESTS_SEND_COMMAND = {
    "01": {"command": "auto"},
    "07": {"command": "auto", "num_repeats": 1, "delay_secs": 0.02},  # min
    "08": {"command": "auto", "num_repeats": 3, "delay_secs": 0.05},  # default
    "09": {"command": "auto", "num_repeats": 5, "delay_secs": 1.0},  # max
}


# TODO: extended test of underlying method
@pytest.mark.parametrize("idx", TESTS_SEND_COMMAND)
async def test_send_command(hass: HomeAssistant, entry: ConfigEntry, idx: str) -> None:
    """Test the ramses_cc.send_command service call."""

    data = {
        "entity_id": "remote.40_123456",
        **TESTS_SEND_COMMAND[idx],  # type: ignore[dict-item]
    }

    await _test_entity_service_call(
        hass, SVC_SEND_COMMAND, data, schemas=SVCS_RAMSES_REMOTE
    )


async def test_put_co2_level(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the put_room_co2_level service call."""

    data = {
        "entity_id": "sensor.32_097710_co2_level",
        "co2_level": 600,
    }

    await _test_entity_service_call(
        hass, SVC_PUT_CO2_LEVEL, data, schemas=SVCS_RAMSES_SENSOR
    )


async def test_put_dhw_temp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the put_dhe_temp service call."""

    data = {
        "entity_id": "sensor.07_046947_temperature",
        "temperature": 56.3,
    }

    await _test_entity_service_call(
        hass, SVC_PUT_DHW_TEMP, data, schemas=SVCS_RAMSES_SENSOR
    )


async def test_put_indoor_humidity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the put_indoor_humidity service call."""

    data = {
        "entity_id": "sensor.32_139773_indoor_humidity",
        "indoor_humidity": 56.3,
    }

    await _test_entity_service_call(
        hass, SVC_PUT_INDOOR_HUMIDITY, data, schemas=SVCS_RAMSES_SENSOR
    )


async def test_put_room_temp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the put_room_temp service call."""

    data = {
        "entity_id": "sensor.34_092243_temperature",
        "temperature": 21.3,
    }

    await _test_entity_service_call(
        hass, SVC_PUT_ROOM_TEMP, data, schemas=SVCS_RAMSES_SENSOR
    )


async def test_fake_dhw_temp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {
        "entity_id": "water_heater.01_145038_hw",
        "temperature": 51.3,
    }

    await _test_entity_service_call(
        hass, SVC_FAKE_DHW_TEMP, data, schemas=SVCS_RAMSES_WATER_HEATER
    )


async def test_fake_zone_temp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {
        "entity_id": "climate.01_145038_02",
        "temperature": 21.3,
    }

    await _test_entity_service_call(
        hass, SVC_FAKE_ZONE_TEMP, data, schemas=SVCS_RAMSES_CLIMATE
    )


async def test_get_dhw_schedule(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {"entity_id": "water_heater.01_145038_hw"}

    await _test_entity_service_call(
        hass, SVC_GET_DHW_SCHEDULE, data, schemas=SVCS_RAMSES_WATER_HEATER
    )


async def test_get_zone_schedule(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {"entity_id": "climate.01_145038_02"}

    await _test_entity_service_call(
        hass, SVC_GET_ZONE_SCHEDULE, data, schemas=SVCS_RAMSES_CLIMATE
    )


async def test_reset_dhw_mode(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {"entity_id": "water_heater.01_145038_hw"}

    await _test_entity_service_call(
        hass, SVC_RESET_DHW_MODE, data, schemas=SVCS_RAMSES_WATER_HEATER
    )


async def test_reset_dhw_params(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {"entity_id": "water_heater.01_145038_hw"}

    await _test_entity_service_call(
        hass, SVC_RESET_DHW_PARAMS, data, schemas=SVCS_RAMSES_WATER_HEATER
    )


async def test_reset_system_mode(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {"entity_id": "climate.01_145038"}

    await _test_entity_service_call(
        hass, SVC_RESET_SYSTEM_MODE, data, schemas=SVCS_RAMSES_CLIMATE
    )


async def test_reset_zone_config(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {
        "entity_id": "climate.01_145038_02",
    }

    await _test_entity_service_call(
        hass, SVC_RESET_ZONE_CONFIG, data, schemas=SVCS_RAMSES_CLIMATE
    )


async def test_reset_zone_mode(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {"entity_id": "climate.01_145038_02"}

    await _test_entity_service_call(
        hass, SVC_RESET_ZONE_MODE, data, schemas=SVCS_RAMSES_CLIMATE
    )


async def test_set_dhw_boost(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {"entity_id": "water_heater.01_145038_hw"}

    await _test_entity_service_call(
        hass, SVC_SET_DHW_BOOST, data, schemas=SVCS_RAMSES_WATER_HEATER
    )


# See: https://github.com/ramses-rf/ramses_cc/issues/163
TESTS_SET_DHW_MODE_GOOD = {
    # "11": {"mode": "follow_schedule"},  # CommandInvalid: Invalid args: For mode=00, until and duration must both be None
    # "21": {"mode": "permanent_override", "active": True},  # CommandInvalid: Invalid args: For mode=02, until and duration must both be None
    # "31": {"mode": "advanced_override", "active": True},  # CommandInvalid: Invalid args: For mode=01, until and duration must both be None
    "41": {"mode": "temporary_override", "active": True},
    "52": {"mode": "temporary_override", "active": True, "duration": {"hours": 5}},
    "62": {"mode": "temporary_override", "active": True, "until": _UNTIL},
    # TODO next 2 should fail in Gateway.send_cmd()
    "42": {"mode": "temporary_override", "active": False},  # #        missing duration
    "79": {
        "mode": "temporary_override",
        "active": True,
        "duration": {"hours": 5},
        "until": _UNTIL,
    },
}  # requires custom assert, returned from mock method success
TESTS_SET_DHW_MODE_FAIL: dict[str, dict[str, Any]] = {
    "00": {},  # #                                                     missing mode
    "29": {"active": True},  # #                                       missing mode
    "59": {"active": True, "duration": {"hours": 5}},  # #             missing mode
    "69": {"active": True, "until": _UNTIL},  # #                      missing mode
}
TESTS_SET_DHW_MODE_FAIL2: dict[str, dict[str, Any]] = {
    "11": {
        "mode": "follow_schedule"
    },  # CommandInvalid: Invalid args: For mode=00, until and duration must both be None
    "21": {
        "mode": "permanent_override",
        "active": True,
    },  # CommandInvalid: Invalid args: For mode=02, until and duration must both be None
    "31": {
        "mode": "advanced_override",
        "active": True,
    },  # CommandInvalid: Invalid args: For mode=01, until and duration must both be None
    # above 3 should be GOOD
    "12": {"mode": "follow_schedule", "active": True},  # #            *extra* active
    "20": {"mode": "permanent_override"},  # #                         missing active
    "22": {"mode": "permanent_override", "active": True, "duration": {"hours": 5}},
    "23": {"mode": "permanent_override", "active": True, "until": _UNTIL},
    "30": {"mode": "advanced_override"},  # #                          missing active
    "32": {"mode": "advanced_override", "active": True, "duration": {"hours": 5}},
    "33": {"mode": "advanced_override", "active": True, "until": _UNTIL},
    "40": {"mode": "temporary_override"},  # #                         missing active
    # "42": {"mode": "temporary_override", "active": False},  # #        missing duration
    "50": {"mode": "temporary_override", "duration": {"hours": 5}},  # missing active
    "60": {"mode": "temporary_override", "until": _UNTIL},  # #        missing active
    # "79": {
    #     "mode": "temporary_override",
    #     "active": True,
    #     "duration": {"hours": 5},
    #     "until": _UNTIL,
    # },
}


# TODO: extended test of underlying method (duration/until)
@pytest.mark.parametrize("idx", TESTS_SET_DHW_MODE_GOOD)
async def test_set_dhw_mode_good(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that valid params are acceptable to the entity service schema."""

    data = {
        "entity_id": "water_heater.01_145038_hw",
        **TESTS_SET_DHW_MODE_GOOD[idx],  # type: ignore[dict-item]
    }

    asserts = {"priority": Priority.HIGH, "wait_for_reply": True}

    await _test_entity_service_call(
        hass, SVC_SET_DHW_MODE, data, asserts, schemas=SVCS_RAMSES_WATER_HEATER
    )

    # # without the mock, can confirm the params are acceptable to the library
    # _ = await hass.services.async_call(
    #     DOMAIN, service=SVC_SET_DHW_MODE, service_data=data, blocking=True
    # )


@pytest.mark.parametrize("idx", TESTS_SET_DHW_MODE_FAIL)
async def test_set_dhw_mode_fail(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that invalid params are unacceptable to the entity service schema."""

    data = {
        "entity_id": "water_heater.01_145038_hw",
        **TESTS_SET_DHW_MODE_FAIL[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_DHW_MODE, data, schemas=SVCS_RAMSES_WATER_HEATER
        )
    except vol.MultipleInvalid:
        pass
    else:
        raise AssertionError("Expected vol.MultipleInvalid")


@pytest.mark.parametrize("idx", TESTS_SET_DHW_MODE_FAIL2)
async def test_set_dhw_mode_fail2(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that invalid params are unacceptable to the entity service schema."""

    data = {
        "entity_id": "water_heater.01_145038_hw",
        **TESTS_SET_DHW_MODE_FAIL2[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_DHW_MODE, data, schemas=SVCS_RAMSES_WATER_HEATER
        )
    except CommandInvalid:
        pass
    else:
        raise AssertionError("Expected Wrong Argument exception")


TESTS_SET_DHW_PARAMS = {
    "00": {},
    "01": {"setpoint": 55},
    "07": {"setpoint": 30, "overrun": 0, "differential": 1},  # min
    "08": {"setpoint": 50, "overrun": 0, "differential": 10},  # default
    "09": {"setpoint": 85, "overrun": 10, "differential": 10},  # max
}


@pytest.mark.parametrize("idx", TESTS_SET_DHW_PARAMS)
async def test_set_dhw_params(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    data = {
        "entity_id": "water_heater.01_145038_hw",
        **TESTS_SET_DHW_PARAMS[idx],
    }

    await _test_entity_service_call(
        hass, SVC_SET_DHW_PARAMS, data, schemas=SVCS_RAMSES_WATER_HEATER
    )


async def test_set_dhw_schedule(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {
        "entity_id": "water_heater.01_145038_hw",
        "schedule": "",
    }

    await _test_entity_service_call(
        hass, SVC_SET_DHW_SCHEDULE, data, schemas=SVCS_RAMSES_WATER_HEATER
    )


# Set_system_mode tests
TESTS_SET_SYSTEM_MODE_GOOD: dict[str, dict[str, Any]] = {
    "00": {"mode": "auto"},
    "01": {"mode": "eco_boost"},
    "02": {"mode": "day_off", "period": {"days": 3}},
    "03": {"mode": "eco_boost", "duration": {"hours": 3, "minutes": 30}},
    # TODO next entry should fail in Gateway.send_cmd()
    "05": {
        "mode": "day_off",
        "period": {"days": 3},
        "duration": {"hours": 3, "minutes": 30},
    },
}  # requires custom assert, returned from mock method success
TESTS_SET_SYSTEM_MODE_FAIL: dict[str, dict[str, Any]] = {
    "04": {},
}  # no asserts, caught in entity_schema
TESTS_SET_SYSTEM_MODE_FAIL2: dict[str, dict[str, Any]] = {
    # TODO next entry should fail in Gateway.send_cmd()
    # "05": {
    #     "mode": "day_off",
    #     "period": {"days": 3},
    #     "duration": {"hours": 3, "minutes": 30},
    # },
}
# TESTS_SET_SYSTEM_MODE_FAIL2_EXCEPTIONS:
#     "05": {},  # requires custom asserts, returned from mock method failure


# TODO: extended test of underlying method (duration/period)
@pytest.mark.parametrize("idx", TESTS_SET_SYSTEM_MODE_GOOD)
async def test_set_system_mode_good(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that valid params are acceptable to the entity service schema
    as well as to the (mocked) parsing checks in ramses_rf.gateway.Gateway.send_cmd
    Cover nested if-then-else not supported as entity-schema since HA 2025.09"""

    data = {
        "entity_id": "climate.01_145038",
        **TESTS_SET_SYSTEM_MODE_GOOD[idx],
    }

    asserts = {"priority": Priority.HIGH, "wait_for_reply": True}

    await _test_entity_service_call(
        hass, SVC_SET_SYSTEM_MODE, data, asserts, schemas=SVCS_RAMSES_CLIMATE
    )


@pytest.mark.parametrize("idx", TESTS_SET_SYSTEM_MODE_FAIL)
async def test_set_system_mode_fail(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that invalid params are unacceptable to the entity service schema."""

    data = {
        "entity_id": "climate.01_145038_02",
        **TESTS_SET_SYSTEM_MODE_FAIL[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_SYSTEM_MODE, data, schemas=SVCS_RAMSES_CLIMATE
        )
    except vol.MultipleInvalid:
        pass
    else:
        raise AssertionError("Expected vol.MultipleInvalid")


@pytest.mark.parametrize("idx", TESTS_SET_SYSTEM_MODE_FAIL2)
async def test_set_system_mode_fail2(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that valid params are acceptable to the entity service schema
    as well as to the (mocked) parsing checks in ramses_rf.gateway.Gateway.send_cmd
    Cover nested if-then-else not supported as entity-schema since HA 2025.09"""

    data = {
        "entity_id": "climate.01_145038",
        **TESTS_SET_SYSTEM_MODE_FAIL2[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_SYSTEM_MODE, data, schemas=SVCS_RAMSES_CLIMATE
        )
    except CommandInvalid:
        pass
    else:
        raise AssertionError("Expected Wrong Argument exception")


TESTS_SET_ZONE_CONFIG = {
    "00": {},
    "01": {
        "min_temp": 15,
        "max_temp": 28,
    },
    "09": {
        "min_temp": 5,
        "max_temp": 35,
        "local_override": True,
        "openwindow_function": True,
        "multiroom_mode": False,
    },
}


@pytest.mark.parametrize("idx", TESTS_SET_ZONE_CONFIG)
async def test_set_zone_config(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    data = {
        "entity_id": "climate.01_145038_02",
        **TESTS_SET_ZONE_CONFIG[idx],
    }

    await _test_entity_service_call(
        hass, SVC_SET_ZONE_CONFIG, data, schemas=SVCS_RAMSES_CLIMATE
    )


TESTS_SET_ZONE_MODE_GOOD: dict[str, dict[str, Any]] = {
    # "11": {"mode": "follow_schedule"},
    # "21": {"mode": "permanent_override", "setpoint": 12.1},
    # "31": {"mode": "advanced_override", "setpoint": 13.1},
    "41": {"mode": "temporary_override", "setpoint": 14.1},  # default duration 1 hour
    "52": {"mode": "temporary_override", "setpoint": 15.1, "duration": {"hours": 5}},
    "62": {"mode": "temporary_override", "setpoint": 16.1, "until": _UNTIL},
    # should fail
    "79": {
        "mode": "temporary_override",
        "setpoint": 16.9,
        "duration": {"hours": 5},
        "until": _UNTIL,
    },
}
TESTS_SET_ZONE_MODE_FAIL: dict[str, dict[str, Any]] = {
    "00": {},  # #                                                     missing mode
    "29": {"setpoint": 12.9},  # #                                     missing mode
    "59": {"setpoint": 15.9, "duration": {"hours": 5}},  # #           missing mode
    "69": {"setpoint": 16.9, "until": _UNTIL},  # #                    missing mode
    "70": {"other": True},  # #                                        extra
}
TESTS_SET_ZONE_MODE_FAIL2: dict[str, dict[str, Any]] = {
    "11": {"mode": "follow_schedule"},
    "21": {"mode": "permanent_override", "setpoint": 12.1},
    "31": {"mode": "advanced_override", "setpoint": 13.1},
    # TODO above 3 should pass
    "12": {"mode": "follow_schedule", "setpoint": 11.2},  # #          *extra* setpoint
    "20": {"mode": "permanent_override"},  # #                         missing setpoint
    "22": {"mode": "permanent_override", "setpoint": 12.2, "duration": {"hours": 5}},
    "23": {"mode": "permanent_override", "setpoint": 12.3, "until": _UNTIL},
    "30": {"mode": "advanced_override"},  # #                          missing setpoint
    "32": {"mode": "advanced_override", "setpoint": 13.2, "duration": {"hours": 5}},
    "33": {"mode": "advanced_override", "setpoint": 13.3, "until": _UNTIL},
    "40": {"mode": "temporary_override"},  # # missing setpoint + duration
    "50": {"mode": "temporary_override", "duration": {"hours": 5}},  # missing setpoint
    "60": {"mode": "temporary_override", "until": _UNTIL},  # #        missing setpoint
    # TODO next should fail - all TODOs to be fixed in ramses_rf
    # "79": {
    #     "mode": "temporary_override",
    #     "setpoint": 16.9,
    #     "duration": {"hours": 5},
    #     "until": _UNTIL,
    # },
}


@pytest.mark.parametrize("idx", TESTS_SET_ZONE_MODE_GOOD)
async def test_set_zone_mode_good(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that valid params are acceptable to the entity service schema."""

    data = {
        "entity_id": "climate.01_145038_02",
        **TESTS_SET_ZONE_MODE_GOOD[idx],
    }

    asserts = {"priority": Priority.HIGH}

    await _test_entity_service_call(
        hass, SVC_SET_ZONE_MODE, data, asserts, schemas=SVCS_RAMSES_CLIMATE
    )

    # # without the mock, can confirm the params are acceptable to the library
    # _ = await hass.services.async_call(
    #     DOMAIN, service=SVC_SET_ZONE_MODE, service_data=data, blocking=True
    # )


@pytest.mark.parametrize("idx", TESTS_SET_ZONE_MODE_FAIL)
async def test_set_zone_mode_fail(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that invalid params are unacceptable to the entity service schema."""

    data = {
        "entity_id": "climate.01_145038_02",
        **TESTS_SET_ZONE_MODE_FAIL[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_ZONE_MODE, data, schemas=SVCS_RAMSES_CLIMATE
        )
    except vol.MultipleInvalid:
        pass
    else:
        raise AssertionError("Expected vol.MultipleInvalid")


@pytest.mark.parametrize("idx", TESTS_SET_ZONE_MODE_FAIL2)
async def test_set_zone_mode_fail2(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that valid params are acceptable to the entity service schema."""

    data = {
        "entity_id": "climate.01_145038_02",
        **TESTS_SET_ZONE_MODE_FAIL2[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_ZONE_MODE, data, schemas=SVCS_RAMSES_CLIMATE
        )
    except CommandInvalid:
        pass
    else:
        raise AssertionError("Expected Wrong Argument exception")


async def test_set_zone_schedule(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {
        "entity_id": "climate.01_145038_02",
        "schedule": "",
    }

    await _test_entity_service_call(
        hass, SVC_SET_ZONE_SCHEDULE, data, schemas=SVCS_RAMSES_CLIMATE
    )


async def test_svc_bind_device(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the service call."""

    data = {
        "device_id": "22:140285",
        "offer": {"30C9": "00"},
    }
    schemas = {SVC_BIND_DEVICE: SCH_BIND_DEVICE}

    await _test_service_call(hass, SVC_BIND_DEVICE, data, schemas=schemas)


async def test_svc_force_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the service call."""

    data: dict[str, Any] = {}
    schemas = {SVC_FORCE_UPDATE: SCH_NO_SVC_PARAMS}

    await _test_service_call(hass, SVC_FORCE_UPDATE, data, schemas=schemas)


async def test_svc_send_packet(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Test the service call."""

    data = {
        "device_id": "18:000730",
        "verb": " I",
        "code": "1FC9",
        "payload": "00",
    }
    schemas = {SVC_SEND_PACKET: SCH_SEND_PACKET}

    await _test_service_call(hass, SVC_SEND_PACKET, data, schemas=schemas)


async def test_svc_send_packet_with_impersonation(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Test the service call."""

    data = {
        "device_id": "37:123456",
        "from_id": "40:123456",
        "verb": " I",
        "code": "22F1",
        "payload": "000304",
    }
    schemas = {SVC_SEND_PACKET: SCH_SEND_PACKET}

    await _test_service_call(hass, SVC_SEND_PACKET, data, schemas=schemas)

"""Test the services of ramses_cc."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime as dt, timedelta as td
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
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
from custom_components.ramses_cc.climate import RamsesController, RamsesZone
from custom_components.ramses_cc.const import SystemMode, ZoneMode
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
    SVCS_RAMSES_CLIMATE,
    SVCS_RAMSES_REMOTE,
    SVCS_RAMSES_SENSOR,
    SVCS_RAMSES_WATER_HEATER,
)
from ramses_rf.gateway import Gateway

from ..virtual_rf import VirtualRf
from .helpers import TEST_DIR, cast_packets_to_rf

# patched constants
_CALL_LATER_DELAY: Final = 0  # from: custom_components.ramses_cc.broker.py


NUM_DEVS_BEFORE = 3  # HGI, faked THM, faked REM
NUM_DEVS_AFTER = 15  # proxy for success of cast_packets_to_rf()
NUM_SVCS_AFTER = 36  # proxy for success, platform services included since 0.51.8
NUM_ENTS_AFTER = 47  # proxy for success
NUM_ENTS_AFTER_ALT = (
    NUM_ENTS_AFTER - 9
)  # adjust number to subtract when adding sensors in sensors.py

# format for datetime asserts, returns as: {'until': datetime.datetime(2025, 8, 11, 22, 11, 14, 774707)}
# we must round down to prev full hour to allow pytest server run time
# this could still fail 1 sec after whole hour, so allow +/- 1 minute on test outcomes
# no problem if datetime is in the past, as it is not verified anywhere

# until an hour from "now",  min. 1, max. 24:
_ASS_UNTIL = dt.now().replace(microsecond=0) + td(hours=1)
_ASS_UNTIL_3DAYS = dt.now().replace(minute=0, second=0, microsecond=0) + td(days=3)
_ASS_UNTIL_MIDNIGHT = dt.now().replace(hour=0, minute=0, second=0, microsecond=0) + td(
    days=1
)
_ASS_UNTIL_10D = dt.now().replace(minute=0, second=0, microsecond=0) + td(
    days=10, hours=4
)  # min. 1, max. 24

# same item in service call entry format, calculated from their assert expected form above:
_UNTIL = _ASS_UNTIL.strftime(
    "%Y-%m-%d %H:%M:%S"  # until an hour from now, formatted "2024-03-16 14:00:00", no msec
)
# _UNTIL_MIDNIGHT = _ASS_UNTIL_MIDNIGHT.strftime("%Y-%m-%d %H:%M:%S")
# _UNTIL10D = _ASS_UNTIL_10D.strftime("%Y-%m-%d %H:%M:%S")

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
        # validates extra schema in Ramses_cc ramses_rf built-in validation, by mocking
        "ramses_tx.command.Command.set_dhw_mode",  # small timing offset would often make tests fail, hence approx
        # to catch nested entry schema, uses dedicated asserts than other services
        # because values are normalised in the process
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
        # validates extra schema in Ramses_cc ramses_rf built-in validation, by mocking
        "ramses_tx.command.Command.set_system_mode",  # small timing offset would often make tests fail, hence approx
        # to catch nested entry schema, uses dedicated asserts than other services because values are normalised
        SCH_SET_SYSTEM_MODE,
    ),
    SVC_SET_ZONE_CONFIG: (
        "custom_components.ramses_cc.climate.RamsesZone.async_set_zone_config",
        SCH_SET_ZONE_CONFIG,
    ),
    SVC_SET_ZONE_MODE: (
        # validates extra schema in Ramses_cc ramses_rf built-in validation, by mocking
        "ramses_tx.command.Command.set_zone_mode",  # small timing offset would often make tests fail, hence approx
        # to catch nested entry schema, uses dedicated asserts than other services because values are normalised
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
    # 2025.10.0: some services registered earlier during async_setup, not in platform


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
        assert (
            len(broker._entities) == NUM_ENTS_AFTER_ALT  # _setup_via_entry_
        )  # adjust when adding sensors etc

    return entry


@pytest.fixture()  # need hass fixture to ensure hass/rf use same event loop
async def entry(hass: HomeAssistant) -> AsyncGenerator[ConfigEntry]:
    """Set up the test bed."""

    # Utilize a virtual evofw3-compatible gateway
    rf = VirtualRf(2)
    rf.set_gateway(rf.ports[0], "18:006402")

    with patch(
        "custom_components.ramses_cc.services._CALL_LATER_DELAY", _CALL_LATER_DELAY
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
            # the set_x_mode tests compare the kwargs arriving after they were normalised
            # these test involve datetime comparison, and must be approximated to be reliable
            # simple/unreliable: assert mock_method.call_args.kwargs == asserts
            assert mock_method.call_args.kwargs == pytest.approx(asserts, abs=0.1)


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
    "11": {"mode": "follow_schedule"},
    "21": {
        "mode": "permanent_override",
        "active": True,
    },
    "31": {
        "mode": "advanced_override",
        "active": True,
    },
    # # small timing offset would often make these tests fail, hence approx
    # "41": {"mode": "temporary_override", "active": True},  # default duration 1h
    # "52": {
    #     "mode": "temporary_override",
    #     "active": True,
    #     "duration": {"hours": 4},
    # },  # = end of today
    "62": {
        "mode": "temporary_override",
        "active": True,
        "until": _UNTIL,
    },  # time rounded no msecs
}  # requires custom asserts, returned from mock method success
# with ramses_tx.command.Command.set_dhw_mode as the mock method
TESTS_SET_DHW_MODE_GOOD_ASSERTS: dict[str, dict[str, Any]] = {
    "11": {
        "mode": "follow_schedule",
        "active": None,
        "until": None,
    },
    "21": {
        "mode": "permanent_override",
        "active": True,
        "until": None,
    },
    "31": {
        "mode": "advanced_override",
        "active": True,
        "until": None,
    },
    "41": {
        "mode": "temporary_override",
        "active": True,
        "until": _ASS_UNTIL,
    },
    "52": {
        "mode": "temporary_override",
        "active": True,
        "until": dt.now().replace(minute=0, second=0, microsecond=0) + td(hours=4),
    },
    "62": {
        "mode": "temporary_override",
        "active": True,
        "until": _ASS_UNTIL,
    },
}
TESTS_SET_DHW_MODE_FAIL: dict[str, dict[str, Any]] = {
    "00": {},  # #                                                     missing mode
    "29": {"active": True},  # #                                       missing mode
    "59": {"active": True, "duration": {"hours": 5}},  # #             missing mode
    "69": {"active": True, "until": _UNTIL},  # #                      missing mode
}
TESTS_SET_DHW_MODE_FAIL2: dict[str, dict[str, Any]] = {
    "12": {"mode": "follow_schedule", "active": True},  # #            *extra* active
    "20": {"mode": "permanent_override"},  # #                         missing active
    "22": {"mode": "permanent_override", "active": True, "duration": {"hours": 5}},
    "23": {"mode": "permanent_override", "active": True, "until": _UNTIL},
    "30": {"mode": "advanced_override"},  # #                          missing active
    "32": {"mode": "advanced_override", "active": True, "duration": {"hours": 5}},
    "33": {"mode": "advanced_override", "active": True, "until": _UNTIL},
    "40": {"mode": "temporary_override"},  # #                         missing active
    "42": {"mode": "temporary_override", "active": False},  # #        missing duration
    "50": {"mode": "temporary_override", "duration": {"hours": 5}},  # missing active
    "60": {"mode": "temporary_override", "until": _UNTIL},  # #        missing active
    "79": {
        "mode": "temporary_override",
        "active": True,
        "duration": {"hours": 5},
        "until": _UNTIL,
    },
}


# TODO: extended test of underlying method (duration/until)
@pytest.mark.parametrize("idx", TESTS_SET_DHW_MODE_GOOD)
async def test_set_dhw_mode_good(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that valid params are acceptable to the entity service schema in HA +
    to the (mocked) parsing checks in ramses_rf.gateway.Gateway.send_cmd
    Replaces nested if-then-else not supported as entity-schema since HA 2025.09"""

    data = {
        "entity_id": "water_heater.01_145038_hw",
        **TESTS_SET_DHW_MODE_GOOD[idx],  # type: ignore[dict-item]
    }

    await _test_entity_service_call(
        hass,
        SVC_SET_DHW_MODE,
        data,
        TESTS_SET_DHW_MODE_GOOD_ASSERTS[idx],
        schemas=SVCS_RAMSES_WATER_HEATER,
    )

    # # without the mock, can confirm the params are acceptable to the library
    # _ = await hass.services.async_call(
    #     DOMAIN, service=SVC_SET_DHW_MODE, service_data=data, blocking=True
    # )


@pytest.mark.parametrize("idx", TESTS_SET_DHW_MODE_FAIL)
async def test_set_dhw_mode_fail(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """
    Confirm that invalid params are rejected by the entity service schema + water_heater checks.
    """

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
    """Confirm that invalid params are rejected by the entity service schema."""

    data = {
        "entity_id": "water_heater.01_145038_hw",
        **TESTS_SET_DHW_MODE_FAIL2[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_DHW_MODE, data, schemas=SVCS_RAMSES_WATER_HEATER
        )
    except vol.MultipleInvalid:
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
    # Note for all 4 tests: the mock method does not report receiving 'mode'
    "00": {"mode": "auto"},
    "01": {"mode": "eco_boost"},
    # TODO small timing offset makes the next test often fail locally and on GitHub, round times in Command?
    # "02": {"mode": "day_off", "period": {"days": 3}},
    # "03": {"mode": "eco_boost", "duration": {"hours": 3}},
}  # requires custom asserts, returned from mock method success
# with mock method ramses_tx.command.Command.set_system_mode
TESTS_SET_SYSTEM_MODE_GOOD_ASSERTS: dict[str, dict[str, Any]] = {
    # mode not received by mock method, but on the way validation filter is applied without errors
    "00": {"until": None},  # "mode": "auto" not passed to mock
    "01": {"until": None},  # "mode": "eco_boost" not passed to mock
    "02": {
        # "mode": "day_off",
        "until": _ASS_UNTIL_3DAYS,
    },  # must adjust for pytest run time
    "03": {
        # "mode": "eco_boost",
        "until": dt.now().replace(minute=0, second=0, microsecond=0) + td(minutes=180),
    },
}

TESTS_SET_SYSTEM_MODE_FAIL: dict[str, dict[str, Any]] = {
    "04": {},  # flagged!
}  # no asserts required, caught in entity_schema

TESTS_SET_SYSTEM_MODE_FAIL2: dict[str, dict[str, Any]] = {
    "05": {
        "mode": "day_off",
        "period": {"days": 3},  # both duration and period
        "duration": {"hours": 3, "minutes": 30},
    },
}  # no asserts required, caught in checked_entry validation


# TODO: extended test of underlying method (duration/period)
@pytest.mark.parametrize("idx", TESTS_SET_SYSTEM_MODE_GOOD)
async def test_set_system_mode_good(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that valid params are acceptable to the entity service schema."""

    data = {
        "entity_id": "climate.01_145038",
        **TESTS_SET_SYSTEM_MODE_GOOD[idx],
    }

    # Patch async_send_cmd to prevent actual network traffic/protocol errors
    # while still allowing the Command creation (which is what we assert on) to happen.
    with patch("ramses_rf.gateway.Gateway.async_send_cmd", new_callable=AsyncMock):
        await _test_entity_service_call(
            hass,
            SVC_SET_SYSTEM_MODE,
            data,
            TESTS_SET_SYSTEM_MODE_GOOD_ASSERTS[idx],
            schemas=SVCS_RAMSES_CLIMATE,
        )


@pytest.mark.parametrize("idx", TESTS_SET_SYSTEM_MODE_FAIL)
async def test_set_system_mode_fail(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that invalid params are rejected by the entity service schema."""

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
    """Confirm that valid params are acceptable to the entity service schema in HA +
    to the (mocked) parsing checks in ramses_rf.gateway.Gateway.send_cmd
    Replaces nested if-then-else not supported as entity-schema since HA 2025.09"""

    data = {
        "entity_id": "climate.01_145038",
        **TESTS_SET_SYSTEM_MODE_FAIL2[idx],
    }

    try:
        await _test_entity_service_call(
            hass, SVC_SET_SYSTEM_MODE, data, schemas=SVCS_RAMSES_CLIMATE
        )
    except vol.MultipleInvalid:
        pass
    else:
        raise AssertionError("Expected Wrong Argument exception")


TESTS_SET_ZONE_CONFIG = {
    "00": {},
    "01": {
        "min_temp": 15,
        "max_temp": 31,
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
    "11": {"mode": "follow_schedule"},
    "21": {
        "mode": "permanent_override",
        "setpoint": 12.1,
    },
    "31": {
        "mode": "advanced_override",
        "setpoint": 13.1,
    },
    # TODO small timing offset makes the next 2 test often fail locally and on GitHub
    # "41": {"mode": "temporary_override", "setpoint": 14.1},  # default duration 1 hour will be added
    # "52": {"mode": "temporary_override", "setpoint": 15.1, "duration": {"hours": 3}},
    "62": {
        "mode": "temporary_override",
        "setpoint": 16.1,
        "until": _UNTIL,
    },  # time rounded, no msec
    # next tests are from issue #276, simulating normalised inputs
    "276": {"mode": "permanent_override", "setpoint": 25},
    "277": {"mode": "temporary_override", "setpoint": 19, "until": _UNTIL},
}  # requires custom asserts, returned from mock method success
# with mock method ramses_tx.command.Command.set_zone_mode
TESTS_SET_ZONE_MODE_GOOD_ASSERTS: dict[str, dict[str, Any]] = {
    "11": {"mode": "follow_schedule", "setpoint": None, "until": None},
    "21": {"mode": "permanent_override", "setpoint": 12.1, "until": None},
    "31": {"mode": "advanced_override", "setpoint": 13.1, "until": None},
    "41": {
        "mode": "temporary_override",
        "setpoint": 14.1,
        "until": _ASS_UNTIL,
    },
    "52": {
        "mode": "temporary_override",
        "setpoint": 15.1,
        "until": dt.now().replace(minute=0, second=0, microsecond=0) + td(hours=3),
    },
    "62": {"mode": "temporary_override", "setpoint": 16.1, "until": _ASS_UNTIL},
    "276": {"mode": "permanent_override", "setpoint": 25, "until": None},
    "277": {"mode": "temporary_override", "setpoint": 19, "until": _ASS_UNTIL},
}

TESTS_SET_ZONE_MODE_FAIL: dict[str, dict[str, Any]] = {
    "00": {},  # #                                                     missing mode
    "29": {"setpoint": 12.9},  # #                                     missing mode
    "59": {"setpoint": 15.9, "duration": {"hours": 5}},  # #           missing mode
    "69": {"setpoint": 16.9, "until": _UNTIL},  # #                    missing mode
    "70": {"other": True},  # #                                        extra
}
TESTS_SET_ZONE_MODE_FAIL2: dict[str, dict[str, Any]] = {
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
    "79": {
        "mode": "temporary_override",
        "setpoint": 16.9,
        "duration": {"hours": 5},
        "until": _UNTIL,
    },
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

    # Patch async_send_cmd to prevent actual network traffic/protocol errors
    # while still allowing the Command creation (which is what we assert on) to happen.
    with patch("ramses_rf.gateway.Gateway.async_send_cmd", new_callable=AsyncMock):
        await _test_entity_service_call(
            hass,
            SVC_SET_ZONE_MODE,
            data,
            TESTS_SET_ZONE_MODE_GOOD_ASSERTS[idx],
            schemas=SVCS_RAMSES_CLIMATE,
        )

    # # without the mock, can confirm the params are acceptable to the library
    # _ = await hass.services.async_call(
    #     DOMAIN, service=SVC_SET_ZONE_MODE, service_data=data, blocking=True
    # )


@pytest.mark.parametrize("idx", TESTS_SET_ZONE_MODE_FAIL)
async def test_set_zone_mode_fail(
    hass: HomeAssistant, entry: ConfigEntry, idx: str
) -> None:
    """Confirm that invalid params are rejected by the entity service schema."""

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
    except vol.MultipleInvalid:
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


# TODO add tests for core climate services that ramses_cc intercepts/handles

# async def test_set_temperature(hass: HomeAssistant, entry: ConfigEntry) -> None:
#     """
#     Test standard HA action, picked up by ramses_cc and sent to set_zone_mode().
#     No schema (entry handled by HA).
#     See issue #276
#
#     :param hass: the HA instance
#     :param entry: the climate entity object to configure
#     """
#     data = {
#         "entity_id": "climate.01_145038_02",
#         "temperature": 25,
#     }
#
#     # how to address the hass core CLIMATE domain, not ramses_cc
#     hass.async_create_task(
#         hass.services.async_call(
#             'climate', 'async_set_temperature', {"temperature": 25}
#         )
#     )


########################################################################################
# New tests for the climate async migration


@pytest.fixture
def mock_broker() -> MagicMock:
    """Mock the RamsesBroker."""
    return MagicMock()


@pytest.fixture
def mock_evohome() -> MagicMock:
    """Mock the Evohome device."""
    device = MagicMock()
    device.id = "01:123456"
    device.set_mode = AsyncMock()
    device.reset_mode = AsyncMock()
    # Mocks for properties used in __init__ or properties
    device.zones = []
    device.system_mode = {
        "system_mode": SystemMode.AUTO,
    }
    return device


@pytest.fixture
def mock_zone() -> MagicMock:
    """Mock the Zone device."""
    device = MagicMock()
    device.id = "04:123456"
    device.set_mode = AsyncMock()
    device.reset_mode = AsyncMock()
    device.set_config = AsyncMock()
    device.reset_config = AsyncMock()
    # Mocks for properties
    device.temperature = 20.0
    device.setpoint = 21.0
    device.params = {}
    device.idx = "01"
    device.heating_type = "radiator"
    device.mode = {"mode": ZoneMode.SCHEDULE, "setpoint": 21.0}
    device.config = {"min_temp": 5.0, "max_temp": 35.0}
    device.schedule = []
    device.schedule_version = 1
    # Linked TCS
    tcs = MagicMock()
    tcs.system_mode = {"system_mode": SystemMode.AUTO}
    device.tcs = tcs
    return device


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_controller_async_set_hvac_mode(
    mock_init: MagicMock, mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test RamsesController.async_set_hvac_mode awaits set_mode."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())
    entity._device = mock_evohome
    entity.async_write_ha_state_delayed = MagicMock()

    # Test Valid Mode
    await entity.async_set_hvac_mode(HVACMode.OFF)
    mock_evohome.set_mode.assert_awaited_once_with(SystemMode.HEAT_OFF, until=None)


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_controller_async_set_preset_mode(
    mock_init: MagicMock, mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test RamsesController.async_set_preset_mode awaits set_mode."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())
    entity._device = mock_evohome
    entity.async_write_ha_state_delayed = MagicMock()

    # Test Valid Preset
    await entity.async_set_preset_mode("away")
    mock_evohome.set_mode.assert_awaited_once_with(SystemMode.AWAY, until=None)


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_controller_validation_error(
    mock_init: MagicMock, mock_broker: MagicMock, mock_evohome: MagicMock
) -> None:
    """Test validation errors raise ServiceValidationError."""
    entity = RamsesController(mock_broker, mock_evohome, MagicMock())
    entity._device = mock_evohome

    # Mock set_mode to raise Voluptuous error
    mock_evohome.set_mode.side_effect = vol.Invalid("Invalid mode")

    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_zone_async_set_hvac_mode(
    mock_init: MagicMock, mock_broker: MagicMock, mock_zone: MagicMock
) -> None:
    """Test RamsesZone.async_set_hvac_mode awaits helpers."""
    entity = RamsesZone(mock_broker, mock_zone, MagicMock())
    entity._device = mock_zone
    entity.async_write_ha_state_delayed = MagicMock()

    # Test Auto (Reset Mode)
    await entity.async_set_hvac_mode(HVACMode.AUTO)
    mock_zone.reset_mode.assert_awaited_once()

    # Test Heat (Set Permanent)
    mock_zone.reset_mode.reset_mock()
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    mock_zone.set_mode.assert_awaited_once_with(
        mode=ZoneMode.PERMANENT, setpoint=25, until=None
    )


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_zone_async_set_temperature(
    mock_init: MagicMock, mock_broker: MagicMock, mock_zone: MagicMock
) -> None:
    """Test RamsesZone.async_set_temperature awaits set_mode."""
    entity = RamsesZone(mock_broker, mock_zone, MagicMock())
    entity._device = mock_zone
    entity.async_write_ha_state_delayed = MagicMock()

    await entity.async_set_temperature(temperature=22.5)
    mock_zone.set_mode.assert_awaited()
    # Verify kwargs were passed correctly (mode=ADVANCED inferred from args)
    _, kwargs = mock_zone.set_mode.call_args
    assert kwargs["setpoint"] == 22.5


@patch("custom_components.ramses_cc.climate.RamsesEntity.__init__", return_value=None)
async def test_zone_helpers_are_async(
    mock_init: MagicMock, mock_broker: MagicMock, mock_zone: MagicMock
) -> None:
    """Verify helpers are awaitable."""
    entity = RamsesZone(mock_broker, mock_zone, MagicMock())
    entity._device = mock_zone
    entity.async_write_ha_state_delayed = MagicMock()

    await entity.async_reset_zone_config()
    mock_zone.reset_config.assert_awaited_once()

    await entity.async_set_zone_config(min_temp=10)
    mock_zone.set_config.assert_awaited_once_with(min_temp=10)

"""Test the compatibility of the interface (/api/states) with EvoControl.

See: https://www.amazon.co.uk/dp/B0BL1CN6WS

The intention here is to confirm the namespace remains consistent, so that the
interface with EvoControl is not broken from one version of this integration to
the next.

The test will check schema JSON, entity_id, attributes (attr_id and values).

Note that EvoControl uses the /api/states endpoint to get its data (and that is
tested only indirectly here).

This does not test any service calls, or any other endpoints.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.climate import PRESET_ECO, ClimateEntity, HVACMode
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.water_heater import WaterHeaterEntity
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.binary_sensor import BINARY_SENSOR_DESCRIPTIONS
from custom_components.ramses_cc.climate import CLIMATE_DESCRIPTIONS
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from custom_components.ramses_cc.sensor import SENSOR_DESCRIPTIONS
from custom_components.ramses_cc.water_heater import WATER_HEATER_DESCRIPTIONS
from ramses_rf.gateway import Gateway
from ramses_rf.system import Evohome

from .helpers import TEST_DIR

INPUT_FILE = "/system_1.log"
SCHEMA_FILE = "/system_1.json"


class MockRamsesCoordinator:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass


async def instantiate_entities(
    hass: HomeAssistant,
) -> tuple[
    list[ClimateEntity],
    list[WaterHeaterEntity],
    list[BinarySensorEntity],
    list[SensorEntity],
]:
    with open(f"{TEST_DIR}/{INPUT_FILE}") as f:
        gwy: Gateway = Gateway(
            port_name=None, input_file=f, config={"disable_discovery": True}
        )
        await gwy.start()
        await gwy.stop()  # have to stop MessageIndex thread, aka: gwy.msg_db.stop()

    coordinator: RamsesCoordinator = MockRamsesCoordinator(hass)

    # climate entities (TCS, zones)
    rf_climates = [s for s in gwy.systems if isinstance(s, Evohome)]
    rf_climates += [z for s in gwy.systems for z in s.zones if isinstance(s, Evohome)]

    climates: list[ClimateEntity] = [
        description.ramses_cc_class(coordinator, device, description)
        for device in rf_climates
        for description in CLIMATE_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
    ]

    # water_heater entities (DHW)
    rf_heaters = [s.dhw for s in gwy.systems if s.dhw if isinstance(s, Evohome)]

    water_heaters: list[WaterHeaterEntity] = [
        description.ramses_cc_class(coordinator, device, description)
        for device in rf_heaters
        for description in WATER_HEATER_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
    ]

    # binary_sensors & sensors entities
    rf_devices = gwy.devices + rf_climates + rf_heaters

    binary_sensors: list[BinarySensorEntity] = [
        description.ramses_cc_class(coordinator, device, description)
        for device in rf_devices
        for description in BINARY_SENSOR_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
        and hasattr(device, description.ramses_rf_attr)
    ]

    sensors: list[SensorEntity] = [
        description.ramses_cc_class(coordinator, device, description)
        for device in rf_devices
        for description in SENSOR_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
        and hasattr(device, description.ramses_rf_attr)
    ]

    return climates, water_heaters, binary_sensors, sensors


async def test_namespace(hass: HomeAssistant) -> None:
    """Test the namespace of entities/attrs used by EvoControl."""

    with open(f"{TEST_DIR}/{SCHEMA_FILE}") as f:
        _SCHEMA: dict[str, dict[str, Any]] = json.load(f)
        CTL_ID = list(_SCHEMA.keys())[0]  # ctl_id via a webform, from the user
        SCHEMA = list(_SCHEMA.values())[0]

    # The intention here is check the namespace used by EvoControl
    climates, water_heaters, binary_sensors, sensors = await instantiate_entities(hass)

    #
    # evo_control uses: binary_sensor.${cid}_status
    id = f"binary_sensor.{CTL_ID}_status"

    binary: BinarySensorEntity = [e for e in binary_sensors if e.entity_id == id][0]
    assert binary.unique_id == f"{CTL_ID}-status"
    assert binary.state == STATE_OFF

    #
    # evo_control uses: the working_schema
    schema = binary.extra_state_attributes["working_schema"]
    assert schema["stored_hotwater"] == SCHEMA["stored_hotwater"]
    assert schema["zones"] == SCHEMA["zones"]

    #
    # evo_control uses: binary_sensor.${i}_battery_low
    for dev_id in (
        "04:056053",
        "07:046947",
        "22:140285",
        "34:092243",
    ):  # via walking the schema
        id = f"binary_sensor.{dev_id}_battery_low"

        binary = [e for e in binary_sensors if e.entity_id == id][0]
        assert binary.unique_id == f"{dev_id}-battery_low"
        assert binary.state in (STATE_ON, STATE_OFF, None)

        battery_level = binary.extra_state_attributes["battery_level"]
        assert (
            battery_level is None or 0.0 < battery_level < 1.0
        )  # TODO: check values, not types

    #
    # evo_control uses: binary_sensor.${cid}_${haZid}_window_open
    for zon_idx in ("02", "0A"):  # via walking the schema
        id = f"binary_sensor.{CTL_ID}_{zon_idx}_window_open"

        binary = [e for e in binary_sensors if e.entity_id == id][0]
        assert binary.unique_id == f"{CTL_ID}_{zon_idx}-window_open"
        assert binary.state in (STATE_ON, STATE_OFF, None)

    #
    # evo_control uses: sensor.${cid}_heat_demand
    id = f"sensor.{CTL_ID}_heat_demand"

    sensor: SensorEntity = [e for e in sensors if e.entity_id == id][0]
    assert sensor.unique_id == f"{CTL_ID}-heat_demand"
    assert sensor.state == 72.0

    #
    # evo_control uses: sensor.${dhwRelayId}_relay_demand
    dhw_id = SCHEMA["stored_hotwater"]["hotwater_valve"]  # via walking the schema
    id = f"binary_sensor.{dhw_id}_active"

    binary = [e for e in binary_sensors if e.entity_id == id][0]
    assert binary.unique_id == f"{dhw_id}-active"
    assert binary.state in (STATE_ON, STATE_OFF, None)

    id = f"sensor.{dhw_id}_relay_demand"

    sensor = [e for e in sensors if e.entity_id == id][0]
    assert sensor.unique_id == f"{dhw_id}-relay_demand"
    assert sensor.state is None or 0.0 <= sensor.state <= 100.0

    #
    # evo_control uses: sensor.${cid}_${haZid}_heat_demand
    for zon_idx in ("02", "0A", "HW"):  # via walking the schema
        id = f"sensor.{CTL_ID}_{zon_idx}_heat_demand"

        sensor = [e for e in sensors if e.entity_id == id][0]
        assert sensor.unique_id == f"{CTL_ID}_{zon_idx}-heat_demand"
        assert sensor.state is None or 0.0 <= sensor.state <= 100.0

    #
    # evo_control uses: climate.${cid}
    id = f"climate.{CTL_ID}"  # ctl_id via a webform, from the user

    climate: ClimateEntity = [e for e in climates if e.entity_id == id][0]
    assert climate.unique_id == CTL_ID
    # assert climate.name == f"Controller {CTL_ID}"  # TODO

    assert climate.state == HVACMode.HEAT
    assert climate.preset_mode == PRESET_ECO
    assert climate.extra_state_attributes["system_mode"] == {
        "system_mode": "eco_boost",
        "until": "2022-03-06T14:44:00",
    }

    #
    # evo_control uses: climate.${cid}_${haZid}
    for zon_idx in ("02", "0A"):  # via walking the schema
        id = f"climate.{CTL_ID}_{zon_idx}"

        climate = [e for e in climates if e.entity_id == id][0]
        assert climate.unique_id == f"{CTL_ID}_{zon_idx}"
        # assert climate.name == SCHEMA["zones"][zon_idx]["_name"]  # TODO

        if zon_idx == "02":
            assert climate.extra_state_attributes["mode"] == {
                "mode": "permanent_override",
                "setpoint": 5.0,
            }
            assert (
                climate.current_temperature == 18.16
            )  # equivalent to {"temperatureStatus": isAvailable: true, temperature: 18.16}

        else:
            assert climate.extra_state_attributes["mode"] == {
                "mode": "temporary_override",
                "setpoint": 20.0,
                "until": "2022-01-22T10:00:00",
            }
            assert (
                climate.current_temperature is None
            )  # equivalent to {"temperatureStatus": isAvailable: false}

    #
    # evo_control uses: water_heater.${cid}_hw
    id = f"water_heater.{CTL_ID}_HW"  # ctl_id via a webform, from the user

    heater: WaterHeaterEntity = [e for e in water_heaters if e.entity_id == id][0]
    assert heater.unique_id == f"{CTL_ID}_HW"
    # assert heater.name == f"{CTL_ID} XXX"  # TODO set name

    assert heater.extra_state_attributes["mode"] == {
        "mode": "temporary_override",
        "active": True,
        "until": "2022-02-10T22:00:00",
    }
    assert heater.current_temperature == 61.87

    assert True

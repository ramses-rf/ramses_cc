"""Test compatibility of the interface (/api/states) with EvoControl.

See: https://www.amazon.co.uk/dp/B0BL1CN6WS

The intention here is to confirm the namespace remains consistent,
so that the interface with EvoControl is not broken from one version
of this integration to the next.

The test will check schema JSON, entity_id, attributes (attr_id and
values).

Note that EvoControl uses the /api/states endpoint to get its data
(and that is tested only indirectly here).

This does not test any service calls, or any other endpoints.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, cast

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import PRESET_ECO, HVACMode
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.water_heater import WaterHeaterEntity
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.binary_sensor import BINARY_SENSOR_DESCRIPTIONS
from custom_components.ramses_cc.climate import CLIMATE_DESCRIPTIONS
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from custom_components.ramses_cc.helpers import as_iso
from custom_components.ramses_cc.sensor import SENSOR_DESCRIPTIONS
from custom_components.ramses_cc.water_heater import WATER_HEATER_DESCRIPTIONS
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.systems import Evohome
from ramses_tx.config import EngineConfig

from .helpers import TEST_DIR

INPUT_FILE = "/system_1.log"
SCHEMA_FILE = "/system_1.json"


async def async_flush_queues(gwy: Any) -> None:
    """Deterministically drain specific backend CQRS queues.

    Hardcoded references are used to avoid introspection side-effects
    (e.g., prematurely joining transport queues causing test teardown
    drops and lost connections).
    """
    queues: list[asyncio.Queue[Any]] = []

    # 1. Legacy / Top-level Gateway Queues
    if hasattr(gwy, "msg_queue") and isinstance(gwy.msg_queue, asyncio.Queue):
        queues.append(gwy.msg_queue)

    # 2. Engine Layer Queues
    engine = getattr(gwy, "_engine", None)
    if engine and hasattr(engine, "_msg_queue"):
        if isinstance(engine._msg_queue, asyncio.Queue):
            queues.append(engine._msg_queue)

    # 3. Phase 2.95+ Central Dispatcher Queues
    dispatcher = getattr(gwy, "dispatcher", None) or getattr(
        gwy, "central_dispatcher", None
    )
    if dispatcher:
        for q_name in (
            "_in_queue",
            "ssot_queue",
            "discovery_queue",
            "binding_queue",
            "faked_queue",
        ):
            if hasattr(dispatcher, q_name):
                q = getattr(dispatcher, q_name)
                if isinstance(q, asyncio.Queue):
                    queues.append(q)

    # Await specifically targeted queues
    for q in queues:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(q.join(), timeout=5.0)

    # Ensure the event loop has ticked enough to process immediate task
    # results from synchronous TopologyBuilder iterations.
    for _ in range(50):
        await asyncio.sleep(0)


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
    # 1. Provide the string path instead of an open file object
    log_path = f"{TEST_DIR}/{INPUT_FILE}"

    # Transport layer parameters go into EngineConfig
    engine_config = EngineConfig(
        disable_qos=True,
        input_file=log_path,
    )

    # Application layer parameters go into GatewayConfig
    gwy_config = GatewayConfig(
        disable_discovery=True,
        engine=engine_config,
    )

    # Explicitly set port_name to None to bypass hardware serial
    # initialisation
    gwy: Gateway = Gateway(port_name=None, config=gwy_config)

    await gwy.start()

    # Deterministically flush CQRS background queues
    await async_flush_queues(gwy)

    # have to stop MessageIndex thread, aka: gwy.msg_db.stop()
    await gwy.stop()

    coordinator = cast(RamsesCoordinator, MockRamsesCoordinator(hass))

    # climate entities (TCS, zones) - UPDATED to use device_registry
    rf_climates = [s for s in gwy.device_registry.systems if isinstance(s, Evohome)]
    rf_climates += [
        z
        for s in gwy.device_registry.systems
        for z in s.zones
        if isinstance(s, Evohome)
    ]

    climates: list[ClimateEntity] = [
        description.ramses_cc_class(
            coordinator,
            cast(Any, device),
            description,
        )
        for device in rf_climates
        for description in CLIMATE_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
    ]

    # water_heater entities (DHW) - UPDATED to use device_registry
    rf_heaters = [
        s.dhw for s in gwy.device_registry.systems if s.dhw if isinstance(s, Evohome)
    ]

    water_heaters: list[WaterHeaterEntity] = [
        description.ramses_cc_class(
            coordinator,
            cast(Any, device),
            description,
        )
        for device in rf_heaters
        for description in WATER_HEATER_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
    ]

    # binary_sensors & sensors entities - UPDATED to use device_registry
    rf_devices = list(gwy.device_registry.devices) + rf_climates + rf_heaters

    binary_sensors: list[BinarySensorEntity] = [
        description.ramses_cc_class(
            coordinator,
            cast(Any, device),
            description,
        )
        for device in rf_devices
        for description in BINARY_SENSOR_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
        and hasattr(device, description.ramses_rf_attr)
    ]

    sensors: list[SensorEntity] = [
        description.ramses_cc_class(
            coordinator,
            cast(Any, device),
            description,
        )
        for device in rf_devices
        for description in SENSOR_DESCRIPTIONS
        if isinstance(device, description.ramses_rf_class)
        and hasattr(device, description.ramses_rf_attr)
    ]

    # Pre-resolve lazy async attributes to prevent test failures on
    # first access
    for entity in climates + water_heaters + binary_sensors + sensors:
        entity.entity_id = f"test.{entity.unique_id}"
        entity.hass = hass

        # Trigger lazy load
        _ = entity.state
        if hasattr(entity, "extra_state_attributes"):
            _ = entity.extra_state_attributes

    await hass.async_block_till_done()

    return climates, water_heaters, binary_sensors, sensors


async def test_namespace(hass: HomeAssistant) -> None:
    """Test the namespace of entities/attrs used by EvoControl."""

    with open(f"{TEST_DIR}/{SCHEMA_FILE}") as f:
        _SCHEMA: dict[str, dict[str, Any]] = json.load(f)
        # ctl_id via a webform, from the user
        CTL_ID = list(_SCHEMA.keys())[0]
        SCHEMA = list(_SCHEMA.values())[0]

    # The intention here is check the namespace used by EvoControl
    (
        climates,
        water_heaters,
        binary_sensors,
        sensors,
    ) = await instantiate_entities(hass)

    #
    # evo_control uses: binary_sensor.${cid}_status
    # NOTE: entity_id is None (not registered), so we lookup by unique_id
    uid = f"{CTL_ID}-status"

    binary: BinarySensorEntity = [e for e in binary_sensors if e.unique_id == uid][0]
    assert binary.unique_id == uid
    assert binary.state == STATE_OFF

    #
    # evo_control uses: the working_schema
    attrs = binary.extra_state_attributes
    assert attrs is not None
    schema = attrs["working_schema"]
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
        uid = f"{dev_id}-battery_low"

        binary = [e for e in binary_sensors if e.unique_id == uid][0]
        assert binary.unique_id == uid
        assert binary.state in (STATE_ON, STATE_OFF, None)

        attrs = binary.extra_state_attributes
        assert attrs is not None
        battery_level = attrs["battery_level"]
        if battery_level != "N/A":  # Display for None
            assert 0.0 <= battery_level <= 1.0  # TODO: check values, not types

    #
    # evo_control uses: binary_sensor.${cid}_${haZid}_window_open
    for zon_idx in ("02", "0A"):  # via walking the schema
        uid = f"{CTL_ID}_{zon_idx}-window_open"

        binary = [e for e in binary_sensors if e.unique_id == uid][0]
        assert binary.unique_id == uid
        assert binary.state in (STATE_ON, STATE_OFF, None)

    #
    # evo_control uses: sensor.${cid}_heat_demand
    uid = f"{CTL_ID}-heat_demand"

    sensor: SensorEntity = [e for e in sensors if e.unique_id == uid][0]
    assert sensor.unique_id == uid
    assert sensor.state == 72.0

    #
    # evo_control uses: sensor.${dhwRelayId}_relay_demand
    # via walking the schema
    dhw_id = SCHEMA["stored_hotwater"]["hotwater_valve"]
    uid = f"{dhw_id}-active"

    binary = [e for e in binary_sensors if e.unique_id == uid][0]
    assert binary.unique_id == uid
    assert binary.state in (STATE_ON, STATE_OFF, None)

    uid = f"{dhw_id}-relay_demand"

    sensor = [e for e in sensors if e.unique_id == uid][0]
    assert sensor.unique_id == uid
    assert sensor.state is None or 0.0 <= sensor.state <= 100.0

    #
    # evo_control uses: sensor.${cid}_${haZid}_heat_demand
    for zon_idx in ("02", "0A", "HW"):  # via walking the schema
        uid = f"{CTL_ID}_{zon_idx}-heat_demand"

        sensor = [e for e in sensors if e.unique_id == uid][0]
        assert sensor.unique_id == uid
        assert sensor.state is None or 0.0 <= sensor.state <= 100.0

    #
    # evo_control uses: climate.${cid}
    uid = CTL_ID  # ctl_id via a webform, from the user

    climate: ClimateEntity = [e for e in climates if e.unique_id == uid][0]
    assert climate.unique_id == uid
    # assert climate.name == f"Controller {CTL_ID}"  # TODO

    assert climate.state == HVACMode.HEAT
    assert climate.preset_mode == PRESET_ECO

    attrs = climate.extra_state_attributes
    assert attrs is not None
    sys_mode = attrs.get("system_mode")
    assert sys_mode["system_mode"] == "eco_boost"
    assert as_iso(sys_mode["until"]) == "2022-03-06T14:44:00"

    #
    # evo_control uses: climate.${cid}_${haZid}
    for zon_idx in ("02", "0A"):  # via walking the schema
        uid = f"{CTL_ID}_{zon_idx}"

        climate = [e for e in climates if e.unique_id == uid][0]
        assert climate.unique_id == uid
        # assert climate.name == SCHEMA["zones"][zon_idx]["_name"]
        # TODO

        attrs = climate.extra_state_attributes
        assert attrs is not None

        if zon_idx == "02":
            assert attrs.get("mode") is None
            # equivalent to {"temperatureStatus": isAvailable: true,
            # temperature: 18.16}
            assert climate.current_temperature == 18.16

        else:
            mode_attr = attrs.get("mode")
            assert mode_attr is None
            assert climate.current_temperature is None

    #
    # evo_control uses: water_heater.${cid}_hw
    uid = f"{CTL_ID}_HW"  # ctl_id via a webform, from the user

    heater: WaterHeaterEntity = [e for e in water_heaters if e.unique_id == uid][0]
    assert heater.unique_id == uid
    # assert heater.name == f"{CTL_ID} XXX"  # TODO set name

    attrs = heater.extra_state_attributes
    assert attrs is not None
    heater_mode = attrs.get("mode")
    assert heater_mode is not None
    assert heater_mode["mode"] == "temporary_override"
    assert heater.current_temperature == 63.1

    assert True

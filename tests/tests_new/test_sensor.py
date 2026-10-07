"""Tests for the ramses_cc sensor platform."""

from __future__ import annotations

import logging
from datetime import UTC, datetime as dt
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfRatio,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.const import (
    CONF_ADVANCED_FEATURES,
    CONF_LAST_MSG_SENSORS,
    DOMAIN,
    SZ_LAST_MSG,
)
from custom_components.ramses_cc.sensor import (
    SENSOR_DESCRIPTIONS,
    RamsesLastMessageSensor,
    RamsesSensor,
    RamsesSensorEntityDescription,
    VentilationDemandCapable,
    _sync_last_msg_registry_entries,
    async_setup_entry,
)
from ramses_rf.const import (
    SZ_CIRCUIT_MODE,
    SZ_COOLING_DEMAND,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_HEAT_DEMAND,
    SZ_PUMP_RELAY_STATE,
    SZ_SETPOINT,
    SZ_TEMPERATURE,
)
from ramses_rf.devices import (
    HgiGateway,
    HvacHumiditySensor,
    HvacRemote,
    HvacVentilator,
    OtbGateway,
    Thermostat,
    UfhCircuit,
    UfhController,
)
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_rf.enums import PumpRelayState, ThermalMode
from ramses_rf.exceptions import DeviceNotFaked
from ramses_tx.const import Code, Verb
from ramses_tx.dtos import CommandDTO


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Return a mock RamsesCoordinator."""
    coordinator = MagicMock()
    coordinator.hass = MagicMock()
    coordinator.async_register_platform = MagicMock()
    return coordinator


@pytest.fixture
def mock_device() -> MagicMock:
    """Return a mock RamsesRFEntity."""
    device = MagicMock(spec=RamsesRFEntity)
    device.id = "01:123456"
    return device


async def test_async_setup_entry(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test the platform setup and entity creation callback.

    :param hass: The Home Assistant instance.
    :param mock_coordinator: The mock coordinator fixture.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.runtime_data = mock_coordinator
    async_add_entities = MagicMock()

    # Mock async_get_current_platform to avoid RuntimeError
    with patch(
        "custom_components.ramses_cc.sensor.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    # Verify platform registration
    mock_coordinator.async_register_platform.assert_called_once()
    callback_func = mock_coordinator.async_register_platform.call_args[0][1]

    # Dynamically find the SZ_TEMPERATURE description intended for HvacHumiditySensor.
    # This prevents the test from breaking if the SENSOR_DESCRIPTIONS array order changes.
    target_desc = next(
        desc
        for desc in SENSOR_DESCRIPTIONS
        if desc.key == SZ_TEMPERATURE
        and (
            desc.ramses_rf_class is HvacHumiditySensor
            or HvacHumiditySensor in get_args(desc.ramses_rf_class)
        )
    )

    with patch(
        "custom_components.ramses_cc.sensor.SENSOR_DESCRIPTIONS",
        (target_desc,),
    ):
        # device 1: Matches the class and has the attribute
        dev_match = MagicMock(spec=HvacHumiditySensor)
        dev_match.id = "01:111111"
        setattr(dev_match, target_desc.ramses_rf_attr, 21.5)

        # device 2: Matches class but MISSING attribute
        dev_no_attr = MagicMock(spec=HvacHumiditySensor)
        dev_no_attr.id = "01:222222"
        # Since MagicMock(spec=...) automatically adds spec attributes,
        # we delete it
        delattr(dev_no_attr, target_desc.ramses_rf_attr)

        # device 3: Does NOT match class
        dev_wrong_class = MagicMock(spec=Thermostat)
        dev_wrong_class.id = "01:333333"

        # Run callback
        callback_func([dev_match, dev_no_attr, dev_wrong_class])

        # Should only add 1 entity (for dev_match)
        assert async_add_entities.call_count == 1
        entities = async_add_entities.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RamsesSensor)
        assert entities[0].unique_id.startswith("01:111111")


def test_sensor_init_and_properties(
    mock_coordinator: MagicMock, mock_device: MagicMock
) -> None:
    """Test initialization and basic properties of RamsesSensor."""
    # Create a description
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "test_key"
    desc.ramses_rf_attr = "temperature"
    desc.ramses_cc_icon_off = "mdi:thermometer-off"
    desc.icon = "mdi:thermometer"

    # Initialize
    sensor = RamsesSensor(mock_coordinator, mock_device, desc)

    assert sensor.unique_id == "01:123456-test_key"


@pytest.fixture
def mock_device_gwy():
    """Fixture for a mocked device."""
    device = MagicMock()
    device.id = "01:123455"
    device._gateway = MagicMock()
    device.gateway = device._gateway
    device._gateway.async_send_raw_command = AsyncMock()
    device._gateway.async_send_cmd = device._gateway.async_send_raw_command
    return device


@pytest.fixture
def mock_entity_description_codes():
    """Fixture for a mocked entity description."""
    description = MagicMock()
    description.key = "test_key"
    description.poll_codes = ["0001", "0002"]  # For poll-driven test
    return description


@pytest.fixture
def mock_entity_description_no_codes():
    """Fixture for a mocked entity description."""
    description = MagicMock()
    description.key = "test_key_no_codes"
    return description


@pytest.fixture
def entity_push_driven(
    mock_coordinator: MagicMock, mock_device_gwy, mock_entity_description_codes
):
    """Entity for push-driven scenario (should_poll=False)."""
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "test_key"
    desc.ramses_rf_attr = "test_attr"
    sensor = RamsesSensor(mock_coordinator, mock_device_gwy, desc)
    sensor._attr_should_poll = False
    sensor.entity_description = mock_entity_description_codes
    return sensor


@pytest.fixture
def entity_poll_driven(
    mock_coordinator: MagicMock, mock_device_gwy, mock_entity_description_codes
):
    """Entity for poll-driven scenario (should_poll=True)."""
    sensor = RamsesSensor(
        mock_coordinator, mock_device_gwy, mock_entity_description_codes
    )
    sensor._attr_should_poll = True
    return sensor


@pytest.fixture
def entity_poll_driven_no_codes(
    mock_coordinator: MagicMock,
    mock_device_gwy,
    mock_entity_description_no_codes,
):
    """Entity for poll-driven scenario (should_poll=True)."""
    sensor = RamsesSensor(
        mock_coordinator, mock_device_gwy, mock_entity_description_no_codes
    )
    sensor._attr_should_poll = True  # override init
    return sensor


@pytest.mark.asyncio
async def test_async_update_push_driven(
    entity_push_driven, caplog: pytest.LogCaptureFixture
):
    """Test that async_update does nothing for push-driven entities."""
    assert entity_push_driven.should_poll is False

    with caplog.at_level(logging.DEBUG):
        await entity_push_driven.async_update()
        # No commands sent
        entity_push_driven._device._gateway.async_send_raw_command.assert_not_called()
        # No polling logs
        assert "Polled" not in caplog.text


@pytest.mark.asyncio
async def test_async_update_poll_driven_success(
    entity_poll_driven, caplog: pytest.LogCaptureFixture
):
    """Test that async_update sends commands for poll-driven entities (success)."""
    assert entity_poll_driven.should_poll is True

    with caplog.at_level(logging.DEBUG):
        await entity_poll_driven.async_update()

        # Check that commands were sent for each poll_code
        mock_send = entity_poll_driven._device._gateway.async_send_raw_command
        assert mock_send.call_count == 2
        calls = mock_send.call_args_list
        assert calls[0][0][0] == CommandDTO(
            verb="RQ",
            addr1="18:000730",
            addr2="01:123455",
            addr3="--:------",
            code="0001",
            payload="00",
        )
        assert calls[1][0][0] == CommandDTO(
            verb="RQ",
            addr1="18:000730",
            addr2="01:123455",
            addr3="--:------",
            code="0002",
            payload="00",
        )

        # Check logs
        assert "Polled 0001 for 01:123455" in caplog.text
        assert "Polled 0002 for 01:123455" in caplog.text


@pytest.mark.asyncio
async def test_async_update_poll_driven_failure(
    entity_poll_driven, caplog: pytest.LogCaptureFixture
):
    """Test that async_update handles and logs errors for poll-driven entities."""
    assert entity_poll_driven.should_poll is True

    # Force an error in async_send_raw_command
    entity_poll_driven._device._gateway.async_send_raw_command = AsyncMock(
        side_effect=Exception("Connection error")
    )

    with caplog.at_level(logging.DEBUG):
        await entity_poll_driven.async_update()

        # Commands were attempted
        mock_send = entity_poll_driven._device._gateway.async_send_raw_command
        assert mock_send.call_count == 2

        # Errors were logged
        assert (
            "Poll 0001 for 01:123455 failed: Connection error" in caplog.text
        )
        assert (
            "Poll 0002 for 01:123455 failed: Connection error" in caplog.text
        )


@pytest.mark.asyncio
async def test_async_update_no_poll_codes(
    entity_poll_driven_no_codes, caplog: pytest.LogCaptureFixture
):
    """Test that async_update does nothing if there are no poll_codes."""
    assert entity_poll_driven_no_codes.should_poll is True

    with caplog.at_level(logging.DEBUG):
        await entity_poll_driven_no_codes.async_update()
        mock_send_no = (
            entity_poll_driven_no_codes._device._gateway.async_send_raw_command
        )
        mock_send_no.assert_not_called()
        assert "Polled" not in caplog.text


def test_sensor_native_value(
    mock_coordinator: MagicMock, mock_device: MagicMock
) -> None:
    """Test native_value logic including percentage handling and caching."""
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "test_key"
    desc.ramses_rf_attr = "test_attr"

    sensor = RamsesSensor(mock_coordinator, mock_device, desc)

    # 1. Normal value
    mock_device.test_attr = 15.5
    sensor._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    assert sensor.native_value == 15.5

    # 2. Percentage value (should be multiplied by 100)
    mock_device.test_attr = 0.75
    sensor._attr_native_unit_of_measurement = PERCENTAGE
    assert sensor.native_value == 75.0

    # 3. Value expires -> Should return CACHED 75.0
    mock_device.test_attr = None
    assert sensor.native_value == 75.0

    # 4. New instance -> Initial None is preserved
    sensor_new = RamsesSensor(mock_coordinator, mock_device, desc)
    assert sensor_new.native_value is None


def test_sensor_icon(
    mock_coordinator: MagicMock, mock_device: MagicMock
) -> None:
    """Test icon property logic."""
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "test_key"
    desc.ramses_rf_attr = "val"
    desc.icon = "mdi:on"
    desc.ramses_cc_icon_off = "mdi:off"

    sensor = RamsesSensor(mock_coordinator, mock_device, desc)

    # 1. Value is Truthy -> returns normal icon (via super)
    mock_device.val = 10
    sensor._attr_native_unit_of_measurement = "generic"
    assert sensor.icon == "mdi:on"

    # 2. Value is Falsy (0) -> returns icon_off
    mock_device.val = 0
    assert sensor.icon == "mdi:off"

    # 3. No icon_off defined -> returns normal icon
    desc.ramses_cc_icon_off = None
    mock_device.val = 0
    assert sensor.icon == "mdi:on"


async def test_async_put_co2_level(mock_coordinator: MagicMock) -> None:
    """Test async_put_co2_level."""
    device = MagicMock()
    device.id = "30:111111"
    device.is_faked = True
    device.set_co2_level = AsyncMock()
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "co2"
    desc.ramses_rf_attr = "co2_level"

    sensor = RamsesSensor(mock_coordinator, device, desc)
    sensor._attr_device_class = SensorDeviceClass.CO2
    sensor._attr_native_unit_of_measurement = UnitOfRatio.PARTS_PER_MILLION

    # 1. Success
    await sensor.async_put_co2_level(800)
    device.set_co2_level.assert_awaited_once_with(800)

    # 2. Incompatible device (lacks capability)
    wrong_device = MagicMock(spec=["id"])
    wrong_device.id = "01:222222"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    with pytest.raises(
        ServiceValidationError, match="does not support setting CO2 level"
    ):
        await sensor_bad.async_put_co2_level(800)

    # 3. Device not faked
    unfaked_device = MagicMock()
    unfaked_device.id = "30:111111"
    unfaked_device.is_faked = False
    unfaked_device.set_co2_level = AsyncMock()
    sensor_unfaked = RamsesSensor(mock_coordinator, unfaked_device, desc)
    with pytest.raises(
        ServiceValidationError, match="not configured as faked"
    ):
        await sensor_unfaked.async_put_co2_level(800)

    # 4. Backend DeviceNotFaked translation
    backend_unfaked_device = MagicMock()
    backend_unfaked_device.id = "30:111111"
    backend_unfaked_device.is_faked = True
    backend_unfaked_device.set_co2_level = AsyncMock(
        side_effect=DeviceNotFaked("Device not faked")
    )
    sensor_backend_unfaked = RamsesSensor(
        mock_coordinator, backend_unfaked_device, desc
    )
    with pytest.raises(ServiceValidationError, match="Device not faked"):
        await sensor_backend_unfaked.async_put_co2_level(800)


async def test_async_put_ventilation_demand(
    mock_coordinator: MagicMock,
) -> None:
    device = MagicMock(spec=VentilationDemandCapable)
    device.id = "29:150156"
    device.is_faked = True
    device.set_ventilation_demand = AsyncMock()
    mock_coordinator.entry.options = {
        "schema": {device.id: {"_bound": "32:134044"}}
    }
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "co2"
    desc.ramses_rf_attr = "co2_level"
    sensor = RamsesSensor(mock_coordinator, device, desc)
    sensor._attr_device_class = SensorDeviceClass.CO2
    sensor._attr_native_unit_of_measurement = UnitOfRatio.PARTS_PER_MILLION

    await sensor.async_put_ventilation_demand(37.5)

    device.set_ventilation_demand.assert_awaited_once_with("32:134044", 0.375)

    wrong_device = MagicMock(spec=["id"])
    wrong_device.id = "01:222222"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.CO2
    sensor_bad._attr_native_unit_of_measurement = UnitOfRatio.PARTS_PER_MILLION
    with pytest.raises(
        ServiceValidationError,
        match="does not support setting ventilation demand",
    ):
        await sensor_bad.async_put_ventilation_demand(37.5)

    unfaked_device = MagicMock(spec=VentilationDemandCapable)
    unfaked_device.id = "29:150157"
    unfaked_device.is_faked = False
    sensor_unfaked = RamsesSensor(mock_coordinator, unfaked_device, desc)
    sensor_unfaked._attr_device_class = SensorDeviceClass.CO2
    sensor_unfaked._attr_native_unit_of_measurement = (
        UnitOfRatio.PARTS_PER_MILLION
    )
    with pytest.raises(
        ServiceValidationError, match="not configured as faked"
    ):
        await sensor_unfaked.async_put_ventilation_demand(37.5)

    mock_coordinator.entry.options = {"schema": {device.id: []}}
    with pytest.raises(ValueError, match="has no bound Orcon fan"):
        await sensor.async_put_ventilation_demand(37.5)

    mock_coordinator.entry.options = {
        "schema": {device.id: {"_bound": "31:123456"}}
    }
    with pytest.raises(ValueError, match="has no bound Orcon fan"):
        await sensor.async_put_ventilation_demand(37.5)

    mock_coordinator.entry.options = {
        "schema": {device.id: {"_bound": "32:134044"}}
    }
    device.set_ventilation_demand = AsyncMock(
        side_effect=DeviceNotFaked("Device not faked")
    )
    with pytest.raises(ServiceValidationError, match="Device not faked"):
        await sensor.async_put_ventilation_demand(37.5)


async def test_async_put_dhw_temp(mock_coordinator: MagicMock) -> None:
    """Test async_put_dhw_temp."""
    device = MagicMock()
    device.id = "07:111111"
    device.is_faked = True
    device.set_temperature = AsyncMock()
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "dhw"
    desc.ramses_rf_attr = "temperature"

    sensor = RamsesSensor(mock_coordinator, device, desc)
    sensor._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    sensor.async_write_ha_state = MagicMock()

    # 1. Success
    await sensor.async_put_dhw_temp(55.0)
    device.set_temperature.assert_awaited_with(55.0)

    # 2. Incompatible device (lacks capability)
    wrong_device = MagicMock(spec=["id"])
    wrong_device.id = "01:222222"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    with pytest.raises(
        ServiceValidationError, match="does not support setting temperature"
    ):
        await sensor_bad.async_put_dhw_temp(50.0)

    # 3. Device not faked
    unfaked_device = MagicMock()
    unfaked_device.id = "07:111111"
    unfaked_device.is_faked = False
    unfaked_device.set_temperature = AsyncMock()
    sensor_unfaked = RamsesSensor(mock_coordinator, unfaked_device, desc)
    with pytest.raises(
        ServiceValidationError, match="not configured as faked"
    ):
        await sensor_unfaked.async_put_dhw_temp(50.0)


async def test_async_put_indoor_humidity(mock_coordinator: MagicMock) -> None:
    """Test async_put_indoor_humidity."""
    # Arrange
    device = MagicMock()
    device.id = "30:222222"
    device.is_faked = True
    device.set_indoor_humidity = AsyncMock()
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "hum"
    desc.ramses_rf_attr = "indoor_humidity"

    sensor = RamsesSensor(mock_coordinator, device, desc)
    sensor._attr_device_class = SensorDeviceClass.HUMIDITY
    sensor._attr_native_unit_of_measurement = PERCENTAGE

    # 1. Success - Act & Assert
    await sensor.async_put_indoor_humidity(50.0)
    device.set_indoor_humidity.assert_awaited_once_with(0.5)

    # 2. Incompatible device (lacks capability) - Act & Assert
    wrong_device = MagicMock(spec=["id"])
    wrong_device.id = "01:333333"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    with pytest.raises(
        ServiceValidationError,
        match="does not support setting indoor humidity",
    ):
        await sensor_bad.async_put_indoor_humidity(50.0)

    # 3. Device not faked - Act & Assert
    unfaked_device = MagicMock()
    unfaked_device.id = "30:222222"
    unfaked_device.is_faked = False
    unfaked_device.set_indoor_humidity = AsyncMock()
    sensor_unfaked = RamsesSensor(mock_coordinator, unfaked_device, desc)
    with pytest.raises(
        ServiceValidationError, match="not configured as faked"
    ):
        await sensor_unfaked.async_put_indoor_humidity(50.0)


async def test_async_put_room_temp(mock_coordinator: MagicMock) -> None:
    """Test async_put_room_temp."""
    device = MagicMock()
    device.id = "03:111111"
    device.is_faked = True
    device.set_temperature = AsyncMock()
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "temp"
    desc.ramses_rf_attr = "temperature"

    sensor = RamsesSensor(mock_coordinator, device, desc)
    sensor._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    sensor.async_write_ha_state = MagicMock()

    # 1. Success
    await sensor.async_put_room_temp(21.0)
    device.set_temperature.assert_awaited_with(21.0)

    # 2. Incompatible device (lacks capability)
    wrong_device = MagicMock(spec=["id"])
    wrong_device.id = "01:444444"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    with pytest.raises(
        ServiceValidationError, match="does not support setting temperature"
    ):
        await sensor_bad.async_put_room_temp(21.0)

    # 3. Device not faked
    unfaked_device = MagicMock()
    unfaked_device.id = "03:111111"
    unfaked_device.is_faked = False
    unfaked_device.set_temperature = AsyncMock()
    sensor_unfaked = RamsesSensor(mock_coordinator, unfaked_device, desc)
    with pytest.raises(
        ServiceValidationError, match="not configured as faked"
    ):
        await sensor_unfaked.async_put_room_temp(21.0)


async def test_async_setup_entry_full_descriptions(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    # Test the platform setup with real SENSOR_DESCRIPTIONS.
    entry = MagicMock()
    entry.entry_id = "test_entry_full"
    entry.runtime_data = mock_coordinator
    async_add_entities = MagicMock()

    otb_dev = MagicMock(spec=OtbGateway)
    otb_dev.id = "10:111111"
    otb_dev.heat_demand = 0.5
    otb_dev.boiler_output_temp = 45.0

    with patch(
        "custom_components.ramses_cc.sensor.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    mock_coordinator.async_register_platform.assert_called_once()
    callback_func = mock_coordinator.async_register_platform.call_args[0][1]

    callback_func([otb_dev])

    assert async_add_entities.call_count == 1
    entities = async_add_entities.call_args[0][0]
    assert len(entities) > 0
    assert any(
        e.entity_description.key == "boiler_output_temp" for e in entities
    )


def test_ufc_pump_relay_sensor(mock_coordinator: MagicMock) -> None:
    """Test UfhController pump relay state sensor."""
    # Arrange
    target_desc = next(
        desc for desc in SENSOR_DESCRIPTIONS if desc.key == SZ_PUMP_RELAY_STATE
    )
    assert target_desc.device_class == SensorDeviceClass.ENUM
    assert target_desc.entity_category == EntityCategory.DIAGNOSTIC
    assert target_desc.options == ["heating", "cooling", "off"]
    assert target_desc.state_class is None

    device = MagicMock(spec=UfhController)
    device.id = "02:123456"

    sensor = RamsesSensor(mock_coordinator, device, target_desc)

    # Act & Assert - HEATING state
    device.pump_relay_state = MagicMock(return_value=PumpRelayState.HEATING)
    assert sensor.native_value == "heating"
    assert sensor.icon == "mdi:pump"

    # Act & Assert - COOLING state
    device.pump_relay_state = MagicMock(return_value=PumpRelayState.COOLING)
    assert sensor.native_value == "cooling"
    assert sensor.icon == "mdi:pump"

    # Act & Assert - OFF state
    device.pump_relay_state = MagicMock(return_value=PumpRelayState.OFF)
    assert sensor.native_value == "off"
    assert sensor.icon == "mdi:pump"

    # Act & Assert - None / unhydrated
    device.pump_relay_state = MagicMock(return_value=None)
    sensor._last_known_value = None
    assert sensor.native_value is None


async def test_async_setup_entry_ufc_pump_relay(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test setup entry registers UfhController pump relay sensor."""
    # Arrange
    entry = MagicMock()
    entry.entry_id = "test_ufc_entry"
    entry.runtime_data = mock_coordinator
    async_add_entities = MagicMock()

    ufc_dev = MagicMock(spec=UfhController)
    ufc_dev.id = "02:123456"
    ufc_dev.heat_demand = 0.5
    ufc_dev.pump_relay_state = MagicMock(return_value=PumpRelayState.HEATING)

    # Act
    with patch(
        "custom_components.ramses_cc.sensor.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    mock_coordinator.async_register_platform.assert_called_once()
    callback_func = mock_coordinator.async_register_platform.call_args[0][1]
    callback_func([ufc_dev])

    # Assert
    assert async_add_entities.call_count == 1
    entities = async_add_entities.call_args[0][0]
    assert any(
        e.entity_description.key == SZ_PUMP_RELAY_STATE for e in entities
    )


async def test_async_setup_entry_ufh_circuit_sensors(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    # Arrange
    entry = MagicMock()
    entry.entry_id = "test_circuit_entry"
    entry.runtime_data = mock_coordinator
    async_add_entities = MagicMock()

    circuit_dev = MagicMock(spec=UfhCircuit)
    circuit_dev.id = "02:123456_00"
    circuit_dev.heat_demand = 0.75
    circuit_dev.cooling_demand = 0.25
    circuit_dev.circuit_mode = ThermalMode.HEAT
    circuit_dev.setpoint = 21.0

    # Act
    with patch(
        "custom_components.ramses_cc.sensor.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    mock_coordinator.async_register_platform.assert_called_once()
    callback_func = mock_coordinator.async_register_platform.call_args[0][1]
    callback_func([circuit_dev])

    # Assert
    assert async_add_entities.call_count == 1
    entities = async_add_entities.call_args[0][0]
    keys = {e.entity_description.key for e in entities}
    assert SZ_HEAT_DEMAND in keys
    assert SZ_COOLING_DEMAND in keys
    assert SZ_CIRCUIT_MODE in keys
    assert SZ_SETPOINT in keys


async def test_ufh_circuit_sensor_native_values(
    mock_coordinator: MagicMock,
) -> None:
    # Arrange
    circuit_dev = MagicMock(spec=UfhCircuit)
    circuit_dev.id = "02:123456_00"
    circuit_dev.heat_demand = 0.80
    circuit_dev.cooling_demand = 0.15
    circuit_dev.circuit_mode = ThermalMode.COOL
    circuit_dev.setpoint = 22.5

    heat_desc = next(
        d
        for d in SENSOR_DESCRIPTIONS
        if d.key == SZ_HEAT_DEMAND and d.ramses_rf_class is not OtbGateway
    )
    cooling_desc = next(
        d for d in SENSOR_DESCRIPTIONS if d.key == SZ_COOLING_DEMAND
    )
    mode_desc = next(
        d for d in SENSOR_DESCRIPTIONS if d.key == SZ_CIRCUIT_MODE
    )
    setpoint_desc = next(
        d for d in SENSOR_DESCRIPTIONS if d.key == SZ_SETPOINT
    )

    heat_sensor = RamsesSensor(mock_coordinator, circuit_dev, heat_desc)
    cooling_sensor = RamsesSensor(mock_coordinator, circuit_dev, cooling_desc)
    mode_sensor = RamsesSensor(mock_coordinator, circuit_dev, mode_desc)
    setpoint_sensor = RamsesSensor(
        mock_coordinator, circuit_dev, setpoint_desc
    )

    # Act & Assert
    assert heat_sensor.native_value == 80.0
    assert cooling_sensor.native_value == 15.0
    assert mode_sensor.native_value == "cool"
    assert setpoint_sensor.native_value == 22.5


async def test_ufh_controller_heat_demand_fa_fc_sensors(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
) -> None:
    # Arrange
    ufc_dev = MagicMock(spec=UfhController)
    ufc_dev.id = "02:123456"
    ufc_dev.heat_demand_fa = AsyncMock(return_value=0.40)
    ufc_dev.heat_demand_fc = AsyncMock(return_value=0.60)

    fa_desc = next(
        d for d in SENSOR_DESCRIPTIONS if d.key == f"{SZ_HEAT_DEMAND}_fa"
    )
    fc_desc = next(
        d for d in SENSOR_DESCRIPTIONS if d.key == f"{SZ_HEAT_DEMAND}_fc"
    )

    fa_sensor = RamsesSensor(mock_coordinator, ufc_dev, fa_desc)
    fa_sensor.hass = hass
    fc_sensor = RamsesSensor(mock_coordinator, ufc_dev, fc_desc)
    fc_sensor.hass = hass

    # Act - First access triggers background resolution
    _ = fa_sensor.native_value
    _ = fc_sensor.native_value
    await hass.async_block_till_done()

    # Assert - After background task completes, native_value is populated
    assert fa_sensor.native_value == 40.0
    assert fc_sensor.native_value == 60.0


async def test_ramses_sensor_async_update_polls_with_verb_rq(
    mock_coordinator: MagicMock,
) -> None:
    # Arrange
    mock_device = MagicMock()
    mock_device.id = "01:123456"
    mock_device._gateway = MagicMock()
    mock_device.gateway = mock_device._gateway
    mock_device._gateway.async_send_raw_command = AsyncMock()
    mock_device._gateway.async_send_cmd = (
        mock_device._gateway.async_send_raw_command
    )

    desc = RamsesSensorEntityDescription(
        key="test_poll_sensor",
        ramses_rf_attr="temp",
        poll_codes=[Code._30C9],
    )
    sensor = RamsesSensor(mock_coordinator, mock_device, desc)
    sensor._attr_should_poll = True

    # Act
    await sensor.async_update()

    # Assert
    mock_device._gateway.async_send_raw_command.assert_awaited_once()
    sent_cmd = mock_device._gateway.async_send_raw_command.call_args[0][0]
    assert isinstance(sent_cmd, CommandDTO)
    assert sent_cmd.verb == Verb.RQ
    assert sent_cmd.code == "30C9"


def test_last_msg_sensor_description() -> None:
    """The last_msg description uses the RamsesLastMessageSensor class."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)
    assert desc.ramses_cc_class is RamsesLastMessageSensor
    assert desc.device_class is None
    assert desc.state_class is None
    assert desc.entity_category == EntityCategory.DIAGNOSTIC


def test_fan_rate_restricted_to_ventilator() -> None:
    """fan_rate is only created for FAN-class devices (issue 1216).

    A REM/DIS remote has no ventilator; its fan_rate merely mirrors a
    byte from its own last transmitted command.
    """
    descs = [d for d in SENSOR_DESCRIPTIONS if d.key == SZ_FAN_RATE]
    assert len(descs) == 1
    assert descs[0].ramses_rf_class is HvacVentilator


def test_fan_mode_split_by_device_class() -> None:
    """fan_mode reads 'Last mode sent' on REM/DIS, 'Fan mode' on FAN."""
    descs = [d for d in SENSOR_DESCRIPTIONS if d.key == SZ_FAN_MODE]
    assert len(descs) == 2
    by_name = {d.name: d for d in descs}
    assert by_name["Fan mode"].ramses_rf_class is HvacVentilator
    assert by_name["Last mode sent"].ramses_rf_class is HvacRemote
    # Both expose a last_sent attribute resolved from the device's
    # last_fan_mode_dtm (absent on ramses_rf versions without it).
    for desc in descs:
        assert desc.ramses_cc_extra_attributes == {
            "last_sent": "last_fan_mode_dtm"
        }


def test_last_msg_sensor_values(mock_coordinator: MagicMock) -> None:
    """native_value is verb/code dst payload; attrs describe the message."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)
    device = MagicMock(spec=RamsesRFEntity)
    device.id = "04:123456"

    dtm = dt(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
    msg = MagicMock()
    msg.dtm = dtm
    msg.verb = " I"
    msg.code = "22F3"
    msg.dst.id = "32:999888"
    msg.payload = {"fan_mode": "boost"}
    device.last_command = msg

    sensor = RamsesLastMessageSensor(mock_coordinator, device, desc)

    assert sensor.native_value.startswith("I/22F3 32:999888")
    assert "boost" in sensor.native_value
    attrs = sensor.extra_state_attributes
    assert attrs["sent"] == dtm.isoformat()
    assert attrs["verb"] == " I"
    assert attrs["code"] == "22F3"
    assert attrs["dst"] == "32:999888"
    assert "payload" not in attrs


def test_last_msg_sensor_no_message(
    mock_coordinator: MagicMock,
) -> None:
    """A device never heard from yields None state and no msg attrs."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)
    device = MagicMock(spec=RamsesRFEntity)
    device.id = "04:123456"
    device.last_command = None

    sensor = RamsesLastMessageSensor(mock_coordinator, device, desc)

    assert sensor.native_value is None
    attrs = sensor.extra_state_attributes
    assert "verb" not in attrs
    assert "code" not in attrs
    assert "sent" not in attrs


def test_last_msg_sensor_naive_dtm(mock_coordinator: MagicMock) -> None:
    """Naive message timestamps are reported as UTC in the sent attribute."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)
    device = MagicMock(spec=RamsesRFEntity)
    device.id = "04:123456"

    naive = dt(2024, 5, 6, 7, 8, 9)
    msg = MagicMock()
    msg.dtm = naive
    msg.payload = {"fan_mode": "low"}
    device.last_command = msg

    sensor = RamsesLastMessageSensor(mock_coordinator, device, desc)
    attrs = sensor.extra_state_attributes
    assert attrs["sent"] == naive.replace(tzinfo=UTC).isoformat()


def test_last_msg_sensor_none_payload(mock_coordinator: MagicMock) -> None:
    """A message with a None payload still shows its header."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)
    device = MagicMock(spec=RamsesRFEntity)
    device.id = "04:123456"

    msg = MagicMock()
    msg.dtm = dt(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
    msg.verb = "RQ"
    msg.code = "10D0"
    msg.dst.id = "32:153289"
    msg.payload = None
    device.last_command = msg

    sensor = RamsesLastMessageSensor(mock_coordinator, device, desc)
    assert sensor.native_value == "RQ/10D0 32:153289"
    assert "sent" in sensor.extra_state_attributes


def test_last_msg_sensor_truncates_long_payload(
    mock_coordinator: MagicMock,
) -> None:
    """Payloads longer than the HA state limit are truncated."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)
    device = MagicMock(spec=RamsesRFEntity)
    device.id = "04:123456"

    msg = MagicMock()
    msg.dtm = dt(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
    msg.payload = {"data": "x" * 300}
    device.last_command = msg

    sensor = RamsesLastMessageSensor(mock_coordinator, device, desc)
    assert len(sensor.native_value) == 255
    assert sensor.native_value.endswith("...")


def test_last_msg_sensor_hgi_disabled_by_default(
    mock_coordinator: MagicMock,
) -> None:
    """HGI last_msg sensors default to disabled to limit recorder writes."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)

    hgi = MagicMock(spec=HgiGateway)
    hgi.id = "18:123456"
    sensor = RamsesLastMessageSensor(mock_coordinator, hgi, desc)
    assert sensor.entity_registry_enabled_default is False

    rem = MagicMock(spec=HvacRemote)
    rem.id = "29:123456"
    sensor = RamsesLastMessageSensor(mock_coordinator, rem, desc)
    assert sensor.entity_registry_enabled_default is True


async def test_last_msg_sensor_gated_by_option(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """The last_msg sensor is only created when the option is enabled."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == SZ_LAST_MSG)

    device = MagicMock(spec=HvacRemote)
    device.id = "29:111111"
    device.last_msg = MagicMock()

    for options, expected in (
        ({}, 0),
        ({CONF_ADVANCED_FEATURES: {}}, 0),
        ({CONF_ADVANCED_FEATURES: {CONF_LAST_MSG_SENSORS: False}}, 0),
        ({CONF_ADVANCED_FEATURES: {CONF_LAST_MSG_SENSORS: True}}, 1),
    ):
        entry = MagicMock()
        entry.entry_id = "test_entry_gated"
        entry.runtime_data = mock_coordinator
        entry.options = options
        async_add_entities = MagicMock()
        mock_coordinator.async_register_platform.reset_mock()

        with (
            patch(
                "custom_components.ramses_cc.sensor.async_get_current_platform"
            ),
            patch(
                "custom_components.ramses_cc.sensor.SENSOR_DESCRIPTIONS",
                (desc,),
            ),
        ):
            await async_setup_entry(hass, entry, async_add_entities)
            callback_func = mock_coordinator.async_register_platform.call_args[
                0
            ][1]
            callback_func([device])

        assert len(async_add_entities.call_args[0][0]) == expected


async def test_last_msg_registry_disabled_when_option_off(
    hass: HomeAssistant,
) -> None:
    """Enabled last_msg entries are disabled when the option is off.

    Entries already disabled by the user are left alone.
    """
    registry = er.async_get(hass)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_ADVANCED_FEATURES: {CONF_LAST_MSG_SENSORS: False}},
    )
    config_entry.add_to_hass(hass)

    enabled = registry.async_get_or_create(
        "sensor", DOMAIN, "01:123456-last_msg", config_entry=config_entry
    )
    user_disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "01:123457-last_msg",
        config_entry=config_entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    _sync_last_msg_registry_entries(hass, config_entry)

    assert (
        registry.async_get(enabled.entity_id).disabled_by
        == er.RegistryEntryDisabler.INTEGRATION
    )
    assert (
        registry.async_get(user_disabled.entity_id).disabled_by
        == er.RegistryEntryDisabler.USER
    )


async def test_last_msg_registry_reenabled_when_option_on(
    hass: HomeAssistant,
) -> None:
    """Integration-disabled last_msg entries re-enable when option is on.

    HGI (18:) entries stay disabled (default-off for HGIs) and
    user-disabled entries are left alone.
    """
    registry = er.async_get(hass)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_ADVANCED_FEATURES: {CONF_LAST_MSG_SENSORS: True}},
    )
    config_entry.add_to_hass(hass)

    int_disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "32:157747-last_msg",
        config_entry=config_entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    hgi_disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "18:006402-last_msg",
        config_entry=config_entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    user_disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "29:179540-last_msg",
        config_entry=config_entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    _sync_last_msg_registry_entries(hass, config_entry)

    assert registry.async_get(int_disabled.entity_id).disabled_by is None
    assert (
        registry.async_get(hgi_disabled.entity_id).disabled_by
        == er.RegistryEntryDisabler.INTEGRATION
    )
    assert (
        registry.async_get(user_disabled.entity_id).disabled_by
        == er.RegistryEntryDisabler.USER
    )

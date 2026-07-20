"""Tests for the ramses_cc sensor platform."""

from __future__ import annotations

import logging
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.sensor import (
    SENSOR_DESCRIPTIONS,
    RamsesSensor,
    RamsesSensorEntityDescription,
    async_setup_entry,
)
from ramses_rf.const import SZ_TEMPERATURE
from ramses_rf.devices import (
    DhwSensor,
    HvacCarbonDioxideSensor,
    HvacHumiditySensor,
    OtbGateway,
    Thermostat,
)
from ramses_rf.entity import Entity as RamsesRFEntity
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
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}
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
        "custom_components.ramses_cc.sensor.SENSOR_DESCRIPTIONS", (target_desc,)
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
    device._gwy = MagicMock()
    device._gwy.async_send_cmd = AsyncMock()
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
    mock_coordinator: MagicMock, mock_device_gwy, mock_entity_description_no_codes
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
        entity_push_driven._device._gwy.async_send_cmd.assert_not_called()
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
        assert entity_poll_driven._device._gwy.async_send_cmd.call_count == 2
        calls = entity_poll_driven._device._gwy.async_send_cmd.call_args_list
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

    # Force an error in async_send_cmd
    entity_poll_driven._device._gwy.async_send_cmd = AsyncMock(
        side_effect=Exception("Connection error")
    )

    with caplog.at_level(logging.DEBUG):
        await entity_poll_driven.async_update()

        # Commands were attempted
        assert entity_poll_driven._device._gwy.async_send_cmd.call_count == 2

        # Errors were logged
        assert "Poll 0001 for 01:123455 failed: Connection error" in caplog.text
        assert "Poll 0002 for 01:123455 failed: Connection error" in caplog.text


@pytest.mark.asyncio
async def test_async_update_no_poll_codes(
    entity_poll_driven_no_codes, caplog: pytest.LogCaptureFixture
):
    """Test that async_update does nothing if there are no poll_codes."""
    assert entity_poll_driven_no_codes.should_poll is True

    with caplog.at_level(logging.DEBUG):
        await entity_poll_driven_no_codes.async_update()
        entity_poll_driven_no_codes._device._gwy.async_send_cmd.assert_not_called()
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


def test_sensor_icon(mock_coordinator: MagicMock, mock_device: MagicMock) -> None:
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


def test_async_put_co2_level(mock_coordinator: MagicMock) -> None:
    """Test async_put_co2_level."""
    device = MagicMock(spec=HvacCarbonDioxideSensor)
    device.id = "30:111111"
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "co2"
    desc.ramses_rf_attr = "co2_level"

    sensor = RamsesSensor(mock_coordinator, device, desc)
    sensor._attr_device_class = SensorDeviceClass.CO2
    sensor._attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION

    # 1. Success
    sensor.async_put_co2_level(800)
    assert device.co2_level == 800

    # 2. Assert fail: Wrong Device Class
    sensor._attr_device_class = SensorDeviceClass.TEMPERATURE
    with pytest.raises(AssertionError):
        sensor.async_put_co2_level(800)
    sensor._attr_device_class = SensorDeviceClass.CO2

    # 3. TypeError: Wrong device type
    wrong_device = MagicMock(spec=RamsesRFEntity)
    wrong_device.id = "01:222222"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.CO2
    sensor_bad._attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION

    with pytest.raises(TypeError, match="Cannot set CO2 level"):
        sensor_bad.async_put_co2_level(800)


async def test_async_put_dhw_temp(mock_coordinator: MagicMock) -> None:
    """Test async_put_dhw_temp."""
    device = MagicMock(spec=DhwSensor)
    device.id = "07:111111"
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

    # 2. TypeError: Wrong device type
    wrong_device = MagicMock(spec=RamsesRFEntity)
    wrong_device.id = "01:222222"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor_bad._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    with pytest.raises(TypeError, match="Cannot set DHW temperature"):
        await sensor_bad.async_put_dhw_temp(50.0)


def test_async_put_indoor_humidity(mock_coordinator: MagicMock) -> None:
    """Test async_put_indoor_humidity."""
    device = MagicMock(spec=HvacHumiditySensor)
    device.id = "30:222222"
    desc = MagicMock(spec=RamsesSensorEntityDescription)
    desc.key = "hum"
    desc.ramses_rf_attr = "indoor_humidity"

    sensor = RamsesSensor(mock_coordinator, device, desc)
    sensor._attr_device_class = SensorDeviceClass.HUMIDITY
    sensor._attr_native_unit_of_measurement = PERCENTAGE

    # 1. Success
    sensor.async_put_indoor_humidity(50.0)
    assert device.indoor_humidity == 0.5

    # 2. TypeError
    wrong_device = MagicMock(spec=RamsesRFEntity)
    wrong_device.id = "01:333333"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.HUMIDITY
    sensor_bad._attr_native_unit_of_measurement = PERCENTAGE

    with pytest.raises(TypeError, match="Cannot set indoor humidity"):
        sensor_bad.async_put_indoor_humidity(50.0)


async def test_async_put_room_temp(mock_coordinator: MagicMock) -> None:
    """Test async_put_room_temp."""
    device = MagicMock(spec=Thermostat)
    device.id = "03:111111"
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

    # 2. TypeError
    wrong_device = MagicMock(spec=RamsesRFEntity)
    wrong_device.id = "01:444444"
    sensor_bad = RamsesSensor(mock_coordinator, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor_bad._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    with pytest.raises(TypeError, match="Cannot set room temperature"):
        await sensor_bad.async_put_room_temp(21.0)


async def test_async_setup_entry_full_descriptions(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    # Test the platform setup with real SENSOR_DESCRIPTIONS.
    entry = MagicMock()
    entry.entry_id = "test_entry_full"
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}
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
    assert any(e.entity_description.key == "boiler_output_temp" for e in entities)

"""Tests for the ramses_cc sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorEntityDescription
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
    async_setup_entry,
)
from ramses_rf.device import Fakeable
from ramses_rf.device.heat import DhwSensor, Thermostat
from ramses_rf.device.hvac import HvacCarbonDioxideSensor, HvacHumiditySensor
from ramses_rf.entity_base import Entity as RamsesRFEntity


@pytest.fixture
def mock_broker() -> MagicMock:
    """Return a mock RamsesBroker."""
    broker = MagicMock()
    broker.hass = MagicMock()
    broker.async_register_platform = MagicMock()
    return broker


@pytest.fixture
def mock_device() -> MagicMock:
    """Return a mock RamsesRFEntity."""
    device = MagicMock(spec=RamsesRFEntity)
    device.id = "01:123456"
    return device


async def test_async_setup_entry(hass: HomeAssistant, mock_broker: MagicMock) -> None:
    """Test the platform setup and entity creation callback."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    hass.data[DOMAIN] = {entry.entry_id: mock_broker}
    async_add_entities = MagicMock()

    # Mock async_get_current_platform
    with patch(
        "custom_components.ramses_cc.sensor.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    # Verify platform registration
    mock_broker.async_register_platform.assert_called_once()
    callback_func = mock_broker.async_register_platform.call_args[0][1]

    # Use the first description (SZ_TEMPERATURE for HvacHumiditySensor | TrvActuator)
    # We patch SENSOR_DESCRIPTIONS to ONLY contain this one description
    # This prevents the mock device from matching multiple descriptions
    target_desc = SENSOR_DESCRIPTIONS[0]

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
        # Since MagicMock(spec=...) automatically adds spec attributes, we delete it
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
    mock_broker: MagicMock, mock_device: MagicMock
) -> None:
    """Test initialization and basic properties of RamsesSensor."""
    # Create a description
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "test_key"
    desc.ramses_rf_attr = "temperature"
    desc.ramses_cc_icon_off = "mdi:thermometer-off"
    desc.icon = "mdi:thermometer"

    # Initialize
    sensor = RamsesSensor(mock_broker, mock_device, desc)

    assert sensor.unique_id == "01:123456-test_key"
    # Note: sensor.py generates entity_id using device.id directly, preserving colons
    assert sensor.entity_id == "sensor.01:123456_test_key"


def test_sensor_available_property(
    mock_broker: MagicMock, mock_device: MagicMock
) -> None:
    """Test the 'available' property logic."""
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "test"
    desc.ramses_rf_attr = "attr"
    # Ensure translation_key is None to avoid ValueError in validation if called
    desc.translation_key = None

    sensor = RamsesSensor(mock_broker, mock_device, desc)

    # 1. Not Fakeable, State is None -> False
    # We patch SensorEntity.state because RamsesSensor inherits from it
    with patch(
        "homeassistant.components.sensor.SensorEntity.state", new_callable=PropertyMock
    ) as mock_state:
        mock_state.return_value = None
        # Assign to variable to avoid Mypy narrowing the property permanently
        is_available = sensor.available
        assert is_available is False

        # 2. Not Fakeable, State is 'active' (not None) -> True
        mock_state.return_value = "21.5"
        is_available = sensor.available
        assert is_available is True

        # 3. Fakeable and is_faked -> True (even if state is None)
        mock_fake_device = MagicMock(spec=Fakeable)
        mock_fake_device.id = "02:000000"
        mock_fake_device.is_faked = True
        sensor_fake = RamsesSensor(mock_broker, mock_fake_device, desc)

        mock_state.return_value = None
        # We must verify available on the sensor_fake instance
        # Since logic calls self.state, which is inherited, patching the class affects it
        is_available = sensor_fake.available
        assert is_available is True


def test_sensor_native_value(mock_broker: MagicMock, mock_device: MagicMock) -> None:
    """Test native_value logic including percentage handling."""
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "test_key"
    desc.ramses_rf_attr = "test_attr"

    sensor = RamsesSensor(mock_broker, mock_device, desc)

    # 1. Normal value
    mock_device.test_attr = 15.5
    sensor._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    assert sensor.native_value == 15.5

    # 2. Percentage value (should be multiplied by 100)
    mock_device.test_attr = 0.75
    sensor._attr_native_unit_of_measurement = PERCENTAGE
    assert sensor.native_value == 75.0

    # 3. Percentage None
    mock_device.test_attr = None
    assert sensor.native_value is None


def test_sensor_icon(mock_broker: MagicMock, mock_device: MagicMock) -> None:
    """Test icon property logic."""
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "test_key"
    desc.ramses_rf_attr = "val"
    desc.icon = "mdi:on"
    desc.ramses_cc_icon_off = "mdi:off"

    sensor = RamsesSensor(mock_broker, mock_device, desc)

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


def test_async_put_co2_level(mock_broker: MagicMock) -> None:
    """Test async_put_co2_level."""
    device = MagicMock(spec=HvacCarbonDioxideSensor)
    device.id = "30:111111"
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "co2"
    desc.ramses_rf_attr = "co2_level"

    sensor = RamsesSensor(mock_broker, device, desc)
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
    sensor_bad = RamsesSensor(mock_broker, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.CO2
    sensor_bad._attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION

    with pytest.raises(TypeError, match="Cannot set CO2 level"):
        sensor_bad.async_put_co2_level(800)


def test_async_put_dhw_temp(mock_broker: MagicMock) -> None:
    """Test async_put_dhw_temp."""
    device = MagicMock(spec=DhwSensor)
    device.id = "07:111111"
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "dhw"
    desc.ramses_rf_attr = "temperature"

    sensor = RamsesSensor(mock_broker, device, desc)
    sensor._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    # 1. Success
    sensor.async_put_dhw_temp(55.0)
    assert device.temperature == 55.0

    # 2. TypeError: Wrong device type
    wrong_device = MagicMock(spec=RamsesRFEntity)
    wrong_device.id = "01:222222"
    sensor_bad = RamsesSensor(mock_broker, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor_bad._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    # Error message in code is "Cannot set CO2 level on..." (copy-paste error in source)
    with pytest.raises(TypeError, match="Cannot set CO2 level"):
        sensor_bad.async_put_dhw_temp(50.0)


def test_async_put_indoor_humidity(mock_broker: MagicMock) -> None:
    """Test async_put_indoor_humidity."""
    device = MagicMock(spec=HvacHumiditySensor)
    device.id = "30:222222"
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "hum"
    desc.ramses_rf_attr = "indoor_humidity"

    sensor = RamsesSensor(mock_broker, device, desc)
    sensor._attr_device_class = SensorDeviceClass.HUMIDITY
    sensor._attr_native_unit_of_measurement = PERCENTAGE

    # 1. Success
    sensor.async_put_indoor_humidity(50.0)
    assert device.indoor_humidity == 0.5

    # 2. TypeError
    wrong_device = MagicMock(spec=RamsesRFEntity)
    wrong_device.id = "01:333333"
    sensor_bad = RamsesSensor(mock_broker, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.HUMIDITY
    sensor_bad._attr_native_unit_of_measurement = PERCENTAGE

    with pytest.raises(TypeError, match="Cannot set indoor humidity"):
        sensor_bad.async_put_indoor_humidity(50.0)


def test_async_put_room_temp(mock_broker: MagicMock) -> None:
    """Test async_put_room_temp."""
    device = MagicMock(spec=Thermostat)
    device.id = "03:111111"
    desc = MagicMock(spec=SensorEntityDescription)
    desc.key = "temp"
    desc.ramses_rf_attr = "temperature"

    sensor = RamsesSensor(mock_broker, device, desc)
    sensor._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    # 1. Success
    sensor.async_put_room_temp(21.0)
    assert device.temperature == 21.0

    # 2. TypeError
    wrong_device = MagicMock(spec=RamsesRFEntity)
    wrong_device.id = "01:444444"
    sensor_bad = RamsesSensor(mock_broker, wrong_device, desc)
    sensor_bad._attr_device_class = SensorDeviceClass.TEMPERATURE
    sensor_bad._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    # Error message in code is "Cannot set CO2 level on..."
    with pytest.raises(TypeError, match="Cannot set CO2 level"):
        sensor_bad.async_put_room_temp(21.0)

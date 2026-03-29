"""Tests for the ramses_cc binary_sensor platform."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ramses_cc.binary_sensor import (
    ATTR_BATTERY_LEVEL,
    RamsesBatteryBinarySensor,
    RamsesBinarySensor,
    RamsesBinarySensorEntityDescription,
    RamsesGatewayBinarySensor,
    RamsesLogbookBinarySensor,
    RamsesSystemBinarySensor,
    async_setup_entry,
)
from custom_components.ramses_cc.const import DOMAIN
from ramses_rf.device.base import BatteryState, HgiGateway
from ramses_rf.system.heat import Logbook, System
from ramses_tx.const import SZ_IS_EVOFW3


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Return a mock RamsesCoordinator.

    :return: A mock object simulating the RamsesCoordinator.
    """
    coordinator = MagicMock()
    coordinator.async_register_platform = MagicMock()
    return coordinator


async def test_async_setup_entry(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test the platform setup and entity creation callback.

    :param hass: The Home Assistant instance.
    :param mock_coordinator: The mock coordinator fixture.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}

    mock_add_entities = MagicMock()

    with patch("custom_components.ramses_cc.binary_sensor.entity_platform"):
        await async_setup_entry(hass, entry, mock_add_entities)

    assert mock_coordinator.async_register_platform.called
    # Extract the callback passed to async_register_platform
    add_devices_callback = mock_coordinator.async_register_platform.call_args[0][1]

    # Create a mock device that matches one of the descriptions
    mock_device = MagicMock(spec=HgiGateway)
    mock_device.id = "18:123456"

    # Call the callback with the mock device
    add_devices_callback([mock_device])

    # Verify async_add_entities was called with the created entity
    assert mock_add_entities.called
    created_entities = mock_add_entities.call_args[0][0]
    assert len(created_entities) == 1
    assert isinstance(created_entities[0], RamsesGatewayBinarySensor)


async def test_generic_binary_sensor(mock_coordinator: MagicMock) -> None:
    """Test RamsesBinarySensor base class logic.

    :param mock_coordinator: The mock coordinator fixture.
    """
    description = RamsesBinarySensorEntityDescription(
        key="test_sensor",
        ramses_rf_attr="test_attr",
        name="Test Sensor",
        icon="mdi:test",
        icon_off="mdi:test-off",
    )

    mock_device = MagicMock()
    mock_device.id = "01:123456"

    # Mock a recent message so availability check passes
    # Assign to the state_store mock so the base RamsesEntity successfully evaluates it
    msg_recent = MagicMock()
    msg_recent.dtm = dt_util.now()
    mock_device.state_store = MagicMock()
    mock_device.state_store._msgs_ = {"0000": msg_recent}

    sensor = RamsesBinarySensor(mock_coordinator, mock_device, description)

    assert sensor.unique_id == "01:123456-test_sensor"

    # Assign to a variable first to satisfy Mypy
    avail_state = sensor.available
    assert avail_state is True

    # Test is_on and icon resolution based on property
    mock_device.test_attr = True
    state_1 = sensor.is_on
    assert state_1 is True
    icon_1 = sensor.icon
    assert icon_1 == "mdi:test"

    mock_device.test_attr = False
    state_2 = sensor.is_on
    assert state_2 is False
    icon_2 = sensor.icon
    assert icon_2 == "mdi:test-off"

    # Test callable attribute support (Duck-Typing backwards compat)
    mock_device.test_attr = MagicMock(return_value=True)
    state_3 = sensor.is_on
    assert state_3 is True


async def test_battery_binary_sensor(mock_coordinator: MagicMock) -> None:
    """Test RamsesBatteryBinarySensor.

    :param mock_coordinator: The mock coordinator fixture.
    """
    description = RamsesBinarySensorEntityDescription(
        key="test_battery",
        ramses_rf_attr="battery_low",
        name="Test Battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        ramses_cc_class=RamsesBatteryBinarySensor,
    )

    mock_device = MagicMock()
    mock_device.id = "04:123456"

    sensor: Any = RamsesBatteryBinarySensor(mock_coordinator, mock_device, description)

    # 1. Battery state present - Mocked as a return value for the callable DTO
    mock_device.battery_state.return_value = {
        ATTR_BATTERY_LEVEL: 0.5,
        BatteryState.BATTERY_LOW: True,
    }
    # Mock the specific attr for is_on
    setattr(mock_device, description.ramses_rf_attr, True)

    state_1 = sensor.is_on
    assert state_1 is True
    attrs = sensor.extra_state_attributes
    assert attrs[ATTR_BATTERY_LEVEL] == 0.5

    # 2. Battery state missing
    mock_device.battery_state.return_value = None
    attrs_2 = sensor.extra_state_attributes
    assert attrs_2[ATTR_BATTERY_LEVEL] is None


async def test_logbook_binary_sensor_availability(mock_coordinator: MagicMock) -> None:
    """Test RamsesLogbookBinarySensor availability delegates to device.

    :param mock_coordinator: The mock coordinator fixture.
    """
    description = RamsesBinarySensorEntityDescription(
        key="active_fault",
        name="Active fault",
        ramses_rf_attr="active_faults",
        ramses_cc_class=RamsesLogbookBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=Logbook)
    mock_device.id = "01:123456"

    sensor: Any = RamsesLogbookBinarySensor(mock_coordinator, mock_device, description)

    # Case A: Device is not available
    mock_device.is_available = False
    assert sensor.available is False

    # Case B: Device is available
    mock_device.is_available = True
    assert sensor.available is True


async def test_logbook_binary_sensor_state(mock_coordinator: MagicMock) -> None:
    """Test RamsesLogbookBinarySensor state based on faults.

    :param mock_coordinator: The mock coordinator fixture.
    """
    description = RamsesBinarySensorEntityDescription(
        key="active_fault",
        name="Active fault",
        ramses_rf_attr="active_faults",
        ramses_cc_class=RamsesLogbookBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=Logbook)
    mock_device.id = "01:123456"

    sensor: Any = RamsesLogbookBinarySensor(mock_coordinator, mock_device, description)

    # 1. Test is_on = False (No faults) - Using .return_value for callable
    mock_device.active_faults.return_value = []

    # Assign to a variable first. This satisfies Ruff and Mypy
    initial_state = sensor.is_on
    assert initial_state is False

    # 2. Test is_on = True (Has faults)
    mock_device.active_faults.return_value = [{"fault": "error"}]
    final_state = sensor.is_on
    assert final_state is True


async def test_system_binary_sensor_availability(mock_coordinator: MagicMock) -> None:
    """Test RamsesSystemBinarySensor availability delegates to device.

    :param mock_coordinator: The mock coordinator fixture.
    """
    description = RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="id",
        name="System status",
        ramses_cc_class=RamsesSystemBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=System)
    mock_device.id = "01:123456"

    sensor: Any = RamsesSystemBinarySensor(mock_coordinator, mock_device, description)

    # Case A: Device is not available
    mock_device.is_available = False
    assert sensor.available is False

    # Case B: Device is available
    mock_device.is_available = True
    assert sensor.available is True


@patch("custom_components.ramses_cc.binary_sensor.resolve_async_attr")
async def test_gateway_binary_sensor_attrs(
    mock_resolve_async_attr: MagicMock, mock_coordinator: MagicMock
) -> None:
    """Test RamsesGatewayBinarySensor attribute caching and async schema resolution.

    :param mock_resolve_async_attr: Mock for the async attribute resolver helper.
    :param mock_coordinator: The mock coordinator fixture.
    """
    description = RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="id",
        name="Gateway status",
        ramses_cc_class=RamsesGatewayBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=HgiGateway)
    mock_device.id = "18:123456"

    # Setup the gateway mock to match the expected structure
    gwy = MagicMock()
    gwy.tcs.id = "01:111111"

    # Mock the resolve_async_attr helper to return our safe, synchronous schema dict
    mock_resolve_async_attr.return_value = {"system_schema": "test"}

    gwy.known_list = {"10:1": {"alias": "test", "class": "RAD", "faked": True}}
    gwy._engine = MagicMock()
    gwy._engine._enforce_known_list = True
    gwy._engine._exclude = {}
    gwy._engine._transport.get_extra_info.return_value = True

    mock_device._gwy = gwy

    sensor: Any = RamsesGatewayBinarySensor(mock_coordinator, mock_device, description)

    # Fetch attributes (should cache and utilize the mocked async helper)
    attrs = sensor.extra_state_attributes

    # Verify our architectural fix: the helper MUST be called to prevent coroutine crashes
    mock_resolve_async_attr.assert_called_once_with(sensor, gwy.tcs, "_schema_min")

    assert attrs["config"]["enforce_known_list"] is True
    assert "01:111111" in attrs["schema"]
    assert attrs["schema"]["01:111111"] == {"system_schema": "test"}
    assert attrs[SZ_IS_EVOFW3] is True

    # Verify filtering/shrinking of known_list, ensure falsey/none values don't block
    # the whitelisted keys
    known = attrs["known_list"][0]["10:1"]
    assert known["alias"] == "test"
    assert known["class"] == "RAD"
    assert known["faked"] is True


async def test_gateway_binary_sensor_state(mock_coordinator: MagicMock) -> None:
    """Test RamsesGatewayBinarySensor is_on state logic.

    :param mock_coordinator: The mock coordinator fixture.
    """
    description = RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="id",
        name="Gateway status",
        ramses_cc_class=RamsesGatewayBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=HgiGateway)
    mock_device.id = "18:123456"
    gwy = MagicMock()
    mock_device._gwy = gwy

    sensor: Any = RamsesGatewayBinarySensor(mock_coordinator, mock_device, description)

    # 1. Case A: Recent message -> is_on False (Problem = False -> OK)
    msg = MagicMock()
    msg.dtm = dt_util.now()
    gwy._this_msg = msg

    # Assign to variable to prevent Mypy narrowing sensor.is_on to Literal[False]
    is_on_check_a = sensor.is_on
    assert is_on_check_a is False

    # 2. Case B: Old message -> is_on True (Problem = True -> Fault)
    # Using same msg object, just change timestamp
    msg.dtm = dt_util.now() - timedelta(seconds=400)
    is_on_check_b = sensor.is_on
    assert is_on_check_b is True

"""Tests for the ramses_cc binary_sensor platform."""

from __future__ import annotations

from datetime import datetime as dt, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant

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
def mock_broker() -> MagicMock:
    """Return a mock RamsesBroker.

    :return: A mock object simulating the RamsesBroker.
    """
    broker = MagicMock()
    broker.async_register_platform = MagicMock()
    return broker


async def test_async_setup_entry(hass: HomeAssistant, mock_broker: MagicMock) -> None:
    """Test the platform setup and entity creation callback.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    hass.data[DOMAIN] = {entry.entry_id: mock_broker}
    async_add_entities = MagicMock()

    # Mock async_get_current_platform to avoid RuntimeError
    with patch(
        "custom_components.ramses_cc.binary_sensor.entity_platform.async_get_current_platform"
    ) as mock_plat:
        mock_plat.return_value = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

    mock_broker.async_register_platform.assert_called_once()
    callback_func = mock_broker.async_register_platform.call_args[0][1]

    # Create mock devices corresponding to descriptions
    # 1. Gateway (HgiGateway)
    dev_gwy = MagicMock(spec=HgiGateway)
    dev_gwy.id = "18:111111"

    # 2. System (System)
    dev_sys = MagicMock(spec=System)
    dev_sys.id = "01:222222"

    # Call the internal callback
    callback_func([dev_gwy, dev_sys])

    # Verify entities are added
    assert async_add_entities.call_count == 1
    entities = async_add_entities.call_args[0][0]

    # We expect at least Gateway status and System status
    assert len(entities) >= 2
    assert any(isinstance(e, RamsesGatewayBinarySensor) for e in entities)
    assert any(isinstance(e, RamsesSystemBinarySensor) for e in entities)


async def test_ramses_binary_sensor_on(mock_broker: MagicMock) -> None:
    """Test RamsesBinarySensor when on."""
    description = RamsesBinarySensorEntityDescription(
        key="test_generic",
        ramses_rf_attr="test_attr",
        name="Test Generic",
        icon="mdi:on",
        icon_off="mdi:off",
    )
    mock_device = MagicMock()
    mock_device.id = "13:123456"
    setattr(mock_device, description.ramses_rf_attr, True)

    sensor = RamsesBinarySensor(mock_broker, mock_device, description)

    assert sensor.available is True
    assert sensor.is_on is True
    assert sensor.icon == description.icon


async def test_ramses_binary_sensor_off(mock_broker: MagicMock) -> None:
    """Test RamsesBinarySensor when off."""
    description = RamsesBinarySensorEntityDescription(
        key="test_generic",
        ramses_rf_attr="test_attr",
        name="Test Generic",
        icon="mdi:on",
        icon_off="mdi:off",
    )
    mock_device = MagicMock()
    mock_device.id = "13:123456"
    setattr(mock_device, description.ramses_rf_attr, False)

    sensor = RamsesBinarySensor(mock_broker, mock_device, description)

    assert sensor.is_on is False
    assert sensor.icon == description.icon_off


async def test_battery_binary_sensor(mock_broker: MagicMock) -> None:
    """Test RamsesBatteryBinarySensor.

    :param mock_broker: The mock broker fixture.
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

    sensor: Any = RamsesBatteryBinarySensor(mock_broker, mock_device, description)

    # 1. Battery state present
    mock_device.battery_state = {
        ATTR_BATTERY_LEVEL: 0.5,
        BatteryState.BATTERY_LOW: True,
    }
    # Mock the specific attr for is_on
    setattr(mock_device, description.ramses_rf_attr, True)

    assert sensor.is_on is True
    attrs = sensor.extra_state_attributes
    assert attrs[ATTR_BATTERY_LEVEL] == 0.5

    # 2. Battery state None
    mock_device.battery_state = None
    attrs = sensor.extra_state_attributes
    assert attrs[ATTR_BATTERY_LEVEL] is None


async def test_logbook_binary_sensor_availability(mock_broker: MagicMock) -> None:
    """Test RamsesLogbookBinarySensor availability based on message age.

    :param mock_broker: The mock broker fixture.
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

    sensor: Any = RamsesLogbookBinarySensor(mock_broker, mock_device, description)

    # Case A: No message -> Not available
    mock_device._msgs = {}
    assert sensor.available is False

    # Case B: Old message -> Not available
    msg_old = MagicMock()
    msg_old.dtm = dt.now() - timedelta(seconds=1300)
    mock_device._msgs = {"0418": msg_old}
    assert sensor.available is False

    # Case C: Recent message -> Available
    msg_new = MagicMock()
    msg_new.dtm = dt.now() - timedelta(seconds=100)
    mock_device._msgs = {"0418": msg_new}
    assert sensor.available is True


async def test_logbook_binary_sensor_state(mock_broker: MagicMock) -> None:
    """Test RamsesLogbookBinarySensor state based on faults.

    :param mock_broker: The mock broker fixture.
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

    sensor: Any = RamsesLogbookBinarySensor(mock_broker, mock_device, description)

    # 1. Test is_on = False (No faults)
    mock_device.active_faults = []

    # Assign to a variable first. This satisfies Ruff
    initial_state = sensor.is_on
    assert initial_state is False

    # 2. Test is_on = True (Faults present)
    mock_device.active_faults = ["fault"]
    assert sensor.is_on is True

    # 2. Test is_on = True (Faults present)
    mock_device.active_faults = ["fault"]
    assert sensor.is_on is True


async def test_system_binary_sensor_availability(mock_broker: MagicMock) -> None:
    """Test RamsesSystemBinarySensor availability calculation.

    :param mock_broker: The mock broker fixture.
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

    sensor: Any = RamsesSystemBinarySensor(mock_broker, mock_device, description)

    # 1. Case A: No message -> Not available
    mock_device._msgs = {}

    # Assign to variable to prevent Mypy from narrowing sensor.available to Literal[False]
    avail_a = sensor.available
    assert avail_a is False

    # 2. Case B: Message present -> Available
    # timeout = 100 * 3 = 300s. Message is now (0s age). 0 < 300 -> True
    msg = MagicMock()
    msg.dtm = dt.now()
    msg.payload = {"remaining_seconds": 100}
    mock_device._msgs = {"1F09": msg}

    avail_b = sensor.available
    assert avail_b is True

    # 3. Case C: Message expired -> Not available
    msg.dtm = dt.now() - timedelta(seconds=400)

    avail_c = sensor.available
    assert avail_c is False


async def test_system_binary_sensor_state(mock_broker: MagicMock) -> None:
    """Test RamsesSystemBinarySensor state logic.

    :param mock_broker: The mock broker fixture.
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

    sensor: Any = RamsesSystemBinarySensor(mock_broker, mock_device, description)

    # is_on logic: Inverse of super().is_on which returns ID/True
    # super().is_on returns getattr(id) -> "01:123456" -> Truthy
    # sensor.is_on -> not Truthy -> False
    assert sensor.is_on is False


async def test_gateway_binary_sensor_attributes(mock_broker: MagicMock) -> None:
    """Test RamsesGatewayBinarySensor extra state attributes.

    :param mock_broker: The mock broker fixture.
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

    # Mock internal structure for extra_state_attributes
    gwy = MagicMock()
    mock_device._gwy = gwy
    gwy.tcs = MagicMock()
    gwy.tcs.id = "01:111"
    gwy.tcs._schema_min = {"zon": "val"}
    gwy._enforce_known_list = True
    gwy.known_list = {"10:1": {"alias": "test", "class": "RAD", "faked": True}}
    gwy._exclude = {}
    gwy._transport.get_extra_info.return_value = True  # SZ_IS_EVOFW3

    sensor: Any = RamsesGatewayBinarySensor(mock_broker, mock_device, description)

    # 1. Extra State Attributes
    attrs: dict[str, Any] = sensor.extra_state_attributes
    assert attrs["schema"] == {"01:111": {"zon": "val"}}
    assert attrs["config"]["enforce_known_list"] is True
    assert attrs[SZ_IS_EVOFW3] is True

    # Check 'shrink' logic in known_list
    # The shrink function removes nulls and non-whitelisted keys
    known = attrs["known_list"][0]["10:1"]
    assert known["alias"] == "test"
    assert known["class"] == "RAD"
    assert known["faked"] is True


async def test_gateway_binary_sensor_state(mock_broker: MagicMock) -> None:
    """Test RamsesGatewayBinarySensor is_on state logic.

    :param mock_broker: The mock broker fixture.
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

    sensor: Any = RamsesGatewayBinarySensor(mock_broker, mock_device, description)

    # 1. Case A: Recent message -> is_on False (Problem = False -> OK)
    msg = MagicMock()
    msg.dtm = dt.now()
    gwy._this_msg = msg

    # Assign to variable to prevent Mypy narrowing sensor.is_on to Literal[False]
    is_on_check_a = sensor.is_on
    assert is_on_check_a is False

    # 2. Case B: Old message -> is_on True (Problem = True -> Fault)
    # Using same msg object, just changing dtm
    msg.dtm = dt.now() - timedelta(seconds=301)

    is_on_check_b = sensor.is_on
    assert is_on_check_b is True

    # 3. Case C: No message -> is_on True
    gwy._this_msg = None

    is_on_check_c = sensor.is_on
    assert is_on_check_c is True

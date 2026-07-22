# --- START OF FILE test_binary_sensor.py ---

"""Tests for the ramses_cc binary_sensor platform."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.binary_sensor import (
    SZ_BATTERY_LEVEL,
    SZ_BATTERY_LOW,
    SZ_BATTERY_STATE,
    RamsesBatteryBinarySensor,
    RamsesBinarySensor,
    RamsesBinarySensorEntityDescription,
    RamsesGatewayBinarySensor,
    RamsesLogbookBinarySensor,
    RamsesSystemBinarySensor,
    async_setup_entry,
)
from custom_components.ramses_cc.const import DOMAIN
from ramses_rf.devices import HgiGateway
from ramses_rf.systems.tcs import Logbook, System
from ramses_tx.const import SZ_IS_EVOFW3


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Return a mock RamsesCoordinator.

    :return: A mock object simulating the RamsesCoordinator.
    :rtype: MagicMock
    """
    coordinator = MagicMock()
    coordinator.async_register_platform = MagicMock()
    return coordinator


async def test_async_setup_entry(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test the platform setup and entity creation callback.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}

    mock_add_entities = MagicMock()
    mock_device = MagicMock(spec=HgiGateway)
    mock_device.id = "18:123456"

    # Act
    with patch("custom_components.ramses_cc.binary_sensor.entity_platform"):
        await async_setup_entry(hass, entry, mock_add_entities)

    add_callback = mock_coordinator.async_register_platform.call_args[0][1]
    add_callback([mock_device])
    created_entities = mock_add_entities.call_args[0][0]

    # Assert
    assert mock_coordinator.async_register_platform.called
    assert mock_add_entities.called
    assert len(created_entities) == 1
    assert isinstance(created_entities[0], RamsesGatewayBinarySensor)


@patch("custom_components.ramses_cc.binary_sensor.resolve_async_attr")
async def test_generic_binary_sensor(
    mock_resolve_async_attr: MagicMock, mock_coordinator: MagicMock
) -> None:
    """Test RamsesBinarySensor base class logic.

    :param mock_resolve_async_attr: Mock for the async resolver.
    :type mock_resolve_async_attr: MagicMock
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="test_sensor",
        ramses_rf_attr="test_attr",
        name="Test Sensor",
        icon="mdi:test",
        icon_off="mdi:test-off",
    )

    mock_device = MagicMock()
    mock_device.id = "01:123456"
    # Mock library availability delegation so the base RamsesEntity evaluates it correctly
    mock_device.is_available = True

    # Act
    sensor: RamsesBinarySensor = RamsesBinarySensor(
        mock_coordinator, mock_device, description
    )
    avail_state = sensor.available

    # Assert
    assert sensor.unique_id == "01:123456-test_sensor"
    # Assign to a variable first to satisfy Mypy
    assert avail_state is True

    # Act (is_on true)
    mock_resolve_async_attr.return_value = True
    state_1 = sensor.is_on
    icon_1 = sensor.icon

    # Assert
    assert state_1 is True
    assert icon_1 == "mdi:test"

    # Act (is_on false)
    mock_resolve_async_attr.return_value = False
    state_2 = sensor.is_on
    icon_2 = sensor.icon

    # Assert
    assert state_2 is False
    assert icon_2 == "mdi:test-off"

    # Act (Duck-Typing backwards compat)
    mock_resolve_async_attr.return_value = True
    state_3 = sensor.is_on

    # Assert
    assert state_3 is True


@patch("custom_components.ramses_cc.binary_sensor.resolve_async_attr")
async def test_battery_binary_sensor(
    mock_resolve_async_attr: MagicMock, mock_coordinator: MagicMock
) -> None:
    """Test RamsesBatteryBinarySensor.

    :param mock_resolve_async_attr: Mock for the async resolver.
    :type mock_resolve_async_attr: MagicMock
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="test_battery",
        ramses_rf_attr=SZ_BATTERY_LOW,
        name="Test Battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        ramses_cc_class=RamsesBatteryBinarySensor,
    )

    mock_device = MagicMock()
    mock_device.id = "04:123456"

    sensor: RamsesBatteryBinarySensor = RamsesBatteryBinarySensor(
        mock_coordinator, mock_device, description
    )

    # Helper to mock multiple async attribute resolutions on the same entity
    def mock_resolve_state(entity: Any, device: Any, attr: str) -> Any:
        if attr == SZ_BATTERY_STATE:
            return {
                SZ_BATTERY_LEVEL: 0.5,
                SZ_BATTERY_LOW: True,
            }
        if attr == SZ_BATTERY_LOW:
            return True
        return None

    # Act (Battery state present)
    # 1. Battery state present - Mocked via async resolve
    mock_resolve_async_attr.side_effect = mock_resolve_state
    state_1 = sensor.is_on
    attrs = sensor.extra_state_attributes

    # Assert
    assert state_1 is True
    assert attrs[SZ_BATTERY_LEVEL] == 0.5

    # Act (Battery state missing)
    # 2. Battery state missing
    mock_resolve_async_attr.side_effect = lambda e, d, a: None
    attrs_2 = sensor.extra_state_attributes

    # Assert
    assert attrs_2[SZ_BATTERY_LEVEL] == "N/A"


async def test_logbook_binary_sensor_availability(
    mock_coordinator: MagicMock,
) -> None:
    """Test RamsesLogbookBinarySensor availability delegates to device.

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="active_fault",
        name="Active fault",
        ramses_rf_attr="active_faults",
        ramses_cc_class=RamsesLogbookBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=Logbook)
    mock_device.id = "01:123456"
    mock_device.state_store = MagicMock()

    sensor: RamsesLogbookBinarySensor = RamsesLogbookBinarySensor(
        mock_coordinator, mock_device, description
    )

    # Act (Case A: Device unavailable)
    # Case A: Device is not available (Delegated to library's is_available property)
    mock_device.is_available = False

    # Assert
    assert sensor.available is False

    # Act (Case B: Device available)
    # Case B: Device is available (Delegated to library's is_available property)
    mock_device.is_available = True

    # Assert
    assert sensor.available is True


@patch("custom_components.ramses_cc.binary_sensor.resolve_async_attr")
async def test_logbook_binary_sensor_state(
    mock_resolve_async_attr: MagicMock, mock_coordinator: MagicMock
) -> None:
    """Test RamsesLogbookBinarySensor state based on faults.

    :param mock_resolve_async_attr: Mock for the async resolver.
    :type mock_resolve_async_attr: MagicMock
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="active_fault",
        name="Active fault",
        ramses_rf_attr="active_faults",
        ramses_cc_class=RamsesLogbookBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=Logbook)
    mock_device.id = "01:123456"

    sensor: RamsesLogbookBinarySensor = RamsesLogbookBinarySensor(
        mock_coordinator, mock_device, description
    )

    # Act (is_on = False)
    # 1. Test is_on = False (No faults)
    mock_resolve_async_attr.return_value = []
    initial_state = sensor.is_on

    # Assert
    assert initial_state is False

    # Act (is_on = True)
    # 2. Test is_on = True (Has faults)
    mock_resolve_async_attr.return_value = [{"fault": "error"}]
    final_state = sensor.is_on

    # Assert
    assert final_state is True

    # Act (is_on = False, issue 841)
    # 3. Test is_on = False when active_faults is None (empty/unloaded log)
    #    must report off, not unknown (regression vs 0.54.3)
    mock_resolve_async_attr.return_value = None
    none_state = sensor.is_on

    # Assert
    assert none_state is False


async def test_system_binary_sensor_availability(
    mock_coordinator: MagicMock,
) -> None:
    """Test RamsesSystemBinarySensor availability delegates to device.

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="id",
        name="System status",
        ramses_cc_class=RamsesSystemBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=System)
    mock_device.id = "01:123456"
    mock_device.state_store = MagicMock()

    sensor: RamsesSystemBinarySensor = RamsesSystemBinarySensor(
        mock_coordinator, mock_device, description
    )

    # Act & Assert (Case A: Device unavailable)
    # Case A: Device is not available (Delegated to library's is_available property)
    mock_device.is_available = False
    assert sensor.available is False

    # Act & Assert (Case B: Device available)
    # Case B: Device is available (Delegated to library's is_available property)
    mock_device.is_available = True
    assert sensor.available is True


@patch("custom_components.ramses_cc.binary_sensor.resolve_async_attr")
async def test_gateway_binary_sensor_attrs(
    mock_resolve_async_attr: MagicMock, mock_coordinator: MagicMock
) -> None:
    """Test RamsesGatewayBinarySensor attribute caching and async schema resolution.

    :param mock_resolve_async_attr: Mock for the attribute resolver helper.
    :type mock_resolve_async_attr: MagicMock
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="is_active",
        name="Gateway status",
        ramses_cc_class=RamsesGatewayBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=HgiGateway)
    mock_device.id = "18:123456"

    # Set up the gateway mock to match the expected structure
    gwy = MagicMock()
    gwy.tcs.id = "01:111111"

    # Mock the resolve_async_attr helper to return a safe schema dict
    mock_resolve_async_attr.return_value = {"system_schema": "test"}

    # Mock the Phase 2.77 configuration facade
    gwy.config = MagicMock()
    gwy.config.known_list = {"10:1": {"alias": "test", "class": "RAD", "faked": True}}

    gwy._engine = MagicMock()
    gwy._engine._enforce_known_list = True
    gwy._engine._exclude = {}
    gwy._engine._transport.get_extra_info.return_value = True

    mock_device._gwy = gwy

    sensor: RamsesGatewayBinarySensor = RamsesGatewayBinarySensor(
        mock_coordinator, mock_device, description
    )

    # Act
    # Fetch attributes (should cache and utilize the mocked async helper)
    attrs = sensor.extra_state_attributes

    # Assert
    mock_resolve_async_attr.assert_called_once_with(sensor, gwy.tcs, "_schema_min")

    assert attrs["config"]["enforce_known_list"] is True
    assert "01:111111" in attrs["schema"]
    assert attrs["schema"]["01:111111"] == {"system_schema": "test"}
    assert attrs[SZ_IS_EVOFW3] is True

    # Verify filtering/shrinking of known_list
    known = attrs["known_list"][0]["10:1"]
    assert known["alias"] == "test"
    assert known["class"] == "RAD"
    assert known["faked"] is True


async def test_gateway_binary_sensor_state(
    mock_coordinator: MagicMock,
) -> None:
    """Test RamsesGatewayBinarySensor is_on state logic.

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="status",
        ramses_rf_attr="is_active",
        name="Gateway status",
        ramses_cc_class=RamsesGatewayBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    mock_device = MagicMock(spec=HgiGateway)
    mock_device.id = "18:123456"

    sensor: RamsesGatewayBinarySensor = RamsesGatewayBinarySensor(
        mock_coordinator, mock_device, description
    )

    # Act & Assert
    # 1. Case A: Gateway active (OK) -> is_on False (no problem)
    mock_device.is_active = True
    is_on_check_a = sensor.is_on
    assert is_on_check_a is False

    # 2. Case B: Gateway inactive (dead) -> is_on True (problem)
    mock_device.is_active = False
    is_on_check_b = sensor.is_on
    assert is_on_check_b is True


@patch("custom_components.ramses_cc.binary_sensor.CommandDTO")
@patch("custom_components.ramses_cc.binary_sensor.resolve_async_attr")
async def test_logbook_async_added_to_hass(
    mock_resolve: MagicMock,
    mock_cmd: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Test RamsesLogbookBinarySensor.async_added_to_hass polling logic."""
    # Arrange
    description = RamsesBinarySensorEntityDescription(
        key="active_fault",
        name="Active fault",
        ramses_rf_attr="active_faults",
        ramses_cc_class=RamsesLogbookBinarySensor,
        device_class=BinarySensorDeviceClass.PROBLEM,
    )
    mock_device = MagicMock(spec=Logbook)
    mock_device.id = "01:123456"
    mock_device._tcs = MagicMock()
    mock_device._tcs.id = "01:123456"
    mock_device._tcs.get_faultlog = AsyncMock()

    sensor: RamsesLogbookBinarySensor = RamsesLogbookBinarySensor(
        mock_coordinator, mock_device, description
    )

    # Act (active_faults None)
    # 1. active_faults is None, tcs has get_faultlog
    mock_resolve.return_value = None
    with patch(
        "custom_components.ramses_cc.binary_sensor."
        "RamsesBinarySensor.async_added_to_hass"
    ):
        await sensor.async_added_to_hass()

    # Assert
    mock_device._tcs.get_faultlog.assert_awaited_once_with(limit=1, force_refresh=True)

    # Arrange (Missing get_faultlog)
    # 2. active_faults is None, tcs lacks get_faultlog but has _gwy
    del mock_device._tcs.get_faultlog
    mock_device._tcs._gwy = MagicMock()
    mock_device._tcs._gwy.async_send_cmd = AsyncMock()
    mock_cmd.return_value = "mock_cmd"

    # Act
    with patch(
        "custom_components.ramses_cc.binary_sensor."
        "RamsesBinarySensor.async_added_to_hass"
    ):
        await sensor.async_added_to_hass()

    # Assert
    mock_cmd.assert_called_once_with(
        verb="RQ",
        addr1="18:000730",
        addr2="01:123456",
        addr3="--:------",
        code="0418",
        payload="00",
    )
    mock_device._tcs._gwy.async_send_cmd.assert_awaited_once_with("mock_cmd")

    # Act (Exception handling)
    # 3. Exception handling
    mock_device._tcs._gwy.async_send_cmd.side_effect = Exception("Boom")
    with patch(
        "custom_components.ramses_cc.binary_sensor."
        "RamsesBinarySensor.async_added_to_hass"
    ):
        # Should not raise
        await sensor.async_added_to_hass()

    # Arrange (active_faults present)
    # 4. active_faults is not None (should not poll)
    mock_resolve.return_value = [{"fault": "error"}]
    mock_cmd.reset_mock()

    # Act
    with patch(
        "custom_components.ramses_cc.binary_sensor."
        "RamsesBinarySensor.async_added_to_hass"
    ):
        await sensor.async_added_to_hass()

    # Assert
    mock_cmd.assert_not_called()

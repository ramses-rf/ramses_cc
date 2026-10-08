"""Tests for type definitions, protocols, and type guards in typing.py."""

from __future__ import annotations

from typing import Any

from custom_components.ramses_cc.typing import (
    FakedSensorDevice,
    FanParamDevice,
    FanParamEventData,
    HasSensorProperty,
    PendingEntity,
    RamsesConfigData,
    StoreStateDict,
    is_faked_sensor_device,
    is_fan_param_device,
    is_pending_entity,
)


class MockFanDevice:
    """Mock implementing FanParamDevice protocol."""

    def __init__(self, dev_id: str = "32:123456") -> None:
        self.id = dev_id

    def get_fan_param(self, param_id: str) -> Any:
        return 100

    def clear_fan_param(self, param_id: str) -> None:
        pass

    def set_initialized_callback(self, cb: Any) -> None:
        pass

    def set_param_update_callback(self, cb: Any) -> None:
        pass


class MockPendingEntity:
    """Mock implementing PendingEntity protocol."""

    def __init__(self) -> None:
        self.pending_timer = None

    def set_pending(self) -> None:
        pass

    def _clear_pending_after_timeout(self, timeout: int) -> Any:
        pass


class MockFakedSensorDevice:
    """Mock implementing FakedSensorDevice protocol."""

    def __init__(self) -> None:
        self.co2_level: float | None = 450.0
        self.indoor_humidity: float | None = 55.0

    async def set_temperature(self, temperature: float) -> None:
        pass


class MockSensorContainer:
    """Mock implementing HasSensorProperty protocol."""

    @property
    def sensor(self) -> Any:
        return "mock_sensor"


class NonConformingObject:
    """Object not conforming to any protocol."""

    pass


def test_is_fan_param_device() -> None:
    # Arrange
    matching = MockFanDevice()
    non_matching = NonConformingObject()

    # Act & Assert
    assert is_fan_param_device(matching) is True
    assert is_fan_param_device(non_matching) is False
    assert is_fan_param_device(None) is False
    assert isinstance(matching, FanParamDevice)


def test_is_pending_entity() -> None:
    # Arrange
    matching = MockPendingEntity()
    non_matching = NonConformingObject()

    # Act & Assert
    assert is_pending_entity(matching) is True
    assert is_pending_entity(non_matching) is False
    assert is_pending_entity(None) is False
    assert isinstance(matching, PendingEntity)


def test_is_faked_sensor_device() -> None:
    # Arrange
    matching = MockFakedSensorDevice()
    non_matching = NonConformingObject()

    # Act & Assert
    assert is_faked_sensor_device(matching) is True
    assert is_faked_sensor_device(non_matching) is False
    assert is_faked_sensor_device(None) is False
    assert isinstance(matching, FakedSensorDevice)


def test_has_sensor_property_protocol() -> None:
    # Arrange
    matching = MockSensorContainer()
    non_matching = NonConformingObject()

    # Act & Assert
    assert isinstance(matching, HasSensorProperty)
    assert not isinstance(non_matching, HasSensorProperty)


def test_typed_dicts() -> None:
    # Arrange & Act
    event_data: FanParamEventData = {
        "device_id": "32:123456",
        "param_id": "speed",
        "value": 2,
    }
    store_state: StoreStateDict = {
        "schema": {},
        "packets": ["045  I --- 01:123456 ..."],
    }
    config_data: RamsesConfigData = {
        "schema": {},
        "advanced_features": {},
        "ramses_rf": {},
    }

    # Assert
    assert event_data["device_id"] == "32:123456"
    assert store_state["packets"] == ["045  I --- 01:123456 ..."]
    assert "schema" in config_data

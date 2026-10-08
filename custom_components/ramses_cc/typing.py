"""Type definitions, protocols, and type guards for ramses_cc.

This module provides structural typing protocols, type guard functions, and
TypedDict schemas to enforce strict type safety across the integration.
"""

from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    TypedDict,
    TypeGuard,
    runtime_checkable,
)

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

type RamsesConfigEntry = ConfigEntry[RamsesCoordinator]


@runtime_checkable
class FanParamDevice(Protocol):
    """Protocol for RAMSES devices that support fan parameters."""

    id: str

    def get_fan_param(self, param_id: str) -> Any:
        """Get the value of a fan parameter.

        :param param_id: The identifier of the parameter.
        :type param_id: str
        :returns: The parameter value or None.
        :rtype: Any
        """
        ...

    def clear_fan_param(self, param_id: str) -> None:
        """Clear a stored fan parameter.

        :param param_id: The identifier of the parameter.
        :type param_id: str
        :returns: None
        :rtype: None
        """
        ...

    def set_initialized_callback(self, cb: Callable[[], Any]) -> None:
        """Register a callback for when the device finishes initialization.

        :param cb: The callback function to invoke.
        :type cb: Callable[[], Any]
        :returns: None
        :rtype: None
        """
        ...

    def set_param_update_callback(
        self, cb: Callable[[str, Any], None]
    ) -> None:
        """Register a callback for parameter updates.

        :param cb: The callback function to invoke on parameter update.
        :type cb: Callable[[str, Any], None]
        :returns: None
        :rtype: None
        """
        ...


@runtime_checkable
class PendingEntity(Protocol):
    """Protocol for entities supporting pending state management."""

    pending_timer: Any

    def set_pending(self) -> None:
        """Mark the entity as having a pending state update.

        :returns: None
        :rtype: None
        """
        ...

    def _clear_pending_after_timeout(self, timeout: int) -> Any:
        """Clear pending state after a timeout period.

        :param timeout: The timeout duration in seconds.
        :type timeout: int
        :returns: Coroutine or None.
        :rtype: Any
        """
        ...


@runtime_checkable
class FakedSensorDevice(Protocol):
    """Protocol for devices supporting faked temperature or sensor values."""

    co2_level: float | None
    indoor_humidity: float | None

    async def set_temperature(self, temperature: float) -> None:
        """Set a faked temperature value on the device.

        :param temperature: The target temperature in Celsius.
        :type temperature: float
        :returns: None
        :rtype: None
        """
        ...


@runtime_checkable
class HasSensorProperty(Protocol):
    """Protocol for objects exposing a sensor child property."""

    @property
    def sensor(self) -> Any:
        """The child sensor property.

        :returns: The sensor instance or None.
        :rtype: Any
        """
        ...


class FanParamEventData(TypedDict):
    """Event data payload for fan parameter update events."""

    device_id: str
    param_id: str
    value: Any


class StoreStateDict(TypedDict, total=False):
    """Schema for persistent client state storage."""

    schema: dict[str, Any]
    packets: list[str]
    remotes: dict[str, Any]
    discovery_state: dict[str, Any]


class RamsesConfigData(TypedDict, total=False):
    """Schema for Ramses config entry data and options."""

    schema: dict[str, Any]
    advanced_features: dict[str, Any]
    ramses_rf: dict[str, Any]


def is_fan_param_device(obj: Any) -> TypeGuard[FanParamDevice]:
    """Type guard to check if an object satisfies the FanParamDevice protocol.

    :param obj: The object instance to check.
    :type obj: Any
    :returns: True if the object implements FanParamDevice.
    :rtype: bool
    """
    return isinstance(obj, FanParamDevice)


def is_pending_entity(obj: Any) -> TypeGuard[PendingEntity]:
    """Type guard to check if an object satisfies the PendingEntity protocol.

    :param obj: The object instance to check.
    :type obj: Any
    :returns: True if the object implements PendingEntity.
    :rtype: bool
    """
    return isinstance(obj, PendingEntity)


def is_faked_sensor_device(obj: Any) -> TypeGuard[FakedSensorDevice]:
    """Type guard to check if an object satisfies FakedSensorDevice.

    :param obj: The object instance to check.
    :type obj: Any
    :returns: True if the object implements FakedSensorDevice.
    :rtype: bool
    """
    return isinstance(obj, FakedSensorDevice)

"""RAMSES RF - HVAC Sensor Devices."""

from __future__ import annotations

from datetime import timedelta as td
from typing import TYPE_CHECKING, Any

from .. import exceptions as exc
from ..const import (
    HEARTBEAT_TIMEOUT_SENSOR,
    SZ_CO2_LEVEL,
    SZ_INDOOR_HUMIDITY,
    SZ_PRESENCE_DETECTED,
    SZ_TEMPERATURE,
    Code,
    DevType,
)
from ..models import DeviceTraits, HvacState
from ...ramses_tx import Command, Packet, Priority

from .dev_base import BatteryState, DeviceHvac, Fakeable

if TYPE_CHECKING:
    from ..messages import Message


class HvacSensorBase(DeviceHvac):
    """Base class for HVAC sensor devices.

    This class serves as a base for all sensor devices in the HVAC domain.
    It provides common functionality for sensor data collection and processing.
    """

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the HvacSensorBase class.

        :param args: Positional arguments passed to the parent class
        :param traits: Strictly typed traits object for device creation
        :param kwargs: Keyword arguments passed to the parent class
        """
        super().__init__(*args, traits=traits, **kwargs)
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    def _post_class_promote(self) -> None:
        """Initialize state when promoted from a generic HVAC device."""
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout before the device is considered unavailable.

        :return: The timeout duration.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_SENSOR


class CarbonDioxide(HvacSensorBase):  # 1298
    """The CO2 sensor (cardinal code is 1298)."""

    async def co2_level(self) -> int | None:
        """Get the CO2 level in ppm.

        :return: The CO2 level in parts per million (ppm), or None if not available
        :rtype: int | None
        """
        return self.hvac_state.co2_level

    async def set_co2_level(self, value: int | None) -> Packet | None:
        """Set a fake CO2 level for the sensor.

        :param value: The CO2 level in ppm to set, or None to clear the fake value
        :type value: int | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent packet
        :rtype: Packet | None
        """

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_co2_level(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def status(self) -> dict[str, Any]:
        """Return the status of the CO2 sensor.

        :return: A dictionary containing the sensor's status including CO2 level
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_CO2_LEVEL: await self.co2_level(),
        }


class IndoorHumidity(HvacSensorBase):  # 12A0
    """The relative humidity sensor (12A0)."""

    async def indoor_humidity(self) -> float | None:
        """Get the indoor relative humidity.

        :return: The indoor relative humidity as a percentage (0-100), or None if not available
        :rtype: float | None
        """
        return self.hvac_state.indoor_humidity

    async def set_indoor_humidity(self, value: float | None) -> Packet | None:
        """Set a fake indoor humidity value for the sensor.

        :param value: The humidity percentage to set (0-100), or None to clear the fake value
        :type value: float | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent packet
        :rtype: Packet | None
        """

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_indoor_humidity(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def status(self) -> dict[str, Any]:
        """Return the status of the indoor humidity sensor.

        :return: A dictionary containing the sensor's status including humidity level
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_INDOOR_HUMIDITY: await self.indoor_humidity(),
        }


class PresenceDetect(HvacSensorBase):  # 2E10
    """The presence sensor (2E10/31E0)."""

    # .I --- 37:154011 --:------ 37:154011 1FC9 030 00-31E0-96599B 00-1298-96599B 00-2E10-96599B 01-10E0-96599B 00-1FC9-96599B    # CO2, idx|10E0 == 01
    # .W --- 28:126620 37:154011 --:------ 1FC9 012 00-31D9-49EE9C 00-31DA-49EE9C                                                 # FAN, BRDG-02A55
    # .I --- 37:154011 28:126620 --:------ 1FC9 001 00                                                                            # CO2, incl. integrated control, PIR

    async def presence_detected(self) -> bool | None:
        """Get the presence detection status.

        :return: True if presence is detected, False if not, None if status is unknown
        :rtype: bool | None
        """
        return self.hvac_state.presence_detected

    async def set_presence_detected(self, value: bool | None) -> Packet | None:
        """Set a fake presence detection state for the sensor.

        :param value: The presence state to set (True/False), or None to clear the fake value
        :type value: bool | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent packet
        :rtype: Packet | None
        """

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_presence_detected(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def status(self) -> dict[str, Any]:
        """Return the status of the presence sensor.

        :return: A dictionary containing the sensor's status including presence detection state
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_PRESENCE_DETECTED: await self.presence_detected(),
        }


class HvacHumiditySensor(BatteryState, IndoorHumidity, Fakeable):  # HUM: I/12A0
    """The class for a humidity sensor.

    The cardinal code is 12A0.
    """

    _SLUG: str = DevType.HUM

    async def temperature(self) -> float | None:
        """Return the current temperature in Celsius.

        :return: The temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.temperature

    async def dewpoint_temp(self) -> float | None:
        """Return the dewpoint temperature in Celsius.

        :return: The dewpoint temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.dewpoint_temp

    async def status(self) -> dict[str, Any]:
        """Return the status of the humidity sensor.

        :return: A dictionary containing the sensor's status including temperature and humidity
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_TEMPERATURE: await self.temperature(),
            "dewpoint_temp": await self.dewpoint_temp(),
        }


class HvacCarbonDioxideSensor(CarbonDioxide, Fakeable):  # CO2: I/1298
    """The class for a CO2 sensor.

    The cardinal code is 1298.
    """

    _SLUG: str = DevType.CO2

    # .I --- 29:181813 63:262142 --:------ 1FC9 030 00-31E0-76C635 01-31E0-76C635 00-1298-76C635 67-10E0-76C635 00-1FC9-76C635
    # .W --- 32:155617 29:181813 --:------ 1FC9 012 00-31D9-825FE1 00-31DA-825FE1  # The HRU
    # .I --- 29:181813 32:155617 --:------ 1FC9 001 00

    async def initiate_binding_process(
        self,
    ) -> tuple[Packet, Message, Packet, Packet | None]:
        """Initiate the binding process for the CO2 sensor.

        :return: The packet sent to initiate binding
        :rtype: tuple[Packet, Message, Packet, Packet | None]
        :raises exc.BindingError: If binding fails
        """
        return await super()._initiate_binding_process(
            (Code._31E0, Code._1298, Code._2E10)
        )

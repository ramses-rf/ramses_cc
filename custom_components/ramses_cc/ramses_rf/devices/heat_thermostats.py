"""RAMSES RF - Heating Thermostat & Setpoint Devices."""

from __future__ import annotations

from datetime import timedelta as td
from typing import TYPE_CHECKING, Any, Final

from ..const import (
    HEARTBEAT_TIMEOUT_TRV,
    SZ_ZONE_DEMAND,
    SZ_SETPOINT,
    SZ_WINDOW_OPEN,
    Code,
    DevType,
)
from ..models import DeviceTraits, TrvState
from ...ramses_tx import Packet

from .dev_base import BatteryState, Fakeable
from .heat_actuators import HeatDemand
from .heat_sensors import Temperature

if TYPE_CHECKING:
    from ..messages import Message


class Setpoint(Temperature):  # 2309
    SETPOINT: Final = SZ_SETPOINT  # degrees Celsius

    async def setpoint(self) -> float | None:  # 2309
        return self.temp_state.setpoint

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.SETPOINT: await self.setpoint(),
        }


class Thermostat(BatteryState, Setpoint, Fakeable):  # THM (..):
    """The THM/STA class, such as a TR87RF."""

    _SLUG = DevType.THM
    _STATE_ATTR = "temperature"

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

    async def initiate_binding_process(
        self,
    ) -> tuple[Packet, Message, Packet, Packet | None]:
        return await super()._initiate_binding_process(
            (Code._2309, Code._30C9, Code._0008)
        )


class TrvActuator(BatteryState, HeatDemand, Setpoint):  # TRV (04):
    """The TRV class, such as a HR92."""

    WINDOW_OPEN: Final = SZ_WINDOW_OPEN

    _SLUG = DevType.TRV
    _STATE_ATTR = SZ_ZONE_DEMAND

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)
        self.trv_state = TrvState()

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout before the device is considered unavailable.

        :return: The timeout duration.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_TRV

    async def zone_demand(self) -> float | None:  # 3150
        if (zone_demand := self.demand_state.zone_demand) is None:
            if await self.setpoint() is False:
                return 0  # instead of None (no 3150s sent when setpoint is False)
        return zone_demand

    async def window_open(self) -> bool | None:  # 12B0
        return self.trv_state.window_open

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.WINDOW_OPEN: await self.window_open(),
        }

"""RAMSES RF - Heating Sensor Devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from ..const import FA, SZ_TEMPERATURE, Code, DevType
from ..exceptions import DeviceNotFaked
from ..models import DeviceTraits
from ...ramses_tx import Command, Packet, Priority
from ...ramses_tx.typing import PayDictT

from .dev_base import BatteryState, DeviceHeat, Fakeable

if TYPE_CHECKING:
    from ..messages import Message


class Weather(DeviceHeat):  # 0002
    TEMPERATURE: Final = SZ_TEMPERATURE  # TODO: deprecate

    async def temperature(self) -> float | None:  # 0002
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Packet | None:
        """Fake the outdoor temperature of the sensor."""

        if not self.is_faked:
            raise DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_outdoor_temp(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.TEMPERATURE: await self.temperature(),
        }


class DhwTemperature(DeviceHeat):  # 1260
    TEMPERATURE: Final = SZ_TEMPERATURE  # TODO: deprecate

    async def temperature(self) -> float | None:  # 1260
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Packet | None:
        """Fake the DHW temperature of the sensor."""

        if not self.is_faked:
            raise DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_dhw_temp(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.TEMPERATURE: await self.temperature(),
        }


class Temperature(DeviceHeat):  # 30C9
    # .I --- 34:145039 --:------ 34:145039 1FC9 012 00-30C9-8A368F 00-1FC9-8A368F
    # .W --- 01:054173 34:145039 --:------ 1FC9 006 03-2309-04D39D  # real CTL
    # .I --- 34:145039 01:054173 --:------ 1FC9 006 00-30C9-8A368F
    async def temperature(self) -> float | None:  # 30C9
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Packet | None:
        """Fake the indoor temperature of the sensor."""

        if not self.is_faked:
            raise DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_sensor_temp(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            SZ_TEMPERATURE: await self.temperature(),
        }


class DhwSensor(DhwTemperature, BatteryState, Fakeable):  # DHW (07): 10A0, 1260
    """The DHW class, such as a CS92."""

    DHW_PARAMS: Final = "dhw_params"

    _SLUG: str = DevType.DHW
    _STATE_ATTR = DhwTemperature.TEMPERATURE

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

        self._child_id = FA  # NOTE: domain_id

    def _post_class_promote(self) -> None:
        """Initialize DHW state when promoted in-place from a generic device."""
        self.__dict__.setdefault("_child_id", FA)

    def _handle_msg(self, msg: Message) -> None:  # NOTE: active
        super()._handle_msg(msg)

        if getattr(self._gwy.config, "disable_discovery", False):
            return

        # TODO: why are we doing this here? Should simply use dscovery poller!
        # The following is required, as CTLs don't send spontaneously
        if msg.code == Code._1260 and getattr(self, "ctl", None):
            # update the controller DHW temp
            self._send_cmd(Command.get_dhw_temp(self.ctl.id))  # type: ignore[union-attr]

    async def initiate_binding_process(
        self,
    ) -> tuple[Packet, Message, Packet, Packet | None]:
        return await super()._initiate_binding_process(Code._1260)

    async def dhw_params(self) -> PayDictT._10A0 | None:
        return cast(
            PayDictT._10A0 | None, await self.entity_state.get_value(Code._10A0)
        )

    async def params(self) -> dict[str, Any]:
        base_params = await super().params()
        return {
            **base_params,
            self.DHW_PARAMS: await self.dhw_params(),
        }


class OutSensor(Weather, Fakeable):  # OUT: 17
    """The OUT class (external sensor), such as a HB85/HB95."""

    # LUMINOSITY = "luminosity"  # lux
    # WINDSPEED = "windspeed"  # km/h

    _SLUG = DevType.OUT
    _STATE_ATTR = SZ_TEMPERATURE

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

    # async def initiate_binding_process(self) -> tuple[Packet, Message, Packet, Packet | None]:
    #     return await super()._initiate_binding_process(...)

"""RAMSES RF - Heating Actuator Devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from ..const import (
    DOMAIN_TYPE_MAP,
    F9,
    FA,
    FC,
    I_,
    RQ,
    SZ_ZONE_DEMAND,
    SZ_RELAY_DEMAND,
    Code,
    DevType,
)
from ..models import DeviceTraits
from ...ramses_tx import Command, Priority
from ...ramses_tx.const import SZ_PRIORITY
from ...ramses_tx.typing import PayDictT, PayloadT

from .dev_base import DeviceHeat

if TYPE_CHECKING:
    from ..messages import Message

QOS_LOW = {SZ_PRIORITY: Priority.LOW}  # FIXME:  deprecate QoS in kwargs


class Actuator(DeviceHeat):  # 3EF0, 3EF1 (for 10:/13:)
    # .I --- 13:109598 --:------ 13:109598 3EF0 003 00C8FF                # event-driven, 00/C8
    # RP --- 13:109598 18:002563 --:------ 0008 002 00C8                  # 00/C8, as above
    # RP --- 13:109598 18:002563 --:------ 3EF1 007 0000BF-00BFC8FF       # 00/C8, as above

    # RP --- 10:048122 18:140805 --:------ 3EF1 007 007FFF-003C2A10       # 10:s only RP, always 7FFF
    # RP --- 13:109598 18:199952 --:------ 3EF1 007 0001B8-01B800FF       # 13:s only RP

    # RP --- 10:047707 18:199952 --:------ 3EF0 009 001110-0A00FF-033100  # 10:s only RP
    # RP --- 10:138926 34:010253 --:------ 3EF0 006 002E11-0000FF         # 10:s only RP
    # .I --- 13:209679 --:------ 13:209679 3EF0 003 00C8FF                # 13:s only  I

    ACTUATOR_CYCLE: Final = "actuator_cycle"
    ACTUATOR_ENABLED: Final = "actuator_enabled"  # boolean
    ACTUATOR_STATE: Final = "actuator_state"
    MODULATION_LEVEL: Final = "modulation_level"  # percentage (0.0-1.0)

    def _handle_msg(self, msg: Message) -> None:  # NOTE: active
        super()._handle_msg(msg)

        if getattr(self, "_SLUG", None) == DevType.OTB:
            return

        if getattr(self._gwy.config, "disable_discovery", False):
            return

        # TODO: why are we doing this here? Should simply use discovery poller!
        if msg.code == Code._3EF0 and msg.verb == I_ and not self.is_faked:
            # lf._send_cmd(Command.get_relay_demand(self.id), qos=QOS_LOW)
            self._send_cmd(
                Command.from_attrs(RQ, self.id, Code._3EF1, PayloadT("00")), **QOS_LOW
            )  # actuator cycle

    async def actuator_cycle(self) -> dict[str, Any] | None:  # 3EF1
        """Return the actuator cycle state.

        # TODO: Refactor for #714 (CQRS API Boundaries).
        # This is a legacy shim to maintain backward compatibility with ramses_cc.
        """
        state = getattr(self, "act_state", None)
        if not state:
            return None

        raw_dict = {
            "actuator_countdown": state.actuator_countdown,
            "cycle_countdown": state.cycle_countdown,
            "actuator_enabled": state.actuator_enabled,
            "modulation_level": state.modulation_level,
        }

        # Dynamically strip None values to mimic legacy optional keys
        clean_dict = {k: v for k, v in raw_dict.items() if v is not None}
        return clean_dict if clean_dict else None

    async def actuator_state(self) -> dict[str, Any] | None:  # 3EF0
        """Return the actuator modulation state.

        # TODO: Refactor for #714 (CQRS API Boundaries).
        # This is a legacy shim to maintain backward compatibility with ramses_cc.
        """
        state = getattr(self, "act_state", None)
        if not state:
            return None

        flame_status = (
            state.flame_on if state.flame_on is not None else state.flame_active
        )

        raw_dict = {
            "ch_active": state.ch_active,
            "ch_enabled": state.ch_enabled,
            "ch_setpoint": state.ch_setpoint,
            "cool_active": state.cool_active,
            "dhw_active": state.dhw_active,
            "flame_on": flame_status,
            "max_rel_modulation": state.max_rel_modulation,
            "modulation_level": state.modulation_level,
        }

        # Dynamically strip None values to mimic legacy optional keys
        clean_dict = {k: v for k, v in raw_dict.items() if v is not None}
        return clean_dict if clean_dict else None

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.ACTUATOR_CYCLE: await self.actuator_cycle(),
            self.ACTUATOR_STATE: await self.actuator_state(),
        }


class HeatDemand(DeviceHeat):  # 3150
    ZONE_DEMAND: Final = SZ_ZONE_DEMAND  # percentage valve open (0.0-1.0)

    async def zone_demand(self) -> float | None:  # 3150
        return self.demand_state.zone_demand

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.ZONE_DEMAND: await self.zone_demand(),
        }


class RelayDemand(DeviceHeat):  # 0008
    # .I --- 01:054173 --:------ 01:054173 1FC9 018 03-0008-04D39D FC-3B00-04D39D 03-1FC9-04D39D
    # .W --- 13:123456 01:054173 --:------ 1FC9 006 00-3EF0-35E240
    # .I --- 01:054173 13:123456 --:------ 1FC9 006 00-FFFF-04D39D

    # Some either 00/C8, others 00-C8
    # .I --- 01:145038 --:------ 01:145038 0008 002 0314  # ZON valve zone (ELE too?)
    # .I --- 01:145038 --:------ 01:145038 0008 002 F914  # HTG valve
    # .I --- 01:054173 --:------ 01:054173 0008 002 FA00  # DHW valve
    # .I --- 01:145038 --:------ 01:145038 0008 002 FC14  # appliance_relay

    # RP --- 13:109598 18:199952 --:------ 0008 002 0000
    # RP --- 13:109598 18:199952 --:------ 0008 002 00C8

    RELAY_DEMAND: Final = SZ_RELAY_DEMAND  # percentage (0.0-1.0)

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        if not self.is_faked:  # discover_flag & Discover.STATUS and
            self.discovery.add_cmd(Command.get_relay_demand(self.id), 60 * 15)

    async def relay_demand(self) -> float | None:  # 0008
        return self.demand_state.relay_demand

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.RELAY_DEMAND: await self.relay_demand(),
        }


class BdrSwitch(Actuator, RelayDemand):  # BDR (13):
    """The BDR class, such as a BDR91.

    BDR91s can be used in six distinct modes, including:

    - x2 boiler controller (FC/TPI): either traditional, or newer heat pump-aware
    - x1 electric heat zones (0x/ELE)
    - x1 zone valve zones (0x/VAL)
    - x2 DHW thingys (F9/DHW, FA/DHW)
    """

    ACTIVE: Final = "active"
    TPI_PARAMS: Final = "tpi_params"

    _SLUG = DevType.BDR
    _STATE_ATTR = "active"

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

    def _setup_discovery_cmds(self) -> None:
        """Discover BDRs.

        The BDRs have one of six roles:
         - heater relay *or* a heat pump relay (alternative to an OTB)
         - DHW hot water valve *or* DHW heating valve
         - Zones: Electric relay *or* Zone valve relay

        They all seem to respond thus (TODO: heat pump/zone valve relay):
         - all BDR91As will (erractically) RP to these RQs
             0016, 1FC9 & 0008, 1100, 3EF1
         - all BDR91As will *not* RP to these RQs
             0009, 10E0, 3B00, 3EF0
         - a BDR91A will *periodically* send an I/3B00/00C8 if it is the heater relay
        """
        super()._setup_discovery_cmds()

        if self.is_faked:
            return

        self.discovery.add_cmd(Command.get_tpi_params(self.id), 6 * 3600)  # params
        self.discovery.add_cmd(
            Command.from_attrs(RQ, self.id, Code._3EF1, PayloadT("00")),
            60 if getattr(self, "_child_id", None) in (F9, FA, FC) else 300,
        )  # status

    async def active(self) -> bool | None:  # 3EF0, 3EF1
        """Return the actuator's current state."""
        state = getattr(self, "act_state", None)
        if state and state.modulation_level is not None:
            return bool(state.modulation_level)
        return None

    async def relay_demand(self) -> float | None:
        """Return the relay demand of the BDR91."""
        if (demand := await super().relay_demand()) is not None:
            return demand
        state = getattr(self, "act_state", None)
        return state.modulation_level if state else None

    async def role(self) -> str | None:
        """Return the role of the BDR91A (there are six possibilities)."""

        # TODO: use self._parent?
        child_id = getattr(self, "_child_id", None)
        if child_id in DOMAIN_TYPE_MAP:
            return DOMAIN_TYPE_MAP[child_id]
        elif self._parent and type(self._parent).__name__ == "Zone":
            # TODO: remove need for string comparison hack
            return getattr(self._parent, "heating_type", None)

        # if Code._3B00 in _msgs and _msgs[Code._3B00].verb == I_:
        #     self._is_tpi = True
        # if Code._1FC9 in _msgs and _msgs[Code._1FC9].verb == RP:
        #     if Code._3B00 in _msgs[Code._1FC9].raw_payload:
        #         self._is_tpi = True

        return None

    async def tpi_params(self) -> PayDictT._10A0 | None:
        return cast(
            PayDictT._10A0 | None, await self.entity_state.get_value(Code._1100)
        )

    async def schema(self) -> dict[str, Any]:
        base_schema = await super().schema()
        return {
            **base_schema,
            "role": await self.role(),
        }

    async def params(self) -> dict[str, Any]:
        base_params = await super().params()
        return {
            **base_params,
            self.TPI_PARAMS: await self.tpi_params(),
        }

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.ACTIVE: await self.active(),
        }


class JimDevice(Actuator):  # BDR (08):
    _SLUG: str = DevType.JIM
    _STATE_ATTR: str | None = None

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)


class JstDevice(RelayDemand):  # BDR (31):
    _SLUG: str = DevType.JST
    _STATE_ATTR: str | None = None

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

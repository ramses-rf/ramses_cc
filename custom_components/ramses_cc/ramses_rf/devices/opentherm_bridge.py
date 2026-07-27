"""RAMSES RF - OpenTherm Bridge Device."""

from __future__ import annotations

import logging
from datetime import timedelta as td
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from ..const import FC, HEARTBEAT_TIMEOUT_OTB, I_, RP, RQ, Code, DevType
from ..models import DemandState, DeviceTraits, OpenThermState, TemperatureState
from ...ramses_tx import Command, Priority
from ...ramses_tx.const import (
    SZ_BOILER_OUTPUT_TEMP,
    SZ_BOILER_RETURN_TEMP,
    SZ_BOILER_SETPOINT,
    SZ_BURNER_FAILED_STARTS,
    SZ_BURNER_HOURS,
    SZ_BURNER_STARTS,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_CH_MAX_SETPOINT,
    SZ_CH_PUMP_HOURS,
    SZ_CH_PUMP_STARTS,
    SZ_CH_SETPOINT,
    SZ_CH_WATER_PRESSURE,
    SZ_COOLING_ACTIVE,
    SZ_COOLING_ENABLED,
    SZ_DHW_ACTIVE,
    SZ_DHW_BLOCKING,
    SZ_DHW_BURNER_HOURS,
    SZ_DHW_BURNER_STARTS,
    SZ_DHW_ENABLED,
    SZ_DHW_FLOW_RATE,
    SZ_DHW_PUMP_HOURS,
    SZ_DHW_PUMP_STARTS,
    SZ_DHW_SETPOINT,
    SZ_DHW_TEMP,
    SZ_FAULT_PRESENT,
    SZ_FLAME_ACTIVE,
    SZ_FLAME_SIGNAL_LOW,
    SZ_MAX_REL_MODULATION,
    SZ_NUM_REPEATS,
    SZ_OEM_CODE,
    SZ_OTC_ACTIVE,
    SZ_OUTSIDE_TEMP,
    SZ_PRIORITY,
    SZ_REL_MODULATION_LEVEL,
    SZ_SUMMER_MODE,
    MsgId,
)
from ...ramses_tx.typing import PayDictT, PayloadT

from ..protocol.opentherm import (
    PARAMS_DATA_IDS,
    SCHEMA_DATA_IDS,
    STATUS_DATA_IDS,
    SZ_MSG_ID,
    SZ_MSG_NAME,
    SZ_MSG_TYPE,
    SZ_VALUE,
    OtDataId,
    OtMsgType,
)
from .heat_actuators import Actuator, HeatDemand

if TYPE_CHECKING:
    from ..messages import Message

QOS_LOW = {SZ_PRIORITY: Priority.LOW}  # FIXME:  deprecate QoS in kwargs
QOS_MID = {SZ_PRIORITY: Priority.HIGH}  # FIXME: deprecate QoS in kwargs
QOS_MAX = {SZ_PRIORITY: Priority.HIGH, SZ_NUM_REPEATS: 3}  # FIXME: deprecate QoS...

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_ENABLE_DEPRECATION: Final[bool] = False
_DBG_EXTRA_OTB_DISCOVERY: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


def _to_msg_id(data_id: OtDataId) -> MsgId:
    return cast(MsgId, f"{data_id:02X}")


# NOTE: config.use_native_ot should enforce sends, but not reads from _msgz DB
class OtbGateway(Actuator, HeatDemand):  # OTB (10): 3220 (22D9, others)
    """The OTB class, specifically an OpenTherm Bridge (R8810A Bridge)."""

    # see: https://www.opentherm.eu/request-details/?post_ids=2944
    # see: https://www.automatedhome.co.uk/vbulletin/showthread.php?6400-(New)-cool-mode-in-Evohome

    _SLUG = DevType.OTB
    _STATE_ATTR = SZ_REL_MODULATION_LEVEL

    OT_TO_RAMSES: dict[MsgId, Code] = {  # TODO: move to opentherm.py
        MsgId._00: Code._3EF0,  # master/slave status (actuator_state)
        MsgId._01: Code._22D9,  # boiler_setpoint
        MsgId._0E: Code._3EF0,  # max_rel_modulation_level (is a PARAM?)
        MsgId._11: Code._3EF0,  # rel_modulation_level (actuator_state, also Code._3EF1)
        MsgId._12: Code._1300,  # ch_water_pressure
        MsgId._13: Code._12F0,  # dhw_flow_rate
        MsgId._19: Code._3200,  # boiler_output_temp
        MsgId._1A: Code._1260,  # dhw_temp
        MsgId._1B: Code._1290,  # outside_temp
        MsgId._1C: Code._3210,  # boiler_return_temp
        MsgId._38: Code._10A0,  # dhw_setpoint (is a PARAM)
        MsgId._39: Code._1081,  # ch_max_setpoint (is a PARAM)
    }
    RAMSES_TO_OT: dict[Code, MsgId] = {
        v: k for k, v in OT_TO_RAMSES.items() if v != Code._3EF0
    }  # also 10A0?

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the OpenTherm Bridge device software twin.

        :param args: Positional arguments passed to base class.
        :param traits: Strictly typed traits definition object.
        :type traits: DeviceTraits | None
        :param kwargs: Keyword arguments passed to base class.
        """
        super().__init__(*args, traits=traits, **kwargs)
        self.opentherm_state = OpenThermState()

        self._child_id = FC  # NOTE: domain_id
        # TODO[F4-PR2]: Purge legacy OpenTherm write-model tracking.
        self._msgs_ot: dict[MsgId, Message] = {}

    def _post_class_promote(self) -> None:
        """Initialize OTB state when promoted in-place from a generic device."""
        self.__dict__.setdefault("_child_id", FC)
        # TODO[F4-PR2]: Purge legacy OpenTherm write-model tracking.
        self.__dict__.setdefault("_msgs_ot", {})

        if not hasattr(self, "temp_state"):
            self.temp_state = TemperatureState()
        if not hasattr(self, "demand_state"):
            self.demand_state = DemandState()
        if not hasattr(self, "opentherm_state"):
            self.opentherm_state = OpenThermState()

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout before the device is considered unavailable.

        :return: The timeout duration.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_OTB

    def _setup_discovery_cmds(self) -> None:
        def which_cmd(
            use_native_ot: Literal["always", "prefer", "avoid", "never"] | str | None,
            msg_id: MsgId,
        ) -> Command | None:
            """Create a OT cmd, or its RAMSES equivalent, depending."""
            # we know RQ|3220 is an option, question is: use that, or RAMSES or nothing?
            if use_native_ot in ("always", "prefer"):
                return Command.get_opentherm_data(self.id, msg_id)
            if msg_id in self.OT_TO_RAMSES:  # is: in ("avoid", "never")
                return Command.from_attrs(
                    RQ, self.id, self.OT_TO_RAMSES[msg_id], PayloadT("00")
                )
            if use_native_ot == "avoid":
                return Command.get_opentherm_data(self.id, msg_id)
            return None  # use_native_ot == "never"

        super()._setup_discovery_cmds()

        # always send at least one of RQ|3EF0 or RQ|3220|00 (status)
        if getattr(self._gwy.config, "use_native_ot", "avoid") != "never":
            self.discovery.add_cmd(Command.get_opentherm_data(self.id, MsgId._00), 60)

        if getattr(self._gwy.config, "use_native_ot", "avoid") != "always":
            self.discovery.add_cmd(
                Command.from_attrs(RQ, self.id, Code._3EF0, PayloadT("00")), 60
            )
            self.discovery.add_cmd(  # NOTE: this code is a WIP
                Command.from_attrs(RQ, self.id, Code._2401, PayloadT("00")), 60
            )

        for data_id in SCHEMA_DATA_IDS:  # From OT v2.2: version numbers
            if cmd := which_cmd(
                getattr(self._gwy.config, "use_native_ot", "avoid"), _to_msg_id(data_id)
            ):
                self.discovery.add_cmd(cmd, 6 * 3600, delay=180)

        for data_id in PARAMS_DATA_IDS:  # params or L/T state
            if cmd := which_cmd(
                getattr(self._gwy.config, "use_native_ot", "avoid"), _to_msg_id(data_id)
            ):
                self.discovery.add_cmd(cmd, 3600, delay=90)

        for data_id in STATUS_DATA_IDS:  # except "00", see above
            if data_id == 0x00:
                continue
            if cmd := which_cmd(
                getattr(self._gwy.config, "use_native_ot", "avoid"), _to_msg_id(data_id)
            ):
                self.discovery.add_cmd(cmd, 300, delay=15)

        if _DBG_EXTRA_OTB_DISCOVERY:  # TODO: these are WIP, but do vary in payload
            for code in (
                Code._2401,  # WIP - modulation_level + flags?
                Code._3221,  # R8810A/20A
                Code._3223,  # R8810A/20A
            ):
                self.discovery.add_cmd(
                    Command.from_attrs(RQ, self.id, code, PayloadT("00")), 60
                )

        if _DBG_EXTRA_OTB_DISCOVERY:  # TODO: these are WIP, appear FIXED in payload
            for code in (
                Code._0150,  # payload always "000000", R8820A only?
                Code._1098,  # payload always "00C8",   R8820A only?
                Code._10B0,  # payload always "0000",   R8820A only?
                Code._1FD0,  # payload always "0000000000000000"
                Code._2400,  # payload always "0000000F"
                Code._2410,  # payload always "000000000000000000000000010000000100000C"
                Code._2420,  # payload always "0000001000000...
            ):  # TODO: to test against BDR91T
                self.discovery.add_cmd(
                    Command.from_attrs(RQ, self.id, code, PayloadT("00")), 300
                )

    # TODO[F4-PR2]: Purge legacy OpenTherm write-model tracking.
    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        if msg.verb not in (I_, RP):
            return

        if msg.code == Code._3220:
            self._handle_3220(msg)
        elif msg.code in self.RAMSES_TO_OT:
            self._handle_code(msg)

    def _handle_3220(self, msg: Message) -> None:
        """Handle 3220-based messages."""

        # NOTE: Reserved msgs have null data, but that msg_id may later be OK!
        if msg.payload[SZ_MSG_TYPE] == OtMsgType.RESERVED:
            return

        # NOTE: Some msgs have invalid data, but that msg_id may later be OK!
        if msg.payload.get(SZ_VALUE) is None:
            return

        # msg_id is int in msg payload/opentherm.py, but MsgId (str) in this module
        msg_id = _to_msg_id(msg.payload[SZ_MSG_ID])
        self._msgs_ot[msg_id] = msg

        if not _DBG_ENABLE_DEPRECATION:  # FIXME: data gaps
            return

        reset = msg.payload[SZ_MSG_TYPE] not in (
            OtMsgType.DATA_INVALID,
            OtMsgType.UNKNOWN_DATAID,
            OtMsgType.RESERVED,  # but some are ?always reserved
        )
        self.discovery.deprecate_code_ctx(msg._pkt, ctx=msg_id, reset=reset)

    def _handle_code(self, msg: Message) -> None:
        """Handle non-3220-based messages."""

        if msg.code == Code._3EF0 and msg.verb == I_:
            # NOTE: this is development/discovery code  # chasing flags
            # self._send_cmd(
            #     Command.get_opentherm_data(self.id, MsgId._00), **QOS_MID
            # )  # FIXME: deprecate QoS in kwargs
            return

        if msg.code in (Code._10A0, Code._3EF1):
            return

        if not _DBG_ENABLE_DEPRECATION:  # FIXME: data gaps
            return

        # TODO: can be temporarily 7FFF?
        if msg._pkt.payload[2:] == "7FFF" or (
            msg.code == Code._1300 and msg._pkt.payload[2:] == "09F6"
        ):  # latter is CH water pressure
            self.discovery.deprecate_code_ctx(msg._pkt)
        else:
            self.discovery.deprecate_code_ctx(msg._pkt, reset=True)

    @staticmethod
    def _ot_msg_name(msg: Message) -> str:  # TODO: remove
        return (
            msg.payload[SZ_MSG_NAME]
            if isinstance(msg.payload[SZ_MSG_NAME], str)
            else f"{msg.payload[SZ_MSG_ID]:02X}"
        )

    async def boiler_output_temp(self) -> float | None:  # 3220|19, or 3200
        return self.opentherm_state.temperatures.boiler_output

    async def boiler_return_temp(self) -> float | None:  # 3220|1C, or 3210
        return self.opentherm_state.temperatures.boiler_return

    async def boiler_setpoint(self) -> float | None:  # 3220|01, or 22D9
        return self.opentherm_state.temperatures.boiler_setpoint

    async def ch_max_setpoint(self) -> float | None:  # 3220|39, or 1081
        return self.opentherm_state.temperatures.ch_max_setpoint

    async def ch_setpoint(self) -> float | None:  # 3EF0 (byte 7, only R8820A?)
        return self.opentherm_state.temperatures.ch_setpoint

    async def ch_water_pressure(self) -> float | None:  # 3220|12, or 1300
        return self.opentherm_state.ch_water_pressure

    async def dhw_flow_rate(self) -> float | None:  # 3220|13, or 12F0
        return self.opentherm_state.dhw_flow_rate

    async def dhw_setpoint(self) -> float | None:  # 3220|38, or 10A0
        return self.opentherm_state.temperatures.dhw_setpoint

    async def dhw_temp(self) -> float | None:  # 3220|1A, or 1260
        return self.opentherm_state.temperatures.dhw

    async def max_rel_modulation(
        self,
    ) -> float | None:  # 3220|0E, or 3EF0 (byte 8) NOTE: not reliable?
        return self.opentherm_state.max_rel_modulation

    async def oem_code(self) -> float | None:  # 3220|73, no known RAMSES equivalent
        if (msg := self._msgs_ot.get(MsgId._73)) and not getattr(
            msg, "_expired", False
        ):
            return cast(float | None, msg.payload.get(SZ_VALUE))
        return None

    async def outside_temp(self) -> float | None:  # 3220|1B, 1290
        return self.opentherm_state.temperatures.outside

    async def rel_modulation_level(
        self,
    ) -> float | None:  # 3220|11, or 3EF0/3EF1 NOTE: not reliable?
        return self.opentherm_state.rel_modulation_level

    async def ch_active(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 3) NOTE: not reliable?
        return self.opentherm_state.flags.ch_active

    async def ch_enabled(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 6) NOTE: not reliable?
        return self.opentherm_state.flags.ch_enabled

    async def cooling_active(self) -> bool | None:  # 3220|00, TODO: no known RAMSES
        return self.opentherm_state.flags.cooling_active

    async def cooling_enabled(self) -> bool | None:  # 3220|00, TODO: no known RAMSES
        return self.opentherm_state.flags.cooling_enabled

    async def dhw_active(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 3) NOTE: not reliable?
        return self.opentherm_state.flags.dhw_active

    async def dhw_blocking(self) -> bool | None:  # 3220|00, TODO: no known RAMSES
        return self.opentherm_state.flags.dhw_blocking

    async def dhw_enabled(self) -> bool | None:  # 3220|00, TODO: no known RAMSES
        return self.opentherm_state.flags.dhw_enabled

    async def fault_present(self) -> bool | None:  # 3220|00, TODO: no known RAMSES
        return self.opentherm_state.flags.fault_present

    async def flame_active(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 3) NOTE: not reliable?
        return self.opentherm_state.flags.flame_active

    async def otc_active(self) -> bool | None:  # 3220|00, TODO: no known RAMSES
        return self.opentherm_state.flags.otc_active

    async def summer_mode(self) -> bool | None:  # 3220|00, TODO: no known RAMSES
        return self.opentherm_state.flags.summer_mode

    async def opentherm_schema(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            self._ot_msg_name(v): v.payload
            for k, v in self._msgs_ot.items()
            if getattr(self.discovery, "_supported_cmds_ctx", {}).get(k)
            and int(k, 16) in SCHEMA_DATA_IDS
        }
        return {
            m: {k: v for k, v in p.items() if k.startswith(SZ_VALUE)}
            for m, p in result.items()
        }

    async def opentherm_counters(self) -> dict[str, Any]:  # all are U16
        return {
            SZ_BURNER_HOURS: self.opentherm_state.counters.burner_hours,
            SZ_BURNER_STARTS: self.opentherm_state.counters.burner_starts,
            SZ_BURNER_FAILED_STARTS: self.opentherm_state.counters.burner_failed_starts,
            SZ_CH_PUMP_HOURS: self.opentherm_state.counters.ch_pump_hours,
            SZ_CH_PUMP_STARTS: self.opentherm_state.counters.ch_pump_starts,
            SZ_DHW_BURNER_HOURS: self.opentherm_state.counters.dhw_burner_hours,
            SZ_DHW_BURNER_STARTS: self.opentherm_state.counters.dhw_burner_starts,
            SZ_DHW_PUMP_HOURS: self.opentherm_state.counters.dhw_pump_hours,
            SZ_DHW_PUMP_STARTS: self.opentherm_state.counters.dhw_pump_starts,
            SZ_FLAME_SIGNAL_LOW: self.opentherm_state.counters.flame_signal_low,
        }  # 0x73 is not a counter: is OEM diagnostic code...

    async def opentherm_params(
        self,
    ) -> dict[str, Any]:  # F8_8, U8, {"hb": S8, "lb": S8}
        result = {
            self._ot_msg_name(v): v.payload
            for k, v in self._msgs_ot.items()
            if getattr(self.discovery, "_supported_cmds_ctx", {}).get(k)
            and int(k, 16) in PARAMS_DATA_IDS
        }
        return {
            m: {k: v for k, v in p.items() if k.startswith(SZ_VALUE)}
            for m, p in result.items()
        }

    async def ramses_schema(self) -> PayDictT.EMPTY:
        return {}

    async def ramses_params(self) -> dict[str, float | None]:
        return {
            SZ_MAX_REL_MODULATION: await self.max_rel_modulation(),
        }

    async def traits(self) -> dict[str, Any]:
        base_traits = await super().traits()
        return {
            **base_traits,
            "opentherm_traits": await self.discovery.supported_cmds_ot(),
            "ramses_ii_traits": await self.discovery.supported_cmds(),
        }

    async def schema(self) -> dict[str, Any]:
        base_schema = await super().schema()
        return {
            **base_schema,
            "opentherm_schema": await self.opentherm_schema(),
            "ramses_ii_schema": await self.ramses_schema(),
        }

    async def params(self) -> dict[str, Any]:
        base_params = await super().params()
        return {
            **base_params,
            "opentherm_params": await self.opentherm_params(),
            "ramses_ii_params": await self.ramses_params(),
        }

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,  # incl. actuator_cycle, actuator_state
            SZ_BOILER_OUTPUT_TEMP: await self.boiler_output_temp(),
            SZ_BOILER_RETURN_TEMP: await self.boiler_return_temp(),
            SZ_BOILER_SETPOINT: await self.boiler_setpoint(),
            SZ_CH_SETPOINT: await self.ch_setpoint(),
            SZ_CH_MAX_SETPOINT: await self.ch_max_setpoint(),
            SZ_CH_WATER_PRESSURE: await self.ch_water_pressure(),
            SZ_DHW_FLOW_RATE: await self.dhw_flow_rate(),
            SZ_DHW_SETPOINT: await self.dhw_setpoint(),
            SZ_DHW_TEMP: await self.dhw_temp(),
            SZ_OEM_CODE: await self.oem_code(),
            SZ_OUTSIDE_TEMP: await self.outside_temp(),
            SZ_REL_MODULATION_LEVEL: await self.rel_modulation_level(),
            SZ_CH_ACTIVE: await self.ch_active(),
            SZ_CH_ENABLED: await self.ch_enabled(),
            SZ_COOLING_ACTIVE: await self.cooling_active(),
            SZ_COOLING_ENABLED: await self.cooling_enabled(),
            SZ_DHW_ACTIVE: await self.dhw_active(),
            SZ_DHW_BLOCKING: await self.dhw_blocking(),
            SZ_DHW_ENABLED: await self.dhw_enabled(),
            SZ_FAULT_PRESENT: await self.fault_present(),
            SZ_FLAME_ACTIVE: await self.flame_active(),
            SZ_SUMMER_MODE: await self.summer_mode(),
            SZ_OTC_ACTIVE: await self.otc_active(),
        }

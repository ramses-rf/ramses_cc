"""RAMSES RF - Heating Controller Devices."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final, cast

from ..const import (
    DEV_ROLE_MAP,
    FA,
    FC,
    RQ,
    SZ_DOMAIN_ID,
    SZ_ZONE_DEMAND,
    SZ_RELAY_DEMAND,
    SZ_UFH_IDX,
    SZ_ZONE_IDX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    ZON_ROLE_MAP,
    Code,
    DevType,
)
from ..entity import Entity
from ..helpers import shrink
from ..models import DeviceTraits
from ..schemas import SCH_TCS, SZ_CIRCUITS
from ..topology import Child, Parent
from ...ramses_tx import Command
from ...ramses_tx.typing import DeviceIdT, DevIndexT, PayloadT

from .dev_base import DeviceHeat

if TYPE_CHECKING:
    from ..systems import Evohome, Zone

    from ..messages import Message


_LOGGER = logging.getLogger(__name__)


class Controller(DeviceHeat):  # CTL (01):
    """The Controller base class."""

    ZONE_DEMAND: Final = SZ_ZONE_DEMAND

    _SLUG = DevType.CTL
    _STATE_ATTR = ZONE_DEMAND

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

        self.tcs: Evohome | None = None  # TODO: = self?
        self._make_tcs_controller(**kwargs)  # NOTE: must create_from_schema first

    def _post_class_promote(self) -> None:
        """Initialize CTL state when promoted in-place from a generic device."""
        self.__dict__.setdefault("tcs", None)
        if not self.tcs:
            self._make_tcs_controller()

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        if not self.is_faked:
            self.discovery.add_cmd(
                Command.from_attrs(RQ, self.id, Code._2E04, PayloadT("00")),
                60 * 60,  # Poll every 60 minutes after initial startup query
            )

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        if self.tcs:
            self.tcs._handle_msg(msg)

    def _make_tcs_controller(
        self, *, msg: Message | None = None, **schema: Any
    ) -> None:  # CH/DHW
        """Attach a TCS (create/update as required) after passing it any msg."""

        def get_system(*, msg: Message | None = None, **schema: Any) -> Evohome:
            """Return a TCS (temperature control system), create it if required.

            Use the schema to create/update it, then pass it any msg to handle.

            TCSs are uniquely identified by a controller ID.
            If a TCS is created, attach it to this device (which should be a CTL).
            """

            from ..systems import system_factory

            schema = shrink(SCH_TCS(schema))

            if not self.tcs:
                self.tcs = cast("Evohome", system_factory(self, msg=msg, **schema))

            elif schema and self.tcs:
                self.tcs._update_schema(**schema)

            if msg and self.tcs:
                self.tcs._handle_msg(msg)
            return self.tcs

        super()._make_tcs_controller(msg=None, **schema)

        self.tcs = get_system(msg=msg, **schema)


class Programmer(Controller):  # PRG (23):
    """The Controller base class."""

    _SLUG = DevType.PRG

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        if not self.is_faked:
            # PRGs respond to RP for 1090, 10A0, 3EF1
            self.discovery.add_cmd(
                Command.from_attrs(RQ, self.id, Code._1090, PayloadT("00")),
                60 * 60 * 6,  # every 6 hours
            )
            self.discovery.add_cmd(
                Command.from_attrs(RQ, self.id, Code._10A0, PayloadT("00")),
                60 * 60 * 6,
            )


class RfgGateway(DeviceHeat):  # RFG (30:)
    """The RFG100 base class."""

    _SLUG = DevType.RFG

    _STATE_ATTR = None


class UfhController(Parent, DeviceHeat):  # UFC (02):
    """The UFC class, the HCE80 that controls the UFH zones."""

    ZONE_DEMAND: Final = SZ_ZONE_DEMAND

    _SLUG = DevType.UFC
    _STATE_ATTR = ZONE_DEMAND

    _child_id = FA
    _iz_controller = True

    childs: list[Child]  # TODO: check (code so complex, not sure if this is true)

    # 12:27:24.398 067  I --- 02:000921 --:------ 01:191718 3150 002 0360
    # 12:27:24.546 068  I --- 02:000921 --:------ 01:191718 3150 002 065A
    # 12:27:24.693 067  I --- 02:000921 --:------ 01:191718 3150 002 045C
    # 12:27:24.824 059  I --- 01:191718 --:------ 01:191718 3150 002 FC5C
    # 12:27:24.857 067  I --- 02:000921 --:------ 02:000921 3150 006 0060-015A-025C

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)
        self._init_ufh_state()

    def _init_ufh_state(self) -> None:
        """Initialize UFH-specific instance attributes (idempotent)."""
        self.__dict__.setdefault("circuit_by_id", {f"{i:02X}": {} for i in range(8)})

    def _post_class_promote(self) -> None:
        """Initialize UFH state when promoted in-place from a generic device."""
        self._init_ufh_state()

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        # Only RPs are: 0001, 0005/000C, 10E0, 000A/2309 & 22D0

        cmd = Command.from_attrs(
            RQ, self.id, Code._0005, PayloadT(f"00{DEV_ROLE_MAP.UFH}")
        )
        self.discovery.add_cmd(cmd, 60 * 60 * 24)

        # TODO: this needs work
        # if discover_flag & Discover.PARAMS:  # only 2309 has any potential?
        for ufc_idx in getattr(self, "circuit_by_id", {}):
            cmd = Command.get_zone_config(self.id, ufc_idx)
            self.discovery.add_cmd(cmd, 60 * 60 * 6)

            cmd = Command.get_zone_setpoint(self.id, ufc_idx)
            self.discovery.add_cmd(cmd, 60 * 60 * 6)

        for ufc_idx in range(8):
            payload = PayloadT(f"{ufc_idx:02X}{DEV_ROLE_MAP.UFH}")
            cmd = Command.from_attrs(RQ, self.id, Code._000C, payload)
            self.discovery.add_cmd(cmd, 60 * 60 * 24)

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        # Several assumptions are made, regarding 000C pkts:
        # - UFC bound only to CTL (not, e.g. SEN)
        # - all circuits bound to the same controller

        if msg.code == Code._0005:  # system_zones
            # {'zone_type': '09', 'zone_mask':[1, 1, 1, 1, 1, 0, 0, 0], 'zone_class': 'underfloor_heating'}

            if msg.payload.get(SZ_ZONE_TYPE) not in (
                ZON_ROLE_MAP.ACT,
                ZON_ROLE_MAP.UFH,
            ):
                return  # ignoring ZON_ROLE_MAP.SEN for now

            for idx, flag in enumerate(msg.payload.get(SZ_ZONE_MASK, [])):
                ufh_idx = f"{idx:02X}"
                if not flag:
                    self.circuit_by_id[ufh_idx] = {SZ_ZONE_IDX: None}
                # FIXME: this causing tests to fail when read-only protocol
                # elif SZ_ZONE_IDX not in self.circuit_by_id[ufh_idx]:
                #     cmd = Command.from_attrs(
                #         RQ, self.ctl.id, Code._000C, f"{ufh_idx}{DEV_ROLE_MAP.UFH}"
                #     )
                #     self._send_cmd(cmd)

        elif msg.code == Code._0008:  # relay_demand
            if msg.payload.get(SZ_DOMAIN_ID) == FC:
                pass
            else:  # FA
                pass

        elif msg.code == Code._000C:  # zone_devices
            # {'zone_type': '09', 'ufh_idx': '00', 'zone_idx': '09', 'device_role': 'ufh_actuator', 'devices':['01:095421']}
            # {'zone_type': '09', 'ufh_idx': '07', 'zone_idx': None, 'device_role': 'ufh_actuator', 'devices':[]}

            if msg.payload.get(SZ_ZONE_TYPE) not in (
                ZON_ROLE_MAP.ACT,
                ZON_ROLE_MAP.UFH,
            ):
                return  # ignoring ZON_ROLE_MAP.SEN for now

            ufh_idx = msg.payload.get(SZ_UFH_IDX)  # circuit idx
            if ufh_idx is None:
                return
            # Read-Model Update ONLY. No `self.set_parent()` graph mutation here.
            self.circuit_by_id[ufh_idx] = {SZ_ZONE_IDX: msg.payload.get(SZ_ZONE_IDX)}

        elif msg.code == Code._22C9:  # setpoint_bounds
            # .I --- 02:017205 --:------ 02:017205 22C9 024 00076C0A280101076C0A28010...
            # .I --- 02:017205 --:------ 02:017205 22C9 006 04076C0A2801
            pass

        elif msg.code == Code._3150:  # zone_demands
            if isinstance(msg.payload, list):  # the circuit demands
                pass
            elif msg.payload.get(SZ_DOMAIN_ID) == FC:
                pass
            else:
                zone_idx = msg.payload.get(SZ_ZONE_IDX)
                msg_dst_tcs = getattr(msg.dst, "tcs", None)
                if zone_idx and msg_dst_tcs and hasattr(msg_dst_tcs, "zone_by_idx"):
                    if zone := msg_dst_tcs.zone_by_idx.get(zone_idx):
                        zone._handle_msg(msg)

        # elif msg.code not in (Code._10E0, Code._22D0):
        #     print("xxx")
        # "0008|FA/FC", "22C9|array", "22D0|none", "3150|ZZ/array(/FC?)"

    # TODO: should be a private method
    def get_circuit(
        self, cct_idx: str, *, msg: Message | None = None, **schema: Any
    ) -> Any:
        """Return a UFH circuit, create it if required.

        First, use the schema to create/update it, then pass it any msg to handle.

        Circuits are uniquely identified by a UFH controller ID|cct_idx pair.
        If a circuit is created, attach it to this UFC.
        """

        schema = {}  # shrink(SCH_CCT(schema))

        cct = cast("UfhCircuit | None", self.child_by_id.get(cct_idx))
        if not cct:
            cct = UfhCircuit(self, cct_idx)
            self.child_by_id[cct_idx] = cct
            self.childs.append(cct)

        elif schema:
            cct._update_schema(**schema)

        if msg:
            cct._handle_msg(msg)
        return cct

    # @property
    # def circuits(self) -> dict:  # 000C
    #     return self.circuit_by_id

    async def zone_demand(self) -> float | None:  # 3150|FC (there is also 3150|FA)
        state = getattr(self, "demand_state", None)
        return state.zone_demand if state else None

    async def pump_active(self) -> bool | None:  # 3EF0 byte 3 or 3150 demand fallback
        """Return True if the UFH pump relay is active.

        Primary source: 3EF0 byte 3 flags (0x10=cooling, 0x02=heating).
        Fallback: zone_demand > 10% from 3150 packets (always available).
        The HCC100 only broadcasts 3EF0 when 2D49 binding is active.
        """
        from_3ef0 = await self.entity_state.get_value(Code._3EF0, key="pump_active")
        if from_3ef0 is not None:
            _LOGGER.debug(
                "%s pump_active: 3EF0 returned %s", self.id, from_3ef0
            )
            return cast("bool | None", from_3ef0)
        # DEBUG: trace why 3EF0 returned None
        msgs_dict = await self.entity_state.get_message_log_flat()
        has_3ef0_in_flat = Code._3EF0 in msgs_dict
        _LOGGER.warning(
            "%s pump_active: 3EF0 returned None! "
            "3EF0 in message_log_flat=%s, _current_state keys=%s",
            self.id,
            has_3ef0_in_flat,
            [str(h.code) for h in self.entity_state._current_state.keys()],
        )
        # Fallback: infer from zone demand (3150 packets) — threshold 10%
        # zone_demand is on a 0.0–1.0 scale (from hex_to_percent), not 0–100
        try:
            demand = await self.zone_demand()
        except BaseException as exc:
            _LOGGER.warning(
                "%s pump_active fallback EXCEPTION before log: %s %s",
                self.id, type(exc).__name__, exc
            )
            raise
        _LOGGER.warning(  # noqa: E501
            "%s pump_active fallback: zone_demand()=%s, demand_state=%s",
            self.id, demand, getattr(self, "demand_state", "MISSING")
        )
        if demand is not None:
            return demand > 0.10
        return None

    async def mode(self) -> str | None:  # 2D49
        """Return 'cool', 'heat', or None from 2D49 changeover state."""
        return await self.entity_state.get_value(Code._2D49, key="mode")

    async def ufc_mode(self) -> dict | None:  # 22D0
        """Return the UFC heat/cool mode flags (from 22D0).

        Returns parsed dict with keys: cool_mode, heat_mode, is_active.
        """
        return await self.entity_state.get_value(Code._22D0)

    async def zone_demands(self) -> list[dict[str, Any]] | None:  # 3150|ufh_idx array
        """Return the UFH heat demands.

        # TODO: Refactor for #714 (CQRS API Boundaries).
        # This is a legacy shim to maintain backward compatibility with ramses_cc.
        """
        state = getattr(self, "ufh_state", None)
        if state and state.zone_demands:
            return [
                {"ufx_idx": str(k), "zone_demand": v}
                for k, v in state.zone_demands.items()
            ]
        return None

    async def relay_demand(self) -> float | None:  # 0008|FC
        state = getattr(self, "demand_state", None)
        return state.relay_demand if state else None

    async def relay_demand_fa(self) -> float | None:  # 0008|FA
        state = getattr(self, "ufh_state", None)
        return state.relay_demand_fa if state else None

    async def setpoints(self) -> dict[str, Any] | None:  # 22C9|ufh_idx array
        """Return the UFH setpoints.

        # TODO: Refactor for #714 (CQRS API Boundaries).
        # This is a legacy shim to maintain backward compatibility with ramses_cc.
        """
        state = getattr(self, "ufh_state", None)
        if state is None:
            return None

        # Return the dictionary exactly as is (even if empty `{}`, to match legacy)
        return cast(dict[str, Any], state.setpoints)

    async def schema(self) -> dict[str, Any]:
        base_schema = await super().schema()
        return {
            **base_schema,
            SZ_CIRCUITS: getattr(self, "circuit_by_id", {}),
        }

    async def params(self) -> dict[str, Any]:
        base_params = await super().params()
        return {
            **base_params,
            SZ_CIRCUITS: await self.setpoints(),
        }

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            SZ_ZONE_DEMAND: await self.zone_demand(),
            SZ_RELAY_DEMAND: await self.relay_demand(),
            f"{SZ_RELAY_DEMAND}_fa": await self.relay_demand_fa(),
        }


class UfhCircuit(Child, Entity):  # FIXME
    """The UFH circuit class (UFC:circuit is much like CTL/TCS:zone).

    NOTE: for circuits, there's a difference between :
     - `self.ctl`: the UFH controller, and
     - `self.tcs.ctl`: the Evohome controller
    """

    _SLUG: str = "CCT"  # previously None, strict Mypy fix
    _STATE_ATTR: str | None = None

    def __init__(self, ufc: UfhController, ufh_idx: str) -> None:
        super().__init__(ufc._gwy)

        # FIXME: gwy.message_store entities must know their parent device ID
        # and their own idx
        self._z_id = ufc.id
        self._z_idx = cast("DevIndexT", ufh_idx)

        self.id: DeviceIdT = cast("DeviceIdT", f"{ufc.id}_{ufh_idx}")

        self.ufc: UfhController = ufc
        self._child_id = ufh_idx

        # TODO: _ctl should be: .ufc? .ctl?
        self._ctl: Controller | None = None
        self._zone: Zone | None = None

    def _update_schema(self, **kwargs: Any) -> None:
        raise NotImplementedError

    @property
    def ufx_idx(self) -> str:
        return str(self._child_id)

    @property
    def zone_idx(self) -> str | None:
        if self._zone:
            return str(self._zone._child_id)
        return None

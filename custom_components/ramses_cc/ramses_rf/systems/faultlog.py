#!/usr/bin/env python3
"""RAMSES RF - Expose an 0418 fault log (is a stateful process)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, NewType, TypeAlias

from .. import exceptions as exc
from ..models import FaultLogState, StateUpdatedEvent
from ...ramses_tx import Command, Packet
from ...ramses_tx.const import (
    SZ_LOG_ENTRY,
    SZ_LOG_IDX,
    FaultDeviceClass,
    FaultState,
    FaultType,
)
from ...ramses_tx.helpers import parse_fault_log_entry
from ...ramses_tx.typing import DeviceIdT, PayloadT

from ..messages import Message

from ..const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from ..systems.tcs import _LogbookT


FaultTupleT: TypeAlias = tuple[FaultType, FaultDeviceClass, DeviceIdT | None, str]


DEFAULT_GET_LIMIT = 6


_LOGGER = logging.getLogger(__name__)


#  {'log_idx': '00', 'log_entry': ('21-12-23T11:59:35', 'restore', 'battery_low', 'actuator', '00', '04:164787', 'B0', '0000', 'FFFF7000')}


@dataclasses.dataclass(frozen=True, kw_only=True, order=True)
class FaultLogEntry:
    """A fault log entry of an evohome fault log.

    Fault log entries do have a log_idx attr, but this is merely their current location
    in the system's fault log.
    """

    timestamp: str  # #               # 21-12-23T11:59:35 - assume is unique
    fault_state: FaultState  # #      # fault, restore, unknown_c0
    fault_type: FaultType  # #        # system_fault, battery_low, sensor_fault, etc.
    domain_idx: str  # #              # 00-0F, FC, etc. ? only if dev_class is/not CTL?
    device_class: FaultDeviceClass  # # controller, actuator, sensor, etc.
    device_id: DeviceIdT | None  # #  # 04:164787

    # def __post_init__(self):
    #     def modify(device_id: DeviceIdT) -> DeviceIdT:
    #     object.__setattr__(self, "device_id", modify(self.device_id))

    def __str__(self) -> str:
        return (
            f"{self.timestamp}, {(self.fault_state + ','):<8} {self.fault_type}, "
            f"{self.device_id}, {self.domain_idx}, {self.device_class}"
        )

    def _is_matching_pair(self, other: object) -> bool:
        """Return True if the other entry could be a matching pair (fault/restore)."""

        if not isinstance(other, FaultLogEntry):
            raise exc.SystemInconsistent(f"{other} is not a FaultLogEntry")

        if self.fault_state == FaultState.FAULT:
            return (
                other.fault_state == FaultState.RESTORE
                and self._as_tuple() == other._as_tuple()
                and other.timestamp > self.timestamp
            )

        if self.fault_state == FaultState.RESTORE:
            return (
                other.fault_state == FaultState.FAULT
                and self._as_tuple() == other._as_tuple()
                and other.timestamp < self.timestamp
            )

        return False

    def _as_tuple(self) -> FaultTupleT:  # only for use within this class
        """Return the log entry as a tuple, excluding dtm & state (fault/restore)."""

        return (
            self.fault_type,
            self.device_class,
            self.device_id,
            self.domain_idx,
        )

    @classmethod
    def from_msg(cls, msg: Message) -> FaultLogEntry:
        """Create a fault log entry from a message's packet."""
        return cls.from_pkt(msg._pkt)

    @classmethod
    def from_pkt(cls, pkt: Packet) -> FaultLogEntry:
        """Create a fault log entry from a packet's payload."""

        log_entry = parse_fault_log_entry(pkt.payload)
        if log_entry is None:
            raise exc.SystemInconsistent("Null fault log entry")

        return cls(**{k: v for k, v in log_entry.items() if k[:1] != "_"})  # type: ignore[arg-type]


FaultDtmT = NewType("FaultDtmT", str)
FaultIdxT = NewType("FaultIdxT", int)

FaultLogT: TypeAlias = dict[FaultDtmT, FaultLogEntry]
FaultMapT: TypeAlias = OrderedDict[FaultIdxT, FaultDtmT]


class FaultLog:  # 0418
    """The fault log of an evohome system.

    This code assumes that the `timestamp` attr of each log entry is a unique identifier.

    Null entries do not have a timestamp. All subsequent entries will also be null.

    The `log_idx` is not an identifier: it is merely the current position of a log entry
    in the system log.

    New entries are added to the top of the log (log_idx=0), and the log_idx is
    incremented for all existing log entries.
    """

    _MAX_LOG_IDX = 0x3F  # evohome controller only keeps most recent 64 entries

    def __init__(self, tcs: _LogbookT) -> None:
        self._tcs: _LogbookT = tcs
        self.id = tcs.id
        self._gwy = tcs._gwy

        self.state = FaultLogState()

        self._log: FaultLogT = {}
        self._map: FaultMapT = OrderedDict()
        self._log_done: bool | None = None

        self._is_current: bool = False  # if we know our log is out of date
        self._get_lock = asyncio.Lock()

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        """Replace the internal CQRS read-model state with a new immutable object.

        This method acts as the single ingestion point for state changes, completely
        bypassing legacy packet interception.
        """
        if isinstance(event.state, FaultLogState):
            self.state = event.state

    def _insert_into_map(self, idx: FaultIdxT, dtm: FaultDtmT | None) -> FaultMapT:
        """Rebuild the map (as best as possible), given the a log entry."""

        new_map: FaultMapT = OrderedDict()

        # usu. idx == 0, but could be > 0
        new_map |= {
            k: v for k, v in self._map.items() if k < idx and (dtm is None or v > dtm)
        }

        if dtm is None:  # there are no subsequent log entries
            return new_map

        new_map |= {idx: dtm}

        if not (idxs := [k for k, v in self._map.items() if v < dtm]):
            return new_map

        if (next_idx := min(idxs)) > idx:
            diff = 0
        elif next_idx == idx:
            diff = 1  # next - idx + 1
        else:
            diff = idx + 1  # 1 if self._map.get(idx) else 0

        new_map |= {
            k + diff: v  # type: ignore[misc]
            for k, v in self._map.items()
            if (k >= idx or v < dtm) and k + diff <= self._MAX_LOG_IDX
        }

        return new_map

    def handle_msg(self, msg: Message) -> None:
        """Handle a fault log message (some valid payloads should be ignored)."""

        assert msg.code == Code._0418 and msg.verb in (I_, RP), "Coding error"

        if msg.verb == RP and msg.payload[SZ_LOG_ENTRY] is None:
            # such payloads have idx == "00" (is sentinel for null), so can't know the
            # corresponding RQ's log_idx, but if verb == I_, safely assume log_idx is 0
            return

        self._process_msg(msg)

    def _process_msg(self, msg: Message) -> None:
        """Handle a processable fault log message."""

        if msg.verb == I_:
            self._is_current = False

        if SZ_LOG_IDX not in msg.payload:
            return  # we can't do anything useful with this message

        idx: FaultIdxT = int(msg.payload[SZ_LOG_IDX], 16)  # type: ignore[assignment]

        if msg.payload[SZ_LOG_ENTRY] is None:  # NOTE: Subsequent entries will be empty
            self._map = self._insert_into_map(idx, None)
            self._log = {k: v for k, v in self._log.items() if k in self._map.values()}
            return  # If idx != 0, should we also check from idx = 0?

        entry = FaultLogEntry.from_msg(msg)  # if msg.payload[SZ_LOG_ENTRY] else None
        dtm: FaultDtmT = entry.timestamp  # type: ignore[assignment]

        if self._map.get(idx) == dtm:
            return  # i.e. No evidence anything has changed

        if dtm not in self._log:
            self._log |= {dtm: entry}  # must add entry before _insert_into_map()
        self._map = self._insert_into_map(idx, dtm)  # updates self._map
        self._log = {k: v for k, v in self._log.items() if k in self._map.values()}

        # if idx != 0:  # there's other (new/changed) entries above this one?
        #     pass

    def _hack_pkt_idx(self, pkt: Packet, cmd: Command) -> Message:
        """Modify the Packet so that it has the log index of its corresponding Command.

        If there is no log entry for log_idx=<idx>, then the headers won't match:
        - cmd rx_hdr is 0418|RP|<ctl_id>|<idx> (expected)
        - pkt hdr will  0418|RP|<ctl_id>|00    (response from controller)

        We can only assume that the Pkt is the reply to the Cmd, which is why using
        QoS with wait_for_reply=True is vital when getting fault log entries.

        We can assume 0418| I|<ctl_id>|00 is only for log_idx=00 (I|0418s are stateless)
        """

        assert pkt.verb == RP and pkt.code == Code._0418 and pkt._idx == "00"
        assert pkt.payload == "000000B0000000000000000000007FFFFF7000000000"

        assert cmd.verb == RQ and pkt.code == Code._0418
        assert cmd.rx_header and cmd.rx_header[:-2] == pkt._hdr[:-2]  # reply to this RQ

        if cmd._idx == "00":  # no need to hack
            return Message._from_pkt(pkt)

        idx = cmd.rx_header[-2:]  # cmd._idx could be bool/None?
        pkt.payload = PayloadT(f"0000{idx}B0000000000000000000007FFFFF7000000000")

        # NOTE: must now reset pkt payload, and its header
        pkt._repr = pkt._hdr_ = pkt._ctx_ = pkt._idx_ = None  # type: ignore[assignment]
        pkt._frame = pkt._frame[:50] + idx + pkt._frame[52:]

        assert pkt._hdr == cmd.rx_header, f"{self}: Coding error"
        assert str(pkt) == pkt._frame[:50] + idx + pkt._frame[52:], (
            f"{self}: Coding error"
        )

        msg = Message._from_pkt(pkt)
        msg._payload = {SZ_LOG_IDX: idx, SZ_LOG_ENTRY: None}  # PayDictT._0418_NULL

        return msg

    async def get_faultlog(
        self,
        /,
        *,
        start: int = 0,
        limit: int | None = DEFAULT_GET_LIMIT,
        force_refresh: bool = False,
    ) -> dict[FaultIdxT, FaultLogEntry]:
        """Retrieve the fault log from the controller."""

        # Short-circuit: Return instantly from memory if already current and no refresh forced
        if not force_refresh and self._is_current:
            return self.faultlog

        if limit is None:
            limit = DEFAULT_GET_LIMIT

        # Use the lock to prevent concurrent network spam if called multiple times simultaneously
        async with self._get_lock:
            # Double-check inside the lock in case another task just finished fetching it
            if not force_refresh and self._is_current:
                return self.faultlog

            error_occurred = False

            for idx in range(start, min(start + limit, self._MAX_LOG_IDX + 1)):
                cmd = Command.get_system_log_entry(self.id, idx)

                try:
                    pkt = await self._gwy.async_send_cmd(cmd, wait_for_reply=True)
                except exc.RamsesException as err:
                    _LOGGER.warning(f"Failed to retrieve fault log entry {idx}: {err}")
                    error_occurred = True
                    self._is_current = False
                    break

                if pkt.payload == "000000B0000000000000000000007FFFFF7000000000":
                    msg = self._hack_pkt_idx(
                        pkt, cmd
                    )  # RPs for null entries have idx==00
                    self._process_msg(msg)  # since pkt via dispatcher aint got idx
                    break

                self._process_msg(
                    Message._from_pkt(pkt)
                )  # JIC dispatcher doesn't do this for us

            # Only mark as current if the entire requested fetch completed without timing out
            if not error_occurred:
                self._is_current = True

        return self.faultlog

    @property
    def faultlog(self) -> dict[FaultIdxT, FaultLogEntry]:
        """Return the fault log of a system."""

        # if self._faultlog:
        #     return self._faultlog

        return {idx: self._log[dtm] for idx, dtm in self._map.items()}

    @property
    def is_current(self) -> bool:
        """Return True if the local fault log is identical to the controller's.

        To force an I/O update, explicitly call `get_faultlog(force_refresh=True)`.
        """
        return self._is_current

    @property
    def latest_event(self) -> FaultLogEntry | None:
        """Return the most recently logged event (fault or restore), if any."""

        if not self._log:
            return None

        return self._log[max(k for k in self._log)]

    @property
    def latest_fault(self) -> FaultLogEntry | None:
        """Return the most recently logged fault, if any."""

        if not self._log:
            return None

        faults = [k for k, v in self._log.items() if v.fault_state == FaultState.FAULT]

        if not faults:
            return None

        return self._log[max(faults)]

    @property
    def active_faults(self) -> tuple[FaultLogEntry, ...] | None:
        """Return a list of all faults outstanding (i.e. no corresponding restore)."""

        if not self._log:
            return None

        restores = {}
        faults = {}

        for entry in sorted(self._log.values(), reverse=True):
            if entry.fault_state == FaultState.RESTORE:
                # keep to match against upcoming faults
                restores[entry._as_tuple()] = entry

            if entry.fault_state == FaultState.FAULT:
                # look for (existing) matching restore, otherwise keep
                if entry._as_tuple() in restores:
                    del restores[entry._as_tuple()]
                else:
                    faults[entry._as_tuple()] = entry

        return tuple(faults.values())

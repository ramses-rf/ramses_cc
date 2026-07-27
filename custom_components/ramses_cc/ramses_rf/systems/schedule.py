#!/usr/bin/env python3
"""RAMSES RF - Expose an 0404 schedule (is a stateful process)."""

# TODO: use schemas from evohome_async

from __future__ import annotations

import asyncio
import logging
import struct
import zlib
from collections.abc import Iterable
from datetime import timedelta as td
from typing import TYPE_CHECKING, Any, Final, NotRequired, TypeAlias, TypedDict

import voluptuous as vol

from .. import exceptions as exc
from ..const import (
    SZ_FRAG_NUMBER,
    SZ_FRAGMENT,
    SZ_SCHEDULE,
    SZ_TOTAL_FRAGS,
    SZ_ZONE_IDX,
)
from ..messages import Message
from ...ramses_tx.command import Command
from ...ramses_tx.const import SZ_CHANGE_COUNTER, Priority
from ...ramses_tx.exceptions import ProtocolSendFailed
from ...ramses_tx.packet import Packet

from ..const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from ..systems.zones import DhwZone, Zone


# Constants
FIVE_MINS: Final = td(minutes=5)

SZ_MSG: Final = "msg"
SZ_DAY_OF_WEEK: Final = "day_of_week"
SZ_HEAT_SETPOINT: Final = "heat_setpoint"
SZ_SWITCHPOINTS: Final = "switchpoints"
SZ_TIME_OF_DAY: Final = "time_of_day"
SZ_ENABLED: Final = "enabled"

REGEX_TIME_OF_DAY: Final = r"^([0-1][0-9]|2[0-3]):[0-5][05]$"


# Types
class EmptyDictT(TypedDict):
    """An empty typed dictionary used as a sentinel."""

    pass


class SwitchPointDhw(TypedDict):
    """A dictionary representing a DHW switchpoint."""

    time_of_day: str
    enabled: bool


class SwitchPointZon(TypedDict):
    """A dictionary representing a Zone heating switchpoint."""

    time_of_day: str
    heat_setpoint: float


SwitchPointT: TypeAlias = SwitchPointDhw | SwitchPointZon
SwitchPointsT: TypeAlias = list[SwitchPointDhw] | list[SwitchPointZon]


class DayOfWeek(TypedDict):
    """A dictionary representing a schedule for a single day."""

    day_of_week: int
    switchpoints: SwitchPointsT


DayOfWeekT: TypeAlias = DayOfWeek
InnerScheduleT: TypeAlias = list[DayOfWeek]


class _OuterSchedule(TypedDict):
    """A dictionary representing a full schedule payload."""

    zone_idx: str
    schedule: InnerScheduleT


class _EmptySchedule(TypedDict):
    """A dictionary representing an empty schedule payload."""

    zone_idx: str
    schedule: NotRequired[EmptyDictT | None]


OuterScheduleT: TypeAlias = _OuterSchedule | _EmptySchedule

_PayloadT: TypeAlias = dict[str, Any]  # Message payload
_PayloadSetT: TypeAlias = list[_PayloadT | None]

_FragmentT: TypeAlias = str
_FragmentSetT: TypeAlias = list[_FragmentT]

EMPTY_PAYLOAD_SET: _PayloadSetT = [None]


_LOGGER = logging.getLogger(__name__)


def schema_sched(schema_switchpoint: vol.Schema) -> vol.Schema:
    """Generate a voluptuous schema for a weekly schedule.

    :param schema_switchpoint: The schema describing an individual
        switchpoint.
    :return: A voluptuous Schema object for the 7-day schedule array.
    """
    schema_sched_day = vol.Schema(
        {
            vol.Required(SZ_DAY_OF_WEEK): int,
            vol.Required(SZ_SWITCHPOINTS): vol.All(
                [schema_switchpoint], vol.Length(min=1)
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )
    return vol.Schema(
        vol.All([schema_sched_day], vol.Length(min=0, max=7)),
        extra=vol.PREVENT_EXTRA,
    )


SCH_SWITCHPOINT_DHW = vol.Schema(
    {
        vol.Required(SZ_TIME_OF_DAY): vol.Match(REGEX_TIME_OF_DAY),
        vol.Required(SZ_ENABLED): bool,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SWITCHPOINT_ZON = vol.Schema(
    {
        vol.Required(SZ_TIME_OF_DAY): vol.Match(REGEX_TIME_OF_DAY),
        vol.Required(SZ_HEAT_SETPOINT): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=35)
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SCHEDULE_DHW = schema_sched(SCH_SWITCHPOINT_DHW)
SCH_SCHEDULE_DHW_OUTER = vol.Schema(
    {
        vol.Required(SZ_ZONE_IDX): "HW",
        vol.Required(SZ_SCHEDULE): SCH_SCHEDULE_DHW,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SCHEDULE_ZON = schema_sched(SCH_SWITCHPOINT_ZON)
SCH_SCHEDULE_ZON_OUTER = vol.Schema(
    {
        vol.Required(SZ_ZONE_IDX): vol.Match(r"0[0-F]"),
        vol.Required(SZ_SCHEDULE): SCH_SCHEDULE_ZON,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_FULL_SCHEDULE = vol.Schema(
    vol.Any(SCH_SCHEDULE_DHW_OUTER, SCH_SCHEDULE_ZON_OUTER),
    extra=vol.PREVENT_EXTRA,
)


# TODO: make stateful (a la binding)
class Schedule:  # 0404
    """The schedule of a zone."""

    def __init__(self, zone: DhwZone | Zone) -> None:
        """Initialize the Schedule for a specific zone.

        :param zone: The heating or DHW zone this schedule applies to.
        """
        _LOGGER.debug("Schedule(zon=%s).__init__()", zone)

        self.id = zone.id
        self._zone = zone
        self.idx = zone.idx

        self.ctl = zone.ctl
        self.tcs = zone.tcs
        self._gwy = zone._gwy

        self._full_schedule: OuterScheduleT | EmptyDictT = {}

        self._payload_set: _PayloadSetT = list(EMPTY_PAYLOAD_SET)  # Rx'd
        self._fragments: _FragmentSetT = []  # to Tx

        self._global_ver = 0  # None is a sentinel for 'dont know'
        self._sched_ver = 0  # the global_ver when this schedule was retrieved

    def __str__(self) -> str:
        """Return a string representation of the schedule object."""
        return f"{self._zone} (schedule)"

    def _handle_msg(self, msg: Message) -> None:
        """Process a schedule packet: if possible, create the corresponding schedule.

        :param msg: The incoming message to parse.
        """
        if msg.code == Code._0006:  # keep up, in cause is useful to know in future
            self._global_ver = msg.payload.get(SZ_CHANGE_COUNTER, 0)
            return

        if msg.code != Code._0404:
            return

        if msg.verb == RQ:
            return

        # can do via here, or via gwy.async_send_cmd(cmd)
        # next line also in self._get_schedule(), so protected here with a lock
        if (
            msg.payload.get(SZ_TOTAL_FRAGS) != 0xFF
            and self.tcs.zone_lock_idx != self.idx
        ):
            try:
                self._payload_set = self._update_payload_set(
                    self._payload_set, msg.payload
                )
            except exc.ScheduleError as err:
                _LOGGER.warning(
                    "%s: Dropped corrupted schedule fragments: %s", self, err
                )
                self._payload_set = list(EMPTY_PAYLOAD_SET)

    async def _is_dated(self, *, force_io: bool = False) -> tuple[bool, bool]:
        """Indicate if a more recent schedule might be available.

        If required, retrieve the latest global version (change counter)
        from the TCS.

        There may be a false positive if another zone's schedule is
        changed when this zone's schedule has not. There may be a false
        negative if this zone's schedule was changed only very recently
        and a cached global version was used.

        If `force_io`, then a true negative is guaranteed (it forces an
        RQ|0006 unless self._global_ver > self._sched_ver).

        :param force_io: True to force an IO request to check versions.
        :return: A tuple of (is_dated, did_io).
        """
        # this will not cause an I/O...
        if (
            not force_io
            and not self._sched_ver
            or (self._global_ver and self._global_ver > self._sched_ver)
        ):
            return True, False  # is_dated, did_io

        # this may cause an I/O...
        self._global_ver, did_io = await self.tcs._schedule_version()
        if did_io or self._global_ver > self._sched_ver:
            return (
                self._global_ver > self._sched_ver,
                did_io,
            )  # is_dated, did_io

        if force_io:  # this will cause an I/O...
            self._global_ver, did_io = await self.tcs._schedule_version(
                force_io=force_io
            )

        return (
            self._global_ver > self._sched_ver,
            did_io,
        )  # is_dated, did_io

    async def get_schedule(
        self, *, force_io: bool = False, timeout: float = 15
    ) -> InnerScheduleT | None:
        """Retrieve/return the brief schedule of a zone.

        Return the cached schedule (which may have been eavesdropped)
        only if the global change counter has not increased. Otherwise,
        RQ the latest schedule from the controller and return that.

        If `force_io`, then the latest schedule is guaranteed (it forces
        an RQ|0006).

        :param force_io: Set to True to force fetching a new schedule
            from the controller.
        :param timeout: Maximum time in seconds to wait for the schedule.
        :return: The schedule details or None if not found.
        :raises exc.ScheduleFlowError: If unable to obtain the schedule
            before timeout.
        """
        try:
            await asyncio.wait_for(
                self._get_schedule(force_io=force_io), timeout=timeout
            )
        except TimeoutError as err:
            raise exc.ScheduleFlowError(
                f"Failed to obtain schedule within {timeout} secs"
            ) from err
        except ProtocolSendFailed:
            # Silently drop the background request if the transport is
            # inactive (e.g., during cache restoration prior to gateway
            # startup).
            _LOGGER.debug(
                f"{self}: Dropped request: gateway transport is inactive.",
            )
            return None

        return self.schedule

    async def _get_schedule(self, *, force_io: bool = False) -> None:
        """Retrieve/return the schedule of a zone and sets
        `self._full_schedule`.

        :param force_io: Set to True to force IO fetching.
        """

        async def get_fragment(frag_num: int) -> _PayloadT:
            """Retrieve a schedule fragment from the controller.

            :param frag_num: The fragment index number to fetch.
            :return: The dictionary payload of the fragment.
            """
            frag_set_size = 0 if frag_num == 1 else _len(self._payload_set)
            cmd = Command.get_schedule_fragment(
                self.ctl.id, self.idx, frag_num, frag_set_size
            )
            pkt: Packet = await self._gwy.async_send_cmd(
                cmd, wait_for_reply=True, priority=Priority.HIGH
            )
            msg = Message._from_pkt(pkt)
            assert isinstance(msg.payload, dict)  # mypy check
            return msg.payload  # may: TimeoutError?

        is_dated, did_io = await self._is_dated(force_io=force_io)
        if is_dated:
            self._full_schedule = {}  # keep frags, maybe only other scheds have changed
        if self._full_schedule:
            return

        await self.tcs._obtain_lock(self.idx)  # maybe raise TimeOutError

        if not did_io:  # must know the version of the schedule about to be RQ'd
            self._global_ver, _ = await self.tcs._schedule_version(force_io=True)

        self._payload_set[0] = None  # if 1st frag valid: schedule very likely unchanged
        while frag_num := next(
            (i for i, f in enumerate(self._payload_set, 1) if f is None), 0
        ):
            if frag_num == 0:
                break
            fragment = await get_fragment(frag_num)
            # next line also in self._handle_msg(), so protected there with a lock
            try:
                self._payload_set = self._update_payload_set(
                    self._payload_set, fragment
                )
            except exc.ScheduleError as err:
                _LOGGER.warning(
                    "%s: Dropped corrupted schedule fragments during fetch: %s",
                    self,
                    err,
                )
                self._payload_set = list(EMPTY_PAYLOAD_SET)
                break

            if self._full_schedule:  # TODO: potential for infinite loop?
                self._sched_ver = self._global_ver  # type: ignore[unreachable]
                break

        self.tcs._release_lock()

    def _proc_payload_set(self, payload_set: _PayloadSetT) -> OuterScheduleT | None:
        """Process a payload set and return the full schedule (sets
        `self._schedule`).

        If the schedule is for DHW, set the `zone_idx` key to 'HW' (to
        avoid confusing with zone '00').

        :param payload_set: The completed array of fragment payloads.
        :return: The full schedule block.
        :raises exc.ScheduleError: On failure to decompress fragment
            string blob.
        """
        # TODO: relying upon caller to ensure set is only empty or full

        if payload_set == EMPTY_PAYLOAD_SET:
            self._full_schedule = {SZ_ZONE_IDX: self.idx}
            return self._full_schedule

        try:
            schedule = fragz_to_full_sched(
                str(payload[SZ_FRAGMENT])
                for payload in payload_set
                if payload and SZ_FRAGMENT in payload
            )  # TODO: messy - what is set not full
        except zlib.error as err:
            raise exc.ScheduleError("Failed to decompress schedule fragments") from err

        if self.idx == "HW":
            schedule[SZ_ZONE_IDX] = "HW"
        self._full_schedule = schedule

        return self._full_schedule  # NOTE: not self.schedule

    def _update_payload_set(
        self, payload_set: _PayloadSetT, payload: _PayloadT
    ) -> _PayloadSetT:
        """Add a fragment to a frag set and process/return the new set.

        If the frag set is complete, check for a schedule (sets
        `self._schedule`). If required, start a new frag set with the
        fragment.

        :param payload_set: The existing fragment collection.
        :param payload: The new payload dict to integrate.
        :return: The updated set of payloads.
        """

        def init_payload_set(payload: _PayloadT) -> _PayloadSetT:
            total_frags = payload.get(SZ_TOTAL_FRAGS)
            frag_num = payload.get(SZ_FRAG_NUMBER)

            if total_frags is None or frag_num is None:
                return list(EMPTY_PAYLOAD_SET)

            _payload_set: _PayloadSetT = [None] * total_frags

            # Guard against invalid fragment indices that could cause IndexError
            if 0 < frag_num <= total_frags:
                _payload_set[frag_num - 1] = payload
            return _payload_set

        if payload.get(SZ_TOTAL_FRAGS) is None:  # zone has no schedule
            payload_set = list(EMPTY_PAYLOAD_SET)
            self._proc_payload_set(payload_set)
            return payload_set

        if payload.get(SZ_TOTAL_FRAGS) != _len(payload_set):  # sched has changed
            return init_payload_set(payload)

        frag_num = payload.get(SZ_FRAG_NUMBER)
        if frag_num is not None and 0 < frag_num <= _len(payload_set):
            payload_set[frag_num - 1] = payload

        if None in payload_set or self._proc_payload_set(
            payload_set
        ):  # sets self._schedule
            return payload_set

        return init_payload_set(payload)

    async def set_schedule(
        self, schedule: InnerScheduleT, force_refresh: bool = False
    ) -> InnerScheduleT | None:
        """Set the schedule of a zone.

        :param schedule: The array representing the days of the week
            schedule.
        :param force_refresh: True to query and retrieve the new
            schedule directly after setting.
        :return: The updated InnerSchedule array.
        :raises exc.ScheduleError: On validation or serialization failure.
        :raises exc.ScheduleFlowError: On transmission timeout.
        """

        async def put_fragment(frag_num: int, frag_cnt: int, fragment: str) -> None:
            """Send a schedule fragment to the controller."""
            cmd = Command.set_schedule_fragment(
                self.ctl.id, self.idx, frag_num, frag_cnt, fragment
            )
            await self._gwy.async_send_cmd(
                cmd, wait_for_reply=True, priority=Priority.HIGH
            )

        def normalise_validate(schedule: InnerScheduleT) -> _OuterSchedule:
            full_schedule: _OuterSchedule

            if self.idx == "HW":
                full_schedule = {SZ_ZONE_IDX: "HW", SZ_SCHEDULE: schedule}
                schedule_schema = SCH_SCHEDULE_DHW_OUTER
            else:
                full_schedule = {SZ_ZONE_IDX: self.idx, SZ_SCHEDULE: schedule}
                schedule_schema = SCH_SCHEDULE_ZON_OUTER

            try:
                full_schedule = schedule_schema(full_schedule)
            except vol.MultipleInvalid as err:
                raise exc.ScheduleError(f"failed to set schedule: {err}") from err

            if self.idx == "HW":
                # HACK: to avoid confusing dhw with zone '00'
                full_schedule[SZ_ZONE_IDX] = "00"

            return full_schedule

        full_schedule: _OuterSchedule = normalise_validate(schedule)
        self._fragments = full_sched_to_fragz(full_schedule)

        await self.tcs._obtain_lock(self.idx)  # maybe raise TimeOutError

        try:
            for num, frag in enumerate(self._fragments, 1):
                await put_fragment(num, len(self._fragments), frag)
        except TimeoutError as err:
            raise exc.ScheduleFlowError(f"failed to set schedule: {err}") from err
        else:
            if not force_refresh:
                self._global_ver, _ = await self.tcs._schedule_version(force_io=True)
                self._sched_ver = self._global_ver
        finally:
            self.tcs._release_lock()

        if force_refresh:
            await self.get_schedule(force_io=True)  # sets self._full_schedule
        else:
            self._full_schedule = full_schedule

        return self.schedule

    @property
    def schedule(self) -> InnerScheduleT | None:
        """Return the current (not full) schedule, if any."""
        if not self._full_schedule:  # can be {}
            return None
        result: InnerScheduleT = self._full_schedule.get(SZ_SCHEDULE)  # type: ignore[assignment]
        return result

    @property
    def version(self) -> int | None:
        """Return the version associated with the current schedule, if any."""
        return self._sched_ver if self._full_schedule else None


def _len(payload_set: _PayloadSetT) -> int:
    """Return the total number of fragments in the complete frag set.

    Return 0 if the expected set size is unknown (sentinel value as per RAMSES II).
    Uses len(payload_set) directly.

    :param payload_set: The current list of payloads.
    :return: The total expected fragments based on the set size.
    """
    return len(payload_set)


def fragz_to_full_sched(fragments: Iterable[_FragmentT]) -> _OuterSchedule:
    """Convert a tuple of fragments strs (a blob) into a schedule.

    :param fragments: An iterable of hexadecimal string fragments.
    :return: A parsed `_OuterSchedule` TypedDict representation.
    :raises zlib.error: On invalid payload compression stream.
    """

    def setpoint(value: int) -> dict[str, bool | float]:
        if value in (0, 1):
            return {SZ_ENABLED: bool(value)}
        return {SZ_HEAT_SETPOINT: value / 100}

    raw_schedule = zlib.decompress(bytearray.fromhex("".join(fragments)))

    old_day = 0
    schedule: InnerScheduleT = []
    switchpoints: SwitchPointsT = []

    idx: int
    dow: int
    tod: int
    val: int

    for i in range(0, len(raw_schedule), 20):
        idx, dow, tod, val = _struct_unpack(raw_schedule[i : i + 20])

        if dow > old_day:
            schedule.append({SZ_DAY_OF_WEEK: old_day, SZ_SWITCHPOINTS: switchpoints})
            old_day, switchpoints = dow, []

        switchpoint: SwitchPointDhw | SwitchPointZon = {
            SZ_TIME_OF_DAY: "{:02d}:{:02d}".format(*divmod(tod, 60))
        } | setpoint(val)  # type: ignore[assignment]
        switchpoints.append(switchpoint)  # type: ignore[arg-type]

    schedule.append({SZ_DAY_OF_WEEK: old_day, SZ_SWITCHPOINTS: switchpoints})

    return {SZ_ZONE_IDX: f"{idx:02X}", SZ_SCHEDULE: schedule}


def full_sched_to_fragz(full_schedule: _OuterSchedule) -> list[_FragmentT]:
    """Convert a schedule into a set of fragments (a blob).

    :param full_schedule: The `_OuterSchedule` dictionary
        representation.
    :return: A list of string fragments representing the zlib compressed
        binary.
    :raises KeyError: If expected keys are missing from the structure.
    """
    cobj = zlib.compressobj(level=9, wbits=14)
    frags: list[bytes] = []

    days_of_week: InnerScheduleT = full_schedule[SZ_SCHEDULE]
    for week_day in days_of_week:
        switchpoints: SwitchPointsT = week_day[SZ_SWITCHPOINTS]
        for switchpoint in switchpoints:
            frags.append(_struct_pack(full_schedule, week_day, switchpoint))

    blob = (b"".join(cobj.compress(f) for f in frags) + cobj.flush()).hex().upper()

    return [blob[i : i + 82] for i in range(0, len(blob), 82)]


def _struct_pack(
    full_schedule: OuterScheduleT,
    week_day: DayOfWeekT,
    switchpoint: SwitchPointDhw | SwitchPointZon,
) -> bytes:
    """Pack schedule information into bytes layout for transport.

    :param full_schedule: The outer schedule context.
    :param week_day: The specific day dict object.
    :param switchpoint: The specific time array dict object.
    :return: A bytes struct representing this switchpoint rule.
    """
    idx_: str = full_schedule[SZ_ZONE_IDX]
    dow_: int = week_day[SZ_DAY_OF_WEEK]
    tod_: str = switchpoint[SZ_TIME_OF_DAY]

    idx = int(idx_, 16)
    dow = int(dow_)
    tod = int(tod_[:2]) * 60 + int(tod_[3:])

    if SZ_HEAT_SETPOINT in switchpoint:
        val = int(
            switchpoint[SZ_HEAT_SETPOINT] * 100  # type: ignore[typeddict-item]
        )
    else:
        val = int(bool(switchpoint[SZ_ENABLED]))

    return struct.pack("<xxxxBxxxBxxxHxxHxx", idx, dow, tod, val)


def _struct_unpack(raw_schedule: bytes) -> tuple[int, int, int, int]:
    """Unpack a compressed RAMSES binary schedule format.

    :param raw_schedule: Uncompressed 20-byte block.
    :return: A tuple mapping (idx, day_of_week, time_of_day, value).
    """
    idx, dow, tod, val, _ = struct.unpack("<xxxxBxxxBxxxHxxHH", raw_schedule)
    return idx, dow, tod, val


# 16:27:56.942 000 RQ --- 18:006402 01:145038 --:------ 0006 001 00
# 16:27:56.958 038 RP --- 01:145038 18:006402 --:------ 0006 004 00050009

# 16:27:57.005 000 RQ --- 18:006402 01:145038 --:------ 0404 007 0120000800-0100
# 16:27:57.068 037 RP --- 01:145038 18:006402 --:------ 0404 048 0120000829-0103-68816DCFCB0980301045D1994C3E624916660956604596600516E1D285094112F566F5B80C072222A2
# 16:27:57.114 000 RQ --- 18:006402 01:145038 --:------ 0404 007 0120000800-0203
# 16:27:57.161 038 RP --- 01:145038 18:006402 --:------ 0404 048 0120000829-0203-52DF92C79CEA7EDA91C7F06997FDEFC620B287D6143C054FC153F01C780E3C079E03CFC033F00C3C03
# 16:27:57.202 000 RQ --- 18:006402 01:145038 --:------ 0404 007 0120000800-0303
# 16:27:57.245 038 RP --- 01:145038 18:006402 --:------ 0404 045 0120000826-0303-CF83E7C1F3E079F0CADC3E5E696BFECC944EED5BF5DEAD7AAD45F0227811BCD87937936E24CF

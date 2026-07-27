#!/usr/bin/env python3
"""RAMSES RF - Base classes and helpers for protocol commands."""

from __future__ import annotations

import logging
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, TypeVar

from .. import exceptions as exc
from ..address import HGI_DEV_ADDR, NON_DEV_ADDR, pkt_addrs
from ..const import DEVICE_ID_REGEX, FA, I_, W_, ZON_MODE_MAP
from ..frame import Frame, pkt_header
from ..typing import DeviceIdT, HeaderT, PayloadT

if TYPE_CHECKING:
    from ..const import Code, VerbT

_LOGGER = logging.getLogger(__name__)

_ZoneIdxT = TypeVar("_ZoneIdxT", int, str)
_T = TypeVar("_T", bound="CommandBase")


def _check_idx(zone_idx: int | str) -> str:
    """Validate and normalize a zone index or DHW index."""
    if not isinstance(zone_idx, int | str):
        raise exc.CommandInvalid(f"Invalid value for zone_idx: {zone_idx}")
    if isinstance(zone_idx, str):
        zone_idx = FA if zone_idx == "HW" else zone_idx
    result: int = zone_idx if isinstance(zone_idx, int) else int(zone_idx, 16)
    if 0 > result > 15 and result != 0xFA:
        raise exc.CommandInvalid(f"Invalid value for zone_idx: {result}")
    return f"{result:02X}"


def _normalise_mode(
    mode: int | str | None,
    target: bool | float | None,
    until: dt | str | None,
    duration: int | None,
) -> str:
    """Validate and normalize a heating mode for zone or DHW control."""
    if mode is None and target is None:
        raise exc.CommandInvalid(
            "Invalid args: One of mode or setpoint/active can't be None"
        )
    if until and duration:
        raise exc.CommandInvalid(
            "Invalid args: At least one of until or duration must be None"
        )

    if mode is None:
        if until:
            mode = ZON_MODE_MAP.TEMPORARY
        elif duration:
            mode = ZON_MODE_MAP.COUNTDOWN
        else:
            mode = ZON_MODE_MAP.PERMANENT  # TODO: advanced_override?
    elif isinstance(mode, int):
        mode = f"{mode:02X}"
    if mode not in ZON_MODE_MAP:
        mode = ZON_MODE_MAP._hex(mode)  # type: ignore[arg-type]  # may raise KeyError

    assert isinstance(mode, str)  # mypy check

    if mode != ZON_MODE_MAP.FOLLOW and target is None:
        raise exc.CommandInvalid(
            f"Invalid args: For {ZON_MODE_MAP[mode]}, setpoint/active can't be None"
        )

    return mode


def _normalise_until(
    mode: int | str | None,
    _: Any,
    until: dt | str | None,
    duration: int | None,
) -> tuple[Any, Any]:
    """Validate and normalize timing parameters for zone/DHW mode changes."""
    if mode == ZON_MODE_MAP.TEMPORARY:
        if duration is not None:
            raise exc.CommandInvalid(
                f"Invalid args: For mode={mode}, duration must be None"
            )
        if until is None:
            mode = ZON_MODE_MAP.ADVANCED  # or: until = dt.now() + td(hour=1)

    elif mode in ZON_MODE_MAP.COUNTDOWN:
        if duration is None:
            raise exc.CommandInvalid(
                f"Invalid args: For mode={mode}, duration can't be None"
            )
        if until is not None:
            raise exc.CommandInvalid(
                f"Invalid args: For mode={mode}, until must be None"
            )

    elif until is not None or duration is not None:
        raise exc.CommandInvalid(
            f"Invalid args: For mode={mode}, until and duration must both be None"
        )

    return until, duration  # TODO return updated mode for ZON_MODE_MAP.TEMPORARY ?


class CommandBase(Frame):
    """The Command class base (packets to be transmitted)."""

    def __init__(self, frame: str) -> None:
        """Create a command from a string (and its meta-attrs)."""

        try:
            super().__init__(frame)
        except exc.PacketInvalid as err:
            raise exc.CommandInvalid(err.message) from err

        try:
            self._validate(strict_checking=False)
        except exc.PacketInvalid as err:
            raise exc.CommandInvalid(err.message) from err

        try:
            self._validate(strict_checking=True)
        except exc.PacketInvalid as err:
            _LOGGER.warning(f"{self} < Command is potentially invalid: {err}")

        self._rx_header: HeaderT | None = None

    @classmethod  # convenience constructor
    def from_attrs(
        cls: type[_T],
        verb: VerbT,
        dest_id: DeviceIdT | str,
        code: Code,
        payload: PayloadT,
        *,
        from_id: DeviceIdT | str | None = None,
        seqn: int | str | None = None,
    ) -> _T:
        """Create a command from its attrs using a destination device_id."""

        from_id = from_id or HGI_DEV_ADDR.id

        addrs: tuple[DeviceIdT | str, DeviceIdT | str, DeviceIdT | str]

        if dest_id == from_id:
            addrs = (from_id, NON_DEV_ADDR.id, dest_id)
        else:
            addrs = (from_id, dest_id, NON_DEV_ADDR.id)

        return cls._from_attrs(
            verb,
            code,
            payload,
            addr0=addrs[0],
            addr1=addrs[1],
            addr2=addrs[2],
            seqn=seqn,
        )

    @classmethod  # generic constructor
    def _from_attrs(
        cls: type[_T],
        verb: str | VerbT,
        code: str | Code,
        payload: PayloadT,
        *,
        addr0: DeviceIdT | str | None = None,
        addr1: DeviceIdT | str | None = None,
        addr2: DeviceIdT | str | None = None,
        seqn: int | str | None = None,
    ) -> _T:
        """Create a command from its attrs using an address set."""

        verb = I_ if verb == "I" else W_ if verb == "W" else verb

        addr0 = addr0 or NON_DEV_ADDR.id
        addr1 = addr1 or NON_DEV_ADDR.id
        addr2 = addr2 or NON_DEV_ADDR.id

        if verb == "RQ" and addr0 == NON_DEV_ADDR.id and addr1 == NON_DEV_ADDR.id:
            raise exc.CommandInvalid(
                "Cannot generate RQ command without a valid target address"
            )

        _, _, *addrs = pkt_addrs(" ".join((str(addr0), str(addr1), str(addr2))))

        if seqn is None or seqn in ("", "-", "--", "---"):
            seqn = "---"
        elif isinstance(seqn, int):
            seqn = f"{int(seqn):03d}"

        frame = " ".join(
            (
                verb,
                seqn,
                *(a.id for a in addrs),
                code,
                f"{int(len(payload) / 2):03d}",
                payload,
            )
        )

        return cls(frame)

    @classmethod  # used by CLI for -x switch (NB: no len field)
    def from_cli(cls: type[_T], cmd_str: str) -> _T:
        """Create a command from a CLI string (the -x switch)."""

        parts = cmd_str.upper().split()
        if len(parts) < 4:
            raise exc.CommandInvalid(
                f"Command string is not parseable: '{cmd_str}'"
                ", format is: verb [seqn] addr0 [addr1 [addr2]] code payload"
            )

        verb = parts.pop(0)
        seqn = "---" if DEVICE_ID_REGEX.ANY.match(parts[0]) else parts.pop(0)
        payload = PayloadT(parts.pop()[:48])
        code = parts.pop()

        addrs: tuple[DeviceIdT | str, DeviceIdT | str, DeviceIdT | str]

        if not 0 < len(parts) < 4:
            raise exc.CommandInvalid(f"Command is invalid: '{cmd_str}'")
        elif len(parts) == 1 and verb == I_:
            addrs = (NON_DEV_ADDR.id, NON_DEV_ADDR.id, parts[0])
        elif len(parts) == 1:
            addrs = (HGI_DEV_ADDR.id, parts[0], NON_DEV_ADDR.id)
        elif len(parts) == 2 and parts[0] == parts[1]:
            addrs = (parts[0], NON_DEV_ADDR.id, parts[1])
        elif len(parts) == 2:
            addrs = (parts[0], parts[1], NON_DEV_ADDR.id)
        else:
            addrs = (parts[0], parts[1], parts[2])

        return cls._from_attrs(
            verb,
            code,
            payload,
            **{f"addr{k}": v for k, v in enumerate(addrs)},
            seqn=seqn,
        )

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object."""
        comment = f" # {self._hdr}{f' ({self._ctx})' if self._ctx else ''}"
        return f"... {self}{comment}"

    def __str__(self) -> str:
        """Return a brief readable string representation of this object."""
        return super().__repr__()

    def clone_with_source(self: _T, new_src: DeviceIdT | str) -> _T:
        """Return a new Command instance identical to this one, but with a new source address."""
        new_frame = (
            f"{self.verb} {self.seqn} {new_src} {self._addrs[1].id} {self._addrs[2].id} "
            f"{self.code} {int(self.len_):03d} {self.payload}"
        )
        return type(self)(new_frame)

    @property
    def tx_header(self) -> HeaderT:
        """Return the QoS header of this (request) packet."""
        return self._hdr

    @property
    def rx_header(self) -> HeaderT | None:
        """Return the QoS header of a corresponding response packet (if any)."""
        if self.tx_header and self._rx_header is None:
            self._rx_header = pkt_header(self, rx_header=True)
        return self._rx_header

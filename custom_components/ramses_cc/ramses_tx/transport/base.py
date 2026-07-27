#!/usr/bin/env python3
"""RAMSES RF - Base classes for RAMSES-II compatible packet transports."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, TypeAlias

from .. import exceptions as exc
from ..const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3, SZ_SIGNATURE
from ..helpers import dt_now
from ..interfaces import TransportInterface
from ..packet import Packet
from ..typing import DeviceIdT

if TYPE_CHECKING:
    from ..protocol import RamsesProtocolT

_LOGGER = logging.getLogger(__name__)

_MAX_TRACKED_TRANSMITS = 99
_MAX_TRACKED_DURATION = 300
_DBG_DISABLE_REGEX_WARNINGS = False


@dataclass
class TransportConfig:
    """Configuration parameters for Ramses transports.

    Replaces kwargs payload previously passed to transport and factories.
    """

    disable_sending: bool = False
    autostart: bool = False
    log_all: bool = False
    evofw_flag: str | None = None
    use_regex: dict[str, dict[str, str]] = field(default_factory=dict)
    timeout: float | None = None
    app_context: Any | None = None


class _BaseTransport:
    """Base class for all transports."""

    def __init__(self) -> None:
        pass


class _ReadTransport(_BaseTransport, TransportInterface):
    """Interface for read-only transports."""

    _protocol: RamsesProtocolT = None  # type: ignore[assignment]
    _loop: asyncio.AbstractEventLoop

    _is_hgi80: bool | None = None  # NOTE: None (unknown) is as False (is_evofw3)

    def __init__(
        self,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the read-only transport."""
        _BaseTransport.__init__(self)

        self._loop = loop or asyncio.get_event_loop()
        self._extra: dict[str, Any] = {} if extra is None else extra

        self._evofw_flag = config.evofw_flag

        self._closing: bool = False
        self._reading: bool = False

        self._this_pkt: Packet | None = None
        self._prev_pkt: Packet | None = None

        for key in (SZ_ACTIVE_HGI, SZ_SIGNATURE):
            self._extra.setdefault(key, None)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._protocol})"

    def _dt_now(self) -> dt:
        """Return a precise datetime, using last packet's dtm field."""
        try:
            return self._this_pkt.dtm  # type: ignore[union-attr]
        except AttributeError:
            return dt(1970, 1, 1, 1, 0)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The asyncio event loop as declared by SerialTransport."""
        return self._loop

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra information about the transport."""
        if name == SZ_IS_EVOFW3:
            return not self._is_hgi80
        return self._extra.get(name, default)

    def is_closing(self) -> bool:
        """Return True if the transport is closing or has closed."""
        return self._closing

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Inform the protocol that this transport has closed."""
        if self._closing:
            return
        self._closing = True

        self.loop.call_soon_threadsafe(
            functools.partial(self._protocol.connection_lost, exc)
        )

    def close(self) -> None:
        """Close the transport gracefully."""
        self._close()

    def is_reading(self) -> bool:
        """Return True if the transport is receiving."""
        return self._reading

    def pause_reading(self) -> None:
        """Pause the receiving end (no data to protocol.pkt_received())."""
        self._reading = False

    def resume_reading(self) -> None:
        """Resume the receiving end."""
        self._reading = True

    def _make_connection(self, gwy_id: DeviceIdT | None) -> None:
        """Register the connection with the protocol."""
        self._extra[SZ_ACTIVE_HGI] = gwy_id  # or HGI_DEV_ADDR.id

        self.loop.call_soon_threadsafe(
            functools.partial(self._protocol.connection_made, self, ramses=True)
        )

    def _frame_read(self, dtm_str: str, frame: str) -> None:
        """Make a Packet from the Frame and process it."""
        if not frame.strip():
            return

        try:
            pkt = Packet.from_file(dtm_str, frame)
        except ValueError as err:
            _LOGGER.debug("%s < PacketInvalid(%s)", frame, err)
            return
        except exc.PacketInvalid as err:
            _LOGGER.warning("%s < PacketInvalid(%s)", frame, err)
            return

        self._pkt_read(pkt)

    def _pkt_read(self, pkt: Packet) -> None:
        """Pass any valid Packets to the protocol's callback."""
        self._this_pkt, self._prev_pkt = pkt, self._this_pkt

        if self._closing is True:
            raise exc.TransportError("Transport is closing or has closed")

        try:
            self.loop.call_soon_threadsafe(self._protocol.pkt_received, pkt)
        except AssertionError as err:
            _LOGGER.exception("%s < exception from msg layer: %s", pkt, err)
        except exc.ProtocolError as err:
            _LOGGER.error("%s < exception from msg layer: %s", pkt, err)

    async def send_frame(self, frame: str) -> None:
        """Send a frame (alias for write_frame)."""
        await self.write_frame(frame)

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler."""
        raise exc.TransportSerialError("This transport is read only")


class _FullTransport(_ReadTransport):
    """Interface representing a bidirectional transport."""

    def __init__(
        self,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the full transport."""
        _ReadTransport.__init__(self, config=config, extra=extra, loop=loop)

        self._disable_sending = config.disable_sending
        self._transmit_times: deque[dt] = deque(maxlen=_MAX_TRACKED_TRANSMITS)

    def _dt_now(self) -> dt:
        """Get a precise datetime, using the current dtm."""
        return dt_now()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra info, including transmit rate calculations."""
        if name == "tx_rate":
            return self._report_transmit_rate()
        return super().get_extra_info(name, default=default)

    def _report_transmit_rate(self) -> float:
        """Return the transmit rate in transmits per minute."""
        now_dt = dt.now()
        dtm = now_dt - td(seconds=_MAX_TRACKED_DURATION)
        transmit_times = tuple(t for t in self._transmit_times if t > dtm)

        if len(transmit_times) <= 1:
            return float(len(transmit_times))

        duration: float = (transmit_times[-1] - transmit_times[0]) / td(seconds=1)
        return int(len(transmit_times) / duration * 6000) / 100

    def _track_transmit_rate(self) -> None:
        """Track the Tx rate as period of seconds per x transmits."""
        self._transmit_times.append(dt.now())
        _LOGGER.debug(f"Current Tx rate: {self._report_transmit_rate():.2f} pkts/min")

    def write(self, data: bytes) -> None:
        """Write the data to the underlying handler."""
        raise exc.TransportError("write() not implemented, use write_frame() instead")

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler."""
        if self._disable_sending is True:
            raise exc.TransportError("Sending has been disabled")
        if self._closing is True:
            raise exc.TransportError("Transport is closing or has closed")

        self._log_tx_packet(frame)
        self._track_transmit_rate()
        await self._write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport."""
        raise NotImplementedError("_write_frame() not implemented here")

    def _log_tx_packet(self, frame: str) -> None:
        """Emit outbound frames to the packet log for symmetry with inbound logs."""
        frame_clean = frame.strip()
        if not frame_clean:
            return

        if not frame_clean[:3].isdigit():
            frame_clean = f"000 {frame_clean}"

        try:
            Packet(dt_now(), frame_clean)
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("Failed to log Tx frame: %s", err)


_RegexRuleT: TypeAlias = dict[str, str]

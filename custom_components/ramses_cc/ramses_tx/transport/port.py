#!/usr/bin/env python3
"""RAMSES RF - Serial port packet transport.

For ser2net, use the following YAML with:
``ser2net -c misc/ser2net.yaml``

.. code-block::

    connection: &con00
    accepter: telnet(rfc2217),tcp,5001
    timeout: 0
    connector: serialdev,/dev/ttyUSB0,115200n81,local
    options:
        max-connections: 3

For ``socat``, see:

.. code-block::

    socat -dd pty,raw,echo=0 pty,raw,echo=0
    python client.py monitor /dev/pts/0
    cat packet.log | cut -d ' ' -f 2- | unix2dos > /dev/pts/1

For re-flashing evofw3 via Arduino IDE on *my* atmega328p (YMMV):

  - Board:      atmega328p (SW UART)
  - Bootloader: Old Bootloader
  - Processor:  atmega328p (5V, 16 MHz)
  - Host:       57600 (or 115200, YMMV)
  - Pinout:     Nano

For re-flashing evofw3 via Arduino IDE on *my* atmega32u4 (YMMV):

  - Board:      atmega32u4 (HW UART)
  - Processor:  atmega32u4 (5V, 16 MHz)
  - Pinout:     Pro Micro
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime as dt
from functools import wraps
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, cast

from serial import Serial, SerialException

from .. import exceptions as exc
from ..command import Command
from ..const import (
    DUTY_CYCLE_DURATION,
    MAX_DUTY_CYCLE_RATE,
    MIN_INTER_WRITE_GAP,
    SZ_ACTIVE_HGI,
    SZ_SIGNATURE,
    Code,
)
from ..discovery import is_hgi80
from ..packet import Packet
from ..typing import ExceptionT, SerPortNameT
from .base import TransportConfig, _FullTransport
from .helpers import _normalise, _str

if TYPE_CHECKING:
    from ..protocol import RamsesProtocolT

_LOGGER = logging.getLogger(__name__)

_SIGNATURE_GAP_SECS: Final[float] = 0.05
_SIGNATURE_MAX_TRYS: Final[int] = 40  # was: 24
_SIGNATURE_MAX_SECS: Final[int] = 3

_DBG_DISABLE_DUTY_CYCLE_LIMIT: Final[bool] = False
_DBG_FORCE_FRAME_LOGGING: Final[bool] = False

__all__ = [
    "PortTransport",
    "serial_asyncio",
]

try:
    import serial_asyncio_fast as serial_asyncio  # type: ignore[import-not-found, import-untyped, unused-ignore]

    _LOGGER.debug("Using pyserial-asyncio-fast in place of pyserial-asyncio")
except ImportError:
    import serial_asyncio  # type: ignore[import-not-found, import-untyped, unused-ignore, no-redef]


def limit_duty_cycle(
    max_duty_cycle: float, time_window: int = DUTY_CYCLE_DURATION
) -> Callable[..., Any]:
    """Limit the Tx rate to the RF duty cycle regulations (e.g. 1% per hour)."""
    TX_RATE_AVAIL: int = 38400  # bits per second (deemed)
    FILL_RATE: float = TX_RATE_AVAIL * max_duty_cycle  # bits per second
    BUCKET_CAPACITY: float = FILL_RATE * time_window

    def decorator(
        fnc: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        @wraps(fnc)
        async def wrapper(
            self: PortTransport, frame: str, *args: Any, **kwargs: Any
        ) -> None:
            # Lazy initialize the instance-bound duty cycle variables
            if self._tx_bits_in_bucket is None or self._tx_last_time_bit_added is None:
                self._tx_bits_in_bucket = BUCKET_CAPACITY
                self._tx_last_time_bit_added = perf_counter()

            rf_frame_size = 330 + len(frame[46:]) * 10

            elapsed_time = perf_counter() - self._tx_last_time_bit_added
            self._tx_bits_in_bucket = min(
                self._tx_bits_in_bucket + elapsed_time * FILL_RATE,
                BUCKET_CAPACITY,
            )
            self._tx_last_time_bit_added = perf_counter()

            if _DBG_DISABLE_DUTY_CYCLE_LIMIT:
                self._tx_bits_in_bucket = BUCKET_CAPACITY

            if self._tx_bits_in_bucket < rf_frame_size:
                await asyncio.sleep(
                    (rf_frame_size - self._tx_bits_in_bucket) / FILL_RATE
                )

            try:
                await fnc(self, frame, *args, **kwargs)
            finally:
                if self._tx_bits_in_bucket is not None:
                    self._tx_bits_in_bucket -= rf_frame_size

        @wraps(fnc)
        async def null_wrapper(
            self: PortTransport, frame: str, *args: Any, **kwargs: Any
        ) -> None:
            await fnc(self, frame, *args, **kwargs)

        if 0 < max_duty_cycle <= 1:
            return wrapper

        return null_wrapper

    return decorator


class _PortTransportAbstractor(serial_asyncio.SerialTransport):
    """Do the bare minimum to abstract a transport from its underlying class."""

    serial: Serial

    def __init__(
        self,
        serial_instance: Serial,
        protocol: RamsesProtocolT,
        /,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the port transport abstractor."""
        super().__init__(loop or asyncio.get_event_loop(), protocol, serial_instance)


class PortTransport(_FullTransport, _PortTransportAbstractor):  # type: ignore[misc]
    """Send/receive packets async to/from evofw3/HGI80 via a serial port.

    See: https://github.com/ghoti57/evofw3
    """

    _init_fut: asyncio.Future[Packet | None]
    _init_task: asyncio.Task[None]

    _recv_buffer: bytes = b""

    _tx_bits_in_bucket: float | None = None
    _tx_last_time_bit_added: float | None = None

    def __init__(
        self,
        serial_instance: Serial,
        protocol: RamsesProtocolT,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the port transport."""
        _PortTransportAbstractor.__init__(self, serial_instance, protocol, loop=loop)
        _FullTransport.__init__(self, config=config, extra=extra, loop=loop)

        self._tx_bits_in_bucket = None
        self._tx_last_time_bit_added = None
        self._log_all = config.log_all

        self._init_fut = self._loop.create_future()

        self._leaker_sem = asyncio.BoundedSemaphore()
        self._leaker_task = self._loop.create_task(
            self._leak_sem(), name="PortTransport._leak_sem()"
        )

        self._loop.create_task(
            self._create_connection(),
            name="PortTransport._create_connection()",
        )

    async def _create_connection(self) -> None:
        """Invoke connection_made() callback after HGI80 discovery."""
        self._is_hgi80 = await is_hgi80(cast(SerPortNameT, self.serial.name or ""))

        async def connect_sans_signature() -> None:
            """Call connection_made() without sending/waiting for a
            signature.
            """
            self._init_fut.set_result(None)
            self._make_connection(gwy_id=None)

        async def connect_with_signature() -> None:
            """Poll port with signatures, call connection_made() after
            first echo.
            """
            sig = Command._puzzle()
            self._extra[SZ_SIGNATURE] = sig.payload

            num_sends = 0
            while num_sends < _SIGNATURE_MAX_TRYS:
                num_sends += 1

                await self._write_frame(str(sig))
                await asyncio.sleep(_SIGNATURE_GAP_SECS)

                if self._init_fut.done():
                    pkt = self._init_fut.result()
                    self._make_connection(gwy_id=pkt.src.id if pkt else None)
                    return

            if not self._init_fut.done():
                self._init_fut.set_result(None)

            self._make_connection(gwy_id=None)
            return

        if self._disable_sending:
            self._init_task = self._loop.create_task(
                connect_sans_signature(),
                name="PortTransport.connect_sans_signature()",
            )
        else:
            self._init_task = self._loop.create_task(
                connect_with_signature(),
                name="PortTransport.connect_with_signature()",
            )

        try:
            await asyncio.wait_for(self._init_fut, timeout=_SIGNATURE_MAX_SECS)
        except TimeoutError as err:
            raise exc.TransportSerialError(
                f"Failed to initialise Transport within {_SIGNATURE_MAX_SECS} secs"
            ) from err

    async def _leak_sem(self) -> None:
        """Used to enforce a minimum time between calls to self.write()."""
        while True:
            await asyncio.sleep(MIN_INTER_WRITE_GAP)
            with contextlib.suppress(ValueError):
                self._leaker_sem.release()

    def _read_ready(self) -> None:
        """Make Frames from the read data and process them."""

        def bytes_read(data: bytes) -> Iterable[tuple[dt, bytes]]:
            self._recv_buffer += data
            if b"\r\n" in self._recv_buffer:
                lines = self._recv_buffer.split(b"\r\n")
                self._recv_buffer = lines[-1]
                for line in lines[:-1]:
                    yield self._dt_now(), line + b"\r\n"

        try:
            data: bytes = self.serial.read(self._max_read_size)
        except SerialException as err:
            if not self._closing:
                self._close(exc=exc.TransportSerialError(err))
            return

        if not data:
            return

        for dtm, raw_line in bytes_read(data):
            if _DBG_FORCE_FRAME_LOGGING:
                _LOGGER.warning("Rx: %s", raw_line)
            elif _LOGGER.getEffectiveLevel() == logging.INFO:
                _LOGGER.info("Rx: %s", raw_line)

            self._frame_read(
                dtm.isoformat(timespec="milliseconds"),
                _normalise(_str(raw_line)),
            )

    def _pkt_read(self, pkt: Packet) -> None:
        if (
            not self._init_fut.done()
            and pkt.code == Code._PUZZ
            and pkt.payload == self._extra.get(SZ_SIGNATURE)
        ):
            self._extra[SZ_ACTIVE_HGI] = pkt.src.id
            self._init_fut.set_result(pkt)

        super()._pkt_read(pkt)

    @limit_duty_cycle(MAX_DUTY_CYCLE_RATE)
    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler (e.g. serial port,
        MQTT).
        """
        await self._leaker_sem.acquire()
        await super().write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport."""
        data = bytes(frame, "ascii") + b"\r\n"

        log_msg = f"Serial transport Tx frame: {frame}"
        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning(log_msg)
        elif _LOGGER.getEffectiveLevel() > logging.DEBUG or self._log_all:
            _LOGGER.info(log_msg)
        else:
            _LOGGER.debug(log_msg)

        try:
            self._write(data)
        except SerialException as err:
            self._abort(cast(Any, exc.TransportSerialError(err)))
            return

    def _write(self, data: bytes) -> None:
        """Perform the actual write to the serial port."""
        self.serial.write(data)

    def _abort(self, exc: ExceptionT) -> None:  # type: ignore[override]
        """Abort the transport."""
        super()._abort(exc)

        if hasattr(self, "_init_task") and self._init_task:
            self._init_task.cancel()
        if hasattr(self, "_leaker_task") and self._leaker_task:
            self._leaker_task.cancel()

    def _close(  # type: ignore[override]
        self, exc: exc.RamsesException | None = None
    ) -> None:
        """Close the transport (cancel any outstanding tasks)."""
        super()._close(exc)

        if init_task := getattr(self, "_init_task", None):
            init_task.cancel()

        if leaker_task := getattr(self, "_leaker_task", None):
            leaker_task.cancel()

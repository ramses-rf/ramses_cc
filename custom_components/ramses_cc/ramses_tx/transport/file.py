#!/usr/bin/env python3
"""RAMSES RF - File-based packet transport."""

from __future__ import annotations

import asyncio
import functools
import logging
from io import TextIOWrapper
from typing import TYPE_CHECKING, Any

import aiofiles  # type: ignore[import-untyped]

from ..const import SZ_READER_TASK
from ..exceptions import RamsesException, TransportSourceInvalid
from .base import TransportConfig, _ReadTransport

if TYPE_CHECKING:
    from ..protocol import RamsesProtocolT

_LOGGER = logging.getLogger(__name__)


class _FileTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying class."""

    def __init__(
        self,
        pkt_source: dict[str, str] | str | TextIOWrapper,
        protocol: RamsesProtocolT,
        /,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the file transport abstractor."""
        self._pkt_source = pkt_source
        self._protocol = protocol
        self._loop = loop or asyncio.get_event_loop()


class FileTransport(_ReadTransport, _FileTransportAbstractor):
    """Receive packets from a read-only source such as packet log or a dict."""

    def __init__(
        self,
        pkt_source: dict[str, str] | str | TextIOWrapper,
        protocol: RamsesProtocolT,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the file transport."""
        if not config.disable_sending:
            raise TransportSourceInvalid("This Transport cannot send packets")

        _FileTransportAbstractor.__init__(self, pkt_source, protocol, loop=loop)
        _ReadTransport.__init__(self, config=config, extra=extra, loop=loop)

        self._evt_reading = asyncio.Event()

        self._extra[SZ_READER_TASK] = self._reader_task = self._loop.create_task(
            self._start_reader(), name="FileTransport._start_reader()"
        )

        self._make_connection(None)

    async def _start_reader(self) -> None:
        """Start the reader task."""
        self._reading = True
        self._evt_reading.set()  # Start in reading state

        try:
            await self._producer_loop()
        except asyncio.CancelledError:
            # CancelledError is a BaseException, so we pass None to indicate a clean close
            self._loop.call_soon_threadsafe(
                functools.partial(self._protocol.connection_lost, None)
            )
            raise
        except (RamsesException, OSError) as err:
            self._loop.call_soon_threadsafe(
                functools.partial(self._protocol.connection_lost, err)
            )
        else:
            self._loop.call_soon_threadsafe(
                functools.partial(self._protocol.connection_lost, None)
            )

    def pause_reading(self) -> None:
        """Pause the receiving end (no data to protocol.pkt_received())."""
        self._reading = False
        self._evt_reading.clear()  # Puts the loop to sleep efficiently

    def resume_reading(self) -> None:
        """Resume the receiving end."""
        self._reading = True
        self._evt_reading.set()  # Wakes the loop immediately

    async def _producer_loop(self) -> None:
        """Loop through the packet source for Frames and process them."""
        if isinstance(self._pkt_source, dict):
            for dtm_str, pkt_line in self._pkt_source.items():
                await self._process_line(dtm_str, pkt_line)

        elif isinstance(self._pkt_source, str):
            try:
                # Removed redundant mode="r" to satisfy Ruff UP015
                async with aiofiles.open(self._pkt_source, encoding="utf-8") as file:
                    async for dtm_pkt_line in file:
                        await self._process_line_from_raw(dtm_pkt_line)
            except FileNotFoundError as err:
                _LOGGER.warning(f"Correct the packet file name; {err}")

        elif isinstance(self._pkt_source, TextIOWrapper):
            # Wrap the synchronous TextIOWrapper for asynchronous iteration
            async_file = aiofiles.wrap(self._pkt_source)
            async for dtm_pkt_line in async_file:
                await self._process_line_from_raw(dtm_pkt_line)

        else:
            raise TransportSourceInvalid(
                f"Packet source is not dict, TextIOWrapper or str: {self._pkt_source!r}"
            )

    async def _process_line_from_raw(self, line: str) -> None:
        """Helper to process raw lines."""
        if (line := line.strip()) and line[:1] != "#":
            await self._process_line(line[:26], line[27:])

    async def _process_line(self, dtm_str: str, frame: str) -> None:
        """Push frame to protocol in a thread-safe way."""
        await self._evt_reading.wait()
        self._frame_read(dtm_str, frame)
        await asyncio.sleep(0)

    def _close(self, exc: RamsesException | None = None) -> None:
        """Close the transport (cancel any outstanding tasks)."""
        super()._close(exc)

        if hasattr(self, "_reader_task") and self._reader_task:
            self._reader_task.cancel()

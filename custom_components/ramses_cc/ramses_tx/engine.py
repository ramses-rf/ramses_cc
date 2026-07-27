#!/usr/bin/env python3

"""RAMSES RF - The serial to RF engine."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, Never

from .address import ALL_DEV_ADDR, HGI_DEV_ADDR, NON_DEV_ADDR
from .command import Command
from .const import (
    DEFAULT_DISABLE_QOS,
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    DEFAULT_WAIT_FOR_REPLY,
    SZ_ACTIVE_HGI,
    Code,
    Priority,
)
from .dtos import PacketDTO
from .packet import Packet
from .protocol import protocol_factory
from .schemas import (
    SZ_PACKET_LOG,
    SZ_PORT_CONFIG,
    SZ_PORT_NAME,
    select_device_filter_mode,
)
from .transport import TransportConfig, transport_factory
from .typing import PktLogConfigT, PortConfigT, QosParams

if TYPE_CHECKING:
    from .config import EngineConfig
    from .const import VerbT
    from .protocol import RamsesProtocolT
    from .transport import RamsesTransportT
    from .typing import DeviceIdT, MsgHandlerT, PayloadT


DEV_MODE = False

_LOGGER = logging.getLogger(__name__)


class Engine:
    """The engine class.

    Manages the transport layer, protocol binding, task registry, and
    asynchronous command dispatching for the RF network.
    """

    def __init__(
        self,
        config: EngineConfig,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None = None,
    ) -> None:
        self.config = config

        if self.config.port_name and self.config.input_file:
            _LOGGER.warning(
                "Port (%s) specified, so file (%s) ignored",
                self.config.port_name,
                self.config.input_file,
            )
            self.config.input_file = None

        self._disable_sending = self.config.disable_sending
        if self.config.input_file:
            self._disable_sending = True
        elif not self.config.port_name:
            raise TypeError("Either a port_name or an input_file must be specified")

        self.ser_name = self.config.port_name
        self._input_file = self.config.input_file

        self._port_config: PortConfigT | dict[Never, Never] = (
            self.config.port_config or {}
        )
        self._packet_log: PktLogConfigT | dict[Never, Never] = (
            self.config.packet_log or {}
        )
        self._loop = loop or asyncio.get_running_loop()

        self._exclude: list[str] = self.config.block_list or []
        self._include: list[str] = self.config.known_list or []
        self._unwanted: list[DeviceIdT] = [
            NON_DEV_ADDR.id,
            ALL_DEV_ADDR.id,
            "01:000001",  # type: ignore[list-item]  # why this one?
        ]
        self._enforce_known_list = select_device_filter_mode(
            self.config.enforce_known_list,
            self._include,
            self._exclude,
        )
        self._log_all_mqtt = self.config.log_all_mqtt
        self._evofw_flag = self.config.evofw_flag
        self._use_regex = self.config.use_regex or {}
        self._disable_qos = (
            self.config.disable_qos
            if self.config.disable_qos is not None
            else DEFAULT_DISABLE_QOS
        )

        self._transport_constructor = transport_constructor
        self._app_context = self.config.app_context

        self._hgi_id = self.config.hgi_id

        self._engine_lock = asyncio.Lock()
        self._engine_state: (
            tuple[MsgHandlerT | None, bool | None, *tuple[Any, ...]] | None
        ) = None

        self._protocol: RamsesProtocolT = None  # type: ignore[assignment]
        self._transport: RamsesTransportT | None = None

        # Thread-safe lock for task registry modifications
        self._tasks_lock = threading.Lock()
        self._tasks: list[asyncio.Task[Any]] = []

        self._set_msg_handler(self._msg_handler)

    def __str__(self) -> str:
        if self._hgi_id:
            return f"{self._hgi_id} ({self.ser_name})"

        if not self._transport:
            return f"{HGI_DEV_ADDR.id} ({self.ser_name})"

        device_id = self._transport.get_extra_info(
            SZ_ACTIVE_HGI, default=HGI_DEV_ADDR.id
        )
        return f"{device_id} ({self.ser_name})"

    def _dt_now(self) -> dt:
        timesource: Callable[[], dt] = getattr(self._transport, "_dt_now", dt.now)
        return timesource()

    def _set_msg_handler(self, msg_handler: MsgHandlerT) -> None:
        """Create an appropriate protocol for the packet source."""
        self._protocol = protocol_factory(
            msg_handler,
            disable_sending=self._disable_sending,
            disable_qos=self._disable_qos,
            enforce_include_list=self._enforce_known_list,
            exclude_list=self._exclude,
            include_list=self._include,
            hgi_id=self._hgi_id,
        )

    def add_msg_handler(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        msg_filter: Callable[[PacketDTO], bool] | None = None,
    ) -> Callable[[], None]:
        """Add a Message handler to the underlying Protocol."""
        return self._protocol.add_handler(msg_handler, msg_filter=msg_filter)

    def add_raw_pkt_handler(
        self,
        msg_handler: MsgHandlerT,
        /,
    ) -> Callable[[], None]:
        """Add a raw packet handler that fires before the device ID filter.

        See ``_BaseProtocol.add_raw_pkt_handler`` for details.
        """
        return self._protocol.add_raw_pkt_handler(msg_handler)

    async def start(self) -> None:
        """Create a suitable transport for the specified packet source.

        Initiate receiving (Messages) and sending (Commands).
        """
        pkt_source: dict[str, Any] = {}
        if self.ser_name:
            pkt_source[SZ_PORT_NAME] = self.ser_name
            pkt_source[SZ_PORT_CONFIG] = self._port_config
        else:
            pkt_source[SZ_PACKET_LOG] = self._input_file

        transport_config = TransportConfig(
            disable_sending=bool(self._disable_sending),
            log_all=bool(self._log_all_mqtt),
            evofw_flag=self._evofw_flag,
            use_regex=self._use_regex,
            app_context=self._app_context,
        )

        extra_info: dict[str, Any] = {}
        if self._hgi_id:
            extra_info[SZ_ACTIVE_HGI] = self._hgi_id

        self._transport = await transport_factory(
            self._protocol,
            config=transport_config,
            loop=self._loop,
            transport_constructor=self._transport_constructor,
            extra=extra_info if extra_info else None,
            **pkt_source,
        )

        await self._protocol.wait_for_connection_made()

        if self._input_file:
            await self._protocol.wait_for_connection_lost(timeout=86400)
            # timeout set to timeout=86400, to stop type checker complaint if
            # sent to None

    async def stop(self) -> None:
        """Close the transport (will stop the protocol)."""
        self._disable_sending = True

        # Shutdown Safety - securely lock the task registry to clean up
        with self._tasks_lock:
            tasks = [t for t in self._tasks if not t.done()]
            for t in tasks:
                t.cancel()

        if tasks:
            await asyncio.wait(tasks)

        # Clear any unretrieved exceptions from background tasks securely
        with self._tasks_lock:
            for task in self._tasks:
                if task.done() and not task.cancelled():
                    if exc := task.exception():
                        _LOGGER.debug(
                            "Background task %s failed: %s",
                            task.get_name(),
                            exc,
                        )

        if self._transport:
            self._transport.close()
            await self._protocol.wait_for_connection_lost()

        return None

    async def _drop_msg(self, msg: PacketDTO) -> None:
        """Discard messages silently while paused."""
        _LOGGER.info("Message dropped while engine paused: %s", msg)

    async def _pause(self, *args: Any) -> None:
        """Pause the (active) engine or raise a RuntimeError."""
        if self._engine_lock.locked():
            raise RuntimeError("Unable to pause engine, failed to acquire lock")

        await self._engine_lock.acquire()
        try:
            if self._engine_state is not None:
                raise RuntimeError("Unable to pause engine, it is already paused")

            # Secure state transition within lock
            self._engine_state = (None, None, tuple())
        finally:
            self._engine_lock.release()

        # Schedule transport pauses cleanly via the event loop
        self._loop.call_soon(self._protocol.pause_writing)
        if self._transport:
            pause_reading = getattr(self._transport, "pause_reading", None)
            if pause_reading:
                self._loop.call_soon(pause_reading)

        self._protocol._msg_handler, handler = (
            self._drop_msg,
            self._protocol._msg_handler,
        )

        self._disable_sending, read_only = True, self._disable_sending

        self._engine_state = (handler, read_only, *args)

    async def _resume(self) -> tuple[Any, ...]:
        """Resume the (paused) engine or raise a RuntimeError."""
        args: tuple[Any, ...]

        try:
            await asyncio.wait_for(self._engine_lock.acquire(), timeout=0.1)
        except TimeoutError as err:
            raise RuntimeError(
                "Unable to resume engine, failed to acquire lock"
            ) from err

        try:
            if self._engine_state is None:
                raise RuntimeError("Unable to resume engine, it was not paused")

            # Atomic restoration of state inside the lock
            self._protocol._msg_handler, self._disable_sending, *args = (
                self._engine_state  # type: ignore[assignment]
            )
            self._engine_state = None
        finally:
            self._engine_lock.release()

        # Schedule transport resumes cleanly via the event loop
        if self._transport:
            resume_reading = getattr(self._transport, "resume_reading", None)
            if resume_reading:
                self._loop.call_soon(resume_reading)
        if not self._disable_sending:
            self._loop.call_soon(self._protocol.resume_writing)

        return tuple(args)

    def add_task(self, task: asyncio.Task[Any]) -> None:
        """Keep a track of tasks securely, so we can tidy-up."""
        with self._tasks_lock:
            self._tasks = [t for t in self._tasks if not t.done()]
            self._tasks.append(task)

    @staticmethod
    def create_cmd(
        verb: VerbT,
        device_id: DeviceIdT,
        code: Code,
        payload: PayloadT,
        *,
        from_id: str | None = None,
        seqn: str | None = None,
    ) -> Command:
        """Make a command addressed to device_id."""
        kwargs = {}
        if from_id is not None:
            kwargs["from_id"] = from_id
        if seqn is not None:
            kwargs["seqn"] = seqn

        return Command.from_attrs(verb, device_id, code, payload, **kwargs)

    async def async_send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> Packet:
        """Send a Command and return the corresponding Packet."""
        qos = QosParams(
            max_retries=max_retries,
            timeout=timeout,
            wait_for_reply=wait_for_reply,
        )

        return await self._protocol.send_cmd(
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            qos=qos,
        )

    async def _msg_handler(self, msg: PacketDTO) -> None:
        """Process incoming messages from the protocol."""
        # Safely pass execution to Gateway's extended handling logic
        handler = getattr(self, "_handle_msg", None)
        if handler:
            res = handler(msg)
            if asyncio.iscoroutine(res):
                await res

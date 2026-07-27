#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet protocol finite state machine.

This module manages the state transitions and orchestrates commands using
the dedicated QoS manager.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import timedelta as td
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from ..address import HGI_DEVICE_ID
from ..command import Command
from ..const import (
    DEFAULT_BUFFER_SIZE,
    DEFAULT_ECHO_TIMEOUT,
    DEFAULT_RPLY_TIMEOUT,
    MAX_RETRY_LIMIT,
    MAX_SEND_TIMEOUT,
    Code,
    Priority,
)
from ..exceptions import (
    PacketPayloadInvalid,
    ProtocolError,
    ProtocolFsmError,
    ProtocolSendFailed,
    ProtocolTimeoutError,
    TransportError,
)
from ..helpers import dt_now
from ..interfaces import StateMachineInterface, TransportInterface
from ..packet import Packet
from ..typing import HeaderT, QosParams
from .qos import QosManager

if TYPE_CHECKING:
    from .core import RamsesProtocolT


_DBG_MAINTAIN_STATE_CHAIN: Final[bool] = False
_DBG_USE_STRICT_TRANSITIONS: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


class ProtocolContext(StateMachineInterface):
    """The context for the protocol finite state machine."""

    SEND_TIMEOUT_LIMIT = MAX_SEND_TIMEOUT

    def __init__(
        self,
        protocol: RamsesProtocolT,
        /,
        *,
        echo_timeout: float = DEFAULT_ECHO_TIMEOUT,
        reply_timeout: float = DEFAULT_RPLY_TIMEOUT,
        max_retry_limit: int = MAX_RETRY_LIMIT,
        max_buffer_size: int = DEFAULT_BUFFER_SIZE,
    ) -> None:
        """Initialize the protocol state machine context.

        :param protocol: The protocol instance using this context.
        :type protocol: RamsesProtocolT
        :param echo_timeout: Timeout for an echo response.
        :type echo_timeout: float
        :param reply_timeout: Timeout for a full reply response.
        :type reply_timeout: float
        :param max_retry_limit: Maximum number of times to retry a send.
        :type max_retry_limit: int
        :param max_buffer_size: Maximum size of the command queue.
        :type max_buffer_size: int
        """
        self._protocol = protocol
        self._loop = protocol._loop

        # Delegate QoS state management to the isolated QosManager
        self._qos_mgr = QosManager(
            self._loop,
            echo_timeout=echo_timeout,
            reply_timeout=reply_timeout,
            max_retry_limit=max_retry_limit,
            max_buffer_size=max_buffer_size,
        )

        self._expiry_timer: asyncio.Task[None] | None = None
        self._state: _ProtocolStateT = None  # type: ignore[assignment]

        self._send_fnc: Callable[[Command], Coroutine[Any, Any, None]] = None  # type: ignore[assignment]

        # Track send_fnc_wrapper tasks so they can be cancelled on teardown.
        # These are created by _send_cmd() via loop.create_task() and are NOT
        # visible to the Engine's task registry, so without tracking them here
        # they linger as "Lingering task" errors in tests and can block
        # gateway.stop() in production.
        self._send_tasks: set[asyncio.Task[None]] = set()

        self.set_state(Inactive)

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object."""
        msg = f"<ProtocolContext state={repr(self._state)[21:-1]}"
        if not self._qos_mgr.is_active:
            return msg + ">"
        if self._qos_mgr.tx_count == 0:
            return msg + ", tx_count=0/0>"
        return msg + f", tx_count={self._qos_mgr.tx_count}/{self._qos_mgr.tx_limit}>"

    # Properties to maintain backward compatibility with the test suite
    @property
    def max_retry_limit(self) -> int:
        return self._qos_mgr.max_retry_limit

    @max_retry_limit.setter
    def max_retry_limit(self, value: int) -> None:
        self._qos_mgr.max_retry_limit = value

    @property
    def echo_timeout(self) -> float:
        return self._qos_mgr.echo_timeout

    @property
    def reply_timeout(self) -> float:
        return self._qos_mgr.reply_timeout

    @property
    def qsize(self) -> int:
        return self._qos_mgr.qsize

    @property
    def _cmd_tx_count(self) -> int:
        return self._qos_mgr.tx_count

    @property
    def _cmd_tx_limit(self) -> int:
        return self._qos_mgr.tx_limit

    @property
    def is_sending(self) -> bool:
        """Return True if the context is currently sending a command."""
        return isinstance(self._state, WantEcho | WantRply)

    @property
    def state(self) -> _ProtocolStateT:
        """Return the current state of the FSM."""
        return self._state

    def set_state(
        self,
        state_class: _ProtocolStateClassT,
        expired: bool = False,
        timed_out: bool = False,
        exception: Exception | None = None,
        result: Packet | None = None,
    ) -> None:
        """Transition the state machine to a new state.

        :param state_class: The new state class to transition to.
        :type state_class: _ProtocolStateClassT
        :param expired: Whether the state transition is due to a full expiry.
        :type expired: bool
        :param timed_out: Whether the state transition is due to a timeout.
        :type timed_out: bool
        :param exception: Any exception that caused the state transition.
        :type exception: Exception | None
        :param result: Any resulting packet associated with the transition.
        :type result: Packet | None
        """

        async def expire_state_on_timeout() -> None:
            delay, old_val = self._qos_mgr.get_and_update_delay(
                isinstance(self._state, WantEcho)
            )
            await asyncio.sleep(delay)
            self._qos_mgr.restore_multiplier(old_val)

            level = (
                logging.DEBUG
                if self._qos_mgr.tx_count < 3
                else logging.INFO
                if self._qos_mgr.tx_count == 3
                else logging.WARNING
            )
            state_str = "echo" if isinstance(self._state, WantEcho) else "reply"
            _LOGGER.log(
                level,
                f"Timeout expired waiting for {state_str}: {self} (delay={delay})",
            )

            if self._qos_mgr.tx_count < self._qos_mgr.tx_limit:
                self.set_state(WantEcho, timed_out=True)
            else:
                self.set_state(IsInIdle, expired=True)

        def effect_state(timed_out: bool) -> None:
            if timed_out and self._qos_mgr.cmd is not None:
                self._send_cmd(self._qos_mgr.cmd, is_retry=True)

            if isinstance(self._state, IsInIdle):
                self._loop.call_soon_threadsafe(self._check_buffer_for_cmd)
            elif (
                isinstance(self._state, WantRply)
                and self._qos_mgr.qos
                and not self._qos_mgr.qos.wait_for_reply
            ):
                self.set_state(IsInIdle, result=self._state._echo_pkt)
            elif isinstance(self._state, WantEcho | WantRply):
                self._expiry_timer = self._loop.create_task(expire_state_on_timeout())

        if self._expiry_timer is not None:
            self._expiry_timer.cancel("Changing state")
            self._expiry_timer = None

        current_state_name = self._state.__class__.__name__
        new_state_name = state_class.__name__
        transition = f"{current_state_name}->{new_state_name}"

        if self._qos_mgr.fut is None:
            _LOGGER.debug(
                f"FSM state changed {transition}: no active future (ctx={self})"
            )
        elif self._qos_mgr.fut.cancelled() and not isinstance(self._state, IsInIdle):
            _LOGGER.debug(
                f"FSM state changed {transition}: future cancelled (expired={expired}, ctx={self})"
            )
        elif exception:
            _LOGGER.debug(
                f"FSM state changed {transition}: exception occurred (error={exception}, ctx={self})"
            )
            if not self._qos_mgr.fut.done():
                self._qos_mgr.fut.set_exception(exception)
        elif result:
            _LOGGER.debug(
                f"FSM state changed {transition}: result received (result={result._hdr}, ctx={self})"
            )
            if not self._qos_mgr.fut.done():
                self._qos_mgr.fut.set_result(result)
        elif expired:
            _LOGGER.debug(f"FSM state changed {transition}: timer expired (ctx={self})")
            if not self._qos_mgr.fut.done():
                self._qos_mgr.fut.set_exception(
                    ProtocolSendFailed(f"{self}: Exceeded maximum retries")
                )
        else:
            _LOGGER.debug(f"FSM state changed {transition}: successful (ctx={self})")

        prev_state = self._state
        self._state = state_class(self)

        if _DBG_MAINTAIN_STATE_CHAIN:
            setattr(self._state, "_prev_state", prev_state)  # noqa: B010

        if timed_out:
            self._qos_mgr.tx_count += 1
        elif isinstance(self._state, WantEcho):
            self._qos_mgr.tx_count = 1
        elif not isinstance(self._state, WantRply):
            self._qos_mgr.reset_active()

        self._loop.call_soon_threadsafe(effect_state, timed_out)

    def connection_made(self, transport: TransportInterface) -> None:
        """Handle the transport connection being made."""
        self._state.connection_made()

    def connection_lost(self, err: Exception | None) -> None:
        """Handle the transport connection being lost."""
        # Cancel any in-flight send_fnc_wrapper tasks that are not tracked
        # by the Engine's task registry.  Without this, they linger as
        # "Lingering task" errors and can block gateway.stop().
        for task in list(self._send_tasks):
            task.cancel()
        self._send_tasks.clear()

        self._state.connection_lost()

    def pkt_received(self, pkt: Packet) -> None:
        """Process a received packet (echo or reply)."""
        self._state.pkt_rcvd(pkt)

    def pause_writing(self) -> None:
        """Handle the transport pausing writing."""
        self._state.writing_paused()

    def resume_writing(self) -> None:
        """Handle the transport resuming writing."""
        self._state.writing_resumed()

    async def send_cmd(
        self,
        send_fnc: Callable[[Command], Coroutine[Any, Any, None]],
        cmd: Command,
        priority: Priority,
        qos: QosParams,
    ) -> Packet:
        """Send a Command with QoS (retries, until success or Exception).

        :param send_fnc: The function used to actually transmit the command.
        :type send_fnc: Callable[[Command], Coroutine[Any, Any, None]]
        :param cmd: The command to send.
        :type cmd: Command
        :param priority: The transmission priority.
        :type priority: Priority
        :param qos: Quality of Service parameters.
        :type qos: QosParams
        :return: The received response packet, or the echo if no response is expected.
        :rtype: Packet
        :raises ProtocolTimeoutError: If the global send timer expires.
        :raises ProtocolSendFailed: If retries are exhausted.
        """
        self._send_fnc = send_fnc

        if isinstance(self._state, Inactive):
            raise ProtocolSendFailed(f"{self}: Send failed (no active transport?)")

        fut = self._qos_mgr.enqueue(priority, cmd, qos)

        if isinstance(self._state, IsInIdle):
            self._loop.call_soon_threadsafe(self._check_buffer_for_cmd)

        timeout = min(qos.timeout, self.SEND_TIMEOUT_LIMIT)
        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as err:
            msg = f"{self}: Expired global timer after {timeout} sec"
            _LOGGER.warning(
                "TOUT.. = %s: send_timeout=%s (%s)",
                self,
                timeout,
                self._qos_mgr.cmd is cmd,
            )
            if self._qos_mgr.cmd is cmd:
                self.set_state(IsInIdle, expired=True)
            raise ProtocolTimeoutError(msg) from err

        try:
            pkt = fut.result()
            if pkt is None:
                raise ProtocolSendFailed(f"{self}: Send failed: FSM returned None")
            return pkt
        except ProtocolSendFailed:
            raise
        except (ProtocolError, TransportError) as err:
            raise ProtocolSendFailed(f"{self}: Send failed: {err}") from err

    def _check_buffer_for_cmd(self) -> None:
        """Check the queue buffer and send the next command if available."""
        if not self._qos_mgr.get_next():
            return

        try:
            assert self._qos_mgr.cmd is not None
            self._send_cmd(self._qos_mgr.cmd)
        finally:
            self._qos_mgr.task_done()

    def _send_cmd(self, cmd: Command, is_retry: bool = False) -> None:
        """Wrapper to send a command with retries, until success or exception.

        :param cmd: The command to transmit.
        :type cmd: Command
        :param is_retry: Flag indicating if this is a retry attempt.
        :type is_retry: bool
        """

        async def send_fnc_wrapper(cmd: Command) -> None:
            # Native Sync Collision Avoidance incorporated into FSM queue processing
            def is_imminent(p: Packet) -> bool:
                lower = td(seconds=0.010 * 0.8)
                upper = lower + td(seconds=0.084)
                return bool(
                    lower
                    < (p.dtm + td(seconds=int(p.payload[2:6], 16) / 10) - dt_now())
                    < upper
                )

            start = perf_counter()
            # Wait, self._protocol._tracked_sync_cycles is populated in base.py
            while any(
                is_imminent(p)
                for p in getattr(self._protocol, "_tracked_sync_cycles", [])
            ):
                await asyncio.sleep(0.010)
            if perf_counter() - start > 0.010:
                await asyncio.sleep(0.084)

            try:
                await self._send_fnc(cmd)
            # NOTE this exception has been left deliberatley broad to
            # allow any unexpected failures to correctly transition the
            # FSM back to the IsInIdle state and injects the exception
            # into the pending future to unblock the queue
            except Exception as err:
                self.set_state(IsInIdle, exception=err)

        try:
            self._state.cmd_sent(cmd, is_retry=is_retry)
        except ProtocolFsmError as err:
            self.set_state(IsInIdle, exception=err)
        else:
            task = self._loop.create_task(send_fnc_wrapper(cmd))
            self._send_tasks.add(task)
            task.add_done_callback(self._send_tasks.discard)


class ProtocolStateBase:
    """The base class for the protocol finite state machine states."""

    def __init__(self, context: ProtocolContext) -> None:
        """Initialize the state with the protocol context."""
        self._context = context
        self._sent_cmd: Command | None = None
        self._echo_pkt: Packet | None = None
        self._rply_pkt: Packet | None = None

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this state."""
        msg = f"<ProtocolState state={self.__class__.__name__}"
        if self._rply_pkt:
            return msg + f" rply={self._rply_pkt._hdr}>"
        if self._echo_pkt:
            return msg + f" echo={self._echo_pkt._hdr}>"
        if self._sent_cmd:
            return msg + f" cmd_={self._sent_cmd._hdr}>"
        return msg + ">"

    def connection_made(self) -> None:
        """Do nothing, as (except for InActive) we're already connected."""
        pass

    def connection_lost(self) -> None:
        """Transition to Inactive, regardless of current state."""
        if isinstance(self._context._state, Inactive):
            return

        if isinstance(self._context._state, IsInIdle):
            self._context.set_state(Inactive)
            return

        self._context.set_state(Inactive, exception=TransportError("Connection lost"))

    def pkt_rcvd(self, pkt: Packet) -> None:
        """Raise a NotImplementedError."""
        raise NotImplementedError("Invalid state to receive a packet")

    def writing_paused(self) -> None:
        """Do nothing."""
        pass

    def writing_resumed(self) -> None:
        """Do nothing."""
        pass

    def cmd_sent(self, cmd: Command, is_retry: bool | None = None) -> None:
        """Raise an error as default states cannot send commands."""
        raise ProtocolFsmError(f"Invalid state to send a command: {self._context}")


class Inactive(ProtocolStateBase):
    """The Protocol is not connected to the transport layer."""

    def connection_made(self) -> None:
        """Transition to IsInIdle."""
        self._context.set_state(IsInIdle)

    def pkt_rcvd(self, pkt: Packet) -> None:
        """Raise an exception, as a packet is not expected in this state."""
        if pkt.code != Code._PUZZ:
            _LOGGER.warning("%s: Invalid state to receive a packet", self._context)


class IsInIdle(ProtocolStateBase):
    """The Protocol is not in the process of sending a Command."""

    def pkt_rcvd(self, pkt: Packet) -> None:
        """Do nothing as we're not expecting an echo, nor a reply."""
        pass

    def cmd_sent(self, cmd: Command, is_retry: bool | None = None) -> None:
        """Transition to WantEcho."""
        self._sent_cmd = cmd

        if HGI_DEVICE_ID in cmd.tx_header:
            assert cmd._hdr_ is not None
            cmd._hdr_ = HeaderT(
                cmd._hdr_.replace(HGI_DEVICE_ID, self._context._protocol.hgi_id)
            )
        self._context.set_state(WantEcho)


class WantEcho(ProtocolStateBase):
    """The Protocol is waiting to receive an echo Packet."""

    def __init__(self, context: ProtocolContext) -> None:
        """Initialize the state from the previous context state."""
        super().__init__(context)
        self._sent_cmd = context._state._sent_cmd

    def pkt_rcvd(self, pkt: Packet) -> None:
        """If the pkt is the expected Echo, transition to IsInIdle, or WantRply."""
        if self._sent_cmd is None:
            _LOGGER.debug(
                "%s: received packet while _sent_cmd is None "
                "(unsolicited broadcast?), ignoring",
                self._context,
            )
            return

        try:
            pkt_hdr = pkt._hdr
        except PacketPayloadInvalid:
            return  # malformed packet, ignore

        if (
            self._sent_cmd.rx_header
            and pkt_hdr == self._sent_cmd.rx_header
            and (
                pkt.dst.id == self._sent_cmd.src.id
                or (
                    self._sent_cmd.src.id == HGI_DEVICE_ID
                    and pkt.dst.id == self._context._protocol.hgi_id
                )
            )
        ):
            level = (
                logging.DEBUG
                if self._context._cmd_tx_count < 3
                else logging.INFO
                if self._context._cmd_tx_count == 3
                else logging.WARNING
            )
            _LOGGER.log(
                level,
                "%s: Invalid state to receive a reply (expecting echo)",
                self._context,
            )
            self._rply_pkt = pkt
            self._context.set_state(IsInIdle, result=pkt)
            return

        if HGI_DEVICE_ID in pkt_hdr:
            assert pkt._hdr_ is not None
            pkt__hdr = HeaderT(
                pkt._hdr_.replace(HGI_DEVICE_ID, self._context._protocol.hgi_id)
            )
        else:
            pkt__hdr = pkt_hdr

        if pkt__hdr != self._sent_cmd.tx_header:
            return

        self._echo_pkt = pkt
        if self._sent_cmd.rx_header:
            self._context.set_state(WantRply)
        else:
            self._context.set_state(IsInIdle, result=pkt)

    def cmd_sent(self, cmd: Command, is_retry: bool | None = None) -> None:
        """Transition to WantEcho (i.e. a retransmit)."""
        pass


class WantRply(ProtocolStateBase):
    """The Protocol is waiting to receive an reply Packet."""

    def __init__(self, context: ProtocolContext) -> None:
        """Initialize the state with the echo that triggered it."""
        super().__init__(context)
        self._sent_cmd = context._state._sent_cmd
        self._echo_pkt = context._state._echo_pkt

    def pkt_rcvd(self, pkt: Packet) -> None:
        """If the pkt is the expected reply, transition to IsInIdle."""
        if self._sent_cmd is None or self._echo_pkt is None:
            _LOGGER.debug(
                "%s: received packet while _sent_cmd/_echo_pkt is None "
                "(unsolicited broadcast?), ignoring",
                self._context,
            )
            return

        try:
            pkt_hdr = pkt._hdr
        except PacketPayloadInvalid:
            return  # malformed packet, ignore

        if pkt_hdr == self._sent_cmd.tx_header and pkt.src == self._echo_pkt.src:
            level = (
                logging.DEBUG
                if self._context._cmd_tx_count < 3
                else logging.INFO
                if self._context._cmd_tx_count == 3
                else logging.WARNING
            )
            _LOGGER.log(
                level,
                "%s: Invalid state to receive an echo (expecting reply)",
                self._context,
            )
            return

        if (
            self._sent_cmd.rx_header[:8] == "0418|RP|"  # type: ignore[index]
            and self._sent_cmd.rx_header[:-2] == pkt_hdr[:-2]  # type: ignore[index]
            and pkt.payload == "000000B0000000000000000000007FFFFF7000000000"
        ):
            self._rply_pkt = pkt
        elif pkt_hdr != self._sent_cmd.rx_header:
            return
        else:
            self._rply_pkt = pkt

        self._context.set_state(IsInIdle, result=pkt)


_ProtocolStateT: TypeAlias = Inactive | IsInIdle | WantEcho | WantRply
_ProtocolStateClassT: TypeAlias = (
    type[Inactive] | type[IsInIdle] | type[WantEcho] | type[WantRply]
)

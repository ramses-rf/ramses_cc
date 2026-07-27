#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet protocol implementations.

This module provides the concrete Protocol classes (ReadProtocol and
PortProtocol) that bind the transport, state machine, and base filters
together.
"""

from __future__ import annotations

import logging
from typing import Final, TypeAlias

from ..command import Command
from ..const import (
    DEFAULT_DISABLE_QOS,
    DEFAULT_GAP_DURATION,
    DEFAULT_NUM_REPEATS,
    MAX_GAP_DURATION,
    MAX_NUM_REPEATS,
    SZ_ACTIVE_HGI,
    SZ_IS_EVOFW3,
    Code,
    Priority,
)
from ..exceptions import ProtocolError, ProtocolSendFailed, ProtocolTimeoutError
from ..interfaces import TransportInterface
from ..packet import Packet
from ..typing import MsgHandlerT, QosParams
from .base import DEFAULT_QOS, _DeviceIdFilterMixin
from .fsm import ProtocolContext

_DBG_DISABLE_IMPERSONATION_ALERTS: Final[bool] = False
_DBG_DISABLE_QOS: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


class ReadProtocol(_DeviceIdFilterMixin):
    """A protocol that can only receive Packets."""

    def __init__(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        enforce_include_list: bool = False,
        exclude_list: list[str] | None = None,
        include_list: list[str] | None = None,
        hgi_id: str | None = None,
    ) -> None:
        """Initialize the Read-Only protocol.

        :param msg_handler: The callback invoked when a valid message is processed.
        :type msg_handler: MsgHandlerT
        :param enforce_include_list: Flag to strictly enforce the include list.
        :type enforce_include_list: bool
        :param exclude_list: List of device IDs to block.
        :type exclude_list: list[str] | None
        :param include_list: List of device IDs to allow.
        :type include_list: list[str] | None
        :param hgi_id: The active HGI device ID.
        :type hgi_id: str | None
        """
        super().__init__(
            msg_handler,
            disable_warnings=True,
            enforce_include_list=enforce_include_list,
            exclude_list=exclude_list,
            include_list=include_list,
            hgi_id=hgi_id,
        )
        self._pause_writing = True

    def connection_made(  # type: ignore[override]
        self, transport: TransportInterface, /, *, ramses: bool = False
    ) -> None:
        """Consume the callback if invoked by SerialTransport rather than PortTransport.

        Our PortTransport wraps SerialTransport and will wait for the signature echo
        to be received (c.f. FileTransport) before calling connection_made(ramses=True).
        """
        super().connection_made(transport)

    def resume_writing(self) -> None:
        """Raise an exception as the Protocol cannot send Commands."""
        raise NotImplementedError(f"{self}: The chosen Protocol is Read-Only")

    async def send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams | None = None,
    ) -> Packet:
        """Raise an exception as the Protocol cannot send Commands."""
        raise NotImplementedError(f"{cmd._hdr}: < this Protocol is Read-Only")


class PortProtocol(_DeviceIdFilterMixin):
    """A protocol that can receive Packets and send Commands +/- QoS (using a FSM)."""

    def __init__(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        disable_qos: bool | None = DEFAULT_DISABLE_QOS,
        enforce_include_list: bool = False,
        exclude_list: list[str] | None = None,
        include_list: list[str] | None = None,
        hgi_id: str | None = None,
    ) -> None:
        """Add a FSM to the Protocol, to provide QoS.

        :param msg_handler: The callback invoked when a valid message is processed.
        :type msg_handler: MsgHandlerT
        :param disable_qos: Flag to globally disable QoS capabilities.
        :type disable_qos: bool | None
        :param enforce_include_list: Flag to strictly enforce the include list.
        :type enforce_include_list: bool
        :param exclude_list: List of device IDs to block.
        :type exclude_list: list[str] | None
        :param include_list: List of device IDs to allow.
        :type include_list: list[str] | None
        :param hgi_id: The active HGI device ID.
        :type hgi_id: str | None
        """
        super().__init__(
            msg_handler,
            disable_warnings=False,
            enforce_include_list=enforce_include_list,
            exclude_list=exclude_list,
            include_list=include_list,
            hgi_id=hgi_id,
        )
        self._context = ProtocolContext(self)
        self._disable_qos = disable_qos

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object."""
        if not self._context:
            return super().__repr__()
        cls = self._context.state.__class__.__name__
        return f"QosProtocol({cls}, len(queue)={self._context.qsize})"

    def connection_made(  # type: ignore[override]
        self, transport: TransportInterface, /, *, ramses: bool = False
    ) -> None:
        """Consume the callback if invoked by SerialTransport rather than PortTransport.

        Our PortTransport wraps SerialTransport and will wait for the signature echo
        to be received (c.f. FileTransport) before calling connection_made(ramses=True).
        """
        if not ramses:
            return None

        super().connection_made(transport)

        # ROBUSTNESS FIX: Ensure self._transport is set even if the wait future was cancelled
        if self._transport is None:
            _LOGGER.warning(
                f"{self}: Transport bound after wait cancelled (late connection)"
            )
            self._transport = transport

        # Safe access with check (optional but recommended)
        if self._transport:
            self._set_active_hgi(self._transport.get_extra_info(SZ_ACTIVE_HGI))
            self._is_evofw3 = self._transport.get_extra_info(SZ_IS_EVOFW3)

        if not self._context:
            return

        self._context.connection_made(transport)

        if self._pause_writing:
            self._context.pause_writing()
        else:
            self._context.resume_writing()

    def connection_lost(self, err: Exception | None) -> None:
        """Inform the FSM that the connection with the Transport has been lost."""
        super().connection_lost(err)
        if self._context:
            self._context.connection_lost(err)

    def pause_writing(self) -> None:
        """Inform the FSM that the Protocol has been paused."""
        super().pause_writing()
        if self._context:
            self._context.pause_writing()

    def resume_writing(self) -> None:
        """Inform the FSM that the Protocol has been resumed."""
        super().resume_writing()
        if self._context:
            self._context.resume_writing()

    def _pkt_received(self, pkt: Packet) -> None:
        """Pass any valid/wanted packets to the callback."""
        super()._pkt_received(pkt)
        if self._context:
            self._context.pkt_received(pkt)

    async def _send_impersonation_alert(self, cmd: Command) -> None:
        """Send a puzzle packet warning that impersonation is occurring."""
        if _DBG_DISABLE_IMPERSONATION_ALERTS:
            return

        msg = f"{self}: Impersonating device: {cmd.src}, for pkt: {cmd.tx_header}"
        if self._is_evofw3 is False:
            _LOGGER.error(f"{msg}, NB: non-evofw3 gateways can't impersonate!")
        else:
            _LOGGER.info(msg)

        await self._send_cmd(Command._puzzle(msg_type="11", message=cmd.tx_header))

    async def _send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams = DEFAULT_QOS,
    ) -> Packet:
        """Wrapper to send a Command with QoS (retries, until success or exception)."""

        async def send_cmd(kmd: Command) -> None:
            """Wrapper for self._send_frame(cmd)."""
            await self._send_frame(
                str(kmd), gap_duration=gap_duration, num_repeats=num_repeats
            )

        qos = qos or DEFAULT_QOS

        if _DBG_DISABLE_QOS:
            await send_cmd(cmd)
            return None  # type: ignore[return-value]

        _CODES = (Code._0006, Code._0404, Code._0418, Code._1FC9)

        if self._disable_qos is True or _DBG_DISABLE_QOS:
            qos._wait_for_reply = False
        elif self._disable_qos is None and cmd.code not in _CODES:
            qos._wait_for_reply = False

        assert self._context

        try:
            return await self._context.send_cmd(send_cmd, cmd, priority, qos)
        except ProtocolTimeoutError as err:
            _LOGGER.warning(f"{self}: Send timed out for {cmd._hdr}: {err}")
            raise
        except ProtocolError as err:
            _LOGGER.info(f"{self}: Failed to send {cmd._hdr}: {err}")
            raise

    async def send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams | None = None,
    ) -> Packet:
        """Send a Command with Qos (with retries, until success or ProtocolError).

        Returns the Command's response Packet or the Command echo if a response is not
        expected (e.g. sending an RP).

        If wait_for_reply is True, return the RQ's RP (or W's I), or raise an exception
        if one doesn't arrive. If it is False, return the echo of the Command only. If
        it is None (the default), act as True for RQs, and False for all other Commands.

        num_repeats is # of times to send the Command, in addition to the fist transmit,
        with gap_duration seconds between each transmission. If wait_for_reply is True,
        then num_repeats is ignored.

        Commands are queued and sent FIFO, except higher-priority Commands are always
        sent first.

        Will raise:
            ProtocolTimeoutError: global send timer expired before getting echo/reply.
            ProtocolSendFailed:   tried to Tx Command, but didn't get echo/reply.
            ProtocolError:        didn't attempt to Tx Command for some reason.
        """
        assert 0 <= gap_duration <= MAX_GAP_DURATION, "Out of range: gap_duration"
        assert 0 <= num_repeats <= MAX_NUM_REPEATS, "Out of range: num_repeats"

        if qos and not self._context:
            _LOGGER.warning(f"{cmd} < QoS is currently disabled by this Protocol")

        if qos and qos.wait_for_reply and num_repeats:
            _LOGGER.warning(f"{cmd} < num_repeats set to 0, as wait_for_reply is True")
            num_repeats = 0

        # Patch command with actual HGI ID if it uses the default placeholder.
        # PortProtocol.send_cmd overrides _BaseProtocol.send_cmd (which does this
        # patching at base.py:309), so we must replicate it here. Without this,
        # all commands go out with the 18:000730 placeholder instead of the real
        # HGI ID. See ramses_cc#757.
        cmd = self._patch_cmd_if_needed(cmd)

        # Manual filter check to avoid calling super().send_cmd(), which fails
        if not self._is_wanted_addrs(cmd.src.id, cmd.dst.id, sending=True):
            raise ProtocolError(f"Command excluded by device_id filter: {cmd}")

        pkt = await self._send_cmd(
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            qos=qos or DEFAULT_QOS,
        )

        if not pkt:
            raise ProtocolSendFailed(f"Failed to send command: {cmd} (REPORT THIS)")

        return pkt


RamsesProtocolT: TypeAlias = PortProtocol | ReadProtocol

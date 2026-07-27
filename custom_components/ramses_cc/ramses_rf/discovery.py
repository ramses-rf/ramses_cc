#!/usr/bin/env python3
"""RAMSES RF - Discovery Service Component.

This module provides the DiscoveryService component, which orchestrates
background polling and discovery commands for an entity, replacing the
legacy _Discovery inheritance model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, cast

from .protocol.opentherm import OPENTHERM_MESSAGES
from ..ramses_tx import Command, Packet
from ..ramses_tx.const import I_, RP, Code

from . import exceptions as exc
from .helpers import schedule_task
from .messages import Message
from .protocol.ramses import CODES_SCHEMA
from .routing import StateHeader

if TYPE_CHECKING:
    from .protocol.opentherm import OtDataId
    from ..ramses_tx.const import MsgId
    from ..ramses_tx.typing import HeaderT

    from .gateway import Gateway

_LOGGER = logging.getLogger(__name__)

_SZ_LAST_PKT: str = "last_msg"
_SZ_NEXT_DUE: str = "next_due"
_SZ_TIMEOUT: str = "timeout"
_SZ_FAILURES: str = "failures"
_SZ_INTERVAL: str = "interval"
_SZ_COMMAND: str = "command"

_DBG_ENABLE_DISCOVERY_BACKOFF: bool = False


class DiscoveryService:
    """Manages discovery orchestration and polling loops for an entity.

    This class is intended to be composed into Entity classes. It tracks
    which commands an entity supports and periodically polls them.
    """

    MAX_CYCLE_SECS: int = 30
    MIN_CYCLE_SECS: int = 3

    def __init__(self, entity: Any, gwy: Gateway) -> None:
        """Initialize the DiscoveryService.

        :param entity: The entity this service represents.
        :type entity: Any
        :param gwy: The gateway orchestrator providing I/O access.
        :type gwy: Gateway
        """
        self._entity = entity
        self._gwy = gwy

        self.cmds: dict[HeaderT, dict[str, Any]] = {}
        self._poller: asyncio.Task[None] | None = None
        self._start_handle: asyncio.Handle | None = None

        self._supported_cmds: dict[str, bool | None] = {}
        self._supported_cmds_ctx: dict[str, bool | None] = {}

        if not gwy.config.disable_discovery:
            try:
                self._start_handle = asyncio.get_running_loop().call_soon(
                    self.start_poller
                )
            except RuntimeError:
                # Fallback if instantiated outside of a running event loop context
                _LOGGER.debug(
                    "No running event loop; discovery poller must be started manually."
                )

    async def supported_cmds(self) -> dict[Code, Any]:
        """Return the current list of pollable command codes.

        :returns: A dictionary mapping supported Codes to their names.
        :rtype: dict[Code, Any]
        """
        if self._gwy.message_store:
            return {
                code: CODES_SCHEMA[code]["name"]
                for code in sorted(
                    await self._gwy.message_store.get_rp_codes(
                        (self._entity.id[:9], self._entity.id[:9])
                    )
                )
                if self.is_not_deprecated_cmd(code)
            }
        msgs = await self._entity.entity_state.get_all_messages()
        rp_codes = {msg.code for msg in msgs if msg.verb == RP}
        return {
            code: (CODES_SCHEMA[code]["name"] if code in CODES_SCHEMA else None)
            for code in sorted(rp_codes)
            if self.is_not_deprecated_cmd(code)
        }

    async def supported_cmds_ot(self) -> dict[str, Any]:
        """Return the current list of pollable OT msg_ids.

        :returns: A dictionary mapping OpenTherm msg_ids to their descriptions.
        :rtype: dict[str, Any]
        """

        def _to_data_id(msg_id: MsgId | str) -> OtDataId:
            return cast("OtDataId", int(msg_id, 16))

        res: list[str] = []
        if self._gwy.message_store:
            for msg in self._gwy.message_store.log_by_dtm:
                if (
                    msg.verb == RP
                    and msg.code == Code._3220
                    and (
                        msg.src.id == self._entity.id[:9]
                        or msg.dst.id == self._entity.id[:9]
                    )
                ):
                    ctx = msg.context.value
                    _LOGGER.debug("Fetched OT ctx from index: %s", ctx)
                    val = f"{ctx:02X}" if isinstance(ctx, int) else str(ctx)
                    if val not in res:
                        res.append(val)
        else:
            msgs = await self._entity.entity_state.get_all_messages()
            for msg in msgs:
                if msg.code == Code._3220 and msg.verb == RP:
                    ctx = msg.context.value
                    val = f"{ctx:02X}" if isinstance(ctx, int) else str(ctx)
                    if val not in res:
                        res.append(val)

        return {
            f"0x{msg_id}": OPENTHERM_MESSAGES[_to_data_id(msg_id)].get("en")
            for msg_id in sorted(res)
            if (
                self.is_not_deprecated_cmd(Code._3220, ctx=msg_id)
                and _to_data_id(msg_id) in OPENTHERM_MESSAGES
            )
        }

    def is_not_deprecated_cmd(self, code: Code, ctx: str | None = None) -> bool:
        """Return True if the code|ctx pair is not deprecated.

        :param code: The Command code to check.
        :type code: Code
        :param ctx: The context string, if applicable.
        :type ctx: str | None
        :returns: True if the command is still supported.
        :rtype: bool
        """
        if ctx is None:
            supported_cmds = self._supported_cmds
            idx = str(code)
        else:
            supported_cmds = self._supported_cmds_ctx
            idx = f"{code}|{ctx}"

        return supported_cmds.get(idx, None) is not False

    def add_cmd(
        self,
        cmd: Command,
        interval: float,
        *,
        delay: float = 0,
        timeout: float | None = None,
    ) -> None:
        """Schedule a command to run periodically.

        :param cmd: The command to poll.
        :type cmd: Command
        :param interval: The polling interval in seconds.
        :type interval: float
        :param delay: The initial delay before the first poll, defaults to 0.
        :type delay: float, optional
        :param timeout: The request timeout, defaults to None.
        :type timeout: float | None, optional
        :raises CommandInvalid: If the header is missing.
        """
        if cmd.rx_header is None:
            raise exc.CommandInvalid(
                f"cmd({cmd}): invalid (null) header not added to discovery"
            )

        if cmd.rx_header in self.cmds:
            _LOGGER.info(f"cmd({cmd}): duplicate header not added to discovery")
            return

        if delay:
            delay += random.uniform(0.05, 0.45)

        self.cmds[cmd.rx_header] = {
            _SZ_COMMAND: cmd,
            _SZ_INTERVAL: td(seconds=max(interval, self.MAX_CYCLE_SECS)),
            _SZ_LAST_PKT: None,
            _SZ_NEXT_DUE: dt.now() + td(seconds=delay),
            _SZ_TIMEOUT: timeout,
            _SZ_FAILURES: 0,
        }

    def start_poller(self) -> None:
        """Start the discovery poller (if it is not already running)."""
        self._start_handle = None  # call_soon callback has now fired

        if self._poller and not self._poller.done():
            return

        self._poller = schedule_task(self.poll_cmds)
        self._poller.set_name(f"{self._entity.id}_discovery_poller")
        self._gwy.add_task(self._poller)

    async def stop_poller(self) -> None:
        """Stop the discovery poller (only if it is running)."""
        # Cancel any pending call_soon(start_poller) that hasn't fired yet.
        if self._start_handle:
            self._start_handle.cancel()
            self._start_handle = None

        if not self._poller or self._poller.done():
            return

        self._poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._poller

    async def poll_cmds(self) -> None:
        """Send any outstanding commands that are past due."""
        while True:
            await self.discover()

            if self.cmds:
                next_due = min(t[_SZ_NEXT_DUE] for t in self.cmds.values())
                delay = max((next_due - dt.now()).total_seconds(), self.MIN_CYCLE_SECS)
            else:
                delay = self.MAX_CYCLE_SECS

            await asyncio.sleep(min(delay, self.MAX_CYCLE_SECS))

    async def discover(self) -> None:
        """Process the outstanding discovery commands."""

        async def find_latest_msg(hdr: HeaderT, task: dict[str, Any]) -> Message | None:
            """Return the latest message for a header from any source."""
            msgs: list[Message] = []

            code_str, _, src_id, *args = hdr.split("|")
            ctx_val: str | bool | None = args[0] if args else None
            if ctx_val == "True":
                ctx_val = True
            elif ctx_val == "False":
                ctx_val = False

            for v in (I_, RP):
                search_hdr = StateHeader.create(code_str, v, src_id, ctx_val)
                m = await self._entity.entity_state._get_msg_by_hdr(search_hdr)
                if m is not None:
                    msgs.append(m)

            try:
                cmd_code: Code = task[_SZ_COMMAND].code
                if cmd_code in (Code._000A, Code._30C9):
                    tcs = getattr(self._entity, "tcs", None)
                    if tcs:
                        tcs_id = getattr(tcs, "id", None)
                        if self._gwy.message_store and tcs_id:
                            found_msgs = []
                            for m in self._gwy.message_store.log_by_dtm:
                                if (
                                    m.code == cmd_code
                                    and m.verb in (I_, RP)
                                    and str(m.context.value) == "True"
                                    and (
                                        m.src.id == tcs_id[:9] or m.dst.id == tcs_id[:9]
                                    )
                                ):
                                    found_msgs.append(m)

                            if found_msgs:
                                msgs.append(max(found_msgs, key=lambda x: x.dtm))
                            else:
                                _LOGGER.debug(
                                    "No msg found for hdr %s, task code %s",
                                    hdr,
                                    cmd_code,
                                )
                        else:
                            tcs_msgs = await tcs.entity_state.get_all_messages()
                            found = [
                                m
                                for m in tcs_msgs
                                if m.code == cmd_code
                                and m.verb == I_
                                and m.context.value is True
                            ]
                            if found:
                                msgs.append(max(found, key=lambda x: x.dtm))
            except KeyError:
                pass

            return max(msgs) if msgs else None

        def backoff(hdr: HeaderT, failures: int) -> td:
            """Calculate the backoff interval based on failure count."""
            standard_interval: td = cast("td", self.cmds[hdr][_SZ_INTERVAL])

            if failures == 0:
                return standard_interval

            # 1. ORIGINAL DEBUG BEHAVIOR: Aggressive rapid-fire polling
            if _DBG_ENABLE_DISCOVERY_BACKOFF:
                if failures > 5:
                    secs = 60 * 60 * 6
                    _LOGGER.error(
                        f"No response for {hdr} ({failures}/5): throttling to 1/6h"
                    )
                elif failures > 2:
                    _LOGGER.warning(
                        f"No response for {hdr} ({failures}/5): retrying in {self.MAX_CYCLE_SECS}s"
                    )
                    secs = self.MAX_CYCLE_SECS
                else:
                    _LOGGER.info(
                        f"No response for {hdr} ({failures}/5): retrying in {self.MIN_CYCLE_SECS}s"
                    )
                    secs = self.MIN_CYCLE_SECS
                return td(seconds=secs)

            # 2. NEW PRODUCTION BEHAVIOR: Safe exponential backoff
            if failures == 1:
                secs = 60
                _LOGGER.info(f"No response for {hdr} (1/5): retrying in 1m")
            elif failures == 2:
                secs = 240
                _LOGGER.warning(f"No response for {hdr} (2/5): retrying in 4m")
            elif failures == 3:
                secs = 450
                _LOGGER.warning(f"No response for {hdr} (3/5): retrying in 7.5m")
            elif failures == 4:
                secs = 900
                _LOGGER.warning(f"No response for {hdr} (4/5): retrying in 15m")
            elif failures == 5:
                secs = 1800
                _LOGGER.warning(f"No response for {hdr} (5/5): retrying in 30m")
            else:
                secs = 3600
                _LOGGER.error(
                    f"No response for {hdr} ({failures}/5+): throttling to 1h"
                )

            return td(seconds=min(secs, standard_interval.total_seconds()))

        async def send_disc_cmd(
            hdr: HeaderT, task: dict[str, Any], timeout: float = 15
        ) -> Packet | None:
            """Send a scheduled command and wait for/return the response."""
            try:
                pkt: Packet | None = await asyncio.wait_for(
                    self._gwy.async_send_cmd(task[_SZ_COMMAND]),
                    timeout=timeout,
                )
            except exc.ProtocolError as err:
                _LOGGER.warning(
                    f"{self._entity}: Failed to send discovery cmd: {hdr}: {err}"
                )
            except TimeoutError as err:
                _LOGGER.warning(
                    f"{self._entity}: Failed to send discovery cmd: {hdr} within {timeout} secs: {err}"
                )
            else:
                return pkt
            return None

        for hdr, task in self.cmds.items():
            dt_now = dt.now()

            msg = await find_latest_msg(hdr, task)
            if msg and (task[_SZ_NEXT_DUE] < msg.dtm + task[_SZ_INTERVAL]):
                task[_SZ_FAILURES] = 0
                task[_SZ_LAST_PKT] = msg._pkt
                task[_SZ_NEXT_DUE] = msg.dtm + task[_SZ_INTERVAL]

            if task[_SZ_NEXT_DUE] > dt_now:
                continue

            task[_SZ_NEXT_DUE] = dt_now + task[_SZ_INTERVAL]

            cmd_code = task[_SZ_COMMAND].code
            if not self.is_not_deprecated_cmd(cmd_code):
                continue
            if not self.is_not_deprecated_cmd(
                cmd_code, ctx=task[_SZ_COMMAND].payload[4:6]
            ):
                continue

            task[_SZ_NEXT_DUE] = dt_now + backoff(hdr, task[_SZ_FAILURES])

            if pkt := await send_disc_cmd(hdr, task):
                task[_SZ_FAILURES] = 0
                task[_SZ_LAST_PKT] = pkt
                task[_SZ_NEXT_DUE] = pkt.dtm + task[_SZ_INTERVAL]
            else:
                task[_SZ_FAILURES] += 1
                task[_SZ_LAST_PKT] = None
                task[_SZ_NEXT_DUE] = dt_now + backoff(hdr, task[_SZ_FAILURES])

    def deprecate_code_ctx(
        self, pkt: Packet, ctx: str | None = None, reset: bool = False
    ) -> None:
        """If a code|ctx is deprecated twice, stop polling for it.

        :param pkt: The packet triggering the deprecation.
        :type pkt: Packet
        :param ctx: The context string, defaults to None.
        :type ctx: str | None, optional
        :param reset: True to reinstate polling, defaults to False.
        :type reset: bool, optional
        """

        def deprecate(supported_dict: dict[str, bool | None], idx: str) -> None:
            if idx not in supported_dict:
                supported_dict[idx] = None
            elif supported_dict[idx] is None:
                _LOGGER.info(
                    f"{pkt} < Polling now deprecated for code|ctx={idx}: "
                    "it appears to be unsupported"
                )
                supported_dict[idx] = False

        def reinstate(supported_dict: dict[str, bool | None], idx: str) -> None:
            if self.is_not_deprecated_cmd(Code(idx.split("|")[0]), None) is False:
                _LOGGER.info(
                    f"{pkt} < Polling now reinstated for code|ctx={idx}: "
                    "it now appears supported"
                )
            if idx in supported_dict:
                supported_dict.pop(idx)

        if ctx is None:
            supported_cmds = self._supported_cmds
            idx: str = str(pkt.code)
        else:
            supported_cmds = self._supported_cmds_ctx
            idx = f"{pkt.code}|{ctx}"

        (reinstate if reset else deprecate)(supported_cmds, idx)

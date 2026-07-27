#!/usr/bin/env python3
"""RAMSES RF - Gateway Lifecycle Management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime as dt, timedelta as td
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, Any, cast

from ..ramses_tx import Packet, protocol_factory, set_pkt_logging_config
from ..ramses_tx.const import SZ_ACTIVE_HGI
from ..ramses_tx.logger import flush_packet_log

from .const import DONT_CREATE_MESSAGES, HIGH_VOLUME_STATUS_CODES, I_, RP, RQ, W_, Code
from .exceptions import DeviceNotFoundError
from .messages import Message
from .pipeline.ingestion import StateProjector
from .schemas import load_schema
from .state import MessageStore
from .typing import DeviceIdT

if TYPE_CHECKING:
    from ..ramses_tx import Engine
    from ..ramses_tx.dtos import PacketDTO

    from .config import GatewayConfig
    from .devices import Device
    from .gateway import Gateway
    from .interfaces import DeviceRegistryInterface, MessageStoreInterface
    from .systems import Evohome

_LOGGER = logging.getLogger(__name__)


class GatewayLifecycle:
    """Gateway lifecycle management and orchestration."""

    if TYPE_CHECKING:

        @property
        def config(self) -> GatewayConfig: ...
        @property
        def device_registry(self) -> DeviceRegistryInterface: ...

        _engine: Engine
        _message_store: MessageStoreInterface | None
        _pkt_log_listener: QueueListener | None
        _schema: dict[str, Any]
        state_projector: StateProjector | None

        def add_task(self, task: asyncio.Task[Any]) -> None: ...
        def clear_message_history(self) -> None: ...
        async def schema(self) -> dict[str, Any]: ...
        async def _msg_handler(self, dto: PacketDTO) -> None: ...

    def create_sqlite_message_index(self) -> None:
        """Initialize the SQLite MessageStore."""
        self._message_store = MessageStore(disk_path=self.config.database_path)

    async def start(
        self,
        /,
        *,
        start_discovery: bool = True,
        cached_packets: dict[str, dict[str, Any] | str] | None = None,
    ) -> None:
        """Start the Gateway and Initiate discovery as required."""

        def initiate_discovery(dev_list: list[Device], sys_list: list[Evohome]) -> None:
            _LOGGER.debug("Engine: Initiating/enabling discovery...")
            for device in dev_list:
                device.discovery.start_poller()
            for system in sys_list:
                system.discovery.start_poller()
                for zone in system.zones:
                    zone.discovery.start_poller()
                if system.dhw:
                    system.dhw.discovery.start_poller()

        _, self._pkt_log_listener = await set_pkt_logging_config(
            cc_console=(self.config.reduce_processing >= DONT_CREATE_MESSAGES),
            **self._engine._packet_log,
        )  # type: ignore[arg-type]

        if self._pkt_log_listener:
            self._pkt_log_listener.start()

            pkt_log_config = cast("dict[str, Any]", self._engine._packet_log)
            if flush_interval := pkt_log_config.get("flush_interval", 0):

                async def _periodic_flush() -> None:
                    try:
                        while True:
                            await asyncio.sleep(flush_interval)
                            await self._engine._loop.run_in_executor(
                                None, flush_packet_log, self._pkt_log_listener
                            )
                    except asyncio.CancelledError:
                        pass

                self.add_task(self._engine._loop.create_task(_periodic_flush()))

        _LOGGER.info("Ramses RF starts central MessageStore")
        self.create_sqlite_message_index()

        # Initialize the CQRS State Projector with a dummy queue.
        # We do not call .start() because the Phase 2.75 async cutover is paused.
        # It will be fed synchronously via the _msg_handler bridge.
        if self.state_projector is None:
            self.state_projector = StateProjector(self, asyncio.Queue())

        self.config.disable_discovery, disable_discovery = (
            True,
            self.config.disable_discovery,
        )

        load_schema(
            cast("Gateway", self),
            known_list=self.config.known_list,  # type: ignore[arg-type]
            **self._schema,
        )

        # Bootstrap the local hardware adapter (HGI) into the registry early if configured.
        # Because the adapter has no topological bonds to a heating controller,
        # the TopologyBuilder pipeline will never emit a BIND_DEVICE event for it.
        if self.config.hgi_id:
            with contextlib.suppress(DeviceNotFoundError):
                self.device_registry.get_device(cast(DeviceIdT, self.config.hgi_id))

        if cached_packets:
            await self._restore_cached_packets(cached_packets)

        await self._engine.start()

        # If the underlying transport dynamically discovered the hardware adapter
        # (e.g. from parsing the first frames of a log file stream), sync the
        # configuration state and explicitly register it into the graph.
        if self._engine._transport:
            if hgi_id := self._engine._transport.get_extra_info(SZ_ACTIVE_HGI):
                hgi_str = str(hgi_id)
                if not self.config.hgi_id:
                    self.config.hgi_id = hgi_str
                    if hasattr(self.config.engine, "hgi_id"):
                        self.config.engine.hgi_id = hgi_str
                with contextlib.suppress(DeviceNotFoundError):
                    self.device_registry.get_device(cast(DeviceIdT, hgi_str))

        self.config.disable_discovery = disable_discovery

        if (
            not self._engine._disable_sending
            and not self.config.disable_discovery
            and start_discovery
        ):
            initiate_discovery(
                self.device_registry.devices, self.device_registry.systems
            )

    async def stop(self) -> None:
        """Stop the Gateway and tidy up."""
        self.config.disable_discovery = True

        # Cancel binding managers and discovery pollers before stopping engine
        for dev in self.device_registry.devices:
            bm = getattr(dev, "_binding_manager", None)
            if bm:
                with contextlib.suppress(Exception):
                    bm.cancel()
            if hasattr(dev, "stop_poller"):
                with contextlib.suppress(Exception):
                    await dev.stop_poller()
            disc = getattr(dev, "discovery", None)
            if disc:
                with contextlib.suppress(Exception):
                    await disc.stop_poller()

        await self._engine.stop()

        # The StateProjector background worker isn't running, but we call stop
        # just to be architecturally safe and clean up any dangling references.
        if self.state_projector is not None:
            await self.state_projector.stop()

        if self._pkt_log_listener:

            def _stop_listener(listener: QueueListener) -> None:
                listener.stop()
                for handler in listener.handlers:
                    handler.close()

            await self._engine._loop.run_in_executor(
                None, _stop_listener, self._pkt_log_listener
            )
            self._pkt_log_listener = None

        if self._message_store:
            self._message_store.stop()

    async def _pause(self, *args: Any) -> None:
        """Pause the (unpaused) gateway (disables sending/discovery)."""
        _LOGGER.debug("Gateway: Pausing engine...")

        self.config.disable_discovery, disc_flag = (
            True,
            self.config.disable_discovery,
        )

        try:
            await self._engine._pause(disc_flag, *args)
        except RuntimeError:
            self.config.disable_discovery = disc_flag
            raise

    async def _resume(self) -> tuple[Any, ...]:
        """Resume the (paused) gateway."""
        args: tuple[Any, ...]
        _LOGGER.debug("Gateway: Resuming engine...")

        self.config.disable_discovery, *args = (
            await self._engine._resume()  # type: ignore[assignment]
        )

        return tuple(args)

    async def get_state(
        self, include_expired: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the current schema & state (may include expired packets)."""
        await self._pause()

        def wanted_msg(msg: Message, include_expired: bool = False) -> bool:
            if msg.code == Code._313F:
                return msg.verb in (I_, RP)
            if getattr(msg, "_expired", False) and not include_expired:
                return False
            if msg.code == Code._0404:
                return msg.verb in (I_, W_) and msg.len > 7
            if msg.verb in (W_, RQ):
                return False
            return include_expired or not getattr(msg, "_expired", False)

        pkts: dict[str, Any] = {}
        if self._message_store:
            all_msgs = await self._message_store.all(include_expired=True)
            for i, msg in enumerate(all_msgs):
                if wanted_msg(msg, include_expired=include_expired):
                    dtm_str = msg.dtm.isoformat(timespec="microseconds")
                    pkts[dtm_str] = msg.dto.source_packets[0].__dict__
                if i > 0 and i % 100 == 0:
                    await asyncio.sleep(0)

        await self._resume()
        return await self.schema(), dict(sorted(pkts.items()))

    async def _restore_cached_packets(
        self, packets: dict[str, dict[str, Any] | str], _clear_state: bool = False
    ) -> None:
        """Restore cached packets (may include expired packets)."""

        def clear_state() -> None:
            _LOGGER.info("Gateway: Clearing existing schema/state...")
            cast("Gateway", self)._tcs = None
            self.device_registry.devices.clear()
            self.device_registry.device_by_id.clear()
            self.clear_message_history()

        _LOGGER.debug("Gateway: Restoring a cached packet log...")
        await self._pause()

        if _clear_state:
            clear_state()

        enforce_include_list = bool(
            self._engine._enforce_known_list and self.config.hgi_id
        )

        tmp_protocol = protocol_factory(
            self._msg_handler,
            disable_sending=True,
            enforce_include_list=enforce_include_list,
            exclude_list=self._engine._exclude,
            include_list=self._engine._include,
        )

        cutoff_dtm = dt.now(tz=UTC) - td(hours=1)

        for i, (dtm, state) in enumerate(packets.items()):
            if i > 0 and i % 100 == 0:
                await asyncio.sleep(0)

            try:
                clean_dtm = dtm.replace("Z", "+00:00")
                pkt_dtm = dt.fromisoformat(clean_dtm)
                if pkt_dtm.tzinfo is None:
                    pkt_dtm = pkt_dtm.replace(tzinfo=UTC)
                is_old = pkt_dtm < cutoff_dtm
            except (TypeError, ValueError):
                is_old = False

            if is_old:
                is_match = False
                if isinstance(state, dict) and "code" in state:
                    is_match = state["code"] in HIGH_VOLUME_STATUS_CODES
                else:
                    frame_str = (
                        state.get("frame", "")
                        if isinstance(state, dict)
                        else str(state)
                    )
                    is_match = any(
                        f" {c} " in frame_str for c in HIGH_VOLUME_STATUS_CODES
                    )

                if is_match:
                    continue

            try:
                pkt = Packet.from_dict(dtm, state)
                tmp_protocol.pkt_received(pkt)
            except Exception as err:
                _LOGGER.debug("Gateway: Failed to restore packet %s: %s", dtm, err)

        _LOGGER.debug("Gateway: Restored, resuming")
        await self._resume()

#!/usr/bin/env python3
"""RAMSES RF - the gateway facade."""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from collections.abc import Awaitable, Callable
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, Any, cast

from ..ramses_tx import I_, RP, Command, Engine, Packet
from ..ramses_tx.const import (
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    DEFAULT_WAIT_FOR_REPLY,
    SZ_ACTIVE_HGI,
    Priority,
)
from ..ramses_tx.dtos import PacketDTO
from ..ramses_tx.exceptions import PacketInvalid, ProtocolSendFailed
from ..ramses_tx.schemas import SZ_BLOCK_LIST, SZ_ENFORCE_KNOWN_LIST, SZ_KNOWN_LIST
from ..ramses_tx.typing import PayloadT

from .config import GatewayConfig as GatewayConfig
from .const import Code, VerbT
from .devices import DeviceFilter, DeviceRegistry, HgiGateway, device_factory
from .dispatcher import detect_array_fragment, process_msg
from .interfaces import (
    DeviceFilterInterface,
    DeviceRegistryInterface,
    GatewayInterface,
    MessageStoreInterface,
)
from .lifecycle import GatewayLifecycle
from .messages import ApplicationMessage, Message as rf_msg
from .pipeline.topology_builder import TopologyBuilder
from .schemas import (
    SCH_GLOBAL_SCHEMAS,
    SZ_CONFIG,
    SZ_ENABLE_EAVESDROP,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
)
from .systems.tcs import Evohome
from .typing import DeviceIdT

if TYPE_CHECKING:
    from ..ramses_tx import RamsesTransportT

_LOGGER = logging.getLogger(__name__)


class Gateway(GatewayLifecycle, GatewayInterface):
    """The gateway class.

    This class serves as the primary interface for the RAMSES RF network.
    It manages the serial connection (via ``Engine``), device discovery,
    schema maintenance, and message dispatching.
    """

    def __init__(
        self,
        port_name: str | None = None,
        *,
        config: GatewayConfig | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Gateway instance."""
        self._gwy_config = config or GatewayConfig()

        if port_name is not None:
            self._gwy_config.engine.port_name = port_name

        if kwargs:
            keys = list(kwargs.keys())
            _LOGGER.warning(
                "Gateway received legacy kwargs: %s. Please migrate "
                "ramses_cc to use the GatewayConfig object.",
                keys,
            )
            warnings.warn(
                f"Initializing Gateway with **kwargs {keys} is deprecated "
                "and will be removed in a future release. Please use "
                "GatewayConfig.",
                DeprecationWarning,
                stacklevel=2,
            )

            def _apply_kwargs(cfg_dict: dict[str, Any]) -> None:
                """Recursively unpack nested dictionaries to apply configs."""
                for key, value in cfg_dict.items():
                    if hasattr(self._gwy_config.engine, key):
                        setattr(self._gwy_config.engine, key, value)
                    elif hasattr(self._gwy_config, key):
                        setattr(self._gwy_config, key, value)
                    elif isinstance(value, dict):
                        _apply_kwargs(value)
                    else:
                        _LOGGER.error(
                            "Gateway received unsupported kwarg: %s. "
                            "This argument is ignored.",
                            key,
                        )

            _apply_kwargs(kwargs)

        if self._gwy_config.debug_mode:
            _LOGGER.setLevel(logging.DEBUG)

        # Override EngineConfig with the stripped-down L7 properties
        self._gwy_config.engine.hgi_id = self._gwy_config.hgi_id
        self._gwy_config.engine.known_list = list(self._gwy_config.known_list.keys())
        self._gwy_config.engine.block_list = list(self._gwy_config.block_list.keys())

        self._engine = Engine(
            self._gwy_config.engine,
            loop=loop,
            transport_constructor=transport_constructor,
        )

        # Force the engine's protocol to use Gateway's message handler
        self._engine._set_msg_handler(self._msg_handler)

        if self._engine._disable_sending:
            self._gwy_config.disable_discovery = True

        if self._gwy_config.enable_eavesdrop:
            _LOGGER.warning(
                f"{SZ_ENABLE_EAVESDROP}=True: this is strongly discouraged "
                "for routine use (there be dragons here)"
            )

        self._schema: dict[str, Any] = SCH_GLOBAL_SCHEMAS(self._gwy_config.schema or {})

        self._tcs: Evohome | None = None

        self._device_filter: DeviceFilterInterface = DeviceFilter(
            include=cast(list[DeviceIdT], list(self._gwy_config.known_list.keys())),
            exclude=cast(list[DeviceIdT], list(self._gwy_config.block_list.keys())),
            unwanted=self._engine._unwanted,
            enforce_known_list=self._gwy_config.engine.enforce_known_list,
            hgi_id_provider=lambda: getattr(self.hgi, "id", None),
        )

        self._device_registry: DeviceRegistryInterface = DeviceRegistry(
            device_filter=self._device_filter,
            config=self._gwy_config,
            device_factory_cb=lambda addr, msg, traits: device_factory(
                gwy=self, dev_addr=addr, msg=msg, traits=traits
            ),
        )

        # Instantiate the new asynchronous Topology Builder engine
        self._topology_builder = TopologyBuilder(
            emit_event_cb=self._device_registry.handle_topology_event,
            enable_eavesdrop=self._gwy_config.enable_eavesdrop,
        )

        self._message_store: MessageStoreInterface | None = None
        self._pkt_log_listener: QueueListener | None = None

        # Initialize placeholder for the CQRS StateProjector
        self.state_projector = None

        self._prev_msg: ApplicationMessage | None = None
        self._this_msg: ApplicationMessage | None = None
        self._history_lock = threading.Lock()

        # 1. Controller Knowledge Bridge
        def is_controller(device_id: str) -> bool:
            if device_id.startswith("02:"):
                return True
            dev = self._device_registry.device_by_id.get(cast(DeviceIdT, device_id))
            if dev:
                return getattr(dev, "_is_controller", True)
            return True

        rf_msg._IS_CONTROLLER_CB = is_controller

    def __repr__(self) -> str:
        if not self._engine.ser_name:
            return f"Gateway(input_file={self._engine._input_file})"
        return (
            f"Gateway(port_name={self._engine.ser_name}, "
            f"port_config={self._engine._port_config})"
        )

    @property
    def device_registry(self) -> DeviceRegistryInterface:
        return self._device_registry

    @property
    def config(self) -> GatewayConfig:
        return self._gwy_config

    @property
    def message_store(self) -> MessageStoreInterface | None:
        return self._message_store

    @message_store.setter
    def message_store(self, value: MessageStoreInterface | None) -> None:
        self._message_store = value

    @property
    def hgi(self) -> HgiGateway | None:
        if not self._engine._transport:
            return None
        if device_id := self._engine._transport.get_extra_info(SZ_ACTIVE_HGI):
            return self.device_registry.device_by_id.get(device_id)
        return None

    def update_message_history(self, msg: ApplicationMessage) -> None:
        with self._history_lock:
            self._prev_msg = self._this_msg
            self._this_msg = msg

    def clear_message_history(self) -> None:
        with self._history_lock:
            self._prev_msg = None
            self._this_msg = None

    @property
    def tcs(self) -> Evohome | None:
        if self._tcs is None and self.device_registry.systems:
            self._tcs = self.device_registry.systems[0]
        return self._tcs

    async def _config(self) -> dict[str, Any]:
        return {
            "_gateway_id": self.hgi.id if self.hgi else None,
            SZ_MAIN_TCS: self.tcs.id if self.tcs else None,
            SZ_CONFIG: {SZ_ENFORCE_KNOWN_LIST: self.config.engine.enforce_known_list},
            SZ_KNOWN_LIST: await self.device_registry.known_list(),
            SZ_BLOCK_LIST: self.config.engine.block_list or [],
            "_unwanted": sorted(self._engine._unwanted),
        }

    async def schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {SZ_MAIN_TCS: self.tcs.ctl.id if self.tcs else None}
        for tcs in self.device_registry.systems:
            schema[tcs.ctl.id] = await tcs.schema()
        schema[f"{SZ_ORPHANS}_heat"] = await self.device_registry.get_heat_orphans()
        schema[f"{SZ_ORPHANS}_hvac"] = await self.device_registry.get_hvac_orphans()
        return schema

    async def params(self) -> dict[str, Any]:
        return await self.device_registry.params()

    async def status(self) -> dict[str, Any]:
        status_dict = await self.device_registry.status()
        tx_rate = (
            self._engine._transport.get_extra_info("tx_rate")
            if self._engine._transport
            else None
        )
        status_dict["_tx_rate"] = tx_rate
        return status_dict

    async def get_state(
        self, include_expired: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the current system state for warm restarts (e.g., for HA ramses_cc).

        This returns a tuple of (schema, packets). The packets payload is sourced
        directly from the OSI L7 MessageStore, containing the single most recent
        packet for every unique StateHeader. It strictly maps ``dtm_str`` to a
        dictionary containing ``code``, ``verb``, and ``payload`` to maintain
        parity with legacy serializers.

        :param include_expired: Included for backward compatibility, unused.
        :type include_expired: bool
        :return: A tuple containing the schema dictionary and the state dictionary.
        :rtype: tuple[dict[str, Any], dict[str, Any]]
        """
        state_dict: dict[str, Any] = {}
        if self.message_store is not None:
            # Use getattr fallback in case interface strictly lacks state_cache mapping
            state_cache = getattr(self.message_store, "state_cache", {})

            for msg in state_cache.values():
                if msg.verb not in (I_, RP):
                    continue

                dtm_str = msg.dtm.isoformat(timespec="microseconds")
                state_dict[dtm_str] = {
                    "verb": msg.verb,
                    "src": msg.src.id,  # Keep for downstream legacy parsers
                    "dst": msg.dst.id,  # Keep for downstream legacy parsers
                    "addr1": msg._addrs[0].id,  # <-- Exact raw addr1
                    "addr2": msg._addrs[1].id,  # <-- Exact raw addr2
                    "addr3": msg._addrs[2].id,  # <-- Exact raw addr3
                    "code": str(msg.code),
                    "payload": msg.payload,
                }

        schema_dict = await self.schema()
        return schema_dict, state_dict

    async def _msg_handler(self, dto: PacketDTO) -> None:
        try:
            app_msg = ApplicationMessage.from_dto(dto)
        except PacketInvalid:
            return

        app_msg.set_gateway(self._engine)
        app_msg.bind_context(self)  # noqa: B010
        self.update_message_history(app_msg)

        assert self._this_msg

        if self._prev_msg and detect_array_fragment(
            self._this_msg,
            self._prev_msg,
        ):
            app_msg._force_has_array()
            app_msg._payload = self._prev_msg.payload + (
                app_msg.payload
                if isinstance(app_msg.payload, list)
                else [app_msg.payload]
            )

        # NEW: Feed the async TopologyBuilder so it can structurally map the
        # graph *before* the message state is ingested by the Read-Models.
        raw_payload = getattr(app_msg, "payload", None)

        if isinstance(raw_payload, (dict, list)):
            # Bridge the payload to satisfy core.Message strict dict typing
            core_data = (
                raw_payload
                if isinstance(raw_payload, dict)
                else {"_array": raw_payload}
            )

            # Temporary Phase 2.8 Strangler Fig Translation
            from .enums import Topic
            from .messages.core import Message as CoreMessage

            core_msg = CoreMessage(
                topic=Topic.TOPOLOGY_DISCOVERY,
                header=app_msg.state_header,
                src=app_msg.src,
                dst=app_msg.dst,
                data=core_data,
                packets=(),  # L3 packets dropped for legacy bridging
                timestamp=app_msg.dtm,
            )
            await self._topology_builder.consume(core_msg)

        await process_msg(self, app_msg)

        # Phase 2.95 CQRS Strangler Bridge: Because the Phase 2.99 Async Queue Cutover
        # is currently paused, we must feed the CQRS StateProjector synchronously
        # here so the PR 2 Read-Models get properly hydrated in production.
        if self.state_projector is not None:
            self.state_projector.process_message_state(app_msg)

    def add_msg_handler(
        self,
        msg_handler: Callable[[PacketDTO], Awaitable[None]],
        /,
        *,
        msg_filter: Callable[[PacketDTO], bool] | None = None,
    ) -> Callable[[], None]:
        return self._engine.add_msg_handler(msg_handler, msg_filter=msg_filter)

    def add_raw_pkt_handler(
        self,
        msg_handler: Callable[[PacketDTO], Awaitable[None]],
        /,
    ) -> Callable[[], None]:
        """Add a raw packet handler that fires before the device ID filter.

        Used by the passive scan engine to see packets from unknown devices
        even when ``enforce_known_list=True``.
        """
        return self._engine.add_raw_pkt_handler(msg_handler)

    def add_task(self, task: asyncio.Task[Any]) -> None:
        self._engine.add_task(task)

    @staticmethod
    def create_cmd(
        verb: VerbT,
        device_id: DeviceIdT,
        code: Code,
        payload: PayloadT,
        **kwargs: Any,
    ) -> Command:
        return Engine.create_cmd(
            verb,
            device_id,
            code,
            payload,
            **kwargs,
        )

    def send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> asyncio.Task[Packet]:
        coro = self.async_send_cmd(
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            timeout=timeout,
            wait_for_reply=wait_for_reply,
            max_retries=max_retries,
        )
        task = self._engine._loop.create_task(coro)

        def _clear_exc(fut: asyncio.Task[Any]) -> None:
            if not fut.cancelled() and fut.exception():
                _LOGGER.debug("Background task failed: %s", fut.exception())

        task.add_done_callback(_clear_exc)
        self.add_task(task)
        return task

    async def async_send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> Packet:
        try:
            return await self._engine.async_send_cmd(
                cmd,
                gap_duration=gap_duration,
                num_repeats=num_repeats,
                priority=priority,
                max_retries=max_retries,
                timeout=timeout,
                wait_for_reply=wait_for_reply,
            )
        except (ProtocolSendFailed, NotImplementedError) as err:
            if (
                self.config.disable_discovery
                or self._engine._disable_sending
                or "Inactive" in str(err)
                or "Read-Only" in str(err)
            ):
                raise asyncio.CancelledError(
                    f"Gateway shutting down, suppressed teardown leak: {err}"
                ) from err
            raise

"""RAMSES RF - Abstract Base Classes and Interfaces."""

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from ..ramses_tx import Command, Packet, Priority, QosParams

from .messages import Message
from .typing import DeviceIdT, DeviceListT

if TYPE_CHECKING:
    from .models import TopologyChangedEvent
    from .topology import Parent


class CommandDispatcher(Protocol):
    """Protocol for a service/callback that dispatches commands."""

    async def __call__(
        self,
        cmd: Command,
        *,
        priority: Priority | None = None,
        qos: QosParams | None = None,
    ) -> Packet | None:
        """Dispatch a command asynchronously."""
        ...


class MessageStoreInterface(Protocol):
    def add(self, msg: Any) -> Any: ...
    def add_record(
        self, src: str, code: str = "", verb: str = "", payload: str = "00"
    ) -> None: ...
    def start_consumer(self, in_queue: asyncio.Queue[Any]) -> None:
        """Start the asynchronous queue consumer task for SSOT ingestion."""
        ...

    async def get(
        self,
        msg: Any | None = None,
        *,
        dtm: Any | None = None,
        src: str | None = None,
        dst: str | None = None,
        verb: str | None = None,
        code: str | None = None,
        ctx: Any | None = None,
        hdr: str | None = None,
    ) -> tuple[Any, ...]: ...
    async def rem(
        self,
        msg: Any | None = None,
        *,
        dtm: Any | None = None,
        src: str | None = None,
        dst: str | None = None,
        verb: str | None = None,
        code: str | None = None,
        ctx: Any | None = None,
        hdr: str | None = None,
    ) -> tuple[Any, ...] | None: ...
    async def contains(
        self,
        *,
        dtm: Any | None = None,
        src: str | None = None,
        dst: str | None = None,
        verb: str | None = None,
        code: str | None = None,
        ctx: Any | None = None,
        hdr: str | None = None,
    ) -> bool: ...
    async def get_rp_codes(self, parameters: tuple[str, ...]) -> list[Any]: ...
    async def all(self, include_expired: bool = False) -> tuple[Any, ...]: ...
    async def clr(self) -> None: ...
    async def qry(self, sql: str, parameters: tuple[str, ...]) -> tuple[Any, ...]: ...
    async def qry_field(
        self, sql: str, parameters: tuple[str, ...]
    ) -> list[tuple[Any, ...]]: ...

    @property
    def log_by_dtm(self) -> Any: ...
    @property
    def state_cache(self) -> Any: ...
    def flush(self) -> None: ...
    def stop(self) -> None: ...


class DeviceInterface(Protocol):
    """Interface for a standard RF Device."""

    @property
    def id(self) -> DeviceIdT:
        """Return the device ID.

        :return: The Device ID.
        """
        ...

    async def traits(self) -> dict[str, Any]:
        """Return the device traits.

        :return: A dictionary of device traits.
        """
        ...

    def _handle_msg(self, msg: Message) -> None:
        """Process an incoming message.

        :param msg: The message to process.
        """
        ...


class DeviceFilterInterface(Protocol):
    """Interface for the Device Filter service."""

    def check_filter_lists(self, dev_id: DeviceIdT) -> None:
        """Raise a DeviceNotFoundError if a device_id is filtered out.

        :param dev_id: The device identifier to evaluate.
        """
        ...


class DeviceRegistryInterface(Protocol):
    """Interface for the Device Registry service."""

    @property
    def devices(self) -> list[Any]:
        """Return the list of devices."""
        ...

    @property
    def device_by_id(self) -> dict[DeviceIdT, Any]:
        """Return the mapping of device IDs to devices."""
        ...

    @property
    def system_by_id(self) -> dict[DeviceIdT, Any]:
        """Return a mapping of device IDs to their associated systems."""
        ...

    @property
    def systems(self) -> list[Any]:
        """Return a list of all identified systems."""
        ...

    def _add_device(self, dev: Any) -> None:
        """Add a device to the registry."""
        ...

    def get_device(
        self,
        device_id: DeviceIdT,
        *,
        msg: Message | None = None,
        parent: "Parent | None" = None,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> Any:
        """Return a device, creating it if it does not already exist."""
        ...

    async def fake_device(
        self,
        device_id: DeviceIdT,
        create_device: bool = False,
    ) -> Any:
        """Create a faked device."""
        ...

    async def known_list(self) -> DeviceListT:
        """Return the working known_list."""
        ...

    async def get_heat_orphans(self) -> list[DeviceIdT]:
        """Return a list of IDs for orphaned heat devices."""
        ...

    async def get_hvac_orphans(self) -> list[DeviceIdT]:
        """Return a list of IDs for orphaned HVAC devices."""
        ...

    async def params(self) -> dict[str, Any]:
        """Return the parameters for all devices."""
        ...

    async def status(self) -> dict[str, Any]:
        """Return the status for all devices."""
        ...

    def handle_topology_event(self, event: "TopologyChangedEvent") -> None:
        """Process an immutable structural graph mutation event."""
        ...


class GatewayInterface(Protocol):
    """Interface for the core Gateway orchestrator."""

    @property
    def device_registry(self) -> DeviceRegistryInterface:
        """Return the Device Registry."""
        ...

    @property
    def message_store(self) -> MessageStoreInterface | None: ...

    @message_store.setter
    def message_store(self, value: MessageStoreInterface | None) -> None: ...

    @property
    def config(self) -> Any:
        """Return the gateway configuration."""
        ...

    async def async_send_cmd(
        self,
        cmd: Command,
        /,
        *,
        priority: Priority = Priority.DEFAULT,
        wait_for_reply: bool | None = True,
        max_retries: int = 3,
        timeout: float = 3.0,
    ) -> Packet:
        """Send a command asynchronously and return the resulting packet."""
        ...

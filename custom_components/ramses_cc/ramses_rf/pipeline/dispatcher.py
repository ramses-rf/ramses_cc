"""RAMSES RF - Asynchronous Central Dispatcher routing pipeline."""

import asyncio
import contextlib
import logging
from typing import Final

from ..messages.core import Message

_LOGGER = logging.getLogger(__name__)

_ALL_DEVICE_ID: Final[str] = "63:262142"
_CODE_BINDING: Final[str] = "1FC9"
_PHASE_OFFER: Final[str] = "offer"


class CentralDispatcher:
    """Asynchronous pipeline task to route Messages to L7 domain engines.

    Replaces the legacy synchronous callback routing from `gwy.route_payload`.
    Evaluates OSI Master Plan L7 routing rules and drops messages into target
    asyncio Queues for decoupled consumption.
    """

    def __init__(self, in_queue: asyncio.Queue[Message]) -> None:
        """Initialize the dispatcher and its target routing queues.

        :param in_queue: Queue providing decoded Messages from the Decoder.
        :type in_queue: asyncio.Queue[Message]
        """
        self._in_queue = in_queue

        # Target Domain Queues
        self.ssot_queue: asyncio.Queue[Message] = asyncio.Queue()
        self.discovery_queue: asyncio.Queue[Message] = asyncio.Queue()
        self.binding_queue: asyncio.Queue[Message] = asyncio.Queue()
        self.faked_queue: asyncio.Queue[Message] = asyncio.Queue()

        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background routing task."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background routing task cleanly."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """Main asynchronous loop consuming the input queue."""
        while True:
            msg = await self._in_queue.get()
            try:
                self._dispatch(msg)
            except Exception as err:
                _LOGGER.exception("CentralDispatcher failed to route message: %s", err)
            finally:
                self._in_queue.task_done()

    def _dispatch(self, msg: Message) -> None:
        """Evaluate L7 routing rules and push to appropriate queues.

        :param msg: The message to route.
        :type msg: Message
        """
        # 1. Standard Routing: All messages hit SSOT & Discovery
        self.ssot_queue.put_nowait(msg)
        self.discovery_queue.put_nowait(msg)

        # Retrieve the code natively from the L7 header
        code = str(msg.header.code)

        # 2. Binding Anomaly (1FC9 Offers)
        if code == _CODE_BINDING and msg.data.get("phase") == _PHASE_OFFER:
            self.binding_queue.put_nowait(msg)
            return

        # 3. Global Broadcast / Virtualization Interception
        if msg.dst.id == _ALL_DEVICE_ID:
            # Broadcasts must reach faked devices to evaluate network changes
            self.faked_queue.put_nowait(msg)
        elif msg.dst.id != msg.src.id:
            # Directed command to another device.
            # Route to faked queue so virtual device consumers can intercept
            # if the target address matches a virtual shadow.
            self.faked_queue.put_nowait(msg)

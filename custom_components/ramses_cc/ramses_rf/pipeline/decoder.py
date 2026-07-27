"""RAMSES RF - Asynchronous payload decoder engine pipeline."""

import asyncio
import contextlib
import logging
from typing import Any

from ..address import Address
from ..enums import Topic
from ..messages.core import Message
from ..parsers.decoder import decode_packet
from ..routing import StateHeader
from ...ramses_tx import exceptions as exc
from ...ramses_tx.const import Code
from ...ramses_tx.dtos import PacketDTO
from ...ramses_tx.typing import DeviceIdT

_LOGGER = logging.getLogger(__name__)


class DecoderEngine:
    """Asynchronous pipeline task to decode PacketDTOs into L7 Messages."""

    def __init__(
        self,
        in_queue: asyncio.Queue[PacketDTO],
        out_queue: asyncio.Queue[Message],
    ) -> None:
        """Initialize the decoder engine queues.

        :param in_queue: Queue providing stitched transport packets.
        :type in_queue: asyncio.Queue[PacketDTO]
        :param out_queue: Queue receiving fully decoded Messages.
        :type out_queue: asyncio.Queue[Message]
        """
        self._in_queue = in_queue
        self._out_queue = out_queue
        self._task: asyncio.Task[None] | None = None

        # Anti-spam tracker to ensure we only log each unknown code once per session
        self._unknown_codes: set[str] = set()

    async def start(self) -> None:
        """Start the background consumer task."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background consumer task cleanly."""
        if self._task:
            self._task.cancel()
            with (
                contextlib.suppress(asyncio.CancelledError),
            ):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """Main asynchronous loop consuming the input queue."""
        while True:
            dto = await self._in_queue.get()
            try:
                await self._process_packet(dto)
            except Exception as err:
                _LOGGER.exception("DecoderEngine failed to process packet: %s", err)
            finally:
                self._in_queue.task_done()

    async def _process_packet(self, dto: PacketDTO) -> None:
        """Decode a single PacketDTO and output a frozen Message.

        :param dto: The inbound packet to decode.
        :type dto: PacketDTO
        """
        try:
            try:
                raw_data = decode_packet(dto)
            except exc.PacketPayloadInvalid:
                # Fallback for null payloads mirroring legacy base.py logic
                if self._has_payload(dto):
                    raise
                raw_data = {}
        except exc.PacketInvalid as err:
            _LOGGER.warning("Invalid packet discarded: %s", repr(err))
            return

        # Ensure strict adherence to dict[str, Any] L7 constraints
        data: dict[str, Any] = (
            {"_array": raw_data} if isinstance(raw_data, list) else dict(raw_data)
        )

        # Safe L2 positional MAC resolution matching legacy routing
        addr1 = dto.addr1 if dto.addr1 else "--:------"
        addr2 = dto.addr2 if dto.addr2 else "--:------"
        addr3 = dto.addr3 if dto.addr3 else "--:------"

        valid = [a for a in (addr1, addr2, addr3) if a and a != "--:------"]
        src_id = valid[0] if valid else "--:------"
        dst_id = valid[1] if len(valid) > 1 else src_id

        # COMMUNITY LOGGING HOOK: Trap unknown command codes gracefully
        try:
            Code(dto.code)
        except ValueError:
            if dto.code not in self._unknown_codes:
                self._unknown_codes.add(dto.code)
                _LOGGER.warning(
                    "Unknown command code '%s' detected from device %s. "
                    "Please help support the development of ramses_rf by raising "
                    "an issue on GitHub with your packet log to help us decode it! "
                    "Payload: %s",
                    dto.code,
                    src_id,
                    dto.payload,
                )

        # Native L7 Context & Header Generation
        ctx_val: str | bool | None = dto.payload[:2] if dto.payload else False
        if dto.code == "3220" and len(dto.payload) >= 6:
            ctx_val = dto.payload[4:6]

        header = StateHeader.create(
            code=dto.code,
            verb=dto.verb,
            source_id=src_id,
            context_val=ctx_val,
        )

        # Instantiate the immutable historical fact at the pipeline's edge
        msg = Message(
            topic=Topic.RAW_EVENT,
            header=header,
            src=Address(DeviceIdT(src_id)),
            dst=Address(DeviceIdT(dst_id)),
            data=data,
            packets=(dto,),
            timestamp=dto.timestamp,
        )

        await self._out_queue.put(msg)

    def _has_payload(self, dto: PacketDTO) -> bool:
        """Return False if there is no payload, matching legacy rules.

        :param dto: The packet being evaluated.
        :type dto: PacketDTO
        :return: True if payload is expected, False otherwise.
        :rtype: bool
        """
        try:
            length = int(dto.length)
        except ValueError:
            length = 0

        if length == 1:
            return False
        if str(dto.verb).strip() == "RQ":
            if length == 2 and dto.code != "0016":
                return False
        return True

"""RAMSES RF - Asynchronous packet reassembly pipeline."""

import asyncio
import contextlib
import logging
from datetime import timedelta as td
from typing import Final

from ...ramses_tx.dtos import PacketDTO

_LOGGER = logging.getLogger(__name__)

# Codes whose ``I`` packets are *always* array fragments at the L3 layer
# and can therefore be safely buffered for reassembly using only (code,
# verb) inspection -- the only metadata available to this module via
# PacketDTO.
#
# Audit of ramses_rf.protocol.ramses.CODES_WITH_ARRAYS (the
# authoritative schema, which lists 0005/0009/000A/2309/30C9/2249/22C9/
# 3150 plus the special-cased 000C/1FC9) for issue #669:
#
#   - 000A, 22C9: safe. Their ``I`` packets from a controller/UFC are
#     arrays (a lone single-element ``I`` is an acceptable false
#     negative, as noted in ramses_tx.frame.Frame._has_array).
#   - 2309, 30C9: NOT safe to add here. These routinely emit *single*
#     zone packets (one zone's temperature) that must pass straight
#     through. The frame layer distinguishes array vs single via a
#     payload-length multiple heuristic (Frame._has_array) that this
#     module does not have access to.
#   - 3150: NOT safe here for the same reason -- a single heat-demand
#     packet is indistinguishable from a fragment without the
#     length/domain check (and the UFC-only src.type guard) that lives
#     in the frame layer.
#   - 1FC9, 000C, 0005, 0009, 2249: special-cased or non-fragmenting
#     here.
#
# Expanding this set to 2309/30C9/3150 correctly requires migrating the
# Frame._has_array length heuristic into (or exposing it to) the
# reassembly pipeline, which is part of the broader #608 schema
# migration. Until then, the conservative set below matches the legacy
# detect_array_fragment() behaviour in dispatcher.py without introducing
# false buffering of routine single-zone packets. See issue #669.
_ARRAY_CODES: Final[tuple[str, ...]] = (
    "000A",
    "22C9",
)
_VERB_I: Final[str] = " I"

# A pending array is keyed by (src_id, code) so that an intervening
# packet from a different source (or a different code) does not abort
# an in-flight reassembly. See issue #669 (sliding window buffer).
_PendingKey = tuple[str, str]


class ReassemblyBuffer:
    """Asynchronous pipeline task to stitch multi-packet arrays.

    Consumes raw PacketDTOs from the transport layer, buffers potential
    array fragments, and outputs unified PacketDTOs.

    Unlike a single-slot buffer, this implementation maintains a
    *sliding window* of pending arrays keyed by ``(src_id, code)``. An
    unrelated packet (RF noise, or a broadcast from a different device)
    that arrives between two fragments of an array therefore passes
    straight through without aborting the in-progress reassembly.
    """

    def __init__(
        self,
        in_queue: asyncio.Queue[PacketDTO],
        out_queue: asyncio.Queue[PacketDTO],
        array_timeout: float = 15.0,
    ) -> None:
        """Initialize the reassembly buffer queues.

        :param in_queue: Queue providing raw transport packets.
        :type in_queue: asyncio.Queue[PacketDTO]
        :param out_queue: Queue receiving stitched packets.
        :type out_queue: asyncio.Queue[PacketDTO]
        :param array_timeout: Seconds to wait for the next fragment.
        :type array_timeout: float
        """
        self._in_queue = in_queue
        self._out_queue = out_queue
        self._array_timeout = array_timeout
        self._task: asyncio.Task[None] | None = None

        self._pending: dict[_PendingKey, PacketDTO] = {}

    async def start(self) -> None:
        """Start the background consumer task."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background consumer task cleanly."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """Main asynchronous loop consuming the input queue."""
        while True:
            try:
                # Wait for a packet, or timeout if we have any pending
                # arrays
                timeout = self._array_timeout if self._pending else None
                dto = await asyncio.wait_for(self._in_queue.get(), timeout=timeout)
                await self._process_packet(dto)
                self._in_queue.task_done()

            except TimeoutError:
                # One or more pending arrays have exceeded their timeout:
                # flush every stale pending packet in arrival-key order.
                await self._flush_stale_pending()

    async def _flush_stale_pending(self) -> None:
        """Flush every pending array whose timeout has expired.

        The loop's ``wait_for`` only times out when no packet has
        arrived for ``array_timeout`` seconds, so every pending entry
        (which was buffered at or before the most recently received
        packet) is stale by definition when this is called.
        """
        # Snapshot the keys to avoid mutating the dict while iterating.
        for key in list(self._pending):
            pending = self._pending.pop(key, None)
            if pending is None:
                continue
            _LOGGER.warning(
                "%s < Array reassembly timeout (%ss): flushing "
                "incomplete multi-packet message",
                pending.code,
                self._array_timeout,
            )
            await self._out_queue.put(pending)

    async def _process_packet(self, dto: PacketDTO) -> None:
        """Evaluate a packet for array stitching or passthrough.

        A packet is matched against a pending array for the same
        ``(src_id, code)``. Unrelated packets (different source or code)
        are passed straight through without disturbing any in-flight
        reassembly.

        :param dto: The inbound packet to evaluate.
        :type dto: PacketDTO
        """
        key: _PendingKey = (dto.addr1, dto.code)
        pending = self._pending.get(key)

        if pending is None:
            if self._is_potential_array_start(dto):
                self._pending[key] = dto
            else:
                await self._out_queue.put(dto)
            return

        if self._is_array_fragment(pending, dto):
            stitched_dto = self._stitch_dtos(pending, dto)
            await self._out_queue.put(stitched_dto)
            del self._pending[key]
        else:
            # The pending fragment for this key is not continued by
            # `dto`: flush the stale pending packet and re-evaluate
            # `dto` itself.
            _LOGGER.warning(
                "%s < Array reassembly interrupted for %s: flushing "
                "incomplete multi-packet message",
                pending.code,
                key,
            )
            await self._out_queue.put(pending)
            del self._pending[key]
            if self._is_potential_array_start(dto):
                self._pending[key] = dto
            else:
                await self._out_queue.put(dto)

    def _is_potential_array_start(self, dto: PacketDTO) -> bool:
        """Return True if the packet might be the start of an array.

        :param dto: The packet to evaluate.
        :type dto: PacketDTO
        :return: True if the packet matches array criteria.
        :rtype: bool
        """
        return dto.code in _ARRAY_CODES and dto.verb == _VERB_I

    def _is_array_fragment(self, prev: PacketDTO, this: PacketDTO) -> bool:
        """Return True if 'this' packet is a fragment of 'prev'.

        :param prev: The previously buffered packet.
        :type prev: PacketDTO
        :param this: The current packet being evaluated.
        :type this: PacketDTO
        :return: True if the packets should be stitched.
        :rtype: bool
        """
        if this.code != prev.code or this.verb != prev.verb:
            return False
        if this.addr1 != prev.addr1:
            return False
        return this.timestamp < prev.timestamp + td(seconds=self._array_timeout)

    def _stitch_dtos(self, prev: PacketDTO, this: PacketDTO) -> PacketDTO:
        """Combine two array fragments into a single PacketDTO.

        :param prev: The first fragment.
        :type prev: PacketDTO
        :param this: The second fragment.
        :type this: PacketDTO
        :return: A new PacketDTO containing the combined payload.
        :rtype: PacketDTO
        """
        combined_payload = f"{prev.payload}{this.payload}"
        combined_length = f"{len(combined_payload) // 2:03d}"

        # DTO is frozen; instantiate a new one with the combined L3 state
        return PacketDTO(
            timestamp=this.timestamp,
            rssi=this.rssi,
            verb=prev.verb,
            seq=prev.seq,
            addr1=prev.addr1,
            addr2=prev.addr2,
            addr3=prev.addr3,
            code=prev.code,
            length=combined_length,
            payload=combined_payload,
        )

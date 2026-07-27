#!/usr/bin/env python3
"""RAMSES RF - State Storage and Database Query Component.

This module provides the EntityState component, which manages database
interactions and state querying for an entity, bridging the Event-Driven
architecture using native StateHeader dictionary lookups.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime as dt
from typing import TYPE_CHECKING, Any, cast

from ...ramses_tx.address import ALL_DEVICE_ID

# noqa: F401, isort: skip, pylint: disable=unused-import
from ...ramses_tx.const import I_, RP, RQ, Code, VerbT

from .. import exceptions as exc
from ..const import SZ_DOMAIN_ID, SZ_NAME, SZ_ZONE_IDX
from ..messages import ApplicationMessage, Message
from ..protocol.ramses import CODES_SCHEMA
from ..routing import RoutingContext, StateHeader

if TYPE_CHECKING:
    from ...ramses_tx.typing import HeaderT

    from ..interfaces import DeviceInterface, GatewayInterface
    from .store import MessageStore

_LOGGER = logging.getLogger(__name__)

# Constants for slicing the device ID
_ID_SLICE = 9


class StateCache:
    """Encapsulates the state cache data collection for an entity.

    Now natively powered by O(1) dictionary lookups via StateHeader DTOs.
    """

    def __init__(self) -> None:
        """Initialize the StateCache."""
        self._cache: dict[StateHeader, ApplicationMessage] = {}

    def add(self, msg: ApplicationMessage) -> None:
        """Add a message to the cache natively via its header."""
        self._cache[msg.state_header] = msg

    def get_message(self, header: StateHeader) -> ApplicationMessage | None:
        """Retrieve a message directly by its StateHeader."""
        return self._cache.get(header)

    def get_by_routing_key(
        self, code: Code | str, verb: VerbT | str, ctx: Any
    ) -> ApplicationMessage | None:
        """Fallback O(N) lookup when source_id is unknown."""
        ctx_str = RoutingContext(ctx).as_string
        for hdr, msg in self._cache.items():
            if (
                hdr.code == code
                and hdr.verb == verb
                and hdr.context.as_string == ctx_str
            ):
                return msg
        return None

    def get_by_code(self, code: Code | str) -> list[ApplicationMessage]:
        """Retrieve all messages for a specific code."""
        return [msg for hdr, msg in self._cache.items() if hdr.code == code]

    def get_all(self) -> list[ApplicationMessage]:
        """Retrieve all stored messages."""
        return list(self._cache.values())

    def get_records(
        self,
    ) -> list[tuple[Code | str, VerbT | str, Any, ApplicationMessage]]:
        """Retrieve all cache records as tuples of (code, verb, ctx, msg)."""
        return [(h.code, h.verb, h.context.value, m) for h, m in self._cache.items()]


class EntityState:
    """Manages database interactions and state queries for an entity.

    This class acts as a stateless facade. It delegates all heavy lifting
    to the Gateway's central MessageStore while serving as the SSOT ingestion
    point.
    """

    def __init__(self, entity: DeviceInterface, gwy: GatewayInterface) -> None:
        """Initialize the EntityState."""
        self._entity = entity
        self._gwy = gwy
        # Context-Aware O(1) Dictionary natively keyed by StateHeader DTO
        self._current_state: dict[StateHeader, ApplicationMessage] = {}
        self._log_cursor: int = 0  # Tracks cursor position in global log

        # Tracks DB tasks to maintain Message object immutability
        self._pending_deletes: set[StateHeader] = set()
        self._delete_tasks: set[asyncio.Task[Any]] = set()

    def _sync_state(self) -> None:
        """Hybrid Push Model: Sync O(1) cache using cursor on global log."""
        if self._gwy.message_store is None:
            return

        log_iterable = self._gwy.message_store.log_by_dtm
        if isinstance(log_iterable, dict):
            log_iterable = list(log_iterable.values())

        current_len = len(log_iterable)
        if current_len == self._log_cursor:
            return  # No new packets, O(1) instant exit

        if current_len < self._log_cursor:
            # The global log was cleared (e.g. cache reset). Rebuild.
            self._log_cursor = 0
            self._current_state.clear()
            self._pending_deletes.clear()
            for task in self._delete_tasks:
                if not task.done():
                    task.cancel()
            self._delete_tasks.clear()

        # Only process NEW packets appended since the last sync
        new_msgs = log_iterable[self._log_cursor :]
        for msg in new_msgs:
            self.ingest_message(msg)

        self._log_cursor = current_len

    def ingest_message(self, msg: ApplicationMessage) -> None:
        """SSOT async ingestion queue preparation.

        In Phase 3, this will drop the DecodedMessage into an asyncio.Queue.
        For now, it processes the state synchronously.
        """
        self.update_state(msg)

    def update_state(self, msg: ApplicationMessage) -> None:
        """Push model: Instantly cache relevant messages upon arrival."""
        if not self._is_relevant_msg(msg):
            return

        entity_id = self._entity.id
        is_dhw = entity_id[_ID_SLICE:] == "_HW"
        is_zone = len(entity_id) > 9 and not is_dhw
        ctx = msg.context.value

        if is_zone:
            zone_idx = entity_id[_ID_SLICE + 1 :]
            in_dict = isinstance(msg.payload, dict)
            in_list = isinstance(msg.payload, list)
            if not (
                ctx == zone_idx
                or (in_dict and str(msg.payload.get(SZ_ZONE_IDX)) == zone_idx)
                or (
                    in_list
                    and any(
                        isinstance(d, dict) and str(d.get(SZ_ZONE_IDX)) == zone_idx
                        for d in msg.payload
                    )
                )
            ):
                return  # Payload does not belong to this specific zone

        if is_dhw:
            in_dict = isinstance(msg.payload, dict)
            in_list = isinstance(msg.payload, list)
            if not (
                ctx in ("FC", "FA", "F9", "FA")
                or (in_dict and "dhw_idx" in msg.payload)
                or (
                    in_list
                    and any(isinstance(d, dict) and "dhw_idx" in d for d in msg.payload)
                )
            ):
                return  # Payload does not belong to DHW

        # Context-aware O(1) Overwrite directly keyed by DTO
        self._current_state[msg.state_header] = msg

    def _is_relevant_msg(self, msg: ApplicationMessage) -> bool:
        """Check if a central MessageStore packet is relevant to entity."""
        return bool(
            msg.src.id == self._entity.id[:_ID_SLICE]
            or (msg.dst.id == self._entity.id[:_ID_SLICE] and msg.verb != RQ)
            or (msg.dst.id == ALL_DEVICE_ID and msg.code == Code._1FC9)
        )

    async def get_all_messages(self) -> list[ApplicationMessage]:
        """Return a flattened list of all messages logged on this device."""
        cache = await self._build_state_cache()
        return cache.get_all()

    _msg_list = get_all_messages

    def _add_record(
        self,
        dev_id: str,
        code: Code | str | None = None,
        verb: str = " I",
        payload: str = "00",
    ) -> None:
        """Add a (dummy) record to the central SQLite MessageStore."""
        if self._gwy.message_store:
            self._gwy.message_store.add_record(
                dev_id, code=str(code), verb=verb, payload=payload
            )

    async def _delete_msg(self, msg: ApplicationMessage) -> None:
        """Remove the msg from the central state databases."""
        if self._gwy.message_store:
            store = cast("MessageStore", self._gwy.message_store)
            await store.rem(msg)
        self._pending_deletes.discard(msg.state_header)

    async def _get_msg_by_hdr(
        self, hdr: HeaderT | StateHeader
    ) -> ApplicationMessage | None:
        """Return a msg, if any, that matches a given header."""
        if isinstance(hdr, str):
            code_str, verb_str, src_id, *args = hdr.split("|")
            ctx_val: str | bool | None = args[0] if args else None
            if ctx_val == "True":
                ctx_val = True
            elif ctx_val == "False":
                ctx_val = False
            header = StateHeader.create(code_str, verb_str, src_id, ctx_val)
        else:
            header = hdr

        if self._gwy.message_store:
            store = cast("MessageStore", self._gwy.message_store)
            msgs = await store.get(hdr=header)
            if msgs:
                if (
                    msgs[0].state_header != header
                    and msgs[0].state_header.legacy_hdr != hdr
                ):
                    raise exc.DatabaseQueryError(
                        f"Header mismatch: {msgs[0].state_header.legacy_hdr} != {hdr}"
                    )
                return cast("ApplicationMessage", msgs[0])
            return None

        cache = await self._build_state_cache()
        msg = cache.get_message(header)

        # Fallbacks for unknown source ID routing
        if msg is None:
            msg = cache.get_by_routing_key(
                header.code, header.verb, header.context.value
            )
        if msg is None:
            msg = cache.get_by_routing_key(header.code, header.verb, False)
        if msg is None:
            msg = cache.get_by_routing_key(header.code, header.verb, None)

        if msg is None:
            return None

        if msg.state_header != header and msg.state_header.legacy_hdr != hdr:
            raise exc.DatabaseQueryError(
                f"Header mismatch: {msg.state_header.legacy_hdr} != {hdr}"
            )
        return msg

    async def get_flag(self, code: Code | str, key: str, idx: int) -> bool | None:
        """Get the boolean value of a specific flag within a payload."""
        if flags := await self.get_value(code, key=key):
            return bool(flags[idx])
        return None

    _msg_flag = get_flag

    async def get_value(
        self,
        code: Code | str | tuple[Code | str, ...] | ApplicationMessage,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Get the value for a Code from the database or from a Message."""
        if isinstance(code, (str, tuple)):
            return await self._msg_value_code(code, *args, **kwargs)

        assert isinstance(code, Message), f"Invalid format: get_value({code})"
        return self._msg_value_msg(code, *args, **kwargs)

    _msg_value = get_value

    async def _msg_value_code(
        self,
        code: Code | str | tuple[Code | str, ...],
        verb: VerbT | str | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Query the index for the most recent key: value pairs."""
        assert not isinstance(code, tuple) or verb is None, (
            f"Unsupported: using a tuple ({code}) with a verb ({verb})"
        )

        self._sync_state()

        if verb:
            if verb == VerbT.RQ or verb == "RQ":
                assert not isinstance(code, tuple), (
                    f"Unsupported: using a keyword ({key}) with verb RQ"
                )
                key = None
            try:
                cd = await self.find_latest_code(code, key, **kwargs, verb=verb)
                if cd:
                    cache = await self._build_state_cache()

                    # Retrieve the exact context-aware message
                    entity_id = self._entity.id
                    is_dhw = entity_id[_ID_SLICE:] == "_HW"
                    is_zone = len(entity_id) > 9 and not is_dhw
                    ctx = kwargs.get(
                        "zone_idx",
                        entity_id[_ID_SLICE + 1 :] if is_zone else None,
                    )
                    if not ctx and is_dhw:
                        ctx = kwargs.get("dhw_idx", "HW")

                    msg = cache.get_by_routing_key(cd, verb, ctx)
                    if msg is None:
                        # Fallbacks for base devices
                        msg = cache.get_by_routing_key(cd, verb, False)
                    if msg is None:
                        msg = cache.get_by_routing_key(cd, verb, None)
                else:
                    msg = None
            except KeyError:
                msg = None
        elif isinstance(code, tuple):
            msgs_dict = await self.get_message_log_flat()
            msgs_list = [m for m in msgs_dict.values() if m.code in code]
            msg = max(msgs_list, key=lambda m: m.dtm) if msgs_list else None
        else:
            msgs_dict = await self.get_message_log_flat()
            msg = msgs_dict.get(code)

        return self._msg_value_msg(msg, key=key, **kwargs)

    def _msg_value_msg(
        self,
        msg: ApplicationMessage | None,
        key: str | None = "*",
        zone_idx: str | None = None,
        domain_id: str | None = None,
    ) -> Any:
        """Get all or a specific key with its values from a Message."""
        if msg is None:
            return None

        if getattr(msg, "_expired", False):
            hdr = msg.state_header
            if hdr not in self._pending_deletes:
                loop = getattr(self._gwy, "_loop", None)
                if loop is None:
                    with contextlib.suppress(RuntimeError):
                        loop = asyncio.get_running_loop()

                # Protect against the Mock Trap during testing
                if isinstance(loop, asyncio.AbstractEventLoop):
                    self._pending_deletes.add(hdr)
                    task = loop.create_task(self._delete_msg(msg))
                    if task is not None:
                        self._delete_tasks.add(task)
                        task.add_done_callback(self._delete_tasks.discard)

        payload = msg.payload

        if msg.code == Code._1FC9:
            return [x[1] for x in payload]

        idx: str | None = None
        val: str | None = None

        if domain_id:
            idx, val = SZ_DOMAIN_ID, domain_id
        elif zone_idx:
            idx, val = SZ_ZONE_IDX, zone_idx

        if isinstance(payload, dict):
            msg_dict = payload
            if idx and idx != SZ_DOMAIN_ID and msg_dict.get(idx) != val:
                return None
        elif idx:
            msg_dict = {
                k: v for d in payload for k, v in d.items() if d.get(idx) == val
            }
            if not msg_dict:
                return None
        else:
            if not payload:
                return None
            if isinstance(payload, list) and (key == "*" or not key):
                return payload
            msg_dict = payload[0]

        if key == "*" or not key:
            return {
                k: v
                for k, v in msg_dict.items()
                if k not in ("dhw_idx", SZ_DOMAIN_ID, SZ_ZONE_IDX) and k[:1] != "_"
            }
        return msg_dict.get(key)

    async def _msg_dev_qry(self) -> list[Code | str] | None:
        """Retrieve a list of Code keys involving this device."""
        res: set[Code | str] = set()
        entity_id = self._entity.id
        is_dhw = entity_id[_ID_SLICE:] == "_HW"
        is_zone = len(entity_id) > 9 and not is_dhw
        zone_idx = entity_id[_ID_SLICE + 1 :] if is_zone else None

        cache = await self._build_state_cache()

        for code, verb, ctx, msg in cache.get_records():
            if verb not in (I_, RP):
                continue
            if is_dhw:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if (
                    ctx in ("FC", "FA", "F9", "FA")
                    or (in_dict and "dhw_idx" in msg.payload)
                    or (
                        in_list
                        and any(
                            isinstance(d, dict) and "dhw_idx" in d for d in msg.payload
                        )
                    )
                ):
                    res.add(code)
            elif is_zone:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if (
                    ctx == zone_idx
                    or (in_dict and str(msg.payload.get("zone_idx")) == zone_idx)
                    or (
                        in_list
                        and any(
                            isinstance(d, dict) and str(d.get("zone_idx")) == zone_idx
                            for d in msg.payload
                        )
                    )
                ):
                    res.add(code)
            else:
                res.add(code)
        return list(res)

    async def find_latest_code(
        self,
        code: Code | str | tuple[Code | str, ...] | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> Code | str | None:
        """Retrieve the most current Code involving this device."""
        latest: dt = dt.min.replace(tzinfo=UTC)
        res: Code | str | None = None

        entity_id = self._entity.id
        is_dhw = entity_id[_ID_SLICE:] == "_HW"
        is_zone = len(entity_id) > 9 and not is_dhw
        zone_idx = kwargs.get(
            "zone_idx", entity_id[_ID_SLICE + 1 :] if is_zone else None
        )
        dhw_idx = kwargs.get("dhw_idx")

        allowed_verbs = (
            (kwargs.get("verb"),)
            if kwargs.get("verb") in (" I", "RP")
            else (" I", "RP")
        )

        cache = await self._build_state_cache()

        for cd, verb, ctx, msg in cache.get_records():
            if code is not None:
                if isinstance(code, tuple) and cd not in code:
                    continue
                elif not isinstance(code, tuple) and cd != code:
                    continue

            if verb not in allowed_verbs:
                continue

            if zone_idx is not None:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if not (
                    str(ctx) == str(zone_idx)
                    or (in_dict and str(msg.payload.get("zone_idx")) == str(zone_idx))
                    or (
                        in_list
                        and any(
                            isinstance(d, dict)
                            and str(d.get("zone_idx")) == str(zone_idx)
                            for d in msg.payload
                        )
                    )
                ):
                    continue

            if dhw_idx is not None:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if not (
                    str(ctx) == str(dhw_idx)
                    or ctx in ("FC", "FA", "F9", "FA")
                    or (in_dict and "dhw_idx" in msg.payload)
                    or (
                        in_list
                        and any(
                            isinstance(d, dict) and "dhw_idx" in d for d in msg.payload
                        )
                    )
                ):
                    continue

            if key is not None:
                if isinstance(msg.payload, dict):
                    if key not in msg.payload:
                        continue
                elif isinstance(msg.payload, list):
                    if not any(isinstance(d, dict) and key in d for d in msg.payload):
                        continue
                else:
                    continue

            # Ensure timezone awareness for legacy naive dt
            msg_dtm = msg.dtm
            if msg_dtm.tzinfo is None:
                msg_dtm = msg_dtm.replace(tzinfo=UTC)

            if msg_dtm > latest:
                latest = msg_dtm
                res = cd
        return res

    _msg_qry_by_code_key = find_latest_code

    async def _msg_qry(self, sql: str) -> list[dict[str, Any]]:
        """Custom query for an entity's stored payloads."""
        _LOGGER.warning("Legacy _msg_qry (SQL) called. Returning empty.")
        return []

    async def get_message_log_flat(self) -> dict[Code | str, ApplicationMessage]:
        """Dynamically build flat dict of all I/RP messages logged."""
        self._sync_state()
        _msg_dict: dict[Code | str, ApplicationMessage] = {}

        # Build from _build_state_cache to guarantee strict zone_idx isolation
        cache = await self._build_state_cache()

        for code, verb, _ctx, msg in cache.get_records():
            if verb not in (I_, RP):
                continue
            if code not in _msg_dict or msg.dtm > _msg_dict[code].dtm:
                _msg_dict[code] = msg

        return _msg_dict

    async def _build_state_cache(self) -> StateCache:
        """Dynamically build a flat cache of all messages for this entity."""
        self._sync_state()
        cache = StateCache()

        # Extremely fast O(1) iteration, bypassing global history!
        for _hdr, msg in self._current_state.items():
            cache.add(msg)

        return cache

    def _handle_msg(self, msg: ApplicationMessage) -> None:
        """Deprecated: The proxy no longer caches its own packets."""
        pass

    async def traits(self) -> dict[str, Any]:
        """Get the codes seen by the entity."""
        msgs_dict = await self.get_message_log_flat()

        # Build traits dictionary safely checking for valid Code enums
        codes: dict[Code | str, str | None] = {}
        for code in sorted(msgs_dict, key=str):
            if msgs_dict[code].src.id == self._entity.id[:9]:
                name = None
                # Type-narrow to satisfy CODES_SCHEMA strict indexing requirements
                if isinstance(code, Code) and code in CODES_SCHEMA:
                    name = CODES_SCHEMA[code].get(SZ_NAME)
                codes[code] = name

        return {"_sent": list(codes.keys())}

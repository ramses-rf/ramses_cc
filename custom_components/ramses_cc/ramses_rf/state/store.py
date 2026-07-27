#!/usr/bin/env python3
"""
RAMSES RF - Message database and index.

.. table:: Database Query Methods
   :widths: auto

   =====  ============  ===========  ==========  ====  ========================
    ix    method name   args         returns     uses  used by
   =====  ============  ===========  ==========  ====  ========================
   i1     get           Msg, kwargs  tuple(Msg)        EntityState
   i2     contains      kwargs       bool        i1    EntityState
   i7     get_rp_codes  src, dst     list(Code)        Discovery-supported_cmds
   =====  ============  ===========  ==========  ====  ========================
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import uuid
from collections import OrderedDict
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, NewType, cast

import orjson

from ...ramses_tx import RP, RQ, Code, Packet

from ..exceptions import DatabaseQueryError
from ..messages.base import Message
from ..messages.core import Message as CoreMessage
from ..protocol_schema import CODES_SCHEMA
from ..routing import StateHeader
from ..sqlite_worker import PacketLogEntry, SQLiteWorker

DtmStrT = NewType("DtmStrT", str)

if TYPE_CHECKING:
    MsgDdT = OrderedDict[DtmStrT, Message]

_LOGGER = logging.getLogger(__name__)


def _setup_db_adapters() -> None:
    """Set up the database adapters and converters."""

    def adapt_datetime_iso(val: dt) -> str:
        """Adapt datetime.datetime to timezone-naive ISO 8601 datetime to
        match _message_log dtm keys."""
        return val.isoformat(timespec="microseconds")

    sqlite3.register_adapter(dt, adapt_datetime_iso)

    def convert_datetime(val: bytes) -> dt:
        """Convert ISO 8601 datetime to datetime.datetime object to import
        dtm in msg_db."""
        return dt.fromisoformat(val.decode())

    sqlite3.register_converter("DTM", convert_datetime)


def payload_keys(parsed_payload: list[dict[str, Any]] | dict[str, Any]) -> str:
    """
    Copy payload keys for fast query check.

    :param parsed_payload: pre-parsed message payload dict
    :return: string of payload keys, separated by the | char
    """
    _keys: str = "|"

    def append_keys(ppl: dict[str, Any]) -> str:
        _ks: str = ""
        for k, v in ppl.items():
            if (
                k not in _ks and k not in _keys and v is not None
            ):  # ignore keys with None value
                _ks += k + "|"
        return _ks

    if isinstance(parsed_payload, list):
        for d in parsed_payload:
            _keys += append_keys(d)
    elif isinstance(parsed_payload, dict):
        _keys += append_keys(parsed_payload)
    return _keys


class MessageStore:
    """A central in-memory SQLite3 database for indexing RF messages.
    Index holds all the latest messages to & from all devices by `dtm`
    (timestamp) and strictly-typed `StateHeader` DTOs."""

    _housekeeping_task: asyncio.Task[None]
    _hydration_task: asyncio.Task[None]

    def __init__(
        self,
        maintain: bool = True,
        db_path: str = ":memory:",
        disk_path: str | None = "ramses.db",
    ) -> None:
        """Instantiate a message database/index."""

        self.maintain = maintain
        # In-memory RAM caches (Filled & cleaned up in housekeeping_loop)
        # stores all messages for retrieval.
        self._message_log: MsgDdT = OrderedDict()
        # Caches the latest message per strictly-typed StateHeader.
        self._state_cache: dict[StateHeader, Message] = {}

        # Synchronous Test Mode: Bypass background worker entirely if testing
        self._is_testing = "PYTEST_CURRENT_TEST" in os.environ

        self._consumer_task: asyncio.Task[None] | None = None

        if self._is_testing:
            self._worker: SQLiteWorker | None = None
            self._cx: sqlite3.Connection | None = None
        else:
            # For :memory: databases with multiple connections (Reader vs Worker)
            # We must use a Shared Cache URI so both threads see the same data.
            if db_path == ":memory:":
                # Unique ID ensures parallel tests don't share the same in-memory DB
                db_path = f"file:ramses_rf_{uuid.uuid4()}?mode=memory&cache=shared"

            # Start the Storage Worker (Write Connection)
            # This thread handles all blocking INSERT/UPDATE operations
            self._worker = SQLiteWorker(db_path, disk_path=disk_path)

            # Wait for the worker to create the tables.
            # This prevents "no such table" errors on immediate reads.
            if not self._worker.wait_for_ready(timeout=10.0):
                _LOGGER.error(
                    "MessageStore: SQLiteWorker timed out initializing database"
                )

            # Connect to a SQLite DB (Read Connection)
            self._cx = sqlite3.connect(
                db_path,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=False,
                uri=True,  # Enable URI parsing for shared memory support
                timeout=10.0,  # Increased timeout to reduce 'database locked' errors
                isolation_level=None,  # Autocommit mode prevents stale snapshots
            )

            # Enable Write-Ahead Logging for Reader as well
            if db_path != ":memory:" and "mode=memory" not in db_path:
                with contextlib.suppress(sqlite3.Error):
                    self._cx.execute("PRAGMA journal_mode=WAL")
            elif "cache=shared" in db_path:
                # Shared cache (used in tests) requires read_uncommitted to prevent
                # readers from blocking writers (Table Locking).
                with contextlib.suppress(sqlite3.Error):
                    self._cx.execute("PRAGMA read_uncommitted = true")

        _setup_db_adapters()  # DTM adapter/converter

        if self.maintain:
            self._lock = asyncio.Lock()
            self._last_housekeeping: dt = cast(dt, None)
            self._housekeeping_task = cast(asyncio.Task[None], None)

        self.start()

    def __repr__(self) -> str:
        return f"MessageStore({len(self._message_log)} messages)"

    def start(self) -> None:
        """Start the housekeeper loop."""

        if self.maintain:
            if getattr(self, "_housekeeping_task", None) and (
                not self._housekeeping_task.done()
            ):
                return

            self._hydration_task = asyncio.create_task(
                self._hydrate_ram(), name=f"{self.__class__.__name__}.hydrator"
            )

            self._housekeeping_task = asyncio.create_task(
                self._housekeeping_loop(), name=f"{self.__class__.__name__}.housekeeper"
            )

    def stop(self) -> None:
        """Stop the housekeeper loop and resources."""

        c_task = getattr(self, "_consumer_task", None)
        if c_task and not c_task.done():
            c_task.cancel()  # stop the SSOT queue consumer

        h_task = getattr(self, "_housekeeping_task", None)
        if self.maintain and h_task and not h_task.done():
            h_task.cancel()  # stop the housekeeper

        hy_task = getattr(self, "_hydration_task", None)
        if hy_task and not hy_task.done():
            hy_task.cancel()

        if self._worker:
            # Trigger a final snapshot to ensure no data is lost on shutdown
            self._worker.submit_snapshot()
            self._worker.flush(timeout=5.0)
            is_stopped = self._worker.stop(timeout=5.0)  # Stop the background thread
            if not is_stopped:
                _LOGGER.warning("MessageStore: SQLiteWorker shutdown timed out.")

        cx = getattr(self, "_cx", None)
        if cx is not None:
            try:
                cx.commit()
                cx.close()
            except sqlite3.ProgrammingError:
                pass  # Connection might already be closed

    @property
    def log_by_dtm(self) -> tuple[Message, ...]:
        """Return the messages in the index safely encapsulated as a tuple."""
        return tuple(self._message_log.values())

    @property
    def state_cache(self) -> dict[StateHeader, Message]:
        """Return the latest messages in the index natively keyed by StateHeader."""
        return self._state_cache

    def flush(self) -> None:
        """Flush the storage worker queue.

        This is primarily for testing to ensure data persistence before querying.
        """
        if self._worker:
            self._worker.flush()

    def start_consumer(self, in_queue: asyncio.Queue[CoreMessage]) -> None:
        """Start the asynchronous queue consumer task for SSOT ingestion.

        :param in_queue: The SSOT event bus queue from the Central Dispatcher.
        :type in_queue: asyncio.Queue[CoreMessage]
        """
        c_task = getattr(self, "_consumer_task", None)
        if c_task and not c_task.done():
            return
        self._consumer_task = asyncio.create_task(
            self._consume_loop(in_queue), name=f"{self.__class__.__name__}.consumer"
        )

    async def _consume_loop(self, in_queue: asyncio.Queue[CoreMessage]) -> None:
        """Continuously ingest messages from the event bus queue.

        :param in_queue: The event bus queue.
        :type in_queue: asyncio.Queue[CoreMessage]
        """
        while True:
            core_msg = await in_queue.get()
            try:
                # Strangler Bridge: Pass the raw packet back to the legacy SSOT
                if core_msg.packets:
                    try:
                        legacy_msg = Message(core_msg.packets[0])
                        # Unwrap _array to preserve legacy List[Dict] compatibility
                        if "_array" in core_msg.data:
                            legacy_msg._payload = core_msg.data["_array"]
                            legacy_msg._force_has_array()
                        else:
                            legacy_msg._payload = core_msg.data
                        self.add(legacy_msg)
                    except Exception as err:
                        _LOGGER.debug(
                            "MessageStore ignored legacy malformed packet: %s", err
                        )
            except Exception as err:
                _LOGGER.error("MessageStore consumer failed to add message: %s", err)
            finally:
                in_queue.task_done()

    async def _hydrate_ram(self) -> None:
        """Hydrate RAM cache from the in-memory database.

        This routine runs as a non-blocking background task.
        """
        cx = getattr(self, "_cx", None)
        if cx is None:
            return

        def _fetch_all(conn: sqlite3.Connection) -> list[Any]:
            return conn.execute("SELECT * FROM messages ORDER BY dtm ASC").fetchall()

        try:
            rows = await asyncio.to_thread(_fetch_all, cx)
        except sqlite3.Error as err:
            _LOGGER.error("Failed to fetch messages for hydration: %s", err)
            return

        has_lock = getattr(self, "_lock", None)
        if has_lock:
            await self._lock.acquire()

        try:
            for row in rows:
                dtm_val = row[0]
                verb = row[1]
                src = row[2]
                dst = row[3]
                code = row[4]
                hdr = row[6]
                payload_blob = row[8]

                # Extract frame if available, else generate fallback dummy string
                frame = (
                    row[9]
                    if len(row) > 9 and row[9]
                    else f"{verb} --- {src} {dst} --:------ {code} 001 00"
                )
                dtm_str = cast(DtmStrT, dtm_val.isoformat(timespec="microseconds"))

                # Reconstruct exactly as received. RSSI is stripped natively,
                # so we pad with `... ` to satisfy Packet logic.
                pkt_line = f"... {frame}"
                try:
                    pkt = Packet(dtm_val, pkt_line)
                    msg = Message._from_pkt(pkt)
                    msg._payload = orjson.loads(payload_blob)

                    self._message_log[dtm_str] = msg
                    self._state_cache[msg.state_header] = msg
                except Exception as err:
                    _LOGGER.debug("Failed to reconstruct message for %s: %s", hdr, err)
        finally:
            if has_lock:
                self._lock.release()

        _LOGGER.info("Hydrated %d messages into RAM cache", len(rows))

    async def _housekeeping_loop(self) -> None:
        """Periodically remove stale messages from the index,
        unless `self.maintain` is False - as in (most) tests."""

        async def housekeeping(dt_now: dt, _cutoff: td = td(days=1)) -> None:
            """
            Deletes all messages older than a given delta from the dict
            using the MessageStore.
            :param dt_now: current timestamp
            :param _cutoff: the oldest timestamp to retain, default is 24
                hours ago
            """
            dtm = dt_now - _cutoff

            # Submit prune request to worker (Non-blocking I/O)
            if self._worker:
                self._worker.submit_prune(dtm)

            # Prune in-memory cache synchronously (Fast CPU-bound op)
            dtm_iso = dtm.isoformat(timespec="microseconds")

            # make this operation atomic, i.e. update self._message_log only on success
            try:
                await self._lock.acquire()
                # Rebuild dict keeping only newer items
                self._message_log = OrderedDict(
                    (k, v) for k, v in self._message_log.items() if k >= dtm_iso
                )

                valid_dtms = set(self._message_log.keys())
                self._state_cache = {
                    m.state_header: m
                    for m in self._state_cache.values()
                    if cast(DtmStrT, m.dtm.isoformat(timespec="microseconds"))
                    in valid_dtms
                }

            except Exception as err:
                _LOGGER.warning("MessageStore housekeeping error: %s", err)
            else:
                _LOGGER.debug(
                    "MessageStore housekeeping completed, retained messages >= %s",
                    dtm,
                )
            finally:
                self._lock.release()

        while True:
            self._last_housekeeping = dt.now()
            await asyncio.sleep(900)
            _LOGGER.info("Starting next MessageStore housekeeping")
            await housekeeping(self._last_housekeeping)

            if self._worker:
                self._worker.submit_snapshot()

    def add(self, msg: Message) -> Message | None:
        """
        Add a single message to the MessageStore.
        Logs a warning if there is a duplicate dtm.

        :returns: any message that was removed because it had the same header
        """
        dup: tuple[Message, ...] = ()
        old: Message | None = None

        # Check in-memory cache for collision instead of blocking SQL
        dtm_str = cast(DtmStrT, msg.dtm.isoformat(timespec="microseconds"))
        if dtm_str in self._message_log:
            dup = (self._message_log[dtm_str],)

        try:
            self._insert_into(msg)
        except sqlite3.Error:
            pass
        else:
            self._message_log[dtm_str] = msg
            self._state_cache[msg.state_header] = msg
        finally:
            pass

        if (
            dup
            and (msg.src is not msg.dst)
            and not msg.dst.id.startswith("18:")  # HGI
            and msg.verb != RQ  # these may come very quickly
        ):  # when src==dst, expect to add duplicate, don't warn
            _LOGGER.debug(
                "Overwrote dtm (%s) for %s: %s (contrived log?)",
                msg.dtm,
                msg.state_header.legacy_hdr,
                dup[0]._pkt,
            )

        return old

    def add_record(
        self, src: str, code: str = "", verb: str = "", payload: str = "00"
    ) -> None:
        """
        Add a single record to the MessageStore with timestamp `now()`
        and no Message contents.

        :param src: device id to use as source address
        :param code: device id to use as destination address (can be
            identical)
        :param verb: two letter verb str to use
        :param payload: payload str to use
        """
        _now: dt = dt.now()
        dtm = cast(DtmStrT, _now.isoformat(timespec="microseconds"))
        hdr = f"{code}|{verb}|{src}|{payload}"

        if self._worker:
            data = PacketLogEntry(
                dtm=_now,
                verb=verb,
                src=src,
                dst=src,
                code=code,
                ctx=None,
                hdr=hdr,
                plk="|",
                payload_blob=orjson.dumps({"payload": payload}),
                frame=f"{verb} --- {src} {src} --:------ {code} 001 {payload}",
            )
            self._worker.submit_packet(data)

        msg: Message = Message._from_pkt(
            Packet(_now, f"... {verb} --- {src} --:------ {src} {code} 005 0000000000")
        )
        self._message_log[dtm] = msg
        self._state_cache[msg.state_header] = msg

    def _insert_into(self, msg: Message) -> Message | None:
        """
        Insert a message into the index.

        :returns: any message replaced (by same hdr)
        """
        if self._worker:
            try:
                payload_blob = orjson.dumps(msg.payload)
            except orjson.JSONEncodeError as err:
                _LOGGER.warning("Failed to serialize payload: %s", err)
                payload_blob = b"{}"

            data = PacketLogEntry(
                dtm=msg.dtm,
                verb=str(msg.verb),
                src=msg.src.id,
                dst=msg.dst.id,
                code=str(msg.code),
                ctx=msg.context.as_string,
                hdr=msg.state_header.legacy_hdr,
                plk=payload_keys(msg.payload),
                payload_blob=payload_blob,
                frame=getattr(msg._pkt, "_frame", ""),
            )

            self._worker.submit_packet(data)

        return None

    async def rem(
        self,
        msg: Message | None = None,
        *,
        dtm: dt | str | None = None,
        src: str | None = None,
        dst: str | None = None,
        verb: str | None = None,
        code: str | None = None,
        ctx: Any | None = None,
        hdr: str | StateHeader | None = None,
    ) -> tuple[Message, ...] | None:
        """Remove a set of message(s) from the index."""
        kwargs = {
            k: v
            for k, v in {
                "dtm": dtm,
                "src": src,
                "dst": dst,
                "verb": verb,
                "code": code,
                "ctx": ctx,
                "hdr": hdr,
            }.items()
            if v is not None
        }

        if not bool(msg) ^ bool(kwargs):
            raise DatabaseQueryError(
                "Either a Message or kwargs should be provided, not both"
            )

        msgs: tuple[Message, ...] | None = None
        try:
            # Safely unpack explicitly for strict typing
            msgs_to_remove = await self.get(
                msg=msg,
                dtm=dtm,
                src=src,
                dst=dst,
                verb=verb,
                code=code,
                ctx=ctx,
                hdr=hdr,
            )

            if self._worker:
                if msg:
                    kwargs["dtm"] = msg.dtm

                sql = "DELETE FROM messages WHERE "
                sql += " AND ".join(f"{k} = ?" for k in kwargs)

                sql_params = tuple(kwargs.values())

                self._worker.submit_delete_message(sql, sql_params)

            msgs = tuple(msgs_to_remove)

        except sqlite3.Error as err:
            cx = getattr(self, "_cx", None)
            if cx is not None:
                await asyncio.to_thread(cx.rollback)
            raise DatabaseQueryError(f"Delete failed: {err}") from err

        else:
            if msgs is not None:
                for m in msgs:
                    dtm_val = cast(DtmStrT, m.dtm.isoformat(timespec="microseconds"))
                    self._message_log.pop(dtm_val, None)
                    self._state_cache.pop(m.state_header, None)
        return msgs

    async def get(
        self,
        msg: Message | None = None,
        *,
        dtm: dt | str | None = None,
        src: str | None = None,
        dst: str | None = None,
        verb: str | None = None,
        code: str | None = None,
        ctx: Any | None = None,
        hdr: str | StateHeader | None = None,
    ) -> tuple[Message, ...]:
        """Public method to get a set of message(s) from the index."""
        kwargs = {
            k: v
            for k, v in {
                "dtm": dtm,
                "src": src,
                "dst": dst,
                "verb": verb,
                "code": code,
                "ctx": ctx,
                "hdr": hdr,
            }.items()
            if v is not None
        }

        if not (bool(msg) ^ bool(kwargs)):
            raise DatabaseQueryError(
                "Either a Message or kwargs should be provided, not both"
            )

        if msg:
            kwargs["dtm"] = msg.dtm

        if "ctx" in kwargs:
            c_val = kwargs["ctx"]
            if isinstance(c_val, str):
                kwargs["ctx"] = c_val
            elif c_val:
                kwargs["ctx"] = "True"
            else:
                kwargs["ctx"] = "False"

        res: list[Message] = []
        for m in self.log_by_dtm:
            match = True
            for k, v in kwargs.items():
                if k == "dtm" and m.dtm != v:
                    match = False
                    break
                elif k == "verb" and str(m.verb) != v:
                    match = False
                    break
                elif k == "src" and m.src.id != v:
                    match = False
                    break
                elif k == "dst" and m.dst.id != v:
                    match = False
                    break
                elif k == "code" and str(m.code) != v:
                    match = False
                    break
                elif k == "hdr":
                    if isinstance(v, StateHeader):
                        if m.state_header != v:
                            match = False
                            break
                    else:
                        if m.state_header.legacy_hdr != v:
                            match = False
                            break
                elif k == "ctx":
                    if m.context.as_string != str(v):
                        match = False
                        break
            if match:
                res.append(m)

        return tuple(res)

    async def contains(
        self,
        *,
        dtm: dt | str | None = None,
        src: str | None = None,
        dst: str | None = None,
        verb: str | None = None,
        code: str | None = None,
        ctx: Any | None = None,
        hdr: str | StateHeader | None = None,
    ) -> bool:
        """Check if the MessageStore contains at least 1 record that
        matches the provided fields."""
        return (
            len(
                await self.get(
                    dtm=dtm, src=src, dst=dst, verb=verb, code=code, ctx=ctx, hdr=hdr
                )
            )
            > 0
        )

    async def get_rp_codes(self, parameters: tuple[str, ...]) -> list[Code]:
        """Get a list of Codes from the index, given parameters."""
        src_id = parameters[0]
        dst_id = parameters[1] if len(parameters) > 1 else None

        codes = set()
        for m in self._state_cache.values():
            if m.verb == RP and (m.src.id == src_id or m.dst.id == dst_id):
                codes.add(m.code)

        def get_code(c: str) -> Code:
            for Cd in CODES_SCHEMA:
                if c == Cd:
                    return Cd
            return Code(c)

        return [get_code(str(c)) for c in codes]

    async def qry(self, sql: str, parameters: tuple[str, ...]) -> tuple[Message, ...]:
        """Deprecated: Returns empty for legacy callers."""
        _LOGGER.warning(
            "Legacy qry (SQL) called. Returning empty in CQRS architecture."
        )
        return ()

    async def qry_field(
        self, sql: str, parameters: tuple[str, ...]
    ) -> list[tuple[dt | str, str]]:
        """Deprecated: Returns empty for legacy callers."""
        _LOGGER.warning(
            "Legacy qry_field (SQL) called. Returning empty in CQRS architecture."
        )
        return []

    async def all(self, include_expired: bool = False) -> tuple[Message, ...]:
        """Get all messages from the index."""
        return self.log_by_dtm

    async def clr(self) -> None:
        """Clear the message index (remove indexes of all messages)."""
        if self._worker:
            self._worker.submit_delete_message("DELETE FROM messages", ())

        self._message_log.clear()
        self._state_cache.clear()

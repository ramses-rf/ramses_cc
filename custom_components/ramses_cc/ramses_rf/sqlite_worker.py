# src/ramses_rf/sqlite_worker.py
"""RAMSES RF - Background storage worker for async I/O."""

from __future__ import annotations

import contextlib
import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, NamedTuple

_LOGGER = logging.getLogger(__name__)


class PacketLogEntry(NamedTuple):
    """Represents a packet to be written to the database."""

    dtm: Any
    verb: str
    src: str
    dst: str
    code: str
    ctx: str | None
    hdr: str
    plk: str
    payload_blob: bytes
    frame: str


class PruneRequest(NamedTuple):
    """Represents a request to prune old records."""

    dtm_limit: Any


class SnapshotRequest(NamedTuple):
    """Represents a request to snapshot the in-memory DB to disk."""

    pass


class DeleteMessageRequest(NamedTuple):
    """Represents a request to delete a specific message or set of messages."""

    query: str
    params: tuple[Any, ...]


class FlushRequest(NamedTuple):
    """Represents a request to flush the queue and signal completion."""

    event: threading.Event


QueueItem = (
    PacketLogEntry
    | PruneRequest
    | SnapshotRequest
    | DeleteMessageRequest
    | FlushRequest
    | None
)


class SQLiteWorker:
    """A background worker thread to handle blocking storage I/O
    asynchronously."""

    def __init__(self, db_path: str = ":memory:", disk_path: str | None = None) -> None:
        """Initialize the storage worker thread."""
        self._db_path = db_path
        self._disk_path = disk_path
        self._queue: queue.SimpleQueue[QueueItem] = queue.SimpleQueue()
        self._ready_event = threading.Event()

        # Allows process exit even if stop() is missed
        self._thread = threading.Thread(
            target=self._run,
            name="RamsesStorage",
            daemon=True,
        )
        self._thread.start()

    def wait_for_ready(self, timeout: float | None = None) -> bool:
        """Wait until the database is initialized and ready."""
        return self._ready_event.wait(timeout)

    def submit_packet(self, packet: PacketLogEntry) -> None:
        """Submit a packet tuple for SQL insertion (Non-blocking)."""
        self._queue.put(packet)

    def submit_prune(self, dtm_limit: Any) -> None:
        """Submit a prune request for SQL deletion (Non-blocking)."""
        self._queue.put(PruneRequest(dtm_limit))

    def submit_snapshot(self) -> None:
        """Submit a disk snapshot request (Non-blocking)."""
        self._queue.put(SnapshotRequest())

    def submit_delete_message(self, query: str, params: tuple[Any, ...]) -> None:
        """Submit a request to delete specific messages (Non-blocking)."""
        self._queue.put(DeleteMessageRequest(query, params))

    def flush(self, timeout: float = 10.0) -> None:
        """Block until all currently pending tasks are processed."""
        # We inject a special marker into the queue
        sentinel = threading.Event()
        self._queue.put(FlushRequest(sentinel))

        # Wait for the worker to set the sentinel
        if not sentinel.wait(timeout):
            _LOGGER.warning("SQLiteWorker flush timed out")

    def stop(self, timeout: float = 3.0) -> bool:
        """
        Signal the worker to stop processing and close resources safely.

        :returns: True if the thread exited cleanly, False if it timed out.
        """
        self._queue.put(None)  # Poison pill
        # Give the worker a chance to wrap up gracefully
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            _LOGGER.warning("SQLiteWorker thread did not cleanly exit within timeout.")
            return False
        return True

    def _init_db(self, conn: sqlite3.Connection) -> None:
        """Initialize the database schema."""
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                dtm    DTM      NOT NULL PRIMARY KEY,
                verb   TEXT(2)  NOT NULL,
                src    TEXT(12) NOT NULL,
                dst    TEXT(12) NOT NULL,
                code   TEXT(4)  NOT NULL,
                ctx    TEXT,
                hdr    TEXT     NOT NULL UNIQUE,
                plk    TEXT     NOT NULL,
                payload_blob BLOB NOT NULL,
                frame  TEXT     NOT NULL
            )
            """
        )
        # Handle migration for users with the old schema (Phase 2.1 upgrade)
        with contextlib.suppress(sqlite3.OperationalError):
            cursor.execute("ALTER TABLE messages ADD COLUMN frame TEXT DEFAULT ''")

        # Create indexes to speed up future reads
        for col in ("verb", "src", "dst", "code", "ctx", "hdr"):
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{col} ON messages ({col})")
        conn.commit()

    def _run(self) -> None:
        """The main loop running in the background thread."""
        _LOGGER.debug("SQLiteWorker thread started.")

        # Setup SQLite connection in this thread
        try:
            conn = sqlite3.connect(
                self._db_path,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=False,
                uri=True,
                timeout=10.0,
            )

            # Enable Write-Ahead Logging for concurrency
            if self._db_path != ":memory:" and "mode=memory" not in self._db_path:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
            elif "cache=shared" in self._db_path:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA read_uncommitted = true")

            # Phase 2.4: Startup Hydration
            if self._disk_path and (
                self._db_path == ":memory:" or "mode=memory" in self._db_path
            ):
                disk_path_obj = Path(self._disk_path)
                if disk_path_obj.exists():
                    try:
                        disk_conn = sqlite3.connect(self._disk_path)
                        disk_conn.backup(conn)
                        disk_conn.close()
                        _LOGGER.info(
                            "Hydrated memory DB from disk: %s", self._disk_path
                        )
                    except sqlite3.Error as err:
                        _LOGGER.error("Failed to hydrate from disk: %s", err)

            self._init_db(conn)
            self._ready_event.set()  # Signal that tables exist
        except sqlite3.Error as err:
            _LOGGER.error("Failed to initialize storage database: %s", err)
            self._ready_event.set()  # Avoid blocking waiters forever
            return

        while True:
            try:
                # Block here waiting for work
                item = self._queue.get()

                if item is None:  # Shutdown signal
                    break

                if isinstance(item, PacketLogEntry):
                    # Optimization: Batch processing
                    batch = [item]
                    # Drain queue of pending SQL tasks to bulk insert
                    while not self._queue.empty():
                        try:
                            # Peek/get next item without blocking
                            next_item = self._queue.get_nowait()
                            if next_item is None:
                                self._queue.put(None)  # Re-queue poison pill
                                break

                            if isinstance(next_item, PacketLogEntry):
                                batch.append(next_item)
                            else:
                                # Handle other types after this batch
                                self._queue.put(next_item)  # Re-queue
                                break
                        except queue.Empty:
                            break

                    try:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO messages
                            (dtm, verb, src, dst, code, ctx, hdr, plk,
                            payload_blob, frame)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            batch,
                        )
                        conn.commit()
                    except sqlite3.Error as err:
                        _LOGGER.error("SQL Write Failed: %s", err)

                elif isinstance(item, PruneRequest):
                    try:
                        conn.execute(
                            "DELETE FROM messages WHERE dtm < ?", (item.dtm_limit,)
                        )
                        conn.commit()
                        _LOGGER.debug("Pruned records older than %s", item.dtm_limit)
                    except sqlite3.Error as err:
                        _LOGGER.error("SQL Prune Failed: %s", err)

                elif isinstance(item, DeleteMessageRequest):
                    try:
                        conn.execute(item.query, item.params)
                        conn.commit()
                        _LOGGER.debug("Deleted specific message via queue.")
                    except sqlite3.Error as err:
                        _LOGGER.error("SQL Delete Message Failed: %s", err)

                elif isinstance(item, SnapshotRequest):
                    if self._disk_path:
                        try:
                            disk_conn = sqlite3.connect(self._disk_path)
                            conn.backup(disk_conn)
                            disk_conn.close()
                            _LOGGER.debug("Snapshot written to %s", self._disk_path)
                        except sqlite3.Error as err:
                            _LOGGER.error("SQL Snapshot Failed: %s", err)

                elif isinstance(item, FlushRequest):
                    # Flush requested
                    item.event.set()

            except Exception as err:
                _LOGGER.exception("SQLiteWorker encountered an error: %s", err)

        # Cleanup
        with contextlib.suppress(sqlite3.ProgrammingError):
            conn.close()
        _LOGGER.debug("SQLiteWorker thread stopped.")

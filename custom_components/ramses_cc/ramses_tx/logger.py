#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

This module wraps logger to provide bespoke functionality, especially for timestamps.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from datetime import datetime as dt
from logging.handlers import (
    MemoryHandler,
    QueueHandler,
    QueueListener,
    TimedRotatingFileHandler as _TimedRotatingFileHandler,
)
from queue import Queue
from typing import Any

from .version import VERSION

DEV_MODE = False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)

try:
    import colorlog
except ModuleNotFoundError:
    _use_color_ = False
    _LOGGER.warning("Consider installing the colorlog library for colored logging")
else:
    _use_color_ = True
    # logging.basicConfig(format=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT)  # Causes issues

DEFAULT_FMT = "%(asctime)s.%(msecs)03d %(message)s"
DEFAULT_DATEFMT = "%H:%M:%S"

# TODO: make account for the non-printing characters
CONSOLE_COLS = int(shutil.get_terminal_size(fallback=(int(2e3), 24)).columns - 1)

if DEV_MODE:  # Do this to have longer-format console messages
    # HH:MM:SS.sss vs YYYY-MM-DDTHH:MM:SS.ssssss, shorter format for the console
    CONSOLE_FMT = f"%(asctime)s%(frame).{CONSOLE_COLS - 27}s"
else:
    CONSOLE_FMT = f"%(asctime)s%(frame).{CONSOLE_COLS - 13}s"

PKT_LOG_FMT = "%(asctime)s%(frame)s"

BANDW_SUFFIX = "%(message)s%(error_text)s%(comment)s"
COLOR_SUFFIX = "%(yellow)s%(message)s%(red)s%(error_text)s%(cyan)s%(comment)s"

# How to strip ASCII colour from a text file:
#   sed -r "s/\x1B\[(([0-9]{1,2})?(;)?([0-9]{1,2})?)?[m,K,H,f,J]//g" file_name

LOG_COLOURS = {
    "DEBUG": "white",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold_red",
    "CRITICAL": "bold_red",
}  # default_log_colors


class _Logger(logging.Logger):  # use pkt.dtm for the log record timestamp
    """Logger instances represent a single logging channel."""

    def makeRecord(
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Mapping[str, object] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        """Create a specialized LogRecord with a bespoke timestamp.

        Will overwrite created and msecs (and thus asctime), but not relativeCreated.
        """

        extra = dict(extra or {})  # work with a copy
        extra["frame"] = extra.pop("_frame", "")
        if extra["frame"]:
            extra["frame"] = f" {extra['_rssi']} {extra['frame']}"

        rv = super().makeRecord(
            name, level, fn, lno, msg, args, exc_info, func, extra, sinfo
        )

        if hasattr(rv, "dtm"):  # if dtm := extra.get("dtm"):
            ct = rv.dtm.timestamp()
            rv.created = ct
            rv.msecs = (ct - int(ct)) * 1000

        if rv.msg:
            rv.msg = f" < {rv.msg}"

        if value := getattr(rv, "error_text", None):  # HACK
            rv.error_text = f" * {value}"

        if value := getattr(rv, "comment", None):  # HACK
            rv.comment = f" # {value}"

        return rv


class _Formatter:  # format asctime with configurable precision
    """Formatter instances convert a LogRecord to text."""

    converter = None  # was: time.localtime
    default_time_format = "%Y-%m-%dT%H:%M:%S.%f"
    precision = 6

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Return the creation time (asctime) of the LogRecord as formatted text.

        Allows for sub-millisecond precision, using datetime instead of time objects.
        """
        result = dt.fromtimestamp(record.created).strftime(
            datefmt or self.default_time_format
        )
        if "f" not in self.default_time_format:
            return result
        precision = self.precision or -1
        return result[: precision - 6] if -1 <= precision < 6 else result


class ColoredFormatter(_Formatter, colorlog.ColoredFormatter):  # type: ignore[misc]
    pass


class Formatter(_Formatter, logging.Formatter):  # type: ignore[misc]
    pass


class PktLogFilter(logging.Filter):  # record.levelno in (logging.INFO, logging.WARNING)
    """For packet log files, process only wanted packets."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if the record is to be processed."""
        # if record._frame[4:] or record.comment or record.error_text:
        return record.levelno in (logging.INFO, logging.WARNING)


class StdErrFilter(logging.Filter):  # record.levelno >= logging.WARNING
    """For sys.stderr, process only wanted packets."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if the record is to be processed."""  # WARNING-30, ERROR-40
        return record.levelno >= logging.WARNING


class StdOutFilter(logging.Filter):  # record.levelno < logging.WARNING
    """For sys.stdout, process only wanted packets."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if the record is to be processed."""  # INFO-20, DEBUG-10
        return record.levelno < logging.WARNING


class BlockMqttFilter(logging.Filter):
    """Block mqtt and Sent traffic"""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if the record is to be processed."""
        return not (  # ignored if Config lag_all is True
            record.getMessage().startswith("mq Rx: ")
            or record.getMessage().startswith("Sent: ")
        )


class TimedRotatingFileHandler(_TimedRotatingFileHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        assert self.when == "MIDNIGHT"
        self.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)


def getLogger(  # permits a bespoke Logger class
    name: str | None = None, pkt_log: bool = False
) -> logging.Logger:
    """Return a logger with the specified name, creating it if necessary.

    Used to set record timestamps to its packet timestamp instead of the current time.
    """
    if name is None or not pkt_log:
        return logging.getLogger(name)

    # Acquire lock, so no-one else uses our Logger class
    try:  # TODO: remove this ASAP
        logging._acquireLock()  # type: ignore[attr-defined]
    except AttributeError:  # Python 3.13+
        logging._lock.acquire()  # type: ignore[attr-defined]

    klass = logging.getLoggerClass()
    logging.setLoggerClass(_Logger)

    logger = logging.getLogger(name)

    logging.setLoggerClass(klass)

    try:  # TODO: remove this ASAP
        logging._releaseLock()  # type: ignore[attr-defined]
    except AttributeError:  # Python 3.13+
        logging._lock.release()  # type: ignore[attr-defined]

    return logger


def set_logger_timesource(dtm_now: Callable[..., dt]) -> None:
    """Set a custom record factory, with a bespoke source of timestamps.

    Used to have records with the same datetime as the most recent packet log record.
    """

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)

        ct = dtm_now().timestamp()
        record.created = ct
        record.msecs = (ct - int(ct)) * 1000

        return record

    old_factory = logging.getLogRecordFactory()

    logging.setLogRecordFactory(record_factory)


def set_pkt_logging(
    logger: logging.Logger,
    cc_console: bool = False,
    packet_log_path: str | None = "",
    packet_log_prefix: str | None = "packet_log",
    packet_log_retention_days: int = 7,
    rotate_bytes: int | None = None,
    buffer_capacity: int = 0,
    flush_level: int = logging.ERROR,
    flush_interval: float = 60.0,
) -> QueueListener | None:
    """Create/configure handlers, formatters, etc.

    Parameters:
    - logger:         The logger to configure.
    - cc_console:     If True, output to stdout/stderr.
    - packet_log_path: base path to store packet logs in, from root
    - packet_log_prefix: prefix of file to store packet logs in
    - packet_log_retention_days: keep copies, rotate at midnight unless:
    - rotate_bytes: rotate log files when log > rotate_size
    - buffer_capacity: If > 0, buffer logs in memory up to this capacity.
    - flush_level: The logging level that triggers the buffer to flush.
    - flush_interval: Accepted here to satisfy kwargs unpacked
      from config (unused locally).
    """

    logger.propagate = False  # log file is distinct from any app/debug logging
    logger.setLevel(logging.DEBUG)  # must be at least .INFO

    # as set_pkt_logging() may be called several times: to avoid duplicates in logs...
    for handler in logger.handlers:  # dont use logger.hasHandlers() as not propagating
        logger.removeHandler(handler)

    handlers: list[logging.Handler] = []

    file_name: str | None = None
    if packet_log_prefix:
        if packet_log_path:
            os.makedirs(packet_log_path, exist_ok=True)
            file_name = os.path.join(packet_log_path, f"{packet_log_prefix}.log")
        else:
            file_name = f"{packet_log_prefix}.log"

    if file_name:  # note: this opens the packet_log file IO and may block
        file_handler: logging.Handler

        if rotate_bytes:
            packet_log_retention_days = packet_log_retention_days or 2
            file_handler = logging.handlers.RotatingFileHandler(
                file_name,
                maxBytes=rotate_bytes,
                backupCount=packet_log_retention_days,
            )
        elif packet_log_retention_days:
            file_handler = TimedRotatingFileHandler(
                file_name,
                when="MIDNIGHT",
                backupCount=packet_log_retention_days,
            )
        else:
            file_handler = logging.FileHandler(file_name)

        logfile_fmt = Formatter(fmt=PKT_LOG_FMT + BANDW_SUFFIX)

        file_handler.setFormatter(logfile_fmt)
        file_handler.setLevel(logging.INFO)  # .INFO (usually), or .DEBUG
        file_handler.addFilter(PktLogFilter())  # record.levelno in (.INFO, .WARNING)

        if buffer_capacity > 0:
            mem_handler = MemoryHandler(
                capacity=buffer_capacity,
                flushLevel=flush_level,
                target=file_handler,
            )
            mem_handler.addFilter(PktLogFilter())
            handlers.append(mem_handler)
        else:
            handlers.append(file_handler)

    elif cc_console:
        pass

    elif not cc_console:
        logger.setLevel(logging.CRITICAL)
        return None

    if cc_console:  # CC: output to stdout/stderr
        console_fmt: ColoredFormatter | Formatter
        if _use_color_:
            console_fmt = ColoredFormatter(
                fmt=f"%(log_color)s{CONSOLE_FMT + COLOR_SUFFIX}",
                reset=True,
                log_colors=LOG_COLOURS,
            )
        else:
            console_fmt = Formatter(fmt=CONSOLE_FMT + BANDW_SUFFIX)

        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(console_fmt)
        handler.setLevel(logging.WARNING)  # must be .WARNING or less
        handler.addFilter(StdErrFilter())  # record.levelno >= .WARNING
        handlers.append(handler)

        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(console_fmt)
        handler.setLevel(logging.DEBUG)  # must be .INFO or less
        handler.addFilter(StdOutFilter())  # record.levelno < .WARNING
        handlers.append(handler)

    # Use QueueHandler to decouple logging I/O from the main loop (see Issue #397)
    if handlers:
        log_queue: Queue[Any] = Queue(-1)
        listener = QueueListener(log_queue, *handlers, respect_handler_level=True)
        queue_handler = QueueHandler(log_queue)
        logger.addHandler(queue_handler)
    else:
        return None

    extras = {
        "_frame": "",
        "error_text": "",
        "comment": f"ramses_tx {VERSION}",
    }
    logger.warning("", extra=extras)  # initial log line

    return listener


def flush_packet_log(listener: QueueListener | None) -> None:
    """Manually flush any MemoryHandler associated with the packet log listener.

    :param listener: The QueueListener managing the log handlers.
    """
    if listener is None:
        return
    for handler in listener.handlers:
        if isinstance(handler, MemoryHandler):
            handler.flush()

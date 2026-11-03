"""Diagnostics support for the ramses_cc integration.

Provides a redacted, JSON-serialisable snapshot of the integration
state for bug triage (ramses-rf/ramses_cc issue 1214): the config
entry, ramses_rf/ramses_tx versions, entity/device counts, gateway
schema/status/config, transport and pool state, discovery metadata,
ramses WARNING/ERROR blocks (issue 1374) and bounded head+tail
windows of the Home Assistant log (``ramses_*`` entries only) and
the packet log — a single file a user can attach to a bug report.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote, urlparse

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from ramses_rf import VERSION as RAMSES_RF_VERSION
from ramses_tx import VERSION as RAMSES_TX_VERSION
from ramses_tx.const import SZ_ACTIVE_HGI
from ramses_tx.transport.helpers import redact_url

from .const import (
    CONF_ADDITIONAL_PORTS,
    DOMAIN,
    SZ_PACKET_LOG,
    SZ_PACKET_LOG_PATH,
    SZ_PACKET_LOG_PREFIX,
    SZ_PORT_NAME,
    SZ_SERIAL_PORT,
)
from .helpers import engine_transport

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)

# Credential-type keys blanked wholesale by async_redact_data.  Port
# names and URLs are handled by _redact_config()/_redact_line() so the
# shape of the config stays visible for triage.
TO_REDACT: Final[set[str]] = {
    "access_token",
    "api_key",
    "password",
    "passwd",
    "refresh_token",
    "secret",
    "token",
    "username",
}
REDACTED: Final[str] = "**REDACTED**"

# Fallback credential mask for URLs urlparse() cannot handle; matches
# the userinfo part of ``scheme://user[:pass]@host``.
_URL_CRED_RE: Final[re.Pattern[str]] = re.compile(r"://[^/\s]*@")

# Minimum length for bare credential strings extracted from
# URLs/config; shorter strings risk matching harmless log text.
_MIN_SECRET_LEN: Final[int] = 3

# Bounded head/tail windows keep the download small enough to attach
# to an issue: the head shows startup, the tail shows recent state.
_LOG_HEAD_BYTES: Final[int] = 256 * 1024
_LOG_TAIL_BYTES: Final[int] = 256 * 1024
_LOG_HEAD_LINES: Final[int] = 400
_LOG_TAIL_LINES: Final[int] = 200

# Logger name fragment covering ramses_cc, ramses_rf and ramses_tx.
_LOG_FILTER: Final[str] = "ramses"

# A HA log record starts with a "YYYY-MM-DD HH:MM:SS" timestamp;
# lines without one (tracebacks, multi-line messages) are
# continuations of the preceding record.
_LOG_RECORD_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
)

# WARNING/ERROR/CRITICAL lines are collected with context, but only
# from ramses log records — users attach this file to public issues
# and lines from other loggers can leak hosts/account names (issue
# 1374).  Non-ramses matches are only tallied in ``other_matches``.
# Each match keeps -10/+20 lines; windows merge; output is capped at
# the most recent _ERR_MAX_BLOCKS blocks.
_ERR_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:WARNING|ERROR|CRITICAL)\b"
)
_ERR_CTX_BEFORE: Final[int] = 10
_ERR_CTX_AFTER: Final[int] = 20
_ERR_MAX_BLOCKS: Final[int] = 12


def _redact_port(value: Any) -> Any:
    """Mask a port name: URLs keep their scheme, paths are removed.

    A ``/dev/serial/by-id/`` name can contain the gateway's serial/MAC,
    so plain paths are redacted entirely; URLs keep the scheme and host
    with credentials masked by ``redact_url``.
    """
    if not isinstance(value, str) or not value:
        return value
    if "://" in value:
        redacted = redact_url(value)
        # if urlparse() failed inside redact_url, mask the userinfo
        # part ourselves rather than leaking the credentials
        return _URL_CRED_RE.sub("://***:***@", redacted)
    return REDACTED


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive values in entry data/options.

    :param config: The entry data or options mapping.
    :type config: dict[str, Any]
    :return: A redacted copy.
    :rtype: dict[str, Any]
    """
    redacted = async_redact_data(dict(config), TO_REDACT)

    ser_port = redacted.get(SZ_SERIAL_PORT)
    if isinstance(ser_port, dict):
        ser_port[SZ_PORT_NAME] = _redact_port(ser_port.get(SZ_PORT_NAME))
    elif isinstance(ser_port, str):
        redacted[SZ_SERIAL_PORT] = _redact_port(ser_port)

    additional = redacted.get(CONF_ADDITIONAL_PORTS)
    if isinstance(additional, list):
        redacted[CONF_ADDITIONAL_PORTS] = [
            _redact_port(port) for port in additional
        ]

    # a custom log dir can reveal the host layout (e.g. /home/<user>)
    packet_log = redacted.get(SZ_PACKET_LOG)
    if isinstance(packet_log, dict) and packet_log.get(SZ_PACKET_LOG_PATH):
        packet_log[SZ_PACKET_LOG_PATH] = REDACTED
    return redacted


def _credential_values(obj: Any, found: list[str]) -> None:
    """Collect values stored under TO_REDACT keys, recursively."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if (
                key in TO_REDACT
                and isinstance(value, str)
                and len(value) >= _MIN_SECRET_LEN
            ):
                found.append(value)
            else:
                _credential_values(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _credential_values(value, found)


def _sensitive_strings(config: dict[str, Any]) -> list[str]:
    """Collect raw secrets to scrub from included log lines.

    Covers the configured port strings (paths and URLs), the user/
    password extracted from any ``scheme://user:pass@host`` URLs,
    the configured packet log dir, and values stored under TO_REDACT
    keys anywhere in the config.
    """
    candidates: list[Any] = []
    ser_port = config.get(SZ_SERIAL_PORT)
    if isinstance(ser_port, dict):
        candidates.append(ser_port.get(SZ_PORT_NAME))
    elif isinstance(ser_port, str):
        candidates.append(ser_port)
    additional = config.get(CONF_ADDITIONAL_PORTS)
    if isinstance(additional, list):
        candidates.extend(additional)
    packet_log = config.get(SZ_PACKET_LOG)
    if isinstance(packet_log, dict):
        candidates.append(packet_log.get(SZ_PACKET_LOG_PATH))

    secrets: list[str] = []
    for value in candidates:
        if not isinstance(value, str) or not value:
            continue
        secrets.append(value)
        if "://" not in value or "@" not in value:
            continue
        try:
            parsed = urlparse(value)
        except ValueError:
            continue
        for cred in (parsed.username, parsed.password):
            if cred and len(cred) >= _MIN_SECRET_LEN:
                secrets.extend((cred, unquote(cred)))
    _credential_values(config, secrets)
    return secrets


def _redact_line(line: str, secrets: list[str]) -> str:
    """Mask URLs and configured secret strings in a log line."""
    line = _URL_CRED_RE.sub("://***:***@", redact_url(line))
    for secret in secrets:
        line = line.replace(secret, REDACTED)
    return line


def _deep_scrub(value: Any, secrets: list[str]) -> Any:
    """Recursively scrub secrets from every string in a structure."""
    if isinstance(value, str):
        return _redact_line(value, secrets)
    if isinstance(value, dict):
        return {key: _deep_scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_scrub(item, secrets) for item in value]
    return value


def _display_path(hass: HomeAssistant, path: Path) -> str:
    """Return a log file path relative to the config dir, else basename.

    Absolute paths can reveal the host layout (e.g. ``/home/<user>``).
    """
    try:
        return f"<config>/{path.relative_to(hass.config.config_dir)}"
    except ValueError:
        return path.name


def _packet_log_candidates(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[Path]:
    """Return the packet log path candidates, in priority order.

    ``packet_log_path`` is a folder (a pre-0.60 full file path ending in
    ``.log`` is also accepted); ``packet_log_prefix`` is the file
    prefix (default ``packet_log``).  Falls back to the HA config dir.
    The ``is_file()`` check runs inside the executor job, off the
    event loop (issue 1374).
    """
    packet_log = entry.options.get(SZ_PACKET_LOG)
    if not isinstance(packet_log, dict):
        packet_log = {}
    prefix = packet_log.get(SZ_PACKET_LOG_PREFIX)
    if not isinstance(prefix, str) or not prefix:
        prefix = "packet_log"

    candidates: list[Path] = []
    raw_path = packet_log.get(SZ_PACKET_LOG_PATH)
    if isinstance(raw_path, str) and raw_path.strip():
        base = Path(raw_path.strip())
        candidates.append(
            base if base.suffix == ".log" else base / f"{prefix}.log"
        )
    candidates.extend(
        (
            Path(hass.config.path(f"{prefix}.log")),
            Path(hass.config.path("ramses_rf_logs", f"{prefix}.log")),
        )
    )
    return candidates


def _read_log_window(path: Path) -> tuple[list[str], list[str], bool]:
    """Read the head and tail windows of a file as text lines.

    Reads the first ``_LOG_HEAD_BYTES`` and last ``_LOG_TAIL_BYTES``
    of the file, or the whole file when it fits in both windows.
    Partial lines at the window edges are dropped.

    :param path: The log file path.
    :type path: Path
    :return: (head_lines, tail_lines, truncated); truncated is True
        when lines between the windows were omitted.
    :rtype: tuple[list[str], list[str], bool]
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as file:
            if size <= _LOG_HEAD_BYTES + _LOG_TAIL_BYTES:
                lines = (
                    file.read().decode("utf-8", errors="replace").splitlines()
                )
                head = lines[:_LOG_HEAD_LINES]
                tail = lines[_LOG_HEAD_LINES:][-_LOG_TAIL_LINES:]
                omitted = len(lines) - len(head) - len(tail)
                return head, tail, omitted > 0

            head_data = file.read(_LOG_HEAD_BYTES)
            file.seek(-_LOG_TAIL_BYTES, 2)
            tail_data = file.read()
    except OSError as err:
        _LOGGER.debug("Diagnostics: cannot read %s: %s", path, err)
        return [], [], False

    head = head_data.decode("utf-8", errors="replace").splitlines()
    if head and not head_data.endswith(b"\n"):
        head = head[:-1]  # drop the partial line at the window edge
    tail = tail_data.decode("utf-8", errors="replace").splitlines()
    if tail:
        tail = tail[1:]  # drop the partial line at the window edge
    return head[:_LOG_HEAD_LINES], tail[-_LOG_TAIL_LINES:], True


def _extract_error_blocks(path: Path) -> dict[str, Any]:
    """Collect ramses WARNING/ERROR/CRITICAL lines with context.

    Only ramses log records (``_LOG_FILTER`` in the line) and their
    continuation lines (e.g. tracebacks) are included; matching
    records from other loggers are only tallied in ``other_matches``.

    :param path: The log file path.
    :type path: Path
    :return: ``ramses_matches``/``other_matches`` counts and context
        ``blocks`` (each with its ``first_line`` number and ``lines``).
    :rtype: dict[str, Any]
    """
    try:
        matches: list[int] = []
        other_matches = 0
        in_ramses = False
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for idx, line in enumerate(file, 1):
                if _LOG_RECORD_RE.match(line):
                    in_ramses = _LOG_FILTER in line
                if not _ERR_LINE_RE.search(line):
                    continue
                if in_ramses or _LOG_FILTER in line:
                    matches.append(idx)
                else:
                    other_matches += 1
    except OSError as err:
        _LOGGER.debug("Diagnostics: cannot read %s: %s", path, err)
        return {
            "ramses_matches": 0,
            "other_matches": 0,
            "blocks_omitted": 0,
            "blocks": [],
        }
    if not matches:
        return {
            "ramses_matches": 0,
            "other_matches": other_matches,
            "blocks_omitted": 0,
            "blocks": [],
        }

    ranges: list[list[int]] = []
    for idx in matches:
        start = max(1, idx - _ERR_CTX_BEFORE)
        end = idx + _ERR_CTX_AFTER
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])

    kept = ranges[-_ERR_MAX_BLOCKS:]
    blocks: list[dict[str, Any]] = []
    block: dict[str, Any] | None = None
    pos = 0
    in_ramses = False
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for idx, raw in enumerate(file, 1):
            if _LOG_RECORD_RE.match(raw):
                in_ramses = _LOG_FILTER in raw
            while pos < len(kept) and idx > kept[pos][1]:
                pos += 1
                block = None
            if pos >= len(kept):
                break
            if idx < kept[pos][0]:
                continue
            if not (in_ramses or _LOG_FILTER in raw):
                continue
            if block is None:
                block = {"first_line": idx, "lines": []}
                blocks.append(block)
            block["lines"].append(raw.rstrip("\n"))
    return {
        "ramses_matches": len(matches),
        "other_matches": other_matches,
        "blocks_omitted": len(ranges) - len(kept),
        "blocks": blocks,
    }


async def _gateway_diagnostics(gwy: Any) -> dict[str, Any]:
    """Collect schema/status/config from the ramses_rf gateway.

    Uses the public ``config_snapshot`` (ramses_rf >= the version with
    it) with a fallback to the private ``_config`` for older releases.
    Each section is captured independently and a failure degrades to an
    ``error`` marker instead of failing the whole download.
    """
    gateway: dict[str, Any] = {}
    for key, attr in (
        ("schema", "schema"),
        ("status", "status"),
        ("config", "config_snapshot"),
    ):
        method: Callable[[], Any] | None = getattr(gwy, attr, None)
        if method is None and attr == "config_snapshot":
            method = getattr(gwy, "_config", None)
        if method is None:
            continue
        try:
            gateway[key] = await method()
        except Exception as err:
            _LOGGER.debug("Diagnostics: gateway %s failed: %r", attr, err)
            gateway[key] = {"error": repr(err)}
    return gateway


def _transport_diagnostics(
    coordinator: RamsesCoordinator, gwy: Any
) -> dict[str, Any]:
    """Collect transport/pool state via the coordinator and gateway.

    Uses the public ``Gateway.transport_info`` when available and falls
    back to the private ``_engine._transport`` chain on older ramses_rf.
    """
    info: dict[str, Any] = {
        "is_pool_enabled": coordinator.is_pool_enabled,
        "pool_children": coordinator.get_pool_child_status(),
    }

    transport_info = getattr(gwy, "transport_info", None)
    if isinstance(transport_info, dict):
        info.update(transport_info)
        return info

    transport = engine_transport(gwy)
    if transport is not None:
        info.update(
            {
                "type": type(transport).__name__,
                SZ_ACTIVE_HGI: transport.get_extra_info(SZ_ACTIVE_HGI),
                "pool_hgi_ids": transport.get_extra_info("pool_hgi_ids"),
                "tx_rate": transport.get_extra_info("tx_rate"),
            }
        )
    return info


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a ramses_cc config entry.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param entry: The config entry.
    :type entry: ConfigEntry
    :return: Redacted diagnostics data.
    :rtype: dict[str, Any]
    """
    coordinator: RamsesCoordinator | None = getattr(
        entry, "runtime_data", None
    )
    secrets = _sensitive_strings(dict(entry.data)) + _sensitive_strings(
        dict(entry.options)
    )

    integration = await async_get_integration(hass, DOMAIN)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)

    diag: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "version": entry.version,
            "data": _redact_config(dict(entry.data)),
            "options": _redact_config(dict(entry.options)),
        },
        "versions": {
            "integration": integration.version,
            "ramses_rf": RAMSES_RF_VERSION,
            "ramses_tx": RAMSES_TX_VERSION,
        },
        "entities": {
            "total": len(entities),
            "per_domain": dict(
                sorted(Counter(e.domain for e in entities).items())
            ),
        },
        "devices": {"total": len(devices)},
    }

    gwy = coordinator.client if coordinator is not None else None
    if coordinator is not None:
        if gwy is not None:
            diag["gateway"] = await _gateway_diagnostics(gwy)
        diag["transport"] = _transport_diagnostics(coordinator, gwy)

        if coordinator.discovery_manager is not None:
            try:
                diag["discovery"] = (
                    coordinator.discovery_manager.export_state()
                )
            except Exception as err:
                _LOGGER.debug("Diagnostics: discovery export failed: %r", err)
                diag["discovery"] = {"error": repr(err)}

    ha_log_path = Path(hass.config.path("home-assistant.log"))
    ha_prev_log_path = Path(hass.config.path("home-assistant.log.1"))
    logs = await hass.async_add_executor_job(
        _collect_log_data,
        ha_log_path,
        ha_prev_log_path,
        _packet_log_candidates(hass, entry),
    )
    (ha_head, ha_tail, ha_truncated) = logs["ha"]
    (pkt_head, pkt_tail, pkt_truncated) = logs["packet"]
    packet_log_path: Path | None = logs["packet_path"]

    diag["logs"] = {
        "home_assistant_log": {
            "path": _display_path(hass, ha_log_path),
            "truncated": ha_truncated,
            "head": [
                _redact_line(line, secrets)
                for line in ha_head
                if _LOG_FILTER in line
            ],
            "tail": [
                _redact_line(line, secrets)
                for line in ha_tail
                if _LOG_FILTER in line
            ],
            "errors": logs["ha_errors"],
        },
        # errors from the previous run — where a crash lives after a
        # restart/rotation
        "home_assistant_log_previous": (
            {
                "path": _display_path(hass, ha_prev_log_path),
                "errors": logs["ha_prev_errors"],
            }
            if logs["ha_prev_errors"] is not None
            else None
        ),
        "packet_log": (
            {
                "path": _display_path(hass, packet_log_path),
                "truncated": pkt_truncated,
                "head": [_redact_line(line, secrets) for line in pkt_head],
                "tail": [_redact_line(line, secrets) for line in pkt_tail],
            }
            if packet_log_path is not None
            else None
        ),
    }

    # final pass: credentials can also surface in gateway/discovery
    # payloads or error reprs (e.g. a URL in an exception message)
    return _deep_scrub(diag, secrets)


def _collect_log_data(
    ha_log_path: Path,
    ha_prev_log_path: Path,
    packet_log_candidates: list[Path],
) -> dict[str, Any]:
    """Read log windows and error blocks in a single executor job."""
    packet_log_path = next(
        (path for path in packet_log_candidates if path.is_file()),
        None,
    )
    return {
        "ha": _read_log_window(ha_log_path),
        "ha_errors": _extract_error_blocks(ha_log_path),
        "ha_prev_errors": (
            _extract_error_blocks(ha_prev_log_path)
            if ha_prev_log_path.is_file()
            else None
        ),
        "packet_path": packet_log_path,
        "packet": (
            _read_log_window(packet_log_path)
            if packet_log_path is not None
            else ([], [], False)
        ),
    }

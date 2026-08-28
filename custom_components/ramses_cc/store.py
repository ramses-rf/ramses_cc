"""Storage handler for RAMSES integration."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import Any, Final

import yaml  # type: ignore[import-untyped, unused-ignore]
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    STORAGE_KEY,
    STORAGE_VERSION,
    SZ_CLIENT_STATE,
    SZ_PACKETS,
    SZ_REMOTES,
    SZ_SCHEMA,
)
from .discovery import SZ_DISCOVERY

_LOGGER = logging.getLogger(__name__)

_BACKUP_KEY: Final[str] = "schema_backups"
_MAX_BACKUPS: Final[int] = 5
_BACKUP_DIR: Final[str] = "ramses_cc_backups"


class RamsesCcStore(Store[dict[str, Any]]):  # type: ignore[misc]
    """HA Store subclass with a migration hook for ramses_cc .storage.

    STORAGE_VERSION stays at 1 — the store format hasn't changed.
    The Phase 3a command migration (remotes → schema _commands) is
    handled at runtime by the coordinator's ``_sync_remotes_to_schema``,
    not by a storage version bump.  The Phase 4 known_list removal is
    handled by the config entry migration (``async_migrate_entry`` in
    ``__init__.py``), not by the store — known_list was never stored
    in .storage, it lived in the config entry options.

    ``max_readable_version`` is set to 2 so that .storage files written
    by the earlier (briefly-released) v2 code can still be loaded.  The
    migration function is a no-op identity — the v2 data format is
    identical to v1, so no transformation is needed.  After loading, the
    data is saved back as v1.

    Downgrade safety: 0.58.0/0.58.1 have ``STORAGE_VERSION = 1`` and
    don't set ``max_readable_version``, so they can read v1 files (what
    we write).  They cannot read v2 files, but since we now write v1,
    this is not a problem.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate stored data to the current version.

        v2 → v1 (identity): the v2 data format is identical to v1 — the
        version bump was reverted because the migration is handled at
        runtime by ``_sync_remotes_to_schema`` in the coordinator.
        v1 → v1 is also a no-op.
        """
        _LOGGER.debug(
            "Migrating ramses_cc storage: v%s.%s → v%s.%s (no-op identity)",
            old_major_version,
            old_minor_version,
            self.version,
            self.minor_version,
        )
        return old_data


class RamsesStore:
    """Class to handle persistence of RAMSES configuration and state."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the storage helper."""
        self._hass = hass
        self._store = RamsesCcStore(
            hass, STORAGE_VERSION, STORAGE_KEY, max_readable_version=2
        )

    async def async_load(self) -> dict[str, Any]:
        """Load the data from the persistent storage.

        :return: The stored data or an empty dictionary if no data exists
        """
        return await self._store.async_load() or {}

    async def async_save(
        self,
        schema: dict[str, Any],
        packets: dict[str, dict[str, Any] | str],
        remotes: dict[str, Any],
        discovery: dict[str, Any] | None = None,
    ) -> None:
        """Save the current state to persistent storage.

        If ``discovery`` is None, any existing discovery state is preserved
        (not overwritten) — this prevents a new coordinator from wiping the
        discovery state during reload before the scan engine has started.

        :param schema: The current device schema
        :param packets: The cached packet log (supports legacy strings
            and JSON DTOs)
        :param remotes: The known remotes and their commands
        :param discovery: The discovery scan state (metadata + engine state)
        """
        data: dict[str, Any] = {
            SZ_CLIENT_STATE: {SZ_SCHEMA: schema, SZ_PACKETS: packets},
            SZ_REMOTES: remotes,
        }

        if discovery is not None:
            data[SZ_DISCOVERY] = discovery
        else:
            # Preserve existing discovery state if we don't have new data
            existing = await self._store.async_load()
            if existing and SZ_DISCOVERY in existing:
                data[SZ_DISCOVERY] = existing[SZ_DISCOVERY]

        # Preserve existing backups (in .storage)
        existing = await self._store.async_load() or {}
        if _BACKUP_KEY in existing:
            data[_BACKUP_KEY] = existing[_BACKUP_KEY]

        await self._store.async_save(data)

    async def async_save_backup(
        self,
        schema: dict[str, Any],
        known_list: dict[str, Any],
        *,
        reason: str = "migration",
    ) -> str | None:
        """Save a backup of schema + known_list as a YAML file.

        Writes a human-readable YAML file to
        ``<config_dir>/ramses_cc_backups/`` so users can open it,
        inspect it, and copy/paste values back into the schema editor
        if a migration goes wrong.

        Also keeps a pointer in .storage (``schema_backups`` key) with the
        file path and timestamp for the restore service to find them.

        :param schema: The schema dict before migration.
        :param known_list: The known_list dict before migration.
        :param reason: Short label for the backup filename (e.g. "migration",
            "phase2", "class_update").
        :return: The path to the backup file, or None on failure.
        """
        timestamp = time.time()
        timestamp_str = time.strftime(
            "%Y%m%d_%H%M%S", time.localtime(timestamp)
        )

        # Build the backup content
        backup_data = {
            "timestamp": timestamp_str,
            "reason": reason,
            "schema": schema,
            "known_list": known_list,
        }

        # Write to <config_dir>/ramses_cc_backups/
        backup_dir = self._hass.config.path(_BACKUP_DIR)
        filename = f"backup_{timestamp_str}_{reason}.yaml"
        filepath = os.path.join(backup_dir, filename)

        try:
            # Create directory if it doesn't exist (run in executor)
            await self._hass.async_add_executor_job(
                _ensure_backup_dir, backup_dir
            )
            # Write the YAML file (run in executor)
            await self._hass.async_add_executor_job(
                _write_yaml_file, filepath, backup_data
            )
        except OSError as err:
            _LOGGER.error("Failed to write backup file %s: %s", filepath, err)
            return None

        _LOGGER.info(
            "Saved schema backup to %s (reason: %s)", filepath, reason
        )

        # Also track in .storage for the restore service
        existing = await self._store.async_load() or {}
        backups: list[dict[str, Any]] = existing.get(_BACKUP_KEY, [])
        backups.append(
            {
                "timestamp": timestamp,
                "reason": reason,
                "filepath": filepath,
                "filename": filename,
            }
        )
        # Trim to max backups (keep the most recent)
        if len(backups) > _MAX_BACKUPS:
            # Remove oldest backup files that are no longer tracked
            removed = backups[:-_MAX_BACKUPS]
            for entry in removed:
                old_path = entry.get("filepath")
                if old_path:
                    await self._hass.async_add_executor_job(
                        _safe_remove, old_path
                    )
            backups = backups[-_MAX_BACKUPS:]

        data = existing.copy()
        data[_BACKUP_KEY] = backups
        await self._store.async_save(data)

        return filepath

    async def async_load_backups(self) -> list[dict[str, Any]]:
        """Load the backup index from .storage.

        :return: A list of backup metadata dicts, each with timestamp,
            reason, filepath, filename.
        """
        existing = await self._store.async_load() or {}
        return existing.get(_BACKUP_KEY, [])

    async def async_load_backup_file(
        self, filepath: str
    ) -> dict[str, Any] | None:
        """Load a specific backup YAML file.

        :param filepath: Path to the backup YAML file.
        :return: The backup dict with schema + known_list, or None on failure.
        """
        try:
            return await self._hass.async_add_executor_job(
                _read_yaml_file, filepath
            )
        except (OSError, yaml.YAMLError) as err:
            _LOGGER.error("Failed to read backup file %s: %s", filepath, err)
            return None


def _ensure_backup_dir(backup_dir: str) -> None:
    """Create the backup directory if it doesn't exist."""
    os.makedirs(backup_dir, exist_ok=True)


def _write_yaml_file(filepath: str, data: dict[str, Any]) -> None:
    """Write a YAML file with a header comment."""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(
            f"# ramses_cc schema backup\n"
            f"# timestamp: {data['timestamp']}\n"
            f"# reason: {data['reason']}\n"
            f"# This file was created automatically before a migration.\n"
            f"# You can copy/paste values from here back into the schema "
            f"editor.\n\n"
        )
        yaml.dump(
            {
                "schema": data["schema"],
                "known_list": data["known_list"],
            },
            f,
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
        )


def _read_yaml_file(filepath: str) -> dict[str, Any]:
    """Read a YAML file."""
    with open(filepath, encoding="utf-8") as f:
        return yaml.load(f, Loader=yaml.SafeLoader)


def _safe_remove(filepath: str) -> None:
    """Remove a file, ignoring errors."""
    with contextlib.suppress(OSError):
        os.remove(filepath)

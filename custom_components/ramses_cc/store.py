"""Storage handler for RAMSES integration."""

from __future__ import annotations

import logging
from typing import Any, Final

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


class RamsesStore:
    """Class to handle persistence of RAMSES configuration and state."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the storage helper."""
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

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
        :param packets: The cached packet log (supports legacy strings and JSON DTOs)
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

        # Preserve existing backups
        existing = await self._store.async_load() or {}
        if _BACKUP_KEY in existing:
            data[_BACKUP_KEY] = existing[_BACKUP_KEY]

        await self._store.async_save(data)

    async def async_save_backup(
        self, schema: dict[str, Any], known_list: dict[str, Any]
    ) -> None:
        """Save an incremental backup of schema + known_list.

        Keeps the most recent ``_MAX_BACKUPS`` backups.  Users can
        copy/paste these to an older version if a migration goes wrong.

        :param schema: The schema dict before migration.
        :param known_list: The known_list dict before migration.
        """
        import time

        existing = await self._store.async_load() or {}
        backups: list[dict[str, Any]] = existing.get(_BACKUP_KEY, [])

        backups.append(
            {
                "timestamp": time.time(),
                "schema": schema,
                "known_list": known_list,
            }
        )

        # Trim to max backups (keep the most recent)
        if len(backups) > _MAX_BACKUPS:
            backups = backups[-_MAX_BACKUPS:]

        # Save without touching the rest of the data
        data = existing.copy()
        data[_BACKUP_KEY] = backups
        await self._store.async_save(data)

        _LOGGER.info(
            "Saved schema backup #%d (total backups: %d)",
            len(backups),
            len(backups),
        )

    async def async_load_backups(self) -> list[dict[str, Any]]:
        """Load all stored backups.

        :return: A list of backup dicts, each with timestamp, schema, known_list.
        """
        existing = await self._store.async_load() or {}
        return existing.get(_BACKUP_KEY, [])

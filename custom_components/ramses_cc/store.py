"""Storage handler for RAMSES integration."""

from __future__ import annotations

from typing import Any

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

        await self._store.async_save(data)

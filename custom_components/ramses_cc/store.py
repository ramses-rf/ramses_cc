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
        self, schema: dict[str, Any], packets: dict[str, str], remotes: dict[str, Any]
    ) -> None:
        """Save the current state to persistent storage.

        :param schema: The current device schema
        :param packets: The cached packet log
        :param remotes: The known remotes and their commands
        """
        data = {
            SZ_CLIENT_STATE: {SZ_SCHEMA: schema, SZ_PACKETS: packets},
            SZ_REMOTES: remotes,
        }
        await self._store.async_save(data)

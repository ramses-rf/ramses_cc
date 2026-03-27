"""Helper utilities for ramses_cc."""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import suppress
from datetime import datetime
from typing import Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from ramses_tx import Code, Message

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def ha_device_id_to_ramses_device_id(
    hass: HomeAssistant, ha_device_id: str
) -> str | None:
    """Return a RAMSES device_id (e.g. "32:153289") for a HA device registry id.

    The HA device id is the opaque string shown when using service UI targets.

    :param hass: The Home Assistant instance.
    :param ha_device_id: The Home Assistant device registry ID.
    :return: The RAMSES device ID (e.g., '01:123456') or None if not found.
    """

    if not ha_device_id:
        return None

    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get(ha_device_id)
    if not device_entry:
        return None

    for domain, dev_id in device_entry.identifiers:
        if domain == DOMAIN:
            return str(dev_id)

    _LOGGER.debug(
        "HA device_id %s has no %s identifier in device registry", ha_device_id, DOMAIN
    )
    return None


def ramses_device_id_to_ha_device_id(
    hass: HomeAssistant, ramses_device_id: str
) -> str | None:
    """Return a HA device registry id for a RAMSES device_id.

    :param hass: The Home Assistant instance.
    :param ramses_device_id: The RAMSES device ID (e.g., '01:123456').
    :return: The Home Assistant device registry ID or None if not found.
    """

    if not ramses_device_id:
        return None

    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, ramses_device_id)})
    if not device_entry:
        return None

    return cast(str, device_entry.id)


def fields_to_aware(dt_or_none: datetime | str | None) -> datetime | None:
    """Convert a potentially naive datetime or string to an aware datetime.

    :param dt_or_none: The datetime object, ISO string, or None to convert.
    :return: An aware datetime object or None.
    """
    if dt_or_none is None:
        return None

    # Use a local variable to help Mypy track the type conversion
    final_dt: datetime | None

    # If it's a string (common in tests or certain library states), parse it
    if isinstance(dt_or_none, str):
        final_dt = dt_util.parse_datetime(dt_or_none)
    else:
        final_dt = dt_or_none

    # Check if parsing failed or if we have a valid datetime
    if final_dt is None:
        return None

    # At this point, Mypy knows final_dt is strictly a datetime object
    if final_dt.tzinfo is not None:
        return final_dt

    # If it is naive, assume it is Local Time (Wall Clock) and make it aware
    return cast(datetime, dt_util.as_local(final_dt))


def as_iso(val: Any) -> str:
    """Convert a datetime or string to a naive ISO string for comparison."""
    if isinstance(val, datetime):
        return val.replace(tzinfo=None).isoformat()
    return str(val)


def latest_dtm(msgs: dict[Code, Message]) -> datetime | None:
    """Get the latest datetime from the device registry."""
    if not msgs:
        return None
    latest_dtm = None
    for msg in msgs.values():
        msg_dtm = getattr(msg, "dtm", None)
        if msg_dtm:
            if latest_dtm is None or msg_dtm > latest_dtm:
                latest_dtm = msg_dtm

    if latest_dtm is None:
        return None
    return latest_dtm


def resolve_async_attr(
    entity: Any, obj: Any, attr_name: str, default: Any = None
) -> Any:
    """Safely get an attribute, resolving coroutines lazily.

    Bridges the gap between HA's synchronous properties and ramses_rf's async DTOs.
    """
    val = getattr(obj, attr_name, default)

    # If it is a method, call it to get the actual value (or the coroutine)
    if callable(val):
        with suppress(TypeError):
            val = val()

    # Aggressively identify if the result is asynchronous
    is_async = (
        inspect.isawaitable(val)
        or asyncio.iscoroutine(val)
        or type(val).__name__ in ("coroutine", "Task", "Future")
    )

    if is_async:
        # Prevent "RuntimeWarning: coroutine was never awaited" if we cannot resolve it
        if not hasattr(entity, "hass") or entity.hass is None:
            if hasattr(val, "close"):
                val.close()
            return default

        cache_key = f"_cached_{id(obj)}_{attr_name}"
        resolving_key = f"_resolving_{id(obj)}_{attr_name}"

        # Dispatch the background task to resolve the coroutine
        if not getattr(entity, resolving_key, False):
            setattr(entity, resolving_key, True)

            async def _resolve() -> None:
                try:
                    # Fetch fresh data so we don't reuse a stale/closed coroutine
                    fresh_val = getattr(obj, attr_name)
                    if callable(fresh_val):
                        fresh_val = fresh_val()

                    if (
                        inspect.isawaitable(fresh_val)
                        or asyncio.iscoroutine(fresh_val)
                        or type(fresh_val).__name__ == "coroutine"
                    ):
                        res = await fresh_val
                    else:
                        res = fresh_val

                    # Update cache and trigger a state write if the value changed
                    if getattr(entity, cache_key, object()) != res:
                        setattr(entity, cache_key, res)
                        if getattr(entity, "entity_id", None):
                            entity.async_write_ha_state()
                except Exception as err:
                    _LOGGER.debug("Error resolving async state %s: %s", attr_name, err)
                finally:
                    setattr(entity, resolving_key, False)

            entity.hass.async_create_task(_resolve())

        # Cleanup the initial coroutine we created synchronously to avoid memory leaks/warnings
        if hasattr(val, "close"):
            val.close()

        cached = getattr(entity, cache_key, default)

        # Absolute safeguard: never return a coroutine to Home Assistant properties
        if (
            inspect.isawaitable(cached)
            or asyncio.iscoroutine(cached)
            or type(cached).__name__ == "coroutine"
        ):
            return default

        return cached

    # Return standard synchronous values immediately
    return val

"""Helper utilities for ramses_cc."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime as dt
from typing import Any, Final, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from ramses_tx.dtos import CommandDTO
from ramses_tx.exceptions import PacketInvalid
from ramses_tx.packet import Packet

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_UNSET: Final[Any] = object()


@dataclass(slots=True)
class _AsyncAttrState:
    """State container for lazy async property resolution."""

    cached: Any = _UNSET
    resolving: bool = False
    resolving_task: asyncio.Task[None] | None = None
    last_dispatch: float = 0.0


def ha_device_id_to_ramses_device_id(
    hass: HomeAssistant, ha_device_id: str
) -> str | None:
    """Return a RAMSES device_id for a HA device registry id.

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
        "HA device_id %s has no %s identifier in device registry",
        ha_device_id,
        DOMAIN,
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
    device_entry = dev_reg.async_get_device(
        identifiers={(DOMAIN, ramses_device_id)}
    )
    if not device_entry:
        return None

    return cast(str | None, device_entry.id)


def fields_to_aware(dt_or_none: dt | str | None) -> dt | None:
    """Convert a potentially naive datetime or string to an aware datetime.

    :param dt_or_none: The datetime object, ISO string, or None to convert.
    :return: An aware datetime object or None.
    """
    if dt_or_none is None:
        return None

    # Use a local variable to help Mypy track the type conversion
    final_dt: dt | None

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
    return cast(dt | None, dt_util.as_local(final_dt))


def as_iso(val: Any) -> str:
    """Convert a datetime or string to a naive ISO string for comparison."""
    if isinstance(val, dt):
        return val.replace(tzinfo=None).isoformat()
    return str(val)


def resolve_async_attr[T](
    entity: Any, obj: Any, attr_name: str, default: T | None = None
) -> T | Any:
    """Safely get an attribute, resolving coroutines lazily.

    Bridges the gap between HA's synchronous properties and ramses_rf's
    async DTOs.

    Includes a per-attribute cooldown (default 30s) to prevent command floods
    when the async getter has side-effects (e.g. ramses_rf's ``system_mode()``
    dispatches a 2E04 RQ when the state is unhydrated).  Without this, every
    HA property access re-dispatches the same RQ in a tight loop.
    """
    val = getattr(obj, attr_name, default)

    # If it is a method, call it to get the actual value (or the coroutine)
    if callable(val):
        with suppress(TypeError):
            val = val()

    # Aggressively identify if the result is asynchronous
    is_async = inspect.isawaitable(val) or isinstance(val, asyncio.Future)

    if is_async:
        # Prevent "RuntimeWarning: coroutine was never awaited" if we
        # cannot resolve it
        if not hasattr(entity, "hass") or entity.hass is None:
            close_fn = getattr(val, "close", None)
            if callable(close_fn):
                close_fn()
            return default

        state_map: dict[tuple[int, str], _AsyncAttrState]
        if not hasattr(entity, "_async_attr_state"):
            entity._async_attr_state = {}
        state_map = entity._async_attr_state

        state_key = (id(obj), attr_name)
        state = state_map.get(state_key)
        if state is None:
            state = _AsyncAttrState()
            state_map[state_key] = state

        # Cooldown: don't re-dispatch the async getter within a short window
        # of the last dispatch.  This prevents command floods when the getter
        # has side-effects (e.g. dispatching RQ commands) and the result is
        # still None (unhydrated state).
        # However, if the cached value is None (unhydrated), we skip the
        # cooldown so that the first real data (e.g. a 30C9 temperature
        # broadcast) is picked up immediately rather than waiting.
        #
        # The cooldown was 30s (issue 1042), but that caused stale sensor
        # readings for up to 30s after a packet updated the underlying
        # state.  It is now 1s, and ``reset_async_attr_cooldown`` is called
        # on every inbound packet (in ``_async_update_and_write_state``) to
        # reset the cooldown timestamp.  This allows the next property
        # access to dispatch a fresh ``_resolve()`` that reads the updated
        # state, while preserving the cached value so HA doesn't detect
        # spurious state changes (which caused the recorder death spiral,
        # issue 1040).
        COOLDOWN_SECS = 1
        now = time.monotonic()
        cached_val = state.cached if state.cached is not _UNSET else default
        within_cooldown = (
            cached_val is not None
            and (now - state.last_dispatch) < COOLDOWN_SECS
        )

        # Dispatch the background task to resolve the coroutine
        if not state.resolving and not within_cooldown:
            state.resolving = True
            state.last_dispatch = now

            async def _resolve() -> None:
                try:
                    # Fetch fresh data so we don't reuse a stale/closed
                    # coroutine
                    fresh_val = getattr(obj, attr_name)
                    if callable(fresh_val):
                        fresh_val = fresh_val()

                    if inspect.isawaitable(fresh_val) or isinstance(
                        fresh_val, asyncio.Future
                    ):
                        res = await fresh_val
                    else:
                        res = fresh_val

                    # Update cache and trigger a state write if the
                    # value changed
                    if state.cached != res:
                        state.cached = res
                        if getattr(entity, "entity_id", None):
                            entity.async_write_ha_state()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    _LOGGER.debug(
                        "Error resolving async state %s: %s", attr_name, err
                    )
                finally:
                    state.resolving = False

            state.resolving_task = entity.hass.async_create_task(_resolve())

        # Cleanup the initial coroutine we created synchronously
        close_fn = getattr(val, "close", None)
        if callable(close_fn):
            close_fn()

        cached = state.cached if state.cached is not _UNSET else default

        # Absolute safeguard: never return a coroutine to HA properties
        if inspect.isawaitable(cached) or isinstance(cached, asyncio.Future):
            return default

        return cached

    # Return standard synchronous values immediately
    return val


def clear_async_attr_cache(entity: Any) -> None:
    """Clear all resolve_async_attr cooldown/cache state for an entity.

    This forces the next property access to re-dispatch the async getter,
    bypassing the cooldown.  Used by force_update so that freshly-received
    packet data is visible immediately.

    If a resolution task is still in flight (e.g. waiting on a real RQ/RP
    round-trip that can take up to 20s), it is cancelled here rather than
    just clearing its "resolving" flag.  Otherwise the next property access
    would dispatch a *second* concurrent getter for the same attribute
    (the old task keeps running since nothing cancelled it), doubling
    outbound command traffic every time force_update is called — this
    compounds badly when force_update is invoked frequently (e.g. across
    many test recipes), overloading the transport.
    """
    state_map: dict[tuple[int, str], _AsyncAttrState] | None = getattr(
        entity, "_async_attr_state", None
    )
    if not state_map:
        return

    # First cancel any in-flight resolution tasks.
    for state in state_map.values():
        if (
            state.resolving_task is not None
            and not state.resolving_task.done()
        ):
            state.resolving_task.cancel()

    # Then clear cooldown/cache/resolving-flag state so the next property
    # access re-dispatches a fresh getter.
    state_map.clear()


def reset_async_attr_cooldown(entity: Any) -> None:
    """Reset the cooldown timestamp for all async attrs on an entity.

    Unlike ``clear_async_attr_cache``, this preserves the cached value
    and does NOT cancel in-flight resolution tasks.  It only resets
    ``last_dispatch`` to 0 so that the next property access can dispatch
    a fresh ``_resolve()`` if the current one has completed.

    This is called on every inbound packet (via
    ``_async_update_and_write_state``) to ensure that freshly-received
    packet data is visible immediately (issue 1042), while avoiding the
    death spiral that ``clear_async_attr_cache`` caused (issue 1040):

    - **Cached value preserved**: the next ``async_write_ha_state()``
      returns the previous value, so HA detects no state change and
      skips the recorder write.  Only when ``_resolve()`` completes with
      a different value does a second state write trigger a recorder
      write.
    - **In-flight tasks not cancelled**: if a ``_resolve()`` is already
      running (e.g. waiting on an RQ/RP round-trip), it is allowed to
      complete.  The ``resolving`` flag prevents a duplicate dispatch.
    - **Cooldown reset**: once the in-flight ``_resolve()`` completes,
      the next property access (from the next packet's state write)
      will dispatch a fresh ``_resolve()`` that reads the updated state.
    """
    state_map: dict[tuple[int, str], _AsyncAttrState] | None = getattr(
        entity, "_async_attr_state", None
    )
    if not state_map:
        return

    for state in state_map.values():
        state.last_dispatch = 0.0


def parse_packet_string(packet_str: str) -> CommandDTO | None:
    """Parse a packet string into a CommandDTO.

    Handles both clean CLI formats and raw RF frames.
    """
    # 1. Try strict CLI format first
    try:
        cmd = CommandDTO.from_cli(packet_str)
        # Validate verb
        if cmd.verb.strip() not in ("I", "W", "RQ", "RP"):
            raise ValueError("Invalid verb")

        # Validate no garbage
        parts = packet_str[2:].split()
        if len(parts) > 0 and parts[0] == "---":
            parts.pop(0)
        if len(parts) > 6:
            raise ValueError("Trailing garbage")

        return cmd
    except (ValueError, IndexError):
        pass

    # 2. Fallback: Parse as a raw RF frame
    try:
        packet = Packet.from_port(dt.now(), packet_str)
        dto = packet.to_dto()
        return CommandDTO(
            verb=dto.verb,
            addr1=dto.addr1,
            addr2=dto.addr2,
            addr3=dto.addr3,
            code=dto.code,
            payload=dto.payload,
        )
    except PacketInvalid:
        return None


def _is_mock(obj: Any) -> bool:
    """Return True if obj is a unittest.mock object."""
    return type(obj).__name__ in (
        "MagicMock",
        "AsyncMock",
        "Mock",
        "PropertyMock",
    )


def extract_demand(val: Any) -> float | None:
    """Extract a numeric demand value (0.0 to 1.0) from a float or DTO object.

    :param val: A float, ThermalDemandDTO, UfhCircuitDemandDTO, or None.
    :return: Float demand value or None.
    """
    if val is None or _is_mock(val):
        return None
    if hasattr(val, "thermal_demand"):
        res = val.thermal_demand
        return float(res) if res is not None and not _is_mock(res) else None
    if hasattr(val, "heat_demand"):
        res = val.heat_demand
        return float(res) if res is not None and not _is_mock(res) else None
    if hasattr(val, "demand"):
        res = val.demand
        return float(res) if res is not None and not _is_mock(res) else None
    if isinstance(val, (int, float)):
        return float(val)
    return None


def resolve_demand_attr(
    entity: Any, obj: Any, primary_attr: str, fallback_attr: str
) -> Any:
    """Resolve primary attribute with fallback.

    Resolve primary_attr (e.g. thermal_demand) with fallback (heat_demand).
    Handles MagicMock objects in tests cleanly when only fallback_attr
    was mocked.
    """
    if _is_mock(obj):
        obj_dict = getattr(obj, "__dict__", {})
        if primary_attr not in obj_dict and fallback_attr in obj_dict:
            return resolve_async_attr(entity, obj, fallback_attr)
    val = resolve_async_attr(entity, obj, primary_attr)
    if val is None or _is_mock(val):
        fallback_val = resolve_async_attr(entity, obj, fallback_attr)
        if fallback_val is not None and not _is_mock(fallback_val):
            return fallback_val
    return val


def dto_to_dict(val: Any) -> Any:
    """Convert a DTO, dataclass, or container of DTOs into plain dictionaries.

    :param val: A DTO object, dict, list, or primitive value.
    :return: Clean dict, list, or primitive suitable for HA state attributes.
    """
    if val is None or _is_mock(val):
        return None
    if hasattr(val, "__dataclass_fields__"):
        return {k: dto_to_dict(v) for k, v in asdict(val).items()}
    if isinstance(val, dict):
        return {k: dto_to_dict(v) for k, v in val.items()}
    if isinstance(val, list):
        return [dto_to_dict(item) for item in val]
    if hasattr(val, "value"):  # Enums
        return val.value
    return val

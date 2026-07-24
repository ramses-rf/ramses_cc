"""Service Handler for RAMSES integration."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import logging
import re
from typing import TYPE_CHECKING, Any, Final, cast

from homeassistant.core import ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later

from ramses_rf.address import Address
from ramses_rf.commands.core import Command as Intent
from ramses_rf.devices import Fakeable
from ramses_rf.enums import Action
from ramses_rf.exceptions import BindingFlowFailed
from ramses_rf.protocol.ramses import _2411_PARAMS_SCHEMA as _2411_PARAMS_SCHEMA
from ramses_rf.schemas import (
    SZ_ACTUATORS,
    SZ_APPLIANCE_CONTROL,
    SZ_DHW_SYSTEM,
    SZ_DHW_VALVE,
    SZ_HTG_VALVE,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
    SZ_ORPHANS_HEAT,
    SZ_ORPHANS_HVAC,
    SZ_REMOTES,
    SZ_SENSOR,
    SZ_SENSORS,
    SZ_SYSTEM,
    SZ_UFH_SYSTEM,
    SZ_ZONES,
)
from ramses_tx.address import pkt_addrs
from ramses_tx.dtos import CommandDTO
from ramses_tx.exceptions import (
    PacketAddrSetInvalid,
    ProtocolSendFailed,
    ProtocolTimeoutError,
    TransportError,
)

from .const import CONF_SCHEMA, DOMAIN, SZ_KNOWN_LIST, SZ_TR_SKIPPED
from .helpers import parse_packet_string

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)

_CALL_LATER_DELAY: Final = 5  # needed for tests
_DEVICE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-F]{2}:[0-9A-F]{6}$", re.I)


def _device_in_fragment(fragment: dict[str, Any], device_id: str) -> bool:
    """Check if a device_id appears anywhere in a schema fragment."""

    def _search(node: Any) -> bool:
        if isinstance(node, str):
            return node == device_id
        if isinstance(node, list):
            return any(_search(item) for item in node)
        if isinstance(node, dict):
            if device_id in node:
                return True
            return any(_search(v) for v in node.values())
        return False

    return _search(fragment)


# Single-slot schema roles that hold exactly one device ID.  If a fragment
# tries to place a device into one of these slots while a *different* device
# already holds it, the merge would displace the existing device — which then
# becomes an orphan and gets re-discovered, causing a discovery loop (issue
# 834 comment 5044906835).  The guard in _apply_schema_entry detects such
# conflicts and redirects the new device to orphans_heat instead.
# NOTE: zone/DHW sensor slots are intentionally excluded — replacing a sensor
# is a legitimate user action (swapping a TRV), not an automatic
# misclassification loop.  The loop risk is specific to relay/actuator slots
# where the scan engine's heuristic (e.g. 3B00/3EF0 → appliance_control) can
# flag two devices for the same slot.
_SINGLE_SLOT_ROLES: Final[frozenset[str]] = frozenset(
    {SZ_APPLIANCE_CONTROL, "hotwater_valve", "heating_valve"}
)


def _resolve_single_slot_conflicts(
    fragment: dict[str, Any],
    current_schema: dict[str, Any],
    device_id: str,
) -> dict[str, Any]:
    """Prevent a fragment from displacing a different device from a single-slot role.

    Scans the fragment for single-slot keys (``appliance_control``,
    ``hotwater_valve``, ``heating_valve``, ``sensor``) that would overwrite a
    slot already held by a *different* device in ``current_schema``.  When a
    conflict is found, the conflicting key is removed from the fragment and
    the device being accepted is redirected to ``orphans_heat`` so the user
    can resolve the conflict manually.

    This prevents the discovery loop described in issue 834 comment
    5044906835: two relays both broadcasting 3B00/3EF0 are both classified as
    ``appliance_control``; accepting one displaces the other from the slot,
    which is then re-discovered and re-accepted, ad infinitum.

    :param fragment: The schema fragment from generate_schema_entry.
    :param current_schema: The current config entry schema (before merge).
    :param device_id: The device being accepted (the one the fragment is for).
    :return: A possibly-modified fragment with conflicts resolved.
    """
    result = copy.deepcopy(fragment)
    redirected = False

    for tcs_id, tcs_frag in result.items():
        if not isinstance(tcs_id, str) or not isinstance(tcs_frag, dict):
            continue
        if tcs_id in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC, "orphans"):
            continue
        current_tcs = current_schema.get(tcs_id, {})
        if not isinstance(current_tcs, dict):
            current_tcs = {}

        # system.appliance_control
        frag_sys = tcs_frag.get(SZ_SYSTEM)
        if isinstance(frag_sys, dict):
            frag_app = frag_sys.get(SZ_APPLIANCE_CONTROL)
            cur_sys = current_tcs.get(SZ_SYSTEM, {})
            cur_app = (
                cur_sys.get(SZ_APPLIANCE_CONTROL) if isinstance(cur_sys, dict) else None
            )
            if (
                isinstance(frag_app, str)
                and frag_app == device_id
                and cur_app
                and cur_app != device_id
            ):
                _LOGGER.warning(
                    "accept_discovered_device: %s would displace %s from "
                    "appliance_control slot in %s — redirecting to "
                    "orphans_heat to avoid discovery loop (issue 834)",
                    device_id,
                    cur_app,
                    tcs_id,
                )
                frag_sys.pop(SZ_APPLIANCE_CONTROL)
                if not frag_sys:
                    tcs_frag.pop(SZ_SYSTEM)
                redirected = True

        # stored_hotwater.hotwater_valve / heating_valve (sensor excluded —
        # sensor replacement is a legitimate user action, not a loop risk)
        frag_dhw = tcs_frag.get(SZ_DHW_SYSTEM)
        if isinstance(frag_dhw, dict):
            cur_dhw = current_tcs.get(SZ_DHW_SYSTEM, {})
            if not isinstance(cur_dhw, dict):
                cur_dhw = {}
            for slot_key in ("hotwater_valve", "heating_valve"):
                frag_val = frag_dhw.get(slot_key)
                cur_val = cur_dhw.get(slot_key)
                if (
                    isinstance(frag_val, str)
                    and frag_val == device_id
                    and cur_val
                    and cur_val != device_id
                ):
                    _LOGGER.warning(
                        "accept_discovered_device: %s would displace %s "
                        "from %s slot in %s.stored_hotwater — redirecting "
                        "to orphans_heat to avoid discovery loop (issue 834)",
                        device_id,
                        cur_val,
                        slot_key,
                        tcs_id,
                    )
                    frag_dhw.pop(slot_key)
                    redirected = True
            if not frag_dhw:
                tcs_frag.pop(SZ_DHW_SYSTEM)

    if redirected:
        # Add the device to orphans_heat so it's not lost
        orphans = result.get(SZ_ORPHANS_HEAT, [])
        if not isinstance(orphans, list):
            orphans = []
        if device_id not in orphans:
            orphans = sorted([*orphans, device_id])
            result[SZ_ORPHANS_HEAT] = orphans

    return result


class _MockServiceCall:
    """Minimal stand-in for ServiceCall when invoking a service handler internally.

    Only provides ``.data`` — enough for handlers that only read data fields.
    """

    __slots__ = ("data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class RamsesServiceHandler:
    """Handler for RAMSES integration service calls."""

    def __init__(self, coordinator: RamsesCoordinator) -> None:
        """Initialize the Service Handler."""
        self._coordinator = coordinator
        self.hass = coordinator.hass
        self._fan_param_sequences: dict[str, asyncio.Task[Any]] = {}
        self._probe_task: asyncio.Task[Any] | None = None
        self._call_later_handles: list[Any] = []
        self._pending_timers: list[asyncio.Task[Any]] = []

    @callback
    def _schedule_refresh(self, _: Any) -> None:
        """Schedule a coordinator refresh.

        :param _: Unused argument (required for async_call_later callback signature).
        """
        self.hass.async_create_task(self._coordinator.async_request_refresh())

    def _schedule_refresh_later(self) -> None:
        """Schedule a refresh via async_call_later, tracking the handle for cleanup."""
        handle = async_call_later(
            self.hass,
            _CALL_LATER_DELAY,
            self._schedule_refresh,
        )
        self._call_later_handles.append(handle)

    def _schedule_clear_pending(self, entity: Any, timeout: int) -> None:
        """Schedule a _clear_pending_after_timeout task on the entity, tracked for cleanup.

        :param entity: The entity to clear pending state on.
        :param timeout: Timeout in seconds.
        """
        if not entity or not hasattr(entity, "_clear_pending_after_timeout"):
            return
        # Cancel any previous pending timer on the entity
        prev = getattr(entity, "_pending_timer", None)
        if prev and not prev.done():
            prev.cancel()
        task = self.hass.async_create_task(
            cast(Any, entity)._clear_pending_after_timeout(timeout)
        )
        entity._pending_timer = task
        self._pending_timers.append(task)

    def register_pending_timer(self, task: asyncio.Task[Any]) -> None:
        """Register a pending timer task for central cleanup on shutdown.

        :param task: The asyncio task to track.
        """
        self._pending_timers.append(task)

    async def async_cleanup(self) -> None:
        """Cancel pending tasks and scheduled callbacks during unload."""
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._probe_task
            self._probe_task = None

        for task in list(self._fan_param_sequences.values()):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._fan_param_sequences.clear()

        for task in list(self._pending_timers):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._pending_timers.clear()

        for handle in self._call_later_handles:
            handle()
        self._call_later_handles.clear()

    async def async_bind_device(self, call: ServiceCall) -> None:
        """Handle the bind_device service call to bind a device to the system.

        :param call: The service call object containing binding details (device_id, offer, etc.).
        :raises HomeAssistantError: If the client is not initialized or binding fails.
        """

        if not self._coordinator.client:
            raise HomeAssistantError(
                "Cannot bind device: RAMSES RF client is not initialized"
            )

        device: Fakeable

        try:
            device = await self._coordinator.client.device_registry.fake_device(
                call.data["device_id"]
            )
        except LookupError as err:
            _LOGGER.error("%s", err)
            raise HomeAssistantError(
                f"Device not found: {call.data.get('device_id')}"
            ) from err

        cmd = (
            parse_packet_string(call.data["device_info"])
            if call.data["device_info"]
            else None
        )

        _LOGGER.warning("Starting binding process for device %s", device.id)

        try:
            # Extract the first key from the 'confirm' dict as the confirm_code
            confirm_data = call.data.get("confirm", {})
            confirm_code = next(iter(confirm_data), None)

            await device._initiate_binding_process(
                list(call.data["offer"].keys()),
                confirm_code=confirm_code,
                ratify_cmd=cmd,
            )

            _LOGGER.warning(
                "Success! Binding process completed for device %s", device.id
            )

        except BindingFlowFailed as err:
            raise HomeAssistantError(
                f"Binding failed for device {device.id}: {err}"
            ) from err
        except Exception as err:
            _LOGGER.error("Binding process failed for device %s: %s", device.id, err)
            raise HomeAssistantError(
                f"Unexpected error during binding for {device.id}: {err}"
            ) from err

        # Schedule a refresh (DataUpdateCoordinator pattern)
        self._schedule_refresh_later()

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Create and send a raw command packet via the transport layer.

        :param call: The service call object containing packet details (verb, code, payload, etc.).
        :raises HomeAssistantError: If the client is not initialized.
        """
        if not self._coordinator.client:
            raise HomeAssistantError(
                "Cannot send packet: RAMSES RF client is not initialized"
            )
        kwargs = dict(call.data.items())  # is ReadOnlyDict
        if (
            call.data["device_id"] == "18:000730"
            and kwargs.get("from_id", "18:000730") == "18:000730"
            and self._coordinator.client.hgi
            and self._coordinator.client.hgi.id
        ):
            kwargs["device_id"] = self._coordinator.client.hgi.id

        cmd = self._coordinator.client.create_cmd(**kwargs)

        cmd = self._adjust_sentinel_packet(cmd)

        try:
            await self._coordinator.client.async_send_cmd(cmd)
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to send packet: {err}") from err

        self._schedule_refresh_later()

    def _adjust_sentinel_packet(self, cmd: CommandDTO) -> CommandDTO:
        """Fix address positioning for specific sentinel packets (18:000730)."""
        # HACK: to fix the device_id when GWY announcing.
        if not self._coordinator.client:
            raise HomeAssistantError(
                "Cannot set parameter: RAMSES RF client is not initialized"
            )
        hgi = self._coordinator.client.hgi
        if not hgi or not hgi.id:
            return cmd

        # CommandDTO uses positional addressing (addr1/addr2/addr3).
        # addr1 is the source (from_id), addr2 is the destination.
        if cmd.addr1 != "18:000730" or cmd.addr2 != hgi.id:
            return cmd

        try:
            # Validate if the current address structure is acceptable
            pkt_addrs(f"{hgi.id} {cmd.addr2} {cmd.addr3}")
        except PacketAddrSetInvalid:
            # If invalid, swap addr2 and addr3 to correct the structure.
            # CommandDTO is frozen, so use dataclasses.replace.
            cmd = dataclasses.replace(cmd, addr2=cmd.addr3, addr3=cmd.addr2)
            _LOGGER.debug(
                "Swapped addresses for sentinel packet 18:000730 to maintain protocol validity"
            )

        return cmd

    async def async_get_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Handle 'get_fan_param' service call.

        Sends a request to retrieve a specific parameter from a fan device.

        :param call: The service call object or dictionary containing parameter details.
        :raises HomeAssistantError: If the client is not initialized or the request fails.
        :raises ServiceValidationError: If the parameters are invalid.
        """
        if not self._coordinator.client:
            raise HomeAssistantError(
                "Cannot get parameter: RAMSES RF client is not initialized"
            )
        entity = None  # Ensure entity is defined for finally/except blocks

        try:
            data = self._normalize_service_call(call)

            _LOGGER.debug("Processing get_fan_param service call with data: %s", data)

            # Extract id's
            original_device_id, normalized_device_id, from_id = (
                self._get_device_and_from_id(data)
            )

            # 1. Validate Destination specifically
            if not original_device_id:
                # Use ServiceValidationError for UI feedback
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="service_device_id_missing",
                    translation_placeholders={"data": str(data)},
                )

            param_id = self._get_param_id(data)

            # If no from_id or a bound device was found then try gateway HGI
            if not from_id and original_device_id:
                gateway_id = getattr(
                    getattr(self._coordinator.client, "hgi", None), "id", None
                )
                if isinstance(gateway_id, str) and _DEVICE_ID_RE.match(
                    gateway_id.strip()
                ):
                    from_id = gateway_id.strip()
                    _LOGGER.debug(
                        "No explicit/bound from_id for %s, using gateway id %s",
                        original_device_id,
                        from_id,
                    )

            # 2. Validate Source specifically
            if not from_id:
                _LOGGER.warning(
                    "Cannot get parameter: No valid source device available for destination %s. "
                    "Need either: explicit 'from_id', or a REM/DIS device that was 'bound' in the configuration.",
                    original_device_id,
                )
                return

            # Find the corresponding entity and set it to pending
            entity = self._coordinator.fan_handler.find_param_entity(
                normalized_device_id, param_id
            )
            if entity and hasattr(entity, "set_pending"):
                cast(Any, entity).set_pending()

            intent = Intent(
                src=Address(from_id),
                dst=Address(original_device_id),
                action=Action.GET_FAN_PARAM,
                data={"param_id": param_id},
            )
            # Use the CQRS CommandDispatcher, which translates the intent
            # to a CommandDTO and shims it back to a legacy Command that
            # async_send_cmd accepts.  Calling build_dto() + async_send_cmd()
            # directly passes a CommandDTO (positional addr1/addr2/addr3) to
            # a code path that expects cmd.src.id, raising AttributeError.
            # See ramses_cc issue 851.
            _LOGGER.debug("Sending get_fan_param intent: %s", intent)
            await self._coordinator.client.dispatcher.send(intent)

            # Clear pending state after timeout (non-blocking)
            self._schedule_clear_pending(entity, 30)

        except ServiceValidationError:
            # Bubble up validation errors directly to the UI
            self._schedule_clear_pending(entity, 0)
            raise

        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            # Raise friendly error for UI
            self._schedule_clear_pending(entity, 0)
            raise HomeAssistantError(f"Failed to get fan parameter: {err}") from err

        except ValueError as err:
            # Catch errors from helpers (e.g. _get_param_id) and raise friendly error
            _LOGGER.error("Failed to get fan parameter: %s", err)
            self._schedule_clear_pending(entity, 0)
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="service_param_invalid",
                translation_placeholders={"err": str(err)},
            ) from err

        except Exception as err:
            _LOGGER.error("Failed to get fan parameter: %s", err, exc_info=True)
            # Clear pending state on error
            self._schedule_clear_pending(entity, 0)
            # Raise friendly error for UI
            raise HomeAssistantError(f"Failed to get fan parameter: {err}") from err

    async def get_all_fan_params(self, call: dict[str, Any] | ServiceCall) -> None:
        """Wrapper for _async_run_fan_param_sequence.

        Initiates a sequence to retrieve all known fan parameters.

        :param call: The service call object or dictionary containing target details.
        """
        self.hass.async_create_task(self._async_run_fan_param_sequence(call))

    async def _async_run_fan_param_sequence(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Handle 'update_fan_params' service call (or direct dict)."""
        try:
            data = self._normalize_service_call(call)
            _LOGGER.debug(
                "Processing update_fan_params service call with data: %s", data
            )
            device_id = self._resolve_device_id(data)
            if not device_id:
                _LOGGER.warning(
                    "Cannot run fan param sequence: missing device_id in call %s",
                    data,
                )
                return
        except Exception as err:
            _LOGGER.error("Invalid service call data: %s", err)
            return

        device_key = device_id.replace(":", "_").upper()

        existing = self._fan_param_sequences.get(device_key)
        if existing:
            if existing.done():
                self._fan_param_sequences.pop(device_key, None)
            else:
                _LOGGER.debug(
                    "Skipping duplicate fan param sweep for %s (task_id=%s still running)",
                    device_id,
                    id(existing),
                )
                return

        current_task = asyncio.current_task()
        if current_task is None:
            # Fallback sentinel so we can still clear the tracker.
            current_task = asyncio.create_task(asyncio.sleep(0))
            # The task should never be awaited, cancel immediately once stored.
            current_task.cancel()

        self._fan_param_sequences[device_key] = current_task

        try:
            for idx, param_id in enumerate(_2411_PARAMS_SCHEMA):
                try:
                    try:
                        param_data = dict(data)
                    except (TypeError, ValueError):
                        param_data = (
                            {k: v for k, v in data.items()}
                            if hasattr(data, "items")
                            else data
                        )
                    param_data["param_id"] = param_id
                    await self.async_get_fan_param(param_data)

                    if idx < len(_2411_PARAMS_SCHEMA) - 1:
                        await asyncio.sleep(0.5)

                except ProtocolTimeoutError as err:
                    _LOGGER.warning(
                        "Timeout getting fan parameter %s for device: %s "
                        "(will retry on next poll cycle)",
                        param_id,
                        err,
                    )
                    continue
                except Exception as err:
                    _LOGGER.error(
                        "Failed to get fan parameter %s for device: %s", param_id, err
                    )
                    continue
        finally:
            tracked = self._fan_param_sequences.get(device_key)
            if tracked is current_task:
                self._fan_param_sequences.pop(device_key, None)

    async def async_set_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Handle 'set_fan_param' service call.

        Sends a command to set a specific parameter on a fan device.

        :param call: The service call object or dictionary containing parameter details and value.
        :raises HomeAssistantError: If the client is not initialized or the request fails.
        :raises ValueError: If required parameters are missing.
        """
        if not self._coordinator.client:
            raise HomeAssistantError(
                "Cannot set parameter: RAMSES RF client is not initialized"
            )
        entity = None

        try:
            data = self._normalize_service_call(call)

            _LOGGER.debug("Processing set_fan_param service call with data: %s", data)

            original_device_id, normalized_device_id, from_id = (
                self._get_device_and_from_id(data)
            )

            # 1. Validate Destination specifically
            if not original_device_id:
                msg = f"Cannot set parameter: Destination 'device_id' is missing or invalid in call: {data}"
                _LOGGER.warning(msg)
                raise HomeAssistantError(msg)

            # 2. Validate Source specifically
            if not from_id:
                msg = (
                    f"Cannot set parameter: No valid source device available for destination {original_device_id}. "
                    "Need either: explicit 'from_id', or a REM/DIS device that was 'bound' in the configuration."
                )
                _LOGGER.warning(msg)
                raise HomeAssistantError(msg)

            param_id = self._get_param_id(data)

            value = data.get("value")
            if value is None:
                raise ValueError("Missing required parameter: value")

            _LOGGER.debug(
                "Setting parameter %s=%s on device %s from %s",
                param_id,
                value,
                original_device_id,
                from_id,
            )

            entity = self._coordinator.fan_handler.find_param_entity(
                normalized_device_id, param_id
            )
            if entity and hasattr(entity, "set_pending"):
                cast(Any, entity).set_pending()

            intent = Intent(
                src=Address(from_id),
                dst=Address(original_device_id),
                action=Action.SET_FAN_PARAM,
                data={"param_id": param_id, "value": value},
            )
            # Use the CQRS CommandDispatcher (translates intent → CommandDTO
            # → legacy Command).  See get_fan_param above / issue 851.
            _LOGGER.debug("Sending set_fan_param intent: %s", intent)
            await self._coordinator.client.dispatcher.send(intent)
            await asyncio.sleep(0.2)

            self._schedule_clear_pending(entity, 30)

        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            self._schedule_clear_pending(entity, 0)
            raise HomeAssistantError(f"Failed to set fan parameter: {err}") from err
        except ValueError as err:
            self._schedule_clear_pending(entity, 0)
            raise HomeAssistantError(
                f"Invalid parameter for set_fan_param: {err}"
            ) from err
        except Exception as err:
            _LOGGER.error("Failed to set fan parameter: %s", err, exc_info=True)
            self._schedule_clear_pending(entity, 0)
            raise HomeAssistantError(f"Failed to set fan parameter: {err}") from err

    # Private Helpers

    def _get_param_id(self, call: dict[str, Any]) -> str:
        """Get and validate parameter ID from service call data."""
        data = self._normalize_service_call(call)
        param_id: str | None = data.get("param_id")
        if not param_id:
            _LOGGER.error("Missing required parameter: param_id")
            raise ValueError("required key not provided @ data['param_id']")

        param_id = str(param_id).upper().strip()

        try:
            if len(param_id) != 2 or int(param_id, 16) < 0 or int(param_id, 16) > 0xFF:
                raise ValueError
        except (ValueError, TypeError):
            error_msg = f"Invalid parameter ID: '{param_id}'. Must be a 2-digit hexadecimal value (00-FF)"
            _LOGGER.error(error_msg)
            raise ValueError(error_msg) from None

        return param_id

    def _target_to_device_id(self, target: dict[str, Any]) -> str | None:
        """Translate HA target selectors into a RAMSES device id using registries."""
        if not target:
            return None

        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        def _device_entry_to_ramses_id(
            _device_entry: dr.DeviceEntry | None,
        ) -> str | None:
            if not _device_entry:
                return None
            for domain, dev_id in _device_entry.identifiers:
                if domain == DOMAIN:
                    return str(dev_id)
            return None

        resolved_ids: list[str] = []

        # 1. Check Entity IDs
        entity_ids = target.get("entity_id")
        if entity_ids:
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            for entity_id in entity_ids:
                if (
                    entity_entry := ent_reg.async_get(entity_id)
                ) and entity_entry.device_id:
                    device_entry = dev_reg.async_get(entity_entry.device_id)
                    if device_id := _device_entry_to_ramses_id(device_entry):
                        resolved_ids.append(device_id)

        # 2. Check Device IDs
        if not resolved_ids:
            device_ids = target.get("device_id")
            if device_ids:
                if isinstance(device_ids, str):
                    device_ids = [device_ids]
                for device_id in device_ids:
                    device_entry = dev_reg.async_get(device_id)
                    if resolved := _device_entry_to_ramses_id(device_entry):
                        resolved_ids.append(resolved)

        # 3. Check Area IDs
        if not resolved_ids:
            area_ids = target.get("area_id")
            if area_ids:
                if isinstance(area_ids, str):
                    area_ids = [area_ids]
                for area_id in area_ids:
                    for device_entry in dev_reg.devices.values():
                        if device_entry.area_id == area_id:
                            if resolved := _device_entry_to_ramses_id(device_entry):
                                resolved_ids.append(resolved)
                    if resolved_ids:
                        break

        return resolved_ids[0] if resolved_ids else None

    def _resolve_device_id(self, data: dict[str, Any]) -> str | None:
        """Return device_id from either explicit device_id or HA target selector."""

        def _get_first(key: str) -> Any | None:
            val = data.get(key)
            if val is None:
                return None
            if isinstance(val, list):
                if not val:
                    return None
                if len(val) > 1:
                    _LOGGER.warning(
                        "Multiple values for '%s' provided, using first one: %s",
                        key,
                        val[0],
                    )
                data[key] = val[0]
                return val[0]
            return val

        if (device_id := _get_first("device_id")) is not None:
            if isinstance(device_id, str):
                if ":" in device_id or "_" in device_id:
                    return device_id
                if resolved := self._target_to_device_id({"device_id": [device_id]}):
                    data["device_id"] = resolved
                    return str(resolved)
            res = str(device_id)
            data["device_id"] = res
            return res

        if (ha_device := _get_first("device")) is not None:
            if isinstance(ha_device, str):
                if resolved := self._target_to_device_id({"device_id": [ha_device]}):
                    data["device_id"] = resolved
                    return str(resolved)

        if (target := data.get("target")) and (
            resolved := self._target_to_device_id(target)
        ):
            data["device_id"] = resolved
            return str(resolved)

        return None

    def _get_device_and_from_id(self, data: dict[str, Any]) -> tuple[str, str, str]:
        """Resolve the target device and the source (from) device IDs."""
        device_id = self._resolve_device_id(data)
        if not device_id:
            return "", "", ""

        device = self._coordinator._get_device(device_id)
        if not device:
            return device_id, device_id.replace(":", "_"), ""

        from_id = data.get("from_id")
        if not from_id:
            from_id = device.get_bound_rem()

        if from_id is None:
            from_id = ""

        return device.id, device.id.replace(":", "_"), from_id

    def _normalize_service_call(
        self, call: dict[str, Any] | ServiceCall
    ) -> dict[str, Any]:
        """Return a mutable dict containing service call data and target info."""
        if isinstance(call, ServiceCall):
            data = dict(call.data)
            target = getattr(call, "target", None)
        else:
            data = dict(call)
            target = data.get("target")

        if target:
            if hasattr(target, "as_dict"):
                data["target"] = target.as_dict()
            elif isinstance(target, dict):
                data["target"] = target

        return data

    # ───────────────────────────────────────────────────────────────────────
    # discover_known_devices
    # ───────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_device_ids_from_schema(schema: dict[str, Any]) -> set[str]:
        """Extract all device IDs from a ramses_rf global schema dict.

        The schema structure (SCH_GLOBAL_SCHEMAS_DICT) contains:
        - SZ_MAIN_TCS: the CTL device_id (01:...)
        - <CTL device_id>: a TCS dict with system, dhw, ufh, zones, orphans
        - <FAN device_id>: an HVAC dict with remotes, sensors
        - SZ_ORPHANS_HEAT / SZ_ORPHANS_HVAC: lists of orphan device IDs

        :param schema: The global schema dict (config or merged).
        :return: A set of all device IDs found in the schema.
        """
        device_ids: set[str] = set()

        # Main TCS (the CTL)
        if ctl_id := schema.get(SZ_MAIN_TCS):
            device_ids.add(ctl_id)

        for key, value in schema.items():
            # Skip non-device-id keys and ramses_cc extension keys
            if key in (
                SZ_MAIN_TCS,
                SZ_ORPHANS_HEAT,
                SZ_ORPHANS_HVAC,
                "transport_constructor",
            ):
                continue
            if not _DEVICE_ID_RE.match(str(key)):
                continue

            # key is a device_id (CTL or FAN)
            device_ids.add(str(key))

            if not isinstance(value, dict):
                continue

            # Heat TCS structure
            # System → appliance_control
            if isinstance(value.get(SZ_SYSTEM), dict):
                if app_id := value[SZ_SYSTEM].get(SZ_APPLIANCE_CONTROL):
                    device_ids.add(app_id)

            # DHW system → sensor, dhw_valve, htg_valve
            if isinstance(value.get(SZ_DHW_SYSTEM), dict):
                dhw = value[SZ_DHW_SYSTEM]
                if sensor_id := dhw.get(SZ_SENSOR):
                    device_ids.add(sensor_id)
                if valve_id := dhw.get(SZ_DHW_VALVE):
                    device_ids.add(valve_id)
                if valve_id := dhw.get(SZ_HTG_VALVE):
                    device_ids.add(valve_id)

            # UFH system → UFC device_ids and circuit zone indices
            if isinstance(value.get(SZ_UFH_SYSTEM), dict):
                for ufc_id in value[SZ_UFH_SYSTEM]:
                    if _DEVICE_ID_RE.match(str(ufc_id)):
                        device_ids.add(str(ufc_id))

            # Zones → sensor, actuators
            if isinstance(value.get(SZ_ZONES), dict):
                for zone_data in value[SZ_ZONES].values():
                    if not isinstance(zone_data, dict):
                        continue
                    if sensor_id := zone_data.get(SZ_SENSOR):
                        device_ids.add(sensor_id)
                    for act_id in zone_data.get(SZ_ACTUATORS, []):
                        device_ids.add(act_id)

            # TCS-level orphans
            for orphan_id in value.get(SZ_ORPHANS, []):
                device_ids.add(orphan_id)

            # HVAC structure: remotes, sensors
            for remote_id in value.get(SZ_REMOTES, []):
                device_ids.add(remote_id)
            for sensor_id in value.get(SZ_SENSORS, []):
                device_ids.add(sensor_id)

        # Global orphans
        for orphan_id in schema.get(SZ_ORPHANS_HEAT, []):
            device_ids.add(orphan_id)
        for orphan_id in schema.get(SZ_ORPHANS_HVAC, []):
            device_ids.add(orphan_id)

        return device_ids

    async def async_discover_known_devices(self, call: ServiceCall) -> None:
        """Force-create known_list and schema devices and trigger their discovery pollers.

        Uses the existing ``DiscoveryService`` in ramses_rf — each device
        class knows its own RQ codes via ``_setup_discovery_cmds()``.  This
        service simply ensures the devices exist in the registry (creating
        them from the known_list and/or schema if needed) and then forces
        an immediate discovery cycle so the pollers send their RQs right
        away instead of waiting for the next scheduled poll.

        HGI-class devices are skipped — they are gateways, not responders,
        and will be detected naturally when they send traffic. Multi-HGI
        support is not yet available in ramses_rf.

        :param call: The service call object (optional ``device_id`` field).
        """
        client = self._coordinator.client
        if not client:
            raise HomeAssistantError(
                "Cannot discover devices: RAMSES RF client is not initialized"
            )

        known_list: dict[str, Any] = self._coordinator.options.get(SZ_KNOWN_LIST, {})
        config_schema: dict[str, Any] = self._coordinator.options.get(CONF_SCHEMA, {})

        # Collect device IDs from both known_list and schema
        all_device_ids: set[str] = set(known_list.keys())
        schema_device_ids = self._extract_device_ids_from_schema(config_schema)
        all_device_ids |= schema_device_ids

        if not all_device_ids:
            _LOGGER.warning(
                "discover_known_devices: no known_list or schema configured"
            )
            return

        # Optionally restrict to a single device
        target_device_id: str | None = call.data.get("device_id")
        if target_device_id:
            if target_device_id not in all_device_ids:
                _LOGGER.warning(
                    "discover_known_devices: device %s not in known_list or schema",
                    target_device_id,
                )
                return
            all_device_ids = {target_device_id}

        device_registry = client.device_registry
        device_by_id = device_registry.device_by_id

        # Classify each device
        created: list[str] = []
        already_present: list[str] = []
        skipped_hgi: list[str] = []

        for device_id in sorted(all_device_ids):
            # Skip the active HGI itself
            if client.hgi and device_id == client.hgi.id:
                continue

            # Check if device is HGI-class (from known_list traits or address prefix)
            traits = known_list.get(device_id, {})
            is_hgi = traits.get("class", "").upper() == "HGI" or device_id.startswith(
                "18:"
            )

            if device_id in device_by_id:
                already_present.append(device_id)
            elif is_hgi:
                # Skip HGI gateways — they don't respond to RQs and have no
                # discovery commands. They'll be detected when they send traffic.
                # TODO: add multi-HGI support when ramses_rf supports it
                skipped_hgi.append(device_id)
                _LOGGER.info(
                    "Skipping HGI %s (gateways don't respond to RQs, "
                    "will be detected when it sends traffic)",
                    device_id,
                )
                continue
            else:
                # Force-create the device — this calls _setup_discovery_cmds()
                # which adds the right RQ codes to the device's DiscoveryService.
                try:
                    dev = device_registry.get_device(device_id)
                    created.append(device_id)
                    _LOGGER.debug(
                        "Created device %s (%s), discovery poller started with %d cmds",
                        device_id,
                        getattr(dev, "_SLUG", "?"),
                        len(dev.discovery.cmds),
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to create device %s: %s",
                        device_id,
                        err,
                    )

        _LOGGER.info(
            "Discovering known devices: %d from known_list, %d from schema, "
            "%d already present, %d created, %d HGI skipped",
            len(known_list),
            len(schema_device_ids),
            len(already_present),
            len(created),
            len(skipped_hgi),
        )

        if not created and not already_present:
            _LOGGER.info("discover_known_devices: nothing to do")
            return

        # Run the discovery probing and entity creation in the background
        # so the service call returns immediately. Each probe that times out
        # can block for 20s, and with multiple devices this would otherwise
        # freeze the UI for minutes.
        # Cancel any previous probe task before starting a new one.
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
        self._probe_task = self.hass.async_create_task(
            self._async_probe_and_discover(
                created, already_present, zero_cmds_skip=skipped_hgi
            )
        )

    async def _async_probe_and_discover(
        self,
        created: list[str],
        already_present: list[str],
        *,
        zero_cmds_skip: list[str] | None = None,
    ) -> None:
        """Probe devices and trigger entity discovery (runs in background).

        This is the slow part of ``discover_known_devices`` — it sends RQ
        commands to each device and waits for responses/timeouts.  It should
        not block the event loop or the service call response.
        """
        client = self._coordinator.client
        if not client:
            return

        device_by_id = client.device_registry.device_by_id

        # Force an immediate discovery cycle for all known devices.
        # This sends any due RQ commands right away instead of waiting
        # for the poller's next scheduled cycle.
        # NOTE: devices with zero discovery cmds (TRV, DHW sensor, THM, etc.)
        # will be created but not actively probed — they are verified only
        # when they send traffic or the CTL's 000C response reveals them.
        probed = 0
        zero_cmds = 0
        for device_id in created + already_present:
            dev = device_by_id.get(device_id)
            if dev is None:
                continue
            if client.hgi and device_id == client.hgi.id:
                continue
            if not dev.discovery.cmds:
                zero_cmds += 1
                continue
            try:
                await dev.discovery.discover()
                probed += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Discovery cycle failed for %s: %s", device_id, err)

        _LOGGER.info(
            "Discovery cycle complete: %d devices probed, %d newly created, "
            "%d with zero discovery cmds (passive only), %d HGI skipped",
            probed,
            len(created),
            zero_cmds,
            len(zero_cmds_skip or []),
        )

        # TODO: Phase 3 — when ramses_rf exposes TopologyChangedEvent via an
        # external callback API, listen to it here to trigger entity creation
        # reactively instead of polling _discover_new_entities() on a timer.
        # The minimal API would be:
        #   client.register_topology_event_callback(self._on_topology_event)
        # This depends on the ramses_rf CQRS event bus work.

        # Trigger entity discovery to pick up any new devices
        await self._coordinator._discover_new_entities()  # noqa: SLF001

        # Schedule a refresh to update entities
        self._schedule_refresh_later()

    # ------------------------------------------------------------------
    # Passive device scan services
    # ------------------------------------------------------------------

    async def async_get_discovered_devices(self, call: ServiceCall) -> None:
        """Handle the get_discovered_devices service call.

        Returns the list of discovered devices via fire_event so callers
        (scripts, automations, ramses_extras card) can consume it.

        :param call: The service call with optional status/enabled filters.
        :raises HomeAssistantError: If the discovery manager is not running.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError(
                "Passive device scan is not enabled. "
                "Enable it in the integration's advanced features."
            )

        from .discovery import DiscoveryStatus

        status_str = call.data.get("status")
        status = DiscoveryStatus(status_str) if status_str else None
        enabled = call.data.get("enabled")

        entries = self._coordinator.discovery_manager.get_devices(
            status=status, enabled=enabled
        )

        _LOGGER.info(
            "get_discovered_devices: found %d device(s) (filter: status=%s, enabled=%s)",
            len(entries),
            status_str,
            enabled,
        )
        for entry in entries:
            dev = entry.device
            mismatch = entry.metadata.class_mismatch
            _LOGGER.info(
                "  %s: type=%s, confidence=%s, status=%s, enabled=%s%s",
                dev.device_id,
                dev.likely_type,
                dev.confidence,
                entry.metadata.status.value,
                entry.metadata.enabled,
                f", class_mismatch={mismatch}" if mismatch else "",
            )

        # Fire an event with the results for automations/scripts
        self.hass.bus.async_fire(
            f"{DOMAIN}_discovered_devices",
            {"devices": [e.to_dict() for e in entries]},
        )

    async def async_accept_discovered_device(self, call: ServiceCall) -> None:
        """Handle the accept_discovered_device service call.

        Accepts a discovered device, auto-generates a schema entry (if
        not provided), merges it into the config entry schema, adds the
        device to the known_list (so enforce_known_list allows it), and
        triggers discover_known_devices to create the entity.

        :param call: The service call with device_id and optional
            owner/schema_entry/ctl_id.
        :raises HomeAssistantError: If the discovery manager is not running.
        :raises ServiceValidationError: If the device is not in the discovery list.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError("Passive device scan is not enabled")

        device_id = call.data["device_id"]
        owner = call.data.get("owner")
        schema_entry = call.data.get("schema_entry")
        ctl_id = call.data.get("ctl_id")

        # Auto-detect CTL from the existing schema if not provided, so that
        # OTB/BDR/DHW devices are placed correctly (appliance_control,
        # hotwater_valve, etc.) instead of in orphans_heat.
        if not ctl_id:
            config_schema = self._coordinator.options.get(CONF_SCHEMA, {})
            if isinstance(config_schema, dict):
                main_tcs = config_schema.get("main_tcs")
                if isinstance(main_tcs, str):
                    ctl_id = main_tcs
        # Fall back to the runtime client's main TCS (issue 834:
        # main_tcs may not be in the config entry options yet if
        # sync_topology hasn't run since the last profile reload —
        # the coordinator only writes main_tcs to the config entry
        # during async_save_client_state → sync_learned_topology).
        if not ctl_id and self._coordinator.client:
            tcs = getattr(self._coordinator.client, "tcs", None)
            if tcs and isinstance(getattr(tcs, "id", None), str):
                ctl_id = tcs.id
        _LOGGER.debug(
            "accept_discovered_device: device_id=%s, ctl_id=%s, "
            "client=%s, config_main_tcs=%s",
            device_id,
            ctl_id,
            self._coordinator.client is not None,
            self._coordinator.options.get(CONF_SCHEMA, {}).get("main_tcs")
            if isinstance(self._coordinator.options.get(CONF_SCHEMA, {}), dict)
            else None,
        )

        try:
            entry = self._coordinator.discovery_manager.accept_device(
                device_id,
                owner=owner,
                schema_entry=schema_entry,
                ctl_id=ctl_id,
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

        # Merge the generated/provided schema entry into the coordinator's
        # local options and add the device to the known_list + runtime include
        # lists so enforce_known_list allows it.
        if entry and entry.metadata.schema_entry:
            self._apply_schema_entry(
                entry.metadata.schema_entry, device_id, owner=owner
            )

        # Persist the updated options to the config entry immediately.
        # We suppress the reload by setting a timestamp flag that the update
        # listener checks — the running coordinator already has the updated
        # options, so a reload would be disruptive (tears down the transport
        # while pending tasks are in flight).
        #
        # NOTE: async_update_entry schedules the update listener as an async
        # task, not a synchronous call.  Using a timestamp (checked with a
        # 5-second window in the update listener) avoids the race condition
        # where a boolean flag is reset before the listener runs.
        if entry and entry.metadata.schema_entry:
            import time as time_mod

            self._coordinator._suppress_reload = time_mod.time()  # noqa: SLF001
            self.hass.config_entries.async_update_entry(
                self._coordinator.entry, options=self._coordinator.options
            )

        # Trigger discovery for this specific device (entities created here)
        _LOGGER.info("Accepted discovered device: %s, triggering discovery", device_id)
        await self.async_discover_known_devices(
            _MockServiceCall({"device_id": device_id})
        )

    def _apply_schema_entry(
        self, fragment: dict[str, Any], device_id: str, *, owner: str | None = None
    ) -> None:
        """Apply a schema fragment to the coordinator's local options.

        Smart-merges the fragment into the schema.  If the device already
        exists somewhere in the schema (orphans, a zone, DHW), it is removed
        from the old location before merging the new fragment — this prevents
        duplicate entries and overwriting existing zone sensors.

        The known_list is now auto-derived from the schema at client creation
        time, so we only need to add the device to the user-known_list if
        there are trait overrides (e.g. owner/alias).  Also updates the
        running ramses_rf client's include lists so that enforce_known_list
        allows packet processing and device creation.

        Does NOT update the config entry (caller does that separately to
        control when the reload happens).

        :param fragment: A partial schema dict (e.g. from generate_schema_entry).
        :param device_id: The device ID being accepted.
        :param owner: Optional owner label (stored as alias in known_list overrides).
        """
        from ramses_rf.helpers import deep_merge

        from .schemas import remove_device_from_schema

        # 1. Remove device from old location, then merge fragment
        #    deep_merge(src, dst) — src takes precedence, so fragment is src
        #    to ensure the new placement wins.  User-authored keys (_name,
        #    _class, etc.) stay because the fragment doesn't contain them.
        current_options = dict(self._coordinator.options)
        current_schema: dict[str, Any] = dict(current_options.get(CONF_SCHEMA, {}))

        # Safeguard: if the device already has a root entry in the schema
        # (e.g. added manually via the schema editor), do NOT let the
        # auto-generated fragment overwrite user-configured keys like
        # _class, remotes, _commands, etc.  The fragment is only needed
        # to place the device in the right location (orphans, remotes[],
        # zones, etc.) — the root entry is already correct.
        existing_root = current_schema.get(device_id)
        has_existing_root = isinstance(existing_root, dict) and bool(existing_root)

        cleaned = remove_device_from_schema(current_schema, device_id)

        # Loop prevention (issue 834 comment 5044906835): if the fragment
        # would place device_id into a single-slot role (appliance_control,
        # hotwater_valve, heating_valve, sensor) that is already held by a
        # *different* device, strip the conflicting key and redirect the
        # new device to orphans_heat.  Without this, deep_merge overwrites
        # the existing device, which becomes an orphan and gets re-discovered
        # — causing an infinite discovery loop.
        fragment = _resolve_single_slot_conflicts(fragment, cleaned, device_id)

        if has_existing_root:
            # Strip the device's root entry from the fragment so deep_merge
            # doesn't overwrite it.  The fragment still has placement keys
            # (orphans_hvac, remotes[], etc.) which are merged normally.
            fragment = {k: v for k, v in fragment.items() if k != device_id}
            # Ensure the existing root entry is preserved in cleaned
            # (remove_device_from_schema keeps it, but be explicit)
            if device_id not in cleaned:
                cleaned[device_id] = existing_root

        merged = deep_merge(fragment, cleaned)

        # 1b. Clear _skipped flag — the device is being accepted, so it
        #     should no longer be marked as skipped.  deep_merge can't
        #     remove keys, so we do it explicitly here.
        #     Also clear stale _comment — refresh_device_comments will
        #     regenerate it from the scan engine's latest data.
        for dev_key in merged:
            if not isinstance(dev_key, str) or not dev_key.startswith(
                (
                    "01:",
                    "02:",
                    "04:",
                    "07:",
                    "10:",
                    "12:",
                    "13:",
                    "17:",
                    "22:",
                    "23:",
                    "30:",
                    "34:",
                    "37:",
                )
            ):
                continue
            entry = merged.get(dev_key)
            if not isinstance(entry, dict):
                continue
            # Clear _skipped for the device being accepted and for any
            # devices that are referenced in the fragment (e.g. zone
            # sensors/actuators that were placed by generate_schema_entry)
            if dev_key == device_id or _device_in_fragment(fragment, dev_key):
                entry.pop(SZ_TR_SKIPPED, None)
                # Clear stale _comment — device_comments is the canonical
                # source, refreshed by refresh_device_comments
                entry.pop("_comment", None)

        current_options[CONF_SCHEMA] = merged

        # 2. Only add to known_list if there are trait overrides (e.g. alias).
        #    The known_list is auto-derived from the schema, so we don't need
        #    to add the device ID just for enforce_known_list — that happens
        #    automatically.  We only keep user overrides here.
        if owner:
            current_known: dict[str, Any] = dict(current_options.get(SZ_KNOWN_LIST, {}))
            if device_id not in current_known:
                current_known[device_id] = {}
            current_known[device_id]["alias"] = owner
            current_options[SZ_KNOWN_LIST] = current_known

        # Update the coordinator's local copy so discover_known_devices sees it
        self._coordinator.options = current_options

        # 3. Add to the running ramses_rf client's include lists so
        #    enforce_known_list allows packet processing and device creation
        client = self._coordinator.client
        if client:
            engine = getattr(client, "_engine", None)
            if engine and device_id not in engine._include:
                engine._include.append(device_id)
            dev_filter = getattr(client, "_device_filter", None)
            if dev_filter and device_id not in dev_filter._include:
                dev_filter._include.append(device_id)

        _LOGGER.debug(
            "Applied schema fragment for %s (known_list auto-derived from schema)",
            device_id,
        )

    async def _remove_device_from_config(self, device_id: str) -> None:
        """Remove a device from the config schema + known_list + ramses_rf.

        After this, the passive scan can re-discover the device if it is
        still sending traffic.  The device is removed from:

        - ``CONF_SCHEMA`` (top-level key + orphan lists + zone references)
        - ``SZ_KNOWN_LIST`` (user overrides only — the auto-derived part
          follows from the schema)
        - ramses_rf's engine/device_filter include lists
        - ramses_rf's device registry (so ``_is_known`` returns False)

        The config entry is updated via ``async_update_entry`` which
        triggers a reload (unless suppressed).
        """
        from .schemas import remove_device_from_schema

        current_options = dict(self._coordinator.options)
        current_schema: dict[str, Any] = dict(current_options.get(CONF_SCHEMA, {}))

        # 1. Remove from all schema locations (orphan lists, zones, etc.)
        cleaned = remove_device_from_schema(current_schema, device_id)
        # 2. Also remove the device's own top-level key
        cleaned.pop(device_id, None)
        current_options[CONF_SCHEMA] = cleaned

        # 3. Remove from known_list (user overrides)
        current_known: dict[str, Any] = dict(current_options.get(SZ_KNOWN_LIST, {}))
        current_known.pop(device_id, None)
        if current_known:
            current_options[SZ_KNOWN_LIST] = current_known
        else:
            current_options.pop(SZ_KNOWN_LIST, None)

        # 4. Remove from ramses_rf's include lists so enforce_known_list
        #    stops processing its packets
        client = self._coordinator.client
        if client:
            engine = getattr(client, "_engine", None)
            if engine and device_id in engine._include:
                engine._include.remove(device_id)
            dev_filter = getattr(client, "_device_filter", None)
            if dev_filter and device_id in dev_filter._include:
                dev_filter._include.remove(device_id)
            # 5. Remove from ramses_rf device registry for cleanliness —
            #    _is_known() no longer checks the registry (SSOT, issue 767),
            #    but a stale ghost entry could still receive state updates
            #    via other code paths.
            dev_registry = getattr(client, "device_registry", None)
            if dev_registry and device_id in dev_registry.device_by_id:
                dev = dev_registry.device_by_id.get(device_id)
                if dev and hasattr(dev_registry, "remove_device"):
                    with contextlib.suppress(Exception):
                        dev_registry.remove_device(dev)

        # Update coordinator's local copy
        self._coordinator.options = current_options

        # 6. Persist to config entry (triggers reload)
        if self._coordinator.entry:
            import time as time_mod

            self._coordinator._suppress_reload = time_mod.time()
            self.hass.config_entries.async_update_entry(
                self._coordinator.entry, options=current_options
            )

        _LOGGER.info(
            "Removed device %s from schema + known_list (will be re-discovered "
            "if still active)",
            device_id,
        )

    async def async_discard_discovered_device(self, call: ServiceCall) -> None:
        """Handle the discard_discovered_device service call.

        Discards the device from discovery and removes it from the schema
        so the scan can re-discover it if still active.

        :param call: The service call with device_id.
        :raises HomeAssistantError: If the discovery manager is not running.
        :raises ServiceValidationError: If the device is not in the discovery list.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError("Passive device scan is not enabled")

        device_id = call.data["device_id"]
        try:
            self._coordinator.discovery_manager.discard_device(device_id)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

        # Remove from schema + known_list so scan re-discovers it
        await self._remove_device_from_config(device_id)

    async def async_remove_discovered_device(self, call: ServiceCall) -> None:
        """Handle the remove_discovered_device service call.

        Removes the device from the schema, known_list, and ramses_rf's
        include lists so the scan can re-discover it if still active.

        :param call: The service call with device_id.
        :raises HomeAssistantError: If the discovery manager is not running.
        :raises ServiceValidationError: If the device is not in the discovery list.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError("Passive device scan is not enabled")

        device_id = call.data["device_id"]
        try:
            self._coordinator.discovery_manager.remove_device(device_id)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

        # Remove from schema + known_list so scan re-discovers it
        await self._remove_device_from_config(device_id)

    async def async_enable_discovered_device(self, call: ServiceCall) -> None:
        """Handle the enable_discovered_device service call.

        :param call: The service call with device_id.
        :raises HomeAssistantError: If the discovery manager is not running.
        :raises ServiceValidationError: If the device is not in the discovery list.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError("Passive device scan is not enabled")

        device_id = call.data["device_id"]
        try:
            self._coordinator.discovery_manager.enable_device(device_id)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_disable_discovered_device(self, call: ServiceCall) -> None:
        """Handle the disable_discovered_device service call.

        :param call: The service call with device_id.
        :raises HomeAssistantError: If the discovery manager is not running.
        :raises ServiceValidationError: If the device is not in the discovery list.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError("Passive device scan is not enabled")

        device_id = call.data["device_id"]
        try:
            self._coordinator.discovery_manager.disable_device(device_id)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_add_faked_rem(self, call: ServiceCall) -> None:
        """Handle the add_faked_rem service call.

        Creates a faked REM entry for sending commands to a FAN.
        Merges the schema entry (with _faked, _bound, _class traits)
        into the config entry, persists it, and triggers entity creation.

        :param call: The service call with device_id, bound_to, and optional alias.
        :raises HomeAssistantError: If the discovery manager is not running.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError("Passive device scan is not enabled")

        device_id = call.data["device_id"]
        bound_to = call.data["bound_to"]
        alias = call.data.get("alias")

        entry = self._coordinator.discovery_manager.add_faked_rem(
            device_id, bound_to=bound_to, alias=alias
        )

        # Merge the schema entry into the coordinator's options and
        # persist to the config entry (same pattern as accept_discovered_device).
        if entry and entry.metadata.schema_entry:
            self._apply_schema_entry(entry.metadata.schema_entry, device_id)

            import time as time_mod

            self._coordinator._suppress_reload = time_mod.time()  # noqa: SLF001
            self.hass.config_entries.async_update_entry(
                self._coordinator.entry, options=self._coordinator.options
            )

        _LOGGER.info("Added faked REM %s bound to %s", device_id, bound_to)

        # Trigger discovery for this specific device (entity created here)
        await self.async_discover_known_devices(
            _MockServiceCall({"device_id": device_id})
        )

    async def async_remove_device(self, call: ServiceCall) -> None:
        """Handle the remove_device service call.

        Removes a device from the schema (zones, orphans, main_tcs, DHW,
        HVAC remotes/sensors), from the known_list, and from the HA device
        registry.  This is a clean removal for devices that have been
        replaced, are no longer present, or were added by mistake.

        Unlike ``remove_discovered_device`` (which only marks a discovered
        device as removed in the discovery metadata), this service removes
        the device from the actual schema and HA registries — the device
        will not reappear on restart.

        The HGI (gateway) cannot be removed — it is always required for the
        integration to function.

        :param call: The service call with ``device_id``.
        :raises ServiceValidationError: If the device_id is the HGI or not
            found in the schema.
        """
        from .schemas import remove_device_from_schema

        device_id = call.data["device_id"]

        # The HGI is the gateway — removing it would break the integration.
        # It is always in the known_list (see coordinator safety net) and
        # must not be removed.
        config_entry = self._coordinator.entry
        options = dict(self._coordinator.options)
        schema: dict[str, Any] = dict(options.get(CONF_SCHEMA, {}))
        known_list: dict[str, Any] = dict(options.get(SZ_KNOWN_LIST, {}))

        # Check if the device is the HGI (main_tcs is a CTL, not HGI, but
        # the HGI has class=HGI in known_list overrides or is the only 18:
        # device).  We check by looking at the known_list for class=HGI.
        dev_override = known_list.get(device_id, {})
        if (
            isinstance(dev_override, dict)
            and str(dev_override.get("class", "")).upper() == "HGI"
        ):
            raise ServiceValidationError(
                f"Cannot remove the HGI gateway device ({device_id})"
            )

        # Check if the device exists anywhere in the schema
        schema_str = str(schema)
        if device_id not in schema_str and device_id not in known_list:
            raise ServiceValidationError(
                f"Device {device_id} not found in schema or known_list"
            )

        # 1. Remove from schema (zones, orphans, DHW, HVAC, appliance_control)
        cleaned = remove_device_from_schema(schema, device_id)

        # Also remove the device's own top-level key (e.g. "32:153289": {})
        # remove_device_from_schema deliberately keeps it so a new fragment
        # can be merged, but for a full removal we delete it.
        if device_id in cleaned:
            del cleaned[device_id]

        # Clear main_tcs if it points to this device
        if cleaned.get(SZ_MAIN_TCS) == device_id:
            cleaned.pop(SZ_MAIN_TCS, None)

        options[CONF_SCHEMA] = cleaned

        # 2. Remove from known_list
        if device_id in known_list:
            known_list.pop(device_id, None)
            options[SZ_KNOWN_LIST] = known_list

        # 3. Persist to config entry (suppress reload — coordinator will
        #    be reloaded by the caller if needed, or the device simply
        #    disappears on next restart)
        import time as time_mod

        self._coordinator.options = options
        self._coordinator._suppress_reload = time_mod.time()  # noqa: SLF001
        self.hass.config_entries.async_update_entry(config_entry, options=options)

        # 4. Remove from HA device registry
        dev_reg = dr.async_get(self.hass)
        if config_entry.entry_id is not None:
            for dev_entry in dr.async_entries_for_config_entry(
                dev_reg, config_entry.entry_id
            ):
                for domain, dev_id in dev_entry.identifiers:
                    if domain == DOMAIN and str(dev_id) == device_id:
                        dev_reg.async_remove_device(dev_entry.id)
                        _LOGGER.info(
                            "Removed HA device registry entry for %s", device_id
                        )
                        break

        # 5. Remove from running ramses_rf client's include lists so
        #    enforce_known_list stops allowing packets for this device
        client = self._coordinator.client
        if client:
            engine = getattr(client, "_engine", None)
            if engine and device_id in engine._include:  # noqa: SLF001
                engine._include.remove(device_id)  # noqa: SLF001
            dev_filter = getattr(client, "_device_filter", None)
            if dev_filter and device_id in dev_filter._include:  # noqa: SLF001
                dev_filter._include.remove(device_id)  # noqa: SLF001

        _LOGGER.info("Removed device %s from schema and registries", device_id)

"""Service Handler for RAMSES integration."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later

from ramses_rf.device import Fakeable
from ramses_rf.exceptions import BindingFlowFailed
from ramses_tx.address import pkt_addrs
from ramses_tx.command import Command
from ramses_tx.exceptions import PacketAddrSetInvalid

from .const import _2411_PARAMS_SCHEMA, DOMAIN

if TYPE_CHECKING:
    from .broker import RamsesBroker

_LOGGER = logging.getLogger(__name__)

_CALL_LATER_DELAY: Final = 5  # needed for tests
_DEVICE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-F]{2}:[0-9A-F]{6}$", re.I)


class RamsesServiceHandler:
    """Handler for RAMSES integration service calls."""

    def __init__(self, broker: RamsesBroker) -> None:
        """Initialize the Service Handler."""
        self._broker = broker
        self.hass = broker.hass

    async def async_bind_device(self, call: ServiceCall) -> None:
        """Handle the bind_device service call to bind a device to the system."""
        device: Fakeable

        try:
            device = self._broker.client.fake_device(call.data["device_id"])
        except LookupError as err:
            _LOGGER.error("%s", err)
            raise HomeAssistantError(
                f"Device not found: {call.data.get('device_id')}"
            ) from err

        cmd = Command(call.data["device_info"]) if call.data["device_info"] else None

        _LOGGER.warning("Starting binding process for device %s", device.id)

        try:
            await device._initiate_binding_process(
                list(call.data["offer"].keys()),
                confirm_code=list(call.data["confirm"].keys()),
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

        async_call_later(self.hass, _CALL_LATER_DELAY, self._broker.async_update)

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Create and send a raw command packet via the transport layer."""
        kwargs = dict(call.data.items())  # is ReadOnlyDict
        if (
            call.data["device_id"] == "18:000730"
            and kwargs.get("from_id", "18:000730") == "18:000730"
            and self._broker.client.hgi.id
        ):
            kwargs["device_id"] = self._broker.client.hgi.id

        cmd = self._broker.client.create_cmd(**kwargs)

        self._adjust_sentinel_packet(cmd)

        await self._broker.client.async_send_cmd(cmd)
        async_call_later(self.hass, _CALL_LATER_DELAY, self._broker.async_update)

    def _adjust_sentinel_packet(self, cmd: Command) -> None:
        """Fix address positioning for specific sentinel packets (18:000730)."""
        # HACK: to fix the device_id when GWY announcing.
        if cmd.src.id != "18:000730" or cmd.dst.id != self._broker.client.hgi.id:
            return

        try:
            # Validate if the current address structure is acceptable
            pkt_addrs(self._broker.client.hgi.id + cmd._frame[16:37])
        except PacketAddrSetInvalid:
            # If invalid, swap addr1 and addr2 to correct the structure
            cmd._addrs[1], cmd._addrs[2] = cmd._addrs[2], cmd._addrs[1]
            cmd._repr = None  # Invalidate cached representation
            _LOGGER.debug(
                "Swapped addresses for sentinel packet 18:000730 to maintain protocol validity"
            )

    async def async_get_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Handle 'get_fan_param' dict."""
        entity = None  # Ensure entity is defined for finally/except blocks

        try:
            data = self._normalize_service_call(call)

            _LOGGER.debug("Processing get_fan_param service call with data: %s", data)

            # Extract id's
            original_device_id, normalized_device_id, from_id = (
                self._get_device_and_from_id(data)
            )
            param_id = self._get_param_id(data)

            # If no from_id or a bound device was found then try gateway HGI
            if not from_id and original_device_id:
                gateway_id = getattr(
                    getattr(self._broker.client, "hgi", None), "id", None
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

            # Check if we got valid source device info
            if not all([original_device_id, normalized_device_id, from_id]):
                _LOGGER.warning(
                    "Cannot get parameter: No valid source device available from %s. "
                    "Need either: explicit from_id, or a REM/DIS device that was 'bound' in the configuration.",
                    data,
                )
                return

            # Find the corresponding entity and set it to pending
            entity = self._broker.fan_handler.find_param_entity(
                normalized_device_id, param_id
            )
            if entity and hasattr(entity, "set_pending"):
                entity.set_pending()

            cmd = Command.get_fan_param(original_device_id, param_id, src_id=from_id)
            _LOGGER.debug("Sending command: %s", cmd)

            # Send the command directly using the gateway
            await self._broker.client.async_send_cmd(cmd)

            # Clear pending state after timeout (non-blocking)
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(30))
        except ValueError as err:
            # Log validation errors but don't re-raise them for edge cases
            _LOGGER.error("Failed to get fan parameter: %s", err)
            # Clear pending state immediately on validation error
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(0))
            return
        except Exception as err:
            _LOGGER.error("Failed to get fan parameter: %s", err, exc_info=True)
            # Clear pending state on error
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(0))
            # Raise friendly error for UI
            raise HomeAssistantError(f"Failed to get fan parameter: {err}") from err

    async def get_all_fan_params(self, call: dict[str, Any] | ServiceCall) -> None:
        """Wrapper for _async_run_fan_param_sequence."""
        self.hass.loop.create_task(self._async_run_fan_param_sequence(call))

    async def _async_run_fan_param_sequence(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Handle 'update_fan_params' service call (or direct dict)."""
        try:
            data = self._normalize_service_call(call)
            _LOGGER.debug(
                "Processing update_fan_params service call with data: %s", data
            )
        except Exception as err:
            _LOGGER.error("Invalid service call data: %s", err)
            return

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

            except Exception as err:
                _LOGGER.error(
                    "Failed to get fan parameter %s for device: %s", param_id, err
                )
                continue

    async def async_set_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Handle 'set_fan_param' service call (or direct dict)."""
        entity = None

        try:
            data = self._normalize_service_call(call)

            _LOGGER.debug("Processing set_fan_param service call with data: %s", data)

            original_device_id, normalized_device_id, from_id = (
                self._get_device_and_from_id(data)
            )

            if not all([original_device_id, normalized_device_id, from_id]):
                msg = (
                    f"Cannot set parameter: No valid source device available from {data}. "
                    "Need either: explicit from_id, or a REM/DIS device that was 'bound' in the configuration."
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

            entity = self._broker.fan_handler.find_param_entity(
                normalized_device_id, param_id
            )
            if entity and hasattr(entity, "set_pending"):
                entity.set_pending()

            cmd = Command.set_fan_param(
                original_device_id, param_id, value, src_id=from_id
            )
            await self._broker.client.async_send_cmd(cmd)
            await asyncio.sleep(0.2)

            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(30))

        except ValueError as err:
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(0))
            raise HomeAssistantError(
                f"Invalid parameter for set_fan_param: {err}"
            ) from err
        except Exception as err:
            _LOGGER.error("Failed to set fan parameter: %s", err, exc_info=True)
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(0))
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

        device = self._broker._get_device(device_id)
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
        if isinstance(call, dict):
            data = dict(call)
        elif hasattr(call, "data"):
            data = dict(call.data)
        else:
            data = dict(call)

        target = getattr(call, "target", None)
        if target:
            if hasattr(target, "as_dict"):
                data["target"] = target.as_dict()
            elif isinstance(target, dict):
                data["target"] = target

        return data

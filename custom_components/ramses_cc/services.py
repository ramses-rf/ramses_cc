"""Service Handler for RAMSES integration."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Final, cast

from homeassistant.core import ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later

from ramses_rf.devices import Fakeable
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
from ramses_tx.command import Command
from ramses_tx.exceptions import (
    PacketAddrSetInvalid,
    ProtocolSendFailed,
    ProtocolTimeoutError,
    TransportError,
)

from .const import CONF_SCHEMA, DOMAIN, SZ_DISABLED_DEVICES, SZ_KNOWN_LIST

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)

_CALL_LATER_DELAY: Final = 5  # needed for tests
_DEVICE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-F]{2}:[0-9A-F]{6}$", re.I)


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

    @callback
    def _schedule_refresh(self, _: Any) -> None:
        """Schedule a coordinator refresh.

        :param _: Unused argument (required for async_call_later callback signature).
        """
        self.hass.async_create_task(self._coordinator.async_request_refresh())

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

        cmd = Command(call.data["device_info"]) if call.data["device_info"] else None

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
        async_call_later(
            self.hass,
            _CALL_LATER_DELAY,
            self._schedule_refresh,
        )

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

        self._adjust_sentinel_packet(cmd)

        try:
            await self._coordinator.client.async_send_cmd(cmd)
        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            raise HomeAssistantError(f"Failed to send packet: {err}") from err

        async_call_later(
            self.hass,
            _CALL_LATER_DELAY,
            self._schedule_refresh,
        )

    def _adjust_sentinel_packet(self, cmd: Command) -> None:
        """Fix address positioning for specific sentinel packets (18:000730)."""
        # HACK: to fix the device_id when GWY announcing.
        if not self._coordinator.client:
            raise HomeAssistantError(
                "Cannot set parameter: RAMSES RF client is not initialized"
            )
        hgi = self._coordinator.client.hgi
        if not hgi or not hgi.id:
            return

        if cmd.src.id != "18:000730" or cmd.dst.id != hgi.id:
            return

        try:
            # Validate if the current address structure is acceptable without slicing
            addr1 = cmd._addrs[1].id if len(cmd._addrs) > 1 else "--:------"
            addr2 = cmd._addrs[2].id if len(cmd._addrs) > 2 else "--:------"

            pkt_addrs(f"{hgi.id} {addr1} {addr2}")
        except PacketAddrSetInvalid:
            # If invalid, swap addr1 and addr2 to correct the structure safely
            if isinstance(cmd._addrs, list):
                cmd._addrs[1], cmd._addrs[2] = cmd._addrs[2], cmd._addrs[1]
            else:
                cmd._addrs = (cmd._addrs[0], cmd._addrs[2], cmd._addrs[1])

            cast(Any, cmd)._repr = None  # Invalidate cached representation
            _LOGGER.debug(
                "Swapped addresses for sentinel packet 18:000730 to maintain protocol validity"
            )

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

            cmd = Command.get_fan_param(original_device_id, param_id, src_id=from_id)
            _LOGGER.debug("Sending command: %s", cmd)

            # Send the command directly using the gateway
            await self._coordinator.client.async_send_cmd(cmd)

            # Clear pending state after timeout (non-blocking)
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(30)
                )

        except ServiceValidationError:
            # Bubble up validation errors directly to the UI
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(0)
                )
            raise

        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            # Raise friendly error for UI
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(0)
                )
            raise HomeAssistantError(f"Failed to get fan parameter: {err}") from err

        except ValueError as err:
            # Catch errors from helpers (e.g. _get_param_id) and raise friendly error
            _LOGGER.error("Failed to get fan parameter: %s", err)
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(0)
                )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="service_param_invalid",
                translation_placeholders={"err": str(err)},
            ) from err

        except Exception as err:
            _LOGGER.error("Failed to get fan parameter: %s", err, exc_info=True)
            # Clear pending state on error
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(0)
                )
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

            cmd = Command.set_fan_param(
                original_device_id, param_id, value, src_id=from_id
            )
            await self._coordinator.client.async_send_cmd(cmd)
            await asyncio.sleep(0.2)

            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(30)
                )

        except (
            ProtocolSendFailed,
            ProtocolTimeoutError,
            TimeoutError,
            TransportError,
        ) as err:
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(0)
                )
            raise HomeAssistantError(f"Failed to set fan parameter: {err}") from err
        except ValueError as err:
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(0)
                )
            raise HomeAssistantError(
                f"Invalid parameter for set_fan_param: {err}"
            ) from err
        except Exception as err:
            _LOGGER.error("Failed to set fan parameter: %s", err, exc_info=True)
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                self.hass.async_create_task(
                    cast(Any, entity)._clear_pending_after_timeout(0)
                )
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
        - <FAN device_id>: a VCS dict with remotes, sensors
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
                SZ_DISABLED_DEVICES,
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

            # HVAC VCS structure: remotes, sensors
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

        # Skip disabled devices (declined via discovery review)
        disabled: set[str] = set(config_schema.get(SZ_DISABLED_DEVICES, []))
        all_device_ids -= disabled

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
        self.hass.async_create_task(
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
        async_call_later(
            self.hass,
            _CALL_LATER_DELAY,
            self._schedule_refresh,
        )

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
            _LOGGER.info(
                "  %s: type=%s, confidence=%s, status=%s, enabled=%s",
                dev.device_id,
                dev.likely_type,
                dev.confidence,
                entry.metadata.status.value,
                entry.metadata.enabled,
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
        # We suppress the reload by wrapping in a flag that the update
        # listener checks — the running coordinator already has the updated
        # options, so no reload is needed.
        if entry and entry.metadata.schema_entry:
            self._coordinator._suppress_reload = True  # noqa: SLF001
            self.hass.config_entries.async_update_entry(
                self._coordinator.entry, options=self._coordinator.options
            )
            self._coordinator._suppress_reload = False  # noqa: SLF001

        # Trigger discovery for this specific device (entities created here)
        _LOGGER.info("Accepted discovered device: %s, triggering discovery", device_id)
        await self.async_discover_known_devices(
            _MockServiceCall({"device_id": device_id})
        )

    def _apply_schema_entry(
        self, fragment: dict[str, Any], device_id: str, *, owner: str | None = None
    ) -> None:
        """Apply a schema fragment to the coordinator's local options.

        Deep-merges the fragment into the schema.  The known_list is now
        auto-derived from the schema at client creation time, so we only
        need to add the device to the user-known_list if there are trait
        overrides (e.g. owner/alias).  Also updates the running ramses_rf
        client's include lists so that enforce_known_list allows packet
        processing and device creation.

        Does NOT update the config entry (caller does that separately to
        control when the reload happens).

        :param fragment: A partial schema dict (e.g. from generate_schema_entry).
        :param device_id: The device ID being accepted.
        :param owner: Optional owner label (stored as alias in known_list overrides).
        """
        from ramses_rf.helpers import deep_merge

        # 1. Merge schema into local options
        current_options = dict(self._coordinator.options)
        current_schema: dict[str, Any] = dict(current_options.get(CONF_SCHEMA, {}))
        merged = deep_merge(current_schema, fragment)
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

    async def async_discard_discovered_device(self, call: ServiceCall) -> None:
        """Handle the discard_discovered_device service call.

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

    async def async_remove_discovered_device(self, call: ServiceCall) -> None:
        """Handle the remove_discovered_device service call.

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

        :param call: The service call with device_id, bound_to, and optional alias.
        :raises HomeAssistantError: If the discovery manager is not running.
        """
        if not self._coordinator.discovery_manager:
            raise HomeAssistantError("Passive device scan is not enabled")

        device_id = call.data["device_id"]
        bound_to = call.data["bound_to"]
        alias = call.data.get("alias")

        self._coordinator.discovery_manager.add_faked_rem(
            device_id, bound_to=bound_to, alias=alias
        )

        _LOGGER.info("Added faked REM %s bound to %s", device_id, bound_to)

"""Fan Logic Handler for RAMSES integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta as td
from typing import TYPE_CHECKING, Any

from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from ramses_rf.const import DevType
from ramses_rf.devices import Device, HvacRemoteBase, HvacVentilator
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_tx.dtos import CommandDTO
from ramses_tx.typing import DeviceIdT

from .const import CONF_SCHEMA, DOMAIN, SIGNAL_NEW_DEVICES, SZ_TR_BOUND

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator
    from .entity import RamsesEntity

_LOGGER = logging.getLogger(__name__)


class RamsesFanHandler:
    """Handler for FAN (HVAC) specific logic, bindings, and parameters."""

    def __init__(self, coordinator: RamsesCoordinator) -> None:
        """Initialize the Fan Handler."""
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self._fan_bound_to_remote: dict[str, DeviceIdT] = {}

    def find_param_entity(self, device_id: str, param_id: str) -> RamsesEntity | None:
        """Find a parameter entity by device ID and parameter ID.

        Helper Method that searches for a number entity corresponding to a specific
        parameter on a device.

        The callers pass a *normalized* device_id (colons replaced with
        underscores, e.g. ``32_153289``) and an *uppercased* param_id
        (e.g. ``3D``).  The number platform, however, stores entities with
        the following unique_id formats:

        * **New** (since PR 581): ``"{device.id}-param_{key}"`` where
          ``device.id`` keeps its colons and ``key`` preserves the schema
          case — e.g. ``32:153289-param_3D``.
        * **Old** (pre-migration): ``"{normalized_id}_param_{param.lower()}"``
          — e.g. ``32_153289_param_3d``.

        We try the new format first and fall back to the old one so that
        entities that have not yet been migrated are still found.

        :param device_id: The ID of the device (normalized or raw,
            e.g. ``32_153289`` or ``32:153289``).
        :param param_id: The 2-character hex ID of the parameter (uppercased,
            e.g. ``3D``).
        :return: The found entity or None if not found in the registry/platform.
        """
        # Restore colons for the new unique_id format (device.id keeps colons)
        colon_device_id = str(device_id).replace("_", ":")
        normalized_device_id = str(device_id).replace(":", "_").lower()

        # New format: "32:153289-param_3D" (colons, schema case)
        new_unique_id = f"{colon_device_id}-param_{param_id}"
        # Old format: "32_153289_param_3d" (underscores, lowercase)
        old_unique_id = f"{normalized_device_id}_param_{param_id.lower()}"

        ent_reg = er.async_get(self.hass)
        entity_id = ent_reg.async_get_entity_id("number", DOMAIN, new_unique_id)
        if entity_id is None:
            entity_id = ent_reg.async_get_entity_id("number", DOMAIN, old_unique_id)
        if entity_id is None:
            _LOGGER.debug(
                "Entity (unique_id=%s or %s) not found in registry.",
                new_unique_id,
                old_unique_id,
            )
            return None

        _LOGGER.debug("Found entity %s in entity registry", entity_id)

        platforms = self.coordinator.platforms.get(Platform.NUMBER, [])
        for platform in platforms:
            if hasattr(platform, "entities") and entity_id in platform.entities:
                return platform.entities[entity_id]

        return None

    def create_parameter_entities(self, device: RamsesRFEntity) -> None:
        """Signal the number platform to create parameter entities for a device.

        The number platform handles entity creation via its device discovery callback.
        This method just signals that a new FAN device with 2411 support has been
        discovered.

        :param device: The ramses_rf device instance to create parameters for.
        """
        device_id = device.id
        _LOGGER.debug(
            "Signaling number platform about FAN device %s with 2411 support",
            device_id,
        )
        async_dispatcher_send(
            self.hass,
            SIGNAL_NEW_DEVICES.format(Platform.NUMBER),
            [device],
        )

    async def setup_fan_bound_devices(self, device: Device) -> None:
        """Set up bound devices for a FAN device.

        Checks the known_list configuration for devices bound to this FAN
        (REMotes or DIStribution units) and registers the binding in the
        underlying library.

        ``_bound`` accepts ``str | list[str]`` (multi-REM binding).
        Each bound device is registered via ``add_bound_device()``.

        :param device: The FAN device instance to configure bindings for.
        """
        # Only proceed if this is a FAN device
        if not isinstance(device, HvacVentilator):
            return

        # Phase 4: bound device IDs come from the schema _bound trait (SSOT).
        # The known_list override is no longer stored in config entry options.
        schema = self.coordinator.options.get(CONF_SCHEMA, {})
        schema_entry = schema.get(device.id, {})
        bound_value = None
        if isinstance(schema_entry, dict):
            bound_value = schema_entry.get(SZ_TR_BOUND)
        if not bound_value:
            return

        # Normalize to list — _bound accepts str | list[str]
        if isinstance(bound_value, str):
            bound_device_ids: list[str] = [bound_value]
        elif isinstance(bound_value, list):
            bound_device_ids = bound_value
        else:
            _LOGGER.warning(
                "Cannot bind device(s) to FAN %s: invalid bound type (%s)",
                device.id,
                type(bound_value),
            )
            return

        if not self.coordinator.client:
            _LOGGER.warning("Cannot look up bound device: Client not ready")
            return

        for bound_device_id in bound_device_ids:
            _LOGGER.info(
                "Binding FAN %s and REM/DIS device %s", device.id, bound_device_id
            )

            bound_device = self.coordinator._get_device(bound_device_id)

            if bound_device:
                # Determine the device type based on the class
                if isinstance(bound_device, HvacRemoteBase):
                    device_type = DevType.REM
                elif (
                    hasattr(bound_device, "_SLUG") and bound_device._SLUG == DevType.DIS
                ):
                    device_type = DevType.DIS
                else:
                    _LOGGER.warning(
                        "Cannot bind device %s of type %s to FAN %s: must be REM or DIS",
                        bound_device_id,
                        getattr(bound_device, "_SLUG", "unknown"),
                        device.id,
                    )
                    continue

                # Add the bound device to the FAN's tracking
                device.add_bound_device(bound_device_id, device_type)
                _LOGGER.info(
                    "Bound FAN %s to %s device %s",
                    device.id,
                    device_type,
                    bound_device_id,
                )
                # add the HvacVentilator device id to the coordinator's dict
                self._fan_bound_to_remote[str(bound_device_id)] = device.id
            else:
                _LOGGER.warning(
                    "Bound device %s not found for FAN %s", bound_device_id, device.id
                )

    async def async_setup_fan_device(self, device: Device) -> None:
        """Set up a FAN device and its parameter entities.

        Configures bindings, sets up initialization callbacks for parameter
        discovery, and establishes parameter update callbacks for event firing.

        :param device: The device instance to set up.
        """
        _LOGGER.debug("Setting up device: %s", device.id)

        # For FAN devices, set up bound devices and parameter handling
        if hasattr(device, "_SLUG") and device._SLUG == "FAN":
            await self.setup_fan_bound_devices(device)

            # Set up the initialization callback - will be called on first message
            if hasattr(device, "set_initialized_callback"):

                async def on_fan_first_message() -> None:
                    """Handle the first message received from a FAN device."""
                    _LOGGER.debug(
                        "First message received from FAN %s, creating parameter entities",
                        device.id,
                    )
                    # Create parameter entities after first message is received
                    self.create_parameter_entities(device)
                    # Request all parameters after creating entities (non-blocking if fails)
                    _call: dict[str, DeviceIdT] = {
                        "device_id": device.id,
                    }
                    try:
                        self.coordinator.get_all_fan_params(_call)
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to request parameters for device %s during startup: %s. "
                            "Entities will still work for received parameter updates.",
                            device.id,
                            err,
                        )

                    # HACK: Force one time RQ of 10D0 - TODO(eb): remove when PR #632 is working
                    try:
                        cmd = CommandDTO(
                            verb="RQ",
                            addr1="18:000730",
                            addr2=device.id,
                            addr3="--:------",
                            code="10D0",
                            payload="00",
                        )
                        _LOGGER.debug("Poll 10D0 filter_remaining for %s", device.id)
                        await device._gateway.async_send_cmd(cmd)
                    except Exception as err:
                        _LOGGER.debug(
                            "Failed to poll filter_remaining for %s: %s",
                            device.id,
                            err,
                            exc_info=True,
                        )

                    # Start periodic 2411 parameter polling (every 6 hours).
                    # ramses_rf 0.58.3+ removed the discovery poll that used to
                    # do this; without it, parameter values go stale after the
                    # initial sweep.  See ramses_cc issue 851.
                    self._start_param_polling(device)

            set_init_cb = getattr(device, "set_initialized_callback", None)
            if callable(set_init_cb):
                set_init_cb(lambda: self.hass.async_create_task(on_fan_first_message()))

            # Set up parameter update callback
            set_param_cb = getattr(device, "set_param_update_callback", None)
            if callable(set_param_cb):
                # Create a closure to capture the current device_id
                def create_param_callback(dev_id: str) -> Callable[[str, Any], None]:
                    def param_callback(param_id: str, value: Any) -> None:
                        _LOGGER.debug(
                            "Parameter %s updated for device %s: %s (firing event)",
                            param_id,
                            dev_id,
                            value,
                        )
                        # Fire the event for Home Assistant entities
                        self.hass.bus.async_fire(
                            f"{DOMAIN}.fan_param_updated",
                            {"device_id": dev_id, "param_id": param_id, "value": value},
                        )

                    return param_callback

                set_param_cb(create_param_callback(device.id))
                _LOGGER.debug(
                    "Set up parameter update callback for device %s", device.id
                )

            # Fallback: if no initialized callback, request params immediately.
            # When the callback exists, param requests are deferred to
            # on_fan_first_message to avoid timeouts before the device is online.
            if not hasattr(device, "set_initialized_callback"):
                call: dict[str, Any] = {
                    "device_id": device.id,
                }
                try:
                    self.coordinator.get_all_fan_params(call)
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to request parameters for device %s during setup: %s. "
                        "Entities will still work for received parameter updates.",
                        device.id,
                        err,
                    )
                # Start periodic polling for devices without initialized callback too
                self._start_param_polling(device)

    def _start_param_polling(self, device: RamsesRFEntity) -> None:
        """Start periodic 2411 parameter polling for a FAN device.

        ramses_rf 0.58.3+ removed the discovery poll that used to refresh 2411
        parameter values daily.  Without periodic polling, values go stale
        after the initial sweep at startup.  This schedules a timer-based
        callback every 6 hours.  See ramses_cc issue 851.

        Uses ``async_track_time_interval`` (the HA-idiomatic pattern, also
        used by the coordinator for discovery and state-save timers) rather
        than a ``while True`` + ``asyncio.sleep`` loop.  The timer callback
        is fire-and-forget — it never blocks startup and is automatically
        cleaned up via ``entry.async_on_unload`` when the config entry is
        unloaded.

        :param device: The FAN device to poll periodically.
        """
        dev_id = device.id

        async def _poll_params(_: Any) -> None:
            """Request all 2411 parameters from the device."""
            _LOGGER.debug("Periodic 2411 poll for %s", dev_id)
            call: dict[str, Any] = {"device_id": dev_id}
            try:
                self.coordinator.get_all_fan_params(call)
            except Exception as err:
                _LOGGER.debug("Periodic param poll failed for %s: %s", dev_id, err)

        # async_track_time_interval returns a cancel callable.  Register it
        # for automatic cleanup on config entry unload — no manual
        # _stop_param_polling needed.
        cancel_cb = async_track_time_interval(self.hass, _poll_params, td(hours=6))
        self.coordinator.entry.async_on_unload(cancel_cb)

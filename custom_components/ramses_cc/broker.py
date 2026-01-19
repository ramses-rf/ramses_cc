"""Broker for RAMSES integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from copy import deepcopy
from datetime import datetime as dt, timedelta
from threading import Semaphore
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.event import async_track_time_interval

from ramses_rf.device import Device
from ramses_rf.device.hvac import HvacRemoteBase, HvacVentilator
from ramses_rf.entity_base import Child, Entity as RamsesRFEntity
from ramses_rf.gateway import Gateway
from ramses_rf.system import Evohome, System, Zone
from ramses_tx.const import Code
from ramses_tx.schemas import extract_serial_port

from .const import (
    CONF_COMMANDS,
    CONF_RAMSES_RF,
    CONF_SCAN_INTERVAL,
    CONF_SCHEMA,
    DOMAIN,
    SIGNAL_NEW_DEVICES,
    SIGNAL_UPDATE,
    SZ_CLIENT_STATE,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_PACKETS,
    SZ_REMOTES,
    SZ_SCHEMA,
    SZ_SERIAL_PORT,
)
from .fan_handler import RamsesFanHandler
from .schemas import merge_schemas, schema_is_minimal
from .services import RamsesServiceHandler
from .store import RamsesStore

if TYPE_CHECKING:
    from . import RamsesEntity

_LOGGER = logging.getLogger(__name__)

SAVE_STATE_INTERVAL: Final[timedelta] = timedelta(minutes=5)


class RamsesBroker:
    """Central coordinator for the RAMSES integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the RAMSES broker and its data structures."""
        self.hass = hass
        self.entry = entry
        self.options = deepcopy(dict(entry.options))
        self.store = RamsesStore(hass)
        self.fan_handler = RamsesFanHandler(self)
        self.service_handler = RamsesServiceHandler(self)

        _LOGGER.debug("Config = %s", entry.options)

        self.client: Gateway = None
        self._remotes: dict[str, dict[str, str]] = {}

        self._platform_setup_tasks: dict[str, asyncio.Task[bool]] = {}
        self._entities: dict[str, RamsesEntity] = {}  # domain entities
        self._device_info: dict[str, DeviceInfo] = {}

        # Discovered client objects...
        self._devices: list[Device] = []
        self._systems: list[System] = []
        self._zones: list[Zone] = []
        self._dhws: list[Zone] = []
        self._parameter_entities_created: set[str] = set()

        self._sem = Semaphore(value=1)

        # Initialize platforms dictionary to store platform references
        self.platforms: dict[str, Any] = {}
        self.learn_device_id: str | None = None

    async def async_setup(self) -> None:
        """Set up the RAMSES client and load configuration."""
        storage = await self.store.async_load()
        _LOGGER.debug("Storage = %s", storage)

        remote_commands = {
            k: v[CONF_COMMANDS]
            for k, v in self.options.get(SZ_KNOWN_LIST, {}).items()
            if v.get(CONF_COMMANDS)
        }
        self._remotes = storage.get(SZ_REMOTES, {}) | remote_commands

        client_state: dict[str, Any] = storage.get(SZ_CLIENT_STATE, {})

        config_schema = self.options.get(CONF_SCHEMA, {})
        _LOGGER.debug("CONFIG_SCHEMA: %s", config_schema)
        if not schema_is_minimal(config_schema):
            _LOGGER.warning("The config schema is not minimal (consider minimising it)")

        cached_schema = client_state.get(SZ_SCHEMA, {})
        _LOGGER.debug("CACHED_SCHEMA: %s", cached_schema)

        if cached_schema and (
            merged_schema := merge_schemas(config_schema, cached_schema)
        ):
            try:
                self.client = self._create_client(merged_schema)
            except (LookupError, vol.MultipleInvalid) as err:
                _LOGGER.warning("Failed to initialise with merged schema: %s", err)

        if not self.client:
            try:
                self.client = self._create_client(config_schema)
            except (ValueError, vol.Invalid) as err:
                _LOGGER.error(
                    "Critical error: Failed to initialise client with config schema: %s",
                    err,
                )
                raise ValueError(f"Failed to initialise RAMSES client: {err}") from err

        def cached_packets() -> dict[str, str]:
            msg_code_filter = ["313F"]
            _known_list = self.options.get(SZ_KNOWN_LIST, {})

            packets = {}
            for dtm, pkt in client_state.get(SZ_PACKETS, {}).items():
                try:
                    dt_obj = dt.fromisoformat(dtm)
                except ValueError:
                    _LOGGER.warning(
                        "Ignoring cached packet with invalid timestamp: %s", dtm
                    )
                    continue

                if (
                    dt_obj > dt.now() - timedelta(days=1)
                    and pkt[41:45] not in msg_code_filter
                    and (
                        not self.options[CONF_RAMSES_RF].get(SZ_ENFORCE_KNOWN_LIST)
                        or pkt[11:20] in _known_list
                        or pkt[21:30] in _known_list
                    )
                ):
                    packets[dtm] = pkt

            return packets

        chpkt = cached_packets()
        _LOGGER.info(chpkt)
        await self.client.start(cached_packets=chpkt)
        self.entry.async_on_unload(self.client.stop)

    async def async_start(self) -> None:
        """Initialize the update cycle for the RAMSES broker."""
        await self.async_update()

        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self.async_update,
                timedelta(seconds=self.options.get(CONF_SCAN_INTERVAL, 60)),
            )
        )
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass, self.async_save_client_state, SAVE_STATE_INTERVAL
            )
        )
        self.entry.async_on_unload(self.async_save_client_state)

    def _create_client(self, schema: dict[str, Any]) -> Gateway:
        """Create and configure a new RAMSES client instance."""
        port_name, port_config = extract_serial_port(self.options[SZ_SERIAL_PORT])

        return Gateway(
            port_name=port_name,
            loop=self.hass.loop,
            port_config=port_config,
            packet_log=self.options.get(SZ_PACKET_LOG, {}),
            known_list=self.options.get(SZ_KNOWN_LIST, {}),
            config=self.options.get(CONF_RAMSES_RF, {}),
            **schema,
        )

    async def async_save_client_state(self, _: dt | None = None) -> None:
        """Save the current state of the RAMSES client to persistent storage."""
        _LOGGER.info("Saving the client state cache (packets, schema)")

        schema, packets = self.client.get_state()
        remotes = self._remotes | {
            k: v._commands for k, v in self._entities.items() if hasattr(v, "_commands")
        }

        await self.store.async_save(schema, packets, remotes)

    def _get_device(self, device_id: str) -> Any | None:
        """Get a device by ID."""
        if dev := next((d for d in self._devices if d.id == device_id), None):
            return dev
        if self.client and hasattr(self.client, "device_by_id"):
            return self.client.device_by_id.get(device_id)
        return None

    def async_register_platform(
        self,
        platform: EntityPlatform,
        add_new_devices: Callable[[RamsesRFEntity], None],
    ) -> None:
        """Register a platform that has entities with the broker."""
        platform_str = platform.domain if hasattr(platform, "domain") else platform
        _LOGGER.debug("Registering platform %s", platform_str)

        if platform_str not in self.platforms:
            self.platforms[platform_str] = []
        self.platforms[platform_str].append(platform)

        _LOGGER.debug(
            "Connecting signal for platform %s: %s",
            platform_str,
            SIGNAL_NEW_DEVICES.format(platform_str),
        )

        self.entry.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_NEW_DEVICES.format(platform_str), add_new_devices
            )
        )

    async def _async_setup_platform(self, platform: str) -> bool:
        """Set up a platform and return True if successful."""
        if platform not in self._platform_setup_tasks:
            self._platform_setup_tasks[platform] = self.hass.async_create_task(
                self.hass.config_entries.async_forward_entry_setups(
                    self.entry, [platform]
                )
            )
        try:
            await self._platform_setup_tasks[platform]
            _LOGGER.debug("Platform setup completed for %s", platform)
            return True
        except Exception as err:
            _LOGGER.error(
                "Error setting up %s platform: %s", platform, str(err), exc_info=True
            )
            return False

    async def async_unload_platforms(self) -> bool:
        """Unload all platforms associated with this integration."""
        tasks: list[Coroutine[Any, Any, bool]] = [
            self.hass.config_entries.async_forward_entry_unload(self.entry, platform)
            for platform, task in self._platform_setup_tasks.items()
            if not task.cancel()
        ]
        result = all(await asyncio.gather(*tasks))
        _LOGGER.debug("Platform unload completed with result: %s", result)
        return result

    def _update_device(self, device: RamsesRFEntity) -> None:
        """Update device information in the device registry."""
        if hasattr(device, "name") and device.name:
            name = device.name
        elif isinstance(device, System):
            name = f"Controller {device.id}"
        elif device._SLUG:
            name = f"{device._SLUG} {device.id}"
        else:
            name = device.id

        if info := device._msg_value_code(Code._10E0):
            model = info.get("description")
        else:
            model = device._SLUG

        device_registry = dr.async_get(self.hass)

        if isinstance(device, Zone) and device.tcs:
            _LOGGER.info(f"ZONE {model} via_device SET to {device.tcs}")
            via_device = (DOMAIN, device.tcs.id)
        elif isinstance(device, Child) and device._parent:
            _LOGGER.info(f"CHILD {model} via_device SET to {device._parent}")
            via_device = (DOMAIN, device._parent.id)
        else:
            via_device = None

        device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=name,
            manufacturer=None,
            model=model,
            via_device=via_device,
            serial_number=device.id,
        )

        if self._device_info.get(device.id) == device_info:
            return
        self._device_info[device.id] = device_info

        device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id, **device_info
        )

    async def async_update(self, _: dt | None = None) -> None:
        """Retrieve the latest state data from the client library."""
        gwy: Gateway = self.client

        async def async_add_entities(
            platform: str, devices: list[RamsesRFEntity]
        ) -> None:
            if not devices:
                return None
            await self._async_setup_platform(platform)
            async_dispatcher_send(
                self.hass, SIGNAL_NEW_DEVICES.format(platform), devices
            )

        def find_new_entities(
            known: list[RamsesRFEntity], current: list[RamsesRFEntity]
        ) -> tuple[list[RamsesRFEntity], list[RamsesRFEntity]]:
            new = [x for x in current if x not in known]
            return known + new, new

        self._systems, new_systems = find_new_entities(
            self._systems,
            [s for s in gwy.systems if isinstance(s, Evohome)],
        )
        self._zones, new_zones = find_new_entities(
            self._zones,
            [z for s in gwy.systems for z in s.zones if isinstance(s, Evohome)],
        )
        self._dhws, new_dhws = find_new_entities(
            self._dhws,
            [s.dhw for s in gwy.systems if s.dhw if isinstance(s, Evohome)],
        )
        self._devices, new_devices = find_new_entities(self._devices, gwy.devices)

        for device in self._devices + self._systems + self._zones + self._dhws:
            self._update_device(device)

        for device in new_devices + new_systems + new_zones + new_dhws:
            await self.fan_handler.async_setup_fan_device(device)

        new_entities = new_devices + new_systems + new_zones + new_dhws

        await async_add_entities(Platform.BINARY_SENSOR, new_entities)
        await async_add_entities(Platform.SENSOR, new_entities)

        await async_add_entities(
            Platform.CLIMATE, [d for d in new_devices if isinstance(d, HvacVentilator)]
        )
        await async_add_entities(
            Platform.REMOTE, [d for d in new_devices if isinstance(d, HvacRemoteBase)]
        )
        await async_add_entities(Platform.CLIMATE, new_systems)
        await async_add_entities(Platform.CLIMATE, new_zones)
        await async_add_entities(Platform.WATER_HEATER, new_dhws)
        await async_add_entities(Platform.NUMBER, new_entities)

        if new_entities:
            await self.async_save_client_state()

        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # Delegate service calls to the Service Handler
    async def async_bind_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler."""
        await self.service_handler.async_bind_device(call)

    async def async_force_update(self, _: ServiceCall) -> None:
        """Force an immediate update of all device states."""
        await self.async_update()

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Delegate to Service Handler."""
        await self.service_handler.async_send_packet(call)

    async def async_get_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Delegate to Service Handler."""
        await self.service_handler.async_get_fan_param(call)

    async def _async_run_fan_param_sequence(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Delegate to Service Handler to run the fan parameter sequence."""
        await self.service_handler._async_run_fan_param_sequence(call)

    def get_all_fan_params(self, call: dict[str, Any] | ServiceCall) -> None:
        """Delegate to Service Handler."""
        # Note: get_all_fan_params is not async, it wraps the async call in a task
        self.hass.loop.create_task(
            self.service_handler._async_run_fan_param_sequence(call)
        )

    async def async_set_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Delegate to Service Handler."""
        await self.service_handler.async_set_fan_param(call)

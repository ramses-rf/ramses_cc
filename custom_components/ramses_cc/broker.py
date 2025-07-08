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
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from ramses_rf.device import Fakeable
from ramses_rf.device.base import Device
from ramses_rf.device.hvac import HvacRemoteBase, HvacVentilator
from ramses_rf.entity_base import Child, Entity as RamsesRFEntity
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import SZ_SCHEMA
from ramses_rf.system import Evohome, System, Zone
from ramses_tx.address import pkt_addrs
from ramses_tx.command import Command
from ramses_tx.const import Code
from ramses_tx.exceptions import PacketAddrSetInvalid
from ramses_tx.schemas import (
    SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_SERIAL_PORT,
    extract_serial_port,
)

from .const import (
    CONF_COMMANDS,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    DOMAIN,
    SIGNAL_NEW_DEVICES,
    SIGNAL_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
    SZ_CLIENT_STATE,
    SZ_PACKETS,
    SZ_REMOTES,
)
from .schemas import merge_schemas, schema_is_minimal

if TYPE_CHECKING:
    from . import RamsesEntity


_LOGGER = logging.getLogger(__name__)

SAVE_STATE_INTERVAL: Final[timedelta] = timedelta(minutes=5)

_CALL_LATER_DELAY: Final = 5  # needed for tests


class RamsesBroker:
    """Container for client and data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the client and its data structure(s)."""

        self.hass = hass
        self.entry = entry
        self.options = deepcopy(dict(entry.options))
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

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

        self._sem = Semaphore(value=1)

        self.learn_device_id: str | None = None  # TODO: can we do without this?

    async def async_setup(self) -> None:
        """Set up the client, loading and checking state and config."""
        storage = await self._store.async_load() or {}
        _LOGGER.debug("Storage = %s", storage)

        remote_commands = {
            k: v[CONF_COMMANDS]
            for k, v in self.options.get(SZ_KNOWN_LIST, {}).items()
            if v.get(CONF_COMMANDS)
        }
        self._remotes = storage.get(SZ_REMOTES, {}) | remote_commands

        client_state: dict[str, Any] = storage.get(SZ_CLIENT_STATE, {})

        config_schema = self.options.get(CONF_SCHEMA, {})
        if not schema_is_minimal(config_schema):  # move this logic into ramses_rf?
            _LOGGER.warning("The config schema is not minimal (consider minimising it)")

        cached_schema = client_state.get(SZ_SCHEMA, {})
        if cached_schema and (
            merged_schema := merge_schemas(config_schema, cached_schema)
        ):
            try:
                self.client = self._create_client(merged_schema)
            except (LookupError, vol.MultipleInvalid) as err:
                # LookupError:     ...in the schema, but also in the block_list
                # MultipleInvalid: ...extra keys not allowed @ data['???']
                _LOGGER.warning("Failed to initialise with merged schema: %s", err)

        if not self.client:
            self.client = self._create_client(config_schema)

        def cached_packets() -> dict[str, str]:  # dtm_str, packet_as_str
            msg_code_filter = ["313F"]  # ? 1FC9
            return {
                dtm: pkt
                for dtm, pkt in client_state.get(SZ_PACKETS, {}).items()
                if dt.fromisoformat(dtm) > dt.now() - timedelta(days=1)
                and pkt[41:45] not in msg_code_filter
            }

        # NOTE: Warning: 'Detected blocking call to sleep inside the event loop'
        # - in pyserial: rfc2217.py, in Serial.open(): `time.sleep(0.05)`
        await self.client.start(cached_packets=cached_packets())
        self.entry.async_on_unload(self.client.stop)

    async def async_start(self) -> None:
        """Perform initial update, then poll and save state at intervals."""

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

    def _create_client(
        self,
        schema: dict[str, Any],
    ) -> Gateway:
        """Create a client with an initial schema (merged or config)."""
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
        """Save the client state to the application store."""

        _LOGGER.info("Saving the client state cache (packets, schema)")

        schema, packets = self.client.get_state()
        remotes = self._remotes | {
            k: v._commands for k, v in self._entities.items() if hasattr(v, "_commands")
        }

        await self._store.async_save(
            {
                SZ_CLIENT_STATE: {SZ_SCHEMA: schema, SZ_PACKETS: packets},
                SZ_REMOTES: remotes,
            }
        )

    def async_register_platform(
        self,
        platform: EntityPlatform,
        add_new_devices: Callable[[RamsesRFEntity], None],
    ) -> None:
        """Register a platform for device addition."""
        self.entry.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_NEW_DEVICES.format(platform.domain), add_new_devices
            )
        )

    async def _async_setup_platform(self, platform: str) -> None:
        """Set up a platform."""
        if platform not in self._platform_setup_tasks:
            self._platform_setup_tasks[platform] = self.hass.async_create_task(
                self.hass.config_entries.async_forward_entry_setups(
                    self.entry, [platform]
                )
            )
        await self._platform_setup_tasks[platform]

    async def async_unload_platforms(self) -> bool:
        """Unload platforms."""
        tasks: list[Coroutine[Any, Any, bool]] = [
            self.hass.config_entries.async_forward_entry_unload(self.entry, platform)
            for platform, task in self._platform_setup_tasks.items()
            if not task.cancel()
        ]
        return all(await asyncio.gather(*tasks))

    def _update_device(self, device: RamsesRFEntity) -> None:
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

        if isinstance(device, Zone) and device.tcs:
            via_device = (DOMAIN, device.tcs.id)
        elif isinstance(device, Child) and device._parent:
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

        device_registry = dr.async_get(self.hass)
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

        new_entities = new_devices + new_systems + new_zones + new_dhws
        # these two are the only opportunity to use async_forward_entry_setups with
        # multiple platforms (i.e. not just one)...
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

        if new_entities:
            await self.async_save_client_state()

        # Trigger state updates of all entities
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # The service handlers are class methods to facilitate mocking...
    async def async_bind_device(self, call: ServiceCall) -> None:
        """Handle the bind_device service call."""

        device: Fakeable

        try:
            device = self.client.fake_device(call.data["device_id"])
        except LookupError as err:
            _LOGGER.error("%s", err)
            return

        cmd = Command(call.data["device_info"]) if call.data["device_info"] else None

        await device._initiate_binding_process(  # may: BindingFlowFailed
            list(call.data["offer"].keys()),
            confirm_code=list(call.data["confirm"].keys()),
            ratify_cmd=cmd,
        )  # TODO: will need to re-discover schema
        async_call_later(self.hass, _CALL_LATER_DELAY, self.async_update)

    async def async_force_update(self, _: ServiceCall) -> None:
        """Handle the force_update service call."""

        await self.async_update()

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Create a command packet and send it via the transport."""

        kwargs = dict(call.data.items())  # is ReadOnlyDict
        if (
            call.data["device_id"] == "18:000730"
            and kwargs.get("from_id", "18:000730") == "18:000730"
            and self.client.hgi.id
        ):
            kwargs["device_id"] = self.client.hgi.id

        cmd = self.client.create_cmd(**kwargs)

        # HACK: to fix the device_id when GWY announcing, will be:
        #    I --- 18:000730 18:006402 --:------ 0008 002 00C3  # because src != dst
        # ... should be:
        #    I --- 18:000730 --:------ 18:006402 0008 002 00C3  # 18:730 is sentinel
        if cmd.src.id == "18:000730" and cmd.dst.id == self.client.hgi.id:
            try:
                pkt_addrs(self.client.hgi.id + cmd._frame[16:37])
            except PacketAddrSetInvalid:
                cmd._addrs[1], cmd._addrs[2] = cmd._addrs[2], cmd._addrs[1]
                cmd._repr = None

        await self.client.async_send_cmd(cmd)
        async_call_later(self.hass, _CALL_LATER_DELAY, self.async_update)

    async def async_get_fan_param(self, call: ServiceCall) -> None:
        """Handle get_fan_param service call.

        This sends a parameter read request to the specified fan device. The response
        will be processed by the device's normal packet handling.

        Args:
            call: Service call data containing:
                - device_id: Target device ID (required)
                - param_id: Parameter ID to read (required, 2 hex digits)
                - from_id: Source device ID (optional, defaults to HGI)
                - fan_id: Fan ID (optional, defaults to device_id)

        Raises:
            ValueError: If required parameters are missing or invalid
        """
        _LOGGER.debug("Processing get_fan_param service call with data: %s", call.data)
        try:
            # Get parameters from service call with defaults
            device_id = call.data.get("device_id")
            param_id = call.data.get("param_id")
            from_id = call.data.get(
                "from_id",
                self.client.hgi.id if self.client and self.client.hgi else None,
            )
            fan_id = call.data.get("fan_id", device_id)

            _LOGGER.debug(
                "Parsed parameters - device_id: %s, param_id: %s, from_id: %s, fan_id: %s",
                device_id,
                param_id,
                from_id,
                fan_id,
            )

            # Validate required parameters
            if not device_id:
                _LOGGER.error("Missing required parameter: device_id")
                raise ValueError("required key not provided @ data['device_id']")

            if not param_id:
                _LOGGER.error("Missing required parameter: param_id")
                raise ValueError("required key not provided @ data['param_id']")

            if not from_id:
                raise ValueError("No source device ID specified and HGI not available")

            cmd = Command.get_fan_param(fan_id, param_id, src_id=from_id)
            _LOGGER.debug("Sending command: %s", cmd)

            # Send the command directly using the gateway
            await self.client.async_send_cmd(cmd)

            # Add a small delay to prevent message flooding
            await asyncio.sleep(0.2)

        except Exception as ex:
            _LOGGER.warning(
                "Failed to send get_fan_param command to %s (param: %s, from: %s): %s",
                device_id,
                param_id,
                from_id,
                ex,
                exc_info=_LOGGER.isEnabledFor(logging.DEBUG),
            )
            # Don't re-raise, as this is a fire-and-forget operation
            # The RP packet handler will log any responses if received

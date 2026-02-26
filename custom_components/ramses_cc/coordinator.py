"""Coordinator for RAMSES integration."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Coroutine
from copy import deepcopy
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ramses_rf.device import Device
from ramses_rf.device.hvac import HvacRemoteBase, HvacVentilator
from ramses_rf.entity_base import Child, Entity as RamsesRFEntity
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.system import Evohome, System, Zone
from ramses_tx.const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3, Code
from ramses_tx.schemas import extract_serial_port

from .const import (
    CONF_COMMANDS,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_RAMSES_RF,
    CONF_SCAN_INTERVAL,
    CONF_SCHEMA,
    DEFAULT_HGI_ID,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    SIGNAL_NEW_DEVICES,
    SZ_CLIENT_STATE,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_PACKETS,
    SZ_PORT_NAME,
    SZ_REMOTES,
    SZ_SCHEMA,
    SZ_SERIAL_PORT,
)
from .fan_handler import RamsesFanHandler
from .mqtt_bridge import RamsesMqttBridge
from .schemas import merge_schemas, schema_is_minimal
from .services import RamsesServiceHandler
from .store import RamsesStore

if TYPE_CHECKING:
    from .entity import RamsesEntity

_LOGGER = logging.getLogger(__name__)

SAVE_STATE_INTERVAL: Final[timedelta] = timedelta(minutes=5)


class MqttGateway(Gateway):
    """Custom Gateway that forces the use of the HA MQTT Bridge.

    This class bridges the gap between ramses_rf's strict schema validation
    and ramses_tx's flexible transport factory.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the gateway."""
        # 1. Pop our custom arguments to avoid 'voluptuous' schema validation errors
        #    in the parent class.
        self._custom_factory = kwargs.pop("transport_constructor", None)
        self._custom_extra = kwargs.pop("extra", None)

        # 2. Initialize the standard Gateway
        super().__init__(**kwargs)

    async def start(self, **kwargs: Any) -> None:
        """Start the gateway, injecting the custom transport constructor."""
        # 3. Inject our factory into _kwargs with the specific name 'transport_constructor'
        #    This is what ramses_tx.transport.transport_factory looks for.
        if self._custom_factory:
            _LOGGER.debug("Injecting custom transport_constructor into MqttGateway")
            self._kwargs["transport_constructor"] = self._custom_factory

        # 4. Inject extra info (HGI ID) into _kwargs so transport_factory receives it
        if self._custom_extra:
            _LOGGER.debug("Injecting custom extra info into MqttGateway")
            self._kwargs["extra"] = self._custom_extra

        # 5. Call parent start(), which calls transport_factory(**self._kwargs)
        await super().start(**kwargs)


class RamsesCoordinator(DataUpdateCoordinator):
    """Central coordinator for the RAMSES integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the RAMSES coordinator and its data structures."""
        self.hass = hass
        self.entry = entry
        self.options = deepcopy(dict(entry.options))
        self.store = RamsesStore(hass)

        # Initialize handlers
        self.fan_handler = RamsesFanHandler(self)
        self.service_handler = RamsesServiceHandler(self)
        self.mqtt_bridge: RamsesMqttBridge | None = None

        _LOGGER.debug("Config = %s", entry.options)

        self.client: Gateway | None = None
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

        # Initialize platforms dictionary to store platform references
        self.platforms: dict[str, Any] = {}
        self.learn_device_id: str | None = None

        # Load scan interval from options, default to 60s if missing
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, 60)
        _LOGGER.debug(
            "Coordinator initialized with scan_interval: %s seconds", scan_interval
        )

        # Initialize the DataUpdateCoordinator
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    def _get_saved_packets(self, client_state: dict[str, Any]) -> dict[str, str]:
        """Filter cached packets to remove expired or unwanted entries."""
        msg_code_filter = ["313F"]
        known_list = self.options.get(SZ_KNOWN_LIST, {})
        enforce_known_list = self.options[CONF_RAMSES_RF].get(SZ_ENFORCE_KNOWN_LIST)

        packets = {}
        now = dt_util.now()

        # Iterate over packets from storage
        for dtm, pkt in client_state.get(SZ_PACKETS, {}).items():
            try:
                dt_obj = datetime.fromisoformat(dtm)
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            except ValueError:
                _LOGGER.warning(
                    "Ignoring cached packet with invalid timestamp: %s", dtm
                )
                continue

            # Check age (keep last 24 hours) and known list enforcement
            if (
                dt_obj > now - timedelta(days=1)
                and pkt[41:45] not in msg_code_filter
                and (
                    not enforce_known_list
                    or pkt[11:20] in known_list
                    or pkt[21:30] in known_list
                )
            ):
                packets[dtm] = pkt

        return packets

    async def async_setup(self) -> None:
        """Set up the RAMSES client and load configuration.

        Loads storage, restores remote commands, and initializes the Gateway client.
        """
        storage = await self.store.async_load()
        _LOGGER.debug("Storage = %s", storage)

        # 1. Load Remotes
        remote_commands = {
            k: v[CONF_COMMANDS]
            for k, v in self.options.get(SZ_KNOWN_LIST, {}).items()
            if v.get(CONF_COMMANDS)
        }
        self._remotes = storage.get(SZ_REMOTES, {}) | remote_commands

        client_state: dict[str, Any] = storage.get(SZ_CLIENT_STATE, {})

        # 2. Schema Handling
        config_schema = self.options.get(CONF_SCHEMA, {})
        _LOGGER.debug("CONFIG_SCHEMA: %s", config_schema)
        if not schema_is_minimal(config_schema):
            _LOGGER.warning("The config schema is not minimal (consider minimising it)")

        cached_schema = client_state.get(SZ_SCHEMA, {})
        _LOGGER.debug("CACHED_SCHEMA: %s", cached_schema)

        # Try merging schemas
        if cached_schema and (
            merged_schema := merge_schemas(config_schema, cached_schema)
        ):
            try:
                self.client = self._create_client(merged_schema)
            except (LookupError, vol.MultipleInvalid) as err:
                _LOGGER.warning("Failed to initialise with merged schema: %s", err)

        # Fallback to config schema
        if not self.client:
            try:
                self.client = self._create_client(config_schema)
            except (ValueError, vol.Invalid) as err:
                _LOGGER.error(
                    "Critical error: Failed to initialise client with config schema: %s",
                    err,
                )
                raise ValueError(f"Failed to initialise RAMSES client: {err}") from err

        # 3. Packet Handling (Refactored)
        cached_packets = self._get_saved_packets(client_state)
        _LOGGER.info("Starting with %s cached packets", len(cached_packets))

        start_kwargs: dict[str, Any] = {"cached_packets": cached_packets}

        await self.client.start(**start_kwargs)
        self.entry.async_on_unload(self.client.stop)

    async def async_start(self) -> None:
        """Start the coordinator and initiate the first refresh.

        Starts discovery loops, saves initial state, and triggers the first data update.
        """
        # Note: self.client.start() should have been called in async_setup

        # 1. Trigger the first discovery immediately
        #    We call this directly because we want entities found BEFORE we finish setup
        _LOGGER.debug("Coordinator: Starting initial discovery...")
        await self._discover_new_entities()

        # 2. Schedule the Discovery Loop
        #    This runs independently of the DataUpdateCoordinator's internal timer.
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._async_discovery_task,
                timedelta(seconds=self.entry.options.get(CONF_SCAN_INTERVAL, 60)),
            )
        )

        # Trigger the first update immediately (calls _async_update_data)
        # This will raise ConfigEntryNotReady if it fails, which is handled by HA
        await self.async_config_entry_first_refresh()

        # Keep the dedicated interval for saving client state to disk
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass, self.async_save_client_state, SAVE_STATE_INTERVAL
            )
        )
        self.entry.async_on_unload(self.async_save_client_state)

    def _create_client(self, schema: dict[str, Any]) -> Gateway:
        """Create and configure a new RAMSES client instance."""

        # 1. Get the raw dict config from HA options
        raw_config = self.options.get(CONF_RAMSES_RF, {}).copy()

        # 2. Identify which keys belong to the new GatewayConfig dataclass
        valid_config_keys = set(inspect.signature(GatewayConfig).parameters.keys())

        # 3. Identify which keys belong directly to Gateway.__init__
        valid_gateway_keys = set(inspect.signature(Gateway.__init__).parameters.keys())

        # 4. Split the dict: GatewayConfig args vs Gateway __init__ args
        gwy_config_args = {
            k: v for k, v in raw_config.items() if k in valid_config_keys
        }

        # Drop any deprecated keys that don't belong to either!
        handled_keys = {
            "self",
            "kwargs",
            "config",
            "schema",
            "packet_log",
            "known_list",
            "port_name",
            "loop",
        }
        gateway_kwargs = {
            k: v
            for k, v in raw_config.items()
            if k in valid_gateway_keys and k not in handled_keys
        }

        kwargs = {
            "packet_log": self.options.get(SZ_PACKET_LOG, {}),
            "known_list": self.options.get(SZ_KNOWN_LIST, {}),
            "config": GatewayConfig(**gwy_config_args),
            "schema": schema,
            **gateway_kwargs,
        }

        # Check for HA MQTT Strategy
        if self.options.get(CONF_MQTT_USE_HA):
            if not self.hass.config_entries.async_entries("mqtt"):
                raise ConfigEntryNotReady(
                    "Home Assistant MQTT integration is not set up"
                )

            # Retrieve config options
            mqtt_topic = self.options.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)
            hgi_id = self.options.get(CONF_MQTT_HGI_ID, DEFAULT_HGI_ID)

            self.mqtt_bridge = RamsesMqttBridge(self.hass, mqtt_topic, hgi_id)

            # Ensure the bridge unsubscribes from MQTT on shutdown
            self.entry.async_on_unload(self.mqtt_bridge.close)

            # Pass factory in kwargs so MqttGateway pops it.
            # We are essentially "queueing" it to be injected into _kwargs later.
            # UPDATED: Use 'transport_constructor' as per new API guide
            kwargs["transport_constructor"] = self.mqtt_bridge.async_transport_factory

            # We must provide a port_name to satisfy ramses_tx validation.
            port_name = self.options.get(SZ_SERIAL_PORT, {}).get(SZ_PORT_NAME, "mqtt")

            # Pass the configured HGI ID to ramses_rf.
            # This allows the library to handle ID logic natively without patching.
            kwargs["hgi_id"] = hgi_id

            # Inject HGI into known_list (Redundant but safe fallback)
            # The config_flow now handles this, but we maintain it here to satisfy
            # ramses_rf schema validation during runtime.
            known_list = kwargs.get("known_list", {}).copy()
            device_entry = known_list.setdefault(hgi_id, {})
            device_entry["class"] = "HGI"
            device_entry.setdefault("alias", "ramses_esp")
            kwargs["known_list"] = known_list

            # NOTE: SZ_IS_EVOFW3=True enables address patching in ramses_tx protocol.
            # This is required because MqttTransport behaves like evofw3 (masquerading),
            # not like a strict HGI80 (which enforces 18:000730).
            kwargs["extra"] = {
                SZ_ACTIVE_HGI: hgi_id,
                SZ_IS_EVOFW3: True,
            }

            # Use our custom Gateway subclass
            return MqttGateway(
                port_name=port_name,
                loop=self.hass.loop,
                **kwargs,
            )

        else:
            # Standard Serial/USB setup
            port_name, port_config = extract_serial_port(self.options[SZ_SERIAL_PORT])
            kwargs["port_config"] = port_config

            return Gateway(
                port_name=port_name,
                loop=self.hass.loop,
                **kwargs,
            )

    async def async_save_client_state(self, _: datetime | None = None) -> None:
        """Save the current state of the RAMSES client to persistent storage.

        :param _: Optional datetime argument from async_track_time_interval.
        """

        if not self.client:
            _LOGGER.debug("Cannot save state: Client not initialized")
            return

        # Support both async (new) and sync (old) client.get_state()
        result = self.client.get_state()

        if inspect.isawaitable(result):
            schema, packets = await result
        else:
            schema, packets = result

        _LOGGER.info("Saving the client state cache (packets, schema)")

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
        """Register a platform that has entities with the coordinator.

        :param platform: The HA platform instance (e.g. climate, sensor).
        :param add_new_devices: Callback to add new devices to HA.
        """
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
        """Unload all platforms associated with this integration.

        :return: True if all platforms unloaded successfully.
        """
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

    async def _async_update_data(self) -> None:
        """Fetch data from the RAMSES RF client."""
        _LOGGER.debug("Coordinator: _async_update_data called (Heartbeat)")
        if not self.client:
            _LOGGER.debug(
                "Coordinator: (_async_update_data) Client is None, skipping update"
            )
            return

        # The Coordinator is now only responsible for updating entities that already exist.
        # If ramses_rf pushes updates via callbacks, you might not even need logic here.
        # But if you need to poll for specific values (e.g. fault status), do it here.

        return None

    async def _async_discovery_task(self, _now: datetime | None = None) -> None:
        """Wrapper to call discovery from the interval listener."""
        try:
            await self._discover_new_entities()
        except Exception as err:
            _LOGGER.error("Discovery error: %s", err)

    async def _discover_new_entities(self) -> None:
        """Discover new devices in the client and register them with HA."""
        gwy: Gateway = self.client

        # Snapshot the lists to avoid RuntimeError if ramses_rf updates them continuously
        # This fixes the silent failure where list changes size during iteration
        current_devices = list(gwy.devices)
        current_systems = list(gwy.systems)

        # --- DIAGNOSTIC LOGGING ---
        # This will reveal if ramses_rf has actually found any devices.
        _LOGGER.info(
            "Discovery: Devices=%s, Systems=%s",
            len(current_devices),
            len(current_systems),
        )
        if len(current_devices) > 0:
            _LOGGER.debug("Discovered Devices: %s", [d.id for d in current_devices])

        async def async_add_entities(
            platform: str, devices: list[RamsesRFEntity]
        ) -> None:
            if not devices:
                return
            await self._async_setup_platform(platform)
            async_dispatcher_send(
                self.hass, SIGNAL_NEW_DEVICES.format(platform), devices
            )

        def find_new_entities(
            known: list[RamsesRFEntity], current: list[RamsesRFEntity]
        ) -> tuple[list[RamsesRFEntity], list[RamsesRFEntity]]:
            new = [x for x in current if x not in known]
            return known + new, new

        # Identify new items compared to what we already know
        self._systems, new_systems = find_new_entities(
            self._systems,
            [s for s in current_systems if isinstance(s, Evohome)],
        )
        self._zones, new_zones = find_new_entities(
            self._zones,
            [z for s in current_systems for z in s.zones if isinstance(s, Evohome)],
        )
        self._dhws, new_dhws = find_new_entities(
            self._dhws,
            [s.dhw for s in current_systems if s.dhw if isinstance(s, Evohome)],
        )
        self._devices, new_devices = find_new_entities(self._devices, current_devices)

        # Process new devices for fan logic
        # Systems/DHWs must be processed before Devices to ensure via_device parents exist
        for device in new_systems + new_dhws + new_zones + new_devices:
            await self.fan_handler.async_setup_fan_device(device)
            # Register device in registry once upon discovery
            self._update_device(device)

        new_entities = new_systems + new_dhws + new_zones + new_devices

        if not new_entities:
            return

        # Register new entities with platforms
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

        # Trigger a save if we found something new
        await self.async_save_client_state()

    # Delegate service calls to the Service Handler
    async def async_bind_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_bind_device(call)

    async def async_force_update(self, _: ServiceCall) -> None:
        """Force an immediate update of all device states.

        :param _: Unused service call argument.
        """
        await self.async_refresh()

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_send_packet(call)

    async def async_get_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call or dictionary containing parameters.
        """
        await self.service_handler.async_get_fan_param(call)

    async def _async_run_fan_param_sequence(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Delegate to Service Handler to run the fan parameter sequence.

        :param call: The service call or dictionary containing parameters.
        """
        await self.service_handler._async_run_fan_param_sequence(call)

    def get_all_fan_params(self, call: dict[str, Any] | ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call or dictionary containing parameters.
        """
        # Note: get_all_fan_params is not async, it wraps the async call in a task
        self.hass.async_create_task(
            self.service_handler._async_run_fan_param_sequence(call)
        )

    async def async_set_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call or dictionary containing parameters.
        """
        await self.service_handler.async_set_fan_param(call)

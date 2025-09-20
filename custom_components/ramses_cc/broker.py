"""Broker for RAMSES integration."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from copy import deepcopy
from datetime import datetime as dt, timedelta
from threading import Semaphore
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er
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
from ramses_tx.const import Code, DevType
from ramses_tx.exceptions import PacketAddrSetInvalid
from ramses_tx.ramses import _2411_PARAMS_SCHEMA
from ramses_tx.schemas import (
    SZ_BOUND_TO,
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
        """Initialize the RAMSES broker and its data structures.

        :param hass: Home Assistant instance
        :type hass: HomeAssistant
        :param entry: Configuration entry for this integration
        :type entry: ConfigEntry

        .. note::
            Initializes the client connection. Calls async_setup() to complete initialization.
        """

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

        # Initialize platforms dictionary to store platform references
        self.platforms: dict[str, Any] = {}

        self.learn_device_id: str | None = None  # TODO: can we do without this?

    async def async_setup(self) -> None:
        """Set up the RAMSES client and load configuration.

        This method:
        - Loads any cached packets from storage
        - Creates and configures the RAMSES client
        - Starts the client connection
        - Sets up the save state timer

        :raises ValueError: If there's an error in the configuration
        :raises RuntimeError: If the client fails to start
        """
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
        """Initialize the update cycle for the RAMSES broker.

        This method:
        - Performs an initial update of all devices
        - Sets up periodic updates based on the configured scan interval
        - Sets up periodic state saving

        :raises RuntimeError: If the client is not properly initialized

        .. note::
            This is called after async_setup() to start the periodic updates.
        """

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
        """Create and configure a new RAMSES client instance.

        :param schema: Configuration schema for the client
        :type schema: dict[str, Any]
        :return: Configured Gateway instance
        :rtype: Gateway
        :raises ValueError: If the configuration is invalid

        .. note::
            This method creates a new Gateway instance with the provided configuration
            and sets up the necessary callbacks for device discovery and updates.
        """
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
        """Save the current state of the RAMSES client to persistent storage.

        :param _: Unused parameter for callback compatibility
        :type _: dt | None

        .. note::
            This method saves important state information including:
            - Remote command mappings
            - Other client state that needs to persist between restarts

            It's called periodically and on shutdown.
        """

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

    def _get_device(self, device_id: str) -> Any | None:
        """Get a device by ID.

        :param device_id: The ID of the device to find
        :type device_id: str
        :return: The device if found, None otherwise
        :rtype: Any | None
        """
        return next((d for d in self._devices if d.id == device_id), None)

    def async_register_platform(
        self,
        platform: EntityPlatform,
        add_new_devices: Callable[[RamsesRFEntity], None],
    ) -> None:
        """Register a platform that has entities with the broker.

        :param platform: The platform to register
        :type platform: EntityPlatform
        :param add_new_devices: Callback function to add new devices to the platform
        :type add_new_devices: Callable[[RamsesRFEntity], None]
        """
        platform_str = platform.domain if hasattr(platform, "domain") else platform
        _LOGGER.debug("[broker]Registering platform %s", platform_str)

        # Store the platform reference for entity lookup
        if platform_str not in self.platforms:
            self.platforms[platform_str] = []
        self.platforms[platform_str].append(platform)

        _LOGGER.debug(
            "[broker] Connecting signal for platform %s: %s",
            platform_str,
            SIGNAL_NEW_DEVICES.format(platform_str),
        )

        self.entry.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_NEW_DEVICES.format(platform_str), add_new_devices
            )
        )

    async def _async_setup_platform(self, platform: str) -> bool:
        """Set up a platform and return True if successful.

        :param platform: The platform to set up (e.g., 'climate', 'sensor')
        :type platform: str
        :return: True if the platform was set up successfully, False otherwise
        :rtype: bool
        """
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
        except Exception as ex:
            _LOGGER.error(
                "Error setting up %s platform: %s", platform, str(ex), exc_info=True
            )
            return False

    async def async_unload_platforms(self) -> bool:
        """Unload all platforms associated with this integration.

        :return: True if all platforms were unloaded successfully, False otherwise
        :rtype: bool
        """
        tasks: list[Coroutine[Any, Any, bool]] = [
            self.hass.config_entries.async_forward_entry_unload(self.entry, platform)
            for platform, task in self._platform_setup_tasks.items()
            if not task.cancel()
        ]
        result = all(await asyncio.gather(*tasks))
        _LOGGER.debug("Platform unload completed with result: %s", result)
        return result

    async def _get_all_fan_params(
        self, device: Device, from_id: str | None = None
    ) -> None:
        """Request values for all supported 2411 parameters of a device.

        Uses the async_get_fan_param servicecall to request each parameter value.

        :param device: The target device to request parameters from
        :type device: Device
        :param from_id: Optional source device ID for the request. If not provided,
                       will use a bound REM/DIS device or the HGI.
        :type from_id: str | None
        """
        if not hasattr(device, "supports_2411") or not device.supports_2411:
            _LOGGER.debug("Device %s does not support 2411 parameters", device.id)
            return

        _LOGGER.debug("Requesting all parameter values for device %s", device.id)

        # If from_id is not provided, try to get it from bound REM/DIS device or HGI
        if from_id is None:
            if hasattr(device, "get_bound_rem") and (from_id := device.get_bound_rem()):
                _LOGGER.debug("Using bound device %s for parameter requests", from_id)
            elif self.client and self.client.hgi and (from_id := self.client.hgi.id):
                _LOGGER.debug("Using HGI device %s for parameter requests", from_id)

            if not from_id:
                _LOGGER.error(
                    "Cannot request parameters: No HGI or bound REM/DIS device available for %s",
                    device.id,
                )
                return

        # Request parameters one at a time with a small delay between them
        for param_id in _2411_PARAMS_SCHEMA:
            try:
                _LOGGER.debug("Requesting value for parameter %s", param_id)
                # Create parameter dictionary with proper types
                params = {
                    "device_id": str(device.id),
                    "param_id": str(param_id),
                    "from_id": str(from_id)
                    if from_id
                    else None,  # Either HGI or bound REM/DIS device
                    "fan_id": str(
                        device.id
                    ),  # Always use the device's own ID as fan_id
                }
                # Call directly with the params dict
                await self.async_get_fan_param(params)
                await asyncio.sleep(0.1)  # Small delay between requests
            except Exception as ex:
                _LOGGER.warning(
                    "Failed to request parameter %s: %s",
                    param_id,
                    str(ex),
                    exc_info=True,
                )

    async def _async_create_parameter_entities(self, device: Device) -> None:
        """Create parameter entities for a device that supports 2411 parameters.

        :param device: The device to create parameter entities for
        :type device: Device
        """
        from .number import (
            async_create_parameter_entities,
        )  # import here to avoid circular import

        entities = await async_create_parameter_entities(self, device)
        if entities:
            _LOGGER.info(
                "Adding %d parameter entities for %s", len(entities), device.id
            )
            async_dispatcher_send(
                self.hass, SIGNAL_NEW_DEVICES.format(Platform.NUMBER), entities
            )
        else:
            _LOGGER.warning("No parameter entities created for %s", device.id)

    async def _setup_fan_bound_devices(self, device: Device) -> None:
        """Set up bound devices for a FAN device.
        A FAN will only respond to 2411 messages on RQ from a bound device (REM/DIS).
        In config flow, a 'bound' trait can be added to a FAN to specify the bound device.

        :param device: The FAN device to set up bound devices for
        :type device: Device

        .. note::
            Currently supports only one bound device. To support multiple bound devices:
            - Update the schema to accept a list of bound devices
            - Modify this method to handle multiple devices
            - Add appropriate methods to the HVAC class
        """
        # Only proceed if this is a FAN device
        if not isinstance(device, HvacVentilator):
            return

        # Get device configuration from known_list
        device_config = self.options.get(SZ_KNOWN_LIST, {}).get(device.id, {})
        if SZ_BOUND_TO in device_config:
            _LOGGER.debug("Device config: %s", device_config)
            _LOGGER.debug("Device type: %s", device.type)
            _LOGGER.debug("Device class: %s", device.__class__)

            bound_device_id = device_config[SZ_BOUND_TO]
            _LOGGER.info(
                "Binding FAN %s and REM/DIS device %s", device.id, bound_device_id
            )

            # Find the bound device and get its type
            bound_device = next(
                (d for d in self.client.devices if d.id == bound_device_id),
                None,
            )

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
                    return

                # Add the bound device to the FAN's tracking
                device.add_bound_device(bound_device_id, device_type)
                _LOGGER.info(
                    "Bound FAN %s to %s device %s",
                    device.id,
                    device_type,
                    bound_device_id,
                )
            else:
                _LOGGER.warning(
                    "Bound device %s not found for FAN %s", bound_device_id, device.id
                )

    async def _async_setup_device(self, device: Device) -> None:
        """Set up a device and its entities.

        This method is called from async_update() when a device is first discovered.
        For FAN devices, it also sets up bound REM/DIS devices and parameter handling.

        :param device: The device to set up
        :type device: Device

        .. note::
            For FAN devices, this method will:
            - Set up bound REM/DIS devices
            - Set up parameter handling
            - Create parameter entities after the first message is received
            - Request all parameter values
        """
        _LOGGER.debug("Setting up device: %s", device)

        # For FAN devices, set up bound devices and parameter handling
        if hasattr(device, "_SLUG") and device._SLUG == "FAN":
            await self._setup_fan_bound_devices(device)

            # Set up the initialization callback - will be called on first message
            if hasattr(device, "set_initialized_callback"):

                async def on_fan_first_message() -> None:
                    """Handle the first message received from a FAN device.

                    Creates parameter entities and requests all parameter values.

                    .. note::
                        It's set as the initialization callback in hvac.py.
                    """
                    _LOGGER.debug(
                        "First message received from FAN %s, creating parameter entities",
                        device.id,
                    )
                    # Create parameter entities after first message is received
                    await self._async_create_parameter_entities(device)
                    # Request all parameters after creating entities
                    await self._get_all_fan_params(device)

                device.set_initialized_callback(
                    lambda: self.hass.async_create_task(on_fan_first_message())
                )

            # Set up parameter update callback
            if hasattr(device, "set_param_update_callback"):
                device.set_param_update_callback(
                    lambda param_id, value: self.hass.bus.async_fire(
                        "ramses_cc.fan_param_updated",
                        {"device_id": device.id, "param_id": param_id, "value": value},
                    )
                )

            # Check if device is already initialized (e.g., from cached messages)
            # This handles the case where we restart but the device already has state
            if hasattr(device, "supports_2411") and device.supports_2411:
                if getattr(device, "_initialized", False):
                    _LOGGER.debug(
                        "Device %s already initialized, creating parameter entities and requesting parameters",
                        device.id,
                    )
                    await self._async_create_parameter_entities(device)
                    _LOGGER.debug(
                        "Sending signal to NUMBER platform to add new devices for FAN %s",
                        device.id,
                    )
                    platform_obj = Platform.NUMBER
                    async_dispatcher_send(
                        self.hass,
                        SIGNAL_NEW_DEVICES.format(platform_obj.domain),
                        [device],
                    )
                    await self._get_all_fan_params(device)

    def _update_device(self, device: RamsesRFEntity) -> None:
        """Update device information in the device registry.

        This method updates the device registry with the latest information
        about a device, including its name, model, and relationships.

        :param device: The device to update in the registry
        :type device: RamsesRFEntity
        """
        if hasattr(device, "_name") and device._name:
            name = device._name
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
        """Retrieve the latest state data from the client library.

        This method is called periodically by Home Assistant's update coordinator
        to refresh the state of all devices.

        :param _: Unused parameter for backward compatibility
        :type _: dt | None
        """

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
            """Find new entities that are in current but not in known.

            :param known: List of known entities
            :type known: list[RamsesRFEntity]
            :param current: List of current entities
            :type current: list[RamsesRFEntity]
            :return: A tuple containing (updated known list, new entities)
            :rtype: tuple[list[RamsesRFEntity], list[RamsesRFEntity]]
            """
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
            await self._async_setup_device(device)

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
        await async_add_entities(Platform.NUMBER, new_entities)

        if new_entities:
            await self.async_save_client_state()

        # Trigger state updates of all entities
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def async_bind_device(self, call: ServiceCall) -> None:
        """Handle the bind_device service call to bind a device to the system.

        This method initiates the binding process for a device, allowing it to be
        recognized and controlled by the system.
        This method will NOT set the 'bound' trait in config flow (yet).

        :param call: Service call containing binding parameters
        :type call: ServiceCall
        :raises LookupError: If the specified device ID is not found

        .. note::
            The service call should include:
            - device_id: The ID of the device to bind
            - device_info: Optional device information
            - offer: Dictionary of binding offers
            - confirm: Dictionary of confirmation codes

            After successful binding, the device schema will need to be rediscovered.
        """

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
        """Force an immediate update of all device states.

        This method triggers a full refresh of all device states by calling
        async_update(). It's typically used to manually refresh the state
        of all devices when needed.

        :param _: Unused service call parameter (for callback compatibility)
        :type _: ServiceCall

        """

        await self.async_update()

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Create and send a raw command packet via the transport layer.

        :param call: Service call containing the packet data
        :type call: ServiceCall
        :raises ValueError: If the packet data is invalid

        .. note::
            The service call should include:
            - device_id: Target device ID
            - from_id: Source device ID (defaults to controller)
            - Other packet-specific parameters
        """

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

    def _find_entity(self, device_id: str, param_id: str) -> Any | None:
        """Find an entity by device ID and parameter ID.

        :param device_id: The device ID (with either colons or underscores)
        :type device_id: str
        :param param_id: The parameter ID of the entity to find
        :type param_id: str
        :return: The found entity or None if not found
        :rtype: Any | None
        """
        # Normalize device ID to use underscores for entity ID
        safe_device_id = device_id.replace(":", "_")
        target_entity_id = f"number.{safe_device_id}_param_{param_id}"

        # First try to find the entity in the entity registry
        ent_reg = er.async_get(self.hass)
        entity_entry = ent_reg.async_get(target_entity_id)
        if entity_entry:
            _LOGGER.debug(f"Found entity {target_entity_id} in entity registry")
            # Get the actual entity from the platform to make sure entity is fully loaded
            platforms = self.platforms.get("number", [])
            _LOGGER.debug(f"Checking platforms: {platforms}")
            for platform in platforms:
                if (
                    hasattr(platform, "entities")
                    and target_entity_id in platform.entities
                ):
                    return platform.entities[target_entity_id]
                else:
                    _LOGGER.debug(
                        f"Entity {target_entity_id} not found in platform.entities (yet)."
                    )

        _LOGGER.debug(f"Entity {target_entity_id} not found in registry.")
        return None

    def _set_entity_pending(self, entity: Any, param_id: str) -> None:
        """Mark an entity as pending an update.

        :param entity: The entity to mark as pending
        :type entity: Any
        :param param_id: The parameter ID being updated (for logging)
        :type param_id: str

        .. note::
            The pending state is stored in the entity's attributes.
        """
        if not entity:
            return

        try:
            entity._is_pending = True
            entity.async_write_ha_state()
            _LOGGER.debug("Set pending state for parameter %s", param_id)
        except Exception as ex:
            _LOGGER.debug(
                "Failed to set pending state for parameter %s: %s", param_id, str(ex)
            )

    def _clear_entity_pending(self, entity: Any, param_id: str) -> None:
        """Clear the pending state for an entity.

        :param entity: The entity to update
        :type entity: Any
        :param param_id: The parameter ID that was being updated (for logging)
        :type param_id: str
        """
        if not entity:
            return

        try:
            if hasattr(entity, "_is_pending"):
                entity._is_pending = False
                entity.async_write_ha_state()
                _LOGGER.debug("Cleared pending state for parameter %s", param_id)
        except Exception as ex:
            _LOGGER.debug(
                "Failed to clear pending state for parameter %s: %s", param_id, str(ex)
            )

    async def _clear_pending_timeout(
        self, entity: Any, param_id: str, timeout: int = 30
    ) -> None:
        """Clear pending state after a timeout if still set.

        :param entity: The entity to update
        :type entity: Any
        :param param_id: The parameter ID for logging
        :type param_id: str
        :param timeout: Timeout in seconds
        :type timeout: int
        """
        await asyncio.sleep(timeout)
        self._clear_entity_pending(entity, param_id)

    async def async_get_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Handle get_fan_param service call (or direct dict).

        This sends a parameter read request to the specified fan device. The response
        will be processed by the device's normal packet handling.

        :param call: Service call data containing device and parameter info
        :type call: dict[str, Any] | ServiceCall
        :raises ValueError: If required parameters are missing or invalid

        The call data should contain:
            - device_id (str): Target device ID (required)
            - param_id (str): Parameter ID to read (required, 2 hex digits)
            - from_id (str, optional): Source device ID (defaults to HGI)
        """
        # Handle both ServiceCall and direct dict inputs
        data: dict[str, Any] = call.data if hasattr(call, "data") else call

        # Extract parameters
        device_id: str | None = data.get("device_id")
        param_id: str | None = data.get("param_id")
        from_id: str | None = data.get("from_id")

        # Validate required parameters
        if not device_id:
            _LOGGER.error("Missing required parameter: device_id")
            raise ValueError("required key not provided @ data['device_id']")

        if not param_id:
            _LOGGER.error("Missing required parameter: param_id")
            raise ValueError("required key not provided @ data['param_id']")

        # Validate parameter ID format (must be 2-digit hex)
        try:
            if len(param_id) != 2 or int(param_id, 16) < 0 or int(param_id, 16) > 0xFF:
                raise ValueError
        except (ValueError, TypeError):
            error_msg = f"Invalid parameter ID: '{param_id}'. Must be a 2-digit hexadecimal value (00-FF)"
            _LOGGER.error(error_msg)
            raise ValueError(error_msg) from None

        if not from_id and self.client and self.client.hgi:
            from_id = self.client.hgi.id

        if not from_id:
            raise ValueError("No source device ID specified and HGI not available")

        # Find the corresponding entity and set it to pending
        entity = self._find_entity(device_id, param_id)
        self._set_entity_pending(entity, param_id)

        cmd = Command.get_fan_param(device_id, param_id, src_id=from_id)
        _LOGGER.debug("Sending command: %s", cmd)

        # Send the command directly using the gateway with a little delay to deminish message flooding
        await self.client.async_send_cmd(cmd)
        await asyncio.sleep(0.2)

        # Clear pending state after 30 seconds if no response is received
        if entity:
            self.hass.async_create_task(self._clear_pending_timeout(entity, param_id))

    async def async_get_all_fan_params(
        self, device_id: str, from_id: str | None = None
    ) -> None:
        """Request all fan parameters for a device.

        This method sends a parameter read request for each parameter in the 2411 schema
        to the specified fan device. The responses will be processed by the device's
        normal packet handling.

        :param device_id: The device ID to request parameters for
        :type device_id: str
        :param from_id: Optional source device ID (defaults to HGI or bound REM/DIS device)
        :type from_id: str | None
        :raises ValueError: If device_id is not provided or device not found
        """
        if not device_id:
            raise ValueError("device_id is required")

        # Find the device
        device = self._get_device(device_id)
        if not device:
            raise ValueError(f"Device {device_id} not found")

        # Get the list of parameters to request
        for param_id in _2411_PARAMS_SCHEMA:
            try:
                _LOGGER.debug("Requesting value for parameter %s", param_id)
                # Create parameter dictionary
                params: dict[str, Any] = {
                    "device_id": device_id,
                    "param_id": param_id,
                    "from_id": from_id,  # Either HGI or bound REM/DIS device
                }
                # Call directly with the params dict
                await self.async_get_fan_param(params)
                await asyncio.sleep(0.1)
            except Exception as ex:
                _LOGGER.warning(
                    "Failed to request parameter %s: %s", param_id, ex, exc_info=True
                )

    async def async_set_fan_param(self, call: ServiceCall) -> None:
        """Handle set_fan_param service call.

        This sends a parameter write request to the specified fan device. The response
        will be processed by the device's normal packet handling.

        :param call: Service call data containing device and parameter info
        :raises ValueError: If required parameters are missing or invalid
        :raises ValueError: If parameter ID is not a valid 2-digit hex value
        :raises ValueError: If device is not found or not a FAN device

        The call data should contain:
            - device_id (str): Target FAN device ID (required)
            - param_id (str): Parameter ID to write (required, 2 hex digits)
            - value: The value to set (required, type depends on parameter)
            - from_id (str, optional): Source device ID (defaults to HGI)
        """
        _LOGGER.debug("Processing set_fan_param service call with data: %s", call.data)

        try:
            # Get and validate parameters
            device_id = call.data.get("device_id")
            if not device_id:
                raise ValueError("device_id is required")

            # Get and validate device
            device = self._get_device(device_id)
            if not device or not hasattr(device, "get_bound_rem"):
                raise ValueError(f"Device {device_id} not found or is not a FAN device")

            # Get and validate parameter ID
            param_id = str(call.data.get("param_id", "")).upper()
            if not re.match(r"^[0-9A-F]{2}$", param_id):
                raise ValueError("Parameter ID must be a 2-digit hex value (00-FF)")

            # Get and validate value
            value = call.data.get("value")
            if value is None:
                raise ValueError("Value is required")

            # Get source device (from_id)
            from_id = call.data.get("from_id")
            if not from_id:
                from_id = device.get_bound_rem()
                if not from_id:
                    raise ValueError(
                        "No source device specified and no bound REM/DIS device found. "
                        "Please specify from_id or configure a bound device."
                    )

            # Log the operation
            _LOGGER.debug(
                "Setting parameter %s=%s on device %s from %s",
                param_id,
                value,
                device_id,
                from_id,
            )

            # Set up pending state
            entity = self._find_entity(device_id, param_id)
            self._set_entity_pending(entity, param_id)

            # Send command
            cmd = Command.set_fan_param(device_id, param_id, value, src_id=from_id)
            await self.client.async_send_cmd(cmd)
            await asyncio.sleep(0.2)  # Prevent flooding

            # Clear pending state after timeout
            if entity:
                asyncio.create_task(self._clear_pending_timeout(entity, param_id))

        except Exception as ex:
            _LOGGER.error("Failed to set fan parameter: %s", ex, exc_info=True)
            raise

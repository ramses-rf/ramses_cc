"""Broker for RAMSES integration.
This module provides the central coordination logic for the RAMSES RF integration.
It handles the lifecycle of the connection to the RAMSES network (via Serial or MQTT),
manages the discovery and update of entities, and routes service calls to the
underlying client library.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import datetime as dt, timedelta
from threading import Semaphore
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from ramses_rf import Gateway
from ramses_rf.device import Fakeable
from ramses_rf.device.base import Device
from ramses_rf.device.hvac import HvacRemoteBase, HvacVentilator
from ramses_rf.entity_base import Child, Entity as RamsesRFEntity
from ramses_rf.exceptions import BindingFlowFailed
from ramses_rf.schemas import SZ_CLASS, SZ_SCHEMA
from ramses_rf.system import Evohome, System, Zone
from ramses_tx.address import pkt_addrs
from ramses_tx.command import Command
from ramses_tx.const import SZ_ACTIVE_HGI, Code, DevType
from ramses_tx.exceptions import PacketAddrSetInvalid  # , TransportSourceInvalid
from ramses_tx.ramses import _2411_PARAMS_SCHEMA
from ramses_tx.schemas import (
    SZ_BOUND_TO,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_SERIAL_PORT,
    DeviceIdT,
    extract_serial_port,
)

# --- START: Safe Import for CallbackTransport ---
# We use a flag for logic checks, and 'Any' for type checks if missing.
# This satisfies Pylance (Any is a type) and Runtime (flag prevents crash).
if TYPE_CHECKING:
    from ramses_tx.transport import CallbackTransport

    HAS_MQTT_SUPPORT = True
else:
    try:
        from ramses_tx.transport import CallbackTransport

        HAS_MQTT_SUPPORT = True
    except ImportError:
        from typing import Any

        CallbackTransport = Any  # 'Any' is a valid type, 'None' is not.
        HAS_MQTT_SUPPORT = False

        _LOGGER = logging.getLogger(__name__)
        _LOGGER.warning(
            "ramses_rf library is too old: CallbackTransport not available. "
            "Home Assistant MQTT support will be disabled."
        )
# --- END: Safe Import ---

from .const import (
    CONF_COMMANDS,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
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
    from homeassistant.components.mqtt import ReceiveMessage

    from . import RamsesEntity

_LOGGER = logging.getLogger(__name__)

# We check for mqtt during config flow, but we handle the import safely here
try:
    from homeassistant.components import mqtt

    _LOGGER.debug("homeassistant.components.mqtt imported successfully")
except ImportError:
    mqtt = None

    _LOGGER.debug("homeassistant.components.mqtt not imported")

SAVE_STATE_INTERVAL: Final[timedelta] = timedelta(minutes=5)

_CALL_LATER_DELAY: Final = 5  # needed for tests


class RamsesBroker:
    """Central coordinator for the RAMSES integration.

    This class serves as the main bridge between Home Assistant and the RAMSES RF protocol.
    It manages the client connection, device discovery, entity lifecycle, and provides
    service endpoints for advanced operations like parameter reading/writing and packet
    injection. The broker handles the complexity of the RAMSES protocol while presenting
    a clean interface to Home Assistant's entity system.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the RAMSES broker and its data structures.

        Initializes the client connection placeholders and internal stores.
        Actual client startup occurs in :meth:`async_setup`.

        :param hass: The Home Assistant instance.
        :type hass: HomeAssistant
        :param entry: The configuration entry for this integration.
        :type entry: ConfigEntry

        .. note::
            Initializes the client connection. Calls async_setup() to complete initialization.
        """

        self.hass = hass
        self.entry = entry
        self.options = {**entry.data, **entry.options}
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        _LOGGER.debug("Config = %s", entry.options)

        self.client: Gateway = None
        self._remotes: dict[str, dict[str, str]] = {}

        self._platform_setup_tasks: dict[str, asyncio.Task[bool]] = {}

        self._entities: dict[str, RamsesEntity] = {}  # domain entities

        self._device_info: dict[str, DeviceInfo] = {}

        self.mqtt_bridge: RamsesMqttBridge | None = None

        # Discovered client objects...
        self._devices: list[Device] = []
        self._systems: list[System] = []
        self._zones: list[Zone] = []
        self._dhws: list[Zone] = []
        self._parameter_entities_created: set[str] = set()

        self._sem = Semaphore(value=1)

        # Initialize platforms dictionary to store platform references
        self.platforms: dict[str, Any] = {}

        self.learn_device_id: str | None = None  # TODO: can we do without this?

    async def async_setup(self) -> None:
        """Set up the RAMSES client and load configuration.

        This method loads cached packets from storage, creates and configures the
        RAMSES client (via Serial or MQTT), and establishes the initial connection.

        :raises ValueError: If there is a critical error in the configuration schema.
        :raises RuntimeError: If the client fails to start or connect.
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
        _LOGGER.debug("CONFIG_SCHEMA: %s", config_schema)
        if not schema_is_minimal(config_schema):  # move this logic into ramses_rf?
            _LOGGER.warning("The config schema is not minimal (consider minimising it)")

        cached_schema = client_state.get(SZ_SCHEMA, {})
        # issue #296: skip unknown devs from cached_schema if enforce_known_list
        # remains chance that while enforce_known was Off, a heat element is picked up
        # and added to the system schema and cached. Must clear system_cache to fix.
        _LOGGER.debug("CACHED_SCHEMA: %s", cached_schema)

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
            try:
                self.client = self._create_client(config_schema)
            except (ValueError, vol.Invalid) as err:
                _LOGGER.error(
                    "Critical error: Failed to initialise client with config schema: %s",
                    err,
                )
                raise ValueError(f"Failed to initialise RAMSES client: {err}") from err

        # Check if bridge exists (it might be None if library is old)
        if self.mqtt_bridge:
            # Lifecycle Management: Delayed Start
            # We must ensure the HA MQTT client is fully connected before starting
            # our bridge. This prevents "Client not connected" errors during boot.
            if not await mqtt.async_wait_for_mqtt_client(self.hass):
                _LOGGER.error(
                    "MQTT integration failed to start. RAMSES RF cannot proceed."
                )
                return

            await self.mqtt_bridge.async_start(self.client)
            # Add listener to stop bridge when entry unloads
            self.entry.async_on_unload(self.mqtt_bridge.async_stop)

        # Check if bridge exists (it might be None if library is old)
        if self.mqtt_bridge:
            # Lifecycle Management: Delayed Start
            # We must ensure the HA MQTT client is fully connected before starting
            # our bridge. This prevents "Client not connected" errors during boot.
            if not await mqtt.async_wait_for_mqtt_client(self.hass):
                _LOGGER.error(
                    "MQTT integration failed to start. RAMSES RF cannot proceed."
                )
                return

            await self.mqtt_bridge.async_start(self.client)
            # Add listener to stop bridge when entry unloads
            self.entry.async_on_unload(self.mqtt_bridge.async_stop)

        def cached_packets() -> dict[str, str]:  # dtm_str, packet_as_str
            msg_code_filter = ["313F"]  # ? 1FC9
            _known_list = self.options.get(SZ_KNOWN_LIST, {})

            # -----------------------------------------------
            # Retrieve all packets
            all_packets = client_state.get(SZ_PACKETS, {})

            # Simple optimization: If huge, only take the last 1000 to prevent startup timeout
            if len(all_packets) > 1000:
                _LOGGER.warning(
                    f"Packet cache too large ({len(all_packets)}). Loading only last 1000 to prevent timeout."
                )
                # Sort by timestamp (key) and take last 1000
                sorted_keys = sorted(all_packets.keys())[-1000:]
                all_packets = {k: all_packets[k] for k in sorted_keys}
            # -----------------------------------------------

            return {
                dtm: pkt
                for dtm, pkt in client_state.get(SZ_PACKETS, {}).items()
                if dt.fromisoformat(dtm) > dt.now() - timedelta(days=1)
                and pkt[41:45] not in msg_code_filter
                and (
                    # FIX: Use .get() to safely access CONF_RAMSES_RF, defaulting to empty dict
                    not self.options.get(CONF_RAMSES_RF, {}).get(SZ_ENFORCE_KNOWN_LIST)
                    or pkt[11:20] in _known_list
                    or pkt[21:30] in _known_list
                )
                # prevent adding unknown messages when known list is enforced
                # also add filter for block_list?
            }

        # NOTE: Warning: 'Detected blocking call to sleep inside the event loop'
        # - in pyserial: rfc2217.py, in Serial.open(): `time.sleep(0.05)`
        chpkt = cached_packets()
        _LOGGER.info(chpkt)
        await self.client.start(cached_packets=chpkt)
        self.entry.async_on_unload(self.client.stop)

    async def async_start(self) -> None:
        """Initialize the periodic update cycle for the RAMSES broker.

        This method performs an initial update of all devices and sets up
        background tasks for periodic polling and state persistence.
        It must be called after :meth:`async_setup`.

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

        Instantiates the `Gateway` class, handling the logic to select between
        Home Assistant MQTT transport or standard Serial/TCP transport based on
        configuration.

        :param schema: The configuration schema for the client.
        :type schema: dict[str, Any]
        :returns: A configured Gateway instance ready to be started.
        :rtype: Gateway

        .. note::
            This method creates a new Gateway instance with the provided configuration
            and sets up the necessary callbacks for device discovery and updates.
        """
        # Defaults
        port_name = None
        port_config = {}
        transport_constructor = None

        # Used for sanitization (may be modified in MQTT block)
        known_list = self.options.get(SZ_KNOWN_LIST, {})

        # 1. Check if we are using Home Assistant MQTT
        if self.options.get(CONF_MQTT_USE_HA):
            if not HAS_MQTT_SUPPORT:
                _LOGGER.error(
                    "Configuration asks for Home Assistant MQTT, but the installed "
                    "ramses_rf library is too old (missing CallbackTransport). "
                    "Please update the library."
                )
                # Fallback to avoid crash
                port_name, port_config = extract_serial_port(
                    self.options.get(SZ_SERIAL_PORT, {})
                )
            else:
                _LOGGER.info("Configuring RAMSES_RF to use Home Assistant MQTT")
                topic = self.options.get(CONF_MQTT_TOPIC, "RAMSES/GATEWAY")

                topic = self.options.get(CONF_MQTT_TOPIC, "RAMSES/GATEWAY")

                # Look for configured HGI candidates
                candidates = [
                    k
                    for k, v in known_list.items()
                    if v.get(SZ_CLASS) in (DevType.HGI, "gateway_interface")
                ]

                # Default to None, or the sentinel if that's all we have
                hgi_id = None

                # Filter out the sentinel to find "real" HGIs
                real_hgis = [x for x in candidates if x != "18:000730"]

                if real_hgis:
                    hgi_id = real_hgis[0]  # Pick the first real one
                    _LOGGER.info(
                        f"HGI Selection: Found {len(real_hgis)} real HGI(s), selected {hgi_id}"
                    )

                    # Sanitize known_list: remove phantom HGI if we have a real one
                    if "18:000730" in known_list:
                        known_list = dict(known_list)  # Create a copy to modify
                        del known_list["18:000730"]
                        _LOGGER.debug(
                            "Sanitized known_list: removed phantom HGI 18:000730"
                        )

                elif "18:000730" in candidates:
                    hgi_id = "18:000730"
                    _LOGGER.info("HGI Selection: Only sentinel HGI found")
                else:
                    _LOGGER.info("HGI Selection: No HGIs found in known_list")

                self.mqtt_bridge = RamsesMqttBridge(self.hass, topic, hgi_id=hgi_id)

                # IMPORTANT: Dummy string to satisfy library checks
                port_name = "mqtt://homeassistant"
                port_config = {}
                transport_constructor = self.mqtt_bridge.async_transport_constructor

        else:
            # 2. Fallback to Standard Serial / TCP
            # extract_serial_port handles Missing Keys gracefully by returning (None, None)
            port_name, port_config = extract_serial_port(
                self.options.get(SZ_SERIAL_PORT, {})
            )

        # 3. Final Safety Check
        # If port_name is still None (e.g. invalid config), force a dummy value
        # IF we have a transport constructor.
        if transport_constructor and not port_name:
            _LOGGER.warning("Port name missing in HA MQTT mode, forcing dummy")
            port_name = "/dev/null"

        # 4. Prepare Kwargs
        kwargs = {
            "loop": self.hass.loop,
            "port_config": port_config,
            "packet_log": self.options.get(SZ_PACKET_LOG, {}),
            "known_list": known_list,
            "config": self.options.get(CONF_RAMSES_RF, {}),
            **schema,
        }

        # CRITICAL SANITIZATION: Remove conflicting keys from kwargs
        # We are passing port_name positionally, so it MUST NOT be in kwargs
        for key in ["port_name", "input_file", "serial_port"]:
            if key in kwargs:
                _LOGGER.warning(f"Removing conflicting key '{key}' from Gateway kwargs")
                kwargs.pop(key)

        if transport_constructor:
            kwargs["transport_constructor"] = transport_constructor

        # DEBUG: Inspect the payload before launch
        _LOGGER.debug(
            f"Calling Gateway(port_name='{port_name}', kwargs={list(kwargs.keys())})"
        )

        # 5. Instantiate Gateway with Explicit Positional Argument
        client = Gateway(port_name, **kwargs)

        return client

    async def async_save_client_state(self, _: dt | None = None) -> None:
        """Save the current state of the RAMSES client to persistent storage.

        Persists critical data including the discovered network schema,
        packet cache, and remote command mappings.

        :param _: Unused parameter, required for compatibility with `async_track_time_interval`.
        :type _: dt | None

        .. note::
            This method saves important state information including:
            - Remote command mappings
            - Other client state that needs to persist between restarts
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
        """Retrieve a device object by its ID.

        :param device_id: The unique identifier of the device (e.g., '01:123456').
        :type device_id: str
        :returns: The device object if found, otherwise None.
        :rtype: Any | None
        """
        return next((d for d in self._devices if d.id == device_id), None)

    def async_register_platform(
        self,
        platform: EntityPlatform,
        add_new_devices: Callable[[RamsesRFEntity], None],
    ) -> None:
        """Register a Home Assistant entity platform with the broker.

        This allows the broker to dispatch new device discovery events to the
        appropriate platform (e.g., climate, sensor, water_heater).

        :param platform: The entity platform to register.
        :type platform: EntityPlatform
        :param add_new_devices: A callback function to add new entities to the platform.
        :type add_new_devices: Callable[[RamsesRFEntity], None]
        """
        platform_str = platform.domain if hasattr(platform, "domain") else platform
        _LOGGER.debug("Registering platform %s", platform_str)

        # Store the platform reference for entity lookup
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
        """Set up a specific Home Assistant platform.

        Ensures that the config entry setup for a given platform (e.g., 'climate')
        is forwarded and completed.

        :param platform: The domain of the platform to set up.
        :type platform: str
        :returns: True if the platform was set up successfully, False otherwise.
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
        except Exception as err:
            _LOGGER.error(
                "Error setting up %s platform: %s", platform, str(err), exc_info=True
            )
            return False

    async def async_unload_platforms(self) -> bool:
        """Unload all platforms associated with this integration.

        Typically called when the integration config entry is being unloaded.

        :returns: True if all platforms were unloaded successfully, False otherwise.
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

    def _create_parameter_entities(self, device: RamsesRFEntity) -> None:
        """Create parameter entities for a device supporting 2411 parameters.

        Creates Home Assistant `Number` entities for all supported 2411 parameters.
        These entities are automatically added to the number platform and
        will automatically receive parameter updates via the event system.

        :param device: The device (typically a FAN) to create parameter entities for.
        :type device: RamsesRFEntity

        .. note:: This method is called automatically during device setup and should
        not be called manually. Parameter entities are created only once per
        device per Home Assistant session.
        """
        device_id = device.id
        from .number import create_parameter_entities

        entities = create_parameter_entities(self, device)
        _LOGGER.debug(
            "create_parameter_entities returned %d entities for %s",
            len(entities),
            device_id,
        )
        if entities:
            _LOGGER.info(
                "Adding %d parameter entities for %s", len(entities), device_id
            )
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_DEVICES.format("number"),
                entities,
            )
        else:
            _LOGGER.debug("No parameter entities created for %s", device_id)

    _fan_bound_to_remote: dict[str, DeviceIdT] = {}
    # hold a reverse lookup dict of remote_id: parent_id's
    # used to find bound fan for a remote entity target

    async def _setup_fan_bound_devices(self, device: Device) -> None:
        """Set up bound devices for a FAN device.
        A FAN will only respond to 2411 messages on RQ from a bound device (REM/DIS).
        In config flow, a 'bound' trait can be added to a FAN to specify the bound device.
        Each bound device is added to the broker's _fan_bound_to_remote dict.

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

        # Use .get() and handle None/Empty immediately
        bound_device_id = device_config.get(SZ_BOUND_TO)
        if not bound_device_id:
            return

        # Explicit type check for safety
        if not isinstance(bound_device_id, str):
            _LOGGER.warning(
                "Cannot bind device %s to FAN %s: invalid bound device id type (%s)",
                bound_device_id,
                device.id,
                type(bound_device_id),
            )
            return

        _LOGGER.debug("Device config: %s", device_config)
        _LOGGER.debug("Device type: %s", device.type)
        _LOGGER.debug("Device class: %s", device.__class__)

        _LOGGER.info("Binding FAN %s and REM/DIS device %s", device.id, bound_device_id)

        # Find the bound device and get its type
        bound_device = next(
            (d for d in self.client.devices if d.id == bound_device_id),
            None,
        )

        if bound_device:
            # Determine the device type based on the class
            if isinstance(bound_device, HvacRemoteBase):
                device_type = DevType.REM
            elif hasattr(bound_device, "_SLUG") and bound_device._SLUG == DevType.DIS:
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
            # add the HvacVentilator device id to the broker's dict
            self._fan_bound_to_remote[str(bound_device_id)] = device.id
        else:
            _LOGGER.warning(
                "Bound device %s not found for FAN %s", bound_device_id, device.id
            )

    async def _async_setup_fan_device(self, device: Device) -> None:
        """Set up a FAN device and its parameter entities.

        Called during discovery to initialize specific logic for HVAC Ventilators,
        including parameter handling callbacks and initial data requests.

        :param device: The FAN device to set up.
        :type device: Device

        .. note::
            This method performs FAN-specific setup including:
            - Setting up bound REM/DIS devices
            - Setting up parameter handling callbacks
            - Creating parameter entities after the first message is received
            - Requesting all parameter values
        """
        _LOGGER.debug("Setting up device: %s", device.id)

        # For FAN devices, set up bound devices and parameter handling
        if hasattr(device, "_SLUG") and device._SLUG == "FAN":
            await self._setup_fan_bound_devices(device)

            # Set up the initialization callback - will be called on first message
            if hasattr(device, "set_initialized_callback"):

                async def on_fan_first_message() -> None:
                    """Handle the first message received from a FAN device.

                    Creates parameter entities and requests all parameter values.
                    Set as the initialization callback in hvac.py.
                    """
                    _LOGGER.debug(
                        "First message received from FAN %s, creating parameter entities",
                        device.id,
                    )
                    # Create parameter entities after first message is received
                    self._create_parameter_entities(device)
                    # Request all parameters after creating entities (non-blocking if fails)
                    _call: dict[str, DeviceIdT] = {
                        "device_id": device.id,
                    }
                    try:
                        self.get_all_fan_params(_call)
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to request parameters for device %s during startup: %s. "
                            "Entities will still work for received parameter updates.",
                            device.id,
                            err,
                        )

                device.set_initialized_callback(
                    lambda: self.hass.async_create_task(on_fan_first_message())
                )

            # Set up parameter update callback
            if hasattr(device, "set_param_update_callback"):
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
                            "ramses_cc.fan_param_updated",
                            {"device_id": dev_id, "param_id": param_id, "value": value},
                        )

                    return param_callback

                device.set_param_update_callback(create_param_callback(device.id))
                _LOGGER.debug(
                    "Set up parameter update callback for device %s", device.id
                )

            # Check if device is already initialized (e.g., from cached messages)
            # This handles the case where we restart but the device already has state
            if hasattr(device, "supports_2411") and device.supports_2411:
                if getattr(device, "_initialized", False):
                    _LOGGER.debug(
                        "Device %s already initialized, creating parameter entities and requesting parameters",
                        device.id,
                    )
                    self._create_parameter_entities(device)
                    async_dispatcher_send(
                        self.hass,
                        SIGNAL_NEW_DEVICES.format("number"),
                        [device],
                    )
                call: dict[str, Any] = {
                    "device_id": device.id,
                }
                try:
                    self.get_all_fan_params(call)
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to request parameters for device %s during setup: %s. "
                        "Entities will still work for received parameter updates.",
                        device.id,
                        err,
                    )

    def _update_device(self, device: RamsesRFEntity) -> None:
        """Update device information in the Home Assistant device registry.

        Synchronizes the device's metadata (model, name, via_device relationship)
        with the HA registry.

        :param device: The device to update.
        :type device: RamsesRFEntity
        """
        if hasattr(device, "name") and device.name:
            name = device.name  # only used for Zones _0004?
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

        device_registry = dr.async_get(self.hass)  # need it earlier to catch via-device

        # See issue 249: non-existing 'via_device' in tests/tests_old/test_init_data.py
        if isinstance(device, Zone) and device.tcs:
            _LOGGER.info(f"ZONE {model} via_device SET to {device.tcs}")
            via_device = (DOMAIN, device.tcs.id)
        elif isinstance(device, Child) and device._parent:
            _LOGGER.info(f"CHILD {model} via_device SET to {device._parent}")
            # try:
            #     # check for issue 249, not allowed after HA 2025.12
            #     # see core/homeassistant/helpers/device_registry.py L968
            #     _LOGGER.debug(f"Parent {device._parent} has id: {device._parent.id}")
            #     via = device_registry.async_get(device._parent.id)
            #     if via is None:
            #         _LOGGER.info(
            #             f"Parent device {device._parent} does not exist. Removing via"
            #         )
            #         via_device = None
            #     else:
            via_device = (DOMAIN, device._parent.id)
            # except TransportSourceInvalid:
            #     _LOGGER.info(f"Parent {device._parent} HAS NO ID")
            #     via_device = None
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
            return  # this device was already added to registry
        self._device_info[device.id] = device_info

        device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id, **device_info
        )

    async def async_update(self, _: dt | None = None) -> None:
        """Retrieve the latest state data from the client library.

        This method queries the `ramses_rf` library for the current state of devices,
        identifies new entities, updates the registry, and dispatches signals to
        notify platforms of changes.

        :param _: Unused parameter, required for compatibility with `async_track_time_interval`.
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
            await self._async_setup_fan_device(device)

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
        """Handle the `bind_device` service call.

        Initiates a binding handshake between a device (faked by the gateway)
        and a controller.
        This method will NOT set the 'bound' trait in config flow (yet).

        :param call: The service call object containing binding parameters.
        :type call: ServiceCall
        :raises LookupError: If the specified device ID is not found
        :raises HomeAssistantError: If the binding process fails

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
            raise HomeAssistantError(
                f"Device not found: {call.data.get('device_id')}"
            ) from err

        cmd = Command(call.data["device_info"]) if call.data["device_info"] else None

        _LOGGER.warning("Starting binding process for device %s", device.id)

        try:
            await device._initiate_binding_process(  # may: BindingFlowFailed
                list(call.data["offer"].keys()),
                confirm_code=list(call.data["confirm"].keys()),
                ratify_cmd=cmd,
            )  # TODO: will need to re-discover schema

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

        async_call_later(self.hass, _CALL_LATER_DELAY, self.async_update)

    async def async_force_update(self, _: ServiceCall) -> None:
        """Handle the `force_update` service call.

        Triggers an immediate refresh of all device states by calling :meth:`async_update`.
        It's typically used to manually refresh the state  of all devices when needed.

        :param _: Unused service call parameter (for callback compatibility)
        :type _: ServiceCall
        """

        await self.async_update()

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Handle the `send_packet` service call.

        Constructs and sends a raw command packet via the active transport layer.

        :param call: The service call object containing packet definition parameters.
        :type call: ServiceCall
        :raises ValueError: If packet construction fails due to invalid parameters.

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

        self._adjust_sentinel_packet(cmd)

        await self.client.async_send_cmd(cmd)
        async_call_later(self.hass, _CALL_LATER_DELAY, self.async_update)

    def _adjust_sentinel_packet(self, cmd: Command) -> None:
        """Fix address positioning for specific sentinel packets (18:000730).

        This checks if the packet addresses are valid for the HGI gateway and
        swaps address 1 and address 2 if necessary to ensure protocol compliance.

        :param cmd: The command object to check and adjust
        :type cmd: Command
        """
        # HACK: to fix the device_id when GWY announcing.
        # Current: I --- 18:000730 18:006402 --:------ 0008 002 00C3
        # Target:  I --- 18:000730 --:------ 18:006402 0008 002 00C3
        if cmd.src.id != "18:000730" or cmd.dst.id != self.client.hgi.id:
            return

        try:
            # Validate if the current address structure is acceptable
            pkt_addrs(self.client.hgi.id + cmd._frame[16:37])
        except PacketAddrSetInvalid:
            # If invalid, swap addr1 and addr2 to correct the structure
            cmd._addrs[1], cmd._addrs[2] = cmd._addrs[2], cmd._addrs[1]
            cmd._repr = None  # Invalidate cached representation
            _LOGGER.debug(
                "Swapped addresses for sentinel packet 18:000730 to maintain protocol validity"
            )

    # fan_param (2411) private and service methods.
    # Called from climate.py and remote.py as service @callback, or directly with dict (only)

    def _find_param_entity(self, device_id: str, param_id: str) -> RamsesEntity | None:
        """Find a parameter entity by device ID and parameter ID.

        Helper Method that searches for a number entity corresponding to a specific
        parameter on a device.
        This method handles device ID normalization automatically and searches both
        the entity registry and active platform entities.

        :param device_id: The device ID (supports both colon and underscore formats)
        :type device_id: str
        :param param_id: The parameter ID of the entity to find
        :type param_id: str
        :return: The found number entity or None if not found
        :rtype: Any | None
        """
        # Normalize device ID to use underscores and lowercase for entity ID (same as entity creation)
        safe_device_id = str(device_id).replace(":", "_").lower()
        target_entity_id = f"number.{safe_device_id}_param_{param_id.lower()}"

        # First try to find the entity in the entity registry
        ent_reg = er.async_get(self.hass)
        entity_entry = ent_reg.async_get(target_entity_id)
        if entity_entry:
            _LOGGER.debug("Found entity %s in entity registry", target_entity_id)
            # Get the actual entity from the platform to make sure entity is fully loaded
            platforms = self.platforms.get("number", [])
            _LOGGER.debug("Checking platforms: %s", platforms)
            for platform in platforms:
                if (
                    hasattr(platform, "entities")
                    and target_entity_id in platform.entities
                ):
                    return platform.entities[target_entity_id]
                else:
                    _LOGGER.debug(
                        "Entity %s not found in platform.entities (yet).",
                        target_entity_id,
                    )

            # Entity exists in registry but not yet loaded in platform
            _LOGGER.debug(
                "Entity %s exists in registry but not yet loaded in platform",
                target_entity_id,
            )
            return None

        _LOGGER.debug("Entity %s not found in registry.", target_entity_id)
        return None

    def _get_param_id(self, call: dict[str, Any]) -> str:
        """Get and validate parameter ID from service call data.

        Helper method that extracts and validates the parameter ID with consistent
        error handling and logging.

        :param call: Dict containing parameter info
        :type call: dict[str, Any]
        :return: The validated parameter ID as uppercase 2-digit hex string
        :rtype: str
        :raises ValueError: If the parameter ID is missing or invalid.
        """
        data = self._normalize_service_call(call)

        # Extract parameter ID
        param_id: str | None = data.get("param_id")
        if not param_id:
            _LOGGER.error("Missing required parameter: param_id")
            raise ValueError("required key not provided @ data['param_id']")

        # Convert to uppercase string for consistency
        param_id = str(param_id).upper()

        # Strip whitespace for normalization
        param_id = param_id.strip()

        # Validate parameter ID format (must be 2-digit hex)
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

        if not resolved_ids and (device_ids := target.get("device_id")):
            if isinstance(device_ids, str):
                device_ids = [device_ids]
            for device_id in device_ids:
                device_entry = dev_reg.async_get(device_id)
                if resolved := _device_entry_to_ramses_id(device_entry):
                    resolved_ids.append(resolved)

        if not resolved_ids and (area_ids := target.get("area_id")):
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
        device_id = data.get("device_id")
        if device_id:
            # Handle list case first
            if isinstance(device_id, list):
                if not device_id:  # Empty list
                    return None
                # If it's a list with one item, use that
                if len(device_id) == 1:
                    device_id = device_id[0]
                    data["device_id"] = (
                        device_id  # Update the data with the single value
                    )
                else:
                    # For multiple device IDs, log a warning and use the first one
                    _LOGGER.warning(
                        "Multiple device_ids provided, using first one: %s",
                        device_id[0],
                    )
                    device_id = device_id[0]
                    data["device_id"] = (
                        device_id  # Update the data with the first value
                    )

            # Now handle the single device_id case
            if isinstance(device_id, str):
                if ":" in device_id or "_" in device_id:
                    return device_id
                if resolved := self._target_to_device_id({"device_id": [device_id]}):
                    data["device_id"] = resolved
                    return resolved
            else:
                # Handle other types (shouldn't normally happen)
                device_id = str(device_id)
                data["device_id"] = device_id
                return device_id

        # Support UI device selection via services.yaml device selector field
        # (a HA device registry id/UUID), while keeping device_id for RAMSES ids.
        ha_device_id = data.get("device")
        if ha_device_id:
            if isinstance(ha_device_id, list):
                if not ha_device_id:
                    return None
                if len(ha_device_id) > 1:
                    _LOGGER.warning(
                        "Multiple devices provided, using first one: %s",
                        ha_device_id[0],
                    )
                ha_device_id = ha_device_id[0]
                data["device"] = ha_device_id
            if isinstance(ha_device_id, str):
                if resolved := self._target_to_device_id({"device_id": [ha_device_id]}):
                    data["device_id"] = resolved
                    return resolved

        # Try to get device_id from target
        target = data.get("target")
        if target and (resolved := self._target_to_device_id(target)):
            data["device_id"] = resolved
            return resolved

        return None

    def _get_device_and_from_id(self, call: dict[str, Any]) -> tuple[str, str, str]:
        """Get device_id and from_id with validation and fallback logic.

        Resolves the source ID using fallback logic:
        1. Explicit `from_id` in call data.
        2. Bound device (e.g., Remote) if configured.
        3. Fails if neither is available.

        :param call: Dict containing device and parameter info
        :type call: dict[str, Any]
        :return: Tuple of (original_device_id, normalized_device_id, from_id)
        :param call: The service call or data dictionary.
        :type call: ServiceCall | dict[str, Any]
        :returns: A tuple of (original_device_id, normalized_device_id, from_id).
                  Returns empty strings if validation fails.
        :rtype: tuple[str, str, str]
        """
        # Handle both ServiceCall and direct dict inputs
        data: dict[str, Any] = call.data if hasattr(call, "data") else call

        # Extract and validate device_id - _resolve_device_id now always returns str or None
        device_id = self._resolve_device_id(data)

        if not device_id:
            _LOGGER.error("Missing or invalid device_id")
            return "", "", ""  # Return empty strings to indicate validation failure

        # At this point, device_id is guaranteed to be a non-empty string
        original_device_id = device_id
        try:
            split_id = device_id.split(".")[1]
        except IndexError:
            split_id = device_id
        normalized_device_id = split_id.replace(":", "_").lower()

        # Get from_id with fallback logic (same as _get_from_id)
        from_id = data.get("from_id")
        if from_id:
            return original_device_id, normalized_device_id, str(from_id)

        # Try to get device for bound device lookup (for set_fan_param operations)
        try:
            device = self._get_device(original_device_id)
            if device and hasattr(device, "get_bound_rem"):
                bound_device_id = device.get_bound_rem()
                if bound_device_id:
                    _LOGGER.debug("Using bound device %s as from_id", bound_device_id)
                    return original_device_id, normalized_device_id, bound_device_id
                else:
                    # No bound device configured - this is expected for many setups
                    _LOGGER.debug(
                        "FAN device %s has no bound REM/DIS device configured. "
                        "Parameter requests will be skipped to avoid communication timeouts.",
                        original_device_id,
                    )
                    return "", "", ""  # Signal that no valid source is available
        except Exception:
            # Ignore device lookup errors
            pass

        # Explicit from_id was required but not found
        _LOGGER.warning(
            "No source device ID available for %s. "
            "FAN parameter operations require a bound REM/DIS device or explicit from_id.",
            original_device_id,
        )
        return "", "", ""  # Return empty strings to indicate no valid source

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

    async def async_get_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Handle 'get_fan_param' dict.

        This sends a parameter read request to the specified fan device.
        Fire and Forget, The response from the fan will be processed by the device's
        normal message handling.
        It can also be called from other methods using a dict.

        :param call: Dict containing device and parameter info
        :type call: dict[str, Any]
        :raises ValueError: If required parameters are missing or invalid
        :raises ValueError: If device is not found or not a FAN device
        :raises ValueError: If parameter ID is not a valid 2-digit hex value

        The call dict should contain:
            - device_id (str): Target device ID (supports colon/underscore formats)
            - param_id (str): Parameter ID to read (2 hex digits)
        and optionally:
            - from_id (str): Source device ID (defaults to bound_REM)
        """
        try:
            data = self._normalize_service_call(call)

            _LOGGER.debug("Processing get_fan_param service call with data: %s", data)

            # Extract id's
            original_device_id, normalized_device_id, from_id = (
                self._get_device_and_from_id(data)
            )
            param_id = self._get_param_id(data)

            # Check if we got valid source device info
            if not all([original_device_id, normalized_device_id, from_id]):
                _LOGGER.warning(
                    "Cannot get parameter: No valid source device available from %s. "
                    "Need either: explicit from_id, or a REM/DIS device that was 'bound' in the configuration.",
                    data,
                )
                return

            # Find the corresponding entity and set it to pending
            entity = self._find_param_entity(normalized_device_id, param_id)
            if entity and hasattr(entity, "set_pending"):
                entity.set_pending()

            cmd = Command.get_fan_param(original_device_id, param_id, src_id=from_id)
            _LOGGER.debug("Sending command: %s", cmd)

            # Send the command directly using the gateway
            await self.client.async_send_cmd(cmd)

            # Clear pending state after timeout (non-blocking)
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(30))
        except ValueError as err:
            # Log validation errors but don't re-raise them for edge cases
            _LOGGER.error("Failed to get fan parameter: %s", err)
            return
        except Exception as err:
            _LOGGER.error("Failed to get fan parameter: %s", err, exc_info=True)
            # Clear pending state on error
            if (
                "entity" in locals()
                and entity
                and hasattr(entity, "_clear_pending_after_timeout")
            ):
                asyncio.create_task(entity._clear_pending_after_timeout(0))
            raise

    def get_all_fan_params(self, call: dict[str, Any] | ServiceCall) -> None:
        """Wrapper for _async_run_fan_param_sequence.
        Create a task to run the fan parameter sequence without blocking HA.
        This allows for the sequence to run in the background while HA remains responsive.

        :param call: Dict containing device info
        :type call: dict[str, Any]
        :raises ValueError: If device_id is not provided or device not found
        :raises ValueError: If device is not a FAN device
        :raises RuntimeError: If communication with device fails

        The call dict should contain:
            - device_id (str): Target device ID (required, supports colon/underscore formats)
        and optionally:
            - from_id (str): Source device ID (defaults to Bound Rem or HGI)
        """
        self.hass.loop.create_task(self._async_run_fan_param_sequence(call))

    async def _async_run_fan_param_sequence(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Handle 'update_fan_params' service call (or direct dict).

        This service sends parameter read requests (RQ) for each parameter defined
        in the 2411 parameter schema to the specified FAN device. Each request is
        sent sequentially with a small delay to avoid overwhelming the device.
        It can also be called from other methods using a dict.

        :param call: Dict containing device info
        :type call: dict[str, Any]
        :raises ValueError: If device_id is not provided or device not found
        :raises ValueError: If device is not a FAN device
        :raises RuntimeError: If communication with device fails

        The call dict should contain:
            - device_id (str): Target device ID (supports colon/underscore formats)
        and optionally:
            - from_id (str): Source device ID (defaults to Bound REM/DIS)

        note: This method is called by get_all_fan_params() and should not be called directly.
        """
        try:
            data = self._normalize_service_call(call)

            _LOGGER.debug(
                "Processing update_fan_params service call with data: %s", data
            )

            # Get the list of parameters to request
            # Add delay between requests to prevent flooding the RF protocol
            for idx, param_id in enumerate(_2411_PARAMS_SCHEMA):
                # Create parameter-specific data by copying base data and adding param_id
                # Handle different types of mapping objects safely
                try:
                    param_data = dict(data)
                except (TypeError, ValueError):
                    # If dict() fails, try to copy as a regular dict
                    param_data = (
                        {k: v for k, v in data.items()}
                        if hasattr(data, "items")
                        else data
                    )
                param_data["param_id"] = param_id
                await self.async_get_fan_param(param_data)

                # Add delay between requests (except after the last one)
                # This prevents overwhelming the device and protocol buffer
                if idx < len(_2411_PARAMS_SCHEMA) - 1:
                    await asyncio.sleep(0.5)  # 500ms between requests
        except Exception as err:
            _LOGGER.error("Failed to get fan parameters for device: %s", err)
            # Don't re-raise the exception - handle it gracefully like other methods
            return

    async def async_set_fan_param(self, call: dict[str, Any] | ServiceCall) -> None:
        """Handle 'set_fan_param' service call (or direct dict).

        This service sends a parameter write request (WR) to the specified FAN device to
        set a parameter value. Fire and Forget - The request is sent asynchronously and
        the response will be processed by the device's normal packet handling.

        :param call: Dictionary containing device info or ServiceCall object
        :type call: dict[str, Any] | ServiceCall
        :raises HomeAssistantError: If validation fails or communication errors occur

        The call dict should contain:
            - device_id (str): Target device ID (supports colon/underscore formats)
            - param_id (str): Parameter ID to write (2 hex digits)
            - value (int): The value to set (type depends on parameter), -1 if not provided
        and optionally:
            - from_id (str): Source device ID (defaults to bound REM/DIS)
        """
        try:
            data = self._normalize_service_call(call)

            _LOGGER.debug("Processing set_fan_param service call with data: %s", data)

            # Extract id's
            original_device_id, normalized_device_id, from_id = (
                self._get_device_and_from_id(data)
            )

            # Check if we got valid source device info
            if not all([original_device_id, normalized_device_id, from_id]):
                msg = (
                    f"Cannot set parameter: No valid source device available from {data}. "
                    "Need either: explicit from_id, or a REM/DIS device that was 'bound' in the configuration."
                )
                _LOGGER.warning(msg)
                raise HomeAssistantError(msg)

            param_id = self._get_param_id(data)

            # Get and validate value
            value = data.get("value")
            if value is None:
                raise ValueError("Missing required parameter: value")

            # Log the operation
            _LOGGER.debug(
                "Setting parameter %s=%s on device %s from %s",
                param_id,
                value,
                original_device_id,
                from_id,
            )

            # Set up pending state
            entity = self._find_param_entity(normalized_device_id, param_id)
            if entity and hasattr(entity, "set_pending"):
                entity.set_pending()

            # Send command
            cmd = Command.set_fan_param(
                original_device_id, param_id, value, src_id=from_id
            )
            await self.client.async_send_cmd(cmd)
            await asyncio.sleep(0.2)

            # Clear pending state after timeout (non-blocking)
            if entity and hasattr(entity, "_clear_pending_after_timeout"):
                asyncio.create_task(entity._clear_pending_after_timeout(30))

        except ValueError as err:
            # Raise friendly error for UI
            raise HomeAssistantError(
                f"Invalid parameter for set_fan_param: {err}"
            ) from err
        except Exception as err:
            _LOGGER.error("Failed to set fan parameter: %s", err, exc_info=True)
            raise HomeAssistantError(f"Failed to set fan parameter: {err}") from err


class RamsesMqttBridge:
    """The Bridge between Home Assistant MQTT and ramses_rf.

    This class implements the 'Inversion of Control' pattern by providing
    the transport_constructor and io_writer callbacks required by ramses_rf.
    It translates MQTT messages to protocol frames and vice-versa.
    """

    def __init__(
        self, hass: HomeAssistant, topic_root: str, hgi_id: str | None = None
    ) -> None:
        """Initialize the MQTT bridge.

        :param hass: The Home Assistant instance.
        :type hass: HomeAssistant
        :param topic_root: The root MQTT topic for this gateway.
        :type topic_root: str
        :param hgi_id: The device ID of the HGI (if known).
        :type hgi_id: str | None
        """
        self._hass = hass
        self._topic_root = topic_root if topic_root.endswith("/") else f"{topic_root}/"
        self._hgi_id = hgi_id

        # Topics
        # We listen to everything under the root to catch incoming data
        self._sub_topic = f"{self._topic_root}#"

        self._transport: CallbackTransport | None = None
        self._gateway: Gateway | None = None

        self._mqtt_unsub: Callable[[], None] | None = None
        self._status_unsub: Callable[[], None] | None = None

        self._queue: deque[tuple[str, str]] = deque()

    async def async_transport_constructor(
        self,
        protocol: Any,
        disable_sending: bool = False,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> CallbackTransport:
        """Factory function injected into ramses_rf to create the transport layer.

        :param protocol: The protocol instance from ramses_rf.
        :type protocol: Any
        :param kwargs: Additional arguments for transport creation.
        :type kwargs: Any
        :returns: A configured CallbackTransport connected to this bridge's IO.
        :rtype: CallbackTransport
        """
        _LOGGER.debug("Initializing CallbackTransport for MQTT bridge")

        if not HAS_MQTT_SUPPORT:
            raise ImportError("CallbackTransport class not found in ramses_rf library")

        # FIX: Inject Active HGI ID so protocol.py doesn't complain "Active Gateway None"
        if self._hgi_id:
            extra = extra or {}
            extra[SZ_ACTIVE_HGI] = self._hgi_id

        self._transport = CallbackTransport(
            protocol,
            io_writer=self._async_mqtt_publish,
            disable_sending=disable_sending,
            extra=extra,
        )

        # CRITICAL: Activate the transport (ramses_rf 0.52.5+ IoC Requirement)
        # We must signal to the protocol that the connection is made because
        # CallbackTransport is passive and doesn't do it automatically.
        protocol.connection_made(self._transport, ramses=True)
        # CRITICAL: Enable reading by default (ramses_rf 0.52.5+ IoC Requirement)
        if mqtt:
            self._status_unsub = mqtt.async_subscribe_connection_status(
                self._hass, self._on_mqtt_connection_state_changed
            )

        self._transport.resume_reading()
        _LOGGER.info(
            "RamsesMqttBridge: Protocol connection established explicitly in constructor"
        )

        return self._transport

    @callback
    def _on_mqtt_connection_state_changed(self, is_connected: bool) -> None:
        """Handle MQTT connection state changes."""
        if is_connected:
            _LOGGER.info("MQTT connected: Flushing packet queue")
            self._hass.async_create_task(self._async_flush_queue())

    async def _async_flush_queue(self) -> None:
        """Flush the packet queue."""
        while self._queue:
            q_topic, q_payload = self._queue.popleft()
            # Parse payload back to frame for logging (payload is json {"msg": "frame"})
            try:
                q_frame = json.loads(q_payload)["msg"]
            except (json.JSONDecodeError, KeyError):
                q_frame = "unknown"

            await mqtt.async_publish(
                self._hass, q_topic, q_payload, qos=0, retain=False
            )
            _LOGGER.info("Released queued packet: %s", q_frame)

    async def _async_mqtt_publish(self, frame: str) -> None:
        """IO Writer callback: Publish raw packets to MQTT.

        Format:
        Topic: ``RAMSES/GATEWAY/<gateway_id>/tx``
        Payload: ``{"msg": "..."}``

        :param frame: The raw packet string to publish.
        :type frame: str
        """
        if not self._gateway:
            _LOGGER.warning("Attempted to publish before Gateway is ready")
            return

        # Extract Active Gateway ID to build the topic
        # Default to generic if unknown (shouldn't happen in normal op)
        gwy_id = (
            self._gateway.hgi.id if self._gateway.hgi else (self._hgi_id or "18:000730")
        )

        topic = f"{self._topic_root}{gwy_id}/tx"
        payload = json.dumps({"msg": frame})

        # Section 6.1: Boundary Logging (Outgoing)
        _LOGGER.debug(f"Publishing to MQTT: {topic} -> {payload}")

        try:
            # Check if MQTT is connected
            if mqtt and not mqtt.is_connected(self._hass):
                self._queue.append((topic, payload))
                _LOGGER.warning(
                    "MQTT not connected - Queueing packet: %s",
                    frame,
                )
                return

            # Flush queue if connected
            await self._async_flush_queue()

            await mqtt.async_publish(self._hass, topic, payload, qos=0, retain=False)

        except Exception as err:
            _LOGGER.error(f"Failed to publish to MQTT: {err}")
            # We do not raise here to prevent crashing the protocol loop

        # CRITICAL FIX: Loopback for ramses_rf QosProtocol
        # "Echo" the packet back to the transport immediately to satisfy QosProtocol,
        # which expects to "hear" the packet on the bus after transmission.
        # This prevents the 'echo_timeout' errors seen in logs.
        if self._transport:
            # Timestamp: Use current time (naive, as per ramses_rf expectation)
            dtm = dt.now().isoformat()
            # CRITICAL: Packet.from_file expects "RSSI <Frame>" (offset 4).
            # TX frames lack RSSI, so we prepend "000 ".
            # We MUST use rstrip() to preserve leading space (e.g. " I") required by regex.
            self._transport.receive_frame(f"000 {frame.rstrip()}", dtm=dtm)

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Handle incoming MQTT messages from Home Assistant.

        Parses the payload and injects valid packets into the ramses_rf transport.

        :param msg: The MQTT message object containing topic and payload.
        :type msg: ReceiveMessage
        """
        # msg.topic, msg.payload, msg.qos

        # 1. Parsing Logic
        # Expected Topic: RAMSES/GATEWAY/<device_id>/rx
        # Expected Payload: JSON {"ts": "...", "msg": "..."}

        try:
            if (
                "cc1101_state" in msg.topic
            ):  # Skip state messages from ramses_esp_eth loaded device
                return

            _LOGGER.debug(
                f"Received MQTT message: topic={msg.topic}, payload={msg.payload}"
            )

            # Check if this is a 'rx' topic (incoming data)
            if not msg.topic.endswith("/rx"):
                return

            payload_str = str(msg.payload)
            json_data = json.loads(payload_str)

            # Extract generic packet string
            packet = json_data.get("msg")
            timestamp_str = json_data.get("ts")

            # CRITICAL FIX: Timezone Mismatch
            # ramses_rf uses naive datetimes (dt.now()) internally for expiration checks.
            # If MQTT provides an aware datetime (ISO string with offset/Z), it causes a TypeError.
            # We must force the timestamp to be naive (local time) before passing it to the transport.
            if timestamp_str:
                try:
                    dtm = dt.fromisoformat(timestamp_str)
                    if dtm.tzinfo is not None:
                        # Convert to naive (drop timezone info)
                        dtm = dtm.replace(tzinfo=None)
                        timestamp_str = dtm.isoformat()
                except ValueError:
                    pass  # Let the transport handle invalid formats (or just pass raw string)

            timestamp = timestamp_str

            if not packet:
                _LOGGER.debug(f"Ignored invalid JSON payload: {payload_str}")
                return

            # Section 6.1: Boundary Logging (Incoming)
            # Detailed logging is handled inside CallbackTransport.receive_frame
            # but we log connection inference here.

            # 2. Inject into Transport
            if self._transport:
                self._transport.receive_frame(packet, dtm=timestamp)

                # 3. Gateway Identification (HACK: IoC Side-effect)
                # We need to ensure ramses_rf knows which gateway ID is active
                # derived from the topic structure.
                # Topic: root/DEVICE_ID/rx
                try:
                    parts = msg.topic.split("/")
                    device_id = parts[-2]

                    # Section 6.1: Boundary Logging (HGI Detection)
                    _LOGGER.debug(
                        f"HGI Detection: Inspecting topic {msg.topic}, extracted candidate ID: {device_id}"
                    )

                    # We inject this into the transport's extra info if not set
                    # This allows the protocol to identify the HGI
                    if "18:" in device_id:
                        # Accessing private attribute is necessary for this IoC pattern
                        # to set the "Active HGI"
                        current_hgi = self._transport.get_extra_info(SZ_ACTIVE_HGI)
                        if not current_hgi:
                            _LOGGER.info(
                                f"Inferred active HGI from MQTT topic: {device_id}"
                            )
                            self._transport._extra[SZ_ACTIVE_HGI] = device_id
                        elif current_hgi != device_id:
                            _LOGGER.warning(
                                f"HGI conflict detected: topic implies {device_id} but active HGI is {current_hgi}"
                            )
                        else:
                            _LOGGER.debug(
                                f"Confirmed active HGI matches MQTT topic: {device_id}"
                            )

                except IndexError:
                    pass

        except json.JSONDecodeError:
            _LOGGER.warning(f"Received malformed JSON on {msg.topic}: {msg.payload}")
        except Exception as err:
            _LOGGER.exception(f"Unexpected error processing MQTT message: {err}")

    async def async_start(self, gateway: Gateway) -> None:
        """Start the bridge: Subscribe to topics and monitor connection.

        :param gateway: The initialized RAMSES gateway instance.
        :type gateway: Gateway
        """
        self._gateway = gateway

        # 1. Subscribe to MQTT
        _LOGGER.debug(f"Subscribing to {self._sub_topic}")
        self._mqtt_unsub = await mqtt.async_subscribe(
            self._hass, self._sub_topic, self._handle_mqtt_message, qos=0
        )

        # 2. Setup Circuit Breaker (Connection Status)
        # We assume the user has configured the standard HA status topic or we use the availability API
        # Section 4.2: Circuit Breaker
        self._status_unsub = mqtt.async_subscribe_connection_status(
            self._hass, self._handle_connection_status
        )

        # Trigger initial check
        if mqtt.is_connected(self._hass):
            self._handle_connection_status(True)

    @callback
    def _handle_connection_status(self, connected: bool) -> None:
        """Handle MQTT connection status changes (Circuit Breaker).

        Pauses the transport when MQTT is disconnected to prevent write errors.

        :param connected: True if MQTT is connected, False otherwise.
        :type connected: bool
        """
        if not self._transport:
            return

        if connected:
            _LOGGER.info("MQTT Connected: Resuming ramses_rf transport")
            self._transport.resume_reading()
            if self._gateway:
                self._gateway._protocol.resume_writing()
        else:
            _LOGGER.warning("MQTT Disconnected: Pausing ramses_rf transport")
            self._transport.pause_reading()
            if self._gateway:
                self._gateway._protocol.pause_writing()

    async def async_stop(self) -> None:
        """Stop the bridge and cleanup subscriptions."""
        if self._mqtt_unsub:
            self._mqtt_unsub()
            self._mqtt_unsub = None

        if self._status_unsub:
            self._status_unsub()
            self._status_unsub = None

        _LOGGER.info("RamsesMqttBridge stopped")

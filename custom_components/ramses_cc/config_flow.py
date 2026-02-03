"""Config flow to configure Ramses integration."""

import asyncio
import logging
import re
from abc import abstractmethod
from copy import deepcopy
from typing import Any, Final
from urllib.parse import urlparse

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.components import mqtt, usb
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.storage import Store
from serial.tools import list_ports  # type: ignore[import-untyped]

from ramses_rf.schemas import (
    SCH_GATEWAY_DICT,
    SCH_GLOBAL_SCHEMAS,
    SZ_RESTORE_CACHE,
    SZ_SCHEMA,
)
from ramses_tx.const import Code
from ramses_tx.schemas import (
    SCH_ENGINE_DICT,
    SCH_SERIAL_PORT_CONFIG,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_FILE_NAME,
    SZ_KNOWN_LIST,
    SZ_LOG_ALL_MQTT,
    SZ_PACKET_LOG,
    SZ_PORT_NAME,
    SZ_ROTATE_BACKUPS,
    SZ_ROTATE_BYTES,
    SZ_SERIAL_PORT,
    SZ_SQLITE_INDEX,
)

from .const import (
    CONF_ADVANCED_FEATURES,
    CONF_MESSAGE_EVENTS,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    CONF_SEND_PACKET,
    DEFAULT_HGI_ID,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    SZ_CLIENT_STATE,
    SZ_PACKETS,
)
from .schemas import SCH_GLOBAL_TRAITS_DICT

_LOGGER = logging.getLogger(__name__)

CONF_MANUAL_PATH: Final = "Enter Manually..."  # TODO i18n this string
CONF_MQTT_PATH: Final = "MQTT Broker..."
CONF_HA_MQTT_PATH: Final = "Use Home Assistant MQTT - In development!"
CONF_ZIGBEE_DEVICE: Final = "Choose Zigbee device"


def get_usb_ports() -> dict[str, str]:
    """Return a dict of USB ports and their friendly names."""
    ports = list_ports.comports()
    port_descriptions = {}
    for port in ports:
        vid: str | None = None
        pid: str | None = None
        if port.vid is not None and port.pid is not None:
            usb_device = usb.usb_device_from_port(port)
            vid = usb_device.vid
            pid = usb_device.pid
        dev_path = usb.get_serial_by_id(port.device)
        human_name = usb.human_readable_device_name(
            dev_path,
            port.serial_number,
            port.manufacturer,
            port.description,
            vid,
            pid,
        )
        port_descriptions[dev_path] = human_name
    return port_descriptions


async def async_get_usb_ports(hass: HomeAssistant) -> dict[str, str]:
    """Return a dict of USB ports and their friendly names."""
    return await hass.async_add_executor_job(get_usb_ports)


class BaseRamsesFlow(FlowHandler):
    """Mixin for common Ramses flow steps and forms."""

    options: dict[str, Any]

    def __init__(self, initial_setup: bool = False) -> None:
        """Initialize flow."""
        super().__init__()
        self._initial_setup = initial_setup
        self._manual_serial_port = False
        self._discovery_failed = False  # Track if discovery failed

    def get_options(self) -> None:
        """Load options from the config entry or initialize defaults.

        Populates `self.options` from the existing config entry if available.
        Otherwise, it initializes defaults or preserves options accumulating
        during the current flow step.
        """
        if (
            hasattr(self, "config_entry")
            and self.config_entry is not None
            and self.config_entry.options is not None
        ):
            options = deepcopy(dict(self.config_entry.options))
        else:  # create an empty config_entry for new installs
            # Preserve any existing options that were set during the current flow
            options = getattr(self, "options", {})
        options.setdefault(CONF_RAMSES_RF, {})
        options.setdefault(SZ_SERIAL_PORT, {})
        self.options = options

    @abstractmethod
    def _async_save(self) -> FlowResult:
        """Finish the flow."""

    async def _discover_mqtt_hgi(self) -> str | None:
        """Discover HGI device on MQTT."""
        # Use a future to capture the first result
        found_device: asyncio.Future[str | None] = self.hass.loop.create_future()

        @callback
        def _msg_callback(msg: Any) -> None:
            if found_device.done():
                return

            # _LOGGER.debug("MQTT Discovery received: %s", msg.topic)

            # Topic format: RAMSES/GATEWAY/{device_id}/...
            # We subscribe to wildcard #, so we split and look for 18:xxxxxx anywhere
            try:
                parts = msg.topic.split("/")
                for part in parts:
                    if part.startswith("18:"):
                        _LOGGER.debug(f"Discovery found device: {part}")
                        found_device.set_result(part)
                        return
            except Exception:
                pass

        # Determine topic to scan. Use default if not set.
        # We use a wildcard # to catch ANY topic (rx, status, etc) that might be retained
        scan_topic = f"{DEFAULT_MQTT_TOPIC}/#"
        _LOGGER.debug(f"Starting discovery on topic: {scan_topic}")

        try:
            # We must be careful if MQTT is not fully loaded, though check is done before calling this
            unsub = await mqtt.async_subscribe(self.hass, scan_topic, _msg_callback)
            try:
                # Wait up to 5 seconds. If there are retained messages, this will be instant.
                return await asyncio.wait_for(found_device, timeout=5.0)
            except TimeoutError:
                _LOGGER.debug("Discovery timed out")
                return None
            finally:
                unsub()
        except Exception as err:
            _LOGGER.warning("MQTT discovery failed: %s", err)
            return None

    async def async_step_choose_serial_port(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ramses choose serial port step."""
        self.get_options()  # not available during init
        errors: dict[str, str] = {}

        # --- PART 1: Handle the User's Selection ---
        if user_input is not None:
            port_name = user_input[SZ_PORT_NAME]

            if port_name == CONF_MQTT_PATH:
                return await self.async_step_mqtt_config()
            elif port_name == CONF_HA_MQTT_PATH:
                mqtt_entries = self.hass.config_entries.async_entries("mqtt")
                if not any(
                    entry.state == ConfigEntryState.LOADED for entry in mqtt_entries
                ):
                    errors["base"] = "mqtt_missing"
                else:
                    self.options[CONF_MQTT_USE_HA] = True
                    self.options.setdefault(CONF_MQTT_HGI_ID, DEFAULT_HGI_ID)
                    self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = "mqtt_ha"

                    # Perform discovery
                    if self._initial_setup:
                        discovered_id = await self._discover_mqtt_hgi()
                        if discovered_id:
                            self.options[CONF_MQTT_HGI_ID] = discovered_id
                            self._discovery_failed = False
                        else:
                            # Discovery failed, flag it for the next step
                            self._discovery_failed = True

                    if self._initial_setup:
                        return await self.async_step_config()
                    return self._async_save()
            elif port_name == CONF_ZIGBEE_DEVICE:
                return await self.async_step_zigbee_device()
            elif port_name == CONF_MANUAL_PATH:
                self._manual_serial_port = True
            else:
                self.options.pop(CONF_MQTT_USE_HA, None)
                self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = user_input[SZ_PORT_NAME]
                _LOGGER.debug(
                    f"DEBUG: Saved port_name = {user_input[SZ_PORT_NAME]} to options"
                )
            if not errors:
                return await self.async_step_configure_serial_port()

        # --- PART 2: Prepare the Menu ---
        ports = await async_get_usb_ports(self.hass)

        # Check for MQTT availability to adjust label
        mqtt_entries = self.hass.config_entries.async_entries("mqtt")
        mqtt_ready = any(
            entry.state == ConfigEntryState.LOADED for entry in mqtt_entries
        )
        mqtt_label = CONF_HA_MQTT_PATH
        if not mqtt_ready:
            if mqtt_entries:
                mqtt_label = f"{CONF_HA_MQTT_PATH} (MQTT integration not ready)"
            else:
                mqtt_label = f"{CONF_HA_MQTT_PATH} (MQTT integration not found)"

        # Always add options
        ports[CONF_HA_MQTT_PATH] = mqtt_label
        ports[CONF_MQTT_PATH] = CONF_MQTT_PATH
        ports[CONF_ZIGBEE_DEVICE] = CONF_ZIGBEE_DEVICE
        ports[CONF_MANUAL_PATH] = CONF_MANUAL_PATH

        port_name = self.options[SZ_SERIAL_PORT].get(SZ_PORT_NAME)
        if self.options.get(CONF_MQTT_USE_HA):
            default_port = CONF_HA_MQTT_PATH
        elif port_name is None:
            default_port = vol.UNDEFINED
        elif port_name in ports:
            default_port = port_name
        else:
            default_port = CONF_MANUAL_PATH

        data_schema = {
            vol.Required(
                SZ_PORT_NAME,
                default=default_port,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=k, label=v)
                        for k, v in ports.items()
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }

        return self.async_show_form(
            step_id="choose_serial_port",
            data_schema=vol.Schema(data_schema),
            errors=errors,
            last_step=False,
        )

    async def async_step_mqtt_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow user to enter MQTT details separately."""

        if user_input is not None:
            # 1. Extract data from the form
            host = user_input.get("host")
            port = user_input.get("port")
            username = user_input.get("username")
            password = user_input.get("password")

            # 2. Construct the connection string
            # Format: mqtt://user:pass@host:port
            if username or password:
                safe_user = username if username else ""
                safe_pass = password if password else ""
                auth = f"{safe_user}:{safe_pass}@"
            else:
                auth = ""

            serial_path = f"mqtt://{auth}{host}:{port}"

            # 3. Save to options and proceed
            self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = serial_path
            # Ensure internal flag is False for custom MQTT
            self.options[CONF_MQTT_USE_HA] = False
            return await self.async_step_configure_serial_port()

        # --- PRE-FILL LOGIC STARTS HERE ---
        # Get current settings to pre-fill the boxes
        current_path = self.options.get(SZ_SERIAL_PORT, {}).get(SZ_PORT_NAME, "")

        # Defaults if nothing is found
        suggested_host = None
        suggested_port = 1883
        suggested_user = None
        suggested_pass = None

        # If we already have an MQTT string, break it apart!
        if current_path and current_path.startswith("mqtt://"):
            try:
                parsed = urlparse(current_path)
                suggested_host = parsed.hostname
                suggested_port = parsed.port if parsed.port else 1883
                suggested_user = parsed.username
                suggested_pass = parsed.password
            except ValueError:
                pass  # If string is weird, just leave boxes blank
        # --- PRE-FILL LOGIC ENDS HERE ---

        # Define the Form Schema with 'suggested_value'
        data_schema = {
            vol.Required(
                "host", description={"suggested_value": suggested_host}
            ): selector.TextSelector(),
            vol.Required(
                "port", default=1883, description={"suggested_value": suggested_port}
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=65535,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
            vol.Optional(
                "username", description={"suggested_value": suggested_user}
            ): selector.TextSelector(),
            vol.Optional(
                "password", description={"suggested_value": suggested_pass}
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }

        return self.async_show_form(
            step_id="mqtt_config",
            data_schema=vol.Schema(data_schema),
            errors={},
            last_step=False,
        )

    async def async_step_zigbee_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow user to select a Zigbee device."""
        
        _LOGGER.info("=== Entered async_step_zigbee_device ===")
        _LOGGER.info(f"user_input: {user_input}")
        
        try:
            if user_input is not None:
                # Construct Zigbee URL from user input
                ieee = user_input.get("ieee")
                cluster = user_input.get("cluster", "0xfc00")
                attr = user_input.get("attr", "0x0000")
                endpoint = int(user_input.get("endpoint", 10))
                write_cluster = user_input.get("write_cluster", "0xfc01")
                write_attr = user_input.get("write_attr", "0x0000")
                write_endpoint = int(user_input.get("write_endpoint", 10))

                zigbee_url = f"zigbee://{ieee}/{cluster}/{attr}/{endpoint}/{write_cluster}/{write_attr}/{write_endpoint}"
                _LOGGER.info(f"Constructed Zigbee URL: {zigbee_url}")
                self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = zigbee_url
                return await self.async_step_configure_serial_port()

            # Define the form schema for Zigbee device configuration
            _LOGGER.info("Creating form schema...")
            data_schema = {
                vol.Required("ieee"): selector.TextSelector(),
                vol.Optional("cluster", default="0xfc00"): selector.TextSelector(),
                vol.Optional("attr", default="0x0000"): selector.TextSelector(),
                vol.Optional("endpoint", default=10): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=255, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional("write_cluster", default="0xfc01"): selector.TextSelector(),
                vol.Optional("write_attr", default="0x0000"): selector.TextSelector(),
                vol.Optional("write_endpoint", default=10): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=255, mode=selector.NumberSelectorMode.BOX)
                ),
            }
            
            _LOGGER.info("Showing form...")
            return self.async_show_form(
                step_id="zigbee_device",
                data_schema=vol.Schema(data_schema),
                errors={},
                last_step=False,
            )
        except Exception as err:
            _LOGGER.error(f"EXCEPTION in async_step_zigbee_device: {err}", exc_info=True)
            raise

    async def async_step_configure_serial_port(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ramses configure serial port step."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            suggested_values = deepcopy(dict(user_input))

            config = user_input.get(SZ_SERIAL_PORT, {})
            try:
                SCH_SERIAL_PORT_CONFIG(config)
            except vol.Invalid as err:
                errors[SZ_SERIAL_PORT] = "invalid_port_config"
                description_placeholders["error_detail"] = err.msg

            if not errors:
                if SZ_PORT_NAME in user_input:
                    config[SZ_PORT_NAME] = user_input[SZ_PORT_NAME]
                else:
                    # Debug: Check what we have in options
                    _LOGGER.debug(
                        f"DEBUG: self.options[SZ_SERIAL_PORT] = {self.options[SZ_SERIAL_PORT]}"
                    )
                    port_name = self.options[SZ_SERIAL_PORT][SZ_PORT_NAME]
                    _LOGGER.debug(f"DEBUG: Retrieved port_name = {port_name}")
                    if port_name is None:
                        _LOGGER.error("ERROR: port_name is None!")
                        errors[SZ_PORT_NAME] = "port_name_required"
                    else:
                        config[SZ_PORT_NAME] = port_name

                if not errors:
                    _LOGGER.debug(f"DEBUG: Final config = {config}")
                    self.options[SZ_SERIAL_PORT] = config
                    # Ensure internal flag is cleared if we set a manual port
                    self.options.pop(CONF_MQTT_USE_HA, None)
                    if self._initial_setup:
                        return await self.async_step_config()
                    return self._async_save()
        else:
            suggested_values = {
                SZ_PORT_NAME: self.options[SZ_SERIAL_PORT].get(SZ_PORT_NAME),
                SZ_SERIAL_PORT: {
                    k: v
                    for k, v in self.options[SZ_SERIAL_PORT].items()
                    if k != SZ_PORT_NAME
                },
            }

        data_schema: dict[vol.Marker, Any] = {}
        if self._manual_serial_port:
            data_schema |= {
                vol.Required(
                    SZ_PORT_NAME,
                    description={"suggested_value": suggested_values.get(SZ_PORT_NAME)},
                ): selector.TextSelector(),
            }
        data_schema |= {
            vol.Optional(
                SZ_SERIAL_PORT,
                description={"suggested_value": suggested_values.get(SZ_SERIAL_PORT)},
            ): selector.ObjectSelector()
        }

        return self.async_show_form(
            step_id="configure_serial_port",
            data_schema=vol.Schema(data_schema),
            description_placeholders=description_placeholders,
            errors=errors,
            last_step=not self._initial_setup,
        )

    async def async_step_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Gateway config step."""
        managed_keys = (
            SZ_ENFORCE_KNOWN_LIST,
            SZ_LOG_ALL_MQTT,
            SZ_SQLITE_INDEX,  # temporary 0.52.x dev option
        )
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        self.get_options()  # not available during init

        # Check if we should warn about discovery failure
        if self._discovery_failed:
            errors["base"] = "discovery_failed"
            # Explicitly cast to string to ensure translation interpolation works
            description_placeholders["default_id"] = str(DEFAULT_HGI_ID)
            # Reset flag so we don't show it again if they click submit
            self._discovery_failed = False

        if user_input is not None:
            suggested_values = user_input

            gateway_config = user_input.get(CONF_RAMSES_RF, {}) | {
                k: self.options[CONF_RAMSES_RF][k]
                for k in managed_keys
                if k in self.options[CONF_RAMSES_RF]
            }
            try:
                vol.Schema(SCH_GATEWAY_DICT | SCH_ENGINE_DICT, extra=vol.PREVENT_EXTRA)(
                    gateway_config
                )
            except vol.Invalid as err:
                errors[CONF_RAMSES_RF] = "invalid_gateway_config"
                description_placeholders["error_detail"] = err.msg

            if not errors:
                self.options[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
                self.options[CONF_RAMSES_RF] = gateway_config
                if CONF_MQTT_HGI_ID in user_input:
                    hgi_id = user_input[CONF_MQTT_HGI_ID]
                    self.options[CONF_MQTT_HGI_ID] = hgi_id

                    # Populate known_list if using HA MQTT, and a valid ID is provided
                    # This ensures it shows up in the "System schema" step immediately
                    if self.options.get(CONF_MQTT_USE_HA):
                        known_list = self.options.get(SZ_KNOWN_LIST, {}).copy()
                        if hgi_id not in known_list:
                            _LOGGER.debug(
                                "Config Flow: Injecting MQTT HGI %s into known_list",
                                hgi_id,
                            )
                            known_list[hgi_id] = {
                                "class": "HGI",
                                "alias": "ramses_esp",
                            }
                            self.options[SZ_KNOWN_LIST] = known_list

                if CONF_MQTT_TOPIC in user_input:
                    self.options[CONF_MQTT_TOPIC] = user_input[CONF_MQTT_TOPIC]

                if self._initial_setup:
                    return await self.async_step_schema()
                return self._async_save()
        else:
            suggested_values = {
                CONF_SCAN_INTERVAL: self.options.get(CONF_SCAN_INTERVAL),
                CONF_MQTT_HGI_ID: self.options.get(CONF_MQTT_HGI_ID),
                CONF_MQTT_TOPIC: self.options.get(CONF_MQTT_TOPIC),
                CONF_RAMSES_RF: {
                    k: v
                    for k, v in self.options[CONF_RAMSES_RF].items()
                    if k not in managed_keys
                },
            }

        data_schema = {
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=60,
                description={
                    "suggested_value": suggested_values.get(CONF_SCAN_INTERVAL, 60)
                },
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=600,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
            vol.Optional(
                CONF_RAMSES_RF,
                description={"suggested_value": suggested_values.get(CONF_RAMSES_RF)},
            ): selector.ObjectSelector(),
        }

        # If using MQTT, expose the HGI ID field and Topic
        if self.options.get(CONF_MQTT_USE_HA):
            data_schema[
                vol.Optional(
                    CONF_MQTT_TOPIC,
                    default=DEFAULT_MQTT_TOPIC,
                    description={
                        "suggested_value": suggested_values.get(
                            CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC
                        )
                    },
                )
            ] = selector.TextSelector()

            data_schema[
                vol.Optional(
                    CONF_MQTT_HGI_ID,
                    default=DEFAULT_HGI_ID,
                    description={
                        "suggested_value": suggested_values.get(
                            CONF_MQTT_HGI_ID, DEFAULT_HGI_ID
                        )
                    },
                )
            ] = selector.TextSelector()

        return self.async_show_form(
            step_id="config",
            data_schema=vol.Schema(data_schema),
            description_placeholders=description_placeholders,
            errors=errors,
            last_step=not self._initial_setup,
        )

    async def async_step_schema(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """System schema step."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        self.get_options()  # was not available during init
        enforce_known_was_on: bool = self.options[CONF_RAMSES_RF].get(
            SZ_ENFORCE_KNOWN_LIST
        )

        if user_input is not None:
            suggested_values = user_input

            try:
                SCH_GLOBAL_SCHEMAS(user_input.get(CONF_SCHEMA, {}))
            except vol.Invalid as err:
                errors[CONF_SCHEMA] = "invalid_schema"
                description_placeholders["error_detail"] = err.msg

            try:
                vol.Schema(SCH_GLOBAL_TRAITS_DICT)(
                    {SZ_KNOWN_LIST: user_input.get(SZ_KNOWN_LIST)}
                )
            except vol.Invalid as err:
                errors[SZ_KNOWN_LIST] = "invalid_traits"
                description_placeholders["error_detail"] = err.msg

            if not errors:
                self.options[CONF_SCHEMA] = user_input.get(CONF_SCHEMA, {})
                self.options[SZ_KNOWN_LIST] = user_input.get(SZ_KNOWN_LIST, {})
                self.options[CONF_RAMSES_RF][SZ_ENFORCE_KNOWN_LIST] = user_input[
                    SZ_ENFORCE_KNOWN_LIST
                ]
                # if ENFORCE_KNOWN_LIST changed from Off to On, also clear both caches
                if (not enforce_known_was_on) and (user_input[SZ_ENFORCE_KNOWN_LIST]):
                    # Unload immediately to stop scheduled coordinator state saves
                    await self.hass.config_entries.async_unload(
                        self.config_entry.entry_id
                    )
                    store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
                    _stored_data: dict[str, Any] = await store.async_load() or {}
                    if SZ_CLIENT_STATE in _stored_data:
                        _stored_data[SZ_CLIENT_STATE].pop(SZ_SCHEMA)
                        _stored_data[SZ_CLIENT_STATE].pop(SZ_PACKETS)
                    # save stored_data
                    await store.async_save(_stored_data)
                    _LOGGER.warning(
                        "Caches were cleared after enforcing Known List. Restart HA next."
                    )
                self.options[CONF_RAMSES_RF][SZ_LOG_ALL_MQTT] = user_input[
                    SZ_LOG_ALL_MQTT
                ]
                self.options[CONF_RAMSES_RF][SZ_SQLITE_INDEX] = user_input[
                    SZ_SQLITE_INDEX  # temporary 0.52.x dev option
                ]
                if self._initial_setup:
                    return await self.async_step_advanced_features()
                return self._async_save()
        else:
            suggested_values = {
                CONF_SCHEMA: self.options.get(CONF_SCHEMA),
                SZ_KNOWN_LIST: self.options.get(SZ_KNOWN_LIST),
                SZ_ENFORCE_KNOWN_LIST: self.options[CONF_RAMSES_RF].get(
                    SZ_ENFORCE_KNOWN_LIST
                ),
                SZ_LOG_ALL_MQTT: self.options[CONF_RAMSES_RF].get(SZ_LOG_ALL_MQTT),
                SZ_SQLITE_INDEX: self.options[CONF_RAMSES_RF].get(
                    SZ_SQLITE_INDEX  # temporary 0.52.x dev option
                ),
            }

        data_schema = {
            vol.Optional(
                CONF_SCHEMA,
                description={"suggested_value": suggested_values.get(CONF_SCHEMA)},
            ): selector.ObjectSelector(),
            vol.Optional(
                SZ_KNOWN_LIST,
                description={"suggested_value": suggested_values.get(SZ_KNOWN_LIST)},
            ): selector.ObjectSelector(),
            vol.Required(
                SZ_ENFORCE_KNOWN_LIST,
                default=False,
                description={
                    "suggested_value": suggested_values.get(SZ_ENFORCE_KNOWN_LIST)
                },
            ): selector.BooleanSelector(),
            vol.Optional(
                SZ_LOG_ALL_MQTT,
                default=False,
                description={"suggested_value": suggested_values.get(SZ_LOG_ALL_MQTT)},
            ): selector.BooleanSelector(),
            vol.Optional(
                SZ_SQLITE_INDEX,  # temporary 0.52.x dev option
                default=False,
                description={"suggested_value": suggested_values.get(SZ_SQLITE_INDEX)},
            ): selector.BooleanSelector(),
        }

        description_placeholders["wiki_url"] = (
            "https://github.com/ramses-rf/ramses_cc/wiki/"
        )

        return self.async_show_form(
            step_id="schema",
            data_schema=vol.Schema(data_schema),
            description_placeholders=description_placeholders,
            errors=errors,
            last_step=not self._initial_setup,
        )

    async def async_step_advanced_features(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Advanced features step."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        self.get_options()  # not available during init

        if user_input is not None:
            suggested_values = user_input
            if message_events := user_input.get(CONF_MESSAGE_EVENTS):
                try:
                    re.compile(message_events)
                except re.error as err:
                    errors[CONF_MESSAGE_EVENTS] = "invalid_regex"
                    description_placeholders["error_detail"] = err.msg

            if not errors:
                self.options[CONF_ADVANCED_FEATURES] = user_input
                if self._initial_setup:
                    return await self.async_step_packet_log()
                return self._async_save()
        else:
            suggested_values = self.options.get(CONF_ADVANCED_FEATURES, {})

        data_schema = {
            vol.Optional(
                CONF_SEND_PACKET,
                default=False,
                description={"suggested_value": suggested_values.get(CONF_SEND_PACKET)},
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_MESSAGE_EVENTS,
                description={
                    "suggested_value": suggested_values.get(CONF_MESSAGE_EVENTS)
                },
            ): selector.TextSelector(),
        }

        return self.async_show_form(
            step_id="advanced_features",
            data_schema=vol.Schema(data_schema),
            description_placeholders=description_placeholders,
            errors=errors,
            last_step=not self._initial_setup,
        )

    async def async_step_packet_log(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Packet log step."""
        if user_input is not None:
            self.options[SZ_PACKET_LOG] = user_input
            return self._async_save()

        self.get_options()  # not available during init
        suggested_values = self.options.get(SZ_PACKET_LOG, {})

        data_schema = {
            vol.Optional(
                SZ_FILE_NAME,
                description={"suggested_value": suggested_values.get(SZ_FILE_NAME)},
            ): selector.TextSelector(),
            vol.Optional(
                SZ_ROTATE_BYTES,
                description={"suggested_value": suggested_values.get(SZ_ROTATE_BYTES)},
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="bytes",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
            vol.Optional(
                SZ_ROTATE_BACKUPS,
                default=7,
                description={
                    "suggested_value": suggested_values.get(SZ_ROTATE_BACKUPS)
                },
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="backups",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
        }

        return self.async_show_form(
            step_id="packet_log", data_schema=vol.Schema(data_schema)
        )


class RamsesConfigFlow(BaseRamsesFlow, ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Config flow for Ramses."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize Ramses config flow."""
        super().__init__(initial_setup=True)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user. Required by hassfest:
        if a config flow is “discoverable”, it must set a unique ID"""
        await self.async_set_unique_id(DOMAIN)
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return await self.async_step_choose_serial_port()

    def _async_save(self) -> FlowResult:
        return self.async_create_entry(title="RAMSES RF", data={}, options=self.options)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import entry from configuration.yaml."""

        self.options = deepcopy(import_data)
        self.options[CONF_SCAN_INTERVAL] = import_data[
            CONF_SCAN_INTERVAL
        ].total_seconds()
        self.options.pop(SZ_RESTORE_CACHE, None)

        return self._async_save()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options callback for Ramses."""
        return RamsesOptionsFlowHandler()


class RamsesOptionsFlowHandler(BaseRamsesFlow, OptionsFlow):
    """Options config flow handler for Ramses."""

    def __init__(self) -> None:
        """Initialize Ramses config options flow."""
        super().__init__()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the config options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "choose_serial_port",
                "config",
                "schema",
                "advanced_features",
                "packet_log",
                "clear_cache",
            ],
        )

    def _async_save(self) -> FlowResult:
        result = self.async_create_entry(title="", data=self.options)

        # Reload only if setup is failing as changes are normally handled by the update listener
        if self.config_entry.state in (
            ConfigEntryState.SETUP_ERROR,
            ConfigEntryState.SETUP_RETRY,
        ):
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )

        return result

    async def async_step_clear_cache(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Clear cache step."""
        if user_input is not None:
            # Unload immediately to stop scheduled coordinator state saves
            if self.config_entry.state == ConfigEntryState.LOADED:
                await self.hass.config_entries.async_unload(self.config_entry.entry_id)

            store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
            stored_data: dict[str, Any] = await store.async_load() or {}

            if SZ_CLIENT_STATE in stored_data:
                if user_input["clear_schema"]:
                    stored_data[SZ_CLIENT_STATE].pop(SZ_SCHEMA)

                    def filter_schema_packets(
                        packets: dict[str, str],
                    ) -> dict[str, str]:
                        return {
                            dtm: pkt
                            for dtm, pkt in packets.items()
                            if pkt[41:45] not in [Code._0004, Code._0005, Code._000C]
                        }

                    # Filter out cached packets used for schema discovery
                    stored_data[SZ_CLIENT_STATE][SZ_PACKETS] = filter_schema_packets(
                        stored_data[SZ_CLIENT_STATE].get(SZ_PACKETS, {})
                    )

                if user_input["clear_packets"]:
                    stored_data[SZ_CLIENT_STATE].pop(SZ_PACKETS)
            await store.async_save(stored_data)

            self.hass.async_create_task(
                self.hass.config_entries.async_setup(self.config_entry.entry_id)
            )

            return self.async_abort(reason="cache_cleared")

        data_schema = {
            vol.Required("clear_schema", default=False): selector.BooleanSelector(),
            vol.Required("clear_packets", default=False): selector.BooleanSelector(),
        }

        return self.async_show_form(
            step_id="clear_cache",
            data_schema=vol.Schema(data_schema),
        )

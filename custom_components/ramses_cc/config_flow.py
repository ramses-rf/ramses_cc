"""Config flow to configure Ramses integration."""

from __future__ import annotations

import logging
import re
from abc import abstractmethod
from copy import deepcopy
from typing import Any, Final
from urllib.parse import urlparse

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.components import usb
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
    SCH_GLOBAL_TRAITS_DICT,
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
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_PACKET_SOURCE,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    CONF_SEND_PACKET,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    SZ_CLIENT_STATE,
    SZ_PACKETS,
)

_LOGGER = logging.getLogger(__name__)

CONF_MANUAL_PATH: Final = "Enter Manually..."  # TODO i18n this string
CONF_MQTT_PATH: Final = "MQTT Broker..."
CONF_MQTT_HA_PATH: Final = "Home Assistant MQTT"

# Schema for the MQTT step
MQTT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MQTT_TOPIC, default="RAMSES/GATEWAY"): str,
    }
)


def get_usb_ports() -> dict[str, str]:
    """Return a dict of USB ports and their friendly names.

    Iterates through available COM ports and attempts to resolve human-readable
    device names using Home Assistant's USB helpers.

    :return: A dictionary mapping the device path (e.g., /dev/ttyUSB0) to a
        friendly description.
    :rtype: dict[str, str]
    """
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
    """Return a dict of USB ports and their friendly names asynchronously.

    Executes the blocking `get_usb_ports` function in the executor.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :return: A dictionary mapping device paths to friendly names.
    :rtype: dict[str, str]
    """
    return await hass.async_add_executor_job(get_usb_ports)


class BaseRamsesFlow(FlowHandler):
    """Mixin for common Ramses flow steps and forms.

    This class encapsulates the shared logic between the initial ConfigFlow
    and the subsequent OptionsFlow, such as configuring serial ports, schemas,
    and advanced features.
    """

    options: dict[str, Any]

    def __init__(self, initial_setup: bool = False) -> None:
        """Initialize flow.

        :param initial_setup: Flag indicating if this is the initial configuration
            flow (True) or an options flow (False). Defaults to False.
        :type initial_setup: bool
        """
        super().__init__()
        self._initial_setup = initial_setup
        self._manual_serial_port = False
        self.options = {}  # Safe initialization

    def get_options(self) -> None:
        """Retrieve and prepare the options dictionary.

        If a config entry exists, deep copies its options. Otherwise, initializes
        default structures for `CONF_RAMSES_RF` and `SZ_SERIAL_PORT` within the
        flow's internal options storage.
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
        """Finish the flow and save the result.

        This method must be implemented by subclasses to handle the specific
        saving mechanism (creating an entry vs updating an entry).

        :return: The flow result indicating success.
        :rtype: FlowResult
        """

    async def async_step_mqtt_ha(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Home Assistant MQTT selection.

        Checks if the MQTT integration is properly configured in Home Assistant
        before proceeding.
        """
        errors: dict[str, str] = {}
        self.get_options()  # Ensure options are initialized

        # Section 4.3: Config Flow (Runtime Check)
        # Check if MQTT integration is set up
        if not self.hass.config_entries.async_entries("mqtt"):
            return self.async_abort(reason="mqtt_not_setup")

        if user_input is not None:
            self.options[CONF_PACKET_SOURCE] = "mqtt"
            self.options[CONF_MQTT_USE_HA] = True
            self.options[CONF_MQTT_TOPIC] = user_input[CONF_MQTT_TOPIC]
            self.options[SZ_SERIAL_PORT] = {}  # Clear serial config

            return self._async_save()

        return self.async_show_form(
            step_id="mqtt_ha",
            data_schema=MQTT_SCHEMA,
            errors=errors,
            description_placeholders={"default_topic": "RAMSES/GATEWAY"},
        )

    async def async_step_choose_serial_port(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the step to choose a serial port.

        Displays a list of detected USB ports, plus options for Manual entry
        and MQTT configuration.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The next step in the flow.
        :rtype: FlowResult
        """
        self.get_options()  # not available during init

        # --- PART 1: Handle the User's Selection ---
        if user_input is not None:
            port_name = user_input[SZ_PORT_NAME]

            if port_name == CONF_MQTT_PATH:
                return await self.async_step_mqtt_config()
            elif port_name == CONF_MQTT_HA_PATH:
                return await self.async_step_mqtt_ha()
            elif port_name == CONF_MANUAL_PATH:
                self._manual_serial_port = True
            else:
                self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = user_input[SZ_PORT_NAME]
                _LOGGER.debug(
                    f"DEBUG: Saved port_name = {user_input[SZ_PORT_NAME]} to options"
                )
            return await self.async_step_configure_serial_port()

        # --- PART 2: Prepare the Menu ---
        ports = await async_get_usb_ports(self.hass)

        # if not ports:
        #    self._manual_serial_port = True
        #    return await self.async_step_configure_serial_port()

        # Always add these options now
        ports[CONF_MQTT_HA_PATH] = CONF_MQTT_HA_PATH
        ports[CONF_MQTT_PATH] = CONF_MQTT_PATH
        ports[CONF_MANUAL_PATH] = CONF_MANUAL_PATH

        port_name = self.options[SZ_SERIAL_PORT].get(SZ_PORT_NAME)
        if port_name is None:
            default_port = vol.UNDEFINED
        elif self.options.get(CONF_MQTT_USE_HA):
            default_port = CONF_MQTT_HA_PATH
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
            last_step=False,
        )

    async def async_step_mqtt_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow user to enter MQTT connection details separately.

        Constructs a connection string in the format `mqtt://user:pass@host:port`
        based on individual form fields.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The next step in the flow.
        :rtype: FlowResult
        """

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

    async def async_step_configure_serial_port(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the step to configure specific serial port parameters.

        Validates the configuration against `SCH_SERIAL_PORT_CONFIG`.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The next step or the result of saving the flow.
        :rtype: FlowResult
        """
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
        """Handle the step to configure the gateway engine.

        Validates the configuration against `SCH_GATEWAY_DICT` and `SCH_ENGINE_DICT`.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The next step or the result of saving the flow.
        :rtype: FlowResult
        """
        managed_keys = (
            SZ_ENFORCE_KNOWN_LIST,
            SZ_LOG_ALL_MQTT,
            SZ_SQLITE_INDEX,  # temporary 0.52.x dev option
        )
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        self.get_options()  # not available during init

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
                if self._initial_setup:
                    return await self.async_step_schema()
                return self._async_save()
        else:
            suggested_values = {
                CONF_SCAN_INTERVAL: self.options.get(CONF_SCAN_INTERVAL),
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
        """Handle the step to configure system schema and traits.

        Validates against `SCH_GLOBAL_SCHEMAS` and `SCH_GLOBAL_TRAITS_DICT`.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The next step or the result of saving the flow.
        :rtype: FlowResult
        """
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        self.get_options()  # was not available during init

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
        """Handle the step for advanced features like packet sending and message events.

        Validates the message events regex.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The next step or the result of saving the flow.
        :rtype: FlowResult
        """
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
        """Handle the step to configure packet logging.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The result of saving the flow.
        :rtype: FlowResult
        """
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
    """Config flow for Ramses.

    Handles the initial setup of the Ramses RF integration.
    """

    VERSION = 1
    # Set the handler manually instead of using 'domain=DOMAIN' in the class definition
    # to satisfy Mypy which doesn't recognize the __init_subclass__ kwarg.
    # handler = DOMAIN  <-- DELETED: using domain=DOMAIN in class definition instead

    def __init__(self) -> None:
        """Initialize Ramses config flow.

        Sets the flow to initial setup mode.
        """
        super().__init__(initial_setup=True)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step.

        Directs the user to the menu selection.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The menu step.
        :rtype: FlowResult
        """
        return await self.async_step_menu()

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow user to choose between Serial, Direct MQTT, or HA MQTT.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The menu form.
        :rtype: FlowResult
        """
        return self.async_show_menu(
            step_id="menu",
            menu_options=["serial", "mqtt_ha", "mqtt_direct"],
        )

    async def async_step_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wrapper to call the base serial port step.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The serial port choice step.
        :rtype: FlowResult
        """
        return await self.async_step_choose_serial_port(user_input)

    async def async_step_mqtt_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wrapper to call the base MQTT config step.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The MQTT config step.
        :rtype: FlowResult
        """
        return await self.async_step_mqtt_config(user_input)

    async def async_step_mqtt_ha(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Home Assistant MQTT selection.

        Checks if the MQTT integration is properly configured in Home Assistant
        before proceeding.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The result of the flow (abort or create entry).
        :rtype: FlowResult
        """
        # Call the base class implementation which now handles saving via _async_save
        return await super().async_step_mqtt_ha(user_input)

    def _async_save(self) -> FlowResult:
        """Save the initial configuration entry.

        :return: The result of creating the config entry.
        :rtype: FlowResult
        """
        return self.async_create_entry(title="RAMSES RF", data={}, options=self.options)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import entry from configuration.yaml.

        :param import_data: The configuration data imported from YAML.
        :type import_data: dict[str, Any]
        :return: The result of the import flow.
        :rtype: ConfigFlowResult
        """

        self.options = deepcopy(import_data)
        self.options[CONF_SCAN_INTERVAL] = import_data[
            CONF_SCAN_INTERVAL
        ].total_seconds()
        self.options.pop(SZ_RESTORE_CACHE, None)

        return self._async_save()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler for Ramses.

        :param config_entry: The config entry instance.
        :type config_entry: ConfigEntry
        :return: The initialized options flow handler.
        :rtype: OptionsFlow
        """
        return RamsesOptionsFlowHandler()


class RamsesOptionsFlowHandler(BaseRamsesFlow, OptionsFlow):
    """Options config flow handler for Ramses.

    Handles reconfiguration of the integration after initial setup.
    """

    def __init__(self) -> None:
        """Initialize Ramses config options flow."""
        super().__init__()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the config options.

        Displays the menu of available option steps.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The menu form.
        :rtype: ConfigFlowResult
        """
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
        """Save the updated options.

        Reloads the integration if it is currently in a setup error state.

        :return: The result of creating the config entry.
        :rtype: FlowResult
        """
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
        """Handle the step to clear the internal cache.

        Modifies the underlying JSON storage file to remove schema or packet
        data based on user selection.

        :param user_input: The input provided by the user, if any.
        :type user_input: dict[str, Any] | None
        :return: The result of the flow (abort on success).
        :rtype: FlowResult
        """
        if user_input is not None:
            # Unload immediately to stop scheduled broker state saves
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

"""Config flow to configure Ramses integration."""

import asyncio
import logging
import re
from abc import abstractmethod
from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Final
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
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    selector,
)
from homeassistant.helpers.storage import Store

from ramses_rf.schemas import (
    SCH_GATEWAY_DICT,
    SCH_GLOBAL_SCHEMAS,
    SZ_RESTORE_CACHE,
    SZ_SCHEMA,
)
from ramses_tx.schemas import (
    SCH_ENGINE_DICT,
    SCH_SERIAL_PORT_CONFIG,
    SZ_BUFFER_CAPACITY,
    SZ_FLUSH_INTERVAL,
    SZ_LOG_ALL_MQTT,
    SZ_PACKET_LOG,
    SZ_PACKET_LOG_PATH,
    SZ_PACKET_LOG_PREFIX,
    SZ_PACKET_LOG_RETENTION_DAYS,
    SZ_PORT_NAME,
    SZ_ROTATE_BYTES,
    SZ_SERIAL_PORT,
    # deprecated 0.56.0 but allowed as extras:
    # SZ_FILE_NAME, SZ_ROTATE_BACKUPS, SZ_SQLITE_INDEX
)

from .const import (
    CONF_ADVANCED_FEATURES,
    CONF_AUTO_NOTIFY,
    CONF_FRESH_START,
    CONF_GATEWAY_TIMEOUT,
    CONF_LOST_THRESHOLD,
    CONF_MESSAGE_EVENTS,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_PASSIVE_SCAN,
    CONF_RAMSES_RF,
    CONF_SCHEMA,
    CONF_SEND_PACKET,
    DEFAULT_HGI_ID,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    SZ_CLIENT_STATE,
    SZ_DEVICE_COMMENTS,
    SZ_OWNER,
    SZ_PACKETS,
    SZ_TR_CLASS,
    SZ_TR_NAME,
    SZ_TR_OWNER,
    SZ_TR_SKIPPED,
)
from .schemas import migrate_known_list_traits, order_schema

_LOGGER = logging.getLogger(__name__)

CONF_MANUAL_PATH: Final = "Enter Manually..."  # TODO i18n these strings
CONF_MQTT_PATH: Final = "MQTT Broker..."
CONF_HA_MQTT_PATH: Final = "Use Home Assistant MQTT - In development!"
CONF_ZIGBEE_DEVICE: Final = "Zigbee device"


if hasattr(usb, "async_scan_serial_ports"):
    # Compatible with Home Assistant Core 2026.5.0
    def get_usb_ports() -> dict[str, str]:
        """Return a dict of USB ports and their friendly names.

        :return: A dictionary mapping device paths to descriptions.
        """
        port_descriptions = {}
        scan_ports: Callable[[], Any] = getattr(
            usb, "scan_serial_ports", lambda: []
        )

        for port in scan_ports():
            vid = getattr(port, "vid", None)
            pid = getattr(port, "pid", None)
            human_name = usb.human_readable_device_name(
                port.device,
                port.serial_number,
                port.manufacturer,
                port.description,
                vid,
                pid,
            )
            port_descriptions[port.device] = human_name
        return port_descriptions

else:
    from serial.tools import list_ports  # type: ignore[import-untyped]

    # Compatible with all earlier versions.
    # TODO: remove Q3 2026
    def get_usb_ports() -> dict[str, str]:
        """Return a dict of USB ports and their friendly names.

        :return: A dictionary mapping device paths to descriptions.
        """
        ports = list_ports.comports()
        port_descriptions = {}
        usb_device_from_port: Callable[[Any], Any] | None = getattr(
            usb, "usb_device_from_port", None
        )

        for port in ports:
            vid: str | None = None
            pid: str | None = None
            if (
                port.vid is not None
                and port.pid is not None
                and usb_device_from_port
            ):
                usb_dev = usb_device_from_port(port)
                vid = usb_dev.vid
                pid = usb_dev.pid
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
    """Return a dict of USB ports and their friendly names.

    :param hass: The Home Assistant instance.
    :return: A dictionary mapping device paths to descriptions.
    """
    return await hass.async_add_executor_job(get_usb_ports)


def _extract_ieee_from_device(device_entry: dr.DeviceEntry) -> str | None:
    """Extract the IEEE address from a device registry entry.

    :param device_entry: The device registry entry to inspect.
    :return: The IEEE string, or None if not found.
    """
    for _domain, ident in device_entry.identifiers:
        ident_str = str(ident)
        if re.fullmatch(r"[0-9A-Fa-f:]{8,}", ident_str):
            return ident_str
    return None


class BaseRamsesFlow:
    """Mixin for common Ramses flow steps and forms."""

    options: dict[str, Any]
    config_entry: ConfigEntry | None = None

    if TYPE_CHECKING:
        hass: HomeAssistant

        def async_show_form(self, **kwargs: Any) -> ConfigFlowResult:
            """Show form.

            :param kwargs: Keyword arguments for the form.
            :return: The generated flow result.
            """
            ...

        def async_create_entry(self, **kwargs: Any) -> ConfigFlowResult:
            """Create entry.

            :param kwargs: Keyword arguments for entry creation.
            :return: The generated flow result.
            """
            ...

        def async_abort(self, **kwargs: Any) -> ConfigFlowResult:
            """Abort flow.

            :param kwargs: Keyword arguments for abortion.
            :return: The generated flow result.
            """
            ...

        def async_show_menu(self, **kwargs: Any) -> ConfigFlowResult:
            """Show menu.

            :param kwargs: Keyword arguments for the menu.
            :return: The generated flow result.
            """
            ...

    def __init__(self, initial_setup: bool = False) -> None:
        """Initialize flow.

        :param initial_setup: Whether this is the initial setup.
        """
        super().__init__()
        self._initial_setup = initial_setup
        self._manual_serial_port = False
        self._discovery_failed = False  # Track if discovery failed

    def get_options(self) -> None:
        """Load options from the config entry or initialize defaults.

        Populates `self.options` from the existing config entry if
        available. Otherwise, it initializes defaults or preserves
        options accumulating during the current flow step.
        """
        if (
            self.config_entry is not None
            and self.config_entry.options is not None
        ):
            options = deepcopy(dict(self.config_entry.options))
        else:  # create an empty config_entry for new installs
            # Preserve existing options set during current flow
            options = getattr(self, "options", {})
        options.setdefault(CONF_RAMSES_RF, {})
        options.setdefault(SZ_SERIAL_PORT, {})
        self.options = options

    @abstractmethod
    def _async_save(self) -> ConfigFlowResult:
        """Finish the flow.

        :return: The generated config flow result.
        """

    async def _discover_mqtt_hgi(self) -> str | None:
        """Discover HGI device on MQTT.

        :return: Discovered MQTT HGI device identifier or None.
        """
        # Use a future to capture the first result
        found_device: asyncio.Future[str | None] = (
            self.hass.loop.create_future()
        )

        @callback
        def _msg_callback(msg: Any) -> None:
            """Handle incoming MQTT discovery messages.

            :param msg: The incoming MQTT message.
            """
            if found_device.done():
                return

            # _LOGGER.debug("MQTT Discovery received: %s", msg.topic)

            # Topic format: RAMSES/GATEWAY/{device_id}/...
            # Subscribe to wildcard #, split and look for 18:xxxxxx
            try:
                parts = msg.topic.split("/")
                for part in parts:
                    if part.startswith("18:"):
                        _LOGGER.debug("Discovery found device: %s", part)
                        found_device.set_result(part)
                        return
            except (AttributeError, TypeError, ValueError) as err:
                _LOGGER.debug("MQTT discovery topic parse error: %s", err)

        # Determine topic to scan. Use default if not set.
        # Wildcard # catches ANY topic (rx, status) that might be retained
        scan_topic = f"{DEFAULT_MQTT_TOPIC}/#"
        _LOGGER.debug("Starting discovery on topic: %s", scan_topic)

        try:
            # Careful if MQTT not fully loaded (checked before calling)
            unsub = await mqtt.async_subscribe(
                self.hass, scan_topic, _msg_callback
            )
            try:
                # Wait up to 5s. If retained messages, this is instant.
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
    ) -> ConfigFlowResult:
        """Ramses choose serial port step.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
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
                    entry.state == ConfigEntryState.LOADED
                    for entry in mqtt_entries
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
                self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = user_input[
                    SZ_PORT_NAME
                ]
                _LOGGER.debug(
                    "DEBUG: Saved port_name = %s to options",
                    user_input[SZ_PORT_NAME],
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
                mqtt_label = (
                    f"{CONF_HA_MQTT_PATH} (MQTT integration not ready)"
                )
            else:
                mqtt_label = (
                    f"{CONF_HA_MQTT_PATH} (MQTT integration not found)"
                )

        # Always add options
        ports[CONF_HA_MQTT_PATH] = mqtt_label
        ports[CONF_MQTT_PATH] = CONF_MQTT_PATH

        # If exactly one ramses_esp32c6 Zigbee device is present, show its
        # friendly name in the selector label. Otherwise show a generic label.
        try:
            dev_reg = dr.async_get(self.hass)
            matches = [
                dev
                for dev in getattr(dev_reg, "devices", {}).values()
                if "ramses_esp32c6" in (dev.model or "").lower()
            ]
            if len(matches) == 1:
                raw_name = (
                    matches[0].name or matches[0].name_by_user or matches[0].id
                )
                display_name = (
                    raw_name.split(" ", 1)[1].strip()
                    if " " in raw_name
                    else raw_name
                )
                zigbee_label = f"Zigbee device: {display_name}"
            else:
                zigbee_label = "Zigbee device"
        except Exception:
            zigbee_label = "Zigbee device"

        ports[CONF_ZIGBEE_DEVICE] = zigbee_label
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
    ) -> ConfigFlowResult:
        """Allow user to enter MQTT details separately.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
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
            # Ensure internal flag is False for custom MQTT
            self.options[CONF_MQTT_USE_HA] = False
            return await self.async_step_configure_serial_port()

        # --- PRE-FILL LOGIC STARTS HERE ---
        # Get current settings to pre-fill the boxes
        current_path = self.options.get(SZ_SERIAL_PORT, {}).get(
            SZ_PORT_NAME, ""
        )

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
                "port",
                default=1883,
                description={"suggested_value": suggested_port},
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
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD
                )
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
    ) -> ConfigFlowResult:
        """Allow user to select a Zigbee device.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        _LOGGER.debug(
            "Entered async_step_zigbee_device; showing device selector"
        )

        try:
            dev_reg = dr.async_get(self.hass)

            # If user submitted a device (from multi-device selector)
            if user_input is not None and "device" in user_input:
                device_id = user_input.get("device")
                if not isinstance(device_id, str):
                    return self.async_show_form(
                        step_id="zigbee_device",
                        data_schema=vol.Schema(
                            {vol.Required("device"): selector.DeviceSelector()}
                        ),
                        errors={"device": "invalid_device"},
                        last_step=False,
                    )

                device_entry = dev_reg.async_get(device_id)

                if not device_entry:
                    return self.async_show_form(
                        step_id="zigbee_device",
                        data_schema=vol.Schema(
                            {vol.Required("device"): selector.DeviceSelector()}
                        ),
                        errors={"device": "device_not_found"},
                        last_step=False,
                    )

                ieee = _extract_ieee_from_device(device_entry)

                if not ieee:
                    return self.async_show_form(
                        step_id="zigbee_device",
                        data_schema=vol.Schema(
                            {vol.Required("device"): selector.DeviceSelector()}
                        ),
                        errors={"device": "no_ieee_identifier"},
                        last_step=False,
                    )

                zigbee_url = (
                    f"zigbee://{ieee}/0xfc00/0x0000/10/0xfc01/0x0000/10"
                )
                _LOGGER.info(
                    "Constructed Zigbee URL from device %s: %s",
                    device_id,
                    zigbee_url,
                )
                self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = zigbee_url
                return await self.async_step_configure_serial_port()

            # No submission yet — find matching devices.
            matches = [
                dev
                for dev in getattr(dev_reg, "devices", {}).values()
                if "ramses_esp32c6" in (dev.model or "").lower()
            ]

            if len(matches) == 0:
                return self.async_show_form(
                    step_id="zigbee_device",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                "retry", default=False
                            ): selector.BooleanSelector()
                        }
                    ),
                    errors={"base": "no_ramses_device_found"},
                    last_step=False,
                )

            if len(matches) == 1:
                candidate = matches[0]
                ieee = _extract_ieee_from_device(candidate)

                if not ieee:
                    return self.async_show_form(
                        step_id="zigbee_device",
                        data_schema=vol.Schema(
                            {
                                vol.Required(
                                    "retry", default=False
                                ): selector.BooleanSelector()
                            }
                        ),
                        errors={"base": "no_ieee_identifier"},
                        last_step=False,
                    )

                zigbee_url = (
                    f"zigbee://{ieee}/0xfc00/0x0000/10/0xfc01/0x0000/10"
                )
                _LOGGER.info(
                    "Auto-constructed Zigbee URL from device %s: %s",
                    candidate.id,
                    zigbee_url,
                )
                self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = zigbee_url
                return await self.async_step_configure_serial_port()

            # Multiple matches: present a selector for the user to choose.
            options = [
                selector.SelectOptionDict(
                    value=dev.id,
                    label=dev.name or dev.name_by_user or dev.id,
                )
                for dev in matches
            ]
            return self.async_show_form(
                step_id="zigbee_device",
                data_schema=vol.Schema(
                    {
                        vol.Required("device"): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=options)
                        )
                    }
                ),
                errors={},
                last_step=False,
            )
        except Exception as err:
            _LOGGER.error(
                "EXCEPTION in async_step_zigbee_device: %s", err, exc_info=True
            )
            raise

    async def async_step_configure_serial_port(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ramses configure serial port step.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
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
                        "DEBUG: self.options[SZ_SERIAL_PORT] = %s",
                        self.options[SZ_SERIAL_PORT],
                    )
                    port_name = self.options[SZ_SERIAL_PORT][SZ_PORT_NAME]
                    _LOGGER.debug("DEBUG: Retrieved port_name = %s", port_name)
                    if port_name is None:
                        _LOGGER.error("ERROR: port_name is None!")
                        errors[SZ_PORT_NAME] = "port_name_required"
                    else:
                        config[SZ_PORT_NAME] = port_name

                if not errors:
                    _LOGGER.debug("DEBUG: Final config = %s", config)
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
                    description={
                        "suggested_value": suggested_values.get(SZ_PORT_NAME)
                    },
                ): selector.TextSelector(),
            }
        data_schema |= {
            vol.Optional(
                SZ_SERIAL_PORT,
                description={
                    "suggested_value": suggested_values.get(SZ_SERIAL_PORT)
                },
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
    ) -> ConfigFlowResult:
        """Gateway config step.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        managed_keys = (SZ_LOG_ALL_MQTT,)
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        self.get_options()  # not available during init

        # Check if we should warn about discovery failure
        if self._discovery_failed:
            errors["base"] = "discovery_failed"
            # Cast to string to ensure translation interpolation works
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
                vol.Schema(
                    SCH_GATEWAY_DICT | SCH_ENGINE_DICT, extra=vol.PREVENT_EXTRA
                )(gateway_config)
            except vol.Invalid as err:
                errors[CONF_RAMSES_RF] = "invalid_gateway_config"
                description_placeholders["error_detail"] = err.msg

            if not errors:
                self.options[CONF_SCAN_INTERVAL] = user_input.get(
                    CONF_SCAN_INTERVAL, 60
                )
                self.options[CONF_GATEWAY_TIMEOUT] = user_input.get(
                    CONF_GATEWAY_TIMEOUT, 10
                )
                self.options[CONF_RAMSES_RF] = gateway_config
                if CONF_MQTT_HGI_ID in user_input:
                    hgi_id = user_input[CONF_MQTT_HGI_ID]
                    self.options[CONF_MQTT_HGI_ID] = hgi_id

                    # Inject HGI into schema if using HA MQTT, and a valid ID
                    # is provided.  This ensures it shows up in the "System
                    # schema" step immediately.  (Phase 4: was previously
                    # injected into known_list — now goes to schema as _
                    # traits, the single source of truth.)
                    if self.options.get(CONF_MQTT_USE_HA):
                        schema = self.options.get(CONF_SCHEMA, {}).copy()
                        if hgi_id not in schema:
                            _LOGGER.debug(
                                "Config Flow: Inject MQTT HGI %s into schema",
                                hgi_id,
                            )
                            schema[hgi_id] = {
                                "_class": "HGI",
                                "_alias": "ramses_esp",
                            }
                            self.options[CONF_SCHEMA] = schema

                if CONF_MQTT_TOPIC in user_input:
                    self.options[CONF_MQTT_TOPIC] = user_input[CONF_MQTT_TOPIC]

                if self._initial_setup:
                    return await self.async_step_schema()
                return self._async_save()
        else:
            suggested_values = {
                CONF_SCAN_INTERVAL: self.options.get(CONF_SCAN_INTERVAL),
                CONF_GATEWAY_TIMEOUT: self.options.get(CONF_GATEWAY_TIMEOUT),
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
                    "suggested_value": suggested_values.get(
                        CONF_SCAN_INTERVAL, 60
                    )
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
            vol.Required(
                CONF_GATEWAY_TIMEOUT,
                default=10,
                description={
                    "suggested_value": suggested_values.get(
                        CONF_GATEWAY_TIMEOUT, 10
                    )
                },
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=60,
                        unit_of_measurement="minutes",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
            vol.Optional(
                CONF_RAMSES_RF,
                description={
                    "suggested_value": suggested_values.get(CONF_RAMSES_RF)
                },
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
    ) -> ConfigFlowResult:
        """System schema step.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        self.get_options()  # was not available during init

        if user_input is not None:
            suggested_values = user_input

            # Strip ramses_cc-specific keys and _ traits before validating
            # with SCH_GLOBAL_SCHEMAS (which has extra=PREVENT_EXTRA and
            # rejects _ prefixed keys)
            from .schemas import strip_traits_for_validation

            original_schema = user_input.get(CONF_SCHEMA, {})
            if isinstance(original_schema, dict):
                # First strip top-level cc-only keys (device_comments)
                raw_schema = dict(original_schema)
                cc_only_data = {}
                if SZ_DEVICE_COMMENTS in raw_schema:
                    cc_only_data[SZ_DEVICE_COMMENTS] = raw_schema.pop(
                        SZ_DEVICE_COMMENTS
                    )
                # Then strip _ prefixed keys and trait-only entries
                raw_schema = strip_traits_for_validation(raw_schema)
            else:
                raw_schema = original_schema
                cc_only_data = {}

            try:
                SCH_GLOBAL_SCHEMAS(raw_schema)
            except vol.Invalid as err:
                errors[CONF_SCHEMA] = "invalid_schema"
                description_placeholders["error_detail"] = err.msg

            if not errors:
                # Detect devices that were removed from the schema.
                # This covers both full wipe (all devices removed) and
                # single-device removal.  For each removed device, we
                # reset its discovery metadata so it's re-discovered as NEW.
                import re as _re

                _dev_id_re = _re.compile(r"^\d{2}:\d{6}$")
                prev_schema = self.options.get(CONF_SCHEMA, {})

                def _extract_device_ids(schema: dict[str, Any]) -> set[str]:
                    """Extract device IDs from schema (keys + orphans)."""
                    ids = {str(k) for k in schema if _dev_id_re.match(str(k))}
                    for v in schema.values():
                        if isinstance(v, list):
                            ids.update(
                                str(d) for d in v if _dev_id_re.match(str(d))
                            )
                    return ids

                prev_device_ids = _extract_device_ids(prev_schema)
                new_schema_dict = (
                    original_schema
                    if isinstance(original_schema, dict)
                    else raw_schema | cc_only_data
                )
                new_device_ids = _extract_device_ids(new_schema_dict or {})
                removed_devices = prev_device_ids - new_device_ids
                schema_wiped = bool(prev_device_ids) and not new_device_ids

                # Save the original schema (with _ traits and cc-only keys)
                if isinstance(original_schema, dict):
                    self.options[CONF_SCHEMA] = order_schema(original_schema)
                else:
                    self.options[CONF_SCHEMA] = order_schema(
                        raw_schema | cc_only_data
                    )

                # Owner name: set root _owner and update all devices.
                # - Devices without _owner → backfill with new owner name
                # - Devices with the OLD root owner → rename to new owner name
                # - Devices with a different _owner (foreign) → left untouched
                owner_name = (user_input.get("owner_name") or "me").strip()
                schema_dict = self.options[CONF_SCHEMA]
                if isinstance(schema_dict, dict):
                    old_owner = schema_dict.get(SZ_OWNER)
                    schema_dict[SZ_OWNER] = owner_name
                    for k, v in schema_dict.items():
                        if not (
                            isinstance(v, dict) and _dev_id_re.match(str(k))
                        ):
                            continue
                        existing = v.get(SZ_TR_OWNER)
                        if not isinstance(existing, str):
                            # No _owner → backfill
                            v[SZ_TR_OWNER] = owner_name
                        elif old_owner and existing == old_owner:
                            # Had the old root owner → rename
                            v[SZ_TR_OWNER] = owner_name
                        # else: foreign owner → leave untouched
                self.options[CONF_RAMSES_RF][SZ_LOG_ALL_MQTT] = user_input.get(
                    SZ_LOG_ALL_MQTT, False
                )

                # If devices were removed from the schema, reset their
                # discovery metadata so they're re-discovered as NEW.
                # Without this, the scan restores old ACCEPTED/DISCARDED
                # statuses and get_devices(status=NEW) returns empty.
                #
                # Also clean device_comments for removed devices (issue 905):
                # scan engine tracks ALL RF traffic (including foreign), so
                # stale comments with zone bindings would cause sync to
                # re-add the removed device to a zone on next save cycle.
                if removed_devices and self.config_entry is not None:
                    # Clean up device_comments for removed devices
                    schema_dict = self.options[CONF_SCHEMA]
                    if isinstance(schema_dict, dict):
                        comments = schema_dict.get(SZ_DEVICE_COMMENTS)
                        if isinstance(comments, dict):
                            cleaned_comments = {
                                k: v
                                for k, v in comments.items()
                                if k not in removed_devices
                            }
                            if len(cleaned_comments) != len(comments):
                                schema_dict[SZ_DEVICE_COMMENTS] = (
                                    cleaned_comments
                                )
                                _LOGGER.info(
                                    "Removed %d device comment(s) for "
                                    "removed devices: %s",
                                    len(comments) - len(cleaned_comments),
                                    sorted(
                                        removed_devices & set(comments.keys())
                                    ),
                                )
                    store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
                    _stored = await store.async_load() or {}
                    from .discovery import SZ_DISCOVERY, SZ_DISCOVERY_DEVICES

                    discovery = _stored.get(SZ_DISCOVERY, {})
                    devices_meta = discovery.get(SZ_DISCOVERY_DEVICES, {})

                    if schema_wiped:
                        # Full wipe — clear all discovery data
                        _stored.pop(SZ_DISCOVERY, None)
                        _LOGGER.info(
                            "Schema wiped in editor — cleared discovery "
                            "metadata so devices are re-discovered as NEW"
                        )
                    else:
                        # Per-device removal — reset only the removed devices
                        # to NEW status, and remove them from the scan state
                        # so the scan re-discovers them from scratch.
                        for dev_id in removed_devices:
                            if dev_id in devices_meta:
                                devices_meta[dev_id] = {
                                    "status": "new",
                                    "enabled": False,
                                    "faked": False,
                                    "schema_entry": None,
                                    "owner": None,
                                }
                                _LOGGER.info(
                                    "Device %s removed from schema — reset "
                                    "discovery metadata to NEW",
                                    dev_id,
                                )
                        # Also remove from scan_state for fresh re-discovery
                        scan_state = discovery.get("scan_state", "")
                        if scan_state:
                            import json as _json

                            try:
                                scan_data = _json.loads(scan_state)
                                scan_devices = {
                                    d["device_id"]: d
                                    for d in scan_data.get("devices", [])
                                    if d["device_id"] not in removed_devices
                                }
                                scan_data["devices"] = list(
                                    scan_devices.values()
                                )
                                discovery["scan_state"] = _json.dumps(
                                    scan_data
                                )
                            except (ValueError, KeyError):
                                pass  # corrupt scan_state, leave as-is
                        discovery[SZ_DISCOVERY_DEVICES] = devices_meta
                        _stored[SZ_DISCOVERY] = discovery

                    await store.async_save(_stored)

                    # Add removed devices to the coordinator's _removed_devices
                    # set so sync_learned_topology doesn't re-add them from
                    # the learned schema on the next save cycle (issue 905).
                    # ramses_rf has no remove_device API, so the learned schema
                    # still references removed devices until restart.
                    coord = getattr(self.config_entry, "runtime_data", None)
                    if coord is not None and hasattr(
                        coord, "_removed_devices"
                    ):
                        coord._removed_devices.update(removed_devices)  # noqa: SLF001

                if self._initial_setup:
                    return await self.async_step_advanced_features()
                return self._async_save()
        else:
            suggested_values = {
                CONF_SCHEMA: self.options.get(CONF_SCHEMA),
                "owner_name": self.options.get(CONF_SCHEMA, {}).get(
                    SZ_OWNER, "me"
                ),
                SZ_LOG_ALL_MQTT: self.options[CONF_RAMSES_RF].get(
                    SZ_LOG_ALL_MQTT, False
                ),
            }

        data_schema = {
            vol.Optional(
                CONF_SCHEMA,
                description={
                    "suggested_value": suggested_values.get(CONF_SCHEMA)
                },
            ): selector.ObjectSelector(),
            vol.Required(
                "owner_name",
                default=suggested_values.get("owner_name", "me"),
                description={
                    "label": "System owner name (tags devices; foreign "
                    "go to block_list)",
                },
            ): selector.TextSelector(),
            vol.Optional(
                SZ_LOG_ALL_MQTT,
                default=False,
                description={
                    "suggested_value": suggested_values.get(SZ_LOG_ALL_MQTT)
                },
            ): selector.BooleanSelector(),
        }

        description_placeholders["wiki_url"] = (
            "https://github.com/ramses-rf/ramses_cc/wiki/"
        )

        return self.async_show_form(
            step_id="schema",
            data_schema=vol.Schema(
                # cv.deprecated(
                #     "sqlite_index", raise_if_present=False
                # ),  # Deprecated Q3 2026
                data_schema,
                extra=vol.ALLOW_EXTRA,
            ),  # extra = migration from v1
            description_placeholders=description_placeholders,
            errors=errors,
            last_step=not self._initial_setup,
        )

    async def async_step_advanced_features(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Advanced features step.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
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
                description={
                    "suggested_value": suggested_values.get(CONF_SEND_PACKET)
                },
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_MESSAGE_EVENTS,
                description={
                    "suggested_value": suggested_values.get(
                        CONF_MESSAGE_EVENTS
                    )
                },
            ): selector.TextSelector(),
            vol.Optional(
                CONF_PASSIVE_SCAN,
                default=True,
                description={
                    "suggested_value": suggested_values.get(CONF_PASSIVE_SCAN)
                },
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_AUTO_NOTIFY,
                default=True,
                description={
                    "suggested_value": suggested_values.get(CONF_AUTO_NOTIFY)
                },
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_LOST_THRESHOLD,
                default=7,
                description={
                    "suggested_value": suggested_values.get(
                        CONF_LOST_THRESHOLD
                    )
                },
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=90,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="days",
                )
            ),
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
    ) -> ConfigFlowResult:
        """Packet log step.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        if user_input is not None:
            # Coerce flush_level string from selector back to integer
            if "flush_level" in user_input:
                user_input["flush_level"] = int(user_input["flush_level"])
            self.options[SZ_PACKET_LOG] = user_input
            return self._async_save()

        self.get_options()  # not available during init
        suggested_values = self.options.get(SZ_PACKET_LOG, {})

        data_schema = {
            vol.Optional(
                SZ_PACKET_LOG_PATH,
                default="/config/ramses_rf_logs/",
                description={
                    "suggested_value": suggested_values.get(
                        SZ_PACKET_LOG_PATH, "/config/ramses_rf_logs/"
                    )
                },
            ): selector.TextSelector(),
            vol.Optional(
                SZ_PACKET_LOG_PREFIX,
                default="packet_log",
                description={
                    "suggested_value": suggested_values.get(
                        SZ_PACKET_LOG_PREFIX, "packet_log"
                    )
                },
            ): selector.TextSelector(),
            vol.Optional(
                SZ_PACKET_LOG_RETENTION_DAYS,
                default=7,
                description={
                    "suggested_value": suggested_values.get(
                        SZ_PACKET_LOG_RETENTION_DAYS, 7
                    )
                },
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="days",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Coerce(int),
            ),
            vol.Optional(
                SZ_ROTATE_BYTES,
                description={
                    "suggested_value": suggested_values.get(SZ_ROTATE_BYTES)
                },
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="bytes",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Coerce(int),
            ),
            vol.Optional(
                SZ_BUFFER_CAPACITY,
                default=0,
                description={
                    "suggested_value": suggested_values.get(
                        SZ_BUFFER_CAPACITY, 0
                    )
                },
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="lines",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Coerce(int),
            ),
            vol.Optional(
                SZ_FLUSH_INTERVAL,
                default=60.0,
                description={
                    "suggested_value": suggested_values.get(
                        SZ_FLUSH_INTERVAL, 60.0
                    )
                },
            ): vol.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        step=0.1,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Coerce(float),
            ),
            vol.Optional(
                "flush_level",
                default=str(logging.ERROR),
                description={
                    "suggested_value": str(
                        suggested_values.get("flush_level", logging.ERROR)
                    )
                },
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=str(logging.INFO), label="INFO (20)"
                        ),
                        selector.SelectOptionDict(
                            value=str(logging.WARNING), label="WARNING (30)"
                        ),
                        selector.SelectOptionDict(
                            value=str(logging.ERROR), label="ERROR (40)"
                        ),
                        selector.SelectOptionDict(
                            value=str(logging.CRITICAL), label="CRITICAL (50)"
                        ),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }

        return self.async_show_form(
            step_id="packet_log",
            data_schema=vol.Schema(
                # cv.deprecated(
                #     "file_name", raise_if_present=False
                # ),  # Deprecated Q3 2026
                # cv.deprecated(
                #     "rotate_backups", raise_if_present=False
                # ),    # Deprecated Q3 2026
                data_schema,
                extra=vol.ALLOW_EXTRA,
            ),  # extra = migration from v1
        )


class RamsesConfigFlow(BaseRamsesFlow, ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Config flow for Ramses."""

    VERSION = 3
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize Ramses config flow."""
        super().__init__(initial_setup=True)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user.

        Required by hassfest: if a config flow is discoverable, it must
        set a unique ID.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        await self.async_set_unique_id(DOMAIN)
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return await self.async_step_choose_serial_port()

    def _async_save(self) -> ConfigFlowResult:
        """Save the config flow entry.

        :return: The generated config flow result.
        """
        return self.async_create_entry(
            title="RAMSES RF", data={}, options=self.options
        )

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Import entry from configuration.yaml.

        :param import_data: Data to be imported from config.
        :return: The generated config flow result.
        """
        self.options = deepcopy(import_data)
        self.options[CONF_SCAN_INTERVAL] = import_data[
            CONF_SCAN_INTERVAL
        ].total_seconds()
        self.options.pop(SZ_RESTORE_CACHE, None)

        # Phase 4: migrate known_list traits into schema (same logic as
        # async_migrate_entry v2→v3).  YAML configs may still use known_list.
        known_list = self.options.pop("known_list", None)
        # block_list is added by SCH_DOMAIN_CONFIG validation with a default
        # empty dict — pop it so it doesn't end up in the schema.
        self.options.pop("block_list", None)

        # In YAML config, the TCS schema structure (01:..., orphans_heat,
        # orphans_hvac, etc.) lives at the top level alongside ramses_rf,
        # serial_port, etc.  Move it under CONF_SCHEMA for the config entry.
        _DOMAIN_KEYS = {
            CONF_RAMSES_RF,
            CONF_SCAN_INTERVAL,
            CONF_ADVANCED_FEATURES,
            SZ_SERIAL_PORT,
            SZ_PACKET_LOG,
            SZ_RESTORE_CACHE,
            CONF_SCHEMA,
            "known_list",
            "block_list",
        }
        schema_from_yaml = {
            k: v for k, v in self.options.items() if k not in _DOMAIN_KEYS
        }
        for k in schema_from_yaml:
            self.options.pop(k)

        # Merge with existing CONF_SCHEMA (shouldn't exist in YAML)
        existing_schema = self.options.get(CONF_SCHEMA, {})
        if isinstance(existing_schema, dict):
            merged = {**existing_schema, **schema_from_yaml}
        else:
            merged = schema_from_yaml
        self.options[CONF_SCHEMA] = merged

        if known_list and isinstance(known_list, dict):
            schema = self.options.get(CONF_SCHEMA, {})
            if isinstance(schema, dict):
                self.options[CONF_SCHEMA] = migrate_known_list_traits(
                    schema, known_list
                )

        # Remove enforce_known_list from ramses_rf — always-on now.
        if isinstance(self.options.get(CONF_RAMSES_RF), dict):
            ramses_rf = {**self.options[CONF_RAMSES_RF]}
            ramses_rf.pop("enforce_known_list", None)
            self.options[CONF_RAMSES_RF] = ramses_rf

        return self._async_save()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options callback for Ramses.

        :param config_entry: The loaded configuration entry.
        :return: An instance of the OptionsFlow handler.
        """
        return RamsesOptionsFlowHandler(config_entry)


class RamsesOptionsFlowHandler(BaseRamsesFlow, OptionsFlow):
    """Options config flow handler for Ramses."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize Ramses config options flow."""
        super().__init__()
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the config options.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "choose_serial_port",
                "config",
                "schema",
                "advanced_features",
                "packet_log",
                "review_discovered",
                "review_device_health",
                "clear_cache",
            ],
        )

    def _async_save(self) -> ConfigFlowResult:
        """Save the configured options.

        :return: The generated config flow result.
        """
        result = self.async_create_entry(title="", data=self.options)

        # Reload only if setup failing; updates handled by update listener
        if self.config_entry is not None and self.config_entry.state in (
            ConfigEntryState.SETUP_ERROR,
            ConfigEntryState.SETUP_RETRY,
        ):
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(
                    self.config_entry.entry_id
                )
            )

        return result

    async def async_step_review_discovered(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review discovered devices and accept/decline/skip them.

        Shows devices found by the passive scan that haven't been reviewed yet.
        The user can accept (add to schema), decline (discard),
        or skip (defer decision — device stays NEW and re-appears next review).
        """
        self.get_options()  # populate self.options from config entry

        # Get the coordinator's discovery manager
        coordinator = getattr(self.config_entry, "runtime_data", None)

        if not coordinator or not coordinator.discovery_manager:
            return self.async_show_form(
                step_id="review_discovered",
                description_placeholders={
                    "message": "Passive device scan is not enabled."
                },
                last_step=True,
            )

        # Get pending devices (status=new)
        from .discovery import DiscoveryStatus

        # Sync discovery metadata with the current schema before checking
        # for new devices.  This stashes schema_device_ids so that
        # check_for_new_devices can suppress notifications for devices
        # that are already in the schema but lost their metadata (issue 917).
        # NOTE: use _extract_schema_device_ids (unstripped) so that HGI
        # (18:) entries are included — _strip_and_orchestrate drops them
        # because ramses_rf doesn't need them, but discovery tracking
        # must know they're in the schema (issue 987).
        config_schema_for_sync = self.options.get(CONF_SCHEMA, {})
        if isinstance(config_schema_for_sync, dict):
            from .coordinator import RamsesCoordinator

            schema_device_ids = RamsesCoordinator._extract_schema_device_ids(
                config_schema_for_sync
            )
            foreign_device_ids = RamsesCoordinator._extract_foreign_device_ids(
                config_schema_for_sync
            )
            coordinator.discovery_manager.sync_with_schema(
                schema_device_ids, foreign_device_ids
            )

        # Run an immediate check so devices found by the scan since the
        # last periodic checkpoint are visible without waiting up to 5 min.
        coordinator.discovery_manager.check_for_new_devices()

        # Also check for class mismatches so they're up to date
        config_schema_check = self.options.get(CONF_SCHEMA, {})
        if isinstance(config_schema_check, dict):
            coordinator.discovery_manager.check_class_mismatches(
                config_schema_check
            )
            coordinator.discovery_manager.check_missing_class(
                config_schema_check
            )
            coordinator.discovery_manager.check_name_mismatches(
                config_schema_check,
                zones=coordinator._zones,  # noqa: SLF001
            )

        new_devices = coordinator.discovery_manager.get_devices(
            status=DiscoveryStatus.NEW
        )
        mismatched_devices = (
            coordinator.discovery_manager.get_mismatched_devices()
        )
        missing_class_devices = (
            coordinator.discovery_manager.get_missing_class_devices()
        )
        name_mismatch_devices = (
            coordinator.discovery_manager.get_name_mismatch_devices()
        )
        # Deduplicate: a device could appear in multiple categories — only
        # show it once.  Priority: NEW > class_mismatch > missing_class >
        # name_mismatch.
        new_ids = {d.device.device_id for d in new_devices}
        mismatched_only = [
            e for e in mismatched_devices if e.device.device_id not in new_ids
        ]
        seen_ids = new_ids | {e.device.device_id for e in mismatched_only}
        missing_class_only = [
            e
            for e in missing_class_devices
            if e.device.device_id not in seen_ids
        ]
        seen_ids |= {e.device.device_id for e in missing_class_only}
        name_mismatch_only = [
            e
            for e in name_mismatch_devices
            if e.device.device_id not in seen_ids
        ]
        devices = new_devices
        if (
            not devices
            and not mismatched_only
            and not missing_class_only
            and not name_mismatch_only
        ):
            # If the user already submitted the form, close it.
            # Otherwise show the "no devices" message once.
            if user_input is not None:
                return self._async_save()
            return self.async_show_form(
                step_id="review_discovered",
                description_placeholders={
                    "message": "No new devices to review."
                },
                last_step=True,
            )

        if user_input is not None:
            # Process accept/decline for each device
            config_schema = dict(self.options.get(CONF_SCHEMA, {}))
            changed = False

            # Determine the root owner name.  If the user provided one,
            # store it as the root _owner key.  Default to "me" if not set.
            root_owner = user_input.get("owner_name", "").strip()
            if not root_owner:
                root_owner = config_schema.get(SZ_OWNER, "me")
            if (
                SZ_OWNER not in config_schema
                or config_schema[SZ_OWNER] != root_owner
            ):
                config_schema[SZ_OWNER] = root_owner
                changed = True

            # Determine the CTL ID from the schema's main_tcs so that
            # OTB/BDR devices are placed as appliance_control/hotwater_valve
            # instead of orphans_heat when auto-generating schema entries.
            ctl_id = (
                config_schema.get("main_tcs")
                if isinstance(config_schema.get("main_tcs"), str)
                else None
            )

            # Check for bulk action
            bulk = user_input.get("bulk_action", "none")

            for entry in devices:
                device_id = entry.device.device_id
                # Per-device action overrides bulk action unless per-device
                # is "skip" (default) and bulk is not "none"
                per_device = user_input.get(f"device_{device_id}", "skip")
                action = per_device if per_device != "skip" else bulk
                if action in ("none", "skip"):
                    # Mark as skipped in the schema so it's visible and
                    # survives cache loss (lives in config entry, not .storage)
                    from .schemas import remove_device_from_schema

                    config_schema = remove_device_from_schema(
                        config_schema, device_id
                    )
                    if device_id not in config_schema:
                        config_schema[device_id] = {}
                    config_schema[device_id][SZ_TR_SKIPPED] = True
                    config_schema[device_id][SZ_TR_OWNER] = root_owner
                    changed = True
                    continue
                if action == "accept":
                    # Accept the device — this generates a schema entry
                    accepted = coordinator.discovery_manager.accept_device(
                        device_id,
                        owner=user_input.get(f"owner_{device_id}"),
                        ctl_id=ctl_id,
                    )
                    # Add to schema using the generated schema entry
                    if accepted.metadata.schema_entry:
                        from ramses_rf.helpers import deep_merge

                        from .schemas import remove_device_from_schema
                        from .services import _resolve_single_slot_conflicts

                        # Remove from old location, then merge with fragment
                        # as src (precedence) so the new placement wins.
                        # Resolve single-slot conflicts (appliance_control,
                        # hotwater_valve, heating_valve) before merging so
                        # that accepting a second relay for the same slot
                        # doesn't displace the first — which would create an
                        # orphan that gets re-discovered and re-notified
                        # every checkpoint (ramses-rf/ramses_cc#917).
                        fragment = _resolve_single_slot_conflicts(
                            accepted.metadata.schema_entry,
                            config_schema,
                            device_id,
                        )
                        config_schema = remove_device_from_schema(
                            config_schema, device_id
                        )
                        config_schema = deep_merge(fragment, config_schema)
                        # Clear _skipped — deep_merge can't remove keys
                        dev_entry = config_schema.get(device_id)
                        if isinstance(dev_entry, dict):
                            dev_entry.pop(SZ_TR_SKIPPED, None)
                            dev_entry.pop("_comment", None)
                            # Use per-device owner if provided, else root
                            # owner. Lets user accept device (create entities)
                            # while tagging as foreign (e.g. neighbour's FAN).
                            per_device_owner = (
                                user_input.get(f"owner_{device_id}", "") or ""
                            ).strip()
                            dev_entry[SZ_TR_OWNER] = (
                                per_device_owner
                                if per_device_owner
                                else root_owner
                            )
                        changed = True
                elif action == "decline":
                    # Decline — mark foreign owner so it goes to block_list
                    # (not known_list). Prevents log spam without creating
                    # entities. Stays in schema for visibility.
                    coordinator.discovery_manager.discard_device(device_id)
                    # Remove from old location, then add as trait-only entry
                    from .schemas import remove_device_from_schema

                    config_schema = remove_device_from_schema(
                        config_schema, device_id
                    )
                    if device_id not in config_schema:
                        config_schema[device_id] = {}
                    config_schema[device_id][SZ_TR_OWNER] = "not-me"
                    changed = True

            # Check if any class updates will happen — backup before modifying
            has_class_update = any(
                user_input.get(f"mismatch_{entry.device.device_id}")
                == "update_class"
                for entry in mismatched_only
            )
            if has_class_update and coordinator.store:
                await coordinator.store.async_save_backup(
                    config_schema,
                    {},  # known_list removed in Phase 4 — schema is sole SSOT
                    reason="class_update",
                )

            # Process class mismatch devices (already accepted, _class differs)
            class_updates: list[str] = []
            for entry in mismatched_only:
                device_id = entry.device.device_id
                action = user_input.get(f"mismatch_{device_id}", "skip")
                if action == "update_class":
                    # Update _class in schema to match discovery's likely_type
                    dev_entry = config_schema.get(device_id)
                    if isinstance(dev_entry, dict):
                        dev_entry[SZ_TR_CLASS] = str(entry.device.likely_type)
                        # Apply per-device owner if provided, else root owner
                        per_device_owner = (
                            user_input.get(f"owner_{device_id}", "") or ""
                        ).strip()
                        dev_entry[SZ_TR_OWNER] = (
                            per_device_owner
                            if per_device_owner
                            else root_owner
                        )
                        changed = True
                        class_updates.append(device_id)
                        _LOGGER.info(
                            "review_discovered: updated _class for %s to %s "
                            "(discovery suggestion accepted)",
                            device_id,
                            entry.device.likely_type,
                        )
                    # Clear dismissed flag — mismatch resolved by updating
                    meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if meta:
                        meta.class_mismatch_dismissed = False
                elif action == "keep":
                    # Keep existing _class — but still apply owner
                    dev_entry = config_schema.get(device_id)
                    if isinstance(dev_entry, dict):
                        per_device_owner = (
                            user_input.get(f"owner_{device_id}", "") or ""
                        ).strip()
                        if per_device_owner or dev_entry.get(SZ_TR_OWNER) != (
                            per_device_owner
                            if per_device_owner
                            else root_owner
                        ):
                            dev_entry[SZ_TR_OWNER] = (
                                per_device_owner
                                if per_device_owner
                                else root_owner
                            )
                            changed = True
                # "keep" or "skip" — do nothing to _class, schema stays as-is
                # Clear the mismatch flag for both "update_class" and "keep"
                if action in ("update_class", "keep"):
                    meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if meta:
                        meta.class_mismatch = None
                        if action == "keep":
                            # Persist dismissal so check_class_mismatches
                            # doesn't re-flag device on next checkpoint
                            meta.class_mismatch_dismissed = True

            # Process missing_class devices (accepted, no _class in schema,
            # but discovery has a likely_type).  The user can add _class
            # from the discovery suggestion or skip.
            for entry in missing_class_only:
                device_id = entry.device.device_id
                action = user_input.get(f"missing_class_{device_id}", "skip")
                if action == "add_class":
                    dev_entry = config_schema.get(device_id)
                    if isinstance(dev_entry, dict):
                        dev_entry[SZ_TR_CLASS] = str(entry.device.likely_type)
                        # Apply per-device owner if provided, else root owner
                        per_device_owner = (
                            user_input.get(f"owner_{device_id}", "") or ""
                        ).strip()
                        dev_entry[SZ_TR_OWNER] = (
                            per_device_owner
                            if per_device_owner
                            else root_owner
                        )
                        changed = True
                        _LOGGER.info(
                            "review_discovered: added _class=%s for %s "
                            "(was missing, discovery suggestion accepted)",
                            entry.device.likely_type,
                            device_id,
                        )
                # Clear missing_class flag for both "add_class" and "skip"
                # so notification doesn't re-fire immediately. For "skip"
                # also set missing_class_dismissed to prevent re-flagging.
                if action in ("add_class", "skip"):
                    meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if meta:
                        meta.missing_class = None
                        if action == "skip":
                            # Persist dismissal so check_missing_class
                            # doesn't re-flag device on next checkpoint
                            meta.missing_class_dismissed = True
                        elif action == "add_class":
                            # User added a class — clear any prior dismissal
                            meta.missing_class_dismissed = False

            # Process name mismatch zones (schema _name differs from
            # controller's 0004 name).  The controller is authoritative
            # for _name — the user can update _name to match, or use
            # _alias for a custom display name.  No dismiss option.
            for entry in name_mismatch_only:
                device_id = entry.device.device_id
                action = user_input.get(f"name_mismatch_{device_id}", "skip")
                if action == "update_name":
                    nm = entry.metadata.name_mismatch or ""
                    ctrl_name = (
                        nm.split("controller=")[1]
                        if "controller=" in nm
                        else None
                    )
                    if ctrl_name:
                        # zone_id is "<ctl_id>_<zone_index>"
                        parts = device_id.rsplit("_", 1)
                        if len(parts) == 2:
                            ctl_id, zone_index = parts
                            ctl_entry = config_schema.get(ctl_id)
                            if isinstance(ctl_entry, dict):
                                ctl_zones = ctl_entry.get("zones")
                                if isinstance(ctl_zones, dict):
                                    zone_entry = ctl_zones.get(zone_index)
                                    if isinstance(zone_entry, dict):
                                        zone_entry[SZ_TR_NAME] = ctrl_name
                                        changed = True
                                        _LOGGER.info(
                                            "review_discovered: updated _name "
                                            "for zone %s to %r (controller "
                                            "authoritative, issue 947)",
                                            device_id,
                                            ctrl_name,
                                        )
                # Clear the name_mismatch flag for both "update_name"
                # and "skip".  For "skip", the flag will be re-set on the
                # next checkpoint by check_name_mismatches (no dismiss —
                # the schema _name should match the controller).
                meta = coordinator.discovery_manager._metadata.get(device_id)
                if meta:
                    meta.name_mismatch = None

            if changed:
                self.options[CONF_SCHEMA] = order_schema(config_schema)

            # Persist discovery metadata before the reload triggered by
            # _async_save().  Without this, the ACCEPTED/DISCARDED status
            # set by accept_device/discard_device above is lost when the
            # coordinator is torn down and recreated — the new coordinator
            # restores from .storage/, which only gets updated during the
            # 5-minute checkpoint.  After reload, check_for_new_devices
            # sees the devices with no metadata and re-notifies them as
            # NEW (issue 917).
            if coordinator.discovery_manager:
                try:
                    await coordinator.async_save_client_state()
                except Exception as err:
                    _LOGGER.warning(
                        "review_discovered: failed to persist discovery "
                        "state before reload: %s",
                        err,
                    )

            return self._async_save()

        # Build a summary table for the description
        lines: list[str] = []
        if devices:
            lines.append(f"**{len(devices)} new device(s) to review:**\n")
            lines.append(
                "| Device | Type | Conf | RSSI | Codes | Bound | Zone | "
                "Batt | Pkts |"
            )
            lines.append(
                "|--------|------|------|------|-------|-------|------|------|------|"
            )
            for entry in devices:
                d = entry.device
                codes = ", ".join(sorted(d.codes_seen[:4]))
                if len(d.codes_seen) > 4:
                    codes += f" (+{len(d.codes_seen) - 4})"
                rssi = f"{d.rssi:.0f}" if d.rssi is not None else "—"
                packet_count = d.source_count + d.destination_count
                battery = "yes" if d.is_battery else "no"
                bound = d.bound_to or "—"
                zone_s = d.zone_index or "—"
                lines.append(
                    f"| `{d.device_id}` | {d.likely_type or '?'} | "
                    f"{d.confidence} | {rssi} | {codes} | {bound} | "
                    f"{zone_s} | {battery} | {packet_count} |"
                )

        if mismatched_only:
            if lines:
                lines.append("\n")
            lines.append(
                f"**{len(mismatched_only)} device(s) with class mismatch:**\n"
            )
            lines.append(
                "| Device | Schema _class | Discovery suggests | Confidence |"
            )
            lines.append(
                "|--------|---------------|-------------------|------------|"
            )
            for entry in mismatched_only:
                d = entry.device
                # Parse the mismatch desc: "schema=FAN, discovery=DIS"
                mm = entry.metadata.class_mismatch or ""
                schema_cls = (
                    mm.split("schema=")[1].split(",")[0]
                    if "schema=" in mm
                    else "?"
                )
                disc_cls = (
                    mm.split("discovery=")[1] if "discovery=" in mm else "?"
                )
                lines.append(
                    f"| `{d.device_id}` | {schema_cls} | {disc_cls} | "
                    f"{d.confidence} |"
                )

        if missing_class_only:
            if lines:
                lines.append("\n")
            lines.append(
                f"**{len(missing_class_only)} device(s) with missing "
                "_class:**\n"
            )
            lines.append("| Device | Discovery suggests | Confidence |")
            lines.append("|--------|-------------------|------------|")
            for entry in missing_class_only:
                d = entry.device
                # Parse the missing_class desc: "discovery=FAN"
                mc = entry.metadata.missing_class or ""
                disc_cls = (
                    mc.split("discovery=")[1] if "discovery=" in mc else "?"
                )
                lines.append(
                    f"| `{d.device_id}` | {disc_cls} | {d.confidence} |"
                )

        if name_mismatch_only:
            if lines:
                lines.append("\n")
            lines.append(
                f"**{len(name_mismatch_only)} zone(s) with name mismatch:**\n"
            )
            lines.append("| Zone | Schema _name | Controller reports |")
            lines.append("|------|-------------|-------------------|")
            for entry in name_mismatch_only:
                d = entry.device
                nm = entry.metadata.name_mismatch or ""
                schema_name = (
                    nm.split("schema=")[1].split(",")[0]
                    if "schema=" in nm
                    else "?"
                )
                ctrl_name = (
                    nm.split("controller=")[1] if "controller=" in nm else "?"
                )
                lines.append(
                    f"| `{d.device_id}` | {schema_name} | {ctrl_name} |"
                )

        if not lines:
            lines.append("No new devices or mismatches to review.")
        summary = "\n".join(lines)

        # Build form with device selectors — each field name includes
        # the device info so the user can see what they're accepting.
        form_fields: dict[Any, Any] = {}

        # Owner name field — sets the ROOT _owner in the schema.
        # This is the system-wide owner.  Per-device owner fields below
        # override it for individual devices.  If no root _owner was set
        # yet, this fills it in.
        existing_owner = self.options.get(CONF_SCHEMA, {}).get(SZ_OWNER, "")
        form_fields[
            vol.Required(
                "owner_name",
                default=existing_owner or "me",
                description={
                    "label": "Root owner name (applies to all devices "
                    "without a per-device owner below)",
                },
            )
        ] = selector.TextSelector()

        # Bulk action selector — applies to all devices that are still "skip"
        form_fields[
            vol.Required(
                "bulk_action",
                default="none",
                description={
                    "label": "Apply to all devices (overridden by "
                    "per-device choice)"
                },
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": "none", "label": "No bulk action"},
                    {"value": "accept", "label": "Accept all"},
                    {"value": "decline", "label": "Decline all"},
                    {"value": "skip", "label": "Skip all"},
                ],
            )
        )

        for entry in devices:
            d = entry.device
            device_id = d.device_id
            # Build a descriptive name for the field
            desc_parts = [f"{device_id}", f"type={d.likely_type or '?'}"]
            if d.confidence:
                desc_parts.append(f"conf={d.confidence}")
            if d.bound_to:
                desc_parts.append(f"bound={d.bound_to}")
            if d.zone_index:
                desc_parts.append(f"zone={d.zone_index}")
            if d.is_battery:
                desc_parts.append("battery")
            packet_count = d.source_count + d.destination_count
            desc_parts.append(f"packets={packet_count}")
            field_label = " | ".join(desc_parts)

            form_fields[
                vol.Required(
                    f"device_{device_id}",
                    default="skip",
                    description={"label": field_label},
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "skip", "label": "Skip for now"},
                        {"value": "accept", "label": "Accept"},
                        {"value": "decline", "label": "Decline"},
                    ],
                )
            )
            form_fields[
                vol.Optional(
                    f"owner_{device_id}",
                    description={
                        "label": f"Owner for {device_id} (overrides "
                        "root owner)"
                    },
                )
            ] = selector.TextSelector()

        # Add form fields for class mismatch devices
        config_schema_for_prefill = self.options.get(CONF_SCHEMA, {})
        for entry in mismatched_only:
            d = entry.device
            device_id = d.device_id
            mm = entry.metadata.class_mismatch or ""
            schema_cls = (
                mm.split("schema=")[1].split(",")[0]
                if "schema=" in mm
                else "?"
            )
            disc_cls = mm.split("discovery=")[1] if "discovery=" in mm else "?"
            field_label = (
                f"{device_id} | schema _class={schema_cls} → "
                f"discovery suggests {disc_cls} (conf={d.confidence})"
            )
            form_fields[
                vol.Required(
                    f"mismatch_{device_id}",
                    default="skip",
                    description={"label": field_label},
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "skip", "label": "Skip for now"},
                        {
                            "value": "update_class",
                            "label": f"Update to {disc_cls}",
                        },
                        {"value": "keep", "label": f"Keep {schema_cls}"},
                    ],
                )
            )
            existing_dev_owner = (
                config_schema_for_prefill.get(device_id, {}).get(
                    SZ_TR_OWNER, ""
                )
                if isinstance(config_schema_for_prefill.get(device_id), dict)
                else ""
            )
            form_fields[
                vol.Optional(
                    f"owner_{device_id}",
                    description={
                        "label": f"Owner for {device_id} (overrides "
                        "root owner)",
                        "suggested_value": existing_dev_owner,
                    },
                )
            ] = selector.TextSelector()

        # Add form fields for missing_class devices
        for entry in missing_class_only:
            d = entry.device
            device_id = d.device_id
            mc = entry.metadata.missing_class or ""
            disc_cls = mc.split("discovery=")[1] if "discovery=" in mc else "?"
            field_label = (
                f"{device_id} | no _class in schema → "
                f"discovery suggests {disc_cls} (conf={d.confidence})"
            )
            form_fields[
                vol.Required(
                    f"missing_class_{device_id}",
                    default="skip",
                    description={"label": field_label},
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "skip", "label": "Skip for now"},
                        {
                            "value": "add_class",
                            "label": f"Add _class: {disc_cls}",
                        },
                    ],
                )
            )
            existing_dev_owner = (
                config_schema_for_prefill.get(device_id, {}).get(
                    SZ_TR_OWNER, ""
                )
                if isinstance(config_schema_for_prefill.get(device_id), dict)
                else ""
            )
            form_fields[
                vol.Optional(
                    f"owner_{device_id}",
                    description={
                        "label": f"Owner for {device_id} (overrides "
                        "root owner)",
                        "suggested_value": existing_dev_owner,
                    },
                )
            ] = selector.TextSelector()

        # Add form fields for name mismatch zones.
        # The controller's 0004 name is authoritative for _name.
        # The user can update _name to match, or use _alias for a custom
        # display name.  No dismiss option — the schema _name should
        # match the controller (issue 947).
        for entry in name_mismatch_only:
            d = entry.device
            device_id = d.device_id
            nm = entry.metadata.name_mismatch or ""
            schema_name = (
                nm.split("schema=")[1].split(",")[0]
                if "schema=" in nm
                else "?"
            )
            ctrl_name = (
                nm.split("controller=")[1] if "controller=" in nm else "?"
            )
            field_label = (
                f"{device_id} | schema _name={schema_name} → "
                f"controller reports {ctrl_name}"
            )
            form_fields[
                vol.Required(
                    f"name_mismatch_{device_id}",
                    default="update_name",
                    description={"label": field_label},
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {
                            "value": "update_name",
                            "label": f"Update _name to {ctrl_name}",
                        },
                        {
                            "value": "skip",
                            "label": "Skip (will re-appear next checkpoint)",
                        },
                    ],
                )
            )

        return self.async_show_form(
            step_id="review_discovered",
            data_schema=vol.Schema(form_fields),
            description_placeholders={"message": summary},
            last_step=True,
        )

    async def async_step_review_device_health(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review devices that haven't been seen recently.

        Shows orphaned devices (in schema but not seen by scan for >threshold)
        and lost devices (ACCEPTED, not seen for >threshold).  The user can
        keep (dismiss the flag) or remove (full cleanup via remove_device
        service) each device individually.
        """
        self.get_options()

        coordinator = getattr(self.config_entry, "runtime_data", None)

        if not coordinator or not coordinator.discovery_manager:
            return self.async_show_form(
                step_id="review_device_health",
                description_placeholders={
                    "message": "Passive device scan is not enabled."
                },
                last_step=True,
            )

        from .discovery import DiscoveryStatus

        # Run an immediate check so the flags are up to date
        config_schema_check = self.options.get(CONF_SCHEMA, {})
        if isinstance(config_schema_check, dict):
            coordinator.discovery_manager.check_orphaned_devices(
                config_schema_check
            )
            coordinator.discovery_manager.check_for_lost_devices()

        orphaned_devices = coordinator.discovery_manager.get_orphaned_devices()
        lost_devices = coordinator.discovery_manager.get_lost_devices()

        # Deduplicate: a device could be both orphaned and lost — only
        # show it once, prioritising LOST (more severe).
        lost_ids = {e.device.device_id for e in lost_devices}
        orphaned_only = [
            e for e in orphaned_devices if e.device.device_id not in lost_ids
        ]

        if not orphaned_only and not lost_devices:
            if user_input is not None:
                return self._async_save()
            return self.async_show_form(
                step_id="review_device_health",
                description_placeholders={
                    "message": "No orphaned or lost devices."
                },
                last_step=True,
            )

        if user_input is not None:
            # Process each device — "keep" clears the flag, "remove" calls
            # the remove_device service for full cleanup.
            config_schema = dict(self.options.get(CONF_SCHEMA, {}))
            removed_any = False
            for entry in lost_devices:
                device_id = entry.device.device_id
                action = user_input.get(f"lost_{device_id}", "keep")
                if action == "remove":
                    await self.hass.services.async_call(
                        DOMAIN,
                        "remove_device",
                        {"device_id": device_id},
                        blocking=True,
                    )
                    removed_any = True
                    _LOGGER.info(
                        "review_device_health: removed lost device %s",
                        device_id,
                    )
                elif action == "keep":
                    # Clear LOST status → back to ACCEPTED, clear orphaned.
                    # Set _suppress_not_seen in the schema so future
                    # orphaned notifications are suppressed (issue 988).
                    # An INFO log is still emitted once every
                    # threshold_days as a gentle reminder.
                    meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if meta:
                        meta.status = DiscoveryStatus.ACCEPTED
                        meta.orphaned = None
                    # Mark in schema to suppress future notifications
                    # Default: 7 days (then re-notify if still not seen).
                    # User can set True (forever) or a different number
                    # of days manually in the schema.
                    dev_entry = config_schema.get(device_id)
                    if isinstance(dev_entry, dict):
                        dev_entry["_suppress_not_seen"] = 7
                        config_schema[device_id] = dev_entry

            for entry in orphaned_only:
                device_id = entry.device.device_id
                action = user_input.get(f"orphaned_{device_id}", "keep")
                if action == "remove":
                    await self.hass.services.async_call(
                        DOMAIN,
                        "remove_device",
                        {"device_id": device_id},
                        blocking=True,
                    )
                    removed_any = True
                    _LOGGER.info(
                        "review_device_health: removed orphaned device %s",
                        device_id,
                    )
                elif action == "keep":
                    # Clear orphaned flag and set _suppress_not_seen in
                    # the schema so check_orphaned_devices doesn't re-notify
                    # on the next checkpoint cycle (issue 988).  An INFO
                    # log is still emitted once every threshold_days.
                    meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if meta:
                        meta.orphaned = None
                    dev_entry = config_schema.get(device_id)
                    if isinstance(dev_entry, dict):
                        dev_entry["_suppress_not_seen"] = 7
                        config_schema[device_id] = dev_entry

            # Save discovery metadata to .storage via coordinator's save
            # cycle (export_state is called in async_save_client_state)
            await coordinator.async_save_client_state()

            if removed_any:
                # remove_device service already updated the config entry,
                # so refresh self.options from the coordinator to avoid
                # overwriting with stale data, then save normally
                self.options = dict(coordinator.options)
            else:
                # No removals — update schema in self.options with
                # _suppress_not_seen flags set by "keep" actions
                self.options[CONF_SCHEMA] = config_schema

            return self._async_save()

        # Build summary table
        lines: list[str] = []
        if lost_devices:
            lines.append(
                f"**{len(lost_devices)} lost device(s)** "
                "(accepted but not seen for >7 days):\n"
            )
            lines.append("| Device | Type | Last seen | Status |")
            lines.append("|--------|------|-----------|--------|")
            for entry in lost_devices:
                d = entry.device
                last_seen = getattr(d, "last_seen", "—")
                lines.append(
                    f"| `{d.device_id}` | {d.likely_type or '?'} | "
                    f"{last_seen} | LOST |"
                )

        if orphaned_only:
            if lines:
                lines.append("\n")
            lines.append(
                f"**{len(orphaned_only)} orphaned device(s)** "
                "(in schema but not seen recently):\n"
            )
            lines.append("| Device | Type | Last seen | Note |")
            lines.append("|--------|------|-----------|------|")
            for entry in orphaned_only:
                d = entry.device
                last_seen = getattr(d, "last_seen", "—")
                note = entry.metadata.orphaned or ""
                lines.append(
                    f"| `{d.device_id}` | {d.likely_type or '?'} "
                    f"| {last_seen} | {note} |"
                )

        if not lines:
            lines.append("No orphaned or lost devices.")
        summary = "\n".join(lines)

        # Build form with per-device Keep/Remove selectors
        form_fields: dict[Any, Any] = {}

        for entry in lost_devices:
            d = entry.device
            device_id = d.device_id
            last_seen = getattr(d, "last_seen", "—")
            field_label = (
                f"{device_id} | {d.likely_type or '?'} | "
                f"last seen: {last_seen} | LOST"
            )
            form_fields[
                vol.Required(
                    f"lost_{device_id}",
                    default="keep",
                    description={"label": field_label},
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "keep", "label": "Keep (dismiss flag)"},
                        {"value": "remove", "label": "Remove device"},
                    ],
                )
            )

        for entry in orphaned_only:
            d = entry.device
            device_id = d.device_id
            last_seen = getattr(d, "last_seen", "—")
            field_label = (
                f"{device_id} | {d.likely_type or '?'} | "
                f"last seen: {last_seen} | orphaned"
            )
            form_fields[
                vol.Required(
                    f"orphaned_{device_id}",
                    default="keep",
                    description={"label": field_label},
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "keep", "label": "Keep (dismiss flag)"},
                        {"value": "remove", "label": "Remove device"},
                    ],
                )
            )

        return self.async_show_form(
            step_id="review_device_health",
            data_schema=vol.Schema(form_fields),
            description_placeholders={"message": summary},
            last_step=True,
        )

    async def async_step_clear_cache(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Clear cache step.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        if user_input is not None:
            # Unload immediately to stop scheduled coordinator state saves
            if (
                self.config_entry is not None
                and self.config_entry.state == ConfigEntryState.LOADED
                and self.config_entry.entry_id is not None
            ):
                await self.hass.config_entries.async_unload(
                    self.config_entry.entry_id
                )

            # When clearing the schema, also remove stale HA device registry
            # entries for this config entry.  Without this, ramses_cc recreates
            # all old devices from the HA device registry on reload even though
            # .storage and the config schema were wiped.
            if user_input["clear_schema"] and self.config_entry is not None:
                dev_reg = dr.async_get(self.hass)
                stale = dr.async_entries_for_config_entry(
                    dev_reg, self.config_entry.entry_id
                )
                for dev in stale:
                    dev_reg.async_remove_device(dev.id)
                if stale:
                    _LOGGER.info(
                        "Clear cache: removed %d stale HA device(s)",
                        len(stale),
                    )

            store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
            stored_data: dict[str, Any] = await store.async_load() or {}

            if SZ_CLIENT_STATE in stored_data:
                if user_input["clear_schema"]:
                    stored_data[SZ_CLIENT_STATE].pop(SZ_SCHEMA)

                    def filter_schema_packets(
                        packets: dict[str, dict[str, Any] | str],
                    ) -> dict[str, dict[str, Any] | str]:
                        """Filter packets used for schema discovery.

                        :param packets: The cached packets.
                        :return: The filtered packets.
                        """
                        msg_code_filter = {"0004", "0005", "000C"}
                        return {
                            dtm: packet
                            for dtm, packet in packets.items()
                            if (  # PacketDTO format since 0.56.3, cf coord
                                isinstance(packet, dict)
                                and packet.get("code") not in msg_code_filter
                            )
                            or (  # legacy 0.54.x string packets
                                isinstance(packet, str)
                                and not any(
                                    f" {code} " in packet
                                    for code in msg_code_filter
                                )
                            )
                        }

                    # Filter out cached packets used for schema discovery
                    stored_data[SZ_CLIENT_STATE][SZ_PACKETS] = (
                        filter_schema_packets(
                            stored_data[SZ_CLIENT_STATE].get(SZ_PACKETS, {})
                        )
                    )

                if user_input["clear_packets"]:
                    stored_data[SZ_CLIENT_STATE].pop(SZ_PACKETS)

            if user_input.get("clear_discovery") or user_input["clear_schema"]:
                from .discovery import SZ_DISCOVERY

                stored_data.pop(SZ_DISCOVERY, None)
                if user_input["clear_schema"] and not user_input.get(
                    "clear_discovery"
                ):
                    _LOGGER.info(
                        "Clear cache: clearing discovery metadata "
                        "(schema wiped, devices should be re-discovered "
                        "as NEW)"
                    )

            await store.async_save(stored_data)

            # Also clear the config entry options (schema)
            # so that a fresh start truly starts from zero.  The .storage
            # cache is only half the story — the config entry options hold
            # the authoritative schema that ramses_rf uses to create devices.
            # Without clearing them, devices reappear immediately on restart.
            #
            # The CONF_FRESH_START flag tells the coordinator to wipe
            # .storage on its next setup, covering the race where the
            # unload save re-populates .storage after we just cleared it.
            if self.config_entry is not None and (
                user_input["clear_schema"] or user_input["clear_packets"]
            ):
                new_options = dict(self.config_entry.options)
                if user_input["clear_schema"]:
                    new_options.pop(CONF_SCHEMA, None)
                new_options[CONF_FRESH_START] = True
                self.hass.config_entries.async_update_entry(
                    self.config_entry, options=new_options
                )

            if (
                self.config_entry is not None
                and self.config_entry.entry_id is not None
            ):
                self.hass.async_create_task(
                    self.hass.config_entries.async_setup(
                        self.config_entry.entry_id
                    )
                )

            return self.async_abort(reason="cache_cleared")

        data_schema = {
            vol.Required(
                "clear_schema", default=False
            ): selector.BooleanSelector(),
            vol.Required(
                "clear_packets", default=False
            ): selector.BooleanSelector(),
            vol.Required(
                "clear_discovery", default=False
            ): selector.BooleanSelector(),
            # clear_known_list was removed in Phase 4 — known_list is now
            # derived from schema, so clearing the schema is sufficient.
        }

        return self.async_show_form(
            step_id="clear_cache",
            data_schema=vol.Schema(data_schema),
        )

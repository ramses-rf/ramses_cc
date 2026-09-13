"""Config flow to configure Ramses integration."""

import asyncio
import logging
import os
import re
from abc import abstractmethod
from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import urlparse

import probatio as prob
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
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    selector,
)
from homeassistant.helpers.storage import Store

from ramses_rf.const import SZ_ACCEPT, SZ_ZONES
from ramses_rf.schemas import (
    SCH_GATEWAY_DICT,
    SCH_GLOBAL_SCHEMAS,
    SZ_RESTORE_CACHE,
    SZ_SCHEMA,
)
from ramses_tx.const import DEVICE_ID_REGEX, HGI_ID_PATTERN, Code
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
from ramses_tx.transport.helpers import redact_url

from .const import (
    CONF_ADDITIONAL_PORTS,
    CONF_ADVANCED_FEATURES,
    CONF_AUTO_NOTIFY,
    CONF_FRESH_START,
    CONF_GATEWAY_OFFLINE_NOTIFY,
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
    CONF_WAIT_ONLINE_TIMEOUT,
    DEFAULT_HGI_ID,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_WAIT_ONLINE_TIMEOUT,
    DOMAIN,
    HGI_COMMENT_WARNING,
    HGI_PREFIX,
    STORAGE_KEY,
    STORAGE_VERSION,
    SZ_CLIENT_STATE,
    SZ_DEVICE_COMMENTS,
    SZ_OWNER,
    SZ_PACKETS,
    SZ_TR_CLASS,
    SZ_TR_COMMENT,
    SZ_TR_NAME,
    SZ_TR_OWNER,
    SZ_TR_SKIPPED,
    build_hgi_comment,
)
from .ha_compat import _REAL_VOL, vol_schema
from .schemas import migrate_known_list_traits, order_schema

_LOGGER = logging.getLogger(__name__)

CONF_MANUAL_PATH: Final = "Enter Manually..."  # TODO i18n these strings
CONF_MQTT_PATH: Final = "MQTT Broker..."
CONF_HA_MQTT_PATH: Final = "Use Home Assistant MQTT - In development!"
CONF_ZIGBEE_DEVICE: Final = "Zigbee device"

# HGI device ID regex: 18:NNNNNN (class 18, 6 decimal digits).
# Uses DEVICE_ID_REGEX.HGI from ramses_tx (single source of truth).
_HGI_ID_RE: Final = DEVICE_ID_REGEX.HGI


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
    from serialx import list_serial_ports

    # Compatible with all earlier versions.
    # TODO: remove Q3 2026
    def get_usb_ports() -> dict[str, str]:
        """Return a dict of USB ports and their friendly names.

        :return: A dictionary mapping device paths to descriptions.
        """
        ports = list_serial_ports()
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
    return cast(
        dict[str, str], await hass.async_add_executor_job(get_usb_ports)
    )


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
                    if part.startswith(HGI_PREFIX):
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

    async def _async_validate_port_connection(
        self, port_name: str
    ) -> str | None:
        """Validate port reachability and syntax before completing configuration.

        :param port_name: The port path, URI, or selection identifier.
        :type port_name: str
        :returns: Error key string if validation fails, or None if valid.
        :rtype: str | None
        """
        if not port_name:
            return "port_name_required"

        if port_name in (CONF_HA_MQTT_PATH, "mqtt_ha"):
            mqtt_entries = self.hass.config_entries.async_entries("mqtt")
            if not any(
                entry.state == ConfigEntryState.LOADED
                for entry in mqtt_entries
            ):
                return "mqtt_missing"
            return None

        if port_name.startswith("mqtt://"):
            try:
                parsed = urlparse(port_name)
                if not parsed.hostname:
                    return "cannot_connect"
                if parsed.port is not None and not (0 < parsed.port <= 65535):
                    return "cannot_connect"
            except (ValueError, AttributeError):
                return "cannot_connect"
            return None

        if port_name.startswith("zigbee://"):
            try:
                parsed = urlparse(port_name)
                path_parts = [
                    p for p in parsed.path.strip("/").split("/") if p
                ]
                if not parsed.netloc or len(path_parts) < 6:
                    return "invalid_port_config"
            except (ValueError, AttributeError):
                return "invalid_port_config"
            return None

        if port_name.startswith(
            ("rfc2217://", "socket://", "tcp://", "spy://", "alt://")
        ):
            try:
                parsed = urlparse(port_name)
                if not parsed.hostname or not parsed.port:
                    return "cannot_connect"
                if not (0 < parsed.port <= 65535):
                    return "cannot_connect"
            except (ValueError, AttributeError):
                return "cannot_connect"
            return None

        def _check_local_port() -> bool:
            try:
                if port_name.startswith("/dev/"):
                    return os.path.exists(port_name)
                if port_name.startswith("COM") or port_name == "/dev/null":
                    return True
                return os.path.exists(port_name)
            except OSError:
                return False

        is_valid = await self.hass.async_add_executor_job(_check_local_port)
        if not is_valid:
            return "cannot_connect"

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
                    redact_url(user_input[SZ_PORT_NAME]),
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
        # friendly name in the selector label. Otherwise, show a generic label.
        try:
            dev_reg = dr.async_get(self.hass)
            matches = [
                dev_entry
                for dev_entry in dev_reg.async_get_devices(identifiers=DOMAIN)
                if "ramses_esp32c6"
                in (dev_reg.async_get(dev_entry).model or "").lower()
            ]
            if len(matches) == 1:
                raw_name = (
                    dev_reg.async_get(matches[0]).name
                    or dev_reg.async_get(matches[0]).name_by_user
                    or dev_reg.async_get(matches[0]).id
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
            default_port = prob.UNDEFINED
        elif port_name in ports:
            default_port = port_name
        else:
            default_port = CONF_MANUAL_PATH

        data_schema = {
            prob.Required(
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

        # used in test_configure_serial_port_missing_port_name
        _optional_schema = {
            prob.Optional(
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
            data_schema=vol_schema(data_schema),
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
            prob.Required(
                "host", description={"suggested_value": suggested_host}
            ): selector.TextSelector(),
            prob.Required(
                "port",
                default=1883,
                description={"suggested_value": suggested_port},
            ): prob.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=65535,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
            prob.Optional(
                "username", description={"suggested_value": suggested_user}
            ): selector.TextSelector(),
            prob.Optional(
                "password", description={"suggested_value": suggested_pass}
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD
                )
            ),
        }

        return self.async_show_form(
            step_id="mqtt_config",
            data_schema=vol_schema(data_schema),
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
                        data_schema=vol_schema(
                            {
                                prob.Required(
                                    "device"
                                ): selector.DeviceSelector()
                            }
                        ),
                        errors={"device": "invalid_device"},
                        last_step=False,
                    )

                device_entry = dev_reg.async_get(device_id)

                if not device_entry:
                    return self.async_show_form(
                        step_id="zigbee_device",
                        data_schema=vol_schema(
                            {
                                prob.Required(
                                    "device"
                                ): selector.DeviceSelector()
                            }
                        ),
                        errors={"device": "device_not_found"},
                        last_step=False,
                    )

                ieee = _extract_ieee_from_device(device_entry)

                if not ieee:
                    return self.async_show_form(
                        step_id="zigbee_device",
                        data_schema=vol_schema(
                            {
                                prob.Required(
                                    "device"
                                ): selector.DeviceSelector()
                            }
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
                dev_entry
                for dev_entry in dev_reg.async_get_devices(identifiers=DOMAIN)
                if "ramses_esp32c6"
                in (dev_reg.async_get(dev_entry).model or "").lower()
            ]

            if len(matches) == 0:
                return self.async_show_form(
                    step_id="zigbee_device",
                    data_schema=vol_schema(
                        {
                            prob.Required(
                                "retry", default=False
                            ): selector.BooleanSelector()
                        }
                    ),
                    errors={"base": "no_ramses_device_found"},
                    last_step=False,
                )

            if len(matches) == 1:
                candidate = matches[0]
                ieee = _extract_ieee_from_device(dev_reg.async_get(candidate))

                if not ieee:
                    return self.async_show_form(
                        step_id="zigbee_device",
                        data_schema=vol_schema(
                            {
                                prob.Required(
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
                    dev_reg.async_get(candidate).id,
                    zigbee_url,
                )
                self.options[SZ_SERIAL_PORT][SZ_PORT_NAME] = zigbee_url
                return await self.async_step_configure_serial_port()

            # Multiple matches: present a selector for the user to choose.
            options = [
                selector.SelectOptionDict(
                    value=dev_reg.async_get(dev_entry).id,
                    label=dev_reg.async_get(dev_entry).name
                    or dev_reg.async_get(dev_entry).name_by_user
                    or dev_reg.async_get(dev_entry).id,
                )
                for dev_entry in matches
            ]
            return self.async_show_form(
                step_id="zigbee_device",
                data_schema=vol_schema(
                    {
                        prob.Required("device"): selector.SelectSelector(
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
            except prob.Invalid as err:
                errors[SZ_SERIAL_PORT] = "invalid_port_config"
                description_placeholders["error_detail"] = err.msg

            if not errors:
                if SZ_PORT_NAME in user_input:
                    config[SZ_PORT_NAME] = user_input[SZ_PORT_NAME]
                else:
                    # Debug: Check what we have in options
                    _LOGGER.debug(
                        "DEBUG: self.options[SZ_SERIAL_PORT] = %s",
                        redact_url(str(self.options[SZ_SERIAL_PORT])),
                    )
                    port_name = self.options[SZ_SERIAL_PORT][SZ_PORT_NAME]
                    _LOGGER.debug(
                        "DEBUG: Retrieved port_name = %s",
                        redact_url(port_name),
                    )
                    if port_name is None:
                        _LOGGER.error("ERROR: port_name is None!")
                        errors[SZ_PORT_NAME] = "port_name_required"
                    else:
                        config[SZ_PORT_NAME] = port_name

                if not errors:
                    port_name = config.get(SZ_PORT_NAME)
                    conn_err = await self._async_validate_port_connection(
                        str(port_name or "")
                    )
                    if conn_err:
                        errors["base"] = conn_err

                if not errors:
                    _log_config = dict(config)
                    if isinstance(_log_config.get(SZ_PORT_NAME), str):
                        _log_config[SZ_PORT_NAME] = redact_url(
                            _log_config[SZ_PORT_NAME]
                        )
                    _LOGGER.debug("DEBUG: Final config = %s", _log_config)
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

        data_schema: dict[prob.Marker, Any] = {}
        if self._manual_serial_port:
            _suggested_port = suggested_values.get(SZ_PORT_NAME)
            data_schema |= {
                prob.Required(
                    SZ_PORT_NAME,
                    description={
                        "suggested_value": redact_url(_suggested_port)
                        if isinstance(_suggested_port, str)
                        else _suggested_port
                    },
                ): selector.TextSelector(),
            }
        data_schema |= {
            prob.Optional(
                SZ_SERIAL_PORT,
                description={
                    "suggested_value": suggested_values.get(SZ_SERIAL_PORT)
                },
            ): selector.ObjectSelector()
        }

        return self.async_show_form(
            step_id="configure_serial_port",
            data_schema=vol_schema(data_schema),
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
                vol_schema(
                    SCH_GATEWAY_DICT | SCH_ENGINE_DICT,
                    extra=prob.PREVENT_EXTRA,
                )(gateway_config)
            except (prob.Invalid, _REAL_VOL.Invalid) as err:
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
                        schema = deepcopy(self.options.get(CONF_SCHEMA, {}))
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
            prob.Required(
                CONF_SCAN_INTERVAL,
                default=60,
                description={
                    "suggested_value": suggested_values.get(
                        CONF_SCAN_INTERVAL, 60
                    )
                },
            ): prob.All(
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
            prob.Required(
                CONF_GATEWAY_TIMEOUT,
                default=10,
                description={
                    "suggested_value": suggested_values.get(
                        CONF_GATEWAY_TIMEOUT, 10
                    )
                },
            ): prob.All(
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
            prob.Optional(
                CONF_RAMSES_RF,
                description={
                    "suggested_value": suggested_values.get(CONF_RAMSES_RF)
                },
            ): selector.ObjectSelector(),
        }

        # If using MQTT, expose the HGI ID field and Topic
        if self.options.get(CONF_MQTT_USE_HA):
            data_schema[
                prob.Optional(
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
                prob.Optional(
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
            data_schema=vol_schema(data_schema),
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
            except prob.Invalid as err:
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
                            # No _owner → backfill, EXCEPT for 18: HGI
                            # discovery candidates (issue 1119).  Those
                            # must stay ownerless until the user
                            # explicitly accepts them via the config
                            # flow, otherwise they'd be silently
                            # promoted to accepted pool members.
                            if (
                                k.startswith(HGI_PREFIX)
                                and v.get("_class", "").upper() == "HGI"
                            ):
                                continue
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
            prob.Optional(
                CONF_SCHEMA,
                description={
                    "suggested_value": suggested_values.get(CONF_SCHEMA)
                },
            ): selector.ObjectSelector(),
            prob.Required(
                "owner_name",
                default=suggested_values.get("owner_name", "me"),
                description={
                    "label": "System owner name (tags devices; foreign "
                    "go to block_list)",
                },
            ): selector.TextSelector(),
            prob.Optional(
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
            data_schema=vol_schema(
                # cv.deprecated(
                #     "sqlite_index", raise_if_present=False
                # ),  # Deprecated Q3 2026
                data_schema,
                extra=prob.ALLOW_EXTRA,
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
            prob.Optional(
                CONF_SEND_PACKET,
                default=False,
                description={
                    "suggested_value": suggested_values.get(CONF_SEND_PACKET)
                },
            ): selector.BooleanSelector(),
            prob.Optional(
                CONF_MESSAGE_EVENTS,
                description={
                    "suggested_value": suggested_values.get(
                        CONF_MESSAGE_EVENTS
                    )
                },
            ): selector.TextSelector(),
            prob.Optional(
                CONF_PASSIVE_SCAN,
                default=True,
                description={
                    "suggested_value": suggested_values.get(CONF_PASSIVE_SCAN)
                },
            ): selector.BooleanSelector(),
            prob.Optional(
                CONF_AUTO_NOTIFY,
                default=True,
                description={
                    "suggested_value": suggested_values.get(CONF_AUTO_NOTIFY)
                },
            ): selector.BooleanSelector(),
            prob.Optional(
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
            prob.Optional(
                CONF_GATEWAY_OFFLINE_NOTIFY,
                default=True,
                description={
                    "suggested_value": suggested_values.get(
                        CONF_GATEWAY_OFFLINE_NOTIFY, True
                    )
                },
            ): selector.BooleanSelector(),
        }

        return self.async_show_form(
            step_id="advanced_features",
            data_schema=vol_schema(data_schema),
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
            prob.Optional(
                SZ_PACKET_LOG_PATH,
                default="/config/ramses_rf_logs/",
                description={
                    "suggested_value": suggested_values.get(
                        SZ_PACKET_LOG_PATH, "/config/ramses_rf_logs/"
                    )
                },
            ): selector.TextSelector(),
            prob.Optional(
                SZ_PACKET_LOG_PREFIX,
                default="packet_log",
                description={
                    "suggested_value": suggested_values.get(
                        SZ_PACKET_LOG_PREFIX, "packet_log"
                    )
                },
            ): selector.TextSelector(),
            prob.Optional(
                SZ_PACKET_LOG_RETENTION_DAYS,
                default=7,
                description={
                    "suggested_value": suggested_values.get(
                        SZ_PACKET_LOG_RETENTION_DAYS, 7
                    )
                },
            ): prob.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="days",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                prob.Coerce(int),
            ),
            prob.Optional(
                SZ_ROTATE_BYTES,
                description={
                    "suggested_value": suggested_values.get(SZ_ROTATE_BYTES)
                },
            ): prob.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="bytes",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                prob.Coerce(int),
            ),
            prob.Optional(
                SZ_BUFFER_CAPACITY,
                default=0,
                description={
                    "suggested_value": suggested_values.get(
                        SZ_BUFFER_CAPACITY, 0
                    )
                },
            ): prob.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="lines",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                prob.Coerce(int),
            ),
            prob.Optional(
                SZ_FLUSH_INTERVAL,
                default=60.0,
                description={
                    "suggested_value": suggested_values.get(
                        SZ_FLUSH_INTERVAL, 60.0
                    )
                },
            ): prob.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        step=0.1,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                prob.Coerce(float),
            ),
            prob.Optional(
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
            data_schema=vol_schema(
                # cv.deprecated(
                #     "file_name", raise_if_present=False
                # ),  # Deprecated Q3 2026
                # cv.deprecated(
                #     "rotate_backups", raise_if_present=False
                # ),    # Deprecated Q3 2026
                data_schema,
                extra=prob.ALLOW_EXTRA,
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
        # async_migrate_entry v2→v3).  This import flow only runs once
        # (when no config entry exists yet), so it is the only chance to
        # transfer known_list traits from YAML into the config entry
        # schema.  After this, the YAML known_list is redundant —
        # async_setup warns and backs it up (issue 1055).
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
                "manage_pool",
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

        Clears the coordinator's ``_suppress_reload`` flag so the
        update listener (triggered by ``async_create_entry``) actually
        reloads the integration.  Without this, a race with
        ``sync_learned_topology`` (which sets ``_suppress_reload`` when
        persisting schema/comments) can suppress the reload that the
        config flow expects — leaving the running coordinator with
        stale transport config (e.g. MQTT pool bridge not restarted
        after a non-primary HGI switches from USB to MQTT).

        :return: The generated config flow result.
        """
        # Clear _suppress_reload so the update listener reloads.
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is not None and hasattr(
            coordinator, "_suppress_reload"
        ):
            coordinator._suppress_reload = 0.0  # noqa: SLF001

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

    async def async_step_manage_pool(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the HGI pool (issue 1119, 1171).

        Shows all pool members including the primary.  Any member can
        be removed — removing the primary auto-promotes another
        accepted HGI.  If no other HGI exists, removal is blocked.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        self.get_options()  # not available during init
        errors: dict[str, str] = {}

        # Sentinel value for "no new port selected"
        NO_ADD = "__none__"

        if user_input is not None:
            # Save the current additional ports (removals applied)
            # Filter out the __none__ sentinel (shown when no ports exist)
            additional: list[str] = [
                p
                for p in user_input.get(CONF_ADDITIONAL_PORTS, [])
                if p != "__none__"
            ]
            # Schema pool members that the user wants to keep (checked)
            keep_schema_members: list[str] = user_input.get(
                "schema_pool_members", []
            )
            # Discovery candidates that the user wants to accept
            accept_candidates: list[str] = user_input.get(
                "accept_discovery_candidates", []
            )
            add_choice = user_input.get("add_new_port", NO_ADD)
            # Wait-online timeout (seconds) for MQTT pool bridge
            wait_timeout = user_input.get(CONF_WAIT_ONLINE_TIMEOUT)

            # Validate: no duplicates, primary port not in additional
            primary = self.options.get(SZ_SERIAL_PORT, {}).get(SZ_PORT_NAME)
            if primary and primary in additional:
                errors["base"] = "pool_duplicate_primary"
            else:
                # Determine the primary HGI ID
                primary_hgi_id_input: str | None = None
                if isinstance(primary, str) and (
                    primary.startswith("mqtt://")
                    or primary == "mqtt_ha"
                    or self.options.get(CONF_MQTT_USE_HA)
                ):
                    primary_hgi_id_input = self.options.get(CONF_MQTT_HGI_ID)
                    if not primary_hgi_id_input and isinstance(primary, str):
                        m = re.search(rf"({HGI_ID_PATTERN})", primary)
                        if m:
                            primary_hgi_id_input = m.group(1)

                # Process schema pool member removals — unchecking a
                # schema pool member preserves _owner and marks it with
                # _removed_from_pool so it can be re-added easily.
                # The primary HGI can also be removed — if it's removed
                # and another accepted HGI exists, auto-promote that one.
                schema_dict = deepcopy(self.options.get(CONF_SCHEMA, {}))
                if isinstance(schema_dict, dict):
                    root_owner = schema_dict.get(SZ_OWNER, "me")
                    # First pass: figure out what would be removed and
                    # what would remain — without modifying yet.
                    to_demote: list[str] = []
                    remaining_hgis: list[str] = []
                    for dev_id, entry in schema_dict.items():
                        if (
                            dev_id.startswith(HGI_PREFIX)
                            and isinstance(entry, dict)
                            and entry.get("_class", "").upper() == "HGI"
                            and entry.get(SZ_TR_OWNER) == root_owner
                            and not entry.get("_disabled")
                        ):
                            if dev_id not in keep_schema_members:
                                to_demote.append(dev_id)
                            else:
                                remaining_hgis.append(dev_id)

                    is_mqtt_primary = isinstance(primary, str) and (
                        primary.startswith("mqtt://")
                        or primary == "mqtt_ha"
                        or self.options.get(CONF_MQTT_USE_HA)
                    )

                    # If all owned HGIs are being removed, require
                    # confirmation regardless of transport type.
                    # Don't demote yet — show the error first so the
                    # user can retry without losing the members.
                    if to_demote and not remaining_hgis:
                        if not user_input.get("confirm_clear_last"):
                            errors["base"] = "pool_confirm_clear_last"
                            # Don't demote — fall through to form
                        else:
                            # User confirmed — demote all and clear
                            # the primary port.  Mark HGIs as
                            # explicitly removed so the coordinator
                            # doesn't auto-re-add them via discovery.
                            for dev_id in to_demote:
                                entry = schema_dict.get(dev_id, {})
                                if isinstance(entry, dict):
                                    entry["_removed_from_pool"] = True
                                    schema_dict[dev_id] = entry
                            self.options[CONF_SCHEMA] = schema_dict
                            self.options[SZ_SERIAL_PORT] = {}
                            self.options.pop(CONF_MQTT_HGI_ID, None)
                            self.options.pop(CONF_MQTT_USE_HA, None)
                    elif to_demote:
                        # Normal removal (some HGIs remain) — demote
                        # now and auto-promote if needed.
                        for dev_id in to_demote:
                            entry = schema_dict.get(dev_id, {})
                            if isinstance(entry, dict):
                                entry["_removed_from_pool"] = True
                                schema_dict[dev_id] = entry
                        self.options[CONF_SCHEMA] = schema_dict

                        # Auto-promote if the old primary was removed
                        if remaining_hgis and is_mqtt_primary:
                            current_hgi_id = self.options.get(CONF_MQTT_HGI_ID)
                            if current_hgi_id not in remaining_hgis:
                                new_primary = sorted(remaining_hgis)[0]
                                self.options[CONF_MQTT_HGI_ID] = new_primary
                                if isinstance(
                                    primary, str
                                ) and primary.startswith("mqtt://"):
                                    from .coordinator import RamsesCoordinator

                                    new_url = RamsesCoordinator._build_explicit_mqtt_url(
                                        primary, new_primary
                                    )
                                    if new_url:
                                        self.options[SZ_SERIAL_PORT][
                                            SZ_PORT_NAME
                                        ] = new_url

                # Handle add choices
                # Accept discovery candidates — set _owner on selected
                # unowned HGIs to promote them to pool members.
                if accept_candidates:
                    if not isinstance(schema_dict, dict):
                        schema_dict = deepcopy(
                            self.options.get(CONF_SCHEMA, {})
                        )
                    root_owner = schema_dict.get(SZ_OWNER, "me")
                    for dev_id in accept_candidates:
                        entry = schema_dict.get(dev_id, {})
                        if isinstance(entry, dict):
                            entry[SZ_TR_OWNER] = root_owner
                            entry.pop("_removed_from_pool", None)
                            schema_dict[dev_id] = entry
                    self.options[CONF_SCHEMA] = schema_dict
                    _LOGGER.info(
                        "Accepted %d discovery candidate(s) as pool "
                        "members: %s",
                        len(accept_candidates),
                        accept_candidates,
                    )

                CONF_MQTT_HA_ID = "__mqtt_ha_id__"
                CONF_MQTT_FULL_URL = "__mqtt_full_url__"
                CONF_SERIAL_PORT = "__serial_port__"

                # Phase 2: serial pool children are now supported.
                # MQTT pool children are callback-driven via the
                # HA-native RamsesMqttPoolBridge (no paho inside HA,
                # issue 1119).  Both serial and MQTT can be mixed.
                # Zigbee remains gated until Phase 3.

                if add_choice == CONF_MQTT_HA_ID:
                    # HA MQTT device ID — just enter 18:NNNNNN
                    self.options[CONF_ADDITIONAL_PORTS] = additional
                    if wait_timeout is not None:
                        self.options[CONF_WAIT_ONLINE_TIMEOUT] = float(
                            wait_timeout
                        )
                    self._pool_add_in_progress = True
                    return await self.async_step_manage_pool_mqtt()
                elif add_choice == CONF_MQTT_FULL_URL:
                    # HA MQTT HGI with an optional topic prefix
                    self.options[CONF_ADDITIONAL_PORTS] = additional
                    if wait_timeout is not None:
                        self.options[CONF_WAIT_ONLINE_TIMEOUT] = float(
                            wait_timeout
                        )
                    self._pool_add_in_progress = True
                    return await self.async_step_manage_pool_mqtt_url()
                elif add_choice == CONF_SERIAL_PORT:
                    # Serial/USB port — select from available ports
                    self.options[CONF_ADDITIONAL_PORTS] = additional
                    if wait_timeout is not None:
                        self.options[CONF_WAIT_ONLINE_TIMEOUT] = float(
                            wait_timeout
                        )
                    return await self.async_step_manage_pool_serial()
                elif add_choice and add_choice.startswith("__readd__"):
                    # Re-add a previously removed HGI
                    readd_id = add_choice[len("__readd__") :]
                    schema_dict = deepcopy(self.options.get(CONF_SCHEMA, {}))
                    if readd_id in schema_dict and isinstance(
                        schema_dict[readd_id], dict
                    ):
                        root_owner = schema_dict.get(SZ_OWNER) or "me"
                        schema_dict[SZ_OWNER] = root_owner
                        schema_dict[readd_id][SZ_TR_OWNER] = root_owner
                        schema_dict[readd_id].pop("_removed_from_pool", None)
                        self.options[CONF_SCHEMA] = schema_dict
                    # If no primary is set, this HGI becomes the primary
                    if not primary and readd_id.startswith(HGI_PREFIX):
                        self.options[CONF_MQTT_HGI_ID] = readd_id
                        self.options.setdefault(CONF_MQTT_USE_HA, True)
                        self.options[SZ_SERIAL_PORT] = {
                            SZ_PORT_NAME: "mqtt_ha"
                        }
                    self.options[CONF_ADDITIONAL_PORTS] = additional
                    if wait_timeout is not None:
                        self.options[CONF_WAIT_ONLINE_TIMEOUT] = float(
                            wait_timeout
                        )
                    return self._async_save()
                elif add_choice and add_choice.startswith("__accept__"):
                    # Accept a discovery candidate (unowned HGI) as a
                    # pool member — set _owner so it becomes active.
                    accept_id = add_choice[len("__accept__") :]
                    schema_dict = deepcopy(self.options.get(CONF_SCHEMA, {}))
                    if accept_id in schema_dict and isinstance(
                        schema_dict[accept_id], dict
                    ):
                        root_owner = schema_dict.get(SZ_OWNER) or "me"
                        schema_dict[SZ_OWNER] = root_owner
                        schema_dict[accept_id][SZ_TR_OWNER] = root_owner
                        self.options[CONF_SCHEMA] = schema_dict
                    self.options[CONF_ADDITIONAL_PORTS] = additional
                    if wait_timeout is not None:
                        self.options[CONF_WAIT_ONLINE_TIMEOUT] = float(
                            wait_timeout
                        )
                    return self._async_save()
                elif not errors:
                    # No new port and no errors — just save removals
                    # and _preferred_type updates.
                    # Capture old _preferred_type values BEFORE
                    # modifying the schema, so we can detect changes.
                    old_schema_prefs: dict[str, str] = {}
                    _old_schema = self.options.get(CONF_SCHEMA, {})
                    if isinstance(_old_schema, dict):
                        for _k, _v in _old_schema.items():
                            if isinstance(_v, dict):
                                old_schema_prefs[_k] = str(
                                    _v.get("_preferred_type", "")
                                )
                    schema_dict = deepcopy(self.options.get(CONF_SCHEMA, {}))
                    if isinstance(schema_dict, dict):
                        for key, val in user_input.items():
                            if key.startswith("_preferred_type_"):
                                dev_id = key[len("_preferred_type_") :]
                                if dev_id in schema_dict and isinstance(
                                    schema_dict[dev_id], dict
                                ):
                                    if val:
                                        schema_dict[dev_id][
                                            "_preferred_type"
                                        ] = val
                                        # Also update _comment to
                                        # include the selected transport
                                        # so it shows "(detected)" next
                                        # time.
                                        comment = str(
                                            schema_dict[dev_id].get(
                                                "_comment", ""
                                            )
                                        ).lower()
                                        if val not in comment:
                                            parts = []
                                            if (
                                                "usb" in comment
                                                or val == "usb"
                                            ):
                                                parts.append("usb")
                                            if (
                                                "mqtt" in comment
                                                or val == "mqtt"
                                            ):
                                                parts.append("mqtt")
                                            if (
                                                "zigbee" in comment
                                                or val == "zigbee"
                                            ):
                                                parts.append("zigbee")
                                            schema_dict[dev_id]["_comment"] = (
                                                build_hgi_comment(parts)
                                            )
                        self.options[CONF_SCHEMA] = schema_dict
                    self.options[CONF_ADDITIONAL_PORTS] = additional
                    if wait_timeout is not None:
                        self.options[CONF_WAIT_ONLINE_TIMEOUT] = float(
                            wait_timeout
                        )

                    # Detect if the user switched the primary HGI's
                    # _preferred_type to a different transport.  If so,
                    # switch the primary transport to match (issue 1171).
                    # The MQTT bridge uses the HA MQTT integration's
                    # broker — if it's not configured, show an error.
                    _primary_port = self.options.get(SZ_SERIAL_PORT, {}).get(
                        SZ_PORT_NAME, ""
                    )
                    # Get the runtime port-to-HGI mapping from the
                    # coordinator so we can identify the actual primary
                    # HGI (the one on the primary serial port), not just
                    # the first HGI with _owner: me in schema order
                    # (issue 1185).
                    _runtime_map: dict[str, str] = {}
                    _coord = getattr(self.config_entry, "runtime_data", None)
                    if _coord is not None and hasattr(
                        _coord, "serial_port_hgi_map"
                    ):
                        _runtime_map = _coord.serial_port_hgi_map
                    _primary_hgi_id: str | None = None
                    if isinstance(_primary_port, str):
                        if _primary_port.startswith("mqtt://"):
                            m = re.search(
                                rf"({HGI_ID_PATTERN})", _primary_port
                            )
                            if m:
                                _primary_hgi_id = m.group(1)
                        elif _primary_port == "mqtt_ha":
                            _primary_hgi_id = self.options.get(
                                CONF_MQTT_HGI_ID
                            )
                    # For serial primary, find the primary HGI.
                    # Use the runtime port mapping first (the HGI
                    # physically on the primary serial port), then
                    # fall back to schema iteration (issue 1185).
                    if not _primary_hgi_id:
                        if (
                            isinstance(_primary_port, str)
                            and _primary_port in _runtime_map
                        ):
                            _primary_hgi_id = _runtime_map[_primary_port]
                    if not _primary_hgi_id and isinstance(schema_dict, dict):
                        root_owner = schema_dict.get(SZ_OWNER, "me")
                        for dev_id, entry in schema_dict.items():
                            if (
                                dev_id.startswith(HGI_PREFIX)
                                and isinstance(entry, dict)
                                and entry.get("_class", "").upper() == "HGI"
                                and entry.get(SZ_TR_OWNER) == root_owner
                            ):
                                _primary_hgi_id = dev_id
                                break
                    if _primary_hgi_id and isinstance(schema_dict, dict):
                        new_pref = schema_dict.get(_primary_hgi_id, {}).get(
                            "_preferred_type", ""
                        )
                        # Only trigger a transport switch when the
                        # _preferred_type actually CHANGED — not when
                        # the form just re-submitted the same default.
                        # Treat "" and "mqtt" as equivalent (both mean
                        # MQTT) since the selector uses "mqtt" as the
                        # value for the MQTT option.
                        old_pref_norm = (
                            old_schema_prefs.get(_primary_hgi_id, "") or "mqtt"
                        )
                        new_pref_norm = new_pref or "mqtt"
                        if new_pref_norm == old_pref_norm:
                            # No change — don't switch transport
                            pass
                        else:
                            current_primary = self.options.get(
                                SZ_SERIAL_PORT, {}
                            ).get(SZ_PORT_NAME, "")
                            is_current_serial = isinstance(
                                current_primary, str
                            ) and (
                                current_primary.startswith("/dev/")
                                or current_primary.startswith("socket://")
                                or current_primary.startswith("rfc2217://")
                            )
                            is_current_mqtt = isinstance(
                                current_primary, str
                            ) and (
                                current_primary.startswith("mqtt://")
                                or current_primary == "mqtt_ha"
                            )

                            if new_pref == "mqtt" and is_current_serial:
                                # Switch primary to MQTT — redirect to
                                # the MQTT URL step so the user can
                                # enter the full broker URL (reuses
                                # existing UI).  Pre-fill with the HA
                                # MQTT integration's broker if available.
                                _LOGGER.info(
                                    "Pool: switching primary HGI %s "
                                    "from serial to MQTT — redirecting "
                                    "to MQTT URL entry",
                                    _primary_hgi_id,
                                )
                                self._switching_primary_to_mqtt = (
                                    _primary_hgi_id
                                )
                                return await self.async_step_manage_pool_mqtt_url()
                            elif new_pref == "usb" and is_current_mqtt:
                                # Switch primary to serial — redirect
                                # to the serial port selection step.
                                _LOGGER.info(
                                    "Pool: switching primary HGI %s "
                                    "from MQTT to serial — redirecting "
                                    "to serial port selection",
                                    _primary_hgi_id,
                                )
                                self._switching_primary_to_serial = (
                                    _primary_hgi_id
                                )
                                return (
                                    await self.async_step_manage_pool_serial()
                                )

                    # Handle non-primary HGI transport switches.
                    # When a non-primary HGI switches from USB to MQTT,
                    # remove its serial port from additional_ports and
                    # redirect to the MQTT URL step so the user can
                    # confirm the broker URL (pre-filled from the HA MQTT
                    # integration).  When switching from MQTT to USB, we
                    # can't auto-add the serial port here (we don't know
                    # which port to use) — the user must add it via "Add
                    # new port > Serial/USB port".
                    if not errors and isinstance(schema_dict, dict):
                        _runtime_map_np: dict[str, str] = {}
                        _coord_np = getattr(
                            self.config_entry, "runtime_data", None
                        )
                        if _coord_np is not None and hasattr(
                            _coord_np, "serial_port_hgi_map"
                        ):
                            _runtime_map_np = _coord_np.serial_port_hgi_map
                        # Reverse map: HGI ID -> port name
                        _runtime_hgi_port_np: dict[str, str] = {
                            v: k for k, v in _runtime_map_np.items()
                        }
                        _additional_ports = self.options.get(
                            CONF_ADDITIONAL_PORTS, []
                        )
                        _additional_changed = False
                        _switching_secondary_to_mqtt: str | None = None
                        for key, val in user_input.items():
                            if not key.startswith("_preferred_type_"):
                                continue
                            dev_id = key[len("_preferred_type_") :]
                            if dev_id == _primary_hgi_id:
                                continue  # primary handled above
                            old_np = old_schema_prefs.get(dev_id, "") or "mqtt"
                            new_np = (val or "mqtt").lower()
                            if old_np == new_np:
                                continue
                            if new_np == "mqtt" and old_np == "usb":
                                # Switching non-primary from USB to MQTT.
                                # Remove its serial port from
                                # additional_ports if we know it from
                                # the runtime map.
                                port = _runtime_hgi_port_np.get(dev_id)
                                if port and port in _additional_ports:
                                    _additional_ports = [
                                        p
                                        for p in _additional_ports
                                        if p != port
                                    ]
                                    _additional_changed = True
                                    _LOGGER.info(
                                        "Pool: removed serial port %s "
                                        "from additional_ports (HGI %s "
                                        "switched to MQTT)",
                                        port,
                                        dev_id,
                                    )
                                # Remember the HGI ID so we can
                                # redirect to the broker URL step.
                                _switching_secondary_to_mqtt = dev_id
                        if _additional_changed:
                            self.options[CONF_ADDITIONAL_PORTS] = (
                                _additional_ports
                            )
                        if _switching_secondary_to_mqtt:
                            # Redirect to the MQTT URL step so the
                            # user can confirm the broker URL
                            # (pre-filled from the HA MQTT integration).
                            self._switching_secondary_to_mqtt = (
                                _switching_secondary_to_mqtt
                            )
                            _LOGGER.info(
                                "Pool: switching non-primary HGI %s "
                                "from serial to MQTT — redirecting "
                                "to MQTT URL entry",
                                _switching_secondary_to_mqtt,
                            )
                            return await self.async_step_manage_pool_mqtt_url()

                    if not errors:
                        return self._async_save()
                # If errors is non-empty, fall through to show the form
                # again with the error message.

        # Build the current state for display
        primary_port = self.options.get(SZ_SERIAL_PORT, {}).get(
            SZ_PORT_NAME, ""
        )
        current_additional = self.options.get(CONF_ADDITIONAL_PORTS, [])

        # Get the runtime port-to-HGI mapping from the coordinator.
        # The transport discovers which HGI is on which serial port
        # at startup (via !I or _PUZZ probe).  This lets us show the
        # actual port assignment in the pool member labels (issue 1185).
        _runtime_port_hgi_map: dict[str, str] = {}
        coord = getattr(self.config_entry, "runtime_data", None)
        if coord is not None and hasattr(coord, "serial_port_hgi_map"):
            _runtime_port_hgi_map = coord.serial_port_hgi_map
        # Reverse map: HGI ID -> port name
        _runtime_hgi_port_map: dict[str, str] = {
            v: k for k, v in _runtime_port_hgi_map.items()
        }

        # Schema-derived pool members (HGIs with _owner: me and _class:
        # HGI) — these are active pool members managed via the schema.
        # Show them in the form with a checkbox for each; unchecking
        # marks them _removed_from_pool while preserving _owner.
        # The primary HGI is also listed (marked as "primary") so the
        # user can see the full pool composition.
        #
        # Phase 2: hybrid pools (serial + MQTT) are now supported.
        # Show all accepted HGIs as schema pool members regardless of
        # whether the primary is serial or MQTT.  Each HGI's
        # _preferred_type determines its transport in the hybrid pool.
        # (Phase 1 restricted this to MQTT-only primaries.)
        schema = self.options.get(CONF_SCHEMA, {})
        if not isinstance(schema, dict):
            schema = {}
        root_owner = schema.get(SZ_OWNER, "me")
        schema_pool_members: list[str] = []
        # Discovery candidates: HGIs in the schema without _owner.
        # These are shown in the pool management step so the user can
        # accept them (set _owner) directly, without waiting for the
        # discovery flow.
        discovery_candidates: list[str] = []
        for dev_id, entry in schema.items():
            if (
                dev_id.startswith(HGI_PREFIX)
                and dev_id != DEFAULT_HGI_ID
                and isinstance(entry, dict)
                and entry.get("_class", "").upper() == "HGI"
                and not entry.get("_disabled")
                and not entry.get("_removed_from_pool")
            ):
                if entry.get(SZ_TR_OWNER) == root_owner:
                    schema_pool_members.append(dev_id)
                elif not entry.get(SZ_TR_OWNER):
                    discovery_candidates.append(dev_id)

        # Determine the primary HGI ID (from the MQTT URL or CONF_MQTT_HGI_ID)
        # so we can label it in the pool list.
        primary_hgi_id: str | None = None
        if isinstance(primary_port, str) and (
            primary_port.startswith("mqtt://")
            or primary_port == "mqtt_ha"
            or (
                self.options.get(CONF_MQTT_USE_HA)
                and not (
                    primary_port.startswith("/dev/")
                    or primary_port.startswith("socket://")
                    or primary_port.startswith("rfc2217://")
                )
            )
        ):
            primary_hgi_id = self.options.get(CONF_MQTT_HGI_ID)
            if not primary_hgi_id:
                m = re.search(rf"({HGI_ID_PATTERN})", primary_port)
                if m:
                    primary_hgi_id = m.group(1)
        # For serial primary, find the primary HGI from the schema
        # (the accepted HGI with _owner and _class: HGI).
        # When the runtime port-to-HGI mapping is available, use it
        # directly — the primary HGI is the one on the primary serial
        # port (issue 1185).  Otherwise, prefer the HGI with
        # _preferred_type: usb, falling back to the first accepted HGI.
        if not primary_hgi_id and isinstance(schema, dict):
            # Runtime mapping: the HGI on the primary serial port.
            if (
                isinstance(primary_port, str)
                and primary_port in _runtime_port_hgi_map
            ):
                primary_hgi_id = _runtime_port_hgi_map[primary_port]
            # First pass: look for _preferred_type: usb.
            if not primary_hgi_id:
                for dev_id, entry in schema.items():
                    if (
                        dev_id.startswith(HGI_PREFIX)
                        and dev_id != DEFAULT_HGI_ID
                        and isinstance(entry, dict)
                        and entry.get("_class", "").upper() == "HGI"
                        and entry.get(SZ_TR_OWNER) == root_owner
                        and not entry.get("_disabled")
                        and str(entry.get("_preferred_type", "")).lower()
                        == "usb"
                    ):
                        primary_hgi_id = dev_id
                        break
            # Fall back: first accepted HGI.
            if not primary_hgi_id:
                for dev_id, entry in schema.items():
                    if (
                        dev_id.startswith(HGI_PREFIX)
                        and dev_id != DEFAULT_HGI_ID
                        and isinstance(entry, dict)
                        and entry.get("_class", "").upper() == "HGI"
                        and entry.get(SZ_TR_OWNER) == root_owner
                        and not entry.get("_disabled")
                    ):
                        primary_hgi_id = dev_id
                        break

        # Auto-accept the primary HGI for serial/USB primaries.
        # If the primary port is serial and the HGI ID is known (from
        # the runtime port mapping), it is a valid HGI on the primary
        # port — we can safely assume it is owned by the root owner.
        # This ensures the primary HGI appears as a pool member in the
        # pool menu instead of showing "0 pool members" when there is
        # clearly an active HGI on the primary port.
        if (
            primary_hgi_id
            and isinstance(primary_port, str)
            and (
                primary_port.startswith("/dev/")
                or primary_port.startswith("socket://")
                or primary_port.startswith("rfc2217://")
            )
            and isinstance(schema, dict)
            and primary_hgi_id in schema
            and isinstance(schema[primary_hgi_id], dict)
            and not schema[primary_hgi_id].get(SZ_TR_OWNER)
        ):
            schema[primary_hgi_id][SZ_TR_OWNER] = root_owner
            # Persist the change so the coordinator sees it as accepted.
            new_options = dict(self.options)
            new_options[CONF_SCHEMA] = deepcopy(schema)
            self.options = new_options
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=new_options
            )
            _LOGGER.info(
                "Auto-accepted primary HGI %s as pool member "
                "(serial primary on %s)",
                primary_hgi_id,
                primary_port,
            )
            # Rebuild the pool members / discovery candidates lists
            # now that the primary has _owner set.
            schema_pool_members = []
            discovery_candidates = []
            for dev_id, entry in schema.items():
                if (
                    dev_id.startswith(HGI_PREFIX)
                    and dev_id != DEFAULT_HGI_ID
                    and isinstance(entry, dict)
                    and entry.get("_class", "").upper() == "HGI"
                    and not entry.get("_disabled")
                    and not entry.get("_removed_from_pool")
                ):
                    if entry.get(SZ_TR_OWNER) == root_owner:
                        schema_pool_members.append(dev_id)
                    elif not entry.get(SZ_TR_OWNER):
                        discovery_candidates.append(dev_id)

        # Build a label for each pool member showing its broker info.
        # For the primary HGI: the primary_port URL.
        # For additional HGIs: the explicit per-HGI MQTT URL.
        # Mask credentials in MQTT URLs for display (uses the shared
        # redact_url helper from ramses_tx.transport.helpers).
        _mask_mqtt_url = redact_url

        def _pool_member_label(dev_id: str) -> str:
            """Build a human-readable label with transport type and broker info.

            Shows the transport type (USB serial vs MQTT callback) so
            the user can distinguish pool members in a hybrid pool
            (Phase 2, issue 1119).

            When the runtime port-to-HGI mapping is available (from the
            coordinator), the actual serial port is shown for each USB
            HGI (issue 1185).
            """
            # Check if this HGI is on a known serial port (runtime).
            runtime_port = _runtime_hgi_port_map.get(dev_id)

            if dev_id == primary_hgi_id:
                # For the primary, ensure the topic is shown even if
                # the URL has no path (e.g. mqtt://broker:1883).
                display_url = primary_port
                if isinstance(display_url, str) and display_url.startswith(
                    "mqtt://"
                ):
                    from urllib.parse import urlparse, urlunparse

                    try:
                        parsed = urlparse(display_url)
                        path = (parsed.path or "").rstrip("/")
                        if not path:
                            # No topic in URL — show the default
                            path = "/RAMSES/GATEWAY"
                            display_url = urlunparse(
                                parsed._replace(path=path)
                            )
                    except (ValueError, AttributeError):
                        pass
                elif isinstance(display_url, str) and (
                    display_url.startswith("/dev/")
                    or display_url.startswith("socket://")
                    or display_url.startswith("rfc2217://")
                ):
                    # Serial primary.  If the runtime mapping knows
                    # which port this HGI is actually on, show it.
                    # Otherwise show the configured primary port.
                    port = runtime_port or display_url
                    return f"HGI: {dev_id} (primary, USB, {port})"
                elif display_url == "mqtt_ha" or (
                    self.options.get(CONF_MQTT_USE_HA)
                    and not (
                        isinstance(display_url, str)
                        and (
                            display_url.startswith("/dev/")
                            or display_url.startswith("socket://")
                            or display_url.startswith("rfc2217://")
                        )
                    )
                ):
                    # HA MQTT integration path — the topic prefix is
                    # stored separately in CONF_MQTT_TOPIC, not in the
                    # port URL.  Show it so the user can see which
                    # topic the wildcard subscriptions use.
                    topic = self.options.get(
                        CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC
                    )
                    display_url = f"mqtt_ha, topic: {topic}"
                    return f"HGI: {dev_id} (primary, MQTT, {_mask_mqtt_url(display_url)})"
                return (
                    f"HGI: {dev_id} (primary, {_mask_mqtt_url(display_url)})"
                )
            # Non-primary HGI — check _preferred_type and runtime
            # port to determine the transport label, regardless of
            # whether the primary is serial or MQTT (issue 1185).
            schema_entry = schema.get(dev_id, {})
            preferred = ""
            comment = ""
            if isinstance(schema_entry, dict):
                preferred = str(
                    schema_entry.get("_preferred_type", "")
                ).lower()
                comment = str(schema_entry.get("_comment", ""))
            # Strip the HGI_COMMENT_WARNING suffix — it's meant for
            # the schema editor, not the pool management UI.
            comment = comment.replace(HGI_COMMENT_WARNING, "").strip()
            detected_str = f" [{comment}]" if comment else ""

            # If the runtime mapping shows this HGI is on a serial
            # port, show it as USB with the actual port (issue 1185).
            if runtime_port:
                return f"HGI: {dev_id} (USB, {runtime_port}){detected_str}"

            # Check _preferred_type first — the schema is authoritative
            # for transport preference (issue 1185).
            if preferred == "usb":
                return f"HGI: {dev_id} (USB){detected_str}"
            if preferred == "zigbee":
                return f"HGI: {dev_id} (Zigbee){detected_str}"

            # No explicit _preferred_type — fall back to primary
            # transport context.
            # If the primary is MQTT, show the MQTT URL for this HGI.
            if isinstance(primary_port, str) and primary_port.startswith(
                "mqtt://"
            ):
                from .coordinator import RamsesCoordinator

                explicit = RamsesCoordinator._build_explicit_mqtt_url(
                    primary_port, dev_id
                )
                if explicit:
                    return f"HGI: {dev_id} (MQTT, {_mask_mqtt_url(explicit)})"
            # If the primary is serial, schema HGIs are MQTT
            # callback-driven pool members (Phase 2 hybrid pool).
            if isinstance(primary_port, str) and (
                primary_port.startswith("/dev/")
                or primary_port.startswith("socket://")
                or primary_port.startswith("rfc2217://")
            ):
                # Default: MQTT pool member.
                if self.options.get(CONF_MQTT_USE_HA):
                    topic = self.options.get(
                        CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC
                    )
                    return (
                        f"HGI: {dev_id} (MQTT, topic: {topic}){detected_str}"
                    )
                return f"HGI: {dev_id} (MQTT){detected_str}"
            return f"HGI: {dev_id} (schema, _owner: {root_owner})"

        # Debug: log the runtime port-to-HGI mapping and pool member labels
        # so we can verify the pool management display (issue 1185).
        _LOGGER.debug(
            "ManagePool: runtime_port_hgi_map=%s, primary_hgi_id=%s, "
            "primary_port=%s",
            _runtime_port_hgi_map,
            primary_hgi_id,
            redact_url(primary_port),
        )

        # Build options for the "current ports" multi-select (for removal)
        # Show each current additional port with a friendly label
        current_options: list[selector.SelectOptionDict] = []
        for port in current_additional:
            if port.startswith("mqtt://"):
                label = f"MQTT: {_mask_mqtt_url(port)}"
            elif port.startswith("zigbee://"):
                label = f"Zigbee: {port}"
            else:
                label = port
            current_options.append(
                selector.SelectOptionDict(value=port, label=label)
            )

        # Build options for the "add new port" dropdown.
        # Phase 2: serial and MQTT HGIs are supported as pool children.
        # Serial children are transport-driven (serialx); MQTT children
        # are callback-driven via the HA-native RamsesMqttPoolBridge
        # (no paho inside HA — issue 1119).
        # Zigbee remains gated until Phase 3 (PR 6).
        # TODO: re-enable zigbee when Phase 3 (PR 6) lands.
        CONF_MQTT_HA_ID = "__mqtt_ha_id__"
        CONF_MQTT_FULL_URL = "__mqtt_full_url__"
        CONF_SERIAL_PORT = "__serial_port__"
        add_options: list[selector.SelectOptionDict] = [
            selector.SelectOptionDict(value=NO_ADD, label="(nothing to add)"),
            selector.SelectOptionDict(
                value=CONF_MQTT_HA_ID, label="HA MQTT device ID..."
            ),
            selector.SelectOptionDict(
                value=CONF_MQTT_FULL_URL,
                label="HA MQTT HGI with topic prefix...",
            ),
            selector.SelectOptionDict(
                value=CONF_SERIAL_PORT, label="Serial/USB port..."
            ),
        ]
        # List removed HGIs so the user can re-add them directly.
        # Phase 2: re-add works for both serial and MQTT primaries
        # (hybrid pool support).
        if isinstance(schema, dict):
            for dev_id, entry in schema.items():
                if (
                    dev_id.startswith(HGI_PREFIX)
                    and isinstance(entry, dict)
                    and entry.get("_class", "").upper() == "HGI"
                    and entry.get("_removed_from_pool")
                ):
                    add_options.append(
                        selector.SelectOptionDict(
                            value=f"__readd__{dev_id}",
                            label=f"Re-add HGI: {dev_id}",
                        )
                    )
                # Also list discovery candidates (unowned HGIs) so the
                # user can accept them directly from the pool menu
                # without going through the discovery flow.
                if (
                    dev_id.startswith(HGI_PREFIX)
                    and dev_id != DEFAULT_HGI_ID
                    and isinstance(entry, dict)
                    and entry.get("_class", "").upper() == "HGI"
                    and not entry.get(SZ_TR_OWNER)
                    and not entry.get("_removed_from_pool")
                    and not entry.get("_disabled")
                ):
                    add_options.append(
                        selector.SelectOptionDict(
                            value=f"__accept__{dev_id}",
                            label=f"Accept discovery candidate: {dev_id}",
                        )
                    )

        # Build the data schema — if there are current ports, show them
        # in a multi-select for removal; always show the "add new" dropdown
        if current_options:
            ports_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=current_options,
                    mode=selector.SelectSelectorMode.LIST,
                    multiple=True,
                )
            )
        else:
            ports_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value="__none__", label="(no additional ports)"
                        )
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                    multiple=True,
                )
            )

        # Schema pool members selector (multi-select for removal).
        # All accepted HGIs are listed, including the primary.
        # Removing the primary auto-promotes another HGI (or blocks
        # if it's the last one).
        removable_pool_hgis = sorted(set(schema_pool_members))
        if removable_pool_hgis:
            _pool_labels: list[str] = []
            for _dev_id in removable_pool_hgis:
                _label = _pool_member_label(_dev_id)
                _pool_labels.append(f"{_dev_id} -> {_label}")
            _LOGGER.debug("ManagePool: pool member labels: %s", _pool_labels)
            schema_pool_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=dev_id,
                            label=_pool_member_label(dev_id),
                        )
                        for dev_id in removable_pool_hgis
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                    multiple=True,
                )
            )
        else:
            schema_pool_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value="__none__",
                            label="(no schema pool members)",
                        )
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                    multiple=True,
                )
            )

        # Build per-HGI _preferred_type selectors so the user can
        # mark which transport each HGI uses (Phase 2 hybrid pool).
        # Show for both serial and MQTT primary so the user can
        # switch the primary transport via _preferred_type.
        # Always show all transport options, but mark which ones were
        # detected (from the _comment field, e.g. "Supports: usb, mqtt").
        preferred_type_selectors: dict[str, Any] = {}
        if (
            isinstance(primary_port, str)
            and (
                primary_port.startswith("/dev/")
                or primary_port.startswith("socket://")
                or primary_port.startswith("rfc2217://")
                or primary_port.startswith("mqtt://")
                or primary_port == "mqtt_ha"
            )
            and isinstance(schema, dict)
        ):
            for dev_id in removable_pool_hgis:
                entry = schema.get(dev_id, {})
                current_pref = ""
                detected_types: list[str] = []
                if isinstance(entry, dict):
                    current_pref = str(
                        entry.get("_preferred_type", "")
                    ).lower()
                    # Parse _comment to find detected transports.
                    comment = str(entry.get("_comment", "")).lower()
                    if "usb" in comment:
                        detected_types.append("usb")
                    if "mqtt" in comment:
                        detected_types.append("mqtt")
                    if "zigbee" in comment:
                        detected_types.append("zigbee")
                # Always show all options, but mark detected ones.
                pref_options: list[selector.SelectOptionDict] = []
                mqtt_label = "MQTT"
                if "mqtt" in detected_types:
                    mqtt_label = "MQTT (detected)"
                pref_options.append(
                    selector.SelectOptionDict(value="mqtt", label=mqtt_label)
                )
                usb_label = "USB (serial)"
                if "usb" in detected_types:
                    usb_label = "USB (serial, detected)"
                pref_options.append(
                    selector.SelectOptionDict(value="usb", label=usb_label)
                )
                zb_label = "Zigbee (not yet supported)"
                if "zigbee" in detected_types:
                    zb_label = "Zigbee (detected, not yet supported)"
                pref_options.append(
                    selector.SelectOptionDict(value="zigbee", label=zb_label)
                )
                if not pref_options:
                    continue  # no options to show
                # Default to the detected transport type when no
                # _preferred_type is set yet.  If only USB is detected,
                # default to "usb".  If only MQTT, default to "" (MQTT).
                # If both, default to "" (MQTT) unless USB is the
                # primary transport (serial primary → prefer USB).
                if not current_pref:
                    if (
                        "usb" in detected_types
                        and "mqtt" not in detected_types
                    ):
                        current_pref = "usb"
                    elif "usb" in detected_types and primary_port.startswith(
                        "/dev/"
                    ):
                        current_pref = "usb"
                preferred_type_selectors[dev_id] = (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=pref_options,
                            mode=selector.SelectSelectorMode.LIST,
                            multiple=False,
                        )
                    ),
                    current_pref or "mqtt",
                )

        data_schema: dict[str, Any] = {
            prob.Optional(
                "schema_pool_members",
                default=removable_pool_hgis,
            ): schema_pool_selector,
        }
        # Add discovery candidates selector (multi-select to accept).
        # Unowned HGIs in the schema are discovery candidates — show
        # them so the user can accept them directly from the pool
        # management step (issue 1119).
        if discovery_candidates:
            candidate_options = [
                selector.SelectOptionDict(
                    value=dev_id,
                    label=_pool_member_label(dev_id),
                )
                for dev_id in sorted(discovery_candidates)
            ]
            data_schema[
                prob.Optional(
                    "accept_discovery_candidates",
                    default=[],
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=candidate_options,
                    mode=selector.SelectSelectorMode.LIST,
                    multiple=True,
                )
            )
        # Add per-HGI _preferred_type selectors.
        for dev_id, (sel, default_val) in preferred_type_selectors.items():
            data_schema[
                prob.Optional(
                    f"_preferred_type_{dev_id}",
                    default=default_val,
                )
            ] = sel
        data_schema.update(
            {
                prob.Optional(
                    CONF_ADDITIONAL_PORTS,
                    default=current_additional,
                ): ports_selector,
                prob.Optional(
                    "add_new_port",
                    default=NO_ADD,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=add_options,
                        mode=selector.SelectSelectorMode.LIST,
                        multiple=False,
                    )
                ),
                prob.Optional(
                    CONF_WAIT_ONLINE_TIMEOUT,
                    default=self.options.get(
                        CONF_WAIT_ONLINE_TIMEOUT,
                        DEFAULT_WAIT_ONLINE_TIMEOUT,
                    ),
                ): prob.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=300,
                            step=1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    prob.Coerce(float),
                ),
                # Confirmation checkbox for removing the last HGI.
                # Only relevant when the user unchecks all pool members.
                prob.Optional(
                    "confirm_clear_last",
                    default=False,
                ): selector.BooleanSelector(),
            }
        )

        # Mask credentials and ensure topic is shown in the primary
        # port for display
        display_primary_port = (
            str(primary_port) if primary_port else "(not set)"
        )
        if isinstance(primary_port, str) and primary_port.startswith(
            "mqtt://"
        ):
            from urllib.parse import urlparse, urlunparse

            try:
                parsed = urlparse(primary_port)
                path = (parsed.path or "").rstrip("/")
                if not path:
                    # No topic in URL — show the default
                    parsed = parsed._replace(path="/RAMSES/GATEWAY")
                if parsed.username:
                    netloc = f"***:***@{parsed.hostname}"
                    if parsed.port:
                        netloc += f":{parsed.port}"
                    parsed = parsed._replace(netloc=netloc)
                display_primary_port = urlunparse(parsed)
            except (ValueError, AttributeError):
                pass

        return self.async_show_form(
            step_id="manage_pool",
            data_schema=vol_schema(data_schema),
            errors=errors,
            description_placeholders={
                "primary_port": display_primary_port,
                "current_count": str(len(current_additional)),
                "schema_pool_members": (
                    ", ".join(schema_pool_members)
                    if schema_pool_members
                    else "(none)"
                ),
                "discovery_candidates": (
                    ", ".join(sorted(discovery_candidates))
                    if discovery_candidates
                    else "(none)"
                ),
            },
            last_step=False,
        )

    async def async_step_manage_pool_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a new MQTT HGI pool member.

        Creates a schema HGI entry with ``_owner`` set to the root
        owner so the coordinator includes it in the pool on reload.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        if not getattr(self, "_pool_add_in_progress", False):
            self.get_options()
        errors: dict[str, str] = {}

        if user_input is not None:
            hgi_id = (user_input.get("hgi_id") or "").strip()

            if not hgi_id:
                errors["base"] = "hgi_id_required"
            elif not _HGI_ID_RE.match(hgi_id):
                errors["base"] = "hgi_id_invalid"
            else:
                # Phase 1: MQTT pool children share the HA MQTT
                # integration's broker/topic — no separate host/port
                # or credentials are needed, and no paho client is
                # created inside HA (issue 1119).
                # Create/update schema HGI entry with _owner = root_owner
                # so the coordinator's _extract_pool_hgis_from_schema()
                # includes it as an accepted pool member.
                schema_dict = deepcopy(self.options.get(CONF_SCHEMA, {}))
                root_owner = schema_dict.get(SZ_OWNER) or "me"
                schema_dict[SZ_OWNER] = root_owner
                if hgi_id not in schema_dict or not isinstance(
                    schema_dict.get(hgi_id), dict
                ):
                    schema_dict[hgi_id] = {}
                schema_dict[hgi_id]["_class"] = "HGI"
                schema_dict[hgi_id][SZ_TR_OWNER] = root_owner
                schema_dict[hgi_id]["_preferred_type"] = "mqtt"
                # Clear the _removed_from_pool trait if it was set
                # (user is explicitly re-adding this HGI)
                schema_dict[hgi_id].pop("_removed_from_pool", None)
                self.options[CONF_SCHEMA] = schema_dict
                _LOGGER.info(
                    "Added MQTT pool HGI %s (schema entry with _owner=%s)",
                    hgi_id,
                    root_owner,
                )
                return self._async_save()

        # Phase 1: only the HGI ID is needed — the broker and topic
        # come from the HA MQTT integration.  No host/port/credentials
        # are stored, and no paho client is created (issue 1119).
        data_schema = {
            prob.Required("hgi_id", default=""): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.TEXT,
                )
            ),
        }

        return self.async_show_form(
            step_id="manage_pool_mqtt",
            data_schema=vol_schema(data_schema),
            errors=errors,
            description_placeholders={},
            last_step=False,
        )

    async def async_step_manage_pool_mqtt_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add an MQTT HGI via HGI ID + optional topic prefix.

        HA's MQTT integration owns the broker connection — this form
        asks only for the HGI device ID and an optional topic prefix
        override (issue 1119).  No broker/port/credentials are
        collected because the HA MQTT integration provides the
        broker.

        When invoked from the pool menu's primary switch (serial →
        MQTT), the HGI is set as the primary MQTT HGI
        (``CONF_MQTT_HGI_ID``) instead of being added to additional
        ports.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        # Don't reload options if we're in a switching flow —
        # the pool form already updated self.options with the
        # new _preferred_type before redirecting here.
        if (
            not hasattr(self, "_switching_primary_to_mqtt")
            and not hasattr(self, "_switching_secondary_to_mqtt")
            and not getattr(self, "_pool_add_in_progress", False)
        ):
            self.get_options()
        errors: dict[str, str] = {}

        switching_primary = getattr(self, "_switching_primary_to_mqtt", None)
        switching_secondary = getattr(
            self, "_switching_secondary_to_mqtt", None
        )
        switching_hgi_id = switching_primary or switching_secondary
        default_hgi_id = switching_hgi_id or ""
        default_topic = self.options.get(CONF_MQTT_TOPIC, "RAMSES/GATEWAY")

        if user_input is not None:
            hgi_id = (user_input.get("hgi_id") or "").strip().upper()
            topic_prefix = (user_input.get("topic_prefix") or "").strip()
            if not hgi_id:
                errors["base"] = "mqtt_hgi_id_required"
            elif not _HGI_ID_RE.match(hgi_id):
                errors["base"] = "mqtt_hgi_id_invalid"
            elif switching_hgi_id and hgi_id != switching_hgi_id:
                # Issue 5: reject an HGI ID different from the
                # switching target — a typo would leave the original
                # HGI with _preferred_type: mqtt, remove its USB port,
                # and add a different accepted HGI to the schema.
                errors["base"] = "mqtt_hgi_id_mismatch"
            else:
                schema_dict = deepcopy(self.options.get(CONF_SCHEMA, {}))
                root_owner = schema_dict.get(SZ_OWNER) or "me"
                schema_dict[SZ_OWNER] = root_owner
                if hgi_id not in schema_dict or not isinstance(
                    schema_dict.get(hgi_id), dict
                ):
                    schema_dict[hgi_id] = {}
                schema_dict[hgi_id]["_class"] = "HGI"
                schema_dict[hgi_id][SZ_TR_OWNER] = root_owner
                schema_dict[hgi_id].pop("_removed_from_pool", None)
                self.options[CONF_SCHEMA] = schema_dict

                # Store the optional topic prefix override if it
                # differs from the configured topic.  An empty
                # submission clears any previous override (issue 1171).
                if topic_prefix and topic_prefix != default_topic:
                    self.options[CONF_MQTT_TOPIC] = topic_prefix
                elif not topic_prefix and CONF_MQTT_TOPIC in self.options:
                    self.options.pop(CONF_MQTT_TOPIC, None)

                if switching_primary:
                    # Set as primary MQTT HGI (serial → MQTT switch).
                    self.options[CONF_MQTT_HGI_ID] = hgi_id
                    self.options[CONF_MQTT_USE_HA] = True
                    # Remove the serial port name — the primary is
                    # now MQTT, not serial.
                    self.options.pop(SZ_SERIAL_PORT, None)
                    _LOGGER.info(
                        "Switched primary HGI %s to MQTT (HA broker)",
                        hgi_id,
                    )
                else:
                    # Non-primary: mark as MQTT-preferred in schema.
                    # The coordinator's _schema_mqtt_preferred logic
                    # picks this up and includes it in the MQTT bridge.
                    if switching_secondary:
                        schema_dict[hgi_id]["_preferred_type"] = "mqtt"
                        self.options[CONF_SCHEMA] = schema_dict
                        _LOGGER.info(
                            "Switched non-primary HGI %s to MQTT (HA broker)",
                            hgi_id,
                        )
                    else:
                        # Direct add: mark as MQTT-preferred so the
                        # coordinator recognises it as an MQTT pool
                        # member (issue 1171).
                        schema_dict[hgi_id]["_preferred_type"] = "mqtt"
                        self.options[CONF_SCHEMA] = schema_dict
                        _LOGGER.info(
                            "Added MQTT pool HGI %s (HA broker)",
                            hgi_id,
                        )
                # Clear the switching flags if set
                if hasattr(self, "_switching_primary_to_mqtt"):
                    del self._switching_primary_to_mqtt
                if hasattr(self, "_switching_secondary_to_mqtt"):
                    del self._switching_secondary_to_mqtt
                return self._async_save()

        data_schema = {
            prob.Required(
                "hgi_id", default=default_hgi_id
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.TEXT,
                )
            ),
            prob.Optional(
                "topic_prefix", default=default_topic
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.TEXT,
                )
            ),
        }

        # Show a different description when switching the primary
        if switching_primary:
            desc = (
                "Switching primary HGI {hgi_id} from serial to MQTT.\n"
                "HA's MQTT integration provides the broker — enter "
                "only the HGI device ID.  The optional topic prefix "
                "defaults to your configured MQTT topic."
            ).replace("{hgi_id}", switching_primary)
        elif switching_secondary:
            desc = (
                "Switching non-primary HGI {hgi_id} from serial to MQTT.\n"
                "HA's MQTT integration provides the broker — enter "
                "only the HGI device ID.  The optional topic prefix "
                "defaults to your configured MQTT topic."
            ).replace("{hgi_id}", switching_secondary)
        else:
            desc = (
                "Add an MQTT HGI to the pool.  HA's MQTT integration "
                "provides the broker — enter only the HGI device ID "
                "(e.g. 18:123456).  The optional topic prefix defaults "
                "to your configured MQTT topic."
            )

        return self.async_show_form(
            step_id="manage_pool_mqtt_url",
            data_schema=vol_schema(data_schema),
            errors=errors,
            description_placeholders={"message": desc},
            last_step=False,
        )

    async def async_step_manage_pool_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a serial/USB port as an additional pool member.

        Phase 2: serial children are transport-driven (serialx) and
        fully send-capable after identity is established.  The port
        is added to CONF_ADDITIONAL_PORTS.

        When invoked from the pool menu's primary switch (MQTT →
        USB), the selected port becomes the primary serial_port
        instead of being added to additional ports.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        # Don't reload options if we're in a switching flow —
        # the pool form already updated self.options with the
        # new _preferred_type before redirecting here.
        switching_primary = hasattr(self, "_switching_primary_to_serial")
        if not switching_primary:
            self.get_options()
        errors: dict[str, str] = {}

        if user_input is not None:
            port = (user_input.get("serial_port") or "").strip()
            if port == "__back__":
                # User chose to go back — return to pool management.
                return await self.async_step_manage_pool()
            if not port or port == "__none__":
                errors["base"] = "serial_port_required"
            else:
                # Check if we're switching the primary from MQTT to serial
                current_primary = self.options.get(SZ_SERIAL_PORT, {}).get(
                    SZ_PORT_NAME, ""
                )
                is_current_mqtt = isinstance(current_primary, str) and (
                    current_primary.startswith("mqtt://")
                    or current_primary == "mqtt_ha"
                )
                if is_current_mqtt:
                    # Switch primary to serial
                    self.options[SZ_SERIAL_PORT] = {SZ_PORT_NAME: port}
                    self.options.pop(CONF_MQTT_USE_HA, None)
                    self.options.pop(CONF_MQTT_HGI_ID, None)
                    _LOGGER.info(
                        "Switched primary HGI from MQTT to serial: %s",
                        port,
                    )
                else:
                    # Add the serial port to additional ports
                    additional = self.options.get(CONF_ADDITIONAL_PORTS, [])
                    if port not in additional:
                        additional.append(port)
                    self.options[CONF_ADDITIONAL_PORTS] = additional
                    _LOGGER.info(
                        "Added serial pool child: %s",
                        port,
                    )
                # Clear switching flags if set
                if hasattr(self, "_switching_primary_to_serial"):
                    del self._switching_primary_to_serial
                if hasattr(self, "_switching_primary_to_mqtt"):
                    del self._switching_primary_to_mqtt
                return self._async_save()

        # Build list of available serial ports using HA's USB port
        # scanner (returns /dev/serial/by-id/ paths with friendly
        # names).  Falls back to serialx.list_serial_ports() if the
        # HA USB scanner is unavailable.
        usb_ports_map: dict[str, str] = {}
        try:
            usb_ports_map = await async_get_usb_ports(self.hass)
            available_ports = list(usb_ports_map.keys())
        except Exception:  # noqa: BLE001
            try:
                from serialx import list_serial_ports

                available_ports = list_serial_ports()
            except Exception:  # noqa: BLE001
                available_ports = []

        # Filter out the primary port and already-added ports
        primary_port = self.options.get(SZ_SERIAL_PORT, {}).get(
            SZ_PORT_NAME, ""
        )
        current_additional = self.options.get(CONF_ADDITIONAL_PORTS, [])
        excluded = {primary_port, *current_additional}

        port_options = [
            selector.SelectOptionDict(
                value=port,
                label=usb_ports_map.get(port, port),
            )
            for port in available_ports
            if port not in excluded
        ]

        if not port_options:
            port_options = [
                selector.SelectOptionDict(
                    value="__none__", label="(no available ports)"
                )
            ]
            # Add a go-back option so the user isn't stuck when
            # switching to serial with no ports available.
            if hasattr(self, "_switching_primary_to_serial"):
                port_options.append(
                    selector.SelectOptionDict(
                        value="__back__", label="(go back to pool management)"
                    )
                )

        data_schema = {
            prob.Required("serial_port"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=port_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }

        return self.async_show_form(
            step_id="manage_pool_serial",
            data_schema=vol_schema(data_schema),
            errors=errors,
            description_placeholders={},
            last_step=False,
        )

    async def async_step_manage_pool_zigbee(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a Zigbee device as an additional port for the pool.

        :param user_input: Dict containing user-provided input data.
        :return: The generated config flow result.
        """
        self.get_options()
        errors: dict[str, str] = {}

        try:
            dev_reg = dr.async_get(self.hass)

            if user_input is not None and "device" in user_input:
                device_id = user_input.get("device")
                if isinstance(device_id, str):
                    device_entry = dev_reg.async_get(device_id)
                    if device_entry:
                        ieee = _extract_ieee_from_device(device_entry)
                        if ieee:
                            zigbee_url = (
                                f"zigbee://{ieee}"
                                "/0xfc00/0x0000/10/0xfc01/0x0000/10"
                            )
                            additional = self.options.get(
                                CONF_ADDITIONAL_PORTS, []
                            )
                            if zigbee_url not in additional:
                                additional = additional + [zigbee_url]
                            self.options[CONF_ADDITIONAL_PORTS] = additional
                            _LOGGER.info(
                                "Added Zigbee additional port: %s",
                                zigbee_url,
                            )
                            return self._async_save()
                        errors["device"] = "no_ieee_identifier"
                    else:
                        errors["device"] = "device_not_found"
                else:
                    errors["device"] = "invalid_device"

            data_schema = {
                prob.Required("device"): selector.DeviceSelector(
                    selector.DeviceSelectorConfig(
                        model="ramses_esp32c6",
                    )
                ),
            }

            return self.async_show_form(
                step_id="manage_pool_zigbee",
                data_schema=vol_schema(data_schema),
                errors=errors,
                description_placeholders={},
                last_step=False,
            )
        except Exception as err:
            _LOGGER.error(
                "EXCEPTION in async_step_manage_pool_zigbee: %s",
                err,
                exc_info=True,
            )
            errors["base"] = "zigbee_error"
            return self.async_show_form(
                step_id="manage_pool_zigbee",
                data_schema=vol_schema(
                    {prob.Required("device"): selector.DeviceSelector()}
                ),
                errors=errors,
                last_step=False,
            )

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
            # Distinguish between "not set up" (transport failed) and
            # "passive scan disabled" (coordinator up but no discovery
            # manager).  The misleading "Passive device scan is not
            # enabled" message confused users when the real issue was
            # a failed transport (issue 1171).
            if coordinator is None:
                message = (
                    "The Ramses RF integration is not running. "
                    "Check the serial port / MQTT broker connection "
                    "and reload the integration."
                )
            elif not getattr(coordinator, "client", None):
                message = (
                    "The Ramses RF transport failed to start. "
                    "Check the serial port / MQTT broker connection "
                    "and reload the integration."
                )
            else:
                message = "Passive device scan is not enabled."
            return self.async_show_form(
                step_id="review_discovered",
                description_placeholders={"message": message},
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
                schema_device_ids, foreign_device_ids, config_schema_for_sync
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
        _LOGGER.debug(
            "review_discovered: get_devices(NEW) returned %d devices: %s",
            len(new_devices),
            [d.device.device_id for d in new_devices],
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
            config_schema = deepcopy(self.options.get(CONF_SCHEMA, {}))
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
                    # Also dismiss missing_class so check_missing_class
                    # doesn't immediately re-flag this device on the next
                    # checkpoint (issue 1136).  The NEW-section skip writes
                    # schema _skipped, but check_missing_class only consults
                    # metadata.missing_class_dismissed — set both so neither
                    # review path re-surfaces the device.
                    skip_meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if skip_meta:
                        skip_meta.missing_class = None
                        skip_meta.missing_class_dismissed = True
                    else:
                        # Edge case: device has no metadata yet (e.g. it
                        # was added to the schema externally).  Create
                        # metadata with the dismissal pre-set so
                        # check_missing_class won't re-flag it (issue 1136).
                        from .discovery import DeviceMetadata

                        coordinator.discovery_manager._metadata[device_id] = (
                            DeviceMetadata(missing_class_dismissed=True)
                        )
                    changed = True
                    continue
                if action == SZ_ACCEPT:
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
                            dev_entry.pop(SZ_TR_COMMENT, None)
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
                            # Phase 2: save _preferred_type for HGI
                            # devices and update _comment.
                            if device_id.startswith(HGI_PREFIX):
                                pref_val = user_input.get(
                                    f"preferred_type_{device_id}", "mqtt"
                                )
                                if pref_val:
                                    dev_entry["_preferred_type"] = pref_val
                                # Update _comment to include selected
                                # transport.
                                comment = str(
                                    dev_entry.get("_comment", "")
                                ).lower()
                                sel = pref_val or "mqtt"
                                parts: list[str] = []
                                if "usb" in comment or sel == "usb":
                                    parts.append("usb")
                                if "mqtt" in comment or sel == "mqtt":
                                    parts.append("mqtt")
                                if "zigbee" in comment or sel == "zigbee":
                                    parts.append("zigbee")
                                dev_entry["_comment"] = build_hgi_comment(
                                    parts
                                )
                        # Clear any prior missing_class dismissal so that
                        # if the user later removes _class from the schema,
                        # check_missing_class can re-flag the device
                        # (issue 1136).
                        accept_meta = (
                            coordinator.discovery_manager._metadata.get(
                                device_id
                            )
                        )
                        if accept_meta:
                            accept_meta.missing_class_dismissed = False
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
                        # Clear _skipped — adding a class means the user
                        # has reviewed and confirmed the device.  Leaving
                        # _skipped would keep the device in a deferred
                        # state with no entities, even though the user
                        # just acknowledged it.  If a new msg code later
                        # suggests a different class, check_class_mismatches
                        # will catch it via the mismatch review path.
                        dev_entry.pop(SZ_TR_SKIPPED, None)
                        changed = True
                        _LOGGER.info(
                            "review_discovered: added _class=%s for %s "
                            "(was missing, discovery suggestion accepted, "
                            "_skipped cleared)",
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
                                ctl_zones = ctl_entry.get(SZ_ZONES)
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
            #
            # IMPORTANT: skip topology sync during this save.  Otherwise
            # sync_learned_topology's enriched write-back sets
            # _suppress_reload, which suppresses the reload from
            # _async_save().  Without that reload, the running gateway
            # keeps its stale (empty) known_list and blocks all packets
            # from the just-accepted devices — the gateway never learns
            # topology and zones end up without sensors (issue 1023).
            # The unload chain (_async_save_on_unload) handles topology
            # sync skipping and .storage persistence during the actual
            # reload; this pre-save just ensures discovery metadata is
            # flushed to .storage before the coordinator is torn down.
            if coordinator.discovery_manager:
                coordinator._skip_topology_sync = True  # noqa: SLF001
                try:
                    await coordinator.async_save_client_state()
                except Exception as err:
                    _LOGGER.warning(
                        "review_discovered: failed to persist discovery "
                        "state before reload: %s",
                        err,
                    )
                finally:
                    coordinator._skip_topology_sync = False  # noqa: SLF001

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
        # Get the current schema for reading _comment (transport
        # capability detection) on HGI entries.
        config_schema = self.options.get(CONF_SCHEMA, {})
        if not isinstance(config_schema, dict):
            config_schema = {}

        # Owner name field — sets the ROOT _owner in the schema.
        # This is the system-wide owner.  Per-device owner fields below
        # override it for individual devices.  If no root _owner was set
        # yet, this fills it in.
        existing_owner = self.options.get(CONF_SCHEMA, {}).get(SZ_OWNER, "")
        form_fields[
            prob.Required(
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
            prob.Required(
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
                prob.Required(
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
                prob.Optional(
                    f"owner_{device_id}",
                    description={
                        "label": f"Owner for {device_id} (overrides "
                        "root owner)"
                    },
                )
            ] = selector.TextSelector()

            # Phase 2: for HGI devices, add a _preferred_type selector
            # so the user can set the transport preference when accepting.
            if device_id.startswith(HGI_PREFIX):
                # Parse existing _comment for detected transports.
                dev_entry = config_schema.get(device_id, {})
                detected_types: list[str] = []
                if isinstance(dev_entry, dict):
                    comment = str(dev_entry.get("_comment", "")).lower()
                    if "usb" in comment:
                        detected_types.append("usb")
                    if "mqtt" in comment:
                        detected_types.append("mqtt")
                    if "zigbee" in comment:
                        detected_types.append("zigbee")
                # Build options — always show all, mark detected.
                pref_opts: list[selector.SelectOptionDict] = []
                mqtt_lbl = "MQTT"
                if "mqtt" in detected_types:
                    mqtt_lbl = "MQTT (detected)"
                pref_opts.append(
                    selector.SelectOptionDict(value="mqtt", label=mqtt_lbl)
                )
                usb_lbl = "USB (serial)"
                if "usb" in detected_types:
                    usb_lbl = "USB (serial, detected)"
                pref_opts.append(
                    selector.SelectOptionDict(value="usb", label=usb_lbl)
                )
                zb_lbl = "Zigbee (not yet supported)"
                if "zigbee" in detected_types:
                    zb_lbl = "Zigbee (detected, not yet supported)"
                pref_opts.append(
                    selector.SelectOptionDict(value="zigbee", label=zb_lbl)
                )
                # Default to the detected transport type when no
                # _preferred_type is set yet.  If only USB is detected,
                # default to "usb".  If only MQTT, default to "" (MQTT).
                # If both, default to "usb" when on serial primary.
                _current_pref = (
                    str(dev_entry.get("_preferred_type", "")).lower()
                    if isinstance(dev_entry, dict)
                    else ""
                )
                _review_default = _current_pref or "mqtt"
                if not _review_default:
                    _primary_port = self.options.get(SZ_SERIAL_PORT, {}).get(
                        SZ_PORT_NAME, ""
                    )
                    if (
                        "usb" in detected_types
                        and "mqtt" not in detected_types
                    ):
                        _review_default = "usb"
                    elif (
                        "usb" in detected_types
                        and isinstance(_primary_port, str)
                        and _primary_port.startswith("/dev/")
                    ):
                        _review_default = "usb"
                form_fields[
                    prob.Optional(
                        f"preferred_type_{device_id}",
                        default=_review_default,
                        description={
                            "label": f"Preferred transport for {device_id} "
                            "(HGI)"
                        },
                    )
                ] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=pref_opts,
                        mode=selector.SelectSelectorMode.LIST,
                        multiple=False,
                    )
                )

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
                prob.Required(
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
                prob.Optional(
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
                prob.Required(
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
                prob.Optional(
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
                prob.Required(
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
            data_schema=vol_schema(form_fields),
            description_placeholders={"message": summary},
            last_step=True,
        )

    async def async_step_review_device_health(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review devices with health issues.

        Shows:
        - Lost devices (ACCEPTED, not seen for >threshold) — keep or remove
        - Orphaned devices (in schema but not seen recently) — keep or remove
        - Weak signal devices (poor RSSI or stale) — dismiss or suppress

        The user can keep (dismiss the flag), suppress future warnings
        (for weak signal), or remove (full cleanup via remove_device
        service) each device individually.
        """
        self.get_options()

        coordinator = getattr(self.config_entry, "runtime_data", None)

        if not coordinator or not coordinator.discovery_manager:
            if coordinator is None:
                message = (
                    "The Ramses RF integration is not running. "
                    "Check the serial port / MQTT broker connection "
                    "and reload the integration."
                )
            elif not getattr(coordinator, "client", None):
                message = (
                    "The Ramses RF transport failed to start. "
                    "Check the serial port / MQTT broker connection "
                    "and reload the integration."
                )
            else:
                message = "Passive device scan is not enabled."
            return self.async_show_form(
                step_id="review_device_health",
                description_placeholders={"message": message},
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
            coordinator.discovery_manager.check_communication_quality(
                config_schema_check,
                devices=(coordinator._devices if coordinator.client else None),
            )

        orphaned_devices = coordinator.discovery_manager.get_orphaned_devices()
        lost_devices = coordinator.discovery_manager.get_lost_devices()
        weak_signal_devices = (
            coordinator.discovery_manager.get_weak_signal_devices()
        )

        # Deduplicate: a device with weak_signal is being heard (has
        # RSSI data), so it cannot be lost — exclude it from the lost
        # list.  Orphaned + weak can coexist (orphaned = not in schema,
        # weak = poor signal), so show both sections independently.
        weak_ids = {e.device.device_id for e in weak_signal_devices}
        lost_devices = [
            e for e in lost_devices if e.device.device_id not in weak_ids
        ]
        lost_ids = {e.device.device_id for e in lost_devices}
        orphaned_only = [
            e for e in orphaned_devices if e.device.device_id not in lost_ids
        ]
        weak_only = list(weak_signal_devices)

        if not orphaned_only and not lost_devices and not weak_only:
            if user_input is not None:
                return self._async_save()
            return self.async_show_form(
                step_id="review_device_health",
                description_placeholders={
                    "message": "No orphaned, lost, or weak-signal devices."
                },
                last_step=True,
            )

        if user_input is not None:
            # Process each device — "keep" clears the flag, "remove" calls
            # the remove_device service for full cleanup.
            config_schema = deepcopy(self.options.get(CONF_SCHEMA, {}))
            removed_any = False
            for entry in lost_devices:
                device_id = entry.device.device_id
                action = user_input.get(f"lost_{device_id}", "keep")
                if action == "remove":
                    try:
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
                    except ServiceValidationError as err:
                        _LOGGER.warning(
                            "review_device_health: cannot remove lost "
                            "device %s: %s",
                            device_id,
                            err,
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
                    try:
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
                    except ServiceValidationError as err:
                        _LOGGER.warning(
                            "review_device_health: cannot remove "
                            "orphaned device %s: %s",
                            device_id,
                            err,
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

            for entry in weak_only:
                device_id = entry.device.device_id
                action = user_input.get(f"weak_{device_id}", "keep")
                if action == "suppress":
                    # Suppress future weak-signal warnings for this device
                    # by setting _suppress_weak_signal in the schema.
                    meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if meta:
                        meta.weak_signal = None
                    dev_entry = config_schema.get(device_id)
                    if isinstance(dev_entry, dict):
                        dev_entry["_suppress_weak_signal"] = True
                        config_schema[device_id] = dev_entry
                    _LOGGER.info(
                        "review_device_health: suppressed weak-signal "
                        "warnings for %s",
                        device_id,
                    )
                elif action == "keep":
                    # Dismiss the flag — the device is known to be weak
                    # but the user doesn't want to suppress future
                    # warnings entirely.  weak_signal_dismissed prevents
                    # re-flagging until quality recovers and degrades again.
                    meta = coordinator.discovery_manager._metadata.get(
                        device_id
                    )
                    if meta:
                        meta.weak_signal = None
                        meta.weak_signal_dismissed = True

            # Save discovery metadata to .storage via coordinator's save
            # cycle (export_state is called in async_save_client_state)
            await coordinator.async_save_client_state()

            if removed_any:
                # remove_device service already updated the config entry,
                # so refresh self.options from the coordinator to avoid
                # overwriting with stale data, then save normally
                self.options = deepcopy(dict(coordinator.options))
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

        if weak_only:
            if lines:
                lines.append("\n")
            lines.append(
                f"**{len(weak_only)} weak signal device(s)** "
                "(poor RSSI or stale — check RF range/batteries):\n"
            )
            lines.append("| Device | Type | Issue |")
            lines.append("|--------|------|-------|")
            for entry in weak_only:
                d = entry.device
                note = entry.metadata.weak_signal or ""
                lines.append(
                    f"| `{d.device_id}` | {d.likely_type or '?'} | {note} |"
                )

        if not lines:
            lines.append("No orphaned, lost, or weak-signal devices.")
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
                prob.Required(
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
                prob.Required(
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

        for entry in weak_only:
            d = entry.device
            device_id = d.device_id
            note = entry.metadata.weak_signal or ""
            field_label = f"{device_id} | {d.likely_type or '?'} | {note}"
            form_fields[
                prob.Required(
                    f"weak_{device_id}",
                    default="keep",
                    description={"label": field_label},
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {
                            "value": "keep",
                            "label": "Dismiss (re-warn if it degrades again)",
                        },
                        {
                            "value": "suppress",
                            "label": "Suppress (no future warnings)",
                        },
                    ],
                )
            )

        return self.async_show_form(
            step_id="review_device_health",
            data_schema=vol_schema(form_fields),
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
                    stored_data[SZ_CLIENT_STATE].pop(SZ_SCHEMA, None)

                    def filter_schema_packets(
                        packets: dict[str, dict[str, Any] | str],
                    ) -> dict[str, dict[str, Any] | str]:
                        """Filter packets used for schema discovery.

                        :param packets: The cached packets.
                        :return: The filtered packets.
                        """
                        msg_code_filter = {
                            Code._0004,
                            Code._0005,
                            Code._000C,
                        }
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
            #
            # Only set CONF_FRESH_START for clear_schema — it wipes the
            # entire .storage (schema, packets, discovery).  For
            # clear_packets alone, the targeted edit-and-save above is
            # sufficient and preserves discovery metadata and schema
            # (issue 1056: clearing packets was also wiping discovered
            # items, forcing users to re-accept existing devices).
            if self.config_entry is not None and user_input["clear_schema"]:
                new_options = dict(self.config_entry.options)
                if user_input["clear_schema"]:
                    # Preserve foreign-owned device entries (_owner: not-me)
                    # when the schema is cleared.  These represent the user's
                    # explicit decision to decline a foreign device (e.g. a
                    # neighbour's HGI).  Without this, the device is
                    # re-discovered as NEW after the wipe, forcing the user
                    # to re-decline it (issue 1020).
                    from .coordinator import RamsesCoordinator

                    old_schema = new_options.get(CONF_SCHEMA, {})

                    # Clear retained MQTT LWT messages for known HGIs so
                    # they don't reappear as discovery candidates after the
                    # schema wipe.  The MQTT broker retains the last
                    # "online"/"offline" LWT message for each HGI, and the
                    # MqttPoolBridge re-discovers HGIs from retained
                    # "online" messages on reload.  Publishing an empty
                    # retained payload clears the retained message (MQTT
                    # spec: a zero-length payload with retain=True clears
                    # the retained message for that topic).
                    # Only do this for MQTT-primary configs (mqtt_use_ha
                    # or mqtt:// URL) where the broker is reachable.
                    is_mqtt_primary = bool(
                        new_options.get(CONF_MQTT_USE_HA)
                    ) or (
                        isinstance(
                            new_options.get(SZ_SERIAL_PORT, {}).get(
                                SZ_PORT_NAME
                            ),
                            str,
                        )
                        and new_options[SZ_SERIAL_PORT][
                            SZ_PORT_NAME
                        ].startswith("mqtt://")
                    )
                    if is_mqtt_primary:
                        topic_prefix = new_options.get(
                            CONF_MQTT_TOPIC, "RAMSES/GATEWAY"
                        )
                        hgi_ids_to_clear = [
                            dev_id
                            for dev_id, entry in old_schema.items()
                            if (
                                dev_id.startswith(HGI_PREFIX)
                                and isinstance(entry, dict)
                                and entry.get("_class", "").upper() == "HGI"
                            )
                        ]
                        if hgi_ids_to_clear:
                            try:
                                from homeassistant.components import (
                                    mqtt as mqtt_comp,
                                )

                                for hgi_id in hgi_ids_to_clear:
                                    topic = f"{topic_prefix}/{hgi_id}"
                                    await mqtt_comp.async_publish(
                                        self.hass,
                                        topic,
                                        "",
                                        0,
                                        True,  # retain=True, empty payload
                                    )
                                _LOGGER.info(
                                    "Clear cache: cleared %d retained "
                                    "LWT message(s) for HGIs: %s",
                                    len(hgi_ids_to_clear),
                                    sorted(hgi_ids_to_clear),
                                )
                            except Exception as err:
                                _LOGGER.warning(
                                    "Clear cache: failed to clear "
                                    "retained LWT messages: %s",
                                    str(err)[:200],
                                )

                    foreign_ids = (
                        RamsesCoordinator._extract_foreign_device_ids(
                            old_schema
                        )
                    )
                    if foreign_ids:
                        root_owner = old_schema.get(SZ_OWNER, "me")
                        preserved: dict[str, Any] = {SZ_OWNER: root_owner}
                        for dev_id in foreign_ids:
                            entry = old_schema.get(dev_id)
                            if isinstance(entry, dict):
                                preserved[dev_id] = {
                                    SZ_TR_OWNER: entry.get(
                                        SZ_TR_OWNER, "not-me"
                                    )
                                }
                        new_options[CONF_SCHEMA] = preserved
                        _LOGGER.info(
                            "Clear cache: preserved %d foreign-owned "
                            "device entry/entries across schema wipe: %s",
                            len(foreign_ids),
                            sorted(foreign_ids),
                        )
                    else:
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
            prob.Required(
                "clear_schema", default=False
            ): selector.BooleanSelector(),
            prob.Required(
                "clear_packets", default=False
            ): selector.BooleanSelector(),
            prob.Required(
                "clear_discovery", default=False
            ): selector.BooleanSelector(),
            # clear_known_list was removed in Phase 4 — known_list is now
            # derived from schema, so clearing the schema is sufficient.
        }

        return self.async_show_form(
            step_id="clear_cache",
            data_schema=vol_schema(data_schema),
        )

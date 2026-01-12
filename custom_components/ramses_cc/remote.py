"""Support for RAMSES HVAC RF remotes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.remote import (
    ENTITY_ID_FORMAT,
    RemoteEntity,
    RemoteEntityDescription,
    RemoteEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)

from ramses_rf.device.hvac import HvacRemote
from ramses_tx import DeviceIdT
from ramses_tx.command import Command
from ramses_tx.const import Priority

from . import RamsesEntity, RamsesEntityDescription
from .broker import RamsesBroker
from .const import ATTR_DEVICE_ID, DOMAIN
from .schemas import DEFAULT_DELAY_SECS, DEFAULT_NUM_REPEATS, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the remote platform.

    :param hass: The Home Assistant instance.
    :param entry: The config entry.
    :param async_add_entities: Callback to add entities.
    """
    broker: RamsesBroker = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: list[HvacRemote]) -> None:
        entities = [
            RamsesRemoteEntityDescription.ramses_cc_class(
                broker, device, RamsesRemoteEntityDescription()
            )
            for device in devices
        ]
        async_add_entities(entities)

    broker.async_register_platform(platform, add_devices)


class RamsesRemote(RamsesEntity, RemoteEntity):
    """Representation of a RAMSES RF remote."""

    _device: HvacRemote

    _attr_assumed_state: bool = True
    _attr_supported_features: int = (
        RemoteEntityFeature.LEARN_COMMAND | RemoteEntityFeature.DELETE_COMMAND
    )

    def __init__(
        self,
        broker: RamsesBroker,
        device: HvacRemote,
        entity_description: RamsesRemoteEntityDescription,
    ) -> None:
        """Initialize a HVAC remote.

        :param broker: The RamsesBroker instance.
        :param device: The backend device instance.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found %s", device.id)
        super().__init__(broker, device, entity_description)

        self.entity_id = ENTITY_ID_FORMAT.format(device.id)

        self._attr_is_on = True
        self._commands: dict[str, str] = broker._remotes.get(device.id, {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: A dictionary of state attributes.
        """
        return super().extra_state_attributes | {"commands": self._commands}

    async def async_delete_command(
        self,
        command: Iterable[str] | str,
        **kwargs: Any,
    ) -> None:
        """Delete commands from the database.

        :param command: The command(s) to delete.
        :param kwargs: Arbitrary keyword arguments.
        """
        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        # if len(command) != 1:
        #     raise TypeError("must be exactly one command to delete")

        assert not kwargs, kwargs  # TODO: remove me

        self._commands = {k: v for k, v in self._commands.items() if k not in command}

    async def async_learn_command(
        self,
        command: Iterable[str] | str,
        timeout: int = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        """Learn a command from a device (remote) and add to the database.

        :param command: The command(s) to learn.
        :param timeout: Timeout in seconds, defaults to DEFAULT_TIMEOUT.
        :param kwargs: Arbitrary keyword arguments.
        :raises TypeError: If command argument is invalid.
        """
        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise TypeError("must be exactly one command to learn")

        assert not kwargs, kwargs  # TODO: remove me

        if command[0] in self._commands:
            await self.async_delete_command(command)

        # Event to signal when the command is received
        learn_event = asyncio.Event()

        @callback
        def event_filter(event_data: dict[str, Any]) -> bool:
            """Return True if the listener callable should run.

            :param event_data: The data payload of the event (dict).
            :return: True if the event matches the filter.
            """
            codes = ("22F1", "22F3", "22F7")
            return event_data["src"] == self._device.id and event_data["code"] in codes

        @callback
        def listener(event: Event) -> None:
            """Save the command to storage.

            :param event: The event object.
            """
            # if event.data["packet"] in self._commands.values():  # TODO
            #     raise DuplicateError
            self._commands[command[0]] = event.data["packet"]
            learn_event.set()

        with self._broker._sem:
            self._broker.learn_device_id = self._device.id
            remove_listener = self.hass.bus.async_listen(
                f"{DOMAIN}_learn", listener, event_filter
            )

            try:
                await asyncio.wait_for(learn_event.wait(), timeout=timeout)
            except TimeoutError:
                _LOGGER.warning(
                    "Timeout (start=%s) waiting for command '%s'",
                    timeout,
                    command[0],
                )
            finally:
                self._broker.learn_device_id = None
                remove_listener()

    async def async_send_command(
        self,
        command: Iterable[str] | str,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        delay_secs: float = DEFAULT_DELAY_SECS,
        hold_secs: None = None,
        **kwargs: Any,
    ) -> None:
        """Send commands from a device (remote).

        :param command: The command(s) to send.
        :param num_repeats: Number of times to repeat the command.
        :param delay_secs: Delay between repeats (gap duration).
        :param hold_secs: Not supported.
        :param kwargs: Arbitrary keyword arguments.
        :raises TypeError: If hold_secs is provided or command format is invalid.
        :raises LookupError: If the command is not known.
        """
        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise TypeError("must be exactly one command to send")

        if hold_secs:
            raise TypeError("hold_secs is not supported")

        assert not kwargs, kwargs  # TODO: remove me

        if command[0] not in self._commands:
            raise LookupError(f"command '{command[0]}' is not known")

        if not self._device.is_faked:  # have to check here, as not using device method
            raise TypeError(f"{self._device.id} is not configured for faking")

        cmd = Command(self._commands[command[0]])

        try:
            await self._broker.client.async_send_cmd(
                cmd,
                priority=Priority.HIGH,
                num_repeats=num_repeats,
                gap_duration=delay_secs,  # We map 'delay_secs' to 'gap_duration' in ramses_rf
            )

        except (TimeoutError, Exception) as err:
            # Catch TimeoutError (from ramses_rf) and generic Exception to prevent bubbling
            _LOGGER.warning(
                "Error sending command '%s' to device %s: %s",
                command[0],
                self._device.id,
                err,
            )

        # This will now execute even if the transmission failed
        await self._broker.async_update()

    async def async_add_command(
        self,
        command: Iterable[str] | str,
        packet_string: str,
        **kwargs: Any,
    ) -> None:
        """Directly add (or replace) a command without RF learning.

        :param command: The command name to add.
        :param packet_string: The raw packet string for the command.
        :param kwargs: Arbitrary keyword arguments.
        :raises TypeError: If command format is invalid.
        :raises ValueError: If packet_string is invalid.
        """
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise TypeError("must be exactly one command to add")

        assert not kwargs, kwargs  # TODO: remove me

        # Basic validation: ensure packet parses as a Command
        try:
            Command(packet_string)
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"packet_string invalid: {err}") from err

        if command[0] in self._commands:
            await self.async_delete_command(command)

        self._commands[command[0]] = packet_string

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the remote device.

        :param kwargs: Additional arguments.
        """
        _LOGGER.debug("Turning off REM device %s", self._device.id)
        pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the remote device.

        :param kwargs: Additional arguments.
        """
        _LOGGER.debug("Turning on REM device %s", self._device.id)
        pass

    # the 2411 fan_param services, adapted from climate.py (no REM update_all service)

    @callback
    async def async_get_fan_rem_param(self, **kwargs: Any) -> None:
        """Handle 'get_fan_param' service call.

        :param kwargs: Arbitrary keyword arguments.
        """
        _LOGGER.info(
            "Fan param read via remote entity %s (%s, id %s)",
            self.entity_id,
            self.__class__.__name__,
            self._device.id,
        )
        parent: DeviceIdT = self._broker._fan_bound_to_remote.get(self._device.id, None)
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            await self._broker.async_get_fan_param(kwargs)
        else:
            _LOGGER.warning("REM %s not bound to a FAN", self._device.id)

    @callback
    async def async_set_fan_rem_param(self, **kwargs: Any) -> None:
        """Handle 'set_fan_param' service call.

        :param kwargs: Arbitrary keyword arguments.
        """
        _LOGGER.info(
            "Fan param write via remote entity %s (%s)",
            self.entity_id,
            self.__class__.__name__,
        )
        parent: DeviceIdT = self._broker._fan_bound_to_remote.get(self._device.id, None)
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            await self._broker.async_set_fan_param(kwargs)
        else:
            _LOGGER.warning("REM %s not bound to a FAN", self._device.id)

    async def async_update_fan_rem_params(self, **kwargs: Any) -> None:
        """Handle 'update_fan_params' service call.

        :param kwargs: Arbitrary keyword arguments.
        """
        _LOGGER.info(
            "Fan read all params via remote entity %s (%s)",
            self.entity_id,
            self.__class__.__name__,
        )
        parent: DeviceIdT = self._broker._fan_bound_to_remote.get(self._device.id, None)
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            # Run synchronous I/O function in the executor to avoid blocking the loop
            await self.hass.async_add_executor_job(
                self._broker.get_all_fan_params, kwargs
            )
        else:
            _LOGGER.warning("REM %s not bound to a FAN", self._device.id)


@dataclass(frozen=True, kw_only=True)
class RamsesRemoteEntityDescription(RamsesEntityDescription, RemoteEntityDescription):
    """Class describing Ramses remote entities."""

    key = "remote"

    # integration-specific attributes
    ramses_cc_class: type[RamsesRemote] = RamsesRemote

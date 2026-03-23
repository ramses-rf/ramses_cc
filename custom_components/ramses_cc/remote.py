"""Support for RAMSES HVAC RF remotes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.remote import (
    RemoteEntity,
    RemoteEntityDescription,
    RemoteEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)
from homeassistant.helpers.event import async_track_state_change_event

from ramses_rf.device.hvac import HvacRemote
from ramses_tx.command import Command
from ramses_tx.const import DEFAULT_GAP_DURATION, Priority
from ramses_tx.exceptions import ProtocolError, ProtocolSendFailed, ProtocolTimeoutError
from ramses_tx.typing import DeviceIdT

from .const import ATTR_DEVICE_ID, DOMAIN
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .schemas import DEFAULT_NUM_REPEATS, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the remote platform.

    :param hass: The Home Assistant instance.
    :param entry: The config entry.
    :param async_add_entities: Callback to add entities.
    """
    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: list[HvacRemote]) -> None:
        entities = [
            RamsesRemoteEntityDescription.ramses_cc_class(
                coordinator, device, RamsesRemoteEntityDescription()
            )
            for device in devices
        ]
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)


class RamsesRemote(RamsesEntity, RemoteEntity):
    """Representation of a RAMSES RF remote."""

    _device: HvacRemote

    _attr_assumed_state: bool = True
    _attr_supported_features: int = (
        RemoteEntityFeature.LEARN_COMMAND | RemoteEntityFeature.DELETE_COMMAND
    )

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: HvacRemote,
        entity_description: RamsesRemoteEntityDescription,
    ) -> None:
        """Initialize a HVAC remote.

        :param coordinator: The RamsesCoordinator instance.
        :param device: The backend device instance.
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found %s", device.id)
        super().__init__(coordinator, device, entity_description)

        self._attr_is_on = True
        self._commands: dict[str, str] = coordinator._remotes.get(device.id, {})

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

        Usage:

        .. code-block::

            service: remote.delete_command
            data:
              command: boost
            target:
              entity_id: remote.device_id

        :param command: The command(s) to delete.
        :param kwargs: Arbitrary keyword arguments.
        """
        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        # if len(command) != 1:
        #     raise HomeAssistantError("must be exactly one command to delete")

        assert not kwargs, kwargs  # TODO: remove me

        self._commands = {k: v for k, v in self._commands.items() if k not in command}

    async def async_learn_command(
        self,
        command: Iterable[str] | str,
        timeout: int = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        """Learn a command from a device (remote) and add to the database.

        Usage:

        .. code-block::

            service: remote.learn_command
            data:
              command: boost
              timeout: 3
            target:
              entity_id: remote.device_id

        :param command: The command to learn, either as str or list of strs.
        :param timeout: Timeout in seconds, defaults to DEFAULT_TIMEOUT.
        :param kwargs: Arbitrary keyword arguments.
        :raises HomeAssistantError: If command argument is invalid or on TimeOut.
        """
        _LOGGER.debug("REM Learn starting, cmd: %s", command)
        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise HomeAssistantError("Enter exactly one command to learn")

        assert not kwargs, kwargs  # TODO: remove me

        if command[0] in self._commands:
            await self.async_delete_command(command)

        # Event to signal when the command is received, TODO not thread safe!
        learning_session = asyncio.Event()

        @callback
        async def _async_on_change(event: Event) -> None:
            """Save the new command to storage.

            :param event: Event to evaluate
            """

            codes = ("22F1", "22F3", "22F7")

            # if event.data["packet"] in self._commands.values():  # TODO
            #     raise DuplicateError

            new_state: State = event.data["new_state"]
            # _LOGGER.debug("REM event new_state: %s", new_state)
            new_data = new_state.attributes["extra_data"]
            # to extract e.g. 'code' in a jinja template, use:
            # {{ state_attr('event.ramses_cc_learn_event', 'extra_data')['code'] }}

            if new_data["src"] == self._device.id and new_data["code"] in codes:
                self._commands[command[0]] = new_data["packet"]
                learning_session.set()  # stops learn session
            else:
                _LOGGER.debug("REM FILTER FAILED: %s", new_data["code"])

        with self.coordinator._sem:
            _LOGGER.debug("LEARN _sem set, setting up listener")
            self.coordinator.learn_device_id = self._device.id
            remove_listener = async_track_state_change_event(
                self.hass, "event.ramses_cc_learn_event", _async_on_change
            )  # entity_ids format: event.{DOMAIN}_{event._attr_unique_id}

            try:
                _LOGGER.debug("REM LEARN listener attached, listening")
                await asyncio.wait_for(learning_session.wait(), timeout=timeout)
            except TimeoutError as err:
                warn_text = (
                    f"Timeout (start={timeout}) waiting for command '{command[0]}'"
                )
                _LOGGER.warning(warn_text)
                # Catch and rethrow to UI
                raise HomeAssistantError(f"{warn_text} ({err})") from err
            finally:
                self.coordinator.learn_device_id = (
                    None  # deactivates the ramses_cc_learn_event msg callback
                )
                remove_listener()
                _LOGGER.debug("REM LEARN listener removed")

    async def async_send_command(
        self,
        command: Iterable[str] | str,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        delay_secs: float = DEFAULT_GAP_DURATION,
        hold_secs: None = None,
        **kwargs: Any,
    ) -> None:
        """Send commands from a device (remote).

        Usage:

        .. code-block::

            service: remote.send_command
            data:
              command: boost
              delay_secs: 0.05
              num_repeats: 3
              device: 12:345678 (see NOTE)
            target:
              entity_id: remote.device_id

        :param command: The command(s) to send.
        :param num_repeats: Number of times to repeat the command.
        :param delay_secs: Delay between repeats (gap duration).
        :param hold_secs: Not supported.
        :param kwargs: Arbitrary keyword arguments.
        :raises HomeAssistantError: If hold_secs is provided or command format is invalid.
        :raises LookupError: If the command is not known.
        """
        # NOTE This command can also be called directly from Actions>remote.send_command
        # in that case:
        # - validate entry (example: max_num_repeats = 255!
        # - if device is supplied, lookup device_id and replace self.entity_id?
        if kwargs:
            _extra: str = (
                " The provided Device is ignored." if (kwargs.get("device")) else ""
            )
            _LOGGER.warning(
                "Use ramses_cc 'Send a Remote command' instead of this HA command to assure valid entry.%s",
                _extra,
            )
        # TODO validate/normalise other entry values?

        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise HomeAssistantError("must be exactly one command to send")

        if hold_secs:
            raise HomeAssistantError("hold_secs is not supported")

        if command[0] not in self._commands:
            raise HomeAssistantError(f"command '{command[0]}' is not known")

        if not self._device.is_faked:  # have to check here, as not using device method
            raise HomeAssistantError(f"{self._device.id} is not configured for faking")

        cmd = Command(self._commands[command[0]])

        if not self.coordinator.client:
            raise HomeAssistantError(
                "Cannot send command: RAMSES RF client is not initialized"
            )

        try:
            await self.coordinator.client.async_send_cmd(
                cmd,
                priority=Priority.HIGH,
                num_repeats=num_repeats,
                gap_duration=delay_secs,  # We map 'delay_secs' to 'gap_duration' in ramses_rf
            )

        except (
            TimeoutError,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            ProtocolError,
            AssertionError,
            Exception,
        ) as err:
            # Catch and rethrow TimeoutError (from ramses_rf) and generic Exceptions
            raise HomeAssistantError(
                f"Error sending command '{command[0]}' to device {self._device.id} ({err})"
            ) from err

        # This will now execute even if the transmission failed
        await self.coordinator.async_refresh()

    async def async_add_command(
        self,
        command: Iterable[str] | str,
        packet_string: str,
        **kwargs: Any,
    ) -> None:
        """Directly add (or replace) a command without RF learning.

        Usage:

        .. code-block::

            service: remote.add_command
            data:
              command: boost
              packet_string: "RQ --- 29:162275 30:123456 --:------ 22F1 003 000030"
            target:
              entity_id: remote.device_id

        :param command: The command name to add.
        :param packet_string: The raw packet string for the command.
        :param kwargs: Arbitrary keyword arguments.
        :raises HomeAssistantError: If command format is invalid.
        :raises ValueError: If packet_string is invalid.
        """
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise HomeAssistantError("must be exactly one command to add")

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

        :param kwargs: Additional arguments for the turn_off operation.
        """
        _LOGGER.debug("Turning off REM device %s", self._device.id)
        pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the remote device.

        :param kwargs: Additional arguments for the turn_on operation.
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
        parent: DeviceIdT = self.coordinator.fan_handler._fan_bound_to_remote.get(
            self._device.id, None
        )
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            await self.coordinator.async_get_fan_param(kwargs)
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
        parent: DeviceIdT = self.coordinator.fan_handler._fan_bound_to_remote.get(
            self._device.id, None
        )
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            await self.coordinator.async_set_fan_param(kwargs)
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
        parent: DeviceIdT = self.coordinator.fan_handler._fan_bound_to_remote.get(
            self._device.id, None
        )
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            # Call coordinator method directly on the loop.
            # coordinator.get_all_fan_params internally calls loop.create_task().
            # It is NOT blocking and must NOT be run in an executor.
            self.coordinator.get_all_fan_params(kwargs)
        else:
            _LOGGER.warning("REM %s not bound to a FAN", self._device.id)


@dataclass(frozen=True, kw_only=True)
class RamsesRemoteEntityDescription(RamsesEntityDescription, RemoteEntityDescription):
    """Class describing Ramses remote entities."""

    key = "remote"

    # integration-specific attributes
    ramses_cc_class: type[RamsesRemote] = RamsesRemote

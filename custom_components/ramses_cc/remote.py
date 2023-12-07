"""Support for RAMSES HVAC RF remotes."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime as dt, timedelta
import logging
from typing import Any

from ramses_rf.device.base import Entity as RamsesRFEntity
from ramses_rf.device.hvac import HvacRemote
from ramses_tx import Command, Priority
import voluptuous as vol

from homeassistant.components.remote import (
    RemoteEntity,
    RemoteEntityDescription,
    RemoteEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import DiscoveryInfoType

from . import RamsesController, RamsesEntity, RamsesEntityDescription
from .const import (
    ATTR_COMMAND,
    ATTR_DELAY_SECS,
    ATTR_NUM_REPEATS,
    ATTR_TIMEOUT,
    CONTROLLER,
    DOMAIN,
    SERVICE_DELETE_COMMAND,
    SERVICE_LEARN_COMMAND,
    SERVICE_SEND_COMMAND,
)


@dataclass(kw_only=True)
class RamsesRemoteEntityDescription(RamsesEntityDescription, RemoteEntityDescription):
    """Class describing Ramses remote entities."""


_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Ramses remotes."""
    controller: RamsesController = hass.data[DOMAIN][CONTROLLER]
    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        SERVICE_LEARN_COMMAND,
        {
            vol.Required(ATTR_COMMAND): cv.string,
            vol.Required(ATTR_TIMEOUT, default=60): vol.All(
                cv.positive_int, vol.Range(min=30, max=300)
            ),
        },
        "async_learn_command",
    )

    platform.async_register_entity_service(
        SERVICE_SEND_COMMAND,
        {
            vol.Required(ATTR_COMMAND): cv.string,
            vol.Required(ATTR_NUM_REPEATS, default=3): cv.positive_int,
            vol.Required(ATTR_DELAY_SECS, default=0.2): cv.positive_float,
        },
        "async_send_command",
    )

    platform.async_register_entity_service(
        SERVICE_DELETE_COMMAND,
        {vol.Required(ATTR_COMMAND): cv.string},
        "async_delete_command",
    )

    async def async_add_new_entity(entity: RamsesRFEntity) -> None:
        entities = []

        if isinstance(entity, HvacRemote):
            entities.append(
                RamsesRemote(
                    controller,
                    entity,
                    RamsesRemoteEntityDescription(key="remote"),
                )
            )

        async_add_entities(entities)

    controller.async_register_platform(platform, async_add_new_entity)


class RamsesRemote(RamsesEntity, RemoteEntity):
    """Representation of a Ramses remote."""

    rf_entity: HvacRemote

    _attr_assumed_state = True
    _attr_is_on = True
    _attr_supported_features = (
        RemoteEntityFeature.LEARN_COMMAND | RemoteEntityFeature.DELETE_COMMAND
    )

    def __init__(
        self, controller, device, entity_description: RamsesRemoteEntityDescription
    ) -> None:
        """Initialize a remote."""
        super().__init__(controller, device, entity_description)

        self.entity_id = f"{DOMAIN}.{device.id}"

        self._commands: dict[str, dict] = controller._known_commands.get(device.id, {})

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the integration-specific state attributes."""
        return super().extra_state_attributes | {
            "commands": self._commands,
        }

    @property
    def name(self) -> str:
        """Return the name of the remote."""
        return self.rf_entity.id

    async def async_delete_command(
        self,
        command: Iterable[str] | str,
        **kwargs: Any,
    ) -> None:
        """Delete commands from the database.

        service: remote.delete_command
        data:
          command: boost
        target:
          entity_id: remote.device_id
        """

        if isinstance(command, str):  # HACK to make it work as per HA service call
            command = [command]

        self._commands = {k: v for k, v in self._commands.items() if k not in command}

    async def async_learn_command(
        self,
        command: Iterable[str] | str,
        timeout: float = 60,
        **kwargs: Any,
    ) -> None:
        """Learn a command from a device (remote) and add to the database.

        service: remote.learn_command
        data:
          command: boost
          timeout: 3
        target:
          entity_id: remote.device_id
        """

        @callback
        def event_filter(event: Event) -> bool:
            """Return True if the listener callable should run."""
            codes = ("22F1", "22F3", "22F7")
            return (
                event.data["src"] == self.rf_entity.id and event.data["code"] in codes
            )

        @callback
        def listener(event: Event) -> None:
            """Save the command to storage."""
            # if event.data["packet"] in self._commands.values():  # TODO
            #     raise DuplicateError
            self._commands[command[0]] = event.data["packet"]

        if isinstance(command, str):  # HACK to make it work as per HA service call
            command = [command]

        if len(command) != 1:  # TODO: Bug was here
            raise TypeError("must be exactly one command to learn")
        if not isinstance(timeout, float | int) or not 5 <= timeout <= 300:
            raise TypeError("timeout must be 5 to 300 (default 60)")

        if command[0] in self._commands:
            await self.async_delete_command(command)

        with self.controller._sem:
            self.controller.learn_device_id = self.rf_entity.id
            remove_listener = self.hass.bus.async_listen(
                f"{DOMAIN}_learn", listener, event_filter
            )

            dt_expires = dt.now() + timedelta(seconds=timeout)
            while dt.now() < dt_expires:
                await asyncio.sleep(0.005)
                if self._commands.get(command[0]):
                    break

            self.controller.learn_device_id = None
            remove_listener()

    async def async_send_command(
        self,
        command: Iterable[str] | str,  # HA is Iterable, ramses is str
        delay_secs: float = 0.05,
        num_repeats: int = 3,
        **kwargs: Any,
    ) -> None:
        """Send commands from a device (remote).

        service: remote.send_command
        data:
          command: boost
          delay_secs: 0.05
          num_repeats: 3
        target:
          entity_id: remote.device_id
        """

        if isinstance(command, str):  # HACK to make it work as per HA service call
            command = [command]

        if len(command) != 1:
            raise TypeError("must be exactly one command to send")
        if not isinstance(delay_secs, float | int) or not 0.02 <= delay_secs <= 1:
            raise TypeError("delay_secs must be 0.02 to 1.0 (default 0.05)")
        if not isinstance(num_repeats, int) or not 1 <= num_repeats <= 5:
            raise TypeError("num_repeats must be 1 to 5 (default 3)")

        if command[0] not in self._commands:
            raise LookupError(f"command '{command[0]}' is not known")

        if (
            not self.rf_entity.is_faked
        ):  # have to check here, as not using device method
            raise TypeError(f"{self.rf_entity.id} is not enabled for faking")

        for x in range(num_repeats):
            if x != 0:
                await asyncio.sleep(delay_secs)
            cmd = Command(
                self._commands[command[0]],
                qos={"priority": Priority.HIGH, "retries": 0},
            )
            self.controller.client.send_cmd(cmd)

        await self.controller.async_update()

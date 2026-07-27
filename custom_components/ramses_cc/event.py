"""Event platform for RAMSES RF."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from re import Pattern
from typing import TYPE_CHECKING, Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .ramses_rf.messages import Message
from .ramses_tx.dtos import PacketDTO
from .ramses_tx.exceptions import PacketInvalid

from .const import CONF_ADVANCED_FEATURES, CONF_MESSAGE_EVENTS, DOMAIN, SIGNAL_UPDATE

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True, slots=True)
class RamsesEventData:
    """Class to hold RamsesEvent data."""

    type: str
    device_id: str | None
    dtm: str | None
    src: str | None
    dst: str | None
    verb: str | None
    code: str | None
    payload: str | None
    packet: str | None


class RamsesEventType(StrEnum):
    """Create ramses_cc event type enum."""

    LEARN = f"{DOMAIN}_learn"
    REGEX = f"{DOMAIN}_regex_match"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Event setup for RAMSES RF entry system-wide events."""

    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    # get regex from config flow
    features: dict[str, Any] = config_entry.options.get(CONF_ADVANCED_FEATURES, {})
    if message_events := features.get(CONF_MESSAGE_EVENTS):
        message_events_regex = re.compile(message_events)
    else:
        message_events_regex = None

    async_add_entities(
        [
            RamsesLearnEvent(
                coordinator,
                hass,
                data={"type": RamsesEventType.LEARN},
            ),
            RamsesRegexEvent(
                coordinator,
                hass,
                data={"type": RamsesEventType.REGEX},
                regex=message_events_regex,
            ),
        ]
    )


class RamsesEvent(EventEntity):
    """Representation of a RAMSES RF event."""

    _attr_event_types = [
        RamsesEventType.LEARN,
        RamsesEventType.REGEX,
    ]

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        hass: HomeAssistant,
        data: dict[str, Any],
        event_callback: Any,
    ) -> None:
        """Initialize the event entity."""
        self._coordinator: RamsesCoordinator = coordinator
        self._hass = hass
        self._type: str = data["type"]
        self._data = data
        self._event_callback = event_callback
        self._remove: Callable[[], None] | None = None
        super().__init__()
        _LOGGER.debug("RamsesEvent init completed for %s", self._type)

    def update_data(self, data: dict[str, Any]) -> None:
        """Update the event from async_process_msg()."""
        if self._type == data["type"]:
            self._data = data
            self._async_handle_event(self._type)
        else:
            warn_text = f"Cannot change event type {self._type} to: {data['type']}"
            _LOGGER.warning(warn_text)
            raise HomeAssistantError(warn_text)

    @callback
    def _async_handle_event(self, event: str) -> None:
        """Handle the RAMSES event."""

        _LOGGER.debug("handle_event %s, data: %s", self._type, self._data)
        self._trigger_event(
            event
            # no extra attributes required for the regex event
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks with the coordinator and store result to allow their removal."""
        if self._coordinator.client:
            await super().async_added_to_hass()
            _LOGGER.debug("RamsesEvent added_to_hass completed")
            self._remove = self._coordinator.client.add_msg_handler(
                self._event_callback
            )

    async def async_will_remove_from_hass(self) -> None:
        """Deregister callbacks with the coordinator."""
        if self._coordinator.client and self._remove is not None:
            self._remove()
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        :return: A dictionary of state attributes.
        """
        return {"type": self._type, "data": self._data}


class RamsesLearnEvent(RamsesEvent):
    """Representation of a RAMSES RF Learn event."""

    _attr_event_types = [
        RamsesEventType.LEARN,
    ]

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        hass: HomeAssistant,
        data: dict[str, Any],
    ) -> None:
        """Initialize the event.

        :param coordinator: The data update coordinator for the integration.
        :param hass: The Home Assistant instance.
        :param data: Supporting data to send with the event
        """

        self._type = RamsesEventType.LEARN
        self._attr_unique_id = "learn_event"
        self._attr_translation_key = "learn_event"

        @callback
        def async_process_msg(dto: PacketDTO, *args: Any, **kwargs: Any) -> None:
            """Process a message from the event bus and pass it on."""
            try:
                msg = Message(dto)
            except PacketInvalid:
                return

            # Defer SIGNAL_UPDATE so that the Gateway's async _msg_handler
            # (CQRS ingestion) has completed.  The protocol calls extra
            # handlers synchronously before the Gateway's create_task'd
            # coroutine runs, and the coroutine has internal await points
            # that yield to the event loop before _cqrs_ingestion_engine
            # executes.  Using a small delay ensures we run after all
            # pending coroutines in the current batch have completed.
            src_id = msg.src.id
            dst_id = msg.dst.id if msg.dst and msg.dst.id != msg.src.id else None

            async def _send_signals() -> None:
                await asyncio.sleep(0.05)
                async_dispatcher_send(self._hass, f"{SIGNAL_UPDATE}_{src_id}")
                if dst_id:
                    async_dispatcher_send(self._hass, f"{SIGNAL_UPDATE}_{dst_id}")

            self._hass.async_create_task(_send_signals())

            if (
                coordinator.learn_device_id is not None
                and coordinator.learn_device_id == msg.src.id
            ):
                event_data = {
                    "type": RamsesEventType.LEARN,
                    "src": msg.src.id,
                    "code": str(msg.code),
                    "packet": repr(msg),
                }
                self.update_data(event_data)

        super().__init__(coordinator, hass, data, event_callback=async_process_msg)


class RamsesRegexEvent(RamsesEvent):
    """Representation of a RAMSES RF Regex Match event."""

    _attr_event_types = [
        RamsesEventType.REGEX,
    ]

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        hass: HomeAssistant,
        data: dict[str, Any],
        regex: Pattern[str] | None = None,
    ) -> None:
        """Initialize the event.

        :param coordinator: The data update coordinator for the integration.
        :param hass: The Home Assistant instance.
        :param data: Supporting data to send with the event
        :param regex: The regular expression to match against
        """

        self.regex = regex
        self._type = RamsesEventType.REGEX
        self._attr_unique_id = "regex_event"
        self._attr_translation_key = "regex_event"

        @callback
        def async_process_msg(dto: PacketDTO, *args: Any, **kwargs: Any) -> None:
            """Process a message from the event bus and pass it on."""
            try:
                msg = Message(dto)
            except PacketInvalid:
                return

            # async_dispatcher_send(self._hass, f"{SIGNAL_UPDATE}_{msg.src.id}")
            # if msg.dst and msg.dst.id != msg.src.id:
            #     async_dispatcher_send(self._hass, f"{SIGNAL_UPDATE}_{msg.dst.id}")

            # filter msg by advanced_config regex, fire an event if a match
            if regex and regex.search(f"{msg!r}"):
                event_data = {
                    "type": RamsesEventType.REGEX,
                    "device_id": msg.src.id,
                    "dtm": msg.dtm.isoformat(),
                    "src": msg.src.id,
                    "dst": msg.dst.id,
                    "verb": msg.verb,
                    "code": str(msg.code),
                    "payload": msg.payload,
                    "packet": repr(msg),
                }
                self.update_data(event_data)

        super().__init__(coordinator, hass, data, event_callback=async_process_msg)

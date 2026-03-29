"""Tests for the RamsesEvent class."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.const import (
    CONF_ADVANCED_FEATURES,
    CONF_MESSAGE_EVENTS,
    DOMAIN,
)
from custom_components.ramses_cc.event import (
    RamsesEvent,
    RamsesEventData,
    RamsesEventType,
    RamsesLearnEvent,
    RamsesRegexEvent,
    async_setup_entry,
)


# Mock Message class
@dataclass
class mock_message:
    src: MagicMock
    dst: MagicMock | None
    dtm: MagicMock
    verb: str
    code: str
    payload: str
    _pkt: str


# Mock Coordinator
@pytest.fixture
def mock_coordinator(learn_device_id: str | None = None) -> MagicMock:
    """A mock object simulating the RamsesCoordinator."""
    coordinator = MagicMock()
    coordinator.async_register_platform = MagicMock()
    coordinator.learn_device_id = learn_device_id
    return coordinator


# Mock HomeAssistant
@pytest.fixture
def mock_hass() -> MagicMock:
    return MagicMock(spec=HomeAssistant, data={})


# Mock ConfigEntry
@pytest.fixture
def mock_config_entry() -> MagicMock:
    return MagicMock(spec=ConfigEntry, entry_id="123")


# Test RamsesEventData
def test_ramses_event_data() -> None:
    data = RamsesEventData(
        type="test",
        device_id="dev1",
        dtm="2023-01-01",
        src="src1",
        dst="dst1",
        verb="RP",
        code="code1",
        payload="payload1",
        packet="packet1",
    )
    assert data.type == "test"
    assert data.device_id == "dev1"


# Test RamsesEventType
def test_ramses_event_type() -> None:
    assert f"{DOMAIN}_learn" == RamsesEventType.LEARN
    assert f"{DOMAIN}_regex_match" == RamsesEventType.REGEX


# Test RamsesEvent
@pytest.mark.asyncio
async def test_ramses_event_init(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    event = RamsesEvent(
        mock_coordinator,
        mock_hass,
        {"type": RamsesEventType.LEARN},
        event_callback=MagicMock(),
    )
    assert event._type == RamsesEventType.LEARN


@pytest.mark.asyncio
async def test_ramses_event_update_data(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    event = RamsesEvent(
        mock_coordinator,
        mock_hass,
        {"type": RamsesEventType.LEARN},
        event_callback=MagicMock(),
    )
    with patch.object(event, "async_write_ha_state") as mock_write:
        event.update_data({"type": RamsesEventType.LEARN, "extra": "data"})
    mock_write.assert_called_once()
    assert event._type == RamsesEventType.LEARN
    assert event._data == {"type": RamsesEventType.LEARN, "extra": "data"}


@pytest.mark.asyncio
async def test_ramses_event_update_data_error(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    event = RamsesEvent(
        mock_coordinator,
        mock_hass,
        {"type": RamsesEventType.LEARN},
        event_callback=MagicMock(),
    )

    # Expect error
    with pytest.raises(HomeAssistantError):
        event.update_data({"type": RamsesEventType.REGEX, "extra": "data"})


@pytest.mark.asyncio
async def test_ramses_event_async_added_to_hass(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    mock_callback = MagicMock()
    event = RamsesEvent(
        mock_coordinator,
        mock_hass,
        {"type": RamsesEventType.LEARN},
        event_callback=mock_callback,
    )
    mock_coordinator.client.add_msg_handler.return_value = MagicMock()
    await event.async_added_to_hass()
    assert event._remove is not None


@pytest.mark.asyncio
async def test_ramses_event_async_will_remove_from_hass(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    mock_remove = MagicMock()
    event = RamsesEvent(
        mock_coordinator,
        mock_hass,
        {"type": RamsesEventType.LEARN},
        event_callback=MagicMock(),
    )
    event._remove = mock_remove
    await event.async_will_remove_from_hass()
    mock_remove.assert_called_once()


# Test RamsesLearnEvent
@pytest.mark.asyncio
async def test_ramses_learn_event_init(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    mock_coordinator.learn_device_id = "dev1"
    event = RamsesLearnEvent(
        mock_coordinator, mock_hass, {"type": RamsesEventType.LEARN}
    )
    assert event._attr_event_types == [RamsesEventType.LEARN]
    assert event._attr_unique_id == "learn_event"


@pytest.mark.asyncio
async def test_ramses_learn_event_async_process_msg(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    mock_coordinator.learn_device_id = "dev1"
    event = RamsesLearnEvent(
        mock_coordinator, mock_hass, {"type": RamsesEventType.LEARN}
    )
    msg = mock_message(
        src=MagicMock(id="dev1"),
        dst=MagicMock(id="dev2"),
        dtm=MagicMock(isoformat=MagicMock(return_value="2023-01-01")),
        verb="verb1",
        code="code1",
        payload="payload1",
        _pkt="packet1",
    )
    with patch.object(event, "update_data") as mock_update:
        event._event_callback(msg)
        mock_update.assert_called_once_with(  # TODO(eb): fails
            {
                "type": RamsesEventType.LEARN,
                "src": "dev1",
                "code": "code1",
                "packet": "packet1",
            }
        )


# Test RamsesRegexEvent
@pytest.mark.asyncio
async def test_ramses_regex_event_init(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    regex = re.compile("test")
    event = RamsesRegexEvent(
        mock_coordinator, mock_hass, {"type": RamsesEventType.REGEX}, regex=regex
    )
    assert event._attr_event_types == [RamsesEventType.REGEX]
    assert event.regex == regex


@pytest.mark.asyncio
async def test_ramses_regex_event_async_process_msg(
    mock_hass: MagicMock, mock_coordinator: MagicMock
) -> None:
    regex = re.compile("payload1")
    event = RamsesRegexEvent(
        mock_coordinator, mock_hass, {"type": RamsesEventType.REGEX}, regex=regex
    )
    msg = mock_message(
        src=MagicMock(id="dev1"),
        dst=MagicMock(id="dev2"),
        dtm=MagicMock(isoformat=MagicMock(return_value="2023-01-01")),
        verb="verb1",
        code="code1",
        payload="payload1",
        _pkt="packet1",
    )
    with patch.object(event, "update_data") as mock_update:
        event._event_callback(msg)
        mock_update.assert_called_once_with(
            {
                "type": RamsesEventType.REGEX,
                "device_id": "dev1",
                "dtm": "2023-01-01",
                "src": "dev1",
                "dst": "dev2",
                "verb": "verb1",
                "code": "code1",
                "payload": "payload1",
                "packet": "packet1",
            }
        )


# next 3 moved here from test_init events 0.55.6


async def test_domain_event_platform(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test the event platform setup and entity creation callback.

    :param hass: The Home Assistant instance.
    :param mock_config_entry: The mock config fixture.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {CONF_ADVANCED_FEATURES: {CONF_MESSAGE_EVENTS: None}}
    hass.data[DOMAIN] = {entry.entry_id: mock_coordinator}

    mock_add_entities = MagicMock()

    await async_setup_entry(hass, entry, mock_add_entities)

    assert mock_add_entities.called
    # Verify 2 events created
    assert len(mock_add_entities.call_args) == 2

    # Create a mock device that matches one of the descriptions
    mock_event = MagicMock(spec=RamsesEvent)
    mock_event.id = "test_event"
    mock_event.data = {"type": "tst"}
    mock_event.coordinator = mock_coordinator
    mock_event.hass = hass

    # Call the callback with the mock event
    mock_add_entities([mock_event])

    # Verify async_add_entities was called with the created entity
    assert mock_add_entities.called
    created_entities = mock_add_entities.call_args[0][0]
    assert len(created_entities) == 1
    assert isinstance(created_entities[0], RamsesEvent)
    assert created_entities[0].data["type"] == "tst"


@pytest.mark.skip  # TODO(eb): fix from bus listener to event state change listener
async def test_domain_events(hass: HomeAssistant, mock_coordinator: MagicMock) -> None:
    """Test async_register_domain_events callbacks."""
    # 1. Test with configured message events
    # entry = MagicMock()
    # entry.options = {CONF_ADVANCED_FEATURES: {CONF_MESSAGE_EVENTS: ".*"}}
    #
    # # We need to capture the inner 'async_process_msg' function defined inside async_register_domain_events
    # with patch.object(mock_coordinator.client, "add_msg_handler") as mock_add_handler:
    #     async_register_domain_events(hass, entry, mock_coordinator)
    #     assert mock_add_handler.called
    #     callback_func = mock_add_handler.call_args[0][0]
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="events_test_entry",
        options={
            "ramses_rf": {},
            "serial_port": "/dev/ttyUSB0",
            CONF_ADVANCED_FEATURES: {CONF_MESSAGE_EVENTS: ".*"},
        },
    )

    # 1. Test with configured message events
    with patch(
        "custom_components.ramses_cc.entity.RamsesEntity.available",
        new_callable=PropertyMock,
        return_value=True,
    ):
        await async_setup_component(hass, DOMAIN, entry)
        await hass.async_block_till_done()

    # loaded_entry = hass.config_entries.async_entries(DOMAIN)[0]
    # assert loaded_entry.state == ConfigEntryState.LOADED

    PLATFORMS = [Platform.EVENT]
    callback_func: Callable[..., Any] | None = None

    # We need to capture the inner 'async_process_msg' function defined inside RamsesEvent
    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS
    )  # init Events platform

    entity_registry = er.async_get(hass)
    event_entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    for event in event_entities:
        if event.domain == DOMAIN and isinstance(event, RamsesEvent):
            callback_func = event._event_callback
            break

    # Mock a Ramses Message
    msg = MagicMock()
    msg.dtm.isoformat.return_value = "2023-01-01T12:00:00"
    msg.src.id = "01:111111"
    msg.dst.id = "01:222222"
    msg.verb = " I"
    msg.code = "1234"
    msg.payload = {}
    msg._pkt = "PACKET_STRING"

    # Create a listener for the bus event
    events = []

    async def capture_event(event: Any) -> None:
        events.append(event)

    hass.bus.async_listen(f"{DOMAIN}_regex_match", capture_event)

    # Fire the callback
    if callback_func is not None:
        callback_func(msg)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["code"] == "1234"
    assert events[0].data["packet"] == "PACKET_STRING"

    # 2. Test Learn Mode Event Firing
    # Set coordinator to learn mode for this device
    mock_coordinator.learn_device_id = "01:111111"  # Matches msg.src.id
    learn_events = []

    async def capture_learn(event: Any) -> None:
        learn_events.append(event)

    hass.bus.async_listen(f"{DOMAIN}_learn", capture_learn)

    # Fire the callback again
    if callback_func is not None:
        callback_func(msg)
    await hass.async_block_till_done()

    assert len(learn_events) == 1
    assert learn_events[0].data["src"] == "01:111111"
    assert learn_events[0].data["packet"] == "PACKET_STRING"


@pytest.mark.skip  # TODO(eb): fix from bus listener to event state change listener
async def test_domain_events_no_config(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """Test async_register_domain_events with no message events configured."""
    entry = MagicMock()
    # No advanced features / message events configured
    entry.options = {}

    with patch.object(mock_coordinator.client, "add_msg_handler") as mock_add_handler:
        # async_register_domain_events(hass, entry, mock_coordinator)
        # TODO add direct Platform setup, see test_domain_events

        assert mock_add_handler.called
        callback_func = mock_add_handler.call_args[0][0]

    msg = MagicMock()
    msg._pkt = "PACKET"

    events = []

    async def capture_event(event: Any) -> None:
        events.append(event)

    hass.bus.async_listen(f"{DOMAIN}_regex_match", capture_event)

    # Fire callback - should NOT generate an event because no regex was compiled
    callback_func(msg)
    await hass.async_block_till_done()

    assert len(events) == 0

"""Tests for the ramses_cc button platform."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.button import (
    FILTER_RESET_KEY,
    HGI_BUTTON_DESCRIPTIONS,
    RamsesButtonBase,
    RamsesButtonEntityDescription,
    _ButtonFactory,
    async_setup_entry,
)
from custom_components.ramses_cc.const import (
    DOMAIN,
)
from custom_components.ramses_cc.helpers import normalize_device_id
from custom_components.ramses_cc.schemas import (
    SVC_FORCE_UPDATE,
    SVC_RESET_FILTER,
)
from ramses_rf.devices import HgiGateway, HvacRemote, HvacVentilator

HGI_ID = "18:123456"
FAN_ID = "29:123456"
REMOTE_ENTITY_ID = "remote.fan_remote"

HGI_BUTTON_KEYS = ("force_update", "sync_topology", "discover_known_devices")


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Return a mock RamsesCoordinator.

    :return: A mock object simulating the RamsesCoordinator.
    :rtype: MagicMock
    """
    coordinator = MagicMock()
    coordinator.async_register_platform = MagicMock()
    coordinator.devices = []
    coordinator.entry.entry_id = "test-entry"
    coordinator.hass = MagicMock()
    return coordinator


@pytest.fixture
def mock_hgi() -> MagicMock:
    """Return a mock HGI gateway device."""
    device = MagicMock(spec=HgiGateway)
    device.id = HGI_ID
    return device


@pytest.fixture
def mock_fan() -> MagicMock:
    """Return a mock FAN (ventilator) device with a bound REM."""
    device = MagicMock(spec=HvacVentilator)
    device.id = FAN_ID
    device.get_bound_rem.return_value = "29:654321"
    # device.supports_10d0.return_value = True  # requires _rf 0.60.10
    return device


def _make_entry(coordinator: MagicMock) -> MagicMock:
    """Return a mock config entry whose runtime_data is the coordinator."""
    entry = MagicMock()
    entry.runtime_data = coordinator
    return entry


def _patch_device_slug() -> Any:
    """Patch device_slug to resolve mock devices like real ones.

    The real helper inspects ramses_rf internals a MagicMock can't
    provide, so map by isinstance/_SLUG instead.
    """

    def _slug(device: Any) -> str:
        if isinstance(device, HgiGateway):
            return "HGI"
        return getattr(device, "_SLUG", "")

    return patch(
        "custom_components.ramses_cc.button.device_slug", side_effect=_slug
    )


def _registry_entity(device_id: str | None = "fake-device") -> MagicMock:
    """Return a mock entity-registry entry for a remote entity."""
    entity = MagicMock()
    entity.domain = "remote"
    entity.unique_id = FAN_ID
    entity.entity_id = REMOTE_ENTITY_ID
    entity.device_id = device_id
    return entity


# --- async_setup_entry / add_devices callback ---------------------------------


async def test_add_devices_skips_non_devices(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """The callback must ignore items that are not ramses_rf entities.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    """
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()

    with patch("custom_components.ramses_cc.button.entity_platform"):
        await async_setup_entry(hass, entry, mock_add_entities)

    add_callback = mock_coordinator.async_register_platform.call_args[0][1]

    # Act
    add_callback(["not-a-device", 42, None])

    # Assert
    assert not mock_add_entities.called


async def test_add_devices_accepts_prebuilt_entities(
    hass: HomeAssistant, mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """The callback passes through (unloaded) button entities directly.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    """
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()
    platform = MagicMock()
    # button.py calls entity_platform.async_get_current_platform(), so the
    # mock it returns is the object whose .entities must be a real dict
    platform.async_get_current_platform.return_value = platform
    platform.entities = {}

    with patch("custom_components.ramses_cc.button.entity_platform", platform):
        await async_setup_entry(hass, entry, mock_add_entities)
    add_callback = mock_coordinator.async_register_platform.call_args[0][1]

    description = HGI_BUTTON_DESCRIPTIONS[0]
    prebuilt = RamsesButtonBase(mock_coordinator, mock_hgi, description)
    prebuilt._attr_unique_id = f"{HGI_ID}-{description.key}"
    prebuilt.entity_id = f"button.{description.key}"

    # Act
    add_callback([prebuilt])

    # Assert
    assert mock_add_entities.called
    assert mock_add_entities.call_args[0][0] == [prebuilt]

    # Act (again: entity already loaded in the platform)
    mock_add_entities.reset_mock()
    platform.entities = {prebuilt.entity_id: prebuilt}

    add_callback([prebuilt])

    # Assert
    assert not mock_add_entities.called


# --- factory: HGI buttons -------------------------------------------------------


async def test_hgi_buttons_created_per_service(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """One button per gateway-level service, with deterministic unique_ids."""
    # Arrange
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        buttons = factory.button_entities(mock_hgi)

    # Assert
    assert len(buttons) == len(HGI_BUTTON_KEYS)
    assert {b.unique_id for b in buttons} == {
        f"{normalize_device_id(HGI_ID)}-{key}" for key in HGI_BUTTON_KEYS
    }
    assert [b.entity_description.service for b in buttons] == [
        description.service for description in HGI_BUTTON_DESCRIPTIONS
    ]


async def test_hgi_buttons_deduplicated(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """Re-processing the same HGI must not create duplicate buttons."""
    # Arrange
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        first = factory.button_entities(mock_hgi)
        second = factory.button_entities(mock_hgi)

    # Assert
    assert first and not second


async def test_hgi_buttons_skipped_for_non_gateway(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """A non-HGI device must not receive gateway-level buttons."""
    # Arrange
    factory = _ButtonFactory(mock_coordinator)

    # Act & Assert
    assert factory.hgi_buttons(mock_fan) == []


# --- factory: FAN filter-reset buttons -------------------------------------------


def _patch_entity_registry(entities: list[MagicMock]) -> Any:
    """Patch the entity-registry lookups used by the button factory."""
    return patch(
        "custom_components.ramses_cc.button.er",
        MagicMock(
            async_get=MagicMock(return_value=MagicMock()),
            async_entries_for_config_entry=MagicMock(return_value=entities),
        ),
    )


async def test_fan_button_created_with_remote_target(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """The filter-reset button targets the FAN's remote entity.

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_fan: The mock FAN device fixture.
    :type mock_fan: MagicMock
    """
    # Arrange
    factory = _ButtonFactory(mock_coordinator)
    registry_entry = _registry_entity(device_id="device-rem")
    if hasattr(mock_fan, "supports_10d0"):
        mock_fan.supports_10d0.return_value = True

    # Act
    with _patch_device_slug(), _patch_entity_registry([registry_entry]):
        buttons = factory.fan_buttons(mock_fan)

    # Assert
    assert len(buttons) == 1
    button = buttons[0]
    assert (
        button.unique_id == f"{normalize_device_id(FAN_ID)}-{FILTER_RESET_KEY}"
    )
    assert button.entity_description.service == SVC_RESET_FILTER
    assert button.entity_description.target == {
        "entity_id": [REMOTE_ENTITY_ID]
    }
    assert button.entity_description.entity_category is None


async def test_fan_button_retry_when_no_bound_rem(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """A FAN without a bound REM yields no button (retried later)."""
    # Arrange
    mock_fan.get_bound_rem.return_value = None
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with (
        _patch_device_slug(),
        _patch_entity_registry([]),
    ):
        buttons = factory.fan_buttons(mock_fan)

    # Assert
    assert buttons == []


async def test_fan_button_retry_when_no_remote_entity(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """A FAN whose remote entity is not yet registered yields no button."""
    # Arrange
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with (
        _patch_device_slug(),
        _patch_entity_registry([]),
    ):
        buttons = factory.fan_buttons(mock_fan)

    # Assert
    assert buttons == []


async def test_no_fan_button_when_remote_entity_not_faked(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """A FAN whose remote entity is not set up as _faked yields no button."""
    # Arrange
    factory = _ButtonFactory(mock_coordinator)
    bound_remote = MagicMock(spec=HvacRemote)
    bound_remote.is_faked.return_value = False
    mock_fan.get_bound_rem.return_value = bound_remote

    # Act
    with (
        _patch_device_slug(),
        _patch_entity_registry([]),
    ):
        buttons = factory.fan_buttons(mock_fan)

    # Assert
    assert buttons == []


async def test_no_fan_button_on_rem_device(
    mock_coordinator: MagicMock,
) -> None:
    """A REM (remote) device must not receive a filter-reset button."""
    # Arrange: a REM is an HvacRemote, not an HvacVentilator
    mock_rem = MagicMock(spec=HvacRemote)
    mock_rem.id = "29:654321"
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        buttons = factory.button_entities(mock_rem)

    # Assert
    assert buttons == []


# --- RamsesButtonBase.async_press ------------------------------------------------


async def test_press_calls_configured_service(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """Pressing a button calls its configured ramses_cc service."""
    # Arrange
    description = RamsesButtonEntityDescription(
        key="force_update",
        name="Force update",
        service=SVC_FORCE_UPDATE,
        service_data={"param": "value"},
    )
    button = RamsesButtonBase(mock_coordinator, mock_hgi, description)
    button.hass = MagicMock()
    button.hass.services.async_call = AsyncMock()

    # Act
    await button.async_press()

    # Assert
    button.hass.services.async_call.assert_awaited_once_with(
        DOMAIN,
        SVC_FORCE_UPDATE,
        service_data={"param": "value"},
        target={},
        blocking=True,
    )


async def test_press_without_service_logs_warning(
    mock_coordinator: MagicMock,
    mock_hgi: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A button with no configured service logs a warning and calls nothing.

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    :param caplog: The log-capture fixture.
    :type caplog: pytest.LogCaptureFixture
    """
    # Arrange
    description = RamsesButtonEntityDescription(key="broken", name="Broken")
    button = RamsesButtonBase(mock_coordinator, mock_hgi, description)
    button.hass = MagicMock()
    button.hass.services.async_call = AsyncMock()

    # Act
    await button.async_press()

    # Assert
    assert "has no service configured" in caplog.text
    assert not button.hass.services.async_call.called


async def test_extra_state_attributes_include_target(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """extra_state_attributes exposes the configured target."""
    # Arrange
    description = RamsesButtonEntityDescription(
        key="reset_filter_counter",
        name="Reset filter counter",
        service=SVC_RESET_FILTER,
        target={"entity_id": [REMOTE_ENTITY_ID]},
    )
    button = RamsesButtonBase(mock_coordinator, mock_hgi, description)

    # Act
    attrs = button.extra_state_attributes

    # Assert
    assert attrs["target"] == {"entity_id": [REMOTE_ENTITY_ID]}


async def test_factory_coerces_set_target_to_list(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """A set-valued entity_id target is coerced to a sorted list.

    hass.services.async_call rejects a set as an entity_id target, so
    the factory normalizes the shape for any description.
    """
    # Arrange
    description = RamsesButtonEntityDescription(
        key="reset_filter_counter",
        name="Reset filter counter",
        service=SVC_RESET_FILTER,
        target={"entity_id": {REMOTE_ENTITY_ID, "remote.other"}},
    )
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        normalized = factory._make_button(mock_hgi, description, HGI_ID)

    # Assert: the set was coerced to a sorted list
    assert normalized is not None
    assert normalized.entity_description.target == {
        "entity_id": [REMOTE_ENTITY_ID, "remote.other"]  # order sorted?
    }


async def test_extra_state_attributes_no_target(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """extra_state_attributes not exposes empty target."""
    # Arrange
    description = RamsesButtonEntityDescription(
        key="reset_filter_counter",
        name="Reset filter counter",
        service=SVC_RESET_FILTER,
    )
    button = RamsesButtonBase(mock_coordinator, mock_hgi, description)

    # Act
    attrs = button.extra_state_attributes

    # Assert
    assert not hasattr(attrs, "target")


# --- factory: remaining HGI branch --------------------------------------------


async def test_hgi_buttons_skipped_when_no_id(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """An HGI without an id must not receive gateway buttons.

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    """
    # Arrange
    mock_hgi.id = None
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        buttons = factory.hgi_buttons(mock_hgi)

    # Assert
    assert buttons == []


# --- factory: remaining FAN branches -------------------------------------------


async def test_fan_buttons_skipped_for_non_fan(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """A non-FAN device must not receive a filter-reset button.

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    """
    # Arrange
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        buttons = factory.fan_buttons(mock_hgi)

    # Assert
    assert buttons == []


async def test_fan_button_skipped_when_10d0_unsupported(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """A FAN that does not support 10D0 filter change gets no button."""
    # Arrange: expose the supports_10d0 attribute (requires _rf 0.60.10)
    mock_fan.supports_10d0 = False
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        buttons = factory.fan_buttons(mock_fan)

    # Assert
    assert buttons == []


async def test_fan_button_deduplicated(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """Re-processing the same FAN must not create a duplicate button."""
    # Arrange
    factory = _ButtonFactory(mock_coordinator)
    registry_entry = _registry_entity()

    # Act
    with _patch_device_slug(), _patch_entity_registry([registry_entry]):
        first = factory.fan_buttons(mock_fan)
        second = factory.fan_buttons(mock_fan)

    # Assert
    assert first and not second


# --- factory: button_entities slug routing --------------------------------------


async def test_button_entities_routes_fan(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """button_entities routes FAN devices to the filter-reset button."""
    # Arrange
    mock_fan._SLUG = "FAN"  # resolved by _patch_device_slug's fallback
    registry_entry = _registry_entity()
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug(), _patch_entity_registry([registry_entry]):
        buttons = factory.button_entities(mock_fan)

    # Assert
    assert len(buttons) == 1
    assert (
        buttons[0].unique_id
        == f"{normalize_device_id(FAN_ID)}-{FILTER_RESET_KEY}"
    )


async def test_button_entities_ignores_unknown_slug(
    mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """Devices with an unroutable slug receive no buttons."""
    # Arrange
    mock_fan._SLUG = "CTL"
    factory = _ButtonFactory(mock_coordinator)

    # Act
    with _patch_device_slug():
        buttons = factory.button_entities(mock_fan)

    # Assert
    assert buttons == []


# --- async_setup_entry: setup pass ------------------------------------------------


def _patch_platform(entities: dict[str, Any]) -> Any:
    """Patch entity_platform with a platform whose entities is a dict."""
    platform = MagicMock()
    platform.async_get_current_platform.return_value = platform
    platform.entities = entities
    return patch(
        "custom_components.ramses_cc.button.entity_platform", platform
    )


async def test_setup_adds_buttons_for_known_devices(
    hass: HomeAssistant, mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """Devices known at setup time get their buttons added immediately.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    """
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()
    mock_coordinator._devices = [mock_hgi]

    # Act
    with _patch_device_slug(), _patch_platform({}):
        await async_setup_entry(hass, entry, mock_add_entities)

    # Assert: one batch of HGI buttons added during the setup pass
    assert mock_add_entities.call_count == 1
    added = mock_add_entities.call_args[0][0]
    assert len(added) == len(HGI_BUTTON_KEYS)


async def test_setup_without_devices_logs_no_entities(
    hass: HomeAssistant,
    mock_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup with no button-worthy devices logs and adds nothing.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param caplog: The log-capture fixture.
    :type caplog: pytest.LogCaptureFixture
    """
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()
    mock_coordinator._devices = []

    # Act
    with _patch_platform({}):
        await async_setup_entry(hass, entry, mock_add_entities)

    # Assert
    assert not mock_add_entities.called
    assert "No button entities registered at setup time" in caplog.text


# --- async_setup_entry / add_devices: remaining dispatch branches ------------------


async def test_add_devices_single_device_dispatch(
    hass: HomeAssistant, mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """A single device (not a Sequence) dispatched via the callback works.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    """
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()

    with _patch_device_slug(), _patch_platform({}):
        await async_setup_entry(hass, entry, mock_add_entities)
    add_callback = mock_coordinator.async_register_platform.call_args[0][1]

    # Act: a bare device, as the new-device dispatch delivers it
    mock_add_entities.reset_mock()
    with _patch_device_slug():
        add_callback(mock_hgi)

    # Assert
    assert mock_add_entities.call_count == 1
    assert len(mock_add_entities.call_args[0][0]) == len(HGI_BUTTON_KEYS)


async def test_add_devices_empty_list(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """An empty dispatch payload returns early without side effects."""
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()

    with _patch_platform({}):
        await async_setup_entry(hass, entry, mock_add_entities)
    add_callback = mock_coordinator.async_register_platform.call_args[0][1]

    # Act
    add_callback([])

    # Assert
    assert not mock_add_entities.called


async def test_add_devices_mixed_list_processes_devices(
    hass: HomeAssistant, mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """A mixed payload (entity + device) falls through to the device loop.

    :param hass: The Home Assistant instance.
    :type hass: HomeAssistant
    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    """
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()

    with _patch_device_slug(), _patch_platform({}):
        await async_setup_entry(hass, entry, mock_add_entities)
    add_callback = mock_coordinator.async_register_platform.call_args[0][1]

    prebuilt = RamsesButtonBase(
        mock_coordinator, mock_hgi, HGI_BUTTON_DESCRIPTIONS[0]
    )
    prebuilt.entity_id = "button.prebuilt"

    # Act: not all items are buttons, so the device loop must run
    mock_add_entities.reset_mock()
    with _patch_device_slug():
        add_callback([prebuilt, mock_hgi])

    # Assert: the device's buttons were added (the prebuilt one is not
    # routed through the direct-entity branch from a mixed list)
    assert mock_add_entities.call_count == 1
    assert len(mock_add_entities.call_args[0][0]) == len(HGI_BUTTON_KEYS)


async def test_add_devices_skips_unknown_slug_device(
    hass: HomeAssistant, mock_coordinator: MagicMock, mock_fan: MagicMock
) -> None:
    """Devices routed to no button type add nothing via dispatch."""
    # Arrange
    entry = _make_entry(mock_coordinator)
    mock_add_entities = MagicMock()
    mock_fan._SLUG = "CTL"

    with _patch_device_slug(), _patch_platform({}):
        await async_setup_entry(hass, entry, mock_add_entities)
    add_callback = mock_coordinator.async_register_platform.call_args[0][1]

    # Act
    add_callback([mock_fan])

    # Assert
    assert not mock_add_entities.called


# --- RamsesButtonBase.extra_state_attributes: remaining branch ---------------------


async def test_extra_state_attributes_target_is_none_when_unset(
    mock_coordinator: MagicMock, mock_hgi: MagicMock
) -> None:
    """With no target configured, the target attribute is None.

    (This replaces test_extra_state_attributes_no_target: the attribute
    is always present in the dict, but None-valued when unset.)

    :param mock_coordinator: The mock coordinator fixture.
    :type mock_coordinator: MagicMock
    :param mock_hgi: The mock HGI gateway device fixture.
    :type mock_hgi: MagicMock
    """
    # Arrange
    description = RamsesButtonEntityDescription(
        key="sync_topology", name="Sync topology"
    )
    button = RamsesButtonBase(mock_coordinator, mock_hgi, description)

    # Act
    attrs = button.extra_state_attributes

    # Assert
    assert "target" in attrs
    assert attrs["target"] is None

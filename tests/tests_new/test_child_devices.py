"""Unit tests for child device registry integration in ramses_cc.

Tests compliance with Home Assistant Core 2026.9+ parent_device_id and
ChildDeviceInfo specifications for Zones and UFH Circuits (Issue #1007).
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from custom_components.ramses_cc.entity import RamsesEntity
from ramses_rf.devices import Controller, DeviceHvac, UfhCircuit, UfhController
from ramses_rf.systems import System, Zone
from ramses_rf.topology import Child


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Provide a mock ConfigEntry for ramses_cc tests."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_entry_child_devs")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_coordinator(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> RamsesCoordinator:
    """Provide a minimal RamsesCoordinator instance."""
    coordinator = MagicMock(spec=RamsesCoordinator)
    coordinator.hass = hass
    coordinator.entry = mock_config_entry
    coordinator._device_info = {}
    coordinator.client = MagicMock()
    # Bind actual _async_update_device method for testing
    coordinator._async_update_device = (  # type: ignore[method-assign]
        RamsesCoordinator._async_update_device.__get__(
            coordinator, RamsesCoordinator
        )
    )
    return coordinator


async def test_zone_registered_as_child_of_tcs(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Verify an Evohome Zone registers as a ChildDeviceInfo of its TCS."""
    # Arrange
    dev_reg = dr.async_get(hass)
    parent_tcs_entry = dev_reg.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "01:123456")},
        name="Controller 01:123456",
    )

    mock_tcs = MagicMock(spec=System)
    mock_tcs.id = "01:123456"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "01:123456_01"
    mock_zone.name = "Living Room"
    mock_zone.tcs = mock_tcs

    # Act
    await mock_coordinator._async_update_device(mock_zone)

    # Assert
    cached_info = mock_coordinator._device_info.get("01:123456_01")
    assert cached_info is not None
    assert cached_info["identifiers"] == {(DOMAIN, "01:123456_01")}
    assert cached_info["name"] == "Living Room"
    assert cached_info["parent_device_id"] == parent_tcs_entry.id

    child_entry = dev_reg.async_get_child_device_by_identifier(
        (DOMAIN, "01:123456_01"), mock_config_entry.entry_id
    )
    assert child_entry is not None
    assert child_entry.parent_device_id == parent_tcs_entry.id


async def test_unnamed_zone_fallback_to_friendly_name(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    # Arrange
    dev_reg = dr.async_get(hass)
    parent_tcs_entry = dev_reg.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "01:123456")},
        name="Controller 01:123456",
        model="Controller",
    )

    mock_tcs = MagicMock(spec=System)
    mock_tcs.id = "01:123456"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "01:123456_04"
    mock_zone.name = None
    mock_zone._child_id = "04"
    mock_zone.tcs = mock_tcs

    # Act
    await mock_coordinator._async_update_device(mock_zone)

    # Assert
    cached_info = mock_coordinator._device_info.get("01:123456_04")
    assert cached_info is not None
    assert cached_info["identifiers"] == {(DOMAIN, "01:123456_04")}
    assert cached_info["name"] == "Zone 4"
    assert cached_info["parent_device_id"] == parent_tcs_entry.id

    child_entry = dev_reg.async_get_child_device_by_identifier(
        (DOMAIN, "01:123456_04"), mock_config_entry.entry_id
    )
    assert child_entry is not None
    assert child_entry.name == "Zone 4"
    assert child_entry.parent_device_id == parent_tcs_entry.id


async def test_controller_device_registration_and_model(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    # Arrange
    dev_reg = dr.async_get(hass)

    mock_ctl = MagicMock(spec=Controller)
    mock_ctl.id = "01:088175"
    mock_ctl.name = None
    mock_ctl._SLUG = "CTL"
    mock_ctl.state_store = None

    # Act
    await mock_coordinator._async_update_device(mock_ctl)

    # Assert
    ctl_info = mock_coordinator._device_info.get("01:088175")
    assert ctl_info is not None
    assert ctl_info["name"] == "Controller 01:088175"
    assert ctl_info["model"] == "Controller"
    assert ctl_info.get("manufacturer") is None

    ctl_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "01:088175"), mock_config_entry.entry_id
    )
    assert ctl_entry is not None
    assert ctl_entry.name == "Controller 01:088175"
    assert ctl_entry.model == "Controller"
    assert ctl_entry.manufacturer is None


async def test_ufh_circuit_registered_as_child_of_ufc(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Verify an Underfloor Heating Circuit registers as a ChildDeviceInfo of its UFC."""
    # Arrange
    dev_reg = dr.async_get(hass)
    parent_ufc_entry = dev_reg.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "02:222222")},
        name="UFH Controller 02:222222",
    )

    mock_ufc = MagicMock(spec=UfhController)
    mock_ufc.id = "02:222222"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.name = "Kitchen"

    mock_circuit = MagicMock(spec=UfhCircuit)
    mock_circuit.id = "02:222222_00"
    mock_circuit.ufh_index = "00"
    mock_circuit.ufc = mock_ufc
    mock_circuit.zone = mock_zone
    mock_circuit.name = None

    # Act
    await mock_coordinator._async_update_device(mock_circuit)

    # Assert
    cached_info = mock_coordinator._device_info.get("02:222222_00")
    assert cached_info is not None
    assert cached_info["identifiers"] == {(DOMAIN, "02:222222_00")}
    assert cached_info["name"] == "UFH Circuit 02:222222_00"
    assert cached_info["parent_device_id"] == parent_ufc_entry.id
    assert cached_info.get("suggested_area") == "Kitchen"

    circuit_entry = dev_reg.async_get_child_device_by_identifier(
        (DOMAIN, "02:222222_00"), mock_config_entry.entry_id
    )
    assert circuit_entry is not None
    assert circuit_entry.parent_device_id == parent_ufc_entry.id


async def test_eager_parent_registration_when_child_updated_first(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Verify that an unhydrated parent is eagerly registered if child is discovered first."""
    # Arrange - Neither parent nor child is registered yet
    dev_reg = dr.async_get(hass)
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, "01:654321"), mock_config_entry.entry_id
        )
        is None
    )

    mock_tcs = MagicMock(spec=System)
    mock_tcs.id = "01:654321"
    mock_tcs.name = "Controller 01:654321"
    mock_tcs._SLUG = "CTL"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "01:654321_02"
    mock_zone.name = "Bedroom"
    mock_zone.tcs = mock_tcs

    # Act
    await mock_coordinator._async_update_device(mock_zone)

    # Assert - Parent should have been registered eagerly
    parent_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "01:654321"), mock_config_entry.entry_id
    )
    assert parent_entry is not None

    child_entry = dev_reg.async_get_child_device_by_identifier(
        (DOMAIN, "01:654321_02"), mock_config_entry.entry_id
    )
    assert child_entry is not None
    assert child_entry.parent_device_id == parent_entry.id


async def test_entity_device_info_property_resolves_child_info(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Verify RamsesEntity.device_info returns the coordinator's cached ChildDeviceInfo."""
    # Arrange
    dev_reg = dr.async_get(hass)
    parent_entry = dev_reg.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "01:999999")},
        name="Controller 01:999999",
    )

    mock_tcs = MagicMock(spec=System)
    mock_tcs.id = "01:999999"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "01:999999_01"
    mock_zone.name = "Hallway"
    mock_zone.tcs = mock_tcs

    await mock_coordinator._async_update_device(mock_zone)

    entity_description = EntityDescription(key="temperature")
    entity = RamsesEntity(mock_coordinator, mock_zone, entity_description)

    # Act
    resolved_info = entity.device_info

    # Assert
    assert resolved_info is not None
    assert resolved_info["identifiers"] == {(DOMAIN, "01:999999_01")}
    assert resolved_info["parent_device_id"] == parent_entry.id

    # Simulate EntityPlatform linking entity device_info to registry
    res = dev_reg.async_get_or_create_child(
        config_entry_id=mock_config_entry.entry_id,
        **cast(dr.ChildDeviceInfo, resolved_info),
    )
    assert res.parent_device_id == parent_entry.id


async def test_standalone_devices_remain_main_devices(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Verify TRVs, relays, and REM accessories remain main devices (DeviceInfo)."""
    # Arrange
    dev_reg = dr.async_get(hass)

    mock_trv = MagicMock(spec=Child)
    mock_trv.id = "04:111111"
    mock_trv.name = "TRV Actuator"
    mock_trv._SLUG = "TRV"
    mock_trv._parent = MagicMock()
    mock_trv._parent.id = "01:123456_01"

    mock_rem = MagicMock(spec=DeviceHvac)
    mock_rem.id = "37:222222"
    mock_rem.name = "Remote Sensor"
    mock_rem._SLUG = "REM"
    mock_rem._parent_fan = MagicMock()
    mock_rem._parent_fan.id = "32:111111"

    # Act
    await mock_coordinator._async_update_device(mock_trv)
    await mock_coordinator._async_update_device(mock_rem)

    # Assert
    trv_info = mock_coordinator._device_info.get("04:111111")
    assert trv_info is not None
    assert "parent_device_id" not in trv_info
    assert "via_device" not in trv_info

    rem_info = mock_coordinator._device_info.get("37:222222")
    assert rem_info is not None
    assert "parent_device_id" not in rem_info
    assert "via_device" not in rem_info

    # Verify they were created as standard devices in device registry
    trv_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "04:111111"), mock_config_entry.entry_id
    )
    assert trv_entry is not None
    assert (
        not hasattr(trv_entry, "parent_device_id")
        or trv_entry.parent_device_id is None
    )


async def test_backward_compatibility_fallback(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Verify fallback behavior when ChildDeviceInfo is unavailable (pre-2026.9)."""
    # Arrange
    mock_tcs = MagicMock(spec=System)
    mock_tcs.id = "01:888888"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "01:888888_01"
    mock_zone.name = "Study"
    mock_zone.tcs = mock_tcs

    with (
        patch(
            "homeassistant.helpers.device_registry.ChildDeviceInfo",
            create=False,
        ),
        patch.object(dr, "ChildDeviceInfo", create=False),
        patch(
            "homeassistant.helpers.device_registry.async_get"
        ) as mock_dr_get,
    ):
        delattr(dr, "ChildDeviceInfo")
        mock_registry = MagicMock()
        mock_dr_get.return_value = mock_registry

        # Act
        await mock_coordinator._async_update_device(mock_zone)

        # Assert
        call_kwargs = mock_registry.async_get_or_create.call_args[1]
        assert call_kwargs["via_device"] == (DOMAIN, "01:888888")


async def test_child_device_unresolvable_parent_logs_warning(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify warning is logged when a parent device cannot be resolved."""
    # Arrange
    mock_tcs = MagicMock(spec=System)
    mock_tcs.id = "01:000000"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "01:000000_01"
    mock_zone.name = "Attic"
    mock_zone.tcs = mock_tcs

    with patch(
        "homeassistant.helpers.device_registry.async_get"
    ) as mock_dr_get:
        mock_registry = MagicMock()
        # Ensure parent resolution returns None even after update
        mock_registry.async_get_device_by_identifier.return_value = None
        mock_dr_get.return_value = mock_registry

        # Act
        await mock_coordinator._async_update_device(mock_zone)

        # Assert
        assert (
            "Parent device 01:000000 could not be registered for child"
            in caplog.text
        )


async def test_child_device_already_cached_early_return(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Verify early return when child device is already cached identically."""
    # Arrange
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "01:112233")},
        name="Controller 01:112233",
    )

    mock_tcs = MagicMock(spec=System)
    mock_tcs.id = "01:112233"

    mock_zone = MagicMock(spec=Zone)
    mock_zone.id = "01:112233_01"
    mock_zone.name = "Dining Room"
    mock_zone.tcs = mock_tcs

    # First update caches the child device
    await mock_coordinator._async_update_device(mock_zone)
    cached_info = mock_coordinator._device_info.get("01:112233_01")
    assert cached_info is not None

    with patch.object(
        dev_reg, "async_get_or_create_child"
    ) as mock_create_child:
        # Act - second update with identical info
        await mock_coordinator._async_update_device(mock_zone)

        # Assert - should return early without creating child again
        mock_create_child.assert_not_called()


async def test_main_device_with_suggested_area(
    mock_coordinator: RamsesCoordinator,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Verify main devices support suggested_area kwargs correctly."""
    # Arrange
    dev_reg = dr.async_get(hass)

    mock_zone = MagicMock(spec=Zone)
    mock_zone.name = "Living Room"

    mock_circuit = MagicMock(spec=UfhCircuit)
    mock_circuit.id = "02:123456_99"
    mock_circuit.ufh_index = "99"
    mock_circuit.ufc = None
    mock_circuit.zone = mock_zone
    mock_circuit.name = None

    # Act
    await mock_coordinator._async_update_device(mock_circuit)

    # Assert
    dev_info = mock_coordinator._device_info.get("02:123456_99")
    assert dev_info is not None
    assert dev_info.get("suggested_area") == "Living Room"

    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, "02:123456_99"), mock_config_entry.entry_id
    )
    assert device_entry is not None
    assert device_entry.suggested_area == "Living Room"


async def test_backward_compatibility_fallback_child_and_hvac(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Verify fallback behavior for Child and DeviceHvac when ChildDeviceInfo is absent."""
    # Arrange
    mock_parent = MagicMock()
    mock_parent.id = "01:333333"

    mock_child = MagicMock(spec=Child)
    mock_child.id = "13:333333"
    mock_child.name = "Relay"
    mock_child._parent = mock_parent
    mock_child._SLUG = "BDR"

    mock_fan = MagicMock()
    mock_fan.id = "32:444444"

    mock_hvac = MagicMock(spec=DeviceHvac)
    mock_hvac.id = "37:444444"
    mock_hvac.name = "Vent Switch"
    mock_hvac._parent_fan = mock_fan
    mock_hvac._SLUG = "REM"

    with (
        patch(
            "homeassistant.helpers.device_registry.ChildDeviceInfo",
            create=False,
        ),
        patch.object(dr, "ChildDeviceInfo", create=False),
        patch(
            "homeassistant.helpers.device_registry.async_get"
        ) as mock_dr_get,
    ):
        delattr(dr, "ChildDeviceInfo")
        mock_registry = MagicMock()
        mock_dr_get.return_value = mock_registry

        # Act
        await mock_coordinator._async_update_device(mock_child)
        call_kwargs_child = mock_registry.async_get_or_create.call_args[1]
        assert call_kwargs_child["via_device"] == (DOMAIN, "01:333333")

        await mock_coordinator._async_update_device(mock_hvac)
        call_kwargs_hvac = mock_registry.async_get_or_create.call_args[1]
        assert call_kwargs_hvac["via_device"] == (DOMAIN, "32:444444")

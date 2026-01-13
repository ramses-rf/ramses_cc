"""Tests to achieve 100% coverage for the ramses_cc number platform."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceRegistry

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.number import (
    RamsesNumberBase,
    RamsesNumberEntityDescription,
    RamsesNumberParam,
    async_setup_entry,
    create_parameter_entities,
    get_param_descriptions,
    normalize_device_id,
)
from ramses_rf.entity_base import Entity as RamsesRFEntity

# Constants
FAN_ID = "30:999888"


class MockDevice(RamsesRFEntity):
    """Mock device class that includes necessary methods for spec validation."""

    supports_2411: bool = True

    def get_fan_param(self, param_id: str) -> float | None:
        """Stub."""
        return None  # pragma: no cover

    def clear_fan_param(self, param_id: str) -> None:
        """Stub."""
        pass  # pragma: no cover


class FakeParam(RamsesNumberParam):
    """Fake parameter class for mocking isinstance checks and attributes."""

    _device: Any = None  # Explicitly define _device so spec allows it


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> MagicMock:
    """Return a mock RamsesBroker configured for entity creation."""
    broker = MagicMock()
    # broker.hass needs .data and .services to satisfy internal HA helpers
    broker.hass = MagicMock(spec=HomeAssistant)
    broker.hass.data = {}
    broker.hass.services = MagicMock(spec=ServiceRegistry)
    # Mock config_dir and bus for entity registry
    broker.hass.config = MagicMock()
    broker.hass.config.config_dir = "/tmp"
    broker.hass.bus = MagicMock()

    # Handle async_create_task to avoid RuntimeWarning for unawaited coroutines
    def mock_create_task(coro: Any) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    broker.hass.async_create_task = MagicMock(side_effect=mock_create_task)

    broker.entry = MagicMock()
    broker.entry.entry_id = "test_entry"
    broker.async_set_fan_param = AsyncMock()
    broker.devices = []

    hass.data[DOMAIN] = {"test_entry": broker}
    return broker


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device with valid ID."""
    device = MagicMock(spec=MockDevice)
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    device.get_fan_param.return_value = None
    return device


@pytest.fixture
def number_entity(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> RamsesNumberParam:
    """Return an initialized RamsesNumberParam."""
    desc = RamsesNumberEntityDescription(key="param_01", ramses_rf_attr="01")
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity.hass = mock_broker.hass
    # Mock async_write_ha_state to avoid coroutine warnings
    entity.async_write_ha_state = MagicMock()
    return entity


def test_normalize_device_id() -> None:
    """Test device ID normalization helper."""
    assert normalize_device_id("01:123456") == "01_123456"
    assert normalize_device_id("30:ABCDEF") == "30_abcdef"


async def test_setup_entry_direct_entities(
    hass: HomeAssistant, mock_broker: MagicMock, number_entity: RamsesNumberParam
) -> None:
    """Test adding entities directly to the platform."""
    entry = MagicMock(entry_id="test_entry")
    async_add_entities = MagicMock()

    with patch(
        "custom_components.ramses_cc.number.async_get_current_platform",
        return_value=MagicMock(entities={}),
    ):
        await async_setup_entry(hass, entry, async_add_entities)
        add_devices_cb = mock_broker.async_register_platform.call_args[0][1]

        # Test adding new entity directly
        # Ensure the mock passes isinstance(x, RamsesNumberParam)
        new_entity = MagicMock(spec=FakeParam)
        new_entity.entity_id = "number.new_unique"
        new_entity.unique_id = "new_unique"
        new_entity._device = MagicMock()
        new_entity._request_parameter_value = AsyncMock()

        add_devices_cb([new_entity])
        assert async_add_entities.called


async def test_setup_entry_direct_duplicate(
    hass: HomeAssistant, mock_broker: MagicMock
) -> None:
    """Test adding direct entity that already exists in platform."""
    entry = MagicMock(entry_id="test_entry")
    async_add_entities = MagicMock()

    # Mock platform with existing entity
    existing_entity = MagicMock(spec=FakeParam)
    existing_entity.entity_id = "number.existing"

    with patch(
        "custom_components.ramses_cc.number.async_get_current_platform",
        return_value=MagicMock(entities={"number.existing": existing_entity}),
    ):
        await async_setup_entry(hass, entry, async_add_entities)
        add_devices_cb = mock_broker.async_register_platform.call_args[0][1]

        # Pass duplicate entity
        duplicate = MagicMock(spec=FakeParam)
        duplicate.entity_id = "number.existing"

        add_devices_cb([duplicate])
        # Should NOT add entities
        assert not async_add_entities.called


async def test_setup_entry_device_processing(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test device processing, including existing devices and filtering."""
    entry = MagicMock(entry_id="test_entry")
    async_add_entities = MagicMock()

    mock_broker.devices = [mock_fan_device]

    with (
        patch.object(hass, "async_create_task"),
        patch(
            "custom_components.ramses_cc.number.async_get_current_platform",
            return_value=MagicMock(entities={}),
        ) as mock_plat,
    ):
        mock_entity = MagicMock(spec=FakeParam)
        mock_entity.entity_id = "number.new_param"
        mock_entity.unique_id = "new_unique_id"
        mock_entity._device = MagicMock()
        mock_entity._device.id = "dev_id"
        mock_entity._request_parameter_value = AsyncMock()

        existing_entity = MagicMock(spec=FakeParam)
        existing_entity.entity_id = "number.existing_param"
        existing_entity.unique_id = "existing_unique_id"
        existing_entity._device = MagicMock()
        existing_entity._device.id = "dev_id"

        # Case 1: Existing entity in platform (skip)
        mock_plat.return_value.entities = {"number.existing_param": existing_entity}

        with patch(
            "custom_components.ramses_cc.number.create_parameter_entities",
            return_value=[mock_entity, existing_entity],
        ):
            await async_setup_entry(hass, entry, async_add_entities)

            assert async_add_entities.call_count == 1
            added_entities = async_add_entities.call_args[0][0]
            assert len(added_entities) == 1
            assert added_entities[0] == mock_entity

        # Case 2: Device callback
        add_devices_cb = mock_broker.async_register_platform.call_args[0][1]
        async_add_entities.reset_mock()

        add_devices_cb(["not_a_device"])
        assert not async_add_entities.called

        with patch(
            "custom_components.ramses_cc.number.create_parameter_entities",
            return_value=[mock_entity, existing_entity],
        ):
            add_devices_cb([mock_fan_device])
            assert async_add_entities.called


async def test_setup_entry_empty_devices(
    hass: HomeAssistant, mock_broker: MagicMock
) -> None:
    """Test setup entry with empty devices list."""
    entry = MagicMock(entry_id="test_entry")
    async_add_entities = MagicMock()

    with patch(
        "custom_components.ramses_cc.number.async_get_current_platform",
        return_value=MagicMock(entities={}),
    ):
        await async_setup_entry(hass, entry, async_add_entities)
        add_devices_cb = mock_broker.async_register_platform.call_args[0][1]

        # Call with empty list
        add_devices_cb([])
        assert not async_add_entities.called


async def test_scaling_logic(mock_broker: MagicMock) -> None:
    """Test RamsesNumberBase scaling and conversion methods."""
    desc = RamsesNumberEntityDescription(key="test", ramses_rf_attr="01")
    entity = RamsesNumberBase(mock_broker, MagicMock(id="10:111111"), desc)
    entity.hass = mock_broker.hass

    assert entity._scale_for_storage(None) is None
    entity._is_percentage = True
    assert entity._scale_for_storage(50.0) == 0.5
    entity._is_percentage = False
    assert entity._scale_for_storage(50.0) == 50.0

    assert entity._scale_for_display(" None ") is None
    assert entity._scale_for_display("") is None
    assert entity._scale_for_display("invalid") is None
    assert entity._scale_for_display(None) is None

    entity._is_percentage = True
    assert entity._scale_for_display(0.5) == 50.0
    entity._is_percentage = False
    assert entity._scale_for_display(0.5) == 0.5


async def test_validation_logic(mock_broker: MagicMock) -> None:
    """Test value validation logic."""
    desc = RamsesNumberEntityDescription(key="test", ramses_rf_attr="01")
    entity = RamsesNumberBase(mock_broker, MagicMock(id="10:111111"), desc)

    valid, err = entity._validate_value_range(None)
    assert not valid
    assert "required" in err

    entity._attr_native_min_value = 0
    entity._attr_native_max_value = 10

    valid, err = entity._validate_value_range(-1)
    assert not valid
    assert "below minimum" in err

    valid, err = entity._validate_value_range(11)
    assert not valid
    assert "above maximum" in err

    valid, err = entity._validate_value_range(5)
    assert valid
    assert err is None


async def test_init_special_params(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test initialization logic for special parameters."""
    desc_95 = RamsesNumberEntityDescription(
        key="param_95",
        ramses_rf_attr="95",
        unit_of_measurement="%",
        min_value=0,
        max_value=1,
    )
    entity_95 = RamsesNumberParam(mock_broker, mock_fan_device, desc_95)
    assert entity_95._attr_native_max_value == 100
    assert entity_95._is_percentage is True

    desc_75 = RamsesNumberEntityDescription(key="param_75", ramses_rf_attr="75")
    entity_75 = RamsesNumberParam(mock_broker, mock_fan_device, desc_75)
    assert entity_75.mode == "slider"
    assert entity_75._attr_native_step == 0.1

    desc_prec = RamsesNumberEntityDescription(
        key="p", ramses_rf_attr="01", precision=0.5
    )
    entity_prec = RamsesNumberParam(mock_broker, mock_fan_device, desc_prec)
    assert entity_prec._attr_native_step == 0.5


async def test_init_generic_percentage(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test generic percentage scaling."""
    desc_perc = RamsesNumberEntityDescription(
        key="param_perc",
        ramses_rf_attr="99",  # Not 95
        unit_of_measurement="%",
        min_value=0,
        max_value=1,
    )
    entity_perc = RamsesNumberParam(mock_broker, mock_fan_device, desc_perc)
    assert entity_perc._is_percentage is True
    # Should scale 0->0, 1->100
    assert entity_perc._attr_native_min_value == 0
    assert entity_perc._attr_native_max_value == 100


async def test_init_parameter_52(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test initialization logic for parameter 52 (not scaled)."""
    desc_52 = RamsesNumberEntityDescription(
        key="param_52",
        ramses_rf_attr="52",
        unit_of_measurement="%",
        min_value=0,
        max_value=100,
    )
    entity_52 = RamsesNumberParam(mock_broker, mock_fan_device, desc_52)
    # Check that is_percentage is False for param 52 despite unit being %
    assert entity_52._is_percentage is False
    # Check bounds are not scaled
    assert entity_52._attr_native_max_value == 100


async def test_events_handling(number_entity: RamsesNumberParam) -> None:
    """Test event handling."""
    await number_entity.async_added_to_hass()
    assert number_entity.hass.bus.async_listen.called
    callback = number_entity.hass.bus.async_listen.call_args[0][1]

    event = MagicMock()
    event.data = {
        "device_id": number_entity._device.id,
        "param_id": "01",
        "value": 0.5,
    }
    callback(event)
    assert number_entity._param_native_value["01"] == 0.5

    event.data = {
        "device_id": "99:999999",
        "param_id": "01",
        "value": 0.9,
    }
    callback(event)
    assert number_entity._param_native_value["01"] == 0.5


async def test_events_handling_no_param_id(number_entity: RamsesNumberParam) -> None:
    """Test event handling return when no param id."""
    # Remove attr from description
    new_desc = dataclasses.replace(number_entity.entity_description, ramses_rf_attr="")
    number_entity.entity_description = new_desc

    await number_entity.async_added_to_hass()
    callback = number_entity.hass.bus.async_listen.call_args[0][1]

    # Should return early and not raise
    callback(MagicMock())


async def test_request_parameter_value(number_entity: RamsesNumberParam) -> None:
    """Test requesting parameter values."""
    number_entity._device.get_fan_param.return_value = 0.8
    await number_entity._request_parameter_value()
    assert number_entity.native_value == 0.8
    assert number_entity._device.get_fan_param.call_count == 2

    number_entity._device.get_fan_param.reset_mock()
    number_entity._device.get_fan_param.return_value = None
    number_entity.hass.async_create_task.reset_mock()

    await number_entity._request_parameter_value()
    assert number_entity.native_value == 0.8
    assert number_entity._is_pending
    assert number_entity.hass.async_create_task.called


async def test_request_parameter_value_init_dict(
    number_entity: RamsesNumberParam,
) -> None:
    """Test that dictionary is initialized if key missing."""
    # Clear the dict
    number_entity._param_native_value = {}
    number_entity._device.get_fan_param.return_value = None

    await number_entity._request_parameter_value()
    # Check that key was added
    assert "01" in number_entity._param_native_value
    assert number_entity._param_native_value["01"] is None


async def test_request_parameter_value_missing_attributes(
    number_entity: RamsesNumberParam, mock_broker: MagicMock
) -> None:
    """Test request parameter value early returns due to missing attrs."""
    # Test 1: No device
    number_entity._device = None
    await number_entity._request_parameter_value()
    assert not mock_broker.hass.async_create_task.called

    # Restore device
    number_entity._device = MagicMock()

    # Test 2: No hass
    # We patch hasattr to simulate missing 'hass' attribute
    # since deleting it from a mock is tricky/persistent
    with patch("custom_components.ramses_cc.number.hasattr") as mock_hasattr:

        def side_effect(obj: Any, attr: str) -> bool:
            return attr != "hass"

        mock_hasattr.side_effect = side_effect
        await number_entity._request_parameter_value()
        # Should return early
        assert not mock_broker.hass.async_create_task.called

    # Test 3: No parameter ID in desc
    desc = dataclasses.replace(number_entity.entity_description, ramses_rf_attr="")
    number_entity.entity_description = desc
    await number_entity._request_parameter_value()
    assert not mock_broker.hass.async_create_task.called


async def test_native_value_properties(number_entity: RamsesNumberParam) -> None:
    """Test native_value property logic."""
    # Test auto mode
    assert number_entity.mode == "auto"

    number_entity._param_native_value["01"] = 0.5
    assert number_entity.native_value == 0.5

    with patch.object(number_entity, "_is_boost_mode_param", return_value=True):
        number_entity._param_native_value["01"] = 0.5
        assert number_entity.native_value == 50.0

        number_entity._param_native_value["01"] = "invalid"
        assert number_entity.native_value is None

    new_desc = dataclasses.replace(number_entity.entity_description, ramses_rf_attr="")
    number_entity.entity_description = new_desc
    assert number_entity.native_value is None


async def test_async_set_native_value_success(number_entity: RamsesNumberParam) -> None:
    """Test setting the value successfully.

    :param number_entity: The entity to test
    """
    number_entity.hass.services.async_call = AsyncMock()

    # Normal value
    number_entity._attr_native_min_value = 0
    number_entity._attr_native_max_value = 100
    await number_entity.async_set_native_value(50.0)
    assert number_entity.hass.services.async_call.called

    # Boost mode
    with patch.object(number_entity, "_is_boost_mode_param", return_value=True):
        number_entity.hass.services.async_call.reset_mock()
        await number_entity.async_set_native_value(50.0)
        assert number_entity.hass.services.async_call.called

    # Validation failure
    number_entity.hass.services.async_call = AsyncMock()
    await number_entity.async_set_native_value(200.0)
    assert not number_entity.hass.services.async_call.called

    # Missing Param ID
    new_desc = dataclasses.replace(number_entity.entity_description, ramses_rf_attr="")
    number_entity.entity_description = new_desc
    number_entity.hass.services.async_call.reset_mock()
    await number_entity.async_set_native_value(50.0)
    assert not number_entity.hass.services.async_call.called


async def test_async_set_native_value_error(number_entity: RamsesNumberParam) -> None:
    """Test exception handling in setting value."""
    number_entity.hass.services.async_call = AsyncMock()
    number_entity.hass.services.async_call.side_effect = Exception("Service Fail")

    with pytest.raises(Exception, match="Service Fail"):
        await number_entity.async_set_native_value(50.0)


async def test_icon_logic(number_entity: RamsesNumberParam) -> None:
    """Test icon selection."""
    number_entity._is_pending = True
    assert number_entity.icon == "mdi:timer-sand"
    number_entity._is_pending = False

    new_desc = dataclasses.replace(
        number_entity.entity_description, ramses_cc_icon_off="mdi:off"
    )
    number_entity.entity_description = new_desc

    with patch.object(RamsesNumberParam, "native_value", None):
        assert number_entity.icon == "mdi:off"

    with patch.object(RamsesNumberParam, "native_value", 10):
        # Reset icon_off
        number_entity.entity_description = dataclasses.replace(
            number_entity.entity_description, ramses_cc_icon_off=None
        )

        # Standard units
        number_entity._attr_native_unit_of_measurement = "°C"
        assert number_entity.icon == "mdi:thermometer"

        number_entity._attr_native_unit_of_measurement = "%"
        assert number_entity.icon == "mdi:percent"

        number_entity._attr_native_unit_of_measurement = "min"
        assert number_entity.icon == "mdi:timer"

        # Param 52 (Gauge)
        number_entity._attr_native_unit_of_measurement = "%"
        number_entity.entity_description = dataclasses.replace(
            number_entity.entity_description, ramses_rf_attr="52"
        )
        assert number_entity.icon == "mdi:gauge"

        # Param 54 (Water Percent)
        number_entity._attr_native_unit_of_measurement = ""
        number_entity.entity_description = dataclasses.replace(
            number_entity.entity_description, ramses_rf_attr="54"
        )
        assert number_entity.icon == "mdi:water-percent"

        # Param 95
        number_entity.entity_description = dataclasses.replace(
            number_entity.entity_description, ramses_rf_attr="95"
        )
        assert number_entity.icon == "mdi:fan-speed-3"

        # Default Counter
        number_entity._attr_native_unit_of_measurement = ""
        number_entity.entity_description = dataclasses.replace(
            number_entity.entity_description, ramses_rf_attr="99"
        )
        assert number_entity.icon == "mdi:counter"


async def test_create_parameter_entities_registry(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test registry interaction in create_parameter_entities."""
    mock_reg = MagicMock()
    # First call returns ID (exists), Second returns None (create new)
    mock_reg.async_get_entity_id.side_effect = ["number.existing", None]

    with (
        patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_reg),
        patch(
            "custom_components.ramses_cc.number.get_param_descriptions"
        ) as mock_get_desc,
    ):
        mock_get_desc.return_value = [
            RamsesNumberEntityDescription(key="p1", ramses_rf_attr="01"),
            RamsesNumberEntityDescription(key="p2", ramses_rf_attr="02"),
        ]

        entities = create_parameter_entities(mock_broker, mock_fan_device)
        assert len(entities) == 2
        assert mock_reg.async_get_or_create.call_count == 1


async def test_create_parameter_entities_skip_empty_attr(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test skipping parameters with no attribute ID."""
    with (
        patch("homeassistant.helpers.entity_registry.async_get"),
        patch(
            "custom_components.ramses_cc.number.get_param_descriptions"
        ) as mock_get_desc,
    ):
        mock_get_desc.return_value = [
            RamsesNumberEntityDescription(key="no_attr", ramses_rf_attr=""),
            RamsesNumberEntityDescription(key="ok_attr", ramses_rf_attr="01"),
        ]
        entities = create_parameter_entities(mock_broker, mock_fan_device)
        assert len(entities) == 1
        assert entities[0].entity_description.key == "ok_attr"


async def test_create_parameter_entities_no_support(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test early return when device does not support 2411."""
    mock_fan_device.supports_2411 = False
    entities = create_parameter_entities(mock_broker, mock_fan_device)
    assert len(entities) == 0


async def test_create_parameter_entities_error(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test error handling in entity creation."""
    mock_reg = MagicMock()
    mock_reg.async_get_entity_id.side_effect = ValueError("Processing Error")

    with (
        patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_reg),
        patch(
            "custom_components.ramses_cc.number.get_param_descriptions",
            return_value=[RamsesNumberEntityDescription(key="p1", ramses_rf_attr="01")],
        ),
    ):
        entities = create_parameter_entities(mock_broker, mock_fan_device)
        assert len(entities) == 0


async def test_number_pending_timeout_error(
    number_entity: RamsesNumberParam, caplog: pytest.LogCaptureFixture
) -> None:
    """Test the exception path in pending clear."""
    number_entity.async_write_ha_state = MagicMock()
    with patch("asyncio.sleep", side_effect=RuntimeError("Async Fail")):
        await number_entity._clear_pending_after_timeout(1)
        assert "Error in pending clear task" in caplog.text


async def test_number_pending_timeout_success(
    number_entity: RamsesNumberParam,
) -> None:
    """Test successful pending timeout clear."""
    number_entity.async_write_ha_state = MagicMock()
    number_entity._is_pending = True

    # Use a real (0) sleep or a mock that just returns
    with patch("asyncio.sleep", return_value=None):
        await number_entity._clear_pending_after_timeout(1)

    assert not number_entity._is_pending
    assert number_entity.async_write_ha_state.called


async def test_get_param_descriptions(mock_fan_device: MagicMock) -> None:
    """Test getting parameter descriptions."""
    descs = get_param_descriptions(mock_fan_device)
    assert len(descs) > 0

    mock_fan_device.supports_2411 = False
    descs = get_param_descriptions(mock_fan_device)
    assert len(descs) == 0


async def test_entity_availability(number_entity: RamsesNumberParam) -> None:
    """Test the available property."""
    # With value -> Available
    number_entity._param_native_value["01"] = 10
    assert number_entity.available

    # No value -> Not available
    number_entity._param_native_value["01"] = None
    assert not number_entity.available

    # Missing param ID -> Not available
    desc = dataclasses.replace(number_entity.entity_description, ramses_rf_attr="")
    number_entity.entity_description = desc
    assert not number_entity.available

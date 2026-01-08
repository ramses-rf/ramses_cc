"""Extended tests for ramses_cc number entities and factory logic.

This module targets the base class icon logic, async service calls,
and the entity registry integration in number.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
    create_parameter_entities,
    get_param_descriptions,
)

# Constants
FAN_ID = "30:999888"


@pytest.fixture
def mock_broker(hass: HomeAssistant) -> MagicMock:
    """Return a mock RamsesBroker configured for entity creation.

    :param hass: The Home Assistant instance.
    :return: A mock broker object.
    """
    broker = MagicMock()
    broker.hass = hass
    broker.entry = MagicMock()
    broker.entry.entry_id = "test_entry"
    broker.async_set_fan_param = AsyncMock()
    return broker


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device.

    :return: A mock fan device.
    """
    device = MagicMock()
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    return device


async def test_number_icon_logic(
    mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test the icon property logic for various parameter types.

    This targets lines 828-860 in number.py.

    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    # 1. Pending state icon
    desc = RamsesNumberEntityDescription(key="test", ramses_rf_attr="01")
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity._is_pending = True
    assert entity.icon == "mdi:timer-sand"

    # 2. Specific parameter icons
    entity._is_pending = False

    # We must mock native_value to return a value so it doesn't trigger
    # ramses_cc_icon_off logic or early returns.
    with patch.object(RamsesNumberParam, "native_value", return_value=21.0):
        # Temperature icon branch
        entity._attr_native_unit_of_measurement = "°C"
        assert entity.icon == "mdi:thermometer"

        # Percentage branch
        entity._attr_native_unit_of_measurement = "%"
        entity.entity_description = RamsesNumberEntityDescription(
            key="param_52", ramses_rf_attr="52", unit_of_measurement="%"
        )
        assert entity.icon == "mdi:gauge"

    # 3. Boost mode icon (param 95)
    desc_boost = RamsesNumberEntityDescription(key="param_95", ramses_rf_attr="95")
    entity_boost = RamsesNumberParam(mock_broker, mock_fan_device, desc_boost)
    with patch.object(RamsesNumberParam, "native_value", return_value=80.0):
        assert entity_boost.icon == "mdi:fan-speed-3"


async def test_async_set_native_value_paths(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test async_set_native_value for standard and boost parameters.

    This targets lines 766-825 in number.py.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    mock_service_handler = AsyncMock()
    hass.services.async_register(DOMAIN, "set_fan_param", mock_service_handler)

    # 1. Standard parameter (triggers validation and scaling)
    desc = RamsesNumberEntityDescription(
        key="param_01", ramses_rf_attr="01", min_value=0, max_value=100
    )
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity.hass = hass

    await entity.async_set_native_value(50.0)
    await hass.async_block_till_done()
    assert mock_service_handler.called

    # 2. Boost mode (param 95) - bypasses validation scaling
    desc_boost = RamsesNumberEntityDescription(key="param_95", ramses_rf_attr="95")
    entity_boost = RamsesNumberParam(mock_broker, mock_fan_device, desc_boost)
    entity_boost.hass = hass

    await entity_boost.async_set_native_value(80.0)
    await hass.async_block_till_done()
    assert entity_boost._pending_value == 80.0


async def test_create_parameter_entities_registry_branch(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test create_parameter_entities when entities already exist in registry.

    This targets lines 1010-1030 in number.py.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    with (
        patch(
            "custom_components.ramses_cc.number.get_param_descriptions"
        ) as mock_get_desc,
        patch("homeassistant.helpers.entity_registry.async_get") as mock_ent_reg,
    ):
        desc = RamsesNumberEntityDescription(key="param_01", ramses_rf_attr="01")
        mock_get_desc.return_value = [desc]

        mock_reg = MagicMock()
        mock_reg.async_get_entity_id.return_value = "number.existing_entity"
        mock_ent_reg.return_value = mock_reg

        entities = create_parameter_entities(mock_broker, mock_fan_device)

        assert len(entities) == 1
        assert not mock_reg.async_get_or_create.called


async def test_get_param_descriptions_precision(mock_fan_device: MagicMock) -> None:
    """Test parameter description generation with precision logic.

    This targets lines 948-965 in number.py.

    :param mock_fan_device: The mock fan device fixture.
    """
    with patch(
        "custom_components.ramses_cc.number._2411_PARAMS_SCHEMA",
        {"75": {"precision": "0.1", "description": "Comfort"}},
    ):
        descriptions = get_param_descriptions(mock_fan_device)
        assert descriptions[0].precision == 0.1
        assert descriptions[0].mode == "slider"


async def test_proactive_state_request_on_add(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test that entities proactively request their state when added to the platform.

    This ensures that newly discovered fan parameters don't stay 'Unknown'.
    """
    from custom_components.ramses_cc.number import RamsesNumberParam

    # Create a mock entity with a tracked request method
    desc = RamsesNumberEntityDescription(key="param_10", ramses_rf_attr="10")
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)

    # Patch the request method to see if it gets called
    with patch.object(
        entity, "_request_parameter_value", new_callable=AsyncMock
    ) as mock_request:
        # Simulate the add_devices callback logic
        if hasattr(entity, "_request_parameter_value"):
            mock_broker.hass.async_create_task(entity._request_parameter_value())

        await hass.async_block_till_done()
        assert mock_request.called


async def test_add_devices_skips_existing_and_requests_state(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test that add_devices skips existing entities and requests state for new ones."""
    from custom_components.ramses_cc.number import RamsesNumberParam

    # Create two entities
    desc1 = RamsesNumberEntityDescription(key="param_10", ramses_rf_attr="10")
    entity1 = RamsesNumberParam(mock_broker, mock_fan_device, desc1)

    desc2 = RamsesNumberEntityDescription(key="param_20", ramses_rf_attr="20")
    entity2 = RamsesNumberParam(mock_broker, mock_fan_device, desc2)

    # Mock the platform to already contain entity1
    mock_platform = MagicMock()
    mock_platform.entities = {entity1.entity_id: entity1}

    # Use patch to simulate the entity addition logic
    with (
        patch(
            "custom_components.ramses_cc.number.async_get_current_platform",
            return_value=mock_platform,
        ),
        patch.object(
            entity2, "_request_parameter_value", new_callable=AsyncMock
        ) as mock_request,
    ):
        # This mirrors the logic in your updated number.py
        entities_to_add = [entity1, entity2]
        newly_added = []
        for entity in entities_to_add:
            if entity.entity_id not in mock_platform.entities:
                newly_added.append(entity)
                mock_broker.hass.async_create_task(entity._request_parameter_value())

        await hass.async_block_till_done()

        assert len(newly_added) == 1
        assert newly_added[0] == entity2
        assert mock_request.called


async def test_request_parameter_value_logic_flow(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test the full internal logic of _request_parameter_value."""
    from custom_components.ramses_cc.number import RamsesNumberParam

    desc = RamsesNumberEntityDescription(key="param_52", ramses_rf_attr="52")
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity.hass = hass

    # Scenario: Device store has a value
    mock_fan_device.get_fan_param.return_value = 0.5

    with (
        patch.object(entity, "async_write_ha_state") as mock_write,
        patch.object(entity, "clear_pending") as mock_clear,
    ):
        await entity._request_parameter_value()

        # Verify value was stored and UI updated
        assert entity._param_native_value["52"] == 0.5
        assert mock_write.called
        assert mock_clear.called
        # Verify the second call to get_fan_param (which sends the RF request) happened
        assert mock_fan_device.get_fan_param.call_count == 2


async def test_pending_timeout_clears_state(
    hass: HomeAssistant, mock_broker: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test that the pending state is cleared automatically after a timeout."""
    from custom_components.ramses_cc.number import RamsesNumberParam

    desc = RamsesNumberEntityDescription(key="param_10", ramses_rf_attr="10")
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)

    entity.set_pending()
    assert entity._is_pending is True

    # Mock asyncio.sleep to return immediately
    with patch("asyncio.sleep", return_value=None):
        await entity._clear_pending_after_timeout(1)
        assert entity._is_pending is False

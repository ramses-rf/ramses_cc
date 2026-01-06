"""Extended tests for ramses_cc number entities and factory logic.

This module targets the base class icon logic, async service calls,
and the entity registry integration in number.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

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
    # Temperature icon
    entity._is_pending = False
    entity._attr_native_unit_of_measurement = "°C"
    assert entity.icon == "mdi:thermometer"

    # Boost mode icon (param 95)
    desc_boost = RamsesNumberEntityDescription(key="param_95", ramses_rf_attr="95")
    entity_boost = RamsesNumberParam(mock_broker, mock_fan_device, desc_boost)
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
        # Setup one description
        desc = RamsesNumberEntityDescription(key="param_01", ramses_rf_attr="01")
        mock_get_desc.return_value = [desc]

        # Simulate entity already exists in registry
        mock_reg = MagicMock()
        mock_reg.async_get_entity_id.return_value = "number.existing_entity"
        mock_ent_reg.return_value = mock_reg

        entities = create_parameter_entities(mock_broker, mock_fan_device)

        assert len(entities) == 1
        assert not mock_reg.async_get_or_create.called  # Should skip creation


async def test_get_param_descriptions_precision(mock_fan_device: MagicMock) -> None:
    """Test parameter description generation with precision logic.

    This targets lines 948-965 in number.py.

    :param mock_fan_device: The mock fan device fixture.
    """
    with patch(
        "custom_components.ramses_cc.number._2411_PARAMS_SCHEMA",
        {"75": {"precision": "0.1"}},
    ):
        descriptions = get_param_descriptions(mock_fan_device)
        assert descriptions[0].precision == 0.1
        assert descriptions[0].mode == "slider"  # Forced for param 75

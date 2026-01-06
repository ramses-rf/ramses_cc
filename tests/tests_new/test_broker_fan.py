"""Tests for ramses_cc broker fan parameter logic and number entities.

This module tests the interaction between the RamsesBroker, the Gateway,
and the Number entities used for Fan parameters (2411).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
)

# Constants
FAN_ID = "30:123456"
REM_ID = "32:987654"
PARAM_ID_HEX = "75"  # Temperature parameter
PARAM_ID_INT = 117


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Return a mock Gateway.

    :return: A MagicMock simulating the Gateway with async_send_cmd.
    """
    gateway = MagicMock()
    gateway.async_send_cmd = AsyncMock()
    return gateway


@pytest.fixture
def mock_broker(hass: HomeAssistant, mock_gateway: MagicMock) -> RamsesBroker:
    """Return a configured RamsesBroker.

    :param hass: The Home Assistant instance.
    :param mock_gateway: The mock gateway fixture.
    :return: A RamsesBroker instance with the mock gateway attached.
    """
    entry = MagicMock()
    entry.options = {}
    entry.entry_id = "test_entry"

    broker = RamsesBroker(hass, entry)
    broker.client = mock_gateway
    broker._device_info = {}

    # Mock the hass.data structure
    hass.data[DOMAIN] = {entry.entry_id: broker}

    return broker


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device.

    :return: A MagicMock simulating a HvacVentilator device.
    """
    device = MagicMock()
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    device.get_bound_rem = MagicMock(return_value=REM_ID)
    return device


async def test_broker_get_fan_param(
    mock_broker: RamsesBroker, mock_gateway: MagicMock
) -> None:
    """Test async_get_fan_param service call in broker.py.

    :param mock_broker: The mock broker fixture.
    :param mock_gateway: The mock gateway fixture.
    """
    # 1. Test with explicit from_id
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "from_id": REM_ID}

    await mock_broker.async_get_fan_param(call_data)

    # Verify command sent
    assert mock_gateway.async_send_cmd.called
    cmd = mock_gateway.async_send_cmd.call_args[0][0]
    # Check command details (RQ 2411)
    assert cmd.dst.id == FAN_ID
    assert cmd.verb == "RQ"
    assert cmd.code == "2411"


async def test_broker_set_fan_param(
    mock_broker: RamsesBroker, mock_gateway: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test async_set_fan_param service call in broker.py.

    :param mock_broker: The mock broker fixture.
    :param mock_gateway: The mock gateway fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    # Mock the device lookup so the broker can find the bound remote
    mock_broker._devices = [mock_fan_device]

    # 1. Test with automatic bound device lookup (no from_id)
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 21.5}

    await mock_broker.async_set_fan_param(call_data)

    # Verify command sent
    assert mock_gateway.async_send_cmd.called
    cmd = mock_gateway.async_send_cmd.call_args[0][0]
    # Check command details (W 2411)
    assert cmd.dst.id == FAN_ID
    assert cmd.verb == " W"
    assert cmd.code == "2411"


async def test_number_entity_logic(
    hass: HomeAssistant,
    mock_broker: RamsesBroker,
    mock_fan_device: MagicMock,
) -> None:
    """Test RamsesNumberParam entity logic in number.py.

    :param hass: The Home Assistant instance.
    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    # 1. Setup the entity description
    desc = RamsesNumberEntityDescription(
        key="param_75",
        ramses_rf_attr=PARAM_ID_HEX,
        min_value=0,
        max_value=35,
        unit_of_measurement="°C",
        mode="slider",
    )

    # 2. Create the entity
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity.hass = hass

    # 3. Test Initial State
    assert entity.native_value is None
    assert entity.available is False  # No value yet

    # 4. Test Update from Event (simulating incoming packet)
    event_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 20.5}
    entity._async_param_updated(event_data)

    assert entity.native_value == 20.5
    assert entity.available is True

    # 5. Test Setting Value (async_set_native_value)
    # This should trigger a service call to the broker
    with patch.object(hass.services, "async_call") as mock_service:
        await entity.async_set_native_value(22.0)

        # Check pending state - simple assertion to avoid MyPy unreachable error
        assert entity._is_pending
        assert entity._pending_value == 22.0
        assert entity.icon == "mdi:timer-sand"

        # Verify service call
        mock_service.assert_called_once()
        assert mock_service.call_args[1]["service"] == "set_fan_param"
        assert mock_service.call_args[1]["service_data"]["value"] == 22.0


async def test_broker_fan_setup(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test _async_setup_fan_device logic in broker.py.

    :param mock_broker: The mock broker fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    # Mock callbacks
    mock_fan_device.set_initialized_callback = MagicMock()
    mock_fan_device.set_param_update_callback = MagicMock()

    await mock_broker._async_setup_fan_device(mock_fan_device)

    # Verify callbacks were registered
    assert mock_fan_device.set_initialized_callback.called
    assert mock_fan_device.set_param_update_callback.called

    # Verify parameter update callback logic
    # Get the callback function registered with the device
    callback_fn = mock_fan_device.set_param_update_callback.call_args[0][0]

    # Simulate a parameter update from the device library
    with patch.object(mock_broker.hass.bus, "async_fire") as mock_fire:
        callback_fn(PARAM_ID_HEX, 19.5)

        # Check if HA event was fired
        mock_fire.assert_called_once()
        assert mock_fire.call_args[0][0] == "ramses_cc.fan_param_updated"
        assert mock_fire.call_args[0][1]["value"] == 19.5


async def test_param_validation_logic(mock_broker: RamsesBroker) -> None:
    """Test validation logic in broker helper methods.

    Uses try-except blocks to avoid Mypy unreachable code errors with pytest.raises.

    :param mock_broker: The mock broker fixture.
    """
    # 1. Invalid Parameter ID - Not Hex
    try:
        mock_broker._get_param_id({"param_id": "ZZ"})
        pytest.fail("Should have raised ValueError for non-hex param_id")
    except ValueError:
        pass

    # 2. Invalid Parameter ID - Too Long
    try:
        mock_broker._get_param_id({"param_id": "123"})
        pytest.fail("Should have raised ValueError for long param_id")
    except ValueError:
        pass

    # 3. Valid Parameter ID
    assert mock_broker._get_param_id({"param_id": "75"}) == "75"
    assert (
        mock_broker._get_param_id({"param_id": 75}) == "4B"
    )  # Hex 4B is Int 75 (if passed as int/string mix up)

    # 4. Device Resolution (Target to ID)
    assert mock_broker._resolve_device_id({"device_id": FAN_ID}) == FAN_ID
    assert mock_broker._resolve_device_id({"device_id": [FAN_ID]}) == FAN_ID

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


async def test_number_entity_state(
    hass: HomeAssistant,
    mock_broker: RamsesBroker,
    mock_fan_device: MagicMock,
) -> None:
    """Test RamsesNumberParam entity initialization and state updates.

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


async def test_number_entity_set_value(
    hass: HomeAssistant,
    mock_broker: RamsesBroker,
    mock_fan_device: MagicMock,
) -> None:
    """Test RamsesNumberParam set value logic.

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

    # 3. Register a mock handler for the 'set_fan_param' service
    # The entity calls this service, so we must intercept it to verify the call.
    mock_service_handler = AsyncMock()
    hass.services.async_register(DOMAIN, "set_fan_param", mock_service_handler)

    # 4. Test Setting Value (async_set_native_value)
    await entity.async_set_native_value(22.0)

    # Wait for the service call event to be processed
    await hass.async_block_till_done()

    # Verify our mock service was called
    assert mock_service_handler.called
    # The call_args for a service handler is (ServiceCall,)
    service_call = mock_service_handler.call_args[0][0]
    assert service_call.data["device_id"] == FAN_ID
    assert service_call.data["param_id"] == PARAM_ID_HEX
    assert service_call.data["value"] == 22.0

    # Check pending state
    assert entity._is_pending is True
    assert entity._pending_value == 22.0
    assert entity.icon == "mdi:timer-sand"


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

    # Instead of patching hass.bus.async_fire (read-only), we create a mock listener
    event_callback = MagicMock()
    mock_broker.hass.bus.async_listen("ramses_cc.fan_param_updated", event_callback)

    # Simulate a parameter update from the device library
    callback_fn(PARAM_ID_HEX, 19.5)

    # Wait for event loop to process (fire is sync, but handlers are async)
    await mock_broker.hass.async_block_till_done()

    # Verify our listener was called
    assert event_callback.called
    event = event_callback.call_args[0][0]
    assert event.data["device_id"] == FAN_ID
    assert event.data["param_id"] == PARAM_ID_HEX
    assert event.data["value"] == 19.5


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
        mock_broker._get_param_id({"param_id": 75}) == "75"
    )  # Hex 4B is Int 75 (if passed as int/string mix up)

    # 4. Device Resolution (Target to ID)
    assert mock_broker._resolve_device_id({"device_id": FAN_ID}) == FAN_ID
    assert mock_broker._resolve_device_id({"device_id": [FAN_ID]}) == FAN_ID


async def test_update_fan_params_sequence(
    mock_broker: RamsesBroker,
    mock_gateway: MagicMock,
    mock_fan_device: MagicMock,  # <--- Added fixture here
) -> None:
    """Test the sequential update of fan parameters with mocked schema.

    This test patches the parameter schema to a small subset and mocks asyncio.sleep
    to ensure the test runs instantly without waiting for the 0.5s delay between
    requests.

    :param mock_broker: The mock broker fixture.
    :param mock_gateway: The mock gateway fixture.
    :param mock_fan_device: The mock fan device fixture.
    """
    # Register the mock device so the broker can find the bound remote (source ID)
    mock_broker._devices = [mock_fan_device]

    # Define a tiny schema for testing (just 2 params) to avoid 30+ iterations
    tiny_schema = ["11", "22"]

    # Patch the schema AND asyncio.sleep in a single with-statement (SIM117)
    with (
        patch("custom_components.ramses_cc.broker._2411_PARAMS_SCHEMA", tiny_schema),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        call_data = {"device_id": FAN_ID}
        await mock_broker._async_run_fan_param_sequence(call_data)

    # Verify that exactly 2 commands were sent (one for each param in tiny_schema)
    assert mock_gateway.async_send_cmd.call_count == 2

    # Optional: Verify the calls were correct
    calls = mock_gateway.async_send_cmd.call_args_list
    assert calls[0][0][0].code == "2411"  # First command
    assert calls[1][0][0].code == "2411"  # Second command


async def test_broker_set_fan_param_no_binding(
    mock_broker: RamsesBroker, mock_gateway: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test set_fan_param when the fan has NO bound remote (unbound)."""

    # Mock the device lookup
    mock_broker._devices = [mock_fan_device]

    # 1. Simulate an Unbound Fan (get_bound_rem returns None)
    mock_fan_device.get_bound_rem = MagicMock(return_value=None)

    # 2. Try to set a parameter WITHOUT providing a 'from_id'
    # This forces the broker to look for the bound remote
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 21.5}

    # 3. Expectation: It should NOT crash.
    # It should likely log a warning or raise a specific friendly error,
    # but NOT an AttributeError (which would be a crash).

    # Depending on current implementation, it might just log a warning and return
    # or raise a ValueError. Check broker.py logic.
    # If the code handles it safely, this test passes.

    # Example assertion (adjust based on actual broker.py behavior):
    await mock_broker.async_set_fan_param(call_data)

    # Verify NO command was sent (because there is no source ID)
    mock_gateway.async_send_cmd.assert_not_called()

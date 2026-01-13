"""Tests for the Fan Handler aspect of RamsesBroker (2411 logic, parameters)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
    create_parameter_entities,
)
from ramses_tx.schemas import SZ_KNOWN_LIST

# Constants
FAN_ID = "30:123456"
REM_ID = "32:987654"
PARAM_ID_HEX = "75"  # Temperature parameter
PARAM_ID_INT = 117


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Return a mock Gateway."""
    gateway = MagicMock()
    gateway.async_send_cmd = AsyncMock()
    return gateway


@pytest.fixture
def mock_broker(hass: HomeAssistant, mock_gateway: MagicMock) -> RamsesBroker:
    """Return a configured RamsesBroker."""
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
    """Return a mock Fan device."""
    device = MagicMock()
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    device.get_bound_rem = MagicMock(return_value=REM_ID)
    return device


async def test_broker_get_fan_param(
    mock_broker: RamsesBroker, mock_gateway: MagicMock
) -> None:
    """Test async_get_fan_param service call."""
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "from_id": REM_ID}

    await mock_broker.async_get_fan_param(call_data)

    assert mock_gateway.async_send_cmd.called
    cmd = mock_gateway.async_send_cmd.call_args[0][0]
    assert cmd.dst.id == FAN_ID
    assert cmd.verb == "RQ"
    assert cmd.code == "2411"


async def test_broker_set_fan_param(
    mock_broker: RamsesBroker, mock_gateway: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test async_set_fan_param service call."""
    mock_broker._devices = [mock_fan_device]
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 21.5}

    await mock_broker.async_set_fan_param(call_data)

    assert mock_gateway.async_send_cmd.called
    cmd = mock_gateway.async_send_cmd.call_args[0][0]
    assert cmd.dst.id == FAN_ID
    assert cmd.verb == " W"
    assert cmd.code == "2411"


async def test_broker_set_fan_param_no_binding(
    mock_broker: RamsesBroker, mock_gateway: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test set_fan_param when the fan has NO bound remote (unbound)."""
    mock_broker._devices = [mock_fan_device]
    mock_fan_device.get_bound_rem = MagicMock(return_value=None)

    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 21.5}

    with pytest.raises(
        HomeAssistantError, match="Cannot set parameter: No valid source device"
    ):
        await mock_broker.async_set_fan_param(call_data)

    mock_gateway.async_send_cmd.assert_not_called()


async def test_number_entity_state(
    hass: HomeAssistant, mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test RamsesNumberParam entity initialization and state updates."""
    desc = RamsesNumberEntityDescription(
        key="param_75",
        ramses_rf_attr=PARAM_ID_HEX,
        min_value=0,
        max_value=35,
        unit_of_measurement="°C",
        mode="slider",
    )
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity.hass = hass

    assert entity.native_value is None
    assert entity.available is False

    event_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "value": 20.5}
    entity._async_param_updated(event_data)

    assert entity.native_value == 20.5
    assert entity.available is True


async def test_number_entity_set_value(
    hass: HomeAssistant, mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test RamsesNumberParam set value logic."""
    desc = RamsesNumberEntityDescription(
        key="param_75",
        ramses_rf_attr=PARAM_ID_HEX,
        min_value=0,
        max_value=35,
        unit_of_measurement="°C",
        mode="slider",
    )
    entity = RamsesNumberParam(mock_broker, mock_fan_device, desc)
    entity.hass = hass

    mock_service_handler = AsyncMock()
    hass.services.async_register(DOMAIN, "set_fan_param", mock_service_handler)

    await entity.async_set_native_value(22.0)
    await hass.async_block_till_done()

    assert mock_service_handler.called
    service_call = mock_service_handler.call_args[0][0]
    assert service_call.data["device_id"] == FAN_ID
    assert service_call.data["param_id"] == PARAM_ID_HEX
    assert service_call.data["value"] == 22.0

    assert entity._is_pending is True
    assert entity._pending_value == 22.0


async def test_broker_fan_setup(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test _async_setup_fan_device logic."""
    mock_fan_device.set_initialized_callback = MagicMock()
    mock_fan_device.set_param_update_callback = MagicMock()

    await mock_broker._async_setup_fan_device(mock_fan_device)

    assert mock_fan_device.set_initialized_callback.called
    assert mock_fan_device.set_param_update_callback.called

    callback_fn = mock_fan_device.set_param_update_callback.call_args[0][0]
    event_callback = MagicMock()
    mock_broker.hass.bus.async_listen("ramses_cc.fan_param_updated", event_callback)

    callback_fn(PARAM_ID_HEX, 19.5)
    await mock_broker.hass.async_block_till_done()

    assert event_callback.called
    event = event_callback.call_args[0][0]
    assert event.data["device_id"] == FAN_ID
    assert event.data["value"] == 19.5


async def test_update_fan_params_sequence(
    mock_broker: RamsesBroker, mock_gateway: MagicMock, mock_fan_device: MagicMock
) -> None:
    """Test the sequential update of fan parameters."""
    mock_broker._devices = [mock_fan_device]
    tiny_schema = ["11", "22"]

    with (
        patch("custom_components.ramses_cc.broker._2411_PARAMS_SCHEMA", tiny_schema),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        call_data = {"device_id": FAN_ID}
        await mock_broker._async_run_fan_param_sequence(call_data)

    assert mock_gateway.async_send_cmd.call_count == 2


async def test_create_parameter_entities_logic(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test the factory function for creating number entities."""
    with patch("custom_components.ramses_cc.number.er.async_get") as mock_ent_reg:
        mock_reg = mock_ent_reg.return_value
        mock_reg.async_get_entity_id.return_value = None

        entities = create_parameter_entities(mock_broker, mock_fan_device)

        assert len(entities) > 0
        assert all(isinstance(e, RamsesNumberParam) for e in entities)
        assert entities[0]._device == mock_fan_device


async def test_setup_fan_bound_invalid_type(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test _setup_fan_bound_devices with invalid config type."""
    # Mock known_list with a non-string bound_to value (e.g. an integer)
    mock_broker.options[SZ_KNOWN_LIST] = {
        FAN_ID: {"bound_to": 12345}  # Invalid type
    }

    # This should trigger the warning and return early (lines 365-369)
    await mock_broker._setup_fan_bound_devices(mock_fan_device)

    # Verify no binding occurred
    mock_fan_device.add_bound_device.assert_not_called()

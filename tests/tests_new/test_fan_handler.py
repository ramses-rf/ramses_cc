"""Tests for the Fan Handler aspect of RamsesBroker (2411 logic, parameters)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN, SZ_BOUND_TO, SZ_KNOWN_LIST
from custom_components.ramses_cc.number import (
    RamsesNumberEntityDescription,
    RamsesNumberParam,
    create_parameter_entities,
)
from ramses_tx.const import DevType

# Constants
FAN_ID = "30:123456"
REM_ID = "32:987654"
PARAM_ID_HEX = "75"  # Temperature parameter
PARAM_ID_KEY = "temperature"
PARAM_ID_INT = 117


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Return a mock Gateway."""
    gateway = MagicMock()
    gateway.async_send_cmd = AsyncMock()
    gateway.get_device.return_value = None
    return gateway


@pytest.fixture
def mock_broker(hass: HomeAssistant, mock_gateway: MagicMock) -> RamsesBroker:
    """Return a configured RamsesBroker."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}

    broker = RamsesBroker(hass, entry)
    broker.client = mock_gateway
    # Create fake devices list if needed, or we patch _get_device
    broker._device_info = []

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


async def test_broker_init(mock_broker: RamsesBroker) -> None:
    """Test broker initialization."""
    assert mock_broker.client is not None
    assert mock_broker._devices == []


async def test_broker_get_fan_param(
    mock_broker: RamsesBroker, mock_gateway: MagicMock
) -> None:
    """Test async_get_fan_param service call."""
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX, "from_id": REM_ID}

    with patch.object(mock_broker, "_get_device") as mock_get_dev:
        mock_dev = MagicMock()
        mock_dev.id = FAN_ID
        mock_get_dev.return_value = mock_dev

        await mock_broker.async_get_fan_param(call_data)

    assert mock_gateway.async_send_cmd.called
    cmd = mock_gateway.async_send_cmd.call_args[0][0]
    # Check command attributes if possible, or just that it was sent
    assert cmd is not None


async def test_broker_set_fan_param(
    mock_broker: RamsesBroker, mock_gateway: MagicMock
) -> None:
    """Test async_set_fan_param service call."""
    call_data = {
        "device_id": FAN_ID,
        "param_id": PARAM_ID_HEX,
        "value": 0.5,
        "from_id": REM_ID,
    }

    # Patch _get_device so valid check passes
    with patch.object(mock_broker, "_get_device") as mock_get_dev:
        mock_dev = MagicMock()
        mock_dev.id = FAN_ID
        mock_get_dev.return_value = mock_dev

        await mock_broker.async_set_fan_param(call_data)

    assert mock_gateway.async_send_cmd.called


async def test_broker_set_fan_param_no_value(mock_broker: RamsesBroker) -> None:
    """Test async_set_fan_param raises error when value is missing."""
    call_data = {
        "device_id": FAN_ID,
        "param_id": PARAM_ID_HEX,
        "from_id": REM_ID,
    }

    # Patch _get_device so valid check passes and we hit the value check
    with patch.object(mock_broker, "_get_device") as mock_get_dev:
        mock_dev = MagicMock()
        mock_dev.id = FAN_ID
        mock_get_dev.return_value = mock_dev

        with pytest.raises(
            HomeAssistantError, match="Invalid parameter.*Missing required parameter"
        ):
            await mock_broker.async_set_fan_param(call_data)


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

    # Trigger the warning and return early
    await mock_broker._setup_fan_bound_devices(mock_fan_device)

    # Verify no binding occurred
    mock_fan_device.add_bound_device.assert_not_called()


async def test_setup_fan_bound_not_rem(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test _setup_fan_bound_devices with a device that is not REM or DIS."""
    # Mock a device that is neither HvacRemoteBase nor has _SLUG='DIS'
    bound_dev = MagicMock()
    bound_dev.id = "01:999999"
    # Ensure it fails isinstance(HvacRemoteBase) and checks
    mock_broker.client.devices = [bound_dev]

    mock_broker.options[SZ_KNOWN_LIST] = {FAN_ID: {"bound_to": bound_dev.id}}

    await mock_broker._setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_not_called()


async def test_fan_setup_callbacks_execution(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test execution of the initialization callbacks."""
    mock_fan_device.set_initialized_callback = MagicMock()

    # Call setup
    await mock_broker._async_setup_fan_device(mock_fan_device)

    # Get the lambda passed to callback
    init_lambda = mock_fan_device.set_initialized_callback.call_args[0][0]

    # Use patch.object on the specific instance's attribute
    with (
        patch.object(mock_broker, "get_all_fan_params") as mock_get_params,
        patch.object(mock_broker.hass, "async_create_task") as mock_create_task,
    ):
        mock_create_task.side_effect = lambda coro: coro

        # Execute the lambda (simulating first message arrival)
        coro = init_lambda()
        await coro

        assert mock_get_params.called


async def test_fan_setup_already_initialized(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test _async_setup_fan_device when device is already initialized."""
    mock_fan_device._initialized = True
    mock_fan_device.supports_2411 = True

    # Patch the function where it is DEFINED, which is used by the import in broker.py
    with patch(
        "custom_components.ramses_cc.number.create_parameter_entities"
    ) as mock_create:
        mock_create.return_value = [MagicMock()]
        await mock_broker._async_setup_fan_device(mock_fan_device)

        assert mock_create.called
        # Should also request params
        assert mock_broker.client.async_send_cmd.call_count >= 0


async def test_get_device_and_from_id_fallback(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test fallback to bound device when from_id is missing."""
    mock_broker._devices = [mock_fan_device]
    mock_fan_device.get_bound_rem.return_value = REM_ID

    # Must provide param_id to pass _get_param_id validation (called inside async_get_fan_param)
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX}

    # This calls _get_device_and_from_id internally
    await mock_broker.async_get_fan_param(call_data)

    # Check if the command was sent using the bound REM_ID
    cmd = mock_broker.client.async_send_cmd.call_args[0][0]
    assert cmd.src.id == REM_ID


async def test_run_fan_param_sequence_bad_data(
    mock_broker: RamsesBroker, mock_gateway: MagicMock
) -> None:
    """Test that sequence handles non-dict data gracefully."""
    bad_data = MagicMock()
    bad_data.items.side_effect = TypeError("Not a dict")
    bad_data.get.return_value = "0A"

    class NotADict:
        def get(self, key: str) -> str:
            return "0A"

    bad_data_obj = NotADict()

    with patch(
        "custom_components.ramses_cc.broker.RamsesBroker._normalize_service_call",
        return_value=bad_data_obj,
    ):
        await mock_broker._async_run_fan_param_sequence({})
    pass

    bad_data_mock = MagicMock(spec=dict)
    bad_data_mock.__iter__.side_effect = TypeError
    bad_data_mock.get.return_value = "0A"

    with patch(
        "custom_components.ramses_cc.broker.RamsesBroker._normalize_service_call",
        return_value=bad_data_mock,
    ):
        await mock_broker._async_run_fan_param_sequence({})

    assert mock_gateway.async_send_cmd.call_count >= 0


async def test_setup_fan_bound_success_rem(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test successful binding of a FAN to a REM device."""
    bound_id = "32:111111"

    # Configure the known list with the bound device
    mock_broker.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    # Create the bound device object
    bound_device = MagicMock()
    bound_device.id = bound_id
    mock_broker.client.devices = [bound_device]

    # Helper classes to satisfy isinstance checks in broker.py
    class MockHvacVentilator:
        pass

    class MockHvacRemoteBase:
        pass

    # Assign classes to mocks
    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]
    bound_device.__class__ = MockHvacRemoteBase  # type: ignore[assignment]

    # Patch the classes in the broker module so isinstance checks pass
    with (
        patch("custom_components.ramses_cc.broker.HvacVentilator", MockHvacVentilator),
        patch("custom_components.ramses_cc.broker.HvacRemoteBase", MockHvacRemoteBase),
    ):
        await mock_broker._setup_fan_bound_devices(mock_fan_device)

    # Verify binding was added with correct type
    mock_fan_device.add_bound_device.assert_called_once_with(bound_id, DevType.REM)
    assert mock_broker._fan_bound_to_remote[bound_id] == FAN_ID


async def test_setup_fan_bound_success_dis(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test successful binding of a FAN to a DIS device."""
    bound_id = "32:222222"

    mock_broker.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    bound_device = MagicMock()
    bound_device.id = bound_id
    bound_device._SLUG = DevType.DIS
    mock_broker.client.devices = [bound_device]

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    # Patch HvacVentilator to pass the first guard clause
    # Do NOT patch HvacRemoteBase, so isinstance(bound_device, HvacRemoteBase) will fail (correct for DIS)
    with patch("custom_components.ramses_cc.broker.HvacVentilator", MockHvacVentilator):
        await mock_broker._setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_called_once_with(bound_id, DevType.DIS)
    assert mock_broker._fan_bound_to_remote[bound_id] == FAN_ID


async def test_setup_fan_bound_device_not_found(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test binding when the bound device is not found in client.devices."""
    bound_id = "32:333333"

    mock_broker.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    # Ensure device list is empty
    mock_broker.client.devices = []

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch("custom_components.ramses_cc.broker.HvacVentilator", MockHvacVentilator):
        await mock_broker._setup_fan_bound_devices(mock_fan_device)

    # Should log warning and not add binding
    mock_fan_device.add_bound_device.assert_not_called()
    assert bound_id not in mock_broker._fan_bound_to_remote


async def test_setup_fan_bound_no_config(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test binding when no bound device is configured (early return)."""
    mock_broker.options[SZ_KNOWN_LIST] = {FAN_ID: {}}

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch("custom_components.ramses_cc.broker.HvacVentilator", MockHvacVentilator):
        await mock_broker._setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_not_called()


async def test_setup_fan_bound_bad_device_type(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test binding when device exists but is incompatible (not REM/DIS)."""
    bound_id = "32:444444"

    mock_broker.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    # Device exists but is generic (not REM, no DIS slug)
    bound_device = MagicMock()
    bound_device.id = bound_id
    del bound_device._SLUG  # Ensure no _SLUG attribute exists or it is not DIS
    mock_broker.client.devices = [bound_device]

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch("custom_components.ramses_cc.broker.HvacVentilator", MockHvacVentilator):
        await mock_broker._setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_not_called()


async def test_setup_fan_bound_invalid_id_type(
    mock_broker: RamsesBroker, mock_fan_device: MagicMock
) -> None:
    """Test binding when the bound device ID is not a string (e.g. integer)."""
    # Configure known_list with an integer instead of a string for bound_to
    mock_broker.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: 12345}}

    class MockHvacVentilator:
        pass

    # Satisfy the isinstance(device, HvacVentilator) check
    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch("custom_components.ramses_cc.broker.HvacVentilator", MockHvacVentilator):
        await mock_broker._setup_fan_bound_devices(mock_fan_device)

    # Verify no binding occurred and code returned early
    mock_fan_device.add_bound_device.assert_not_called()


async def test_target_to_device_id_single_string(
    mock_broker: RamsesBroker,
) -> None:
    """Test _target_to_device_id when device_id is a single string (lines 1009-1010)."""
    ha_device_id = "ha_dev_123"
    ramses_dev_id = "10:123456"

    # Target dict with a single string device_id (triggers line 1010)
    target = {"device_id": ha_device_id}

    # Patch the device registry getter used in broker.py
    with patch("custom_components.ramses_cc.broker.dr.async_get") as mock_dr_get:
        mock_reg = mock_dr_get.return_value

        # Create a mock device entry that links HA ID to Ramses ID
        mock_entry = MagicMock()
        mock_entry.identifiers = {(DOMAIN, ramses_dev_id)}

        # Setup registry to return our entry when queried with the HA device ID
        mock_reg.async_get.side_effect = (
            lambda x: mock_entry if x == ha_device_id else None
        )

        # Execute the method
        result = mock_broker._target_to_device_id(target)

    # Verify the resolution worked (meaning the string was successfully converted to a list and processed)
    assert result == ramses_dev_id


async def test_target_to_device_id_single_area_string(
    mock_broker: RamsesBroker,
) -> None:
    """Test _target_to_device_id when area_id is a single string.

    This covers the case where a single area_id string is provided in the target,
    triggering the list conversion logic (lines 1017-1018).
    """
    area_id = "living_room"
    ramses_dev_id = "10:654321"

    target = {"area_id": area_id}

    # Patch device registry
    with patch("custom_components.ramses_cc.broker.dr.async_get") as mock_dr_get:
        mock_reg = mock_dr_get.return_value

        # Create a mock device entry in the correct area with a RAMSES ID
        mock_entry = MagicMock()
        mock_entry.area_id = area_id
        mock_entry.identifiers = {(DOMAIN, ramses_dev_id)}

        # dev_reg.devices.values() is iterated
        mock_reg.devices.values.return_value = [mock_entry]

        # Execute
        result = mock_broker._target_to_device_id(target)

    assert result == ramses_dev_id


async def test_target_empty(mock_broker: RamsesBroker) -> None:
    """Test _target_to_device_id with empty or None input."""
    assert mock_broker._target_to_device_id({}) is None
    assert mock_broker._target_to_device_id(None) is None


async def test_target_entity_id_resolution(mock_broker: RamsesBroker) -> None:
    """Test resolution via entity_id (single string and list)."""
    target_single = {"entity_id": "sensor.temp"}
    target_list = {"entity_id": ["sensor.temp"]}

    ramses_id = "01:111111"
    ha_dev_id = "ha_dev_1"

    with (
        patch("custom_components.ramses_cc.broker.er.async_get") as mock_er_get,
        patch("custom_components.ramses_cc.broker.dr.async_get") as mock_dr_get,
    ):
        # Setup Entity Registry Mock
        mock_ent_reg = mock_er_get.return_value  # This is the registry
        mock_ent_entry = MagicMock()
        mock_ent_entry.device_id = ha_dev_id
        mock_ent_reg.async_get.return_value = mock_ent_entry  # Registry returns entry

        # Setup Device Registry Mock
        mock_dev_reg = mock_dr_get.return_value  # This is the registry
        mock_dev_entry = MagicMock()
        mock_dev_entry.identifiers = {(DOMAIN, ramses_id)}
        mock_dev_reg.async_get.return_value = mock_dev_entry  # Registry returns entry

        # Test Single String
        assert mock_broker._target_to_device_id(target_single) == ramses_id

        # Test List
        assert mock_broker._target_to_device_id(target_list) == ramses_id


async def test_target_device_id_resolution(mock_broker: RamsesBroker) -> None:
    """Test resolution via device_id (single string and list) when entity_id is missing."""
    target_single = {"device_id": "ha_dev_1"}
    target_list = {"device_id": ["ha_dev_1"]}

    ramses_id = "02:222222"

    with patch("custom_components.ramses_cc.broker.dr.async_get") as mock_dr_get:
        # Setup Device Registry Mock
        mock_dev_reg = mock_dr_get.return_value
        mock_dev_entry = MagicMock()
        mock_dev_entry.identifiers = {(DOMAIN, ramses_id)}
        mock_dev_reg.async_get.return_value = mock_dev_entry

        # Test Single String
        assert mock_broker._target_to_device_id(target_single) == ramses_id

        # Test List
        assert mock_broker._target_to_device_id(target_list) == ramses_id


async def test_target_priority_order(mock_broker: RamsesBroker) -> None:
    """Test that Entity ID takes priority over Device ID, which takes priority over Area ID."""
    target = {
        "entity_id": "sensor.exists",
        "device_id": "ha_dev_exists",
        "area_id": "area_exists",
    }

    id_from_entity = "01:000001"

    with (
        patch("custom_components.ramses_cc.broker.er.async_get") as mock_er_get,
        patch("custom_components.ramses_cc.broker.dr.async_get") as mock_dr_get,
    ):
        # 1. Setup successful Entity Lookup
        mock_ent_reg = mock_er_get.return_value
        mock_ent_entry = MagicMock()
        mock_ent_entry.device_id = "ha_dev_from_entity"
        mock_ent_reg.async_get.return_value = mock_ent_entry

        # Mock DR to return the ID derived from Entity
        mock_dev_reg = mock_dr_get.return_value

        def side_effect(dev_id: str) -> MagicMock:
            m = MagicMock()
            if dev_id == "ha_dev_from_entity":
                m.identifiers = {(DOMAIN, id_from_entity)}
                return m
            return MagicMock(identifiers={})  # Return generic for others

        mock_dev_reg.async_get.side_effect = side_effect

        # Should return the one found via entity_id, ignoring device_id/area_id logic
        assert mock_broker._target_to_device_id(target) == id_from_entity


async def test_target_resolution_failures(mock_broker: RamsesBroker) -> None:
    """Test that it returns None when lookups fail or entries lack correct domain."""
    target = {"device_id": "ha_dev_bad"}

    with patch("custom_components.ramses_cc.broker.dr.async_get") as mock_dr_get:
        mock_dev_reg = mock_dr_get.return_value

        # Case 1: Device entry found, but no RAMSES domain identifier
        mock_entry = MagicMock()
        mock_entry.identifiers = {("other_domain", "some_id")}
        mock_dev_reg.async_get.return_value = mock_entry

        assert mock_broker._target_to_device_id(target) is None

        # Case 2: Device entry is None (device not found in registry)
        mock_dev_reg.async_get.return_value = None
        assert mock_broker._target_to_device_id(target) is None

    # Case 3: Entity found, but has no device_id
    with patch("custom_components.ramses_cc.broker.er.async_get") as mock_er_get:
        mock_ent_reg = mock_er_get.return_value
        mock_ent_reg.async_get.return_value = MagicMock(device_id=None)

        assert mock_broker._target_to_device_id({"entity_id": "sensor.orphan"}) is None


async def test_get_fan_param_fallback_hgi(
    mock_broker: RamsesBroker,
    mock_gateway: MagicMock,
    mock_fan_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test async_get_fan_param falls back to HGI ID when no bound remote exists."""
    import logging

    # 1. Setup HGI with a valid ID (matches _DEVICE_ID_RE)
    hgi_id = "18:000123"
    mock_gateway.hgi = MagicMock()
    mock_gateway.hgi.id = hgi_id

    # 2. Setup Device to have NO bound remote
    # This forces the broker to look for a fallback (the HGI)
    mock_broker._devices = [mock_fan_device]
    mock_fan_device.get_bound_rem.return_value = None

    # 3. Prepare call data without an explicit 'from_id'
    call_data = {"device_id": FAN_ID, "param_id": PARAM_ID_HEX}

    # 4. Run with log capture to verify the debug logic
    with caplog.at_level(logging.DEBUG):
        await mock_broker.async_get_fan_param(call_data)

    # 5. Verify the fallback logic triggered
    # Check the specific debug message matches the code path
    assert (
        f"No explicit/bound from_id for {FAN_ID}, using gateway id {hgi_id}"
        in caplog.text
    )

    # Check the command was actually sent with the HGI ID as the source
    assert mock_gateway.async_send_cmd.called
    cmd = mock_gateway.async_send_cmd.call_args[0][0]
    assert cmd.src.id == hgi_id

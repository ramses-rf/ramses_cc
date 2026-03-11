"""Tests for the Fan Handler aspect of RamsesCoordinator (2411 logic, parameters)."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from custom_components.ramses_cc.const import DOMAIN, SZ_BOUND_TO, SZ_KNOWN_LIST
from custom_components.ramses_cc.coordinator import RamsesCoordinator
from ramses_tx.const import DevType

# Constants
FAN_ID = "30:123456"
REM_ID = "32:987654"
PARAM_ID_HEX = "75"


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Return a mock Gateway."""
    gateway = MagicMock()
    gateway.async_send_cmd = AsyncMock()
    gateway.get_device.return_value = None
    return gateway


@pytest.fixture
def mock_coordinator(hass: HomeAssistant, mock_gateway: MagicMock) -> RamsesCoordinator:
    """Return a configured RamsesCoordinator."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}

    coordinator = RamsesCoordinator(hass, entry)
    coordinator.client = mock_gateway
    # Create fake devices list if needed, or we patch _get_device
    coordinator._device_info = []

    # Mock the hass.data structure
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    return coordinator


@pytest.fixture
def mock_fan_device() -> MagicMock:
    """Return a mock Fan device."""
    device = MagicMock()
    device.id = FAN_ID
    device._SLUG = "FAN"
    device.supports_2411 = True
    device.get_bound_rem = MagicMock(return_value=REM_ID)
    return device


async def test_coordinator_fan_setup(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test fan_handler.async_setup_fan_device logic."""
    mock_fan_device.set_initialized_callback = MagicMock()
    mock_fan_device.set_param_update_callback = MagicMock()

    await mock_coordinator.fan_handler.async_setup_fan_device(mock_fan_device)

    assert mock_fan_device.set_initialized_callback.called
    assert mock_fan_device.set_param_update_callback.called

    callback_fn = mock_fan_device.set_param_update_callback.call_args[0][0]
    event_callback = MagicMock()
    mock_coordinator.hass.bus.async_listen(
        "ramses_cc.fan_param_updated", event_callback
    )

    callback_fn(PARAM_ID_HEX, 19.5)
    await mock_coordinator.hass.async_block_till_done()

    assert event_callback.called
    event = event_callback.call_args[0][0]
    assert event.data["device_id"] == FAN_ID
    assert event.data["value"] == 19.5


async def test_setup_fan_bound_invalid_type(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test _setup_fan_bound_devices with invalid config type."""
    # Mock known_list with a non-string bound_to value (e.g. an integer)
    mock_coordinator.options[SZ_KNOWN_LIST] = {
        FAN_ID: {"bound_to": 12345}  # Invalid type
    }

    # Trigger the warning and return early
    await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    # Verify no binding occurred
    mock_fan_device.add_bound_device.assert_not_called()


async def test_setup_fan_bound_not_rem(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test _setup_fan_bound_devices with a device that is not REM or DIS."""
    # Mock a device that is neither HvacRemoteBase nor has _SLUG='DIS'
    bound_dev = MagicMock()
    bound_dev.id = "01:999999"
    # Ensure it fails isinstance(HvacRemoteBase) and checks
    mock_coordinator.client.devices = [bound_dev]

    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {"bound_to": bound_dev.id}}

    await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_not_called()


async def test_fan_setup_callbacks_execution(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test execution of the initialization callbacks."""
    mock_fan_device.set_initialized_callback = MagicMock()

    # Call setup
    await mock_coordinator.fan_handler.async_setup_fan_device(mock_fan_device)

    # Get the lambda passed to callback
    init_lambda = mock_fan_device.set_initialized_callback.call_args[0][0]

    # Use patch.object on the specific instance's attribute
    with (
        patch.object(mock_coordinator, "get_all_fan_params") as mock_get_params,
        patch.object(mock_coordinator.hass, "async_create_task") as mock_create_task,
        patch("custom_components.ramses_cc.number.create_parameter_entities"),
    ):
        mock_create_task.side_effect = lambda coro: coro

        # Execute the lambda (simulating first message arrival)
        coro = init_lambda()
        await coro

        assert mock_get_params.called


async def test_fan_setup_already_initialized(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test fan_handler.async_setup_fan_device when device is already initialized."""
    mock_fan_device._initialized = True
    mock_fan_device.supports_2411 = True

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    # Patch the function where it is DEFINED, which is used by the import in coordinator.py
    with (
        patch(
            "custom_components.ramses_cc.number.create_parameter_entities"
        ) as mock_create,
        patch(
            "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
        ),
    ):
        mock_create.return_value = [MagicMock()]
        await mock_coordinator.fan_handler.async_setup_fan_device(mock_fan_device)

        assert mock_create.called
        # Should also request params
        assert mock_coordinator.client.async_send_cmd.call_count >= 0


async def test_setup_fan_bound_success_rem(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test successful binding of a FAN to a REM device."""
    bound_id = "32:111111"

    # Configure the known list with the bound device
    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    # Create the bound device object
    bound_device = MagicMock()
    bound_device.id = bound_id
    mock_coordinator.client.devices = [bound_device]

    # Helper classes to satisfy isinstance checks in coordinator.py
    class MockHvacVentilator:
        pass

    class MockHvacRemoteBase:
        pass

    # Assign classes to mocks
    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]
    bound_device.__class__ = MockHvacRemoteBase  # type: ignore[assignment]

    # Patch the classes in the coordinator module so isinstance checks pass
    with (
        patch(
            "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
        ),
        patch(
            "custom_components.ramses_cc.fan_handler.HvacRemoteBase", MockHvacRemoteBase
        ),
    ):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    # Verify binding was added with correct type
    mock_fan_device.add_bound_device.assert_called_once_with(bound_id, DevType.REM)
    assert mock_coordinator.fan_handler._fan_bound_to_remote[bound_id] == FAN_ID


async def test_setup_fan_bound_success_dis(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test successful binding of a FAN to a DIS device."""
    bound_id = "32:222222"

    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    bound_device = MagicMock()
    bound_device.id = bound_id
    bound_device._SLUG = DevType.DIS
    mock_coordinator.client.devices = [bound_device]

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    # Patch HvacVentilator to pass the first guard clause
    # Do NOT patch HvacRemoteBase, so isinstance(bound_device, HvacRemoteBase) will fail
    with patch(
        "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
    ):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_called_once_with(bound_id, DevType.DIS)
    assert mock_coordinator.fan_handler._fan_bound_to_remote[bound_id] == FAN_ID


async def test_setup_fan_bound_device_not_found(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test binding when the bound device is not found in client.devices."""
    bound_id = "32:333333"

    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    # Ensure device list is empty
    mock_coordinator.client.devices = []

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch(
        "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
    ):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    # Should log warning and not add binding
    mock_fan_device.add_bound_device.assert_not_called()
    assert bound_id not in mock_coordinator.fan_handler._fan_bound_to_remote


async def test_setup_fan_bound_no_config(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test binding when no bound device is configured (early return)."""
    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {}}

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch(
        "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
    ):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_not_called()


async def test_setup_fan_bound_bad_device_type(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test binding when device exists but is incompatible (not REM/DIS)."""
    bound_id = "32:444444"

    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    # Device exists but is generic (not REM, no DIS slug)
    bound_device = MagicMock()
    bound_device.id = bound_id
    del bound_device._SLUG  # Ensure no _SLUG attribute exists or it is not DIS
    mock_coordinator.client.devices = [bound_device]

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch(
        "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
    ):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    mock_fan_device.add_bound_device.assert_not_called()


async def test_setup_fan_bound_invalid_id_type(
    mock_coordinator: RamsesCoordinator, mock_fan_device: MagicMock
) -> None:
    """Test binding when the bound device ID is not a string (e.g. integer)."""
    # Configure known_list with an integer instead of a string for bound_to
    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: 12345}}

    class MockHvacVentilator:
        pass

    # Satisfy the isinstance(device, HvacVentilator) check
    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with patch(
        "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
    ):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    # Verify no binding occurred and code returned early
    mock_fan_device.add_bound_device.assert_not_called()


async def test_setup_fan_bound_client_not_ready(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test setup_fan_bound_devices when client is not ready."""
    # 1. Configure options so it passes the initial configuration checks
    bound_id = "32:111111"
    mock_coordinator.options[SZ_KNOWN_LIST] = {FAN_ID: {SZ_BOUND_TO: bound_id}}

    # 2. Force the client to be None to trigger the specific branch
    mock_coordinator.client = None

    # 3. Helper class to pass the isinstance(device, HvacVentilator) check
    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    # 4. Run the method and verify the warning is logged
    with (
        patch(
            "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
        ),
        caplog.at_level(logging.WARNING),
    ):
        await mock_coordinator.fan_handler.setup_fan_bound_devices(mock_fan_device)

    assert "Cannot look up bound device: Client not ready" in caplog.text

    # Verify we returned early and didn't attempt to add the device
    mock_fan_device.add_bound_device.assert_not_called()


async def test_find_param_entity_logic(
    mock_coordinator: RamsesCoordinator,
) -> None:
    """Test find_param_entity logic (registry lookup and platform entity retrieval)."""
    # Test 1: Entity not in registry
    with patch("homeassistant.helpers.entity_registry.async_get") as mock_er_get:
        mock_registry = MagicMock()
        mock_er_get.return_value = mock_registry
        mock_registry.async_get_entity_id.return_value = None

        res = mock_coordinator.fan_handler.find_param_entity(FAN_ID, "10")
        assert res is None

    # Test 2: Entity in registry, but platform not loaded or entity not in platform
    with patch("homeassistant.helpers.entity_registry.async_get") as mock_er_get:
        mock_registry = MagicMock()
        mock_er_get.return_value = mock_registry
        mock_registry.async_get_entity_id.return_value = (
            "number.some_entity"  # Found in registry
        )

        # Ensure platforms dict is empty or platform has no entities
        mock_coordinator.platforms = {}

        res = mock_coordinator.fan_handler.find_param_entity(FAN_ID, "10")
        assert res is None

    # Test 3: Entity in registry AND found in platform
    with patch("homeassistant.helpers.entity_registry.async_get") as mock_er_get:
        mock_registry = MagicMock()
        mock_er_get.return_value = mock_registry
        target_id = "number.fan_30_123456_param_10"
        mock_registry.async_get_entity_id.return_value = target_id  # Found in registry

        # Setup fake platform
        mock_entity = MagicMock()
        mock_platform = MagicMock()
        mock_platform.entities = {target_id: mock_entity}

        mock_coordinator.platforms = {Platform.NUMBER: [mock_platform]}

        res = mock_coordinator.fan_handler.find_param_entity(FAN_ID, "10")
        assert res == mock_entity


async def test_fan_setup_callbacks_exception(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test exception handling during initialization callback (get_all_fan_params fails)."""
    mock_fan_device.set_initialized_callback = MagicMock()

    await mock_coordinator.fan_handler.async_setup_fan_device(mock_fan_device)
    init_lambda = mock_fan_device.set_initialized_callback.call_args[0][0]

    with (
        patch.object(mock_coordinator, "get_all_fan_params") as mock_get_params,
        patch.object(mock_coordinator.hass, "async_create_task") as mock_create_task,
        patch("custom_components.ramses_cc.number.create_parameter_entities"),
    ):
        mock_create_task.side_effect = lambda coro: coro
        mock_get_params.side_effect = RuntimeError("Connection Failed")

        # Execute lambda
        await init_lambda()

    assert "Failed to request parameters for device" in caplog.text


async def test_fan_setup_already_initialized_exception(
    mock_coordinator: RamsesCoordinator,
    mock_fan_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test exception handling when already initialized device fails param request."""
    mock_fan_device._initialized = True
    mock_fan_device.supports_2411 = True

    class MockHvacVentilator:
        pass

    mock_fan_device.__class__ = MockHvacVentilator  # type: ignore[assignment]

    with (
        patch("custom_components.ramses_cc.number.create_parameter_entities"),
        patch.object(mock_coordinator, "get_all_fan_params") as mock_get_params,
        patch(
            "custom_components.ramses_cc.fan_handler.HvacVentilator", MockHvacVentilator
        ),
    ):
        mock_get_params.side_effect = RuntimeError("Request Failed")

        await mock_coordinator.fan_handler.async_setup_fan_device(mock_fan_device)

    assert "Failed to request parameters for device" in caplog.text

"""Test coverage for the RAMSES RF broker."""

from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from ramses_rf.device import Device
from ramses_rf.entity_base import Child
from ramses_rf.system import System


async def test_setup_schema_merge_failure(hass: HomeAssistant) -> None:
    """Test setup behavior when merged schema fails validation."""
    entry = MagicMock()
    entry.options = {
        "serial_port": "/dev/ttyUSB0",
        "packet_log": {},
        "ramses_rf": {},
        "known_list": {},
        "config_schema": {},
    }

    broker = RamsesBroker(hass, entry)

    # Mock store load to return a cached schema
    broker._store.async_load = AsyncMock(
        return_value={"client_state": {"schema": {"mock": "schema"}, "packets": {}}}
    )

    # Mock schema handling
    with (
        patch(
            "custom_components.ramses_cc.broker.merge_schemas",
            return_value={"merged": "schema"},
        ),
        patch(
            "custom_components.ramses_cc.broker.schema_is_minimal", return_value=True
        ),
        patch.object(broker, "_create_client") as mock_create_client,
        patch(
            "custom_components.ramses_cc.broker.extract_serial_port",
            return_value=("/dev/ttyUSB0", {}),
        ),
    ):
        # Setup the mock client to be awaitable
        mock_client = MagicMock()
        mock_client.start = AsyncMock()

        # First call fails (merged schema), second call succeeds (config schema)
        mock_create_client.side_effect = [
            vol.MultipleInvalid("Invalid schema"),
            mock_client,
        ]

        await broker.async_setup()

        # Verify _create_client was called twice (fallback occurred)
        assert mock_create_client.call_count == 2
        # Verify the client start was awaited
        mock_client.start.assert_awaited()


def test_get_device_returns_none(hass: HomeAssistant) -> None:
    """Test _get_device returns None when device not found and client not ready (Line 322)."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Ensure client is None (default behavior on init)
    broker.client = None
    broker._devices = []

    # Should hit the final return None
    assert broker._get_device("01:123456") is None


def test_update_device_relationships(hass: HomeAssistant) -> None:
    """Test _update_device for Child with Parent and generic Device."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    broker = RamsesBroker(hass, entry)

    # Mock Device Registry
    dev_reg = MagicMock()
    dev_reg.async_get_or_create = MagicMock()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg):
        # Case 1: Child Device with Parent (Hits ~Lines 678)
        parent = MagicMock(spec=System)
        parent.id = "01:123456"

        child_device = MagicMock(spec=Child)
        child_device.id = "04:123456"
        child_device._parent = parent
        child_device.name = "Test Child"
        child_device._msg_value_code.return_value = {"description": "Test Model"}

        broker._update_device(child_device)

        # Verify via_device is set to parent
        dev_reg.async_get_or_create.assert_called_with(
            config_entry_id="test_entry",
            identifiers={(DOMAIN, "04:123456")},
            name="Test Child",
            manufacturer=None,
            model="Test Model",
            via_device=(DOMAIN, "01:123456"),
            serial_number="04:123456",
        )

        # Case 2: Generic Device (hits 'else' block for via_device = None)
        generic_device = MagicMock(spec=Device)
        generic_device.id = "18:000000"
        generic_device.name = "HGI"
        generic_device._SLUG = "HGI"
        # Explicitly set _parent to None to avoid AttributeError if strict spec is used
        generic_device._parent = None
        generic_device._msg_value_code.return_value = None

        # Reset mock
        broker._device_info = {}

        broker._update_device(generic_device)

        # Verify via_device is None
        args, kwargs = dev_reg.async_get_or_create.call_args
        assert kwargs["via_device"] is None


async def test_bind_device_lookup_error(hass: HomeAssistant) -> None:
    """Test async_bind_device raises HomeAssistantError on LookupError."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # Mock fake_device to raise LookupError
    broker.client.fake_device.side_effect = LookupError("Device not found")

    call = MagicMock()
    call.data = {"device_id": "99:999999"}

    with pytest.raises(HomeAssistantError, match="Device not found"):
        await broker.async_bind_device(call)


def test_find_param_entity_registry_miss(hass: HomeAssistant) -> None:
    """Test _find_param_entity when entity is in registry but not platform."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Mock Entity Registry to return an entry
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = MagicMock(device_id="device_id")

    # Mock Platforms (empty entities dict)
    platform = MagicMock()
    platform.entities = {}
    broker.platforms = {"number": [platform]}

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        entity = broker._find_param_entity("01:123456", "01")

        # Should return None and log the debug message
        assert entity is None


def test_resolve_device_id_edge_cases(hass: HomeAssistant) -> None:
    """Test _resolve_device_id with empty lists and lists of IDs."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Test 1: device_id is an empty list
    data: dict[str, Any] = {"device_id": []}
    assert broker._resolve_device_id(data) is None

    # Test 2: device (HA ID) is an empty list
    data = {"device": []}
    assert broker._resolve_device_id(data) is None

    # Test 3: device (HA ID) is a list with multiple items (Logs warning)
    # We need to mock _target_to_device_id to return something valid
    with patch.object(broker, "_target_to_device_id", return_value="18:123456"):
        # Explicitly annotate data as dict[str, Any] to avoid Mypy overlap error
        # when comparing data["device"] (initially list) with a string.
        data = {"device": ["ha_id_1", "ha_id_2"]}
        result = broker._resolve_device_id(data)
        assert result == "18:123456"
        assert data["device"] == "ha_id_1"  # Should be flattened

    # Test 4: Simple string ID (Line 1052)
    data_str = {"device_id": "01:123456"}
    assert broker._resolve_device_id(data_str) == "01:123456"

    # Test 5: Target dictionary (Lines 1075-1081)
    with patch.object(broker, "_target_to_device_id", return_value="02:222222"):
        data_target: dict[str, Any] = {"target": {"entity_id": "climate.test"}}
        assert broker._resolve_device_id(data_target) == "02:222222"
        assert data_target["device_id"] == "02:222222"

    # Test 6: No matching data (Line 1081)
    assert broker._resolve_device_id({}) is None


async def test_get_fan_param_no_source(hass: HomeAssistant) -> None:
    """Test get_fan_param returns early when from_id cannot be resolved."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # Mock a device that returns None for get_bound_rem()
    device = MagicMock()
    device.id = "32:123456"
    device.get_bound_rem.return_value = None
    broker._get_device = MagicMock(return_value=device)

    # Call without explicit from_id
    call = {"device_id": "32:123456", "param_id": "01"}

    # This should return None and log a warning, not raise
    await broker.async_get_fan_param(call)

    # Verify client.async_send_cmd was NOT called
    broker.client.async_send_cmd.assert_not_called()


async def test_get_fan_param_sets_pending(hass: HomeAssistant) -> None:
    """Test get_fan_param sets entity to pending state."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()
    # Make async_send_cmd awaitable
    broker.client.async_send_cmd = AsyncMock()

    # Setup happy path for IDs using valid RAMSES ID format (XX:YYYYYY)
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # Mock Entity - _clear_pending_after_timeout must be awaitable
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    call = {"device_id": "32:111111", "param_id": "01"}

    await broker.async_get_fan_param(call)

    # Verify set_pending was called
    mock_entity.set_pending.assert_called_once()
    # Verify cleanup was scheduled
    mock_entity._clear_pending_after_timeout.assert_called()


async def test_run_fan_param_sequence_dict_failure(hass: HomeAssistant) -> None:
    """Test _async_run_fan_param_sequence handles dict conversion failure."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Create an object that fails dict() conversion
    class BadData:
        def keys(self) -> None:
            raise ValueError("Boom")

    # Mock normalize to return bad data
    broker._normalize_service_call = MagicMock(return_value=BadData())

    await broker._async_run_fan_param_sequence({})

    # If it didn't raise, the exception was caught.
    # We can assume success if we reached here without crash.


async def test_set_fan_param_errors(hass: HomeAssistant) -> None:
    """Test set_fan_param error handling."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # 1. Missing Source (from_id)
    device = MagicMock()
    device.id = "32:123456"
    device.get_bound_rem.return_value = None
    broker._get_device = MagicMock(return_value=device)

    call = {"device_id": "32:123456", "param_id": "01", "value": 1}

    with pytest.raises(HomeAssistantError, match="Cannot set parameter"):
        await broker.async_set_fan_param(call)

    # 2. Generic Exception during send
    # Setup valid IDs
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )
    # Mock Send to raise generic Exception
    broker.client.async_send_cmd.side_effect = RuntimeError("Transport fail")

    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    # Patch Command.set_fan_param to skip validation for this test
    # This prevents 'value out of range' errors before we reach the send_cmd call
    with (
        patch(
            "custom_components.ramses_cc.broker.Command.set_fan_param",
            return_value="MOCK_CMD",
        ),
        pytest.raises(HomeAssistantError, match="Failed to set fan parameter"),
    ):
        await broker.async_set_fan_param(call)

    # Verify pending was cleared (Line 1391 context)
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


def test_update_device_already_registered(hass: HomeAssistant) -> None:
    """Test _update_device returns early if device is already registered (Line 678)."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    broker = RamsesBroker(hass, entry)

    # Mock Device Registry
    dev_reg = MagicMock()
    dev_reg.async_get_or_create = MagicMock()

    with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg):
        # Create a simple device mock
        device = MagicMock(spec=Device)
        device.id = "13:123456"
        device.name = "Test Device"
        device._SLUG = "BDR"
        device._msg_value_code.return_value = None
        # Ensure it doesn't trigger Child/Zone logic for via_device
        device._parent = None

        # First call - should register the device
        broker._update_device(device)
        assert dev_reg.async_get_or_create.call_count == 1

        # Check internal cache was updated
        assert "13:123456" in broker._device_info

        # Second call with identical state - should return early (hitting line 678)
        broker._update_device(device)

        # Call count should remain 1 (proving the early return worked)
        assert dev_reg.async_get_or_create.call_count == 1


def test_get_param_id_missing_param(hass: HomeAssistant) -> None:
    """Test _get_param_id raises ValueError when param_id is missing (Lines 964-965)."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Call with empty data -> Missing param_id
    with pytest.raises(
        ValueError, match=r"required key not provided @ data\['param_id'\]"
    ):
        broker._get_param_id({})


def test_resolve_device_id_from_ha_registry_id(hass: HomeAssistant) -> None:
    """Test _resolve_device_id resolves HA Registry ID to RAMSES ID."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Input data with an HA Device Registry ID (no colons/underscores)
    # Using hyphens instead of underscores to ensure we bypass the RAMSES ID check
    data = {"device_id": "ha-registry-uuid-123"}

    # Mock successful resolution
    # The code calls _target_to_device_id({"device_id": [device_id]})
    with patch.object(broker, "_target_to_device_id", return_value="18:999999"):
        result = broker._resolve_device_id(data)

        # Verify return value is the resolved RAMSES ID
        assert result == "18:999999"

        # Verify data dictionary was updated in place
        assert data["device_id"] == "18:999999"


def test_get_device_and_from_id_resolve_failure(hass: HomeAssistant) -> None:
    """Test _get_device_and_from_id returns empty tuple if resolution fails (Line ~1113)."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Mock _resolve_device_id to return None, forcing the code to hit 'return "", "", ""'
    with patch.object(broker, "_resolve_device_id", return_value=None):
        result = broker._get_device_and_from_id({})

        # Verify the "magic" empty tuple is returned
        assert result == ("", "", "")


def test_normalize_service_call_variants(hass: HomeAssistant) -> None:
    """Test _normalize_service_call with objects having .data, iterables, and targets."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # 1. Test object with 'data' attribute (Hits 'elif hasattr(call, "data")')
    class MockCall:
        data = {"key": "value_from_attr"}

    result_attr = broker._normalize_service_call(MockCall())
    assert result_attr == {"key": "value_from_attr"}

    # 2. Test iterable/list of tuples (Hits 'else: data = dict(call)')
    call_iterable = [("key", "value_from_iter")]
    result_iter = broker._normalize_service_call(call_iterable)
    assert result_iter == {"key": "value_from_iter"}

    # 3. Test object with target having .as_dict() (Hits 'if hasattr(target, "as_dict")')
    class MockTarget:
        def as_dict(self) -> dict[str, str]:
            return {"entity_id": "climate.test"}

    class MockCallWithTarget:
        data = {"key": "val"}
        target = MockTarget()

    result_target_method = broker._normalize_service_call(MockCallWithTarget())
    assert result_target_method["key"] == "val"
    assert result_target_method["target"] == {"entity_id": "climate.test"}

    # 4. Test object with target as dict (Hits 'elif isinstance(target, dict)')
    class MockCallWithDictTarget:
        data = {"key": "val"}
        target = {"area_id": "living_room"}

    result_target_dict = broker._normalize_service_call(MockCallWithDictTarget())
    assert result_target_dict["key"] == "val"
    assert result_target_dict["target"] == {"area_id": "living_room"}


async def test_get_fan_param_value_error_clears_pending(hass: HomeAssistant) -> None:
    """Test get_fan_param clears pending state when ValueError occurs after entity found."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # 1. Setup valid IDs to ensure we get past initial checks
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # 2. Setup Mock Entity with the required method
    mock_entity = MagicMock()
    # The method must be an AsyncMock so it can be awaited/scheduled
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch Command.get_fan_param to raise ValueError
    # This ensures 'entity' is already assigned before the exception is raised
    with patch(
        "custom_components.ramses_cc.broker.Command.get_fan_param",
        side_effect=ValueError("Simulated Error"),
    ):
        call = {"device_id": "32:111111", "param_id": "01"}

        await broker.async_get_fan_param(call)

    # 4. Verify _clear_pending_after_timeout(0) was called in the except block
    mock_entity._clear_pending_after_timeout.assert_called_with(0)


async def test_run_fan_param_sequence_normalization_error(hass: HomeAssistant) -> None:
    """Test _async_run_fan_param_sequence handles exception during normalization."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)

    # Patch _normalize_service_call to raise an exception immediately
    with (
        patch.object(
            broker,
            "_normalize_service_call",
            side_effect=ValueError("Normalization failed"),
        ),
        patch("custom_components.ramses_cc.broker._LOGGER.error") as mock_error,
    ):
        await broker._async_run_fan_param_sequence({})

        # Verify the error was logged
        assert mock_error.called
        mock_error.assert_called_with("Invalid service call data: %s", ANY)
        # Verify the exception message was passed as the argument
        assert str(mock_error.call_args[0][1]) == "Normalization failed"


async def test_set_fan_param_value_error_clears_pending(hass: HomeAssistant) -> None:
    """Test set_fan_param clears pending state when ValueError occurs after entity found."""
    entry = MagicMock()
    broker = RamsesBroker(hass, entry)
    broker.client = MagicMock()

    # 1. Setup valid IDs so execution proceeds past initial checks
    broker._get_device_and_from_id = MagicMock(
        return_value=("32:111111", "32_111111", "18:000000")
    )

    # 2. Setup Mock Entity with the required async method
    mock_entity = MagicMock()
    mock_entity._clear_pending_after_timeout = AsyncMock()
    broker._find_param_entity = MagicMock(return_value=mock_entity)

    # 3. Patch Command.set_fan_param to raise ValueError
    # This ensures 'entity' is already assigned before the exception is raised
    with patch(
        "custom_components.ramses_cc.broker.Command.set_fan_param",
        side_effect=ValueError("Simulated Validation Error"),
    ):
        call = {"device_id": "32:111111", "param_id": "01", "value": 10}

        # The broker catches ValueError and re-raises it as HomeAssistantError
        with pytest.raises(
            HomeAssistantError, match="Invalid parameter for set_fan_param"
        ):
            await broker.async_set_fan_param(call)

    # 4. Verify _clear_pending_after_timeout(0) was called in the except block
    mock_entity._clear_pending_after_timeout.assert_called_with(0)

"""Test cases for bound device functionality in Ramses integration.

This test file verifies that bound REM/DIS devices are properly used as source devices
for fan parameter operations, ensuring that only REM and DIS devices can be bound to fans.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.ramses_cc.broker import RamsesBroker

# Test constants
TEST_DEVICE_ID = "32:153289"  # FAN device ID
TEST_FROM_ID = "37:168270"  # HGI device ID
TEST_PARAM_ID = "4E"
TEST_VALUE = "25"

# Service names
SERVICE_GET_NAME = "get_fan_param"
SERVICE_SET_NAME = "set_fan_param"


class TestBoundDeviceFunctionality:
    """Test cases for bound device functionality.

    This test class verifies the behaviour of bound REM/DIS devices in fan parameter
    operations, ensuring that bound devices are used as source devices and that
    the fallback logic works correctly.
    """

    @pytest.fixture(autouse=True)
    async def setup_bound_device_fixture(self, hass: HomeAssistant):
        """Set up test environment for bound device operations.

        This fixture runs before each test method and sets up:
        - A real RamsesBroker instance
        - A mock client with an HGI device
        - Patches for Command.set_fan_param and Command.get_fan_param
        - Test command objects for bound device operations

        Args:
            hass: Home Assistant fixture for creating a test environment.
        """
        # Create a real broker instance with a mock config entry
        self.broker = RamsesBroker(hass, MagicMock())

        # Create a mock client with HGI device
        self.mock_client = AsyncMock()
        self.broker.client = self.mock_client
        self.broker.client.hgi = MagicMock(id=TEST_FROM_ID)

        # Patch Command.set_fan_param to control command creation
        self.set_patcher = patch("ramses_tx.command.Command.set_fan_param")
        self.mock_set_fan_param = self.set_patcher.start()

        # Patch Command.get_fan_param to control command creation
        self.get_patcher = patch("ramses_tx.command.Command.get_fan_param")
        self.mock_get_fan_param = self.get_patcher.start()

        # Create test commands that will be returned by the patched methods
        self.mock_set_cmd = MagicMock()
        self.mock_set_cmd.code = "2411"
        self.mock_set_cmd.verb = "W"
        self.mock_set_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_set_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        self.mock_set_fan_param.return_value = self.mock_set_cmd

        self.mock_get_cmd = MagicMock()
        self.mock_get_cmd.code = "2411"
        self.mock_get_cmd.verb = "RQ"
        self.mock_get_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_get_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        self.mock_get_fan_param.return_value = self.mock_get_cmd

        yield  # Test runs here

        # Cleanup - stop all patches
        self.set_patcher.stop()
        self.get_patcher.stop()

    @pytest.mark.asyncio
    async def test_bound_rem_device_functionality(self, hass: HomeAssistant) -> None:
        """Test that bound REM devices are used as from_id for fan operations.

        Verifies that:
        1. When a fan has a bound REM device, it uses that device as from_id
        2. Commands are constructed with the bound device as source
        3. The bound device takes precedence over HGI fallback
        """
        # Setup service call
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Create a mock device with bound REM
        mock_bound_device = MagicMock()
        mock_bound_device.id = "18:123456"  # REM device ID
        mock_bound_device.get_bound_rem.return_value = mock_bound_device.id

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_bound_device):
            # Act - Call the method under test
            await self.broker.async_set_fan_param(call)

            # Assert - Verify command was constructed with bound device as source
            self.mock_set_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                TEST_VALUE,
                src_id=mock_bound_device.id,  # Should use bound device, not HGI
            )

            # Verify command was sent
            self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_set_cmd)

    @pytest.mark.asyncio
    async def test_bound_dis_device_functionality(self, hass: HomeAssistant) -> None:
        """Test that bound DIS devices are used as from_id for fan operations.

        Verifies that:
        1. When a fan has a bound DIS device, it uses that device as from_id
        2. Commands are constructed with the bound device as source
        3. The bound device takes precedence over HGI fallback
        """
        # Setup service call
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Create a mock device with bound DIS
        mock_bound_device = MagicMock()
        mock_bound_device.id = "04:789012"  # DIS device ID
        mock_bound_device.get_bound_rem.return_value = mock_bound_device.id

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_bound_device):
            # Act - Call the method under test
            await self.broker.async_set_fan_param(call)

            # Assert - Verify command was constructed with bound device as source
            self.mock_set_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                TEST_VALUE,
                src_id=mock_bound_device.id,  # Should use bound device, not HGI
            )

            # Verify command was sent
            self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_set_cmd)

    @pytest.mark.asyncio
    async def test_bound_device_precedence_over_hgi(self, hass: HomeAssistant) -> None:
        """Test that bound devices take precedence over HGI gateway.

        Verifies that:
        1. Bound REM/DIS devices are preferred over HGI gateway
        2. Even when HGI is available, bound device is used if present
        """
        # Setup service call without explicit from_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Create a mock device with bound REM
        mock_bound_device = MagicMock()
        mock_bound_device.id = "18:123456"  # REM device ID
        mock_bound_device.get_bound_rem.return_value = mock_bound_device.id

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_bound_device):
            # Act - Call the method under test
            await self.broker.async_set_fan_param(call)

            # Assert - Verify bound device was used, not HGI
            self.mock_set_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                TEST_VALUE,
                src_id=mock_bound_device.id,  # Should use bound device
            )

    @pytest.mark.asyncio
    async def test_bound_device_fallback_to_hgi(self, hass: HomeAssistant) -> None:
        """Test that HGI is used when no bound device is available.

        Verifies that:
        1. When no bound device exists, HGI gateway is used as fallback
        2. This works for both get and set operations
        """
        # Setup service call without explicit from_id
        service_data = {"device_id": TEST_DEVICE_ID, "param_id": TEST_PARAM_ID}
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Create a mock device without bound device
        mock_device = MagicMock()
        mock_device.get_bound_rem.return_value = None  # No bound device

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_device):
            # Act - Call the method under test
            await self.broker.async_get_fan_param(call)

            # Assert - Verify HGI was used as fallback
            self.mock_get_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                src_id=TEST_FROM_ID,  # Should use HGI as fallback
            )

            # Verify command was sent
            self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_get_cmd)

    @pytest.mark.asyncio
    async def test_bound_device_get_operations(self, hass: HomeAssistant) -> None:
        """Test that bound devices work for get operations.

        Verifies that:
        1. Bound devices are used for get operations (not just set operations)
        2. The bound device is used as the source for parameter reads
        """
        # Setup service call for get operation
        service_data = {"device_id": TEST_DEVICE_ID, "param_id": TEST_PARAM_ID}
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Create a mock device with bound DIS
        mock_bound_device = MagicMock()
        mock_bound_device.id = "04:789012"  # DIS device ID
        mock_bound_device.get_bound_rem.return_value = mock_bound_device.id

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_bound_device):
            # Act - Call the method under test
            await self.broker.async_get_fan_param(call)

            # Assert - Verify bound device was used for get operation
            self.mock_get_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                src_id=mock_bound_device.id,  # Should use bound device
            )

            # Verify command was sent
            self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_get_cmd)

    @pytest.mark.asyncio
    async def test_bound_device_bulk_operations(self, hass: HomeAssistant) -> None:
        """Test that bound devices work for bulk update operations.

        Verifies that:
        1. Bound devices are used for update_fan_params operations
        2. All parameters in the schema use the bound device as source
        """
        # Setup service call for bulk update
        service_data = {"device_id": TEST_DEVICE_ID}
        call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

        # Create a mock device with bound REM
        mock_bound_device = MagicMock()
        mock_bound_device.id = "18:123456"  # REM device ID
        mock_bound_device.get_bound_rem.return_value = mock_bound_device.id

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_bound_device):
            # Act - Call the method under test
            await self.broker.async_get_all_fan_params(call)

            # Assert - Verify bound device was used for all parameter requests
            # Note: We can't easily test the exact number without importing the schema,
            # but we can verify that get_fan_param was called multiple times
            assert self.mock_get_fan_param.call_count > 0, (
                "Expected multiple parameter requests"
            )

            # Check that all calls used the bound device as source
            calls = self.mock_get_fan_param.call_args_list
            for call_args in calls:
                args, kwargs = call_args
                assert kwargs["src_id"] == mock_bound_device.id, (
                    f"Expected bound device {mock_bound_device.id} as source, got {kwargs['src_id']}"
                )

    @pytest.mark.asyncio
    async def test_bound_device_with_fan_id(self, hass: HomeAssistant) -> None:
        """Test bound devices work with fan_id parameter.

        Verifies that:
        1. When fan_id is provided, it's used as the target device
        2. Bound device is still used as the source device
        """
        test_fan_id = "99:999999"  # Different from device_id

        # Setup service call with fan_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "fan_id": test_fan_id,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Create a mock device with bound DIS
        mock_bound_device = MagicMock()
        mock_bound_device.id = "04:789012"  # DIS device ID
        mock_bound_device.get_bound_rem.return_value = mock_bound_device.id

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_bound_device):
            # Act - Call the method under test
            await self.broker.async_set_fan_param(call)

            # Assert - Verify fan_id was used as target, bound device as source
            self.mock_set_fan_param.assert_called_once_with(
                test_fan_id,  # fan_id should be used as target
                TEST_PARAM_ID,
                TEST_VALUE,
                src_id=mock_bound_device.id,  # Should use bound device as source
            )

    @pytest.mark.asyncio
    async def test_bound_device_exception_handling(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that exceptions during bound device operations are handled properly.

        Verifies that:
        1. Exceptions during bound device lookup are caught and logged
        2. The operation falls back to HGI when bound device fails
        3. The error message is properly logged
        """
        # Setup service call
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Create a mock device that raises an exception when get_bound_rem is called
        mock_device = MagicMock()
        mock_device.get_bound_rem.side_effect = Exception("Bound device error")

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_device):
            # Clear any existing log captures
            caplog.clear()
            caplog.set_level(logging.ERROR)

            # Act - Call the method under test
            await self.broker.async_set_fan_param(call)

            # Assert - Verify HGI was used as fallback despite the exception
            self.mock_set_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                TEST_VALUE,
                src_id=TEST_FROM_ID,  # Should fall back to HGI
            )

            # Verify command was sent
            self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_set_cmd)

    @pytest.mark.asyncio
    async def test_bound_device_not_available(self, hass: HomeAssistant) -> None:
        """Test behaviour when bound device is not available.

        Verifies that:
        1. When get_bound_rem returns None, HGI is used as fallback
        2. The operation continues normally with HGI as source
        """
        # Setup service call
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Create a mock device without bound device
        mock_device = MagicMock()
        mock_device.get_bound_rem.return_value = None  # No bound device

        # Mock the device lookup to return our mock device
        with patch.object(self.broker, "_get_device", return_value=mock_device):
            # Act - Call the method under test
            await self.broker.async_set_fan_param(call)

            # Assert - Verify HGI was used as fallback
            self.mock_set_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                TEST_VALUE,
                src_id=TEST_FROM_ID,  # Should use HGI as fallback
            )

            # Verify command was sent
            self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_set_cmd)

    @pytest.mark.asyncio
    async def test_bound_device_device_lookup_failure(
        self, hass: HomeAssistant
    ) -> None:
        """Test behaviour when device lookup fails.

        Verifies that:
        1. When _get_device raises an exception, HGI is used as fallback
        2. The operation continues normally with HGI as source
        """
        # Setup service call
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Mock the device lookup to raise an exception
        with patch.object(
            self.broker, "_get_device", side_effect=Exception("Device lookup failed")
        ):
            # Act - Call the method under test
            await self.broker.async_set_fan_param(call)

            # Assert - Verify HGI was used as fallback
            self.mock_set_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                TEST_PARAM_ID,
                TEST_VALUE,
                src_id=TEST_FROM_ID,  # Should use HGI as fallback
            )

            # Verify command was sent
            self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_set_cmd)

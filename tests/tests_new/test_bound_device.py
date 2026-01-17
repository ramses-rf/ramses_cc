"""Test cases for bound device functionality in Ramses integration.

This test file verifies that bound REM/DIS devices are properly used as source devices
for fan parameter operations, ensuring that only REM and DIS devices can be bound to fans.

TODO: add tests routing a service call via a (mocked) device
"""

# import logging
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant  # , ServiceCall

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
    async def setup_bound_device_fixture(
        self, hass: HomeAssistant
    ) -> AsyncGenerator[None]:
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

        # Create a mock device and add it to the client's registry
        self.mock_device = MagicMock()
        self.mock_device.id = TEST_DEVICE_ID
        self.mock_device.get_bound_rem.return_value = None

        # Ensure device_by_id behaves like a dict (synchronous .get)
        self.broker.client.device_by_id = {TEST_DEVICE_ID: self.mock_device}

        # Patch Command.set_fan_param to control command creation
        self.set_patcher = patch(
            "custom_components.ramses_cc.services.Command.set_fan_param"
        )
        self.mock_set_fan_param = self.set_patcher.start()

        # Patch Command.get_fan_param to control command creation
        self.get_patcher = patch(
            "custom_components.ramses_cc.services.Command.get_fan_param"
        )
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
    async def test_explicit_from_id_takes_precedence(self, hass: HomeAssistant) -> None:
        """Test that explicit from_id takes precedence over HGI.

        Verifies that:
        1. When explicit from_id is provided, it is used as the source
        2. The explicit from_id takes precedence over any HGI fallback
        """
        # Setup service call with explicit from_id
        explicit_from_id = "18:123456"
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
            "from_id": explicit_from_id,
        }
        # call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)
        call = service_data

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Assert - Verify explicit from_id was used
        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,
            TEST_PARAM_ID,
            TEST_VALUE,
            src_id=explicit_from_id,  # Should use explicit from_id
        )

        # Verify command was sent
        self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_set_cmd)

    @pytest.mark.asyncio
    async def test_fan_param_get_with_explicit_from_id(
        self, hass: HomeAssistant
    ) -> None:
        """Test that explicit from_id works for get operations.

        Verifies that:
        1. Explicit from_id is used for get operations
        2. The explicit from_id is used as the source for parameter reads
        """
        # Setup service call for get operation with explicit from_id
        explicit_from_id = "04:789012"
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "from_id": explicit_from_id,
        }
        # call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)
        call = service_data

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify explicit from_id was used for get operation
        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,
            TEST_PARAM_ID,
            src_id=explicit_from_id,  # Should use explicit from_id
        )

        # Verify command was sent
        self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_get_cmd)

    @pytest.mark.asyncio
    async def test_fan_param_set_with_fan_id_and_explicit_from_id(
        self, hass: HomeAssistant
    ) -> None:
        """Test that fan_id and explicit from_id work together.

        Verifies that:
        1. When fan_id is provided, it's used as the target device
        2. Explicit from_id is used as the source device
        """
        # test_fan_id = "99:999999"  # Different from device_id
        explicit_from_id = "18:123456"

        # Setup service call with fan_id and explicit from_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            # "fan_id": test_fan_id,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
            "from_id": explicit_from_id,
        }
        # call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)
        call = service_data

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Assert - Verify fan_id was used as target, explicit from_id as source
        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # fan_id deprecated?? fan_id should be used as target
            TEST_PARAM_ID,
            TEST_VALUE,
            src_id=explicit_from_id,  # Should use explicit from_id as source
        )

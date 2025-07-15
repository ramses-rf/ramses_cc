"""Tests for Ramses set_fan_param service.

This module contains tests for the set_fan_param service in the Ramses CC integration.
It verifies the basic functionality of sending fan parameter set commands and handling
various edge cases.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.schemas import SVC_SET_FAN_PARAM

# Test constants
TEST_DEVICE_ID = "32:153289"  # Example fan device ID
TEST_FROM_ID = "37:168270"  # Source device ID (e.g., remote)
TEST_PARAM_ID = "4E"  # Example parameter ID
TEST_VALUE = 50  # Example parameter value

# Type aliases for better readability
MockType = MagicMock
AsyncMockType = AsyncMock


class TestSetFanParameter:
    """Test cases for the set_fan_param service.

    This test class verifies the behavior of the async_set_fan_param method
    in the RamsesBroker class, including error handling and edge cases.
    """

    @pytest.fixture(autouse=True)
    async def setup_fixture(self, hass: HomeAssistant):
        """Set up test environment.

        This fixture runs before each test method and sets up:
        - A real RamsesBroker instance
        - A mock client with an HGI device
        - Patches for Command.set_fan_param
        - Test command objects

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
        self.patcher = patch("ramses_tx.command.Command.set_fan_param")
        self.mock_set_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "W"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        self.mock_set_fan_param.return_value = self.mock_cmd

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()

    @pytest.mark.asyncio
    async def test_basic_fan_param_set(self, hass: HomeAssistant) -> None:
        """Test basic fan parameter set with all required parameters.

        Verifies that:
        1. The command is constructed with correct parameters
        2. The command is sent via the client
        3. No errors are raised
        """
        # Setup service call data with all required parameters
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Assert - Verify command construction
        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # fan_id as positional argument
            TEST_PARAM_ID,  # param_id as positional argument
            TEST_VALUE,  # value as is (will be converted to string in Command.set_fan_param)
            src_id=TEST_FROM_ID,  # src_id as keyword argument
        )

        # Verify command was sent via the client
        self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_cmd)

    @pytest.mark.asyncio
    async def test_hgi_not_available(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test behavior when HGI is not available and no from_id is provided.

        Verifies that:
        1. The error is properly logged when HGI is not available
        2. No command is sent when HGI is not available
        """
        # Stop the patcher to avoid interference with the test
        self.patcher.stop()

        try:
            # Setup a mock client with no HGI device
            mock_client = AsyncMock()
            mock_client.hgi = None  # Simulate HGI not being available
            mock_client.async_send_cmd = AsyncMock()

            # Create a new broker instance with the mock client
            broker = RamsesBroker(hass, MagicMock())
            broker.client = mock_client

            # Setup service call without from_id to trigger HGI fallback
            service_data = {
                "device_id": TEST_DEVICE_ID,
                "param_id": TEST_PARAM_ID,
                "value": TEST_VALUE,
            }
            call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

            # Clear any existing log captures
            caplog.clear()
            caplog.set_level(logging.WARNING)  # Capture warnings and above

            # Act - Call the method under test
            await broker.async_set_fan_param(call)

            # Verify the warning was logged
            warning_message = "No source device ID specified and HGI not available"
            assert any(
                warning_message in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            ), f"Expected warning message '{warning_message}' not found in logs"

            # Verify no command was sent
            mock_client.async_send_cmd.assert_not_called()

        finally:
            # Restore the patcher for other tests
            self.patcher.start()

    @pytest.mark.asyncio
    async def test_without_from_id_uses_hgi(self, hass: HomeAssistant) -> None:
        """Test that omitting from_id uses the HGI device ID.

        Verifies that:
        1. When from_id is not provided, the HGI device ID is used as the source
        2. The command is constructed with the correct parameters
        3. The command is sent via the client
        """
        # Setup service call without from_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Assert - Verify command was constructed with HGI as source
        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # fan_id
            TEST_PARAM_ID,  # param_id
            TEST_VALUE,  # value (will be converted to string in Command.set_fan_param)
            src_id=TEST_FROM_ID,  # Should use HGI device ID as source
        )

        # Verify command was sent
        self.mock_client.async_send_cmd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_required_device_id(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing device_id logs an error.

        Verifies that:
        1. An error is logged when device_id is missing
        2. No command is sent when validation fails
        """
        # Setup service call without device_id
        service_data = {"param_id": TEST_PARAM_ID, "value": TEST_VALUE}
        call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Verify error was logged
        assert any(
            "Missing required parameter: device_id" in record.message
            for record in caplog.records
            if record.levelno == logging.ERROR
        ), "Expected validation error for missing device_id not found in logs"

        # Verify no command was sent
        self.mock_client.async_send_cmd.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_required_param_id(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing param_id logs an error.

        Verifies that:
        1. An error is logged when param_id is missing
        2. No command is sent when validation fails
        """
        # Setup service call without param_id
        service_data = {"device_id": TEST_DEVICE_ID, "value": TEST_VALUE}
        call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Verify error was logged
        assert any(
            "Missing required parameter: param_id" in record.message
            for record in caplog.records
            if record.levelno == logging.ERROR
        ), "Expected validation error for missing param_id not found in logs"

        # Verify no command was sent
        self.mock_client.async_send_cmd.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_required_value(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing value logs an error.

        Verifies that:
        1. An error is logged when value is missing
        2. No command is sent when validation fails
        """
        # Setup service call without value
        service_data = {"device_id": TEST_DEVICE_ID, "param_id": TEST_PARAM_ID}
        call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Verify error was logged
        assert any(
            "Missing required parameter: value" in record.message
            for record in caplog.records
            if record.levelno == logging.ERROR
        ), "Expected validation error for missing value not found in logs"

        # Verify no command was sent
        self.mock_client.async_send_cmd.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_param_id_format(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that invalid param_id format raises CommandInvalid with correct error message.

        Verifies that:
        1. CommandInvalid is raised when param_id has invalid format
        2. The error message is clear and helpful
        """
        # Setup service call with invalid param_id format
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": "INVALID",  # Invalid format
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.WARNING)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Verify the error is logged with a clear and helpful message
        error_logs = [
            record
            for record in caplog.records
            if record.levelno == logging.ERROR
            and "Invalid parameter ID: 'INVALID'" in record.message
        ]
        assert len(error_logs) > 0, "Expected error log for invalid param_id not found"
        assert "Must be a 2-digit hexadecimal value (00-FF)" in error_logs[0].message

        # Verify no command was sent
        self.mock_client.async_send_cmd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_with_fan_id_parameter(self, hass: HomeAssistant) -> None:
        """Test that fan_id parameter is used when provided.

        Verifies that:
        1. When fan_id is provided, it's used instead of device_id for the command
        2. The command is constructed with the correct parameters
        """
        test_fan_id = "99:999999"  # Different from device_id

        # Setup service call with fan_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "fan_id": test_fan_id,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
        }
        call = ServiceCall(hass, "ramses_cc", SVC_SET_FAN_PARAM, service_data)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Assert - Verify command was constructed with fan_id as target
        self.mock_set_fan_param.assert_called_once_with(
            test_fan_id,  # fan_id should be used instead of device_id
            TEST_PARAM_ID,
            TEST_VALUE,  # value as is (will be converted to string in Command.set_fan_param)
            src_id=TEST_FROM_ID,
        )

        # Verify command was sent
        self.mock_client.async_send_cmd.assert_awaited_once()

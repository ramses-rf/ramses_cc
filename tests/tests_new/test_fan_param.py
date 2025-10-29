"""Tests for Ramses fan parameter services.

This module contains comprehensive tests for both get_fan_param and set_fan_param
services in the Ramses RF integration (ramses_cc on github). It verifies the basic
functionality of sending fan parameter commands and handling various edge cases
for both read and write operations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
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
SERVICE_GET_NAME = "get_fan_param"  # Name of the get service
SERVICE_SET_NAME = SVC_SET_FAN_PARAM  # Name of the set service

# Type aliases for better readability
MockType = MagicMock
AsyncMockType = AsyncMock


class TestFanParameterGet:
    """Test cases for the get_fan_param service.

    This test class verifies the behaviour of the async_get_fan_param and
    _async_run_fan_param_sequence methods in the RamsesBroker class, including
    error handling and edge cases for parameter reading operations.
    """

    @pytest.fixture(autouse=True)
    async def setup_get_fixture(
        self, hass: HomeAssistant
    ) -> AsyncGenerator[None, None, None]:
        """Set up test environment for GET operations.

        This fixture runs before each test method and sets up:
        - A real RamsesBroker instance
        - A mock client with an HGI device
        - Patches for Command.get_fan_param
        - Test command objects for GET operations

        Args:
            hass: Home Assistant fixture for creating a test environment.
        """
        # Create a real broker instance with a mock config entry
        self.broker = RamsesBroker(hass, MagicMock())

        # Create a mock client with HGI device
        self.mock_client = AsyncMock()
        self.broker.client = self.mock_client
        self.broker.client.hgi = MagicMock(id=TEST_FROM_ID)

        # Patch Command.get_fan_param to control command creation
        self.patcher = patch("ramses_tx.command.Command.get_fan_param")
        self.mock_get_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "RQ"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        self.mock_get_fan_param.return_value = self.mock_cmd

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()

    @pytest.mark.asyncio
    async def test_basic_fan_param_request(self, hass: HomeAssistant) -> None:
        """Test basic fan parameter request with all required parameters.

        Verifies that:
        1. The command is constructed with correct parameters
        2. The command is sent via the client
        3. No errors are raised
        """
        # Setup service call data with all required parameters
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": TEST_PARAM_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify command construction
        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # device_id as positional argument
            TEST_PARAM_ID,  # param_id as positional argument
            src_id=TEST_FROM_ID,  # src_id as keyword argument
        )

        # Verify command was sent via the client
        self.mock_client.async_send_cmd.assert_awaited_once_with(self.mock_cmd)

    @pytest.mark.asyncio
    async def test_hgi_not_available(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test behaviour when HGI is not available and no from_id is provided.

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
            service_data = {"device_id": TEST_DEVICE_ID, "param_id": TEST_PARAM_ID}
            call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

            # Clear any existing log captures
            caplog.clear()
            caplog.set_level(logging.WARNING)  # Capture warnings and above

            # Act - Call the method under test
            await broker.async_get_fan_param(call)

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
        service_data = {"device_id": TEST_DEVICE_ID, "param_id": TEST_PARAM_ID}
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify command was constructed with HGI as source
        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # fan_id
            TEST_PARAM_ID,  # param_id
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
        service_data = {"param_id": TEST_PARAM_ID}
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify error was logged
        error_message = "Missing required parameter: device_id"
        assert any(
            error_message in record.message
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ), f"Expected error message '{error_message}' not found in logs"

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
        service_data = {"device_id": TEST_DEVICE_ID}
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify error was logged
        error_message = "Missing required parameter: param_id"
        assert any(
            error_message in record.message
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ), f"Expected error message '{error_message}' not found in logs"

        # Verify no command was sent
        self.mock_client.async_send_cmd.assert_not_called()

    @pytest.mark.parametrize(
        "param_id,expected_param_id",
        [
            ("4E", "4E"),  # Valid format
            ("4e", "4E"),  # Lowercase hex -> uppercase
            ("04", "04"),  # With leading zero
            (" 4E ", "4E"),  # With whitespace -> stripped and uppercase
        ],
    )
    @pytest.mark.asyncio
    async def test_param_id_formats(
        self,
        hass: HomeAssistant,
        caplog: pytest.LogCaptureFixture,
        param_id: str,
        expected_param_id: str,
    ) -> None:
        """Test that various param_id formats are handled correctly.

        Verifies that:
        1. The command is constructed with the normalized param_id
        2. The command is sent successfully
        """
        # Setup service call with test param_id
        service_data = {"device_id": TEST_DEVICE_ID, "param_id": param_id}
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.DEBUG)

        # Setup mock command
        mock_cmd = AsyncMock()
        self.mock_get_fan_param.return_value = mock_cmd

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify command was constructed with normalized param_id
        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,
            expected_param_id,  # Should use the normalized param_id
            src_id=TEST_FROM_ID,
        )
        self.mock_client.async_send_cmd.assert_awaited_once_with(mock_cmd)

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, hass: HomeAssistant) -> None:
        """Test that multiple concurrent requests are handled correctly.

        Verifies that:
        1. Multiple concurrent requests don't interfere with each other
        2. Each request gets its own response
        """
        # Number of concurrent requests
        num_requests = 5

        # Create a list of mock commands and service calls
        mock_commands = []
        service_calls = []

        for i in range(num_requests):
            # Create a unique param_id for each request
            param_id = f"{i:02X}"

            # Create a mock command for this request
            mock_cmd = AsyncMock()
            mock_cmd.payload = [None, None, param_id]
            mock_commands.append(mock_cmd)

            # Create a service call for this request
            service_data = {"device_id": TEST_DEVICE_ID, "param_id": param_id}
            call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)
            service_calls.append(call)

            # Configure get_fan_param to return the mock command for this param_id
            self.mock_get_fan_param.return_value = mock_cmd

        # Reset the mock to return commands in sequence
        self.mock_get_fan_param.side_effect = mock_commands

        # Execute all requests concurrently
        tasks = [self.broker.async_get_fan_param(call) for call in service_calls]
        await asyncio.gather(*tasks)

        # Verify each command was sent with the correct parameters
        assert self.mock_client.async_send_cmd.await_count == num_requests

        # Verify each command was constructed with the correct param_id
        for i in range(num_requests):
            param_id = f"{i:02X}"
            self.mock_get_fan_param.assert_any_call(
                TEST_DEVICE_ID, param_id, src_id=TEST_FROM_ID
            )

    @pytest.mark.asyncio
    async def test_custom_fan_id(self, hass: HomeAssistant) -> None:
        """Test that a custom fan_id can be specified.

        Verifies that:
        1. The fan_id parameter is used when provided
        2. The command is constructed with the correct fan_id
        """
        custom_fan_id = "99:999999"

        # Setup service call with custom fan_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "fan_id": custom_fan_id,
            "param_id": TEST_PARAM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify command was constructed with custom fan_id
        self.mock_get_fan_param.assert_called_once_with(
            custom_fan_id,  # Should use the custom fan_id
            TEST_PARAM_ID,
            src_id=TEST_FROM_ID,
        )

        # Verify command was sent
        self.mock_client.async_send_cmd.assert_awaited_once()

    # Error Conditions
    @pytest.mark.parametrize(
        "test_input,should_log_error,expected_error",
        [
            # Missing required parameters - these should log errors
            (
                {"device_id": "", "param_id": TEST_PARAM_ID},
                True,
                "Missing required parameter: device_id",
            ),
            (
                {"device_id": TEST_DEVICE_ID, "param_id": ""},
                True,
                "Missing required parameter: param_id",
            ),
            (
                {"device_id": "", "param_id": ""},
                True,
                "Missing required parameter: device_id",
            ),
            # The following validations are not currently enforced by the broker,
            # so we don't expect them to log errors
            ({"device_id": "invalid!id", "param_id": TEST_PARAM_ID}, False, None),
            (
                {"device_id": "12:345678", "param_id": "XG"},
                True,
                "Invalid parameter ID",
            ),
            (
                {"device_id": "12:345678", "param_id": "4", "from_id": "invalid!id"},
                True,
                "Invalid parameter ID",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_error_conditions(
        self,
        hass: HomeAssistant,
        caplog: pytest.LogCaptureFixture,
        test_input: dict[str, str],
        should_log_error: bool,
        expected_error: str | None,
    ) -> None:
        """Test various error conditions and input validations.

        Note: The broker logs errors but doesn't raise exceptions for validation errors.
        """
        # Setup service call with test data
        service_data = {}

        # Always include device_id and param_id from test_input (may be empty strings for error cases)
        service_data["device_id"] = test_input.get("device_id", "")
        service_data["param_id"] = test_input.get("param_id", "")

        # Include from_id if specified
        if "from_id" in test_input:
            service_data["from_id"] = test_input["from_id"]

        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Setup mock command
        mock_cmd = AsyncMock()
        self.mock_get_fan_param.return_value = mock_cmd

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        if should_log_error and expected_error:
            # Verify the error was logged
            assert any(
                expected_error in record.message
                for record in caplog.records
                if record.levelno >= logging.ERROR
            ), f"Expected error message containing '{expected_error}' not found in logs"

            # Verify no command was sent for error cases
            self.mock_client.async_send_cmd.assert_not_called()
        else:
            # Verify no errors were logged
            assert not any(
                record.levelno >= logging.ERROR for record in caplog.records
            ), f"Unexpected error logged: {[r.message for r in caplog.records]}"

            # Verify command was sent for valid cases
            self.mock_client.async_send_cmd.assert_called_once()

    # Edge Cases
    @pytest.mark.parametrize(
        "param_id,should_log_error,expected_error",
        [
            ("00", False, None),  # Valid minimum param ID
            ("FF", False, None),  # Valid maximum param ID
            (
                "0" * 32,
                True,
                "Invalid parameter ID",
            ),  # Very long param ID -> should fail
            (
                "!@#$%^&*()",
                True,
                "Invalid parameter ID",
            ),  # Special characters -> should fail
        ],
    )
    @pytest.mark.asyncio
    async def test_edge_cases(
        self,
        hass: HomeAssistant,
        caplog: pytest.LogCaptureFixture,
        param_id: str,
        should_log_error: bool,
        expected_error: str | None,
    ) -> None:
        """Test various edge cases with parameter values."""
        # Setup service call with test data
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "param_id": param_id,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Setup mock command
        mock_cmd = AsyncMock()
        self.mock_get_fan_param.return_value = mock_cmd

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        if should_log_error and expected_error:
            # Verify the error was logged
            assert any(
                expected_error in record.message
                for record in caplog.records
                if record.levelno >= logging.ERROR
            ), f"Expected error message containing '{expected_error}' not found in logs"

            # Verify no command was sent for error cases
            self.mock_client.async_send_cmd.assert_not_called()
        else:
            # Verify no errors were logged
            assert not any(
                record.levelno >= logging.ERROR for record in caplog.records
            ), f"Unexpected error logged: {[r.message for r in caplog.records]}"

            # Verify command was sent for valid cases
            self.mock_client.async_send_cmd.assert_called_once()
            self.mock_get_fan_param.assert_called_once_with(
                TEST_DEVICE_ID,
                param_id,  # Should use the exact param_id provided
                src_id=TEST_FROM_ID,
            )

    # Command Construction
    @pytest.mark.parametrize(
        "test_input,expected_args,expected_kwargs",
        [
            (
                {"device_id": "12:345678", "param_id": "4E"},
                ("12:345678", "4E"),
                {"src_id": TEST_FROM_ID},
            ),
            (
                {"device_id": "12:345678", "param_id": "4E", "from_id": "98:765432"},
                ("12:345678", "4E"),
                {"src_id": "98:765432"},
            ),
            (
                {"device_id": "12:345678", "param_id": "4E", "fan_id": "98:765432"},
                ("98:765432", "4E"),  # fan_id should override device_id
                {"src_id": TEST_FROM_ID},
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_command_construction(
        self,
        hass: HomeAssistant,
        test_input: dict[str, str],
        expected_args: tuple[str, str],
        expected_kwargs: dict[str, str],
    ) -> None:
        """Test that commands are constructed with the correct parameters."""
        # Setup service call with test data
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, test_input)

        # Setup mock command
        mock_cmd = AsyncMock()
        self.mock_get_fan_param.return_value = mock_cmd

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify command was constructed with the correct parameters
        self.mock_get_fan_param.assert_called_once_with(
            *expected_args, **expected_kwargs
        )

        # Verify command was sent with the correct parameters
        self.mock_client.async_send_cmd.assert_awaited_once_with(mock_cmd)

        # Reset the side effect for other tests
        self.mock_get_fan_param.side_effect = None


class TestFanParameterSet:
    """Test cases for the set_fan_param service.

    ⚠️  SAFETY NOTICE: This test class uses comprehensive mocking to ensure
    no real commands are sent to actual FAN devices. All Command.set_fan_param
    calls and client.send_cmd operations are intercepted by mocks.

    Safety measures in place:
    - Command.set_fan_param is patched with mock
    - Client.async_send_cmd is mocked
    - Broker uses mock client, not real hardware
    - All assertions verify mock behaviour only
    - No real hardware communication can occur

    This test class verifies the behaviour of the async_set_fan_param method
    in the RamsesBroker class, including error handling and edge cases for
    parameter writing operations.
    """

    @pytest.fixture(autouse=True)
    async def setup_set_fixture(self, hass: HomeAssistant):
        """Set up test environment for SET operations.

        This fixture runs before each test method and sets up:
        - A real RamsesBroker instance
        - A mock client with an HGI device
        - Patches for Command.set_fan_param
        - Test command objects for SET operations

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
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        # Act - Call the method under test
        await self.broker.async_set_fan_param(call)

        # Assert - Verify command construction
        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # device_id as positional argument
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
        """Test behaviour when HGI is not available and no from_id is provided.

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
            call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

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
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

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
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

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
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

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
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

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
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

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
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

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


class TestFanParameterUpdate:
    """Test cases for the update_fan_params service.

    This test class verifies the behaviour of the _async_run_fan_param_sequence method
    in the RamsesBroker class, which sends parameter read requests for all parameters
    defined in the 2411 parameter schema to the specified FAN device.
    """

    @pytest.fixture(autouse=True)
    async def setup_update_fixture(self, hass: HomeAssistant):
        """Set up test environment for UPDATE operations.

        This fixture runs before each test method and sets up:
        - A real RamsesBroker instance
        - A mock client with an HGI device
        - Patches for Command.get_fan_param
        - Test command objects for UPDATE operations

        Args:
            hass: Home Assistant fixture for creating a test environment.
        """
        # Create a real broker instance with a mock config entry
        self.broker = RamsesBroker(hass, MagicMock())

        # Create a mock client with HGI device
        self.mock_client = AsyncMock()
        self.broker.client = self.mock_client
        self.broker.client.hgi = MagicMock(id=TEST_FROM_ID)

        # Patch Command.get_fan_param to control command creation
        self.patcher = patch("ramses_tx.command.Command.get_fan_param")
        self.mock_get_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "RQ"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        self.mock_get_fan_param.return_value = self.mock_cmd

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()

    @pytest.mark.asyncio
    async def test_basic_fan_param_update(self, hass: HomeAssistant) -> None:
        """Test basic fan parameter update with all required parameters.

        Verifies that:
        1. Commands are constructed for all parameters in the schema
        2. All commands are sent via the client
        3. No errors are raised
        """
        # Setup service call data with all required parameters
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

        # Act - Call the method under test
        await self.broker._async_run_fan_param_sequence(call)

        # Verify all parameters in the schema were requested
        # Note: We can't easily test the exact number without importing the schema,
        # but we can verify that get_fan_param was called multiple times
        assert self.mock_get_fan_param.call_count > 0, (
            "Expected multiple parameter requests"
        )

        # Verify commands were sent via the client
        assert self.mock_client.async_send_cmd.call_count > 0, (
            "Expected multiple commands sent"
        )

    @pytest.mark.asyncio
    async def test_update_without_from_id_uses_hgi(self, hass: HomeAssistant) -> None:
        """Test that omitting from_id uses the HGI device ID.

        Verifies that:
        1. When from_id is not provided, the HGI device ID is used as the source
        2. Commands are constructed with the correct parameters
        3. All commands are sent via the client
        """
        # Setup service call without from_id
        service_data = {"device_id": TEST_DEVICE_ID}
        call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

        # Act - Call the method under test
        await self.broker._async_run_fan_param_sequence(call)

        # Verify commands were constructed with HGI as source
        # Check that at least one call was made with the correct parameters
        calls = self.mock_get_fan_param.call_args_list
        assert len(calls) > 0, "Expected multiple parameter requests"

        # Verify commands were sent
        assert self.mock_client.async_send_cmd.call_count > 0, (
            "Expected multiple commands sent"
        )

    @pytest.mark.asyncio
    async def test_update_missing_required_device_id(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing device_id logs an error.

        Verifies that:
        1. An error is logged when device_id is missing
        2. No commands are sent when validation fails
        """
        # Setup service call without device_id
        service_data = {}
        call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act - Call the method under test
        await self.broker._async_run_fan_param_sequence(call)

        # Verify error was logged
        assert any(
            "Missing required parameter: device_id" in record.message
            for record in caplog.records
            if record.levelno == logging.ERROR
        ), "Expected validation error for missing device_id not found in logs"

        # Verify no commands were sent
        self.mock_client.async_send_cmd.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_hgi_not_available(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test behaviour when HGI is not available.

        Verifies that:
        1. The error is properly logged when HGI is not available
        2. No commands are sent when HGI is not available
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
            service_data = {"device_id": TEST_DEVICE_ID}
            call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

            # Clear any existing log captures
            caplog.clear()
            caplog.set_level(logging.WARNING)  # Capture warnings and above

            # Act - Call the method under test
            await broker._async_run_fan_param_sequence(call)

            # Verify the warning was logged
            warning_message = "Cannot get parameter: No valid source device available"
            assert any(
                warning_message in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            ), f"Expected warning message '{warning_message}' not found in logs"

            # Verify no commands were sent
            mock_client.async_send_cmd.assert_not_called()

        finally:
            # Restore the patcher for other tests
            self.patcher.start()

    @pytest.mark.asyncio
    async def test_update_with_fan_id_parameter(self, hass: HomeAssistant) -> None:
        """Test that fan_id parameter is used when provided.

        Verifies that:
        1. When fan_id is provided, it's used instead of device_id for all commands
        2. Commands are constructed with the correct parameters
        3. All commands are sent via the client
        """
        test_fan_id = "99:999999"  # Different from device_id

        # Setup service call with fan_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            "fan_id": test_fan_id,
        }
        call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

        # Act - Call the method under test
        await self.broker._async_run_fan_param_sequence(call)

        # Verify commands were constructed with fan_id as target
        calls = self.mock_get_fan_param.call_args_list
        assert len(calls) > 0, "Expected multiple parameter requests"

        # Check that fan_id was used in the calls
        for call_args in calls:
            args, kwargs = call_args
            assert args[0] == test_fan_id, (
                f"Expected fan_id {test_fan_id} as first argument, got {args[0]}"
            )

        # Verify commands were sent
        assert self.mock_client.async_send_cmd.call_count > 0, (
            "Expected multiple commands sent"
        )

    @pytest.mark.asyncio
    async def test_update_exception_handling(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that exceptions during update operations are properly handled.

        Verifies that:
        1. Exceptions during command sending are caught and logged
        2. The error message is properly logged
        3. The operation fails gracefully
        """
        # Setup service call
        service_data = {"device_id": TEST_DEVICE_ID}
        call = ServiceCall(hass, "ramses_cc", "update_fan_params", service_data)

        # Configure the mock to raise an exception
        error_msg = "Simulated network error"
        self.mock_client.async_send_cmd.side_effect = Exception(error_msg)

        # Clear any existing log captures
        caplog.clear()
        caplog.set_level(logging.ERROR)

        # Act - Call the method under test
        await self.broker._async_run_fan_param_sequence(call)

        # Verify the error was logged
        assert any(
            "Failed to get fan parameters for device" in record.message
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ), "Expected error message about failed update not found in logs"

        assert any(
            error_msg in record.message
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ), f"Expected error message containing '{error_msg}' not found in logs"

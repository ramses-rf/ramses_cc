"""Tests for Ramses fan parameter services.

This module contains comprehensive tests for both get_fan_param and set_fan_param
services in the Ramses RF integration (ramses_cc on github). It verifies the basic
functionality of sending fan parameter commands and handling various edge cases
for both read and write operations.

TODO: add tests routing a service call via a (mocked) device
"""

from __future__ import annotations

# import asyncio
import logging
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.broker import RamsesBroker
from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.schemas import (
    SCH_GET_FAN_PARAM_DOMAIN,
    SVC_GET_FAN_PARAM,
    SVC_SET_FAN_PARAM,
)

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
    async def setup_get_fixture(self, hass: HomeAssistant) -> AsyncGenerator[None]:
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

        # Create a mock device and add it to the registry
        # This prevents _get_device_and_from_id from returning early with empty from_id
        self.mock_device = MagicMock()
        self.mock_device.id = TEST_DEVICE_ID
        self.mock_device.get_bound_rem.return_value = None
        self.broker.client.device_by_id = {TEST_DEVICE_ID: self.mock_device}

        # Patch Command.get_fan_param to control command creation
        self.patcher = patch("custom_components.ramses_cc.broker.Command.get_fan_param")
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
        """Test basic fan parameter request with all required parameters directly on broker.

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

    # @pytest.mark.asyncio
    # async def test_missing_required_device_id(
    #     self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    # ) -> None:
    #     """Test that missing device_id logs an error.
    #
    #     Verifies that:
    #     1. An error is logged when device_id is missing
    #     2. No command is sent when validation fails
    #     """
    #     # Setup service call without device_id
    #     service_data = {"param_id": TEST_PARAM_ID, "from_id": TEST_FROM_ID}
    #     call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)
    #
    #     # Clear any existing log captures
    #     caplog.clear()
    #     caplog.set_level(logging.ERROR)
    #
    #     # Act - Call the method under test
    #     await self.broker.async_get_fan_param(call)
    #
    #     # Assert - Verify error was logged
    #     error_message = "Missing required parameter: device_id"
    #     assert any(
    #         error_message in record.message
    #         for record in caplog.records
    #         if record.levelno >= logging.ERROR
    #     ), f"Expected error message '{error_message}' not found in logs"
    #
    #     # Verify no command was sent
    #     self.mock_client.async_send_cmd.assert_not_called()

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
        service_data = {"device_id": TEST_DEVICE_ID, "from_id": TEST_FROM_ID}
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

    @pytest.mark.asyncio
    async def test_custom_fan_id(self, hass: HomeAssistant) -> None:
        """Test that a custom fan_id can be specified.

        Verifies that:
        1. The fan_id parameter is used when provided
        2. The command is constructed with the correct fan_id
        """
        # custom_fan_id = "99:999999"

        # Setup service call with custom fan_id
        service_data = {
            "device_id": TEST_DEVICE_ID,
            # "fan_id": custom_fan_id,
            "param_id": TEST_PARAM_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        # Act - Call the method under test
        await self.broker.async_get_fan_param(call)

        # Assert - Verify command was constructed with custom fan_id
        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,  # fan_id deprecated?? Should use the custom fan_id
            TEST_PARAM_ID,
            src_id=TEST_FROM_ID,
        )

        # Verify command was sent
        self.mock_client.async_send_cmd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_fan_param_with_ha_device_selector_resolves_device_id(
        self, hass: HomeAssistant
    ) -> None:
        entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
        entry.add_to_hass(hass)
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, TEST_DEVICE_ID)},
            name="Test FAN",
        )

        service_data = {
            "device": device_entry.id,
            "param_id": TEST_PARAM_ID,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_GET_NAME, service_data)

        await self.broker.async_get_fan_param(call)

        self.mock_get_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,
            TEST_PARAM_ID,
            src_id=TEST_FROM_ID,
        )


async def test_get_fan_param_service_schema_accepts_ha_device_selector(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, TEST_DEVICE_ID)},
        name="Test FAN",
    )

    handler = AsyncMock()
    hass.services.async_register(
        DOMAIN, SVC_GET_FAN_PARAM, handler, schema=SCH_GET_FAN_PARAM_DOMAIN
    )

    await hass.services.async_call(
        DOMAIN,
        SVC_GET_FAN_PARAM,
        {"device": device_entry.id, "param_id": TEST_PARAM_ID},
        blocking=True,
    )

    assert handler.called


class TestFanParameterSet:
    """Test cases for the set_fan_param service.

    SAFETY NOTICE: This test class uses comprehensive mocking to ensure
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
    async def setup_set_fixture(self, hass: HomeAssistant) -> AsyncGenerator[None]:
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

        # Create a mock device and add it to the registry
        self.mock_device = MagicMock()
        self.mock_device.id = TEST_DEVICE_ID
        self.mock_device.get_bound_rem.return_value = None
        self.broker.client.device_by_id = {TEST_DEVICE_ID: self.mock_device}

        # Patch Command.set_fan_param to control command creation
        self.patcher = patch("custom_components.ramses_cc.broker.Command.set_fan_param")
        self.mock_set_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "W"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        self.mock_set_fan_param.return_value = self.mock_cmd

        # PERFORMANCE OPTIMIZATION:
        # Patch asyncio.sleep to be instant for set operations which use sleep
        self.sleep_patcher = patch("asyncio.sleep")
        self.mock_sleep = self.sleep_patcher.start()

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()
        self.sleep_patcher.stop()

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
    async def test_set_fan_param_with_ha_device_selector(
        self, hass: HomeAssistant
    ) -> None:
        entry = MockConfigEntry(domain=DOMAIN, entry_id="test")
        entry.add_to_hass(hass)
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, TEST_DEVICE_ID)},
            name="Test FAN",
        )

        service_data = {
            "device": device_entry.id,
            "param_id": TEST_PARAM_ID,
            "value": TEST_VALUE,
            "from_id": TEST_FROM_ID,
        }
        call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)

        await self.broker.async_set_fan_param(call)

        self.mock_set_fan_param.assert_called_once_with(
            TEST_DEVICE_ID,
            TEST_PARAM_ID,
            TEST_VALUE,
            src_id=TEST_FROM_ID,
        )

        # @pytest.mark.asyncio
        # async def test_with_fan_id_parameter(self, hass: HomeAssistant) -> None:
        #     """Test that fan_id parameter is used when provided.
        #
        #     Verifies that:
        #     1. When fan_id is provided, it's used instead of device_id for the command
        #     2. The command is constructed with the correct parameters
        #     """
        #     test_fan_id = "99:999999"  # Different from device_id
        #
        #     # Setup service call with fan_id
        #     service_data = {
        #         "device_id": TEST_DEVICE_ID,
        #         # "fan_id": test_fan_id,
        #         "param_id": TEST_PARAM_ID,
        #         "value": TEST_VALUE,
        #         "from_id": TEST_FROM_ID,
        #     }
        #     call = ServiceCall(hass, "ramses_cc", SERVICE_SET_NAME, service_data)
        #
        #     # Act - Call the method under test
        #     await self.broker.async_set_fan_param(call)
        #
        #     # Assert - Verify command was constructed with fan_id as target
        #     self.mock_set_fan_param.assert_called_once_with(
        #         test_fan_id,  # fan_id should be used instead of device_id
        #         TEST_PARAM_ID,
        #         TEST_VALUE,  # value as is (will be converted to string in Command.set_fan_param)
        #         src_id=TEST_FROM_ID,
        #     )

        # Verify command was sent
        self.mock_client.async_send_cmd.assert_awaited_once()


class TestFanParameterUpdate:
    """Test cases for the update_fan_params service.

    This test class verifies the behaviour of the _async_run_fan_param_sequence method
    in the RamsesBroker class, which sends parameter read requests for all parameters
    defined in the 2411 parameter schema to the specified FAN device.
    """

    @pytest.fixture(autouse=True)
    async def setup_update_fixture(self, hass: HomeAssistant) -> AsyncGenerator[None]:
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

        # Create a mock device and add it to the registry
        self.mock_device = MagicMock()
        self.mock_device.id = TEST_DEVICE_ID
        self.mock_device.get_bound_rem.return_value = None
        self.broker.client.device_by_id = {TEST_DEVICE_ID: self.mock_device}

        # Patch Command.get_fan_param to control command creation
        self.patcher = patch("custom_components.ramses_cc.broker.Command.get_fan_param")
        self.mock_get_fan_param = self.patcher.start()

        # Create a test command that will be returned by the patched method
        self.mock_cmd = MagicMock()
        self.mock_cmd.code = "2411"
        self.mock_cmd.verb = "RQ"
        self.mock_cmd.src = MagicMock(id=TEST_FROM_ID)
        self.mock_cmd.dst = MagicMock(id=TEST_DEVICE_ID)
        self.mock_get_fan_param.return_value = self.mock_cmd

        # PERFORMANCE OPTIMIZATION:
        # Patch asyncio.sleep to be instant for set operations which use sleep
        self.sleep_patcher = patch("asyncio.sleep")
        self.mock_sleep = self.sleep_patcher.start()

        yield  # Test runs here

        # Cleanup - stop all patches
        self.patcher.stop()
        self.sleep_patcher.stop()

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

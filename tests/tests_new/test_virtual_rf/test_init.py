"""Tests for the virtual_rf.__init__ module (rf_factory)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.virtual_rf import VirtualRf, rf_factory


async def test_rf_factory_creation() -> None:
    """Test that rf_factory creates VirtualRf and Gateways correctly."""
    # Schema 0: Explicit HGI80 class
    schema_0 = {
        "config": {"disable_discovery": True},
        "known_list": {"18:123456": {"class": "HGI", "_type": "EVOFW3"}},
    }
    # Schema 1: None (create empty port)
    schema_1 = None
    # Schema 2: Implicit HGI (derived from port index)
    schema_2 = {"config": {"disable_discovery": False}}

    # Mock the Gateway class so we don't spin up real transport threads
    with patch("tests.virtual_rf.Gateway") as MockGateway:
        # Configure the mock instance
        mock_gwy_instance = MockGateway.return_value

        # Ensure start() is awaitable
        mock_gwy_instance.start = AsyncMock()

        # Mock internal structure used in rf_factory assertions
        mock_transport = MagicMock()
        mock_transport._extra = {}
        mock_gwy_instance._transport = mock_transport

        rf, gwys = await rf_factory([schema_0, schema_1, schema_2])

        # Verify VirtualRf was created with 3 ports
        assert isinstance(rf, VirtualRf)
        assert len(rf.ports) == 3

        # Verify 2 Gateways were created (one for schema_1 is None)
        assert len(gwys) == 2

        # Verify Gateway 0 (Explicit ID)
        assert rf.gateways["18:123456"] == rf.ports[0]

        # Verify Gateway 2 (Derived ID: 18:222222)
        # Port index 2 -> "18:" + "2"*6
        assert rf.gateways["18:222222"] == rf.ports[2]

        await rf.stop()


async def test_rf_factory_max_ports() -> None:
    """Test exception when requesting too many ports."""
    schemas = [None] * 10
    with pytest.raises(TypeError, match="Only a maximum of"):
        await rf_factory(schemas)


async def test_rf_factory_bad_schemas() -> None:
    """Test exceptions for invalid schemas."""
    # 1. Multiple Gateways in one schema
    schema_multi = {
        "known_list": {
            "18:000001": {"class": "HGI"},
            "18:000002": {"class": "HGI"},
        }
    }
    with pytest.raises(TypeError, match="Multiple Gateways per schema"):
        await rf_factory([schema_multi])

    # 2. HGI without explicit class definition (when checking 18:xxxxxx)
    schema_bad_hgi: dict[str, dict[str, dict[str, Any]]] = {
        "known_list": {
            "18:000001": {},  # Missing class: HGI
        }
    }
    with pytest.raises(TypeError, match="Any Gateway must have its class defined"):
        await rf_factory([schema_bad_hgi])

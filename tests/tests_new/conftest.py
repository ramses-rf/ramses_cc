"""Fixtures and helpers for the ramses_cc tests."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.syrupy import (  # type: ignore[import-untyped]
    HomeAssistantSnapshotExtension,
)
from syrupy.assertion import SnapshotAssertion

try:
    from ..virtual_rf import VirtualRf
except (ImportError, ModuleNotFoundError):
    VirtualRf = None  # Windows: pty/termios unavailable


@pytest.fixture(autouse=True)  # type: ignore[untyped-decorator]
def auto_enable_custom_integrations(
    enable_custom_integrations: Any,
) -> Generator[None]:
    """Automatically enable custom integrations for all tests.

    :param enable_custom_integrations: The fixture to enable.
    :yield: None.
    """
    yield


# NOTE: ? workaround for: https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/issues/198
@pytest.fixture  # type: ignore[untyped-decorator]  # not loading from pytest_homeassistant_custom_component.plugins
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Return snapshot assertion fixture with the Home Assistant extension.

    :param snapshot: The base snapshot fixture.
    :return: SnapshotAssertion with HA extension.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture(autouse=True)  # type: ignore[untyped-decorator]
def patches_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply necessary monkeypatches before running tests.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    with contextlib.suppress(AttributeError):
        monkeypatch.setattr(
            "ramses_tx.protocol._DBG_DISABLE_IMPERSONATION_ALERTS",
            True,
        )
        monkeypatch.setattr(
            "ramses_tx.transport._DBG_DISABLE_DUTY_CYCLE_LIMIT", True
        )
        monkeypatch.setattr(
            "ramses_tx.transport._DBG_DISABLE_REGEX_WARNINGS", True
        )
        monkeypatch.setattr("ramses_tx.transport.MIN_INTER_WRITE_GAP", 0)

    # monkeypatch.setattr("ramses_tx.protocol._DBG_DISABLE_QOS", True)
    # monkeypatch.setattr("ramses_tx.protocol._DBG_FORCE_LOG_PACKETS", True)
    # monkeypatch.setattr("ramses_tx.transport._DBG_FORCE_FRAME_LOGGING", True)
    # monkeypatch.setattr("ramses_tx.protocol._GAP_BETWEEN_WRITES", 0)


@pytest.fixture()  # type: ignore[untyped-decorator]  # add hass fixture to ensure hass/rf use same event loop
async def rf(hass: HomeAssistant) -> AsyncGenerator[Any]:
    """Utilize a virtual evofw3-compatible gateway.

    :param hass: The Home Assistant core fixture.
    :yield: An instance of VirtualRf.
    """
    if VirtualRf is None:
        pytest.skip(
            "VirtualRf not available on this platform (requires pty/termios)"
        )

    rf_instance = VirtualRf(2)
    rf_instance.set_gateway(rf_instance.ports[0], "18:006402")

    with patch("serial.tools.list_ports.comports", rf_instance.comports):
        try:
            yield rf_instance
        finally:
            await rf_instance.stop()

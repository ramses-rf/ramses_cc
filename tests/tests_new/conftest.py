"""Fixtures and helpers for the ramses_cc tests."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.syrupy import (  # type: ignore[import-untyped]
    HomeAssistantSnapshotExtension,
)
from syrupy.assertion import SnapshotAssertion

try:
    from ..virtual_rf import VirtualRf
except (ImportError, ModuleNotFoundError):
    VirtualRf = None  # type: ignore[assignment,misc]  # Windows: pty/termios unavailable


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """Use SelectorEventLoop on Windows to avoid ProactorEventLoop socket issues.

    pytest-homeassistant-custom-component sets HassEventLoopPolicy at import
    time and then patches asyncio.set_event_loop_policy to a no-op so the
    policy cannot be replaced.  On Windows, HassEventLoopPolicy inherits from
    WindowsProactorEventLoopPolicy whose new_event_loop() creates a
    ProactorEventLoop that calls socket.socketpair() in __init__ — blocked by
    pytest-socket.  We monkey-patch new_event_loop on the class to return a
    SelectorEventLoop instead.
    """
    if sys.platform == "win32":
        from homeassistant import runner  # noqa: PLC0415

        def _win_new_event_loop(self: runner.HassEventLoopPolicy) -> asyncio.AbstractEventLoop:  # noqa: ANN001
            return asyncio.SelectorEventLoop()

        runner.HassEventLoopPolicy.new_event_loop = _win_new_event_loop  # type: ignore[method-assign]


@pytest.fixture(autouse=True, scope="session")  # type: ignore[override]
def mock_zeroconf_resolver():  # type: ignore[override]
    """Override the async session-scoped zeroconf resolver fixture with a sync one.

    The upstream fixture in plugins.py is an async session-scoped fixture that
    instantiates AsyncResolver() (from aiodns), which opens a real UDP socket.
    On Windows this socket creation is blocked by pytest-socket.  Additionally,
    being async+session forces pytest-asyncio to create a session-scoped event
    loop runner whose ProactorEventLoop.__init__ also calls socket.socketpair()
    — also blocked by pytest-socket.

    Overriding with a plain sync fixture avoids both problems.
    """
    mock_resolver = MagicMock()
    mock_resolver.close = AsyncMock()
    mock_resolver.real_close = AsyncMock()

    with patch(
        "homeassistant.helpers.aiohttp_client._async_make_resolver",
        return_value=mock_resolver,
    ) as patcher:
        yield patcher


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: pytest.fixture):  # type: ignore[no-untyped-def]
    yield


# NOTE: ? workaround for: https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/issues/198
@pytest.fixture  # not loading from pytest_homeassistant_custom_component.plugins
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Return snapshot assertion fixture with the Home Assistant extension."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture(autouse=True)
def patches_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply necessary monkeypatches before running tests."""

    with contextlib.suppress(AttributeError):
        monkeypatch.setattr(
            "ramses_tx.protocol._DBG_DISABLE_IMPERSONATION_ALERTS",
            True,
        )
        monkeypatch.setattr("ramses_tx.transport._DBG_DISABLE_DUTY_CYCLE_LIMIT", True)
        monkeypatch.setattr("ramses_tx.transport._DBG_DISABLE_REGEX_WARNINGS", True)
        monkeypatch.setattr("ramses_tx.transport.MIN_INTER_WRITE_GAP", 0)

    # monkeypatch.setattr("ramses_tx.protocol._DBG_DISABLE_QOS", True)
    # monkeypatch.setattr("ramses_tx.protocol._DBG_FORCE_LOG_PACKETS", True)
    # monkeypatch.setattr("ramses_tx.transport._DBG_FORCE_FRAME_LOGGING", True)
    # monkeypatch.setattr("ramses_tx.protocol._GAP_BETWEEN_WRITES", 0)


@pytest.fixture()  # add hass fixture to ensure hass/rf use same event loop
async def rf(hass: HomeAssistant) -> AsyncGenerator[Any]:
    """Utilize a virtual evofw3-compatible gateway."""

    if VirtualRf is None:
        pytest.skip("VirtualRf not available on this platform (requires pty/termios)")

    rf = VirtualRf(2)
    rf.set_gateway(rf.ports[0], "18:006402")

    with patch("serial.tools.list_ports.comports", rf.comports):
        try:
            yield rf
        finally:
            await rf.stop()

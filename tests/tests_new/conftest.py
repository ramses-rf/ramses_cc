"""Fixtures and helpers for the ramses_cc tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.syrupy import (  # type: ignore[import-untyped]
    HomeAssistantSnapshotExtension,
)
from syrupy.assertion import SnapshotAssertion

from ..virtual_rf import VirtualRf


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
    # try:
    monkeypatch.setattr("ramses_tx.protocol._DBG_DISABLE_IMPERSONATION_ALERTS", True)
    # nkeypatch.setattr("ramses_tx.protocol._DBG_DISABLE_QOS", True)
    # nkeypatch.setattr("ramses_tx.protocol._DBG_FORCE_LOG_PACKETS", True)
    monkeypatch.setattr("ramses_tx.transport._DBG_DISABLE_DUTY_CYCLE_LIMIT", True)
    monkeypatch.setattr("ramses_tx.transport._DBG_DISABLE_REGEX_WARNINGS", True)
    # nkeypatch.setattr("ramses_tx.transport._DBG_FORCE_FRAME_LOGGING", True)
    monkeypatch.setattr("ramses_tx.transport.MIN_INTER_WRITE_GAP", 0)

    # except AttributeError:
    #     monkeypatch.setattr("ramses_tx.protocol._GAP_BETWEEN_WRITES", 0)


@pytest.fixture()  # add hass fixture to ensure hass/rf use same event loop
async def rf(hass: HomeAssistant) -> AsyncGenerator[Any]:
    """Utilize a virtual evofw3-compatible gateway."""

    rf = VirtualRf(2)
    rf.set_gateway(rf.ports[0], "18:006402")

    with patch("ramses_tx.transport.comports", rf.comports):
        try:
            yield rf
        finally:
            await rf.stop()

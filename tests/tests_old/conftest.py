"""Fixtures and helpers for the ramses_cc tests."""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: pytest.fixture):  # type: ignore[no-untyped-def]
    yield


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

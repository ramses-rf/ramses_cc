"""Tests for the persistent-notification helper module."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.ramses_cc import notifications


def test_notification_id() -> None:
    """Slugs map to the standard ramses_cc_ notification id."""
    assert (
        notifications.notification_id("gateway_offline")
        == "ramses_cc_gateway_offline"
    )


def test_async_notify_builds_standard_fields(hass: HomeAssistant) -> None:
    """Notification gets the standard id and title prefix."""
    with patch.object(notifications, "_pn_create") as mock_create:
        notifications.async_notify(
            hass,
            "gateway_offline",
            title="Gateway offline",
            message="body",
        )

    mock_create.assert_called_once_with(
        hass,
        message="body",
        title="RAMSES CC: Gateway offline",
        notification_id="ramses_cc_gateway_offline",
    )


def test_async_dismiss(hass: HomeAssistant) -> None:
    """Dismiss targets the standard id and clears the slug."""
    notifications._active.add("gateway_offline")
    with patch.object(notifications, "_pn_dismiss") as mock_dismiss:
        notifications.async_dismiss(hass, "gateway_offline")

    mock_dismiss.assert_called_once_with(hass, "ramses_cc_gateway_offline")
    assert "gateway_offline" not in notifications._active


def test_async_set_notifies_once_until_recovery(hass: HomeAssistant) -> None:
    """A second active=True call does not re-create the notification."""
    with (
        patch.object(notifications, "_pn_create") as mock_create,
        patch.object(notifications, "_pn_dismiss") as mock_dismiss,
    ):
        notifications.async_set(hass, "s", True, title="t", message="m")
        notifications.async_set(hass, "s", True, title="t", message="m")
        assert mock_create.call_count == 1

        notifications.async_set(hass, "s", False)
        mock_dismiss.assert_called_once_with(hass, "ramses_cc_s")

        # Already clear: no second dismiss.
        notifications.async_set(hass, "s", False)
        mock_dismiss.assert_called_once()

        # A new episode notifies again.
        notifications.async_set(hass, "s", True, title="t", message="m")
        assert mock_create.call_count == 2

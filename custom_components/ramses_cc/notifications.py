"""Helpers for RAMSES CC persistent notifications.

Wraps ``homeassistant.components.persistent_notification`` so every
notification uses a consistent id (``ramses_cc_<slug>``) and title
prefix (``RAMSES CC:``).

``async_set()`` implements the common notify-once-until-recovery
pattern: the notification is created on the first ``active=True`` call
and dismissed on the first ``active=False`` call after that.  While a
slug is active the notification is not re-created, so a user dismissal
is respected until the condition recovers and fires again.
"""

from __future__ import annotations

from typing import Final

from homeassistant.components.persistent_notification import (
    async_create as _pn_create,
    async_dismiss as _pn_dismiss,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TITLE_PREFIX: Final = "RAMSES CC"

_active: set[str] = set()


def notification_id(slug: str) -> str:
    """Return the standard notification id for a slug."""
    return f"{DOMAIN}_{slug}"


def async_notify(
    hass: HomeAssistant, slug: str, *, title: str, message: str
) -> None:
    """Create or update a persistent notification in place.

    :param hass: The Home Assistant instance.
    :param slug: Short identifier; the notification id is
        ``ramses_cc_<slug>``.
    :param title: Notification title, shown after the
        ``RAMSES CC:`` prefix.
    :param message: Notification body (markdown).
    """
    _pn_create(
        hass,
        message=message,
        title=f"{TITLE_PREFIX}: {title}",
        notification_id=notification_id(slug),
    )


def async_dismiss(hass: HomeAssistant, slug: str) -> None:
    """Dismiss a persistent notification created by this module."""
    _pn_dismiss(hass, notification_id(slug))
    _active.discard(slug)


def async_set(
    hass: HomeAssistant,
    slug: str,
    active: bool,
    *,
    title: str = "",
    message: str = "",
) -> None:
    """Notify once per active episode; auto-dismiss on recovery.

    :param hass: The Home Assistant instance.
    :param slug: Short identifier; the notification id is
        ``ramses_cc_<slug>``.
    :param active: True while the condition holds.
    :param title: Notification title, shown after the
        ``RAMSES CC:`` prefix.
    :param message: Notification body (markdown).
    """
    if active:
        if slug not in _active:
            _active.add(slug)
            async_notify(hass, slug, title=title, message=message)
    elif slug in _active:
        async_dismiss(hass, slug)

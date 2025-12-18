"""Helper utilities for ramses_cc."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def ha_device_id_to_ramses_device_id(
    hass: HomeAssistant, ha_device_id: str
) -> str | None:
    """Return a RAMSES device_id (e.g. "32:153289") for a HA device registry id.

    The HA device id is the opaque string shown when using service UI targets.
    """

    if not ha_device_id:
        return None

    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get(ha_device_id)
    if not device_entry:
        return None

    for domain, dev_id in device_entry.identifiers:
        if domain == DOMAIN:
            return str(dev_id)

    _LOGGER.debug(
        "HA device_id %s has no %s identifier in device registry", ha_device_id, DOMAIN
    )
    return None


def ramses_device_id_to_ha_device_id(
    hass: HomeAssistant, ramses_device_id: str
) -> str | None:
    """Return a HA device registry id for a RAMSES device_id."""

    if not ramses_device_id:
        return None

    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, ramses_device_id)})
    if not device_entry:
        return None

    return device_entry.id

"""Tests for ramses_cc helper utilities.

This module targets 100% coverage for helpers.py by testing device ID
mappings between Home Assistant and RAMSES RF hardware IDs.
"""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.ramses_cc.const import DOMAIN
from custom_components.ramses_cc.helpers import (
    ha_device_id_to_ramses_device_id,
    ramses_device_id_to_ha_device_id,
)

# Constants
RAMSES_ID = "32:153289"
HA_ID = "opaque_device_id_123"


def test_ha_to_ramses_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from HA registry ID to RAMSES hardware ID.

    This targets ha_device_id_to_ramses_device_id in helpers.py.
    """
    # 1. Handle empty input
    assert ha_device_id_to_ramses_device_id(hass, "") is None

    # 2. Handle non-existent device
    assert ha_device_id_to_ramses_device_id(hass, "missing") is None

    # 3. Handle valid device mapping
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id="test_config",
        identifiers={(DOMAIN, RAMSES_ID)},
    )

    # Retrieve the HA ID created by the registry
    device = dev_reg.async_get_device(identifiers={(DOMAIN, RAMSES_ID)})
    assert device is not None

    result = ha_device_id_to_ramses_device_id(hass, device.id)
    assert result == RAMSES_ID


def test_ramses_to_ha_id_mapping(hass: HomeAssistant) -> None:
    """Test mapping from RAMSES hardware ID to HA registry ID.

    This targets ramses_device_id_to_ha_device_id in helpers.py.
    """
    # 1. Handle empty input
    assert ramses_device_id_to_ha_device_id(hass, "") is None

    # 2. Handle non-existent hardware
    assert ramses_device_id_to_ha_device_id(hass, "99:999999") is None

    # 3. Handle valid mapping
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id="test_config",
        identifiers={(DOMAIN, RAMSES_ID)},
    )

    result = ramses_device_id_to_ha_device_id(hass, RAMSES_ID)
    assert result == device.id

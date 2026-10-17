"""Firmware detection and compatibility checks for the Zigbee picker."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.ramses_cc.zigbee import (
    _native_firmware,
    _native_stack_ready,
    build_zigbee_url,
)
from ramses_tx.exceptions import TransportZigbeeError

IEEE = "10:bd:a3:ff:fe:a8:19:60"
URL = f"zigbee://{IEEE}/0xfc00/0x0000/10/0xfc01/0x0000/10"


@pytest.mark.parametrize(
    ("version", "native"),
    [
        ("0x00060000", True),
        ("0x00060100", True),
        ("0x01000000", True),
        ("v0.6.0", True),
        ("0.6.1", True),
        ("v1.0.0", True),
        ("0x00050500", False),
        ("v0.5.5", False),
        (None, False),
        ("", False),
        ("unknown", False),
        ("v0.6.0-dirty", False),
        ("v0.6.1rc1", False),
        ("v0.256.0", False),
        ("0x00060001", False),
        ("0x0006000000", False),
    ],
)
def test_installed_firmware_versions(
    version: str | None, native: bool
) -> None:
    """Only recognized public firmware at the supported boundary uses APS."""
    assert _native_firmware(version) is native


@pytest.mark.parametrize(
    ("version", "stack_ready", "forced", "suffix"),
    [
        ("0x00060000", True, False, "?ack=aps"),
        ("v0.6.1", True, False, "?ack=aps"),
        ("v0.5.5", True, False, ""),
        (None, True, False, ""),
        ("v0.6.1", False, False, ""),
        ("v0.6.1", True, True, "?ack=legacy"),
    ],
)
async def test_url_uses_installed_version_and_stack(
    hass: HomeAssistant,
    version: str | None,
    stack_ready: bool,
    forced: bool,
    suffix: str,
) -> None:
    """Use the paired ZHA device, not an MQTT twin or advertised release."""
    # Arrange a ZHA bridge and an MQTT twin advertising a newer version.
    zha = MockConfigEntry(domain="zha", data={})
    mqtt = MockConfigEntry(domain="mqtt", data={})
    zha.add_to_hass(hass)
    mqtt.add_to_hass(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=zha.entry_id,
        identifiers={("zha", IEEE)},
        model="Ramses_esp32c6",
        sw_version=version,
    )
    registry.async_get_or_create(
        config_entry_id=mqtt.entry_id,
        identifiers={("mqtt", IEEE)},
        model="Ramses_esp32c6",
        sw_version="v1.0.0",
    )
    with patch(
        "custom_components.ramses_cc.zigbee._native_stack_ready",
        return_value=stack_ready,
    ):
        # Act and assert.
        assert (
            build_zigbee_url(hass, IEEE, force_legacy=forced) == URL + suffix
        )


async def test_missing_registry_device_retains_legacy(
    hass: HomeAssistant,
) -> None:
    """A device removed during setup cannot enable APS accidentally."""
    assert build_zigbee_url(hass, IEEE) == URL


@pytest.mark.parametrize(
    ("rx_commands", "tx_commands", "ready"),
    [
        ({2}, {2, 3}, True),
        ({0}, {2, 3}, False),
        ({2}, {0}, False),
        ({2}, {2}, False),
    ],
)
def test_applied_quirk_requires_both_native_commands(
    hass: HomeAssistant,
    rx_commands: set[int],
    tx_commands: set[int],
    ready: bool,
) -> None:
    """Do not select APS when the applied quirk lacks native commands."""
    # Arrange a capable transport and the actual applied cluster definitions.
    connection = MagicMock()
    connection.get_cluster.side_effect = [
        SimpleNamespace(client_commands=rx_commands),
        SimpleNamespace(client_commands=tx_commands),
    ]
    with (
        patch(
            "ramses_tx.transport.zigbee.transport.ZigbeeTransport",
            new=SimpleNamespace(_enable_native_mode=lambda: None),
        ),
        patch(
            "ramses_tx.transport.zigbee.connection.ZigbeeConnectionManager",
            return_value=connection,
        ),
    ):
        # Act and assert.
        assert _native_stack_ready(hass, IEEE) is ready


def test_old_transport_retains_legacy(hass: HomeAssistant) -> None:
    """The pinned release must not silently receive unsupported APS URLs."""
    with patch(
        "ramses_tx.transport.zigbee.transport.ZigbeeTransport",
        new=SimpleNamespace(),
    ):
        assert not _native_stack_ready(hass, IEEE)


@pytest.mark.parametrize("missing_device", [False, True])
def test_unavailable_quirk_retains_legacy(
    hass: HomeAssistant, missing_device: bool
) -> None:
    """An unavailable ZHA device or cluster leaves the picker usable."""
    connection = MagicMock()
    if missing_device:
        connection.resolve_device.return_value = None
    else:
        connection.get_cluster.side_effect = TransportZigbeeError("Not ready")
    with (
        patch(
            "ramses_tx.transport.zigbee.transport.ZigbeeTransport",
            new=SimpleNamespace(_enable_native_mode=lambda: None),
        ),
        patch(
            "ramses_tx.transport.zigbee.connection.ZigbeeConnectionManager",
            return_value=connection,
        ),
    ):
        assert not _native_stack_ready(hass, IEEE)


def test_missing_zigbee_transport_retains_legacy(hass: HomeAssistant) -> None:
    """Optional Zigbee dependencies need not be installed for other ports."""
    with patch.dict(
        sys.modules, {"ramses_tx.transport.zigbee.connection": None}
    ):
        assert not _native_stack_ready(hass, IEEE)

"""Select the RAMSES bridge protocol from installed firmware capabilities."""

import logging
import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ramses_tx.exceptions import TransportZigbeeError

_LOGGER = logging.getLogger(__name__)
_FIRST_NATIVE_VERSION = (0, 6, 0)


def _native_firmware(version: str | None) -> bool:
    """Recognize public native firmware in ZHA or ESP version notation."""
    if not isinstance(version, str):
        return False
    parts: tuple[int, ...]
    if re.fullmatch(r"0x[0-9a-fA-F]{8}", version):
        packed = int(version, 16)
        if packed & 0xFF:
            return False
        parts = (packed >> 24, (packed >> 16) & 0xFF, (packed >> 8) & 0xFF)
    elif match := re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version):
        parts = tuple(int(part) for part in match.groups())
        if any(part > 255 for part in parts):
            return False
    else:
        return False
    return parts >= _FIRST_NATIVE_VERSION


def _native_stack_ready(hass: HomeAssistant, ieee: str) -> bool:
    """Check the installed transport and the applied device quirk."""
    try:
        from ramses_tx.transport.zigbee.connection import (
            ZigbeeConnectionManager,
        )
        from ramses_tx.transport.zigbee.transport import ZigbeeTransport

        # Older installed transports accept the URL but cannot select APS.
        if not callable(getattr(ZigbeeTransport, "_enable_native_mode", None)):
            return False
        connection = ZigbeeConnectionManager(
            hass=hass,
            ieee=ieee,
            cluster_id=0xFC00,
            attr_id=0,
            endpoint_id=10,
            write_cluster_id=0xFC01,
            write_attr_id=0,
            write_endpoint_id=10,
        )
        device = connection.resolve_device()
        if device is None:
            return False
        rx = connection.get_cluster(device, 10, 0xFC00, "out")
        tx = connection.get_cluster(device, 10, 0xFC01, "in")
        return (
            2 in rx.client_commands
            and 2 in tx.client_commands
            and 3 in tx.client_commands
        )
    except (ImportError, TransportZigbeeError, ValueError):
        return False


def build_zigbee_url(
    hass: HomeAssistant,
    ieee: str,
    *,
    force_legacy: bool = False,
) -> str:
    """Build a bridge URL using the paired device's installed firmware.

    :param hass: Home Assistant instance owning the ZHA device registry.
    :param ieee: IEEE address selected through the device picker.
    :param force_legacy: Explicitly retain text acknowledgements.
    :return: A legacy URL or a URL selecting native APS acknowledgements.
    """
    url = f"zigbee://{ieee}/0xfc00/0x0000/10/0xfc01/0x0000/10"
    if force_legacy:
        return url + "?ack=legacy"
    device = dr.async_get(hass).async_get_device(identifiers={("zha", ieee)})
    version = device.sw_version if device is not None else None
    if _native_firmware(version) and _native_stack_ready(hass, ieee):
        _LOGGER.info("Selecting native APS for Zigbee %s (%s)", ieee, version)
        return url + "?ack=aps"
    _LOGGER.info(
        "Selecting legacy Zigbee for %s (firmware=%s; native support "
        "requires firmware >= v0.6.0 and a matching transport and quirk)",
        ieee,
        version,
    )
    return url

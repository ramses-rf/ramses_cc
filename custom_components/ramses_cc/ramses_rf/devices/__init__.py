#!/usr/bin/env python3
"""RAMSES RF - Heating devices (e.g. CTL, OTB, BDR, TRV)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from .. import exceptions as exc
from ..const import DEV_TYPE_MAP
from ..models import DeviceTraits
from ...ramses_tx.const import DevType

from .dev_filter import DeviceFilter
from .dev_registry import DeviceRegistry

from .dev_base import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    BASE_CLASS_BY_SLUG as _BASE_CLASS_BY_SLUG,
    BatteryState,
    Device,
    Fakeable,
    DeviceHeat,
    HgiGateway,
    DeviceHvac,
)

from ..protocol.ramses import (
    CODES_OF_HEAT_DOMAIN_ONLY,
    CODES_OF_HVAC_DOMAIN_ONLY,
    HVAC_KLASS_BY_VC_PAIR,
)
from .heat_actuators import BdrSwitch, JimDevice, JstDevice
from .heat_controllers import (
    Controller,
    Programmer,
    RfgGateway,
    UfhCircuit,
    UfhController,
)
from .heat_sensors import DhwSensor, OutSensor, Temperature
from .heat_thermostats import Thermostat, TrvActuator
from .hvac_remotes import HvacDisplayRemote, HvacRemote, HvacRemoteBase
from .hvac_sensors import HvacCarbonDioxideSensor, HvacHumiditySensor
from .hvac_ventilators import HvacVentilator, RfsGateway
from .opentherm_bridge import OtbGateway

if TYPE_CHECKING:
    from .. import Gateway
    from ..address import Address

    from ..messages import Message

__all__ = [
    # .base
    "Device",
    "DeviceFilter",
    "DeviceRegistry",
    "BASE_CLASS_BY_SLUG",
    "BatteryState",
    "Fakeable",
    "DeviceHeat",
    "HgiGateway",
    "DeviceHvac",
    # .heat
    "BdrSwitch",
    "Controller",
    "DhwSensor",
    "OtbGateway",
    "OutSensor",
    "RfgGateway",
    "Temperature",
    "Thermostat",
    "TrvActuator",
    "UfhCircuit",
    "UfhController",
    "class_dev_heat",
    # .hvac
    "HvacCarbonDioxideSensor",
    "HvacDisplayRemote",
    "HvacHumiditySensor",
    "HvacRemote",
    "HvacRemoteBase",
    "HvacVentilator",
    "RfsGateway",
    "class_dev_hvac",
    #
    "best_dev_role",
    "device_factory",
]

_LOGGER = logging.getLogger(__name__)

# Gather explicit classes to form the SLUG maps natively (No Magic/Reflection)
_HEAT_CLASSES = (
    BdrSwitch,
    Controller,
    DhwSensor,
    OtbGateway,
    OutSensor,
    Temperature,
    Thermostat,
    TrvActuator,
    UfhController,
    JimDevice,
    JstDevice,
    Programmer,
    RfgGateway,
)
_HEAT_CLASS_BY_SLUG = {cls._SLUG: cls for cls in _HEAT_CLASSES if hasattr(cls, "_SLUG")}

_HVAC_CLASSES = (
    HvacCarbonDioxideSensor,
    HvacHumiditySensor,
    HvacRemote,
    HvacDisplayRemote,
    HvacVentilator,
    RfsGateway,
)
_HVAC_CLASS_BY_SLUG = {cls._SLUG: cls for cls in _HVAC_CLASSES if hasattr(cls, "_SLUG")}

_CLASS_BY_SLUG = _BASE_CLASS_BY_SLUG | _HEAT_CLASS_BY_SLUG | _HVAC_CLASS_BY_SLUG

HEAT_DEV_CLASS_BY_SLUG = {
    k: v for k, v in _HEAT_CLASS_BY_SLUG.items() if k is not DevType.HEA
}
HVAC_DEV_CLASS_BY_SLUG = {
    k: v for k, v in _HVAC_CLASS_BY_SLUG.items() if k is not DevType.HVC
}


def best_dev_role(
    dev_addr: Address,
    *,
    msg: Message | None = None,
    eavesdrop: bool = False,
    traits: DeviceTraits | None = None,
) -> type[Device]:
    """Return the best device role (object class) for a given device id/msg/schema.

    Heat (CH/DHW) devices can reliably be determined by their address type (e.g. '04:').
    Any device without a known Heat type is considered a HVAC device.

    HVAC devices must be explicitly typed, or fingerprinted/eavesdropped.
    The generic HVAC class can be promoted later on, when more information is available.
    """

    cls: type[Device]
    slug: str | None

    traits = traits or DeviceTraits()

    try:  # convert (say) 'dhw_sensor' to DHW
        slug = DEV_TYPE_MAP.slug(traits.device_class)  # type: ignore[arg-type]
    except KeyError:
        slug = traits.device_class

    # a specified device class always takes precedence (even if it is wrong)...
    if slug and slug in _CLASS_BY_SLUG:
        cls = _CLASS_BY_SLUG[slug]
        _LOGGER.debug(
            f"Using an explicitly-defined class for: {dev_addr!r} ({cls._SLUG})"
        )
        return cls

    if dev_addr.type == DEV_TYPE_MAP.HGI:
        _LOGGER.debug(f"Using the default class for: {dev_addr!r} ({HgiGateway._SLUG})")
        return HgiGateway

    try:  # or, is it a well-known CH/DHW class, derived from the device type...
        if cls := class_dev_heat(dev_addr, msg=msg, eavesdrop=eavesdrop):
            _LOGGER.debug(
                f"Using the default Heat class for: {dev_addr!r} ({cls._SLUG})"
            )
            return cls
    except exc.DeviceNotRecognised:
        pass

    try:  # or, a HVAC class, eavesdropped from the message code/payload...
        if cls := class_dev_hvac(dev_addr, msg=msg, eavesdrop=eavesdrop):
            _LOGGER.debug(
                f"Using eavesdropped HVAC class for: {dev_addr!r} ({cls._SLUG})"
            )
            return cls  # includes DeviceHvac
    except exc.DeviceNotRecognised:
        pass

    # otherwise, use the default device class...
    _LOGGER.debug(
        f"Using a promotable HVAC class for: {dev_addr!r} ({DeviceHvac._SLUG})"
    )
    return DeviceHvac


def device_factory(
    gwy: Gateway,
    dev_addr: Address,
    *,
    msg: Message | None = None,
    traits: DeviceTraits | None = None,
) -> Device:
    """Return the initial device class for a given device id/msg/traits.

    Devices of certain classes are promotable to a compatible sub class.
    """

    traits = traits or DeviceTraits()

    cls: type[Device] = best_dev_role(
        dev_addr,
        msg=msg,
        eavesdrop=gwy.config.enable_eavesdrop,
        traits=traits,
    )

    if (
        issubclass(cls, DeviceHvac)
        and traits.device_class in (DevType.HVC, None)
        and traits.faked
    ):
        raise exc.SchemaInconsistentError(
            f"Faked devices from the HVAC domain must have an explicit class: {dev_addr}"
        )

    # Cast strictly resolves Mypy reporting base class returns instead of Device
    return cast(Device, cls.create_from_schema(gwy, dev_addr, traits=traits))


def class_dev_heat(
    dev_addr: Address, *, msg: Message | None = None, eavesdrop: bool = False
) -> type[DeviceHeat]:
    """Return a device class, but only if the device must be from the CH/DHW group.

    May return a device class, DeviceHeat (which will need promotion).
    """

    if dev_addr.type in DEV_TYPE_MAP.THM_DEVICES:
        return _HEAT_CLASS_BY_SLUG[DevType.THM]

    try:
        slug = DEV_TYPE_MAP.slug(dev_addr.type)
    except KeyError:
        pass
    else:
        return _HEAT_CLASS_BY_SLUG[slug]

    if not eavesdrop:
        raise exc.DeviceNotRecognised(
            f"No CH/DHW class for: {dev_addr} (no eavesdropping)"
        )

    if msg and msg.code in CODES_OF_HEAT_DOMAIN_ONLY:
        return DeviceHeat

    raise exc.DeviceNotRecognised(
        f"No CH/DHW class for: {dev_addr} (unknown type: {dev_addr.type})"
    )


def class_dev_hvac(
    dev_addr: Address, *, msg: Message | None = None, eavesdrop: bool = False
) -> type[DeviceHvac]:
    """Return a device class, but only if the device must be from the HVAC group.

    May return a base class, `DeviceHvac`, which will need promotion.
    """

    if not eavesdrop:
        raise exc.DeviceNotRecognised(
            f"No HVAC class for: {dev_addr} (no eavesdropping)"
        )

    if msg is None:
        raise exc.DeviceNotRecognised(f"No HVAC class for: {dev_addr} (no msg)")

    if klass := HVAC_KLASS_BY_VC_PAIR.get((msg.verb, msg.code)):
        return _HVAC_CLASS_BY_SLUG[klass]

    if msg.code in CODES_OF_HVAC_DOMAIN_ONLY:
        return DeviceHvac

    raise exc.DeviceNotRecognised(
        f"No HVAC class for: {dev_addr} (insufficient meta-data)"
    )

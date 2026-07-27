#!/usr/bin/env python3

"""RAMSES RF - Transport layer configuration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Never, TypeVar

import voluptuous as vol

from ..ramses_tx.config import EngineConfig
from ..ramses_tx.const import DEV_TYPE_MAP, DEVICE_ID_REGEX, DevType
from ..ramses_tx.schemas import SZ_BLOCK_LIST, SZ_KNOWN_LIST

_T = TypeVar("_T")


def ConvertNullToDict() -> Callable[[_T | None], _T | dict[Never, Never]]:
    def convert_null_to_dict(node_value: _T | None) -> _T | dict[Never, Never]:
        if node_value is None:
            return {}
        return node_value

    return convert_null_to_dict


SZ_ALIAS: Final = "alias"
SZ_BOUND_TO: Final = "bound"
SZ_CLASS: Final = "class"
SZ_FAKED: Final = "faked"
SZ_SCHEME: Final = "scheme"

SCH_DEVICE_ID_ANY = vol.Match(DEVICE_ID_REGEX.ANY)
SCH_DEVICE_ID_SEN = vol.Match(DEVICE_ID_REGEX.SEN)
SCH_DEVICE_ID_CTL = vol.Match(DEVICE_ID_REGEX.CTL)
SCH_DEVICE_ID_DHW = vol.Match(DEVICE_ID_REGEX.DHW)
SCH_DEVICE_ID_HGI = vol.Match(DEVICE_ID_REGEX.HGI)
SCH_DEVICE_ID_APP = vol.Match(DEVICE_ID_REGEX.APP)
SCH_DEVICE_ID_BDR = vol.Match(DEVICE_ID_REGEX.BDR)
SCH_DEVICE_ID_UFC = vol.Match(DEVICE_ID_REGEX.UFC)

_SCH_TRAITS_DOMAINS = ("heat", "hvac")
_SCH_TRAITS_HVAC_SCHEMES = ("itho", "nuaire", "orcon", "vasco", "climarad")


def sch_global_traits_dict_factory(
    heat_traits: dict[vol.Optional, vol.Any] | None = None,
    hvac_traits: dict[vol.Optional, vol.Any] | None = None,
) -> tuple[dict[vol.Optional, vol.Any], vol.Any]:
    """Return a global traits dict with a configurable extra traits."""

    heat_traits = heat_traits or {}
    hvac_traits = hvac_traits or {}

    SCH_TRAITS_BASE = vol.Schema(
        {
            vol.Optional(SZ_ALIAS, default=None): vol.Any(None, str),
            vol.Optional(SZ_FAKED, default=None): vol.Any(None, bool),
            vol.Optional(vol.Remove("_note")): str,
        },
        extra=vol.PREVENT_EXTRA,
    )

    heat_slugs = list(
        str(s) for s in DEV_TYPE_MAP.slugs() if s not in DEV_TYPE_MAP.HVAC_SLUGS
    )
    SCH_TRAITS_HEAT = SCH_TRAITS_BASE.extend(
        {
            vol.Optional("_domain", default="heat"): "heat",
            vol.Optional(SZ_CLASS): vol.Any(
                None, *heat_slugs, *(str(DEV_TYPE_MAP[s]) for s in heat_slugs)
            ),
        }
    )
    SCH_TRAITS_HEAT = SCH_TRAITS_HEAT.extend(
        heat_traits,
        extra=vol.PREVENT_EXTRA,
    )

    hvac_slugs = list(str(s) for s in DEV_TYPE_MAP.HVAC_SLUGS)
    SCH_TRAITS_HVAC = SCH_TRAITS_BASE.extend(
        {
            vol.Optional("_domain", default="hvac"): "hvac",
            vol.Optional(SZ_CLASS, default="HVC"): vol.Any(
                None, *hvac_slugs, *(str(DEV_TYPE_MAP[s]) for s in hvac_slugs)
            ),
            vol.Optional(SZ_BOUND_TO): vol.Any(None, vol.Match(DEVICE_ID_REGEX.ANY)),
        }
    )
    SCH_TRAITS_HVAC = SCH_TRAITS_HVAC.extend(
        {vol.Optional(SZ_SCHEME): vol.Any(*_SCH_TRAITS_HVAC_SCHEMES)}
    )
    SCH_TRAITS_HVAC = SCH_TRAITS_HVAC.extend(
        hvac_traits,
        extra=vol.PREVENT_EXTRA,
    )

    SCH_TRAITS = vol.Any(
        vol.All(None, ConvertNullToDict()),
        vol.Any(SCH_TRAITS_HEAT, SCH_TRAITS_HVAC),
        extra=vol.PREVENT_EXTRA,
    )
    SCH_DEVICE = vol.Schema(
        {vol.Optional(SCH_DEVICE_ID_ANY): SCH_TRAITS},
        extra=vol.PREVENT_EXTRA,
    )

    global_traits_dict = {
        vol.Optional(SZ_KNOWN_LIST, default={}): vol.Any(
            vol.All(None, ConvertNullToDict()),
            vol.All(SCH_DEVICE, vol.Length(min=0)),
        ),
        vol.Optional(SZ_BLOCK_LIST, default={}): vol.Any(
            vol.All(None, ConvertNullToDict()),
            vol.All(SCH_DEVICE, vol.Length(min=0)),
        ),
    }

    return global_traits_dict, SCH_TRAITS


SCH_GLOBAL_TRAITS_DICT, SCH_TRAITS = sch_global_traits_dict_factory()


@dataclass
class GatewayConfig:
    """Configuration parameters for the Ramses Gateway.

    :param disable_discovery: Disable device discovery, defaults to False.
    :type disable_discovery: bool
    :param enable_eavesdrop: Enable eavesdropping mode, defaults to False.
    :type enable_eavesdrop: bool
    :param reduce_processing: Level of reduced processing, defaults to 0.
    :type reduce_processing: int
    :param max_zones: Maximum number of zones allowed, defaults to 12.
    :type max_zones: int
    :param use_aliases: Mapping of aliases for device IDs.
    :type use_aliases: dict[str, str]
    :param enforce_strict_handling: Enforce strict handling of packets.
    :type enforce_strict_handling: bool
    :param use_native_ot: Preference for using native OpenTherm.
    :type use_native_ot: Literal["always", "prefer", "avoid", "never"] | None
    :param schema: Dictionary representing the schema.
    :type schema: dict[str, Any]
    :param debug_mode: If True, set the logger to debug mode.
    :type debug_mode: bool
    :param gateway_timeout: Custom timeout threshold in minutes.
    :type gateway_timeout: int | None
    :param database_path: Target disk path for the SQLite DB.
    :type database_path: str | None
    :param known_list: A list of known device IDs and their traits.
    :type known_list: dict[str, Any]
    :param block_list: A list of blocked device IDs.
    :type block_list: dict[str, Any]
    :param engine: Typed configuration object for the Transport layer.
    :type engine: EngineConfig
    :param hgi_id: The explicit Device ID of the active HGI hardware.
    :type hgi_id: str | None
    """

    disable_discovery: bool = False
    enable_eavesdrop: bool = False
    reduce_processing: int = 0
    max_zones: int = 12
    use_aliases: dict[str, str] = field(default_factory=dict)
    enforce_strict_handling: bool = False
    use_native_ot: Literal["always", "prefer", "avoid", "never"] | None = None

    schema: dict[str, Any] = field(default_factory=dict)
    debug_mode: bool = False
    gateway_timeout: int | None = None
    database_path: str | None = "ramses.db"

    known_list: dict[str, Any] = field(default_factory=dict)
    block_list: dict[str, Any] = field(default_factory=dict)

    # Transport layer configuration encapsulated perfectly
    engine: EngineConfig = field(default_factory=EngineConfig)

    hgi_id: str | None = None

    def __post_init__(self) -> None:
        """Initialize computed properties natively on startup."""
        if not self.hgi_id:
            explicit_hgis = [
                k
                for k, v in self.known_list.items()
                if v.get(SZ_CLASS) in (DevType.HGI, DEV_TYPE_MAP[DevType.HGI])
            ]
            implicit_hgis = [
                k
                for k, v in self.known_list.items()
                if not v.get(SZ_CLASS) and k[:2] == DEV_TYPE_MAP._hex(DevType.HGI)
            ]

            if explicit_hgis:
                self.hgi_id = explicit_hgis[0]
            elif implicit_hgis:
                self.hgi_id = implicit_hgis[0]

    @property
    def mac_filter_list(self) -> list[str]:
        """Return a flattened list of MAC addresses from the known_list."""
        return list(self.known_list.keys())

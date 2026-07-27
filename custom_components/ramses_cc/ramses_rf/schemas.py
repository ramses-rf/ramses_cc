#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

:term:`Schema` processor for upper layer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, cast

import voluptuous as vol

# TODO: deprecate re-exporting (via as) in favour of direct imports
from ..ramses_tx.const import (
    SZ_ACTUATORS as SZ_ACTUATORS,
    SZ_CONFIG as SZ_CONFIG,
    SZ_DEVICES as SZ_DEVICES,
    SZ_NAME,
    SZ_SENSOR as SZ_SENSOR,
    SZ_ZONE_TYPE,
    SZ_ZONES,
)
from ..ramses_tx.schemas import (  # noqa: F401
    SCH_ENGINE_DICT,
    SZ_BLOCK_LIST,
    SZ_DISABLE_SENDING,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST as SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    sch_packet_log_dict_factory,
    select_device_filter_mode,
)
from ..ramses_tx.typing import DeviceIdT

from . import exceptions as exc

# Import elevated L7 domain concepts from ramses_rf.config instead of ramses_tx.schemas
from .config import (
    SCH_DEVICE_ID_ANY,
    SCH_DEVICE_ID_APP,
    SCH_DEVICE_ID_BDR,
    SCH_DEVICE_ID_CTL,
    SCH_DEVICE_ID_DHW,
    SCH_DEVICE_ID_SEN,
    SCH_DEVICE_ID_UFC,
    SCH_GLOBAL_TRAITS_DICT,
    SCH_TRAITS as SCH_TRAITS,
    SZ_ALIAS as SZ_ALIAS,
    SZ_BOUND_TO as SZ_BOUND_TO,
    SZ_CLASS as SZ_CLASS,
    SZ_FAKED as SZ_FAKED,
    SZ_SCHEME as SZ_SCHEME,
)

# TODO: deprecate re-exporting (via as) in favour of direct imports
from .const import (
    DEFAULT_MAX_ZONES as DEFAULT_MAX_ZONES,
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    DEVICE_ID_REGEX,
    DONT_CREATE_MESSAGES,
    SZ_ZONE_IDX,
    ZON_ROLE_MAP,
    DevRole,
    DevType,
    SystemType,
)

if TYPE_CHECKING:
    from .devices import Device
    from .gateway import Gateway
    from .systems import Evohome


_LOGGER = logging.getLogger(__name__)


#
# 0/5: Schema strings
SZ_SCHEMA: Final = "schema"
SZ_MAIN_TCS: Final = "main_tcs"

SZ_CONTROLLER = DEV_TYPE_MAP[DevType.CTL]
SZ_SYSTEM: Final = "system"
SZ_APPLIANCE_CONTROL = DEV_ROLE_MAP[DevRole.APP]
SZ_ORPHANS: Final = "orphans"
SZ_ORPHANS_HEAT: Final = "orphans_heat"
SZ_ORPHANS_HVAC: Final = "orphans_hvac"

SZ_DHW_SYSTEM: Final = "stored_hotwater"
SZ_DHW_SENSOR = DEV_ROLE_MAP[DevRole.DHW]
SZ_DHW_VALVE = DEV_ROLE_MAP[DevRole.HTG]
SZ_HTG_VALVE = DEV_ROLE_MAP[DevRole.HT1]

SZ_SENSOR_FAKED: Final = "sensor_faked"

SZ_UFH_SYSTEM: Final = "underfloor_heating"
SZ_UFH_CTL = DEV_TYPE_MAP[DevType.UFC]  # ufh_controller
SZ_CIRCUITS: Final = "circuits"

HEAT_ZONES_STRS = tuple(ZON_ROLE_MAP[t] for t in ZON_ROLE_MAP.HEAT_ZONES)

SCH_DOM_ID = vol.Match(r"^[0-9A-F]{2}$")
SCH_UFH_IDX = vol.Match(r"^0[0-8]$")
SCH_ZON_IDX = vol.Match(r"^0[0-9AB]$")  # TODO: what if > 12 zones? (e.g. hometronics)


def ErrorRenamedKey(new_key: str) -> Callable[[Any], None]:
    def renamed_key(node_value: Any) -> None:
        raise vol.Invalid(f"the key name has changed: rename it to '{new_key}'")

    return renamed_key


#
# 1/7: Schemas for CH/DHW systems, aka Heat/TCS (temp control systems)
SCH_TCS_SYS_CLASS = (SystemType.EVOHOME, SystemType.HOMETRONICS, SystemType.SUNDIAL)
SCH_TCS_SYS = vol.Schema(
    {
        vol.Required(SZ_APPLIANCE_CONTROL, default=None): vol.Any(
            None, SCH_DEVICE_ID_APP
        ),
        vol.Optional("heating_control"): ErrorRenamedKey(SZ_APPLIANCE_CONTROL),
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_TCS_DHW = vol.Schema(
    {
        vol.Optional(SZ_SENSOR, default=None): vol.Any(None, SCH_DEVICE_ID_DHW),
        vol.Optional(SZ_DHW_VALVE, default=None): vol.Any(None, SCH_DEVICE_ID_BDR),
        vol.Optional(SZ_HTG_VALVE, default=None): vol.Any(None, SCH_DEVICE_ID_BDR),
        vol.Optional(SZ_DHW_SENSOR): ErrorRenamedKey(SZ_SENSOR),
    },
    extra=vol.PREVENT_EXTRA,
)

_CH_TCS_UFH_CIRCUIT = vol.Schema(
    {
        vol.Required(SCH_UFH_IDX): vol.Schema(
            {vol.Optional(SZ_ZONE_IDX): SCH_ZON_IDX},
        ),
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_TCS_UFH = vol.All(
    vol.Schema(
        {
            vol.Required(SCH_DEVICE_ID_UFC): vol.Any(
                None, {vol.Optional(SZ_CIRCUITS): vol.Any(None, dict)}
            )
        }
    ),
    vol.Length(min=1, max=3),
    extra=vol.PREVENT_EXTRA,
)

SCH_TCS_ZONES_ZON = vol.Schema(
    {
        vol.Optional(SZ_CLASS, default=None): vol.Any(None, *HEAT_ZONES_STRS),
        vol.Optional(SZ_SENSOR, default=None): vol.Any(None, SCH_DEVICE_ID_SEN),
        vol.Optional(SZ_DEVICES): ErrorRenamedKey(SZ_ACTUATORS),
        vol.Optional(SZ_ACTUATORS, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY], vol.Length(min=0)
        ),
        vol.Optional(SZ_ZONE_TYPE): ErrorRenamedKey(SZ_CLASS),
        vol.Optional("zone_sensor"): ErrorRenamedKey(SZ_SENSOR),
        # vol.Optional(SZ_SENSOR_FAKED): bool,
        vol.Optional(f"_{SZ_NAME}"): vol.Any(None, str),
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_TCS_ZONES = vol.All(
    vol.Schema({vol.Required(SCH_ZON_IDX): SCH_TCS_ZONES_ZON}),
    vol.Length(min=1, max=12),
    extra=vol.PREVENT_EXTRA,
)

SCH_TCS = vol.Schema(
    {
        vol.Optional(SZ_SYSTEM, default={}): vol.Any({}, SCH_TCS_SYS),
        vol.Optional(SZ_DHW_SYSTEM, default={}): vol.Any({}, SCH_TCS_DHW),
        vol.Optional(SZ_UFH_SYSTEM, default={}): vol.Any({}, SCH_TCS_UFH),
        vol.Optional(SZ_ORPHANS, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY], vol.Unique()
        ),
        vol.Optional(SZ_ZONES, default={}): vol.Any({}, SCH_TCS_ZONES),
        vol.Optional(vol.Remove("is_tcs")): vol.Coerce(bool),
    },
    extra=vol.PREVENT_EXTRA,
)


#
# 2/7: Schemas for Ventilation control systems, aka HVAC/VCS
SZ_REMOTES: Final = "remotes"
SZ_SENSORS: Final = "sensors"

SCH_VCS_DATA = vol.Schema(
    {
        vol.Optional(SZ_REMOTES, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY],
            vol.Unique(),  # vol.Length(min=1)
        ),
        vol.Optional(SZ_SENSORS, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY],
            vol.Unique(),  # vol.Length(min=1)
        ),
        vol.Optional(vol.Remove("is_vcs")): vol.Coerce(bool),
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_VCS_KEYS = vol.Schema(
    {
        vol.Required(
            vol.Any(SZ_REMOTES, SZ_SENSORS),
            msg=(
                "The ventilation control system schema must include at least "
                f"one of [{SZ_REMOTES}, {SZ_SENSORS}]"
            ),
        ): object
    },
    extra=vol.ALLOW_EXTRA,  # must be ALLOW_EXTRA, as is a subset of SCH_VCS_DATA
)
SCH_VCS = vol.All(SCH_VCS_KEYS, SCH_VCS_DATA)


#
# 3/7: Global Schema for Heat/HVAC systems
SCH_GLOBAL_SCHEMAS_DICT = {  # System schemas - can be 0-many Heat/HVAC schemas
    # orphans are devices to create that won't be in a (cached) schema...
    vol.Optional(SZ_MAIN_TCS): vol.Any(None, SCH_DEVICE_ID_CTL),
    vol.Optional(vol.Remove("main_controller")): vol.Any(None, SCH_DEVICE_ID_CTL),
    vol.Optional(SCH_DEVICE_ID_CTL): vol.Any(SCH_TCS, SCH_VCS),
    vol.Optional(SCH_DEVICE_ID_ANY): SCH_VCS,  # must be after SCH_DEVICE_ID_CTL
    vol.Optional(SZ_ORPHANS_HEAT): vol.All([SCH_DEVICE_ID_ANY], vol.Unique()),
    vol.Optional(SZ_ORPHANS_HVAC): vol.All([SCH_DEVICE_ID_ANY], vol.Unique()),
    vol.Optional("transport_constructor"): vol.Any(callable, None),
}
SCH_GLOBAL_SCHEMAS = vol.Schema(SCH_GLOBAL_SCHEMAS_DICT, extra=vol.PREVENT_EXTRA)

#
# 4/7: Gateway (parser/state) configuration
SZ_DISABLE_DISCOVERY: Final = "disable_discovery"
SZ_ENABLE_EAVESDROP: Final = "enable_eavesdrop"
SZ_ENFORCE_STRICT_HANDLING: Final = "enforce_strict_handling"
SZ_MAX_ZONES: Final = "max_zones"  # TODO: move to TCS-attr from GWY-layer
SZ_REDUCE_PROCESSING: Final = "reduce_processing"
SZ_USE_ALIASES: Final = "use_aliases"  # use friendly device names from known_list
SZ_USE_NATIVE_OT: Final = "use_native_ot"  # favour OT (3220s) over RAMSES

SCH_GATEWAY_DICT = {
    vol.Optional(SZ_DISABLE_DISCOVERY, default=False): bool,
    vol.Optional(SZ_ENABLE_EAVESDROP, default=False): bool,
    vol.Optional(SZ_ENFORCE_STRICT_HANDLING, default=False): bool,
    vol.Optional(SZ_MAX_ZONES, default=DEFAULT_MAX_ZONES): vol.All(
        int, vol.Range(min=1, max=16)
    ),  # NOTE: no default
    vol.Optional(SZ_REDUCE_PROCESSING, default=0): vol.All(
        int, vol.Range(min=0, max=DONT_CREATE_MESSAGES)
    ),
    vol.Optional(SZ_USE_ALIASES, default=False): bool,
    vol.Optional(SZ_USE_NATIVE_OT, default="prefer"): vol.Any(
        "always", "prefer", "avoid", "never"
    ),
}
SCH_GATEWAY_CONFIG = vol.Schema(SCH_GATEWAY_DICT, extra=vol.REMOVE_EXTRA)


#
# 5/7: the Global (gateway) Schema
SCH_GLOBAL_CONFIG = (
    vol.Schema(
        {
            # Gateway/engine Configuration, incl. packet_log, serial_port params...
            vol.Optional(SZ_CONFIG, default={}): SCH_GATEWAY_DICT | SCH_ENGINE_DICT
        },
        extra=vol.PREVENT_EXTRA,
    )
    .extend(SCH_GLOBAL_SCHEMAS_DICT)
    .extend(SCH_GLOBAL_TRAITS_DICT)
    .extend(sch_packet_log_dict_factory(default_backups=0))
)


#
# 6/7: External Schemas, to be used by clients of this library
def NormaliseRestoreCache() -> Callable[[bool | dict[str, bool]], dict[str, bool]]:
    """Convert a shorthand restore_cache bool to a dict.

    restore_cache: bool ->  restore_cache:
                              restore_schema: bool
                              restore_state: bool
    """

    def normalise_restore_cache(node_value: bool | dict[str, bool]) -> dict[str, bool]:
        if isinstance(node_value, dict):
            return node_value
        return {SZ_RESTORE_SCHEMA: node_value, SZ_RESTORE_STATE: node_value}

    return normalise_restore_cache


SZ_RESTORE_CACHE: Final = "restore_cache"
SZ_RESTORE_SCHEMA: Final = "restore_schema"
SZ_RESTORE_STATE: Final = "restore_state"

SCH_RESTORE_CACHE_DICT = {
    vol.Optional(SZ_RESTORE_CACHE, default=True): vol.Any(
        vol.All(bool, NormaliseRestoreCache()),
        vol.Schema(
            {
                vol.Optional(SZ_RESTORE_SCHEMA, default=True): bool,
                vol.Optional(SZ_RESTORE_STATE, default=True): bool,
            }
        ),
    )
}


#
# 7/7: Other stuff
def _get_device(gwy: Gateway, dev_id: DeviceIdT, **kwargs: Any) -> Device:  # , **traits
    """Get a device from the gateway.

    Raise a DeviceNotFoundError if a device_id is filtered out by the known or block list.

    The underlying method is wrapped only to provide a better error message.
    """

    def check_filter_lists(dev_id: DeviceIdT) -> None:
        """Raise a DeviceNotFoundError if a device_id is filtered out by a list."""

        err_msg = None
        if gwy._engine._enforce_known_list and dev_id not in gwy._engine._include:
            err_msg = f"it is in the {SZ_SCHEMA}, but not in the {SZ_KNOWN_LIST}"
        # issue ramses_cc #296: if enforce_known_list is turned on, error on any "unknown" dev_id
        # fix: delete from schema?
        if dev_id in gwy._engine._exclude:
            err_msg = f"it is in the {SZ_SCHEMA}, but also in the {SZ_BLOCK_LIST}"

        if err_msg:
            raise exc.DeviceNotFoundError(
                f"Can't create {dev_id}: {err_msg} (check the lists and the {SZ_SCHEMA})"
            )

    check_filter_lists(dev_id)

    return cast("Device", gwy.device_registry.get_device(dev_id, **kwargs))


def load_schema(
    gwy: Gateway, known_list: dict[DeviceIdT, Any] | None = None, **schema: Any
) -> None:
    """Instantiate all entities in the schema, and faked devices in the known_list."""

    from .devices import Fakeable  # circular import

    known_list = known_list or {}

    # schema: dict = SCH_GLOBAL_SCHEMAS_DICT(schema)

    [
        load_tcs(gwy, ctl_id, schema)  # type: ignore[arg-type]
        for ctl_id, schema in schema.items()
        if re.match(DEVICE_ID_REGEX.ANY, ctl_id) and SZ_REMOTES not in schema
    ]
    if schema.get(SZ_MAIN_TCS):
        gwy._tcs = gwy.device_registry.system_by_id.get(schema[SZ_MAIN_TCS])
    [
        load_fan(gwy, fan_id, schema)  # type: ignore[arg-type]
        for fan_id, schema in schema.items()
        if re.match(DEVICE_ID_REGEX.ANY, fan_id) and SZ_REMOTES in schema
    ]
    [  # NOTE: class favoured, domain ignored
        _get_device(gwy, device_id)  # domain=key[-4:])
        for key in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC)
        for device_id in schema.get(key, [])
    ]  # TODO: pass domain (Heat/HVAC), or generalise to SZ_ORPHANS

    # create any devices in the known list that are faked, or fake those already created
    for device_id, traits in known_list.items():
        if traits.get(SZ_FAKED):
            dev = _get_device(gwy, device_id)  # , **traits)
            if not isinstance(dev, Fakeable):
                raise exc.DeviceNotFaked(f"Device is not fakeable: {dev}")
            if not dev.is_faked:
                dev._make_fake()


def load_fan(gwy: Gateway, fan_id: DeviceIdT, schema: dict[str, Any]) -> Device:
    """Create a FAN using its schema (i.e. with remotes, sensors)."""

    fan = _get_device(gwy, fan_id)
    # fan._update_schema(**schema)  # TODO

    return fan


def load_tcs(gwy: Gateway, ctl_id: DeviceIdT, schema: dict[str, Any]) -> Evohome:
    """Create a TCS using its schema."""
    # print(schema)
    # schema = SCH_TCS_ZONES_ZON(schema)

    ctl = _get_device(gwy, ctl_id)
    if ctl.tcs is None:
        raise exc.SchemaInconsistentError(f"No TCS assigned to controller {ctl.id}")
    ctl.tcs._update_schema(**schema)

    for dev_id in schema.get(SZ_UFH_SYSTEM, {}):  # UFH controllers
        _get_device(gwy, dev_id, parent=ctl.tcs)  # , **_schema)

    for dev_id in schema.get(SZ_ORPHANS, []):
        _get_device(gwy, dev_id, parent=ctl)

    # if DEV_MODE:
    #     import json

    #     src = json.dumps(shrink(schema), sort_keys=True)
    #     dst = json.dumps(shrink(gwy.device_registry.system_by_id[ctl.id].schema), sort_keys=True)
    #     # assert dst == src, "They don't match!"
    #     print(src)
    #     print(dst)

    return ctl.tcs

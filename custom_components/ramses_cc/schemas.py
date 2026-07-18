"""Schemas for RAMSES integration."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import timedelta as td
from typing import Any, Final

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers import config_validation as cv

from ramses_rf.config import sch_global_traits_dict_factory
from ramses_rf.helpers import deep_merge, is_subset, shrink
from ramses_rf.schemas import (
    SCH_GATEWAY_CONFIG,
    SCH_GLOBAL_SCHEMAS_DICT,
    SCH_RESTORE_CACHE_DICT,
    SZ_APPLIANCE_CONTROL,
    SZ_BOUND_TO,
    SZ_CLASS,
    SZ_CONFIG,
    SZ_DHW_SYSTEM,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
    SZ_ORPHANS_HEAT,
    SZ_ORPHANS_HVAC,
    SZ_REMOTES,
    SZ_RESTORE_CACHE,
    SZ_SENSOR,
    SZ_SENSORS,
    SZ_SYSTEM,
    SZ_UFH_SYSTEM,
)
from ramses_tx.const import (
    COMMAND_REGEX,
    DEFAULT_GAP_DURATION,
    # DEFAULT_NUM_REPEATS,  # use 3 in ramses_cc Actions, not 0 like ramses_tx
    MAX_GAP_DURATION,  # renamed from local MAX_DELAY_SECS
    MAX_NUM_REPEATS,
    MIN_GAP_DURATION,  # renamed from local MIN_DELAY_SECS
    MIN_NUM_REPEATS,
    SZ_ACTUATORS,
    SZ_ZONES,
)
from ramses_tx.schemas import (
    SCH_ENGINE_DICT,
    SZ_KNOWN_LIST,
    SZ_PORT_CONFIG,
    SZ_SERIAL_PORT,
    extract_serial_port,
    sch_packet_log_dict_factory,
    sch_serial_port_dict_factory,
)

from .const import (
    ATTR_ACTIVE,
    ATTR_CO2_LEVEL,
    ATTR_COMMAND,
    ATTR_DELAY_SECS,
    ATTR_DEVICE_ID,
    ATTR_DIFFERENTIAL,
    ATTR_DURATION,
    ATTR_INDOOR_HUMIDITY,
    ATTR_LOCAL_OVERRIDE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_MODE,
    ATTR_MULTIROOM,
    ATTR_NUM_ENTRIES,
    ATTR_NUM_REPEATS,
    ATTR_OPENWINDOW,
    ATTR_OVERRUN,
    ATTR_PERIOD,
    ATTR_SCHEDULE,
    ATTR_SETPOINT,
    ATTR_TEMPERATURE,
    ATTR_TIMEOUT,
    ATTR_UNTIL,
    CONF_ADVANCED_FEATURES,
    CONF_AUTO_NOTIFY,
    CONF_COMMANDS,
    CONF_DEV_MODE,
    CONF_LOST_THRESHOLD,
    CONF_MESSAGE_EVENTS,
    CONF_PASSIVE_SCAN,
    CONF_RAMSES_RF,
    CONF_SEND_PACKET,
    CONF_UNKNOWN_CODES,
    SZ_DEVICE_COMMENTS,
    SZ_OWNER,
    SZ_TR_NAME,
    SZ_TR_OWNER,
    SZ_TR_SKIPPED,
    SystemMode,
    ZoneMode,
)

_SchemaT = dict[str, Any]

_LOGGER = logging.getLogger(__name__)

# send_command service action
DEFAULT_NUM_REPEATS: Final[int] = 3  # override ramses_rf DEFAULT_NUM_REPEATS

# Configuration schema for Integration/domain
SCAN_INTERVAL_DEFAULT = td(seconds=60)
SCAN_INTERVAL_MINIMUM = td(seconds=3)

# Schema regex matches
_SCH_DEVICE_ID = cv.matches_regex(r"^[0-9]{2}:[0-9]{6}$")
_SCH_CMD_CODE = cv.matches_regex(r"^[0-9A-F]{4}$")
_SCH_DOM_IDX = cv.matches_regex(r"^[0-9A-F]{2}$")
_SCH_PARAM_ID = vol.All(cv.string, cv.matches_regex(r"^[0-9A-F]{2}$"))
_SCH_COMMAND = cv.matches_regex(COMMAND_REGEX.pattern)

SCH_ADVANCED_FEATURES = vol.Schema(
    {
        vol.Optional(CONF_SEND_PACKET, default=False): cv.boolean,
        vol.Optional(CONF_MESSAGE_EVENTS, default=None): vol.Any(None, cv.is_regex),
        vol.Optional(CONF_DEV_MODE): cv.boolean,
        vol.Optional(CONF_UNKNOWN_CODES): cv.boolean,
        vol.Optional(CONF_PASSIVE_SCAN, default=False): cv.boolean,
        vol.Optional(CONF_AUTO_NOTIFY, default=True): cv.boolean,
        vol.Optional(CONF_LOST_THRESHOLD, default=7): vol.All(
            cv.positive_int, vol.Range(min=1, max=90)
        ),
    }
)

# Define the traits for FAN devices
FAN_TRAITS = {
    vol.Optional(SZ_BOUND_TO): vol.Any(None, _SCH_DEVICE_ID),
    vol.Optional(CONF_COMMANDS): dict,
}

SCH_GLOBAL_TRAITS_DICT, SCH_TRAITS = sch_global_traits_dict_factory(
    hvac_traits=FAN_TRAITS
)

SCH_GATEWAY_CONFIG = SCH_GATEWAY_CONFIG.extend(
    SCH_ENGINE_DICT,
    extra=vol.PREVENT_EXTRA,
)

SCH_PACKET_LOG = sch_packet_log_dict_factory(default_backups=7)

SCH_DOMAIN_CONFIG = (
    vol.Schema(
        {
            vol.Optional(CONF_RAMSES_RF, default={}): SCH_GATEWAY_CONFIG,
            vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL_DEFAULT): vol.All(
                cv.time_period, vol.Range(min=SCAN_INTERVAL_MINIMUM)
            ),
            vol.Optional(CONF_ADVANCED_FEATURES, default={}): SCH_ADVANCED_FEATURES,
        },
        extra=vol.PREVENT_EXTRA,  # will be system, orphan schemas for ramses_rf
    )
    .extend(SCH_GLOBAL_SCHEMAS_DICT)
    .extend(SCH_GLOBAL_TRAITS_DICT)
    .extend(sch_packet_log_dict_factory(default_backups=7))
    .extend(SCH_RESTORE_CACHE_DICT)
    .extend(sch_serial_port_dict_factory())
)

SCH_MINIMUM_TCS = vol.Schema(
    {
        vol.Optional(SZ_SYSTEM): vol.Schema(
            {vol.Required(SZ_APPLIANCE_CONTROL): vol.Match(r"^10:[0-9]{6}$")}
        ),
        vol.Optional(SZ_ZONES, default={}): vol.Schema(
            {
                vol.Required(str): vol.Schema(
                    {vol.Required(SZ_SENSOR): vol.Match(r"^01:[0-9]{6}$")}
                )
            }
        ),
    },
    extra=vol.PREVENT_EXTRA,
)


def normalise_config(config: _SchemaT) -> tuple[str, _SchemaT, _SchemaT]:
    """Return a port/client_config/coordinator_config for the library.

    Extracts and separates the configuration into three parts: the serial port name,
    the configuration for the ramses_rf library (client), and the configuration
    for the HA coordinator (including polling intervals and remote commands).

    :param config: The raw configuration dictionary from Home Assistant.
    :return: A tuple containing:
        - The serial port name (str).
        - The client/library configuration dictionary (_SchemaT).
        - The coordinator configuration dictionary (_SchemaT).
    """

    config = deepcopy(config)

    config[SZ_CONFIG] = config.pop(CONF_RAMSES_RF)

    port_name, port_config = extract_serial_port(config.pop(SZ_SERIAL_PORT))

    # Check if 'v' is truthy (not None) before calling .get()
    # This prevents crashes when a known_list entry is null (e.g. "01:123456": null)
    remote_commands = {
        k: v.pop(CONF_COMMANDS)
        for k, v in config[SZ_KNOWN_LIST].items()
        if v and v.get(CONF_COMMANDS)
    }

    coordinator_keys = (CONF_SCAN_INTERVAL, CONF_ADVANCED_FEATURES, SZ_RESTORE_CACHE)
    return (
        port_name,
        {k: v for k, v in config.items() if k not in coordinator_keys}
        | {SZ_PORT_CONFIG: port_config},
        {k: v for k, v in config.items() if k in coordinator_keys}
        | {"remotes": remote_commands},
    )


def strip_traits_for_validation(schema: _SchemaT) -> _SchemaT:
    """Strip ``_`` prefixed keys and trait-only entries for schema validation.

    ramses_rf's ``SCH_GLOBAL_SCHEMAS`` validator rejects ``_`` prefixed keys
    (user-authored traits like ``_disabled``, ``_name``, ``_alias``) and
    trait-only top-level entries (e.g. ``{"04:111111": {"_disabled": True}}``).

    This function recursively removes all ``_`` prefixed keys from the schema
    and removes top-level device-ID keys whose value would be empty after
    stripping (i.e. trait-only entries).  The result is safe to pass to
    ``SCH_GLOBAL_SCHEMAS`` for validation.

    :param schema: The full schema dict (with traits).
    :return: A cleaned schema dict without ``_`` keys, safe for validation.
    """
    import re

    _DEVICE_ID_RE = re.compile(r"^[0-9]{2}:[0-9]{6}$")
    # Heat-side prefixes that should never be treated as VCS at root level.
    # Any non-01: device NOT in this set is assumed to be HVAC.  Rather than
    # injecting remotes: [] (which creates a fake VCS entry), we move such
    # devices to orphans_hvac where they belong until we know more.
    # See schema_architecture.md, "Device ID prefixes for HVAC".
    _HEAT_PREFIXES = frozenset(("01:", "04:", "07:", "08:", "10:", "13:", "22:", "34:"))

    def _strip_traits(obj: Any) -> Any:
        """Recursively strip _ prefixed keys from dicts."""
        if isinstance(obj, dict):
            return {
                k: _strip_traits(v)
                for k, v in obj.items()
                if not str(k).startswith("_")
            }
        return obj

    # Collect HVAC device IDs that need to be moved to orphans_hvac
    # (non-heat devices at root level without remotes/sensors — ramses_rf's
    # SCH_GLOBAL_SCHEMAS would reject them as invalid VCS entries).
    hvac_to_orphan: set[str] = set()

    # Collect device IDs that already appear in remotes/sensors/actuators
    # lists inside other device entries.  A root entry for such a device
    # should NOT be moved to orphans_hvac — it's already placed in its
    # parent's list, and moving it would create a duplicate.
    placed_in_lists: set[str] = set()
    for _k, _v in schema.items():
        if not isinstance(_v, dict):
            continue
        for _list_key in ("remotes", "sensors", "actuators"):
            if _list_key in _v and isinstance(_v[_list_key], list):
                placed_in_lists.update(_v[_list_key])

    cleaned: _SchemaT = {}
    for key, value in schema.items():
        # Skip top-level _ prefixed keys (root _owner, etc.) — ramses_rf
        # doesn't understand them and they'd fail validation.
        if str(key).startswith("_"):
            continue
        # Track if the original had _ keys before stripping
        had_traits = isinstance(value, dict) and any(
            str(k).startswith("_") for k in value
        )
        # Strip _ keys from the value
        stripped = _strip_traits(value)

        # Non-heat device at root level without remotes/sensors — move to
        # orphans_hvac instead of keeping it as an invalid VCS entry.
        # ramses_rf's SCH_GLOBAL_SCHEMAS treats root-level non-CTL devices
        # as VCS, requiring remotes or sensors.  We don't know enough about
        # the device yet (prefix is ambiguous: 29:/37:/32: can be FAN/REM/
        # CO2/HUM), so orphans_hvac is the safe place.
        # This check comes before the trait-only drop so that trait-only
        # HVAC devices (e.g. {"_alias": "HRU"}) are also moved, not dropped.
        # EXCEPTION: if the device is already in a remotes/sensors/actuators
        # list inside another entry, don't move it to orphans — that would
        # create a duplicate.  Just drop the root entry instead.
        if (
            isinstance(value, dict)
            and _DEVICE_ID_RE.match(str(key))
            and str(key)[:3] not in _HEAT_PREFIXES
            and "remotes" not in stripped
            and "sensors" not in stripped
        ):
            if str(key) in placed_in_lists:
                # Already placed in a parent's list — drop root entry,
                # don't move to orphans (would duplicate)
                continue
            hvac_to_orphan.add(str(key))
            continue

        # If this is a device-ID key that had traits and is now empty
        # after stripping, it was a trait-only entry — drop it
        if (
            had_traits
            and isinstance(value, dict)
            and _DEVICE_ID_RE.match(str(key))
            and not stripped
        ):
            continue

        cleaned[key] = stripped

    # Add moved devices to orphans_hvac
    if hvac_to_orphan:
        existing = set(cleaned.get(SZ_ORPHANS_HVAC, []))
        existing |= hvac_to_orphan
        cleaned[SZ_ORPHANS_HVAC] = sorted(existing)

    return cleaned


# Heat-side prefixes (CH/DHW domain)
_HEAT_PREFIXES_SET = frozenset(
    (
        "01:",
        "02:",
        "04:",
        "07:",
        "08:",
        "10:",
        "12:",
        "13:",
        "17:",
        "22:",
        "23:",
        "30:",
        "34:",
    )
)
# HVAC-side prefixes (ventilation domain)
_HVAC_PREFIXES_SET = frozenset(("18:", "29:", "32:", "37:", "63:"))


def order_schema(schema: _SchemaT) -> _SchemaT:
    """Return a schema dict with keys ordered for human readability.

    Order:
    1. Root traits (_owner, _comment, etc.)
    2. main_tcs
    3. device_comments
    4. orphans_heat (needs work — visible at top)
    5. orphans_hvac (needs work — visible at top)
    6. Heat devices (01:, 04:, 07:, etc.) sorted by _owner then device ID
    7. HVAC devices (18:, 29:, 32:, 37:, etc.) sorted by _owner then device ID
    8. Any remaining keys (sorted)

    Devices without an _owner sort first (empty string sorts before any
    real owner name), so unclaimed devices bubble to the top of each group.

    :param schema: The schema dict to order.
    :return: A new dict with keys in the specified order.
    """
    if type(schema) is not dict:
        return schema

    root_traits: list[tuple[str, Any]] = []
    main_tcs: list[tuple[str, Any]] = []
    comments: list[tuple[str, Any]] = []
    heat_devices: list[tuple[str, Any]] = []
    hvac_devices: list[tuple[str, Any]] = []
    orphans_heat: list[tuple[str, Any]] = []
    orphans_hvac: list[tuple[str, Any]] = []
    other: list[tuple[str, Any]] = []

    for key, value in schema.items():
        skey = str(key)
        if skey.startswith("_"):
            root_traits.append((key, value))
        elif skey == SZ_MAIN_TCS:
            main_tcs.append((key, value))
        elif skey == SZ_DEVICE_COMMENTS:
            comments.append((key, value))
        elif skey == SZ_ORPHANS_HEAT:
            orphans_heat.append((key, value))
        elif skey == SZ_ORPHANS_HVAC:
            orphans_hvac.append((key, value))
        elif skey[:3] in _HEAT_PREFIXES_SET:
            heat_devices.append((key, value))
        elif skey[:3] in _HVAC_PREFIXES_SET:
            hvac_devices.append((key, value))
        else:
            other.append((key, value))

    # Sort device entries by _owner first, then device ID.
    # Devices without _owner (or with non-dict value) sort first.
    def _sort_key(kv: tuple[str, Any]) -> tuple[str, str]:
        owner = ""
        if isinstance(kv[1], dict):
            owner = str(kv[1].get(SZ_TR_OWNER, ""))
        return (owner, str(kv[0]))

    heat_devices.sort(key=_sort_key)
    hvac_devices.sort(key=_sort_key)
    other.sort(key=lambda kv: str(kv[0]))

    ordered: _SchemaT = {}
    for k, v in root_traits:
        ordered[k] = v
    for k, v in main_tcs:
        ordered[k] = v
    for k, v in comments:
        ordered[k] = v
    for k, v in orphans_heat:
        ordered[k] = v
    for k, v in orphans_hvac:
        ordered[k] = v
    for k, v in heat_devices:
        ordered[k] = v
    for k, v in hvac_devices:
        ordered[k] = v
    for k, v in other:
        ordered[k] = v

    return ordered


def merge_schemas(
    config_schema: _SchemaT,
    cached_schema: _SchemaT,
    *,
    schema_is_ssot: bool = False,
) -> _SchemaT | None:
    """Return the config schema deep merged into the cached schema.

    The **config schema is authoritative** for which devices exist — but
    only when *schema_is_ssot* is True (passive scan mode).  In legacy
    mode (no passive scan), the cache is the source of truth for topology
    because the config may not have a ``CONF_SCHEMA`` key at all (old YAML
    format stores device keys at the top level of options).

    When *schema_is_ssot* is True:
    - The cache is only used to restore learned topology for devices that
      ARE in the config schema.
    - Devices that the user removed from the config schema must NOT come
      back from the cache — they will be re-discovered by the passive scan.

    :param config_schema: The schema defined in the integration configuration.
    :param cached_schema: The schema restored from the client state cache.
    :param schema_is_ssot: When True, config schema is authoritative for
        device existence.  When False (legacy), cache is kept as-is.
    :return: A merged schema dictionary if successful, or None if the cached
        schema is incompatible or less complete than the config.
    """
    # Runtime guard: callers may pass non-dict despite the type hint
    if type(config_schema) is not dict or type(cached_schema) is not dict:
        _LOGGER.warning("merge_schemas: non-dict input, skipping merge")
        return None

    # Build a set of device IDs that the config schema says should exist.
    # This is the authoritative list — the cache cannot add devices that
    # the user removed.
    import re

    device_id_re = re.compile(r"^[0-9]{2}:[0-9]{6}$")
    config_device_ids: set[str] = set()
    for key in config_schema:
        if device_id_re.match(str(key)):
            config_device_ids.add(str(key))
    # Also include devices in orphan lists — they're in the config too
    for list_key in _LIST_KEYS:
        if list_key in config_schema and isinstance(config_schema[list_key], list):
            config_device_ids.update(config_schema[list_key])
    # Also include devices in remotes/sensors/actuators lists inside device
    # entries — these are part of the config schema too and must not be
    # filtered out by SSOT checks.
    for key, value in config_schema.items():
        if device_id_re.match(str(key)) and isinstance(value, dict):
            for list_key in _ZONE_LIST_KEYS:
                if list_key in value and isinstance(value[list_key], list):
                    config_device_ids.update(value[list_key])

    if is_subset(shrink(config_schema), shrink(cached_schema)):
        # Additional check: ensure cached schema doesn't have devices in
        # remotes/orphans that are not in the config schema
        cached_device_ids = set()
        for key in cached_schema:
            if device_id_re.match(str(key)):
                cached_device_ids.add(str(key))
            if isinstance(cached_schema[key], dict):
                for list_key in _ZONE_LIST_KEYS:
                    if list_key in cached_schema[key] and isinstance(
                        cached_schema[key][list_key], list
                    ):
                        cached_device_ids.update(cached_schema[key][list_key])
        for list_key in _LIST_KEYS:
            if list_key in cached_schema and isinstance(cached_schema[list_key], list):
                cached_device_ids.update(cached_schema[list_key])

        # Also check: config schema may have device root entries (e.g. FAN
        # with only _ traits and empty remotes) that shrink removes, making
        # is_subset think config is a subset of cached.  If config has device
        # IDs that cached doesn't, we must merge (not use cached directly).
        config_only_devices = config_device_ids - cached_device_ids

        if cached_device_ids.issubset(config_device_ids) and not config_only_devices:
            _LOGGER.info("Using the cached schema")
            result = cached_schema
        elif config_only_devices:
            _LOGGER.info(
                "Config schema has devices not in cached schema (%s), merging",
                sorted(config_only_devices),
            )
            result = deep_merge(config_schema, cached_schema)
        else:
            _LOGGER.info("Cached schema has extra devices in remotes/orphans, merging")
            result = deep_merge(config_schema, cached_schema)
    else:
        merged_schema: _SchemaT = deep_merge(config_schema, cached_schema)

        if is_subset(shrink(config_schema), shrink(merged_schema)):
            _LOGGER.info("Using a merged schema")
            result = merged_schema
        else:
            _LOGGER.info("Cached schema is a subset of config schema. Skipping cached.")
            return None

    # Filter: remove device-ID keys from the result that are NOT in the
    # config schema.  The config schema is authoritative — the cache
    # cannot resurrect devices the user removed.  Only applies in SSOT
    # mode (passive scan).  In legacy mode, the cache is kept as-is.
    if not schema_is_ssot:
        return result

    if not config_device_ids:
        # Config has no devices at all — check if the result has any
        # device IDs to drop.  Device IDs can be top-level keys OR
        # entries inside orphan lists (orphans_heat, orphans_hvac, orphans).
        has_devices = any(device_id_re.match(str(k)) for k in result)
        if not has_devices:
            for list_key in _LIST_KEYS:
                if list_key in result and isinstance(result[list_key], list):
                    if any(device_id_re.match(str(d)) for d in result[list_key]):
                        has_devices = True
                        break
        if not has_devices:
            return result
        # Config is fully wiped of devices — drop all cached device keys
        # and orphan lists, keep only non-device keys (known_list, etc.)
        _LOGGER.info(
            "merge_schemas: config has no devices, dropping all cached "
            "device entries (user wiped schema)"
        )
        return {
            k: v
            for k, v in result.items()
            if not device_id_re.match(str(k)) and k not in _LIST_KEYS
        }

    filtered: _SchemaT = {}
    for key, value in result.items():
        if device_id_re.match(str(key)) and str(key) not in config_device_ids:
            _LOGGER.info(
                "merge_schemas: dropping %s from cached schema "
                "(not in config schema, user removed it)",
                key,
            )
            continue
        # Clear _skipped flag for devices that are in the config schema
        # (user re-added them after being skipped)
        if device_id_re.match(str(key)) and isinstance(value, dict):
            filtered_value = dict(value)
            filtered_value.pop(SZ_TR_SKIPPED, None)
            # Also filter remotes/sensors lists inside device entries
            for list_key in _ZONE_LIST_KEYS:
                if list_key in filtered_value and isinstance(
                    filtered_value[list_key], list
                ):
                    filtered_value[list_key] = [
                        d for d in filtered_value[list_key] if d in config_device_ids
                    ]
                    if not filtered_value[list_key]:
                        del filtered_value[list_key]
            filtered[key] = filtered_value
        else:
            filtered[key] = value

    # Also filter orphan lists: only keep devices that are in the config
    for list_key in _LIST_KEYS:
        if list_key in filtered and isinstance(filtered[list_key], list):
            filtered[list_key] = [
                d for d in filtered[list_key] if d in config_device_ids
            ]
            if not filtered[list_key]:
                del filtered[list_key]

    return filtered


# Schema keys that hold device IDs in list form
_LIST_KEYS = frozenset({SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC, "orphans"})
# Schema keys that hold a single device ID as a scalar
_SCALAR_KEYS = frozenset(
    {SZ_SENSOR, SZ_APPLIANCE_CONTROL, "hotwater_valve", "heating_valve"}
)
# Schema keys that hold lists of device IDs inside zone/TCS entries
_ZONE_LIST_KEYS = frozenset({"actuators", "remotes", "sensors"})

# Keys that identify an HVAC entry (FAN with remotes/sensors)
_HVAC_ENTRY_KEYS = frozenset({SZ_REMOTES, SZ_SENSORS})
# Top-level keys that are HVAC-related
_HVAC_TOP_KEYS = frozenset({SZ_ORPHANS_HVAC})


def extract_hvac_schema(schema: _SchemaT) -> _SchemaT:
    """Extract HVAC-only entries from a full schema.

    Returns a dict containing only the HVAC-related parts:
    - Top-level ``orphans_hvac`` list
    - FAN entries (any top-level key whose value dict has ``remotes``
      or ``sensors``)

    :param schema: The full schema dict.
    :return: A schema dict containing only HVAC entries.
    """
    hvac: dict[str, Any] = {}
    if type(schema) is not dict:
        return hvac

    for key, val in schema.items():
        if key in _HVAC_TOP_KEYS:
            hvac[key] = val
            continue
        if isinstance(val, dict) and _HVAC_ENTRY_KEYS & set(val):
            hvac[key] = val

    return hvac


def merge_hvac_schema(
    config_schema: _SchemaT,
    hvac_schema: _SchemaT,
    *,
    schema_is_ssot: bool = False,
) -> _SchemaT:
    """Merge cached HVAC schema entries into a config schema.

    HVAC entries (FAN with remotes/sensors, orphans_hvac) are lost when
    ramses_rf restarts because ``load_fan`` is a stub.  This helper
    merges them back from the separate HVAC cache.

    - FAN entries: union the ``remotes`` and ``sensors`` lists
    - ``orphans_hvac``: union the lists

    The **config schema is authoritative** — but only when
    *schema_is_ssot* is True (passive scan mode).  In legacy mode, the
    HVAC cache is always merged back because the config may not have a
    ``CONF_SCHEMA`` key (old YAML format).

    :param config_schema: The config schema to merge into.
    :param hvac_schema: The cached HVAC schema to merge from.
    :param schema_is_ssot: When True, config schema is authoritative for
        device existence.  When False (legacy), always merge.
    :return: A new schema with HVAC entries merged in.
    """
    if not hvac_schema or type(hvac_schema) is not dict:
        return config_schema

    # In SSOT mode: config schema is authoritative.  If it has no devices,
    # don't resurrect from HVAC cache.  In legacy mode: always merge.
    if schema_is_ssot:
        import re

        device_id_re = re.compile(r"^[0-9]{2}:[0-9]{6}$")
        config_has_devices = any(
            device_id_re.match(str(k))
            for k in config_schema
            if isinstance(config_schema, dict)
        )
        if not config_has_devices:
            for list_key in _LIST_KEYS:
                if list_key in config_schema and isinstance(
                    config_schema[list_key], list
                ):
                    if any(device_id_re.match(str(d)) for d in config_schema[list_key]):
                        config_has_devices = True
                        break
        if not config_has_devices:
            _LOGGER.info(
                "merge_hvac_schema: config has no devices, skipping HVAC cache "
                "merge (user wiped schema)"
            )
            return config_schema

    result = deepcopy(config_schema)
    changed = False

    # Build set of device IDs that are in the config schema (for SSOT filtering)
    config_device_ids: set[str] = set()
    if schema_is_ssot:
        import re

        device_id_re = re.compile(r"^[0-9]{2}:[0-9]{6}$")
        for key in config_schema:
            if device_id_re.match(str(key)):
                config_device_ids.add(str(key))
        for list_key in _LIST_KEYS:
            if list_key in config_schema and isinstance(config_schema[list_key], list):
                config_device_ids.update(config_schema[list_key])
        # Also include devices in remotes/sensors/actuators lists inside
        # device entries — these are part of the config schema too.
        for key, value in config_schema.items():
            if device_id_re.match(str(key)) and isinstance(value, dict):
                for list_key in _ZONE_LIST_KEYS:
                    if list_key in value and isinstance(value[list_key], list):
                        config_device_ids.update(value[list_key])

    for key, val in hvac_schema.items():
        if key == SZ_ORPHANS_HVAC:
            existing = set(result.get(SZ_ORPHANS_HVAC, []))
            new = set(val) if isinstance(val, list) else set()
            # In SSOT mode, only keep devices that are in config schema
            if schema_is_ssot:
                new = {d for d in new if d in config_device_ids}
            merged = sorted(existing | new)
            if merged != list(result.get(SZ_ORPHANS_HVAC, [])):
                result[SZ_ORPHANS_HVAC] = merged
                changed = True
            continue

        if not isinstance(val, dict):
            continue

        config_entry = result.get(key, {})
        if not isinstance(config_entry, dict):
            config_entry = {}

        for list_key in (SZ_REMOTES, SZ_SENSORS):
            cached_list: list[Any] = val.get(list_key, [])
            if not cached_list:
                continue
            # In SSOT mode, only keep devices that are in config schema
            if schema_is_ssot:
                cached_list = [d for d in cached_list if d in config_device_ids]
            existing_set: set[Any] = set(config_entry.get(list_key, []))
            new_items: list[Any] = [d for d in cached_list if d not in existing_set]
            if new_items:
                config_entry[list_key] = sorted(existing_set | set(new_items))
                changed = True

        if config_entry:
            result[key] = config_entry

    return result if changed else config_schema


def remove_device_from_schema(schema: _SchemaT, device_id: str) -> _SchemaT:
    """Remove a device_id from anywhere in the schema.

    Searches all locations where a device_id can appear:
    - Top-level orphan lists (orphans_heat, orphans_hvac, orphans)
    - Zone sensor/actuators inside a TCS entry
    - DHW sensor/valves inside a TCS entry
    - appliance_control inside a TCS system
    - remotes/sensors inside an HVAC entry

    Does NOT remove the device's own top-level key (e.g. ``"32:153289": {}``)
    — the caller will merge a new fragment that updates it.

    :param schema: The schema dict to clean.
    :param device_id: The device ID to remove.
    :return: A new schema dict with the device removed from its old location.
    """
    new_schema = deepcopy(schema)

    # 1. Remove from top-level orphan lists
    for key in _LIST_KEYS:
        if key in new_schema and isinstance(new_schema[key], list):
            new_schema[key] = [d for d in new_schema[key] if d != device_id]
            if not new_schema[key]:
                del new_schema[key]

    # 2. Search TCS/HVAC entries for the device
    for tcs_id, tcs_entry in list(new_schema.items()):
        if not isinstance(tcs_entry, dict) or tcs_id in _LIST_KEYS:
            continue
        if tcs_id in (SZ_MAIN_TCS, SZ_DEVICE_COMMENTS):
            continue

        # 2a. Check system.appliance_control
        sys_entry = tcs_entry.get(SZ_SYSTEM, {})
        if isinstance(sys_entry, dict):
            for scalar_key in _SCALAR_KEYS:
                if scalar_key in sys_entry and sys_entry[scalar_key] == device_id:
                    sys_entry[scalar_key] = None

        # 2b. Check orphans list inside TCS
        if SZ_ORPHANS in tcs_entry and isinstance(tcs_entry[SZ_ORPHANS], list):
            tcs_entry[SZ_ORPHANS] = [d for d in tcs_entry[SZ_ORPHANS] if d != device_id]
            if not tcs_entry[SZ_ORPHANS]:
                del tcs_entry[SZ_ORPHANS]

        # 2c. Check zones for sensor and actuators
        zones = tcs_entry.get(SZ_ZONES, {})
        if isinstance(zones, dict):
            for _zone_idx, zone in list(zones.items()):
                if not isinstance(zone, dict):
                    continue
                # sensor is a scalar
                if zone.get(SZ_SENSOR) == device_id:
                    zone[SZ_SENSOR] = None
                # actuators is a list
                if "actuators" in zone and isinstance(zone["actuators"], list):
                    zone["actuators"] = [d for d in zone["actuators"] if d != device_id]
                    if not zone["actuators"]:
                        del zone["actuators"]

        # 2d. Check DHW system for sensor and valves
        dhw = tcs_entry.get(SZ_DHW_SYSTEM, {})
        if isinstance(dhw, dict):
            for scalar_key in (SZ_SENSOR, "hotwater_valve", "heating_valve"):
                if scalar_key in dhw and dhw[scalar_key] == device_id:
                    dhw[scalar_key] = None

        # 2e. Check HVAC remotes/sensors lists
        for list_key in _ZONE_LIST_KEYS:
            if list_key in tcs_entry and isinstance(tcs_entry[list_key], list):
                tcs_entry[list_key] = [d for d in tcs_entry[list_key] if d != device_id]
                if not tcs_entry[list_key]:
                    del tcs_entry[list_key]

    # 3. Remove from device_comments (top-level dict: device_id → comment)
    if SZ_DEVICE_COMMENTS in new_schema and isinstance(
        new_schema[SZ_DEVICE_COMMENTS], dict
    ):
        new_schema[SZ_DEVICE_COMMENTS].pop(device_id, None)
        if not new_schema[SZ_DEVICE_COMMENTS]:
            del new_schema[SZ_DEVICE_COMMENTS]

    return new_schema


def _parse_zone_from_comment(comment: str) -> str | None:
    """Parse zone index from a device comment.

    Comments have format like: "bound to 01:216136. zone 07. codes: ..."
    Returns the zone index (e.g., "07") or None if not found.
    """
    if not comment or not isinstance(comment, str):
        return None
    match = re.search(r"zone\s+([0-9A-Fa-f]+)", comment)
    return match.group(1) if match else None


def _parse_bound_tcs_from_comment(comment: str) -> str | None:
    """Parse TCS ID from a device comment.

    Comments have format like: "bound to 01:216136. zone 07. codes: ..."
    Returns the TCS ID (e.g., "01:216136") or None if not found.
    """
    if not comment or not isinstance(comment, str):
        return None
    match = re.search(r"bound to\s+([0-9A-Fa-f]+:[0-9A-Fa-f]+)", comment)
    return match.group(1) if match else None


# Valid sensor/actuator device prefixes (must match ramses_rf's DEVICE_ID_REGEX.SEN)
# 18: (HGI) and 13: (BDR) are NOT valid zone sensors
_VALID_ZONE_SENSOR_RE = re.compile(r"^(01|03|04|12|22|34):[0-9A-Fa-f]{6}$")
# Actuators can be any device ID
_VALID_ZONE_ACTUATOR_RE = re.compile(r"^[0-9]{2}:[0-9]{6}$")
# Valid zone indices (must match ramses_rf's SCH_ZON_IDX: 00-0B, max 12 zones)
_VALID_ZONE_IDX_RE = re.compile(r"^0[0-9AB]$")


def sync_learned_topology(
    config_schema: _SchemaT,
    learned_schema: _SchemaT,
    scan_codes: dict[str, list[str]] | None = None,
) -> _SchemaT | None:
    """Sync learned topology from ramses_rf back into the config schema.

    Compares the learned schema (from ``gateway.schema()``) with the config
    entry schema.  If the learned schema has richer topology (devices in
    zones that config has in orphans, new zones, appliance_control), returns
    an enriched config schema.

    Also parses device comments for zone binding information, which is
    important for passive scan mode where ramses_rf doesn't actively
    discover topology.

    Preserves user-authored keys (``_name``, ``_alias``, ``_class``,
    ``_enabled``) and the ``device_comments`` list.

    :param config_schema: The current config entry schema (user intent).
    :param learned_schema: The learned topology from ``gateway.schema()``.
    :return: An enriched schema dict if changes were made, or None if the
        config schema already matches or is richer than the learned topology.
    """
    if type(config_schema) is not dict:
        return None

    new_schema = deepcopy(config_schema)
    changed = False

    # Keys that are config-only and must be preserved as-is
    config_only_keys = {SZ_DEVICE_COMMENTS, SZ_MAIN_TCS}

    # 0a-pre. Clean up invalid sensor values (e.g. 18: HGI can't be a zone sensor)
    # ramses_rf's validator rejects non-SEN prefixes as zone sensors.
    # Set to None (not delete) so the zone structure is preserved.
    for tcs_id, tcs_entry in new_schema.items():
        if not isinstance(tcs_entry, dict) or tcs_id in config_only_keys:
            continue
        if tcs_id in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC):
            continue
        zones = tcs_entry.get(SZ_ZONES)
        if not isinstance(zones, dict):
            continue
        for _zone_idx, zone in list(zones.items()):
            if not isinstance(zone, dict):
                continue
            sensor = zone.get(SZ_SENSOR)
            if isinstance(sensor, str) and not _VALID_ZONE_SENSOR_RE.match(sensor):
                zone[SZ_SENSOR] = None
                changed = True
            elif sensor is not None and not isinstance(sensor, str):
                zone[SZ_SENSOR] = None
                changed = True
            # Also clean actuators list of non-device entries
            if "actuators" in zone:
                cleaned = [
                    a for a in zone["actuators"] if _VALID_ZONE_ACTUATOR_RE.match(a)
                ]
                if cleaned != zone["actuators"]:
                    zone["actuators"] = cleaned
                    if not zone["actuators"]:
                        del zone["actuators"]
                    changed = True

    # 0a-post. Clean up HGI (18:) entries — they are gateways, not TCSes.
    # Remove any heating-specific keys (zones, system, stored_hotwater,
    # underfloor_heating, orphans) that were incorrectly added by earlier
    # versions of sync_learned_topology.  HGI entries should only contain
    # user-authored traits (_skipped, _class, _comment, etc.).
    _HGI_HEATING_KEYS = frozenset(
        {SZ_ZONES, SZ_SYSTEM, SZ_DHW_SYSTEM, SZ_UFH_SYSTEM, SZ_ORPHANS}
    )
    for dev_id, dev_entry in new_schema.items():
        if not isinstance(dev_entry, dict) or not str(dev_id).startswith("18:"):
            continue
        if dev_id in config_only_keys:
            continue
        for key in _HGI_HEATING_KEYS:
            if key in dev_entry:
                dev_entry.pop(key, None)
                changed = True
        # Remove _skipped from HGI entries — it would cause the scan engine
        # to re-discover the HGI every cycle (the known_list excludes
        # _skipped devices).  HGIs should be in the known_list so the scan
        # engine knows them and doesn't re-discover them.
        if dev_entry.get(SZ_TR_SKIPPED) is True:
            dev_entry.pop(SZ_TR_SKIPPED, None)
            changed = True

    # 0a-post-bis. Remove 18: (HGI) devices from orphan lists — they are
    # gateways, not heating or HVAC devices, and should not be in any
    # orphan list.
    for orphan_key in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC):
        orphan_list = new_schema.get(orphan_key)
        if isinstance(orphan_list, list):
            cleaned = [d for d in orphan_list if not str(d).startswith("18:")]
            if cleaned != orphan_list:
                if cleaned:
                    new_schema[orphan_key] = cleaned
                else:
                    new_schema.pop(orphan_key, None)
                changed = True

    # 0a-post-ter. Clear _skipped for devices that have an active role
    # in the schema (zones, DHW, appliance_control, orphans).  A device
    # that is placed somewhere meaningful should not be marked as skipped
    # — _skipped means "user deferred decision" but the device clearly
    # has a role.  This also fixes devices that were skipped in a prior
    # review_discovered session and later got placed by sync_learned_topology.
    active_device_ids: set[str] = set()
    for orphan_key in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC, "orphans"):
        if isinstance(new_schema.get(orphan_key), list):
            active_device_ids.update(new_schema[orphan_key])
    for key, value in new_schema.items():
        if not isinstance(value, dict) or not str(key).startswith(
            (
                "01:",
                "02:",
                "04:",
                "07:",
                "10:",
                "12:",
                "13:",
                "17:",
                "22:",
                "23:",
                "30:",
                "34:",
                "37:",
            )
        ):
            continue
        # TCS entry — collect zone/DHW/appliance_control devices
        if isinstance(value.get(SZ_ZONES), dict):
            for zone in value[SZ_ZONES].values():
                if isinstance(zone, dict):
                    if zone.get(SZ_SENSOR):
                        active_device_ids.add(zone[SZ_SENSOR])
                    if isinstance(zone.get(SZ_ACTUATORS), list):
                        active_device_ids.update(zone[SZ_ACTUATORS])
        if isinstance(value.get(SZ_DHW_SYSTEM), dict):
            dhw = value[SZ_DHW_SYSTEM]
            for dhw_key in (SZ_SENSOR, "hotwater_valve", "heating_valve"):
                if dhw.get(dhw_key):
                    active_device_ids.add(dhw[dhw_key])
        if isinstance(value.get(SZ_SYSTEM), dict):
            ac = value[SZ_SYSTEM].get(SZ_APPLIANCE_CONTROL)
            if ac:
                active_device_ids.add(ac)
    for dev_id in active_device_ids:
        entry = new_schema.get(dev_id)
        if isinstance(entry, dict) and entry.get(SZ_TR_SKIPPED) is True:
            entry.pop(SZ_TR_SKIPPED, None)
            changed = True

    # 0a-post-quart. Backfill root entries for devices in remotes/sensors/
    # actuators lists that don't have their own root entry.  Before the
    # generate_schema_entry fix, list-based devices (REM/CO2 in remotes[],
    # TRV in zones[], etc.) were accepted without a root entry — so _owner
    # and other traits could never be set on them.  This backfill creates
    # a root entry with the root _owner so SSOT works for these devices.
    root_owner = new_schema.get(SZ_OWNER)
    backfill_count = 0
    for dev_id in active_device_ids:
        if dev_id not in new_schema:
            new_schema[dev_id] = {}
            if root_owner:
                new_schema[dev_id][SZ_TR_OWNER] = root_owner
            changed = True
            backfill_count += 1
            _LOGGER.info(
                "sync_learned_topology: backfilled root entry for %s",
                dev_id,
            )
    # Also check remotes/sensors lists (not in active_device_ids above)
    for key, value in list(new_schema.items()):
        if not isinstance(value, dict) or not str(key).startswith(
            (
                "01:",
                "02:",
                "04:",
                "07:",
                "10:",
                "12:",
                "13:",
                "17:",
                "22:",
                "23:",
                "30:",
                "32:",
                "34:",
                "37:",
            )
        ):
            continue
        for list_key in _ZONE_LIST_KEYS:
            if list_key in value and isinstance(value[list_key], list):
                for dev_id in value[list_key]:
                    if dev_id not in new_schema:
                        new_schema[dev_id] = {}
                        if root_owner:
                            new_schema[dev_id][SZ_TR_OWNER] = root_owner
                        changed = True
                        backfill_count += 1
                        _LOGGER.info(
                            "sync_learned_topology: backfilled root entry for %s (from %s/%s)",
                            dev_id,
                            key,
                            list_key,
                        )
    if backfill_count:
        _LOGGER.info(
            "SSOT backfill: created root entries for %d device(s) that existed "
            "only in lists (remotes/sensors/actuators/orphans). Owner set to '%s'.",
            backfill_count,
            root_owner or "(none)",
        )

    # 0. Build GLOBAL placement maps across all TCS entries.
    # These are used in step 1e/1f to detect cross-TCS moves: a device
    # that learned schema places in CTL-B's zone 03 must be removed from
    # CTL-A's config zones too, not just CTL-B's.
    #   learned_device_zones: device_id -> (tcs_id, zone_idx) — from learned schema
    #   comment_device_zones:  device_id -> (tcs_id, zone_idx) — from device comments
    #   learned_dhw_devices:  device_id -> tcs_id
    learned_device_zones: dict[str, tuple[str, str]] = {}
    comment_device_zones: dict[str, tuple[str, str]] = {}
    learned_dhw_devices: dict[str, str] = {}

    # 0a. Extract zone info from learned schema (ramses_rf's active discovery)
    if learned_schema and type(learned_schema) is dict:
        for tcs_id, learned_entry in learned_schema.items():
            if not isinstance(learned_entry, dict) or tcs_id in config_only_keys:
                continue
            if tcs_id in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC):
                continue
            learned_zones_map = learned_entry.get(SZ_ZONES, {})
            if isinstance(learned_zones_map, dict):
                for lz_idx, lz in learned_zones_map.items():
                    if not isinstance(lz, dict):
                        continue
                    sensor = lz.get(SZ_SENSOR)
                    if isinstance(sensor, str):
                        learned_device_zones[sensor] = (tcs_id, lz_idx)
                    for act in lz.get("actuators", []):
                        if isinstance(act, str):
                            learned_device_zones[act] = (tcs_id, lz_idx)
            learned_dhw_entry = learned_entry.get(SZ_DHW_SYSTEM, {})
            if isinstance(learned_dhw_entry, dict):
                dhw_sensor = learned_dhw_entry.get(SZ_SENSOR)
                if isinstance(dhw_sensor, str):
                    learned_dhw_devices[dhw_sensor] = tcs_id
                for valve_key in ("hotwater_valve", "heating_valve"):
                    valve = learned_dhw_entry.get(valve_key)
                    if isinstance(valve, str):
                        learned_dhw_devices[valve] = tcs_id

    # 0b. Extract zone info from device comments (passive scan/discovery manager)
    # This is important for passive scan mode where ramses_rf doesn't actively
    # discover topology, but the discovery manager infers zone bindings from traffic.
    # When a TRV broadcasts zone-binding codes (30C9, 3150, etc.), the scan engine
    # captures zone_idx but may not have bound_to (since dst is --:------ for
    # broadcasts).  In that case, infer the CTL from main_tcs or the only TCS key.
    main_tcs_id = config_schema.get(SZ_MAIN_TCS)
    # Fallback: find the CTL key (01: or 23: prefix) if main_tcs is not set.
    # When there are multiple 01: keys (CTL + zone sensors), prefer the one
    # that has a "zones" dict — zone sensors don't have zones, only the CTL does.
    if not main_tcs_id:
        ctl_keys = [
            k
            for k in config_schema
            if isinstance(k, str)
            and k[:3] in ("01:", "23:")
            and isinstance(config_schema.get(k), dict)
        ]
        if len(ctl_keys) == 1:
            main_tcs_id = ctl_keys[0]
        elif len(ctl_keys) > 1:
            # Multiple CTL keys — prefer the one with a zones dict
            for k in ctl_keys:
                entry = config_schema.get(k)
                if isinstance(entry, dict) and isinstance(entry.get(SZ_ZONES), dict):
                    main_tcs_id = k
                    break

    device_comments = config_schema.get(SZ_DEVICE_COMMENTS, {})
    if isinstance(device_comments, dict):
        for device_id, comment in device_comments.items():
            if not isinstance(comment, str):
                continue
            # Skip devices that can't be valid zone sensors/actuators (e.g. 18: HGI)
            if not _VALID_ZONE_SENSOR_RE.match(device_id):
                continue
            # Skip CTL (01:) — it is the controller, not a zone member.
            # Its comment may contain "zone NN" (the CTL's own binding zone),
            # but creating a zone from it would add an empty phantom zone.
            if device_id.startswith("01:"):
                continue
            # Build comment_device_zones for ALL devices with zone info in
            # comments, even if they're also in learned_device_zones.  This
            # allows comments (from the scan engine, which tracks zone bindings
            # from live traffic) to override stale learned zones (e.g. when
            # the cached schema was corrupted or 000C packets haven't arrived
            # yet).  Step 1g will move devices from their learned zone to
            # their comment zone if they differ.
            comment_tcs_id = _parse_bound_tcs_from_comment(comment)
            zone_idx = _parse_zone_from_comment(comment)
            # Skip if bound_to is an HGI (18:) — the HGI is the gateway,
            # not a TCS.  Comments like "bound to 18:072981" on a CTL
            # mean the CTL is paired with that gateway, not that the HGI
            # is a temperature control system with zones.
            if comment_tcs_id and comment_tcs_id.startswith("18:"):
                continue
            # Skip invalid zone indices (ramses_rf only allows 00-0B)
            if zone_idx and not _VALID_ZONE_IDX_RE.match(zone_idx):
                continue
            # If no bound_to in comment but zone_idx is present, infer CTL
            if not comment_tcs_id and zone_idx and main_tcs_id:
                comment_tcs_id = main_tcs_id
            if comment_tcs_id and zone_idx:
                comment_device_zones[device_id] = (comment_tcs_id, zone_idx)

    # 1. Sync TCS entries (zones, appliance_control, DHW, orphans)
    if learned_schema and isinstance(learned_schema, dict):
        for tcs_id, learned_entry in learned_schema.items():
            if not isinstance(learned_entry, dict) or tcs_id in config_only_keys:
                continue
            if tcs_id in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC):
                continue

            config_entry = new_schema.get(tcs_id, {})
            if not isinstance(config_entry, dict):
                config_entry = {}

            # Track original config zone keys — used by step 1b-post2 to
            # distinguish user-created zones (keep even if empty) from
            # phantom zones added by learned/comment sync (remove if empty).
            orig_config_zone_keys: set[str] = set()
            orig_config_zones = config_entry.get(SZ_ZONES)
            if isinstance(orig_config_zones, dict):
                orig_config_zone_keys = set(orig_config_zones.keys())

            # 1a. Sync appliance_control
            learned_sys = learned_entry.get(SZ_SYSTEM, {})
            if isinstance(learned_sys, dict):
                learned_app = learned_sys.get(SZ_APPLIANCE_CONTROL)
                if learned_app:
                    config_sys = config_entry.setdefault(SZ_SYSTEM, {})
                    if config_sys.get(SZ_APPLIANCE_CONTROL) != learned_app:
                        config_sys[SZ_APPLIANCE_CONTROL] = learned_app
                        changed = True

            # 1b. Sync zones — this is the key enrichment
            learned_zones = learned_entry.get(SZ_ZONES, {})
            if isinstance(learned_zones, dict):
                config_zones = config_entry.get(SZ_ZONES)
                if not isinstance(config_zones, dict):
                    config_zones = {}
                    config_entry[SZ_ZONES] = config_zones
                for zone_idx, learned_zone in learned_zones.items():
                    if not isinstance(learned_zone, dict):
                        continue
                    config_zone = config_zones.setdefault(zone_idx, {})
                    # Sync sensor (only if config doesn't already have one)
                    learned_sensor = learned_zone.get(SZ_SENSOR)
                    if learned_sensor and not config_zone.get(SZ_SENSOR):
                        config_zone[SZ_SENSOR] = learned_sensor
                        changed = True
                    # Sync actuators (union, don't overwrite)
                    learned_actuators = learned_zone.get("actuators", [])
                    if learned_actuators:
                        existing = set(config_zone.get("actuators", []))
                        new_actuators = [
                            a for a in learned_actuators if a not in existing
                        ]
                        if new_actuators:
                            config_zone["actuators"] = sorted(
                                existing | set(new_actuators)
                            )
                            changed = True
                    # Sync class if learned has it and config doesn't
                    learned_class = learned_zone.get(SZ_CLASS)
                    if learned_class and SZ_CLASS not in config_zone:
                        config_zone[SZ_CLASS] = learned_class
                        changed = True
                    # Sync _name from learned schema (e.g. from 0004 zone_name
                    # packets) if config doesn't already have one
                    learned_name = learned_zone.get(SZ_TR_NAME)
                    if learned_name and not config_zone.get(SZ_TR_NAME):
                        config_zone[SZ_TR_NAME] = learned_name
                        changed = True

            # 1b-post. Sanitize zone assignments — ramses_rf's active discovery
            # sometimes places sensor-type devices (01:, 22:, 34:) in the
            # actuators list instead of as the zone sensor.  This causes
            # RULES EXCEPTIONS in ramses_rf's legacy_trace.  Fix: if a device
            # in actuators is a sensor-type prefix and the zone has no sensor,
            # move it to sensor.
            #
            # Also: ramses_rf sometimes places a TRV (04:) as the zone sensor
            # while a dedicated room thermostat (THM 22: or RND 34:) is stuck
            # in the actuators list.  While TRVs are valid zone sensors per
            # PARENT_RULES, a dedicated thermostat is always preferable.  When
            # both are present, swap: move the TRV to actuators and promote
            # the thermostat to sensor (see issue 813).
            config_zones = config_entry.get(SZ_ZONES)
            if isinstance(config_zones, dict):
                for zone in config_zones.values():
                    if not isinstance(zone, dict):
                        continue
                    # If a TRV (04:) is the sensor, it should be in actuators
                    # instead.  A TRV's primary role is as an actuator (heat
                    # demand / valve position).  If a dedicated thermostat
                    # (01:, 22:, 34:) is also in actuators, promote it to
                    # sensor.  Otherwise, just move the TRV to actuators and
                    # leave sensor=None (see issue 813: single-TRV zones had
                    # the TRV as sensor instead of actuator).
                    sensor = zone.get(SZ_SENSOR)
                    actuators = zone.get("actuators")
                    if (
                        isinstance(sensor, str)
                        and sensor.startswith("04:")
                        and isinstance(actuators, list)
                    ):
                        thermostat = next(
                            (
                                a
                                for a in actuators
                                if isinstance(a, str) and a[:3] in ("01:", "22:", "34:")
                            ),
                            None,
                        )
                        if thermostat:
                            # Move TRV to actuators, thermostat to sensor
                            if sensor not in actuators:
                                actuators.append(sensor)
                            actuators.remove(thermostat)
                            actuators.sort()
                            zone[SZ_SENSOR] = thermostat
                            changed = True
                            _LOGGER.debug(
                                "sync_learned_topology: swapped TRV %s from "
                                "sensor to actuators, promoted %s to sensor "
                                "(dedicated thermostat takes priority)",
                                sensor,
                                thermostat,
                            )
                        else:
                            # No thermostat to swap — just move TRV to actuators
                            if sensor not in actuators:
                                actuators.append(sensor)
                                actuators.sort()
                            zone[SZ_SENSOR] = None
                            changed = True
                            _LOGGER.debug(
                                "sync_learned_topology: moved TRV %s from "
                                "sensor to actuators (no dedicated thermostat "
                                "to promote)",
                                sensor,
                            )
                    elif (
                        isinstance(sensor, str)
                        and sensor.startswith("04:")
                        and not isinstance(actuators, list)
                    ):
                        # TRV is sensor, no actuators list at all — create one
                        zone["actuators"] = [sensor]
                        zone[SZ_SENSOR] = None
                        changed = True
                        _LOGGER.debug(
                            "sync_learned_topology: moved TRV %s from "
                            "sensor to new actuators list",
                            sensor,
                        )
                    # Clear 04: (TRV) from sensor if also in actuators (dup)
                    sensor = zone.get(SZ_SENSOR)
                    actuators = zone.get("actuators")
                    if (
                        isinstance(sensor, str)
                        and sensor.startswith("04:")
                        and isinstance(actuators, list)
                        and sensor in actuators
                    ):
                        zone[SZ_SENSOR] = None
                        changed = True
                        _LOGGER.debug(
                            "sync_learned_topology: cleared TRV %s from "
                            "sensor (duplicate of actuator entry)",
                            sensor,
                        )
                    # Move sensor-type devices from actuators to sensor
                    actuators = zone.get("actuators")
                    if not isinstance(actuators, list):
                        continue
                    sensor = zone.get(SZ_SENSOR)
                    if sensor:
                        continue  # zone already has a sensor
                    # Find first sensor-type device in actuators
                    for act in list(actuators):
                        if isinstance(act, str) and act[:3] in (
                            "01:",
                            "22:",
                            "34:",
                        ):
                            zone[SZ_SENSOR] = act
                            actuators.remove(act)
                            changed = True
                            _LOGGER.debug(
                                "sync_learned_topology: moved %s from "
                                "actuators to sensor (sanitization)",
                                act,
                            )
                            break
                    if not actuators:
                        zone.pop("actuators", None)

            # 1b-post2. Remove empty phantom zones — zones that were NOT in
            # the original config schema (added by learned/comment sync) and
            # have no sensor, no actuators, and no devices in the learned
            # schema.  These can appear when a CTL comment with "zone NN" was
            # previously processed (creating a phantom zone), and ramses_rf
            # then loaded the cached schema and propagated the empty zone.
            # User-created zones (in original config) are preserved even if
            # temporarily empty — the user may have created them intentionally.
            config_zones = config_entry.get(SZ_ZONES)
            if isinstance(config_zones, dict):
                learned_zones_for_tcs = learned_entry.get(SZ_ZONES, {})
                for z_idx in list(config_zones.keys()):
                    if z_idx in orig_config_zone_keys:
                        continue  # user-created zone — keep even if empty
                    cz = config_zones[z_idx]
                    if not isinstance(cz, dict):
                        continue
                    has_sensor = bool(cz.get(SZ_SENSOR))
                    has_actuators = bool(cz.get("actuators"))
                    # Check if learned schema has devices for this zone
                    learned_z = learned_zones_for_tcs.get(z_idx, {})
                    learned_has_devices = bool(
                        isinstance(learned_z, dict)
                        and (learned_z.get(SZ_SENSOR) or learned_z.get("actuators"))
                    )
                    if not has_sensor and not has_actuators and not learned_has_devices:
                        del config_zones[z_idx]
                        changed = True
                        _LOGGER.debug(
                            "sync_learned_topology: removed empty phantom "
                            "zone %s from %s (not in original config, no "
                            "sensor, no actuators, no learned devices)",
                            z_idx,
                            tcs_id,
                        )

            # 1c. Sync DHW system
            learned_dhw = learned_entry.get(SZ_DHW_SYSTEM, {})
            if isinstance(learned_dhw, dict) and learned_dhw:
                config_dhw = config_entry.setdefault(SZ_DHW_SYSTEM, {})
                learned_dhw_sensor = learned_dhw.get(SZ_SENSOR)
                if learned_dhw_sensor and not config_dhw.get(SZ_SENSOR):
                    config_dhw[SZ_SENSOR] = learned_dhw_sensor
                    changed = True

            # 1d. Sync TCS-level orphans (only remove devices now in zones)
            learned_tcs_orphans = set(learned_entry.get(SZ_ORPHANS, []))
            config_tcs_orphans = set(config_entry.get(SZ_ORPHANS, []))
            if learned_tcs_orphans != config_tcs_orphans:
                # Only remove from config orphans if they're in a zone now
                all_zone_devices: set[str] = set()
                for zone in config_entry.get(SZ_ZONES, {}).values():
                    if isinstance(zone, dict):
                        if zone.get(SZ_SENSOR):
                            all_zone_devices.add(zone[SZ_SENSOR])
                        all_zone_devices.update(zone.get("actuators", []))
                to_remove = config_tcs_orphans & all_zone_devices
                if to_remove:
                    remaining = sorted(config_tcs_orphans - to_remove)
                    if remaining:
                        config_entry[SZ_ORPHANS] = remaining
                    else:
                        config_entry.pop(SZ_ORPHANS, None)
                    changed = True

            # 1e. Zone→zone and zone→DHW reassignment — clean old locations.
            # Uses the GLOBAL placement maps built in step 0 so that cross-TCS
            # moves are detected: a device that learned schema places in
            # CTL-B's zone 03 is removed from CTL-A's config zones too.
            if (learned_device_zones or learned_dhw_devices) and isinstance(
                config_entry.get(SZ_ZONES), dict
            ):
                for cz_idx, cz in list(config_entry[SZ_ZONES].items()):
                    if not isinstance(cz, dict):
                        continue
                    # Clear sensor if it moved to a different zone or to DHW
                    sensor_id = cz.get(SZ_SENSOR)
                    if sensor_id and sensor_id in learned_device_zones:
                        new_tcs, new_zone = learned_device_zones[sensor_id]
                        if new_tcs != tcs_id or new_zone != cz_idx:
                            cz[SZ_SENSOR] = None
                            changed = True
                    elif sensor_id and sensor_id in learned_dhw_devices:
                        cz[SZ_SENSOR] = None
                        changed = True
                    # Remove actuators that moved to different zones
                    if "actuators" in cz:
                        new_actuators = [
                            a
                            for a in cz["actuators"]
                            if a not in learned_device_zones
                            or learned_device_zones[a] == (tcs_id, cz_idx)
                        ]
                        if new_actuators != cz["actuators"]:
                            cz["actuators"] = new_actuators
                            if not cz["actuators"]:
                                del cz["actuators"]
                            changed = True

            # 1f. DHW→zone reassignment — clear DHW sensor/valves if the
            # learned schema now has the device in a zone (any TCS) instead
            # of this TCS's DHW.
            if learned_device_zones and isinstance(
                config_entry.get(SZ_DHW_SYSTEM), dict
            ):
                config_dhw = config_entry[SZ_DHW_SYSTEM]
                # Clear DHW sensor if learned placed it in a zone
                dhw_sensor = config_dhw.get(SZ_SENSOR)
                if dhw_sensor and dhw_sensor in learned_device_zones:
                    config_dhw[SZ_SENSOR] = None
                    changed = True
                # Clear DHW valves if learned placed them in a zone
                for valve_key in ("hotwater_valve", "heating_valve"):
                    valve = config_dhw.get(valve_key)
                    if valve and valve in learned_device_zones:
                        config_dhw[valve_key] = None
                        changed = True

            new_schema[tcs_id] = config_entry

    # 1g. Create zone entries from device comment zone info (passive scan mode)
    # This is important for passive scan where ramses_rf doesn't actively discover
    # topology, but the discovery manager has inferred zone bindings from traffic.
    # Only uses comment_device_zones (from step 0b), NOT learned_device_zones
    # (from step 0a) — those are already handled by step 1b.
    #
    # IMPORTANT: Before adding a device to its comment-specified zone, remove it
    # from any other zone in the same TCS.  Otherwise a device that moved zones
    # (e.g. config had it in zone 04, comment says zone 00) ends up duplicated
    # in both zones, which causes "can't change parent" errors in ramses_rf.
    if comment_device_zones:
        for device_id, (tcs_id, zone_idx) in comment_device_zones.items():
            # Skip DHW sensors (07:) — they belong in stored_hotwater.sensor,
            # not in a heating zone.  The "zone 00" in their comment is the
            # DHW domain, not a heating zone index.
            if device_id.startswith("07:"):
                continue
            # Skip if TCS doesn't exist in config
            if tcs_id not in new_schema:
                continue
            tcs_entry = new_schema[tcs_id]
            if not isinstance(tcs_entry, dict):
                tcs_entry = {}
                new_schema[tcs_id] = tcs_entry

            # Create zone if it doesn't exist
            if SZ_ZONES not in tcs_entry:
                tcs_entry[SZ_ZONES] = {}
            zones = tcs_entry[SZ_ZONES]
            if not isinstance(zones, dict):
                zones = {}
                tcs_entry[SZ_ZONES] = zones

            # Remove device from any other zone in this TCS before placing it
            for other_idx, other_zone in zones.items():
                if other_idx == zone_idx or not isinstance(other_zone, dict):
                    continue
                # Remove from actuators
                other_acts = other_zone.get("actuators")
                if isinstance(other_acts, list) and device_id in other_acts:
                    other_acts.remove(device_id)
                    if not other_acts:
                        del other_zone["actuators"]
                    changed = True
                # Clear sensor if it's the device we're moving
                if other_zone.get(SZ_SENSOR) == device_id:
                    other_zone[SZ_SENSOR] = None
                    changed = True

            if zone_idx not in zones:
                zones[zone_idx] = {}
            zone = zones[zone_idx]
            if not isinstance(zone, dict):
                zone = {}
                zones[zone_idx] = zone

            # Add device to zone as sensor or actuator
            # Only sensor-type prefixes (01:, 12:, 22:, 34:) can be zone
            # sensors.  TRVs (04:) and other actuator types are always
            # added as actuators, even if the zone has no sensor yet.
            is_sensor_type = device_id[:3] in ("01:", "12:", "22:", "34:")

            # Skip if device is already the sensor of this zone
            if zone.get(SZ_SENSOR) == device_id:
                continue
            if is_sensor_type and not zone.get(SZ_SENSOR):
                zone[SZ_SENSOR] = device_id
                changed = True
            else:
                # Zone already has a sensor, or device is an actuator type
                if "actuators" not in zone:
                    zone["actuators"] = []
                if device_id not in zone["actuators"]:
                    zone["actuators"].append(device_id)
                    zone["actuators"] = sorted(zone["actuators"])
                    changed = True

    # 1g-post. Place DHW sensors (07:) from device_comments into
    # stored_hotwater.sensor.  The scan engine classifies 07: devices as
    # DHW and includes zone info in the comment, but that "zone" is the
    # DHW domain — the device should be stored_hotwater.sensor, not in a
    # heating zone.
    if isinstance(device_comments, dict):
        for device_id, comment in device_comments.items():
            if not isinstance(comment, str) or not device_id.startswith("07:"):
                continue
            # Find the TCS to place this DHW sensor under
            dhw_tcs_id: str | None = _parse_bound_tcs_from_comment(comment)
            if not dhw_tcs_id or dhw_tcs_id.startswith("18:"):
                # No bound_to or bound to HGI — use main_tcs
                dhw_tcs_id = main_tcs_id
            if not dhw_tcs_id or dhw_tcs_id not in new_schema:
                continue
            tcs_entry = new_schema[dhw_tcs_id]
            if not isinstance(tcs_entry, dict):
                continue
            dhw = tcs_entry.setdefault(SZ_DHW_SYSTEM, {})
            if not dhw.get(SZ_SENSOR):
                dhw[SZ_SENSOR] = device_id
                changed = True
                # Remove from orphans_heat if present
                orphans = new_schema.get(SZ_ORPHANS_HEAT)
                if isinstance(orphans, list) and device_id in orphans:
                    orphans.remove(device_id)
                    if not orphans:
                        new_schema.pop(SZ_ORPHANS_HEAT, None)
                    changed = True

    # 1h. Add HVAC remotes from device comments (passive scan mode)
    # The scan engine infers bound_to for 37: devices when a FAN (32:) sends
    # them a directed packet.  This info appears in comments as
    # "bound to 32:153289".  Add these devices as remotes to the FAN's
    # schema entry if not already present.
    if isinstance(device_comments, dict):
        for device_id, comment in device_comments.items():
            if not isinstance(comment, str) or not device_id.startswith("37:"):
                continue
            fan_id = _parse_bound_tcs_from_comment(comment)
            if not fan_id or not fan_id.startswith("32:"):
                continue
            fan_entry = new_schema.get(fan_id)
            if not isinstance(fan_entry, dict):
                continue
            remotes = fan_entry.get(SZ_REMOTES)
            if not isinstance(remotes, list):
                remotes = []
                fan_entry[SZ_REMOTES] = remotes
            if device_id not in remotes:
                remotes.append(device_id)
                remotes.sort()
                changed = True

    # 1i. Ensure REMs listed in FAN's _bound are also in remotes[].
    # A FAN can have one or more bound REMs (stored as _bound on the FAN
    # entry, copied from the known_list's bound trait).  ramses_rf needs
    # the REM in the FAN's remotes[] list to create the device topology.
    # This step adds any _bound REM that's missing from remotes[].
    # Note: _bound on the FAN means "this REM is bound to this FAN" — it
    # is the canonical place for the binding (a FAN can have multiple
    # bound REMs).  The REM may also have its own _bound trait (pointing
    # back to the FAN) from add_faked_rem, but that is secondary.
    for fan_id, fan_entry in list(new_schema.items()):
        if not isinstance(fan_entry, dict) or not isinstance(fan_id, str):
            continue
        if not fan_id.startswith("32:"):
            continue  # only FAN devices
        bound_rem = fan_entry.get("_bound")
        if not isinstance(bound_rem, str) or not bound_rem.startswith("37:"):
            continue  # _bound should be a REM device ID
        # Add REM to FAN's remotes[] list if not already present
        remotes = fan_entry.get(SZ_REMOTES)
        if not isinstance(remotes, list):
            remotes = []
            fan_entry[SZ_REMOTES] = remotes
        if bound_rem not in remotes:
            remotes.append(bound_rem)
            remotes.sort()
            changed = True

    # 2. Sync top-level orphans_heat — remove devices now in zones or DHW
    config_heat_orphans = set(new_schema.get(SZ_ORPHANS_HEAT, []))
    # Always run this step when there are config orphans, even if they match
    # learned orphans.  Devices may have been placed in zones via device
    # comments (comment_device_zones) even when ramses_rf's learned schema
    # still has them in orphans_heat (e.g. THM/RND zone binding from 000A
    # packets — see issue 813: thermostats appeared in both orphans_heat
    # AND the correct zone until HA restart).
    if config_heat_orphans:
        # Find devices that are in config orphans but in a zone or DHW in
        # learned schema, in new_schema (already updated by steps 1b/1c),
        # or in device comments.
        all_learned_zone_devices: set[str] = set()
        # Scan learned schema
        for learned_entry in (learned_schema or {}).values():
            if not isinstance(learned_entry, dict):
                continue
            for zone in learned_entry.get(SZ_ZONES, {}).values():
                if isinstance(zone, dict):
                    sensor = zone.get(SZ_SENSOR)
                    if isinstance(sensor, str):
                        all_learned_zone_devices.add(sensor)
                    for a in zone.get("actuators", []):
                        if isinstance(a, str):
                            all_learned_zone_devices.add(a)
            # Also check DHW sensor and valves
            learned_dhw = learned_entry.get(SZ_DHW_SYSTEM, {})
            if isinstance(learned_dhw, dict):
                dhw_sensor = learned_dhw.get(SZ_SENSOR)
                if isinstance(dhw_sensor, str):
                    all_learned_zone_devices.add(dhw_sensor)
                for valve_key in ("hotwater_valve", "heating_valve"):
                    valve = learned_dhw.get(valve_key)
                    if isinstance(valve, str):
                        all_learned_zone_devices.add(valve)

        # Scan new_schema (already updated by steps 1b/1c) — this catches
        # devices placed in zones by comment-based sync even when the
        # learned schema still has them in orphans_heat.
        for ns_entry in new_schema.values():
            if not isinstance(ns_entry, dict):
                continue
            for zone in ns_entry.get(SZ_ZONES, {}).values():
                if isinstance(zone, dict):
                    sensor = zone.get(SZ_SENSOR)
                    if isinstance(sensor, str):
                        all_learned_zone_devices.add(sensor)
                    for a in zone.get("actuators", []):
                        if isinstance(a, str):
                            all_learned_zone_devices.add(a)
            ns_dhw = ns_entry.get(SZ_DHW_SYSTEM, {})
            if isinstance(ns_dhw, dict):
                dhw_sensor = ns_dhw.get(SZ_SENSOR)
                if isinstance(dhw_sensor, str):
                    all_learned_zone_devices.add(dhw_sensor)
                for valve_key in ("hotwater_valve", "heating_valve"):
                    valve = ns_dhw.get(valve_key)
                    if isinstance(valve, str):
                        all_learned_zone_devices.add(valve)

        # Also check devices in zones from device comments
        all_learned_zone_devices.update(comment_device_zones.keys())

        to_remove = config_heat_orphans & all_learned_zone_devices
        # Also remove HGI gateways (18:) — they are not heating devices
        to_remove |= {
            d for d in config_heat_orphans if isinstance(d, str) and d.startswith("18:")
        }
        if to_remove:
            remaining = sorted(
                d for d in (config_heat_orphans - to_remove) if isinstance(d, str)
            )
            if remaining:
                new_schema[SZ_ORPHANS_HEAT] = remaining
            else:
                new_schema.pop(SZ_ORPHANS_HEAT, None)
            changed = True

    # 2b. Infer DHW valves from scan codes — 13: devices that send 1100
    # (TPI params with domain_id 00) are boiler relays (hotwater_valve or
    # heating_valve), not zone actuators.  In passive scan mode, the HGI
    # doesn't query 000C with zone_type 0E (HTG), so the only way to
    # identify DHW valves is from the 1100 code they broadcast.
    # This moves such devices from orphans_heat to stored_hotwater.
    if scan_codes:
        heat_orphans = set(new_schema.get(SZ_ORPHANS_HEAT, []))
        dhw_valves_in_orphans = {
            dev_id
            for dev_id in heat_orphans
            if isinstance(dev_id, str)
            and dev_id.startswith("13:")
            and "1100" in scan_codes.get(dev_id, [])
        }
        if dhw_valves_in_orphans:
            # Find the main TCS entry
            main_tcs = new_schema.get(SZ_MAIN_TCS)
            target_tcs_id: str | None = (
                main_tcs
                if isinstance(main_tcs, str)
                and isinstance(new_schema.get(main_tcs), dict)
                else next(
                    (
                        k
                        for k, v in new_schema.items()
                        if isinstance(k, str)
                        and k.startswith("01:")
                        and isinstance(v, dict)
                        and isinstance(v.get(SZ_SYSTEM), dict)
                    ),
                    None,
                )
            )
            if target_tcs_id:
                tcs_entry = new_schema[target_tcs_id]
                dhw = tcs_entry.setdefault(SZ_DHW_SYSTEM, {})
                for dev_id in sorted(dhw_valves_in_orphans):
                    # Assign to hotwater_valve first, then heating_valve
                    if not dhw.get("hotwater_valve"):
                        dhw["hotwater_valve"] = dev_id
                        _LOGGER.info(
                            "sync_learned_topology: inferred %s as "
                            "hotwater_valve (sends 1100, was orphan)",
                            dev_id,
                        )
                    elif not dhw.get("heating_valve"):
                        dhw["heating_valve"] = dev_id
                        _LOGGER.info(
                            "sync_learned_topology: inferred %s as "
                            "heating_valve (sends 1100, was orphan)",
                            dev_id,
                        )
                    else:
                        continue  # both valves already set
                    heat_orphans.discard(dev_id)
                    changed = True
                if heat_orphans:
                    new_schema[SZ_ORPHANS_HEAT] = sorted(heat_orphans)
                else:
                    new_schema.pop(SZ_ORPHANS_HEAT, None)

    # 2c. Infer appliance_control from scan codes — 10: devices that send
    # 3220 (boiler parameters) or 3EF0 (actuator cycle) are OpenTherm
    # Bridges / boiler controllers, not zone actuators.  In passive scan
    # mode, the HGI doesn't query 000C with zone_type FC (appliance_control),
    # so the only way to identify the appliance_control is from these codes.
    # This moves such devices from orphans_heat to system.appliance_control.
    if scan_codes:
        heat_orphans = set(new_schema.get(SZ_ORPHANS_HEAT, []))
        otb_codes = {"3220", "3EF0", "1FD4"}
        otb_in_orphans = {
            dev_id
            for dev_id in heat_orphans
            if isinstance(dev_id, str)
            and dev_id.startswith("10:")
            and otb_codes & set(scan_codes.get(dev_id, []))
        }
        if otb_in_orphans:
            main_tcs = new_schema.get(SZ_MAIN_TCS)
            target_tcs_id = (
                main_tcs
                if isinstance(main_tcs, str)
                and isinstance(new_schema.get(main_tcs), dict)
                else next(
                    (
                        k
                        for k, v in new_schema.items()
                        if isinstance(k, str)
                        and k.startswith("01:")
                        and isinstance(v, dict)
                    ),
                    None,
                )
            )
            if target_tcs_id:
                tcs_entry = new_schema[target_tcs_id]
                config_sys = tcs_entry.setdefault(SZ_SYSTEM, {})
                existing_app = config_sys.get(SZ_APPLIANCE_CONTROL)
                for dev_id in sorted(otb_in_orphans):
                    if existing_app and existing_app != dev_id:
                        _LOGGER.debug(
                            "sync_learned_topology: %s sends OTB codes but "
                            "appliance_control already set to %s",
                            dev_id,
                            existing_app,
                        )
                        continue
                    if existing_app != dev_id:
                        config_sys[SZ_APPLIANCE_CONTROL] = dev_id
                        existing_app = dev_id
                        heat_orphans.discard(dev_id)
                        changed = True
                        _LOGGER.info(
                            "sync_learned_topology: inferred %s as "
                            "appliance_control (sends OTB codes, was orphan)",
                            dev_id,
                        )
                if heat_orphans:
                    new_schema[SZ_ORPHANS_HEAT] = sorted(heat_orphans)
                else:
                    new_schema.pop(SZ_ORPHANS_HEAT, None)

    # 2d. Place orphaned sensor-type devices (22:, 34:) as zone sensors.
    # In passive scan mode, thermostats (22:) and room sensors (34:) may
    # only broadcast 30C9 (setpoint) which has no zone index.  If a zone
    # has actuators (TRVs) but no sensor, and there's an orphaned sensor-
    # type device, place it as that zone's sensor.  This is a heuristic:
    # it only fires when the number of orphaned sensor-type devices equals
    # the number of zones missing a sensor, so each orphan maps to exactly
    # one zone.  When counts don't match, we leave them as orphans rather
    # than guessing wrong.
    if scan_codes:
        heat_orphans = set(new_schema.get(SZ_ORPHANS_HEAT, []))
        sensor_prefixes = ("22:", "34:")
        orphan_sensors = sorted(
            d for d in heat_orphans if isinstance(d, str) and d[:3] in sensor_prefixes
        )
        if orphan_sensors:
            # Find zones with actuators but no sensor across all TCS entries
            for tcs_id, tcs_entry in list(new_schema.items()):
                if not isinstance(tcs_entry, dict) or not tcs_id.startswith("01:"):
                    continue
                zones = tcs_entry.get(SZ_ZONES)
                if not isinstance(zones, dict):
                    continue
                zones_needing_sensor: list[str] = []
                for zone_idx, zone in zones.items():
                    if not isinstance(zone, dict):
                        continue
                    if not zone.get(SZ_SENSOR) and zone.get("actuators"):
                        zones_needing_sensor.append(zone_idx)
                # Only place when counts match exactly — one orphan per zone
                if len(zones_needing_sensor) == len(orphan_sensors) and orphan_sensors:
                    for zone_idx, dev_id in zip(
                        zones_needing_sensor, orphan_sensors, strict=False
                    ):
                        zones[zone_idx][SZ_SENSOR] = dev_id
                        heat_orphans.discard(dev_id)
                        changed = True
                        _LOGGER.info(
                            "sync_learned_topology: placed orphaned %s as "
                            "sensor for zone %s (heuristic: zone has "
                            "actuators but no sensor)",
                            dev_id,
                            zone_idx,
                        )
                    if heat_orphans:
                        new_schema[SZ_ORPHANS_HEAT] = sorted(heat_orphans)
                    else:
                        new_schema.pop(SZ_ORPHANS_HEAT, None)

    # 3. Sync top-level orphans_hvac — remove devices now in HVAC entries
    config_hvac_orphans = set(new_schema.get(SZ_ORPHANS_HVAC, []))
    learned_hvac_orphans = set((learned_schema or {}).get(SZ_ORPHANS_HVAC, []))
    if config_hvac_orphans and config_hvac_orphans != learned_hvac_orphans:
        # Find devices in config orphans that are in an HVAC entry in learned
        all_hvac_entry_devices: set[str] = set()
        for key, val in (learned_schema or {}).items():
            if not isinstance(val, dict):
                continue
            if key in config_only_keys or key in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC):
                continue
            # HVAC entries have remotes/sensors lists
            for list_key in _ZONE_LIST_KEYS:
                if list_key in val and isinstance(val[list_key], list):
                    all_hvac_entry_devices.update(val[list_key])
        # Also check the config schema (step 1h may have added remotes
        # from device comments that aren't in the learned schema yet)
        for key, val in new_schema.items():
            if not isinstance(val, dict):
                continue
            if key in config_only_keys or key in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC):
                continue
            for list_key in _ZONE_LIST_KEYS:
                if list_key in val and isinstance(val[list_key], list):
                    all_hvac_entry_devices.update(val[list_key])
        to_remove = config_hvac_orphans & all_hvac_entry_devices
        # Also remove HGI gateways (18:) — they are not HVAC devices
        to_remove |= {
            d for d in config_hvac_orphans if isinstance(d, str) and d.startswith("18:")
        }
        if to_remove:
            remaining = sorted(config_hvac_orphans - to_remove)
            if remaining:
                new_schema[SZ_ORPHANS_HVAC] = remaining
            else:
                new_schema.pop(SZ_ORPHANS_HVAC, None)
            changed = True

    # 4. Create schema entries for HGI (18:) devices from device_comments.
    # The scan engine tracks HGIs (classified as HGI type), and
    # refresh_device_comments creates comments for them.  But ramses_rf's
    # learned schema doesn't include HGIs (they're gateways, not TCSes).
    # Without this step, HGIs would only exist in device_comments and the
    # known_list — never in the schema.  By creating a minimal entry
    # (empty dict), we track them in the schema so the known_list can
    # eventually be removed.  The entry must NOT have _skipped, otherwise
    # _derive_known_list_from_schema would exclude it from the known_list
    # and the scan engine would re-discover the HGI every cycle.
    # _strip_schema_extensions drops these entries before passing to
    # ramses_rf (which doesn't support HGI at root level).
    device_comments = new_schema.get(SZ_DEVICE_COMMENTS, {})
    if isinstance(device_comments, dict):
        for dev_id, _comment in device_comments.items():
            if not isinstance(dev_id, str) or not dev_id.startswith("18:"):
                continue
            if dev_id not in new_schema:
                new_schema[dev_id] = {}
                changed = True

    # 5. Update device comments with zone info from the learned schema.
    # The scan engine's zone_idx comes from 30C9 broadcast packets, which
    # often default to zone 00.  The learned schema (from ramses_rf's active
    # discovery via 0004/0005 config packets) has the authoritative zone
    # assignments.  Replace the zone info in comments to match the learned
    # schema so comments reflect the real topology, not broadcast defaults.
    if learned_device_zones and isinstance(new_schema.get(SZ_DEVICE_COMMENTS), dict):
        comments = new_schema[SZ_DEVICE_COMMENTS]
        for dev_id, (_tcs_id, zone_idx) in learned_device_zones.items():
            comment = comments.get(dev_id)
            if not isinstance(comment, str):
                continue
            # Parse current zone from comment
            current_zone = _parse_zone_from_comment(comment)
            if current_zone == zone_idx:
                continue  # already correct
            # Replace zone info in the comment
            if current_zone is not None:
                new_comment = comment.replace(
                    f"zone {current_zone}", f"zone {zone_idx}"
                )
            elif "zone " not in comment:
                # Add zone info after "bound to ..." or after the type
                if ". " in comment:
                    # Insert before the next segment after bound_to
                    new_comment = re.sub(
                        r"(bound to [0-9A-Fa-f]+:[0-9A-Fa-f]+\. )",
                        rf"\1zone {zone_idx}. ",
                        comment,
                        count=1,
                    )
                    if new_comment == comment:
                        # No bound_to found — insert after first sentence
                        new_comment = re.sub(
                            r"(\. )",
                            rf"\1zone {zone_idx}. ",
                            comment,
                            count=1,
                        )
                else:
                    new_comment = f"{comment} zone {zone_idx}."
            else:
                new_comment = comment  # shouldn't happen
            if new_comment != comment:
                comments[dev_id] = new_comment
                changed = True

    if not changed:
        return None

    _LOGGER.info("Synced learned topology to config schema")
    return order_schema(new_schema)


SCH_NO_SVC_PARAMS = vol.Schema({}, extra=vol.PREVENT_EXTRA)
SCH_NO_ENTITY_SVC_PARAMS = cv.make_entity_service_schema(
    {},
    extra=vol.PREVENT_EXTRA,
)


# services for ramses_cc integration

_SCH_BINDING = vol.Schema({vol.Required(_SCH_CMD_CODE): vol.Any(None, _SCH_DOM_IDX)})

SCH_BIND_DEVICE = vol.Schema(
    {
        vol.Required("device_id"): _SCH_DEVICE_ID,
        vol.Required("offer"): vol.All(_SCH_BINDING, vol.Length(min=1)),
        vol.Optional("confirm", default={}): vol.Any(
            {}, vol.All(_SCH_BINDING, vol.Length(min=1))
        ),
        vol.Optional("device_info", default=None): vol.Any(None, _SCH_COMMAND),
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SEND_PACKET = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
        vol.Optional("from_id"): _SCH_DEVICE_ID,
        vol.Required("verb"): vol.In((" I", "I", "RQ", "RP", " W", "W")),
        vol.Required("code"): cv.matches_regex(r"^[0-9A-F]{4}$"),
        vol.Required("payload"): cv.matches_regex(r"^([0-9A-F][0-9A-F]){1,48}$"),
    }
)

SVC_BIND_DEVICE: Final = "bind_device"
SVC_FORCE_UPDATE: Final = "force_update"
SVC_SEND_PACKET: Final = "send_packet"
SVC_SYNC_TOPOLOGY: Final = "sync_topology"

SCH_DISCOVER_KNOWN_DEVICES = vol.Schema(
    {
        vol.Optional("device_id"): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

# Discovery scan service schemas

SCH_GET_DISCOVERED_DEVICES = vol.Schema(
    {
        vol.Optional("status"): vol.In(
            ("new", "accepted", "discarded", "removed", "lost")
        ),
        vol.Optional("enabled"): cv.boolean,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_ACCEPT_DISCOVERED_DEVICE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
        vol.Optional("owner"): vol.All(str, vol.Length(max=50)),
        vol.Optional("schema_entry"): dict,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_DISCARD_DISCOVERED_DEVICE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_REMOVE_DISCOVERED_DEVICE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_ENABLE_DISCOVERED_DEVICE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_DISABLE_DISCOVERED_DEVICE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_ADD_FAKED_REM = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
        vol.Required("bound_to"): _SCH_DEVICE_ID,
        vol.Optional("alias"): vol.All(str, vol.Length(max=50)),
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_REMOVE_DEVICE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)


# services for sensor platform

MIN_CO2_LEVEL: Final[int] = 300
MAX_CO2_LEVEL: Final[int] = 9999

SVC_PUT_CO2_LEVEL: Final = "put_co2_level"
SCH_PUT_CO2_LEVEL = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_CO2_LEVEL): vol.All(
            cv.positive_int,
            vol.Range(min=MIN_CO2_LEVEL, max=MAX_CO2_LEVEL),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

MIN_DHW_TEMP: Final[float] = 0
MAX_DHW_TEMP: Final[float] = 99

SVC_PUT_DHW_TEMP: Final = "put_dhw_temp"
SCH_PUT_DHW_TEMP = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float),
            vol.Range(min=MIN_DHW_TEMP, max=MAX_DHW_TEMP),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

MIN_INDOOR_HUMIDITY: Final[float] = 0
MAX_INDOOR_HUMIDITY: Final[float] = 100

SVC_PUT_INDOOR_HUMIDITY: Final = "put_indoor_humidity"
SCH_PUT_INDOOR_HUMIDITY = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_INDOOR_HUMIDITY): vol.All(
            cv.positive_float,
            vol.Range(min=MIN_INDOOR_HUMIDITY, max=MAX_INDOOR_HUMIDITY),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

MIN_ROOM_TEMP: Final[float] = -20
MAX_ROOM_TEMP: Final[float] = 60

SVC_PUT_ROOM_TEMP: Final = "put_room_temp"
SCH_PUT_ROOM_TEMP = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float),
            vol.Range(min=MIN_ROOM_TEMP, max=MAX_ROOM_TEMP),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

SVCS_RAMSES_SENSOR = {
    SVC_PUT_CO2_LEVEL: SCH_PUT_CO2_LEVEL,
    SVC_PUT_DHW_TEMP: SCH_PUT_DHW_TEMP,
    SVC_PUT_INDOOR_HUMIDITY: SCH_PUT_INDOOR_HUMIDITY,
    SVC_PUT_ROOM_TEMP: SCH_PUT_ROOM_TEMP,
}

# services for climate platform

SCH_DURATION = vol.All(  # of time (<=24h)
    cv.time_period,
    vol.Range(min=td(hours=1), max=td(hours=24)),
)
SCH_PERIOD = vol.All(  # of days (0-99)
    cv.time_period, vol.Range(min=td(days=0), max=td(days=99))
)

SVC_SET_SYSTEM_MODE: Final = "set_system_mode"
SCH_SET_SYSTEM_MODE = cv.make_entity_service_schema(
    # nested schemas not allowed after HA 2025.9, extra check moved to climate.py
    {
        vol.Required(ATTR_MODE): vol.In(SystemMode),
        vol.Optional(ATTR_DURATION): vol.Any(SCH_DURATION, None),
        # canBeTemporary: true, timingMode: Duration
        vol.Optional(ATTR_PERIOD): vol.Any(SCH_PERIOD, None),
        # Period: None is indefinitely; 0 is the end of today, 1 is end of tomorrow
    }
)

SCH_SET_SYSTEM_MODE_EXTRA = vol.Schema(  # original Entity Service action validation schema
    # vol.Msg(  # TODO turn on if good checks are working 8-2025
    vol.Any(
        {  # A also: Off, Heat, Cool (for pre-evohome)
            vol.Required(ATTR_MODE): vol.In(
                [SystemMode.AUTO, SystemMode.HEAT_OFF, SystemMode.RESET]
            )
        },
        {  # B
            vol.Required(ATTR_MODE): vol.In([SystemMode.ECO_BOOST]),
            vol.Optional(ATTR_DURATION): vol.Any(SCH_DURATION, None),
        },  # duration: : None is indefinitely; 0 is invalid
        {  # C canBeTemporary: true, timingMode: Period
            vol.Required(ATTR_MODE): vol.In(
                [
                    SystemMode.AWAY,
                    SystemMode.CUSTOM,
                    SystemMode.DAY_OFF,
                    SystemMode.DAY_OFF_ECO,
                ]
            ),
            vol.Optional(ATTR_PERIOD): vol.Any(SCH_PERIOD, None),
        },  # Period: None is indefinitely; 0 is the end of today, 1 is end of tomorrow
    ),
    #     msg="Invalid ramses_cc Zone Mode entry in Entity Service call",
    # ),
    extra=vol.PREVENT_EXTRA,
)

DEFAULT_MIN_TEMP: Final[float] = 5
MIN_MIN_TEMP: Final[float] = 5
MAX_MIN_TEMP: Final[float] = 21

DEFAULT_MAX_TEMP: Final[float] = 35
MIN_MAX_TEMP: Final[float] = 21
MAX_MAX_TEMP: Final[float] = 35

SVC_SET_ZONE_CONFIG: Final = "set_zone_config"
SCH_SET_ZONE_CONFIG = cv.make_entity_service_schema(
    {
        vol.Optional(ATTR_MAX_TEMP, default=DEFAULT_MAX_TEMP): vol.All(
            cv.positive_float, vol.Range(min=MIN_MAX_TEMP, max=MAX_MAX_TEMP)
        ),
        vol.Optional(ATTR_MIN_TEMP, default=DEFAULT_MIN_TEMP): vol.All(
            cv.positive_float, vol.Range(min=MIN_MIN_TEMP, max=MAX_MIN_TEMP)
        ),
        vol.Optional(ATTR_LOCAL_OVERRIDE, default=True): cv.boolean,
        vol.Optional(ATTR_OPENWINDOW, default=True): cv.boolean,
        vol.Optional(ATTR_MULTIROOM, default=True): cv.boolean,
    }
)

SVC_SET_ZONE_MODE: Final = "set_zone_mode"
SCH_SET_ZONE_MODE = cv.make_entity_service_schema(
    # nested schemas not allowed after HA 2025.9, extra check moved to climate.py
    {
        vol.Required(ATTR_MODE): vol.In(
            [
                ZoneMode.SCHEDULE,
                ZoneMode.PERMANENT,
                ZoneMode.ADVANCED,
                ZoneMode.TEMPORARY,
            ]
        ),
        vol.Optional(ATTR_SETPOINT): vol.All(
            cv.positive_float, vol.Range(min=5, max=35)
        ),
        vol.Optional(ATTR_UNTIL): cv.datetime,
        vol.Optional(ATTR_DURATION): vol.All(
            cv.time_period,
            vol.Range(min=td(minutes=5), max=td(days=1)),
        ),
    }
)

SCH_SET_ZONE_MODE_EXTRA = (
    vol.Schema(  # original Entity Service action validation schema
        # vol.Msg(  # TODO turn msg on if checks are working 10-2025
        vol.Any(
            {  # A
                vol.Required(ATTR_MODE): vol.In([ZoneMode.SCHEDULE]),
                # only mode with no setpoint
            },
            {  # B
                vol.Required(ATTR_MODE): vol.In(
                    [ZoneMode.PERMANENT, ZoneMode.ADVANCED]
                ),
                vol.Required(ATTR_SETPOINT): vol.All(
                    cv.positive_float, vol.Range(min=5, max=35)
                ),
            },
            {  # C
                vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
                vol.Required(ATTR_SETPOINT): vol.All(
                    cv.positive_float, vol.Range(min=5, max=35)
                ),
                vol.Required(ATTR_DURATION, default=td(hours=1)): vol.All(
                    cv.time_period,
                    vol.Range(min=td(minutes=5), max=td(days=1)),
                ),
            },
            {  # D
                vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
                vol.Required(ATTR_SETPOINT): vol.All(
                    cv.positive_float, vol.Range(min=5, max=35)
                ),
                vol.Required(ATTR_UNTIL): cv.datetime,
            },
        ),
        #     msg="Invalid ramses_cc Zone Mode entry in Entity Service call",
        # ),
        extra=vol.PREVENT_EXTRA,
    )
)

SVC_SET_ZONE_SCHEDULE: Final = "set_zone_schedule"
SCH_SET_ZONE_SCHEDULE = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_SCHEDULE): cv.string,
    }
)

DEFAULT_NUM_ENTRIES: Final[float] = 8
MIN_NUM_ENTRIES: Final[float] = 1
MAX_NUM_ENTRIES: Final[float] = 64

SVC_GET_SYSTEM_FAULTS: Final = "get_system_faults"
SCH_GET_SYSTEM_FAULTS = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_NUM_ENTRIES, default=DEFAULT_NUM_ENTRIES): vol.All(
            cv.positive_int, vol.Range(min=MIN_NUM_ENTRIES, max=MAX_NUM_ENTRIES)
        ),
    }
)

# Service schema for getting and setting hvac fan parameters (using ramses_rf implementation)
SVC_GET_FAN_PARAM: Final = "get_fan_param"
SVC_GET_FAN_CLIM_PARAM: Final = "get_fan_clim_param"
SVC_GET_FAN_REM_PARAM: Final = "get_fan_rem_param"
SVC_SET_FAN_PARAM: Final = "set_fan_param"
SVC_SET_FAN_CLIM_PARAM: Final = "set_fan_clim_param"
SVC_SET_FAN_REM_PARAM: Final = "set_fan_rem_param"
SVC_UPDATE_FAN_PARAMS: Final = "update_fan_params"

_TARGET_FIELDS = {
    vol.Optional("entity_id"): cv.entity_ids,
    vol.Optional("device_id"): cv.ensure_list_csv,
    vol.Optional("area_id"): cv.ensure_list_csv,
    vol.Optional("device"): vol.Any(None, cv.ensure_list_csv),
}

SCH_GET_FAN_PARAM = cv.make_entity_service_schema(
    {
        **_TARGET_FIELDS,
        vol.Required("param_id"): _SCH_PARAM_ID,
        vol.Optional("from_id"): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_GET_FAN_REM_PARAM = cv.make_entity_service_schema(
    {
        **_TARGET_FIELDS,
        vol.Required("param_id"): _SCH_PARAM_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SET_FAN_PARAM = cv.make_entity_service_schema(
    {
        **_TARGET_FIELDS,
        vol.Required("param_id"): _SCH_PARAM_ID,
        vol.Required("value"): cv.string,
        vol.Optional("from_id"): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SET_FAN_REM_PARAM = cv.make_entity_service_schema(
    {
        **_TARGET_FIELDS,
        vol.Required("param_id"): _SCH_PARAM_ID,
        vol.Required("value"): cv.string,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_UPDATE_FAN_PARAMS = cv.make_entity_service_schema(
    {
        **_TARGET_FIELDS,
        vol.Optional("from_id"): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_GET_FAN_PARAM_DOMAIN = vol.Schema(
    {
        vol.Optional("device"): vol.Any(None, cv.ensure_list_csv),
        vol.Optional("device_id"): vol.Any(None, cv.string),
        vol.Required("param_id"): _SCH_PARAM_ID,
        vol.Optional("from_id"): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_SET_FAN_PARAM_DOMAIN = vol.Schema(
    {
        vol.Optional("device"): vol.Any(None, cv.ensure_list_csv),
        vol.Optional("device_id"): vol.Any(None, cv.string),
        vol.Required("param_id"): _SCH_PARAM_ID,
        vol.Required("value"): cv.string,
        vol.Optional("from_id"): _SCH_DEVICE_ID,
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_UPDATE_FAN_PARAMS_DOMAIN = SCH_UPDATE_FAN_PARAMS


# services without their own schema
SVC_FAKE_ZONE_TEMP: Final = "fake_zone_temp"
SVC_GET_ZONE_SCHEDULE: Final = "get_zone_schedule"
SVC_RESET_SYSTEM_MODE: Final = "reset_system_mode"
SVC_RESET_ZONE_CONFIG: Final = "reset_zone_config"
SVC_RESET_ZONE_MODE: Final = "reset_zone_mode"

SVCS_RAMSES_CLIMATE = {
    SVC_FAKE_ZONE_TEMP: SCH_PUT_ROOM_TEMP,  # a convenience for SVC_PUT_ROOM_TEMP
    SVC_SET_SYSTEM_MODE: SCH_SET_SYSTEM_MODE,
    SVC_SET_ZONE_CONFIG: SCH_SET_ZONE_CONFIG,
    SVC_SET_ZONE_MODE: SCH_SET_ZONE_MODE,
    SVC_RESET_SYSTEM_MODE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_RESET_ZONE_CONFIG: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_RESET_ZONE_MODE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_GET_ZONE_SCHEDULE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_ZONE_SCHEDULE: SCH_SET_ZONE_SCHEDULE,
    SVC_GET_SYSTEM_FAULTS: SCH_GET_SYSTEM_FAULTS,
    SVC_GET_FAN_CLIM_PARAM: SCH_GET_FAN_PARAM,  # UI fan_param actions
    SVC_SET_FAN_CLIM_PARAM: SCH_SET_FAN_PARAM,
    SVC_UPDATE_FAN_PARAMS: SCH_UPDATE_FAN_PARAMS,
}

# services for water_heater platform

SVC_SET_DHW_MODE: Final = "set_dhw_mode"
SCH_SET_DHW_MODE = cv.make_entity_service_schema(
    # nested schemas not allowed after HA 2025.9, extra check moved to climate.py
    {
        vol.Required(ATTR_MODE): vol.In(
            [
                ZoneMode.SCHEDULE,
                ZoneMode.PERMANENT,
                ZoneMode.ADVANCED,
                ZoneMode.TEMPORARY,
            ]
        ),
        vol.Optional(ATTR_ACTIVE): cv.boolean,
        vol.Optional(ATTR_UNTIL): cv.datetime,
        vol.Optional(ATTR_DURATION): vol.All(
            cv.time_period,
            vol.Range(min=td(minutes=5), max=td(days=1)),
        ),
    }
)

SCH_SET_DHW_MODE_EXTRA = vol.Schema(  # original Entity Service action validation schema
    # vol.Msg(  # TODO turn on if good checks are working 8-2025
    vol.Any(
        {  # A
            vol.Required(ATTR_MODE): vol.In([ZoneMode.SCHEDULE]),
            # only mode with no active
        },
        {
            vol.Required(ATTR_MODE): vol.In([ZoneMode.PERMANENT, ZoneMode.ADVANCED]),
            vol.Required(ATTR_ACTIVE): cv.boolean,
        },
        {  # B a.k.a DHW boost
            vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
            vol.Required(ATTR_ACTIVE): True,  # TODO: vol.Any(truthy)
            vol.Required(ATTR_DURATION, default=td(hours=1)): vol.All(
                cv.time_period,
                vol.Range(min=td(minutes=5), max=td(days=1)),
            ),
        },
        {  # C
            vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
            vol.Required(ATTR_ACTIVE): cv.boolean,
            vol.Required(ATTR_DURATION): vol.All(
                cv.time_period,
                vol.Range(min=td(minutes=5), max=td(days=1)),
            ),
        },
        {  # D
            vol.Required(ATTR_MODE): vol.In([ZoneMode.TEMPORARY]),
            vol.Required(ATTR_ACTIVE): cv.boolean,
            vol.Required(ATTR_UNTIL): cv.datetime,
        },
    ),
    #     msg="Invalid ramses_cc Zone Mode entry in Entity Service call",
    # ),
    extra=vol.PREVENT_EXTRA,
)

DEFAULT_DHW_SETPOINT: Final[float] = 50  # degrees celsius, float
MIN_DHW_SETPOINT: Final[float] = 30
MAX_DHW_SETPOINT: Final[float] = 85

DEFAULT_OVERRUN: Final[int] = 5  # minutes, int
MIN_OVERRUN: Final[int] = 0
MAX_OVERRUN: Final[int] = 10

DEFAULT_DIFFERENTIAL: Final[float] = 10  # degrees celsius, float
MIN_DIFFERENTIAL: Final[float] = 1
MAX_DIFFERENTIAL: Final[float] = 10

SVC_SET_DHW_PARAMS: Final = "set_dhw_params"
SCH_SET_DHW_PARAMS = cv.make_entity_service_schema(
    {
        vol.Optional(ATTR_SETPOINT, default=DEFAULT_DHW_SETPOINT): vol.All(
            cv.positive_float, vol.Range(min=MIN_DHW_SETPOINT, max=MAX_DHW_SETPOINT)
        ),
        vol.Optional(ATTR_OVERRUN, default=DEFAULT_OVERRUN): vol.All(
            cv.positive_int, vol.Range(min=MIN_OVERRUN, max=MAX_OVERRUN)
        ),
        vol.Optional(ATTR_DIFFERENTIAL, default=DEFAULT_DIFFERENTIAL): vol.All(
            cv.positive_float, vol.Range(min=MIN_DIFFERENTIAL, max=MAX_DIFFERENTIAL)
        ),
    }
)

SVC_SET_DHW_SCHEDULE: Final = "set_dhw_schedule"
SCH_SET_DHW_SCHEDULE = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_SCHEDULE): cv.string,
    }
)

SVC_FAKE_DHW_TEMP: Final = "fake_dhw_temp"
SVC_GET_DHW_SCHEDULE: Final = "get_dhw_schedule"
SVC_RESET_DHW_MODE: Final = "reset_dhw_mode"
SVC_RESET_DHW_PARAMS: Final = "reset_dhw_params"
SVC_SET_DHW_BOOST: Final = "set_dhw_boost"

SVCS_RAMSES_WATER_HEATER = {
    SVC_FAKE_DHW_TEMP: SCH_PUT_DHW_TEMP,  # a convenience for SVC_PUT_DHW_TEMP
    SVC_RESET_DHW_MODE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_RESET_DHW_PARAMS: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_DHW_BOOST: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_DHW_MODE: SCH_SET_DHW_MODE,
    SVC_SET_DHW_PARAMS: SCH_SET_DHW_PARAMS,
    SVC_GET_DHW_SCHEDULE: SCH_NO_ENTITY_SVC_PARAMS,
    SVC_SET_DHW_SCHEDULE: SCH_SET_DHW_SCHEDULE,
}

# services for remote platform

DEFAULT_TIMEOUT: Final[int] = 60
MIN_TIMEOUT: Final[int] = 30
MAX_TIMEOUT: Final[int] = 300

SVC_LEARN_COMMAND: Final = "learn_command"
SCH_LEARN_COMMAND = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Required(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(
            cv.positive_int, vol.Range(min=MIN_TIMEOUT, max=MAX_TIMEOUT)
        ),
    },
)

# hvac services

# add_command (inject a packet without RF learning loop)
SVC_ADD_COMMAND: Final = "add_command"
SCH_ADD_COMMAND = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Required("packet_string"): cv.string,
    }
)

SVC_SEND_COMMAND: Final = "send_command"
SCH_SEND_COMMAND = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Required(ATTR_NUM_REPEATS, default=3): vol.All(
            cv.positive_int,
            vol.Range(min=MIN_NUM_REPEATS, max=MAX_NUM_REPEATS),
        ),
        vol.Required(ATTR_DELAY_SECS, default=DEFAULT_GAP_DURATION): vol.All(
            cv.positive_float,
            vol.Range(min=MIN_GAP_DURATION, max=MAX_GAP_DURATION),
        ),
    },
)

SVC_DELETE_COMMAND: Final = "delete_command"
SCH_DELETE_COMMAND = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
    },
)

SVCS_RAMSES_REMOTE = {
    SVC_DELETE_COMMAND: SCH_DELETE_COMMAND,
    SVC_ADD_COMMAND: SCH_ADD_COMMAND,
    SVC_LEARN_COMMAND: SCH_LEARN_COMMAND,
    SVC_SEND_COMMAND: SCH_SEND_COMMAND,
    SVC_GET_FAN_REM_PARAM: SCH_GET_FAN_REM_PARAM,
    SVC_SET_FAN_REM_PARAM: SCH_SET_FAN_REM_PARAM,
}

# Service schemas for number platform
SVCS_RAMSES_NUMBER: dict[str, Any] = {
    # set_fan_param is registered as a coordinator/domain service (not an entity service)
}

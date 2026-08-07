# ruff: noqa: E402
# WE DISABLE E402 (Module level import not at top of file) BECAUSE:
# The "Development Hook" logic below must modify `sys.path` BEFORE any other
# imports run. This ensures that if a local development version of `ramses_rf`
# exists, Python loads it instead of the system-installed version.
"""Support for Honeywell's RAMSES-II RF protocol, as used by CH/DHW & HVAC.

Requires a Honeywell HGI80 (or compatible) gateway.
"""

from __future__ import annotations

import copy
import logging
import os
import sys
from types import ModuleType
from typing import Any

# from collections.abc import Callable
#
# from homeassistant.components.event import EventEntity

# --- DEVELOPMENT HOOK ---
# If a local copy of ramses_rf exists, use it instead of the system installed version.
# This allows for testing changes without rebuilding the container.
#
# TODO: The dev hook below is superseded by the PYTHONPATH approach — see the
# "Testing with a local ramses_rf" section in ramses_extras/docs/HA_SIM_TEST_TOOL.md.
# The PYTHONPATH approach is simpler (no ramses_cc modification, no /config/deps
# copy needed) and works with any docker-compose that bind-mounts the ramses_rf
# source tree.  The dev hook is kept for backward compatibility but should not
# be needed for new development.

ENABLE_DEV_HOOK = False  # Set to true to enable the dev hook
DEV_LIB_PATH = "/config/deps/ramses_rf/src"

if ENABLE_DEV_HOOK and os.path.isdir(DEV_LIB_PATH):  # pragma: no cover
    # Insert at index 0 so it takes precedence over system libraries
    sys.path.insert(0, DEV_LIB_PATH)

    logging.getLogger(__name__).warning(
        "SECURITY WARNING: 'ramses_rf' is being loaded from a local development path: %s. "
        "Do not use this in a production environment unless you understand the risks.",
        DEV_LIB_PATH,
    )
# ------------------------

import voluptuous as vol  # type: ignore[import-untyped, unused-ignore]
from homeassistant import config_entries
from homeassistant.components.climate.const import DOMAIN as CLIMATE_ENTITY_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_ENTITY_DOMAIN
from homeassistant.components.remote import DOMAIN as REMOTE_ENTITY_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_ENTITY_DOMAIN
from homeassistant.components.water_heater.const import (
    DOMAIN as WATERHEATER_ENTITY_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.service import verify_domain_control
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ADVANCED_FEATURES,
    CONF_FRESH_START,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_PASSIVE_SCAN,
    CONF_SEND_PACKET,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    SVC_ACCEPT_DISCOVERED_DEVICE,
    SVC_ADD_FAKED_REM,
    SVC_DISABLE_DISCOVERED_DEVICE,
    SVC_DISCARD_DISCOVERED_DEVICE,
    SVC_DISCOVER_KNOWN_DEVICES,
    SVC_ENABLE_DISCOVERED_DEVICE,
    SVC_GET_DISCOVERED_DEVICES,
    SVC_REMOVE_DEVICE,
    SVC_REMOVE_DISCOVERED_DEVICE,
    SZ_PORT_NAME,
    SZ_SERIAL_PORT,
)
from .coordinator import RamsesCoordinator
from .schemas import (
    SCH_ACCEPT_DISCOVERED_DEVICE,
    SCH_ADD_FAKED_REM,
    SCH_BIND_DEVICE,
    SCH_DISABLE_DISCOVERED_DEVICE,
    SCH_DISCARD_DISCOVERED_DEVICE,
    SCH_DISCOVER_KNOWN_DEVICES,
    SCH_DOMAIN_CONFIG,
    SCH_ENABLE_DISCOVERED_DEVICE,
    SCH_GET_DISCOVERED_DEVICES,
    SCH_GET_FAN_PARAM_DOMAIN,
    SCH_NO_SVC_PARAMS,
    SCH_REMOVE_DEVICE,
    SCH_REMOVE_DISCOVERED_DEVICE,
    SCH_SEND_PACKET,
    SCH_SET_FAN_PARAM_DOMAIN,
    SCH_SET_POLLING_INTERVAL,
    SCH_UPDATE_FAN_PARAMS_DOMAIN,
    SVC_BIND_DEVICE,
    SVC_FORCE_UPDATE,
    SVC_GET_FAN_PARAM,
    SVC_SEND_PACKET,
    SVC_SET_FAN_PARAM,
    SVC_SET_POLLING_INTERVAL,
    SVC_SYNC_TOPOLOGY,
    SVC_UPDATE_FAN_PARAMS,
    SVCS_RAMSES_CLIMATE,
    SVCS_RAMSES_NUMBER,
    SVCS_RAMSES_REMOTE,
    SVCS_RAMSES_SENSOR,
    SVCS_RAMSES_WATER_HEATER,
    migrate_known_list_traits,
)
from .typing import RamsesConfigEntry

_LOGGER = logging.getLogger(__name__)

_RAMSES_TX_EXC: ModuleType | None = None


def _get_ramses_tx_exceptions() -> ModuleType:
    """Import ramses_tx.exceptions lazily to avoid circular import issues."""
    global _RAMSES_TX_EXC
    if _RAMSES_TX_EXC is None:
        from ramses_tx import exceptions as exc_module

        _RAMSES_TX_EXC = exc_module
    return _RAMSES_TX_EXC


CONFIG_SCHEMA = vol.All(
    cv.deprecated(DOMAIN, raise_if_present=False),
    vol.Schema({DOMAIN: SCH_DOMAIN_CONFIG}, extra=vol.ALLOW_EXTRA),
)

PLATFORMS = [Platform.EVENT]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Ramses integration."""
    hass.data[DOMAIN] = {}

    # If required, do a one-off import of entry from config yaml
    if DOMAIN in config and not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )

    # register all platform services during async_setup, since 2025.10, see
    # https://developers.home-assistant.io/blog/2025/09/25/entity-services-api-changes
    for entity_domain, services in (
        (CLIMATE_ENTITY_DOMAIN, SVCS_RAMSES_CLIMATE),
        (REMOTE_ENTITY_DOMAIN, SVCS_RAMSES_REMOTE),
        (SENSOR_ENTITY_DOMAIN, SVCS_RAMSES_SENSOR),
        (WATERHEATER_ENTITY_DOMAIN, SVCS_RAMSES_WATER_HEATER),
        (NUMBER_ENTITY_DOMAIN, SVCS_RAMSES_NUMBER),
    ):
        for key, schema in services.items():
            _LOGGER.debug(
                "Registering %s entity service %s with schema %s",
                entity_domain,
                key,
                schema,
            )
            supports_resp = (
                SupportsResponse.OPTIONAL
                if "schedule" in key
                else SupportsResponse.NONE
            )
            service.async_register_platform_entity_service(
                hass,
                DOMAIN,
                key,
                entity_domain=entity_domain,
                schema=schema,
                func=f"async_{key}",
                supports_response=supports_resp,
            )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: RamsesConfigEntry) -> bool:
    """Create a ramses_rf (RAMSES_II)-based system."""
    _LOGGER.debug("Setting up entry %s...", entry.entry_id)

    tx_exc = _get_ramses_tx_exceptions()

    # Check if this entry is already set up
    if entry.entry_id in hass.data[DOMAIN]:
        _LOGGER.debug("Entry %s is already set up", entry.entry_id)
        return True

    healed_options = _healed_serial_port_options(
        {**entry.options},
        mqtt_entries_present=bool(hass.config_entries.async_entries("mqtt")),
    )
    if healed_options is not None:
        hass.config_entries.async_update_entry(entry, options=healed_options)
        _LOGGER.warning(
            "Healed missing serial_port for entry %s by defaulting to mqtt_ha. "
            "Please verify transport settings in the options flow.",
            entry.entry_id,
        )

    # Phase 4 idempotent cleanup: strip stale known_list / enforce_known_list
    # from options if an older profile_loader wrote them after migration.
    # The schema is the sole source of truth — known_list is derived from it.
    _cleanup_stale_known_list(hass, entry)

    # Fresh-start flag: when set (by clear_cache or an external tool like
    # the device simulator), wipe .storage so the integration starts from
    # a clean slate.  This must happen before the coordinator is created
    # so the wipe can't be interrupted by a reload.
    if entry.options.get(CONF_FRESH_START):
        _LOGGER.info("Fresh start requested, clearing .storage cache")

        # Use Store.async_remove() which both invalidates the in-memory
        # cache (the StorageManager keeps a cross-instance cache) AND
        # deletes the .storage file.  Simply deleting the file is not
        # enough because a new Store instance would still read cached
        # data from the manager.
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        await store.async_remove()
        _LOGGER.info("Fresh start: cleared .storage cache")
        # Remove the flag from the config entry options so it doesn't
        # trigger again on the next normal reload.
        new_options = dict(entry.options)
        new_options.pop(CONF_FRESH_START, None)
        hass.config_entries.async_update_entry(entry, options=new_options)

    coordinator = RamsesCoordinator(hass, entry)

    try:
        # Store the coordinator in hass.data before setting it up
        hass.data[DOMAIN][entry.entry_id] = coordinator
        await coordinator.async_setup()
    except tx_exc.TransportSourceInvalid as err:  # not TransportSerialError
        _LOGGER.error("Unrecoverable problem with the serial port: %s", err)
        hass.data[DOMAIN].pop(entry.entry_id, None)  # Clean up if setup fails
        raise ConfigEntryError(f"Unrecoverable serial port error: {err}") from err
    except (tx_exc.TransportError, TimeoutError, ConfigEntryNotReady) as err:
        _LOGGER.warning(
            "Failed to set up entry %s (will retry): %s", entry.entry_id, err
        )
        hass.data[DOMAIN].pop(entry.entry_id, None)  # Clean up if setup fails
        raise ConfigEntryNotReady(
            f"There is a problem with the serial port: {err}"
        ) from err
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)  # Clean up if setup fails
        raise

    # Start the coordinator after successful setup
    await coordinator.async_start()

    _LOGGER.debug("Registering domain services and events")
    async_register_domain_services(hass, entry, coordinator)  # for Services
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)  # for Events
    _LOGGER.debug("Finished registering domain services and events")

    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    _LOGGER.debug("Successfully set up entry %s", entry.entry_id)

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy configuration options to the current version 3.

    v1→v2: Clean up packet_log and ramses_rf dicts (remove deprecated keys).
    v2→v3: Phase 4 — merge known_list traits into schema, drop known_list
           and enforce_known_list from options (schema is now the sole source).

    :param hass: The Home Assistant instance.
    :param entry: The ConfigEntry to migrate.
    :return: True if the migration succeeded.
    """
    _LOGGER.debug("Migrating ramses_cc config entry from version %s", entry.version)

    if entry.version == 1:
        # Create a deep copy of the immutable MappingProxyType to mutate it
        new_options = {**entry.options}

        # 1. Clean up packet_log dictionary
        if isinstance(new_options.get("packet_log"), dict):
            packet_log = {**new_options["packet_log"]}
            # Remove deprecated key mentioned in issue #592
            packet_log.pop("file_name", None)
            # Translate legacy rotate_backups to modern key
            if "rotate_backups" in packet_log:
                packet_log["packet_log_retention_days"] = packet_log.pop(
                    "rotate_backups"
                )

            new_options["packet_log"] = packet_log

        # 2. Clean up ramses_rf dictionary (legacy database storage flags)
        if isinstance(new_options.get("ramses_rf"), dict):
            ramses_rf = {**new_options["ramses_rf"]}
            # Remove deprecated database keys
            for deprecated_key in ["use_database", "database_file", "file_name"]:
                ramses_rf.pop(deprecated_key, None)
            new_options["ramses_rf"] = ramses_rf

        # Update the entry with the cleaned options and bump version
        hass.config_entries.async_update_entry(entry, options=new_options, version=2)
        _LOGGER.info(
            "Successfully migrated ramses_cc config entry %s to version 2",
            entry.entry_id,
        )

    if entry.version == 2:
        # Safety net: snapshot v2 options before the irreversible v2->v3
        # migration.  HA only migrates forward, so a user who downgrades
        # ramses_cc back to v2 code cannot auto-migrate a v3 entry back.
        # This backup allows manual recovery — see the docstring above.
        backup_store = Store(hass, 1, f"{DOMAIN}_migration_v2_backup")
        await backup_store.async_save(
            {
                "entry_id": entry.entry_id,
                "version": 2,
                "options": copy.deepcopy(dict(entry.options)),
            }
        )
        _LOGGER.info(
            "Phase 4 migration: saved v2 options backup to "
            ".storage/%s_migration_v2_backup",
            DOMAIN,
        )

        new_options = {**entry.options}

        # Phase 4: merge known_list traits into schema, then drop known_list.
        # The known_list was the legacy trait store (alias, class, faked,
        # bound, scheme).  The schema now carries these as _ prefixed keys
        # (_alias, _class, _faked, _bound, _scheme).
        #
        # Two cases:
        # 1. Device already has a schema entry → merge traits into it.
        # 2. Device is in known_list but NOT in schema → create a schema
        #    entry with the traits so the device isn't lost (enforce_known_list
        #    is always-on now, so devices must be in the schema-derived
        #    known_list to be allowed through).
        known_list = new_options.pop("known_list", None)
        if known_list and isinstance(known_list, dict):
            schema = new_options.get("schema", {})
            if isinstance(schema, dict):
                new_options["schema"] = migrate_known_list_traits(schema, known_list)
                _LOGGER.info(
                    "Phase 4 migration: merged known_list traits into schema "
                    "for config entry %s",
                    entry.entry_id,
                )

        # Remove enforce_known_list from ramses_rf sub-dict — it is now
        # always-on (hardcoded in coordinator._create_client).
        if isinstance(new_options.get("ramses_rf"), dict):
            ramses_rf = {**new_options["ramses_rf"]}
            ramses_rf.pop("enforce_known_list", None)
            new_options["ramses_rf"] = ramses_rf

        # Remove deprecated disabled_devices key (replaced by _disabled trait)
        new_options.pop("disabled_devices", None)

        hass.config_entries.async_update_entry(entry, options=new_options, version=3)
        _LOGGER.info(
            "Successfully migrated ramses_cc config entry %s to version 3 (Phase 4)",
            entry.entry_id,
        )

    return True


def _cleanup_stale_known_list(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Strip stale known_list / enforce_known_list from options if present.

    Phase 4: the schema is the sole source of truth.  An older profile_loader
    may have written known_list/enforce_known_list to options after the v2→v3
    migration ran.  This idempotent cleanup merges any known_list traits into
    the schema and removes both keys.
    """
    if "known_list" not in entry.options and not (
        isinstance(entry.options.get("ramses_rf"), dict)
        and "enforce_known_list" in entry.options["ramses_rf"]
    ):
        return

    new_options = {**entry.options}
    changed = False

    if "known_list" in new_options:
        known_list = new_options.pop("known_list")
        if known_list and isinstance(known_list, dict):
            schema = new_options.get("schema", {})
            if isinstance(schema, dict):
                new_options["schema"] = migrate_known_list_traits(schema, known_list)
        changed = True

    ramses_rf = new_options.get("ramses_rf", {})
    if isinstance(ramses_rf, dict) and "enforce_known_list" in ramses_rf:
        ramses_rf = dict(ramses_rf)
        ramses_rf.pop("enforce_known_list", None)
        new_options["ramses_rf"] = ramses_rf
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, options=new_options)
        _LOGGER.info(
            "Cleaned up stale known_list/enforce_known_list from config entry %s",
            entry.entry_id,
        )


def _healed_serial_port_options(
    options: dict[str, Any], *, mqtt_entries_present: bool
) -> dict[str, Any] | None:
    """Return healed options if serial_port is missing and MQTT is implied."""
    serial_port = options.get(SZ_SERIAL_PORT)
    serial_port_missing = not isinstance(serial_port, dict) or not serial_port.get(
        SZ_PORT_NAME
    )

    mqtt_hints_present = bool(options.get(CONF_MQTT_USE_HA)) or any(
        key in options for key in (CONF_MQTT_HGI_ID, CONF_MQTT_TOPIC)
    )

    if not serial_port_missing or not (mqtt_hints_present or mqtt_entries_present):
        return None

    new_options = {**options}
    new_options[SZ_SERIAL_PORT] = {SZ_PORT_NAME: "mqtt_ha"}
    new_options.setdefault(CONF_MQTT_USE_HA, True)
    return new_options


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    # Check if the coordinator has suppressed the reload (e.g. during
    # accept_discovered_device, where the running coordinator already has
    # the updated options and a reload would be disruptive).
    #
    # _suppress_reload is a timestamp — if it was set within the last 5
    # seconds, the reload is suppressed.  This avoids the race condition
    # where the flag is reset before the update listener (scheduled as an
    # async task by async_update_entry) has a chance to run.
    import time as time_mod

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    suppress_ts = getattr(coordinator, "_suppress_reload", 0.0) if coordinator else 0.0
    if suppress_ts and (time_mod.time() - suppress_ts) < 5:
        _LOGGER.debug(
            "Config entry %s updated, but reload suppressed (accept flow)",
            entry.entry_id,
        )
        return

    _LOGGER.debug("Config entry %s updated, reloading integration...", entry.entry_id)

    # Just reload the entry, which will handle unloading and setting up again
    # instead of fire and forget with async_create_task
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not await coordinator.async_unload_platforms():
        return False

    # Only remove domain-level services registered in async_register_domain_services.
    # Entity platform services (registered once in async_setup) must NOT be removed
    # here because async_setup is not called again on reload, which would cause
    # "Action ramses_cc.<service> not found" errors after every reload.
    _domain_services = {
        SVC_BIND_DEVICE,
        SVC_FORCE_UPDATE,
        SVC_SEND_PACKET,
        SVC_SET_FAN_PARAM,
        SVC_GET_FAN_PARAM,
        SVC_UPDATE_FAN_PARAMS,
        SVC_DISCOVER_KNOWN_DEVICES,
        SVC_SYNC_TOPOLOGY,
        # Discovery scan services — registered conditionally (passive scan
        # enabled), must be removed on unload so they don't linger with a
        # stale coordinator reference if scan is disabled before reload
        SVC_GET_DISCOVERED_DEVICES,
        SVC_ACCEPT_DISCOVERED_DEVICE,
        SVC_DISCARD_DISCOVERED_DEVICE,
        SVC_REMOVE_DISCOVERED_DEVICE,
        SVC_ENABLE_DISCOVERED_DEVICE,
        SVC_DISABLE_DISCOVERED_DEVICE,
        SVC_ADD_FAKED_REM,
        SVC_REMOVE_DEVICE,
    }
    for svc in _domain_services:
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)  # for Events

    hass.data[DOMAIN].pop(entry.entry_id)
    return True


@callback
def async_register_domain_services(
    hass: HomeAssistant, entry: ConfigEntry, _coordinator: RamsesCoordinator
) -> None:
    """Set up and register handlers for the domain-wide services."""

    @verify_domain_control(DOMAIN)
    async def async_bind_device(call: ServiceCall) -> None:
        await _coordinator.async_bind_device(call)

    @verify_domain_control(DOMAIN)
    async def async_force_update(call: ServiceCall) -> None:
        await _coordinator.async_force_update(call)

    @verify_domain_control(DOMAIN)
    async def async_sync_topology(call: ServiceCall) -> None:
        await _coordinator.async_sync_topology(call)

    @verify_domain_control(DOMAIN)
    async def async_send_packet(call: ServiceCall) -> None:
        await _coordinator.async_send_packet(call)

    @verify_domain_control(DOMAIN)
    async def async_discover_known_devices(call: ServiceCall) -> None:
        await _coordinator.async_discover_known_devices(call)

    @verify_domain_control(DOMAIN)
    async def async_get_discovered_devices(call: ServiceCall) -> None:
        await _coordinator.async_get_discovered_devices(call)

    @verify_domain_control(DOMAIN)
    async def async_accept_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_accept_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_discard_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_discard_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_remove_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_remove_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_enable_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_enable_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_disable_discovered_device(call: ServiceCall) -> None:
        await _coordinator.async_disable_discovered_device(call)

    @verify_domain_control(DOMAIN)
    async def async_add_faked_rem(call: ServiceCall) -> None:
        await _coordinator.async_add_faked_rem(call)

    @verify_domain_control(DOMAIN)
    async def async_remove_device(call: ServiceCall) -> None:
        await _coordinator.async_remove_device(call)

    @verify_domain_control(DOMAIN)
    async def async_set_fan_param(call: ServiceCall) -> None:
        await _coordinator.async_set_fan_param(call)

    @verify_domain_control(DOMAIN)
    async def async_get_fan_param(call: ServiceCall) -> None:
        await _coordinator.async_get_fan_param(call)

    @verify_domain_control(DOMAIN)
    async def async_update_fan_params(call: ServiceCall) -> None:
        await _coordinator._async_run_fan_param_sequence(call)

    @verify_domain_control(DOMAIN)
    async def async_set_polling_interval(call: ServiceCall) -> None:
        await _coordinator.async_set_polling_interval(call)

    # register the handlers
    hass.services.async_register(
        DOMAIN, SVC_BIND_DEVICE, async_bind_device, schema=SCH_BIND_DEVICE
    )

    hass.services.async_register(
        DOMAIN, SVC_FORCE_UPDATE, async_force_update, schema=SCH_NO_SVC_PARAMS
    )

    hass.services.async_register(
        DOMAIN, SVC_SYNC_TOPOLOGY, async_sync_topology, schema=SCH_NO_SVC_PARAMS
    )

    hass.services.async_register(
        DOMAIN,
        SVC_DISCOVER_KNOWN_DEVICES,
        async_discover_known_devices,
        schema=SCH_DISCOVER_KNOWN_DEVICES,
    )

    # Passive device scan services (only if scan is enabled)
    if entry.options.get(CONF_ADVANCED_FEATURES, {}).get(CONF_PASSIVE_SCAN):
        hass.services.async_register(
            DOMAIN,
            SVC_GET_DISCOVERED_DEVICES,
            async_get_discovered_devices,
            schema=SCH_GET_DISCOVERED_DEVICES,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_ACCEPT_DISCOVERED_DEVICE,
            async_accept_discovered_device,
            schema=SCH_ACCEPT_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_DISCARD_DISCOVERED_DEVICE,
            async_discard_discovered_device,
            schema=SCH_DISCARD_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_REMOVE_DISCOVERED_DEVICE,
            async_remove_discovered_device,
            schema=SCH_REMOVE_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_ENABLE_DISCOVERED_DEVICE,
            async_enable_discovered_device,
            schema=SCH_ENABLE_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_DISABLE_DISCOVERED_DEVICE,
            async_disable_discovered_device,
            schema=SCH_DISABLE_DISCOVERED_DEVICE,
        )
        hass.services.async_register(
            DOMAIN,
            SVC_ADD_FAKED_REM,
            async_add_faked_rem,
            schema=SCH_ADD_FAKED_REM,
        )

    # remove_device is always available — users may want to remove devices
    # that were added manually or via a previous passive scan.
    hass.services.async_register(
        DOMAIN,
        SVC_REMOVE_DEVICE,
        async_remove_device,
        schema=SCH_REMOVE_DEVICE,
    )

    hass.services.async_register(
        DOMAIN, SVC_SET_FAN_PARAM, async_set_fan_param, schema=SCH_SET_FAN_PARAM_DOMAIN
    )
    hass.services.async_register(
        DOMAIN, SVC_GET_FAN_PARAM, async_get_fan_param, schema=SCH_GET_FAN_PARAM_DOMAIN
    )
    hass.services.async_register(
        DOMAIN,
        SVC_UPDATE_FAN_PARAMS,
        async_update_fan_params,
        schema=SCH_UPDATE_FAN_PARAMS_DOMAIN,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SET_POLLING_INTERVAL,
        async_set_polling_interval,
        schema=SCH_SET_POLLING_INTERVAL,
    )

    # Advanced features
    if entry.options.get(CONF_ADVANCED_FEATURES, {}).get(CONF_SEND_PACKET):
        hass.services.async_register(
            DOMAIN, SVC_SEND_PACKET, async_send_packet, schema=SCH_SEND_PACKET
        )

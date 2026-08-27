"""Coordinator for RAMSES integration."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import re
import time
from collections.abc import Callable, Coroutine, Sequence
from contextlib import suppress
from copy import deepcopy
from datetime import datetime as dt, timedelta as td
from functools import lru_cache
from threading import Semaphore
from typing import TYPE_CHECKING, Any, Final, TypeVar

import probatio as vol  # type: ignore[import-untyped, unused-ignore]
import serial  # type: ignore[import-untyped]
from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
    async_dismiss as async_dismiss_notification,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ramses_rf.config import strip_and_map_traits as _strip_and_map_traits
from ramses_rf.devices import (
    _CLASS_BY_SLUG,
    DEV_TYPE_MAP,
    Device,
    DeviceHvac,
    HvacRemoteBase,
    HvacVentilator,
)
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.schemas import (
    SZ_ACTUATORS,
    SZ_APPLIANCE_CONTROL,
    SZ_DHW_SYSTEM,
    SZ_DHW_VALVE,
    SZ_HTG_VALVE,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
    SZ_ORPHANS_HEAT,
    SZ_ORPHANS_HVAC,
    SZ_REMOTES,
    SZ_SENSOR,
    SZ_SENSORS,
    SZ_SYSTEM,
    SZ_UFH_SYSTEM,
    SZ_ZONES,
)
from ramses_rf.systems import Evohome, System, Zone
from ramses_rf.topology import Child
from ramses_tx import exceptions as exc
from ramses_tx.config import EngineConfig
from ramses_tx.const import SZ_ACTIVE_HGI, Code
from ramses_tx.dtos import PacketDTO
from ramses_tx.schemas import extract_serial_port

from .const import (
    CONF_ADVANCED_FEATURES,
    CONF_AUTO_NOTIFY,
    CONF_COMMANDS,
    CONF_GATEWAY_TIMEOUT,
    CONF_LOST_THRESHOLD,
    CONF_MQTT_HGI_ID,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USE_HA,
    CONF_PASSIVE_SCAN,
    CONF_RAMSES_RF,
    CONF_SCAN_INTERVAL,
    CONF_SCHEMA,
    CONF_SSOT_MIGRATED,
    DEFAULT_HGI_ID,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    SIGNAL_NEW_DEVICES,
    SIGNAL_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
    SZ_CLIENT_STATE,
    SZ_DEVICE_COMMENTS,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_OWNER,
    SZ_PACKET_LOG,
    SZ_PACKETS,
    SZ_PORT_NAME,
    SZ_SCHEMA,
    SZ_SERIAL_PORT,
    SZ_TR_ALIAS,
    SZ_TR_BOUND,
    SZ_TR_CLASS,
    SZ_TR_COMMANDS,
    SZ_TR_DISABLED,
    SZ_TR_FAKED,
    SZ_TR_NAME,
    SZ_TR_OWNER,
    SZ_TR_SCHEME,
    SZ_TR_SKIPPED,
)
from .discovery import DiscoveryManager
from .fan_handler import RamsesFanHandler
from .helpers import clear_async_attr_cache
from .mqtt_bridge import RamsesMqttBridge
from .schemas import (
    _SCHEMA_EXTENSION_KEYS,
    _strip_and_orchestrate,
    merge_schemas,
    sync_learned_topology,
)
from .services import RamsesServiceHandler
from .store import RamsesStore

if TYPE_CHECKING:
    from .entity import RamsesEntity
    from .number import RamsesNumberParam

_LOGGER = logging.getLogger(__name__)

# Step 5: this polling loop is now a safety net for the event-driven
# schema_updated callback (see _on_rf_schema_updated).  It still covers
# periodic packet-state persistence (a separate concern from topology
# sync) and any topology change that doesn't go through
# DeviceRegistry.handle_topology_event.
# Increased from 5m to 30m (issue ramses-rf/ramses_rf#1027): topology
# changes are already saved near-real-time via the debounced
# _on_rf_schema_updated callback, and a final save happens on shutdown
# via _async_save_on_unload (triggered by async_unload_entry).  The
# interval is now just a safety net for packet-state persistence,
# cutting flash writes by ~83% on HA OS (RPi / HA Yellow eMMC).
SAVE_STATE_INTERVAL: Final[td] = td(minutes=30)
# Step 5: trailing-debounce window for the ramses_rf schema_updated
# callback.  Coalesces bursts of topology events into a single save.
# 2 seconds is long enough to absorb a multi-zone 000C sequence or a
# discovery scan processing several 1FC9 packets, short enough that a
# user-initiated binding is reflected in the config entry near-real-time.
_SCHEMA_UPDATED_DEBOUNCE: Final[td] = td(seconds=2)
_DEVICE_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9A-F]{2}:[0-9A-F]{6}$", re.I
)
# _TCS_ORPHAN_PREFIXES is imported from .schemas (single definition
# shared with strip_traits_for_validation).
_EXTRACT_DEVICE_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9A-F]{2}:[0-9A-F]{6}", re.I
)


@lru_cache(maxsize=128)
def _normalize_class_slug(value: str) -> str:
    """Normalize a class value to a short DevType slug.

    Handles three input forms:
    1. Already a DevType slug (e.g. 'FAN', 'REM') — returned as-is.
    2. Lowercase DevType slug (e.g. 'fan', 'rem') — uppercased.
    3. Entity slug (e.g. 'ventilator', 'switch', 'co2_sensor') —
       mapped to the corresponding DevType slug via DEV_TYPE_MAP.

    Unknown values are returned as-is (ramses_rf will fall back to the
    default class, and _validate_schema_for_ramserf will log a warning).
    """
    if not value or not isinstance(value, str):
        return value
    # Already a valid DevType slug?
    if value in _CLASS_BY_SLUG:
        return value
    # Try uppercase (fan -> FAN)
    if value.upper() in _CLASS_BY_SLUG:
        return value.upper()
    # Try entity slug -> DevType slug (ventilator -> FAN)
    try:
        slug = str(DEV_TYPE_MAP.slug(value))
        if slug in _CLASS_BY_SLUG:
            return slug
    except KeyError:
        pass
    return value  # unknown — keep as-is


# Generic Type for Entity Discovery to satisfy Pylance covariance
_T_Entity = TypeVar("_T_Entity", bound=RamsesRFEntity)


class RamsesCoordinator(DataUpdateCoordinator):
    """Central coordinator for the RAMSES integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the RAMSES coordinator and its data structures."""
        self.hass = hass
        self.entry = entry
        self.options = deepcopy(dict(entry.options))
        self.store = RamsesStore(hass)

        # Initialize handlers
        self.fan_handler = RamsesFanHandler(self)
        self.service_handler = RamsesServiceHandler(self)
        self.mqtt_bridge: RamsesMqttBridge | None = None
        self.discovery_manager: DiscoveryManager | None = None
        self._cached_discovery_state: dict[str, Any] | None = None
        self._suppress_reload: float = 0.0  # timestamp; >0 means suppressed
        self._skip_topology_sync: bool = False
        self._skip_discovery_save: bool = False
        self._discovery_filter_ids: set[str] | None = None
        self._skip_discovery_restore: bool = False
        # Step 5: trailing-debounce task for the ramses_rf schema_updated
        # callback.  Coalesces bursts of topology events (e.g. a discovery
        # scan processing many 1FC9 packets) into a single save cycle.
        # Cancelled on unload and rescheduled on each new event.
        self._schema_updated_debounce_task: asyncio.Task[None] | None = None
        # Device IDs explicitly removed by the user via remove_device.
        # sync_learned_topology must NOT re-add these (ramses_rf has no
        # remove_device API, so the learned schema still references them).
        # Cleared on restart (the cached schema won't have them either,
        # because merge_schemas filters in SSOT mode).
        self._removed_devices: set[str] = set()

        # Redact port details for safe exchange of logs
        print_options = deepcopy(dict(self.options))  # need an extra copy
        if print_options.get("serial_port", None) is not None:
            ser_port = print_options.get("serial_port", "")
            if isinstance(ser_port, dict):
                if ser_port.get("port_name", "").startswith("mqtt://"):
                    print_options["serial_port"]["port_name"] = (
                        "mqtt://usr:pwd(at)url:1883"
                    )
        _LOGGER.debug("Config = %s", print_options)

        self.client: Gateway | None = None
        self._remotes: dict[str, dict[str, Any]] = {}
        # Track device IDs that have _commands in the schema at load time.
        # Used by _sync_remotes_to_schema to prevent resurrecting
        # user-deleted _commands from .storage[remotes].
        self._devices_with_commands: set[str] = set()

        self._platform_setup_tasks: dict[str, asyncio.Task[Any]] = {}
        self._entities: dict[str, RamsesEntity] = {}  # domain entities
        self._device_info: dict[str, DeviceInfo] = {}
        self._disabled_device_ids: set[str] = (
            set()
        )  # _disabled devices (no entities)

        # Discovered client objects...
        self._devices: list[Device] = []
        self._systems: list[System] = []
        self._zones: list[Zone] = []
        self._dhws: list[Zone] = []
        self._parameter_entities_pending: set[str] = set()
        self._parameter_entities_loaded: set[str] = set()
        self._parameter_entities_created: dict[str, RamsesNumberParam] = {}

        self._sem = Semaphore(value=1)

        # Gateway health: _gateway_offline_notified prevents re-creating
        # the persistent notification on every heartbeat.  The actual
        # check uses ramses_rf's Gateway.is_active (SSOT).
        # _health_check_count skips the first few checks to avoid a
        # false "offline" notification during startup (is_active returns
        # False before any packets have arrived).
        self._gateway_offline_notified: bool = False
        self._health_check_count: int = 0

        # Initialize platforms dictionary to store platform references
        self.platforms: dict[str, Any] = {}
        self.learn_device_id: str | None = None

        # Load scan interval from options, default to 60s if missing
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, 60)
        _LOGGER.debug(
            "Coordinator initialized with scan_interval: %s seconds",
            scan_interval,
        )

        # Initialize the DataUpdateCoordinator
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=td(seconds=scan_interval),
        )

    @property
    def active_hgi_id(self) -> str | None:
        """Return the active HGI gateway device ID if available.

        :return: Active HGI device ID string or None.
        :rtype: str | None
        """
        if not self.client:
            return None
        gwy: Gateway = self.client
        engine = getattr(gwy, "_engine", None)
        transport = getattr(engine, "_transport", None) or getattr(
            gwy, "_transport", None
        )
        active_hgi_id: str | None = None
        if transport is not None:
            with suppress(AttributeError, KeyError, TypeError):
                active_hgi_id = transport.get_extra_info(SZ_ACTIVE_HGI)
        if not active_hgi_id:
            active_hgi_id = getattr(engine, "_hgi_id", None)
        if not active_hgi_id and gwy.hgi:
            active_hgi_id = gwy.hgi.id
        return active_hgi_id

    def _get_saved_packets(
        self, client_state: dict[str, Any]
    ) -> dict[str, dict[str, Any] | str]:
        """Filter cached packets to remove expired or unwanted entries.

        Extracts device IDs dynamically to enforce the known list, ensuring
        compatibility with varying packet string formats and JSON DTOs.
        """
        msg_code_filter = ["313F"]
        # Phase 4: known_list is derived from schema, no longer stored in
        # config entry options.  Use the schema-derived known_list for
        # packet filtering.
        config_schema = self.options.get(CONF_SCHEMA, {})
        known_list = self._derive_known_list_from_schema(config_schema)
        enforce_known_list = True  # Phase 4: always-on

        packets: dict[str, dict[str, Any] | str] = {}
        now = dt_util.now()

        # Iterate over packets from storage
        for dtm, packet in client_state.get(SZ_PACKETS, {}).items():
            try:
                dt_obj = dt.fromisoformat(dtm)
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            except ValueError:
                _LOGGER.warning(
                    "Ignoring cached packet with invalid timestamp: %s", dtm
                )
                continue

            # 1. Check age (keep last 24 hours)
            if dt_obj <= now - td(days=1):
                continue

            # Handle new PacketDTO dictionary format natively
            if isinstance(packet, dict):
                # 2. Filter out unwanted message codes
                if packet.get("code") in msg_code_filter:
                    continue

                # 3. Enforce known list dynamically
                if enforce_known_list:
                    found_devices = []
                    # Check raw L3 addresses (addr1/2/3) first — these are
                    # the legacy PacketDTO keys that ramses_rf's get_state()
                    # provides for known_list enforcement (PR 782).
                    # Fall back to logical src/dst for ramses_rf versions
                    # that only have PR 780 (no addr1/2/3 keys yet).
                    for key in ("addr1", "addr2", "addr3", "src", "dst"):
                        addr = packet.get(key)
                        if not addr:
                            continue
                        if (
                            isinstance(addr, dict)
                            and addr.get("device_type") is not None
                            and addr.get("device_id") is not None
                        ):
                            # Reconstruct address string safely
                            found_devices.append(
                                f"{addr['device_type']:02d}:{addr['device_id']:06d}"
                            )
                        else:  # simple string passed in PacketDTO
                            found_devices.append(addr)

                    # If the packet contains no known_list devices, discard it
                    if not any(dev in known_list for dev in found_devices):
                        continue

            # Fallback for users migrating from legacy string-based caches
            else:
                # 2. Filter out unwanted message codes
                # String containment is safer against changes than packet[41:45]
                if any(f" {code} " in packet for code in msg_code_filter):
                    continue

                # 3. Enforce known list dynamically
                if enforce_known_list:
                    # Extract all potential device IDs from the string
                    found_devices = _EXTRACT_DEVICE_ID_RE.findall(packet)

                    # If the packet contains no known_list devices, discard it
                    if not any(dev in known_list for dev in found_devices):
                        continue

            packets[dtm] = packet

        return packets

    async def async_setup(self) -> None:
        """Set up the RAMSES client and load configuration.

        Loads storage, restores remote commands, and initializes Gateway
        client.
        """
        storage = await self.store.async_load()
        _LOGGER.debug("Storage = %s", storage)

        # 1. Load Remotes
        # Precedence (highest wins):
        #   1. Schema _commands (SSOT — user edits, learn_command writes here)
        #   2. .storage[remotes] (cache — learn_command writes here first)
        # Phase 4: known_list[dev][commands] legacy fallback removed —
        # the v2→v3 config entry migration merges these into schema.
        self._remotes = storage.get(SZ_REMOTES, {})

        # 1a. Merge schema _commands into _remotes (SSOT — highest precedence)
        config_schema = self.options.get(CONF_SCHEMA, {})
        if isinstance(config_schema, dict):
            remotes_from_schema = {
                dev_id: entry.get(SZ_TR_COMMANDS, {})
                for dev_id, entry in config_schema.items()
                if isinstance(entry, dict) and SZ_TR_COMMANDS in entry
            }
            if remotes_from_schema:
                self._remotes = self._remotes | remotes_from_schema
                _LOGGER.debug(
                    "Loaded %d device(s) with _commands from schema",
                    len(remotes_from_schema),
                )
            # Track which devices have _commands in the schema at load
            # time, so _sync_remotes_to_schema can skip them if _commands
            # is later absent (user deletion → don't resurrect from remotes).
            self._devices_with_commands = set(remotes_from_schema.keys())

        client_state: dict[str, Any] = storage.get(SZ_CLIENT_STATE, {})

        # 1b. Migration: when passive scan is enabled, check if known_list
        # has devices not in schema and migrate them.  For legacy setups
        # (passive scan off), the derivation logic already handles
        # known_list-only devices, so no migration is needed.
        #
        # This is a ONE-TIME legacy migration, tracked via the
        # CONF_SSOT_MIGRATED flag in the config entry options.  After the
        # migration (or after a schema wipe, which implies the user is on
        # the SSOT model), known_list entries not in the schema are just
        # trait overrides (class, alias, faked, bound, commands) waiting
        # to be applied when devices are (re-)accepted — they must NEVER
        # be migrated into the schema again, and must NOT be wiped (they
        # hold valuable user data such as remote command mappings).
        #
        # When the schema is empty (no real device entries), the user has
        # intentionally wiped it — clear stale discovery state so devices
        # are re-discoverable as NEW.
        # Phase 4: known_list is no longer stored in the config entry
        # (removed by v2→v3 migration).  The schema is the sole source.
        config_schema = self.options.get(CONF_SCHEMA, {})
        advanced = self.entry.options.get(CONF_ADVANCED_FEATURES, {})
        schema_is_ssot = bool(advanced.get(CONF_PASSIVE_SCAN, False))
        if schema_is_ssot:
            schema_device_ids = self._extract_schema_device_ids(config_schema)
            migration_done = bool(advanced.get(CONF_SSOT_MIGRATED, False))

            # Check if schema is effectively empty (no real device entries,
            # only extension keys like _disabled, _skipped, orphans lists).
            # Foreign-owned devices (_owner: not-me) are preserved across
            # schema wipes (issue 1020) but do not count as "real" devices
            # for the fresh-start check — the user wiped their own devices
            # and expects discovery metadata to be cleared so they are
            # re-discovered as NEW.
            foreign_ids = self._extract_foreign_device_ids(config_schema)
            schema_has_devices = bool(schema_device_ids - foreign_ids)

            if not schema_has_devices:
                # Schema is empty — either a fresh SSOT start or the user
                # wiped it.  Devices are (re-)discovered by the passive scan.
                if not migration_done:
                    self._async_mark_ssot_migrated()

                # Clear stale discovery metadata from .storage so the scan
                # starts fresh and devices are re-discovered as NEW.
                # Without this, the scan imports old devices with
                # ACCEPTED/DISCARDED status and get_devices(status=NEW)
                # returns empty.
                from .discovery import SZ_DISCOVERY

                if storage.get(SZ_DISCOVERY):
                    # Use the raw HA Store to clear the discovery key,
                    # bypassing our store wrapper which preserves discovery
                    # when None is passed.
                    from homeassistant.helpers.storage import Store as _HAStore

                    raw_store = _HAStore(
                        self.hass, STORAGE_VERSION, STORAGE_KEY
                    )
                    raw_data = await raw_store.async_load() or {}
                    if SZ_DISCOVERY in raw_data:
                        raw_data.pop(SZ_DISCOVERY, None)
                        await raw_store.async_save(raw_data)
                        _LOGGER.info(
                            "Cleared stale discovery metadata from .storage "
                            "(schema empty, devices re-discovered as NEW)"
                        )
                    # Also prevent the scan from restoring from the stale
                    # in-memory cache by setting a flag
                    self._skip_discovery_restore = True

        # 2. Schema Handling
        _LOGGER.debug("CONFIG_SCHEMA: %s", config_schema)  # noqa: E501  # marker: after-migration

        # Sanitise main_tcs: must point to a key that exists in the schema
        # and looks like a CTL (01:).  A stale/corrupt main_tcs (e.g. a TRV
        # ID from a bad sync_learned_topology cycle) will crash ramses_rf.
        main_tcs = config_schema.get(SZ_MAIN_TCS)
        if main_tcs and (
            main_tcs not in config_schema
            or not isinstance(config_schema.get(main_tcs), dict)
            or not str(main_tcs).startswith("01:")
        ):
            _LOGGER.warning(
                "Sanitising invalid main_tcs=%r (not a valid CTL ID in "
                "schema), clearing it",
                main_tcs,
            )
            config_schema = dict(config_schema)
            config_schema.pop(SZ_MAIN_TCS, None)
            # Also remove the invalid device ID from the schema if it exists
            config_schema.pop(main_tcs, None)
            self.options[CONF_SCHEMA] = config_schema
            # Persist the sanitised schema to the config entry so the fix
            # survives reloads (self.options is in-memory only).
            new_options = {**self.entry.options, CONF_SCHEMA: config_schema}
            self.hass.config_entries.async_update_entry(
                self.entry, options=new_options
            )

        cached_schema = client_state.get(SZ_SCHEMA, {})
        _LOGGER.debug("CACHED_SCHEMA: %s", cached_schema)

        # Try merging schemas
        if cached_schema and (
            merged_schema := merge_schemas(
                config_schema, cached_schema, schema_is_ssot=schema_is_ssot
            )
        ):
            try:
                self.client = self._create_client(merged_schema)
            except (LookupError, vol.MultipleInvalid) as err:
                _LOGGER.warning(
                    "Failed to initialise with merged schema: %s", err
                )

        # Fallback to config schema
        if not self.client:
            try:
                self.client = self._create_client(config_schema)
            except (ValueError, vol.Invalid) as err:
                _LOGGER.error(
                    "Critical error: Failed to initialise client with "
                    "config schema: %s",
                    err,
                )
                raise ValueError(
                    f"Failed to initialise RAMSES client: {err}"
                ) from err

        # 3. Packet Handling (Refactored)
        cached_packets = self._get_saved_packets(client_state)
        _LOGGER.info("Starting with %s cached packets", len(cached_packets))

        start_kwargs: dict[str, Any] = {"cached_packets": cached_packets}

        await self.client.start(**start_kwargs)
        self.entry.async_on_unload(self._async_stop_client)

        # Cancel non-critical tasks (pending timers) on HA stop to avoid
        # "still running after final writes stage" warnings (issue 802)
        unsub_stop = self.hass.bus.async_listen(
            EVENT_HOMEASSISTANT_STOP, self._async_on_ha_stop
        )
        self.entry.async_on_unload(unsub_stop)

        # Reset _suppress_reload — it may have been set by
        # _async_mark_ssot_migrated above to prevent the update listener
        # from reloading during setup.
        self._suppress_reload = 0.0

    def _async_mark_ssot_migrated(
        self, *, schema: dict[str, Any] | None = None
    ) -> None:
        """Mark the one-time SSOT migration as done in the config entry.

        Sets ``CONF_SSOT_MIGRATED=True`` in ``advanced_features`` so the
        legacy known_list→orphans migration never runs again.  From now
        on, known_list entries that aren't in the schema are treated as
        trait overrides (class, alias, faked, bound, commands) for
        devices that will be (re-)discovered by the passive scan.

        Uses ``_suppress_reload`` to prevent the update listener from
        triggering a reload during setup.
        """
        advanced = dict(self.entry.options.get(CONF_ADVANCED_FEATURES, {}))
        if advanced.get(CONF_SSOT_MIGRATED):
            return  # already marked
        advanced[CONF_SSOT_MIGRATED] = True
        new_options = {**self.entry.options, CONF_ADVANCED_FEATURES: advanced}
        if schema is not None:
            new_options[CONF_SCHEMA] = schema
        # Set _suppress_reload so the update listener (scheduled as an
        # async task by async_update_entry) skips the reload.  The flag
        # is reset at the end of async_setup.
        self._suppress_reload = time.time()
        self.hass.config_entries.async_update_entry(
            self.entry, options=new_options
        )
        _LOGGER.info("SSOT migration marked as done in config entry")

    async def async_start(self) -> None:
        """Start the coordinator and initiate the first refresh.

        Starts discovery loops, saves initial state, and triggers the
        first data update.
        """
        # Note: self.client.start() should have been called in async_setup

        # 1. Trigger the first discovery immediately
        #    Called directly so entities are found BEFORE finishing setup
        _LOGGER.debug("Coordinator: Starting initial discovery...")
        await self._discover_new_entities()

        # 2. Schedule the Discovery Loop
        #    Runs independently of DataUpdateCoordinator's internal timer.
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._async_discovery_task,
                td(seconds=self.entry.options.get(CONF_SCAN_INTERVAL, 60)),
            )
        )

        # 3. Start passive device scan if enabled
        advanced = self.entry.options.get(CONF_ADVANCED_FEATURES, {})
        if advanced.get(CONF_PASSIVE_SCAN, False) and self.client:
            await self._async_start_discovery_scan()

        # Trigger the first update immediately (calls _async_update_data)
        # Raises ConfigEntryNotReady if it fails, which is handled by HA
        await self.async_config_entry_first_refresh()

        # Defer SIGNAL_UPDATE so that the Gateway's async _msg_handler
        # (CQRS ingestion) has completed.  The protocol calls extra
        # handlers synchronously before the Gateway's create_task'd
        # coroutine runs, and the coroutine has internal await points
        # that yield to the event loop before _cqrs_ingestion_engine
        # executes.
        @callback
        def _on_packet(dto: PacketDTO) -> None:
            """Emit SIGNAL_UPDATE after ramses_rf has ingested the packet."""

            async def _signal_after_ingestion() -> None:
                await asyncio.sleep(
                    0
                )  # yield to ramses_rf's create_task'd ingestion
                src_id = dto.addr1
                async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{src_id}")
                if dto.addr2 and dto.addr2 != dto.addr1:
                    async_dispatcher_send(
                        self.hass, f"{SIGNAL_UPDATE}_{dto.addr2}"
                    )

            self.hass.async_create_task(_signal_after_ingestion())

        if self.client:
            self.entry.async_on_unload(self.client.add_msg_handler(_on_packet))

        # Step 5: subscribe to ramses_rf topology events so schema changes
        # (binding, class promotion, circuit creation) are written back to
        # the config entry near-real-time instead of waiting up to
        # SAVE_STATE_INTERVAL for the polling fallback.  The callback is
        # debounced (see _on_rf_schema_updated) and the polling loop below
        # stays as a reduced-frequency safety net.
        if self.client:
            self.client.set_schema_updated_callback(self._on_rf_schema_updated)
            self.entry.async_on_unload(
                self._unregister_schema_updated_callback
            )

        # Keep the dedicated interval for saving client state to disk
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass, self.async_save_client_state, SAVE_STATE_INTERVAL
            )
        )
        # On unload, save state but skip topology sync — the learned topology
        # from the dying coordinator should NOT overwrite a fresh-start schema
        # that the user (or simulator) has just cleared.
        self.entry.async_on_unload(self._async_save_on_unload)

    async def _async_start_discovery_scan(self) -> None:
        """Start the passive device scan engine and discovery manager."""
        from ramses_rf.discovery_scan import DiscoveryScan

        if not self.client:
            _LOGGER.warning(
                "Cannot start discovery scan: client not initialized"
            )
            return

        advanced = self.entry.options.get(CONF_ADVANCED_FEATURES, {})
        scan = DiscoveryScan(self.client)
        self.discovery_manager = DiscoveryManager(
            self.hass,
            scan,
            auto_notify=advanced.get(CONF_AUTO_NOTIFY, True),
            lost_threshold_days=advanced.get(CONF_LOST_THRESHOLD, 7),
            active_hgi_id=self.active_hgi_id,
        )

        # Restore persisted state (unless schema was wiped — start fresh)
        stored = await self.store.async_load()
        from .discovery import SZ_DISCOVERY

        if stored.get(SZ_DISCOVERY) and not self._skip_discovery_restore:
            self.discovery_manager.restore_state(stored[SZ_DISCOVERY])
        elif self._skip_discovery_restore:
            _LOGGER.info(
                "Skipping discovery state restore (schema was wiped, "
                "starting with empty discovery)"
            )
        else:
            _LOGGER.info(
                "No discovery state in storage, starting with empty discovery"
            )

        # Sync discovery metadata with current schema: mark devices as
        # REMOVED if in discovery but not in schema (manually removed).
        # Ensures they will be re-discovered if still present.
        # NOTE: use _extract_schema_device_ids (unstripped) so that HGI
        # (18:) entries are included — _strip_and_orchestrate drops them
        # because ramses_rf doesn't need them, but discovery tracking
        # must know they're in the schema (issue 987).
        # Use self.entry.options (live) — see _async_discovery_checkpoint.
        schema = self.entry.options.get(CONF_SCHEMA, {})
        schema_device_ids = self._extract_schema_device_ids(schema)
        foreign_device_ids = self._extract_foreign_device_ids(schema)
        self.discovery_manager.sync_with_schema(
            schema_device_ids, foreign_device_ids
        )

        # Schedule periodic checkpoint + check for new/lost devices.
        # Use 5 min interval for now — TODO: replace with a real-time
        # callback from ramses_rf's DiscoveryScan (see notepad.txt).
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._async_discovery_checkpoint,
                td(minutes=5),
            )
        )
        # Run an immediate check after 10 seconds so new devices from
        # cached packets are detected quickly.
        unsub = async_call_later(
            self.hass, 10, self._async_discovery_checkpoint
        )
        self.entry.async_on_unload(unsub)
        self.entry.async_on_unload(self._async_stop_discovery_scan)
        _LOGGER.info("Passive device scan started")

    async def _async_discovery_checkpoint(self, _: dt | None = None) -> None:
        """Periodic checkpoint: check for new/lost devices and save state."""
        if not self.discovery_manager:
            return
        # Sync discovery metadata with the scan's device list
        # NOTE: use _extract_schema_device_ids (unstripped) so that HGI
        # (18:) entries are included — _strip_and_orchestrate drops them
        # because ramses_rf doesn't need them, but discovery tracking
        # must know they're in the schema (issue 987).
        # IMPORTANT: use self.entry.options (live) not self.options (stale
        # copy from __init__).  When the user accepts devices or adds
        # _class via the review_discovered form, the config entry is
        # updated but the reload is suppressed (accept flow).  Using the
        # stale copy would cause check_missing_class to re-flag devices
        # whose _class was just written (issue 984).
        schema = self.entry.options.get(CONF_SCHEMA, {})
        schema_device_ids = self._extract_schema_device_ids(schema)
        foreign_device_ids = self._extract_foreign_device_ids(schema)
        self.discovery_manager.sync_with_schema(
            schema_device_ids, foreign_device_ids
        )
        # Check ramses_rf known_list for contradiction-based class changes
        # (e.g. FAN→DIS) before running mismatch checks, so rf-flagged
        # mismatches are included in the notification.
        self._check_rf_contradictions()
        # Check for mismatches between discovery and schema
        # (schema is authoritative — this only logs warnings + notification)
        if isinstance(schema, dict):
            self.discovery_manager.check_all_mismatches(
                schema,
                zones=self._zones,
                devices=self._devices if self.client else None,
            )
        self.discovery_manager.check_for_new_devices()
        self.discovery_manager.check_for_lost_devices(
            schema if isinstance(schema, dict) else None
        )
        await self.async_save_client_state()

    async def _async_stop_discovery_scan(self) -> None:
        """Stop the discovery scan engine.

        Saves discovery state before stopping so it can be restored on reload.

        This callback runs FIRST in the unload chain (LIFO order), before
        _async_save_on_unload.  It must therefore check entry.options
        itself to detect schema changes — it cannot rely on the flags
        that _async_save_on_unload sets later.

        Three cases:
        - Schema is empty (full wipe): skip discovery save entirely so
          stale ACCEPTED metadata doesn't override the config flow's clear.
        - Schema has fewer devices (per-device removal): filter the
          discovery state to only include devices still in the schema,
          so removed devices are re-discovered as NEW after reload.
        - Schema unchanged: save normally.
        """
        if self.discovery_manager:
            # Use live entry.options (not stale self.options) to detect
            # schema changes made by the config flow before the reload.
            schema = self.entry.options.get(CONF_SCHEMA, {})
            # Use the full extraction that walks all device locations
            # (appliance_control, DHW valves, zones, UFH, orphans, etc.)
            # — the simplified top-level-only extraction misses devices
            # nested inside TCS structures, causing their ACCEPTED metadata
            # to be filtered out during save and re-notified after reload
            # (ramses-rf/ramses_cc#917).
            schema_device_ids = RamsesCoordinator._extract_schema_device_ids(
                schema
            )

            if not schema_device_ids:
                # Full wipe — skip discovery save entirely
                _LOGGER.info(
                    "Stopping discovery scan: schema is empty, skipping "
                    "discovery state save (user wiped schema)"
                )
                self._skip_discovery_save = True
            else:
                # Export and cache state before stopping, so
                # async_save_client_state (which runs later in the unload
                # chain) still has it available
                self._cached_discovery_state = (
                    self.discovery_manager.export_state()
                )

                # Per-device removal: filter discovery state so removed
                # devices are re-discovered as NEW.  Set the filter IDs
                # here (before the save) so this first save is also correct,
                # not just the second save from _async_save_on_unload.
                self._discovery_filter_ids = schema_device_ids

                cached_count = len(
                    self._cached_discovery_state.get("devices", {})
                )
                filtered_count = len(
                    {
                        d
                        for d in self._cached_discovery_state.get(
                            "devices", {}
                        )
                        if d in schema_device_ids
                    }
                )
                _LOGGER.info(
                    "Stopping discovery scan: caching %d metadata entries "
                    "for save (%d in schema)",
                    cached_count,
                    filtered_count,
                )

            try:
                await self.async_save_client_state()
            finally:
                self._skip_discovery_save = False
                self._discovery_filter_ids = None
            self.discovery_manager.stop()
            self.discovery_manager = None

    # ── Schema-as-single-source-of-truth ──────────────────────────────

    # Keys that ramses_cc adds to the schema dict but ramses_rf doesn't
    # understand.  They are stripped before passing the schema to the Gateway.
    # _SCHEMA_EXTENSION_KEYS is imported from .schemas (shared definition).

    @staticmethod
    def _validate_schema_for_ramserf(schema: dict[str, Any]) -> None:
        """Validate the schema against ramses_rf's strict validator.

        Strips ramses_cc-only extension keys and validates the result
        against ``SCH_GLOBAL_SCHEMAS`` (which uses ``vol.PREVENT_EXTRA``).
        Logs a warning and raises ``ValueError`` if the schema is invalid,
        so the caller can decide whether to save the (invalid) schema or
        skip the save to avoid corrupting the config entry.

        Also checks ``_class`` values against ramses_rf's ``_CLASS_BY_SLUG``
        and warns if any are not valid DevType slugs (e.g. 'ventilator'
        instead of 'FAN').  Invalid ``_class`` values are not rejected
        (ramses_rf falls back to the default class), but the warning helps
        users fix their configuration.

        This is a safety net — the schema should always be valid after
        ``_strip_schema_extensions``, but bugs in sync_learned_topology
        or add_faked_rem could introduce invalid keys.  Catching them
        here prevents a reload failure on the next restart.
        """
        # Check _class values against valid DevType slugs
        for dev_id, entry in schema.items():
            if not isinstance(entry, dict) or not isinstance(dev_id, str):
                continue
            cls = entry.get(SZ_TR_CLASS)
            if isinstance(cls, str) and cls and cls not in _CLASS_BY_SLUG:
                _LOGGER.warning(
                    "Schema entry for %s has _class='%s' which is not a "
                    "valid DevType slug. Valid slugs: %s. "
                    "ramses_rf will fall back to the default class. "
                    "Please update the schema to use a valid slug.",
                    dev_id,
                    cls,
                    ", ".join(sorted(str(s) for s in _CLASS_BY_SLUG)),
                )

        try:
            stripped = RamsesCoordinator._strip_schema_extensions(schema)
            from ramses_rf.schemas import SCH_GLOBAL_SCHEMAS

            SCH_GLOBAL_SCHEMAS(stripped)
        except vol.Invalid as err:
            _LOGGER.error(
                "Schema validation failed before save: %s. Stripped: %s",
                err,
                RamsesCoordinator._strip_schema_extensions(schema),
            )
            raise ValueError(f"Schema validation failed: {err}") from err

    @staticmethod
    def _extract_schema_device_ids(schema: dict[str, Any]) -> set[str]:
        """Extract ALL device IDs from a schema dict.

        Returns every device ID referenced in the schema, including
        foreign-owned devices (``_owner`` != root ``_owner``).  This is
        used by the discovery manager to check whether a device is
        already in the schema (and thus should NOT be flagged as "new").

        Unlike ``_derive_known_list_from_schema``, this does NOT exclude
        foreign-owned devices — they are in the schema, just not in the
        known_list passed to ramses_rf.  Excluding them here would cause
        them to be re-flagged as "new device discovered" on every scan
        cycle (issue 1003).

        :param schema: The global schema dict (may contain extension keys).
        :return: Set of all device IDs in the schema.
        """
        device_ids: set[str] = set()

        # Main TCS (the CTL)
        if ctl_id := schema.get(SZ_MAIN_TCS):
            device_ids.add(ctl_id)

        for key, value in schema.items():
            # Skip non-device-id keys and our extension keys
            if key in _SCHEMA_EXTENSION_KEYS:
                continue
            if key in (
                SZ_MAIN_TCS,
                SZ_ORPHANS_HEAT,
                SZ_ORPHANS_HVAC,
                "transport_constructor",
            ):
                continue
            if not _DEVICE_ID_RE.match(str(key)):
                continue

            # key is a device_id (CTL or FAN)
            device_ids.add(str(key))

            if not isinstance(value, dict):
                continue

            # Heat TCS structure
            if isinstance(value.get(SZ_SYSTEM), dict):
                if app_id := value[SZ_SYSTEM].get(SZ_APPLIANCE_CONTROL):
                    device_ids.add(app_id)

            if isinstance(value.get(SZ_DHW_SYSTEM), dict):
                dhw = value[SZ_DHW_SYSTEM]
                if sensor_id := dhw.get(SZ_SENSOR):
                    device_ids.add(sensor_id)
                if valve_id := dhw.get(SZ_DHW_VALVE):
                    device_ids.add(valve_id)
                if valve_id := dhw.get(SZ_HTG_VALVE):
                    device_ids.add(valve_id)

            if isinstance(value.get(SZ_UFH_SYSTEM), dict):
                for ufc_id in value[SZ_UFH_SYSTEM]:
                    if _DEVICE_ID_RE.match(str(ufc_id)):
                        device_ids.add(str(ufc_id))

            if isinstance(value.get(SZ_ZONES), dict):
                for zone_data in value[SZ_ZONES].values():
                    if not isinstance(zone_data, dict):
                        continue
                    if sensor_id := zone_data.get(SZ_SENSOR):
                        device_ids.add(sensor_id)
                    for act_id in zone_data.get(SZ_ACTUATORS, []):
                        device_ids.add(act_id)

            for orphan_id in value.get(SZ_ORPHANS, []):
                device_ids.add(orphan_id)

            # HVAC structure
            for remote_id in value.get(SZ_REMOTES, []):
                device_ids.add(remote_id)
            for sensor_id in value.get(SZ_SENSORS, []):
                device_ids.add(sensor_id)

        # Global orphans
        for orphan_id in schema.get(SZ_ORPHANS_HEAT, []):
            device_ids.add(orphan_id)
        for orphan_id in schema.get(SZ_ORPHANS_HVAC, []):
            device_ids.add(orphan_id)

        return device_ids

    @staticmethod
    def _extract_foreign_device_ids(schema: dict[str, Any]) -> set[str]:
        """Extract device IDs with a foreign _owner (neighbour's devices).

        These are excluded from discovery — the scan engine sees all RF
        traffic, but foreign devices should not be offered for review.
        """
        root_owner = schema.get(SZ_OWNER)
        if not root_owner:
            return set()
        foreign: set[str] = set()
        for key, value in schema.items():
            if (
                isinstance(value, dict)
                and _DEVICE_ID_RE.match(str(key))
                and isinstance(value.get(SZ_TR_OWNER), str)
                and value[SZ_TR_OWNER] != root_owner
            ):
                foreign.add(str(key))
        return foreign

    @staticmethod
    def _strip_schema_extensions(schema: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *schema* with ramses_cc-only keys removed.

        Thin wrapper around ``_strip_and_orchestrate()`` (in schemas.py) —
        the shared stage-1 + stage-3 stripping logic used by both the
        coordinator (gateway feeding) and config_flow (validation).

        See ``_strip_and_orchestrate`` for the full documentation of what
        stage 3 orchestration does (orphan routing, disabled/skipped/foreign
        filtering, HGI dropping, ``None`` stripping, TCS orphan safety net).
        """
        return _strip_and_orchestrate(schema)

    @staticmethod
    def _extract_device_ids_from_stripped(
        stripped_schema: dict[str, Any],
    ) -> set[str]:
        """Extract device IDs from stripped schema (post-stripping).

        This is used as a safety net to ensure every device in the schema
        is also in the known_list.  It walks the same locations as
        ``_derive_known_list_from_schema`` but operates on the already-stripped
        schema (where trait-only devices have been moved to orphan lists).
        """
        device_ids: set[str] = set()
        ctl_id = stripped_schema.get(SZ_MAIN_TCS)
        if ctl_id:
            device_ids.add(ctl_id)

        for key, value in stripped_schema.items():
            if key in _SCHEMA_EXTENSION_KEYS:
                continue
            if key in (
                SZ_MAIN_TCS,
                SZ_ORPHANS_HEAT,
                SZ_ORPHANS_HVAC,
                "transport_constructor",
            ):
                continue
            if not _DEVICE_ID_RE.match(str(key)):
                continue
            device_ids.add(str(key))
            if not isinstance(value, dict):
                continue
            if isinstance(value.get(SZ_SYSTEM), dict):
                if app_id := value[SZ_SYSTEM].get(SZ_APPLIANCE_CONTROL):
                    device_ids.add(app_id)
            if isinstance(value.get(SZ_DHW_SYSTEM), dict):
                dhw = value[SZ_DHW_SYSTEM]
                if sensor_id := dhw.get(SZ_SENSOR):
                    device_ids.add(sensor_id)
                if valve_id := dhw.get(SZ_DHW_VALVE):
                    device_ids.add(valve_id)
                if valve_id := dhw.get(SZ_HTG_VALVE):
                    device_ids.add(valve_id)
            if isinstance(value.get(SZ_UFH_SYSTEM), dict):
                for ufc_id in value[SZ_UFH_SYSTEM]:
                    if _DEVICE_ID_RE.match(str(ufc_id)):
                        device_ids.add(str(ufc_id))
            if isinstance(value.get(SZ_ZONES), dict):
                for zone_data in value[SZ_ZONES].values():
                    if not isinstance(zone_data, dict):
                        continue
                    if sensor_id := zone_data.get(SZ_SENSOR):
                        device_ids.add(sensor_id)
                    for act_id in zone_data.get(SZ_ACTUATORS, []):
                        device_ids.add(act_id)
            for orphan_id in value.get(SZ_ORPHANS, []):
                device_ids.add(orphan_id)
            for remote_id in value.get(SZ_REMOTES, []):
                device_ids.add(remote_id)
            for sensor_id in value.get(SZ_SENSORS, []):
                device_ids.add(sensor_id)

        for orphan_id in stripped_schema.get(SZ_ORPHANS_HEAT, []):
            device_ids.add(orphan_id)
        for orphan_id in stripped_schema.get(SZ_ORPHANS_HVAC, []):
            device_ids.add(orphan_id)
        return device_ids

    @staticmethod
    def _derive_known_list_from_schema(
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Derive a known_list from the schema structure.

        Walks the schema (same logic as ``_extract_device_ids_from_schema``
        in services.py) and returns a known_list dict where each device ID
        maps to a traits dict with _ traits extracted from the schema entry.

        Phase 4: the schema is the sole source of truth.  There are no user
        overrides from the config entry — all traits live in the schema as
        _ prefixed keys (_class, _alias, _faked, _bound, _scheme).

        :param schema: The global schema dict (may contain extension keys).
        :return: A known_list dict suitable for ``GatewayConfig.known_list``.
        """
        # Collect all device IDs from the schema structure.
        # Reuse _extract_schema_device_ids (which includes foreign-owned
        # devices) and then filter below.
        device_ids = RamsesCoordinator._extract_schema_device_ids(schema)

        # Build the known_list.
        # _skipped devices are excluded (foreign/neighbour — filter rejects).
        # _disabled devices are INCLUDED so ramses_rf doesn't reject packets
        # with DeviceNotFoundError on incoming messages (prevent log spam).
        # Entity creation for _disabled is suppressed in discovery.
        # _owner: devices whose _owner != root _owner are "foreign"
        # — excluded from known_list and added to block_list in _create_client.
        root_owner = schema.get(SZ_OWNER)
        excluded: set[str] = set()
        disabled: set[str] = set()
        foreign: set[str] = set()
        for key, value in schema.items():
            if isinstance(value, dict) and _DEVICE_ID_RE.match(str(key)):
                if value.get(SZ_TR_SKIPPED) is True:
                    excluded.add(str(key))
                elif value.get(SZ_TR_DISABLED) is True:
                    disabled.add(str(key))
                # Check ownership: if root _owner is set and this device has a
                # different _owner, it's foreign → block_list (not known_list).
                if root_owner and isinstance(value.get(SZ_TR_OWNER), str):
                    if value[SZ_TR_OWNER] != root_owner:
                        foreign.add(str(key))

        known_list: dict[str, Any] = {}
        for device_id in device_ids:
            if device_id in excluded:
                continue
            if device_id in foreign:
                continue  # foreign owner → block_list, not known_list
            # Extract _ traits from device's top-level schema entry.
            # Use ramses_rf's strip_and_map_traits as base (maps _bound→bound,
            # _scheme→scheme, _alias→alias, _faked→faked, _class→class), then
            # apply ramses_cc-specific special cases on top.
            entry = schema.get(device_id)
            traits: dict[str, Any] = {}
            if isinstance(entry, dict):
                mapped = _strip_and_map_traits(entry)
                # strip_and_map_traits maps _class→class, _alias→alias,
                # _faked→faked, _bound→bound, _scheme→scheme.  But it
                # doesn't handle ramses_cc-specific special cases:
                #   - class normalization (ventilator → FAN)
                #   - _name → alias (with setdefault, lower priority)
                #   - bound only if _class is set (SCH_TRAITS_HVAC constraint)
                #   - faked only if True
                # So we rebuild traits from the mapped dict with these cases.
                if mapped.get("class"):
                    traits["class"] = _normalize_class_slug(mapped["class"])
                if mapped.get("alias"):
                    traits["alias"] = mapped["alias"]
                if entry.get(SZ_TR_NAME):
                    # _name maps to alias for ramses_rf (display name)
                    traits.setdefault("alias", entry[SZ_TR_NAME])
                if mapped.get("faked") is True:
                    traits["faked"] = True
                if mapped.get("bound"):
                    # ramses_rf 0.58.2+ accepts 'bound' as str | list[str]
                    # (verified config.py:89-93).  str is for REM/DIS pointing
                    # to their FAN; list is for FAN pointing to its REMs
                    # (multi-REM binding).  Both are passed through.
                    traits["bound"] = mapped["bound"]
                if mapped.get("scheme"):
                    traits["scheme"] = mapped["scheme"]
            known_list[device_id] = traits

        # Normalize class slugs in known_list (ventilator -> FAN, etc.)
        for _dev_id, traits in known_list.items():
            if isinstance(traits, dict) and isinstance(
                traits.get("class"), str
            ):
                traits["class"] = _normalize_class_slug(traits["class"])

        # Sanitize: ramses_rf's SCH_TRAITS_HEAT does not accept 'bound'
        # (only SCH_TRAITS_HVAC has it).  Remove 'bound' from:
        # - heat-prefix devices without an explicit class (default to heat)
        # - any device with a heat class (e.g. DIS, CTL, TRV) — even if the
        #   device ID prefix is non-heat (32: with _class=DIS from R24's
        #   class mismatch test)
        _hvac_slugs = set(str(s) for s in DEV_TYPE_MAP.HVAC_SLUGS)
        _hvac_prefixes = ("32:", "29:", "37:", "63:")
        for _device_id, traits in known_list.items():
            if not isinstance(traits, dict) or "bound" not in traits:
                continue
            cls = traits.get("class")
            if cls:
                # Explicit class: keep bound only for HVAC classes
                if str(cls) in _hvac_slugs:
                    continue  # HVAC class — bound is valid
                # Heat class (DIS, CTL, TRV, etc.) → remove bound
                traits.pop("bound", None)
            else:
                # No explicit class: ramses_rf defaults based on prefix.
                # HVAC prefixes (32:, 29:, 37:, 63:) → SCH_TRAITS_HVAC
                # Heat prefixes (01:, 04:, etc.) → SCH_TRAITS_HEAT
                if _device_id[:3] in _hvac_prefixes:
                    continue  # HVAC prefix — bound is valid
                traits.pop("bound", None)

        return known_list

    def _sync_known_list_traits_to_schema(
        self, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Copy traits from user known_list into schema root entries.

        Phase 4: known_list is no longer stored in the config entry.
        This method is kept for backward compatibility but is a no-op —
        the v2→v3 config entry migration already merged known_list traits
        into the schema.

        :param schema: The enriched schema from sync_learned_topology.
        :return: The schema unchanged (no known_list to sync from).
        """
        return schema

    @staticmethod
    def _sync_traits_to_schema(
        schema: dict[str, Any], user_known_list: dict[str, Any]
    ) -> dict[str, Any]:
        """Copy traits from user known_list into schema root entries.

        Static implementation for testability.

        :param schema: The schema to enrich.
        :param user_known_list: The user known_list with trait overrides.
        :return: The schema with known_list traits merged in.
        """
        if not user_known_list or not isinstance(user_known_list, dict):
            return schema

        # Map known_list keys to schema _ trait keys
        trait_map = {
            "class": SZ_TR_CLASS,
            "faked": SZ_TR_FAKED,
            "bound": SZ_TR_BOUND,
            "scheme": SZ_TR_SCHEME,
            "alias": SZ_TR_ALIAS,
        }

        changed = False
        migrated_count = 0
        new_schema = dict(schema)
        for device_id, kl_entry in user_known_list.items():
            if not isinstance(kl_entry, dict) or not kl_entry:
                continue
            entry = new_schema.get(device_id)
            if not isinstance(entry, dict):
                continue  # no root entry — nothing to sync into
            device_changed = False
            for kl_key, sz_tr in trait_map.items():
                if kl_key in kl_entry and sz_tr not in entry:
                    value = kl_entry[kl_key]
                    # Normalize class to short DevType slug (FAN, REM, CO2)
                    # rather than entity slug (ventilator, switch, co2_sensor)
                    # for consistency with ramses_rf's _CLASS_BY_SLUG.
                    # ramses_rf only accepts DevType slugs in _CLASS_BY_SLUG.
                    if kl_key == "class" and isinstance(value, str):
                        value = _normalize_class_slug(value)
                    entry[sz_tr] = value
                    changed = True
                    device_changed = True
                    _LOGGER.info(
                        "SSOT migration: copied %s=%s from known_list to "
                        "schema for %s",
                        sz_tr,
                        kl_entry[kl_key],
                        device_id,
                    )
            if device_changed:
                migrated_count += 1

        if changed:
            _LOGGER.info(
                "SSOT Phase 2 migration: copied traits from known_list to "
                "schema for %d device(s). known_list entries are redundant "
                "and can be removed once verified.",
                migrated_count,
            )
            from .schemas import order_schema

            return order_schema(new_schema)
        return schema

    @staticmethod
    def _sync_remotes_to_schema(
        schema: dict[str, Any],
        remotes: dict[str, dict[str, Any]],
        known_command_devices: set[str] | None = None,
    ) -> dict[str, Any]:
        """Copy learned commands from ``remotes`` into schema ``_commands``.

        This is the Phase 3a SSOT migration for commands: the ``remotes``
        dict (from ``.storage``) is the legacy command store, the schema
        ``_commands`` trait is the new one.  Once commands are in the
        schema, the ``remotes`` entries become a cache/fallback.

        Only copies commands that are NOT already in the schema — schema
        is authoritative, ``remotes`` fills gaps.

        If ``known_command_devices`` is provided, devices in this set that
        don't have ``_commands`` in their schema entry are skipped — the
        user previously had ``_commands`` and deleted them, so re-adding
        from ``remotes`` would resurrect user-deleted commands.  Devices
        NOT in this set are migrated normally (first-time migration).

        :param schema: The schema to enrich.
        :param remotes: The remotes dict from ``.storage`` or in-memory.
        :param known_command_devices: Device IDs that previously had
            ``_commands`` in the schema.  Prevents resurrection of
            user-deleted ``_commands``.
        :return: The schema with ``_commands`` merged in.
        """
        if not remotes or not isinstance(remotes, dict):
            return schema

        changed = False
        migrated_count = 0
        new_schema = dict(schema)
        for device_id, commands in remotes.items():
            if not commands or not isinstance(commands, dict):
                continue
            entry = new_schema.get(device_id)
            if not isinstance(entry, dict):
                # Create a root entry for this device if it doesn't exist
                entry = {}
                new_schema[device_id] = entry
            if SZ_TR_COMMANDS in entry:
                continue  # already has _commands — schema is authoritative
            # Skip if the user previously had _commands and deleted them
            if (
                known_command_devices is not None
                and device_id in known_command_devices
            ):
                _LOGGER.debug(
                    "SSOT Phase 3a: skipping %s — _commands was previously "
                    "in schema but is now absent (user deletion)",
                    device_id,
                )
                continue
            entry[SZ_TR_COMMANDS] = dict(commands)
            changed = True
            migrated_count += 1
            _LOGGER.info(
                "SSOT Phase 3a: copied %d command(s) from remotes to "
                "schema _commands for %s",
                len(commands),
                device_id,
            )

        if changed:
            _LOGGER.info(
                "SSOT Phase 3a migration: copied commands from remotes to "
                "schema for %d device(s).",
                migrated_count,
            )
            from .schemas import order_schema

            return order_schema(new_schema)
        return schema

    @staticmethod
    def _migrate_rem_commands_to_fan(
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate ``_commands`` from REM entries to FAN entries (Phase 3b).

        For each FAN entry with ``_bound``, copies ``_commands`` from the
        bound REM entries to the FAN entry as ``{verb, code, payload}`` dict
        templates.  REM ``_commands`` (packet strings) are NOT deleted —
        they stay for backward compatibility (downgrade path: v2 code
        ignores FAN ``_commands``, reads REM ``_commands``).

        Only copies commands that are NOT already on the FAN entry —
        FAN ``_commands`` is authoritative.

        :param schema: The schema to migrate.
        :return: The schema with FAN ``_commands`` populated.
        """
        changed = False
        new_schema = dict(schema)

        for fan_id, entry in new_schema.items():
            if not isinstance(entry, dict) or entry.get(SZ_TR_CLASS) != "FAN":
                continue

            # Normalise any existing raw string commands on the FAN to
            # dict templates
            fan_commands = entry.get(SZ_TR_COMMANDS)
            if isinstance(fan_commands, dict):
                from .remote import _is_command_dict, _parse_packet_to_template

                for cmd_name, cmd_val in list(fan_commands.items()):
                    if cmd_name.startswith("_"):
                        continue
                    if isinstance(cmd_val, str) and not _is_command_dict(
                        cmd_val
                    ):
                        try:
                            fan_commands[cmd_name] = _parse_packet_to_template(
                                cmd_val
                            )
                            changed = True
                            _LOGGER.info(
                                "Phase 3b: converted raw command '%s' on "
                                "FAN %s to dict template",
                                cmd_name,
                                fan_id,
                            )
                        except (ValueError, IndexError) as err:
                            _LOGGER.debug(
                                "Phase 3b: kept raw command '%s' on FAN %s: %s",
                                cmd_name,
                                fan_id,
                                err,
                            )

            # Get bound REMs
            bound = entry.get(SZ_TR_BOUND, [])
            if isinstance(bound, str):
                bound_rems = [bound]
            elif isinstance(bound, list):
                bound_rems = bound
            else:
                bound_rems = []

            # Collect commands from bound REMs (skip _comment etc.)
            rem_commands: dict[str, str] = {}
            for rem_id in bound_rems:
                rem_entry = new_schema.get(rem_id)
                if isinstance(rem_entry, dict):
                    rem_cmds = rem_entry.get(SZ_TR_COMMANDS, {})
                    if isinstance(rem_cmds, dict):
                        for cmd_name, cmd_val in rem_cmds.items():
                            if cmd_name.startswith("_"):
                                continue  # skip metadata (_comment, etc.)
                            if cmd_name not in rem_commands:
                                rem_commands[cmd_name] = str(cmd_val)

            if rem_commands:
                if not isinstance(fan_commands, dict):
                    fan_commands = {}

                from .remote import _parse_packet_to_template

                for cmd_name, packet_str in rem_commands.items():
                    if cmd_name in fan_commands:
                        # FAN already has this command — skip (authoritative)
                        continue
                    try:
                        fan_commands[cmd_name] = _parse_packet_to_template(
                            packet_str
                        )
                        changed = True
                        _LOGGER.info(
                            "Phase 3b migration: copied command '%s' from "
                            "REM to FAN %s as dict template",
                            cmd_name,
                            fan_id,
                        )
                    except (ValueError, IndexError) as err:
                        _LOGGER.warning(
                            "Phase 3b migration: failed to parse packet "
                            "'%s' for command '%s' on FAN %s: %s",
                            packet_str,
                            cmd_name,
                            fan_id,
                            err,
                        )

            if changed and isinstance(fan_commands, dict):
                entry[SZ_TR_COMMANDS] = fan_commands

            # Auto-inject _comment hint on FAN if it has commands without one
            if (
                isinstance(fan_commands, dict)
                and fan_commands
                and "_comment" not in fan_commands
            ):
                fan_commands["_comment"] = (
                    "Commands on FAN (Phase 3b) — target entity for "
                    "automations"
                )
                entry[SZ_TR_COMMANDS] = fan_commands
                changed = True

            # Auto-inject _comment hint on bound REMs that have commands
            for rem_id in bound_rems:
                rem_entry = new_schema.get(rem_id)
                if isinstance(rem_entry, dict):
                    rem_cmds = rem_entry.get(SZ_TR_COMMANDS, {})
                    if (
                        isinstance(rem_cmds, dict)
                        and rem_cmds
                        and "_comment" not in rem_cmds
                    ):
                        rem_cmds["_comment"] = (
                            "Commands were moved to FAN — consider removing "
                            "from REM, or keep as downgrade backup"
                        )
                        rem_entry[SZ_TR_COMMANDS] = rem_cmds
                        changed = True

        if changed:
            _LOGGER.info(
                "Phase 3b migration: REM _commands → FAN dict templates"
            )
            from .schemas import order_schema

            return order_schema(new_schema)
        return schema

    async def _async_update_schema_commands(
        self, device_id: str, commands: dict[str, str]
    ) -> None:
        """Write ``_commands`` for a device into the config entry schema.

        Called by ``remote.py`` after ``learn_command`` / ``add_command`` /
        ``delete_command`` so commands are persisted to the schema (SSOT)
        in addition to ``.storage[remotes]`` (cache).

        Uses ``async_update_entry`` with ``_suppress_reload`` to avoid
        triggering a coordinator reload while the remote entity is mid-call.
        """
        # Use self.entry.options (live) — see _async_discovery_checkpoint.
        schema = self.entry.options.get(CONF_SCHEMA, {})
        if not isinstance(schema, dict):
            return
        # Use deepcopy so the new schema's entries are separate objects
        # from the old schema's entries.  Without this, modifying an entry
        # in place also modifies the old schema, and HA's async_update_entry
        # sees no difference (old == new) and skips the save.
        new_schema = deepcopy(schema)
        entry = new_schema.get(device_id)
        if not isinstance(entry, dict):
            entry = {}
            new_schema[device_id] = entry
        if commands:
            cmds = dict(commands)
            # Auto-inject _comment hint if missing
            if "_comment" not in cmds:
                dev_class = entry.get(SZ_TR_CLASS, "")
                if dev_class == "FAN":
                    cmds["_comment"] = (
                        "Commands on FAN (Phase 3b) — target entity for "
                        "automations"
                    )
                elif dev_class == "REM":
                    cmds["_comment"] = (
                        "Commands on REM (Phase 3a) — deprecated, use FAN "
                        "instead"
                    )
            entry[SZ_TR_COMMANDS] = cmds
        elif SZ_TR_COMMANDS in entry:
            del entry[SZ_TR_COMMANDS]
        new_options = dict(self.entry.options)
        new_options[CONF_SCHEMA] = new_schema
        self.options = new_options
        self._suppress_reload = time.time()
        self.hass.config_entries.async_update_entry(
            self.entry, options=new_options
        )
        _LOGGER.debug(
            "Wrote %d command(s) to schema _commands for %s",
            len(commands),
            device_id,
        )

    def _create_client(self, schema: dict[str, Any]) -> Gateway:
        """Create and configure a new RAMSES client instance."""
        raw_config = self.options.get(CONF_RAMSES_RF, {}).copy()

        # Phase 4: enforce_known_list is always-on.  The config option was
        # removed (issue 677 fix held since 0.57.6).  This prevents ramses_rf
        # from auto-creating devices from traffic — the only path to entity
        # creation is through the schema (and accept_discovered_device when
        # passive scan is enabled).
        raw_config[SZ_ENFORCE_KNOWN_LIST] = True

        engine_kwargs: dict[str, Any] = {}
        gateway_kwargs: dict[str, Any] = {}

        engine_fields = {f.name for f in dataclasses.fields(EngineConfig)}
        gateway_fields = {f.name for f in dataclasses.fields(GatewayConfig)}

        for k, v in raw_config.items():
            if k in engine_fields:
                engine_kwargs[k] = v
            elif k in gateway_fields and k != "engine":
                gateway_kwargs[k] = v

        engine_kwargs["app_context"] = self.hass

        # ── Schema as single source of truth ──────────────────────────
        # Phase 4: known_list is derived solely from the schema — no user
        # overrides from config entry (removed in v2→v3 migration).
        derived_known_list = self._derive_known_list_from_schema(schema)
        # Track _disabled device IDs so _discover_new_entities can skip them.
        # _disabled devices are in known_list (to avoid DeviceNotFoundError log
        # spam) but should not get HA entities.
        self._disabled_device_ids = {
            str(k)
            for k, v in schema.items()
            if isinstance(v, dict)
            and _DEVICE_ID_RE.match(str(k))
            and v.get(SZ_TR_DISABLED) is True
        }
        # Foreign devices (_owner doesn't match root _owner) go to block_list
        # instead of known_list.  This prevents DeviceNotFoundError log spam
        # for neighbour's / other-RF-library devices — ramses_rf silently
        # drops their packets at the filter level.
        #
        # Foreign HGI devices (18:) are also blocked.  A foreign HGI from a
        # different owner does not communicate with our controller — it is
        # on the same RF frequency but its traffic is addressed to the
        # neighbour's controller, not ours.  The issue 822 eavesdropping
        # exemption (let unknown HGIs through) only applies to HGIs that
        # are not in any list — a foreign HGI marked as such by the user
        # should be filtered out.  See issue ramses-rf/ramses_cc#1020.
        root_owner = schema.get(SZ_OWNER)
        block_list: dict[str, Any] = {}
        if root_owner:
            for k, v in schema.items():
                if (
                    isinstance(v, dict)
                    and _DEVICE_ID_RE.match(str(k))
                    and isinstance(v.get(SZ_TR_OWNER), str)
                    and v[SZ_TR_OWNER] != root_owner
                ):
                    block_list[str(k)] = {}
        # Also add _skipped devices to block_list (deferred decision — still
        # foreign until the user accepts them).
        for k, v in schema.items():
            if (
                isinstance(v, dict)
                and _DEVICE_ID_RE.match(str(k))
                and v.get(SZ_TR_SKIPPED) is True
                and str(k) not in block_list
            ):
                block_list[str(k)] = {}
        if block_list:
            gateway_kwargs["block_list"] = block_list
            _LOGGER.debug(
                "block_list: %s (root_owner=%s)",
                list(block_list.keys()),
                root_owner,
            )
        # Strip commands from traits (ramses_rf doesn't accept them)
        sanitized_known_list = {
            device_id: (
                {k: v for k, v in traits.items() if k != CONF_COMMANDS}
                if isinstance(traits, dict)
                else traits
            )
            for device_id, traits in derived_known_list.items()
        }
        # Device traits (class/alias/faked/bound/scheme) are consumed by
        # ramses_rf DeviceRegistry via GatewayConfig.known_list.
        gateway_kwargs["known_list"] = sanitized_known_list

        packet_log = self.options.get(SZ_PACKET_LOG, {})
        engine_kwargs["packet_log"] = packet_log

        # Strip ramses_cc-only extension keys before passing to ramses_rf
        stripped = self._strip_schema_extensions(schema)

        # Clean up invalid zone sensor values — ramses_rf's SCH_TCS_ZONES_ZON
        # validates sensor against DEVICE_ID_REGEX.SEN (01|03|04|12|22|34).
        # Devices like 18: (HGI) are not valid zone sensors and would cause
        # setup to fail with "not a valid value for dictionary value".
        _VALID_SENSOR_RE = re.compile(r"^(01|03|04|12|22|34):[0-9A-Fa-f]{6}$")
        for _tcs_id, tcs_entry in stripped.items():
            if not isinstance(tcs_entry, dict):
                continue
            zones = tcs_entry.get("zones")
            if not isinstance(zones, dict):
                continue
            for zone in zones.values():
                if not isinstance(zone, dict):
                    continue
                sensor = zone.get("sensor")
                if sensor and not _VALID_SENSOR_RE.match(sensor):
                    _LOGGER.warning(
                        "Removing invalid zone sensor %s (not a valid SEN "
                        "prefix)",
                        sensor,
                    )
                    del zone["sensor"]

        _LOGGER.debug("Schema passed to ramses_rf: %s", stripped)
        _LOGGER.debug(
            "Known_list passed to ramses_rf: %s", sanitized_known_list
        )

        # Safety net: ensure every device_id in the stripped schema is also
        # in the known_list.  ramses_rf's check_filter_lists raises
        # DeviceNotFoundError if a device is in the schema but not in the
        # known_list.  This can happen when sync_learned_topology enriches
        # the config schema with devices that _derive_known_list_from_schema
        # missed (e.g. HVAC devices added as empty dicts that get moved to
        # orphans by _strip_schema_extensions).
        schema_device_ids = self._extract_device_ids_from_stripped(stripped)
        missing = schema_device_ids - set(sanitized_known_list.keys())
        if missing:
            _LOGGER.warning(
                "Schema has %d device(s) not in known_list, adding: %s",
                len(missing),
                sorted(missing),
            )
            for dev_id in sorted(missing):
                sanitized_known_list.setdefault(dev_id, {})

        gateway_kwargs["schema"] = stripped

        gateway_timeout = self.options.get(CONF_GATEWAY_TIMEOUT)
        if gateway_timeout is not None:
            gateway_kwargs["gateway_timeout"] = gateway_timeout

        # Detect the transport type from port_name / flags.
        _serial_port_opts = self.options.get(SZ_SERIAL_PORT, {})
        _port_name_raw = _serial_port_opts.get(SZ_PORT_NAME, "")
        _is_zigbee = isinstance(
            _port_name_raw, str
        ) and _port_name_raw.startswith("zigbee://")
        _is_mqtt_ha_port = (
            isinstance(_port_name_raw, str) and _port_name_raw == "mqtt_ha"
        )
        _is_mqtt_flag = bool(self.options.get(CONF_MQTT_USE_HA))

        if not _port_name_raw:
            mqtt_entries = self.hass.config_entries.async_entries("mqtt")
            if mqtt_entries:
                _LOGGER.warning(
                    "No serial_port configured; defaulting to Home Assistant "
                    "MQTT transport. Please re-open options & re-save."
                )
                _serial_port_opts[SZ_PORT_NAME] = "mqtt_ha"
                _port_name_raw = "mqtt_ha"
                _is_mqtt_ha_port = True
                _is_mqtt_flag = True
            else:
                raise ConfigEntryNotReady(
                    "No serial port configured. Open the Ramses RF options "
                    "flow to select a transport."
                )

        _is_mqtt_ha = _is_mqtt_flag or _is_mqtt_ha_port

        # Inject HGI into known_list for MQTT transports — the HGI is the
        # active gateway and must be in the known_list to avoid ramses_tx
        # warnings ("SHOULD be in the (enforced) known_list").  For serial/
        # USB transports, ramses_rf detects the HGI from traffic and the
        # warning is benign (the HGI is added to the include list after
        # detection).  For MQTT transports, the HGI ID is known upfront
        # (from CONF_MQTT_HGI_ID or embedded in the mqtt:// URL).
        hgi_id: str | None = None
        if _is_mqtt_ha:
            hgi_id = self.options.get(CONF_MQTT_HGI_ID, DEFAULT_HGI_ID)
        elif isinstance(_port_name_raw, str) and _port_name_raw.startswith(
            "mqtt://"
        ):
            # Custom mqtt:// URL — extract HGI ID from the URL path
            # (e.g. mqtt://user:pass@host:1883/topic/18:001234)
            if self.options.get(CONF_MQTT_HGI_ID):
                hgi_id = self.options.get(CONF_MQTT_HGI_ID)
            else:
                import re as _re

                m = _re.search(r"(18:[0-9]{6})(?:/|$)", _port_name_raw)
                if m:
                    hgi_id = m.group(1)
        if hgi_id:
            device_entry = sanitized_known_list.setdefault(hgi_id, {})
            device_entry["class"] = "HGI"
            device_entry.setdefault("alias", "ramses_esp")

        if _is_zigbee:
            # ZigbeeTransport — handled natively by transport_factory.
            # No MQTT broker is required; no RamsesMqttBridge is created.
            # hass reaches ZigbeeTransport via app_context (PR #505).
            engine_config = EngineConfig(**engine_kwargs)
            gwy_config = GatewayConfig(engine=engine_config, **gateway_kwargs)

            return Gateway(
                port_name=_port_name_raw,
                config=gwy_config,
                loop=self.hass.loop,
            )

        if _is_mqtt_ha:
            # RamsesMqttBridge path — uses HA MQTT
            if not self.hass.config_entries.async_entries("mqtt"):
                raise ConfigEntryNotReady(
                    "Home Assistant MQTT integration is not set up"
                )

            # Retrieve config options
            mqtt_topic = self.options.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)
            # hgi_id was already determined above (with mqtt:// URL parsing).
            # In the _is_mqtt_ha branch it's set from CONF_MQTT_HGI_ID.
            assert hgi_id is not None

            self.mqtt_bridge = RamsesMqttBridge(self.hass, mqtt_topic, hgi_id)

            # Ensure the bridge unsubscribes from MQTT on shutdown
            self.entry.async_on_unload(self.mqtt_bridge.close)

            # Pass the configured HGI ID to ramses_rf.
            engine_kwargs["hgi_id"] = hgi_id

            engine_config = EngineConfig(**engine_kwargs)
            gwy_config = GatewayConfig(engine=engine_config, **gateway_kwargs)

            return Gateway(
                port_name=_port_name_raw or "mqtt",
                config=gwy_config,
                loop=self.hass.loop,
                transport_constructor=self.mqtt_bridge.async_transport_factory,
            )

        # Standard Serial/USB setup
        port_name, port_config = extract_serial_port(
            self.options[SZ_SERIAL_PORT]
        )
        engine_kwargs["port_config"] = port_config

        engine_config = EngineConfig(**engine_kwargs)
        gwy_config = GatewayConfig(engine=engine_config, **gateway_kwargs)

        return Gateway(
            port_name=port_name,
            config=gwy_config,
            loop=self.hass.loop,
        )

    async def _async_stop_client(self) -> None:
        """Safely stop RAMSES client, catching transport exceptions."""
        if not self.client:
            return

        _LOGGER.debug("Coordinator: Initiating safe shutdown of RAMSES client")
        try:
            # This triggers ramses_tx teardown and logger buffer flushes
            await self.client.stop()
        except serial.SerialException as err:
            _LOGGER.debug(
                "Serial port disconnected or busy during teardown: %s",
                err,
            )
        except (
            exc.TransportError,
            TimeoutError,
        ) as err:
            _LOGGER.debug(
                "Transport timeout/error during RAMSES client shutdown: %s",
                err,
            )
        except Exception as err:
            _LOGGER.warning(
                "Unexpected error while stopping RAMSES client: %s", err
            )

    async def _async_on_ha_stop(self, _event: Event) -> None:
        """Cancel non-critical tasks when HA stops (issue 802).

        This runs on EVENT_HOMEASSISTANT_STOP, before the 'final writes'
        stage, to cancel lingering _pending_timer tasks that would
        otherwise delay shutdown.

        State saving on shutdown is handled by _async_save_on_unload,
        which runs via entry.async_on_unload when HA calls
        async_unload_entry during shutdown.
        """
        _LOGGER.debug("Coordinator: HA stop event, cancelling pending tasks")
        await self.service_handler.async_cleanup()

    def _unregister_schema_updated_callback(self) -> None:
        """Unregister the ramses_rf schema_updated callback (Step 5).

        Called on config entry unload.  Clears the callback so ramses_rf
        doesn't invoke it after the coordinator is gone.  Safe to call
        even if ``self.client`` is already None (e.g. setup failed
        before the gateway was created).
        """
        if self.client:
            self.client.set_schema_updated_callback(None)

    def _on_rf_schema_updated(self, schema: dict[str, Any]) -> None:
        """Handle updated topology or schema callback from ramses_rf (Step 5).

        Registered with ``Gateway.set_schema_updated_callback()`` in
        ``async_start``.  ramses_rf fires this on every successful
        topology mutation (BIND_DEVICE, UPDATE_DEVICE_CLASS,
        UPDATE_TRAITS, CREATE_CONTROLLER, CREATE_CIRCUIT) after
        ``DeviceRegistry.handle_topology_event`` has applied it.

        Debounced: coalesces bursts of topology events (e.g. a discovery
        scan processing many 1FC9 packets, or a multi-zone 000C sequence)
        into a single ``async_save_client_state`` call.  The schema dict
        passed in is intentionally discarded in favour of re-fetching via
        ``self.client.get_state()`` inside the save cycle — keeps a
        single code path and avoids drift between the event schema and
        the state-save schema.

        :param schema: The latest system schema dict from ramses_rf
            (unused here — see note above).
        :type schema: dict[str, Any]
        """
        if self._skip_topology_sync:
            # Coordinator is unloading/reloading — ignore stale events
            # that were in flight before unload set the guard.
            return
        if self._schema_updated_debounce_task is not None:
            self._schema_updated_debounce_task.cancel()
        # Use async_create_background_task (not async_create_task) so the
        # 2-second debounce sleep does not block hass.async_block_till_done()
        # — otherwise every test fixture that casts packets and calls
        # async_block_till_done() pays a ~2s penalty (issue 930).
        self._schema_updated_debounce_task = (
            self.hass.async_create_background_task(
                self._debounced_topology_sync(),
                "ramses_cc:debounced_topology_sync",
            )
        )

    async def _debounced_topology_sync(self) -> None:
        """Trailing-debounce worker for ``_on_rf_schema_updated``.

        Waits ``_SCHEMA_UPDATED_DEBOUNCE`` seconds, then runs a single
        ``async_save_client_state`` cycle.  If a new topology event
        arrives during the wait, the caller cancels this task and
        starts a fresh one — so only the last event in a burst actually
        triggers a save.
        """
        try:
            await asyncio.sleep(_SCHEMA_UPDATED_DEBOUNCE.total_seconds())
        except asyncio.CancelledError:
            # Superseded by a newer event (or cancelled on unload).
            # The caller that cancelled us owns the handle and will
            # either reschedule or leave it cleared.
            return
        # We survived the debounce window without being cancelled —
        # clear the handle and run the save.  A new event arriving
        # after this point will schedule a fresh debounce.
        self._schema_updated_debounce_task = None
        if self._skip_topology_sync:
            return
        # Check for contradiction-based reclassifications in ramses_rf's
        # known_list (e.g. a FAN that is actually a DIS).  ramses_rf
        # updates the known_list class in-memory; we surface it to the
        # discovery manager so the user gets a persistent notification.
        self._check_rf_contradictions()
        await self.async_save_client_state()

    def _check_rf_contradictions(self) -> None:
        """Check ramses_rf known_list for contradiction-based class changes.

        ramses_rf's HvacTopologyHandler detects when a device's message
        patterns contradict its known_list class (e.g. a FAN that only
        sends RQ 31DA / I 22F1 is actually a DIS).  It updates the
        known_list class in-memory and emits a topology event.

        This method compares the ramses_rf known_list classes with the
        config entry schema's _class values.  When a mismatch is found,
        it flags ``class_mismatch`` on the discovery manager's device
        metadata so the existing persistent notification system fires.

        This is needed because:
        1. The scan engine does not re-classify known devices
           (``_is_known`` short-circuits re-classification), so the
           scan engine's ``likely_type`` stays frozen at the initial
           (possibly wrong) value.
        2. ramses_rf's HvacTopologyHandler has proper contradiction
           detection with thresholds (3+ non-FAN packets) and _locked
           trait support — much more reliable than the scan engine's
           per-packet _classify.
        """
        if not self.client or not self.discovery_manager:
            return

        rf_known = self.client.config.known_list
        if not isinstance(rf_known, dict):
            return
        # Use self.entry.options (live) — see _async_discovery_checkpoint.
        config_schema = self.entry.options.get(CONF_SCHEMA, {})
        if not isinstance(config_schema, dict):
            return
        for dev_id, traits in rf_known.items():
            if not isinstance(traits, dict):
                continue
            rf_class = traits.get("class")
            if not isinstance(rf_class, str) or not rf_class:
                continue
            schema_entry = config_schema.get(dev_id)
            if not isinstance(schema_entry, dict):
                continue
            schema_class = schema_entry.get(SZ_TR_CLASS)
            if not isinstance(schema_class, str) or not schema_class:
                continue
            rf_class_norm = _normalize_class_slug(rf_class)
            schema_class_norm = _normalize_class_slug(schema_class)
            if rf_class_norm.upper() != schema_class_norm.upper():
                # ramses_rf suggests a different class than the schema
                self.discovery_manager.flag_class_mismatch(
                    dev_id,
                    f"schema={schema_class_norm}, rf_suggests={rf_class_norm}",
                )

    async def _async_save_on_unload(self) -> None:
        """Save client state during unload, skipping topology sync.

        During unload (e.g. reload, fresh start), the learned topology from
        the dying coordinator must NOT be written back to the config entry —
        that would overwrite a freshly-cleared schema and defeat the fresh
        start.

        For discovery state: if the schema is empty (full wipe), skip saving
        entirely.  If devices were removed from the schema (per-device
        removal), filter out the removed devices from the discovery state
        so they're re-discovered as NEW after reload.

        IMPORTANT: use self.entry.options (the live config entry options)
        instead of self.options (a stale copy from __init__).  When a config
        flow saves new options and triggers a reload, self.options still
        reflects the OLD options.  Using the stale copy would cause the
        unload to skip saving discovery state even though the schema was
        just updated with accepted devices — the ACCEPTED metadata would
        be lost and devices would re-appear as NEW after reload.
        """
        self._skip_topology_sync = True

        # Step 5: cancel any in-flight debounced topology sync so it
        # doesn't race with this unload save.  The _skip_topology_sync
        # guard above would also suppress it, but cancelling is cleaner
        # and avoids a stray async_save_client_state after unload.
        if self._schema_updated_debounce_task is not None:
            self._schema_updated_debounce_task.cancel()
            self._schema_updated_debounce_task = None

        # Compute the set of device IDs still in the schema.
        # Use entry.options (live) not self.options (stale copy).
        # Use the full extraction that walks all device locations
        # (appliance_control, DHW valves, zones, UFH, orphans, etc.)
        # — the simplified top-level-only extraction misses devices
        # nested inside TCS structures, causing their ACCEPTED metadata
        # to be filtered out during save and re-notified after reload
        # (ramses-rf/ramses_cc#917).
        schema = self.entry.options.get(CONF_SCHEMA, {})
        schema_device_ids = RamsesCoordinator._extract_schema_device_ids(
            schema
        )

        if not schema_device_ids:
            # Schema is empty (full wipe) — don't save discovery state at all
            self._skip_discovery_save = True
        else:
            # Per-device removal — filter discovery state during save
            self._discovery_filter_ids = schema_device_ids

        try:
            await self.async_save_client_state()
        finally:
            self._skip_topology_sync = False
            self._skip_discovery_save = False
            self._discovery_filter_ids = None

    async def async_save_client_state(self, _: dt | None = None) -> None:
        """Save the current state of the RAMSES client to persistent storage.

        :param _: Optional datetime argument from async_track_time_interval.
        """
        if not self.client:
            _LOGGER.debug("Cannot save state: Client not initialized")
            return

        # Support both async (new) and sync (old) client.get_state()
        result: Any = self.client.get_state()

        if inspect.isawaitable(result):
            schema, packets = await result
        else:
            schema, packets = result

        _LOGGER.info("Saving the client state cache (packets, schema)")

        # Sync learned topology from ramses_rf back to the config entry.
        # The learned schema (from gateway.schema()) may have richer topology
        # (zones, bindings) than the config entry schema. If so, write back.
        # Skip during unload (fresh start / reload) so we don't overwrite a
        # freshly-cleared schema with stale learned topology.
        if not self._skip_topology_sync:
            # Use self.entry.options (live) not self.options (stale copy
            # from __init__).  When the user adds _class via the
            # review_discovered form, the config entry is updated but the
            # reload is suppressed.  Using the stale copy here would cause
            # sync_learned_topology to overwrite the user's _class changes
            # with the stale schema (missing _class), re-triggering the
            # missing_class loop on every checkpoint (issue 984).
            config_schema = self.entry.options.get(CONF_SCHEMA, {})
            # Refresh device_comments with latest scan engine zone bindings.
            # Scan engine may have learned zone_index from broadcast traffic
            # (where dst is --:------) not captured when first accepted.
            # Ensures sync_learned_topology has up-to-date zone info.
            comments_refreshed = False
            if self.discovery_manager and isinstance(config_schema, dict):
                existing_comments = config_schema.get(SZ_DEVICE_COMMENTS, {})
                if isinstance(existing_comments, dict):
                    refreshed = self.discovery_manager.refresh_device_comments(
                        existing_comments, config_schema
                    )
                    if refreshed is not existing_comments:
                        config_schema = dict(config_schema)
                        config_schema[SZ_DEVICE_COMMENTS] = refreshed
                        comments_refreshed = True
            _LOGGER.debug(
                "sync_learned_topology: config_schema=%s", config_schema
            )
            _LOGGER.debug("sync_learned_topology: learned_schema=%s", schema)
            # Build scan_codes map for DHW valve inference (13: devices
            # that send 1100 are boiler relays, not zone actuators)
            scan_codes: dict[str, list[str]] = {}
            scan_domain_ids: dict[str, tuple[str | None, bool]] = {}
            if self.discovery_manager:
                sc = self.discovery_manager.get_scan_codes()
                if isinstance(sc, dict):
                    scan_codes = sc
                sdi = self.discovery_manager.get_scan_domain_ids()
                if isinstance(sdi, dict):
                    scan_domain_ids = sdi
            _LOGGER.debug("sync_learned_topology: scan_codes=%s", scan_codes)
            _LOGGER.debug(
                "sync_learned_topology: scan_domain_ids=%s", scan_domain_ids
            )
            _LOGGER.info(
                "sync_learned_topology: removed_devices=%s",
                self._removed_devices,
            )
            enriched = sync_learned_topology(
                config_schema,
                schema,
                scan_codes=scan_codes,
                scan_domain_ids=scan_domain_ids,
                removed_devices=self._removed_devices,
                active_hgi_id=self.active_hgi_id,
            )
            _LOGGER.debug("sync_learned_topology: enriched=%s", enriched)
            if enriched is not None:
                # Backup before SSOT Phase 2 trait migration (known_list)
                # Only needed if user still has a known_list with traits
                # Phase 4: traits already in schema via v2→v3 migration.
                # Sync learned commands from .storage[remotes] into schema
                # _commands (Phase 3a SSOT migration for commands).
                # Backup first if we have remotes not yet migrated.
                if self._remotes:
                    has_unmigrated = any(
                        SZ_TR_COMMANDS
                        not in (e if isinstance(e, dict) else {})
                        for dev_id, e in enriched.items()
                        if dev_id in self._remotes
                        and self._remotes.get(dev_id)
                    )
                    if has_unmigrated:
                        await self.store.async_save_backup(
                            enriched,
                            {},  # known_list removed in Phase 4
                            reason="ssot_phase3a",
                        )
                    enriched = self._sync_remotes_to_schema(
                        enriched, self._remotes, self._devices_with_commands
                    )
                # Phase 3b: migrate REM _commands (packet strings) to FAN
                # _commands (dict templates).  REM entries are kept for
                # backward compatibility (downgrade path).
                enriched = self._migrate_rem_commands_to_fan(enriched)
                # Validate the stripped schema against ramses_rf's strict
                # validator before saving.  This catches root-level _ traits
                # or invalid device IDs early, instead of failing silently
                # when ramses_rf rejects the schema on the next reload.
                #
                # If validation fails, log the error and skip the config
                # entry update — saving an invalid schema would cause a
                # reload failure on the next restart.  The current config
                # entry schema stays as-is (stale but valid) rather than
                # being overwritten with an invalid one.  The rest of the
                # save cycle (packets, remotes, discovery state) continues.
                validation_ok = True
                try:
                    self._validate_schema_for_ramserf(enriched)
                except ValueError as err:
                    _LOGGER.error(
                        "Schema validation failed during save cycle — "
                        "skipping topology sync to avoid corrupting the "
                        "config entry: %s",
                        err,
                    )
                    validation_ok = False
                if validation_ok:
                    _LOGGER.info(
                        "Learned topology is richer than config, syncing back"
                    )
                    new_options = dict(self.entry.options)
                    new_options[CONF_SCHEMA] = enriched
                    self.options = new_options
                    # Suppress the reload that async_update_entry would
                    # trigger, since the running coordinator already has the
                    # updated options and a reload would tear down the
                    # transport while pending _send_cmd tasks are still in
                    # flight (causing lingering tasks).
                    #
                    # NOTE: async_update_entry schedules the update listener
                    # as an async task.  Setting _suppress_reload to a
                    # timestamp and checking it with a 5-second window in
                    # the update listener avoids the race condition where
                    # the flag is reset before the listener runs.
                    self._suppress_reload = time.time()
                    self.hass.config_entries.async_update_entry(
                        self.entry, options=new_options
                    )
            elif comments_refreshed:
                # No topology changes (enriched is None), but the scan engine
                # captured new zone bindings in device_comments.  Persist the
                # updated comments so they survive to the next sync cycle —
                # otherwise sync_learned_topology step 0b never sees the zone
                # info and can't create zones from broadcast traffic.
                _LOGGER.info(
                    "No topology changes, but device_comments refreshed "
                    "from scan engine — persisting updated comments"
                )
                # Also sync remotes to schema _commands (Phase 3a migration).
                # This runs even when topology isn't richer, so commands from
                # .storage[remotes] or known_list[commands] are migrated to
                # schema _commands on every save cycle.
                config_schema = self._sync_remotes_to_schema(
                    config_schema, self._remotes, self._devices_with_commands
                )
                # Phase 3b: also migrate REM → FAN dict templates
                config_schema = self._migrate_rem_commands_to_fan(
                    config_schema
                )
                new_options = dict(self.entry.options)
                new_options[CONF_SCHEMA] = config_schema
                self.options = new_options
                self._suppress_reload = time.time()
                self.hass.config_entries.async_update_entry(
                    self.entry, options=new_options
                )
            else:
                # No topology changes and no comments refreshed, but we
                # still need to sync remotes to schema _commands (Phase 3a
                # migration).  This ensures commands from .storage[remotes]
                # or known_list[commands] are migrated even when the learned
                # topology matches the config.
                if self._remotes:
                    migrated_schema = self._sync_remotes_to_schema(
                        config_schema,
                        self._remotes,
                        self._devices_with_commands,
                    )
                    # Phase 3b: also migrate REM → FAN dict templates
                    migrated_schema = self._migrate_rem_commands_to_fan(
                        migrated_schema
                    )
                    if migrated_schema is not config_schema:
                        _LOGGER.info(
                            "No topology changes, but remotes synced to "
                            "schema _commands — persisting"
                        )
                        new_options = dict(self.entry.options)
                        new_options[CONF_SCHEMA] = migrated_schema
                        self.options = new_options
                        self._suppress_reload = time.time()
                        try:
                            self.hass.config_entries.async_update_entry(
                                self.entry, options=new_options
                            )
                        except Exception as err:
                            _LOGGER.debug(
                                "Failed to persist remotes sync to schema: %s",
                                err,
                            )
        else:
            # During unload: save the config schema (not the learned schema)
            # to .storage, so the cached schema doesn't override a freshly-
            # cleared config schema on the next restart.  The learned schema
            # from the dying coordinator is stale topology that the user may
            # have just cleared — it must not survive in the cache.
            # Use self.entry.options (live) — see _async_discovery_checkpoint.
            schema = self.entry.options.get(CONF_SCHEMA, {})

        # Update _devices_with_commands to reflect the current schema state.
        # This tracks which devices have _commands so that on the next save
        # cycle, _sync_remotes_to_schema can skip devices that previously
        # had _commands but no longer do (user deletion → no resurrection).
        current_schema = self.entry.options.get(CONF_SCHEMA, {})
        if isinstance(current_schema, dict):
            self._devices_with_commands = {
                dev_id
                for dev_id, entry in current_schema.items()
                if isinstance(entry, dict) and SZ_TR_COMMANDS in entry
            }

        # Explicit intermediate dict to solve 'Never is not iterable'
        # Use _commands_for_save (includes _comment metadata) instead of
        # _commands (which has metadata stripped by _split_commands)
        remotes_from_entities: dict[str, Any] = {
            k: getattr(v, "_commands_for_save", getattr(v, "_commands", {}))
            for k, v in self._entities.items()
            if hasattr(v, "_commands")
        }
        remotes = self._remotes | remotes_from_entities

        discovery_state = None
        if not self._skip_discovery_save:
            discovery_state = (
                self.discovery_manager.export_state()
                if self.discovery_manager
                else getattr(self, "_cached_discovery_state", None)
            )
            # If a filter is set (per-device removal during unload), remove
            # devices not in the schema from the discovery state so they
            # are re-discovered as NEW after reload.
            if discovery_state and self._discovery_filter_ids is not None:
                import json as _json

                devices = discovery_state.get("devices", {})
                filtered_devices = {
                    dev_id: meta
                    for dev_id, meta in devices.items()
                    if dev_id in self._discovery_filter_ids
                }
                discovery_state["devices"] = filtered_devices

                # Filter scan_state so scan re-discovers removed devices
                scan_state = discovery_state.get("scan_state", "")
                if scan_state:
                    try:
                        scan_data = _json.loads(scan_state)
                        scan_data["devices"] = [
                            d
                            for d in scan_data.get("devices", [])
                            if d.get("device_id") in self._discovery_filter_ids
                        ]
                        discovery_state["scan_state"] = _json.dumps(scan_data)
                    except (ValueError, KeyError):
                        pass  # corrupt scan_state, leave as-is

        _LOGGER.info(
            "Saving state: discovery_manager=%s, cached=%s, devices=%d",
            bool(self.discovery_manager),
            bool(getattr(self, "_cached_discovery_state", None)),
            len(discovery_state.get("devices", {})) if discovery_state else 0,
        )

        await self.store.async_save(schema, packets, remotes, discovery_state)

    def _get_device(self, device_id: str) -> Any | None:
        """Get a device by ID."""
        if dev := next((d for d in self._devices if d.id == device_id), None):
            return dev
        if self.client and hasattr(self.client, "device_registry"):
            return self.client.device_registry.device_by_id.get(device_id)
        return None

    def async_register_platform(
        self,
        platform: EntityPlatform,
        add_new_devices: Callable[[RamsesRFEntity], None],
    ) -> None:
        """Register a platform that has entities with the coordinator.

        :param platform: The HA platform instance (e.g. climate, sensor).
        :param add_new_devices: Callback to add new devices to HA.
        """
        platform_str = str(getattr(platform, "domain", platform))
        _LOGGER.debug("Registering platform %s", platform_str)

        if platform_str not in self.platforms:
            self.platforms[platform_str] = []
        self.platforms[platform_str].append(platform)

        _LOGGER.debug(
            "Connecting signal for platform %s: %s",
            platform_str,
            SIGNAL_NEW_DEVICES.format(platform_str),
        )

        self.entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NEW_DEVICES.format(platform_str),
                add_new_devices,
            )
        )

    async def _async_setup_platform(self, platform: str) -> bool:
        """Set up a platform and return True if successful."""
        if platform not in self._platform_setup_tasks:
            self._platform_setup_tasks[platform] = self.hass.async_create_task(
                self.hass.config_entries.async_forward_entry_setups(
                    self.entry, [platform]
                )
            )
        try:
            await self._platform_setup_tasks[platform]
            _LOGGER.debug("Platform setup completed for %s", platform)
            return True
        except Exception as err:
            _LOGGER.error(
                "Error setting up %s platform: %s",
                platform,
                str(err),
                exc_info=True,
            )
            return False

    async def async_unload_platforms(self) -> bool:
        """Unload all platforms associated with this integration.

        :return: True if all platforms unloaded successfully.
        """
        # Cancel pending service handler tasks and scheduled callbacks
        await self.service_handler.async_cleanup()

        tasks: list[Coroutine[Any, Any, bool]] = [
            self.hass.config_entries.async_forward_entry_unload(
                self.entry, platform
            )
            for platform, task in self._platform_setup_tasks.items()
            if not task.cancel()
        ]
        result = all(await asyncio.gather(*tasks))
        _LOGGER.debug("Platform unload completed with result: %s", result)
        return result

    async def _async_update_device(self, device: RamsesRFEntity) -> None:
        """Update device information in the device registry.

        :param device: The RamsesRF entity to update.
        :type device: RamsesRFEntity
        :return: None
        :rtype: None
        """
        # Safely resolve device name, handling properties, methods, coroutines
        device_name: str | None = None
        name_attr = getattr(device, "name", None)

        if name_attr:
            raw_name: Any = name_attr
            if callable(raw_name):
                with suppress(TypeError):
                    raw_name = raw_name()

            if inspect.isawaitable(raw_name):
                raw_name = await raw_name

            device_name = str(raw_name) if raw_name else None

        # Fallback names if the device doesn't supply a valid one
        if not device_name:
            if isinstance(device, System):
                device_name = f"Controller {device.id}"
            elif getattr(device, "_SLUG", None):
                device_name = f"{getattr(device, '_SLUG', None)} {device.id}"
            else:
                device_name = str(device.id)

        info: dict[str, Any] | None = None
        state_store = getattr(device, "state_store", None)
        if state_store:
            info = await state_store._msg_value_code(Code._10E0)

        model: str | None = (
            info.get("description") if info else getattr(device, "_SLUG", None)
        )

        device_registry = dr.async_get(self.hass)

        via_device: tuple[str, str] | None = None
        if isinstance(device, Zone) and device.tcs:
            _LOGGER.info("ZONE %s via_device SET to %s", model, device.tcs.id)
            via_device = (DOMAIN, str(device.tcs.id))
        elif isinstance(device, Child) and getattr(device, "_parent", None):
            parent = getattr(device, "_parent", None)
            parent_id = getattr(parent, "id", None) if parent else None
            _LOGGER.info("CHILD %s via_device SET to %s", model, parent_id)
            if parent_id:
                via_device = (DOMAIN, str(parent_id))
        elif isinstance(device, DeviceHvac) and getattr(
            device, "_parent_fan", None
        ):
            # 6d: HVAC devices (REM/CO2) grouped under their FAN parent
            parent_fan = getattr(device, "_parent_fan", None)
            parent_fan_id = (
                getattr(parent_fan, "id", None) if parent_fan else None
            )
            _LOGGER.info("HVAC %s via_device SET to %s", model, parent_fan_id)
            if parent_fan_id:
                via_device = (DOMAIN, str(parent_fan_id))
        else:
            via_device = None

        # Conditionally assemble kwargs to protect HA TypedDict strict checks
        kwargs: dict[str, Any] = {}
        if via_device is not None:
            kwargs["via_device"] = via_device

        device_info = DeviceInfo(
            identifiers={(DOMAIN, str(device.id))},
            name=device_name,
            manufacturer=None,
            model=model,
            serial_number=str(device.id),
            **kwargs,
        )

        if self._device_info.get(str(device.id)) == device_info:
            return

        self._device_info[str(device.id)] = device_info

        device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id, **device_info
        )

    async def _async_update_data(self) -> None:
        """Fetch data from the RAMSES RF client."""
        _LOGGER.debug("Coordinator: _async_update_data called (Heartbeat)")
        if not self.client:
            _LOGGER.debug(
                "Coordinator: (_async_update_data) Client None, skip update"
            )
            return None

        # Gateway health check: use ramses_rf's Gateway.is_active (SSOT)
        # to detect if the gateway has stopped receiving messages.
        # Creates a persistent notification so the user knows the
        # gateway is offline rather than wondering why entities have
        # no values.
        await self._check_gateway_health()

        # Coordinator only updates existing entities. If ramses_rf pushes
        # updates via callbacks, logic here is minimal. Poll values here
        # if needed.

        return None

    async def _check_gateway_health(self) -> None:
        """Check gateway health via ramses_rf's is_active and notify.

        Uses the gateway's ``is_active`` property (SSOT) which compares
        the last message timestamp against ``message_timeout`` (10 min
        default, configurable via ``gateway_timeout``).  Creates a
        persistent notification when the gateway goes inactive and
        dismisses it when it recovers.

        Skips the first few checks to avoid a false "offline" alarm
        during startup (is_active returns False before any packets
        have arrived).
        """
        gwy: Gateway = self.client
        if gwy is None or gwy.hgi is None:
            return

        # Grace period: skip the first 3 checks (covers ~3 minutes
        # at default scan_interval=60s) to allow the first packets
        # to arrive before declaring the gateway offline.
        self._health_check_count += 1
        if self._health_check_count <= 3:
            return

        try:
            is_active = await gwy.hgi.is_active()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Gateway health check failed: %s", err)
            return

        if not is_active and not self._gateway_offline_notified:
            self._gateway_offline_notified = True
            timeout_mins = int(gwy.hgi.message_timeout.total_seconds() / 60)
            _LOGGER.warning(
                "Gateway appears offline: no packets received for %d+ min(s)",
                timeout_mins,
            )
            async_create_notification(
                self.hass,
                title="RAMSES CC: Gateway offline",
                message=(
                    f"The gateway has not received RF packets for "
                    f"{timeout_mins}+ minutes.\n\n"
                    "Entity values may be stale or unavailable. "
                    "Check:\n"
                    "- The ramses_esp / HGI80 device is powered and "
                    "connected\n"
                    "- The MQTT broker is reachable\n"
                    "- The gateway is publishing to the expected topic"
                    "\n\n"
                    "The notification will clear automatically when "
                    "packets resume."
                ),
                notification_id="ramses_cc_gateway_offline",
            )
        elif is_active and self._gateway_offline_notified:
            self._gateway_offline_notified = False
            async_dismiss_notification(self.hass, "ramses_cc_gateway_offline")
            _LOGGER.info("Gateway back online: packets received again")

    async def _async_discovery_task(self, _now: dt | None = None) -> None:
        """Execute periodic discovery task from the interval listener."""
        try:
            await self._discover_new_entities()
        except Exception as err:
            _LOGGER.error("Discovery error: %s", err, exc_info=True)

    async def _discover_new_entities(self) -> None:
        """Discover new devices in the client and register them with HA."""
        if not self.client:
            return

        gwy: Gateway = self.client

        active_hgi_id = self.active_hgi_id
        if (
            isinstance(active_hgi_id, str)
            and _DEVICE_ID_RE.match(active_hgi_id)
            and active_hgi_id not in gwy.device_registry.device_by_id
        ):
            with suppress(Exception):
                gwy.device_registry.get_device(active_hgi_id)

        if (
            self.discovery_manager is not None
            and self.discovery_manager.active_hgi_id != active_hgi_id
        ):
            self.discovery_manager.active_hgi_id = active_hgi_id

        # Snapshot lists to avoid RuntimeError if ramses_rf updates
        # continuously (fixes silent failure when list size changes).
        current_devices = [
            d
            for d in gwy.device_registry.devices
            if d.id not in self._disabled_device_ids
        ]
        current_systems = list(gwy.device_registry.systems)

        # --- DIAGNOSTIC LOGGING ---
        # This will reveal if ramses_rf has actually found any devices.
        _LOGGER.info(
            "Discovery: Devices=%s, Systems=%s",
            len(current_devices),
            len(current_systems),
        )
        if len(current_devices) > 0:
            _LOGGER.debug(
                "Discovered Devices: %s", [d.id for d in current_devices]
            )

        async def async_add_entities(
            platform: str, devices: Sequence[RamsesRFEntity]
        ) -> None:
            if not devices:
                return
            await self._async_setup_platform(platform)
            async_dispatcher_send(
                self.hass, SIGNAL_NEW_DEVICES.format(platform), devices
            )

        def find_new_entities(
            known: list[_T_Entity], current: list[_T_Entity]
        ) -> tuple[list[_T_Entity], list[_T_Entity]]:
            # Compare by device.id, not identity: ramses_rf device classes
            # define no __eq__/__hash__, so `x not in known` falls back to
            # identity comparison.  When ramses_rf recreates a device object
            # (same device.id, new Python identity) during a schema reload
            # or sync_learned_topology, the identity check would treat it as
            # "new" and re-dispatch it to platforms, producing
            # "Platform ramses_cc does not generate unique IDs. ID ... already
            # exists - ignoring ..." in the HA log (climate entity collision
            # on reload).  Comparing by id keeps the recreated object out of
            # the "new" list (no re-dispatch) while swapping the fresh
            # instance into the known list so downstream code (e.g.
            # _async_update_device) doesn't hold a stale reference.
            known_by_id: dict[Any, _T_Entity] = {
                getattr(x, "id", id(x)): x for x in known
            }
            merged: dict[Any, _T_Entity] = {}
            new: list[_T_Entity] = []
            for x in current:
                x_id = getattr(x, "id", id(x))
                if x_id in merged:
                    continue  # dedupe within a single snapshot
                merged[x_id] = x
                if x_id not in known_by_id:
                    new.append(x)
            # preserve any known entities that disappeared from current
            # (they may reappear next cycle; dropping them here would
            # incorrectly re-dispatch them if they return)
            for k_id, k_obj in known_by_id.items():
                if k_id not in merged:
                    merged[k_id] = k_obj
            return list(merged.values()), new

        # Explicit typing bypasses list invariance issues without casting
        current_evo_systems: list[System] = [
            s for s in current_systems if isinstance(s, Evohome)
        ]
        self._systems, new_systems = find_new_entities(
            self._systems, current_evo_systems
        )

        current_zones: list[Zone] = [
            z
            for s in current_systems
            if isinstance(s, Evohome)
            for z in s.zones
        ]
        self._zones, new_zones = find_new_entities(self._zones, current_zones)

        current_dhws: list[Zone] = [
            s.dhw for s in current_systems if isinstance(s, Evohome) and s.dhw
        ]
        self._dhws, new_dhws = find_new_entities(self._dhws, current_dhws)

        self._devices, new_devices = find_new_entities(
            self._devices, current_devices
        )

        # Process new devices for fan logic: Systems/DHWs before Devices
        # to ensure via_device parents exist
        for device in new_systems + new_dhws + new_zones + new_devices:
            await self.fan_handler.async_setup_fan_device(device)
            # Register device in registry once upon discovery
            await self._async_update_device(device)

        # Refresh device names for already-known zones.  Zone names arrive
        # via 0004 packets which may reach ramses_rf *after* the zone was
        # first created from the cached schema (issue 822).  Without this
        # refresh, the device registry keeps the fallback name (e.g.
        # "01:216136_01") forever.  _async_update_device's guard makes this
        # a cheap no-op once the real name is in place, and HA's
        # name/name_by_user split ensures user edits are never clobbered.
        for zone in self._zones:
            await self._async_update_device(zone)

        new_entities = new_systems + new_dhws + new_zones + new_devices

        if not new_entities:
            return

        # Register new entities with platforms
        await async_add_entities(Platform.BINARY_SENSOR, new_entities)
        await async_add_entities(Platform.SENSOR, new_entities)

        await async_add_entities(
            Platform.CLIMATE,
            [d for d in new_devices if isinstance(d, HvacVentilator)],
        )
        # Phase 3b: remote entities on both REMs (HvacRemoteBase) and
        # FANs (HvacVentilator).  FAN entity is the primary target for
        # dict-template commands; REM entity stays for backward compat.
        await async_add_entities(
            Platform.REMOTE,
            [
                d
                for d in new_devices
                if isinstance(d, (HvacRemoteBase, HvacVentilator))
            ],
        )
        await async_add_entities(Platform.CLIMATE, new_systems)
        await async_add_entities(Platform.CLIMATE, new_zones)
        await async_add_entities(Platform.WATER_HEATER, new_dhws)
        await async_add_entities(Platform.NUMBER, new_entities)

        # Trigger a save if we found something new
        await self.async_save_client_state()

    # Delegate service calls to the Service Handler
    async def async_bind_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_bind_device(call)

    async def async_force_update(self, _: ServiceCall) -> None:
        """Force an immediate update of all device states.

        Clears the resolve_async_attr cooldown cache on all entities so
        that freshly-received packet data is visible immediately rather
        than waiting for the 1-second cooldown to expire.

        :param _: Unused service call argument.
        """
        for entity in self._entities.values():
            clear_async_attr_cache(entity)
        await self.async_refresh()

    async def async_sync_topology(self, _: ServiceCall) -> None:
        """Sync learned topology to the config entry immediately.

        Triggers the same save + sync_learned_topology cycle that normally
        runs every 30 minutes (SAVE_STATE_INTERVAL), so users don't have to
        wait after ramses_rf has learned new topology (e.g. from 000C).

        Also runs the discovery mismatch checks (Phase 3c) so mismatches
        are detected immediately rather than waiting for the 30-minute
        checkpoint, and refreshes zone device names so 0004 zone_name
        packets are reflected in the device registry without waiting for
        the next discovery cycle.

        :param _: Unused service call argument.
        """
        _LOGGER.info("Manual topology sync requested (sync_topology service)")
        await self.async_save_client_state()
        # Refresh zone device names — 0004 zone_name packets may have
        # updated zone_state.name since the zone was first created from
        # the cached schema (issue 822, 947).  Without this, the device
        # registry keeps the old name until the next discovery cycle.
        for zone in self._zones:
            await self._async_update_device(zone)
        # Run mismatch checks immediately (Phase 3c)
        if self.discovery_manager:
            # Check ramses_rf known_list for contradiction-based class
            # changes before running mismatch checks, so rf-flagged
            # mismatches are included in the notification.
            self._check_rf_contradictions()
            # Use self.entry.options (live) — see _async_discovery_checkpoint.
            schema = self.entry.options.get(CONF_SCHEMA, {})
            if isinstance(schema, dict):
                self.discovery_manager.check_all_mismatches(
                    schema,
                    zones=self._zones,
                    devices=self._devices if self.client else None,
                )

    async def async_send_packet(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_send_packet(call)

    async def async_probe_hvac_binding(
        self, call: ServiceCall
    ) -> dict[str, Any]:
        """Delegate to Service Handler for HVAC binding probing.

        :param call: The service call object containing parameters.
        """
        return await self.service_handler.async_probe_hvac_binding(call)

    async def async_discover_known_devices(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_discover_known_devices(call)

    async def async_get_discovered_devices(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_get_discovered_devices(call)

    async def async_accept_discovered_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_accept_discovered_device(call)

    async def async_discard_discovered_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_discard_discovered_device(call)

    async def async_remove_discovered_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_remove_discovered_device(call)

    async def async_enable_discovered_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_enable_discovered_device(call)

    async def async_disable_discovered_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_disable_discovered_device(call)

    async def async_add_faked_rem(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_add_faked_rem(call)

    async def async_remove_device(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_remove_device(call)

    async def async_set_polling_interval(self, call: ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call object containing parameters.
        """
        await self.service_handler.async_set_polling_interval(call)

    async def async_get_fan_param(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Delegate to Service Handler.

        :param call: The service call or dictionary containing parameters.
        """
        await self.service_handler.async_get_fan_param(call)

    async def _async_run_fan_param_sequence(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Delegate to Service Handler to run the fan parameter sequence.

        :param call: The service call or dictionary containing parameters.
        """
        await self.service_handler._async_run_fan_param_sequence(call)

    def get_all_fan_params(self, call: dict[str, Any] | ServiceCall) -> None:
        """Delegate to Service Handler.

        :param call: The service call or dictionary containing parameters.
        """
        # Note: get_all_fan_params is synchronous, wraps async call in a task
        self.hass.async_create_task(
            self.service_handler._async_run_fan_param_sequence(call)
        )

    async def async_set_fan_param(
        self, call: dict[str, Any] | ServiceCall
    ) -> None:
        """Delegate to Service Handler.

        :param call: The service call or dictionary containing parameters.
        """
        await self.service_handler.async_set_fan_param(call)

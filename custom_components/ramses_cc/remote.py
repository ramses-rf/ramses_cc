"""Support for RAMSES HVAC RF remotes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.components.remote import (
    RemoteEntity,
    RemoteEntityDescription,
    RemoteEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)
from homeassistant.helpers.event import async_track_state_change_event

from ramses_rf.devices import HvacRemote, HvacVentilator
from ramses_rf.entity import Entity as RamsesRFEntity
from ramses_tx.const import DEFAULT_GAP_DURATION, Priority
from ramses_tx.exceptions import ProtocolError, ProtocolSendFailed, ProtocolTimeoutError

from .const import ATTR_DEVICE_ID, CONF_SCHEMA, DOMAIN
from .coordinator import RamsesCoordinator
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import parse_packet_string
from .schemas import DEFAULT_NUM_REPEATS, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

# Packet template keys (Phase 3b — {verb, code, payload} dict format)
_CMD_VERB: str = "verb"
_CMD_CODE: str = "code"
_CMD_PAYLOAD: str = "payload"
_CMD_SRC: str = "src"  # optional explicit src override

# Reserved metadata keys inside _commands dicts (not commands themselves).
# These are stripped when iterating commands, but preserved when writing
# back to the schema.  Add future Builder metadata keys here (e.g.
# "_description", "_category", "_deprecated") — one place, no scatter.
_RESERVED_CMD_KEYS: frozenset[str] = frozenset({"_comment"})

# Codes that learn_command listens for (22F1/22F3/22F7/22B0)
_LEARN_CODES: tuple[str, ...] = ("22F1", "22F3", "22F7", "22B0")


def _split_commands(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a raw ``_commands`` dict into (commands, metadata).

    Reserved keys (``_comment``, future Builder metadata) are separated
    from actual command entries.  This is the single place that knows
    which keys are metadata — all callers use this helper instead of
    inline checks.

    :param raw: The raw ``_commands`` dict from schema/coordinator.
    :return: A tuple of (commands, metadata) dicts.
    """
    commands: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _RESERVED_CMD_KEYS:
            metadata[k] = v
        else:
            commands[k] = v
    return commands, metadata


def _with_metadata(
    commands: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Re-attach metadata to a commands dict for schema persistence.

    Inverse of ``_split_commands``: used when writing back to the schema
    after modifying commands (delete, learn, add).  Metadata keys like
    ``_comment`` are preserved across mutations.

    :param commands: The command entries (no metadata).
    :param metadata: The metadata entries (e.g. ``_comment``).
    :return: A combined dict suitable for schema persistence.
    """
    result = dict(commands)
    result.update(metadata)
    return result


def _merge_commands(*sources: dict[str, Any]) -> dict[str, Any]:
    """Merge multiple ``_commands`` dicts, keeping metadata from the first.

    Later sources only contribute commands (not metadata) — metadata
    from the first source wins.  This matches the FAN-over-REM priority:
    the FAN's own ``_comment`` is kept, REM metadata is ignored.

    :param sources: Ordered ``_commands`` dicts (highest priority first).
    :return: A merged dict with commands + metadata from the first source.
    """
    if not sources:
        return {}
    result: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    for i, src in enumerate(sources):
        cmds, meta = _split_commands(src)
        if i == 0:
            metadata = meta
        for cmd_name, cmd_val in cmds.items():
            if cmd_name not in result:
                result[cmd_name] = cmd_val
    result.update(metadata)
    return result


def _build_packet_from_template(
    cmd_def: dict[str, str],
    fan_device: HvacVentilator,
    coordinator: RamsesCoordinator,
) -> str:
    """Build a packet string from a ``{verb, code, payload}`` template.

    Addresses are filled at send time:
    - src: explicit ``src`` field, or first bound REM (via get_bound_rem),
      or HGI gateway ID as fallback
    - dst: the FAN's own device ID
    - brd: broadcast address (always ``--:------``)
    - length: calculated from payload (bytes = len(payload) // 2)

    :param cmd_def: Command dict with ``verb``, ``code``, ``payload``,
        optional ``src``.
    :param fan_device: The FAN (HvacVentilator) device that owns the command.
    :param coordinator: The coordinator (for HGI fallback lookup).
    :return: A full packet string ready for ``CommandDTO()``.
    :raises HomeAssistantError: If no src can be resolved.
    """
    verb = cmd_def[_CMD_VERB]
    code = cmd_def[_CMD_CODE]
    payload = cmd_def[_CMD_PAYLOAD]

    # src resolution: explicit src > bound REM > HGI fallback
    src = cmd_def.get(_CMD_SRC)
    if not src:
        src = str(fan_device.get_bound_rem() or "")
    if not src:
        # Fallback to HGI gateway ID
        client = coordinator.client
        if client:
            hgi = getattr(client, "_gwy", None)
            if hgi:
                hgi_dev = getattr(hgi, "_hgi", None) or getattr(hgi, "hgi", None)
                if hgi_dev:
                    src = str(hgi_dev.id)
    if not src:
        raise HomeAssistantError(
            "No bound REM or HGI available to send command — set _bound on the FAN"
        )

    dst = fan_device.id
    return f"{verb} {src} {dst} --:------ {code} {payload}"


def _parse_packet_to_template(packet: str) -> dict[str, str]:
    """Extract ``{verb, code, payload}`` from a captured packet string.

    Inverse of :func:`_build_packet_from_template`.  Used by
    ``learn_command`` to store captured packets as templates (no hardcoded
    addresses).

    Packet format: ``{verb} --- {src} {dst} {brd} {code} {len} {payload}``

    :param packet: Full packet string (e.g.
        ``"W --- 32:153001 30:160000 --:------ 22F7 003 0000EF"``).
    :return: Dict with ``verb``, ``code``, ``payload`` keys.
    :raises ValueError: If the packet doesn't have enough parts.
    """
    parts = packet.split()
    if len(parts) < 8:
        raise ValueError(f"Packet too short to parse: {packet}")
    verb = parts[0]
    code = parts[5]
    payload = parts[7]
    return {_CMD_VERB: verb, _CMD_CODE: code, _CMD_PAYLOAD: payload}


def _is_command_dict(value: Any) -> bool:
    """Check if a command value is a Phase 3b dict template.

    Dict templates have ``verb``, ``code``, ``payload`` keys (at minimum).
    String values are Phase 3a packet strings (backward compat).

    :param value: The command value to check.
    :return: True if the value is a dict command template.
    """
    return (
        isinstance(value, dict)
        and _CMD_VERB in value
        and _CMD_CODE in value
        and _CMD_PAYLOAD in value
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the remote platform.

    Phase 3b: ``remote`` entities are created on both REMs (``HvacRemote``)
    and FANs (``HvacVentilator``).  The FAN entity is the primary target for
    Phase 3b commands (dict templates); the REM entity stays for backward
    compatibility (packet strings).

    :param hass: The Home Assistant instance.
    :param entry: The config entry.
    :param async_add_entities: Callback to add entities.
    """
    coordinator: RamsesCoordinator = hass.data[DOMAIN][entry.entry_id]
    platform: EntityPlatform = async_get_current_platform()

    @callback
    def add_devices(devices: RamsesRFEntity | Sequence[RamsesRFEntity]) -> None:
        # 1. Safely wrap a single device into a list, or keep it as a sequence
        device_list = devices if isinstance(devices, Sequence) else [devices]

        # 2. Iterate over device_list (not 'devices')
        # Phase 3b: create remote entities on both REMs and FANs
        entities = [
            RamsesRemoteEntityDescription.ramses_cc_class(
                coordinator, device, RamsesRemoteEntityDescription(key="remote")
            )
            for device in device_list
            if isinstance(device, (HvacRemote, HvacVentilator))
        ]
        async_add_entities(entities)

    coordinator.async_register_platform(platform, add_devices)


class RamsesRemote(RamsesEntity, RemoteEntity):
    """Representation of a RAMSES RF remote.

    Phase 3b: This entity is created on both REMs (``HvacRemote``) and
    FANs (``HvacVentilator``).  The FAN entity stores commands as
    ``{verb, code, payload}`` dict templates; the REM entity stores
    commands as full packet strings (backward compat).

    For a FAN entity, ``_commands`` is loaded from:
    1. The FAN's own schema ``_commands`` (dict templates — Phase 3b)
    2. The bound REM's ``_commands`` (packet strings — Phase 3a fallback)
    """

    _device: HvacRemote | HvacVentilator

    _attr_assumed_state: bool = True
    _attr_supported_features: int = (
        RemoteEntityFeature.LEARN_COMMAND | RemoteEntityFeature.DELETE_COMMAND
    )

    def __init__(
        self,
        coordinator: RamsesCoordinator,
        device: HvacRemote | HvacVentilator,
        entity_description: RamsesRemoteEntityDescription,
    ) -> None:
        """Initialize a HVAC remote.

        :param coordinator: The RamsesCoordinator instance.
        :param device: The backend device instance (REM or FAN).
        :param entity_description: The entity description.
        """
        _LOGGER.info("Found %s (remote entity)", device.id)
        super().__init__(coordinator, device, entity_description)

        self._attr_is_on = True
        # Load commands: FAN gets its own + bound REM's; REM gets its own.
        # _split_commands separates metadata (_comment) from actual commands.
        raw_commands = coordinator._remotes.get(device.id, {})
        self._commands, meta = _split_commands(raw_commands)
        self._command_comment: str | None = meta.get("_comment")
        if isinstance(device, HvacVentilator):
            # FAN: also load commands from bound REMs (backward compat).
            # _merge_commands keeps FAN metadata, ignores REM metadata.
            bound_rem = device.get_bound_rem()
            if bound_rem:
                rem_commands = coordinator._remotes.get(str(bound_rem), {})
                if rem_commands:
                    merged = _merge_commands(raw_commands, rem_commands)
                    self._commands, meta = _split_commands(merged)
                    self._command_comment = meta.get("_comment")

    @property
    def is_fan_entity(self) -> bool:
        """Return True if this entity is on a FAN (not a REM).

        :return: True if the underlying device is an HvacVentilator.
        """
        return isinstance(self._device, HvacVentilator)

    @property
    def _bound_rem_ids(self) -> list[str]:
        """Return the list of bound REM device IDs (FAN entity only).

        For a FAN entity, reads ``_bound`` from the schema to get all
        bound REM IDs.  For a REM entity, returns an empty list.

        :return: List of bound REM device IDs, or empty list.
        """
        if not self.is_fan_entity:
            return []
        schema = self.coordinator.options.get(CONF_SCHEMA, {})
        entry = schema.get(self._device.id, {})
        if not isinstance(entry, dict):
            return []
        bound = entry.get("_bound", [])
        if isinstance(bound, str):
            return [bound]
        if isinstance(bound, list):
            return bound
        return []

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes.

        For REM entities: ``commands`` (packet strings) + ``bound_to_fan``.
        For FAN entities: ``commands`` (dict templates) + ``bound_rems``.

        :return: A dictionary of state attributes.
        """
        attrs = super().extra_state_attributes | {"commands": self._commands}
        if self._command_comment:
            attrs["command_comment"] = self._command_comment

        if self.is_fan_entity:
            # FAN entity: expose bound REMs list
            bound_rems = self._bound_rem_ids
            if bound_rems:
                attrs["bound_rems"] = bound_rems
        else:
            # REM entity: expose which FAN this REM is bound to
            fan_handler = self.coordinator.fan_handler
            if fan_handler and self._device.id in fan_handler._fan_bound_to_remote:
                attrs["bound_to_fan"] = fan_handler._fan_bound_to_remote[
                    self._device.id
                ]
        return attrs

    @property
    def _commands_for_save(self) -> dict[str, Any]:
        """Return commands + metadata for schema persistence.

        The coordinator reads this during ``async_save_client_state`` to
        build the ``remotes`` dict.  It re-attaches ``_comment`` (and any
        other reserved metadata) that was stripped from ``self._commands``
        by ``_split_commands`` during ``__init__``.
        """
        meta = {"_comment": self._command_comment} if self._command_comment else {}
        return _with_metadata(self._commands, meta)

    async def async_delete_command(
        self,
        command: Iterable[str] | str,
        **kwargs: Any,
    ) -> None:
        """Delete commands from the database.

        Usage:

        .. code-block::

            service: remote.delete_command
            data:
              command: boost
            target:
              entity_id: remote.device_id

        :param command: The command(s) to delete.
        :param kwargs: Arbitrary keyword arguments.
        """
        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        # if len(command) != 1:
        #     raise HomeAssistantError("must be exactly one command to delete")

        assert not kwargs, kwargs  # TODO: remove me

        self._commands = {k: v for k, v in self._commands.items() if k not in command}
        meta = {"_comment": self._command_comment} if self._command_comment else {}
        await self.coordinator._async_update_schema_commands(
            self._device.id, _with_metadata(self._commands, meta)
        )

    async def async_learn_command(
        self,
        command: Iterable[str] | str,
        timeout: float = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        """Learn a command from a device (remote) and add to the database.

        Usage:

        .. code-block::

            service: remote.learn_command
            data:
              command: boost
              timeout: 3
            target:
              entity_id: remote.device_id

        :param command: The command to learn, either as str or list of strs.
        :param timeout: Timeout in seconds, defaults to DEFAULT_TIMEOUT.
        :param kwargs: Arbitrary keyword arguments.
        :raises HomeAssistantError: If command argument is invalid or on TimeOut.
        """
        _LOGGER.debug("REM Learn starting, cmd: %s", command)
        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise HomeAssistantError("Enter exactly one command to learn")

        assert not kwargs, kwargs  # TODO: remove me

        if command[0] in self._commands:
            await self.async_delete_command(command)

        # Event to signal when the command is received, TODO not thread safe!
        learning_session = asyncio.Event()

        @callback
        async def _async_on_change(event: Any) -> None:
            """Save the new command to storage.

            For REM entities: listens to ``src == self._device.id``,
            stores as packet string.

            For FAN entities: listens to ``src in self._bound_rem_ids``,
            stores as ``{verb, code, payload}`` dict template.

            :param event: Event to evaluate
            """

            new_state: State = event.data["new_state"]
            new_data = new_state.attributes["extra_data"]
            # to extract e.g. 'code' in a jinja template, use:
            # {{ state_attr('event.ramses_cc_learn_event', 'extra_data')['code'] }}

            # Determine valid src IDs for this entity
            if self.is_fan_entity:
                valid_srcs = set(self._bound_rem_ids)
            else:
                valid_srcs = {self._device.id}

            if new_data["src"] in valid_srcs and new_data["code"] in _LEARN_CODES:
                if self.is_fan_entity:
                    # FAN entity: store as dict template (Phase 3b)
                    self._commands[command[0]] = _parse_packet_to_template(
                        new_data["packet"]
                    )
                else:
                    # REM entity: store as packet string (Phase 3a)
                    self._commands[command[0]] = new_data["packet"]
                learning_session.set()  # stops learn session
                # Persist to schema (SSOT) — .storage[remotes] is updated
                # on the next 5-min save cycle.  Re-attach metadata.
                meta = (
                    {"_comment": self._command_comment} if self._command_comment else {}
                )
                await self.coordinator._async_update_schema_commands(
                    self._device.id, _with_metadata(self._commands, meta)
                )
            else:
                _LOGGER.debug(
                    "REM FILTER FAILED: src=%s code=%s (valid_srcs=%s)",
                    new_data["src"],
                    new_data["code"],
                    valid_srcs,
                )

        with self.coordinator._sem:
            _LOGGER.debug("LEARN _sem set, setting up listener")
            self.coordinator.learn_device_id = self._device.id
            remove_listener = async_track_state_change_event(
                self.hass, "event.ramses_cc_learn_event", _async_on_change
            )  # entity_ids format: event.{DOMAIN}_{event._attr_unique_id}

            try:
                _LOGGER.debug("REM LEARN listener attached, listening")
                await asyncio.wait_for(learning_session.wait(), timeout=timeout)
            except TimeoutError as err:
                warn_text = (
                    f"Timeout (start={timeout}) waiting for command '{command[0]}'"
                )
                _LOGGER.warning(warn_text)
                # Catch and rethrow to UI
                raise HomeAssistantError(f"{warn_text} ({err})") from err
            finally:
                self.coordinator.learn_device_id = (
                    None  # deactivates the ramses_cc_learn_event msg callback
                )
                remove_listener()
                _LOGGER.debug("REM LEARN listener removed")

    async def async_send_command(
        self,
        command: Iterable[str] | str,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        delay_secs: float = DEFAULT_GAP_DURATION,
        hold_secs: None = None,
        **kwargs: Any,
    ) -> None:
        """Send commands from a device (remote).

        Usage:

        .. code-block::

            service: remote.send_command
            data:
              command: boost
              delay_secs: 0.05
              num_repeats: 3
              device: 12:345678 (see NOTE)
            target:
              entity_id: remote.device_id

        :param command: The command(s) to send.
        :param num_repeats: Number of times to repeat the command.
        :param delay_secs: Delay between repeats (gap duration).
        :param hold_secs: Not supported.
        :param kwargs: Arbitrary keyword arguments.
        :raises HomeAssistantError: If hold_secs is provided or command format is invalid.
        :raises LookupError: If the command is not known.
        """
        # NOTE This command can also be called directly from Actions>remote.send_command
        # in that case:
        # - validate entry (example: max_num_repeats = 255!
        # - if device is supplied, lookup device_id and replace self.entity_id?
        if kwargs:
            _extra: str = (
                " The provided Device is ignored." if (kwargs.get("device")) else ""
            )
            _LOGGER.warning(
                "Use ramses_cc 'Send a Remote command' instead of this HA command to assure valid entry.%s",
                _extra,
            )
        # TODO validate/normalise other entry values?

        # HACK to make ramses_cc call work as per HA service call
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise HomeAssistantError("must be exactly one command to send")

        if hold_secs:
            raise HomeAssistantError("hold_secs is not supported")

        if command[0] not in self._commands:
            raise HomeAssistantError(f"command '{command[0]}' is not known")

        cmd_value = self._commands[command[0]]

        # Phase 3b: dict templates are built at send time; packet strings
        # (Phase 3a) are used directly
        if _is_command_dict(cmd_value):
            # FAN entity with dict template — build packet
            if not isinstance(self._device, HvacVentilator):
                raise HomeAssistantError(
                    "Dict-format commands require a FAN entity target"
                )
            packet_str = _build_packet_from_template(
                cmd_value, self._device, self.coordinator
            )
        else:
            # REM entity with packet string (Phase 3a backward compat)
            packet_str = str(cmd_value)
            if (
                not self._device.is_faked
            ):  # have to check here, as not using device method
                raise HomeAssistantError(
                    f"{self._device.id} is not configured for faking"
                )

        # Users might enter a CLI shorthand OR a raw frame string;
        # _build_packet_from_template returns CLI shorthand (W src dst code
        # payload) which parse_packet_string() parses.  For REM packet strings,
        # users might enter either format.
        cmd = parse_packet_string(packet_str)
        if cmd is None:
            raise ValueError(f"Failed to parse packet_str: {packet_str}")

        if not self.coordinator.client:
            raise HomeAssistantError(
                "Cannot send command: RAMSES RF client is not initialized"
            )

        try:
            await self.coordinator.client.async_send_cmd(
                cmd,
                priority=Priority.HIGH,
                num_repeats=num_repeats,
                gap_duration=delay_secs,  # We map 'delay_secs' to 'gap_duration' in ramses_rf
            )

        except (
            TimeoutError,
            ProtocolSendFailed,
            ProtocolTimeoutError,
            ProtocolError,
            AssertionError,
            Exception,
        ) as err:
            # Catch and rethrow TimeoutError (from ramses_rf) and generic Exceptions
            raise HomeAssistantError(
                f"Error sending command '{command[0]}' to device {self._device.id} ({err})"
            ) from err

        # This will now execute even if the transmission failed
        await self.coordinator.async_refresh()

    async def async_add_command(
        self,
        command: Iterable[str] | str,
        packet_string: str,
        **kwargs: Any,
    ) -> None:
        """Directly add (or replace) a command without RF learning.

        Usage:

        .. code-block::

            service: remote.add_command
            data:
              command: boost
              packet_string: "RQ --- 29:162275 30:123456 --:------ 22F1 003 000030"
            target:
              entity_id: remote.device_id

        For FAN entities (Phase 3b), the packet string is parsed into a
        ``{verb, code, payload}`` dict template before storing.  For REM
        entities, the full packet string is stored as-is (backward compat).

        :param command: The command name to add.
        :param packet_string: The raw packet string for the command.
        :param kwargs: Arbitrary keyword arguments.
        :raises HomeAssistantError: If command format is invalid.
        :raises ValueError: If packet_string is invalid.
        """
        command = [command] if isinstance(command, str) else list(command)
        if len(command) != 1:
            raise HomeAssistantError("must be exactly one command to add")

        assert not kwargs, kwargs  # TODO: remove me

        # Basic validation: ensure packet parses as a CommandDTO
        if parse_packet_string(packet_string) is None:
            raise ValueError(f"packet_string invalid: {packet_string}")

        if command[0] in self._commands:
            await self.async_delete_command(command)

        if self.is_fan_entity:
            # FAN entity: parse to dict template (Phase 3b)
            self._commands[command[0]] = _parse_packet_to_template(packet_string)
        else:
            # REM entity: store packet string as-is (Phase 3a)
            self._commands[command[0]] = packet_string
        meta = {"_comment": self._command_comment} if self._command_comment else {}
        await self.coordinator._async_update_schema_commands(
            self._device.id, _with_metadata(self._commands, meta)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the remote device.

        :param kwargs: Additional arguments for the turn_off operation.
        """
        _LOGGER.debug("Turning off REM device %s", self._device.id)
        pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the remote device.

        :param kwargs: Additional arguments for the turn_on operation.
        """
        _LOGGER.debug("Turning on REM device %s", self._device.id)
        pass

    # the 2411 fan_param services, adapted from climate.py (no REM update_all service)

    @callback
    async def async_get_fan_rem_param(self, **kwargs: Any) -> None:
        """Handle 'get_fan_param' service call.

        :param kwargs: Arbitrary keyword arguments.
        """
        _LOGGER.info(
            "Fan param read via remote entity %s (%s, id %s)",
            self.entity_id,
            self.__class__.__name__,
            self._device.id,
        )
        parent = self.coordinator.fan_handler._fan_bound_to_remote.get(self._device.id)
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            await self.coordinator.async_get_fan_param(kwargs)
        else:
            _LOGGER.warning("REM %s not bound to a FAN", self._device.id)

    @callback
    async def async_set_fan_rem_param(self, **kwargs: Any) -> None:
        """Handle 'set_fan_param' service call.

        :param kwargs: Arbitrary keyword arguments.
        """
        _LOGGER.info(
            "Fan param write via remote entity %s (%s)",
            self.entity_id,
            self.__class__.__name__,
        )
        parent = self.coordinator.fan_handler._fan_bound_to_remote.get(self._device.id)
        if parent:
            kwargs[ATTR_DEVICE_ID] = parent
            kwargs["from_id"] = self._device.id  # replaces manual from_id entry
            await self.coordinator.async_set_fan_param(kwargs)
        else:
            _LOGGER.warning("REM %s not bound to a FAN", self._device.id)

    # NOTE: async_update_fan_rem_params was removed.  It was never registered
    # in SVCS_RAMSES_REMOTE (only get_fan_rem_param / set_fan_rem_param are),
    # so it was unreachable dead code.  The domain 'update_fan_params' service
    # covers the bulk-read use case (with an explicit from_id for the bound
    # REM if needed).  See ramses_cc issue 851.


@dataclass(frozen=True, kw_only=True)
class RamsesRemoteEntityDescription(RamsesEntityDescription, RemoteEntityDescription):
    """Class describing Ramses remote entities."""

    key = "remote"

    # integration-specific attributes
    ramses_cc_class: type[RamsesRemote] = RamsesRemote

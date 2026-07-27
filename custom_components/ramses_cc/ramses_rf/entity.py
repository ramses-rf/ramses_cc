#!/usr/bin/env python3
"""RAMSES RF - Base class for all RAMSES-II objects: devices and constructs."""

from __future__ import annotations

import asyncio
import logging
from inspect import getmembers, isclass
from sys import modules
from typing import TYPE_CHECKING, Any, cast

from ..ramses_tx import Priority, QosParams

from .discovery import DiscoveryService
from .models import (
    DemandState,
    FaultLogState,
    HvacState,
    OpenThermState,
    ScheduleState,
    StateUpdatedEvent,
    TemperatureState,
)
from .state import EntityState

if TYPE_CHECKING:
    from ..ramses_tx import Command, Packet
    from ..ramses_tx.typing import DeviceIdT, DevIndexT

    from .devices import Controller
    from .gateway import Gateway
    from .interfaces import DeviceInterface
    from .messages import Message
    from .systems.tcs import Evohome


_QOS_TX_LIMIT = 12
_ID_SLICE = 9

_LOGGER = logging.getLogger(__name__)


def class_by_attr(name: str, attr: str) -> dict[str, Any]:
    """Return a mapping of a (unique) attr of classes in a module to that class.

    :param name: The module name to inspect.
    :type name: str
    :param attr: The attribute name to use as a key.
    :type attr: str
    :returns: A dictionary mapping attribute values to classes.
    :rtype: dict[str, Any]
    """

    def predicate(m: Any) -> bool:
        return isclass(m) and m.__module__ == name and bool(getattr(m, attr, None))

    return {getattr(c[1], attr): c[1] for c in getmembers(modules[name], predicate)}


class _Entity:
    """The ultimate base class for Devices/Zones/Systems.

    This class is primarily a coordinator that initializes the entity's identity
    and composes the specialized services for state management and discovery.
    """

    _SLUG: str = None  # type: ignore[assignment]

    def __init__(self, gwy: Gateway) -> None:
        """Initialize the base entity and its composed components.

        :param gwy: The gateway orchestrator.
        :type gwy: Gateway
        """
        self._gwy = gwy
        self.id: DeviceIdT = None  # type: ignore[assignment]
        self._qos_tx_count = 0

        # Specialized components via Composition
        self.entity_state: EntityState = EntityState(
            cast("DeviceInterface", self), self._gwy
        )
        self.discovery: DiscoveryService = DiscoveryService(self, self._gwy)

        # Context required by children (Zones/Devices)
        self._z_id: DeviceIdT = None  # type: ignore[assignment]
        self._z_idx: DevIndexT | None = None
        self.ctl: Controller | None = None
        self.tcs: Evohome | None = None

    def __repr__(self) -> str:
        return f"{self.id} ({self._SLUG})"

    def deprecate_device(self, pkt: Packet, reset: bool = False) -> None:
        """If an entity is deprecated enough times, stop sending to it.

        :param pkt: The packet triggering deprecation.
        :type pkt: Packet
        :param reset: If True, reset the deprecation counter, defaults to False.
        :type reset: bool, optional
        """
        if reset:
            self._qos_tx_count = 0
            return

        self._qos_tx_count += 1
        if self._qos_tx_count == _QOS_TX_LIMIT:
            _LOGGER.warning(
                f"{pkt} < Sending now deprecated for {self} "
                "(consider adjusting device_id filters)"
            )

    def _handle_msg(self, msg: Message) -> None:
        """Deprecated in Phase 2.5: Entities no longer cache their own packets.

        Routing is handled directly by the Gateway into the central MessageStore.
        """
        pass

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        """Replace the internal CQRS read-model state with a new immutable object.

        This method acts as the single ingestion point for state changes, completely
        bypassing legacy packet interception.

        :param event: The StateUpdatedEvent container wrapping the new frozen state.
        :type event: StateUpdatedEvent
        :return: None
        :rtype: None
        """
        if isinstance(event.state, TemperatureState) and hasattr(self, "temp_state"):
            setattr(self, "temp_state", event.state)  # noqa: B010
        elif isinstance(event.state, DemandState) and hasattr(self, "demand_state"):
            setattr(self, "demand_state", event.state)  # noqa: B010
        elif isinstance(event.state, ScheduleState) and hasattr(self, "schedule_state"):
            setattr(self, "schedule_state", event.state)  # noqa: B010
        elif isinstance(event.state, FaultLogState) and hasattr(self, "state"):
            setattr(self, "state", event.state)  # noqa: B010
        elif isinstance(event.state, OpenThermState) and hasattr(
            self, "opentherm_state"
        ):
            setattr(self, "opentherm_state", event.state)  # noqa: B010
        elif isinstance(event.state, HvacState) and hasattr(self, "hvac_state"):
            setattr(self, "hvac_state", event.state)  # noqa: B010

    def _send_cmd(self, cmd: Command, **kwargs: Any) -> asyncio.Task[Any] | None:
        """Proxy command sending to the Gateway.

        :param cmd: The command to send.
        :type cmd: Command
        :param kwargs: Optional sending parameters (e.g., priority).
        :type kwargs: Any
        :returns: The corresponding asyncio Task or None.
        :rtype: asyncio.Task[Any] | None
        """
        if self._qos_tx_count > _QOS_TX_LIMIT:
            _LOGGER.info(f"{cmd} < Sending was deprecated for {self}")
            return None

        return self._gwy.send_cmd(cmd, wait_for_reply=False, **kwargs)

    async def _async_send_cmd(
        self,
        cmd: Command,
        priority: Priority | None = None,
        qos: QosParams | None = None,
    ) -> Packet | None:
        """Proxy asynchronous command sending to the Gateway.

        :param cmd: The command to send.
        :type cmd: Command
        :param priority: Transmission priority, defaults to None.
        :type priority: Priority | None, optional
        :param qos: Quality of Service parameters, defaults to None.
        :type qos: QosParams | None, optional
        :returns: The response or echo packet.
        :rtype: Packet | None
        """
        if self._qos_tx_count > _QOS_TX_LIMIT:
            _LOGGER.warning(f"{cmd} < Sending was deprecated for {self}")
            return None

        # Build kwargs dynamically to prevent passing `None` to strict Gateway args
        kwargs: dict[str, Any] = {}
        if priority is not None:
            kwargs["priority"] = priority

        if qos:
            if hasattr(qos, "max_retries") and qos.max_retries is not None:
                kwargs["max_retries"] = qos.max_retries
            if hasattr(qos, "timeout") and qos.timeout is not None:
                kwargs["timeout"] = qos.timeout
            if hasattr(qos, "wait_for_reply") and qos.wait_for_reply is not None:
                kwargs["wait_for_reply"] = qos.wait_for_reply

        return await self._gwy.async_send_cmd(cmd, **kwargs)


class Entity(_Entity):
    """The base class for Devices/Zones/Systems."""

#!/usr/bin/env python3
"""RAMSES RF - Topology and Entity Relationships.

This module manages the graph relationships (Parent/Child) between RAMSES
entities, such as the association between a Zone and its Actuators.

# ARCHITECTURE NOTE: Topology & Heuristic Eavesdropping
#
# Determining bindings to a controller:
#  - Config: As per any explicitly loaded schema.
#  - Discovery: If in 000C packet, or packet *to* device where src is a controller.
#  - Eavesdrop: If packet *from* device where dst is a controller.
#
# Determining location in a schema (domain/DHW/zone):
#  - Config: As per any explicitly loaded schema.
#  - Discovery: If in 000C pkt - (Note: unable to do this for 10: & 00: TRVs).
#  - Discovery: From packet fingerprint, excl. payloads (only for 10:).
#  - Eavesdrop: From packet fingerprint, incl. payloads.
#
# NOTE: L7 Messages are routed only to physical Devices. Routing to virtual entities
# (i.e., Systems, Zones, Circuits) is handled internally by those Devices (e.g., UFC to UfhCircuit).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from . import exceptions as exc
from .const import F9, FA, FC, FF, SZ_ACTUATORS, SZ_SENSOR
from .schemas import SZ_CIRCUITS

if TYPE_CHECKING:
    from ..ramses_tx.typing import DeviceIdT

    from .devices import Controller
    from .entity import Entity
    from .systems.tcs import Evohome


_LOGGER = logging.getLogger(__name__)
_TRACE = logging.getLogger("ramses_rf.legacy_trace")


class Parent:
    """A Parent can be a System (TCS), a heating Zone, or a UFH Controller.

    A Parent maintains a registry of Child entities and validates the
    relationships based on domain-specific rules.
    """

    actuator_by_id: dict[DeviceIdT, Any]
    actuators: list[Any]
    circuit_by_id: dict[str, Any]

    _app_cntrl: Any
    _dhw_sensor: Any
    _dhw_valve: Any
    _htg_valve: Any

    def __init__(self, *args: Any, child_id: str | None = None, **kwargs: Any) -> None:
        """Initialize the Parent relationship manager.

        :param child_id: The domain or zone index for this parent.
        :type child_id: str | None
        """
        super().__init__(*args, **kwargs)

        self._child_id: str = child_id  # type: ignore[assignment]
        self.child_by_id: dict[str, Child] = {}
        self.childs: list[Child] = []

    @property
    def zone_idx(self) -> str:
        """Return the domain or zone index.

        :returns: The index string.
        :rtype: str
        """
        return self._child_id

    @zone_idx.setter
    def zone_idx(self, value: str) -> None:
        """Set the domain or zone index after validation.

        :param value: The new index.
        :type value: str
        """
        self._child_id = value

    def _add_child(
        self, child: Any, *, child_id: str | None = None, is_sensor: bool | None = None
    ) -> None:
        """Add a child device to this Parent, validating the association.

        :param child: The child entity to add.
        :type child: Any
        :param child_id: The specific sub-index (e.g. F9, FA), optional.
        :type child_id: str | None
        :param is_sensor: Whether the child acts as a sensor, optional.
        :type is_sensor: bool | None
        :raises SystemSchemaInconsistent: If the child contradicts existing schema.
        :raises SchemaInconsistentError: If the combination is invalid.
        """
        if hasattr(self, "childs") and child not in self.childs:
            pass

        try:
            if is_sensor and child_id == FA:
                if self._dhw_sensor and self._dhw_sensor is not child:
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed dhw_sensor (from {self._dhw_sensor} to {child})"
                    )
                self._dhw_sensor = child

            elif is_sensor and hasattr(self, SZ_SENSOR):
                if (
                    getattr(self, SZ_SENSOR, None)
                    and getattr(self, SZ_SENSOR) is not child
                ):
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed zone sensor (from {getattr(self, SZ_SENSOR)} to {child})"
                    )
                self._sensor = child

            elif is_sensor:
                raise exc.SchemaInconsistentError(
                    f"not a valid combination for {self}: {child}|{child_id}|{is_sensor}"
                )

            elif hasattr(self, SZ_CIRCUITS):
                if child not in self.circuit_by_id:
                    self.circuit_by_id[child.id] = child

            elif hasattr(self, SZ_ACTUATORS):
                if child not in self.actuators:
                    self.actuators.append(child)
                    self.actuator_by_id[child.id] = child

            elif child_id == F9:
                if self._htg_valve and self._htg_valve is not child:
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed htg_valve (from {self._htg_valve} to {child})"
                    )
                self._htg_valve = child

            elif child_id == FA:
                if self._dhw_valve and self._dhw_valve is not child:
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed dhw_valve (from {self._dhw_valve} to {child})"
                    )
                self._dhw_valve = child

            elif child_id == FC:
                if self._app_cntrl and self._app_cntrl is not child:
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed app_cntrl (from {self._app_cntrl} to {child})"
                    )
                self._app_cntrl = child

            elif child_id == FF:
                pass

            else:
                raise exc.SchemaInconsistentError(
                    f"not a valid combination for {self}: {child}|{child_id}|{is_sensor}"
                )

        except (exc.SchemaInconsistentError, exc.SystemSchemaInconsistent) as err:
            _TRACE.error(
                f"ADD_CHILD EXCEPTION: Validating {child} to parent {self} "
                f"failed: {err}"
            )
            raise

        self.childs.append(child)
        self.child_by_id[child.id] = child


class Child:
    """A Device can be the Child of a Parent (System, Zone, or UFH Controller).

    A Child maintains a reference to its Parent and relies on the central
    Event Bus for topological updates.
    """

    def __init__(
        self,
        *args: Any,
        parent: Parent | None = None,
        is_sensor: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Child relationship manager.

        :param parent: The parent entity, if known.
        :type parent: Parent | None
        :param is_sensor: Whether this entity is a sensor for the parent.
        :type is_sensor: bool | None
        """
        super().__init__(*args, **kwargs)

        self._parent = parent
        self._is_sensor = is_sensor
        self._child_id: str | None = None

    def _get_parent(
        self,
        parent: Parent | None,
        *,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> tuple[Parent, str | None]:
        """Validate and retrieve the target parent for this device.

        :param parent: The proposed parent.
        :type parent: Parent | None
        :param child_id: The specific sub-index (e.g. F9, FA).
        :type child_id: str | None
        :param is_sensor: Whether the child is a sensor.
        :type is_sensor: bool | None
        :returns: A tuple of the validated parent and child_id.
        :rtype: tuple[Parent, str | None]
        :raises SchemaInconsistentError: If validation rules are violated.
        """
        if parent is None:
            raise exc.SchemaInconsistentError(f"{self}: parent cannot be None")

        parent_class = parent.__class__.__name__
        self_class = self.__class__.__name__

        if self_class == "UfhController":
            child_id = FF

        if parent_class == "Controller":
            parent = cast(Any, parent).tcs
            parent_class = parent.__class__.__name__
            _TRACE.info(f"SUB-CONTROLLER: {self} shifted parent to {parent_class}")

        if parent_class in ("Evohome", "System") and child_id:
            if child_id in (F9, FA):
                parent = cast(Any, parent).get_dhw_zone()
                parent_class = parent.__class__.__name__
                _TRACE.info(f"DHW SHIFT: {self} shifted parent to {parent_class}")
            elif (
                hasattr(parent, "_max_zones")
                and int(child_id, 16) < cast(Any, parent)._max_zones
            ):
                parent = cast(Any, parent).get_htg_zone(child_id)
                parent_class = parent.__class__.__name__
                _TRACE.info(f"ZONE SHIFT: {self} shifted parent to {parent_class}")

        elif (
            parent_class
            in (
                "Zone",
                "DhwZone",
                "EleZone",
                "MixZone",
                "RadZone",
                "UfhZone",
                "ValZone",
            )
            and not child_id
        ):
            child_id = child_id or getattr(parent, "idx", None)

        if self._parent and self._parent != parent:
            err_msg = (
                f"{self} can't change parent "
                f"({self._parent}_{self._child_id} to {parent}_{child_id})"
            )
            _TRACE.error(f"PARENT CHANGE EXCEPTION: {err_msg}")
            raise exc.SystemSchemaInconsistent(err_msg)

        PARENT_RULES: dict[str, dict[str, tuple[str, ...]]] = {
            "DhwZone": {SZ_ACTUATORS: ("BdrSwitch",), SZ_SENSOR: ("DhwSensor",)},
            "System": {
                SZ_ACTUATORS: ("BdrSwitch", "OtbGateway", "UfhController"),
                SZ_SENSOR: ("OutSensor",),
            },
            "Evohome": {
                SZ_ACTUATORS: ("BdrSwitch", "OtbGateway", "UfhController"),
                SZ_SENSOR: ("OutSensor",),
            },
            "UfhController": {SZ_ACTUATORS: ("UfhCircuit",), SZ_SENSOR: ()},
            "Zone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "EleZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "MixZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "RadZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "UfhZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "ValZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
        }

        rules = PARENT_RULES.get(parent_class)
        if not rules:
            _TRACE.error(f"PARENT RULES EXCEPTION: {parent} is not a valid parent.")
            raise exc.SchemaInconsistentError(
                f"for Parent {parent}: not a valid parent"
            )

        if is_sensor and self_class not in rules[SZ_SENSOR]:
            _TRACE.error(
                f"RULES EXCEPTION: Sensor {self} must be {rules[SZ_SENSOR]} "
                f"for parent {parent}"
            )
            raise exc.SchemaInconsistentError(
                f"for Parent {parent}: Sensor {self} must be {rules[SZ_SENSOR]}"
            )
        if not is_sensor and self_class not in rules[SZ_ACTUATORS]:
            _TRACE.error(
                f"RULES EXCEPTION: Actuator {self} must be {rules[SZ_ACTUATORS]} "
                f"for parent {parent}"
            )
            raise exc.SchemaInconsistentError(
                f"for Parent {parent}: Actuator {self} must be {rules[SZ_ACTUATORS]}"
            )

        return parent, child_id

    def _apply_topology_link(
        self,
        parent: Parent | None,
        *,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> Parent:
        """Establish a topological link to a parent entity.

        This is a protected method that MUST only be called by the
        DeviceRegistry when processing a validated TopologyChangedEvent.

        :param parent: The parent to link to.
        :type parent: Parent | None
        :param child_id: The specific sub-index.
        :type child_id: str | None
        :param is_sensor: Whether this child is a sensor.
        :type is_sensor: bool | None
        :returns: The validated parent entity.
        :rtype: Parent
        :raises SystemSchemaInconsistent: If a controller conflict occurs.
        """
        try:
            parent, child_id = self._get_parent(
                parent, child_id=child_id, is_sensor=is_sensor
            )
            ctl = (
                parent
                if parent.__class__.__name__ == "UfhController"
                else getattr(parent, "ctl", None)
            )

            this_entity = cast("Entity", self)

            if this_entity.ctl and this_entity.ctl is not ctl:
                raise exc.SystemSchemaInconsistent(
                    f"{self} can't change controller: {this_entity.ctl} to {ctl}"
                )

            parent._add_child(self, child_id=child_id, is_sensor=is_sensor)

        except (exc.SchemaInconsistentError, exc.SystemSchemaInconsistent) as err:
            _TRACE.error(f"LINK EXCEPTION: Failed applying link for {self}: {err}")
            raise

        self._child_id = child_id
        self._parent = parent

        this_entity.ctl = cast("Controller", ctl)
        this_entity.tcs = cast("Evohome", getattr(ctl, "tcs", None))

        return parent

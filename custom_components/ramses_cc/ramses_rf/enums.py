"""RAMSES RF - Cross-domain enumerations for the L7 event pipeline."""

from enum import StrEnum


class Topic(StrEnum):
    """Event Bus routing discriminators."""

    RAW_EVENT = "raw_event"
    STATE_UPDATE = "state_update"
    TOPOLOGY_DISCOVERY = "topology_discovery"


class Action(StrEnum):
    """Standardized intents for outbound commands."""

    SET_TEMPERATURE = "set_temperature"
    SET_MODE = "set_mode"


class TopologyAction(StrEnum):
    """Structural graph mutation actions."""

    PROMOTE_CLASS = "promote_class"
    UPDATE_TRAITS = "update_traits"
    BIND_DEVICE = "bind_device"
    CREATE_CONTROLLER = "create_controller"
    CREATE_CIRCUIT = "create_circuit"

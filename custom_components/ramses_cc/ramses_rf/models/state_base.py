"""RAMSES RF - Base state models and events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime as dt
from typing import Any

from ..enums import TopologyAction
from ..typing import DeviceIdT


@dataclass
class DeviceTraits:
    """Strictly typed traits for device instantiation."""

    device_class: str | None = None
    alias: str | None = None
    faked: bool | None = None
    scheme: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceTraits:
        """Construct DeviceTraits safely from a dynamically parsed
        dictionary.
        """
        return cls(
            device_class=data.get("class"),
            alias=data.get("alias"),
            faked=data.get("faked"),
            scheme=data.get("scheme"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to a dictionary.

        Useful for bridging the boundary into legacy methods expecting
        **kwargs.
        """
        result: dict[str, Any] = {}
        if self.device_class is not None:
            result["class"] = getattr(self.device_class, "value", self.device_class)
        if self.alias is not None:
            result["alias"] = self.alias
        if self.faked is not None:
            result["faked"] = self.faked
        if self.scheme is not None:
            result["scheme"] = getattr(self.scheme, "value", self.scheme)
        return result


def _now_utc() -> dt:
    """Return the current timezone-aware UTC datetime.

    This helper function is required for dataclass default factories.
    If `default=dt.now(UTC)` is used directly in a dataclass field,
    Python evaluates the function exactly once when the module is
    imported. As a result, every event instantiated during the
    runtime would share the exact same timestamp. By providing this
    zero-argument callable to `default_factory`, we ensure a fresh,
    accurate timestamp is generated every time an object is created.
    """
    return dt.now(UTC)


@dataclass(frozen=True, slots=True)
class TopologyChangedEvent:
    """Immutable event representing a structural change in the network
    graph.
    """

    # The structural action to perform
    action: TopologyAction

    # -- Entity Identifiers (Populated based on the Action) --

    # Used for single-device actions (e.g., PROMOTE_CLASS, UPDATE_TRAITS)
    device_id: DeviceIdT | None = None

    # Used together for structural relationship actions (e.g., BIND_DEVICE)
    parent_id: DeviceIdT | None = None
    child_id: DeviceIdT | None = None

    # -- Context & Observability --

    # Flexible domain-specific metadata (e.g., {"zone_idx": "01",
    # "is_sensor": True})
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)

    # The Tracing Triad (Observability & Debugging)
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # causation identifies the rule/engine that generated this guess
    # (e.g., "Rule_000C")
    causation: str = "TopologyBuilder"

    timestamp: dt = field(default_factory=_now_utc)


# --- Phase 2.95: CQRS Domain Read-Models and Events ---


@dataclass(frozen=True, slots=True)
class StateUpdatedEvent:
    """An immutable event representing a state update for an entity.

    Includes the OpenTelemetry tracing triad to guarantee perfect
    observability of the event lineage.
    """

    entity_id: str
    state: Any
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    causation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    timestamp: dt = field(default_factory=_now_utc)

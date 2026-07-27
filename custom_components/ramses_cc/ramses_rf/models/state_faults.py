"""RAMSES RF - Stateful Fault Logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as dt

from .state_base import _now_utc

# --- Stateful Fault Logs ---


@dataclass(frozen=True, slots=True)
class FaultLogEntry:
    """An immutable record of a specific system fault."""

    timestamp: str
    fault_state: str  # e.g., "Restore", "Fault"
    fault_type: str
    domain_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class FaultLogState:
    """The immutable state of the system's 64-slot fault log."""

    entries: tuple[FaultLogEntry, ...] = field(default_factory=tuple)
    is_current: bool = False
    last_updated: dt = field(default_factory=_now_utc)

    @property
    def latest_fault(self) -> FaultLogEntry | None:
        """Convenience pointer for API consumers to grab the newest fault."""
        return self.entries[-1] if self.entries else None

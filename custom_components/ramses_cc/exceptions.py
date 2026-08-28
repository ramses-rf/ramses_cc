"""Exception hierarchy for the RAMSES CC integration.

Provides integration exceptions deriving from HomeAssistantError to wrap
lower-level ramses_tx and upper-level ramses_rf library exceptions.
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class RamsesError(HomeAssistantError):  # type: ignore[misc]
    """Base exception class for all RAMSES CC integration errors."""


class RamsesTransportError(RamsesError):
    """Exception raised when a transport or serial port error occurs."""


class RamsesProtocolError(RamsesError):
    """Exception raised when a RAMSES protocol error or timeout occurs."""


class RamsesDeviceError(RamsesError):
    """Exception raised when a RAMSES device error occurs."""


class RamsesBindingError(RamsesError):
    """Exception raised when a RAMSES binding flow fails or times out."""


class RamsesScheduleError(RamsesError):
    """Exception raised when a schedule operation fails or times out."""


class RamsesSchemaError(RamsesError):
    """Exception raised when a schema configuration is inconsistent."""

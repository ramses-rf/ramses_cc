"""Compatibility helpers for probatio/voluptuous marker conversion.

HA Core's ``cv.make_entity_service_schema()`` internally builds a
voluptuous ``Schema``, which recognises dict keys via
``isinstance(key, voluptuous.Required/Optional)``.  probatio markers
(``probatio.markers.Required``, ``probatio.markers.Optional``) are
**different classes**, so voluptuous does not recognise them and raises
``SchemaError: unsupported schema data type 'Required'``.

On HA 2026.9+ ``install_as_voluptuous()`` aliases ``voluptuous`` to
``probatio`` in ``sys.modules``, so the markers are already compatible
and conversion is a no-op.  On pre-2026.9 HA Core the real voluptuous
is in use and probatio markers must be converted.

This module provides :func:`make_entity_service_schema`, a drop-in
replacement for ``cv.make_entity_service_schema`` that handles the
conversion transparently.  It works regardless of whether the caller
imports ``voluptuous`` or ``probatio`` as ``vol``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as _vol  # real voluptuous (or probatio if aliased by HA 2026.9+)
from homeassistant.helpers import config_validation as cv

if TYPE_CHECKING:
    from homeassistant.helpers.service import VolSchemaType

# Sentinel exported by probatio for "no default specified".  When the
# default is UNDEFINED we must *not* pass ``default=`` to the voluptuous
# marker constructor, otherwise voluptuous would treat ``None`` as the
# default value rather than "no default".
try:
    from probatio import UNDEFINED as _PROBATIO_UNDEFINED
except ImportError:  # probatio not installed (pre-2026.9 HA Core without it)
    _PROBATIO_UNDEFINED = object()  # unique sentinel, never matched


def _convert_marker(marker: Any) -> Any:
    """Convert a probatio Required/Optional marker to a voluptuous marker.

    If *marker* is already a voluptuous marker (HA 2026.9+ where
    voluptuous is aliased to probatio, or when the caller still uses
    ``import voluptuous as vol``), it is returned unchanged.

    :param marker: The dict key to convert.
    :type marker: Any
    :returns: A voluptuous-compatible marker.
    :rtype: Any
    """
    # Already a voluptuous marker — no conversion needed.  On HA 2026.9+
    # voluptuous IS probatio (via install_as_voluptuous), so probatio
    # markers pass this check too.
    if isinstance(marker, (_vol.Required, _vol.Optional)):
        return marker

    cls_name = type(marker).__name__
    if cls_name not in ("Required", "Optional"):
        return marker

    # Build kwargs, omitting UNDEFINED defaults so voluptuous uses its
    # own "no default" sentinel (Ellipsis).
    kwargs: dict[str, Any] = {}
    if not _is_undefined(getattr(marker, "default", _PROBATIO_UNDEFINED)):
        kwargs["default"] = marker.default
    desc = getattr(marker, "description", None)
    if desc is not None:
        kwargs["description"] = desc
    msg = getattr(marker, "msg", None)
    if msg is not None:
        kwargs["msg"] = msg

    if cls_name == "Required":
        return _vol.Required(marker.schema, **kwargs)
    return _vol.Optional(marker.schema, **kwargs)


def _is_undefined(value: Any) -> bool:
    """Check whether *value* is a probatio UNDEFINED sentinel."""
    return value is _PROBATIO_UNDEFINED


def make_entity_service_schema(
    schema: dict[str, Any] | None,
    *,
    extra: int = _vol.PREVENT_EXTRA,
) -> VolSchemaType:
    """Drop-in replacement for ``cv.make_entity_service_schema``.

    Converts probatio ``Required``/``Optional`` markers in *schema* to
    voluptuous markers before delegating to
    ``cv.make_entity_service_schema``.  On HA 2026.9+ (where voluptuous
    is aliased to probatio) the conversion is a no-op.

    :param schema: Service schema dict, possibly with probatio markers.
    :type schema: dict[str, Any] | None
    :param extra: Voluptuous extra-keys policy (default: PREVENT_EXTRA).
    :type extra: int
    :returns: Compiled entity service schema.
    :rtype: VolSchemaType
    """
    if not schema:
        return cv.make_entity_service_schema(schema, extra=extra)

    converted: dict[Any, Any] = {
        _convert_marker(key): value for key, value in schema.items()
    }
    return cv.make_entity_service_schema(converted, extra=extra)

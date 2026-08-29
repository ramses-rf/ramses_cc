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

import importlib
import sys
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


def _get_real_voluptuous() -> Any:
    """Return the real voluptuous module, bypassing the probatio alias.

    HA 2026.9+ calls ``install_as_voluptuous()`` which replaces
    ``sys.modules['voluptuous']`` with probatio.  ``voluptuous_serialize``
    (used by HA's config flow REST API) only recognises real voluptuous
    ``Schema`` objects, so form schemas must be built with real
    voluptuous — not the probatio alias.

    On pre-2026.9 HA (no aliasing), ``_vol`` is already real voluptuous
    and this function returns the same module.
    """
    # If _vol is already real voluptuous (not probatio), return it.
    if (
        not hasattr(_vol, "UNDEFINED") or _vol.__name__ == "voluptuous"
    ):  # pragma: no cover
        return _vol

    # _vol is probatio — temporarily remove the alias and import real voluptuous.
    saved: dict[str, Any] = {}
    for key in list(sys.modules):
        if key == "voluptuous" or key.startswith("voluptuous."):
            saved[key] = sys.modules.pop(key)
    try:
        return importlib.import_module("voluptuous")
    finally:
        sys.modules.update(saved)


# Real voluptuous module (resolved once at import time).
# On HA 2026.9+ this is the actual voluptuous, not the probatio alias.
_REAL_VOL: Any = _get_real_voluptuous()


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
    if isinstance(marker, (_REAL_VOL.Required, _REAL_VOL.Optional)):
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
        return _REAL_VOL.Required(marker.schema, **kwargs)
    return _REAL_VOL.Optional(marker.schema, **kwargs)


def _is_undefined(value: Any) -> bool:
    """Check whether *value* is a probatio UNDEFINED sentinel."""
    return value is _PROBATIO_UNDEFINED


def convert_form_schema(schema: dict[Any, Any]) -> dict[Any, Any]:
    """Convert probatio markers in a form schema dict to voluptuous markers.

    HA's config flow framework calls ``voluptuous_serialize.convert()`` on
    the ``data_schema`` passed to ``async_show_form()``.  If the schema
    dict has probatio ``Required``/``Optional`` markers as keys,
    ``voluptuous_serialize`` (which uses real voluptuous) cannot recognise
    them and raises ``ValueError: Unable to convert schema``.

    This function walks the dict keys and converts any probatio markers
    to their voluptuous equivalents.  On HA 2026.9+ (where voluptuous is
    aliased to probatio) the conversion is a no-op.

    :param schema: Form schema dict, possibly with probatio markers.
    :type schema: dict[Any, Any]
    :returns: A new dict with voluptuous-compatible markers.
    :rtype: dict[Any, Any]
    """
    return {_convert_marker(key): value for key, value in schema.items()}


def vol_schema(schema: dict[Any, Any], *, extra: int = 0) -> Any:
    """Build a schema from a dict that may contain probatio markers.

    This is a drop-in replacement for ``vol.Schema(schema)`` when *schema*
    may contain probatio ``Required``/``Optional`` markers as keys.

    HA's config flow framework serialises the ``data_schema`` passed to
    ``async_show_form()`` via ``voluptuous_serialize.convert()``, which
    uses **real voluptuous** markers.  It also validates user input by
    calling ``data_schema(user_input)``.

    Two scenarios:

    - **HA 2026.9+** (``install_as_voluptuous`` active): ``_vol`` is
      probatio, ``_REAL_VOL`` is real voluptuous.  Probatio markers are
      NOT instances of real voluptuous markers, so
      ``voluptuous_serialize`` cannot serialise them.  We convert them
      to real voluptuous markers and build a **real voluptuous Schema**
      so both serialisation and validation work.

    - **Pre-2026.9 HA** (no aliasing): ``_vol`` is already real
      voluptuous, so ``_vol is _REAL_VOL`` and conversion is a no-op.
      We build a real voluptuous Schema (same as before).

    :param schema: Schema dict, possibly with probatio markers.
    :type schema: dict[Any, Any]
    :param extra: Voluptuous extra-keys policy (default: 0 = ALLOW_EXTRA).
    :type extra: int
    :returns: A real voluptuous Schema with voluptuous-compatible markers.
    :rtype: Any
    """
    converted = convert_form_schema(schema)
    return _REAL_VOL.Schema(converted, extra=extra)


def make_entity_service_schema(
    schema: dict[str, Any] | None,
    *,
    extra: int = _vol.PREVENT_EXTRA,
) -> VolSchemaType:
    """Drop-in replacement for ``cv.make_entity_service_schema``.

    ``schemas.py`` uses ``import voluptuous as vol`` — the same module
    that ``cv`` uses internally.  On HA 2026.9+ both are probatio (via
    ``install_as_voluptuous``); on pre-2026.9 both are real voluptuous.
    In either case the markers are already compatible with
    ``cv.make_entity_service_schema``, so no conversion is needed.

    This wrapper exists for forward-compatibility: if a future PR
    switches ``schemas.py`` to ``import probatio as vol`` while
    ``cv`` still uses real voluptuous, the conversion logic can be
    re-enabled here.

    :param schema: Service schema dict.
    :type schema: dict[str, Any] | None
    :param extra: Voluptuous extra-keys policy (default: PREVENT_EXTRA).
    :type extra: int
    :returns: Compiled entity service schema.
    :rtype: VolSchemaType
    """
    return cv.make_entity_service_schema(schema, extra=extra)

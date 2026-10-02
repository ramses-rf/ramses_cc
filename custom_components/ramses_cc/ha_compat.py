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


def _convert_validator(value: Any) -> Any:
    """Recursively convert probatio validators to voluptuous validators.

    On pre-2026.9 HA, voluptuous and probatio are distinct packages.
    ``voluptuous_serialize.convert()`` checks ``isinstance(schema,
    vol.All)`` using **real voluptuous**, so probatio ``All`` /
    ``Coerce`` / ``Range`` etc. are not recognised and raise
    ``ValueError: Unable to convert schema`` (issue 1093).

    This function walks validator trees and rebuilds probatio
    combinators (``All``, ``Any``) and leaf validators (``Coerce``,
    ``Range``, ``Clamp``, ``Length``, ``In``) with real voluptuous.
    HA selectors, plain types, already-voluptuous validators, and
    constants pass through unchanged.

    On HA 2026.9+ (where voluptuous is aliased to probatio) the
    ``isinstance`` guards are all True, so every branch returns
    *value* unchanged — the function is a no-op.

    :param value: A schema value, possibly a probatio validator.
    :type value: Any
    :returns: A voluptuous-compatible validator.
    :rtype: Any
    """
    cls_name = type(value).__name__

    # Combinators — recurse into sub-validators
    if cls_name == "All" and not isinstance(value, _REAL_VOL.All):
        return _REAL_VOL.All(
            *[_convert_validator(v) for v in value.validators],
            msg=getattr(value, "msg", None),
        )
    if cls_name == "Any" and not isinstance(value, _REAL_VOL.Any):
        return _REAL_VOL.Any(
            *[_convert_validator(v) for v in value.validators],
            msg=getattr(value, "msg", None),
        )

    # Leaf validators — reconstruct with real voluptuous
    if cls_name == "Coerce" and not isinstance(value, _REAL_VOL.Coerce):
        return _REAL_VOL.Coerce(value.type, msg=getattr(value, "msg", None))
    if cls_name == "Range" and not isinstance(value, _REAL_VOL.Range):
        return _REAL_VOL.Range(
            min=value.min,
            max=value.max,
            min_included=value.min_included,
            max_included=value.max_included,
            msg=getattr(value, "msg", None),
        )
    if cls_name == "Clamp" and not isinstance(value, _REAL_VOL.Clamp):
        return _REAL_VOL.Clamp(
            min=value.min,
            max=value.max,
            msg=getattr(value, "msg", None),
        )
    if cls_name == "Length" and not isinstance(value, _REAL_VOL.Length):
        return _REAL_VOL.Length(
            min=value.min,
            max=value.max,
            msg=getattr(value, "msg", None),
        )
    if cls_name == "In" and not isinstance(value, _REAL_VOL.In):
        return _REAL_VOL.In(value.container, msg=getattr(value, "msg", None))

    # HA selectors, plain types, voluptuous validators, constants — OK
    return value


def convert_form_schema(schema: dict[Any, Any]) -> dict[Any, Any]:
    """Convert probatio markers and validators in a form schema dict.

    HA's config flow framework calls ``voluptuous_serialize.convert()`` on
    the ``data_schema`` passed to ``async_show_form()``.  If the schema
    dict has probatio ``Required``/``Optional`` markers as keys **or**
    probatio validators (``All``, ``Coerce``, ``Range``, …) as values,
    ``voluptuous_serialize`` (which uses real voluptuous) cannot recognise
    them and raises ``ValueError: Unable to convert schema``.

    This function walks the dict keys (via :func:`_convert_marker`) and
    values (via :func:`_convert_validator`) and converts any probatio
    constructs to their voluptuous equivalents.  On HA 2026.9+ (where
    voluptuous is aliased to probatio) the conversion is a no-op.

    :param schema: Form schema dict, possibly with probatio constructs.
    :type schema: dict[Any, Any]
    :returns: A new dict with voluptuous-compatible markers and validators.
    :rtype: dict[Any, Any]
    """
    return {
        _convert_marker(key): _convert_validator(value)
        for key, value in schema.items()
    }


def vol_schema(schema: dict[Any, Any], *, extra: int = 0) -> Any:
    """Build a schema from a dict that may contain probatio markers.

    This is a drop-in replacement for ``vol.Schema(schema)`` when *schema*
    may contain probatio ``Required``/``Optional`` markers as keys.

    HA's config flow framework serialises the ``data_schema`` passed to
    ``async_show_form()`` via ``voluptuous_serialize.convert()``, which
    uses the active ``vol`` module's markers.  It also validates user
    input by calling ``data_schema(user_input)``.

    Two scenarios:

    - **HA 2026.9+** (``install_as_voluptuous`` active): ``_vol`` is
      probatio.  The schema dict already contains probatio markers
      (``schemas.py`` imports ``probatio as vol``), and
      ``voluptuous_serialize`` also sees probatio via the alias.  We
      build a **probatio Schema** directly — no marker conversion, no
      wrapper.  Serialisation, validation, and error types
      (``probatio.Invalid``) all work natively.

    - **Pre-2026.9 HA** (no aliasing): ``_vol`` is already real
      voluptuous, but the schema dict may contain probatio markers
      and validators.  We convert both to real voluptuous and build a
      real voluptuous Schema so ``voluptuous_serialize`` can serialise
      it.

    :param schema: Schema dict, possibly with probatio markers.
    :type schema: dict[Any, Any]
    :param extra: Voluptuous extra-keys policy (default: 0 = PREVENT_EXTRA).
    :type extra: int
    :returns: A Schema callable that serialises via voluptuous_serialize
        and raises ``_vol.Invalid`` on validation failure.
    :rtype: Any
    """
    # HA 2026.9+: _vol is probatio (via install_as_voluptuous).
    # The schema dict already has probatio markers (schemas.py imports
    # probatio as vol), and voluptuous_serialize also sees probatio.
    # Build a probatio Schema directly — no conversion, no wrapper.
    # This fixes the serialisation failure reported in issue 1087, where
    # HA 2026.9.0b4 could not serialise the previous _SchemaWrapper.
    if _vol is not _REAL_VOL:
        return _vol.Schema(schema, extra=extra)

    # Pre-2026.9 HA: _vol is real voluptuous, but the schema dict may
    # contain probatio markers.  Convert them and build a real voluptuous
    # Schema so voluptuous_serialize can serialise it.
    converted = convert_form_schema(schema)
    return _REAL_VOL.Schema(converted, extra=extra)


def make_entity_service_schema(
    schema: dict[str, Any] | None,
    *,
    extra: int = _vol.PREVENT_EXTRA,
) -> VolSchemaType:
    """Drop-in replacement for ``cv.make_entity_service_schema``.

    ``schemas.py`` uses ``import probatio as vol``, so schema markers
    are probatio markers.  ``cv.make_entity_service_schema`` uses
    ``cv.vol`` internally — on HA 2026.9+ this is also probatio (via
    ``install_as_voluptuous``), so markers are compatible.  On
    pre-2026.9 HA, ``cv.vol`` is real voluptuous, which doesn't
    recognise probatio markers — so we convert them first.

    :param schema: Service schema dict with probatio markers.
    :type schema: dict[str, Any] | None
    :param extra: Voluptuous extra-keys policy (default: PREVENT_EXTRA).
    :type extra: int
    :returns: Compiled entity service schema.
    :rtype: VolSchemaType
    """
    if not schema:
        return cv.make_entity_service_schema(schema, extra=extra)

    # schemas.py uses `import probatio as vol`, so markers are probatio.
    # If cv.vol is also probatio (HA 2026.9+), markers are compatible.
    # If cv.vol is real voluptuous (pre-2026.9), convert probatio markers.
    # HA 2026.10 removed cv.vol entirely — voluptuous is probatio
    # natively, so absence means no conversion needed.
    if getattr(cv, "vol", None) is not _REAL_VOL:
        # cv.vol is probatio or absent — no conversion needed
        return cv.make_entity_service_schema(schema, extra=extra)

    # cv.vol is real voluptuous — convert probatio markers
    converted: dict[Any, Any] = {
        _convert_marker(key): value for key, value in schema.items()
    }
    return cv.make_entity_service_schema(converted, extra=extra)

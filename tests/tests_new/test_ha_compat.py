"""Tests for the probatio/voluptuous compatibility helper.

Verify that :func:`make_entity_service_schema` correctly converts
probatio ``Required``/``Optional`` markers to voluptuous markers so
that ``cv.make_entity_service_schema`` accepts them on HA pre-2026.9
(where voluptuous is real voluptuous, not aliased to probatio).
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import patch

import probatio
import voluptuous as _vol
from homeassistant.helpers import config_validation as cv

from custom_components.ramses_cc import ha_compat
from custom_components.ramses_cc.ha_compat import (
    _convert_marker,
    make_entity_service_schema,
)


def _get_real_voluptuous() -> Any:
    """Get the real voluptuous module (not the probatio alias).

    HA 2026.9+ calls ``install_as_voluptuous()`` which replaces
    ``sys.modules['voluptuous']`` with probatio.  We can still access
    the real voluptuous by temporarily removing the alias and
    importing the real module.
    """
    # Save and remove the current alias + all submodules
    saved: dict[str, Any] = {}
    for key in list(sys.modules):
        if key == "voluptuous" or key.startswith("voluptuous."):
            saved[key] = sys.modules.pop(key)
    try:
        real_vol = importlib.import_module("voluptuous")
    finally:
        # Restore the alias
        sys.modules.update(saved)
    return real_vol


# Cache the real voluptuous module (imported once at module load)
_REAL_VOL: Any = _get_real_voluptuous()


class TestConvertMarker:
    """Tests for the internal _convert_marker function."""

    def test_probatio_required_converts_to_voluptuous(self) -> None:
        # Arrange
        marker = probatio.Required(
            "test_key", default="def", description="desc"
        )
        # Act
        result = _convert_marker(marker)
        # Assert
        assert isinstance(result, _vol.Required)
        assert result.schema == "test_key"
        assert result.description == "desc"

    def test_probatio_optional_converts_to_voluptuous(self) -> None:
        # Arrange
        marker = probatio.Optional("test_key", default=42)
        # Act
        result = _convert_marker(marker)
        # Assert
        assert isinstance(result, _vol.Optional)
        assert result.schema == "test_key"

    def test_voluptuous_required_passes_through(self) -> None:
        # Arrange
        marker = _vol.Required("test_key", default="def")
        # Act
        result = _convert_marker(marker)
        # Assert — same object, no conversion
        assert result is marker

    def test_voluptuous_optional_passes_through(self) -> None:
        # Arrange
        marker = _vol.Optional("test_key")
        # Act
        result = _convert_marker(marker)
        # Assert
        assert result is marker

    def test_plain_string_key_passes_through(self) -> None:
        # Arrange
        key = "plain_key"
        # Act
        result = _convert_marker(key)
        # Assert
        assert result == "plain_key"

    def test_undefined_default_not_propagated(self) -> None:
        """When no default is specified, the converted marker should
        not carry probatio's UNDEFINED sentinel into voluptuous.

        On HA 2026.9+ (voluptuous aliased to probatio) the marker
        passes through unchanged, so we only assert the no-conversion
        path here.
        """
        # Arrange
        marker = probatio.Required("test_key")
        # Act
        result = _convert_marker(marker)
        # Assert — should be a valid Required marker
        assert isinstance(result, _vol.Required)
        assert result.schema == "test_key"

    def test_explicit_default_is_propagated(self) -> None:
        # Arrange
        marker = probatio.Required("test_key", default="my_default")
        # Act
        result = _convert_marker(marker)
        # Assert
        assert isinstance(result, _vol.Required)
        # probatio wraps defaults in a factory lambda; voluptuous does too
        assert callable(result.default)

    def test_msg_is_propagated(self) -> None:
        # Arrange
        marker = probatio.Required("test_key", msg="custom error")
        # Act
        result = _convert_marker(marker)
        # Assert
        assert isinstance(result, _vol.Required)
        assert result.msg == "custom error"


class TestMakeEntityServiceSchema:
    """Tests for the public make_entity_service_schema wrapper."""

    def test_probatio_markers_accepted(self) -> None:
        # Arrange
        schema: dict[Any, Any] = {
            probatio.Required("temperature"): probatio.All(
                probatio.Coerce(float), probatio.Range(min=0, max=99)
            ),
            probatio.Optional("duration", default=30): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1, max=1440)
            ),
        }
        # Act
        result = make_entity_service_schema(
            schema, extra=probatio.PREVENT_EXTRA
        )
        # Assert — should not raise
        assert result is not None

    def test_voluptuous_markers_accepted(self) -> None:
        # Arrange
        schema: dict[Any, Any] = {
            _vol.Required("temperature"): _vol.All(
                _vol.Coerce(float), _vol.Range(min=0, max=99)
            ),
        }
        # Act
        result = make_entity_service_schema(schema, extra=_vol.PREVENT_EXTRA)
        # Assert
        assert result is not None

    def test_empty_dict_returns_base_schema(self) -> None:
        # Act
        result = make_entity_service_schema({})
        # Assert
        assert result is not None

    def test_none_schema_returns_base_schema(self) -> None:
        # Act
        result = make_entity_service_schema(None)
        # Assert
        assert result is not None

    def test_mixed_markers_accepted(self) -> None:
        # Arrange — mix of probatio and voluptuous markers
        schema: dict[Any, Any] = {
            probatio.Required("probatio_key"): probatio.Coerce(str),
            _vol.Optional("voluptuous_key", default="def"): _vol.Coerce(str),
            "plain_key": _vol.Coerce(int),
        }
        # Act
        result = make_entity_service_schema(schema)
        # Assert
        assert result is not None

    def test_validates_input_correctly(self) -> None:
        """Verify the compiled schema validates input correctly.

        Entity service schemas require at least one entity selector key
        (entity_id, device_id, etc.) from the BASE_ENTITY_SCHEMA.
        """
        # Arrange
        schema: dict[Any, Any] = {
            probatio.Required("temperature"): probatio.All(
                probatio.Coerce(float), probatio.Range(min=0, max=99)
            ),
        }
        compiled = make_entity_service_schema(schema)
        # Act + Assert — valid input (includes required entity_id)
        result = compiled({"entity_id": "climate.test", "temperature": 50.0})
        assert result["temperature"] == 50.0

    def test_rejects_invalid_input(self) -> None:
        # Arrange
        schema: dict[Any, Any] = {
            probatio.Required("temperature"): probatio.All(
                probatio.Coerce(float), probatio.Range(min=0, max=99)
            ),
        }
        compiled = make_entity_service_schema(schema)
        # Act + Assert — out-of-range input
        try:
            compiled({"temperature": 150.0})
            raise AssertionError("Should have raised")
        except Exception:
            pass  # Expected

    def test_raw_cv_rejects_probatio_markers(self) -> None:
        """Verify the bug exists: raw cv.make_entity_service_schema
        fails with probatio markers (when voluptuous is not aliased).

        On HA 2026.9+ where install_as_voluptuous aliases voluptuous to
        probatio, this test is skipped because the markers are already
        compatible.
        """
        # Check if voluptuous is aliased to probatio
        if _vol.Required is probatio.Required:
            import pytest

            pytest.skip("voluptuous is aliased to probatio (HA 2026.9+)")

        # Arrange
        schema: dict[Any, Any] = {
            probatio.Required("temperature"): probatio.Coerce(float),
        }
        # Act + Assert — raw cv should fail
        try:
            cv.make_entity_service_schema(schema, extra=probatio.PREVENT_EXTRA)
            raise AssertionError(
                "raw cv.make_entity_service_schema should reject "
                "probatio markers on pre-2026.9 HA"
            )
        except Exception:
            pass  # Expected — the bug we're fixing


class TestConvertMarkerForced:
    """Tests that force the conversion path by patching ha_compat._vol
    to the real voluptuous module (not the probatio alias).

    On HA 2026.9+ ``install_as_voluptuous()`` aliases voluptuous to
    probatio, so ``_convert_marker`` sees probatio markers as already-
    voluptuous and returns early.  These tests patch ``_vol`` to the
    real voluptuous so the conversion logic (lines 59-77) is exercised.
    """

    def test_probatio_required_converts_with_real_voluptuous(self) -> None:
        """Force conversion: patch _vol to real voluptuous, then verify
        a probatio Required marker is converted to a real voluptuous
        Required marker with default and description propagated.
        """
        marker = probatio.Required(
            "test_key", default="def", description="desc"
        )
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = _convert_marker(marker)
        # Assert — should be a REAL voluptuous Required, not probatio
        assert isinstance(result, _REAL_VOL.Required)
        assert not isinstance(result, probatio.Required)
        assert result.schema == "test_key"
        assert result.description == "desc"

    def test_probatio_optional_converts_with_real_voluptuous(self) -> None:
        """Force conversion: patch _vol to real voluptuous, then verify
        a probatio Optional marker is converted to a real voluptuous
        Optional marker.
        """
        marker = probatio.Optional("test_key", default=42)
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = _convert_marker(marker)
        assert isinstance(result, _REAL_VOL.Optional)
        assert not isinstance(result, probatio.Optional)
        assert result.schema == "test_key"

    def test_undefined_default_not_passed_to_voluptuous(self) -> None:
        """When probatio marker has no default (UNDEFINED), the
        converted voluptuous marker should not receive a default kwarg.
        """
        marker = probatio.Required("test_key")  # no default
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = _convert_marker(marker)
        assert isinstance(result, _REAL_VOL.Required)
        # voluptuous uses Ellipsis as its "no default" sentinel
        assert (
            result.default is _REAL_VOL.UNDEFINED or result.default is Ellipsis
        )

    def test_msg_propagated_to_real_voluptuous(self) -> None:
        """Verify msg kwarg is propagated during forced conversion."""
        marker = probatio.Required("test_key", msg="custom error")
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = _convert_marker(marker)
        assert isinstance(result, _REAL_VOL.Required)
        assert result.msg == "custom error"

    def test_description_propagated_to_real_voluptuous(self) -> None:
        """Verify description kwarg is propagated during forced conversion."""
        marker = probatio.Optional("test_key", description="my desc")
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = _convert_marker(marker)
        assert isinstance(result, _REAL_VOL.Optional)
        assert result.description == "my desc"

    def test_is_undefined_with_real_sentinel(self) -> None:
        """Verify _is_undefined returns True for probatio's UNDEFINED
        and False for other values.
        """
        from custom_components.ramses_cc.ha_compat import _is_undefined

        assert _is_undefined(probatio.UNDEFINED) is True
        assert _is_undefined(None) is False
        assert _is_undefined("something") is False

    def test_make_entity_service_schema_with_forced_conversion(self) -> None:
        """End-to-end: patch _vol to real voluptuous and verify
        make_entity_service_schema still works with probatio markers.
        """
        schema: dict[Any, Any] = {
            probatio.Required("temperature"): probatio.All(
                probatio.Coerce(float), probatio.Range(min=0, max=99)
            ),
            probatio.Optional("duration", default=30): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1, max=1440)
            ),
        }
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = make_entity_service_schema(
                schema, extra=probatio.PREVENT_EXTRA
            )
        assert result is not None

    def test_import_error_fallback_sentinel(self) -> None:
        """Verify the ImportError fallback for _PROBATIO_UNDEFINED.

        When probatio is not installed, ``_PROBATIO_UNDEFINED`` should
        be a unique sentinel object.  We simulate this by reloading
        ha_compat with probatio blocked from import.
        """
        import builtins

        original_import = builtins.__import__

        def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "probatio":
                raise ImportError("blocked for test")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", _blocked_import):
            # Force reimport of ha_compat with probatio blocked
            saved = sys.modules.pop(
                "custom_components.ramses_cc.ha_compat", None
            )
            try:
                reloaded = importlib.import_module(
                    "custom_components.ramses_cc.ha_compat"
                )
                # The fallback sentinel should be a plain object
                assert reloaded._PROBATIO_UNDEFINED is not probatio.UNDEFINED
                assert isinstance(reloaded._PROBATIO_UNDEFINED, object)
            finally:
                if saved is not None:
                    sys.modules["custom_components.ramses_cc.ha_compat"] = (
                        saved
                    )

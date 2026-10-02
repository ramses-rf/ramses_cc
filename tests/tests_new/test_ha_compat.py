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
import pytest
import voluptuous as _vol
from homeassistant.helpers import config_validation as cv

from custom_components.ramses_cc import ha_compat
from custom_components.ramses_cc.ha_compat import (
    _REAL_VOL,
    _convert_marker,
    _convert_validator,
    convert_form_schema,
    make_entity_service_schema,
    vol_schema,
)


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
        assert isinstance(result, _REAL_VOL.Required)
        assert result.schema == "test_key"
        assert result.description == "desc"

    def test_probatio_optional_converts_to_voluptuous(self) -> None:
        # Arrange
        marker = probatio.Optional("test_key", default=42)
        # Act
        result = _convert_marker(marker)
        # Assert
        assert isinstance(result, _REAL_VOL.Optional)
        assert result.schema == "test_key"

    def test_voluptuous_required_passes_through(self) -> None:
        # Arrange — use _REAL_VOL (real voluptuous) marker
        marker = _REAL_VOL.Required("test_key", default="def")
        # Act
        result = _convert_marker(marker)
        # Assert — same object, no conversion
        assert result is marker

    def test_voluptuous_optional_passes_through(self) -> None:
        # Arrange — use _REAL_VOL (real voluptuous) marker
        marker = _REAL_VOL.Optional("test_key")
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
        assert isinstance(result, _REAL_VOL.Required)
        assert result.schema == "test_key"

    def test_explicit_default_is_propagated(self) -> None:
        # Arrange
        marker = probatio.Required("test_key", default="my_default")
        # Act
        result = _convert_marker(marker)
        # Assert
        assert isinstance(result, _REAL_VOL.Required)
        # probatio wraps defaults in a factory lambda; voluptuous does too
        assert callable(result.default)

    def test_msg_is_propagated(self) -> None:
        # Arrange
        marker = probatio.Required("test_key", msg="custom error")
        # Act
        result = _convert_marker(marker)
        # Assert
        assert isinstance(result, _REAL_VOL.Required)
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


class TestConvertFormSchema:
    """Tests for convert_form_schema."""

    def test_converts_probatio_markers(self) -> None:
        """Verify probatio markers in a form schema dict are converted."""
        schema: dict[Any, Any] = {
            probatio.Required("key1"): str,
            probatio.Optional("key2", default="def"): int,
        }
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = convert_form_schema(schema)
        # Keys should be real voluptuous markers
        assert all(
            isinstance(k, (_REAL_VOL.Required, _REAL_VOL.Optional))
            for k in result
        )

    def test_passthrough_when_already_voluptuous(self) -> None:
        """When voluptuous is aliased to probatio (HA 2026.9+),
        markers pass through unchanged.
        """
        schema: dict[Any, Any] = {
            probatio.Required("key1"): str,
        }
        result = convert_form_schema(schema)
        # No conversion needed — markers are already voluptuous-compatible
        assert list(result.keys()) == list(schema.keys())

    def test_plain_string_keys_pass_through(self) -> None:
        """Plain string keys should pass through unchanged."""
        schema: dict[Any, Any] = {"key1": str, "key2": int}
        result = convert_form_schema(schema)
        assert result == schema


class TestVolSchema:
    """Tests for vol_schema."""

    def test_builds_schema_from_probatio_markers(self) -> None:
        """Verify vol_schema builds a working Schema from probatio markers."""
        schema: dict[Any, Any] = {
            probatio.Required("key1", default="def"): str,
        }
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = vol_schema(schema)
        # Should be a voluptuous Schema, not probatio
        assert callable(result)

    def test_serializable_by_voluptuous_serialize(self) -> None:
        """The key test: vol_schema output must be serializable by
        voluptuous_serialize.convert() (used by HA config flow REST API).

        On HA 2026.9+ where voluptuous is aliased to probatio,
        voluptuous_serialize and cv.custom_serializer use probatio's
        UNSUPPORTED sentinel.  If probatio's UNSUPPORTED doesn't match
        voluptuous_serialize's UNSUPPORTED, the conversion fails — this
        is a probatio bug, not a ramses_cc bug, so we skip the test.
        """
        try:
            import voluptuous_serialize  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            import pytest

            pytest.skip("voluptuous_serialize not installed")

        # Check if probatio's UNSUPPORTED matches voluptuous_serialize's
        if hasattr(probatio, "UNSUPPORTED"):
            if probatio.UNSUPPORTED is not voluptuous_serialize.UNSUPPORTED:
                import pytest

                pytest.skip(
                    "probatio's UNSUPPORTED sentinel doesn't match "
                    "voluptuous_serialize's (probatio bug)"
                )

        from homeassistant.helpers import selector

        schema: dict[Any, Any] = {
            probatio.Required("lost_04:150003", default="keep"): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "keep", "label": "Keep"},
                            {"value": "remove", "label": "Remove"},
                        ],
                    )
                )
            ),
        }
        compiled = vol_schema(schema)

        # voluptuous_serialize should be able to convert this
        from homeassistant.helpers import config_validation as cv

        result = voluptuous_serialize.convert(
            compiled, custom_serializer=cv.custom_serializer
        )
        assert isinstance(result, list)
        assert any(item.get("name") == "lost_04:150003" for item in result)

    def test_empty_dict(self) -> None:
        """vol_schema with empty dict should work."""
        result = vol_schema({})
        assert callable(result)

    def test_probatio_path_returns_probatio_schema(self) -> None:
        """On HA 2026.9+ (_vol is probatio), vol_schema must return a
        probatio.Schema directly — not a wrapper.

        This is the fix for issue 1087: HA 2026.9.0b4 tightened schema
        serialisation and could not serialise the previous
        _SchemaWrapper.  Returning a native probatio.Schema makes
        isinstance checks and voluptuous_serialize work natively.
        """
        schema: dict[Any, Any] = {
            probatio.Required("key1", default="def"): str,
        }
        # Simulate HA 2026.9+: _vol is probatio, _REAL_VOL is real voluptuous
        with patch.object(ha_compat, "_vol", probatio):
            result = vol_schema(schema)
        # Must be a real probatio.Schema, not a wrapper
        assert isinstance(result, probatio.Schema), (
            f"Expected probatio.Schema, got {type(result).__name__}"
        )
        # Validation must raise probatio.Invalid natively
        assert result({"key1": "value"}) == {"key1": "value"}
        with pytest.raises(probatio.Invalid):
            result({"key1": 123})  # wrong type: int instead of str

    def test_probatio_path_serializable(self) -> None:
        """On HA 2026.9+, vol_schema output must be serializable by
        voluptuous_serialize.convert().

        This is the regression test for issue 1087.
        """
        try:
            import voluptuous_serialize  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            import pytest

            pytest.skip("voluptuous_serialize not installed")

        # Check if probatio's UNSUPPORTED matches voluptuous_serialize's
        if hasattr(probatio, "UNSUPPORTED"):
            if probatio.UNSUPPORTED is not voluptuous_serialize.UNSUPPORTED:
                import pytest

                pytest.skip(
                    "probatio's UNSUPPORTED sentinel doesn't match "
                    "voluptuous_serialize's (probatio bug)"
                )

        from homeassistant.helpers import selector

        schema: dict[Any, Any] = {
            probatio.Required("lost_04:150003", default="keep"): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "keep", "label": "Keep"},
                            {"value": "remove", "label": "Remove"},
                        ],
                    )
                )
            ),
        }
        # Simulate HA 2026.9+: _vol is probatio
        with patch.object(ha_compat, "_vol", probatio):
            compiled = vol_schema(schema)

        # voluptuous_serialize should be able to convert this
        result = voluptuous_serialize.convert(
            compiled, custom_serializer=cv.custom_serializer
        )
        assert isinstance(result, list)
        assert any(item.get("name") == "lost_04:150003" for item in result)


class TestConvertValidator:
    """Tests for the internal _convert_validator function.

    These tests verify that probatio validators (All, Coerce, Range,
    etc.) are converted to their voluptuous equivalents on pre-2026.9
    HA, and pass through unchanged on HA 2026.9+.
    """

    def test_probatio_all_converts_to_voluptuous(self) -> None:
        # Arrange
        validator = probatio.All(
            probatio.Coerce(int), probatio.Range(min=0, max=10)
        )
        # Act
        result = _convert_validator(validator)
        # Assert
        assert isinstance(result, _REAL_VOL.All)
        assert not isinstance(result, probatio.All)
        assert len(result.validators) == 2

    def test_probatio_coerce_converts_to_voluptuous(self) -> None:
        # Arrange
        validator = probatio.Coerce(int)
        # Act
        result = _convert_validator(validator)
        # Assert
        assert isinstance(result, _REAL_VOL.Coerce)
        assert not isinstance(result, probatio.Coerce)
        assert result.type is int

    def test_probatio_range_converts_to_voluptuous(self) -> None:
        # Arrange
        validator = probatio.Range(min=0, max=99)
        # Act
        result = _convert_validator(validator)
        # Assert
        assert isinstance(result, _REAL_VOL.Range)
        assert not isinstance(result, probatio.Range)
        assert result.min == 0
        assert result.max == 99

    def test_probatio_any_converts_to_voluptuous(self) -> None:
        # Arrange
        validator = probatio.Any(probatio.Coerce(int), probatio.Coerce(float))
        # Act
        result = _convert_validator(validator)
        # Assert
        assert isinstance(result, _REAL_VOL.Any)
        assert not isinstance(result, probatio.Any)
        assert len(result.validators) == 2

    def test_voluptuous_all_passes_through(self) -> None:
        # Arrange — already a real voluptuous All
        validator = _REAL_VOL.All(
            _REAL_VOL.Coerce(int), _REAL_VOL.Range(min=0)
        )
        # Act
        result = _convert_validator(validator)
        # Assert — same object, no conversion
        assert result is validator

    def test_voluptuous_coerce_passes_through(self) -> None:
        # Arrange
        validator = _REAL_VOL.Coerce(int)
        # Act
        result = _convert_validator(validator)
        # Assert
        assert result is validator

    def test_plain_type_passes_through(self) -> None:
        # Arrange
        # Act + Assert
        assert _convert_validator(int) is int
        assert _convert_validator(str) is str
        assert _convert_validator(float) is float

    def test_plain_value_passes_through(self) -> None:
        # Arrange
        # Act + Assert
        assert _convert_validator("hello") == "hello"
        assert _convert_validator(42) == 42

    def test_ha_selector_passes_through(self) -> None:
        # Arrange
        from homeassistant.helpers import selector

        sel = selector.TextSelector()
        # Act
        result = _convert_validator(sel)
        # Assert — same object, selectors are not vol/prob validators
        assert result is sel

    def test_nested_probatio_all_recurses(self) -> None:
        # Arrange — prob.All containing prob.All containing prob.Coerce
        validator = probatio.All(
            probatio.All(probatio.Coerce(int), probatio.Range(min=0, max=10)),
            probatio.Coerce(float),
        )
        # Act
        result = _convert_validator(validator)
        # Assert — outer is voluptuous All
        assert isinstance(result, _REAL_VOL.All)
        # First sub-validator is also converted to voluptuous All
        assert isinstance(result.validators[0], _REAL_VOL.All)
        # Its sub-validators are voluptuous Coerce and Range
        assert isinstance(result.validators[0].validators[0], _REAL_VOL.Coerce)
        assert isinstance(result.validators[0].validators[1], _REAL_VOL.Range)
        # Second sub-validator is voluptuous Coerce
        assert isinstance(result.validators[1], _REAL_VOL.Coerce)

    def test_probatio_all_with_cv_positive_int(self) -> None:
        """Reproduce the exact gateway-config schema from issue 1093:
        prob.All(NumberSelector, cv.positive_int).

        On pre-2026.9 HA, cv.positive_int is already real voluptuous
        (vol.All(Coerce(int), Range(min=0))) and passes through.  On
        2026.9+ (or dev venvs with install_as_voluptuous), cv.positive_int
        is a probatio.All and gets converted to vol.All.  Either way,
        the result sub-validator must be a real voluptuous All.
        """
        from homeassistant.helpers import selector

        # Arrange
        validator = probatio.All(
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=600,
                    unit_of_measurement="seconds",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            cv.positive_int,
        )
        # Act
        result = _convert_validator(validator)
        # Assert — outer is voluptuous All
        assert isinstance(result, _REAL_VOL.All)
        # First sub-validator is the HA selector (passes through)
        assert result.validators[0] is validator.validators[0]
        # Second sub-validator is a real voluptuous All (either passed
        # through from cv.positive_int or converted from probatio.All)
        assert isinstance(result.validators[1], _REAL_VOL.All)


class TestVolSchemaIssue1093:
    """Regression tests for issue 1093: gateway config form schema
    fails to serialise on pre-2026.9 HA because probatio All/Coerce
    validators in the schema values are not recognised by
    voluptuous_serialize (which uses real voluptuous).
    """

    def test_gateway_config_schema_serializes(self) -> None:
        """Reproduce the exact gateway-config form schema from
        config_flow.async_step_config() and verify it serialises via
        voluptuous_serialize without ValueError.

        This is the core regression test for issue 1093.  On pre-2026.9
        HA, the schema values contain prob.All(NumberSelector,
        cv.positive_int) which voluptuous_serialize could not convert
        before the fix.

        This test forces the pre-2026.9 path (patching _vol to
        _REAL_VOL) and requires voluptuous_serialize to also use real
        voluptuous.  On 2026.9+-like envs (where install_as_voluptuous
        is active), voluptuous_serialize sees probatio and cannot
        serialise a real-voluptuous Schema, so we skip.
        """
        try:
            import voluptuous_serialize  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            pytest.skip("voluptuous_serialize not installed")

        # Skip if voluptuous_serialize sees probatio (2026.9+-like env)
        if voluptuous_serialize.vol is not _REAL_VOL:
            pytest.skip(
                "voluptuous_serialize uses probatio (2026.9+-like env)"
            )

        from homeassistant.helpers import selector

        # Reproduce the gateway-config form schema (simplified)
        data_schema: dict[Any, Any] = {
            probatio.Required("scan_interval", default=60): probatio.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=600,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
            probatio.Required("gateway_timeout", default=10): probatio.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=60,
                        unit_of_measurement="minutes",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
        }

        # Force the pre-2026.9 path (vol_schema converts probatio → vol)
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            compiled = vol_schema(data_schema, extra=probatio.PREVENT_EXTRA)

        # Must not raise ValueError
        result = voluptuous_serialize.convert(
            compiled, custom_serializer=cv.custom_serializer
        )
        assert isinstance(result, list)
        names = [item.get("name") for item in result]
        assert "scan_interval" in names
        assert "gateway_timeout" in names

    def test_packet_log_schema_serializes(self) -> None:
        """Reproduce the packet-log form schema which uses
        prob.All(NumberSelector, prob.Coerce(int)) — both the outer
        All and the inner Coerce are probatio and need conversion.

        Skips on 2026.9+-like envs (see test_gateway_config_schema_serializes
        for rationale).
        """
        try:
            import voluptuous_serialize  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            pytest.skip("voluptuous_serialize not installed")

        # Skip if voluptuous_serialize sees probatio (2026.9+-like env)
        if voluptuous_serialize.vol is not _REAL_VOL:
            pytest.skip(
                "voluptuous_serialize uses probatio (2026.9+-like env)"
            )

        from homeassistant.helpers import selector

        data_schema: dict[Any, Any] = {
            probatio.Optional(
                "packet_log_retention_days", default=7
            ): probatio.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="days",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                probatio.Coerce(int),
            ),
            probatio.Optional("rotate_bytes"): probatio.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        unit_of_measurement="bytes",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                probatio.Coerce(int),
            ),
            probatio.Optional("flush_interval", default=60.0): probatio.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        step=0.1,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                probatio.Coerce(float),
            ),
        }

        with patch.object(ha_compat, "_vol", _REAL_VOL):
            compiled = vol_schema(data_schema, extra=probatio.PREVENT_EXTRA)

        result = voluptuous_serialize.convert(
            compiled, custom_serializer=cv.custom_serializer
        )
        assert isinstance(result, list)
        names = [item.get("name") for item in result]
        assert "packet_log_retention_days" in names
        assert "rotate_bytes" in names
        assert "flush_interval" in names

    def test_gateway_config_schema_validates_input(self) -> None:
        """Verify the converted schema still validates user input
        correctly — the fix must not break validation.
        """
        from homeassistant.helpers import selector

        data_schema: dict[Any, Any] = {
            probatio.Required("scan_interval", default=60): probatio.All(
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=600,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                cv.positive_int,
            ),
        }

        with patch.object(ha_compat, "_vol", _REAL_VOL):
            compiled = vol_schema(data_schema, extra=probatio.PREVENT_EXTRA)

        # Valid input
        result = compiled({"scan_interval": 60})
        assert result["scan_interval"] == 60

    def test_convert_form_schema_converts_values(self) -> None:
        """Verify convert_form_schema now converts both keys AND values."""
        from homeassistant.helpers import selector

        schema: dict[Any, Any] = {
            probatio.Required("key"): probatio.All(
                selector.NumberSelector(selector.NumberSelectorConfig(min=0)),
                probatio.Coerce(int),
            ),
        }
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            result = convert_form_schema(schema)

        # Key should be voluptuous Required
        key = list(result.keys())[0]
        assert isinstance(key, _REAL_VOL.Required)

        # Value should be voluptuous All with voluptuous Coerce
        value = list(result.values())[0]
        assert isinstance(value, _REAL_VOL.All)
        assert isinstance(value.validators[1], _REAL_VOL.Coerce)
        assert not isinstance(value.validators[1], probatio.Coerce)

    def test_vol_schema_raises_voluptuous_invalid_on_pre_2026_9(self) -> None:
        """On pre-2026.9 HA, vol_schema returns a real voluptuous Schema
        that raises voluptuous.Invalid — NOT probatio.Invalid.

        config_flow.py catches both types (``except (prob.Invalid,
        _REAL_VOL.Invalid)``) so validation errors are surfaced as form
        errors regardless of HA version.  This test documents the
        exception type so the catch site is not accidentally narrowed.
        """
        data_schema: dict[Any, Any] = {
            probatio.Required("value", default=5): probatio.All(
                probatio.Coerce(int), probatio.Range(min=0, max=10)
            ),
        }
        with patch.object(ha_compat, "_vol", _REAL_VOL):
            compiled = vol_schema(data_schema, extra=probatio.PREVENT_EXTRA)

        # Out-of-range input must raise voluptuous.Invalid on the
        # pre-2026.9 path (real voluptuous Schema + converted validators)
        with pytest.raises(_REAL_VOL.Invalid):
            compiled({"value": 999})

    def test_convert_validator_clamp_length_in(self) -> None:
        """Verify _convert_validator converts Clamp, Length, and In probatio validators."""
        clamp_val = probatio.Clamp(min=1, max=10)
        length_val = probatio.Length(min=2, max=5)
        in_val = probatio.In([1, 2, 3])

        conv_clamp = _convert_validator(clamp_val)
        conv_length = _convert_validator(length_val)
        conv_in = _convert_validator(in_val)

        assert isinstance(conv_clamp, _REAL_VOL.Clamp)
        assert isinstance(conv_length, _REAL_VOL.Length)
        assert isinstance(conv_in, _REAL_VOL.In)

    def test_make_entity_service_schema_pre_2026_9(self) -> None:
        """Verify make_entity_service_schema converts markers when cv.vol is real voluptuous."""
        schema = {
            probatio.Required("command"): str,
            probatio.Optional("value"): int,
        }
        with (
            # create=True: cv.vol was removed in HA 2026.10
            patch.object(cv, "vol", _REAL_VOL, create=True),
            patch(
                "homeassistant.helpers.config_validation.make_entity_service_schema",
                return_value="mock_schema",
            ) as mock_make,
        ):
            result = make_entity_service_schema(schema)
            assert result == "mock_schema"
            mock_make.assert_called_once()
            called_dict = mock_make.call_args[0][0]
            # Verify keys are real voluptuous markers
            for key in called_dict:
                assert isinstance(
                    key, (_REAL_VOL.Required, _REAL_VOL.Optional)
                )

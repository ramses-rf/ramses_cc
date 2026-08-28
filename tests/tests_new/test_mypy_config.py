"""Meta-tests for the ramses_cc mypy configuration.

Verify that the Wave 0 type-safety flags (issue 967) are enabled in
pyproject.toml. These tests guard against accidental removal of the
strictness flags that the type-ignore suppressions depend on.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def _load_mypy_config() -> dict[str, object]:
    """Load the [tool.mypy] section from pyproject.toml."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return data["tool"]["mypy"]  # type: ignore[no-any-return]


class TestMypyStrictnessFlags:
    """Verify Wave 0 strictness flags are enabled."""

    def test_disallow_subclassing_any(self) -> None:
        """PR 3: disallow_subclassing_any must be enabled.

        Without this flag, the # type: ignore[misc] suppressions on
        HA entity subclasses become unused-ignore errors.
        """
        config = _load_mypy_config()
        assert config.get("disallow_subclassing_any") is True

    def test_disallow_untyped_decorators(self) -> None:
        """PR 4: disallow_untyped_decorators must be enabled.

        Without this flag, the # type: ignore[untyped-decorator]
        suppressions on HA @callback decorators become unused-ignore
        errors.
        """
        config = _load_mypy_config()
        assert config.get("disallow_untyped_decorators") is True

    def test_disallow_any_decorated(self) -> None:
        """PR 4: disallow_any_decorated must be enabled.

        This flag triggers [misc] 'Function is untyped after decorator
        transformation' on the def line, which is suppressed alongside
        the [untyped-decorator] on the decorator line.
        """
        config = _load_mypy_config()
        assert config.get("disallow_any_decorated") is True

    def test_warn_redundant_casts(self) -> None:
        """Ensure warn_redundant_casts is enabled (guards PR 1 casts)."""
        config = _load_mypy_config()
        assert config.get("warn_redundant_casts") is True

    def test_warn_unused_ignores(self) -> None:
        """Ensure warn_unused_ignores is enabled for source files."""
        config = _load_mypy_config()
        assert config.get("warn_unused_ignores") is True

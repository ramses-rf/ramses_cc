"""Tests for RAMSES_CC integration helper APIs."""

from __future__ import annotations

from custom_components.ramses_cc.schemas import deep_merge


def test_deep_merge() -> None:
    XXX: dict[str, int | dict[str, int]] = {"a": 10, "b": 20}
    YYY: dict[str, int | dict[str, int]] = {"a": 11, "c": 31}

    # flat dicts: equivalent to the | operator (second arg loses ties)
    assert deep_merge(XXX, YYY) == YYY | XXX
    assert deep_merge(YYY, XXX) == XXX | YYY

    XXX = {"a": 10, "b": 20, "x": {"a": 70, "b": 80}}
    YYY = {"a": 11, "c": 31, "x": {"a": 71, "c": 91}}

    assert deep_merge(XXX, YYY) == {
        "a": 10,
        "c": 31,
        "x": {"a": 70, "c": 91, "b": 80},
        "b": 20,
    }
    assert deep_merge(YYY, XXX) == {
        "a": 11,
        "b": 20,
        "x": {"a": 71, "b": 80, "c": 91},
        "c": 31,
    }

    ZZZ = {"a": 10, "b": 20, "x": None}

    # assert deep_merge(x, z) == {}
    assert deep_merge(ZZZ, XXX) == {"a": 10, "b": 20, "x": None}

#!/usr/bin/env python3
"""RAMSES RF - Helper functions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from inspect import iscoroutinefunction
from typing import Any, TypeAlias

_SchemaT: TypeAlias = dict[str, Any]


def is_subset(inner: _SchemaT, outer: _SchemaT) -> bool:
    """Return True is one dict (or list) is a subset of another."""

    def _is_subset(
        a: dict[str, Any] | list[Any] | Any, b: dict[str, Any] | list[Any] | Any
    ) -> bool:
        if isinstance(a, dict):
            return isinstance(b, dict) and all(
                k in b and _is_subset(v, b[k]) for k, v in a.items()
            )
        if isinstance(a, list):
            return isinstance(b, list) and all(
                any(_is_subset(x, y) for y in b) for x in a
            )
        return bool(a == b)

    return _is_subset(inner, outer)


def deep_merge(src: _SchemaT, dst: _SchemaT, _dc: bool = False) -> _SchemaT:
    """Deep merge a src dict (precedent) into a dst dict and return the result.

    run me with nosetests --with-doctest file.py

    >>>            s = {'data': {'rows': {'pass': 'dog',                'num': '1'}}}
    >>>            d = {'data': {'rows': {               'fail': 'cat', 'num': '5'}}}
    >>> merge(s, d) == {'data': {'rows': {'pass': 'dog', 'fail': 'cat', 'num': '1'}}}
    True
    """

    new_dst = dst if _dc else deepcopy(dst)  # start with copy of dst, merge src into it
    for key, value in src.items():  # values are only: dict, list, value or None
        if isinstance(value, dict):  # is dict
            node = new_dst.setdefault(key, {})  # get node or create one
            deep_merge(value, node, _dc=True)

        elif not isinstance(value, list):  # is value
            new_dst[key] = value  # src takes precedence, assert will fail

        elif key not in new_dst or not isinstance(new_dst[key], list):  # is list
            new_dst[key] = src[key]  # not expected, but maybe

        else:
            new_dst[key] = list(set(src[key] + new_dst[key]))  # will sort

    # assert _is_subset(shrink(src), shrink(new_dst))
    return new_dst


def shrink(
    value: _SchemaT, keep_falsys: bool = False, keep_hints: bool = False
) -> _SchemaT:
    """Return a minimized dict, after removing all the meaningless items.

    Specifically, removes items with:
    - unwanted keys (starting with '_')
    - falsey values
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: walk(v)
                for k, v in node.items()
                if (keep_hints or k[:1] != "_") and (keep_falsys or walk(v))
            }
        elif isinstance(node, list):
            try:
                return sorted([walk(x) for x in node if x])
            except TypeError:  # if a list of dicts
                return [walk(x) for x in node if x]
        else:
            return node

    if not isinstance(value, dict):
        raise TypeError("value is not a dict")

    result: _SchemaT = walk(value)
    return result


def schedule_task(
    fnc: Awaitable[Any] | Callable[..., Any],
    *args: Any,
    delay: float | None = None,
    period: float | None = None,
    **kwargs: Any,
) -> asyncio.Task[Any]:
    """Start a coro after delay seconds."""

    async def execute_fnc(
        fnc: Awaitable[Any] | Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        if iscoroutinefunction(fnc):  # Awaitable, else Callable
            return await fnc(*args, **kwargs)
        return fnc(*args, **kwargs)  # type: ignore[operator]

    async def schedule_fnc(
        fnc: Awaitable[Any] | Callable[..., Any],
        delay: float | None,
        period: float | None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if delay:
            await asyncio.sleep(delay)

        if not period:
            await execute_fnc(fnc, *args, **kwargs)
            return

        while period:
            await execute_fnc(fnc, *args, **kwargs)
            await asyncio.sleep(period)

    return asyncio.create_task(  # do we need to pass in an event loop?
        schedule_fnc(fnc, delay, period, *args, **kwargs), name=str(fnc)
    )

"""Registry for RAMSES RF payload parsers.

This module provides a strictly-typed registration system for payload
parsers, replacing the dynamic locals() inspection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# A type alias for parser functions. They accept a payload string and a
# Message object, returning various mappings or typed dictionaries.
ParserFunc = Callable[..., Any]

_PAYLOAD_PARSERS: dict[str, ParserFunc] = {}


def register_parser(code: str) -> Callable[[ParserFunc], ParserFunc]:
    """Register a parser function for a specific packet code.

    :param code: The 4-character packet code.
    :type code: str
    :return: A decorator that registers the function.
    :rtype: Callable[[ParserFunc], ParserFunc]
    """

    def decorator(func: ParserFunc) -> ParserFunc:
        """Decorate and register the parser function.

        :param func: The parser function to register.
        :type func: ParserFunc
        :return: The unmodified parser function.
        :rtype: ParserFunc
        """
        _PAYLOAD_PARSERS[code.upper()] = func
        return func

    return decorator


def get_parser(code: str) -> ParserFunc | None:
    """Retrieve a registered parser function by packet code.

    :param code: The 4-character packet code to look up.
    :type code: str
    :return: The registered parser function, or None if not found.
    :rtype: ParserFunc | None
    """
    return _PAYLOAD_PARSERS.get(code.upper())

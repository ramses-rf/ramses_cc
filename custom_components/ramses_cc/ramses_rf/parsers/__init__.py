"""RAMSES RF - Payload Parsers Package.

This package provides strictly-typed, domain-specific parsers and a
robust decoding pipeline for RAMSES RF radio packets.
"""

from .dhw import *  # noqa: F403
from .heating import *  # noqa: F403
from .hvac import *  # noqa: F403
from .opentherm import *  # noqa: F403
from .pipeline import PayloadDecoderPipeline
from .registry import get_parser, register_parser
from .system import *  # noqa: F403

__all__ = [
    "PayloadDecoderPipeline",
    "get_parser",
    "register_parser",
]

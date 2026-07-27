"""RAMSES RF - Domain-specific type definitions."""

from datetime import timedelta as td
from typing import Any, TypeAlias, TypedDict

from ..ramses_tx.typing import DeviceIdT as TxDeviceIdT, DevIndexT as TxIndexT

DeviceIdT: TypeAlias = TxDeviceIdT
IndexT: TypeAlias = TxIndexT

DeviceTraitsT: TypeAlias = dict[str, Any]
DeviceListT: TypeAlias = dict[DeviceIdT, DeviceTraitsT]


# For fingerprints.py
class DeviceFingerprint(TypedDict):
    slug: str
    dev_type: str
    date: str
    desc: str


# CODES_SCHEMA entries
CodeSchemaEntry = TypedDict(
    "CodeSchemaEntry",
    {
        "name": str,
        " I": str,  # Regex
        "RQ": str,  # Regex
        "RP": str,  # Regex
        " W": str,  # Regex
        "lifespan": bool | td | None,
    },
    total=False,
)

#!/usr/bin/env python3
"""RAMSES RF - The device Address L7 domain module (Temporary Proxy)."""

from __future__ import annotations

from typing import Final

# TEMPORARY SHIM: We proxy all address logic to the underlying L3 module
# to ensure 100% test parity while the legacy L7 components transition.
# This proxy will be completely deleted in Phase 5.
from ..ramses_tx.address import (
    ALL_DEV_ADDR,
    ALL_DEVICE_ID,
    DEV_TYPE_MAP,
    DEVICE_LOOKUP,
    HGI_DEV_ADDR,
    HGI_DEVICE_ID,
    NON_DEV_ADDR,
    NON_DEVICE_ID,
    Address,
    dev_id_to_hex_id,
    hex_id_to_dev_id,
    id_to_address,
    is_valid_dev_id,
    pkt_addrs,
)

_DBG_DISABLE_STRICT_CHECKING: Final[bool] = False
_DBG_DISABLE_DEV_HVAC = False

__all__ = [
    "ALL_DEV_ADDR",
    "ALL_DEVICE_ID",
    "Address",
    "DEVICE_LOOKUP",
    "DEV_TYPE_MAP",
    "HGI_DEV_ADDR",
    "HGI_DEVICE_ID",
    "NON_DEV_ADDR",
    "NON_DEVICE_ID",
    "dev_id_to_hex_id",
    "hex_id_to_dev_id",
    "id_to_address",
    "is_valid_dev_id",
    "pkt_addrs",
]

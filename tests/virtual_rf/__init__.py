#!/usr/bin/env python3
"""RAMSES RF - A pseudo-mocked serial port used for testing."""

from typing import Any
from unittest.mock import patch

from ramses_rf import Gateway
from ramses_rf.const import DEV_TYPE_MAP, DevType
from ramses_rf.schemas import SZ_CLASS, SZ_KNOWN_LIST

from .const import MAX_NUM_PORTS, HgiFwTypes
from .virtual_rf import VirtualRf

# patched constants
# _DBG_DISABLE_IMPERSONATION_ALERTS = True  # # ramses_tx.protocol
# _DBG_DISABLE_QOS = False  # #                 ramses_tx.protocol
MINIMUM_WRITE_GAP = 0  # #                      ramses_tx.protocol


def _get_hgi_id_for_schema(
    schema: dict[str, Any], port_idx: int
) -> tuple[str, HgiFwTypes]:
    """Return the Gateway's device_id for a schema (if required, construct an id).

    Does not modify the schema.

    If a Gateway (18:) device is present in the schema, it must have a defined class of
    "HGI". Otherwise, the Gateway device_id is derived from the serial port ordinal
    (port_idx, 0-5).
    """

    known_list: dict[str, Any] = schema.get(SZ_KNOWN_LIST, {})

    hgi_ids = [k for k, v in known_list.items() if v.get(SZ_CLASS) == DevType.HGI]

    if len(hgi_ids) > 1:
        raise TypeError("Multiple Gateways per schema are not support")

    elif len(hgi_ids) == 1:
        hgi_id = hgi_ids[0]
        fw_type = known_list[hgi_id].get("_type", "EVOFW3")

    elif [
        k
        for k, v in known_list.items()
        if k[:2] == DEV_TYPE_MAP.HGI and not v.get(SZ_CLASS)
    ]:
        raise TypeError("Any Gateway must have its class defined explicitly")

    else:
        hgi_id = f"18:{str(port_idx) * 6}"
        fw_type = "EVOFW3"

    return hgi_id, fw_type


@patch("ramses_tx.transport.MINIMUM_WRITE_GAP", MINIMUM_WRITE_GAP, create=True)
async def rf_factory(
    schemas: list[dict[str, Any] | None], start_gwys: bool = True
) -> tuple[VirtualRf, list[Gateway]]:
    """Return the virtual network corresponding to a list of gateway schema/configs.

    Each dict entry will consist of a standard gateway config/schema (or None). Any
    serial port configs are ignored, and are instead allocated sequentially from the
    virtual RF pool.
    """

    if len(schemas) > MAX_NUM_PORTS:
        raise TypeError(f"Only a maximum of {MAX_NUM_PORTS} ports is supported")

    gwys = []

    rf = VirtualRf(len(schemas))

    for idx, schema in enumerate(schemas):
        if schema is None:  # assume no gateway device
            # Port already created by VirtualRf.__init__
            continue

        hgi_id, fw_type = _get_hgi_id_for_schema(schema, idx)

        # Port already created by VirtualRf.__init__, just attach gateway info
        rf.set_gateway(rf.ports[idx], hgi_id, fw_type=HgiFwTypes.__members__[fw_type])

        with patch("ramses_tx.transport.comports", rf.comports):
            gwy = Gateway(rf.ports[idx], **schema)
        gwys.append(gwy)

        if start_gwys:
            await gwy.start()
            assert gwy._transport is not None  # mypy
            gwy._transport._extra["virtual_rf"] = rf

    return rf, gwys

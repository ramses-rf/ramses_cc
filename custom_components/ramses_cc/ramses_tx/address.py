#!/usr/bin/env python3
"""RAMSES RF - The strict L2 MAC address module."""

from __future__ import annotations

from functools import lru_cache
from typing import Final, cast

from . import exceptions as exc
from .const import DEV_TYPE_MAP as _DEV_TYPE_MAP, DEVICE_ID_REGEX, DevType
from .typing import DeviceIdT

DEVICE_LOOKUP: dict[str, str] = {
    k: _DEV_TYPE_MAP._hex(k)
    for k in _DEV_TYPE_MAP.SLUGS
    if k not in (DevType.JIM, DevType.JST)
}
DEVICE_LOOKUP |= {"NUL": "63", "---": "--"}
DEV_TYPE_MAP: dict[str, str] = {v: k for k, v in DEVICE_LOOKUP.items()}


HGI_DEVICE_ID: DeviceIdT = "18:000730"  # type: ignore[assignment]
NON_DEVICE_ID: DeviceIdT = "--:------"  # type: ignore[assignment]
ALL_DEVICE_ID: DeviceIdT = "63:262142"  # type: ignore[assignment]

# NOTE: All debug flags should be False for deployment to end-users
_DBG_DISABLE_STRICT_CHECKING: Final[bool] = False
_DBG_DISABLE_DEV_HVAC = False


class Address:
    """The device Address class."""

    _SLUG = None

    def __init__(self, device_id: DeviceIdT) -> None:
        """Create an address from a valid device ID.

        :param device_id: The RAMSES II device ID (e.g., '01:123456')
        :type device_id: DeviceIdT
        :raises ValueError: If the device_id is not a valid format.
        """

        self.id = device_id
        self.type = device_id[:2]  # dex, drops 2nd part, incl. ":"
        self._hex_id: str = None  # type: ignore[assignment]

        if not self.is_valid(device_id):
            raise ValueError(f"Invalid device_id: {device_id}")

    def __repr__(self) -> str:
        return str(self.id)

    def __str__(self) -> str:
        return self._friendly(self.id).strip()

    def __eq__(self, other: object) -> bool:
        if not hasattr(other, "id"):  # can compare Address with Device
            return NotImplemented
        return self.id == other.id  # type: ignore[no-any-return]

    @property
    def hex_id(self) -> str:
        if self._hex_id is not None:
            return self._hex_id
        self._hex_id = self.convert_to_hex(self.id)  # type: ignore[unreachable]
        return self._hex_id

    @staticmethod
    def is_valid(value: str) -> bool:
        return isinstance(value, str) and (
            value == NON_DEVICE_ID or DEVICE_ID_REGEX.ANY.match(value)
        )

    @classmethod
    def _friendly(cls, device_id: DeviceIdT) -> str:
        """Convert (say) '01:145038' to 'CTL:145038'."""

        if not cls.is_valid(device_id):
            raise TypeError

        _type, _tmp = device_id.split(":")

        return f"{DEV_TYPE_MAP.get(_type, f'{_type:>3}')}:{_tmp}"

    @classmethod
    def convert_from_hex(cls, device_hex: str, friendly_id: bool = False) -> str:
        """Convert a 6-character hex string to a device ID.

        :param device_hex: The hex string to convert (e.g., '06368E')
        :type device_hex: str
        :param friendly_id: If True, returns a named ID, defaults to False
        :type friendly_id: bool
        :return: The formatted device ID string
        :rtype: str
        """

        if device_hex == "FFFFFE":  # aka '63:262142'
            return ">null dev<" if friendly_id else ALL_DEVICE_ID

        if not device_hex.strip():  # aka '--:------'
            return f"{'':10}" if friendly_id else NON_DEVICE_ID

        _tmp = int(device_hex, 16)
        device_id = cast(
            DeviceIdT,
            f"{(_tmp & 0xFC0000) >> 18:02d}:{_tmp & 0x03FFFF:06d}",
        )

        return cls._friendly(device_id) if friendly_id else device_id

    @classmethod
    def convert_to_hex(cls, device_id: DeviceIdT) -> str:
        """Convert (say) '01:145038' (or 'CTL:145038') to '06368E'."""

        if not cls.is_valid(device_id):
            raise TypeError

        if len(device_id) == 9:  # e.g. '01:123456'
            dev_type = device_id[:2]

        else:  # len(device_id) == 10, e.g. 'CTL:123456', or ' 63:262142'
            dev_type = DEVICE_LOOKUP.get(device_id[:3], device_id[1:3])

        return f"{(int(dev_type) << 18) + int(device_id[-6:]):0>6X}"


@lru_cache(maxsize=256)
def id_to_address(device_id: DeviceIdT) -> Address:
    """Factory method to cache & return device Address from device ID."""
    return Address(device_id=device_id)


HGI_DEV_ADDR = Address(HGI_DEVICE_ID)  # 18:000730
NON_DEV_ADDR = Address(NON_DEVICE_ID)  # --:------
ALL_DEV_ADDR = Address(ALL_DEVICE_ID)  # 63:262142


def dev_id_to_hex_id(device_id: DeviceIdT) -> str:
    """Convert (say) '01:145038' (or 'CTL:145038') to '06368E'."""

    if len(device_id) == 9:  # e.g. '01:123456'
        dev_type = device_id[:2]

    elif len(device_id) == 10:  # e.g. '01:123456'
        dev_type = DEVICE_LOOKUP.get(device_id[:3], device_id[1:3])

    else:  # len(device_id) == 10, e.g. 'CTL:123456', or ' 63:262142'
        raise ValueError(f"Invalid value: {device_id}, is not 9-10 characters long")

    return f"{(int(dev_type) << 18) + int(device_id[-6:]):0>6X}"


def hex_id_to_dev_id(device_hex: str, friendly_id: bool = False) -> DeviceIdT:
    """Convert (say) '06368E' to '01:145038' (or 'CTL:145038')."""
    if device_hex == "FFFFFE":  # aka '63:262142'
        return cast(DeviceIdT, "NUL:262142" if friendly_id else ALL_DEVICE_ID)

    if not device_hex.strip():  # aka '--:------'
        return cast(DeviceIdT, f"{'':10}" if friendly_id else NON_DEVICE_ID)

    _tmp = int(device_hex, 16)
    dev_type = f"{(_tmp & 0xFC0000) >> 18:02d}"

    if friendly_id:
        dev_type = DEV_TYPE_MAP.get(dev_type, f"{dev_type:<3}")

    return cast(DeviceIdT, f"{dev_type}:{_tmp & 0x03FFFF:06d}")


@lru_cache(maxsize=128)
def is_valid_dev_id(value: str, dev_class: None | str = None) -> bool:
    """Return True if a device_id is valid."""

    if not isinstance(value, str) or not DEVICE_ID_REGEX.ANY.match(value):
        return False

    return not _DBG_DISABLE_DEV_HVAC or value.split(":", 1)[0] in DEV_TYPE_MAP


@lru_cache(maxsize=256)
def pkt_addrs(
    addr_fragment: str,
) -> tuple[Address, Address, Address, Address, Address]:
    """Parse address fields from a 30-character address fragment.

    :param addr_fragment: The 30-char fragment
    :type addr_fragment: str
    :return: A tuple of (src_addr, dst_addr, addr_0, addr_1, addr_2)
    :rtype: tuple[Address, Address, Address, Address, Address]
    :raises PacketAddrSetInvalid: If the address fields are not valid.
    """
    try:
        addrs = tuple(id_to_address(addr_fragment[i : i + 9]) for i in range(0, 30, 10))
    except ValueError as err:
        raise exc.PacketAddrSetInvalid(
            f"Invalid address set: {addr_fragment}: {err}"
        ) from None

    if not _DBG_DISABLE_STRICT_CHECKING and (
        not (
            addrs[0] not in (NON_DEV_ADDR, ALL_DEV_ADDR)
            and addrs[1] == NON_DEV_ADDR
            and addrs[2] != NON_DEV_ADDR
        )
        and not (
            addrs[0] not in (NON_DEV_ADDR, ALL_DEV_ADDR)
            and addrs[1] not in (NON_DEV_ADDR, addrs[0])
            and addrs[2] == NON_DEV_ADDR
        )
        and not (
            addrs[2] not in (NON_DEV_ADDR, ALL_DEV_ADDR)
            and addrs[0] == NON_DEV_ADDR
            and addrs[1] == NON_DEV_ADDR
        )
    ):
        raise exc.PacketAddrSetInvalid(f"Invalid address set: {addr_fragment}")

    device_addrs = list(filter(lambda a: a.type != "--", addrs))  # dex
    src_addr = device_addrs[0]
    dst_addr = device_addrs[1] if len(device_addrs) > 1 else NON_DEV_ADDR

    if src_addr.id == dst_addr.id:  # incl. HGI_DEV_ADDR == HGI_DEV_ADDR
        src_addr = dst_addr

    return src_addr, dst_addr, addrs[0], addrs[1], addrs[2]

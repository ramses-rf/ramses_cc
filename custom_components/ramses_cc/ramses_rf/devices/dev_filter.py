"""RAMSES RF - Device Filtering."""

from __future__ import annotations

from collections.abc import Callable

from ..exceptions import DeviceNotFoundError
from ..typing import DeviceIdT
from ...ramses_tx.schemas import SZ_BLOCK_LIST, SZ_KNOWN_LIST


class DeviceFilter:
    """Service to filter devices based on known and blocked lists."""

    def __init__(
        self,
        include: list[DeviceIdT],
        exclude: list[DeviceIdT],
        unwanted: list[DeviceIdT],
        enforce_known_list: bool,
        hgi_id_provider: Callable[[], str | None],
    ) -> None:
        """Initialize the DeviceFilter.

        :param include: The list of allowed device IDs.
        :type include: list[DeviceIdT]
        :param exclude: The list of blocked device IDs.
        :type exclude: list[DeviceIdT]
        :param unwanted: A shared list tracking invalid or dynamically rejected device IDs.
        :type unwanted: list[DeviceIdT]
        :param enforce_known_list: Whether to strictly enforce the inclusion list.
        :type enforce_known_list: bool
        :param hgi_id_provider: A callable returning the current Gateway (HGI) ID.
        :type hgi_id_provider: Callable[[], str | None]
        """
        self._include = include
        self._exclude = exclude
        self._unwanted = unwanted
        self._enforce_known_list = enforce_known_list
        self._hgi_id_provider = hgi_id_provider

    def check_filter_lists(self, dev_id: DeviceIdT) -> None:
        """Raise a DeviceNotFoundError if a device_id is filtered out by a list.

        :param dev_id: The device identifier to evaluate.
        :type dev_id: DeviceIdT
        :returns: None
        :rtype: None
        :raises DeviceNotFoundError: If the device is unwanted, strictly not known, or excluded.
        """
        if dev_id in self._unwanted:
            raise DeviceNotFoundError(
                f"Can't create {dev_id}: it is unwanted or invalid"
            )

        if self._enforce_known_list and (
            dev_id not in self._include and dev_id != self._hgi_id_provider()
        ):
            self._unwanted.append(dev_id)
            raise DeviceNotFoundError(
                f"Can't create {dev_id}: it is not an allowed device_id"
                f" (if required, add it to the {SZ_KNOWN_LIST})"
            )

        if dev_id in self._exclude:
            self._unwanted.append(dev_id)
            raise DeviceNotFoundError(
                f"Can't create {dev_id}: it is a blocked device_id"
                f" (if required, remove it from the {SZ_BLOCK_LIST})"
            )

"""RAMSES RF - Core inbound message fact definitions."""

from dataclasses import dataclass, field, replace
from datetime import datetime as dt
from typing import Any

from ..address import Address
from ..enums import Topic
from ..routing import StateHeader
from ...ramses_tx.dtos import PacketDTO
from ...ramses_tx.typing import DeviceIdT


@dataclass(frozen=True, slots=True)
class Message:
    """An immutable historical fact representing a decoded event.

    :param topic: The event bus routing discriminator.
    :type topic: Topic
    :param header: The immutable L7 routing state header.
    :type header: StateHeader
    :param src: The logical origin of the message.
    :type src: Address
    :param dst: The logical target of the message.
    :type dst: Address
    :param data: The decoded payload properties.
    :type data: dict[str, Any]
    :param packets: The L3 packets that comprise this message.
    :type packets: tuple[PacketDTO, ...]
    :param timestamp: The time the message was recorded.
    :type timestamp: dt
    :param lineage: The audit trail of message enrichments.
    :type lineage: tuple['Message', ...]
    """

    topic: Topic
    header: StateHeader
    src: Address
    dst: Address
    data: dict[str, Any]
    packets: tuple[PacketDTO, ...]
    timestamp: dt
    lineage: tuple["Message", ...] = field(default_factory=tuple)

    @property
    def addr3(self) -> Address:
        """Return the third address field (the logical destination or owner).

        :return: The third address object.
        :rtype: Address
        """
        if not self.packets:
            return Address(DeviceIdT("--:------"))

        addr_str = self.packets[0].addr3
        return Address(DeviceIdT(addr_str if addr_str else "--:------"))

    def get(self, key: str, default: Any = None) -> Any:
        """Safely extract payload properties without KeyError.

        :param key: The dictionary key to retrieve.
        :type key: str
        :param default: The fallback value if key is missing.
        :type default: Any
        :return: The extracted value.
        :rtype: Any
        """
        return self.data.get(key, default)

    def enrich(self, new_topic: Topic, **kwargs: Any) -> "Message":
        """Spawn an enriched Message, preserving history.

        :param new_topic: The evolved routing discriminator.
        :type new_topic: Topic
        :param kwargs: The new data properties to append.
        :type kwargs: Any
        :return: A new Message instance with updated data.
        :rtype: Message
        """
        new_data = {**self.data, **kwargs}
        new_lineage = self.lineage + (self,)
        return replace(
            self,
            topic=new_topic,
            data=new_data,
            lineage=new_lineage,
        )

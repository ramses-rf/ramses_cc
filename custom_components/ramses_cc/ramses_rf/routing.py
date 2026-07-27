#!/usr/bin/env python3
"""RAMSES RF - Domain Routing and Addressing.

This module provides the strictly-typed, immutable Data Transfer Objects
responsible for L7 domain routing, replacing legacy L3 string parsing.
It acts as the foundation for the asynchronous event-driven SSOT.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..ramses_tx.const import Code, VerbT
from ..ramses_tx.typing import DeviceIdT


class EventTopic(StrEnum):
    """Strict enumeration of valid topics for the SSOT event bus."""

    INFORMATION = "information"  # Maps to ' I'
    TOPOLOGY_DISCOVERY = "topology_discovery"  # Maps to ' I' for 1FC9
    REQUEST = "request"  # Maps to 'RQ'
    WRITE = "write"  # Maps to ' W'
    RESPONSE = "response"  # Maps to 'RP'


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """Represents the sub-payload context for routing within a device.

    This acts as a Secondary Routing Key, guaranteeing isolation between
    different sub-entities operating on the same physical device.
    """

    value: str | bool | None

    @property
    def as_string(self) -> str:
        """Format the context safely for legacy string interpolation.

        Examples:
            RoutingContext(True) -> "True"
            RoutingContext("00") -> "00"
            RoutingContext(None) -> "None"

        :return: The context value as a string.
        :rtype: str
        """
        if self.value is True:
            return "True"
        if self.value is False:
            return "False"
        return str(self.value)


@dataclass(frozen=True, slots=True)
class StateHeader:
    """The primary immutable routing key for state caching and CQRS.

    Provides O(1) dictionary hashing and serves as the bridge toward
    the event-driven Master Plan (routing into topics and dest_ids).
    """

    code: Code | str
    verb: VerbT | str
    source_id: DeviceIdT | str
    context: RoutingContext

    @classmethod
    def create(
        cls,
        code: Code | str,
        verb: VerbT | str,
        source_id: DeviceIdT | str,
        context_val: str | bool | None,
    ) -> StateHeader:
        """Cleanly generate a StateHeader from primitive or rich variables.

        :param code: The message command code (e.g., '3220' or Code._3220).
        :type code: Code | str
        :param verb: The message verb (e.g., ' I', 'RP' or VerbT.I_).
        :type verb: VerbT | str
        :param source_id: The source L2 device ID (e.g., '01:123456').
        :type source_id: DeviceIdT | str
        :param context_val: The sub-payload context key.
        :type context_val: str | bool | None
        :return: The immutable StateHeader instance.
        :rtype: StateHeader
        """
        # Safely promote strings to rich types, gracefully falling back to
        # strings for unregistered OEM/Debug hardware codes.
        try:
            safe_code: Code | str = Code(code) if isinstance(code, str) else code
        except ValueError:
            safe_code = code

        try:
            safe_verb: VerbT | str = VerbT(verb) if isinstance(verb, str) else verb
        except ValueError:
            safe_verb = verb

        safe_src = DeviceIdT(source_id) if isinstance(source_id, str) else source_id

        return cls(
            code=safe_code,
            verb=safe_verb,
            source_id=safe_src,
            context=RoutingContext(context_val),
        )

    @property
    def legacy_hdr(self) -> str:
        """Calculate the legacy state routing header natively.

        Format: '{code}|{verb}|{src_id}|{ctx_str}'
        Example: '3220|RP|01:123456|00'

        :return: The legacy pipe-separated routing string.
        :rtype: str
        """
        return f"{self.code}|{self.verb}|{self.source_id}|{self.context.as_string}"

    @property
    def topic(self) -> EventTopic:
        """Master Plan alignment: Generate the SSOT event topic.

        This provides the standardized envelope topic for the future
        DecodedMessage event stream based on the verb/code.

        :return: The strictly-typed event topic enum.
        :rtype: EventTopic
        """
        if self.code == Code._1FC9:
            return EventTopic.TOPOLOGY_DISCOVERY

        if self.verb == VerbT.I_:
            return EventTopic.INFORMATION
        if self.verb == VerbT.RQ:
            return EventTopic.REQUEST
        if self.verb == VerbT.W_:
            return EventTopic.WRITE

        return EventTopic.RESPONSE

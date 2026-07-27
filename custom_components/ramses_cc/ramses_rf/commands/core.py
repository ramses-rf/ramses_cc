"""RAMSES RF - Core outbound command intent definitions."""

from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

from ..address import Address
from ..enums import Action


@dataclass(frozen=True, slots=True)
class Command:
    """An immutable application intent destined for the RF network.

    :param src: The logical origin of the command.
    :type src: Address
    :param dst: The logical target (effector) of the command.
    :type dst: Address
    :param action: The semantic domain intent.
    :type action: Action
    :param data: The parameters for the command action.
    :type data: dict[str, Any]
    :param needs_reply: True if the L7 FSM should wait for an RP.
    :type needs_reply: bool
    :param timeout: Seconds the L7 FSM should wait for a reply.
    :type timeout: float
    :param generated_by: Human-readable origin (e.g., 'Scheduler').
    :type generated_by: str
    :param correlation_id: Unique UUID for tracing async logs.
    :type correlation_id: UUID
    """

    src: Address
    dst: Address
    action: Action
    data: dict[str, Any]

    # QoS & Conversational Tracking
    needs_reply: bool = False
    timeout: float = 3.0

    # Traceability & Debugging
    generated_by: str = "unknown"
    correlation_id: UUID = field(default_factory=uuid4)

    def get(self, key: str, default: Any = None) -> Any:
        """Safely extract command properties without KeyError.

        :param key: The dictionary key to retrieve.
        :type key: str
        :param default: The fallback value if key is missing.
        :type default: Any
        :return: The extracted value.
        :rtype: Any
        """
        return self.data.get(key, default)

    def with_data(self, **kwargs: Any) -> "Command":
        """Spawn a new Command with updated data parameters.

        :param kwargs: The data properties to update or add.
        :type kwargs: Any
        :return: A cloned Command with enriched data.
        :rtype: Command
        """
        return replace(self, data={**self.data, **kwargs})

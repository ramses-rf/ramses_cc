#!/usr/bin/env python3
"""RAMSES RF - The Application Message module."""

from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any

from ...ramses_tx.dtos import PacketDTO

from ..const import RQ, Code
from ..protocol.ramses import CODES_SCHEMA, SZ_LIFESPAN
from .base import Message

if TYPE_CHECKING:
    from ...ramses_tx.engine import Engine


class ApplicationMessage(Message):
    """Application-level message extended with gateway context and
    expiration.
    """

    CANT_EXPIRE: float = -1.0  # sentinel value for fraction_expired
    HAS_EXPIRED: float = 2.0  # fraction_expired >= HAS_EXPIRED
    IS_EXPIRING: float = 0.8  # fraction_expired >= 0.8 (and < HAS_EXPIRED)

    _engine: Engine | None = None
    _fraction_expired: float | None = None
    _gwy: Any | None = None
    _delete_task_queued: bool = False

    @classmethod
    def from_dto(cls, dto: PacketDTO) -> ApplicationMessage:
        """Factory to safely promote a transport Message to an
        ApplicationMessage.
        """
        # Initialize the subclass identically to how the base class initializes
        return cls(dto)

    def bind_context(self, gwy: Any) -> None:
        """Explicitly assign the application context (gateway).

        :param gwy: The application context (gateway) to associate.
        :type gwy: Any
        """
        self._gwy = gwy

    def set_gateway(self, gwy: Engine) -> None:
        """Set the gateway (engine) instance for this message.

        :param gwy: The gateway (engine) instance to associate.
        :type gwy: Engine
        """
        self._engine = gwy

    def _get_lifespan(self) -> bool | td:
        """Return the lifespan of a packet before it expires."""
        if self.verb in (RQ, " W"):
            return td(seconds=0)

        if self.code in (Code._0005, Code._000C):
            return td(minutes=60 * 24)

        if self.code == Code._0006:
            return td(minutes=60)

        if self.code == Code._0404:
            return td(minutes=60 * 24)

        if self.code == Code._000A and self._has_array:
            return td(minutes=60)

        if self.code == Code._10E0:
            return td(minutes=60 * 24)

        if self.code == Code._1F09:
            return td(seconds=360) if self.verb == " I" else td(seconds=0)

        if self.code == Code._1FC9 and self.verb == "RP":
            return td(minutes=60 * 24)

        if self.code in (Code._2309, Code._30C9) and self._has_array:
            return td(seconds=360)

        if self.code == Code._3220:
            return td(minutes=5) * 2.1

        if (code_schema := CODES_SCHEMA.get(self.code)) and SZ_LIFESPAN in code_schema:
            result = code_schema[SZ_LIFESPAN]
            if isinstance(result, td):
                return result

        return td(minutes=60)

    @property
    def _expired(self) -> bool:
        """Return True if the message is dated, False otherwise.

        :return: True if the message is dated, False otherwise.
        :rtype: bool
        """
        # Safest fallback for unit tests without an engine
        now = self._engine._dt_now() if self._engine else dt.now(tz=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        msg_dtm = self.dtm
        if msg_dtm.tzinfo is None:
            msg_dtm = msg_dtm.replace(tzinfo=UTC)

        # 1. Enforce the hard 7-day expiration limit
        if now - msg_dtm > td(days=7):
            return True

        def fraction_expired(lifespan: td) -> float:
            """Calculate the fraction of expired normal lifespan.

            :param lifespan: The lifespan of the message.
            :type lifespan: td
            :return: The expired fraction.
            :rtype: float
            """
            if lifespan.total_seconds() == 0:
                return self.HAS_EXPIRED
            return float((now - msg_dtm - td(seconds=3)) / lifespan)

        # 1. Look for easy win...
        if self._fraction_expired is not None:
            if self._fraction_expired == self.CANT_EXPIRE:
                return False
            if self._fraction_expired >= self.HAS_EXPIRED:
                return True

        # 2. Need to update the fraction_expired...
        # sync_cycle is a special case
        if self.code == Code._1F09 and self.verb != RQ:
            # RQs won't have remaining_seconds, RP/Ws have only partial
            # cycle times. Use strictly safe dict access per Master Plan.
            rem_secs = 0
            if isinstance(self.payload, dict):
                rem_secs = self.payload.get("remaining_seconds", 0)

            self._fraction_expired = fraction_expired(
                td(seconds=float(rem_secs or 0)),
            )

        else:
            lifespan = self._get_lifespan()
            if lifespan is False:
                self._fraction_expired = self.CANT_EXPIRE
            elif lifespan is True:
                raise NotImplementedError("Lifespan True not implemented")
            else:
                assert isinstance(lifespan, td)
                self._fraction_expired = fraction_expired(lifespan)

        return self._fraction_expired >= self.HAS_EXPIRED

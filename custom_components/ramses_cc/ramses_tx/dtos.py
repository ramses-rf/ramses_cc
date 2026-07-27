"""
Data Transfer Objects for ramses_tx.

This module defines the strict boundaries for OSI layer decoupling
between the RF modem (L1-L3) and the Domain Model (L4-L7).
"""

from dataclasses import dataclass
from datetime import datetime as dt


@dataclass(frozen=True, slots=True)
class PacketDTO:
    """
    Pure data object bridging the ramses_tx modem and ramses_rf.

    :param timestamp: Time the frame was received.
    :type timestamp: datetime
    :param rssi: Received Signal Strength Indicator (e.g., "-72").
    :type rssi: str
    :param verb: The action verb (e.g., "RQ", "I", "W", "RP").
    :type verb: str
    :param seq: The sequence number (e.g., "003").
    :type seq: str
    :param addr1: Positional L2 Address 1 (e.g., "01:145038").
    :type addr1: str
    :param addr2: Positional L2 Address 2.
    :type addr2: str
    :param addr3: Positional L2 Address 3.
    :type addr3: str
    :param code: The packet code (e.g., "30C9").
    :type code: str
    :param length: The payload length.
    :type length: str
    :param payload: Raw hex payload string (e.g., "0001C8").
    :type payload: str
    """

    timestamp: dt
    rssi: str
    verb: str
    seq: str
    addr1: str
    addr2: str
    addr3: str
    code: str
    length: str
    payload: str


@dataclass(frozen=True, slots=True)
class CommandDTO:
    """
    Instructions strictly for L2/L3 transmission over the radio.

    :param verb: The action verb.
    :type verb: str
    :param addr1: Positional L2 Address 1.
    :type addr1: str
    :param addr2: Positional L2 Address 2.
    :type addr2: str
    :param addr3: Positional L2 Address 3.
    :type addr3: str
    :param code: The command code.
    :type code: str
    :param payload: Raw hex payload string.
    :type payload: str
    :param priority: Hardware queue priority (e.g., 1 High).
    :type priority: int
    :param num_repeats: Hardware repeat blasts to beat RF noise.
    :type num_repeats: int
    """

    verb: str
    addr1: str
    addr2: str
    addr3: str
    code: str
    payload: str
    priority: int
    num_repeats: int

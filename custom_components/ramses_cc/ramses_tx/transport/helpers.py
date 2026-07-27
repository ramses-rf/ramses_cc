#!/usr/bin/env python3
"""RAMSES RF - Helper functions for packet transports."""

from __future__ import annotations

import logging
import re
from string import printable

from ..const import I_, RP, RQ, W_

_LOGGER = logging.getLogger(__name__)


def _normalise(pkt_line: str) -> str:
    """Perform any (transparent) frame-level hacks, as required at (near-)RF layer.

    Goals:
      - ensure an evofw3 provides the same output as a HGI80 (none, presently)
      - handle 'strange' packets (e.g. ``I|08:|0008``)

    :param pkt_line: The raw packet string from the hardware.
    :type pkt_line: str
    :return: The normalized packet string.
    :rtype: str
    """
    # TODO: deprecate as only for ramses_esp <0.4.0
    # ramses_esp-specific bugs, see: https://github.com/IndaloTech/ramses_esp/issues/1
    pkt_line = re.sub("\r\r", "\r", pkt_line)
    if pkt_line[:4] == " 000":
        pkt_line = pkt_line[1:]
    elif pkt_line[:2] in (I_, RQ, RP, W_):
        pkt_line = ""

    # pseudo-RAMSES-II packets (encrypted payload?)...
    if pkt_line[10:14] in (" 08:", " 31:") and pkt_line[-16:] == "* Checksum error":
        pkt_line = pkt_line[:-17] + " # Checksum error (ignored)"

    # remove any "/r/n" (leading whitespeace is a problem for commands, but not packets)
    return pkt_line.strip()


def _str(value: bytes) -> str:
    """Decode bytes to a string, ignoring non-printable characters.

    :param value: The bytes to decode.
    :type value: bytes
    :return: The decoded string.
    :rtype: str
    """
    try:
        result = "".join(
            c for c in value.decode("ascii", errors="strict") if c in printable
        )
    except UnicodeDecodeError:
        _LOGGER.warning("%s < Can't decode bytestream (ignoring)", value)
        return ""
    return result

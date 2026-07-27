#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Provide the base class for commands (constructed/sent packets) and packets.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..ramses_rf.protocol.ramses import (
    CODE_IDX_ARE_COMPLEX,
    CODE_IDX_ARE_NONE,
    CODE_IDX_ARE_SIMPLE,
    CODE_IDX_DOMAIN,
    CODES_ONLY_FROM_CTL,
    CODES_SCHEMA,
    CODES_WITH_ARRAYS,
    RQ_NO_PAYLOAD,
)

from . import exceptions as exc
from .address import ALL_DEV_ADDR, NON_DEV_ADDR, Address, pkt_addrs
from .const import COMMAND_REGEX, DEV_ROLE_MAP, DEV_TYPE_MAP
from .typing import HeaderT, PayloadT

# TODO: add _has_idx (as func return only one type, or raise)

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    F8,
    F9,
    FA,
    FC,
    FF,
)

if TYPE_CHECKING:
    from .const import VerbT


_LOGGER = logging.getLogger(__name__)


_PktIdxT = str


class Frame:
    """The Frame class - used as a base by the Command and Packet classes.

    `RQ --- 01:078710 10:067219 --:------ 3220 005 0000050000`
    """

    src: Address  # Address | Device
    dst: Address  # Address | Device
    _addrs: tuple[Address, Address, Address]

    def __init__(self, frame: str) -> None:
        """Create a frame from a string.

        :raises InvalidPacketError: if provided string is invalid.
        """

        self._frame: str = frame
        if not COMMAND_REGEX.match(self._frame):
            raise exc.PacketInvalid(f"Bad frame: Invalid structure: >>>{frame}<<<")

        fields = frame.lstrip().split(" ")

        self.verb: VerbT = frame[:2]  # type: ignore[assignment]
        self.seqn: str = fields[1]  # . frame[3:6]
        self.code: Code = fields[5]  # type: ignore[assignment]
        self.len_: str = fields[6]  # . frame[42:45]  FIXME: len_, _len & len(payload)/2
        self.payload: PayloadT = PayloadT(fields[7])  # frame[46:].split(" ")[0]
        self._len: int = int(len(self.payload) / 2)

        try:
            self.src, self.dst, *self._addrs = pkt_addrs(  # type: ignore[assignment]
                " ".join(fields[i] for i in range(2, 5))  # frame[7:36]
            )
        except exc.PacketInvalid as err:  # will be: InvalidAddrSetError
            raise exc.PacketInvalid("Bad frame: Invalid address set") from err

        if len(self.payload) != int(self.len_) * 2:
            raise exc.PacketInvalid(
                f"Bad frame: Invalid payload: "
                f"len({self.payload}) is not int('{self.len_}' * 2))"
            )

        self._ctx_: bool | str = None  # type: ignore[assignment]
        self._hdr_: HeaderT | None = None
        self._idx_: bool | str = None  # type: ignore[assignment]

        self._has_array_: bool = None  # type: ignore[assignment]
        self._has_ctl_: bool = None  # type: ignore[assignment]  # TODO: remove
        self._has_payload_: bool = None  # type: ignore[assignment]

        self._repr: str = None  # type: ignore[assignment]

    # FIXME: this is messy
    def _validate(self, *, strict_checking: bool = False) -> None:
        """Validate the frame: it may be a cmd or a (response) pkt.

        Raise an exception InvalidPacketError (InvalidAddrSetError) if it is not valid.
        """

        if len(self._frame[46:].split(" ")[0]) != int(self._frame[42:45]) * 2:
            raise exc.PacketInvalid("Bad frame: Payload length mismatch")

        try:
            # self.src, self.dst, *self._addrs = pkt_addrs(self._frame[7:36])
            src, dst, *addrs = pkt_addrs(self._frame[7:36])
        except exc.PacketInvalid as err:  # will be: InvalidAddrSetError
            raise exc.PacketInvalid("Bad frame: Invalid address set") from err

        if not strict_checking:
            return

        try:  # Strict checking: helps users avoid constructing bad commands
            if addrs[0] == NON_DEV_ADDR:
                assert self.verb == I_, "wrong verb or dst addr should be present"
            elif addrs[2] == NON_DEV_ADDR:
                assert self.verb == I_ or src is not dst, (
                    "wrong verb or dst addr should not be src"
                )
            elif addrs[0] is addrs[2]:
                assert self.verb == I_, "wrong verb or dst addr should not be src"
            else:
                assert self.verb in (I_, W_), "wrong verb or dst addr should be src"
        except AssertionError as err:
            raise exc.PacketInvalid(f"Bad frame: Invalid address set: {err}") from err

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object."""

        if self._repr is None:
            self._repr = " ".join(  # type: ignore[unreachable]
                (
                    self.verb,
                    self.seqn,
                    *(repr(a) for a in self._addrs),
                    self.code,
                    self.len_,
                    self.payload,
                )
            )
        return self._repr

    def __str__(self) -> str:
        """Return a brief readable string representation of this object."""
        # repr(self) == repr(cls(str(self)))

        try:
            return f"{self!r} # {self._hdr}"  # code|ver|device_id|context
        except AttributeError as err:
            return f"{self!r} < {err}"

    def __eq__(self, other: object) -> bool:
        if not hasattr(other, "_frame"):
            return NotImplemented
        return self._frame[4:] == other._frame[4:]  # type: ignore[no-any-return]

    @property
    def _has_array(self) -> None | bool:  # TODO: a mess - has false negatives
        """Return the True if the payload is an array, False otherwise.

        May return false negatives (e.g. arrays of length 1), and None if undetermined.

        An example of a false negative is evohome with only one zone (i.e. the periodic
        2309/30C9/000A packets).
        """

        if self._has_array_ is not None:  # HACK: overridden by detect_array(msg, prev)
            return self._has_array_

        # False -ves (array length is 1) are an acceptable compromise to extensive checking

        # .W --- 01:145038 34:092243 --:------ 1FC9 006 07230906368E
        # .I --- 01:145038 --:------ 01:145038 1FC9 018 07000806368E-FC3B0006368E-071FC906368E
        # .I --- 01:145038 --:------ 01:145038 1FC9 018 FA000806368E-FC3B0006368E-FA1FC906368E
        # .I --- 34:092243 --:------ 34:092243 1FC9 030 0030C9896853-002309896853-001060896853-0010E0896853-001FC9896853
        if self.code == Code._1FC9:  # type: ignore[unreachable]
            self._has_array_ = self.verb != RQ  # safe to treat all as array, even len=1
            return self._has_array_  # don't do any checks for 1FC9 (they will fail)

        elif self.verb != I_ or self.code not in CODES_WITH_ARRAYS:
            self._has_array_ = False

        elif self._len != CODES_WITH_ARRAYS[self.code][0]:  # NOTE: can be false -ves
            a, b = divmod(self._len, CODES_WITH_ARRAYS[self.code][0])
            self._has_array_ = a > 0 and b == 0

        elif (
            self.code in (Code._22C9, Code._3150)
            and self.src.type == DEV_TYPE_MAP.UFC
            and self.dst is self.src
            and self.payload[:1] != "F"
        ):
            self._has_array_ = True

        # elif self.code == Code._000C:  # anachronism: variable array length
        #     return

        else:
            self._has_array_ = False

        if self._has_array_:
            len_ = CODES_WITH_ARRAYS[self.code][0]

            assert self._len % len_ == 0, (
                f"{self} < array has length ({self._len}) that is not multiple of {len_}"
            )
            assert (
                self.src.type in (DEV_TYPE_MAP.DTS, DEV_TYPE_MAP.DT2)
                or self.src == self.dst  # DEX
            ), f"{self} < array is from a non-controller (01)"
            assert (
                self.src.type not in (DEV_TYPE_MAP.DTS, DEV_TYPE_MAP.DT2)
                or self.dst.id == NON_DEV_ADDR.id  # DEX
            ), f"{self} < array is from a non-controller (02)"

        # .I --- 10:040239 01:223036 --:------ 0009 003 000000        # not array
        # .I --- 01:102458 --:------ 01:102458 0009 006 FC01FF-F901FF
        # .I --- 01:145038 --:------ 01:145038 0009 006 FC00FF-F900FF
        # .I 034 --:------ --:------ 12:126457 2309 006 017EFF-027EFF
        # .I --- 01:223036 --:------ 01:223036 000A 012 081001F40DAC-091001F40DAC  # 2nd fragment
        # .I 024 --:------ --:------ 12:126457 000A 012 010001F40BB8-020001F40BB8
        # .I --- 02:044328 --:------ 02:044328 22C9 018 0001F40A2801-0101F40A2801-0201F40A2801
        # .I --- 23:100224 --:------ 23:100224 2249 007 007EFF7EFFFFFF  # can have 2 zones
        # .I --- 02:044328 --:------ 02:044328 22C9 018 0001F40A2801-0101F40A2801-0201F40A2801
        # .I --- 02:001107 --:------ 02:001107 3150 010 007A-017A-027A-036A-046A

        return self._has_array_

    @property
    def _has_ctl(self) -> None | bool:
        """Return True if the packet is to/from a controller."""

        # NB: the difference between these (_has_ctl, src, dst, -:-) and above (_has_ctl, src, -:-, src)
        # 2000-01-01T03:00:00.000000 ...  I --- 37:123456 --:------ 37:123456 31DA 030 00C8400518646427102AF82EE031FFFFFFC800C8C83FFF64640AFF0AFF00
        # 2022-11-20T08:32:06.904058 063 RP --- 32:134446 37:171685 --:------ 31DA 030 00EF007FFF2F1B0226069A07EEFFE4F8000038988F0000EFEF1F2420FC00
        # Maybe only use this for CH/DHW, and not HVAC?

        if self._has_ctl_ is not None:
            return self._has_ctl_

        # TODO: handle RQ/RP to/from HGI/RFG, handle HVAC

        if {self.src.type, self.dst.type} & {  # type: ignore[unreachable]
            DEV_TYPE_MAP.CTL,
            DEV_TYPE_MAP.UFC,
            DEV_TYPE_MAP.PRG,
        }:  # DEX
            _LOGGER.debug(f"{self} # HAS controller (10)")
            self._has_ctl_ = True

        # .I --- 12:010740 --:------ 12:010740 30C9 003 0008D9 # not ctl
        elif self.dst is self.src:  # (not needed?) & self.code == I_:
            _LOGGER.debug(
                f"{self} < "
                + (
                    "HAS"
                    if self.code in CODES_ONLY_FROM_CTL + (Code._31D9, Code._31DA)
                    else "no"
                )
                + " controller (20)"
            )
            self._has_ctl_ = any(
                (
                    self.code == Code._3B00 and self.payload[:2] == FC,
                    self.code in CODES_ONLY_FROM_CTL + (Code._31D9, Code._31DA),
                )
            )

        # .I --- --:------ --:------ 10:050360 1FD4 003 002ABE # no ctl
        # .I 095 --:------ --:------ 12:126457 1F09 003 000BC2 # HAS ctl
        # .I --- --:------ --:------ 20:001473 31D9 003 000001 # ctl? (HVAC)
        elif self.dst.id == NON_DEV_ADDR.id:
            _LOGGER.debug(f"{self} # HAS controller (21)")
            self._has_ctl_ = self.src.type != DEV_TYPE_MAP.OTB  # DEX

        # .I --- 10:037879 --:------ 12:228610 3150 002 0000   # HAS ctl
        # .I --- 04:029390 --:------ 12:126457 1060 003 01FF01 # HAS ctl
        elif self.dst.type in (DEV_TYPE_MAP.DTS, DEV_TYPE_MAP.DT2):  # DEX
            _LOGGER.debug(f"{self} # HAS controller (22)")
            self._has_ctl_ = True

        # RQ --- 30:258720 10:050360 --:------ 3EF0 001 00           # UNKNOWN (99)
        # RP --- 10:050360 30:258720 --:------ 3EF0 006 000011010A1C # UNKNOWN (99)

        # RQ --- 18:006402 13:049798 --:------ 1FC9 001 00
        # RP --- 13:049798 18:006402 --:------ 1FC9 006 003EF034C286
        # RQ --- 30:258720 10:050360 --:------ 22D9 001 00
        # RP --- 10:050360 30:258720 --:------ 22D9 003 0003E8
        # RQ --- 30:258720 10:050360 --:------ 3220 005 0000120000
        # RP --- 10:050360 30:258720 --:------ 3220 005 0040120166
        # RQ --- 30:258720 10:050360 --:------ 3EF0 001 00
        # RP --- 10:050360 30:258720 --:------ 3EF0 006 000011010A1C

        # .I --- 34:021943 63:262142 --:------ 10E0 038 000001C8380A01... # unknown
        # .I --- 32:168090 30:082155 --:------ 31E0 004 0000C800          # unknown

        if self._has_ctl_ is None:
            # if DEV_MODE and DEV_TYPE_MAP.HGI not in (
            #     self.src.type,
            #     self.dst.type,
            # ):  # DEX
            #     _LOGGER.warning(f"{self} # has_ctl - undetermined (99)")
            self._has_ctl_ = False

        return self._has_ctl_

    @property
    def _has_idx(self) -> bool:
        """Return True if the payload has an index (or has an array), False otherwise."""

        return self._idx is not False

    @property
    def _has_payload(self) -> bool:
        """Return True if the packet has a non-null payload, False otherwise.

        May return false positives. The payload may still have an idx.
        """

        if self._has_payload_ is not None:
            return self._has_payload_

        self._has_payload_ = not any(  # type: ignore[unreachable]
            (
                self._len == 1,
                self.verb == RQ and self.code in RQ_NO_PAYLOAD,
                self.verb == RQ and self._len == 2 and self.code != Code._0016,
                # self.verb == RQ and self._len == 2 and self.code in (
                #   Code._2309, Code._2349, Code._3EF1
                # ),
            )
        )

        return self._has_payload_

    def _force_has_array(self) -> None:
        self._has_array_ = True
        self._ctx_ = None  # type: ignore[assignment]
        self._hdr_ = None
        self._idx_ = None  # type: ignore[assignment]

    @property
    def _is_fragment(self) -> bool:
        """Return True is the payload *could* be a fragment, False otherwise."""
        # .I 036 --:------ --:------ 12:126457 000A 012 010001F40BB8-020001F40BB8  # max 2 zones

        return (
            self.code in (Code._000A, Code._22C9)
            and self.verb == I_
            and self.src is self.dst
            # and self._has_array  # not needed
        ) or (self.code == Code._0404 and self.verb in (I_, RP))

    @property
    def _ctx(self) -> bool | str:  # incl. self._idx
        """Return the payload's full context, if any (e.g. for 0404: zone_idx/frag_idx).

        Used to store packets in the entity's message DB. It is a superset of _idx.
        """

        if self._ctx_ is not None:
            return self._ctx_

        if self.code in (  # type: ignore[unreachable]
            Code._0005,
            Code._000C,
        ):  # zone_idx, zone_type (device_role)
            self._ctx_ = self.payload[:4]
        elif self.code == Code._0404:  # zone_idx, frag_idx
            self._ctx_ = self._idx + self.payload[10:12]
        else:
            self._ctx_ = self._idx
        return self._ctx_

    @property
    def _hdr(self) -> HeaderT:  # incl. self._ctx
        """Return the QoS header (fingerprint) of this packet (i.e. device_id|code|verb).

        Used for QoS (timeouts, retries), callbacks, etc.
        """

        if self._hdr_ is not None:
            return self._hdr_

        # FIXME: HACK: sometimes RecursionError
        self._hdr_ = HeaderT("|".join((self.code, self.verb)))
        self._hdr_ = pkt_header(self)
        return self._hdr_  # type: ignore[return-value]

    @property
    def _idx(self) -> bool | str:  # FIXME: a mess
        """Return the payload's index, if any (e.g. zone_idx, domain_id  or log_idx).

        Used to route a packet to the correct entity's (i.e. zone/domain) msg handler.
        """

        if self._idx_ is not None:
            return self._idx_

        self._idx_ = _pkt_idx(self) or False  # type: ignore[unreachable]
        return self._idx_


# TODO: a mess - has false negatives
def _pkt_idx(pkt: Frame) -> None | bool | str:  # _has_array, _has_ctl
    """Return the payload's 2-byte context (e.g. zone_idx, domain_id or log_idx).

    May return a 2-byte string (usu. pkt.payload[:2]), or:
    - False if there is no context at all
    - True if the payload is an array
    - None if it is indeterminable
    """
    # The three iterables (none, simple, complex) are mutex

    # FIXME: 0016 is broken

    # mutex 2/4, CODE_IDX_COMPLEX: are not payload[:2]
    if pkt.code == Code._0005:
        return pkt._has_array

    # .I --- 10:040239 01:223036 --:------ 0009 003 000000
    if pkt.code == Code._0009 and pkt.src.type == DEV_TYPE_MAP.OTB:  # DEX
        return False

    if pkt.code == Code._000C:  # zone_idx/domain_id (complex, payload[0:4])
        if pkt.payload[2:4] == DEV_ROLE_MAP.APP:  # "000F"
            return str(FC)  # mypy
        if pkt.payload[0:4] == f"01{DEV_ROLE_MAP.HTG}":  # "010E"
            return str(F9)  # mypy
        if pkt.payload[2:4] in (
            DEV_ROLE_MAP.DHW,
            DEV_ROLE_MAP.HTG,
        ):  # "000D", "000E"
            return str(FA)  # mypy
        return pkt.payload[:2]

    if pkt.code == Code._0404:  # assumes only 1 DHW zone (can be 2, but never seen)
        return "HW" if pkt.payload[2:4] == "23" else pkt.payload[:2]

    if pkt.code == Code._0418:  # log_idx (payload[4:6])
        return pkt.payload[4:6]

    if pkt.code == Code._1100:  # TODO; can do in parser
        return pkt.payload[:2] if pkt.payload[:1] == "F" else False  # only FC

    if pkt.code == Code._3220:  # msg_id/data_id (payload[4:6])
        return pkt.payload[4:6]

    if pkt.code in CODE_IDX_ARE_COMPLEX:  # these should be handled above
        raise NotImplementedError(f"{pkt} # CODE_IDX_COMPLEX")  # a coding error

    # mutex 1/4, CODE_IDX_NONE: always returns False
    if pkt.code in CODE_IDX_ARE_NONE:  # returns False
        if (
            str(CODES_SCHEMA[pkt.code].get(pkt.verb, ""))[:3] == "^00"
            and pkt.payload[:2] != "00"
        ):
            raise exc.PacketPayloadInvalid(
                f"Packet idx is {pkt.payload[:2]}, but expecting no idx (00) (0xAA)"
            )
        return False

    # mutex 3/4, CODE_IDX_SIMPLE: potentially some false -ves?
    if pkt._has_array:
        return True  # excludes len==1 for 000A, 2309, 30C9

    # TODO: is this needed?: exceptions to CODE_IDX_SIMPLE
    if pkt.payload[:2] in (F8, F9, FA, FC):  # TODO: F6, F7?, FB, FD
        if pkt.code not in CODE_IDX_DOMAIN:
            raise exc.PacketPayloadInvalid(
                f"Packet idx is {pkt.payload[:2]}, but not expecting a domain id"
            )
        return pkt.payload[:2]

    if (
        pkt._has_ctl  # TODO: exclude HVAC?
    ):  # risk of false -ves, TODO: pkt.src.type == DEV_TYPE_MAP.HGI too?  # DEX
        # 02:    22C9: would be picked up as an array, if len==1 counted
        # 03:    # .I 028 03:094242 --:------ 03:094242 30C9 003 010B22  # ctl
        # 12/22: 000A|1030|2309|30C9 from (addr0 --:), 1060|3150 (addr0 04:)
        # 23:    0009|10A0
        return pkt.payload[:2]  # tcs._max_zones checked elsewhere

    if pkt.code in (Code._31D9, Code._31DA):
        return pkt.payload[:2]

    if pkt.payload[:2] != "00":
        raise exc.PacketPayloadInvalid(
            f"Packet idx is {pkt.payload[:2]}, but expecting no idx (00) (0xAB)"
        )  # TODO: add a test for this

    if pkt.code in CODE_IDX_ARE_SIMPLE:
        return None  # False  # TODO: return None (less precise) or risk false -ves?

    # mutex 4/4, CODE_IDX_UNKNOWN: an unknown code
    _LOGGER.info(f"{pkt} # Unable to determine payload index (is probably OK)")
    return None


def pkt_header(pkt: Frame, /, rx_header: bool = False) -> None | HeaderT:
    """Return the header of a packet (all packets have a header).

    Used for QoS, and others.

    For rx_header=True, return instead the header of the response packet, if one is
    expected, otherwise return None.

    Examples include:
     I --- 04:155407 --:------ 04:155407 30C9 003 00092F                   # 30C9| I|04:155407
    RP --- 01:223036 18:005567 --:------ 2349 007 0404B000FFFFFF           # 2349|RP|01:223036|04
     I --- 01:223036 --:------ 01:223036 3B00 002 FCC8                     # 3B00| I|01:223036|FC
    RP --- 01:223036 18:005567 --:------ 000C 012 020800125F91020800125F8D # 000C|RP|01:223036|0208
     I --- 01:223036 --:------ 01:223036 2309 030 00-0640 01-03E8 02-03... # 2309| I|01:223036 (True)
    """

    if pkt.code == Code._1FC9:
        # .I --- 34:021943 --:------ 34:021943 1FC9 024 00-2309-8855B7 00-1FC9-8855B7
        # .W --- 01:145038 34:021943 --:------ 1FC9 006 00-2309-06368E  # won't know src until it arrives
        # .I --- 34:021943 01:145038 --:------ 1FC9 006 00-2309-8855B7
        if not rx_header:
            device_id = ALL_DEV_ADDR.id if pkt.src == pkt.dst else pkt.dst.id
            return HeaderT("|".join((pkt.code, pkt.verb, device_id)))
        if pkt.src == pkt.dst:  # and pkt.verb == I_:
            return HeaderT("|".join((pkt.code, W_, pkt.src.id)))
        if pkt.verb == W_:  # and pkt.src != pkt.dst:
            return HeaderT(
                "|".join((pkt.code, I_, pkt.src.id))
            )  # TODO: why not pkt.dst?
        # if pkt.verb == RQ:  # and pkt.src != pkt.dst:  # TODO: this breaks things
        #     return "|".join((pkt.code, RP, pkt.dst.id))
        return None

    # RQ and W use the dst.id rather than the src.id, as:
    # - cmd.src could be 18:000730, and echo .src will have changed to (say) 18:123456
    # - cmd.dst is the effector

    if rx_header:
        if pkt.verb in (I_, RP) or pkt.src == pkt.dst:  # say: xxxx| W|00:000000|xx
            return None  # no response expected
        header = "|".join((pkt.code, RP if pkt.verb == RQ else I_, pkt.dst.id))

    elif pkt.verb in (I_, RP) or pkt.src == pkt.dst:
        header = "|".join((pkt.code, pkt.verb, pkt.src.id))

    else:
        header = "|".join((pkt.code, pkt.verb, pkt.dst.id))

    try:
        return HeaderT(f"{header}|{pkt._ctx}" if isinstance(pkt._ctx, str) else header)
    except AssertionError:
        return HeaderT(header)

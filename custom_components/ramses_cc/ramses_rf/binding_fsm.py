#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Base for all devices.
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import voluptuous as vol

from ..ramses_tx import ALL_DEV_ADDR, ALL_DEVICE_ID, Command, DevType, Priority, QosParams

from . import exceptions as exc
from .messages import Message

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..ramses_tx import IndexT, Packet

    from .interfaces import CommandDispatcher, DeviceInterface

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_DISABLE_PHASE_ASSERTS: Final[bool] = False
_DBG_MAINTAIN_STATE_CHAIN: Final[bool] = False  # maintain Context._prev_state

_LOGGER = logging.getLogger(__name__)


SZ_RESPONDENT: Final = "respondent"
SZ_SUPPLICANT: Final = "supplicant"
SZ_IS_DORMANT: Final = "is_dormant"


CONFIRM_RETRY_LIMIT: Final[int] = (
    3  # automatically Bound, from Confirming > this # of sends
)
SENDING_RETRY_LIMIT: Final[int] = (
    3  # fail Offering/Accepting if no response > this # of sends
)

CONFIRM_TIMEOUT_SECS: Final[float] = (
    3  # automatically Bound, from BoundAccepted > this # of seconds
)
WAITING_TIMEOUT_SECS: Final[float] = (
    5  # fail Listen/Offer/Accept if no pkt rcvd > this # of seconds
)

# raise a BindTimeoutError if expected Pkt is not received before this number of seconds
_TENDER_WAIT_TIME: Final[float] = WAITING_TIMEOUT_SECS  # resp. listening for Offer
_ACCEPT_WAIT_TIME: Final[float] = (
    WAITING_TIMEOUT_SECS  # supp. sent Offer, expecting Accept
)
_AFFIRM_WAIT_TIME: Final[float] = (
    CONFIRM_TIMEOUT_SECS  # resp. sent Accept, expecting Confirm
)
_RATIFY_WAIT_TIME: Final[float] = (
    CONFIRM_TIMEOUT_SECS  # resp. rcvd Confirm, expecting Ratify (10E0)
)


BINDING_QOS = QosParams(
    max_retries=SENDING_RETRY_LIMIT,
    timeout=WAITING_TIMEOUT_SECS * 2,
    wait_for_reply=False,
)


class Vendor(StrEnum):
    """Enumeration of recognized hardware vendors."""

    BROFER = "brofer"
    CLIMARAD = "climarad"
    ITHO = "itho"
    NUAIRE = "nuaire"
    ORCON = "orcon"
    VASCO = "vasco"
    DEFAULT = "default"


SZ_CLASS: Final = "class"
SZ_VENDOR: Final = "vendor"
SZ_TENDER: Final = "tender"
SZ_AFFIRM: Final = "affirm"
SZ_RATIFY: Final = "ratify"

# VOL_SUPPLICANT_ID = vol.Match(re.compile(r"^03:[0-9]{6}$"))
VOL_CODE_REGEX = vol.Match(re.compile(r"^[0-9A-F]{4}$"))
VOL_OEM_ID_REGEX = vol.Match(re.compile(r"^[0-9A-F]{2}$"))

VOL_TENDER_CODES = vol.All(
    {vol.Required(VOL_CODE_REGEX, default="00"): VOL_OEM_ID_REGEX},
    vol.Length(min=1),
)

VOL_SUPPLICANT = vol.Schema(
    {
        vol.Required(SZ_CLASS): vol.Any(DevType.THM.value, DevType.DHW.value),
        vol.Optional(SZ_VENDOR, default="honeywell"): vol.Any(
            "honeywell", "resideo", *(m.value for m in Vendor)
        ),
        vol.Optional(SZ_TENDER): VOL_TENDER_CODES,
        vol.Optional(SZ_AFFIRM, default={}): vol.Any({}),
        vol.Optional(SZ_RATIFY, default=None): vol.Any(None),
    },
    extra=vol.PREVENT_EXTRA,
)


class BindPhase(StrEnum):
    """Enumeration representing the phase of the binding process."""

    TENDER = "offer"
    ACCEPT = "accept"
    AFFIRM = "confirm"
    RATIFY = "addenda"  # Code._10E0


class BindRole(StrEnum):
    """Enumeration representing the binding role of a device."""

    RESPONDENT = "respondent"
    SUPPLICANT = "supplicant"
    IS_DORMANT = "is_dormant"
    IS_UNKNOWN = "is_unknown"


SCHEME_LOOKUP = {
    Vendor.ITHO: {"oem_code": "01"},
    Vendor.BROFER: {"oem_code": "6A"},
    Vendor.NUAIRE: {"oem_code": "6C"},
    Vendor.CLIMARAD: {"oem_code": "65"},
    Vendor.VASCO: {"oem_code": "66"},
    Vendor.ORCON: {"oem_code": "67", "offer_to": ALL_DEVICE_ID},
    Vendor.DEFAULT: {"oem_code": None},
}


#


class BindingManagerBase:
    """The manager is the core service. It should be initiated with a default state."""

    _attr_role = BindRole.IS_UNKNOWN

    _is_respondent: bool | None  # if binding, is either: respondent or supplicant
    _state: BindStateBase = None  # type: ignore[assignment]

    def __init__(self, dev: DeviceInterface, dispatcher: CommandDispatcher) -> None:
        """Initialize the binding manager.

        :param dev: The device interface context for this binding service.
        :param dispatcher: The command dispatcher callback.
        """
        self._dev = dev
        self._dispatcher = dispatcher
        self._loop = asyncio.get_running_loop()
        self._fut: asyncio.Future[Message] | None = None

        self.set_state(DevIsNotBinding)

    def __repr__(self) -> str:
        return f"{self._dev.id} ({self.role}): {self.state!r}"

    def __str__(self) -> str:
        return f"{self._dev.id}: {self.state}"

    def cancel(self) -> None:
        """Cancel any pending wait timer on the current state.

        This should be called during teardown (e.g. gateway.stop()) to ensure
        no ``call_later`` timer handles outlive the binding manager.
        """
        timer = getattr(self._state, "_timer_handle", None) if self._state else None
        if timer:
            timer.cancel()

    def set_state(
        self, state: type[BindStateBase], result: asyncio.Future[Message] | None = None
    ) -> None:
        """Transition the State of the Manager, and process the result, if any.

        :param state: The new state class to transition into.
        :param result: The future result carrying the preceding message state, optional.
        """
        # Ensure prev_state is always available, not only during debugging
        prev_state = self._state

        # if False and result:
        #     try:
        #         self._fut.set_result(result.result())
        #     except exc.BindingError as err:
        #         self._fut.set_result(err)

        # Cancel any pending timer from the previous state before transitioning
        timer = getattr(prev_state, "_timer_handle", None) if prev_state else None
        if timer:
            timer.cancel()

        self._state = state(self)
        if not self.is_binding:
            self._is_respondent = None
        elif state is RespIsWaitingForOffer:
            self._is_respondent = True
        elif state is SuppSendOfferWaitForAccept:
            self._is_respondent = False

        # Log binding completion transitions
        if isinstance(
            self._state, (RespHasBoundAsRespondent, SuppHasBoundAsSupplicant)
        ):
            _LOGGER.info(
                f"{self._dev.id}: Binding process completed: "
                f"{type(prev_state).__name__} -> {state.__name__} (role: {self.role})"
            )

        if _DBG_MAINTAIN_STATE_CHAIN:  # HACK for debugging
            setattr(self._state, "_prev_state", prev_state)  # noqa: B010

    @property
    def state(self) -> BindStateBase:
        """Return the State (phase) of the Manager."""
        return self._state

    @property
    def role(self) -> BindRole:
        """Return the current binding role."""
        if self._is_respondent is True:
            return BindRole.RESPONDENT
        if self._is_respondent is False:
            return BindRole.SUPPLICANT
        return BindRole.IS_DORMANT

    # TODO: Should remain is_binding until after 10E0 rcvd (if one expected)?
    @property
    def is_binding(self) -> bool:
        """Return True if currently participating in a binding process."""
        return not isinstance(self.state, _IS_NOT_BINDING_STATES)

    def rcvd_msg(self, msg: Message) -> None:
        """Pass relevant Messages through to the state processor.

        :param msg: The incoming message to process.
        """
        if msg.code in (Code._1FC9, Code._10E0):
            self.state.rcvd_msg(msg)

    def sent_cmd(self, cmd: Command) -> None:
        """Pass relevant Commands through to the state processor.

        :param cmd: The outgoing command to process.
        """
        if cmd.code in (Code._1FC9, Code._10E0):
            self.state.send_cmd(cmd)


class BindingManagerRespondent(BindingManagerBase):
    """The binding Manager for a Respondent."""

    _attr_role = BindRole.RESPONDENT

    async def wait_for_binding_request(
        self,
        accept_codes: Iterable[Code],
        /,
        *,
        idx: IndexT = "00",
        require_ratify: bool = False,
    ) -> tuple[Message, Packet, Message, Message | None]:
        """Device starts binding as a Respondent, by listening for an Offer.

        Returns the Supplicant's Offer or raises an exception if the binding is
        unsuccessful (BindError).

        :param accept_codes: An iterable of codes this device accepts.
        :param idx: The index to bind to, defaults to '00'.
        :param require_ratify: If True, require an addenda stage.
        :return: A tuple containing the (tender, accept, affirm, ratify) objects.
        :raises exc.BindingFsmError: If already binding.
        """

        if self.is_binding:
            raise exc.BindingFsmError(
                f"{self}: bad State for bindings as a Respondent (is already binding)"
            )
        self.set_state(RespIsWaitingForOffer)  # self._is_respondent = True

        # Step R1: Respondent expects an Offer
        tender = await self._wait_for_offer()

        # Step R2: Respondent expects a Confirm after sending an Accept (accepts Offer)
        accept = await self._accept_offer(tender, accept_codes, idx=idx)
        affirm = await self._wait_for_confirm(accept)

        # Step R3: Respondent expects an Addenda (optional)
        if require_ratify:  # TODO: not recvd as sent to 63:262142
            self.set_state(RespIsWaitingForAddenda)  # HACK: easiest way
            ratify = await self._wait_for_addenda(accept)  # may: exc.BindingFlowFailed:
        else:
            ratify = None

        # self._set_as_bound(tender, accept, affirm, ratify)
        return tender, accept, affirm, ratify

    async def _wait_for_offer(self, timeout: float = _TENDER_WAIT_TIME) -> Message:
        """Resp waits timeout seconds for an Offer to arrive & returns it.

        :param timeout: Time to wait in seconds.
        :return: The offer message.
        """
        return await self.state.wait_for_offer(timeout)

    async def _accept_offer(
        self, tender: Message, codes: Iterable[Code], idx: IndexT = "00"
    ) -> Packet:
        """Resp sends an Accept on the basis of a rcvd Offer & returns the Confirm.

        :param tender: The received offer message.
        :param codes: Iterable of codes accepted.
        :param idx: The bound index.
        :return: The sent accept packet.
        """

        cmd = Command.put_bind(W_, self._dev.id, codes, dst_id=tender.src.id, idx=idx)
        if not _DBG_DISABLE_PHASE_ASSERTS:  # TODO: should be in test suite
            assert Message._from_cmd(cmd).payload["phase"] == BindPhase.ACCEPT

        pkt: Packet = await self._dispatcher(  # type: ignore[assignment]
            cmd, priority=Priority.HIGH, qos=BINDING_QOS
        )

        self.state.cast_accept_offer()
        return pkt

    async def _wait_for_confirm(
        self,
        accept: Packet,
        timeout: float = _AFFIRM_WAIT_TIME,
    ) -> Message:
        """Resp waits timeout seconds for a Confirm to arrive & returns it.

        :param accept: The accept packet previously sent.
        :param timeout: Time to wait in seconds.
        :return: The confirm message.
        """
        return await self.state.wait_for_confirm(timeout)

    async def _wait_for_addenda(
        self,
        accept: Packet,
        timeout: float = _RATIFY_WAIT_TIME,
    ) -> Message:
        """Resp waits timeout seconds for an Addenda to arrive & returns it.

        :param accept: The accept packet previously sent.
        :param timeout: Time to wait in seconds.
        :return: The addenda message.
        """
        return await self.state.wait_for_addenda(timeout)


class BindingManagerSupplicant(BindingManagerBase):
    """The binding Manager for a Supplicant."""

    _attr_role = BindRole.SUPPLICANT

    async def initiate_binding_process(
        self,
        offer_codes: Iterable[Code],
        /,
        *,
        confirm_code: Code | None = None,
        ratify_cmd: Command | None = None,
    ) -> tuple[Packet, Message, Packet, Packet | None]:
        """Device starts binding as a Supplicant, by sending an Offer.

        Returns the Respondent's Accept, or raises an exception if the binding is
        unsuccessful (BindError).

        :param offer_codes: An iterable of codes to offer.
        :param confirm_code: An optional confirm code override.
        :param ratify_cmd: An optional ratification command to finalize binding.
        :return: A tuple containing the (tender, accept, affirm, ratify) objects.
        :raises exc.BindingFsmError: If already binding.
        """

        if self.is_binding:
            raise exc.BindingFsmError(
                f"{self}: bad State for binding as a Supplicant (is already binding)"
            )
        self.set_state(SuppSendOfferWaitForAccept)  # self._is_respondent = False

        oem_code = ratify_cmd.payload[14:16] if ratify_cmd else None

        # Step S1: Supplicant sends an Offer (makes Offer) and expects an Accept
        tender = await self._make_offer(offer_codes, oem_code=oem_code)
        accept = await self._wait_for_accept(tender)

        # Step S2: Supplicant sends a Confirm (confirms Accept)
        affirm = await self._confirm_accept(accept, confirm_code=confirm_code)

        # Step S3: Supplicant sends an Addenda (optional)
        if oem_code:
            self.set_state(SuppIsReadyToSendAddenda)  # HACK: easiest way
            ratify = await self._cast_addenda(accept, ratify_cmd)  # type: ignore[arg-type]
        else:
            ratify = None

        # self._set_as_bound(tender, accept, affirm, ratify)
        return tender, accept, affirm, ratify

    async def _make_offer(
        self,
        codes: Iterable[Code],
        oem_code: str | None = None,
    ) -> Packet:
        """Supp sends an Offer & returns the corresponding Packet.

        :param codes: Codes to offer.
        :param oem_code: Optional OEM specific code block.
        :return: The sent offer packet.
        """
        # if oem_code, send an 10E0

        # state = self.state
        cmd = Command.put_bind(
            I_, self._dev.id, codes, dst_id=self._dev.id, oem_code=oem_code
        )
        if not _DBG_DISABLE_PHASE_ASSERTS:  # TODO: should be in test suite
            assert Message._from_cmd(cmd).payload["phase"] == BindPhase.TENDER

        pkt: Packet = await self._dispatcher(  # type: ignore[assignment]
            cmd, priority=Priority.HIGH, qos=BINDING_QOS
        )

        # await state._fut
        self.state.cast_offer()
        return pkt

    async def _wait_for_accept(
        self,
        tender: Packet,
        timeout: float = _ACCEPT_WAIT_TIME,
    ) -> Message:
        """Supp waits timeout seconds for an Accept to arrive & returns it.

        :param tender: The previously sent offer packet.
        :param timeout: Time to wait in seconds.
        :return: The accept message.
        """
        return await self.state.wait_for_accept(timeout)

    async def _confirm_accept(
        self, accept: Message, confirm_code: Code | None = None
    ) -> Packet:
        """Supp casts a Confirm on the basis of a rcvd Accept & returns the Confirm.

        :param accept: The received accept message.
        :param confirm_code: The code to confirm with.
        :return: The sent confirm packet.
        """

        idx = accept._pkt.payload[:2]  # HACK assumes all idx same

        cmd = Command.put_bind(
            I_, self._dev.id, confirm_code, dst_id=accept.src.id, idx=idx
        )
        if not _DBG_DISABLE_PHASE_ASSERTS:  # TODO: should be in test suite
            assert Message._from_cmd(cmd).payload["phase"] == BindPhase.AFFIRM

        pkt: Packet = await self._dispatcher(  # type: ignore[assignment]
            cmd, priority=Priority.HIGH, qos=BINDING_QOS
        )

        await self.state.cast_confirm_accept()
        return pkt

    async def _cast_addenda(self, accept: Message, cmd: Command) -> Packet:
        """Supp casts an Addenda (the final 10E0 command).

        :param accept: The previously received accept message.
        :param cmd: The ratify command to cast.
        :return: The sent addenda packet.
        """

        pkt: Packet = await self._dispatcher(  # type: ignore[assignment]
            cmd, priority=Priority.HIGH, qos=BINDING_QOS
        )

        await self.state.cast_addenda()
        return pkt


class BindingManager(BindingManagerRespondent, BindingManagerSupplicant):
    """Aggregate manager handling both Respondent and Supplicant flows."""

    _attr_role = BindRole.IS_UNKNOWN


#


class BindStateBase:
    """Base class for all phases within the Binding Finite State Machine."""

    _attr_role = BindRole.IS_UNKNOWN

    _cmds_sent: int = 0  # num of bind cmds sent
    _pkts_rcvd: int = 0  # num of bind pkts rcvd (incl. any echos of sender's own cmd)

    _has_wait_timer: bool = False
    _retry_limit: int = SENDING_RETRY_LIMIT
    _timer_handle: asyncio.TimerHandle

    _next_ctx_state: type[BindStateBase]  # next state, if successful transition

    def __init__(self, context: BindingManagerBase) -> None:
        """Initialize the binding state.

        :param context: The binding manager operating this state.
        """
        self._context = context
        self._loop = context._loop

        # Strong typing on Future ensures .result() correctly returns a Message
        self._fut: asyncio.Future[Message] = self._loop.create_future()
        _LOGGER.debug(f"{self}: Changing state from: {self._context.state} to: {self}")

        if self._has_wait_timer:
            self._timer_handle = self._loop.call_later(
                WAITING_TIMEOUT_SECS,
                self._handle_wait_timer_expired,
                WAITING_TIMEOUT_SECS,
            )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__} (tx={self._cmds_sent})"

    def __str__(self) -> str:
        return self.__class__.__name__

    @property
    def context(self) -> BindingManagerBase:
        """Return the associated manager for this state."""
        return self._context

    async def _wait_for_fut_result(self, timeout: float) -> Message:
        """Wait timeout seconds for an expected event to occur.

        The expected event is defined by the State's sent_cmd, rcvd_msg methods.

        :param timeout: The maximum time to wait in seconds.
        :return: The message containing the expected result.
        """
        try:
            # Wrap in shield to prevent asyncio.wait_for from cancelling the future.
            await asyncio.wait_for(asyncio.shield(self._fut), timeout)
        except TimeoutError:
            self._handle_wait_timer_expired(timeout)
        else:
            self._set_context_state(self._next_ctx_state)

        return self._fut.result()  # may raise exception

    def _handle_wait_timer_expired(self, timeout: float) -> None:
        """Process an overrun of the wait timer when waiting for a Message.

        :param timeout: The timeout limit that was exceeded.
        """
        msg = (
            f"{self._context}: Failed to transition to {self._next_ctx_state}: "
            f"expected message not received after {timeout} secs"
        )

        _LOGGER.warning(msg)
        if not self._fut.done():
            self._fut.set_exception(exc.BindingTimeoutError(msg))
        self._set_context_state(DevHasFailedBinding)

    def _set_context_state(self, next_state: type[BindStateBase]) -> None:
        """Transition the parent manager to a new state.

        :param next_state: The class representing the next state.
        :raises exc.BindingFsmError: If the future was not yet completed.
        """
        if not self._fut.done():  # if not BindRetryError, BindTimeoutError, msg
            raise exc.BindingFsmError  # or: self._fut.set_exception()
        self._context.set_state(next_state, result=self._fut)

    def send_cmd(self, cmd: Command) -> None:
        """Abstract method to handle an outgoing command.

        :param cmd: The command that is being sent.
        """
        raise NotImplementedError

    def rcvd_msg(self, msg: Message) -> None:
        """Abstract method to handle an incoming message.

        :param msg: The message that was received.
        """
        raise NotImplementedError

    @staticmethod
    def is_phase(cmd: Command | Message, phase: BindPhase) -> bool:
        """Evaluate if the given command or message corresponds to the specified binding phase.

        :param cmd: The command or message object.
        :param phase: The binding phase to test against.
        :return: True if the command is aligned with the phase, False otherwise.
        """
        if phase == BindPhase.RATIFY:
            return cmd.verb == I_ and cmd.code == Code._10E0
        if cmd.code != Code._1FC9:
            return False
        if phase == BindPhase.TENDER:
            return cmd.verb == I_ and cmd.dst in (cmd.src, ALL_DEV_ADDR)
        if phase == BindPhase.ACCEPT:
            return cmd.verb == W_ and cmd.dst is not cmd.src
        # if phase == BindPhase.AFFIRM:
        return cmd.verb == I_ and cmd.dst not in (cmd.src, ALL_DEV_ADDR)

    # Respondent State APIs...
    async def wait_for_offer(self, timeout: float | None = None) -> Message:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_offer() from this State"
        )

    def cast_accept_offer(self) -> None:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't accept_offer() from this State"
        )

    async def wait_for_confirm(self, timeout: float | None = None) -> Message:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_confirm() from this State"
        )

    async def wait_for_addenda(self, timeout: float | None = None) -> Message:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_addenda() from this State"
        )

    # Supplicant State APIs...
    def cast_offer(self, timeout: float | None = None) -> None:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't make_offer() from this State"
        )

    async def wait_for_accept(self, timeout: float | None = None) -> Message:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_accept() from this State"
        )

    async def cast_confirm_accept(self, timeout: float | None = None) -> Message:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't confirm_accept() from this State"
        )

    async def cast_addenda(self, timeout: float | None = None) -> Message:
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't cast_addenda() from this State"
        )


class _DevIsWaitingForMsg(BindStateBase):
    """Device waits until it receives the anticipated Packet (Offer or Addenda).

    Failure occurs when the timer expires (timeout) before receiving the Packet.
    """

    _expected_pkt_phase: BindPhase

    _wait_timer_limit: float = 5.1  # WAITING_TIMEOUT_SECS

    def __init__(self, context: BindingManagerBase) -> None:
        super().__init__(context)

        self._timer_handle = self._loop.call_later(
            self._wait_timer_limit,
            self._handle_wait_timer_expired,
            self._wait_timer_limit,
        )

    def _set_context_state(self, next_state: type[BindStateBase]) -> None:
        if self._timer_handle:
            self._timer_handle.cancel()
        super()._set_context_state(next_state)

    def rcvd_msg(self, msg: Message) -> None:
        """If the msg is the waited-for pkt, transition to the next state."""
        if not self._fut.done() and self.is_phase(msg, self._expected_pkt_phase):
            self._fut.set_result(msg)


class _DevIsReadyToSendCmd(BindStateBase):
    """Device sends a Command (Confirm, Addenda) that wouldn't result in a reply Packet.

    Failure occurs when the retry limit is exceeded before receiving a Command echo.
    """

    _expected_cmd_phase: BindPhase

    _send_retry_limit: int = 0  # retries dont include the first send
    _send_retry_timer: float = 0.8  # retry if no echo received before timeout

    def __init__(self, context: BindingManagerBase) -> None:
        super().__init__(context)

        self._cmd: Command | None = None
        self._cmds_sent: int = 0

    def _retries_exceeded(self) -> None:
        """Process an overrun of the retry limit when sending a Command."""

        msg = (
            f"{self._context}: Failed to transition to {self._next_ctx_state}: "
            f"{self._expected_cmd_phase} command echo not received after "
            f"{self._retry_limit} retries"
        )

        _LOGGER.warning(msg)
        if not self._fut.done():
            self._fut.set_exception(exc.BindingFlowFailed(msg))
        self._set_context_state(DevHasFailedBinding)

    def send_cmd(self, cmd: Command) -> None:
        """If sending a cmd, expect the corresponding echo."""

        if not self.is_phase(cmd, self._expected_cmd_phase):
            return

        if self._cmds_sent > self._send_retry_limit:
            self._retries_exceeded()
        self._cmds_sent += 1
        self._cmd = self._cmd or cmd

    def rcvd_msg(self, msg: Message) -> None:
        """If the msg is the echo of the sent cmd, transition to the next state."""
        if self._fut.done():
            return
        if (self._cmd and msg._pkt == self._cmd) or (
            self.is_phase(msg, self._expected_cmd_phase)
            and msg.src.id == self._context._dev.id
        ):
            self._fut.set_result(msg)


class _DevSendCmdUntilReply(_DevIsWaitingForMsg, _DevIsReadyToSendCmd):
    """Device sends a Command (Offer, Accept), until it gets the expected reply Packet.

    Failure occurs when the timer expires (timeout) or the retry limit is exceeded
    before receiving a reply Packet.
    """

    def rcvd_msg(self, msg: Message) -> None:
        """If the msg is the expected reply, transition to the next state."""
        if not self._fut.done() and self.is_phase(msg, self._expected_pkt_phase):
            self._fut.set_result(msg)


class DevHasFailedBinding(BindStateBase):
    """Device has failed binding."""

    _attr_role = BindRole.IS_UNKNOWN


class DevIsNotBinding(BindStateBase):
    """Device is not binding."""

    _attr_role = BindRole.IS_DORMANT


#


class RespHasBoundAsRespondent(BindStateBase):
    """Respondent has received an Offer (+/- an Addenda) & has nothing more to do."""

    _attr_role = BindRole.IS_DORMANT

    def __init__(self, context: BindingManagerBase) -> None:
        super().__init__(context)
        _LOGGER.info(f"{context._dev.id}: Binding completed as respondent")


class RespIsWaitingForAddenda(_DevIsWaitingForMsg, BindStateBase):
    """Respondent has received a Confirm & is waiting for an Addenda."""

    _attr_role = BindRole.RESPONDENT

    _expected_pkt_phase: BindPhase = BindPhase.RATIFY
    _next_ctx_state: type[BindStateBase] = RespHasBoundAsRespondent

    async def wait_for_addenda(self, timeout: float | None = None) -> Message:
        return await self._wait_for_fut_result(timeout or _RATIFY_WAIT_TIME)


class RespSendAcceptWaitForConfirm(_DevSendCmdUntilReply, BindStateBase):
    """Respondent is ready to send an Accept & will expect a Confirm."""

    _attr_role = BindRole.RESPONDENT

    _expected_cmd_phase: BindPhase = BindPhase.ACCEPT
    _expected_pkt_phase: BindPhase = BindPhase.AFFIRM
    _next_ctx_state: type[BindStateBase] = (
        RespHasBoundAsRespondent  # or: RespIsWaitingForAddenda
    )

    def cast_accept_offer(self) -> None:
        """Ignore any received Offer, other than the first."""
        pass

    async def wait_for_confirm(self, timeout: float | None = None) -> Message:
        return await self._wait_for_fut_result(timeout or _AFFIRM_WAIT_TIME)


class RespIsWaitingForOffer(_DevIsWaitingForMsg, BindStateBase):
    """Respondent is waiting for an Offer."""

    _attr_role = BindRole.RESPONDENT

    _expected_pkt_phase: BindPhase = BindPhase.TENDER
    _next_ctx_state: type[BindStateBase] = RespSendAcceptWaitForConfirm

    async def wait_for_offer(self, timeout: float | None = None) -> Message:
        return await self._wait_for_fut_result(timeout or _TENDER_WAIT_TIME)


#


class SuppHasBoundAsSupplicant(BindStateBase):
    """Supplicant has sent a Confirm (+/- an Addenda) & has nothing more to do."""

    _attr_role = BindRole.IS_DORMANT

    def __init__(self, context: BindingManagerBase) -> None:
        super().__init__(context)
        _LOGGER.info(f"{context._dev.id}: Binding completed as supplicant")


class SuppIsReadyToSendAddenda(
    _DevIsReadyToSendCmd, BindStateBase
):  # send until echo, max_retry=1
    """Supplicant has sent a Confirm & is ready to send an Addenda."""

    _attr_role = BindRole.SUPPLICANT

    _expected_cmd_phase: BindPhase = BindPhase.RATIFY
    _next_ctx_state: type[BindStateBase] = SuppHasBoundAsSupplicant

    async def cast_addenda(self, timeout: float | None = None) -> Message:
        return await self._wait_for_fut_result(timeout or _ACCEPT_WAIT_TIME)


class SuppIsReadyToSendConfirm(
    _DevIsReadyToSendCmd, BindStateBase
):  # send until echo, max_retry=1
    """Supplicant has received an Accept & is ready to send a Confirm."""

    _attr_role = BindRole.SUPPLICANT

    _expected_cmd_phase: BindPhase = BindPhase.AFFIRM
    _next_ctx_state: type[BindStateBase] = (
        SuppHasBoundAsSupplicant  # or: SuppIsReadyToSendAddenda
    )

    async def cast_confirm_accept(self, timeout: float | None = None) -> Message:
        return await self._wait_for_fut_result(timeout or _ACCEPT_WAIT_TIME)


class SuppSendOfferWaitForAccept(_DevSendCmdUntilReply, BindStateBase):
    """Supplicant is ready to send an Offer & will expect an Accept."""

    _attr_role = BindRole.SUPPLICANT

    _expected_cmd_phase: BindPhase = BindPhase.TENDER
    _expected_pkt_phase: BindPhase = BindPhase.ACCEPT
    _next_ctx_state: type[BindStateBase] = SuppIsReadyToSendConfirm

    def cast_offer(self, timeout: float | None = None) -> None:
        pass

    async def wait_for_accept(self, timeout: float | None = None) -> Message:
        return await self._wait_for_fut_result(timeout or _ACCEPT_WAIT_TIME)


#


class _BindStates:  # used for test suite
    IS_IDLE_DEVICE = DevIsNotBinding  # may send Offer
    NEEDING_TENDER = RespIsWaitingForOffer  # receives Offer, sends Accept
    NEEDING_ACCEPT = SuppSendOfferWaitForAccept  # receives Accept, sends
    NEEDING_AFFIRM = RespSendAcceptWaitForConfirm
    TO_SEND_AFFIRM = SuppIsReadyToSendConfirm
    NEEDING_RATIFY = RespIsWaitingForAddenda  # Optional: has sent Confirm
    TO_SEND_RATIFY = SuppIsReadyToSendAddenda  # Optional
    HAS_BOUND_RESP = RespHasBoundAsRespondent
    HAS_BOUND_SUPP = SuppHasBoundAsSupplicant
    IS_FAILED_RESP = DevHasFailedBinding
    IS_FAILED_SUPP = DevHasFailedBinding


_IS_NOT_BINDING_STATES = (
    DevHasFailedBinding,
    DevIsNotBinding,
    RespHasBoundAsRespondent,
    SuppHasBoundAsSupplicant,
)

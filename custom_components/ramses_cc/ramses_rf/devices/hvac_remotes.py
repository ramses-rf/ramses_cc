"""RAMSES RF - HVAC Remote Devices."""

from __future__ import annotations

from datetime import timedelta as td
from typing import TYPE_CHECKING, Any

from .. import exceptions as exc
from ..const import (
    HEARTBEAT_TIMEOUT_REMOTE,
    SZ_BOOST_TIMER,
    SZ_FAN_MODE,
    Code,
    DevType,
)
from ..models import DeviceTraits, HvacState
from ...ramses_tx import Command, Packet, Priority

from .dev_base import BatteryState, DeviceHvac, Fakeable

if TYPE_CHECKING:
    from ..messages import Message


class HvacRemoteBase(DeviceHvac):
    """Base class for HVAC remote control devices.

    This class serves as a base for all remote control devices in the HVAC domain.
    It provides common functionality and interfaces for remote control operations.
    """

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the HvacRemoteBase class.

        :param args: Positional arguments passed to the parent class
        :param traits: Strictly typed traits object for device creation
        :param kwargs: Keyword arguments passed to the parent class
        """
        super().__init__(*args, traits=traits, **kwargs)
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    def _post_class_promote(self) -> None:
        """Initialize state when promoted from a generic HVAC device."""
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout before the device is considered unavailable.

        :return: The timeout duration.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_REMOTE


class HvacRemote(BatteryState, Fakeable, HvacRemoteBase):  # REM: I/22F[138]
    """The REM (remote/switch) class, such as a 4-way switch.

    The cardinal codes are 22F1, 22F3 (also 22F8?).
    """

    _SLUG: str = DevType.REM

    async def initiate_binding_process(
        self,
    ) -> tuple[Packet, Message, Packet, Packet | None]:
        # .I --- 37:155617 --:------ 37:155617 1FC9 024 00-22F1-965FE1 00-22F3-965FE1 67-10E09-65FE1 00-1FC9-965FE1
        # .W --- 32:155617 37:155617 --:------ 1FC9 012 00-31D9-825FE1 00-31DA-825FE1
        # .I --- 37:155617 32:155617 --:------ 1FC9 001 00

        return await super()._initiate_binding_process(
            Code._22F1 if self._scheme == "nuaire" else (Code._22F1, Code._22F3)
        )

    async def fan_rate(self) -> str | None:
        """Get the current fan rate setting.

        :return: The fan rate as a string, or None if not available
        :rtype: str | None
        :note: This is a work in progress - rate can be either int or str
        """
        return self.hvac_state.fan_rate

    async def set_fan_rate(self, value: int) -> Packet | None:
        """Set a fake fan rate for the remote control.

        :param value: The fan rate to set (can be int or str, but not None)
        :type value: int
        :raises TypeError: If the remote is not in faked mode
        :return: The sent packet
        :rtype: Packet
        :note: This is a work in progress
        """

        if not self.is_faked:  # NOTE: some remotes are stateless (i.e. except seqn)
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        # TODO: num_repeats=2, or wait_for_reply=True ?

        # NOTE: this is not completely understood (i.e. diffs between vendor schemes)
        cmd = Command.set_fan_mode(self.id, int(4 * value), src_id=self.id)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def fan_mode(self) -> str | None:
        """Return the current fan mode.

        :return: The fan mode as a string, or None if not available
        :rtype: str | None
        """
        return self.hvac_state.fan_mode

    async def boost_timer(self) -> int | None:
        """Return the remaining boost timer in minutes.

        :return: The remaining boost time in minutes, or None if boost is not active
        :rtype: int | None
        """
        return self.hvac_state.boost_timer_mins

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            SZ_FAN_MODE: await self.fan_mode(),
            SZ_BOOST_TIMER: await self.boost_timer(),
        }


class HvacDisplayRemote(HvacRemote):  # DIS
    """The DIS (display switch)."""

    _SLUG: str = DevType.DIS

    # async def initiate_binding_process(self) -> tuple[Packet, Message, Packet, Packet | None]:
    #     return await super()._initiate_binding_process(
    #         (Code._31E0, Code._1298, Code._2E10)
    #     )


_REMOTES = {
    "21800000": {
        "name": "Orcon 15RF",
        "mode": "1,2,3,T,Auto,Away",
    },
    "21800060": {
        "name": "Orcon 15RF Display",
        "mode": "1,2,3,T,Auto,Away",
    },
    "xxx": {
        "name": "Orcon CO2 Control",
        "mode": "1T,2T,3T,Auto,Away",
    },
    "03-00062": {
        "name": "RFT-SPIDER",
        "mode": "1,2,3,T,A",
    },
    "04-00045": {"name": "RFT-CO2"},  # mains-powered
    "04-00046": {"name": "RFT-RV"},
    "545-7550": {
        "name": "RFT-PIR",
    },
    "536-0124": {  # idx="00"
        "name": "RFT",
        "mode": "1,2,3,T",
        "CVE": False,  # not clear
        "HRV": True,
    },
    "536-0146": {  # idx="??"
        "name": "RFT-DF",
        "mode": "",
        "CVE": True,
        "HRV": False,
    },
    "536-0150": {  # idx = "63"
        "name": "RFT-AUTO",
        "mode": "1,Auto,3,T",
        "CVE": True,
        "HRV": True,
    },
}

# see: https://github.com/arjenhiemstra/ithowifi/blob/master/software/NRG_itho_wifi/src/IthoPacket.h

"""
Itho Remote (model) enums.

CVE/HRU remote (536-0124) RFT W: 3 modes, timer
-------------------------------------------------

.. table:: 536-0124
   :widths: auto

   ===========  =========================  ================================================
    "away":     (Code._22F1, 00, 01|04"),  how to invoke?
    "low":      (Code._22F1, 00, 02|04"),  aka eco
    "medium":   (Code._22F1, 00, 03|04"),  aka auto (with sensors) - is that only for 63?
    "high":     (Code._22F1, 00, 04|04"),  aka full

    "timer_1":  (Code._22F3, 00, 00|0A"),  10 minutes full speed
    "timer_2":  (Code._22F3, 00, 00|14"),  20 minutes full speed
    "timer_3":  (Code._22F3, 00, 00|1E"),  30 minutes full speed
   ===========  =========================  ================================================

RFT-AUTO (536-0150) RFT CAR: 2 modes, auto, timer: idx = 63, essentially same as above, but also...
-----------------------------------------------------------------------------------------------------

.. table:: 536-0150
   :widths: auto

   =============  =========================  ================================================
   "auto_night":  (Code._22F8, 63, 02|03"),  additional - press auto x2
   =============  =========================  ================================================

RFT-RV (04-00046), RFT-CO2 (04-00045) - sensors with control
------------------------------------------------------------

.. table:: 04-00046
   :widths: auto

   ==============  ========================================   =============
    "medium":      (Code._22F1, 00, 03|07"),                  1=away, 2=low?
    "auto":        (Code._22F1, 00, 05|07"),                  4=high
    "auto_night":  (Code._22F1, 00, 0B|0B"),

    "timer_1":     (Code._22F3, 00, 00|0A, 00|00, 0000"),     10 minutes
    "timer_2":     (Code._22F3, 00, 00|14, 00|00, 0000"),     20 minutes
    "timer_3":     (Code._22F3, 00, 00|1E, 00|00, 0000"),     30 minutes
   ==============  ========================================   =============

RFT-PIR (545-7550) - presence sensor
------------------------------------

RFT_DF: DemandFlow remote (536-0146)
------------------------------------

.. table:: 536-0146
   :widths: auto

   ===========  ================================  =========================================
    "timer_1":  (Code._22F3, 00, 42|03, 03|03"),  0b01-000-010 = 3 hrs, back to last mode
    "timer_2":  (Code._22F3, 00, 42|06, 03|03"),  0b01-000-010 = 6 hrs, back to last mode
    "timer_3":  (Code._22F3, 00, 42|09, 03|03"),  0b01-000-010 = 9 hrs, back to last mode
    "cook_30":  (Code._22F3, 00, 02|1E, 02|03"),  30 mins (press 1x)
    "cook_60":  (Code._22F3, 00, 02|3C, 02|03"),  60 mins (press 2x)

    "low":      (Code._22F8, 00, 01|02"),         ?eco     co2 <= 1200 ppm?
    "high":     (Code._22F8, 00, 02|02"),         ?comfort co2 <= 1000 ppm?
   ===========  ================================  =========================================


Join commands:
--------------

.. table:: join per accessory type
   :widths: auto

   ==========  =================  =====================  =========================  ==========================  =========================  ==========================  =================  ==========
   type        set 1              set 2                  set 3                      set 4                       set 5                      set 6                       description        art #
   ==========  =================  =====================  =========================  ==========================  =========================  ==========================  =================  ==========
   "CVERFT":   (Code._1FC9,  00,  Code._22F1, 0x000000,                             01, Code._10E0, 0x000000")                                                         CVE/HRU remote     (536-0124)
   "AUTORFT":  (Code._1FC9,  63,  Code._22F8, 0x000000,                             01, Code._10E0, 0x000000")                                                         AUTO RFT           (536-0150)
   "DF":       (Code._1FC9,  00,  Code._22F8, 0x000000,                             00, Code._10E0, 0x000000")                                                         DemandFlow remote  (536-0146)
   "RV":       (Code._1FC9,  00,  Code._12A0, 0x000000,                             01, Code._10E0, 0x000000,   00, Code._31E0, 0x000000,  00, Code._1FC9, 0x000000")  RFT-RV             (04-00046)
   "CO2":      (Code._1FC9,  00,  Code._1298, 0x000000,  00, Code._2E10, 0x000000,  01, Code._10E0, 0x000000,   00, Code._31E0, 0x000000,  00, Code._1FC9, 0x000000")  RFT-CO2            (04-00045)
   ==========  =================  =====================  =========================  ==========================  =========================  ==========================  =================  ==========

Leave commands:
---------------

.. table:: leave per accessory type
   :widths: auto

   ==========  =================  ======================  =========================  ==========
   type        set 1              set 2                   description                art #
   ==========  =================  ======================  =========================  ==========
   "Others":   (Code._1FC9, 00,   Code._1FC9, 0x000000")  standard leave command
   "AUTORFT":  (Code._1FC9, 63,   Code._1FC9, 0x000000")  leave command of AUTO RFT  (536-0150)
   ==========  =================  ======================  =========================  ==========

.. table:: verbs
   :widths: 2, 4

   ======  ========
   verb    byte
   ======  ========
   ``RQ``  ``0x00``
   ``I_``  ``0x01``
   ``W_``  ``0x02``
   ``RP``  ``0x03``
   ======  ========

"""

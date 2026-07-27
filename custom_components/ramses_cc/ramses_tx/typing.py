#!/usr/bin/env python3
"""RAMSES RF - Typing for RamsesProtocol & RamsesTransport."""

from collections.abc import Awaitable, Callable
from datetime import datetime as dt
from enum import EnumCheck, StrEnum, verify
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NewType,
    NotRequired,
    TypeAlias,
    TypedDict,
    TypeVar,
)

from .const import (
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    DEFAULT_WAIT_FOR_REPLY,
    FaultDeviceClass,
    FaultState,
    FaultType,
    Priority,
)

if TYPE_CHECKING:
    from .dtos import PacketDTO
    from .packet import Packet


# Core Types
DeviceIdT = NewType("DeviceIdT", str)
DevIndexT = NewType("DevIndexT", str)
SerPortNameT = NewType("SerPortNameT", str)
ExceptionT = TypeVar("ExceptionT", bound=Exception)
HeaderT = NewType("HeaderT", str)
PayloadT = NewType("PayloadT", str)


# Device Traits
class DeviceTraitsT(TypedDict):
    alias: str | None
    faked: bool | None
    class_: str | None  # "class" is a reserved keyword


DeviceListT: TypeAlias = dict[DeviceIdT, DeviceTraitsT]


if TYPE_CHECKING:
    MsgFilterT = Callable[["PacketDTO"], bool]
    MsgHandlerT = Callable[["PacketDTO"], Awaitable[None]]
else:
    MsgFilterT = Callable[[Any], bool]
    MsgHandlerT = Callable[[Any], Awaitable[None]]


# QoS & Send Parameters
class QosParams:
    """A container for QoS attributes and state."""

    def __init__(
        self,
        *,
        max_retries: int | None = DEFAULT_MAX_RETRIES,
        timeout: float | None = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> None:
        """Create a QosParams instance."""
        if max_retries is None:
            self._max_retries = DEFAULT_MAX_RETRIES
        else:
            self._max_retries = max_retries

        self._timeout = timeout or DEFAULT_SEND_TIMEOUT
        self._wait_for_reply = wait_for_reply

        self._echo_pkt: Packet | None = None
        self._rply_pkt: Packet | None = None

        self._dt_cmd_sent: dt | None = None
        self._dt_echo_rcvd: dt | None = None
        self._dt_rply_rcvd: dt | None = None

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def wait_for_reply(self) -> bool | None:
        return self._wait_for_reply


class SendParams:
    """A container for Send attributes and state."""

    def __init__(
        self,
        *,
        gap_duration: float | None = DEFAULT_GAP_DURATION,
        num_repeats: int | None = DEFAULT_NUM_REPEATS,
        priority: Priority | None = Priority.DEFAULT,
    ) -> None:
        """Create a SendParams instance."""
        self._gap_duration = gap_duration or DEFAULT_GAP_DURATION
        self._num_repeats = num_repeats or DEFAULT_NUM_REPEATS
        self._priority = priority or Priority.DEFAULT

        self._dt_cmd_arrived: dt | None = None
        self._dt_cmd_queued: dt | None = None
        self._dt_cmd_sent: dt | None = None

    @property
    def gap_duration(self) -> float:
        return self._gap_duration

    @property
    def num_repeats(self) -> int:
        return self._num_repeats

    @property
    def priority(self) -> Priority:
        return self._priority


# TypedDicts (formerly in typed_dicts.py)
_HexToTempT: TypeAlias = float | None


LogIdxT = Literal[
    "00",
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
    "07",
    "08",
    "09",
    "0A",
    "0B",
    "0C",
    "0D",
    "0E",
    "0F",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "1A",
    "1B",
    "1C",
    "1D",
    "1E",
    "1F",
    "20",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
    "27",
    "28",
    "29",
    "2A",
    "2B",
    "2C",
    "2D",
    "2E",
    "2F",
    "30",
    "31",
    "32",
    "33",
    "34",
    "35",
    "36",
    "37",
    "38",
    "39",
    "3A",
    "3B",
    "3C",
    "3D",
    "3E",
    "3F",
]


class _FlowRate(TypedDict):
    dhw_flow_rate: _HexToTempT


class _Pressure(TypedDict):
    pressure: _HexToTempT


class _Setpoint(TypedDict):
    setpoint: _HexToTempT


class _Temperature(TypedDict):
    temperature: _HexToTempT


class FaultLogEntryNull(TypedDict):
    _log_idx: LogIdxT


class FaultLogEntry(TypedDict):
    _log_idx: LogIdxT
    timestamp: str
    fault_state: FaultState
    fault_type: FaultType
    domain_idx: str
    device_class: FaultDeviceClass
    device_id: DeviceIdT | None
    _unknown_3: str
    _unknown_7: str
    _unknown_15: str


class AirQuality(TypedDict):
    air_quality: float | None
    air_quality_basis: NotRequired[str]


class Co2Level(TypedDict):
    co2_level: float | None


class RelativeHumidity(TypedDict):
    relative_humidity: _HexToTempT
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]


class IndoorHumidity(TypedDict):
    indoor_humidity: _HexToTempT
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]


class OutdoorHumidity(TypedDict):
    outdoor_humidity: _HexToTempT
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]


class ExhaustTemp(TypedDict):
    exhaust_temp: _HexToTempT


class SupplyTemp(TypedDict):
    supply_temp: _HexToTempT


class IndoorTemp(TypedDict):
    indoor_temp: _HexToTempT


class OutdoorTemp(TypedDict):
    outdoor_temp: _HexToTempT


class Capabilities(TypedDict):
    speed_capabilities: list[str] | None


class BypassPosition(TypedDict):
    bypass_position: float | None


class FanInfo(TypedDict):
    fan_info: str | None
    _unknown_fan_info_flags: list[int]


class ExhaustFanSpeed(TypedDict):
    exhaust_fan: float | None


class SupplyFanSpeed(TypedDict):
    supply_fan: float | None


class RemainingMins(TypedDict):
    remaining_mins: int | None


class PostHeater(TypedDict):
    post_heater: float | None


class PreHeater(TypedDict):
    pre_heater: float | None


class SupplyFlow(TypedDict):
    supply_flow: float | None


class ExhaustFlow(TypedDict):
    exhaust_flow: float | None


class _VentilationState(
    ExhaustFanSpeed,
    FanInfo,
    AirQuality,
    Co2Level,
    ExhaustTemp,
    SupplyTemp,
    IndoorTemp,
    OutdoorTemp,
    Capabilities,
    BypassPosition,
    SupplyFanSpeed,
    RemainingMins,
    PostHeater,
    PreHeater,
    SupplyFlow,
    ExhaustFlow,
):
    indoor_humidity: _HexToTempT
    outdoor_humidity: _HexToTempT
    extra: NotRequired[str | None]


class _empty(TypedDict):
    pass


class _0004(TypedDict):
    name: NotRequired[str | None]


class _0006(TypedDict):
    change_counter: NotRequired[int | None]


class _0008(TypedDict):
    relay_demand: float | None


class _000a(TypedDict):
    zone_idx: NotRequired[str]
    min_temp: float | None
    max_temp: float | None
    local_override: bool
    openwindow_function: bool
    multiroom_mode: bool
    _unknown_bitmap: str


class _0100(TypedDict):
    language: str
    _unknown_0: str


class _0404(TypedDict):
    frag_number: int
    total_frags: int | None
    frag_length: NotRequired[int | None]
    fragment: NotRequired[str]


class _0418_NULL(TypedDict):
    log_idx: NotRequired[LogIdxT]
    log_entry: None


class _0418(TypedDict):
    log_idx: LogIdxT
    log_entry: tuple[str, ...]


class _1060(TypedDict):
    battery_low: bool
    battery_level: float | None


class _1030(TypedDict):
    max_flow_setpoint: float
    min_flow_setpoint: float
    valve_run_time: int
    pump_run_time: int
    boolean_cc: bool


class _1090(TypedDict):
    temperature_0: float | None
    temperature_1: float | None


class _10a0(TypedDict):
    setpoint: _HexToTempT | None
    overrun: NotRequired[int]
    differential: NotRequired[_HexToTempT]


class _10d0(TypedDict):
    days_remaining: int | None
    days_lifetime: NotRequired[int | None]
    percent_remaining: NotRequired[float | None]


class _10e1(TypedDict):
    device_id: DeviceIdT


class _1100(TypedDict):
    domain_id: NotRequired[str]
    cycle_rate: int
    min_on_time: float
    min_off_time: float
    _unknown_0: str
    proportional_band_width: NotRequired[float | None]
    _unknown_1: NotRequired[str | None]


class _1100_IDX(TypedDict):
    domain_id: str


class _12a0(TypedDict):
    hvac_idx: str
    indoor_humidity: NotRequired[_HexToTempT | None]
    outdoor_humidity: NotRequired[_HexToTempT | None]
    relative_humidity: NotRequired[_HexToTempT | None]
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]
    _unknown_12: NotRequired[str]


class _12b0(TypedDict):
    window_open: bool | None


class _12c0(TypedDict):
    temperature: float | None
    units: Literal["Fahrenheit", "Celsius"]
    _unknown_6: NotRequired[str]


class _1f09(TypedDict):
    remaining_seconds: float
    _next_sync: str


class _1f41(TypedDict):
    mode: str
    active: NotRequired[bool | None]
    until: NotRequired[str | None]


class _1fd4(TypedDict):
    ticker: int


@verify(EnumCheck.UNIQUE)
class _BindPhase(StrEnum):
    OFFER = "offer"
    ACCEPT = "accept"
    CONFIRM = "confirm"


class _1fc9(TypedDict):
    phase: str | None
    bindings: list[list[str]]


class _22b0(TypedDict):
    enabled: bool


class _22f4(TypedDict):
    fan_mode: str | None
    fan_rate: str | None


class _2309(TypedDict):
    zone_idx: NotRequired[str]
    setpoint: float | None


@verify(EnumCheck.UNIQUE)
class _ZoneMode(StrEnum):
    FOLLOW = "follow_schedule"
    ADVANCED = "advanced_override"
    PERMANENT = "permanent_override"
    COUNTDOWN = "countdown_override"
    TEMPORARY = "temporary_override"


class _2349(TypedDict):
    mode: _ZoneMode
    setpoint: float | None
    duration: NotRequired[int | None]
    until: NotRequired[str | None]


class _2d49(TypedDict):
    state: bool | None


class _2e04(TypedDict):
    system_mode: str
    until: NotRequired[str | None]


class _3110(TypedDict):
    mode: str
    demand: NotRequired[float | None]


class _313f(TypedDict):
    datetime: str | None
    is_dst: bool | None
    _unknown_0: str


class _3220(TypedDict):
    msg_id: int
    msg_type: str
    msg_name: str
    description: str


class _3222(TypedDict):
    start: int | None
    length: int
    data: NotRequired[str]


class _3b00(TypedDict):
    domain_id: NotRequired[Literal["FC"]]
    actuator_sync: bool | None


class _3ef0_3(TypedDict):
    modulation_level: float | None
    _flags_2: str


class _3ef0_6(_3ef0_3):
    _flags_3: list[int]
    ch_active: bool
    dhw_active: bool
    cool_active: bool
    flame_on: bool
    _unknown_4: str
    _unknown_5: str


class _3ef0_9(_3ef0_6):
    _flags_6: list[int]
    ch_enabled: bool
    ch_setpoint: int
    max_rel_modulation: float


class _3ef1(TypedDict):
    modulation_level: float | None
    actuator_countdown: int | None
    cycle_countdown: int | None
    _unknown_0: str


class _JASPER(TypedDict):
    ordinal: str
    blob: str


class PayDictT:
    """Payload dict types."""

    EMPTY: TypeAlias = _empty

    _0004: TypeAlias = _0004
    _0006: TypeAlias = _0006
    _0008: TypeAlias = _0008
    _000A: TypeAlias = _000a
    _0100: TypeAlias = _0100
    _0404: TypeAlias = _0404
    _0418: TypeAlias = _0418
    _0418_NULL: TypeAlias = _0418_NULL
    _1030: TypeAlias = _1030
    _1060: TypeAlias = _1060
    _1081: TypeAlias = _Setpoint
    _1090: TypeAlias = _1090
    _10A0: TypeAlias = _10a0
    _10D0: TypeAlias = _10d0
    _10E1: TypeAlias = _10e1
    _1100: TypeAlias = _1100
    _1100_IDX: TypeAlias = _1100_IDX
    _1260: TypeAlias = _Temperature
    _1280: TypeAlias = OutdoorHumidity
    _1290: TypeAlias = OutdoorTemp
    _1298: TypeAlias = Co2Level
    _12A0: TypeAlias = _12a0
    _12B0: TypeAlias = _12b0
    _12C0: TypeAlias = _12c0
    _12C8: TypeAlias = AirQuality
    _12F0: TypeAlias = _FlowRate
    _1300: TypeAlias = _Pressure
    _1F09: TypeAlias = _1f09
    _1F41: TypeAlias = _1f41
    _1FC9: TypeAlias = _1fc9
    _1FD4: TypeAlias = _1fd4
    _22B0: TypeAlias = _22b0
    _22F4: TypeAlias = _22f4
    _2309: TypeAlias = _2309
    _2349: TypeAlias = _2349
    _22D9: TypeAlias = _Setpoint
    _2D49: TypeAlias = _2d49
    _2E04: TypeAlias = _2e04
    _3110: TypeAlias = _3110
    _313F: TypeAlias = _313f
    _31DA: TypeAlias = _VentilationState
    _3200: TypeAlias = _Temperature
    _3210: TypeAlias = _Temperature
    _3B00: TypeAlias = _3b00
    _3EF0: TypeAlias = _3ef0_3 | _3ef0_6 | _3ef0_9
    _3EF1: TypeAlias = _3ef1

    _JASPER: TypeAlias = _JASPER

    FAULT_LOG_ENTRY: TypeAlias = FaultLogEntry
    FAULT_LOG_ENTRY_NULL: TypeAlias = FaultLogEntryNull
    TEMPERATURE: TypeAlias = _Temperature

    RELATIVE_HUMIDITY: TypeAlias = RelativeHumidity

    AIR_QUALITY: TypeAlias = AirQuality
    CO2_LEVEL: TypeAlias = Co2Level
    EXHAUST_TEMP: TypeAlias = ExhaustTemp
    SUPPLY_TEMP: TypeAlias = SupplyTemp
    INDOOR_HUMIDITY: TypeAlias = IndoorHumidity
    OUTDOOR_HUMIDITY: TypeAlias = OutdoorHumidity
    INDOOR_TEMP: TypeAlias = IndoorTemp
    OUTDOOR_TEMP: TypeAlias = OutdoorTemp
    CAPABILITIES: TypeAlias = Capabilities
    BYPASS_POSITION: TypeAlias = BypassPosition
    FAN_INFO: TypeAlias = FanInfo
    EXHAUST_FAN_SPEED: TypeAlias = ExhaustFanSpeed
    SUPPLY_FAN_SPEED: TypeAlias = SupplyFanSpeed
    REMAINING_MINUTES: TypeAlias = RemainingMins
    POST_HEATER: TypeAlias = PostHeater
    PRE_HEATER: TypeAlias = PreHeater
    SUPPLY_FLOW: TypeAlias = SupplyFlow
    EXHAUST_FLOW: TypeAlias = ExhaustFlow


class PortConfigT(TypedDict):
    baudrate: int  # 57600, 115200
    dsrdtr: bool
    rtscts: bool
    timeout: int
    xonxoff: bool


class PktLogConfigT(TypedDict):
    packet_log_path: str
    packet_log_prefix: str
    packet_log_retention_days: int | None
    rotate_bytes: int | None
    buffer_capacity: NotRequired[int]
    flush_level: NotRequired[int | str]
    flush_interval: NotRequired[float | int]

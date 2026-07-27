#!/usr/bin/env python3
"""RAMSES RF - Opentherm processor."""

# TODO: a fnc to translate OT flags into a list of strs

from __future__ import annotations

import struct
from collections.abc import Callable
from enum import EnumCheck, IntEnum, StrEnum, verify
from typing import Any, Final, TypeAlias

_DataValueT: TypeAlias = float | int | list[int] | str | None
_FrameT: TypeAlias = str
_MsgStrT: TypeAlias = str


_FlagsSchemaT: TypeAlias = dict[int, dict[str, str]]
_OtMsgSchemaT: TypeAlias = dict[str, Any]


class OtDataId(IntEnum):  # the subset of data-ids used by the OTB
    STATUS = 0x00
    CONTROL_SETPOINT = 0x01
    MASTER_CONFIG = 0x02
    SLAVE_CONFIG = 0x03
    OEM_FAULTS = 0x05
    REMOTE_FLAGS = 0x06
    ROOM_OVERRIDE = 0x09
    # TSP_NUMBER = 0x0A
    # FHB_SIZE = 0x0C
    # FHB_ENTRY = 0x0D
    ROOM_SETPOINT = 0x10
    REL_MODULATION_LEVEL = 0x11
    CH_WATER_PRESSURE = 0x12
    DHW_FLOW_RATE = 0x13
    ROOM_TEMP = 0x18
    BOILER_OUTPUT_TEMP = 0x19
    DHW_TEMP = 0x1A
    OUTSIDE_TEMP = 0x1B
    BOILER_RETURN_TEMP = 0x1C
    DHW_BOUNDS = 0x30
    CH_BOUNDS = 0x31
    DHW_SETPOINT = 0x38
    CH_MAX_SETPOINT = 0x39
    BURNER_FAILED_STARTS = 0x71
    FLAME_LOW_SIGNALS = 0x72
    OEM_CODE = 0x73
    BURNER_STARTS = 0x74
    CH_PUMP_STARTS = 0x75
    DHW_PUMP_STARTS = 0x76
    DHW_BURNER_STARTS = 0x77
    BURNER_HOURS = 0x78
    CH_PUMP_HOURS = 0x79
    DHW_PUMP_HOURS = 0x7A
    DHW_BURNER_HOURS = 0x7B
    #
    _00 = 0x00
    _01 = 0x01
    _02 = 0x02
    _03 = 0x03
    _05 = 0x05
    _06 = 0x06
    _09 = 0x09
    _0A = 0x0A
    _0C = 0x0C
    _0D = 0x0D
    _0E = 0x0E
    _0F = 0x0F
    _10 = 0x10
    _11 = 0x11
    _12 = 0x12
    _13 = 0x13
    _18 = 0x18
    _19 = 0x19
    _1A = 0x1A
    _1B = 0x1B
    _1C = 0x1C
    _30 = 0x30
    _31 = 0x31
    _38 = 0x38
    _39 = 0x39
    _71 = 0x71
    _72 = 0x72
    _73 = 0x73
    _74 = 0x74
    _75 = 0x75
    _76 = 0x76
    _77 = 0x77
    _78 = 0x78
    _79 = 0x79
    _7A = 0x7A
    _7B = 0x7B
    _7C = 0x7C
    _7D = 0x7D
    _7E = 0x7E
    _7F = 0x7F


_OtDataIdT: TypeAlias = OtDataId  # | int

# grep -E 'RP.* 34:.* 30:.* 3220 ' | grep -vE ' 005 00..(01   |05|  |11|12|13|19|1A|1C            |73                           )' returns no results
# grep -E 'RP.* 10:.* 01:.* 3220 ' | grep -vE ' 005 00..(   03|05|0F|11|12|13|19|1A|1C|38|39|71|72|73|74|75|76|77|78|79|7A|7B|7F)' returns no results

# These are R8810A/R8820A-supported msg_ids and their descriptions
SCHEMA_DATA_IDS: Final[dict[_OtDataIdT, _MsgStrT]] = {
    OtDataId._03: "Slave configuration",  # .                                             #   3
    # 003:HB0: Slave configuration: DHW present
    # 003:HB1: Slave configuration: Control type
    # 003:HB4: Slave configuration: Master low-off & pump control
    #
    OtDataId._06: "Remote boiler parameter flags",  # .                                    #   6
    # 006:HB0: Remote boiler parameter transfer-enable: DHW setpoint
    # 006:HB1: Remote boiler parameter transfer-enable: max. CH setpoint
    # 006:LB0: Remote boiler parameter read/write: DHW setpoint
    # 006:LB1: Remote boiler parameter read/write: max. CH setpoint,
    #
    OtDataId._7F: "Slave product version number and type",  # .                           # 127
    #
    # TODO: deprecate 71-2, 74-7B, as appears that always value=None
    # # These are STATUS seen RQ'd by 01:/30:, but here to retrieve less frequently
    # 0x71: "Number of un-successful burner starts",  # .                           # 113
    # 0x72: "Number of times flame signal was too low",  # .                        # 114
    # 0x74: "Number of starts burner",  # .                                         # 116
    # 0x75: "Number of starts central heating pump",  # .                           # 117
    # 0x76: "Number of starts DHW pump/valve",  # .                                 # 118
    # 0x77: "Number of starts burner during DHW mode",  # .                         # 119
    # 0x78: "Number of hours burner is in operation (i.e. flame on)",  # .          # 120
    # 0x79: "Number of hours central heating pump has been running",  # .           # 121
    # 0x7A: "Number of hours DHW pump has been running/valve has been opened",  # . # 122
    # 0x7B: "Number of hours DHW burner is in operation during DHW mode",  # .      # 123
}
PARAMS_DATA_IDS: Final[dict[_OtDataIdT, _MsgStrT]] = {
    OtDataId._0E: "Maximum relative modulation level setting (%)",  # .                   #  14
    OtDataId._0F: "Max. boiler capacity (kW) and modulation level setting (%)",  # .      #  15
    OtDataId._30: "DHW Setpoint upper & lower bounds for adjustment (°C)",  # .           #  48
    OtDataId._31: "Max CH water Setpoint upper & lower bounds for adjustment (°C)",  # .  #  49
    OtDataId._38: "DHW Setpoint (°C) (Remote parameter 1)",  # see: 0x06, is R/W          #  56
    OtDataId._39: "Max CH water Setpoint (°C) (Remote parameter 2)",  # see: 0x06, is R/W #  57
}
STATUS_DATA_IDS: Final[dict[_OtDataIdT, _MsgStrT]] = {
    OtDataId._00: "Master/Slave status flags",  # .                                       #   0
    # 000:HB0: Master status: CH enable
    # 000:HB1: Master status: DHW enable
    # 000:HB2: Master status: Cooling enable
    # 000:HB3: Master status: OTC active
    # 000:HB5: Master status: Summer/winter mode
    # 000:HB6: Master status: DHW blocking
    # 000:LB0: Slave Status: Fault indication
    # 000:LB1: Slave Status: CH mode
    # 000:LB2: Slave Status: DHW mode
    # 000:LB3: Slave Status: Flame status
    #
    OtDataId._01: "CH water temperature Setpoint (°C)",  # NOTE: is W only!               #   1
    OtDataId._11: "Relative Modulation Level (%)",  # .                                   #  17
    OtDataId._12: "Water pressure in CH circuit (bar)",  # .                              #  18
    OtDataId._13: "Water flow rate in DHW circuit. (L/min)",  # .                         #  19
    OtDataId._18: "Room temperature (°C)",  # .                                           #  24
    OtDataId._19: "Boiler flow water temperature (°C)",  # .                              #  25
    OtDataId._1A: "DHW temperature (°C)",  # .                                            #  26
    OtDataId._1B: "Outside temperature (°C)",  # TODO: any value here?  # is R/W          #  27
    OtDataId._1C: "Return water temperature (°C)",  # .                                   #  28
    #
    # These are error/state codes...
    OtDataId._05: "Fault flags & OEM codes",  # .                                         #   5
    # 005:HB0: Service request
    # 005:HB1: Lockout-reset
    # 005:HB2: Low water pressure
    # 005:HB3: Gas/flame fault
    # 005:HB4: Air pressure fault
    # 005:HB5: Water over-temperature
    # 005:LB:  OEM fault code
    #
    OtDataId._73: "OEM diagnostic code",  # .                                             # 115
}
WRITE_DATA_IDS: Final[
    dict[_OtDataIdT, _MsgStrT]
] = {  # Write-Data, NB: some are also Read-Data
    OtDataId._01: "CH water temperature Setpoint (°C)",
    # 001: Control Setpoint i.e. CH water temperature Setpoint (°C)
    #
    OtDataId._02: "Master configuration",
    # 002:HB0: Master configuration: Smart power
    # 002:LB:  Master MemberID code
    #
    OtDataId._09: "Remote override room Setpoint",  # c.f. 0x64, 100                      #   9
    OtDataId._0E: "Maximum relative modulation level setting (%)",  # c.f. 0x11           #  14
    OtDataId._10: "Room Setpoint (°C)",  # .                                              #  16
    OtDataId._18: "Room temperature (°C)",  # .                                           #  24
    OtDataId._1B: "Outside temperature (°C)",  # .                                        #  27
    OtDataId._38: "DHW Setpoint (°C) (Remote parameter 1)",  # .       # is R/W           #  56
    OtDataId._39: "Max CH water Setpoint (°C) (Remote parameters 2)",  # is R/W           #  57
    OtDataId._7C: "Opentherm version Master",  # .                     # is R/W           # 124
    OtDataId._7E: "Master product version number and type",  # .                          # 126
}

OTB_DATA_IDS: Final[dict[_OtDataIdT, _MsgStrT]] = (
    SCHEMA_DATA_IDS
    | PARAMS_DATA_IDS
    | STATUS_DATA_IDS
    | WRITE_DATA_IDS
    | {
        OtDataId._0A: "Number of TSPs supported by slave",  # TODO                        #  10
        OtDataId._0C: "Size of FHB supported by slave",  # .  TODO                        #  12
        OtDataId._0D: "FHB Entry",  # .                       TODO                        #  13
        OtDataId._7D: "Opentherm version Slave",  # .         TODO                        # 125
    }
)

# Data structure shamelessy copied, with thanks to @nlrb, from:
# github.com/nlrb/com.tclcode.otgw (node_modules/otg-api/lib/ot_msg.js),

# Other code shamelessy copied, with thanks to @mvn23, from:
# github.com/mvn23/pyotgw (pyotgw/protocol.py),

# Also see:
# github.com/rvdbreemen/OTGW-firmware
READ_WRITE: Final = "RW"
READ_ONLY: Final = "R-"
WRITE_ONLY: Final = "-W"

EN: Final = "en"
FLAGS: Final = "flags"
DIR: Final = "dir"
NL: Final = "nl"
SENSOR: Final = "sensor"
VAL: Final = "val"
VAR: Final = "var"

FLAG8: Final = "flag8"
FLAG: Final = "flag"
U8: Final = "u8"
S8: Final = "s8"
F8_8: Final = "f8.8"
U16: Final = "u16"
S16: Final = "s16"
SPECIAL: Final[str] = U8  # used for ID 0x14 (20)

HB: Final = "hb"
LB: Final = "lb"

SZ_MESSAGES: Final = "messages"
SZ_DESCRIPTION: Final = "description"
SZ_MSG_ID: Final = "msg_id"
SZ_MSG_NAME: Final = "msg_name"
SZ_MSG_TYPE: Final = "msg_type"
SZ_VALUE: Final = "value"
SZ_VALUE_HB: Final[str] = f"{SZ_VALUE}_{HB}"
SZ_VALUE_LB: Final[str] = f"{SZ_VALUE}_{LB}"


@verify(EnumCheck.UNIQUE)
class Sensor(StrEnum):  # all are F8_8, except COUNTER, CO2_LEVEL
    COUNTER = "counter"
    RATIO = "ratio"
    HUMIDITY = "relative humidity (%)"
    PERCENTAGE = "percentage (%)"
    PRESSURE = "pressure (bar)"
    TEMPERATURE = "temperature (°C)"
    CURRENT = "current (µA)"
    FLOW_RATE = "flow rate (L/min)"
    CO2_LEVEL = "CO2 (ppm)"


@verify(EnumCheck.UNIQUE)
class OtMsgType(StrEnum):
    READ_DATA = "Read-Data"
    WRITE_DATA = "Write-Data"
    INVALID_DATA = "Invalid-Data"
    RESERVED = "-reserved-"
    READ_ACK = "Read-Ack"
    WRITE_ACK = "Write-Ack"
    DATA_INVALID = "Data-Invalid"
    UNKNOWN_DATAID = "Unknown-DataId"


OPENTHERM_MSG_TYPE: dict[int, OtMsgType] = {
    0b000: OtMsgType.READ_DATA,
    0b001: OtMsgType.WRITE_DATA,
    0b010: OtMsgType.INVALID_DATA,
    0b011: OtMsgType.RESERVED,  # as per Unknown-DataId?
    0b100: OtMsgType.READ_ACK,
    0b101: OtMsgType.WRITE_ACK,
    0b110: OtMsgType.DATA_INVALID,  # e.g. sensor fault
    0b111: OtMsgType.UNKNOWN_DATAID,
}

SZ_STATUS_FLAGS: Final = "status_flags"
SZ_MASTER_CONFIG_FLAGS: Final = "master_config_flags"
SZ_SLAVE_CONFIG_FLAGS: Final = "slave_config_flags"
SZ_FAULT_FLAGS: Final = "fault_flags"
SZ_REMOTE_FLAGS: Final = "remote_flags"


# OpenTherm status flags [ID 0: Master status (HB) & Slave status (LB)]
_STATUS_FLAGS: Final[_FlagsSchemaT] = {
    0x0100: {
        EN: "Central heating enable",
        NL: "Centrale verwarming aan",
        VAR: "StatusCHEnabled",
    },  # CH enabled
    0x0200: {
        EN: "DHW enable",
        NL: "Tapwater aan",
        VAR: "StatusDHWEnabled",
    },  # DHW enabled
    0x0400: {
        EN: "Cooling enable",
        NL: "Koeling aan",
        VAR: "StatusCoolEnabled",
    },  # cooling enabled
    0x0800: {
        EN: "Outside temp. comp. active",
        NL: "Compenseren buitentemp.",
        VAR: "StatusOTCActive",
    },  # OTC active
    0x1000: {
        EN: "Central heating 2 enable",
        NL: "Centrale verwarming 2 aan",
        VAR: "StatusCH2Enabled",
    },  # CH2 enabled
    0x2000: {
        EN: "Summer/winter mode",
        NL: "Zomer/winter mode",
        VAR: "StatusSummerWinter",
    },  # summer mode active
    0x4000: {
        EN: "DHW blocking",
        NL: "Tapwater blokkade",
        VAR: "StatusDHWBlocked",
    },  # DHW is blocking
    0x0001: {
        EN: "Fault indication",
        NL: "Fout indicatie",
        VAR: "StatusFault",
    },  # fault state
    0x0002: {
        EN: "Central heating mode",
        NL: "Centrale verwarming mode",
        VAR: "StatusCHMode",
    },  # CH active
    0x0004: {
        EN: "DHW mode",
        NL: "Tapwater mode",
        VAR: "StatusDHWMode",
    },  # DHW active
    0x0008: {
        EN: "Flame status",
        NL: "Vlam status",
        VAR: "StatusFlame",
    },  # flame on
    0x0010: {
        EN: "Cooling status",
        NL: "Status koelen",
        VAR: "StatusCooling",
    },  # cooling active
    0x0020: {
        EN: "Central heating 2 mode",
        NL: "Centrale verwarming 2 mode",
        VAR: "StatusCH2Mode",
    },  # CH2 active
    0x0040: {
        EN: "Diagnostic indication",
        NL: "Diagnose indicatie",
        VAR: "StatusDiagnostic",
    },  # diagnostics mode
}
# OpenTherm Master configuration flags [ID 2: master config flags (HB)]
_MASTER_CONFIG_FLAGS: Final[_FlagsSchemaT] = {
    0x0100: {
        EN: "Smart Power",
        VAR: "ConfigSmartPower",
    },
}
# OpenTherm Slave configuration flags [ID 3: slave config flags (HB)]
_SLAVE_CONFIG_FLAGS: Final[_FlagsSchemaT] = {
    0x0100: {
        EN: "DHW present",
        VAR: "ConfigDHWpresent",
    },
    0x0200: {
        EN: "Control type (modulating on/off)",
        VAR: "ConfigControlType",
    },
    0x0400: {
        EN: "Cooling supported",
        VAR: "ConfigCooling",
    },
    0x0800: {
        EN: "DHW storage tank",
        VAR: "ConfigDHW",
    },
    0x1000: {
        EN: "Master low-off & pump control allowed",
        VAR: "ConfigMasterPump",
    },
    0x2000: {
        EN: "Central heating 2 present",
        VAR: "ConfigCH2",
    },
}
# OpenTherm fault flags [ID 5: Application-specific fault flags (HB)]
_FAULT_FLAGS: Final[_FlagsSchemaT] = {
    0x0100: {
        EN: "Service request",
        NL: "Onderhoudsvraag",
        VAR: "FaultServiceRequest",
    },
    0x0200: {
        EN: "Lockout-reset",
        NL: "Geen reset op afstand",
        VAR: "FaultLockoutReset",
    },
    0x0400: {
        EN: "Low water pressure",
        NL: "Waterdruk te laag",  # codespell:ignore te
        VAR: "FaultLowWaterPressure",
    },
    0x0800: {
        EN: "Gas/flame fault",
        NL: "Gas/vlam fout",
        VAR: "FaultGasFlame",
    },
    0x1000: {
        EN: "Air pressure fault",
        NL: "Luchtdruk fout",
        VAR: "FaultAirPressure",
    },
    0x2000: {
        EN: "Water over-temperature",
        NL: "Water te heet",  # codespell:ignore te
        VAR: "FaultOverTemperature",
    },
}
# OpenTherm remote flags [ID 6: Remote parameter flags (HB)]
_REMOTE_FLAGS: Final[_FlagsSchemaT] = {
    0x0100: {
        EN: "DHW setpoint enable",
        VAR: "RemoteDHWEnabled",
    },
    0x0200: {
        EN: "Max. CH setpoint enable",
        VAR: "RemoteMaxCHEnabled",
    },
    0x0001: {
        EN: "DHW setpoint read/write",
        VAR: "RemoteDHWReadWrite",
    },
    0x0002: {
        EN: "Max. CH setpoint read/write",
        VAR: "RemoteMaxCHReadWrite",
    },
}
# OpenTherm messages  # NOTE: this is used in entity_base.py (traits)
OPENTHERM_MESSAGES: Final[dict[_OtDataIdT, _OtMsgSchemaT]] = {
    OtDataId._00: {  # 0, Status
        EN: "Status",
        DIR: READ_ONLY,
        VAL: {HB: FLAG8, LB: FLAG8},
        FLAGS: SZ_STATUS_FLAGS,
    },
    OtDataId._01: {  # 1, Control Setpoint
        EN: "Control setpoint",
        NL: "Ketel doeltemperatuur",
        DIR: WRITE_ONLY,
        VAL: F8_8,
        VAR: "ControlSetpoint",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._02: {  # 2, Master configuration (Member ID)
        EN: "Master configuration",
        DIR: WRITE_ONLY,
        VAL: {HB: FLAG8, LB: U8},
        FLAGS: SZ_MASTER_CONFIG_FLAGS,
        VAR: {LB: "MasterMemberId"},
    },
    OtDataId._03: {  # 3, Slave configuration (Member ID)
        EN: "Slave configuration",
        DIR: READ_ONLY,
        VAL: {HB: FLAG8, LB: U8},
        FLAGS: SZ_SLAVE_CONFIG_FLAGS,
        VAR: {LB: "SlaveMemberId"},
    },
    OtDataId._05: {  # 5, OEM Fault code
        EN: "Fault flags & OEM fault code",
        DIR: READ_ONLY,
        VAL: {HB: FLAG8, LB: U8},
        VAR: {LB: "OEMFaultCode"},
        FLAGS: SZ_FAULT_FLAGS,
    },
    OtDataId._06: {  # 6, Remote Flags
        EN: "Remote parameter flags",
        DIR: READ_ONLY,
        VAL: FLAG8,
        FLAGS: SZ_REMOTE_FLAGS,
    },
    OtDataId._09: {  # 9, Remote Override Room Setpoint
        EN: "Remote override room setpoint",
        NL: "Overschreven kamer doeltemperatuur",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "RemoteOverrideRoomSetpoint",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._0A: {  # 10, TSP Number
        EN: "Number of transparent slave parameters supported by slave",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "TSPNumber"},
    },
    OtDataId._0C: {  # 12, FHB Size
        EN: "Size of fault history buffer supported by slave",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "FHBSize"},
    },
    OtDataId._0D: {  # 13, FHB Entry
        EN: "Index number/value of referred-to fault history buffer entry",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "FHBIndex", LB: "FHBValue"},
    },
    OtDataId._0E: {  # 14, Max Relative Modulation Level
        EN: "Max. relative modulation level",
        NL: "Max. relatief modulatie-niveau",
        DIR: WRITE_ONLY,
        VAL: F8_8,
        VAR: "MaxRelativeModulationLevel",
        SENSOR: Sensor.PERCENTAGE,
    },
    OtDataId._0F: {  # 15, Max Boiler Capacity & Min Modulation Level
        EN: "Max. boiler capacity (kW) and modulation level setting (%)",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "MaxBoilerCapacity", LB: "MinModulationLevel"},
    },
    OtDataId._10: {  # 16, Current Setpoint
        EN: "Room setpoint",
        NL: "Kamer doeltemperatuur",
        DIR: WRITE_ONLY,
        VAL: F8_8,
        VAR: "CurrentSetpoint",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._11: {  # 17, Relative Modulation Level
        EN: "Relative modulation level",
        NL: "Relatief modulatie-niveau",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "RelativeModulationLevel",
        SENSOR: Sensor.PERCENTAGE,
    },
    OtDataId._12: {  # 18, CH Water Pressure
        EN: "Central heating water pressure (bar)",
        NL: "Keteldruk",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "CHWaterPressure",
        SENSOR: Sensor.PRESSURE,
    },
    OtDataId._13: {  # 19, DHW Flow Rate
        EN: "DHW flow rate (litres/minute)",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "DHWFlowRate",
        SENSOR: Sensor.FLOW_RATE,
    },
    OtDataId._18: {  # 24, Current Room Temperature
        EN: "Room temperature",
        NL: "Kamertemperatuur",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "CurrentTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._19: {  # 25, Boiler Water Temperature
        EN: "Boiler water temperature",
        NL: "Ketelwatertemperatuur",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "BoilerWaterTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._1A: {  # 26, DHW Temperature
        EN: "DHW temperature",
        NL: "Tapwatertemperatuur",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "DHWTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._1B: {  # 27, Outside Temperature
        EN: "Outside temperature",
        NL: "Buitentemperatuur",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "OutsideTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._1C: {  # 28, Return Water Temperature
        EN: "Return water temperature",
        NL: "Retourtemperatuur",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "ReturnWaterTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._30: {  # 48, DHW Boundaries
        EN: "DHW setpoint boundaries",
        DIR: READ_ONLY,
        VAL: S8,
        VAR: {HB: "DHWUpperBound", LB: "DHWLowerBound"},
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._31: {  # 49, CH Boundaries
        EN: "Max. central heating setpoint boundaries",
        DIR: READ_ONLY,
        VAL: S8,
        VAR: {HB: "CHUpperBound", LB: "CHLowerBound"},
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._38: {  # 56, DHW Setpoint
        EN: "DHW setpoint",
        NL: "Tapwater doeltemperatuur",
        DIR: READ_WRITE,
        VAL: F8_8,
        VAR: "DHWSetpoint",
        SENSOR: Sensor.TEMPERATURE,
    },
    OtDataId._39: {  # 57, Max CH Water Setpoint
        EN: "Max. central heating water setpoint",
        NL: "Max. ketel doeltemperatuur",
        DIR: READ_WRITE,
        VAL: F8_8,
        VAR: "MaxCHWaterSetpoint",
        SENSOR: Sensor.TEMPERATURE,
    },
    # OpenTherm 2.2 IDs
    OtDataId._73: {  # 115, OEM Diagnostic code
        EN: "OEM diagnostic code",
        DIR: READ_ONLY,
        VAL: U16,
        VAR: "OEMDiagnosticCode",
    },
    OtDataId._74: {  # 116, Starts Burner
        EN: "Number of starts burner",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "StartsBurner",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._75: {  # 117, Starts CH Pump
        EN: "Number of starts central heating pump",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "StartsCHPump",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._76: {  # 118, Starts DHW Pump
        EN: "Number of starts DHW pump/valve",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "StartsDHWPump",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._77: {  # 119, Starts Burner DHW
        EN: "Number of starts burner during DHW mode",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "StartsBurnerDHW",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._78: {  # 120, Hours Burner
        EN: "Number of hours burner is in operation (i.e. flame on)",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "HoursBurner",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._79: {  # 121, Hours CH Pump
        EN: "Number of hours central heating pump has been running",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "HoursCHPump",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._7A: {  # 122, Hours DHW Pump
        EN: "Number of hours DHW pump has been running/valve has been opened",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "HoursDHWPump",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._7B: {  # 123, Hours DHW Burner
        EN: "Number of hours DHW burner is in operation during DHW mode",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "HoursDHWBurner",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._7C: {  # 124, Master OpenTherm Version
        EN: "Opentherm version Master",
        DIR: WRITE_ONLY,
        VAL: F8_8,
        VAR: "MasterOpenThermVersion",
    },
    OtDataId._7D: {  # 125, Slave OpenTherm Version
        EN: "Opentherm version Slave",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "SlaveOpenThermVersion",
    },
    OtDataId._7E: {  # 126, Master Product Type/Version
        EN: "Master product version and type",
        DIR: WRITE_ONLY,
        VAL: U8,
        VAR: {HB: "MasterProductType", LB: "MasterProductVersion"},
    },
    OtDataId._7F: {  # 127, Slave Product Type/Version
        EN: "Slave product version and type",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "SlaveProductType", LB: "SlaveProductVersion"},
    },
    # ZX-DAVB extras
    OtDataId._71: {  # 113, Bad Starts Burner
        EN: "Number of un-successful burner starts",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "BadStartsBurner?",
        SENSOR: Sensor.COUNTER,
    },
    OtDataId._72: {  # 114, Low Signals Flame
        EN: "Number of times flame signal was too low",
        DIR: READ_WRITE,
        VAL: U16,
        VAR: "LowSignalsFlame?",
        SENSOR: Sensor.COUNTER,
    },
}

_OPENTHERM_MESSAGES: Final[dict[int, _OtMsgSchemaT]] = {
    0x04: {  # 4, Remote Command
        EN: "Remote command",
        DIR: WRITE_ONLY,
        VAL: U8,
        VAR: "RemoteCommand",
    },
    0x07: {  # 7, Cooling Control Signal
        EN: "Cooling control signal",
        DIR: WRITE_ONLY,
        VAL: F8_8,
        VAR: "CoolingControlSignal",
        SENSOR: Sensor.PERCENTAGE,
    },
    0x08: {  # 8, CH2 Control Setpoint
        EN: "Control setpoint for 2nd CH circuit",
        DIR: WRITE_ONLY,
        VAL: F8_8,
        VAR: "CH2ControlSetpoint",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x0B: {  # 11, TSP Entry
        EN: "Index number/value of referred-to transparent slave parameter",
        DIR: READ_WRITE,
        VAL: U8,
        VAR: {HB: "TSPIndex", LB: "TSPValue"},
    },
    0x14: {  # 20, Day/Time
        EN: "Day of week & Time of day",
        DIR: READ_WRITE,
        VAL: {HB: SPECIAL, LB: U8},  # 1..7/0..23, 0..59
        VAR: {HB: "DayHour", LB: "Minutes"},  # HB7-5: Day, HB4-0: Hour
    },
    0x15: {  # 21, Date
        EN: "Date",
        DIR: READ_WRITE,
        VAL: U8,  # 1..12, 1..31
        VAR: {HB: "Month", LB: "DayOfMonth"},
    },
    0x16: {  # 22, Year
        EN: "Year",
        DIR: READ_WRITE,
        VAL: U16,  # 1999-2099
        VAR: "Year",
    },
    0x17: {  # 23, CH2 Current Setpoint
        EN: "Room setpoint for 2nd CH circuit",
        DIR: WRITE_ONLY,
        VAL: F8_8,
        VAR: "CH2CurrentSetpoint",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x1D: {  # 29, Solar Storage Temperature
        EN: "Solar storage temperature",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "SolarStorageTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x1E: {  # 30, Solar Collector Temperature
        EN: "Solar collector temperature",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "SolarCollectorTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x1F: {  # 31, CH2 Flow Temperature
        EN: "Flow temperature for 2nd CH circuit",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "CH2FlowTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x20: {  # 32, DHW2 Temperature
        EN: "DHW 2 temperature",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "DHW2Temperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x21: {  # 33, Boiler Exhaust Temperature
        EN: "Boiler exhaust temperature",
        DIR: READ_ONLY,
        VAL: S16,
        VAR: "BoilerExhaustTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x32: {  # 50, OTC Boundaries
        EN: "OTC heat curve ratio upper & lower bounds",
        DIR: READ_ONLY,
        VAL: S8,
        VAR: {HB: "OTCUpperBound", LB: "OTCLowerBound"},
    },
    0x3A: {  # 58, OTC Heat Curve Ratio
        EN: "OTC heat curve ratio",
        DIR: READ_WRITE,
        VAL: F8_8,
        VAR: "OTCHeatCurveRatio",
        SENSOR: Sensor.RATIO,
    },
    # OpenTherm 2.3 IDs (70-91) for ventilation/heat-recovery applications
    0x46: {  # 70, VH Status
        EN: "Status ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: FLAG8,
        VAR: "VHStatus",
    },
    0x47: {  # 71, VH Control Setpoint
        EN: "Control setpoint ventilation/heat-recovery",
        DIR: WRITE_ONLY,
        VAL: U8,
        VAR: {HB: "VHControlSetpoint"},
    },
    0x48: {  # 72, VH Fault code
        EN: "Fault flags/code ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: {HB: FLAG, LB: U8},
        VAR: {LB: "VHFaultCode"},
    },
    0x49: {  # 73, VH Diagnostic code
        EN: "Diagnostic code ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: U16,
        VAR: "VHDiagnosticCode",
    },
    0x4A: {  # 74, VH Member ID
        EN: "Config/memberID ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: {HB: FLAG, LB: U8},
        VAR: {LB: "VHMemberId"},
    },
    0x4B: {  # 75, VH OpenTherm Version
        EN: "OpenTherm version ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "VHOpenThermVersion",
    },
    0x4C: {  # 76, VH Product Type/Version
        EN: "Version & type ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "VHProductType", LB: "VHProductVersion"},
    },
    0x4D: {  # 77, Relative Ventilation
        EN: "Relative ventilation",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "RelativeVentilation"},
    },
    0x4E: {  # 78, Relative Humidity
        EN: "Relative humidity",
        NL: "Luchtvochtigheid",
        DIR: READ_WRITE,
        VAL: U8,
        VAR: {HB: "RelativeHumidity"},
        SENSOR: Sensor.HUMIDITY,
    },
    0x4F: {  # 79, CO2 Level
        EN: "CO2 level",
        NL: "CO2 niveau",
        DIR: READ_WRITE,
        VAL: U16,  # 0-2000 ppm
        VAR: "CO2Level",
        SENSOR: Sensor.CO2_LEVEL,
    },
    0x50: {  # 80, Supply Inlet Temperature
        EN: "Supply inlet temperature",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "SupplyInletTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x51: {  # 81, Supply Outlet Temperature
        EN: "Supply outlet temperature",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "SupplyOutletTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x52: {  # 82, Exhaust Inlet Temperature
        EN: "Exhaust inlet temperature",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "ExhaustInletTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x53: {  # 83, Exhaust Outlet Temperature
        EN: "Exhaust outlet temperature",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "ExhaustOutletTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x54: {  # 84, Exhaust Fan Speed
        EN: "Actual exhaust fan speed",
        DIR: READ_ONLY,
        VAL: U16,
        VAR: "ExhaustFanSpeed",
    },
    0x55: {  # 85, Inlet Fan Speed
        EN: "Actual inlet fan speed",
        DIR: READ_ONLY,
        VAL: U16,
        VAR: "InletFanSpeed",
    },
    0x56: {  # 86, VH Remote Parameter
        EN: "Remote parameter settings ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: FLAG8,
        VAR: "VHRemoteParameter",
    },
    0x57: {  # 87, Nominal Ventilation
        EN: "Nominal ventilation value",
        DIR: READ_WRITE,
        VAL: U8,
        VAR: "NominalVentilation",
    },
    0x58: {  # 88, VH TSP Size
        EN: "TSP number ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "VHTSPSize"},
    },
    0x59: {  # 89, VH TSP Entry
        EN: "TSP entry ventilation/heat-recovery",
        DIR: READ_WRITE,
        VAL: U8,
        VAR: {HB: "VHTSPIndex", LB: "VHTSPValue"},
    },
    0x5A: {  # 90, VH FHB Size
        EN: "Fault buffer size ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "VHFHBSize"},
    },
    0x5B: {  # 91, VH FHB Entry
        EN: "Fault buffer entry ventilation/heat-recovery",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "VHFHBIndex", LB: "VHFHBValue"},
    },
    # OpenTherm 2.2 IDs
    0x64: {  # 100, Remote Override Function
        EN: "Remote override function",
        DIR: READ_ONLY,
        VAL: {HB: FLAG8, LB: U8},
        VAR: {HB: "RemoteOverrideFunction"},
    },
    # https://www.domoticaforum.eu/viewtopic.php?f=70&t=10893
    # 0x23: {  # 35, Boiler Fan Speed (rpm/60?)?
    # },
    0x24: {  # 36, Electrical current through burner flame (µA)
        EN: "Electrical current through burner flame (µA)",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "BurnerCurrent",
        SENSOR: Sensor.CURRENT,
    },
    0x25: {  # 37, CH2 Room Temperature
        EN: "Room temperature for 2nd CH circuit",
        DIR: READ_ONLY,
        VAL: F8_8,
        VAR: "CH2CurrentTemperature",
        SENSOR: Sensor.TEMPERATURE,
    },
    0x26: {  # 38, Relative Humidity, c.f. 0x4E
        EN: "Relative humidity",
        DIR: READ_ONLY,
        VAL: U8,
        VAR: {HB: "RelativeHumidity"},  # TODO: or LB?
        SENSOR: Sensor.HUMIDITY,
    },
}

# These must have either a FLAGS (preferred) or a VAR for their message name
_OT_FLAG_LOOKUP: Final[dict[str, _FlagsSchemaT]] = {
    SZ_STATUS_FLAGS: _STATUS_FLAGS,
    SZ_MASTER_CONFIG_FLAGS: _MASTER_CONFIG_FLAGS,
    SZ_SLAVE_CONFIG_FLAGS: _SLAVE_CONFIG_FLAGS,
    SZ_FAULT_FLAGS: _FAULT_FLAGS,
    SZ_REMOTE_FLAGS: _REMOTE_FLAGS,
    # SZ_MESSAGES: OPENTHERM_MESSAGES,
}

# R8810A 1018 v4: https://www.opentherm.eu/request-details/?post_ids=2944
# as at: 2021/06/28

# see also: http://otgw.tclcode.com/matrix.cgi#boilers
# 0x00, 0x01, 0x03, 0x05, 0x09, 0x0E, 0x10-13, 0x18-1C, 0x38-39, 0x3F, 0x80, 0xFF
# personal testing:
# 0x00,       0x03, 0x05, 0x06, 0x0C-0D, 0x11-12, 0x19-1A, 0x1C, 0x30-31, 0x38, 0x7D


def parity(x: int) -> int:
    """Make this the docstring."""
    shiftamount = 1
    while x >> shiftamount:
        x ^= x >> shiftamount
        shiftamount <<= 1
    return x & 1


def _msg_value(val_seqx: str, val_type: str) -> _DataValueT:
    """Make this the docstring."""

    assert len(val_seqx) in (2, 4), f"Invalid value sequence: {val_seqx}"

    # based upon: https://github.com/mvn23/pyotgw/blob/master/pyotgw/protocol.py

    def flag8(byte: str, *args: str) -> list[int]:
        """Split a byte (as a str) into a list of 8 bits.

        In the original payload (the OT specification), the lsb is bit 0 (the last bit),
        so the order of bits is reversed here, giving flags[0] (the 1st bit in the
        array) as the lsb.
        """
        assert len(args) == 0 or (len(args) == 1 and args[0] == "")
        return [(bytes.fromhex(byte)[0] & (1 << x)) >> x for x in range(8)]

    def u8(byte: str, *args: str) -> int:
        """Convert a byte (as a str) into an unsigned int."""
        assert len(args) == 0 or (len(args) == 1 and args[0] == "")
        result = struct.unpack(">B", bytes.fromhex(byte))[0]
        assert isinstance(result, int)  # mypy hint
        return result

    def s8(byte: str, *args: str) -> int:
        """Convert a byte (as a str) into a signed int."""
        assert len(args) == 0 or (len(args) == 1 and args[0] == "")
        result = struct.unpack(">b", bytes.fromhex(byte))[0]
        assert isinstance(result, int)  # mypy hint
        return result

    def f8_8(high_byte: str, low_byte: str) -> float:
        """Convert 2 bytes (as strs) into an OpenTherm f8_8 value."""
        if high_byte == low_byte == "FF":  # TODO: move up to parser?
            raise ValueError()
        return float(s16(high_byte, low_byte) / 256)

    def u16(high_byte: str, low_byte: str) -> int:
        """Convert 2 bytes (as strs) into an unsigned int."""
        if high_byte == low_byte == "FF":  # TODO: move up to parser?
            raise ValueError()
        buf = struct.pack(">BB", u8(high_byte), u8(low_byte))
        return int(struct.unpack(">H", buf)[0])

    def s16(high_byte: str, low_byte: str) -> int:
        """Convert 2 bytes (as strs) into a signed int."""
        if high_byte == low_byte == "FF":  # TODO: move up to parser?
            raise ValueError()
        buf = struct.pack(">bB", s8(high_byte), u8(low_byte))
        return int(struct.unpack(">h", buf)[0])

    DATA_TYPES: dict[str, Callable[..., _DataValueT]] = {
        FLAG8: flag8,
        U8: u8,
        S8: s8,
        F8_8: f8_8,
        U16: u16,
        S16: s16,
    }

    # assert not [
    #     k
    #     for k, v in OPENTHERM_MESSAGES.items()
    #     if not isinstance(v[VAL], dict)
    #     and not isinstance(v.get(VAR), dict)
    #     and v[VAL] not in DATA_TYPES
    # ], "Corrupt OPENTHERM_MESSAGES schema"

    try:
        fnc = DATA_TYPES[val_type]
    except KeyError:
        return val_seqx

    try:
        result: _DataValueT = fnc(val_seqx[:2], val_seqx[2:])
        return result
    except ValueError:
        return None


# FIXME: this is not finished...
def _decode_flags(data_id: OtDataId, flags: str) -> _FlagsSchemaT:  # TBA: list[str]:
    try:  # FIXME: don't use _OT_FLAG_LOOKUP
        flag_schema: _FlagsSchemaT = _OT_FLAG_LOOKUP[OPENTHERM_MESSAGES[data_id][FLAGS]]

    except KeyError as err:
        raise KeyError(f"Invalid data-id: 0x{data_id}: has no flags") from err

    return flag_schema


# ot_type, ot_id, ot_value, ot_schema = decode_frame(payload[2:10])
def decode_frame(
    frame: _FrameT,
) -> tuple[OtMsgType, OtDataId, dict[str, Any], _OtMsgSchemaT]:
    """Decode a 3220 payload."""

    if not isinstance(frame, str) or len(frame) != 8:
        raise TypeError(f"Invalid frame (type or length): {frame}")

    if int(frame[:2], 16) // 0x80 != parity(int(frame, 16) & 0x7FFFFFFF):
        raise ValueError(f"Invalid parity bit: 0b{int(frame[:2], 16) // 0x80}")

    if int(frame[:2], 16) & 0x0F != 0x00:
        raise ValueError(f"Invalid spare bits: 0b{int(frame[:2], 16) & 0x0F:04b}")

    msg_type = (int(frame[:2], 16) & 0x70) >> 4

    # if msg_type == 0b011:  # NOTE: this msg-type may no longer be reserved (R8820?)
    #     raise ValueError(f"Reserved msg-type (0b{msg_type:03b})")

    data_id: OtDataId = int(frame[2:4], 16)  # type: ignore[assignment]
    try:
        msg_schema = OPENTHERM_MESSAGES[data_id]
    except KeyError as err:
        raise KeyError(f"Unknown data-id: 0x{frame[2:4]} ({data_id})") from err

    # There are five msg_id with FLAGS - the following is not 100% correct...
    data_value = {SZ_MSG_NAME: msg_schema.get(FLAGS, msg_schema.get(VAR))}

    if msg_type in (0b000, 0b010, 0b011, 0b110, 0b111):
        # if frame[4:] != "0000":  # NOTE: this is not a hard rule, even for 0b000
        #     raise ValueError(f"Invalid data-value for msg-type: 0x{frame[4:]}")

        # Ensure expected value keys are populated to prevent KeyErrors upstream
        if not msg_schema:
            data_value[SZ_VALUE] = None
        elif isinstance(msg_schema[VAL], dict):
            val_hb = msg_schema[VAL].get(HB, msg_schema[VAL])
            val_lb = msg_schema[VAL].get(LB, msg_schema[VAL])
            if val_hb == FLAG8 and val_lb == FLAG8:
                data_value[SZ_VALUE] = None
            else:
                data_value[SZ_VALUE_HB] = None
                data_value[SZ_VALUE_LB] = None
        elif isinstance(msg_schema.get(VAR), dict):
            data_value[SZ_VALUE_HB] = None
            data_value[SZ_VALUE_LB] = None
        else:
            data_value[SZ_VALUE] = None

        return OPENTHERM_MSG_TYPE[msg_type], data_id, data_value, msg_schema

    if not msg_schema:  # may be a corrupt payload
        data_value[SZ_VALUE] = _msg_value(frame[4:8], U16)

    elif isinstance(msg_schema[VAL], dict):
        value_hb = _msg_value(frame[4:6], msg_schema[VAL].get(HB, msg_schema[VAL]))
        value_lb = _msg_value(frame[6:8], msg_schema[VAL].get(LB, msg_schema[VAL]))

        if isinstance(value_hb, list) and isinstance(value_lb, list):  # FLAG8
            data_value[SZ_VALUE] = value_hb + value_lb  # only data_id 0x00
        else:
            data_value[SZ_VALUE_HB] = value_hb
            data_value[SZ_VALUE_LB] = value_lb

    elif isinstance(msg_schema.get(VAR), dict):
        data_value[SZ_VALUE_HB] = _msg_value(frame[4:6], msg_schema[VAL])
        data_value[SZ_VALUE_LB] = _msg_value(frame[6:8], msg_schema[VAL])

    elif msg_schema[VAL] in (FLAG8, U8, S8):
        data_value[SZ_VALUE] = _msg_value(frame[4:6], msg_schema[VAL])

    elif msg_schema[VAL] in (S16, U16):
        data_value[SZ_VALUE] = _msg_value(frame[4:8], msg_schema[VAL])

    elif msg_schema[VAL] != F8_8:  # shouldn't reach here
        data_value[SZ_VALUE] = _msg_value(frame[4:8], U16)

    elif msg_schema[VAL] == F8_8:  # TODO: needs finishing
        result: float | None = _msg_value(frame[4:8], msg_schema[VAL])  # type: ignore[assignment]

        if result is None:
            data_value[SZ_VALUE] = result
        elif msg_schema.get(SENSOR) == Sensor.PERCENTAGE:
            # NOTE: OT defines % as 0.0-100.0, but (this) ramses uses 0.0-1.0 elsewhere
            data_value[SZ_VALUE] = int(result * 2) / 200  # seems precision of 1%
        elif msg_schema.get(SENSOR) == Sensor.FLOW_RATE:
            data_value[SZ_VALUE] = int(result * 100) / 100
        elif msg_schema.get(SENSOR) == Sensor.PRESSURE:
            data_value[SZ_VALUE] = int(result * 10) / 10
        else:  # if msg_schema.get(SENSOR) == (Sensor.TEMPERATURE, Sensor.HUMIDITY):
            data_value[SZ_VALUE] = int(result * 100) / 100

    return OPENTHERM_MSG_TYPE[msg_type], data_id, data_value, msg_schema


# https://github.com/rvdbreemen/OTGW-firmware/blob/main/Specification/New%20OT%20data-ids.txt  # noqa: E501

"""
    New OT Data-ID's - Found two new ID's at this device description:
    http://www.opentherm.eu/product/view/18/feeling-d201-ot
        ID 98: For a specific RF sensor the RF strength and battery level is written
        ID 99: Operating Mode HC1, HC2/ Operating Mode DHW

    Found new data-id's at this page:
    https://www.opentherm.eu/request-details/?post_ids=1833
        ID 109: Electricity producer starts
        ID 110: Electricity producer hours
        ID 111: Electricity production
        ID 112: Cumulative Electricity production

    Found new Data-ID's at this page:
    https://www.opentherm.eu/request-details/?post_ids=1833
        ID 36:   {f8.8}   "Electrical current through burner flame" (µA)
        ID 37:   {f8.8}   "Room temperature for 2nd CH circuit"
        ID 38:   {u8 u8}  "Relative Humidity"

    For Data-ID's 37 and 38 I assumed their data types, for Data ID 36 I determined
    it by matching qSense value with the correct data-type.

    I also analysed OT Remeha qSense <-> Remeha Tzerra communication.
        ID 131:   {u8 u8}   "Remeha dF-/dU-codes"
        ID 132:   {u8 u8}   "Remeha Service message"
        ID 133:   {u8 u8}   "Remeha detection connected SCUs"

    "Remeha dF-/dU-codes": Should match the dF-/dU-codes written on boiler nameplate.
    Read-Data Request (0 0) returns the data. Also accepts Write-Data Requests (dF
    dU),this returns the boiler to its factory defaults.

    "Remeha Service message" Read-Data Request (0 0), boiler returns (0 2) in case of no
    boiler service. Write-Data Request (1 255) clears the boiler service message.
        boiler returns (1 1) = next service type is "A"
        boiler returns (1 2) = next service type is "B"
        boiler returns (1 3) = next service type is "C"

    "Remeha detection connected SCUs": Write-Data Request (255 1) enables detection of
    connected SCU prints, correct response is (Write-Ack 255 1).

    Other Remeha info:
        ID   5: corresponds with the Remeha E:xx fault codes
        ID  11: corresponds with the Remeha Pxx parameter codes
        ID  35: reported value is fan speed in rpm/60
        ID 115: corresponds with Remeha Status & Sub-status numbers, {u8 u8} data-type
"""

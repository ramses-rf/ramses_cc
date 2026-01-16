"""Constants for the virtual_rf library."""

from enum import StrEnum
from typing import Final, TypedDict


class HgiFwTypes(StrEnum):
    HGI_80 = "HGI80"
    EVOFW3 = "EVOFW3"


# Defaults
GWY_ID_0: Final = "18:000000"
GWY_ID_1: Final = "18:111111"
DEFAULT_GWY_ID: Final = bytes("18:000730", "ascii")

MAX_NUM_PORTS: Final = 6

# Protocol / Schema Constants
DEVICE_ID: Final = "device_id"
FW_TYPE: Final = "fw_type"
DEVICE_ID_BYTES: Final = "device_id_bytes"


# Type Definitions
class _GatewaysT(TypedDict):
    device_id: str
    fw_type: HgiFwTypes
    device_id_bytes: bytes


_GwyAttrsT = TypedDict(
    "_GwyAttrsT",
    {
        "manufacturer": str,
        "product": str,
        "vid": int,
        "pid": int,
        "description": str,
        "interface": str | None,
        "serial_number": str | None,
        "subsystem": str,
        "_dev_path": str,
        "_dev_by-id": str,
    },
)


# Gateway Attribute Definitions
_GWY_ATTRS: dict[str, _GwyAttrsT] = {
    HgiFwTypes.HGI_80: {
        "manufacturer": "Texas Instruments",
        "product": "TUSB3410 Boot Device",
        "vid": 0x10AC,  # Honeywell, Inc.
        "pid": 0x0102,  # HGI80
        "description": "TUSB3410 Boot Device",
        "interface": None,
        "serial_number": "TUSB3410",
        "subsystem": "usb",
        #
        "_dev_path": "/dev/ttyUSB0",
        "_dev_by-id": "/dev/serial/by-id/usb-Texas_Instruments_TUSB3410_Boot_Device_TUSB3410-if00-port0",
    },
    HgiFwTypes.EVOFW3: {
        "manufacturer": "SparkFun",
        "product": "evofw3 atmega32u4",
        "vid": 0x1B4F,  # SparkFun Electronics
        "pid": 0x9206,  #
        "description": "evofw3 atmega32u4",
        "interface": None,
        "serial_number": None,
        "subsystem": "usb-serial",
        #
        "_dev_path": "/dev/ttyACM0",
        "_dev_by-id": "/dev/serial/by-id/usb-SparkFun_evofw3_atmega32u4-if00",
    },
    f"{HgiFwTypes.EVOFW3}_alt": {
        "manufacturer": "FTDI",
        "product": "FT232R USB UART",
        "vid": 0x0403,  # FTDI
        "pid": 0x6001,  # SSM-D2
        "description": "FT232R USB UART - FT232R USB UART",
        "interface": "FT232R USB UART",
        "serial_number": "A50285BI",
        "subsystem": "usb-serial",
        #
        "_dev_path": "/dev/ttyUSB0",
        "_dev_by-id": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0",
    },
}

_DEFAULT_GWY_CONFIG: Final = {
    "config": {
        "disable_discovery": True,
        "enforce_known_list": False,
    }
}

"""A virtual RF network useful for testing."""

# NOTE: does not rely on ramses_rf library

import asyncio
import logging
import os
import pty
import re
import tty
from collections import deque
from io import FileIO
from typing import NewType, cast

from serial import Serial, serial_for_url  # type: ignore[import-untyped]

from .const import (
    _GWY_ATTRS,
    DEFAULT_GWY_ID,
    DEVICE_ID,
    DEVICE_ID_BYTES,
    FW_TYPE,
    MAX_NUM_PORTS,
    HgiFwTypes,
    _GatewaysT,
    _GwyAttrsT,
)

_FD = NewType("_FD", int)  # file descriptor
_PN = NewType("_PN", str)  # port name


_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)


class VirtualComPortInfo:
    """A container for emulating pyserial's PortInfo (SysFS) objects."""

    def __init__(self, port_name: _PN, dev_type: HgiFwTypes | None) -> None:
        """Supplies a useful subset of PortInfo attrs according to gateway type."""

        self.device: _PN = port_name  # # e.g. /dev/pts/2 (a la /dev/ttyUSB0)
        self.name: str = port_name[5:]  # e.g.      pts/2 (a la      ttyUSB0)

        self._set_attrs(_GWY_ATTRS[dev_type or HgiFwTypes.EVOFW3])

    def _set_attrs(self, gwy_attrs: _GwyAttrsT) -> None:
        """Set the remaining USB attributes according to the gateway type."""

        self.manufacturer: str = gwy_attrs["manufacturer"]
        self.product: str = gwy_attrs["product"]

        self.vid: int = gwy_attrs["vid"]
        self.pid: int = gwy_attrs["pid"]

        self.description: str = gwy_attrs["description"]
        self.interface: str | None = gwy_attrs["interface"]
        self.serial_number: str | None = gwy_attrs["serial_number"]
        self.subsystem: str = gwy_attrs["subsystem"]


class VirtualRfBase:
    """A virtual many-to-many network of serial port (a la RF network).

    Creates a collection of serial ports. When data frames are received from any one
    port, they are sent to all the other ports.

    The data frames are in the RAMSES_II format, terminated by `\\r\\n`.
    """

    def __init__(self, num_ports: int, log_size: int = 100) -> None:
        """Create `num_ports` virtual serial ports."""

        if os.name != "posix":
            raise RuntimeError(f"Unsupported OS: {os.name} (requires termios)")

        if not (1 <= num_ports <= MAX_NUM_PORTS):
            raise ValueError(f"Port limit exceeded: {num_ports}")

        self._port_info_list: dict[_PN, VirtualComPortInfo] = {}

        self._loop = asyncio.get_running_loop()

        self._master_to_port: dict[_FD, _PN] = {}  # #  for polling port
        self._port_to_master: dict[_PN, _FD] = {}  # #  for logging
        self._port_to_object: dict[_PN, FileIO] = {}  # for I/O (read/write)
        self._port_to_slave_: dict[_PN, _FD] = {}  # #  for cleanup only

        for idx in range(num_ports):
            self._create_port(idx)

        self._log: deque[tuple[_PN, str, bytes]] = deque([], log_size)
        self._replies: dict[str, bytes] = {}
        self._running = False

    def _create_port(self, port_idx: int, dev_type: HgiFwTypes | None = None) -> None:
        """Create a port without a HGI80 attached."""
        master_fd, slave_fd = pty.openpty()  # pty, tty

        tty.setraw(master_fd)  # requires termios module, so: works only on *nix
        os.set_blocking(master_fd, False)  # make non-blocking

        port_name = _PN(os.ttyname(slave_fd))

        self._master_to_port[_FD(master_fd)] = port_name
        self._port_to_master[port_name] = _FD(master_fd)
        self._port_to_object[port_name] = open(master_fd, "rb+", buffering=0)  # noqa: SIM115
        self._port_to_slave_[port_name] = _FD(slave_fd)

        self._set_comport_info(port_name, dev_type=dev_type)

    def comports(
        self, include_links: bool = False
    ) -> list[VirtualComPortInfo]:  # unsorted
        """Use this method to monkey patch serial.tools.list_ports.comports()."""
        return list(self._port_info_list.values())

    def _set_comport_info(
        self, port_name: _PN, dev_type: HgiFwTypes | None = None
    ) -> VirtualComPortInfo:
        """Add comport info to the list (won't fail if the entry already exists)."""
        self._port_info_list.pop(port_name, None)
        self._port_info_list[port_name] = VirtualComPortInfo(port_name, dev_type)
        return self._port_info_list[port_name]

    @property
    def ports(self) -> list[_PN]:
        """Return a list of the names of the serial ports."""
        return list(self._port_to_master)  # [p.name for p in self.comports]

    async def stop(self) -> None:
        """Stop polling ports and distributing data."""
        if not self._running:
            return

        for master_fd in self._master_to_port:
            self._loop.remove_reader(master_fd)

        self._running = False
        self._cleanup()

    def _cleanup(self) -> None:
        """Destroy file objects and file descriptors."""

        for fp in self._port_to_object.values():
            fp.close()  # also closes corresponding master fd
        for fd in self._port_to_slave_.values():
            os.close(fd)  # else this slave fd will persist

    def start(self) -> None:
        """Start polling ports and distributing data."""
        if self._running:
            return

        for master_fd in self._master_to_port:
            self._loop.add_reader(master_fd, self._read_ready, master_fd)

        self._running = True

    def _read_ready(self, fd: _FD) -> None:
        """Callback for when data is ready to read from a port."""
        try:
            self._pull_data_from_src_port(self._master_to_port[fd])
        except (OSError, KeyError) as err:
            _LOGGER.error(f"Error reading from port {fd}: {err}")

    def _pull_data_from_src_port(self, src_port: _PN) -> None:
        """Pull the data from the sending port and process any frames."""

        try:
            data = self._port_to_object[src_port].read()  # read the Tx'd data
        except OSError:
            return

        if not data:
            return

        self._log.append(cast(tuple[_PN, str, bytes], (src_port, "SENT", data)))

        # this assumes all .write(data) are 1+ whole frames terminated with \r\n
        for frame in (d + b"\r\n" for d in data.split(b"\r\n") if d):  # ignore b""
            if fr := self._proc_before_tx(src_port, frame):
                self._cast_frame_to_all_ports(src_port, fr)  # is not echo only

    def _cast_frame_to_all_ports(self, src_port: _PN, frame: bytes) -> None:
        """Pull the frame from the source port and cast it to the RF."""

        _LOGGER.info(f"{src_port:<11} cast:  {frame!r}")
        for dst_port in self._port_to_master:
            self._push_frame_to_dst_port(dst_port, frame)

        # see if there is a faked response (RP/I) for a given command (RQ/W)
        if not (reply := self._find_reply_for_cmd(frame)):
            return

        _LOGGER.info(f"{src_port:<11} rply:  {reply!r}")
        for dst_port in self._port_to_master:
            self._push_frame_to_dst_port(dst_port, reply)  # is not echo only

    def add_reply_for_cmd(self, cmd: str, reply: str) -> None:
        """Add a reply packet for a given command frame (for a mocked device)."""
        self._replies[cmd] = reply.encode() + b"\r\n"

    def _find_reply_for_cmd(self, cmd: bytes) -> bytes | None:
        """Return a reply packet for a given command frame (for a mocked device)."""
        for pattern, reply in self._replies.items():
            if re.match(pattern, cmd.decode()):
                return reply
        return None

    def _push_frame_to_dst_port(self, dst_port: _PN, frame: bytes) -> None:
        """Push the frame to a single destination port."""

        if data := self._proc_after_rx(dst_port, frame):
            self._log.append((dst_port, "RCVD", data))
            self._port_to_object[dst_port].write(data)

    def _proc_after_rx(self, rcv_port: _PN, frame: bytes) -> bytes | None:
        """Allow the device to modify the frame after receiving (e.g. adding RSSI)."""
        return frame

    def _proc_before_tx(self, src_port: _PN, frame: bytes) -> bytes | None:
        """Allow the device to modify the frame before sending (e.g. changing addr0)."""
        return frame


class VirtualRf(VirtualRfBase):
    """A virtual network of serial ports, each with an optional HGI80s or compatible.

    Frames are modified/dropped according to the expected behaviours of the gateway that
    is transmitting (addr0) / receiving (RSSI) it.
    """

    def __init__(self, num_ports: int, log_size: int = 100, start: bool = True) -> None:
        """Create a number of virtual serial ports.

        Each port has the option of a HGI80 or evofw3-based gateway device.
        """

        self._gateways: dict[_PN, _GatewaysT] = {}

        super().__init__(num_ports, log_size)

        if start:
            self.start()

    @property
    def gateways(self) -> dict[str, _PN]:
        return {v[DEVICE_ID]: k for k, v in self._gateways.items()}

    def set_gateway(
        self,
        port_name: _PN,
        device_id: str,
        fw_type: HgiFwTypes = HgiFwTypes.EVOFW3,
    ) -> None:
        """Attach a gateway with a given device_id and FW type to a port.

        Raise an exception if the device_id is already attached to another port.
        """

        if port_name not in self.ports:
            raise LookupError(f"Port does not exist: {port_name}")

        # Check if device_id is already attached to a *different* port
        existing_port = self.gateways.get(device_id)
        if existing_port and existing_port != port_name:
            raise LookupError(f"Gateway exists on another port: {device_id}")

        if fw_type not in HgiFwTypes:
            raise LookupError(f"Unknown FW specified for gateway: {fw_type}")

        self._gateways[port_name] = {
            DEVICE_ID: device_id,
            FW_TYPE: fw_type,
            DEVICE_ID_BYTES: bytes(device_id, "ascii"),
        }

        self._set_comport_info(port_name, dev_type=fw_type)

    async def dump_frames_to_rf(
        self, pkts: list[bytes], /, timeout: float | None = None
    ) -> None:  # TODO: WIP
        """Dump frames as if from a sending port (for mocking)."""

        async def no_data_left_to_send() -> None:
            """Wait until all pending data is read."""
            # With add_reader, we don't have a selector to query like this.
            # We just wait a short moment for callbacks to process.
            await asyncio.sleep(0.001)

        for data in pkts:
            self._log.append(cast(tuple[_PN, str, bytes], ("/dev/mock", "SENT", data)))
            self._cast_frame_to_all_ports(_PN("/dev/mock"), data)  # is not echo only

        if timeout:
            await asyncio.wait_for(no_data_left_to_send(), timeout)

    def _proc_after_rx(self, rcv_port: _PN, frame: bytes) -> bytes | None:
        """Return the frame as it would have been modified by a gateway after Rx.

        Return None if the bytes are not to be Rx by this device.

        Both FW types will prepend an RSSI to the frame.
        """

        if frame[:1] != b"!":
            return b"000 " + frame

        # The type of Gateway will inform next steps (NOTE: is not a ramses_rf.Gateway)
        gwy: _GatewaysT | None = self._gateways.get(rcv_port)

        if gwy is None or gwy.get(FW_TYPE) != HgiFwTypes.EVOFW3:
            return None

        if frame.rstrip() == b"!V":
            return b"# evofw3 0.7.1\r\n"  # self._fxle_objs[port_name].write(data)
        return None  # TODO: return the ! response

    def _proc_before_tx(self, src_port: _PN, frame: bytes) -> bytes | None:
        """Return the frame as it would have been modified by a gateway before Tx.

        Return None if the bytes are not to be Tx to the RF ether (e.g. to echo only).

        Both FW types will convert addr0 (only) from 18:000730 to its actual device_id.
        HGI80-based gateways will silently drop frames with addr0 other than 18:000730.
        """

        # The type of Gateway will inform next steps (NOTE: is not a ramses_rf.Gateway)
        gwy: _GatewaysT | None = self._gateways.get(src_port)

        # Handle trace flags (evofw3 only)
        if frame[:1] == b"!":  # never to be cast, but may be echo'd, or other response
            if gwy is None or gwy.get(FW_TYPE) != HgiFwTypes.EVOFW3:
                return None  # do not Tx the frame
            self._push_frame_to_dst_port(src_port, frame)

        if gwy is None:  # TODO: ?should raise: but is probably from test suite
            return frame

        # Real HGI80s will silently drop cmds if addr0 is not the 18:000730 sentinel
        if gwy[FW_TYPE] == HgiFwTypes.HGI_80 and frame[7:16] != DEFAULT_GWY_ID:
            return None

        # Both (HGI80 & evofw3) will swap out addr0 (and only addr0)
        if frame[7:16] == DEFAULT_GWY_ID:
            frame = frame[:7] + gwy[DEVICE_ID_BYTES] + frame[16:]

        return frame


async def main() -> None:
    """ "Demonstrate the class functionality."""

    num_ports = 3

    rf = VirtualRf(num_ports)
    print(f"Ports are: {rf.ports}")

    sers: list[Serial] = [serial_for_url(rf.ports[i]) for i in range(num_ports)]

    for i in range(num_ports):
        sers[i].write(bytes(f"Hello World {i}! ", "utf-8"))
        await asyncio.sleep(0.005)  # give the write a chance to effect

        print(f"{sers[i].name}: {sers[i].read(sers[i].in_waiting)}")
        sers[i].close()

    await rf.stop()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())

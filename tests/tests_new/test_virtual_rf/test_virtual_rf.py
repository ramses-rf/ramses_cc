"""Tests for the virtual_rf.virtual_rf module."""

import asyncio
from unittest.mock import patch

import pytest
from serial import serial_for_url  # type: ignore[import-untyped]

from tests.virtual_rf import HgiFwTypes, VirtualRf
from tests.virtual_rf.virtual_rf import VirtualRfBase, main


async def test_virtual_rf_lifecycle() -> None:
    """Test manual start and stop of VirtualRf."""
    # VirtualRf(num_ports, log_size, start)
    rf = VirtualRf(2, start=False)

    assert len(rf.ports) == 2

    # Test start - now returns None (uses add_reader)
    rf.start()

    # Test stop
    await rf.stop()


async def test_virtual_rf_double_start() -> None:
    """Test calling start() multiple times is safe."""
    rf = VirtualRf(1, start=False)
    rf.start()

    # Should simply return without error (idempotent)
    rf.start()

    await rf.stop()


async def test_virtual_rf_init_errors() -> None:
    """Test initialization errors (invalid port count)."""
    # Test 0 ports
    with pytest.raises(ValueError, match="Port limit exceeded"):
        VirtualRf(0)

    # Test > MAX ports
    with pytest.raises(ValueError, match="Port limit exceeded"):
        VirtualRf(99)


async def test_virtual_rf_os_error() -> None:
    """Test initialization error on non-posix OS."""
    with (
        patch("os.name", "nt"),
        pytest.raises(RuntimeError, match="Unsupported OS"),
    ):
        VirtualRf(1)


async def test_virtual_rf_data_flow() -> None:
    """Test that data written to one port is received by others."""
    rf = VirtualRf(2, start=True)

    # Use pyserial to emulate a real client connecting to the virtual port
    # Open connection to Port 0
    ser_0 = serial_for_url(rf.ports[0], timeout=0)
    # Open connection to Port 1
    ser_1 = serial_for_url(rf.ports[1], timeout=0)

    try:
        # Write data to Port 0
        msg = b"RQ --- 18:000730 01:123456 --:------ 0006 001 00\r\n"
        ser_0.write(msg)

        # Give asyncio loop a moment to trigger the read callback and cast data
        await asyncio.sleep(0.01)

        # Read from Port 1
        received = ser_1.read(ser_1.in_waiting)
        # VirtualRf prepends RSSI "000 " to packets
        assert received == b"000 " + msg

    finally:
        ser_0.close()
        ser_1.close()
        await rf.stop()


async def test_virtual_rf_set_gateway_success() -> None:
    """Test attaching a gateway successfully."""
    rf = VirtualRf(1, start=False)
    port_name = rf.ports[0]

    rf.set_gateway(port_name, "18:000730", HgiFwTypes.EVOFW3)

    assert rf.gateways["18:000730"] == port_name
    await rf.stop()


async def test_virtual_rf_set_gateway_invalid_port() -> None:
    """Test attaching a gateway to a non-existent port."""
    rf = VirtualRf(1, start=False)

    with pytest.raises(LookupError, match="Port does not exist"):
        rf.set_gateway("/dev/non_existent", "18:000730")

    await rf.stop()


async def test_virtual_rf_set_gateway_duplicate_device() -> None:
    """Test attaching the same device ID to multiple ports."""
    rf = VirtualRf(2, start=False)
    port_0 = rf.ports[0]
    port_1 = rf.ports[1]

    rf.set_gateway(port_0, "18:000730")

    with pytest.raises(LookupError, match="Gateway exists on another port"):
        rf.set_gateway(port_1, "18:000730")

    await rf.stop()


async def test_virtual_rf_set_gateway_invalid_fw() -> None:
    """Test attaching a gateway with an invalid firmware type."""
    rf = VirtualRf(1, start=False)
    port_0 = rf.ports[0]

    # Cast the string to HgiFwTypes to trick the type checker for the test
    with pytest.raises(LookupError, match="Unknown FW specified"):
        rf.set_gateway(port_0, "18:000730", fw_type="INVALID_FW")

    await rf.stop()


async def test_virtual_rf_stop_unstarted() -> None:
    """Test stopping an instance that wasn't started."""
    rf = VirtualRf(1, start=False)
    # Should not raise error
    await rf.stop()


async def test_virtual_rf_dump_frames() -> None:
    """Test dump_frames_to_rf execution."""
    rf = VirtualRf(1, start=True)

    # Just ensure it runs without error
    await rf.dump_frames_to_rf([b"RQ --- 18:000730 01:123456 --:------ 0006 001 00"])

    await rf.stop()


async def test_virtual_rf_dump_frames_timeout() -> None:
    """Test dump_frames_to_rf with a timeout to cover that branch."""
    rf = VirtualRf(1, start=True)
    # Run with a tiny timeout to ensure the wait_for path is executed
    await rf.dump_frames_to_rf(
        [b"RQ --- 18:000730 01:123456 --:------ 0006 001 00"], timeout=0.01
    )
    await rf.stop()


async def test_virtual_rf_hgi80_logic() -> None:
    """Test HGI80 specific logic (dropping invalid addr0, swapping addr0)."""
    rf = VirtualRf(2, start=True)
    hgi_id = "18:123456"

    # Setup Port 0 as HGI80
    rf.set_gateway(rf.ports[0], hgi_id, HgiFwTypes.HGI_80)

    ser_0 = serial_for_url(rf.ports[0], timeout=0)
    ser_1 = serial_for_url(rf.ports[1], timeout=0)

    try:
        # 1. Send packet with WRONG addr0 (not 18:000730) -> Should be dropped
        # Using a raw string that represents a packet where addr0 (src) is NOT the sentinel
        msg_drop = b"RQ --- 18:999999 01:123456 --:------ 0006 001 00\r\n"
        ser_0.write(msg_drop)
        await asyncio.sleep(0.01)
        assert ser_1.in_waiting == 0  # Nothing received

        # 2. Send packet with CORRECT addr0 (18:000730) -> Should be swapped to hgi_id
        msg_send = b"RQ --- 18:000730 01:123456 --:------ 0006 001 00\r\n"
        ser_0.write(msg_send)
        await asyncio.sleep(0.01)

        received = ser_1.read(ser_1.in_waiting)
        # Expect RSSI header + swapped address
        expected_frame = (
            b"RQ --- " + hgi_id.encode() + b" 01:123456 --:------ 0006 001 00\r\n"
        )
        assert received == b"000 " + expected_frame

    finally:
        ser_0.close()
        ser_1.close()
        await rf.stop()


async def test_virtual_rf_reply_mechanism() -> None:
    """Test the mocked reply mechanism."""
    rf = VirtualRf(2, start=True)

    cmd_pattern = r"RQ.* 0006 001 00"
    reply_payload = "RP --- 01:145038 18:013393 --:------ 0006 004 00050135"

    rf.add_reply_for_cmd(cmd_pattern, reply_payload)

    ser_0 = serial_for_url(rf.ports[0], timeout=0)
    ser_1 = serial_for_url(rf.ports[1], timeout=0)  # Receiver to check cast

    try:
        # Send matching command
        msg = b"RQ --- 18:000730 01:123456 --:------ 0006 001 00\r\n"
        ser_0.write(msg)
        await asyncio.sleep(0.01)

        # Expect to see the original message cast to other ports
        # AND the mocked reply cast to other ports

        data = ser_1.read(ser_1.in_waiting)
        # Should contain the cast frame AND the reply
        assert b"000 RP ---" in data
        assert b"00050135" in data

    finally:
        ser_0.close()
        ser_1.close()
        await rf.stop()


async def test_read_ready_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Test exception handling in _read_ready."""
    rf = VirtualRf(1, start=True)

    # Mock _pull_data_from_src_port to raise OSError
    with patch.object(
        rf, "_pull_data_from_src_port", side_effect=OSError("Test Error")
    ):
        # Trigger the reader callback manually
        fd = list(rf._master_to_port.keys())[0]
        rf._read_ready(fd)

        assert "Error reading from port" in caplog.text

    await rf.stop()


async def test_read_ready_key_error(caplog: pytest.LogCaptureFixture) -> None:
    """Test KeyError handling in _read_ready."""
    rf = VirtualRf(1, start=True)

    # Trigger the reader callback with an invalid FD
    rf._read_ready(99999)

    assert "Error reading from port 99999" in caplog.text

    await rf.stop()


async def test_read_ready_eof() -> None:
    """Test _read_ready handling EOF (empty bytes)."""
    rf = VirtualRf(1, start=True)
    port = rf.ports[0]
    fd = rf._port_to_master[port]

    # Mock the file object's read to return b"" (EOF simulation)
    # Patch the specific FileIO object used by VirtualRf
    with patch.object(rf._port_to_object[port], "read", return_value=b""):
        # Trigger read
        rf._read_ready(fd)
        # Nothing should happen, no errors raised

    await rf.stop()


async def test_read_ready_internal_os_error() -> None:
    """Test OSError handling inside _pull_data_from_src_port."""
    rf = VirtualRf(1, start=True)
    port = rf.ports[0]
    fd = rf._port_to_master[port]

    # Mock the file object's read to raise OSError
    with patch.object(
        rf._port_to_object[port], "read", side_effect=OSError("Read Error")
    ):
        # Trigger read via _read_ready (which calls _pull_data...)
        rf._read_ready(fd)
        # Should catch OSError and return, logging nothing

    await rf.stop()


async def test_edge_cases_rx_tx() -> None:
    """Test edge cases for _proc_after_rx and _proc_before_tx."""
    rf = VirtualRf(2, start=True)

    # 1. Test !V command on HGI80 (should return None/Be dropped by proc_after_rx)
    # 2. Test !V command on generic port (No Gateway)
    rf.set_gateway(rf.ports[0], "18:000000", HgiFwTypes.HGI_80)
    # Port 1 has no gateway

    ser_0 = serial_for_url(rf.ports[0], timeout=0)
    ser_1 = serial_for_url(rf.ports[1], timeout=0)

    try:
        # Send !V to HGI80 (should be ignored)
        ser_0.write(b"!V\r\n")
        await asyncio.sleep(0.01)
        assert ser_0.in_waiting == 0  # No response

        # Send !V to generic port (should be ignored)
        ser_1.write(b"!V\r\n")
        await asyncio.sleep(0.01)
        assert ser_1.in_waiting == 0  # No response

    finally:
        ser_0.close()
        ser_1.close()
        await rf.stop()


async def test_tx_from_non_gateway() -> None:
    """Test transmitting from a port with no attached gateway."""
    rf = VirtualRf(2, start=True)
    # Port 0 has no gateway set

    ser_0 = serial_for_url(rf.ports[0], timeout=0)
    ser_1 = serial_for_url(rf.ports[1], timeout=0)

    try:
        # Send normal frame
        msg = b"RQ --- 18:000730 01:123456 --:------ 0006 001 00\r\n"
        ser_0.write(msg)
        await asyncio.sleep(0.01)

        received = ser_1.read(ser_1.in_waiting)
        # Should be echoed (with RSSI prepended by RX logic of dest port)
        assert received == b"000 " + msg

    finally:
        ser_0.close()
        ser_1.close()
        await rf.stop()


async def test_virtual_rf_base_behavior() -> None:
    """Test VirtualRfBase directly to cover base class methods."""
    rf = VirtualRfBase(2)
    rf.start()

    # Verify comports method
    assert len(rf.comports()) == 2
    assert rf.comports()[0].device == rf.ports[0]

    ser_0 = serial_for_url(rf.ports[0], timeout=0)
    ser_1 = serial_for_url(rf.ports[1], timeout=0)

    try:
        # VirtualRfBase should just echo without modification (no RSSI)
        msg = b"TEST MSG\r\n"
        ser_0.write(msg)
        await asyncio.sleep(0.01)

        received = ser_1.read(ser_1.in_waiting)
        assert received == msg  # Exact match, no "000 " prefix

    finally:
        ser_0.close()
        ser_1.close()
        await rf.stop()


async def test_evofw3_special_commands() -> None:
    """Test EVOFW3 specific commands (!V and invalid ones)."""
    rf = VirtualRf(2, start=True)
    rf.set_gateway(rf.ports[0], "18:000730", HgiFwTypes.EVOFW3)

    ser_0 = serial_for_url(rf.ports[0], timeout=0)

    try:
        # 1. Test !V command -> Should return version string
        ser_0.write(b"!V\r\n")
        await asyncio.sleep(0.01)
        resp = ser_0.read(ser_0.in_waiting)
        assert b"# evofw3 0.7.1" in resp

        # 2. Test !Unknown command -> Should return nothing (None)
        ser_0.write(b"!K\r\n")
        await asyncio.sleep(0.01)
        resp = ser_0.read(ser_0.in_waiting)
        assert resp == b""  # Nothing returned

    finally:
        ser_0.close()
        await rf.stop()


async def test_main_function() -> None:
    """Test the main() demo function."""
    # This runs the main function to ensure 100% coverage of that block
    await main()

#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial


READ_FLOAT_START = 1024
READ_INT_START = 2560
READ_WORD_COUNT = 12
MULTIFUNC_ADDR = 1574
ACTIVE_UPLOAD_ADDR = 410


@dataclass
class WrenchSample:
    timestamp_wall_s: float
    fx: float
    fy: float
    fz: float
    mx: float
    my: float
    mz: float


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            lsb = crc & 0x0001
            crc >>= 1
            if lsb:
                crc ^= 0xA001
    return crc


def append_crc(frame: bytes) -> bytes:
    crc = crc16_modbus(frame)
    return frame + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def verify_crc(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    payload = frame[:-2]
    expected = crc16_modbus(payload)
    actual = frame[-2] | (frame[-1] << 8)
    return expected == actual


def build_read_holding_registers_frame(slave_id: int, start_addr: int, register_count: int) -> bytes:
    payload = bytes(
        (
            slave_id,
            0x03,
            (start_addr >> 8) & 0xFF,
            start_addr & 0xFF,
            (register_count >> 8) & 0xFF,
            register_count & 0xFF,
        )
    )
    return append_crc(payload)


def build_write_multiple_registers_frame(slave_id: int, start_addr: int, values: list[int]) -> bytes:
    register_count = len(values)
    byte_count = register_count * 2
    payload = bytearray(
        (
            slave_id,
            0x10,
            (start_addr >> 8) & 0xFF,
            start_addr & 0xFF,
            (register_count >> 8) & 0xFF,
            register_count & 0xFF,
            byte_count,
        )
    )
    for value in values:
        payload.extend(((value >> 8) & 0xFF, value & 0xFF))
    return append_crc(bytes(payload))


def read_exact_response(ser: serial.Serial, expected_min_bytes: int) -> bytes:
    deadline = time.monotonic() + max(ser.timeout or 0.0, 0.2) + 0.5
    buf = bytearray()
    while len(buf) < expected_min_bytes and time.monotonic() < deadline:
        chunk = ser.read(expected_min_bytes - len(buf))
        if chunk:
            buf.extend(chunk)
        else:
            time.sleep(0.005)
    return bytes(buf)


class DedhModbusClient:
    def __init__(self, port: str, baudrate: int, slave_id: int, timeout: float) -> None:
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.timeout = timeout
        self.ser: serial.Serial | None = None

    def __enter__(self) -> "DedhModbusClient":
        self.ser = serial.Serial(self.port, self.baudrate, bytesize=8, parity="N", stopbits=1, timeout=self.timeout)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ser is not None and self.ser.is_open:
            self.ser.close()

    @property
    def serial(self) -> serial.Serial:
        if self.ser is None:
            raise RuntimeError("Serial port is not open")
        return self.ser

    def transact(self, request: bytes, expected_min_bytes: int) -> bytes:
        ser = self.serial
        ser.reset_input_buffer()
        ser.write(request)
        ser.flush()
        response = read_exact_response(ser, expected_min_bytes)
        if len(response) < expected_min_bytes:
            raise RuntimeError(f"Incomplete response: got {len(response)} bytes, expected at least {expected_min_bytes}")
        if not verify_crc(response):
            raise RuntimeError(f"CRC mismatch in response: {response.hex(' ')}")
        if response[0] != self.slave_id:
            raise RuntimeError(f"Unexpected slave ID {response[0]}, expected {self.slave_id}")
        return response

    def read_wrench_float(self) -> WrenchSample:
        request = build_read_holding_registers_frame(self.slave_id, READ_FLOAT_START, READ_WORD_COUNT)
        response = self.transact(request, expected_min_bytes=29)
        if response[1] != 0x03:
            raise RuntimeError(f"Unexpected function code {response[1]}")
        byte_count = response[2]
        if byte_count != 24:
            raise RuntimeError(f"Unexpected byte count {byte_count}, expected 24")
        values = struct.unpack(">6f", response[3:27])
        return WrenchSample(time.time(), *values)

    def read_wrench_int(self) -> WrenchSample:
        request = build_read_holding_registers_frame(self.slave_id, READ_INT_START, READ_WORD_COUNT)
        response = self.transact(request, expected_min_bytes=29)
        if response[1] != 0x03:
            raise RuntimeError(f"Unexpected function code {response[1]}")
        byte_count = response[2]
        if byte_count != 24:
            raise RuntimeError(f"Unexpected byte count {byte_count}, expected 24")
        raw_values = struct.unpack(">6i", response[3:27])
        values = tuple(value / 100.0 for value in raw_values)
        return WrenchSample(time.time(), *values)

    def zero_all_channels(self) -> None:
        request = build_write_multiple_registers_frame(self.slave_id, MULTIFUNC_ADDR, [0x0000, 0x001B])
        response = self.transact(request, expected_min_bytes=8)
        if response[1] != 0x10:
            raise RuntimeError(f"Unexpected function code {response[1]}")

    def start_active_upload_protocol2(self) -> None:
        request = build_write_multiple_registers_frame(self.slave_id, ACTIVE_UPLOAD_ADDR, [0x0003])
        response = self.transact(request, expected_min_bytes=8)
        if response[1] != 0x10:
            raise RuntimeError(f"Unexpected function code {response[1]}")


def format_sample(sample: WrenchSample) -> str:
    return (
        f"t={sample.timestamp_wall_s:.6f} "
        f"Fx={sample.fx:.4f} Fy={sample.fy:.4f} Fz={sample.fz:.4f} "
        f"Mx={sample.mx:.4f} My={sample.my:.4f} Mz={sample.mz:.4f}"
    )


def make_csv_writer(path: Path) -> tuple[csv.writer, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(handle)
    writer.writerow(["timestamp_wall_s", "fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm"])
    return writer, handle


def run_stream(args: argparse.Namespace) -> int:
    csv_writer = None
    csv_handle = None
    if args.csv:
        csv_writer, csv_handle = make_csv_writer(Path(args.csv))

    try:
        with DedhModbusClient(args.port, args.baud, args.slave_id, args.timeout) as client:
            if args.zero_first:
                client.zero_all_channels()
                print("Issued zero-all command.")
                time.sleep(0.1)

            count = 0
            deadline = None if args.duration is None else time.monotonic() + args.duration
            while True:
                sample = client.read_wrench_float() if args.mode == "float" else client.read_wrench_int()
                print(format_sample(sample))
                if csv_writer is not None:
                    csv_writer.writerow(
                        [
                            f"{sample.timestamp_wall_s:.9f}",
                            f"{sample.fx:.9f}",
                            f"{sample.fy:.9f}",
                            f"{sample.fz:.9f}",
                            f"{sample.mx:.9f}",
                            f"{sample.my:.9f}",
                            f"{sample.mz:.9f}",
                        ]
                    )
                count += 1

                if args.samples is not None and count >= args.samples:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if args.interval > 0:
                    time.sleep(args.interval)
    finally:
        if csv_handle is not None:
            csv_handle.close()
    return 0


def run_zero(args: argparse.Namespace) -> int:
    with DedhModbusClient(args.port, args.baud, args.slave_id, args.timeout) as client:
        client.zero_all_channels()
    print("Issued zero-all command.")
    return 0


def run_start_active_upload(args: argparse.Namespace) -> int:
    with DedhModbusClient(args.port, args.baud, args.slave_id, args.timeout) as client:
        client.start_active_upload_protocol2()
    print("Issued active-upload protocol 2 start command.")
    print("Power-cycle the sensor to return to Modbus-RTU mode.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal DEDH-75D Modbus-RTU reader for RS485 validation.")
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/cu.usbserial-XXXX")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate. Factory default is 115200.")
    parser.add_argument("--slave-id", type=int, default=1, help="Modbus slave ID. Factory default is 1.")
    parser.add_argument("--timeout", type=float, default=0.2, help="Serial read timeout in seconds.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    stream_parser = subparsers.add_parser("stream", help="Poll the sensor and print 6-axis wrench values.")
    stream_parser.add_argument("--mode", choices=("float", "int"), default="float", help="Read float or int registers.")
    stream_parser.add_argument("--samples", type=int, help="Stop after N samples.")
    stream_parser.add_argument("--duration", type=float, help="Stop after N seconds.")
    stream_parser.add_argument("--interval", type=float, default=0.05, help="Sleep interval between polls in seconds.")
    stream_parser.add_argument("--csv", help="Optional CSV output path.")
    stream_parser.add_argument("--zero-first", action="store_true", help="Issue zero-all before polling.")
    stream_parser.set_defaults(func=run_stream)

    zero_parser = subparsers.add_parser("zero-all", help="Issue the documented zero-all command.")
    zero_parser.set_defaults(func=run_zero)

    upload_parser = subparsers.add_parser(
        "start-active-upload",
        help="Issue the documented command that switches the sensor into protocol 2 active upload mode.",
    )
    upload_parser.set_defaults(func=run_start_active_upload)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Smoke-test a CoinFT Teensy bridge.

This checks the bridge-level protocol used by UMI-FT and the official CoinFT
Teensy relay:

    host -> Teensy: i, s, t, m commands at 115200 baud
    Teensy -> host:
        0x00 0x00
        + uint32 sequence_id
        + uint32 teensy_time_us
        + 12 uint16 left
        + 12 uint16 right

It does not run ONNX calibration. It only verifies transport and raw channels.
"""

import argparse
import struct
import time

import serial


PACKET_LEN = 58
HEADER = b"\x00\x00"


def read_packet(ser: serial.Serial, timeout_s: float) -> bytes:
    deadline = time.monotonic() + timeout_s
    window = bytearray()
    while time.monotonic() < deadline:
        b = ser.read(1)
        if not b:
            continue
        window += b
        if len(window) > 2:
            window = window[-2:]
        if bytes(window) == HEADER:
            body = ser.read(PACKET_LEN - 2)
            if len(body) == PACKET_LEN - 2:
                return HEADER + body
    raise TimeoutError("No complete 58-byte CoinFT packet found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="Teensy serial port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--tare", action="store_true", help="Send 't' before streaming")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Send 'm' to stream Teensy-generated CoinFT-like packets",
    )
    args = parser.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        ser.write(b"i")
        ser.reset_input_buffer()
        time.sleep(0.1)
        if args.tare:
            ser.write(b"t")
            time.sleep(0.1)
        ser.write(b"m" if args.simulate else b"s")
        time.sleep(0.1)

        try:
            for i in range(args.samples):
                pkt = read_packet(ser, args.timeout)
                sequence_id, teensy_time_us, *values = struct.unpack(
                    "<II" + "H" * 24, pkt[2:]
                )
                left = values[:12]
                right = values[12:]
                host_time_s = time.monotonic()
                print(
                    f"{i:04d} seq={sequence_id} teensy_us={teensy_time_us} "
                    f"host_s={host_time_s:.6f} left={tuple(left)} right={tuple(right)}"
                )
        finally:
            ser.write(b"i")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

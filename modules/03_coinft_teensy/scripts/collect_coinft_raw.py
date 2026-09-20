#!/usr/bin/env python3
"""Collect raw CoinFT packets from the Teensy bridge.

This script is intentionally a faithful collector: it records packet bytes,
host receive timestamps, Teensy packet metadata, and raw 12-channel values.
It does not run tare subtraction, ONNX calibration, smoothing, alignment, or
training-format conversion.
"""

from __future__ import annotations

import argparse
import csv
import json
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import serial


MODULE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = MODULE_ROOT / "captures"

HEADER = b"\x00\x00"
BAUD_RATE = 115200
COINFT_CHANNELS = 12
METADATA_LEN = 8
PACKET_LEN = len(HEADER) + METADATA_LEN + COINFT_CHANNELS * 2 * 2
UINT32_MOD = 2**32


class PacketTimeout(TimeoutError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return DEFAULT_OUTPUT_ROOT / f"coinft_raw_{timestamp_id()}"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_exact(ser: serial.Serial, byte_count: int, deadline_s: float) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining > 0:
        if time.monotonic() >= deadline_s:
            raise PacketTimeout(f"timed out reading {byte_count} bytes")
        chunk = ser.read(remaining)
        if not chunk:
            continue
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_packet(ser: serial.Serial, timeout_s: float) -> tuple[bytes, int]:
    """Return one complete 58-byte packet and skipped byte count."""

    deadline_s = time.monotonic() + timeout_s
    window = bytearray()
    skipped = 0
    while time.monotonic() < deadline_s:
        byte = ser.read(1)
        if not byte:
            continue
        window += byte
        if len(window) > len(HEADER):
            window = window[-len(HEADER):]
            skipped += 1
        if bytes(window) == HEADER:
            body = read_exact(ser, PACKET_LEN - len(HEADER), deadline_s)
            return HEADER + body, skipped
    raise PacketTimeout("no complete CoinFT packet found")


def unpack_packet(packet: bytes) -> tuple[int, int, tuple[int, ...], tuple[int, ...]]:
    if len(packet) != PACKET_LEN:
        raise ValueError(f"expected {PACKET_LEN} bytes, got {len(packet)}")
    if packet[: len(HEADER)] != HEADER:
        raise ValueError("packet header mismatch")

    body = packet[len(HEADER):]
    sequence_id, teensy_time_us = struct.unpack("<II", body[:METADATA_LEN])
    values = struct.unpack("<" + "H" * (COINFT_CHANNELS * 2), body[METADATA_LEN:])
    left = tuple(int(v) for v in values[:COINFT_CHANNELS])
    right = tuple(int(v) for v in values[COINFT_CHANNELS:])
    return int(sequence_id), int(teensy_time_us), left, right


def csv_fieldnames() -> list[str]:
    return [
        "packet_index",
        "host_receive_monotonic_ns",
        "host_receive_unix_ns",
        "sequence_id",
        "teensy_time_us",
        *[f"left_c{i}" for i in range(1, COINFT_CHANNELS + 1)],
        *[f"right_c{i}" for i in range(1, COINFT_CHANNELS + 1)],
    ]


def packet_row(
    *,
    packet_index: int,
    host_receive_monotonic_ns: int,
    host_receive_unix_ns: int,
    sequence_id: int,
    teensy_time_us: int,
    left: tuple[int, ...],
    right: tuple[int, ...],
) -> dict[str, int]:
    row: dict[str, int] = {
        "packet_index": packet_index,
        "host_receive_monotonic_ns": host_receive_monotonic_ns,
        "host_receive_unix_ns": host_receive_unix_ns,
        "sequence_id": sequence_id,
        "teensy_time_us": teensy_time_us,
    }
    row.update({f"left_c{i}": value for i, value in enumerate(left, start=1)})
    row.update({f"right_c{i}": value for i, value in enumerate(right, start=1)})
    return row


def estimate_sequence_gap(last_sequence: int | None, sequence_id: int) -> tuple[int, bool]:
    if last_sequence is None:
        return 0, False
    expected = (last_sequence + 1) % UINT32_MOD
    if sequence_id == expected:
        return 0, False
    gap = (sequence_id - expected) % UINT32_MOD
    if gap < 1_000_000:
        return int(gap), False
    return 0, True


def send_start_commands(ser: serial.Serial, args: argparse.Namespace) -> None:
    if args.no_control_commands:
        return
    ser.write(b"i")
    time.sleep(args.idle_settle_s)
    ser.reset_input_buffer()
    if args.tare:
        ser.write(b"t")
        time.sleep(args.tare_settle_s)
    ser.write(b"m" if args.simulate else b"s")
    time.sleep(args.start_settle_s)


def stop_stream(ser: serial.Serial, args: argparse.Namespace) -> None:
    if args.no_control_commands or args.leave_streaming:
        return
    try:
        ser.write(b"i")
        time.sleep(0.02)
    except Exception:
        pass


def collect(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_packets_path = output_dir / "raw_coinft_packets.bin"
    stream_csv_path = output_dir / "raw_coinft_stream.csv"
    capture_log_path = output_dir / "capture_log.json"

    capture_log: dict[str, object] = {
        "schema_version": 1,
        "row_type": "coinft_raw_capture_log",
        "status": "running",
        "created_at": utc_now_iso(),
        "serial_port": args.port,
        "baud": args.baud,
        "duration_s": args.duration_s,
        "max_packets": args.max_packets,
        "packet_len": PACKET_LEN,
        "header_hex": HEADER.hex(),
        "simulate": bool(args.simulate),
        "tare_sent": bool(args.tare),
        "no_control_commands": bool(args.no_control_commands),
        "outputs": {
            "raw_packets_bin": str(raw_packets_path),
            "raw_stream_csv": str(stream_csv_path),
            "capture_log_json": str(capture_log_path),
        },
        "started_at": utc_now_iso(),
        "started_monotonic_ns": time.monotonic_ns(),
    }
    write_json(capture_log_path, capture_log)

    packet_count = 0
    skipped_byte_count = 0
    timeout_count = 0
    dropped_sequence_count = 0
    out_of_order_sequence_count = 0
    first_sequence: int | None = None
    last_sequence: int | None = None
    first_teensy_time_us: int | None = None
    last_teensy_time_us: int | None = None
    interrupted = False

    deadline_s = None if args.duration_s <= 0 else time.monotonic() + args.duration_s

    with serial.Serial(args.port, args.baud, timeout=args.serial_timeout_s) as ser:
        send_start_commands(ser, args)
        with raw_packets_path.open("wb") as raw_writer, stream_csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=csv_fieldnames())
            writer.writeheader()
            try:
                while True:
                    if deadline_s is not None and time.monotonic() >= deadline_s:
                        break
                    if args.max_packets and packet_count >= args.max_packets:
                        break

                    try:
                        packet, skipped = read_packet(ser, args.packet_timeout_s)
                    except PacketTimeout:
                        timeout_count += 1
                        continue

                    host_receive_monotonic_ns = time.monotonic_ns()
                    host_receive_unix_ns = time.time_ns()
                    sequence_id, teensy_time_us, left, right = unpack_packet(packet)

                    gap, out_of_order = estimate_sequence_gap(last_sequence, sequence_id)
                    dropped_sequence_count += gap
                    out_of_order_sequence_count += int(out_of_order)
                    skipped_byte_count += skipped
                    first_sequence = sequence_id if first_sequence is None else first_sequence
                    first_teensy_time_us = teensy_time_us if first_teensy_time_us is None else first_teensy_time_us
                    last_sequence = sequence_id
                    last_teensy_time_us = teensy_time_us

                    raw_writer.write(packet)
                    writer.writerow(
                        packet_row(
                            packet_index=packet_count,
                            host_receive_monotonic_ns=host_receive_monotonic_ns,
                            host_receive_unix_ns=host_receive_unix_ns,
                            sequence_id=sequence_id,
                            teensy_time_us=teensy_time_us,
                            left=left,
                            right=right,
                        )
                    )
                    packet_count += 1
                    if args.report_interval_packets and packet_count % args.report_interval_packets == 0:
                        print(
                            f"packets={packet_count} seq={sequence_id} "
                            f"dropped={dropped_sequence_count} timeouts={timeout_count}",
                            flush=True,
                        )
            except KeyboardInterrupt:
                interrupted = True
                print()
            finally:
                stop_stream(ser, args)

    completed_at = utc_now_iso()
    capture_log.update(
        {
            "status": "interrupted" if interrupted else "completed",
            "completed_at": completed_at,
            "completed_monotonic_ns": time.monotonic_ns(),
            "packet_count": packet_count,
            "skipped_byte_count": skipped_byte_count,
            "timeout_count": timeout_count,
            "dropped_sequence_count": dropped_sequence_count,
            "out_of_order_sequence_count": out_of_order_sequence_count,
            "first_sequence_id": first_sequence,
            "last_sequence_id": last_sequence,
            "first_teensy_time_us": first_teensy_time_us,
            "last_teensy_time_us": last_teensy_time_us,
        }
    )
    write_json(capture_log_path, capture_log)

    print(f"Wrote {output_dir}")
    print(f"Packets: {packet_count}")
    print(f"CSV: {stream_csv_path}")
    print(f"Raw packets: {raw_packets_path}")
    return 130 if interrupted else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Faithfully collect raw CoinFT packets from a Teensy USB serial bridge.")
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/cu.usbmodemXXXX")
    parser.add_argument("--output-dir", help="Output directory. Defaults to modules/03_coinft_teensy/captures/coinft_raw_<utc>.")
    parser.add_argument("--duration-s", type=float, default=10.0, help="Capture duration. 0 means run until interrupted.")
    parser.add_argument("--max-packets", type=int, default=0, help="Optional packet limit. 0 means no limit.")
    parser.add_argument("--baud", type=int, default=BAUD_RATE)
    parser.add_argument("--serial-timeout-s", type=float, default=0.02)
    parser.add_argument("--packet-timeout-s", type=float, default=1.0)
    parser.add_argument("--tare", action="store_true", help="Send 't' before streaming.")
    parser.add_argument("--simulate", action="store_true", help="Send 'm' instead of 's' for Teensy-generated mock packets.")
    parser.add_argument("--no-control-commands", action="store_true", help="Do not send i/t/s/m commands; only read the current serial stream.")
    parser.add_argument("--leave-streaming", action="store_true", help="Do not send 'i' on exit.")
    parser.add_argument("--idle-settle-s", type=float, default=0.2)
    parser.add_argument("--tare-settle-s", type=float, default=0.1)
    parser.add_argument("--start-settle-s", type=float, default=0.05)
    parser.add_argument("--report-interval-packets", type=int, default=360)
    args = parser.parse_args()
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())

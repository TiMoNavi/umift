#!/usr/bin/env python3
"""Forward official CoinFT Teensy packets to an iPhone over UDP.

The Teensy-facing contract follows the recovered UMI-FT/CoinFT evidence:
115200 baud, host commands i/s/t, and packets shaped as:

    0x00 0x00 + sequence_id uint32 + teensy_time_us uint32
    + 12 uint16 left + 12 uint16 right

The UDP payload is one JSON object per datagram. ONNX calibration is optional;
without models/norms, wrench values are emitted as null so the iPhone can still
test transport, packet rate, drops, and left/right binding.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: pip install pyserial") from exc

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: pip install numpy") from exc


HEADER = b"\x00\x00"
METADATA_LEN = 8
RAW_CHANNELS = 12
PACKET_LEN = len(HEADER) + METADATA_LEN + RAW_CHANNELS * 2 * 2


@dataclass
class Calibrator:
    session: object
    input_name: str
    mu_x: np.ndarray
    sd_x: np.ndarray
    mu_y: np.ndarray
    sd_y: np.ndarray
    offset: np.ndarray
    window: int
    history: list[np.ndarray]

    @classmethod
    def create(
        cls,
        model_path: Path,
        norm_path: Path,
        offset: np.ndarray,
        window: int,
    ) -> "Calibrator":
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("Missing dependency: pip install onnxruntime") from exc

        with norm_path.open("r", encoding="utf-8") as f:
            norm = json.load(f)
        session = ort.InferenceSession(str(model_path))
        return cls(
            session=session,
            input_name=session.get_inputs()[0].name,
            mu_x=np.asarray(norm["mu_x"], dtype=np.float32),
            sd_x=np.asarray(norm["sd_x"], dtype=np.float32),
            mu_y=np.asarray(norm["mu_y"], dtype=np.float32),
            sd_y=np.asarray(norm["sd_y"], dtype=np.float32),
            offset=offset.astype(np.float64),
            window=window,
            history=[],
        )

    def infer(self, raw: np.ndarray) -> list[float]:
        adjusted = raw.astype(np.float64) - self.offset
        x_n = ((adjusted.astype(np.float32) - self.mu_x) / self.sd_x).reshape(1, RAW_CHANNELS)
        y_n = self.session.run(None, {self.input_name: x_n})[0].flatten()
        y = (y_n * self.sd_y + self.mu_y).astype(np.float64)
        self.history.append(y)
        if len(self.history) > self.window:
            self.history.pop(0)
        return np.mean(self.history, axis=0).astype(float).tolist()


def read_packet(ser: serial.Serial, timeout_s: float) -> tuple[int, int, np.ndarray, np.ndarray]:
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
            if len(body) != PACKET_LEN - 2:
                continue
            sequence_id, teensy_time_us = struct.unpack("<II", body[:METADATA_LEN])
            values = struct.unpack("<" + "H" * (RAW_CHANNELS * 2), body[METADATA_LEN:])
            left = np.asarray(values[:RAW_CHANNELS], dtype=np.float64)
            right = np.asarray(values[RAW_CHANNELS:], dtype=np.float64)
            return int(sequence_id), int(teensy_time_us), left, right
    raise TimeoutError(f"No complete {PACKET_LEN}-byte CoinFT packet found")


def collect_offsets(
    ser: serial.Serial,
    samples: int,
    ignored: int,
    timeout_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    left_rows = []
    right_rows = []
    while len(left_rows) < samples:
        _, _, left, right = read_packet(ser, timeout_s)
        left_rows.append(left)
        right_rows.append(right)
        if len(left_rows) % 100 == 0:
            print(f"tare samples {len(left_rows)}/{samples}", file=sys.stderr)
    kept_left = np.vstack(left_rows)[ignored:]
    kept_right = np.vstack(right_rows)[ignored:]
    return np.mean(kept_left, axis=0), np.mean(kept_right, axis=0)


def make_socket(broadcast: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CoinFT Teensy serial to UDP LAN bridge")
    parser.add_argument("--port", required=True, help="Teensy serial port, e.g. /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--udp-host", default="255.255.255.255")
    parser.add_argument("--udp-port", type=int, default=43001)
    parser.add_argument("--broadcast", dest="broadcast", action="store_true", default=True)
    parser.add_argument("--no-broadcast", dest="broadcast", action="store_false")
    parser.add_argument("--source-id", default="coinft-rpi-01")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--tare-samples", type=int, default=1000)
    parser.add_argument("--ignore-tare-samples", type=int, default=10)
    parser.add_argument("--window", type=int, default=10, help="Moving average window for calibrated wrench")
    parser.add_argument("--left-model", type=Path)
    parser.add_argument("--right-model", type=Path)
    parser.add_argument("--left-norm", type=Path)
    parser.add_argument("--right-norm", type=Path)
    parser.add_argument("--no-host-tare", action="store_true", help="Skip host-side raw offset collection")
    parser.add_argument("--send-teensy-tare", action="store_true", help="Send 't' to CoinFT boards before host tare")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    have_calibration = all([args.left_model, args.right_model, args.left_norm, args.right_norm])
    partial_calibration = any([args.left_model, args.right_model, args.left_norm, args.right_norm])
    if partial_calibration and not have_calibration:
        raise SystemExit("Provide all of --left-model --right-model --left-norm --right-norm, or none.")

    destination = (args.udp_host, args.udp_port)
    sock = make_socket(args.broadcast)

    with serial.Serial(args.port, args.baud, timeout=0.05) as ser:
        print(f"opened {args.port} @ {args.baud}", file=sys.stderr)
        ser.write(b"i")
        ser.reset_input_buffer()
        time.sleep(0.2)
        if args.send_teensy_tare:
            ser.write(b"t")
            time.sleep(0.2)
        ser.write(b"s")
        time.sleep(0.1)

        if args.no_host_tare:
            left_offset = np.zeros(RAW_CHANNELS, dtype=np.float64)
            right_offset = np.zeros(RAW_CHANNELS, dtype=np.float64)
        else:
            print("collecting host-side raw offsets", file=sys.stderr)
            left_offset, right_offset = collect_offsets(
                ser,
                samples=args.tare_samples,
                ignored=args.ignore_tare_samples,
                timeout_s=args.timeout,
            )
            print("offsets ready", file=sys.stderr)

        left_cal: Optional[Calibrator] = None
        right_cal: Optional[Calibrator] = None
        if have_calibration:
            left_cal = Calibrator.create(args.left_model, args.left_norm, left_offset, args.window)
            right_cal = Calibrator.create(args.right_model, args.right_norm, right_offset, args.window)
            print("onnx calibration ready", file=sys.stderr)

        seq = 0
        last_monotonic_ns: Optional[int] = None
        try:
            while True:
                teensy_sequence_id, teensy_time_us, left_raw, right_raw = read_packet(ser, args.timeout)
                now_ns = time.time_ns()
                monotonic_ns = time.monotonic_ns()
                if last_monotonic_ns is None:
                    period_hint = None
                else:
                    period_hint = (monotonic_ns - last_monotonic_ns) / 1_000_000_000.0
                last_monotonic_ns = monotonic_ns

                left_adjusted = left_raw - left_offset
                right_adjusted = right_raw - right_offset
                payload = {
                    "schema": "umift.coinft.lan.v1",
                    "seq": seq,
                    "teensy_sequence_id": teensy_sequence_id,
                    "teensy_time_us": teensy_time_us,
                    "source_id": args.source_id,
                    "bridge_time_ns": now_ns,
                    "bridge_monotonic_ns": monotonic_ns,
                    "sample_period_hint_s": period_hint,
                    "left": {
                        "raw": left_raw.astype(int).tolist(),
                        "raw_offset_adjusted": left_adjusted.astype(float).tolist(),
                        "wrench": left_cal.infer(left_raw) if left_cal else None,
                    },
                    "right": {
                        "raw": right_raw.astype(int).tolist(),
                        "raw_offset_adjusted": right_adjusted.astype(float).tolist(),
                        "wrench": right_cal.infer(right_raw) if right_cal else None,
                    },
                }
                data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
                sock.sendto(data, destination)
                if seq % 200 == 0:
                    print(f"sent seq={seq} to {destination[0]}:{destination[1]}", file=sys.stderr)
                seq += 1
        except KeyboardInterrupt:
            print("stopping", file=sys.stderr)
        finally:
            try:
                ser.write(b"i")
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

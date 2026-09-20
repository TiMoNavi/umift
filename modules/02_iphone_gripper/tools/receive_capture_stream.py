#!/usr/bin/env python3
"""Receive UMIFT CaptureCore JSONL frames from the iPhone TCP stream.

The iPhone app listens on TCP port 17381. For USB use, forward that device port
to localhost first, then run this script against 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_ROOT = REPO_ROOT / "modules" / "02_iphone_gripper"
DEFAULT_OUTPUT_DIR = MODULE_ROOT / "recordings" / "stream"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17381


@dataclass
class StreamStats:
    frame_count: int = 0
    pose_valid_count: int = 0
    gripper_valid_count: int = 0
    rgb_valid_count: int = 0
    dropped_sequence_count: int = 0
    json_error_count: int = 0
    started_wall_s: float = field(default_factory=time.monotonic)
    last_report_wall_s: float = field(default_factory=time.monotonic)
    first_timestamp_ns: int | None = None
    last_timestamp_ns: int | None = None
    last_sequence: int | None = None
    gripper_values: list[float] = field(default_factory=list)
    gripper_ages_ms: list[float] = field(default_factory=list)
    timebase_statuses: set[str] = field(default_factory=set)
    tracking_states: set[str] = field(default_factory=set)

    def observe(self, row: dict[str, Any]) -> None:
        if row.get("rowType") != "frame" and row.get("row_type") != "frame":
            return

        self.frame_count += 1
        self.pose_valid_count += bool(row.get("poseValid"))
        self.gripper_valid_count += bool(row.get("gripperValid"))
        self.rgb_valid_count += bool(row.get("rgbValid"))

        sequence = row.get("sequence")
        if isinstance(sequence, int):
            if self.last_sequence is not None and sequence != self.last_sequence + 1:
                self.dropped_sequence_count += max(1, sequence - self.last_sequence - 1)
            self.last_sequence = sequence

        timestamp_ns = row.get("alignedTimestampNS")
        if isinstance(timestamp_ns, int):
            if self.first_timestamp_ns is None:
                self.first_timestamp_ns = timestamp_ns
            self.last_timestamp_ns = timestamp_ns

        gripper_open = row.get("gripperOpenPercent")
        if isinstance(gripper_open, (int, float)):
            self.gripper_values.append(float(gripper_open))

        gripper_age = row.get("gripperAgeMS")
        if isinstance(gripper_age, (int, float)):
            self.gripper_ages_ms.append(float(gripper_age))

        timebase = row.get("timebaseStatus")
        if isinstance(timebase, str):
            self.timebase_statuses.add(timebase)

        tracking = row.get("tracking")
        if isinstance(tracking, str):
            self.tracking_states.add(tracking)

    def mark_json_error(self) -> None:
        self.json_error_count += 1

    def should_report(self, interval_s: float) -> bool:
        now = time.monotonic()
        return now - self.last_report_wall_s >= interval_s

    def report(self, *, final: bool = False) -> str:
        now = time.monotonic()
        elapsed_wall_s = max(0.001, now - self.started_wall_s)
        self.last_report_wall_s = now

        capture_duration_s = None
        capture_fps = None
        if self.first_timestamp_ns is not None and self.last_timestamp_ns is not None:
            capture_duration_s = max(0.0, (self.last_timestamp_ns - self.first_timestamp_ns) / 1e9)
            if capture_duration_s > 0:
                capture_fps = self.frame_count / capture_duration_s

        wall_fps = self.frame_count / elapsed_wall_s
        parts = [
            "final" if final else "live",
            f"frames={self.frame_count}",
            f"wall_fps={wall_fps:.2f}",
        ]
        if capture_fps is not None:
            parts.append(f"capture_fps={capture_fps:.2f}")
        if self.last_sequence is not None:
            parts.append(f"last_seq={self.last_sequence}")
        parts.extend(
            [
                f"pose={self.pose_valid_count}/{self.frame_count}",
                f"gripper={self.gripper_valid_count}/{self.frame_count}",
                f"rgb={self.rgb_valid_count}/{self.frame_count}",
                f"dropped={self.dropped_sequence_count}",
            ]
        )
        if self.gripper_values:
            parts.append(
                "open={:.1f}..{:.1f}%".format(
                    min(self.gripper_values),
                    max(self.gripper_values),
                )
            )
        if self.gripper_ages_ms:
            parts.append(
                "gripper_age_ms={:.1f}/{:.1f}/{:.1f}".format(
                    min(self.gripper_ages_ms),
                    statistics.mean(self.gripper_ages_ms),
                    max(self.gripper_ages_ms),
                )
            )
        if self.timebase_statuses:
            parts.append("timebase=" + ",".join(sorted(self.timebase_statuses)))
        if self.tracking_states:
            parts.append("tracking=" + ",".join(sorted(self.tracking_states)))
        if self.json_error_count:
            parts.append(f"json_errors={self.json_error_count}")
        return " ".join(parts)


def timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def default_output_path() -> Path:
    return DEFAULT_OUTPUT_DIR / f"stream_{timestamp_id()}.jsonl"


def connect_with_retry(host: str, port: int, retry_s: float, timeout_s: float) -> socket.socket:
    deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
    attempt = 0
    while True:
        attempt += 1
        try:
            sock = socket.create_connection((host, port), timeout=5.0)
            sock.settimeout(None)
            print(f"connected host={host} port={port} attempt={attempt}")
            return sock
        except OSError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise SystemExit(f"failed to connect to {host}:{port}: {exc}") from exc
            print(f"waiting for stream host={host} port={port}: {exc}", file=sys.stderr)
            time.sleep(max(0.1, retry_s))


def run(args: argparse.Namespace) -> int:
    output_path = Path(args.output).expanduser().resolve() if args.output else default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sock = connect_with_retry(args.host, args.port, args.retry_s, args.connect_timeout_s)
    stats = StreamStats()

    print(f"writing {output_path}")
    try:
        with sock, sock.makefile("rb") as reader, output_path.open("ab") as writer:
            for raw_line in reader:
                writer.write(raw_line)
                writer.flush()

                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError:
                    stats.mark_json_error()
                    continue
                stats.observe(row)

                if stats.should_report(args.report_interval_s):
                    print(stats.report())
    except KeyboardInterrupt:
        print()
    finally:
        print(stats.report(final=True))
        print(f"saved {output_path}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Receive CaptureCore JSONL frames from a forwarded iPhone TCP stream."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output", help="Output JSONL path. Defaults to recordings/stream/stream_<utc>.jsonl")
    parser.add_argument("--retry-s", type=float, default=1.0)
    parser.add_argument(
        "--connect-timeout-s",
        type=float,
        default=0.0,
        help="Total connect timeout. 0 means wait forever.",
    )
    parser.add_argument("--report-interval-s", type=float, default=1.0)
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""Synthetic Teensy/CoinFT raw sample worker used by the laptop GUI."""

from __future__ import annotations

import csv
import math
import random
import threading
import time
from pathlib import Path
from typing import Any

from umift_laptop_alignment.capture.buffers.timestamped_ring import preroll_start_ns
from umift_laptop_alignment.orchestration.run_layout import register_artifact, timestamp_id
from umift_laptop_alignment.capture.receivers.coinft.buffer import CoinFTRawBuffer


DEFAULT_RECORDING_PREROLL_S = 1.5
DEFAULT_STREAM_BUFFER_S = 3.0
TEENSY_BUFFER_MAX_SAMPLES = 600


class TeensyMockWorker(threading.Thread):
    """Generate raw 12+12 channel Teensy/CoinFT-like samples with pre-roll."""

    channel_count = 12

    def __init__(self, backend: Any, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.backend = backend
        self.stop_event = stop_event

    def output_dir(self) -> Path:
        with self.backend.lock:
            run_dir = self.backend.state.get("run_dir")
        if not run_dir:
            run_dir = str(self.backend.ensure_active_run())
        return Path(str(run_dir)) / "raw" / "coinft" / f"teensy_mock_{timestamp_id()}"

    def run(self) -> None:
        started = time.monotonic()
        live_sample_count = 0
        recorded_sample_count = 0
        episode_recorded_count = 0
        active_episode_index: int | None = None
        persisted_sequences: set[int] = set()
        csv_file: Any | None = None
        writer: csv.DictWriter[Any] | None = None
        csv_path: Path | None = None
        options = self.backend.capture_options_snapshot()
        teensy_buffer = CoinFTRawBuffer(
            name="teensy",
            max_age_s=float(options.get("stream_buffer_s", DEFAULT_STREAM_BUFFER_S)),
            max_items=TEENSY_BUFFER_MAX_SAMPLES,
        )
        fieldnames = [
            "packet_index",
            "host_receive_monotonic_ns",
            "host_receive_unix_ns",
            "sequence_id",
            "teensy_time_us",
            *[f"left_c{i}" for i in range(1, self.channel_count + 1)],
            *[f"right_c{i}" for i in range(1, self.channel_count + 1)],
        ]

        def write_sample_row(row: dict[str, Any]) -> bool:
            nonlocal recorded_sample_count, episode_recorded_count
            sequence = row.get("sequence_id")
            if not isinstance(sequence, int) or sequence in persisted_sequences or writer is None:
                return False
            out = dict(row)
            out["packet_index"] = episode_recorded_count
            writer.writerow(out)
            persisted_sequences.add(sequence)
            recorded_sample_count += 1
            episode_recorded_count += 1
            return True

        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                phase = now - started
                left = self.synthetic_side(phase, side_offset=0.0)
                right = self.synthetic_side(phase, side_offset=0.7)
                row: dict[str, Any] = {
                    "packet_index": "",
                    "host_receive_monotonic_ns": time.monotonic_ns(),
                    "host_receive_unix_ns": time.time_ns(),
                    "sequence_id": live_sample_count,
                    "teensy_time_us": int(phase * 1_000_000),
                }
                row.update({f"left_c{i}": value for i, value in enumerate(left, start=1)})
                row.update({f"right_c{i}": value for i, value in enumerate(right, start=1)})
                teensy_buffer.append_sample(
                    row,
                    timestamp_ns=row["host_receive_monotonic_ns"],
                    sequence_id=row["sequence_id"],
                )

                recording_window = self.backend.recording_window_snapshot()
                recording_active = bool(recording_window.get("active"))
                episode_index = int(recording_window.get("episode_index") or 0)
                if recording_active and (writer is None or active_episode_index != episode_index):
                    if csv_file is not None:
                        csv_file.close()
                    active_episode_index = episode_index
                    episode_recorded_count = 0
                    persisted_sequences = set()
                    output_dir = self.output_dir().parent / f"episode_{episode_index:06d}"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    csv_path = output_dir / "teensy_mock_stream.csv"
                    csv_file = csv_path.open("w", newline="", encoding="utf-8")
                    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                    writer.writeheader()
                    with self.backend.lock:
                        run_dir = self.backend.state.get("run_dir")
                    if run_dir:
                        register_artifact(
                            Path(str(run_dir)),
                            stream="coinft",
                            role=f"raw_mock_episode_{episode_index:06d}",
                            path=csv_path,
                            metadata={
                                "kind": "file",
                                "source": "teensy_mock",
                                "episode_index": episode_index,
                                "trigger": "iphone_recording_event",
                                "recording_preroll_s": recording_window.get("recording_preroll_s"),
                            },
                        )
                    start_ns = preroll_start_ns(
                        recording_window.get("event_start_aligned_monotonic_ns")
                        if isinstance(recording_window.get("event_start_aligned_monotonic_ns"), int)
                        else row["host_receive_monotonic_ns"],
                        float(recording_window.get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S)),
                    )
                    flushed = 0
                    for sample in teensy_buffer.slice_window(start_ns=start_ns):
                        if isinstance(sample.payload, dict) and write_sample_row(sample.payload):
                            flushed += 1
                    if csv_file is not None:
                        csv_file.flush()
                    self.backend.log(f"Teensy pre-roll flushed {flushed} buffered rows for episode {episode_index}")
                    self.backend.update_buffer_state("teensy", teensy_buffer.stats(), {"last_preroll_flushed": flushed})

                if not recording_active and writer is not None:
                    if csv_file is not None:
                        csv_file.flush()
                        csv_file.close()
                    writer = None
                    csv_file = None

                if recording_active and writer is not None:
                    write_sample_row(row)
                    if recorded_sample_count % 10 == 0 and csv_file is not None:
                        csv_file.flush()

                live_sample_count += 1
                elapsed = max(0.001, time.monotonic() - started)
                left_mean = sum(left) / len(left)
                right_mean = sum(right) / len(right)
                state = {
                    "running": True,
                    "recording": recording_active,
                    "live_sample_count": live_sample_count,
                    "recorded_sample_count": recorded_sample_count,
                    "fps": live_sample_count / elapsed,
                    "left_mean": left_mean,
                    "right_mean": right_mean,
                    "output_csv": str(csv_path) if csv_path else None,
                    "latest_sample_monotonic_ns": row["host_receive_monotonic_ns"],
                }
                with self.backend.lock:
                    self.backend.state["teensy"] = state
                    snapshot = dict(self.backend.state)
                self.backend.broadcast({"type": "state", "state": snapshot})
                self.backend.broadcast(
                    {
                        "type": "teensy_sample",
                        "sample": {
                            "packet_index": live_sample_count - 1,
                            "left": left,
                            "right": right,
                            "left_mean": left_mean,
                            "right_mean": right_mean,
                            "fps": state["fps"],
                        },
                    }
                )
                if live_sample_count % 10 == 0:
                    self.backend.refresh_preflight_state()
                if live_sample_count % 30 == 0:
                    self.backend.update_buffer_state("teensy", teensy_buffer.stats())
                time.sleep(1.0 / 60.0)
        finally:
            if csv_file is not None:
                csv_file.close()
            self.backend.teensy_finished()

    def synthetic_side(self, phase: float, *, side_offset: float) -> list[int]:
        values: list[int] = []
        for index in range(self.channel_count):
            slow = math.sin(phase * 1.7 + index * 0.28 + side_offset)
            fast = math.sin(phase * 8.0 + index * 0.43 + side_offset) * 0.22
            noise = random.uniform(-12.0, 12.0)
            value = 2048 + 240 * slow + 90 * fast + noise
            values.append(int(max(0, min(4095, value))))
        return values

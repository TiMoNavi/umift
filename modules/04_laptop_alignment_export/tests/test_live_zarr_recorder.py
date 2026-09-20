from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from umift_laptop_alignment.pipeline.disk.live_zarr_recorder import LiveZarrRecordingWriter


def conversion_snapshot(*, latest_ts: int = 2_000, step_ts: int = 1_000) -> dict:
    source_row = {
        "timestamp_ns": step_ts,
        "timestamp_unix_ns": step_ts + 10,
        "episode_index": 3,
        "recording_active": True,
        "pose_mode": "user_world_phone",
        "pos": [1.0, 2.0, 3.0, 0.1, 0.2, 0.3],
        "gripper_state": [0.0],
        "force": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "force_mode": "placeholder_zero",
    }
    latest_row = {
        **source_row,
        "timestamp_ns": latest_ts,
        "timestamp_unix_ns": latest_ts + 10,
        "pos": [2.0, 2.0, 3.0, 0.1, 0.2, 0.3],
        "gripper_state": [1.0],
    }
    return {
        "latest_row": latest_row,
        "latest_completed_step": {
            "from_timestamp_ns": step_ts,
            "to_timestamp_ns": latest_ts,
            "dt_ms": 1.0,
            "episode_index": 3,
            "recording_active": True,
            "row": source_row,
            "action": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "gripper_action": [1.0],
            "gripper_action_semantics": "next_output_frame_binary_state",
        },
    }


class LiveZarrRecordingWriterTest(unittest.TestCase):
    def test_writes_completed_and_tail_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = LiveZarrRecordingWriter()
            start = writer.start(
                run_dir=Path(tmpdir),
                episode_index=3,
                transition={"event": "record_start_committed", "episode_index": 3},
            )
            self.assertEqual(start["status"], "recording")

            writer.observe_conversion(conversion_snapshot())
            stopped = writer.stop(
                transition={"event": "record_stop_committed", "episode_index": 3},
                conversion=conversion_snapshot(),
            )

            rows_path = Path(stopped["rows_path"])
            rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["row_type"], "live_forceflow_completed_row")
            self.assertEqual(rows[0]["action"], [1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            self.assertEqual(rows[0]["gripper_action"], [1.0])
            self.assertEqual(rows[1]["row_type"], "live_forceflow_terminal_row")
            self.assertEqual(rows[1]["action"], [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            self.assertEqual(rows[1]["gripper_action"], [1.0])

            manifest = json.loads(Path(stopped["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["completed_rows_written"], 1)
            self.assertEqual(manifest["tail_rows_written"], 1)
            self.assertEqual(manifest["final_zarr_writer"], "pending")


if __name__ == "__main__":
    unittest.main()

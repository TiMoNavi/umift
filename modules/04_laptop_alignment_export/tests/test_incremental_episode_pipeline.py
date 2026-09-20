from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import zarr

from umift_laptop_alignment.pipeline.normalize.runner import normalize_episode, resolve_episode_window
from umift_laptop_alignment.orchestration.run_layout import ensure_run_layout
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter import UmiFTReplayBufferExporter
from v2_test_utils import write_v2_session


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_episode_raw(run_dir: Path, episode_index: int, start_ns: int) -> None:
    episode_name = f"episode_{episode_index:06d}"
    d435_dir = run_dir / "raw" / "global_camera" / episode_name
    write_json(
        d435_dir / "camera_metadata.json",
        {
            "runContext": {
                "recordControl": {
                    "episodeIndex": episode_index,
                    "eventAlignedMonotonicNs": start_ns,
                }
            },
            "streams": {"depth": {"shape": [2, 2], "depthScaleMeters": 0.001}},
        },
    )
    d435_dir.mkdir(parents=True, exist_ok=True)
    with (d435_dir / "frame_timestamps.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["frame_index", "host_receive_monotonic_ns"])
        writer.writeheader()
        writer.writerow({"frame_index": 0, "host_receive_monotonic_ns": start_ns + 10})
    coinft_dir = run_dir / "raw" / "coinft" / episode_name
    coinft_dir.mkdir(parents=True, exist_ok=True)
    fields = ["host_receive_monotonic_ns", *[f"left_c{i}" for i in range(1, 13)], *[f"right_c{i}" for i in range(1, 13)]]
    with (coinft_dir / "raw_coinft_stream.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"host_receive_monotonic_ns": start_ns + 20, **{name: 0 for name in fields[1:]}})


def export_inputs(run_dir: Path, episode_index: int, session_id: str) -> None:
    episode_name = f"episode_{episode_index:06d}"
    aligned_dir = run_dir / "aligned" / "episodes" / episode_name
    normalized_dir = run_dir / "normalized" / "episodes" / episode_name
    start_ns = 1_000_000_000 + episode_index * 1_000_000_000
    end_ns = start_ns + 100_000_000
    write_json(
        aligned_dir / "episode.json",
        {
            "source_episode_index": episode_index,
            "session_id": session_id,
            "start_aligned_monotonic_ns": start_ns,
            "end_aligned_monotonic_ns": end_ns,
        },
    )
    rows = []
    for sample_index, timestamp_ns in enumerate((start_ns, start_ns + 33_000_000)):
        rows.append(
            {
                "aligned_monotonic_ns": timestamp_ns,
                "sample_index": sample_index,
                "valid_mask": {"iphone": True, "d435": True, "coinft": True},
                "iphone": {
                    "session_id": session_id,
                    "rgb": {"valid": True, "codec": "h264", "video_path": "unused.h264", "frame_index": sample_index},
                    "pose": {"valid": True, "world_calibrated": True, "frame": "user_world"},
                },
            }
        )
    write_jsonl(aligned_dir / "timeline.jsonl", rows)
    write_jsonl(
        normalized_dir / "coinft_samples.jsonl",
        [
            {
                "aligned_monotonic_ns": start_ns,
                "left_wrench": [0.0] * 6,
                "right_wrench": [0.0] * 6,
            }
        ],
    )


def fake_arrays(*args, **kwargs):
    arrays = {
        "rgb_0": np.zeros((2, 2, 2, 3), dtype=np.uint8),
        "ts_pose_fb_0": np.zeros((2, 7), dtype=np.float32),
        "gripper_0": np.zeros((2, 1), dtype=np.float32),
        "wrench_left_0": np.zeros((2, 6), dtype=np.float32),
        "wrench_right_0": np.zeros((2, 6), dtype=np.float32),
    }
    counts = {
        "missing_iphone_images": 0,
        "missing_global_rgb_frames": 0,
        "missing_depth_frames": 0,
        "missing_iphone_depth_frames": 0,
    }
    return arrays, counts


def directory_hashes(path: Path) -> dict[str, str]:
    return {
        str(file.relative_to(path)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def chunk_hashes(path: Path) -> dict[str, str]:
    return {
        str(file.relative_to(path)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(path.rglob("*"))
        if file.is_file() and not file.name.startswith(".")
    }


class EpisodeWindowTest(unittest.TestCase):
    def test_interrupted_session_does_not_block_later_complete_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            ensure_run_layout(run_dir)
            write_v2_session(
                run_dir,
                session_id="interrupted-session",
                aligned_timestamps=[100, 200],
                events=[(0, 2, 0)],
            )
            write_v2_session(
                run_dir,
                session_id="complete-session",
                aligned_timestamps=[10_000_000_000, 10_100_000_000],
                events=[(0, 2, 0), (1, 3, 1)],
            )
            write_episode_raw(run_dir, 0, 100)
            write_episode_raw(run_dir, 1, 10_000_000_000)

            with self.assertRaisesRegex(SystemExit, "cannot be matched"):
                resolve_episode_window(run_dir, 0)
            complete = resolve_episode_window(run_dir, 1)

            self.assertEqual(complete.session_id, "complete-session")
            self.assertEqual((complete.start_ns, complete.end_ns), (10_000_000_000, 10_100_000_000))

    def test_two_recording_windows_in_one_iphone_session_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            ensure_run_layout(run_dir)
            session_id = "shared-session"
            write_v2_session(
                run_dir,
                session_id=session_id,
                aligned_timestamps=[100, 150, 200, 300, 350, 400],
                events=[(0, 2, 0), (2, 3, 1), (3, 2, 2), (5, 3, 3)],
            )
            write_episode_raw(run_dir, 0, 100)
            write_episode_raw(run_dir, 1, 300)

            first = resolve_episode_window(run_dir, 0)
            second = resolve_episode_window(run_dir, 1)

            self.assertEqual((first.start_ns, first.end_ns), (100, 200))
            self.assertEqual((second.start_ns, second.end_ns), (300, 400))
            self.assertEqual(first.session_id, second.session_id)

            report = normalize_episode(run_dir, 1)
            normalized_rows = [
                json.loads(line)
                for line in (run_dir / report["outputs"]["iphone_frames"]).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["sequence"] for row in normalized_rows], [3, 4, 5])
            self.assertEqual(report["source_episode_index"], 1)


class IncrementalExporterTest(unittest.TestCase):
    def test_append_does_not_modify_existing_episode_and_failure_does_not_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            ensure_run_layout(run_dir)
            export_inputs(run_dir, 0, "shared-session")
            export_inputs(run_dir, 1, "shared-session")
            exporter = UmiFTReplayBufferExporter()

            with patch(
                "umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter.build_episode_arrays",
                side_effect=fake_arrays,
            ):
                first_manifest = exporter.export_episode(run_dir, 0)
                zarr_path = run_dir / first_manifest["outputs"]["zarr"]
                first_group = zarr_path / "data" / "episode_0"
                before = directory_hashes(first_group)
                second_manifest = exporter.export_episode(run_dir, 1)

            self.assertEqual(before, directory_hashes(first_group))
            root = zarr.open_group(str(zarr_path), mode="r")
            self.assertEqual(sorted(root["data"].group_keys()), ["episode_0", "episode_1"])
            self.assertEqual(root["data"]["episode_0"].attrs["source_episode_index"], 0)
            self.assertEqual(root["data"]["episode_1"].attrs["source_episode_index"], 1)
            self.assertEqual(second_manifest["episode_count"], 2)

            export_inputs(run_dir, 2, "shared-session")
            with patch(
                "umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter.build_episode_arrays",
                side_effect=RuntimeError("build failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "build failed"):
                    exporter.export_episode(run_dir, 2)

            self.assertEqual(before, directory_hashes(first_group))
            root = zarr.open_group(str(zarr_path), mode="r")
            self.assertEqual(sorted(root["data"].group_keys()), ["episode_0", "episode_1"])

    def test_delete_middle_episode_reindexes_without_rewriting_retained_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            ensure_run_layout(run_dir)
            exporter = UmiFTReplayBufferExporter()
            for episode_index in range(3):
                export_inputs(run_dir, episode_index, "shared-session")
            with patch(
                "umift_laptop_alignment.pipeline.export.umift_replay_buffer.exporter.build_episode_arrays",
                side_effect=fake_arrays,
            ):
                manifest = None
                for episode_index in range(3):
                    manifest = exporter.export_episode(run_dir, episode_index)
            assert manifest is not None
            zarr_path = run_dir / manifest["outputs"]["zarr"]
            first_before = chunk_hashes(zarr_path / "data" / "episode_0")
            third_before = chunk_hashes(zarr_path / "data" / "episode_2")

            updated = exporter.delete_episode(run_dir, zarr_path, 1)

            self.assertEqual(first_before, chunk_hashes(zarr_path / "data" / "episode_0"))
            self.assertEqual(third_before, chunk_hashes(zarr_path / "data" / "episode_1"))
            root = zarr.open_group(str(zarr_path), mode="r")
            self.assertEqual(sorted(root["data"].group_keys()), ["episode_0", "episode_1"])
            self.assertEqual(root["data"]["episode_1"].attrs["source_episode_index"], 2)
            self.assertEqual(root["data"]["episode_1"].attrs["zarr_episode_index"], 1)
            self.assertEqual(
                [row["source_episode_index"] for row in updated["episodes"]],
                [0, 2],
            )


if __name__ == "__main__":
    unittest.main()

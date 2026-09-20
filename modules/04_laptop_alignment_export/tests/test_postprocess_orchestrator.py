from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umift_laptop_alignment.pipeline.postprocess.orchestrator import (
    PostprocessConfig,
    PostprocessOrchestrator,
    cleanup_intermediates,
    validate_normalization,
    validate_inputs,
)
from umift_laptop_alignment.pipeline.normalize.runner import d435_unix_minus_monotonic_offset_ns
from v2_test_utils import write_v2_session


class PostprocessOrchestratorTest(unittest.TestCase):
    def test_d435_sensor_sequence_gap_blocks_postprocess(self) -> None:
        quality = validate_normalization(
            {
                "iphone": {"frame_count": 10, "event_count": 2},
                "d435": {
                    "frame_count": 10,
                    "sensor_integrity": {
                        "ok": False,
                        "missing_frames": 2,
                        "out_of_order_frames": 0,
                    },
                },
                "coinft": {"sample_count": 10, "calibrated_sample_count": 10},
            }
        )

        self.assertFalse(quality["ok"])
        self.assertIn("missing=2", quality["reasons"][0])

    def test_d435_uses_episode_event_anchor_after_monotonic_clock_reset(self) -> None:
        metadata = {
            "runContext": {
                "recordControl": {
                    "eventAlignedMonotonicNs": 1_300_000_000_000,
                    "eventAlignedUnixNs": 1_784_219_523_000_000_000,
                }
            }
        }
        self.assertEqual(
            d435_unix_minus_monotonic_offset_ns(metadata, fallback=123),
            1_784_218_223_000_000_000,
        )
        self.assertEqual(d435_unix_minus_monotonic_offset_ns({}, fallback=123), 123)

    def test_input_gate_requires_all_three_raw_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            for path in ("raw/iphone_stream", "raw/coinft/episode_000007", "raw/global_camera/episode_000007"):
                (run_dir / path).mkdir(parents=True)
            write_v2_session(
                run_dir,
                aligned_timestamps=[100, 200],
                events=[(0, 2, 0), (1, 3, 1)],
            )
            (run_dir / "raw/coinft/episode_000007/raw_coinft_stream.csv").touch()
            (run_dir / "raw/global_camera/episode_000007/frame_timestamps.csv").touch()
            quality = validate_inputs(run_dir, 7)
            self.assertTrue(quality["ok"])

    def test_input_gate_ignores_unclosed_unrelated_iphone_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            for path in ("raw/coinft/episode_000007", "raw/global_camera/episode_000007"):
                (run_dir / path).mkdir(parents=True)
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
            (run_dir / "raw/coinft/episode_000007/raw_coinft_stream.csv").touch()
            (run_dir / "raw/global_camera/episode_000007/frame_timestamps.csv").touch()

            quality = validate_inputs(run_dir, 7)

            self.assertTrue(quality["ok"])

    def test_cleanup_is_explicit_and_removes_only_intermediates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            (run_dir / "raw").mkdir()
            (run_dir / "normalized/episodes/episode_000007").mkdir(parents=True)
            (run_dir / "aligned/episodes/episode_000007").mkdir(parents=True)
            (run_dir / "normalized/episodes/episode_000008").mkdir(parents=True)
            (run_dir / "exports").mkdir()
            removed = cleanup_intermediates(run_dir, 7)
            self.assertEqual(
                removed,
                ["normalized/episodes/episode_000007", "aligned/episodes/episode_000007"],
            )
            self.assertTrue((run_dir / "normalized/episodes/episode_000008").exists())
            self.assertTrue((run_dir / "raw").exists())
            self.assertTrue((run_dir / "exports").exists())

    def test_cleanup_defaults_to_disabled(self) -> None:
        self.assertFalse(PostprocessConfig().cleanup_intermediates)

    def test_system_exit_is_persisted_as_postprocess_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            for path in (
                "raw/iphone_stream/session",
                "raw/coinft/episode_000007",
                "raw/global_camera/episode_000007",
            ):
                (run_dir / path).mkdir(parents=True)
            write_v2_session(
                run_dir,
                aligned_timestamps=[100, 200],
                events=[(0, 2, 0), (1, 3, 1)],
            )
            (run_dir / "raw/coinft/episode_000007/raw_coinft_stream.csv").touch()
            (run_dir / "raw/global_camera/episode_000007/frame_timestamps.csv").touch()
            states: list[dict] = []
            orchestrator = PostprocessOrchestrator(
                export_controller=object(),
                state_callback=lambda state: states.append(dict(state)),
            )
            with (
                patch(
                    "umift_laptop_alignment.pipeline.postprocess.orchestrator.normalize_episode",
                    return_value={
                        "iphone": {"frame_count": 10, "event_count": 2},
                        "d435": {"frame_count": 10},
                        "coinft": {"sample_count": 10, "calibrated_sample_count": 10},
                    },
                ),
                patch(
                    "umift_laptop_alignment.pipeline.postprocess.orchestrator.align_episode",
                    side_effect=SystemExit("unmatched recording events"),
                ),
            ):
                result = orchestrator.run(run_dir, episode_index=7)

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"], "unmatched recording events")
            self.assertEqual(states[-1]["status"], "error")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from umift_laptop_alignment.pipeline.alignment.runner import (
    TimedRows,
    compact_coinft_interpolated,
    compact_d435,
    episode_target_rows,
)
from umift_laptop_alignment.pipeline.normalize.runner import (
    D435_RGB_DEPTH_MAX_DELTA_MS,
    fit_affine_clock_model,
    normalize_coinft,
)
from umift_laptop_alignment.capture.receivers.coinft.calibration import CoinFTCalibrationConfig
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.selection import coinft_samples_for_episode
from umift_laptop_alignment.pipeline.transforms.iphone_v2 import phone_clock_model


class TimestampAlignmentTest(unittest.TestCase):
    def test_d435_rgb_depth_tolerance_accepts_observed_hardware_skew(self) -> None:
        # Both stable hardware phase offsets observed on the delivery D435
        # (5.096 ms and 8.315 ms) must remain valid synchronized framesets.
        self.assertLessEqual(8.315, D435_RGB_DEPTH_MAX_DELTA_MS)
        self.assertLess(D435_RGB_DEPTH_MAX_DELTA_MS, 32.0)

    def test_iphone_clock_model_uses_low_rtt_bidirectional_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            scale = 0.999950
            offset_ns = 2_000_000_000_000
            rows = []
            for index in range(100):
                phone_ns = 5_000_000_000 + index * 2_000_000_000
                mac_ns = round(scale * phone_ns + offset_ns)
                rtt_ns = 200_000 + (index % 5) * 500_000
                rows.append(
                    {
                        "phone_midpoint_ns": phone_ns,
                        "mac_midpoint_ns": mac_ns,
                        "round_trip_ns": rtt_ns,
                    }
                )
            (session_dir / "clock_sync.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )

            model = phone_clock_model(session_dir)

        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model["method"], "bidirectional_affine_low_rtt")
        self.assertAlmostEqual(model["scale"], scale, delta=1e-8)
        self.assertAlmostEqual(model["offset_ns"], offset_ns, delta=1000)

    def test_affine_clock_model_recovers_rate_and_low_delay_offset(self) -> None:
        scale = 1.000075
        offset_ns = 4_000_000_000_000
        jitter_ns = [0, 180_000, 420_000, 70_000, 900_000, 250_000, 110_000, 600_000]
        pairs = []
        for index in range(400):
            device_ns = index * 5_000_000
            host_ns = round(scale * device_ns + offset_ns + jitter_ns[index % len(jitter_ns)])
            pairs.append((device_ns, host_ns))

        model = fit_affine_clock_model(pairs)

        self.assertIsNotNone(model)
        assert model is not None
        self.assertAlmostEqual(model["scale"], scale, delta=5e-6)
        self.assertAlmostEqual(model["offset_ns"], offset_ns, delta=100_000)
        self.assertGreaterEqual(model["residual_p95_ms"], 0.0)

    def test_normalize_coinft_uses_teensy_clock_and_preserves_receive_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            source = run_dir / "raw" / "coinft" / "episode_000000" / "raw_coinft_stream.csv"
            source.parent.mkdir(parents=True)
            fieldnames = [
                "packet_index",
                "host_receive_monotonic_ns",
                "host_receive_unix_ns",
                "sequence_id",
                "teensy_time_us",
            ]
            offset_ns = 8_000_000_000
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for index in range(20):
                    device_us = 100_000 + index * 5_000
                    ideal_ns = offset_ns + device_us * 1000
                    writer.writerow(
                        {
                            "packet_index": index,
                            "host_receive_monotonic_ns": ideal_ns + (index % 4) * 100_000,
                            "host_receive_unix_ns": ideal_ns + 1_000_000_000_000,
                            "sequence_id": index,
                            "teensy_time_us": device_us,
                        }
                    )
            output = run_dir / "normalized" / "coinft_samples.jsonl"

            report = normalize_coinft(run_dir, output, coinft_config=CoinFTCalibrationConfig())
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(report["clock_alignment"]["models"][0]["valid"])
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[7]["timestamp"]["alignment_method"], "teensy_affine_lower_envelope")
        self.assertEqual(rows[7]["timestamp"]["mac_receive_monotonic_ns"], offset_ns + 135_000_000 + 300_000)
        self.assertAlmostEqual(rows[7]["aligned_monotonic_ns"], offset_ns + 135_000_000, delta=100_000)

    def test_coinft_midpoint_interpolation_and_wide_gap_rejection(self) -> None:
        before = {
            "aligned_monotonic_ns": 10_000_000,
            "packet_index": 1,
            "sequence_id": 1,
            "left_wrench": [0.0] * 6,
            "right_wrench": [10.0] * 6,
        }
        after = {
            "aligned_monotonic_ns": 20_000_000,
            "packet_index": 2,
            "sequence_id": 2,
            "left_wrench": [10.0] * 6,
            "right_wrench": [20.0] * 6,
        }

        midpoint = compact_coinft_interpolated(before, after, 15_000_000, 15.0)
        rejected = compact_coinft_interpolated(before, after, 15_000_000, 5.0)

        self.assertTrue(midpoint["valid"])
        self.assertEqual(midpoint["left_wrench"], [5.0] * 6)
        self.assertEqual(midpoint["right_wrench"], [15.0] * 6)
        self.assertEqual(midpoint["interpolation_alpha"], 0.5)
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["dropped_reason"], "bracketing_gap_outside_threshold")

    def test_d435_rgb_depth_timestamp_mismatch_is_invalid(self) -> None:
        row = {
            "frame_index": 4,
            "quality": {"rgb_depth_pair_valid": False, "rgb_depth_delta_ms": 32.0},
        }

        compact = compact_d435(row, 0, 25.0)

        self.assertFalse(compact["valid"])
        self.assertEqual(compact["dropped_reason"], "rgb_depth_timestamp_mismatch")

    def test_iphone_event_window_keeps_frames_regardless_of_recording_flag(self) -> None:
        rows = [
            {"aligned_monotonic_ns": 90, "sequence": 1, "recording": {"active": False}},
            {"aligned_monotonic_ns": 200, "sequence": 2, "recording": {"active": True}},
            {"aligned_monotonic_ns": 310, "sequence": 3, "recording": {"active": False}},
        ]
        iphone = TimedRows(rows=rows, timestamps=[90, 200, 310])
        episode = {"start_aligned_monotonic_ns": 100, "end_aligned_monotonic_ns": 300}

        selected = episode_target_rows(episode, iphone, TimedRows([], []))

        self.assertEqual([row["sequence"] for row in selected], [1, 2, 3])

    def test_export_prefers_one_interpolated_wrench_per_iphone_timeline_row(self) -> None:
        episode_rows = [
            {
                "aligned_monotonic_ns": index * 33_333_333,
                "coinft": {
                    "valid": True,
                    "left_wrench": [float(index)] * 6,
                    "right_wrench": [float(index + 1)] * 6,
                    "wrench": [[float(index)] * 6, [float(index + 1)] * 6],
                },
            }
            for index in range(5)
        ]

        selected = coinft_samples_for_episode(
            [], episode_rows=episode_rows, start_ns=0, end_ns=200_000_000
        )

        self.assertEqual(len(selected), len(episode_rows))


if __name__ == "__main__":
    unittest.main()

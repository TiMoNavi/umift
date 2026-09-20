from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from umift_laptop_alignment.pipeline.alignment.runner import compact_coinft
from umift_laptop_alignment.pipeline.export.forceflow.legacy_exporters import ForceFlowV1ZarrExporter, calibrated_dual_wrench, calibrated_left_wrench, coerce_force_mode
from umift_laptop_alignment.pipeline.normalize.runner import normalize_coinft
from umift_laptop_alignment.capture.receivers.coinft.calibration import (
    CoinFTCalibrationConfig,
    CoinFTCalibrationRuntime,
    CoinFTSlotCalibrationConfig,
    load_coinft_calibration_config,
)


class FakeCalibrator:
    def __init__(self, slot: CoinFTSlotCalibrationConfig, *, moving_average_window: int):
        self.slot = slot
        self.offset = None
        self.ft_bias = None
        self.model_path = None
        self.norm_path = None

    def set_offset(self, offset: np.ndarray) -> None:
        self.offset = np.asarray(offset, dtype=np.float64)

    def set_ft_bias(self, ft_bias: np.ndarray) -> None:
        self.ft_bias = np.asarray(ft_bias, dtype=np.float64)

    def reset_filter(self) -> None:
        return None

    def zero_raw(self, raw: np.ndarray) -> np.ndarray:
        raw = np.asarray(raw, dtype=np.float64)
        return raw if self.offset is None else raw - self.offset

    def predict_uncorrected(self, raw: np.ndarray) -> np.ndarray:
        residual_n = float(np.mean(self.zero_raw(raw))) * 0.01
        return np.array([0.4 + residual_n, -0.2 + residual_n, 0.3 + residual_n, 0.01, -0.02, 0.03])

    def predict(self, raw: np.ndarray) -> np.ndarray:
        wrench = self.predict_uncorrected(raw)
        return wrench if self.ft_bias is None else wrench - self.ft_bias


def startup_test_config() -> CoinFTCalibrationConfig:
    slots = {
        side: CoinFTSlotCalibrationConfig(
            side=side,
            sensor_index=index,
            slot_id=side,
            hardware_label=side,
        )
        for index, side in enumerate(("left", "right"))
    }
    return CoinFTCalibrationConfig(
        mode="calibrated",
        slots=slots,
        moving_average_window=1,
        raw_stability_window_s=0.2,
        raw_stability_min_wait_s=0.5,
        raw_stability_check_interval_s=0.1,
        raw_stability_mean_delta_counts=8.0,
        raw_stability_noise_std_counts=20.0,
        raw_stability_required_passes=2,
        runtime_settling_s=0.3,
        final_tare_window_s=0.2,
        bias_window_s=0.2,
        zero_check_window_s=0.2,
        zero_force_limit_n=0.15,
    )


class CoinFTCalibrationChainTest(unittest.TestCase):
    def test_default_config_loader_is_raw_until_gui_or_cli_selects_models(self) -> None:
        config = load_coinft_calibration_config(None)

        self.assertEqual(config.mode, "raw")
        self.assertFalse(config.calibrated)
        self.assertIsNone(config.model_set_id)
        self.assertIsNone(config.slot("left"))
        self.assertIsNone(config.slot("right"))

    @patch("umift_laptop_alignment.capture.receivers.coinft.calibration.CoinFTCalibrator", FakeCalibrator)
    def test_startup_calibration_uses_two_raw_tares_and_fixed_ft_bias(self) -> None:
        runtime = CoinFTCalibrationRuntime(startup_test_config())
        timestamp_s = 0.0
        result = {}
        for _ in range(80):
            raw_level = 1020.0 if runtime.status in {
                "runtime_settling",
                "final_tare",
                "bias_calibration",
                "zero_check",
                "ready",
            } else 1000.0
            raw = np.full(12, raw_level)
            result = runtime.process(raw, raw, timestamp_s=timestamp_s)
            timestamp_s += 0.05
            if runtime.ready:
                break

        self.assertEqual(runtime.status, "ready")
        self.assertTrue(result["calibration_ready"])
        np.testing.assert_allclose(runtime.provisional_offsets["left"], np.full(12, 1000.0))
        np.testing.assert_allclose(runtime.final_offsets["left"], np.full(12, 1020.0))
        np.testing.assert_allclose(runtime.ft_biases["left"][:3], [0.4, -0.2, 0.3], atol=1e-9)
        self.assertLessEqual(result["zero_force_max_abs_n"], 0.15)
        np.testing.assert_allclose(result["left_wrench"][:3], [0.0, 0.0, 0.0], atol=1e-9)

    @patch("umift_laptop_alignment.capture.receivers.coinft.calibration.CoinFTCalibrator", FakeCalibrator)
    def test_zero_check_over_point_one_five_newtons_blocks_ready(self) -> None:
        runtime = CoinFTCalibrationRuntime(startup_test_config())
        timestamp_s = 0.0
        for _ in range(100):
            raw_level = 1000.0
            if runtime.status in {"runtime_settling", "final_tare", "bias_calibration"}:
                raw_level = 1020.0
            elif runtime.status in {"zero_check", "zero_check_failed"}:
                raw_level = 1040.0
            raw = np.full(12, raw_level)
            result = runtime.process(raw, raw, timestamp_s=timestamp_s)
            timestamp_s += 0.05
            if runtime.status == "zero_check_failed":
                break

        self.assertEqual(runtime.status, "zero_check_failed")
        self.assertFalse(result["calibration_ready"])
        self.assertGreater(result["zero_force_max_abs_n"], 0.15)
        self.assertIn("restart CoinFT", result["calibration_error"])

    @patch("umift_laptop_alignment.capture.receivers.coinft.calibration.CoinFTCalibrator", FakeCalibrator)
    def test_raw_drift_stays_delayed_without_taring(self) -> None:
        runtime = CoinFTCalibrationRuntime(startup_test_config())
        timestamp_s = 0.0
        for _ in range(30):
            raw = np.full(12, 1000.0 + timestamp_s * 100.0)
            runtime.process(raw, raw, timestamp_s=timestamp_s)
            timestamp_s += 0.05

        self.assertEqual(runtime.status, "delayed")
        self.assertFalse(runtime.ready)
        self.assertEqual(runtime.provisional_offsets, {})

    def test_normalize_preserves_online_calibrated_wrench_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            csv_dir = run_dir / "raw" / "coinft" / "episode_000000"
            csv_dir.mkdir(parents=True)
            csv_path = csv_dir / "raw_coinft_stream.csv"
            fieldnames = [
                "packet_index",
                "host_receive_monotonic_ns",
                "host_receive_unix_ns",
                "sequence_id",
                "teensy_time_us",
                *[f"left_c{i}" for i in range(1, 13)],
                *[f"right_c{i}" for i in range(1, 13)],
                "calibration_status",
                "calibration_error",
                "tare_count",
                "tare_target",
                *[f"left_zeroed_c{i}" for i in range(1, 13)],
                *[f"left_{axis}" for axis in ("fx", "fy", "fz", "mx", "my", "mz")],
                *[f"right_zeroed_c{i}" for i in range(1, 13)],
                *[f"right_{axis}" for axis in ("fx", "fy", "fz", "mx", "my", "mz")],
            ]
            row = {
                "packet_index": 0,
                "host_receive_monotonic_ns": 1000,
                "host_receive_unix_ns": 2000,
                "sequence_id": 7,
                "teensy_time_us": 123,
                "calibration_status": "ok",
                "calibration_error": "",
                "tare_count": 3,
                "tare_target": 3,
            }
            row.update({f"left_c{i}": 100 + i for i in range(1, 13)})
            row.update({f"right_c{i}": 200 + i for i in range(1, 13)})
            row.update({f"left_zeroed_c{i}": i * 0.1 for i in range(1, 13)})
            row.update({f"right_zeroed_c{i}": i * 0.2 for i in range(1, 13)})
            row.update({f"left_{axis}": i for i, axis in enumerate(("fx", "fy", "fz", "mx", "my", "mz"), start=1)})
            row.update({f"right_{axis}": i * 10 for i, axis in enumerate(("fx", "fy", "fz", "mx", "my", "mz"), start=1)})
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(row)

            output_path = run_dir / "normalized" / "coinft_samples.jsonl"
            report = normalize_coinft(run_dir, output_path)
            normalized = output_path.read_text(encoding="utf-8")

        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["calibrated_sample_count"], 1)
        self.assertIn('"left_wrench":[1.0,2.0,3.0,4.0,5.0,6.0]', normalized)
        self.assertIn('"wrench":[[1.0,2.0,3.0,4.0,5.0,6.0],[10.0,20.0,30.0,40.0,50.0,60.0]]', normalized)
        self.assertIn('"has_calibrated_wrench":true', normalized)
        self.assertIn('"has_dual_calibrated_wrench":true', normalized)

    def test_align_compact_coinft_preserves_calibrated_fields(self) -> None:
        row = {
            "packet_index": 1,
            "sequence_id": 2,
            "left_raw": [1] * 12,
            "right_raw": [2] * 12,
            "left_raw_zeroed": [0.1] * 12,
            "right_raw_zeroed": [0.2] * 12,
            "left_wrench": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "right_wrench": [6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
            "wrench": [[1.0, 2.0, 3.0, 4.0, 5.0, 6.0], [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]],
            "side_order": ["left", "right"],
            "wrench_axes": ["fx", "fy", "fz", "mx", "my", "mz"],
            "calibration": {"status": "ok"},
            "quality": {"has_calibrated_wrench": True, "has_dual_calibrated_wrench": True},
            "source_file": "normalized/coinft_samples.jsonl",
        }

        compact = compact_coinft(row, 0, 25.0)

        self.assertTrue(compact["valid"])
        self.assertEqual(compact["left_wrench"], row["left_wrench"])
        self.assertEqual(compact["wrench"], row["wrench"])
        self.assertEqual(compact["side_order"], ["left", "right"])
        self.assertEqual(compact["calibration"], {"status": "ok"})
        self.assertEqual(compact["quality"], {"has_calibrated_wrench": True, "has_dual_calibrated_wrench": True})

    def test_export_helpers_accept_calibrated_left_alias_and_wrench(self) -> None:
        self.assertEqual(coerce_force_mode("calibrated"), "calibrated-dual")
        row = {"coinft": {"left_wrench": [1, 2, 3, 4, 5, 6], "right_wrench": [6, 5, 4, 3, 2, 1]}}

        self.assertEqual(calibrated_left_wrench(row), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(calibrated_dual_wrench(row), [[1.0, 2.0, 3.0, 4.0, 5.0, 6.0], [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]])

    def test_forceflow_v1_dual_force_mapping_manifest(self) -> None:
        exporter = ForceFlowV1ZarrExporter(force_mode="calibrated-dual")
        mapping = exporter.force_mapping_manifest()

        self.assertEqual(mapping["mode"], "coinft_dual_wrench")
        self.assertEqual(mapping["shape"], ["N", 2, 6])
        self.assertEqual(mapping["side_order"], ["left", "right"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import math
import unittest

from umift_laptop_alignment.pipeline.transforms.live_zarr import LiveForceFlowZarrState


def frame(*, timestamp_ns: int, x_m: float, gripper_open: float, sequence: int = 1) -> dict:
    return {
        "sequence": sequence,
        "timestamp": {
            "aligned_monotonic_ns": timestamp_ns,
            "aligned_unix_ns": timestamp_ns + 1_000_000,
        },
        "pose": {
            "valid": True,
            "frame": "user_world",
            "world_calibrated": True,
            "world_origin_status": "calibrated",
            "x_m": x_m,
            "y_m": 0.2,
            "z_m": 0.3,
            "roll_deg": 90.0,
            "pitch_deg": 0.0,
            "yaw_deg": -90.0,
        },
        "gripper": {
            "valid": True,
            "open_percent": gripper_open,
        },
    }


class LiveZarrConversionTest(unittest.TestCase):
    def test_waits_for_gate_before_converting(self) -> None:
        state = LiveForceFlowZarrState()

        snapshot = state.observe_iphone_frame(
            frame(timestamp_ns=1_000, x_m=0.1, gripper_open=1.0),
            recording_gate={"ok": False, "reasons": ["D435 missing"]},
            recording_control={"active": False},
        )

        self.assertEqual(snapshot["status"], "waiting_for_streams")
        self.assertEqual(snapshot["converted_rows"], 0)
        self.assertEqual(snapshot["last_error"], "D435 missing")

    def test_converts_pose_gripper_and_next_frame_step(self) -> None:
        state = LiveForceFlowZarrState()
        gate = {"ok": True, "reasons": []}
        control = {"active": True, "episode_index": 7}

        first = state.observe_iphone_frame(
            frame(timestamp_ns=1_000_000_000, x_m=0.1, gripper_open=0.25, sequence=1),
            recording_gate=gate,
            recording_control=control,
        )
        second = state.observe_iphone_frame(
            frame(timestamp_ns=1_033_333_333, x_m=0.15, gripper_open=0.75, sequence=2),
            recording_gate=gate,
            recording_control=control,
        )

        self.assertEqual(first["latest_row"]["pos"][0], 100.0)
        self.assertAlmostEqual(first["latest_row"]["pos"][3], math.pi / 2)
        self.assertEqual(first["latest_row"]["gripper_state"], [0.0])

        self.assertEqual(second["latest_row"]["pos"][0], 150.0)
        self.assertEqual(second["latest_row"]["gripper_state"], [1.0])
        self.assertEqual(second["recording_rows_observed"], 2)
        self.assertEqual(second["recording_steps_observed"], 1)
        self.assertEqual(second["latest_completed_step"]["action"][0], 50.0)
        self.assertEqual(second["latest_completed_step"]["gripper_action"], [1.0])
        self.assertEqual(second["latest_completed_step"]["gripper_action_semantics"], "next_output_frame_binary_state")

    def test_does_not_complete_step_across_episode_change(self) -> None:
        state = LiveForceFlowZarrState()
        gate = {"ok": True, "reasons": []}

        state.observe_iphone_frame(
            frame(timestamp_ns=1_000, x_m=0.1, gripper_open=1.0),
            recording_gate=gate,
            recording_control={"active": True, "episode_index": 1},
        )
        snapshot = state.observe_iphone_frame(
            frame(timestamp_ns=2_000, x_m=0.2, gripper_open=0.0),
            recording_gate=gate,
            recording_control={"active": True, "episode_index": 2},
        )

        self.assertIsNone(snapshot["latest_completed_step"])
        self.assertEqual(snapshot["completed_steps"], 0)


if __name__ == "__main__":
    unittest.main()

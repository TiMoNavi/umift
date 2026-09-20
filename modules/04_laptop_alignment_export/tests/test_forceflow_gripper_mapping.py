from __future__ import annotations

import unittest

import numpy as np

from umift_laptop_alignment.pipeline.export.forceflow.gripper_mapping import (
    next_gripper_action,
    raw_open_to_binary,
    raw_open_to_unit,
)


class ForceFlowGripperMappingTest(unittest.TestCase):
    def test_unit_values_use_half_open_threshold(self) -> None:
        values = [0.0, 0.49, 0.5, 1.0]
        mapped = [raw_open_to_binary(value, input_scale="unit_0_1", threshold=0.5) for value in values]
        self.assertEqual(mapped, [0.0, 0.0, 1.0, 1.0])

    def test_percent_values_are_normalized_before_threshold(self) -> None:
        values = [0.0, 49.0, 50.0, 100.0]
        mapped = [raw_open_to_binary(value, input_scale="percent_0_100", threshold=0.5) for value in values]
        self.assertEqual(mapped, [0.0, 0.0, 1.0, 1.0])

    def test_auto_scale_accepts_current_unit_and_legacy_percent_values(self) -> None:
        self.assertEqual(raw_open_to_unit(0.75, input_scale="auto"), 0.75)
        self.assertEqual(raw_open_to_unit(75.0, input_scale="auto"), 0.75)

    def test_next_gripper_action_uses_next_state_inside_episode_only(self) -> None:
        state = np.asarray([[0.0], [1.0], [0.0], [0.0], [1.0]], dtype=np.float32)
        episode = np.asarray([0, 0, 0, 1, 1], dtype=np.uint16)

        action = next_gripper_action(state, episode)

        np.testing.assert_array_equal(
            action,
            np.asarray([[1.0], [0.0], [0.0], [1.0], [1.0]], dtype=np.float32),
        )


if __name__ == "__main__":
    unittest.main()

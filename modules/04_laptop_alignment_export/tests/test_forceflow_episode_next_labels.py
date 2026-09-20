from __future__ import annotations

import unittest

import numpy as np

from umift_laptop_alignment.pipeline.export.forceflow.action_mapping import single_step_pose_delta
from umift_laptop_alignment.pipeline.export.forceflow.episode_mapping import episode_ends_from_ids, episode_ids_from_rows


class ForceFlowEpisodeNextLabelsTest(unittest.TestCase):
    def test_single_step_pose_delta_does_not_cross_episode_boundary(self) -> None:
        pos = np.zeros((5, 6), dtype=np.float32)
        pos[:, 0] = np.asarray([0.0, 1.0, 3.0, 10.0, 12.0], dtype=np.float32)
        episode = np.asarray([0, 0, 0, 1, 1], dtype=np.uint16)

        action = single_step_pose_delta(pos, episode)

        np.testing.assert_array_equal(action[:, 0], np.asarray([1.0, 2.0, 0.0, 2.0, 0.0], dtype=np.float32))
        np.testing.assert_array_equal(action[:, 1:], np.zeros((5, 5), dtype=np.float32))

    def test_episode_ends_are_exclusive_cumulative_boundaries(self) -> None:
        episode = np.asarray([0, 0, 0, 1, 1, 4], dtype=np.uint16)

        ends = episode_ends_from_ids(episode)

        np.testing.assert_array_equal(ends, np.asarray([3, 5, 6], dtype=np.uint32))

    def test_episode_ids_from_rows_clamps_missing_or_invalid_to_zero(self) -> None:
        rows = [{"episode_index": 2}, {"episode_index": -1}, {}, {"episode_index": 70000}]

        episode = episode_ids_from_rows(rows)

        np.testing.assert_array_equal(episode, np.asarray([2, 0, 0, 0], dtype=np.uint16))


if __name__ == "__main__":
    unittest.main()

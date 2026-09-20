"""Pose action mapping for ForceFlow exports."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt


def single_step_pose_delta(
    pos: npt.NDArray[np.float32],
    episode: npt.NDArray[np.integer[Any]],
) -> npt.NDArray[np.float32]:
    """Compute pos[t+1] - pos[t] inside each episode.

    Episode tails are padded with zero delta because there is no legal next
    pose target at the terminal row.
    """

    pose = np.asarray(pos, dtype=np.float32)
    episode_ids = np.asarray(episode)
    if pose.ndim != 2 or pose.shape[1] != 6:
        raise ValueError(f"pos must have shape (N, 6), got {pose.shape}")
    if episode_ids.ndim != 1 or episode_ids.shape[0] != pose.shape[0]:
        raise ValueError("episode must have shape (N,) matching pos")

    action = np.zeros_like(pose, dtype=np.float32)
    if pose.shape[0] < 2:
        return action

    same_episode_next = episode_ids[:-1] == episode_ids[1:]
    action[:-1] = np.where(same_episode_next[:, None], pose[1:] - pose[:-1], 0.0)
    return action

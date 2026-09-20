"""Gripper state/action mapping for ForceFlow exports."""

from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

GripperInputScale = Literal["auto", "unit_0_1", "percent_0_100"]


def raw_open_to_unit(value: Any, *, input_scale: GripperInputScale = "auto") -> float:
    """Convert a captured gripper opening value to a 0..1 open fraction."""

    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return 0.0

    raw = float(value)
    if input_scale == "percent_0_100" or (input_scale == "auto" and raw > 1.0):
        raw = raw / 100.0
    elif input_scale not in ("auto", "unit_0_1"):
        raise ValueError(f"Unsupported gripper input scale: {input_scale}")

    return max(0.0, min(1.0, raw))


def unit_open_to_binary(open_unit: float, *, threshold: float = 0.5) -> float:
    """Map continuous opening to ForceFlow's binary convention: 0 closed, 1 open."""

    if not math.isfinite(open_unit):
        return 0.0
    return 1.0 if open_unit >= threshold else 0.0


def raw_open_to_binary(value: Any, *, input_scale: GripperInputScale = "auto", threshold: float = 0.5) -> float:
    return unit_open_to_binary(raw_open_to_unit(value, input_scale=input_scale), threshold=threshold)


def next_gripper_action(
    binary_state: npt.NDArray[np.float32],
    episode: npt.NDArray[np.integer[Any]],
) -> npt.NDArray[np.float32]:
    """Use the next absolute binary gripper state, without crossing episode boundaries."""

    state = np.asarray(binary_state, dtype=np.float32)
    episode_ids = np.asarray(episode)
    if state.ndim != 2 or state.shape[1] != 1:
        raise ValueError(f"binary_state must have shape (N, 1), got {state.shape}")
    if episode_ids.ndim != 1 or episode_ids.shape[0] != state.shape[0]:
        raise ValueError("episode must have shape (N,) matching binary_state")

    output = np.zeros_like(state, dtype=np.float32)
    if state.shape[0] == 0:
        return output

    output[:, 0] = state[:, 0]
    same_episode_next = episode_ids[:-1] == episode_ids[1:]
    output[:-1, 0] = np.where(same_episode_next, state[1:, 0], state[:-1, 0])
    return output

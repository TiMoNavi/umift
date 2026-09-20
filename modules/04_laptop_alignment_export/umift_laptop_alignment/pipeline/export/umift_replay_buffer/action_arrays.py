"""Action placeholder arrays for UMI-FT replay-buffer export."""

from __future__ import annotations

from typing import Any


def build_action_arrays(pose_fb: Any, *, stiffness_constant: float, np: Any) -> dict[str, Any]:
    t_len = int(pose_fb.shape[0])
    return {
        "ts_pose_command_0": pose_fb.copy(),
        "ts_pose_virtual_target_0": pose_fb.copy(),
        "stiffness_0": np.full((t_len,), stiffness_constant, dtype=np.float64),
    }

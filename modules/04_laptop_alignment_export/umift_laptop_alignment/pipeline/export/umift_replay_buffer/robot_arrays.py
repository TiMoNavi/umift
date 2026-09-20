"""Build pose and gripper arrays for one UMI-FT episode."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.gripper import (
    export_gripper_value,
    gripper_open_fraction,
    gripper_width_m,
)
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.pose import export_pose7_from_iphone_pose


def build_robot_arrays(
    *,
    episode_rows: list[dict[str, Any]],
    time_stamps: Any,
    pose_config: dict[str, Any],
    gripper_config: dict[str, Any],
    np: Any,
) -> tuple[dict[str, Any], Any]:
    t_len = len(episode_rows)
    pose_fb = np.zeros((t_len, 7), dtype=np.float64)
    gripper = np.zeros((t_len, 1), dtype=np.float64)
    gripper_widths_m = np.zeros((t_len,), dtype=np.float64)

    for index, row in enumerate(episode_rows):
        iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
        pose = iphone.get("pose", {}) if isinstance(iphone.get("pose"), dict) else {}
        pose_fb[index] = np.asarray(
            export_pose7_from_iphone_pose(pose, pose_config, np),
            dtype=np.float64,
        )

        open_fraction = gripper_open_fraction(iphone.get("gripper"))
        width_m = gripper_width_m(iphone.get("gripper"), open_fraction, gripper_config)
        gripper_widths_m[index] = width_m
        gripper[index, 0] = export_gripper_value(open_fraction, width_m, gripper_config)

    return (
        {
            "ts_pose_fb_0": pose_fb,
            "robot_time_stamps_0": time_stamps.copy(),
            "gripper_0": gripper,
            "gripper_time_stamps_0": time_stamps.copy(),
        },
        gripper_widths_m,
    )

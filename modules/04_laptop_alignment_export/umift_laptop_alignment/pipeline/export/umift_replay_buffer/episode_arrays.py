"""Build per-episode raw arrays for UMI-FT replay-buffer Zarr."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.action_arrays import build_action_arrays
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.camera_arrays import build_camera_arrays
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.robot_arrays import build_robot_arrays
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.timeline_arrays import build_time_stamps
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.wrench_arrays import build_wrench_arrays


def build_episode_arrays(
    run_dir: Path,
    *,
    episode_rows: list[dict[str, Any]],
    coinft_rows: list[dict[str, Any]],
    start_ns: int,
    image_size: tuple[int, int],
    global_size: tuple[int, int],
    write_global_rgb: bool,
    write_depth: bool,
    global_rgb_field: str,
    d435_depth_scale: float,
    depth_clip_m: float,
    stiffness_constant: float,
    pose_config: dict[str, Any],
    gripper_config: dict[str, Any],
    force_config: dict[str, Any],
    cv2: Any,
    np: Any,
) -> tuple[dict[str, Any], dict[str, int]]:
    time_stamps = build_time_stamps(episode_rows=episode_rows, start_ns=start_ns, np=np)
    camera_arrays, counts = build_camera_arrays(
        run_dir,
        episode_rows=episode_rows,
        start_ns=start_ns,
        time_stamps=time_stamps,
        image_size=image_size,
        global_size=global_size,
        write_global_rgb=write_global_rgb,
        write_depth=write_depth,
        global_rgb_field=global_rgb_field,
        d435_depth_scale=d435_depth_scale,
        depth_clip_m=depth_clip_m,
        cv2=cv2,
        np=np,
    )
    robot_arrays, gripper_widths_m = build_robot_arrays(
        episode_rows=episode_rows,
        time_stamps=time_stamps,
        pose_config=pose_config,
        gripper_config=gripper_config,
        np=np,
    )
    wrench_arrays = build_wrench_arrays(
        coinft_rows=coinft_rows,
        start_ns=start_ns,
        time_stamps=time_stamps,
        gripper_widths_m=gripper_widths_m,
        force_config=force_config,
        np=np,
    )
    action_arrays = build_action_arrays(
        robot_arrays["ts_pose_fb_0"],
        stiffness_constant=stiffness_constant,
        np=np,
    )

    arrays = {}
    arrays.update(camera_arrays)
    arrays.update(robot_arrays)
    arrays.update(wrench_arrays)
    arrays.update(action_arrays)
    return arrays, counts

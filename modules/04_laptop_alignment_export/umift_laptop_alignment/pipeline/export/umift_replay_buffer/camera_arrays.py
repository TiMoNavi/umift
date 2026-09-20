"""Build RGB and depth arrays for one UMI-FT episode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.camera_timing import d435_seconds_from_start
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.depth import load_d435_depth, load_iphone_depth
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.rgb import (
    load_d435_rgb,
    load_iphone_rgb,
    resize_bgr_to_rgb,
)


def build_camera_arrays(
    run_dir: Path,
    *,
    episode_rows: list[dict[str, Any]],
    start_ns: int,
    time_stamps: Any,
    image_size: tuple[int, int],
    global_size: tuple[int, int],
    write_global_rgb: bool,
    write_depth: bool,
    global_rgb_field: str,
    d435_depth_scale: float,
    depth_clip_m: float,
    cv2: Any,
    np: Any,
) -> tuple[dict[str, Any], dict[str, int]]:
    t_len = len(episode_rows)
    rgb_0 = np.zeros((t_len, image_size[1], image_size[0], 3), dtype=np.uint8)
    global_rgb = np.zeros((t_len, global_size[1], global_size[0], 3), dtype=np.uint8) if write_global_rgb else None
    global_rgb_times = np.zeros((t_len, 1), dtype=np.float64) if write_global_rgb else None
    depth_0 = np.zeros((t_len, global_size[1], global_size[0], 3), dtype=np.float16) if write_depth else None
    iphone_depth_0 = np.zeros((t_len, image_size[1], image_size[0], 3), dtype=np.float16)
    depth_times = np.zeros((t_len, 1), dtype=np.float64) if write_depth else None
    map_to_d_idx = np.arange(t_len, dtype=np.int64).reshape(-1, 1) if write_depth else None

    video_captures: dict[Path, Any] = {}
    missing_iphone_images = 0
    missing_global_rgb = 0
    missing_depth = 0
    missing_iphone_depth = 0
    try:
        for index, row in enumerate(episode_rows):
            iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
            rgb = iphone.get("rgb", {}) if isinstance(iphone.get("rgb"), dict) else {}
            image = load_iphone_rgb(run_dir, rgb, cv2, video_captures)
            if image is None:
                missing_iphone_images += 1
            else:
                rgb_0[index] = resize_bgr_to_rgb(image, cv2, np, image_size)

            d435 = row.get("d435", {}) if isinstance(row.get("d435"), dict) else {}
            d435_time_s = d435_seconds_from_start(row, start_ns)
            if write_global_rgb and global_rgb is not None and global_rgb_times is not None:
                global_rgb_times[index, 0] = d435_time_s
                image = load_d435_rgb(run_dir, d435, cv2, video_captures)
                if image is None:
                    missing_global_rgb += 1
                else:
                    global_rgb[index] = resize_bgr_to_rgb(image, cv2, np, global_size)
            if write_depth and depth_0 is not None and depth_times is not None:
                depth_times[index, 0] = d435_time_s
                depth = load_d435_depth(run_dir, d435, cv2, np, global_size, d435_depth_scale, depth_clip_m)
                if depth is None:
                    missing_depth += 1
                else:
                    depth_0[index] = depth
            iphone_depth = load_iphone_depth(run_dir, iphone.get("depth", {}), np, cv2, image_size)
            if iphone_depth is None:
                missing_iphone_depth += 1
            else:
                iphone_depth_0[index] = iphone_depth
    finally:
        for capture in video_captures.values():
            capture.release()

    arrays = {
        "rgb_0": rgb_0,
        "rgb_time_stamps_0": time_stamps.copy(),
        "iphone_depth_0": iphone_depth_0,
        "iphone_depth_time_stamps_0": time_stamps.copy(),
    }
    if write_global_rgb and global_rgb is not None and global_rgb_times is not None:
        arrays[global_rgb_field] = global_rgb
        arrays["rgb_global_time_stamps_0"] = global_rgb_times
    if write_depth and depth_0 is not None and depth_times is not None and map_to_d_idx is not None:
        arrays["depth_0"] = depth_0
        arrays["depth_time_stamps_0"] = depth_times
        arrays["map_to_d_idx_0"] = map_to_d_idx

    counts = {
        "missing_iphone_images": missing_iphone_images,
        "missing_global_rgb_frames": missing_global_rgb,
        "missing_depth_frames": missing_depth,
        "missing_iphone_depth_frames": missing_iphone_depth,
    }
    return arrays, counts

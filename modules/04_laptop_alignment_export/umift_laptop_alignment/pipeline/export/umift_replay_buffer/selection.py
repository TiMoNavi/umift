"""Select aligned timeline and CoinFT rows for UMI-FT episodes."""

from __future__ import annotations

from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.wrench import six_float_wrench


def row_is_exportable(row: dict[str, Any]) -> bool:
    valid = row.get("valid_mask", {}) if isinstance(row.get("valid_mask"), dict) else {}
    if not bool(valid.get("iphone", True)):
        return False
    iphone = row.get("iphone", {}) if isinstance(row.get("iphone"), dict) else {}
    rgb = iphone.get("rgb", {}) if isinstance(iphone.get("rgb"), dict) else {}
    pose = iphone.get("pose", {}) if isinstance(iphone.get("pose"), dict) else {}
    return (
        bool(rgb.get("valid", True))
        and rgb.get("codec") == "h264"
        and isinstance(rgb.get("video_path"), str)
        and isinstance(rgb.get("frame_index"), int)
        and bool(pose.get("valid"))
        and bool(pose.get("world_calibrated"))
        and pose.get("frame") == "user_world"
    )


def dual_wrench_from_row(row: dict[str, Any]) -> tuple[list[float] | None, list[float] | None]:
    wrench = row.get("wrench")
    if isinstance(wrench, list) and len(wrench) == 2:
        left = six_float_wrench(wrench[0])
        right = six_float_wrench(wrench[1])
        if left is not None and right is not None:
            return left, right
    return six_float_wrench(row.get("left_wrench")), six_float_wrench(row.get("right_wrench"))


def coinft_row_has_dual_wrench(row: dict[str, Any]) -> bool:
    left, right = dual_wrench_from_row(row)
    return left is not None and right is not None


def coinft_samples_for_episode(
    coinft_samples: list[dict[str, Any]],
    *,
    episode_rows: list[dict[str, Any]],
    start_ns: int,
    end_ns: int,
) -> list[dict[str, Any]]:
    synchronized = []
    for row in episode_rows:
        coinft = row.get("coinft", {}) if isinstance(row.get("coinft"), dict) else {}
        if not bool(coinft.get("valid")) or not coinft_row_has_dual_wrench(coinft):
            continue
        synchronized.append(
            {
                "aligned_monotonic_ns": row.get("aligned_monotonic_ns"),
                "left_wrench": coinft.get("left_wrench"),
                "right_wrench": coinft.get("right_wrench"),
                "wrench": coinft.get("wrench"),
                "calibration": coinft.get("calibration"),
                "quality": coinft.get("quality"),
            }
        )
    if synchronized:
        return synchronized
    selected = [
        row
        for row in coinft_samples
        if isinstance(row.get("aligned_monotonic_ns"), int)
        and start_ns <= int(row["aligned_monotonic_ns"]) <= end_ns
        and coinft_row_has_dual_wrench(row)
    ]
    if selected:
        return selected
    return []

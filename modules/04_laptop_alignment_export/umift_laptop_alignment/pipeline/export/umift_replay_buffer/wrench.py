"""CoinFT wrench parsing and TCP/tool-frame transforms."""

from __future__ import annotations

import math
from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import numeric
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.constants import (
    COINFT_TO_GOPRO_Y_M,
    COINFT_TO_GOPRO_Z_M,
)
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.spatial import (
    adjoint,
    force_moment_to_wrench,
    rotation_y_matrix,
    rotation_z_matrix,
    wrench_to_force_moment,
)


def six_float_wrench(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 6:
        return None
    out = [numeric(item, math.nan) for item in value]
    if any(not math.isfinite(item) for item in out):
        return None
    return out


def coinft_pair_to_tcp(
    left_force_moment: Any,
    right_force_moment: Any,
    gripper_width_m: float,
    config: dict[str, Any],
    np: Any,
) -> tuple[Any, Any]:
    mode = str(config.get("write_tool_frame", "umi_coinft_to_tcp"))
    if mode in ("passthrough", "passthrough_until_transform_is_available", "coinft_sensor_frame"):
        return np.asarray(left_force_moment, dtype=np.float64), np.asarray(right_force_moment, dtype=np.float64)
    if mode != "umi_coinft_to_tcp":
        raise SystemExit(f"Unsupported UMI-FT force write_tool_frame mode: {mode}")

    left_t = transform_coinft_to_tcp("left", gripper_width_m, config, np)
    right_t = transform_coinft_to_tcp("right", gripper_width_m, config, np)
    left_tcp = wrench_to_force_moment(adjoint(left_t, np).T @ force_moment_to_wrench(left_force_moment, np), np)
    right_tcp = wrench_to_force_moment(adjoint(right_t, np).T @ force_moment_to_wrench(right_force_moment, np), np)
    return left_tcp, right_tcp


def transform_coinft_to_tcp(side: str, gripper_width_m: float, config: dict[str, Any], np: Any) -> Any:
    force_config = config.get("coinft_to_tcp", {}) if isinstance(config.get("coinft_to_tcp"), dict) else {}
    dist_z = numeric(force_config.get("dist_coinft2gopro_along_cam_z_m"), COINFT_TO_GOPRO_Z_M)
    dist_y = numeric(force_config.get("dist_coinft2gopro_along_cam_y_m"), COINFT_TO_GOPRO_Y_M)
    half_width = max(0.0, float(gripper_width_m)) / 2.0
    transform = np.eye(4, dtype=np.float64)
    if side == "left":
        transform[:3, :3] = rotation_y_matrix(math.radians(-90.0), np)
        transform[:3, 3] = np.asarray([dist_z, -dist_y, half_width], dtype=np.float64)
    elif side == "right":
        transform[:3, :3] = rotation_z_matrix(math.radians(180.0), np) @ rotation_y_matrix(math.radians(90.0), np)
        transform[:3, 3] = np.asarray([dist_z, dist_y, half_width], dtype=np.float64)
    else:
        raise ValueError(f"Unknown CoinFT side: {side}")
    return transform

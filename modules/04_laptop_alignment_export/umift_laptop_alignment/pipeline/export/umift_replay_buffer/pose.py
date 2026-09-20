"""Pose export transforms for UMI-FT replay-buffer format."""

from __future__ import annotations

import math
from typing import Any

from umift_laptop_alignment.pipeline.export.umift_replay_buffer.common import numeric
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.constants import (
    IPHONE_CAMERA_T_GRIPPER_CENTER_TCP,
)
from umift_laptop_alignment.pipeline.export.umift_replay_buffer.spatial import (
    pose7_to_se3,
    rpy_deg_to_quat_qwxyz,
    se3_to_pose7,
)


def _validated_se3(value: Any, *, label: str, np: Any) -> Any:
    try:
        transform = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{label} must contain numeric values.") from exc
    if transform.shape != (4, 4):
        raise SystemExit(f"{label} must be a 4x4 transform.")
    if not np.isfinite(transform).all():
        raise SystemExit(f"{label} must contain only finite values.")
    if not np.allclose(transform[3], [0.0, 0.0, 0.0, 1.0], atol=1e-4, rtol=0.0):
        raise SystemExit(f"{label} must have homogeneous bottom row [0, 0, 0, 1].")

    transform = transform.copy() / float(transform[3, 3])
    transform[3] = [0.0, 0.0, 0.0, 1.0]
    rotation = transform[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4, rtol=0.0):
        raise SystemExit(f"{label} rotation must be orthonormal.")
    determinant = float(np.linalg.det(rotation))
    if not math.isclose(determinant, 1.0, abs_tol=1e-4):
        raise SystemExit(f"{label} rotation determinant must be +1, got {determinant:.6g}.")
    return transform


def _legacy_iphone_camera_pose7(pose: dict[str, Any]) -> list[float]:
    quaternion = pose.get("quaternion_qwxyz")
    if quaternion is not None:
        if not isinstance(quaternion, (list, tuple)) or len(quaternion) != 4:
            raise SystemExit("iPhone pose quaternion_qwxyz must contain four values.")
        qw, qx, qy, qz = (numeric(value) for value in quaternion)
        if not all(math.isfinite(value) for value in (qw, qx, qy, qz)):
            raise SystemExit("iPhone pose quaternion_qwxyz must contain only finite values.")
        norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
        if norm <= 0.0:
            raise SystemExit("iPhone pose quaternion_qwxyz must have non-zero norm.")
        qw, qx, qy, qz = (qw / norm, qx / norm, qy / norm, qz / norm)
    else:
        qw, qx, qy, qz = rpy_deg_to_quat_qwxyz(
            numeric(pose.get("roll_deg")),
            numeric(pose.get("pitch_deg")),
            numeric(pose.get("yaw_deg")),
        )
    return [
        numeric(pose.get("x_m")),
        numeric(pose.get("y_m")),
        numeric(pose.get("z_m")),
        qw,
        qx,
        qy,
        qz,
    ]


def iphone_camera_pose_in_user_world(pose: dict[str, Any], np: Any) -> Any:
    matrix = pose.get("camera_in_user_world_transform")
    if matrix is not None:
        return _validated_se3(
            matrix,
            label="iPhone pose camera_in_user_world_transform",
            np=np,
        )
    return pose7_to_se3(_legacy_iphone_camera_pose7(pose), np)


def export_pose7_from_iphone_pose(pose: dict[str, Any], config: dict[str, Any], np: Any) -> list[float]:
    legacy_keys = {
        "source",
        "object",
        "export_transform",
        "iphone15_pro_i_t_g",
        "is_robot_absolute_eef",
    }
    present_legacy_keys = sorted(legacy_keys.intersection(config))
    if present_legacy_keys:
        raise SystemExit(
            "Legacy UMI-FT pose config is no longer supported; replace keys "
            f"{present_legacy_keys} with explicit user_world/iphone_camera/gripper_center_tcp semantics."
        )

    user_world_t_iphone_camera = iphone_camera_pose_in_user_world(pose, np)
    transform_name = str(config.get("transform", "iphone_camera_to_gripper_center_tcp"))
    if transform_name in ("none", "identity", "passthrough"):
        return se3_to_pose7(user_world_t_iphone_camera)
    if transform_name != "iphone_camera_to_gripper_center_tcp":
        raise SystemExit(f"Unsupported UMI-FT pose transform: {transform_name}")
    iphone_camera_t_gripper_center_tcp = _validated_se3(
        config.get(
            "iphone_camera_t_gripper_center_tcp",
            IPHONE_CAMERA_T_GRIPPER_CENTER_TCP,
        ),
        label="UMI-FT pose iphone_camera_t_gripper_center_tcp",
        np=np,
    )
    user_world_t_gripper_center_tcp = (
        user_world_t_iphone_camera @ iphone_camera_t_gripper_center_tcp
    )
    return se3_to_pose7(user_world_t_gripper_center_tcp)

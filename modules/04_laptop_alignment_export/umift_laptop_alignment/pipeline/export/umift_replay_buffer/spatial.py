"""Minimal SE(3), quaternion, and wrench math for UMI-FT export."""

from __future__ import annotations

import math
from typing import Any


def rotation_y_matrix(angle_rad: float, np: Any) -> Any:
    c = math.cos(angle_rad)
    s = math.sin(angle_rad)
    return np.asarray(
        [
            [c, 0.0, s],
            [0.0, 1.0, 0.0],
            [-s, 0.0, c],
        ],
        dtype=np.float64,
    )


def rotation_z_matrix(angle_rad: float, np: Any) -> Any:
    c = math.cos(angle_rad)
    s = math.sin(angle_rad)
    return np.asarray(
        [
            [c, -s, 0.0],
            [s, c, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def skew_symmetric(vector: Any, np: Any) -> Any:
    x, y, z = (float(v) for v in vector)
    return np.asarray(
        [
            [0.0, -z, y],
            [z, 0.0, -x],
            [-y, x, 0.0],
        ],
        dtype=np.float64,
    )


def adjoint(transform: Any, np: Any) -> Any:
    rotation = transform[:3, :3]
    translation = transform[:3, 3]
    out = np.zeros((6, 6), dtype=np.float64)
    out[:3, :3] = rotation
    out[3:, 3:] = rotation
    out[3:, :3] = skew_symmetric(translation, np) @ rotation
    return out


def force_moment_to_wrench(force_moment: Any, np: Any) -> Any:
    value = np.asarray(force_moment, dtype=np.float64).reshape(6)
    return np.concatenate([value[3:], value[:3]], axis=0)


def wrench_to_force_moment(wrench: Any, np: Any) -> Any:
    value = np.asarray(wrench, dtype=np.float64).reshape(6)
    return np.concatenate([value[3:], value[:3]], axis=0)


def quat_qwxyz_to_matrix(quaternion: Any, np: Any) -> Any:
    qw, qx, qy, qz = (float(v) for v in quaternion)
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm <= 0.0:
        qw, qx, qy, qz = 1.0, 0.0, 0.0, 0.0
    else:
        qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm
    return np.asarray(
        [
            [1.0 - 2.0 * (qy * qy + qz * qz), 2.0 * (qx * qy - qz * qw), 2.0 * (qx * qz + qy * qw)],
            [2.0 * (qx * qy + qz * qw), 1.0 - 2.0 * (qx * qx + qz * qz), 2.0 * (qy * qz - qx * qw)],
            [2.0 * (qx * qz - qy * qw), 2.0 * (qy * qz + qx * qw), 1.0 - 2.0 * (qx * qx + qy * qy)],
        ],
        dtype=np.float64,
    )


def matrix_to_quat_qwxyz(matrix: Any) -> tuple[float, float, float, float]:
    m = matrix
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (m[2, 1] - m[1, 2]) / s
        qy = (m[0, 2] - m[2, 0]) / s
        qz = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        qw = (m[2, 1] - m[1, 2]) / s
        qx = 0.25 * s
        qy = (m[0, 1] + m[1, 0]) / s
        qz = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        qw = (m[0, 2] - m[2, 0]) / s
        qx = (m[0, 1] + m[1, 0]) / s
        qy = 0.25 * s
        qz = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        qw = (m[1, 0] - m[0, 1]) / s
        qx = (m[0, 2] + m[2, 0]) / s
        qy = (m[1, 2] + m[2, 1]) / s
        qz = 0.25 * s
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm <= 0.0:
        return (1.0, 0.0, 0.0, 0.0)
    return (float(qw / norm), float(qx / norm), float(qy / norm), float(qz / norm))


def pose7_to_se3(pose7: Any, np: Any) -> Any:
    pose = np.asarray(pose7, dtype=np.float64).reshape(7)
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = quat_qwxyz_to_matrix(pose[3:], np)
    transform[:3, 3] = pose[:3]
    return transform


def se3_to_pose7(transform: Any) -> list[float]:
    qw, qx, qy, qz = matrix_to_quat_qwxyz(transform[:3, :3])
    return [
        float(transform[0, 3]),
        float(transform[1, 3]),
        float(transform[2, 3]),
        qw,
        qx,
        qy,
        qz,
    ]


def rpy_deg_to_quat_qwxyz(roll_deg: float, pitch_deg: float, yaw_deg: float) -> tuple[float, float, float, float]:
    roll = math.radians(roll_deg)
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)
    cr = math.cos(roll * 0.5)
    sr = math.sin(roll * 0.5)
    cp = math.cos(pitch * 0.5)
    sp = math.sin(pitch * 0.5)
    cy = math.cos(yaw * 0.5)
    sy = math.sin(yaw * 0.5)
    qw = cr * cp * cy + sr * sp * sy
    qx = sr * cp * cy - cr * sp * sy
    qy = cr * sp * cy + sr * cp * sy
    qz = cr * cp * sy - sr * sp * cy
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm <= 0.0:
        return (1.0, 0.0, 0.0, 0.0)
    return (qw / norm, qx / norm, qy / norm, qz / norm)

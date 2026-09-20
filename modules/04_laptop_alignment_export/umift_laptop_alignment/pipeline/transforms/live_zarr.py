"""Live ForceFlow/Zarr semantic conversion state.

This module does not write final Zarr arrays yet.  It converts the latest
validated live samples into the same scalar semantics the Zarr writer will use,
so the main program can expose current values in /state before recording starts.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from umift_laptop_alignment.pipeline.export.forceflow.gripper_mapping import raw_open_to_binary, raw_open_to_unit


POSE_MODE = "user_world_phone"
FORCE_MODE = "placeholder_zero"
TCP_EXTRINSIC_STATUS = "reserved_not_available"


def _finite_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    output = float(value)
    return output if math.isfinite(output) else None


def _timestamp_from_frame(frame: dict[str, Any]) -> tuple[int | None, int | None]:
    timestamp = frame.get("timestamp") if isinstance(frame.get("timestamp"), dict) else {}
    monotonic_ns = timestamp.get("aligned_monotonic_ns")
    if not isinstance(monotonic_ns, int):
        monotonic_ns = timestamp.get("mac_receive_monotonic_ns")
    unix_ns = timestamp.get("aligned_unix_ns")
    if not isinstance(unix_ns, int):
        unix_ns = timestamp.get("mac_receive_unix_ns")
    return (
        monotonic_ns if isinstance(monotonic_ns, int) else None,
        unix_ns if isinstance(unix_ns, int) else None,
    )


def _pose_mm_rad_from_iphone(frame: dict[str, Any]) -> tuple[list[float] | None, str | None]:
    pose = frame.get("pose") if isinstance(frame.get("pose"), dict) else {}
    if not pose.get("valid"):
        return None, "iphone pose is invalid"
    if not pose.get("world_calibrated"):
        return None, f"iphone world origin is not calibrated ({pose.get('world_origin_status') or 'missing'})"
    if pose.get("frame") != "user_world":
        return None, f"iphone pose frame is {pose.get('frame')!r}, expected 'user_world'"

    x_m = _finite_float(pose.get("x_m"))
    y_m = _finite_float(pose.get("y_m"))
    z_m = _finite_float(pose.get("z_m"))
    roll_deg = _finite_float(pose.get("roll_deg"))
    pitch_deg = _finite_float(pose.get("pitch_deg"))
    yaw_deg = _finite_float(pose.get("yaw_deg"))
    values = [x_m, y_m, z_m, roll_deg, pitch_deg, yaw_deg]
    if any(value is None for value in values):
        return None, "iphone pose is missing numeric xyz/rpy fields"

    return [
        float(x_m) * 1000.0,
        float(y_m) * 1000.0,
        float(z_m) * 1000.0,
        math.radians(float(roll_deg)),
        math.radians(float(pitch_deg)),
        math.radians(float(yaw_deg)),
    ], None


def _gripper_from_iphone(frame: dict[str, Any]) -> tuple[float | None, float | None, float | None, str | None]:
    gripper = frame.get("gripper") if isinstance(frame.get("gripper"), dict) else {}
    if not gripper.get("valid"):
        return None, None, None, "iphone gripper is invalid"
    raw_open = _finite_float(gripper.get("open_percent"))
    if raw_open is None:
        return None, None, None, "iphone gripper opening is missing"
    open_unit = raw_open_to_unit(raw_open, input_scale="auto")
    binary = raw_open_to_binary(raw_open, input_scale="auto", threshold=0.5)
    return raw_open, open_unit, binary, None


@dataclass
class LiveForceFlowRow:
    timestamp_ns: int
    timestamp_unix_ns: int | None
    episode_index: int
    recording_active: bool
    pos: list[float]
    gripper_open_raw: float
    gripper_open_unit: float
    gripper_state: float
    source_frame_sequence: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_ns": self.timestamp_ns,
            "timestamp_unix_ns": self.timestamp_unix_ns,
            "episode_index": self.episode_index,
            "recording_active": self.recording_active,
            "pose_mode": POSE_MODE,
            "is_tcp_pose": False,
            "tcp_extrinsic_status": TCP_EXTRINSIC_STATUS,
            "pos": list(self.pos),
            "pos_units": ["mm", "mm", "mm", "rad", "rad", "rad"],
            "gripper_open_raw": self.gripper_open_raw,
            "gripper_open_unit": self.gripper_open_unit,
            "gripper_state": [self.gripper_state],
            "force": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "force_mode": FORCE_MODE,
            "source_frame_sequence": self.source_frame_sequence,
        }


@dataclass
class LiveForceFlowZarrState:
    """Main-chain state container for live ForceFlow/Zarr conversion values."""

    target_fps: float = 20.0
    status: str = "idle"
    enabled: bool = False
    updated_unix_ns: int | None = None
    reasons: list[str] = field(default_factory=list)
    converted_rows: int = 0
    completed_steps: int = 0
    recording_rows_observed: int = 0
    recording_steps_observed: int = 0
    dropped_rows: int = 0
    last_error: str | None = None
    latest_row: dict[str, Any] | None = None
    latest_completed_step: dict[str, Any] | None = None
    _previous_row: LiveForceFlowRow | None = field(default=None, repr=False)

    def observe_iphone_frame(
        self,
        frame: dict[str, Any],
        *,
        recording_gate: dict[str, Any],
        recording_control: dict[str, Any],
    ) -> dict[str, Any]:
        self.updated_unix_ns = time.time_ns()
        gate_ok = bool(recording_gate.get("ok"))
        self.enabled = gate_ok
        self.reasons = [str(reason) for reason in recording_gate.get("reasons", [])] if isinstance(recording_gate.get("reasons"), list) else []

        if not gate_ok:
            self.status = "waiting_for_streams"
            self.last_error = "; ".join(self.reasons) if self.reasons else None
            return self.snapshot()

        timestamp_ns, timestamp_unix_ns = _timestamp_from_frame(frame)
        if timestamp_ns is None:
            self.status = "invalid_source"
            self.dropped_rows += 1
            self.last_error = "iphone frame has no aligned/mac monotonic timestamp"
            return self.snapshot()

        pos, pose_error = _pose_mm_rad_from_iphone(frame)
        if pos is None:
            self.status = "invalid_source"
            self.dropped_rows += 1
            self.last_error = pose_error
            return self.snapshot()

        raw_open, open_unit, gripper_state, gripper_error = _gripper_from_iphone(frame)
        if raw_open is None or open_unit is None or gripper_state is None:
            self.status = "invalid_source"
            self.dropped_rows += 1
            self.last_error = gripper_error
            return self.snapshot()

        recording_active = bool(recording_control.get("active"))
        episode_index = recording_control.get("episode_index")
        if not isinstance(episode_index, int):
            episode_index = 0

        row = LiveForceFlowRow(
            timestamp_ns=timestamp_ns,
            timestamp_unix_ns=timestamp_unix_ns,
            episode_index=episode_index,
            recording_active=recording_active,
            pos=pos,
            gripper_open_raw=raw_open,
            gripper_open_unit=open_unit,
            gripper_state=gripper_state,
            source_frame_sequence=frame.get("sequence"),
        )

        self.converted_rows += 1
        if row.recording_active:
            self.recording_rows_observed += 1

        previous = self._previous_row
        self.latest_completed_step = None
        if previous is not None and previous.episode_index == row.episode_index:
            action = [float(row.pos[index] - previous.pos[index]) for index in range(6)]
            step_recording = bool(previous.recording_active and row.recording_active)
            self.completed_steps += 1
            if step_recording:
                self.recording_steps_observed += 1
            self.latest_completed_step = {
                "from_timestamp_ns": previous.timestamp_ns,
                "to_timestamp_ns": row.timestamp_ns,
                "dt_ms": (row.timestamp_ns - previous.timestamp_ns) / 1e6,
                "episode_index": row.episode_index,
                "recording_active": step_recording,
                "row": previous.to_dict(),
                "action": action,
                "action_units": ["mm", "mm", "mm", "rad", "rad", "rad"],
                "gripper_action": [row.gripper_state],
                "gripper_action_semantics": "next_output_frame_binary_state",
            }

        self._previous_row = row
        self.latest_row = row.to_dict()
        self.status = "converting_recording" if recording_active else "converting_preview"
        self.last_error = None
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "enabled": self.enabled,
            "updated_unix_ns": self.updated_unix_ns,
            "target_fps": self.target_fps,
            "pose_mode": POSE_MODE,
            "is_tcp_pose": False,
            "tcp_extrinsic_status": TCP_EXTRINSIC_STATUS,
            "T_P_TCP_session": None,
            "T_B_W": None,
            "force_mode": FORCE_MODE,
            "reasons": list(self.reasons),
            "converted_rows": self.converted_rows,
            "completed_steps": self.completed_steps,
            "recording_rows_observed": self.recording_rows_observed,
            "recording_steps_observed": self.recording_steps_observed,
            "dropped_rows": self.dropped_rows,
            "last_error": self.last_error,
            "latest_row": self.latest_row,
            "latest_completed_step": self.latest_completed_step,
            "writer": {
                "status": "not_started",
                "recording_writes_enabled": False,
                "note": "live conversion is active; final Zarr writer is the next pipeline stage",
            },
        }

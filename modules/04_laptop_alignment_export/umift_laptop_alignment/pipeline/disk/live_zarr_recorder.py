"""Recording-window persistence for live ForceFlow/Zarr converted rows."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


@dataclass
class LiveZarrRecordingWriter:
    """Persist completed live converted rows during official recording windows."""

    active: bool = False
    run_dir: Path | None = None
    episode_index: int | None = None
    episode_dir: Path | None = None
    rows_path: Path | None = None
    manifest_path: Path | None = None
    started_unix_ns: int | None = None
    stopped_unix_ns: int | None = None
    completed_rows_written: int = 0
    tail_rows_written: int = 0
    last_error: str | None = None
    _written_source_timestamps: set[int] = field(default_factory=set, repr=False)

    def start(self, *, run_dir: Path, episode_index: int, transition: dict[str, Any]) -> dict[str, Any]:
        self.active = True
        self.run_dir = run_dir
        self.episode_index = episode_index
        self.episode_dir = run_dir / "pipeline" / "live_zarr_rows" / f"episode_{episode_index:06d}"
        self.rows_path = self.episode_dir / "completed_rows.jsonl"
        self.manifest_path = self.episode_dir / "MANIFEST.json"
        self.started_unix_ns = time.time_ns()
        self.stopped_unix_ns = None
        self.completed_rows_written = 0
        self.tail_rows_written = 0
        self.last_error = None
        self._written_source_timestamps.clear()
        self.episode_dir.mkdir(parents=True, exist_ok=True)
        if self.rows_path.exists():
            self.rows_path.unlink()
        self._write_manifest(
            {
                "event": "start",
                "transition": transition,
            }
        )
        return self.snapshot()

    def observe_conversion(self, conversion: dict[str, Any]) -> dict[str, Any]:
        if not self.active:
            return self.snapshot()
        step = conversion.get("latest_completed_step") if isinstance(conversion, dict) else None
        if not isinstance(step, dict) or not step.get("recording_active"):
            return self.snapshot()
        row = step.get("row") if isinstance(step.get("row"), dict) else None
        if not isinstance(row, dict):
            return self.snapshot()
        source_ts = row.get("timestamp_ns")
        if not isinstance(source_ts, int) or source_ts in self._written_source_timestamps:
            return self.snapshot()

        payload = {
            "schema_version": 1,
            "row_type": "live_forceflow_completed_row",
            "written_unix_ns": time.time_ns(),
            "episode_index": self.episode_index,
            "source_timestamp_ns": source_ts,
            "source_row": row,
            "action": step.get("action"),
            "gripper_action": step.get("gripper_action"),
            "action_semantics": "next_output_frame_delta",
            "gripper_action_semantics": step.get("gripper_action_semantics"),
            "next_timestamp_ns": step.get("to_timestamp_ns"),
            "dt_ms": step.get("dt_ms"),
        }
        try:
            if self.rows_path is None:
                raise RuntimeError("rows path is not initialized")
            _append_jsonl(self.rows_path, payload)
            self._written_source_timestamps.add(source_ts)
            self.completed_rows_written += 1
            self.last_error = None
            self._write_manifest({"event": "observe_conversion"})
        except Exception as exc:
            self.last_error = str(exc)
        return self.snapshot()

    def stop(self, *, transition: dict[str, Any], conversion: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.active and isinstance(conversion, dict):
            self._write_tail_row_if_needed(conversion)
        self.active = False
        self.stopped_unix_ns = time.time_ns()
        self._write_manifest(
            {
                "event": "stop",
                "transition": transition,
            }
        )
        return self.snapshot()

    def discard(self, *, transition: dict[str, Any]) -> dict[str, Any]:
        episode_dir = self.episode_dir
        self.active = False
        self.stopped_unix_ns = time.time_ns()
        self.completed_rows_written = 0
        self.tail_rows_written = 0
        self._written_source_timestamps.clear()
        if episode_dir and episode_dir.exists():
            shutil.rmtree(episode_dir)
        self.last_error = None
        return self.snapshot()

    def _write_tail_row_if_needed(self, conversion: dict[str, Any]) -> None:
        row = conversion.get("latest_row") if isinstance(conversion.get("latest_row"), dict) else None
        if not isinstance(row, dict) or not row.get("recording_active"):
            return
        source_ts = row.get("timestamp_ns")
        if not isinstance(source_ts, int) or source_ts in self._written_source_timestamps:
            return
        gripper_state = row.get("gripper_state")
        if not isinstance(gripper_state, list) or not gripper_state:
            gripper_state = [0.0]
        payload = {
            "schema_version": 1,
            "row_type": "live_forceflow_terminal_row",
            "written_unix_ns": time.time_ns(),
            "episode_index": self.episode_index,
            "source_timestamp_ns": source_ts,
            "source_row": row,
            "action": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "gripper_action": [float(gripper_state[0])],
            "action_semantics": "terminal_zero_pose_delta",
            "gripper_action_semantics": "terminal_repeat_current_binary_state",
            "next_timestamp_ns": None,
            "dt_ms": None,
        }
        try:
            if self.rows_path is None:
                raise RuntimeError("rows path is not initialized")
            _append_jsonl(self.rows_path, payload)
            self._written_source_timestamps.add(source_ts)
            self.tail_rows_written += 1
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)

    def _write_manifest(self, extra: dict[str, Any] | None = None) -> None:
        if self.manifest_path is None:
            return
        payload = {
            "schema_version": 1,
            "stage": "live_zarr_recording_rows",
            "status": "recording" if self.active else "stopped",
            "updated_unix_ns": time.time_ns(),
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "episode_index": self.episode_index,
            "rows_path": str(self.rows_path) if self.rows_path else None,
            "completed_rows_written": self.completed_rows_written,
            "tail_rows_written": self.tail_rows_written,
            "pose_mode": "user_world_phone",
            "is_tcp_pose": False,
            "tcp_extrinsic_status": "reserved_not_available",
            "force_mode": "placeholder_zero",
            "final_zarr_writer": "pending",
            "extra": extra or {},
        }
        try:
            _write_json_atomic(self.manifest_path, payload)
        except Exception as exc:
            self.last_error = str(exc)

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": "recording" if self.active else ("stopped" if self.stopped_unix_ns else "idle"),
            "active": self.active,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "episode_index": self.episode_index,
            "episode_dir": str(self.episode_dir) if self.episode_dir else None,
            "rows_path": str(self.rows_path) if self.rows_path else None,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "started_unix_ns": self.started_unix_ns,
            "stopped_unix_ns": self.stopped_unix_ns,
            "completed_rows_written": self.completed_rows_written,
            "tail_rows_written": self.tail_rows_written,
            "last_error": self.last_error,
        }

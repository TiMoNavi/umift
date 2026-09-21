#!/usr/bin/env python3
"""Browser-based receiver GUI for first-pass data collection."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from umift_laptop_alignment.app.http_handler import HTML, Handler
from umift_laptop_alignment.app.platform_support import open_directory

from umift_laptop_alignment.capture.receivers.iphone.archive_receiver_v2 import (
    DEFAULT_DEVICE_PORT,
    DEFAULT_OUTPUT_ROOT,
    ArchiveReceiverV2,
)
from umift_laptop_alignment.orchestration.datasets import (
    compatibility_issues,
    dataset_summary,
    initialize_dataset_metadata,
    list_datasets,
    resolve_dataset_run,
)
from umift_laptop_alignment.orchestration.run_layout import DEFAULT_RUNS_ROOT, append_sync_log, create_run, ensure_run_layout, read_json, register_artifact, update_manifest
from umift_laptop_alignment.capture.receivers.coinft.serial_worker import BAUD_RATE as COINFT_BAUD_RATE
from umift_laptop_alignment.capture.receivers.coinft.serial_worker import CoinFTSerialWorker
from umift_laptop_alignment.capture.receivers.coinft.teensy_mock import TeensyMockWorker
from umift_laptop_alignment.capture.receivers.coinft.calibration import (
    CoinFTCalibrationConfig,
    calibration_status_summary,
    coinft_calibration_config_from_files,
    coinft_calibration_config_from_side_dirs,
    load_coinft_calibration_config,
    raw_coinft_config,
)
from umift_laptop_alignment.capture.receivers.iphone.gui_receiver_v2 import GuiReceiverV2
from umift_laptop_alignment.capture.receivers.iphone.protocol_v2 import (
    FrameMetadata,
    Packet,
    RecordingEvent,
    SessionStart,
    VideoCodecConfig,
)
from umift_laptop_alignment.orchestration.recording_events import RecordingTransition
from umift_laptop_alignment.pipeline.alignment.runner import align_episode as align_episode_impl
from umift_laptop_alignment.pipeline.normalize.runner import normalize_episode as normalize_episode_impl
from umift_laptop_alignment.pipeline.disk.live_zarr_recorder import LiveZarrRecordingWriter
from umift_laptop_alignment.pipeline.export.controller import ExportController
from umift_laptop_alignment.pipeline.postprocess import PostprocessConfig, PostprocessOrchestrator
from umift_laptop_alignment.pipeline.transforms.live_zarr import LiveForceFlowZarrState
from umift_laptop_alignment.pipeline.transforms.iphone_v2 import phone_clock_model


MODULE04_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = DEFAULT_DEVICE_PORT
D435_TEMP_ROOT = Path("/tmp") if sys.platform == "darwin" else Path(tempfile.gettempdir())
DEFAULT_D435_PREVIEW_DIR = D435_TEMP_ROOT / "umift_realsense_preview"
D435_CONTROL_SCRIPT = MODULE04_ROOT / "umift_laptop_alignment" / "capture" / "receivers" / "d435" / "control_capture_macos.sh"
INSTALLED_D435_CONTROL_SCRIPT = Path("/usr/local/libexec/umift-laptop-alignment-d435/control_capture_macos.sh")
INSTALLED_D435_RUNTIME_PID_FILE = INSTALLED_D435_CONTROL_SCRIPT.parent / ".runtime" / "capture.pid"
D435_SUDOERS_INSTALL_SCRIPT = MODULE04_ROOT / "entrypoints" / "install_sudoers_macos.sh"
D435_STREAM_LOG = D435_TEMP_ROOT / "umift_d435_preview.log"
D435_RECORD_CONTROL_FILE = D435_TEMP_ROOT / "umift_d435_record_control.json"
D435_STREAM_STATUS_FILE = D435_TEMP_ROOT / "umift_d435_stream_status.json"
IPHONE_FRESH_MAX_S = 2.0
TEENSY_FRESH_MAX_S = 1.0
D435_PREVIEW_FRESH_MAX_S = 2.5
D435_STARTUP_GRACE_S = 25.0
LIVE_STREAM_SKEW_MAX_S = 1.5
ALIGNMENT_MIN_VALID_RATIO = 0.8
DEFAULT_RECORDING_PREROLL_S = 1.5
DEFAULT_STREAM_BUFFER_S = 3.0
D435_RECORDING_START_TIMEOUT_MIN_S = 6.0
POST_START_GATE_CHECK_DELAY_S = 2.5
COINFT_RESTART_STOP_TIMEOUT_S = 2.0
COINFT_GUI_PUBLISH_INTERVAL_S = 1.0 / 200.0
COINFT_STATE_PUBLISH_INTERVAL_S = 1.0 / 2.0
GUI_STATE_PUBLISH_INTERVAL_S = 1.0 / 2.0
IPHONE_GUI_PUBLISH_INTERVAL_S = 1.0 / 5.0
GUI_CLIENT_LOG_MAX_MESSAGES = 16
PC_TARGET_FPS = 20.0
TEENSY_BUFFER_MAX_SAMPLES = 600
EPISODE_INDEX_STATE_FILE = "episode_index_state.json"
EPISODE_DIR_PATTERN = re.compile(r"^episode_(\d{6})$")
D435_EPISODE_RESERVATION_FILE = ".umift_episode_reservation.json"
DATASET_SELECTION_STATE_FILE = ".selected_dataset.json"


def timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def ns_to_ms(value: Any) -> float | None:
    return value / 1e6 if isinstance(value, int) else None


def fmt_ms(value: Any) -> str:
    return f"{float(value):.1f} ms" if isinstance(value, (int, float)) else "missing"


def d435_recording_start_timeout_s(recording_preroll_s: float) -> float:
    """Allow writer initialization and preroll backfill without restarting acquisition."""
    return max(D435_RECORDING_START_TIMEOUT_MIN_S, 3.0 + 2.0 * max(recording_preroll_s, 0.0))


def file_age_ms(path: Path, now_unix_ns: int) -> float | None:
    try:
        return max(0.0, (now_unix_ns - path.stat().st_mtime_ns) / 1e6)
    except OSError:
        return None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def load_json_if_present(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def episode_indices_on_disk(run_dir: Path) -> set[int]:
    indices: set[int] = set()
    raw_dir = run_dir / "raw"
    if not raw_dir.exists():
        return indices
    for path in raw_dir.rglob("episode_*"):
        if not path.is_dir():
            continue
        match = EPISODE_DIR_PATTERN.fullmatch(path.name)
        if match:
            indices.add(int(match.group(1)))
    return indices


def reserve_next_episode_index(
    run_dir: Path,
    *,
    requested_index: int | None = None,
    current_index: int | None = None,
) -> int:
    state_path = run_dir / "raw" / EPISODE_INDEX_STATE_FILE
    state = load_json_if_present(state_path)
    last_reserved = state.get("last_reserved_episode_index")
    occupied = episode_indices_on_disk(run_dir)
    floors = [index for index in occupied]
    if isinstance(last_reserved, int) and last_reserved >= 0:
        floors.append(last_reserved)
    if isinstance(current_index, int) and current_index >= 0:
        floors.append(current_index)
    minimum_next = max(floors, default=-1) + 1
    episode_index = (
        requested_index
        if isinstance(requested_index, int) and requested_index >= minimum_next
        else minimum_next
    )
    write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "last_reserved_episode_index": episode_index,
            "next_episode_index": episode_index + 1,
            "requested_episode_index": requested_index,
            "occupied_episode_indices": sorted(occupied),
            "reserved_at_unix_ns": time.time_ns(),
        },
    )
    return episode_index


def release_discarded_episode_index(run_dir: Path, episode_index: int) -> bool:
    """Release only the most recently reserved, fully removed episode index."""
    state_path = run_dir / "raw" / EPISODE_INDEX_STATE_FILE
    state = load_json_if_present(state_path)
    if state.get("last_reserved_episode_index") != episode_index:
        return False
    occupied = episode_indices_on_disk(run_dir)
    if episode_index in occupied:
        return False
    previous_index = max((index for index in occupied if index < episode_index), default=-1)
    write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "last_reserved_episode_index": previous_index,
            "next_episode_index": episode_index,
            "released_episode_index": episode_index,
            "occupied_episode_indices": sorted(occupied),
            "released_at_unix_ns": time.time_ns(),
        },
    )
    return True


def discard_episode_artifacts(run_dir: Path, episode_index: int) -> list[str]:
    """Remove artifacts that belong solely to one uncommitted recording episode."""
    episode_name = f"episode_{episode_index:06d}"
    candidates = [
        run_dir / "raw" / "global_camera" / episode_name,
        run_dir / "raw" / "coinft" / episode_name,
        run_dir / "pipeline" / "live_zarr_rows" / episode_name,
        run_dir / "normalized" / "episodes" / episode_name,
        run_dir / "aligned" / "episodes" / episode_name,
    ]
    removed: list[str] = []
    for path in candidates:
        if path.exists():
            shutil.rmtree(path)
            removed.append(str(path.relative_to(run_dir)))
    return removed


def tail_text(path: Path, *, max_bytes: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def d435_error_from_log_tail(text: str) -> str | None:
    if "sudo: a password is required" in text:
        return "D435 requires sudo, but sudo -n has no cached credential"
    if "control_capture_macos.sh must run as root" in text:
        return "D435 control script must run as root"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        last = lines[-1]
        if "error" in last.lower() or "failed" in last.lower():
            return last[:500]
    return None


@dataclass
class ServerConfig:
    host: str
    port: int
    d435_preview_dir: Path
    output_root: Path
    runs_root: Path
    auto_start: bool
    coinft_source: str
    coinft_port: str
    coinft_baud: int
    coinft_simulate: bool
    coinft_tare: bool
    coinft_config: str


class GuiClientMailbox:
    """Coalescing mailbox for one browser's non-critical live display.

    Capture uses its own strict FIFO paths.  The browser is only a monitor, so
    retaining an old state/sample while a newer one exists wastes CPU and makes
    the display look delayed.  Each high-rate message type therefore owns one
    replaceable slot; only logs retain their short ordered history.
    """

    _COALESCED_TYPES = (
        "state",
        "sample",
        "coinft_preview_sample",
        "teensy_sample",
    )

    def __init__(self, initial_state: dict[str, Any]):
        self._condition = threading.Condition()
        self._latest: dict[str, dict[str, Any]] = {
            "state": {"type": "state", "state": initial_state},
        }
        self._logs: deque[dict[str, Any]] = deque(maxlen=GUI_CLIENT_LOG_MAX_MESSAGES)

    def put(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "")
        with self._condition:
            if message_type in self._COALESCED_TYPES:
                self._latest[message_type] = message
            elif message_type == "log":
                self._logs.append(message)
            else:
                # Control/status notifications are rare.  Keeping one latest
                # value makes them bounded without losing their current state.
                self._latest[message_type] = message
            self._condition.notify()

    def get(self, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._latest and not self._logs:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            # State is sent first so labels and preview paths describe the
            # same most-recent acquisition state before sample-level metrics.
            for message_type in self._COALESCED_TYPES:
                message = self._latest.pop(message_type, None)
                if message is not None:
                    return message
            if self._logs:
                return self._logs.popleft()
            # Unknown low-rate message types are still bounded and delivered.
            _, message = self._latest.popitem()
            return message


class ReceiverBackend:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.coinft_calibration_error: str | None = None
        self.coinft_calibration_config = self.load_coinft_calibration_config_for_backend()
        self.lock = threading.Lock()
        self.capture_lifecycle_lock = threading.RLock()
        self.coinft_lifecycle_lock = threading.Lock()
        self.d435_lifecycle_lock = threading.Lock()
        self.episode_allocation_lock = threading.Lock()
        self.clients: list[GuiClientMailbox] = []
        self.worker: GuiReceiverV2 | None = None
        self.stop_event: threading.Event | None = None
        self.teensy_worker: threading.Thread | None = None
        self.teensy_stop_event: threading.Event | None = None
        self.d435_process: subprocess.Popen[bytes] | None = None
        self.d435_log_handle: Any | None = None
        self.d435_started_monotonic_s: float | None = None
        self._last_coinft_gui_publish_monotonic_ns = 0
        self._last_coinft_state_publish_monotonic_ns = 0
        self._last_iphone_gui_publish_monotonic_ns = 0
        self._iphone_v2_min_offset_ns: int | None = None
        self._iphone_v2_alignment_samples = 0
        self._iphone_v2_clock_model: dict[str, Any] | None = None
        self._iphone_v2_first_receive_monotonic_ns: int | None = None
        self._iphone_v2_initial_frame_count = 0
        self._iphone_v2_initial_encoded_bytes = 0
        self._state_broadcast_lock = threading.Lock()
        self._last_state_broadcast_monotonic_ns = 0
        self.live_zarr_conversion = LiveForceFlowZarrState()
        self.live_zarr_writer = LiveZarrRecordingWriter()
        self.export_controller = ExportController(MODULE04_ROOT)
        self.postprocess_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="umift-postprocess",
        )
        self.iphone_event_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="iphone-v2-events",
        )
        self.postprocess = PostprocessOrchestrator(
            self.export_controller,
            config=PostprocessConfig(cleanup_intermediates=False),
            coinft_config=self.coinft_calibration_config,
            state_callback=self.update_postprocess_state,
        )
        initial_coinft_state = {
            "running": False,
            "source": config.coinft_source,
            "serial_port": config.coinft_port or None,
            **calibration_status_summary(self.coinft_calibration_config),
            "calibration_status": "raw_only" if not self.coinft_calibration_config.calibrated else "configured",
            "calibration_ready": not self.coinft_calibration_config.calibrated,
            "calibration_error": self.coinft_calibration_error,
            "live_sample_count": 0,
            "recorded_sample_count": 0,
            "fps": 0.0,
            "left_mean": None,
            "right_mean": None,
            "side_order": ["left", "right"],
            "wrench_axes": ["fx", "fy", "fz", "mx", "my", "mz"],
            "wrench": None,
            "latest": None,
            "output_csv": None,
            "latest_sample_monotonic_ns": None,
            "last_error": None,
        }
        self.state: dict[str, Any] = {
            "running": False,
            "status": "idle",
            "run_id": None,
            "run_dir": None,
            "run_manifest": None,
            "dataset_selection": {
                "selected": False,
                "run_id": None,
                "run_dir": None,
                "display_name": None,
                "compatibility_issues": [],
            },
            "session_id": None,
            "session_dir": None,
            "raw_jsonl": None,
            "aligned_jsonl": None,
            "stats": {},
            "alignment": {},
            "latest_frame": {},
            "latest_event": None,
            "latest_iphone_rgb_path": None,
            "latest_iphone_rgb_version": 0,
            "latest_iphone_wide_preview_path": None,
            "latest_iphone_wide_preview_version": 0,
            "latest_iphone_depth_preview_path": None,
            "latest_iphone_depth_preview_version": 0,
            "latest_iphone_ultra_preview_path": None,
            "latest_iphone_ultra_preview_version": 0,
            "latest_iphone_preview_source": "Wide",
            "latest_iphone_sample_monotonic_ns": None,
            "iphone_video": {
                "protocol_version": 2,
                "codec": "h264",
                "width": 1920,
                "height": 1440,
                "frames_per_second": 30,
                "preview_ready": False,
                "preview_dir": None,
                "last_sequence": None,
                "encoded_bytes": 0,
                "fps": 0.0,
                "mbps": 0.0,
            },
            "recording_control": {
                "armed": False,
                "active": False,
                "episode_index": None,
                "started_at": None,
                "stopped_at": None,
                "last_event": None,
                "blocked": False,
                "blocked_reason": None,
                "source": "iphone_recording_event",
            },
            "recording_gate": {
                "ok": False,
                "status": "not_armed",
                "reasons": ["capture is not armed"],
                "streams": {},
                "checked_at_unix_ns": None,
            },
            "recording_pipeline": {
                "phase": "idle",
                "transition_seq": 0,
                "current_episode_index": None,
                "canonical_event_time": {},
                "last_transition": None,
                "transitions": [],
                "post_start_check": {
                    "status": "idle",
                    "delay_s": POST_START_GATE_CHECK_DELAY_S,
                    "episode_index": None,
                    "checked_at_unix_ns": None,
                    "reasons": [],
                },
            },
            "capture_summary": {
                "status": "idle",
                "ready": False,
                "updated_unix_ns": None,
                "streams": {},
                "recording": {},
                "pipeline": {},
                "gate_reasons": ["capture is not armed"],
            },
            "capture_options": {
                "d435_duration_s": 7200.0,
                "recording_preroll_s": DEFAULT_RECORDING_PREROLL_S,
                "stream_buffer_s": DEFAULT_STREAM_BUFFER_S,
            },
            "buffering": {
                "recording_preroll_s": DEFAULT_RECORDING_PREROLL_S,
                "stream_buffer_s": DEFAULT_STREAM_BUFFER_S,
                "iphone": {},
                "d435": {},
                "teensy": {},
            },
            "teensy": dict(initial_coinft_state),
            "coinft": dict(initial_coinft_state),
            "d435": {
                "running": False,
                "pid": None,
                "mode": "continuous_stream",
                "preview_dir": str(config.d435_preview_dir),
                "log": str(D435_STREAM_LOG),
                "control_file": str(D435_RECORD_CONTROL_FILE),
                "status_file": str(D435_STREAM_STATUS_FILE),
            },
            "d435_recording": {
                "running": False,
                "pid": None,
                "output_dir": None,
                "duration_s": None,
                "log": str(D435_STREAM_LOG),
                "control_file": str(D435_RECORD_CONTROL_FILE),
                "status_file": str(D435_STREAM_STATUS_FILE),
            },
            "derived": {
                "normalized": None,
                "aligned": None,
            },
            "zarr_conversion": self.live_zarr_conversion.snapshot(),
            "zarr_recording": self.live_zarr_writer.snapshot(),
            "export_control": self.export_controller.snapshot(),
            "postprocess": {
                "status": "idle",
                "stage": None,
                "run_dir": None,
                "episode_index": None,
                "cleanup_intermediates": False,
                "error": None,
            },
            "sync_quality": {
                "ok": False,
                "status": "not_checked",
                "reasons": ["alignment has not been checked"],
                "checked_at_unix_ns": None,
            },
            "logs": [],
            "started_wall_s": None,
        }
        self.restore_dataset_selection()

    def update_postprocess_state(self, postprocess: dict[str, Any]) -> None:
        with self.lock:
            self.state["postprocess"] = dict(postprocess)
            self.state["export_control"] = self.export_controller.snapshot()
            alignment_quality = postprocess.get("alignment_quality")
            if isinstance(alignment_quality, dict):
                alignment_ok = bool(alignment_quality.get("ok"))
                self.state["sync_quality"] = {
                    "ok": alignment_ok,
                    "status": "ready" if alignment_ok else "blocked",
                    "reasons": list(alignment_quality.get("reasons") or []),
                    "ratios": dict(alignment_quality.get("ratios") or {}),
                    "checked_at_unix_ns": time.time_ns(),
                }
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})

    def start_postprocess(self, run_dir: Path, episode_index: int) -> None:
        def worker() -> None:
            self.log(f"postprocess started for episode {episode_index}")
            result = self.postprocess.run(run_dir, episode_index=episode_index)
            if result.get("status") == "ok":
                self.log(f"postprocess complete: {result.get('export', {}).get('outputs', {})}")
            else:
                self.log(f"postprocess {result.get('status')}: {result.get('error', 'unknown error')}")

        # Submit and return immediately. Collection and device workers never
        # wait for post-processing to finish.
        self.postprocess_executor.submit(worker)

    def load_coinft_calibration_config_for_backend(self) -> CoinFTCalibrationConfig:
        self.coinft_calibration_error = None
        try:
            return load_coinft_calibration_config(self.config.coinft_config)
        except Exception as exc:  # noqa: BLE001
            self.coinft_calibration_error = str(exc)
            print(f"CoinFT calibration config error: {exc}", flush=True)
            return raw_coinft_config()

    def coinft_calibration_manifest(self) -> dict[str, Any]:
        payload = self.coinft_calibration_config.to_manifest_dict()
        payload["load_error"] = self.coinft_calibration_error
        return payload

    def dataset_compatibility_signature(self) -> dict[str, Any]:
        calibration = self.coinft_calibration_manifest()
        slots = calibration.get("slots", {}) if isinstance(calibration.get("slots"), dict) else {}
        left = slots.get("left", {}) if isinstance(slots.get("left"), dict) else {}
        right = slots.get("right", {}) if isinstance(slots.get("right"), dict) else {}
        export = self.export_controller.snapshot()
        return {
            "schema_version": 1,
            "export_format": export.get("selected_format"),
            "export_profile": export.get("selected_profile"),
            "coinft_mode": calibration.get("mode"),
            "coinft_left_hardware": left.get("hardware_label"),
            "coinft_right_hardware": right.get("hardware_label"),
        }

    def dataset_switch_blockers(self) -> list[str]:
        with self.lock:
            state = copy.deepcopy(self.state)
        blockers = []
        if state.get("running"):
            blockers.append("iPhone receiver is running")
        if state.get("recording_control", {}).get("active"):
            blockers.append("an episode is recording")
        if state.get("coinft", {}).get("running") or state.get("teensy", {}).get("running"):
            blockers.append("CoinFT collector is running")
        if state.get("d435", {}).get("running") or state.get("d435_recording", {}).get("running"):
            blockers.append("D435 is running")
        if state.get("postprocess", {}).get("status") == "running":
            blockers.append("postprocess is running")
        if state.get("export_control", {}).get("status") == "exporting":
            blockers.append("export is running")
        return blockers

    def dataset_catalog(self) -> list[dict[str, Any]]:
        current_signature = self.dataset_compatibility_signature()
        with self.lock:
            selected_run_dir = self.state.get("run_dir")
        result = []
        for row in list_datasets(self.config.runs_root):
            issues = compatibility_issues(row.get("compatibility"), current_signature)
            result.append({
                **row,
                "selected": bool(selected_run_dir and Path(str(selected_run_dir)).resolve() == Path(row["run_dir"]).resolve()),
                "compatibility_issues": issues,
                "compatible": not issues,
            })
        return result

    def dataset_selection_state_path(self) -> Path:
        return self.config.runs_root / DATASET_SELECTION_STATE_FILE

    def persist_dataset_selection(self, run_dir: Path) -> None:
        write_json_atomic(
            self.dataset_selection_state_path(),
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "run_dir": str(run_dir),
                "updated_unix_ns": time.time_ns(),
            },
        )

    def restore_dataset_selection(self) -> None:
        payload = load_json_if_present(self.dataset_selection_state_path())
        value = payload.get("run_dir") or payload.get("run_id") if payload else None
        if not value:
            compatible = []
            current_signature = self.dataset_compatibility_signature()
            for row in list_datasets(self.config.runs_root):
                if not row.get("legacy") and not compatibility_issues(row.get("compatibility"), current_signature):
                    compatible.append(row)
            if len(compatible) == 1:
                value = compatible[0].get("run_dir")
        if not value:
            return
        try:
            run_dir = resolve_dataset_run(self.config.runs_root, str(value))
            summary = dataset_summary(run_dir)
            issues = compatibility_issues(summary.get("compatibility"), self.dataset_compatibility_signature())
            if issues:
                return
            self._activate_dataset(run_dir, issues=issues)
            self.log(f"Dataset restored: {summary.get('display_name') or run_dir.name} -> {run_dir}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"Dataset restore skipped: {exc}")

    def _activate_dataset(self, run_dir: Path, *, issues: list[str] | None = None) -> dict[str, Any]:
        paths = ensure_run_layout(run_dir)
        summary = dataset_summary(paths.run_dir)
        selection = {
            "selected": True,
            "run_id": paths.run_dir.name,
            "run_dir": str(paths.run_dir),
            "display_name": summary.get("display_name"),
            "task_name": summary.get("task_name"),
            "episode_count": summary.get("episode_count", 0),
            "latest_source_episode_index": summary.get("latest_source_episode_index"),
            "zarr_count": summary.get("zarr_count", 0),
            "compatibility_issues": issues or [],
        }
        self.patch_state(
            run_id=paths.run_dir.name,
            run_dir=str(paths.run_dir),
            run_manifest=str(paths.manifest_json),
            dataset_selection=selection,
            session_id=None,
            session_dir=None,
            stats={},
            alignment={},
            latest_event=None,
            derived={"normalized": None, "aligned": None},
            sync_quality={
                "ok": False,
                "status": "not_checked",
                "reasons": ["alignment has not been checked"],
                "checked_at_unix_ns": None,
            },
        )
        self.persist_dataset_selection(paths.run_dir)
        self.persist_coinft_calibration_config(paths.run_dir)
        return selection

    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        blockers = self.dataset_switch_blockers()
        if blockers:
            raise RuntimeError("cannot create Dataset: " + "; ".join(blockers))
        display_name = str(payload.get("display_name") or "").strip()
        task_name = str(payload.get("task_name") or display_name).strip()
        notes = str(payload.get("notes") or "").strip()
        if not display_name:
            raise ValueError("Dataset name is required")
        paths = create_run(
            runs_root=self.config.runs_root,
            task_name=display_name,
            operator=str(payload.get("operator") or "").strip(),
            notes=notes,
        )
        initialize_dataset_metadata(
            paths.run_dir,
            display_name=display_name,
            task_name=task_name,
            notes=notes,
            compatibility=self.dataset_compatibility_signature(),
        )
        selection = self._activate_dataset(paths.run_dir)
        append_sync_log(paths.run_dir, {"event": "dataset_created", "dataset": selection})
        self.log(f"Dataset created: {display_name} -> {paths.run_dir}")
        return {"dataset": dataset_summary(paths.run_dir), "selection": selection}

    def select_dataset(self, value: str) -> dict[str, Any]:
        blockers = self.dataset_switch_blockers()
        if blockers:
            raise RuntimeError("cannot switch Dataset: " + "; ".join(blockers))
        run_dir = resolve_dataset_run(self.config.runs_root, value)
        summary = dataset_summary(run_dir)
        issues = compatibility_issues(summary.get("compatibility"), self.dataset_compatibility_signature())
        if issues:
            raise RuntimeError("Dataset is incompatible with the current capture configuration: " + "; ".join(issues))
        selection = self._activate_dataset(run_dir, issues=issues)
        append_sync_log(run_dir, {"event": "dataset_selected", "dataset": selection})
        self.log(f"Dataset selected: {selection.get('display_name')} -> {run_dir}")
        return {"dataset": dataset_summary(run_dir), "selection": selection}

    def selected_dataset_run(self) -> Path:
        with self.lock:
            run_dir = self.state.get("run_dir")
            selected = bool(self.state.get("dataset_selection", {}).get("selected"))
        if not selected or not run_dir:
            raise RuntimeError("Select or create a Dataset before arming capture")
        resolved = Path(str(run_dir)).expanduser().resolve()
        summary = dataset_summary(resolved)
        issues = compatibility_issues(summary.get("compatibility"), self.dataset_compatibility_signature())
        if issues:
            raise RuntimeError("Selected Dataset is incompatible with the current capture configuration: " + "; ".join(issues))
        return resolved

    def reload_coinft_calibration_config(self) -> CoinFTCalibrationConfig:
        self.coinft_calibration_config = self.load_coinft_calibration_config_for_backend()
        return self.coinft_calibration_config

    def persist_coinft_calibration_config(self, run_dir: Path) -> None:
        manifest = read_json(run_dir / "RUN_MANIFEST.json", {})
        streams = manifest.get("streams", {}) if isinstance(manifest.get("streams"), dict) else {}
        coinft = streams.get("coinft", {}) if isinstance(streams.get("coinft"), dict) else {}
        existing = coinft.get("calibration") if isinstance(coinft.get("calibration"), dict) else None
        explicit_config = bool((self.config.coinft_config or "").strip())
        should_write = explicit_config or self.coinft_calibration_config.calibrated or self.coinft_calibration_error or not existing
        if not should_write:
            return
        update_manifest(
            run_dir,
            {
                "streams": {
                    "coinft": {
                        "calibration": self.coinft_calibration_manifest(),
                    }
                }
            },
        )

    def add_client(self) -> GuiClientMailbox:
        # GUI is a monitor, not a recording transport. Each live stream gets a
        # replaceable slot, so a slow browser can never replay old previews.
        with self.lock:
            self.state["capture_summary"] = self.build_capture_summary_locked()
            snapshot = dict(self.state)
            client = GuiClientMailbox(snapshot)
            self.clients.append(client)
        return client

    def remove_client(self, client: GuiClientMailbox) -> None:
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)

    def broadcast(self, message: dict[str, Any]) -> None:
        # State snapshots are large and every consumer only needs the newest one.
        # Serializing them per sensor sample can monopolize the GIL and delay the
        # iPhone receiver, so publish at a low monitor cadence.
        if message.get("type") == "state":
            now_monotonic_ns = time.monotonic_ns()
            with self._state_broadcast_lock:
                if now_monotonic_ns - self._last_state_broadcast_monotonic_ns < int(GUI_STATE_PUBLISH_INTERVAL_S * 1e9):
                    return
                self._last_state_broadcast_monotonic_ns = now_monotonic_ns
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.put(message)

    def update_coinft_state(self, updates: dict[str, Any]) -> None:
        with self.lock:
            base = self.state.get("coinft")
            if not isinstance(base, dict):
                base = self.state.get("teensy", {}) if isinstance(self.state.get("teensy"), dict) else {}
            state = {**base, **updates}
            self.state["coinft"] = state
            self.state["teensy"] = state
            self.state["capture_summary"] = self.build_capture_summary_locked()
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})

    def publish_coinft_sample(self, state: dict[str, Any], sample: dict[str, Any]) -> None:
        now_monotonic_ns = time.monotonic_ns()
        with self.lock:
            self.state["coinft"] = state
            self.state["teensy"] = state
            self.state["capture_summary"] = self.build_capture_summary_locked()
            publish_sample = (
                now_monotonic_ns - self._last_coinft_gui_publish_monotonic_ns
                >= int(COINFT_GUI_PUBLISH_INTERVAL_S * 1e9)
            )
            publish_state = (
                now_monotonic_ns - self._last_coinft_state_publish_monotonic_ns
                >= int(COINFT_STATE_PUBLISH_INTERVAL_S * 1e9)
            )
            if publish_sample:
                self._last_coinft_gui_publish_monotonic_ns = now_monotonic_ns
            if publish_state:
                self._last_coinft_state_publish_monotonic_ns = now_monotonic_ns
                snapshot = dict(self.state)
        if publish_state:
            self.broadcast({"type": "state", "state": snapshot})
        if publish_sample:
            self.broadcast({"type": "coinft_preview_sample", "sample": sample})

    def log(self, text: str) -> None:
        if not text:
            return
        entry = {"time": time.strftime("%H:%M:%S"), "text": text}
        with self.lock:
            logs = self.state.setdefault("logs", [])
            logs.append(entry)
            del logs[:-120]
        self.broadcast({"type": "log", "entry": entry})

    def patch_state(self, **updates: Any) -> None:
        with self.lock:
            self.state.update(updates)
            self.state["capture_summary"] = self.build_capture_summary_locked()
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})

    def state_snapshot(self) -> dict[str, Any]:
        self.d435_stream_snapshot(update_state=True)
        with self.lock:
            self.state["capture_summary"] = self.build_capture_summary_locked()
            return dict(self.state)

    def capture_options_snapshot(self) -> dict[str, Any]:
        with self.lock:
            options = dict(self.state.get("capture_options", {}))
        options.setdefault("d435_duration_s", 7200.0)
        options.setdefault("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S)
        options.setdefault("stream_buffer_s", DEFAULT_STREAM_BUFFER_S)
        return options

    def d435_control_script(self) -> Path:
        if INSTALLED_D435_CONTROL_SCRIPT.exists():
            return INSTALLED_D435_CONTROL_SCRIPT
        return D435_CONTROL_SCRIPT

    def recording_window_snapshot(self) -> dict[str, Any]:
        with self.lock:
            control = dict(self.state.get("recording_control", {}))
            options = dict(self.state.get("capture_options", {}))
        return {
            "active": bool(control.get("active")),
            "episode_index": control.get("episode_index") if isinstance(control.get("episode_index"), int) else 0,
            "event_start_aligned_monotonic_ns": control.get("started_at") if isinstance(control.get("started_at"), int) else None,
            "event_start_aligned_unix_ns": control.get("started_at_unix_ns") if isinstance(control.get("started_at_unix_ns"), int) else None,
            "recording_preroll_s": float(options.get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S)),
            "stream_buffer_s": float(options.get("stream_buffer_s", DEFAULT_STREAM_BUFFER_S)),
        }

    def build_capture_summary_locked(self) -> dict[str, Any]:
        state = self.state
        gate = state.get("recording_gate", {}) if isinstance(state.get("recording_gate"), dict) else {}
        streams = gate.get("streams", {}) if isinstance(gate.get("streams"), dict) else {}
        buffering = state.get("buffering", {}) if isinstance(state.get("buffering"), dict) else {}
        stats = state.get("stats", {}) if isinstance(state.get("stats"), dict) else {}
        control = state.get("recording_control", {}) if isinstance(state.get("recording_control"), dict) else {}
        pipeline = state.get("recording_pipeline", {}) if isinstance(state.get("recording_pipeline"), dict) else {}
        d435 = state.get("d435", {}) if isinstance(state.get("d435"), dict) else {}
        d435_recording = state.get("d435_recording", {}) if isinstance(state.get("d435_recording"), dict) else {}
        coinft = state.get("coinft", {}) if isinstance(state.get("coinft"), dict) else {}
        if not coinft:
            coinft = state.get("teensy", {}) if isinstance(state.get("teensy"), dict) else {}
        zarr_conversion = state.get("zarr_conversion", {}) if isinstance(state.get("zarr_conversion"), dict) else {}
        zarr_recording = state.get("zarr_recording", {}) if isinstance(state.get("zarr_recording"), dict) else {}
        iphone_stream = streams.get("iphone", {}) if isinstance(streams.get("iphone"), dict) else {}
        d435_stream = streams.get("d435", {}) if isinstance(streams.get("d435"), dict) else {}
        coinft_stream = streams.get("teensy", {}) if isinstance(streams.get("teensy"), dict) else {}

        def span_ms(buffer: Any, key: str = "span_ms") -> float | None:
            if not isinstance(buffer, dict):
                return None
            value = buffer.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        def count_value(buffer: Any, *keys: str) -> int | None:
            if not isinstance(buffer, dict):
                return None
            for key in keys:
                value = buffer.get(key)
                if isinstance(value, int):
                    return value
            return None

        ready = bool(gate.get("ok"))
        if control.get("active"):
            status = "recording"
        elif ready:
            status = "ready"
        elif control.get("armed"):
            status = "blocked"
        else:
            status = "idle"

        return {
            "status": status,
            "ready": ready,
            "updated_unix_ns": time.time_ns(),
            "gate_reasons": list(gate.get("reasons", [])) if isinstance(gate.get("reasons"), list) else [],
            "recording": {
                "armed": bool(control.get("armed")),
                "active": bool(control.get("active")),
                "episode_index": control.get("episode_index"),
                "blocked": bool(control.get("blocked")),
                "blocked_reason": control.get("blocked_reason"),
                "started_at": control.get("started_at"),
                "started_at_unix_ns": control.get("started_at_unix_ns"),
                "last_event": control.get("last_event"),
            },
            "pipeline": {
                "phase": pipeline.get("phase"),
                "transition_seq": pipeline.get("transition_seq"),
                "current_episode_index": pipeline.get("current_episode_index"),
                "canonical_event_time": pipeline.get("canonical_event_time"),
                "last_transition": pipeline.get("last_transition"),
                "post_start_check": pipeline.get("post_start_check"),
            },
            "zarr_conversion": {
                "status": zarr_conversion.get("status"),
                "enabled": bool(zarr_conversion.get("enabled")),
                "pose_mode": zarr_conversion.get("pose_mode"),
                "is_tcp_pose": bool(zarr_conversion.get("is_tcp_pose")),
                "converted_rows": zarr_conversion.get("converted_rows", 0),
                "completed_steps": zarr_conversion.get("completed_steps", 0),
                "recording_rows_observed": zarr_conversion.get("recording_rows_observed", 0),
                "recording_steps_observed": zarr_conversion.get("recording_steps_observed", 0),
                "last_error": zarr_conversion.get("last_error"),
            },
            "zarr_recording": {
                "status": zarr_recording.get("status"),
                "active": bool(zarr_recording.get("active")),
                "episode_index": zarr_recording.get("episode_index"),
                "rows_path": zarr_recording.get("rows_path"),
                "completed_rows_written": zarr_recording.get("completed_rows_written", 0),
                "tail_rows_written": zarr_recording.get("tail_rows_written", 0),
                "last_error": zarr_recording.get("last_error"),
            },
            "streams": {
                "iphone": {
                    "ok": bool(iphone_stream.get("ok")),
                    "running": bool(state.get("running")),
                    "status": state.get("status"),
                    "frame_count": stats.get("frame_count", 0),
                    "age_ms": iphone_stream.get("age_ms"),
                    "buffer_samples": count_value(buffering.get("iphone"), "samples"),
                    "buffer_span_ms": span_ms(buffering.get("iphone")),
                    "pose": iphone_stream.get("pose"),
                    "last_error": None,
                },
                "d435": {
                    "ok": bool(d435_stream.get("ok")),
                    "running": bool(d435_stream.get("running") or d435.get("running")),
                    "recording": bool(d435_recording.get("running")),
                    "pid": d435.get("pid") or d435_recording.get("pid"),
                    "age_ms": d435_stream.get("rgb_age_ms"),
                    "depth_age_ms": d435_stream.get("depth_age_ms"),
                    "fps": d435_stream.get("approx_fps"),
                    "buffer_frames": count_value(d435_stream.get("buffer") or buffering.get("d435"), "frames"),
                    "buffer_span_ms": span_ms(d435_stream.get("buffer") or buffering.get("d435"), "spanMs"),
                    "last_error": d435_stream.get("last_error") or d435.get("last_error"),
                },
                "coinft": {
                    "ok": bool(coinft_stream.get("ok")),
                    "running": bool(coinft.get("running")),
                    "source": coinft.get("source"),
                    "serial_port": coinft.get("serial_port"),
                    "calibration_mode": coinft.get("mode"),
                    "calibration_status": coinft.get("calibration_status"),
                    "calibration_ready": coinft.get("calibration_ready"),
                    "calibration_error": coinft.get("calibration_error"),
                    "stability_passes": coinft.get("stability_passes"),
                    "stability_required_passes": coinft.get("stability_required_passes"),
                    "stability_mean_delta_counts": coinft.get("stability_mean_delta_counts"),
                    "stability_noise_std_counts": coinft.get("stability_noise_std_counts"),
                    "zero_force_max_abs_n": coinft.get("zero_force_max_abs_n"),
                    "zero_force_limit_n": coinft.get("zero_force_limit_n"),
                    "fps": coinft.get("fps"),
                    "live_sample_count": coinft.get("live_sample_count", 0),
                    "recorded_sample_count": coinft.get("recorded_sample_count", 0),
                    "age_ms": coinft_stream.get("age_ms"),
                    "buffer_samples": count_value(buffering.get("teensy"), "samples"),
                    "buffer_span_ms": span_ms(buffering.get("teensy")),
                    "dropped_sequence_count": coinft.get("dropped_sequence_count"),
                    "timeout_count": coinft.get("timeout_count"),
                    "consecutive_timeout_count": coinft.get("consecutive_timeout_count"),
                    "recovery_count": coinft.get("recovery_count"),
                    "missing_side": coinft.get("missing_side"),
                    "active_ports": coinft.get("active_ports"),
                    "last_error": coinft_stream.get("last_error") or coinft.get("last_error"),
                },
            },
        }

    def update_buffer_state(self, stream: str, stats: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        with self.lock:
            buffering = dict(self.state.get("buffering", {}))
            buffering.setdefault("recording_preroll_s", self.state.get("capture_options", {}).get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S))
            buffering.setdefault("stream_buffer_s", self.state.get("capture_options", {}).get("stream_buffer_s", DEFAULT_STREAM_BUFFER_S))
            buffering[stream] = {**stats, **(extra or {})}
            self.state["buffering"] = buffering
            self.state["capture_summary"] = self.build_capture_summary_locked()
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})

    def publish_recording_transition(
        self,
        event_name: str,
        *,
        episode_index: int,
        event_aligned_monotonic_ns: int | None = None,
        event_aligned_unix_ns: int | None = None,
        gate: dict[str, Any] | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            run_dir = self.state.get("run_dir")
        transition = RecordingTransition(
            event=event_name,
            episode_index=episode_index,
            created_unix_ns=time.time_ns(),
            event_aligned_monotonic_ns=event_aligned_monotonic_ns,
            event_aligned_unix_ns=event_aligned_unix_ns,
            run_dir=str(run_dir) if run_dir else None,
            gate=gate or {},
            reason=reason,
            metadata=metadata or {},
        )
        transition_payload = transition.to_dict()
        with self.lock:
            pipeline = dict(self.state.get("recording_pipeline", {}))
            seq = int(pipeline.get("transition_seq") or 0) + 1
            transition_payload["seq"] = seq
            transitions = list(pipeline.get("transitions", []))
            transitions.append(transition_payload)
            del transitions[:-40]
            pipeline["transition_seq"] = seq
            pipeline["last_transition"] = transition_payload
            pipeline["transitions"] = transitions
            pipeline["current_episode_index"] = episode_index
            pipeline["canonical_event_time"] = {
                "event_aligned_monotonic_ns": event_aligned_monotonic_ns,
                "event_aligned_unix_ns": event_aligned_unix_ns,
                "source": transition.source,
            }
            if event_name in {"record_start_requested"}:
                pipeline["phase"] = "starting"
            elif event_name in {"record_start_committed", "record_start_postcheck_ok"}:
                pipeline["phase"] = "active"
            elif event_name in {"record_stop_requested"}:
                pipeline["phase"] = "stopping"
            elif event_name in {"record_stop_committed"}:
                pipeline["phase"] = "stopped"
            elif event_name in {"record_discard_requested"}:
                pipeline["phase"] = "discarding"
            elif event_name in {"record_discard_committed"}:
                pipeline["phase"] = "discarded"
            elif event_name in {"record_start_rejected", "record_start_failed", "record_start_postcheck_failed"}:
                pipeline["phase"] = "error"
            self.state["recording_pipeline"] = pipeline
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.broadcast(
            {
                "type": "recording_transition",
                "transition": transition_payload,
            }
        )
        self.observe_recording_transition_for_live_zarr(transition_payload)
        return transition_payload

    def observe_recording_transition_for_live_zarr(self, transition: dict[str, Any]) -> None:
        event = transition.get("event")
        episode_index = transition.get("episode_index")
        if not isinstance(episode_index, int):
            episode_index = 0
        if event == "record_start_committed":
            run_dir = self.ensure_active_run()
            writer_state = self.live_zarr_writer.start(
                run_dir=run_dir,
                episode_index=episode_index,
                transition=transition,
            )
            self.patch_state(zarr_recording=writer_state)
            self.log(f"live zarr row writer opened episode {episode_index}: {writer_state.get('rows_path')}")
            return
        if event == "record_discard_committed":
            writer_state = self.live_zarr_writer.discard(transition=transition)
            self.patch_state(zarr_recording=writer_state)
            return
        if event in {"record_stop_committed", "record_start_postcheck_failed", "record_start_failed"}:
            with self.lock:
                conversion = copy.deepcopy(self.state.get("zarr_conversion", {}))
            writer_state = self.live_zarr_writer.stop(transition=transition, conversion=conversion)
            self.patch_state(zarr_recording=writer_state)
            if writer_state.get("rows_path"):
                self.log(
                    "live zarr row writer closed "
                    f"rows={writer_state.get('completed_rows_written')} tail={writer_state.get('tail_rows_written')}"
                )

    def schedule_post_start_gate_check(
        self,
        *,
        episode_index: int,
        event_start_ns: int | None,
        event_start_unix_ns: int | None,
    ) -> None:
        token = f"{episode_index}:{time.time_ns()}"
        with self.lock:
            pipeline = dict(self.state.get("recording_pipeline", {}))
            pipeline["post_start_check"] = {
                "status": "scheduled",
                "delay_s": POST_START_GATE_CHECK_DELAY_S,
                "episode_index": episode_index,
                "event_start_aligned_monotonic_ns": event_start_ns,
                "event_start_aligned_unix_ns": event_start_unix_ns,
                "token": token,
                "checked_at_unix_ns": None,
                "reasons": [],
            }
            self.state["recording_pipeline"] = pipeline
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})

        def worker() -> None:
            time.sleep(POST_START_GATE_CHECK_DELAY_S)
            with self.lock:
                pipeline = dict(self.state.get("recording_pipeline", {}))
                check = dict(pipeline.get("post_start_check", {})) if isinstance(pipeline.get("post_start_check"), dict) else {}
                control = dict(self.state.get("recording_control", {}))
            if check.get("token") != token:
                return
            if not control.get("active") or control.get("episode_index") != episode_index:
                return
            gate = self.check_recording_gate()
            reasons = [str(reason) for reason in gate.get("reasons", [])]
            if gate.get("ok"):
                with self.lock:
                    pipeline = dict(self.state.get("recording_pipeline", {}))
                    pipeline["post_start_check"] = {
                        **dict(pipeline.get("post_start_check", {})),
                        "status": "ok",
                        "checked_at_unix_ns": time.time_ns(),
                        "reasons": [],
                    }
                    self.state["recording_pipeline"] = pipeline
                    snapshot = dict(self.state)
                self.broadcast({"type": "state", "state": snapshot})
                self.publish_recording_transition(
                    "record_start_postcheck_ok",
                    episode_index=episode_index,
                    event_aligned_monotonic_ns=event_start_ns,
                    event_aligned_unix_ns=event_start_unix_ns,
                    gate=gate,
                )
                return
            self.handle_post_start_gate_failure(
                episode_index=episode_index,
                event_start_ns=event_start_ns,
                event_start_unix_ns=event_start_unix_ns,
                gate=gate,
                reason_text="; ".join(reasons) or "post-start gate failed",
            )

        threading.Thread(target=worker, daemon=True).start()

    def handle_post_start_gate_failure(
        self,
        *,
        episode_index: int,
        event_start_ns: int | None,
        event_start_unix_ns: int | None,
        gate: dict[str, Any],
        reason_text: str,
    ) -> None:
        with self.lock:
            current_control = dict(self.state.get("recording_control", {}))
        self.patch_state(
            recording_control={
                **current_control,
                "active": False,
                "episode_index": episode_index,
                "last_event": "record_start_postcheck_failed",
                "blocked": True,
                "blocked_reason": reason_text,
                "source": "post_start_gate_check",
            },
            recording_gate={
                **gate,
                "ok": False,
                "status": "blocked",
                "reasons": gate.get("reasons", []) or [reason_text],
            },
        )
        with self.lock:
            pipeline = dict(self.state.get("recording_pipeline", {}))
            pipeline["post_start_check"] = {
                **dict(pipeline.get("post_start_check", {})),
                "status": "failed",
                "checked_at_unix_ns": time.time_ns(),
                "reasons": gate.get("reasons", []) or [reason_text],
            }
            self.state["recording_pipeline"] = pipeline
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.log(f"recording post-start gate failed: {reason_text}")
        self.publish_recording_transition(
            "record_start_postcheck_failed",
            episode_index=episode_index,
            event_aligned_monotonic_ns=event_start_ns,
            event_aligned_unix_ns=event_start_unix_ns,
            gate=gate,
            reason=reason_text,
        )
        self.stop_d435_recording()

    def handle_main_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command", "")).strip()
        args = payload.get("args", {})
        if not isinstance(args, dict):
            args = {}
        if command == "ensure_run":
            run_dir = self.ensure_active_run(args.get("run_dir", ""), args.get("run_id", ""))
            return {"ok": True, "command": command, "run_dir": str(run_dir), "run_id": run_dir.name}
        if command == "arm_capture":
            run_dir = self.selected_dataset_run()
            return {"ok": True, "command": command, **self.arm_capture({**args, "run_dir": str(run_dir)})}
        if command == "arm_all":
            run_dir = self.selected_dataset_run()
            result = self.arm_capture({**args, "run_dir": str(run_dir)})
            return {"ok": True, "command": command, "run_dir": str(run_dir), "run_id": run_dir.name, **result}
        if command == "create_dataset":
            return {"ok": True, "command": command, **self.create_dataset(args)}
        if command == "select_dataset":
            return {"ok": True, "command": command, **self.select_dataset(str(args.get("run_dir") or args.get("run_id") or ""))}
        if command == "stop_capture":
            return {"ok": True, "command": command, **self.stop_capture()}
        if command == "start_coinft":
            with self.lock:
                coinft_running = self.teensy_worker is not None
                recording_active = bool(self.state.get("recording_control", {}).get("active"))
            if recording_active:
                return {
                    "ok": False,
                    "command": command,
                    "error": "Cannot re-zero CoinFT while an Episode is recording",
                }
            if coinft_running:
                result = self.restart_coinft()
                return {"command": command, **result}
            return {"ok": True, "command": command, "started": self.start_coinft()}
        if command == "stop_coinft":
            return {"ok": True, "command": command, "stopped": self.stop_teensy()}
        if command == "activate_coinft_model_folder":
            result = self.activate_coinft_model_folder(args)
            return {"ok": True, "command": command, **result}
        if command == "start_d435_preview":
            started = self.start_d435_preview()
            snapshot = self.d435_stream_snapshot()
            action = "started" if started else ("already_running" if snapshot.get("ok") else "failed")
            return {
                "ok": True,
                "command": command,
                "started": started,
                "ready": bool(snapshot.get("ok")),
                "action": action,
                "error": snapshot.get("last_error"),
            }
        if command == "stop_d435":
            return {"ok": True, "command": command, "stopped": self.stop_d435_preview()}
        if command == "normalize":
            return {"ok": True, "command": command, "report": self.normalize_current_run(args)}
        if command == "align":
            return {"ok": True, "command": command, "report": self.align_current_run(args)}
        if command in {"configure_export", "set_export_control"}:
            return {"ok": True, "command": command, "export_control": self.configure_export_control(args)}
        if command == "export_selected":
            manifest = self.export_selected_current_run(args)
            return {"ok": "error" not in manifest, "command": command, "manifest": manifest}
        if command == "open_output":
            with self.lock:
                path = self.state.get("run_dir") or self.state.get("session_dir") or str(self.config.output_root)
            open_directory(path)
            return {"ok": True, "command": command, "path": path}
        return {"ok": False, "command": command, "error": f"unknown main command: {command}"}

    def activate_coinft_model_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        model_dir = str(args.get("model_dir", "")).strip()
        left_model_path = str(args.get("left_model_path", "")).strip()
        left_norm_path = str(args.get("left_norm_path", "")).strip()
        right_model_path = str(args.get("right_model_path", "")).strip()
        right_norm_path = str(args.get("right_norm_path", "")).strip()
        explicit_paths = (left_model_path, left_norm_path, right_model_path, right_norm_path)
        if any(explicit_paths) and not all(explicit_paths):
            raise ValueError("All four CoinFT left/right ONNX and norm paths are required")
        model_set_id = str(args.get("model_set_id", "")).strip() or None
        if all(explicit_paths):
            selected = coinft_calibration_config_from_files(
                left_model_path=self.local_model_path(left_model_path),
                left_norm_path=self.local_model_path(left_norm_path),
                right_model_path=self.local_model_path(right_model_path),
                right_norm_path=self.local_model_path(right_norm_path),
                left_hardware_label=str(args.get("left_hardware_label", "")).strip() or None,
                right_hardware_label=str(args.get("right_hardware_label", "")).strip() or None,
                model_set_id=model_set_id,
            )
        elif model_dir:
            selected_path = self.local_model_path(model_dir)
            if selected_path.is_dir() and not (selected_path / "model_set.json").exists():
                selected = coinft_calibration_config_from_side_dirs(
                    left_dir=selected_path / "left",
                    right_dir=selected_path / "right",
                    model_set_id=model_set_id or selected_path.name,
                )
            else:
                selected = load_coinft_calibration_config(selected_path)
        else:
            left_dir = str(args.get("left_dir", "")).strip()
            right_dir = str(args.get("right_dir", "")).strip()
            if not left_dir or not right_dir:
                raise ValueError("CoinFT model folder or four left/right model paths are required")
            selected = coinft_calibration_config_from_side_dirs(
                left_dir=self.local_model_path(left_dir),
                right_dir=self.local_model_path(right_dir),
                model_set_id=model_set_id,
            )
        self.coinft_calibration_error = None
        self.coinft_calibration_config = selected
        self.postprocess.coinft_config = selected
        result = {
            "model_set_id": selected.model_set_id,
            "model_set_path": str(selected.model_set_path or selected.config_path or ""),
            "mode": selected.mode,
            "slots": {
                side: slot.to_manifest_dict()
                for side, slot in (selected.slots or {}).items()
            },
        }
        updates = {
            **calibration_status_summary(self.coinft_calibration_config),
            "calibration_status": "configured" if self.coinft_calibration_config.calibrated else "raw_only",
            "calibration_ready": not self.coinft_calibration_config.calibrated,
            "calibration_error": self.coinft_calibration_error,
        }
        self.update_coinft_state(updates)
        if self.teensy_worker is not None:
            self.log("CoinFT model selection activated; restart CoinFT collector to use the new runtime.")
            result["restart_required"] = True
        else:
            result["restart_required"] = False
        self.log(f"CoinFT model selection activated from GUI path: {result.get('model_set_id')}")
        return result

    def local_model_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return MODULE04_ROOT / path

    def refresh_preflight_state(self) -> dict[str, Any]:
        gate = self.check_recording_gate()
        self.patch_state(recording_gate=gate)
        return gate

    def refresh_d435_runtime(self) -> None:
        with self.lock:
            process = self.d435_process
            log_handle = self.d435_log_handle
        if process is None or process.poll() is None:
            return

        returncode = process.returncode
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass

        log_tail = tail_text(D435_STREAM_LOG)
        last_error = d435_error_from_log_tail(log_tail)
        snapshot = None
        with self.lock:
            if self.d435_process is process:
                self.d435_process = None
                self.d435_log_handle = None
                self.d435_started_monotonic_s = None
                self.state["d435"] = {
                    "running": False,
                    "pid": None,
                    "mode": "continuous_stream",
                    "preview_dir": str(self.config.d435_preview_dir),
                    "log": str(D435_STREAM_LOG),
                    "control_file": str(D435_RECORD_CONTROL_FILE),
                    "status_file": str(D435_STREAM_STATUS_FILE),
                    "last_exit_code": returncode,
                    "last_error": last_error,
                }
                self.state["d435_recording"] = {
                    **self.state.get("d435_recording", {}),
                    "running": False,
                    "pid": None,
                    "log": str(D435_STREAM_LOG),
                    "last_exit_code": returncode,
                    "last_error": last_error,
                }
                snapshot = dict(self.state)
        if snapshot is not None:
            self.broadcast({"type": "state", "state": snapshot})
            suffix = f": {last_error}" if last_error else ""
            self.log(f"D435 preview exited code={returncode}{suffix}")

    def check_recording_gate(self) -> dict[str, Any]:
        self.refresh_d435_runtime()
        now_monotonic_ns = time.monotonic_ns()
        now_unix_ns = time.time_ns()
        reasons: list[str] = []
        streams: dict[str, Any] = {}

        with self.lock:
            state = dict(self.state)
            control = dict(state.get("recording_control", {}))
            stats = dict(state.get("stats", {}))
            d435 = dict(state.get("d435", {}))
            d435_recording = dict(state.get("d435_recording", {}))
            teensy = dict(state.get("teensy", {}))
            alignment = dict(state.get("alignment", {}))
            latest_iphone_pose = dict(state.get("latest_iphone_pose", {})) if isinstance(state.get("latest_iphone_pose"), dict) else {}

        if not control.get("armed"):
            reasons.append("capture is not armed on the Mac")

        iphone_latest_ns = state.get("latest_iphone_sample_monotonic_ns")
        iphone_age_ms = None
        iphone_ok = False
        if isinstance(iphone_latest_ns, int):
            iphone_age_ms = (now_monotonic_ns - iphone_latest_ns) / 1e6
            iphone_ok = iphone_age_ms <= IPHONE_FRESH_MAX_S * 1000.0
        if not state.get("running"):
            reasons.append("iPhone receiver is not running")
        if not stats.get("frame_count"):
            reasons.append("iPhone stream has not delivered preview frames yet")
        elif not iphone_ok:
            reasons.append(f"iPhone stream is stale ({fmt_ms(iphone_age_ms)})")
        if alignment.get("status") not in {"receiver_aligned", "estimating"}:
            reasons.append(f"iPhone time alignment is not ready ({alignment.get('status') or 'missing'})")
        if not latest_iphone_pose.get("world_calibrated"):
            reasons.append(
                "iPhone world origin is not calibrated"
                f" ({latest_iphone_pose.get('world_origin_status') or 'missing'})"
            )
        elif not latest_iphone_pose.get("valid"):
            reasons.append("iPhone user-world pose is invalid")
        streams["iphone"] = {
            "ok": bool(state.get("running") and stats.get("frame_count") and iphone_ok),
            "frame_count": stats.get("frame_count", 0),
            "age_ms": iphone_age_ms,
            "alignment_status": alignment.get("status"),
            "pose": latest_iphone_pose,
        }

        d435_snapshot = self.d435_stream_snapshot(now_unix_ns=now_unix_ns)
        if not d435_snapshot.get("running"):
            reasons.append("D435 preview/recording process is not running")
        if d435_snapshot.get("rgb_age_ms") is None or d435_snapshot.get("depth_age_ms") is None:
            reasons.append("D435 preview images are missing")
        elif not d435_snapshot.get("files_fresh"):
            if d435_snapshot.get("startup_grace"):
                reasons.append("D435 preview is warming up")
            else:
                reasons.append(
                    "D435 preview is stale "
                    f"(rgb {fmt_ms(d435_snapshot.get('rgb_age_ms'))}, "
                    f"depth {fmt_ms(d435_snapshot.get('depth_age_ms'))})"
                )
        if d435_snapshot.get("last_error"):
            reasons.append(f"D435 stream error: {d435_snapshot.get('last_error')}")
        streams["d435"] = {
            "ok": bool(d435_snapshot.get("ok")),
            "running": bool(d435_snapshot.get("running")),
            "process_running": bool(d435_snapshot.get("process_running")),
            "status_running": bool(d435_snapshot.get("status_running")),
            "status_fresh": bool(d435_snapshot.get("status_fresh")),
            "files_fresh": bool(d435_snapshot.get("files_fresh")),
            "files_streaming": bool(d435_snapshot.get("files_streaming")),
            "startup_grace": bool(d435_snapshot.get("startup_grace")),
            "startup_age_ms": d435_snapshot.get("startup_age_ms"),
            "pid": d435_snapshot.get("pid"),
            "rgb_age_ms": d435_snapshot.get("rgb_age_ms"),
            "depth_age_ms": d435_snapshot.get("depth_age_ms"),
            "status_age_ms": d435_snapshot.get("status_age_ms"),
            "approx_fps": d435_snapshot.get("approx_fps"),
            "recording_active": d435_snapshot.get("recording_active"),
            "recording_output_dir": d435_snapshot.get("recording_output_dir"),
            "recording_frame_count": d435_snapshot.get("recording_frame_count"),
            "buffer": d435_snapshot.get("buffer"),
            "acquisition": d435_snapshot.get("acquisition"),
            "last_error": d435_snapshot.get("last_error"),
        }

        teensy_latest_ns = teensy.get("latest_sample_monotonic_ns")
        teensy_age_ms = None
        teensy_ok = False
        if isinstance(teensy_latest_ns, int):
            teensy_age_ms = (now_monotonic_ns - teensy_latest_ns) / 1e6
            teensy_ok = teensy_age_ms <= TEENSY_FRESH_MAX_S * 1000.0
        if not teensy.get("running"):
            reasons.append("Teensy stream is not running")
        if not teensy.get("live_sample_count"):
            reasons.append("Teensy stream has not delivered samples yet")
        elif not teensy_ok:
            reasons.append(f"Teensy stream is stale ({fmt_ms(teensy_age_ms)})")
        if teensy.get("last_error"):
            reasons.append(f"Teensy stream error: {teensy.get('last_error')}")
        if teensy.get("missing_side"):
            reasons.append(f"CoinFT missing side: {teensy.get('missing_side')}")
        calibration_required = self.coinft_calibration_config.calibrated
        calibration_ready = bool(teensy.get("calibration_ready")) if calibration_required else True
        calibration_status = str(teensy.get("calibration_status") or "unknown")
        if calibration_required and not calibration_ready:
            calibration_error = teensy.get("calibration_error")
            if calibration_error:
                reasons.append(str(calibration_error))
            else:
                reasons.append(f"CoinFT startup zeroing is not ready ({calibration_status})")
        streams["teensy"] = {
            "ok": bool(
                teensy.get("running")
                and teensy.get("live_sample_count")
                and teensy_ok
                and not teensy.get("last_error")
                and not teensy.get("missing_side")
                and calibration_ready
            ),
            "source": teensy.get("source"),
            "serial_port": teensy.get("serial_port"),
            "live_sample_count": teensy.get("live_sample_count", 0),
            "age_ms": teensy_age_ms,
            "last_error": teensy.get("last_error"),
            "missing_side": teensy.get("missing_side"),
            "active_ports": teensy.get("active_ports"),
            "calibration_status": calibration_status,
            "calibration_ready": calibration_ready,
            "zero_force_max_abs_n": teensy.get("zero_force_max_abs_n"),
            "zero_force_limit_n": teensy.get("zero_force_limit_n"),
        }

        if isinstance(iphone_latest_ns, int) and isinstance(teensy_latest_ns, int):
            skew_ms = abs(iphone_latest_ns - teensy_latest_ns) / 1e6
            streams["live_skew"] = {"iphone_teensy_ms": skew_ms}
            if skew_ms > LIVE_STREAM_SKEW_MAX_S * 1000.0:
                reasons.append(f"iPhone and Teensy live clocks are too far apart ({skew_ms:.1f} ms)")

        ok = not reasons
        return {
            "ok": ok,
            "status": "ready" if ok else "blocked",
            "reasons": reasons,
            "streams": streams,
            "checked_at_unix_ns": now_unix_ns,
        }

    def observe_iphone_frame_for_gate(self, normalized: dict[str, Any]) -> None:
        timestamp = normalized.get("timestamp", {}) if isinstance(normalized.get("timestamp"), dict) else {}
        mac_receive_ns = timestamp.get("mac_receive_monotonic_ns")
        pose = normalized.get("pose", {}) if isinstance(normalized.get("pose"), dict) else {}
        if isinstance(mac_receive_ns, int):
            with self.lock:
                self.state["latest_iphone_sample_monotonic_ns"] = mac_receive_ns
                self.state["latest_iphone_pose"] = {
                    "valid": bool(pose.get("valid")),
                    "frame": pose.get("frame"),
                    "world_calibrated": bool(pose.get("world_calibrated")),
                    "world_origin_status": pose.get("world_origin_status"),
                }

    def observe_live_zarr_conversion(self, normalized: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            gate = dict(self.state.get("recording_gate", {})) if isinstance(self.state.get("recording_gate"), dict) else {}
            control = dict(self.state.get("recording_control", {})) if isinstance(self.state.get("recording_control"), dict) else {}
        conversion = self.live_zarr_conversion.observe_iphone_frame(
            normalized,
            recording_gate=gate,
            recording_control=control,
        )
        writer_state = self.live_zarr_writer.observe_conversion(conversion)
        with self.lock:
            self.state["zarr_conversion"] = conversion
            self.state["zarr_recording"] = writer_state
            self.state["capture_summary"] = self.build_capture_summary_locked()
        return conversion

    def evaluate_normalization_quality(self, report: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        iphone = report.get("iphone", {}) if isinstance(report.get("iphone"), dict) else {}
        d435 = report.get("d435", {}) if isinstance(report.get("d435"), dict) else {}
        coinft = report.get("coinft", {}) if isinstance(report.get("coinft"), dict) else {}
        if int(iphone.get("frame_count") or 0) <= 0:
            reasons.append("normalized iPhone frames are missing")
        if bool(d435.get("missing")) or int(d435.get("frame_count") or 0) <= 0:
            reasons.append("normalized D435 frames are missing")
        d435_integrity = d435.get("sensor_integrity", {}) if isinstance(d435.get("sensor_integrity"), dict) else {}
        if d435_integrity and not bool(d435_integrity.get("ok")):
            reasons.append(
                "D435 sensor frame sequence is discontinuous "
                f"(missing={int(d435_integrity.get('missing_frames') or 0)}, "
                f"out_of_order={int(d435_integrity.get('out_of_order_frames') or 0)})"
            )
        if int(coinft.get("sample_count") or 0) <= 0:
            reasons.append("normalized Teensy/CoinFT samples are missing")
        return {
            "ok": not reasons,
            "status": "ready" if not reasons else "blocked",
            "stage": "normalize",
            "reasons": reasons,
            "checked_at_unix_ns": time.time_ns(),
        }

    def evaluate_alignment_quality(self, report: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        inputs = report.get("inputs", {}) if isinstance(report.get("inputs"), dict) else {}
        counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
        timeline_rows = int(counts.get("timeline_rows") or 0)
        episodes = int(report.get("episodes") or 0)
        if episodes <= 0:
            reasons.append("aligned episodes are missing")
        if timeline_rows <= 0:
            reasons.append("aligned timeline has no rows")
        for key, label in (("iphone_frames", "iPhone"), ("d435_frames", "D435"), ("coinft_samples", "Teensy/CoinFT")):
            if int(inputs.get(key) or 0) <= 0:
                reasons.append(f"{label} input is missing before alignment")
        ratios: dict[str, float] = {}
        if timeline_rows > 0:
            for key, label in (
                ("iphone_valid", "iPhone"),
                ("d435_valid", "D435"),
                ("coinft_valid", "Teensy/CoinFT"),
                ("all_valid", "all streams"),
            ):
                ratio = float(counts.get(key) or 0) / float(timeline_rows)
                ratios[key] = ratio
                if ratio < ALIGNMENT_MIN_VALID_RATIO:
                    reasons.append(f"{label} valid alignment ratio is too low ({ratio:.1%})")
            timeline = report.get("timeline", {}) if isinstance(report.get("timeline"), dict) else {}
            if timeline.get("mode") == "target-fps":
                duplicate_ratio = float(counts.get("iphone_duplicate_targets") or 0) / float(timeline_rows)
                ratios["iphone_duplicate_targets"] = duplicate_ratio
                if duplicate_ratio > 0.05:
                    reasons.append(f"iPhone target-fps timeline reuses too many frames ({duplicate_ratio:.1%})")
        return {
            "ok": not reasons,
            "status": "ready" if not reasons else "blocked",
            "stage": "align",
            "reasons": reasons,
            "ratios": ratios,
            "checked_at_unix_ns": time.time_ns(),
        }

    def ensure_active_run(self, run_dir_text: str = "", run_id: str = "") -> Path:
        run_dir_text = (run_dir_text or "").strip()
        run_id = (run_id or "").strip()
        with self.lock:
            current = self.state.get("run_dir")
        if run_dir_text:
            paths = ensure_run_layout(Path(run_dir_text).expanduser().resolve())
        elif current:
            paths = ensure_run_layout(Path(str(current)).expanduser().resolve())
        else:
            paths = create_run(runs_root=self.config.runs_root, run_id=run_id or None)

        self.patch_state(
            run_id=paths.run_dir.name,
            run_dir=str(paths.run_dir),
            run_manifest=str(paths.manifest_json),
            dataset_selection={
                "selected": True,
                "run_id": paths.run_dir.name,
                "run_dir": str(paths.run_dir),
                "display_name": dataset_summary(paths.run_dir).get("display_name"),
                "compatibility_issues": [],
            },
        )
        self.persist_coinft_calibration_config(paths.run_dir)
        return paths.run_dir

    def reserve_recording_episode_index(self, run_dir: Path, requested_index: Any) -> int:
        with self.lock:
            control = dict(self.state.get("recording_control", {}))
            current_index = control.get("episode_index")
        with self.episode_allocation_lock:
            episode_index = reserve_next_episode_index(
                run_dir,
                requested_index=requested_index if isinstance(requested_index, int) else None,
                current_index=current_index if isinstance(current_index, int) else None,
            )
        self.log(
            "reserved episode "
            f"{episode_index} (iPhone requested {requested_index if isinstance(requested_index, int) else 'none'})"
        )
        return episode_index

    def start_receiver(self, overrides: dict[str, Any] | None = None) -> bool:
        with self.lock:
            if self.worker is not None:
                return False
        overrides = overrides or {}
        run_dir = self.ensure_active_run(overrides.get("run_dir", ""), overrides.get("run_id", ""))
        stop_event = threading.Event()
        worker = GuiReceiverV2(
            callbacks=self,
            stop_event=stop_event,
            output_root=run_dir / "raw" / "iphone_stream",
            transport=str(overrides.get("transport", "usb")),
            device=str(overrides.get("device", "")),
            device_port=int(overrides.get("device_port", DEFAULT_DEVICE_PORT)),
            host=str(overrides.get("host", DEFAULT_TCP_HOST)),
            port=int(overrides.get("port", DEFAULT_TCP_PORT)),
            retry_s=float(overrides.get("retry_s", 1.0)),
            ack_interval_frames=int(overrides.get("ack_interval_frames", 3)),
        )
        with self.lock:
            self.worker = worker
            self.stop_event = stop_event
        self.patch_state(running=True, status="starting", started_wall_s=time.monotonic())
        self.log("iPhone H.264 V2 receiver starting")
        worker.start()
        return True

    def arm_capture(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.capture_lifecycle_lock:
            return self._arm_capture(overrides)

    def _arm_capture(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}
        run_dir = self.ensure_active_run(overrides.get("run_dir", ""), overrides.get("run_id", ""))
        d435_duration_s = float(overrides.get("d435_duration_s", overrides.get("duration_s", 7200.0)))
        recording_preroll_s = float(overrides.get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S))
        stream_buffer_s = max(float(overrides.get("stream_buffer_s", DEFAULT_STREAM_BUFFER_S)), recording_preroll_s + 0.5)
        self.patch_state(
            capture_options={
                "d435_duration_s": d435_duration_s,
                "recording_preroll_s": recording_preroll_s,
                "stream_buffer_s": stream_buffer_s,
            },
            buffering={
                **self.state.get("buffering", {}),
                "recording_preroll_s": recording_preroll_s,
                "stream_buffer_s": stream_buffer_s,
            },
            recording_control={
                **self.state.get("recording_control", {}),
                "armed": False,
                "active": False,
                "last_event": "arming",
                "blocked": False,
                "blocked_reason": None,
            }
        )
        receiver_started = self.start_receiver({**overrides, "run_dir": str(run_dir)})
        with self.lock:
            coinft_running = self.teensy_worker is not None
        if coinft_running:
            coinft_restart = self.restart_coinft()
            teensy_started = bool(coinft_restart.get("started"))
        else:
            coinft_restart = None
            teensy_started = self.start_coinft()
        d435_preview_started = self.start_d435_preview()
        d435_snapshot = self.d435_stream_snapshot()
        if d435_preview_started or (d435_snapshot.get("running") and not d435_snapshot.get("ok")):
            d435_snapshot = self.wait_for_d435_stream_ready()
        if d435_snapshot.get("ok"):
            d435_action = "started" if d435_preview_started else "already_running"
        elif d435_snapshot.get("running"):
            d435_action = "running_unhealthy"
        else:
            d435_action = "failed"
        d435_ready = bool(d435_snapshot.get("ok"))
        if d435_ready:
            self.patch_state(
                recording_control={
                    **self.state.get("recording_control", {}),
                    "armed": True,
                    "active": False,
                    "last_event": "armed",
                    "blocked": False,
                    "blocked_reason": None,
                }
            )
            self.log("capture armed; iPhone record button controls disk recording")
        else:
            d435_error = d435_snapshot.get("last_error") or "D435 did not deliver RGB/depth preview frames"
            self.patch_state(
                recording_control={
                    **self.state.get("recording_control", {}),
                    "armed": False,
                    "active": False,
                    "last_event": "arm_failed",
                    "blocked": True,
                    "blocked_reason": d435_error,
                }
            )
            self.log(f"capture arm failed: {d435_error}")
        gate = self.refresh_preflight_state()
        return {
            "run_dir": str(run_dir),
            "receiver_started": receiver_started,
            "teensy_started": teensy_started,
            "coinft_restart": coinft_restart,
            "d435_preview_started": d435_preview_started,
            "armed": d435_ready,
            "d435_ready": d435_ready,
            "d435_action": d435_action,
            "d435_error": d435_snapshot.get("last_error"),
            "recording_gate": gate,
        }

    def stop_capture(self) -> dict[str, bool]:
        with self.capture_lifecycle_lock:
            result = {
                "receiver_stopped": self.stop_receiver(),
                "coinft_stopped": self.stop_teensy(),
                "d435_stopped": self.stop_d435_preview(),
            }
            self.patch_state(
                recording_control={
                    **self.state.get("recording_control", {}),
                    "armed": False,
                    "active": False,
                    "last_event": "stopped",
                    "blocked": False,
                    "blocked_reason": None,
                }
            )
            self.refresh_preflight_state()
            return result

    def wait_for_d435_stream_ready(self, *, timeout_s: float = D435_STARTUP_GRACE_S) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        snapshot = self.d435_stream_snapshot()
        while not snapshot.get("ok") and time.monotonic() < deadline:
            if snapshot.get("last_error") and not snapshot.get("running"):
                break
            time.sleep(0.1)
            snapshot = self.d435_stream_snapshot()
        return snapshot

    def stop_receiver(self) -> bool:
        with self.lock:
            worker = self.worker
            stop_event = self.stop_event
        if worker is None or stop_event is None:
            return False
        self.log("receiver stopping")
        worker.request_stop()
        worker.join(timeout=5)
        if worker.is_alive():
            self.log("iPhone V2 receiver did not stop within 5 seconds")
        return True

    def iphone_v2_status(self, status: str) -> None:
        self.patch_state(status=status)

    def iphone_v2_log(self, text: str) -> None:
        self.log(text)

    def iphone_v2_finished(self) -> None:
        self.worker_finished()

    def archive_session_started(
        self,
        receiver: ArchiveReceiverV2,
        packet: Packet,
        session: SessionStart,
    ) -> None:
        assert receiver.session_dir is not None
        self.handle_iphone_source_session_change(packet.session_id)
        self._iphone_v2_min_offset_ns = None
        self._iphone_v2_alignment_samples = 0
        self._iphone_v2_clock_model = None
        self._iphone_v2_first_receive_monotonic_ns = None
        writer = receiver.writer
        self._iphone_v2_initial_frame_count = writer.frame_count if writer is not None else 0
        self._iphone_v2_initial_encoded_bytes = writer.encoded_bytes if writer is not None else 0
        with self.lock:
            run_dir_value = self.state.get("run_dir")
        run_dir = Path(str(run_dir_value)).resolve() if run_dir_value else None
        self.patch_state(
            session_id=packet.session_id,
            session_dir=str(receiver.session_dir),
            raw_jsonl=None,
            aligned_jsonl=None,
            latest_iphone_sample_monotonic_ns=None,
            stats={
                "frame_count": self._iphone_v2_initial_frame_count,
                "event_count": 0,
                "dropped_sequence_count": 0,
                "last_sequence": receiver.last_acked_sequence,
                "encoded_bytes": self._iphone_v2_initial_encoded_bytes,
            },
            alignment={"method": "bidirectional_clock_sync", "status": "estimating"},
            iphone_video={
                "protocol_version": 2,
                "codec": "h264",
                "width": session.width,
                "height": session.height,
                "frames_per_second": session.frames_per_second,
                "average_bit_rate": session.average_bit_rate,
                "preview_ready": False,
                "preview_dir": str(receiver.session_dir / "preview"),
                "last_sequence": receiver.last_acked_sequence,
                "encoded_bytes": self._iphone_v2_initial_encoded_bytes,
                "fps": 0.0,
                "mbps": 0.0,
            },
        )
        if run_dir is not None:
            register_artifact(
                run_dir,
                stream="iphone",
                role="raw_session_dir",
                path=receiver.session_dir,
                metadata={
                    "session_id": packet.session_id,
                    "transport": "protocol_v2",
                    "codec": "h264",
                    "kind": "directory",
                },
            )
            append_sync_log(
                run_dir,
                {
                    "event": "iphone_v2_receiver_started",
                    "stream": "iphone",
                    "session_id": packet.session_id,
                    "session_dir": str(receiver.session_dir),
                },
            )
        self.log(f"iPhone V2 session {packet.session_id}")

    def handle_iphone_source_session_change(self, new_session_id: str) -> int | None:
        with self.lock:
            previous_session_id = self.state.get("session_id")
            control = dict(self.state.get("recording_control", {}))
            run_dir_value = self.state.get("run_dir")
        if (
            not previous_session_id
            or previous_session_id == new_session_id
            or not control.get("active")
        ):
            return None
        episode_index = (
            int(control["episode_index"])
            if isinstance(control.get("episode_index"), int)
            else 0
        )
        reason = (
            f"iPhone source session changed from {previous_session_id} "
            f"to {new_session_id} during recording"
        )
        self.patch_state(
            recording_control={
                **control,
                "active": False,
                "last_event": "source_session_lost",
                "blocked": True,
                "blocked_reason": reason,
                "source": "iphone_recording_event",
            }
        )
        run_dir = Path(str(run_dir_value)).resolve() if run_dir_value else None
        self.iphone_event_executor.submit(
            self.finalize_iphone_source_session_loss,
            episode_index,
            str(previous_session_id),
            new_session_id,
            reason,
            run_dir,
        )
        return episode_index

    def finalize_iphone_source_session_loss(
        self,
        episode_index: int,
        previous_session_id: str,
        new_session_id: str,
        reason: str,
        run_dir: Path | None,
    ) -> None:
        d435_stopped = self.stop_d435_recording()
        self.publish_recording_transition(
            "recording_source_session_lost",
            episode_index=episode_index,
            reason=reason,
            metadata={
                "previous_session_id": previous_session_id,
                "new_session_id": new_session_id,
                "d435_stopped": d435_stopped,
            },
        )
        if run_dir is not None:
            append_sync_log(
                run_dir,
                {
                    "event": "recording_source_session_lost",
                    "episode_index": episode_index,
                    "previous_session_id": previous_session_id,
                    "new_session_id": new_session_id,
                    "reason": reason,
                    "d435_stopped": d435_stopped,
                },
            )
        self.log(reason)

    def archive_codec_configured(
        self,
        receiver: ArchiveReceiverV2,
        _packet: Packet,
        config: VideoCodecConfig,
    ) -> None:
        with self.lock:
            current = dict(self.state.get("iphone_video", {}))
        self.patch_state(
            iphone_video={
                **current,
                "codec": "h264",
                "width": config.width,
                "height": config.height,
                "frames_per_second": config.frames_per_second,
                "average_bit_rate": config.average_bit_rate,
                "nal_unit_header_length": config.nal_unit_header_length,
                "parameter_set_count": len(config.parameter_sets),
                "preview_dir": str(receiver.session_dir / "preview") if receiver.session_dir else None,
            }
        )

    def archive_video_frame(
        self,
        receiver: ArchiveReceiverV2,
        packet: Packet,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None:
        writer = receiver.writer
        if writer is None:
            return
        observed_offset_ns = receive_monotonic_ns - packet.capture_timestamp_ns
        if self._iphone_v2_min_offset_ns is None or observed_offset_ns < self._iphone_v2_min_offset_ns:
            self._iphone_v2_min_offset_ns = observed_offset_ns
        self._iphone_v2_alignment_samples += 1
        clock_model = self._iphone_v2_clock_model
        if clock_model is not None:
            aligned_monotonic_ns = int(round(
                float(clock_model["scale"]) * packet.capture_timestamp_ns
                + float(clock_model["offset_ns"])
            ))
            alignment_method = str(clock_model["method"])
            alignment_sample_count = int(clock_model["sample_count"])
            phone_to_mac_offset_ns = float(clock_model["offset_ns"])
        else:
            phone_to_mac_offset_ns = self._iphone_v2_min_offset_ns or 0
            aligned_monotonic_ns = packet.capture_timestamp_ns + phone_to_mac_offset_ns
            alignment_method = "one_way_min_delay"
            alignment_sample_count = self._iphone_v2_alignment_samples
        estimated_latency_ns = max(0, receive_monotonic_ns - aligned_monotonic_ns)
        if self._iphone_v2_first_receive_monotonic_ns is None:
            self._iphone_v2_first_receive_monotonic_ns = receive_monotonic_ns
        elapsed_s = max(
            0.001,
            (receive_monotonic_ns - self._iphone_v2_first_receive_monotonic_ns) / 1e9,
        )
        connection_frame_count = writer.frame_count - self._iphone_v2_initial_frame_count
        connection_encoded_bytes = writer.encoded_bytes - self._iphone_v2_initial_encoded_bytes
        preview_ready = bool(
            receiver.preview_muxer is not None
            and receiver.preview_muxer.fragment_rows
        )
        fps = max(0, connection_frame_count - 1) / elapsed_s
        mbps = connection_encoded_bytes * 8 / elapsed_s / 1e6
        alignment_status = "receiver_aligned" if clock_model is not None else "estimating"
        publish = False
        with self.lock:
            self.state["latest_iphone_sample_monotonic_ns"] = receive_monotonic_ns
            self.state["stats"] = {
                "frame_count": writer.frame_count,
                "event_count": 0,
                "dropped_sequence_count": 0,
                "last_sequence": packet.sequence,
                "encoded_bytes": writer.encoded_bytes,
                "fps": fps,
                "mbps": mbps,
            }
            self.state["alignment"] = {
                "method": alignment_method,
                "status": alignment_status,
                "sample_count": alignment_sample_count,
                "phone_to_mac_scale": clock_model.get("scale") if clock_model else 1.0,
                "phone_to_mac_offset_ns": phone_to_mac_offset_ns,
                "estimated_receive_latency_ns": estimated_latency_ns,
                "rtt_p50_ms": clock_model.get("rtt_p50_ms") if clock_model else None,
            }
            current_video = dict(self.state.get("iphone_video", {}))
            self.state["iphone_video"] = {
                **current_video,
                "preview_ready": preview_ready,
                "last_sequence": packet.sequence,
                "encoded_bytes": writer.encoded_bytes,
                "fps": fps,
                "mbps": mbps,
                "last_receive_unix_ns": receive_unix_ns,
            }
            current_frame = dict(self.state.get("latest_frame", {}))
            self.state["latest_frame"] = {
                **current_frame,
                "sequence": packet.sequence,
                "timestamp": {
                    "phone_local_capture_ns": packet.capture_timestamp_ns,
                    "mac_receive_monotonic_ns": receive_monotonic_ns,
                    "mac_receive_unix_ns": receive_unix_ns,
                    "aligned_monotonic_ns": aligned_monotonic_ns,
                    "estimated_receive_latency_ns": estimated_latency_ns,
                },
                "rgb": {
                    "codec": "h264",
                    "width": current_video.get("width", 1920),
                    "height": current_video.get("height", 1440),
                    "has_payload": True,
                    "is_key_frame": packet.is_key_frame,
                    "encoded_bytes": len(packet.payload),
                },
                "pose": current_frame.get("pose", {"valid": False, "status": "v2_packet_pending"}),
                "gripper": current_frame.get("gripper", {"valid": False, "status": "v2_packet_pending"}),
                "depth": current_frame.get("depth", {"valid": False, "status": "v2_packet_pending"}),
            }
            self.state["capture_summary"] = self.build_capture_summary_locked()
            if (
                receive_monotonic_ns - self._last_iphone_gui_publish_monotonic_ns
                >= int(IPHONE_GUI_PUBLISH_INTERVAL_S * 1e9)
            ):
                self._last_iphone_gui_publish_monotonic_ns = receive_monotonic_ns
                snapshot = dict(self.state)
                publish = True
        if publish:
            self.broadcast({"type": "state", "state": snapshot})

    def archive_clock_sync_sample(
        self,
        receiver: ArchiveReceiverV2,
        _sample: dict[str, object],
    ) -> None:
        if receiver.session_dir is None:
            return
        model = phone_clock_model(receiver.session_dir)
        if model is None:
            return
        self._iphone_v2_clock_model = model

    def archive_frame_metadata(
        self,
        _receiver: ArchiveReceiverV2,
        packet: Packet,
        metadata: FrameMetadata,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None:
        transform = metadata.camera_in_user_world_transform
        pose = self._pose_from_transform(transform) if metadata.pose_valid else None
        gripper = {
            "valid": metadata.gripper_valid,
            "open_percent": metadata.gripper_open_percent if metadata.gripper_valid else None,
            "timestamp_ns": metadata.gripper_timestamp_ns if metadata.gripper_valid else None,
            "age_ms": metadata.gripper_age_ns / 1e6 if metadata.gripper_valid else None,
        }
        depth = {
            "valid": metadata.depth_valid,
            "width": metadata.depth_width,
            "height": metadata.depth_height,
            "pixel_format": metadata.depth_pixel_format,
            "center_m": metadata.depth_center_m if metadata.depth_valid else None,
            "valid_ratio": metadata.depth_valid_ratio if metadata.depth_valid else None,
            "encoding": "float16_le" if metadata.depth_valid else None,
        }
        with self.lock:
            current_frame = dict(self.state.get("latest_frame", {}))
            self.state["latest_frame"] = {
                **current_frame,
                "sequence": packet.sequence,
                "pose": pose or {
                    "valid": False,
                    "status": "world_origin_not_calibrated",
                    "world_calibrated": metadata.world_calibrated,
                },
                "gripper": gripper,
                "depth": depth,
                "recording": {"active": metadata.recording_active},
            }
            self.state["latest_iphone_pose"] = {
                "valid": bool(pose),
                "frame": "user_world" if metadata.pose_frame == 1 else "unavailable",
                "world_calibrated": metadata.world_calibrated,
                "world_origin_status": "calibrated" if metadata.world_calibrated else "not_marked",
                **(pose or {}),
            }

    def archive_recording_event(
        self,
        _receiver: ArchiveReceiverV2,
        packet: Packet,
        event: RecordingEvent,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None:
        code = {
            1: "coinft_restart_requested",
            2: "record_start",
            3: "record_stop",
            4: "record_discard",
        }.get(event.code)
        if code is None:
            self.log(f"unknown iPhone V2 recording event code={event.code}")
            return
        clock_model = self._iphone_v2_clock_model
        if clock_model is not None:
            aligned_monotonic_ns = int(round(
                float(clock_model["scale"]) * packet.capture_timestamp_ns
                + float(clock_model["offset_ns"])
            ))
            alignment_method = str(clock_model["method"])
        else:
            observed_offset_ns = receive_monotonic_ns - packet.capture_timestamp_ns
            minimum_offset_ns = self._iphone_v2_min_offset_ns
            if minimum_offset_ns is None or observed_offset_ns < minimum_offset_ns:
                minimum_offset_ns = observed_offset_ns
            aligned_monotonic_ns = packet.capture_timestamp_ns + minimum_offset_ns
            alignment_method = "one_way_min_delay"
        receive_latency_ns = max(0, receive_monotonic_ns - aligned_monotonic_ns)
        normalized = {
            "sequence": packet.sequence,
            "timestamp": {
                "phone_local_capture_ns": packet.capture_timestamp_ns,
                "mac_receive_monotonic_ns": receive_monotonic_ns,
                "mac_receive_unix_ns": receive_unix_ns,
                "aligned_monotonic_ns": aligned_monotonic_ns,
                "aligned_unix_ns": receive_unix_ns - receive_latency_ns,
                "alignment_method": alignment_method,
            },
            "recording": {"active": code == "record_start"},
        }
        event_payload = {
            "code": code,
            "index": event.event_index,
            "reason": event.reason,
            "event_unix_time_ns": event.event_unix_time_ns,
            "app_uptime_ns": event.app_uptime_ns,
            "frame_sequence": packet.sequence,
        }
        self.iphone_event_executor.submit(
            self.handle_iphone_recording_event,
            event_payload,
            normalized,
        )

    @staticmethod
    def _pose_from_transform(values: tuple[float, ...]) -> dict[str, Any] | None:
        if len(values) != 16 or any(not math.isfinite(value) for value in values):
            return None
        r00, _r01, _r02, tx = values[0:4]
        r10, _r11, _r12, ty = values[4:8]
        r20, r21, r22, tz = values[8:12]
        pitch = math.asin(max(-1.0, min(1.0, -r20)))
        roll = math.atan2(r21, r22)
        yaw = math.atan2(r10, r00)
        return {
            "valid": True,
            "frame": "user_world",
            "world_calibrated": True,
            "x_m": tx,
            "y_m": ty,
            "z_m": tz,
            "roll_deg": math.degrees(roll),
            "pitch_deg": math.degrees(pitch),
            "yaw_deg": math.degrees(yaw),
            "transform": values,
        }

    def worker_finished(self) -> None:
        with self.lock:
            self.worker = None
            self.stop_event = None
            self.state["running"] = False
            if self.state.get("status") != "error":
                self.state["status"] = "idle"
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.log("receiver finished")

    def start_coinft(self) -> bool:
        if self.config.coinft_source == "none":
            self.log("CoinFT collector disabled")
            return False
        if self.config.coinft_source == "mock":
            return self.start_teensy_mock()
        return self.start_coinft_serial()

    def start_coinft_serial(self) -> bool:
        with self.lock:
            if self.teensy_worker is not None:
                return False
        self.ensure_active_run()
        stop_event = threading.Event()
        worker = CoinFTSerialWorker(
            self,
            stop_event,
            port=self.config.coinft_port,
            baud=self.config.coinft_baud,
            simulate=self.config.coinft_simulate,
            tare=self.config.coinft_tare,
            calibration_config=self.coinft_calibration_config,
        )
        with self.lock:
            self.teensy_worker = worker
            self.teensy_stop_event = stop_event
            self.state["teensy"] = {
                **self.state.get("teensy", {}),
                "running": True,
                "source": "coinft_serial",
                "serial_port": self.config.coinft_port or None,
                "baud": self.config.coinft_baud,
                "status": "starting",
                "live_sample_count": 0,
                "recorded_sample_count": 0,
                "fps": 0.0,
                **calibration_status_summary(self.coinft_calibration_config),
                "calibration_status": "error"
                if self.coinft_calibration_error
                else ("warming_up" if self.coinft_calibration_config.calibrated else "raw_only"),
                "calibration_ready": not self.coinft_calibration_config.calibrated,
                "calibration_error": self.coinft_calibration_error,
                "last_error": None,
            }
            self.state["coinft"] = dict(self.state["teensy"])
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.log("CoinFT serial collector starting")
        worker.start()
        return True

    def start_teensy_mock(self) -> bool:
        with self.lock:
            if self.teensy_worker is not None:
                return False
        self.ensure_active_run()
        stop_event = threading.Event()
        worker = TeensyMockWorker(self, stop_event)
        with self.lock:
            self.teensy_worker = worker
            self.teensy_stop_event = stop_event
            self.state["teensy"] = {
                **self.state.get("teensy", {}),
                "running": True,
                "source": "mock",
                "live_sample_count": 0,
                "recorded_sample_count": 0,
                "fps": 0.0,
                **calibration_status_summary(self.coinft_calibration_config),
                "calibration_status": "raw_only",
                "calibration_error": self.coinft_calibration_error,
                "last_error": None,
            }
            self.state["coinft"] = dict(self.state["teensy"])
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.log("teensy mock starting")
        worker.start()
        return True

    def stop_teensy(self) -> bool:
        with self.lock:
            worker = self.teensy_worker
            stop_event = self.teensy_stop_event
        if worker is None or stop_event is None:
            return False
        stop_event.set()
        self.log("CoinFT collector stopping")
        return True

    def stop_teensy_mock(self) -> bool:
        return self.stop_teensy()

    def restart_coinft(self, *, stop_timeout_s: float = COINFT_RESTART_STOP_TIMEOUT_S) -> dict[str, Any]:
        """Restart only the CoinFT worker and wait for its serial port to close."""
        with self.coinft_lifecycle_lock:
            with self.lock:
                worker = self.teensy_worker

            stopped = False
            if worker is not None:
                stopped = self.stop_teensy()
                worker.join(timeout=stop_timeout_s)
                if worker.is_alive():
                    reason = f"CoinFT worker did not stop within {stop_timeout_s:.1f}s"
                    self.log(f"CoinFT restart failed: {reason}")
                    return {"ok": False, "stopped": stopped, "started": False, "reason": reason}
                with self.lock:
                    if self.teensy_worker is worker:
                        self.teensy_worker = None
                        self.teensy_stop_event = None

            started = self.start_coinft()
            if started:
                self.log("CoinFT collector restarted for iPhone record countdown")
                return {"ok": True, "stopped": stopped, "started": True, "reason": None}

            reason = "CoinFT collector did not start"
            self.log(f"CoinFT restart failed: {reason}")
            return {"ok": False, "stopped": stopped, "started": False, "reason": reason}

    def teensy_finished(self) -> None:
        with self.lock:
            self.teensy_worker = None
            self.teensy_stop_event = None
            teensy = {**self.state.get("teensy", {}), "running": False}
            self.state["teensy"] = teensy
            self.state["coinft"] = dict(teensy)
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.log("CoinFT collector finished")

    def d435_control_request(
        self,
        command: str,
        *,
        output_dir: Path | None = None,
        episode_index: int | None = None,
        run_id: str | None = None,
        duration_s: float | None = None,
        event_aligned_monotonic_ns: int | None = None,
        event_aligned_unix_ns: int | None = None,
        recording_preroll_s: float | None = None,
    ) -> str:
        request_id = f"{command}_{time.time_ns()}"
        payload: dict[str, Any] = {
            "schema_version": 1,
            "command": command,
            "request_id": request_id,
            "created_unix_ns": time.time_ns(),
            "trigger": "iphone_recording_event",
        }
        if output_dir is not None:
            payload["output_dir"] = str(output_dir)
        if episode_index is not None:
            payload["episode_index"] = episode_index
        if run_id:
            payload["run_id"] = run_id
        if duration_s is not None:
            payload["duration_s"] = duration_s
        if event_aligned_monotonic_ns is not None:
            payload["event_aligned_monotonic_ns"] = event_aligned_monotonic_ns
        if event_aligned_unix_ns is not None:
            payload["event_aligned_unix_ns"] = event_aligned_unix_ns
        if recording_preroll_s is not None:
            payload["recording_preroll_s"] = float(recording_preroll_s)
        write_json_atomic(D435_RECORD_CONTROL_FILE, payload)
        return request_id

    def read_d435_stream_status(self) -> dict[str, Any]:
        return load_json_if_present(D435_STREAM_STATUS_FILE)

    def d435_stream_snapshot(self, *, now_unix_ns: int | None = None, update_state: bool = True) -> dict[str, Any]:
        self.refresh_d435_runtime()
        now_unix_ns = now_unix_ns if isinstance(now_unix_ns, int) else time.time_ns()
        status = self.read_d435_stream_status()
        status_age_ms = file_age_ms(D435_STREAM_STATUS_FILE, now_unix_ns) if status else None
        status_fresh = (
            status_age_ms is not None
            and status_age_ms <= D435_PREVIEW_FRESH_MAX_S * 1000.0
        )

        rgb_path = self.config.d435_preview_dir / "latest_rgb.jpg"
        depth_path = self.config.d435_preview_dir / "latest_depth.png"
        rgb_age_ms = file_age_ms(rgb_path, now_unix_ns)
        depth_age_ms = file_age_ms(depth_path, now_unix_ns)
        files_fresh = (
            rgb_age_ms is not None
            and depth_age_ms is not None
            and rgb_age_ms <= D435_PREVIEW_FRESH_MAX_S * 1000.0
            and depth_age_ms <= D435_PREVIEW_FRESH_MAX_S * 1000.0
        )

        with self.lock:
            process = self.d435_process
            started_monotonic_s = self.d435_started_monotonic_s
            d435 = dict(self.state.get("d435", {})) if isinstance(self.state.get("d435"), dict) else {}
            d435_recording = (
                dict(self.state.get("d435_recording", {}))
                if isinstance(self.state.get("d435_recording"), dict)
                else {}
            )

        child_process_running = bool(process is not None and process.poll() is None)
        status_running = bool(status.get("running") and status_fresh)
        managed_pid = d435.get("pid") or d435_recording.get("pid")
        if (
            status_running
            and not isinstance(managed_pid, int)
            and INSTALLED_D435_RUNTIME_PID_FILE.is_file()
        ):
            try:
                managed_pid = int(INSTALLED_D435_RUNTIME_PID_FILE.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                managed_pid = None
        managed_process_running = False
        if sys.platform == "darwin" and isinstance(managed_pid, int) and managed_pid > 0:
            try:
                os.kill(managed_pid, 0)
                managed_process_running = True
            except PermissionError:
                managed_process_running = True
            except ProcessLookupError:
                pass
        process_running = child_process_running or managed_process_running
        status_recording = bool(status.get("recordingActive") and status_fresh)
        startup_age_ms = None
        if isinstance(started_monotonic_s, (int, float)):
            startup_age_ms = max(0.0, (time.monotonic() - started_monotonic_s) * 1000.0)
        startup_grace = bool(
            startup_age_ms is not None
            and startup_age_ms <= D435_STARTUP_GRACE_S * 1000.0
            and not status_fresh
        )
        files_streaming = bool(files_fresh and (not status or status.get("running") is not False))
        running = bool(process_running or status_running or startup_grace)
        if status_fresh and "recordingActive" in status:
            recording_active = bool(status.get("recordingActive"))
        else:
            recording_active = bool(d435_recording.get("running"))
        pid = process.pid if child_process_running and process is not None else managed_pid
        acquisition = status.get("acquisition") if isinstance(status.get("acquisition"), dict) else {}
        acquisition_error = acquisition.get("error") if status_fresh else None
        status_error = (status.get("lastError") or acquisition_error) if status_fresh else None
        state_error = None if (status_running or files_streaming or startup_grace) else d435.get("last_error")
        last_error = status_error or state_error
        ok = bool(running and files_fresh and not last_error)

        snapshot = {
            "ok": ok,
            "running": running,
            "process_running": process_running,
            "status_running": status_running,
            "status_fresh": status_fresh,
            "files_fresh": files_fresh,
            "files_streaming": files_streaming,
            "startup_grace": startup_grace,
            "startup_age_ms": startup_age_ms,
            "pid": pid,
            "rgb_age_ms": rgb_age_ms,
            "depth_age_ms": depth_age_ms,
            "status_age_ms": status_age_ms,
            "approx_fps": status.get("approxFps"),
            "recording_active": recording_active,
            "recording_output_dir": status.get("recordingOutputDir"),
            "recording_frame_count": status.get("recordingFrameCount"),
            "buffer": status.get("buffer"),
            "acquisition": acquisition,
            "last_error": last_error,
            "status": status,
        }

        if update_state:
            with self.lock:
                current_d435 = dict(self.state.get("d435", {})) if isinstance(self.state.get("d435"), dict) else {}
                self.state["d435"] = {
                    **current_d435,
                    "running": running,
                    "pid": pid,
                    "mode": "continuous_stream",
                    "preview_dir": str(self.config.d435_preview_dir),
                    "log": str(D435_STREAM_LOG),
                    "control_file": str(D435_RECORD_CONTROL_FILE),
                    "status_file": str(D435_STREAM_STATUS_FILE),
                    "status_age_ms": status_age_ms,
                    "status_running": status_running,
                    "process_running": process_running,
                    "files_fresh": files_fresh,
                    "files_streaming": files_streaming,
                    "startup_grace": startup_grace,
                    "startup_age_ms": startup_age_ms,
                    "acquisition": acquisition,
                    "last_error": last_error,
                }
                current_recording = (
                    dict(self.state.get("d435_recording", {}))
                    if isinstance(self.state.get("d435_recording"), dict)
                    else {}
                )
                self.state["d435_recording"] = {
                    **current_recording,
                    "running": recording_active,
                    "pid": pid if recording_active else None,
                    "output_dir": status.get("recordingOutputDir") or current_recording.get("output_dir"),
                    "log": str(D435_STREAM_LOG),
                    "control_file": str(D435_RECORD_CONTROL_FILE),
                    "status_file": str(D435_STREAM_STATUS_FILE),
                    "last_status": status or current_recording.get("last_status"),
                }
                if isinstance(status.get("buffer"), dict):
                    buffering = dict(self.state.get("buffering", {}))
                    buffering["d435"] = status.get("buffer")
                    self.state["buffering"] = buffering

        return snapshot

    def wait_for_d435_request(self, request_id: str, *, recording_active: bool, timeout_s: float = 1.5) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last_status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = self.read_d435_stream_status()
            if status:
                last_status = status
            if status.get("lastRequestId") == request_id:
                if status.get("lastError"):
                    return status
                if bool(status.get("recordingActive")) == recording_active:
                    return status
            time.sleep(0.03)
        return last_status

    def start_d435_preview(self) -> bool:
        with self.capture_lifecycle_lock, self.d435_lifecycle_lock:
            return self._start_d435_preview()

    def _start_d435_preview(self) -> bool:
        if sys.platform != "darwin":
            message = "D435 GUI capture control currently requires macOS."
            with self.lock:
                self.state["d435"] = {**self.state.get("d435", {}), "running": False, "last_error": message}
                snapshot = dict(self.state)
            self.broadcast({"type": "state", "state": snapshot})
            self.log(message)
            self.refresh_preflight_state()
            return False
        with self.lock:
            existing = self.d435_process
        if existing is not None and existing.poll() is None:
            return False
        existing_stream = self.d435_stream_snapshot()
        if existing_stream.get("running"):
            self.refresh_preflight_state()
            return False

        self.config.d435_preview_dir.mkdir(parents=True, exist_ok=True)
        self.d435_control_request("idle")
        control_script = self.d435_control_script()
        if not INSTALLED_D435_CONTROL_SCRIPT.exists():
            last_error = (
                "D435 passwordless sudo is not installed; run "
                f"bash {D435_SUDOERS_INSTALL_SCRIPT}"
            )
            with self.lock:
                self.state["d435"] = {
                    "running": False,
                    "pid": None,
                    "mode": "continuous_stream",
                    "preview_dir": str(self.config.d435_preview_dir),
                    "log": str(D435_STREAM_LOG),
                    "control_file": str(D435_RECORD_CONTROL_FILE),
                    "status_file": str(D435_STREAM_STATUS_FILE),
                    "control_script": str(INSTALLED_D435_CONTROL_SCRIPT),
                    "last_error": last_error,
                }
                snapshot = dict(self.state)
            self.broadcast({"type": "state", "state": snapshot})
            self.log(f"D435 preview not started: {last_error}")
            self.refresh_preflight_state()
            return False
        sudo_check = subprocess.run(
            ["sudo", "-n", str(control_script), "help"],
            capture_output=True,
            text=True,
            check=False,
        )
        if sudo_check.returncode != 0:
            message = (sudo_check.stderr or sudo_check.stdout or "D435 sudo preflight failed").strip()
            last_error = "D435 passwordless sudo preflight failed"
            if message:
                last_error = f"{last_error}: {message}"
            with self.lock:
                self.state["d435"] = {
                    "running": False,
                    "pid": None,
                    "mode": "continuous_stream",
                    "preview_dir": str(self.config.d435_preview_dir),
                    "log": str(D435_STREAM_LOG),
                    "control_file": str(D435_RECORD_CONTROL_FILE),
                    "status_file": str(D435_STREAM_STATUS_FILE),
                    "control_script": str(control_script),
                    "last_exit_code": sudo_check.returncode,
                    "last_error": last_error,
                }
                snapshot = dict(self.state)
            self.broadcast({"type": "state", "state": snapshot})
            self.log(f"D435 preview not started: {last_error}")
            self.refresh_preflight_state()
            return False
        capture_options = self.capture_options_snapshot()
        for stale_path in (
            D435_STREAM_STATUS_FILE,
            self.config.d435_preview_dir / "latest_rgb.jpg",
            self.config.d435_preview_dir / "latest_depth.png",
        ):
            try:
                stale_path.unlink()
            except OSError:
                pass
        cmd = [
            "sudo",
            "-n",
            str(control_script),
            "start",
            "--stream",
            "--duration-s",
            "7200",
            "--preview-dir",
            str(self.config.d435_preview_dir),
            "--preview-every-n-frames",
            "15",
            "--warmup-frames",
            "0",
            "--recording-preroll-s",
            str(float(capture_options.get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S))),
            "--stream-buffer-s",
            str(float(capture_options.get("stream_buffer_s", DEFAULT_STREAM_BUFFER_S))),
            "--record-control-file",
            str(D435_RECORD_CONTROL_FILE),
            "--status-file",
            str(D435_STREAM_STATUS_FILE),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "D435 control script failed to start").strip()
            with self.lock:
                self.state["d435"] = {
                    "running": False,
                    "pid": None,
                    "mode": "continuous_stream",
                    "preview_dir": str(self.config.d435_preview_dir),
                    "log": str(D435_STREAM_LOG),
                    "control_file": str(D435_RECORD_CONTROL_FILE),
                    "status_file": str(D435_STREAM_STATUS_FILE),
                    "control_script": str(control_script),
                    "last_exit_code": result.returncode,
                    "last_error": message,
                }
            self.log(f"D435 continuous stream failed to start: {message}")
            self.refresh_preflight_state()
            return False
        match = re.search(r"started pid=(\d+)", result.stdout or "")
        managed_pid = int(match.group(1)) if match else None
        with self.lock:
            self.d435_process = None
            self.d435_log_handle = None
            self.d435_started_monotonic_s = time.monotonic()
            self.state["d435"] = {
                "running": True,
                "pid": managed_pid,
                "mode": "continuous_stream",
                "managed_by_control_script": True,
                "preview_dir": str(self.config.d435_preview_dir),
                "log": str(D435_STREAM_LOG),
                "control_file": str(D435_RECORD_CONTROL_FILE),
                "status_file": str(D435_STREAM_STATUS_FILE),
                "control_script": str(control_script),
            }
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.log(f"D435 continuous stream starting pid={managed_pid or 'pending'}")
        self.refresh_preflight_state()
        return True

    def stop_d435_preview(self) -> bool:
        with self.capture_lifecycle_lock, self.d435_lifecycle_lock:
            return self._stop_d435_preview()

    def _stop_d435_preview(self) -> bool:
        if sys.platform != "darwin":
            return False
        with self.lock:
            process = self.d435_process
            log_handle = self.d435_log_handle
            d435_state = dict(self.state.get("d435", {}))
            self.d435_process = None
            self.d435_log_handle = None
            self.d435_started_monotonic_s = None
        try:
            request_id = self.d435_control_request("stop")
            self.wait_for_d435_request(request_id, recording_active=False, timeout_s=1.0)
        except Exception:
            pass
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    process.kill()
        control_result = subprocess.run(
            ["sudo", "-n", str(self.d435_control_script()), "stop"],
            capture_output=True,
            text=True,
            check=False,
        )
        for stale_path in (
            D435_STREAM_STATUS_FILE,
            self.config.d435_preview_dir / "latest_rgb.jpg",
            self.config.d435_preview_dir / "latest_depth.png",
        ):
            try:
                stale_path.unlink()
            except OSError:
                pass
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass
        with self.lock:
            self.state["d435"] = {
                "running": False,
                "pid": None,
                "mode": "continuous_stream",
                "preview_dir": str(self.config.d435_preview_dir),
                "log": str(D435_STREAM_LOG),
                "control_file": str(D435_RECORD_CONTROL_FILE),
                "status_file": str(D435_STREAM_STATUS_FILE),
            }
            self.state["d435_recording"] = {
                **self.state.get("d435_recording", {}),
                "running": False,
                "pid": None,
                "log": str(D435_STREAM_LOG),
            }
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        if control_result.returncode == 0:
            self.log("D435 continuous stream stopped")
        else:
            message = (control_result.stderr or control_result.stdout or "D435 control stop failed").strip()
            self.log(f"D435 continuous stream stop warning: {message}")
        self.refresh_preflight_state()
        return bool(process is not None or d435_state.get("running") or control_result.returncode == 0)

    def start_d435_recording(self, overrides: dict[str, Any] | None = None) -> bool:
        overrides = overrides or {}
        run_dir = self.ensure_active_run(overrides.get("run_dir", ""), overrides.get("run_id", ""))
        episode_index = overrides.get("episode_index")
        if isinstance(episode_index, int):
            output_dir = run_dir / "raw" / "global_camera" / f"episode_{episode_index:06d}"
        else:
            output_dir = run_dir / "raw" / "global_camera" / f"manual_{timestamp_id()}"
        try:
            output_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            self.log(f"D435 recording refused: output directory already exists: {output_dir}")
            return False
        write_json_atomic(
            output_dir / D435_EPISODE_RESERVATION_FILE,
            {
                "schema_version": 1,
                "episode_index": episode_index,
                "run_id": run_dir.name,
                "reserved_at_unix_ns": time.time_ns(),
            },
        )
        duration_s = float(overrides.get("duration_s", 7200.0))
        recording_preroll_s = float(overrides.get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S))
        event_aligned_monotonic_ns = overrides.get("event_aligned_monotonic_ns")
        event_aligned_unix_ns = overrides.get("event_aligned_unix_ns")

        d435_snapshot = self.d435_stream_snapshot()
        if not d435_snapshot.get("running"):
            self.log(
                "D435 recording refused: continuous stream was not initialized during Arm All"
            )
            return False

        request_id = self.d435_control_request(
            "start",
            output_dir=output_dir,
            episode_index=episode_index if isinstance(episode_index, int) else None,
            run_id=run_dir.name,
            duration_s=duration_s,
            event_aligned_monotonic_ns=event_aligned_monotonic_ns if isinstance(event_aligned_monotonic_ns, int) else None,
            event_aligned_unix_ns=event_aligned_unix_ns if isinstance(event_aligned_unix_ns, int) else None,
            recording_preroll_s=recording_preroll_s,
        )
        status = self.wait_for_d435_request(
            request_id,
            recording_active=True,
            timeout_s=d435_recording_start_timeout_s(recording_preroll_s),
        )
        if status.get("lastRequestId") != request_id or not status.get("recordingActive") or status.get("lastError"):
            reason = status.get("lastError") or "D435 continuous stream did not acknowledge recording start"
            self.log(f"D435 recording failed: {reason}")
            stop_request_id = self.d435_control_request("stop")
            self.wait_for_d435_request(stop_request_id, recording_active=False, timeout_s=1.0)
            return False

        register_artifact(
            run_dir,
            stream="d435",
            role=f"raw_episode_{episode_index:06d}" if isinstance(episode_index, int) else "raw_manual_dir",
            path=output_dir,
            metadata={
                "kind": "directory",
                "duration_s": duration_s,
                "recording_preroll_s": recording_preroll_s,
                "event_aligned_monotonic_ns": event_aligned_monotonic_ns,
                "event_aligned_unix_ns": event_aligned_unix_ns,
                "episode_index": episode_index,
            "control": str(self.d435_control_script()),
                "control_file": str(D435_RECORD_CONTROL_FILE),
                "status_file": str(D435_STREAM_STATUS_FILE),
                "trigger": "iphone_recording_event" if isinstance(episode_index, int) else "manual_debug",
            },
        )
        with self.lock:
            self.state["d435_recording"] = {
                "running": True,
                "pid": d435_snapshot.get("pid"),
                "output_dir": str(output_dir),
                "duration_s": duration_s,
                "recording_preroll_s": recording_preroll_s,
                "log": str(D435_STREAM_LOG),
                "control_file": str(D435_RECORD_CONTROL_FILE),
                "status_file": str(D435_STREAM_STATUS_FILE),
                "request_id": request_id,
            }
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        self.log(f"D435 writer opened output={output_dir}")
        self.refresh_preflight_state()
        return True

    def stop_d435_recording(self) -> bool:
        request_id = self.d435_control_request("stop")
        status = self.wait_for_d435_request(request_id, recording_active=False, timeout_s=2.0)
        ok = status.get("lastRequestId") == request_id and not status.get("recordingActive") and not status.get("lastError")
        with self.lock:
            output_dir = self.state.get("d435_recording", {}).get("output_dir")
            self.state["d435_recording"] = {
                "running": False,
                "pid": None,
                "output_dir": output_dir,
                "duration_s": self.state.get("d435_recording", {}).get("duration_s"),
                "log": str(D435_STREAM_LOG),
                "control_file": str(D435_RECORD_CONTROL_FILE),
                "status_file": str(D435_STREAM_STATUS_FILE),
                "request_id": request_id,
                "last_status": status,
            }
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        if ok:
            self.log("D435 writer closed")
        else:
            reason = status.get("lastError") or "D435 continuous stream did not acknowledge recording stop"
            self.log(f"D435 writer stop warning: {reason}")
        self.refresh_preflight_state()
        return bool(ok)

    def recording_gate_active(self) -> bool:
        with self.lock:
            control = self.state.get("recording_control", {})
            return bool(control.get("active"))

    def recording_episode_index(self) -> int:
        with self.lock:
            control = self.state.get("recording_control", {})
            value = control.get("episode_index")
        return int(value) if isinstance(value, int) else 0

    def discard_pending_episode_index(self) -> int | None:
        with self.lock:
            control = self.state.get("recording_control", {})
            value = control.get("episode_index")
            pending = bool(control.get("discard_pending"))
        return int(value) if pending and isinstance(value, int) else None

    def fail_active_recording_writer(self, stream: str, episode_index: int, reason: str) -> None:
        with self.lock:
            control = dict(self.state.get("recording_control", {}))
        if not control.get("active") or control.get("episode_index") != episode_index:
            return
        gate = self.check_recording_gate()
        reason_text = f"{stream} writer failed: {reason}"
        gate = {
            **gate,
            "ok": False,
            "status": "blocked",
            "reasons": [*gate.get("reasons", []), reason_text],
        }
        self.handle_post_start_gate_failure(
            episode_index=episode_index,
            event_start_ns=control.get("started_at") if isinstance(control.get("started_at"), int) else None,
            event_start_unix_ns=(
                control.get("started_at_unix_ns")
                if isinstance(control.get("started_at_unix_ns"), int)
                else None
            ),
            gate=gate,
            reason_text=reason_text,
        )

    def handle_iphone_recording_event(self, event: dict[str, Any], normalized: dict[str, Any]) -> bool:
        code = event.get("code")
        if code == "coinft_restart_requested":
            return self.handle_iphone_coinft_restart_request(event, normalized)
        if code not in {"record_start", "record_stop", "record_discard"}:
            return False

        raw_index = event.get("index")
        with self.lock:
            current_control = dict(self.state.get("recording_control", {}))
            was_active = bool(current_control.get("active"))
            run_dir_text = self.state.get("run_dir")
        current_episode = current_control.get("episode_index")
        if code == "record_start":
            if was_active:
                episode_index = int(current_episode) if isinstance(current_episode, int) else 0
                self.log(f"iPhone record_start ignored: episode {episode_index} is already active")
                self.publish_recording_transition(
                    "record_start_rejected",
                    episode_index=episode_index,
                    reason="recording is already active",
                    metadata={"raw_event_index": raw_index},
                )
                return False
            run_dir = (
                Path(str(run_dir_text)).expanduser().resolve()
                if run_dir_text
                else self.ensure_active_run()
            )
            episode_index = self.reserve_recording_episode_index(run_dir, raw_index)
        else:
            episode_index = int(current_episode) if isinstance(current_episode, int) else (int(raw_index) if isinstance(raw_index, int) else 0)
        timestamp = normalized.get("timestamp", {}) if isinstance(normalized.get("timestamp"), dict) else {}
        aligned_ns = timestamp.get("aligned_monotonic_ns")
        aligned_unix_ns = timestamp.get("aligned_unix_ns")

        if code == "record_start":
            gate = self.check_recording_gate()
            self.patch_state(recording_gate=gate)
            if not gate.get("ok"):
                reasons = gate.get("reasons", [])
                reason_text = "; ".join(str(reason) for reason in reasons) or "unknown preflight failure"
                self.patch_state(
                    recording_control={
                        **current_control,
                        "armed": True,
                        "active": False,
                        "episode_index": episode_index,
                        "started_at": None,
                        "started_at_unix_ns": None,
                        "stopped_at": None,
                        "last_event": "record_start_rejected",
                        "blocked": True,
                        "blocked_reason": reason_text,
                        "source": "iphone_recording_event",
                    }
                )
                if run_dir_text:
                    append_sync_log(
                        Path(str(run_dir_text)),
                        {
                            "event": "record_start_rejected",
                            "episode_index": episode_index,
                            "reasons": reasons,
                            "gate": gate,
                        },
                    )
                self.publish_recording_transition(
                    "record_start_rejected",
                    episode_index=episode_index,
                    event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                    event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                    gate=gate,
                    reason=reason_text,
                )
                self.log(f"iPhone record_start rejected: {reason_text}")
                return False

            self.publish_recording_transition(
                "record_start_requested",
                episode_index=episode_index,
                event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                gate=gate,
                metadata={"raw_event_index": raw_index},
            )
            self.patch_state(
                recording_control={
                    **current_control,
                    "armed": True,
                    "active": False,
                    "episode_index": episode_index,
                    "started_at": aligned_ns,
                    "started_at_unix_ns": aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                    "stopped_at": None,
                    "last_event": "record_start_requested",
                    "blocked": False,
                    "blocked_reason": None,
                    "source": "iphone_recording_event",
                }
            )
            self.log(f"iPhone record_start -> episode {episode_index}: opening disk writers")
            with self.lock:
                capture_options = dict(self.state.get("capture_options", {}))
            d435_started = self.start_d435_recording(
                {
                    "episode_index": episode_index,
                    "duration_s": capture_options.get("d435_duration_s", 7200.0),
                    "recording_preroll_s": capture_options.get("recording_preroll_s", DEFAULT_RECORDING_PREROLL_S),
                    "event_aligned_monotonic_ns": aligned_ns if isinstance(aligned_ns, int) else None,
                    "event_aligned_unix_ns": aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                }
            )
            if not d435_started:
                reason_text = "D435 recording process failed to start"
                self.patch_state(
                    recording_control={
                        **current_control,
                        "armed": True,
                        "active": False,
                        "episode_index": episode_index,
                        "started_at": None,
                        "started_at_unix_ns": None,
                        "last_event": "record_start_rejected",
                        "blocked": True,
                        "blocked_reason": reason_text,
                        "source": "iphone_recording_event",
                    },
                    recording_gate={
                        **gate,
                        "ok": False,
                        "status": "blocked",
                        "reasons": [*gate.get("reasons", []), reason_text],
                    },
                )
                self.log(f"iPhone record_start rejected: {reason_text}")
                self.publish_recording_transition(
                    "record_start_failed",
                    episode_index=episode_index,
                    event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                    event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                    gate={
                        **gate,
                        "ok": False,
                        "status": "blocked",
                        "reasons": [*gate.get("reasons", []), reason_text],
                    },
                    reason=reason_text,
                )
                return False
            self.patch_state(
                recording_control={
                    **current_control,
                    "armed": True,
                    "active": True,
                    "episode_index": episode_index,
                    "started_at": aligned_ns,
                    "started_at_unix_ns": aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                    "stopped_at": None,
                    "last_event": "record_start_committed",
                    "blocked": False,
                    "blocked_reason": None,
                    "source": "iphone_recording_event",
                }
            )
            self.publish_recording_transition(
                "record_start_committed",
                episode_index=episode_index,
                event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                gate=gate,
                metadata={"d435_recording_started": True},
            )
            self.schedule_post_start_gate_check(
                episode_index=episode_index,
                event_start_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                event_start_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
            )
            return True

        if code == "record_discard":
            if not was_active:
                self.log(f"iPhone record_discard ignored for episode {episode_index}: recording gate was not active")
                self.publish_recording_transition(
                    "record_discard_ignored",
                    episode_index=episode_index,
                    event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                    event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                    reason="recording gate was not active",
                )
                return False
            self.publish_recording_transition(
                "record_discard_requested",
                episode_index=episode_index,
                event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                metadata={"raw_event_index": raw_index},
            )
            self.patch_state(
                recording_control={
                    **current_control,
                    "armed": True,
                    "active": False,
                    "episode_index": episode_index,
                    "stopped_at": aligned_ns,
                    "stopped_at_unix_ns": aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                    "last_event": "record_discard_requested",
                    "discard_pending": True,
                    "blocked": False,
                    "source": "iphone_recording_event",
                }
            )
            self.log(f"iPhone record_discard -> episode {episode_index}: closing writers before rollback")
            if not self.stop_d435_recording():
                reason = "D435 writer did not confirm stop; discard cleanup was not started"
                self.patch_state(
                    recording_control={
                        **current_control,
                        "armed": True,
                        "active": False,
                        "episode_index": episode_index,
                        "last_event": "record_discard_failed",
                        "discard_pending": False,
                        "blocked": True,
                        "blocked_reason": reason,
                        "source": "iphone_recording_event",
                    }
                )
                self.publish_recording_transition(
                    "record_discard_failed",
                    episode_index=episode_index,
                    event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                    event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                    reason=reason,
                )
                self.log(reason)
                return False
            self.refresh_preflight_state()
            return False

        if not was_active:
            self.patch_state(
                recording_control={
                    **current_control,
                    "armed": True,
                    "active": False,
                    "episode_index": episode_index,
                "stopped_at": aligned_ns,
                "stopped_at_unix_ns": aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                "last_event": "record_stop_ignored",
                    "source": "iphone_recording_event",
                }
            )
            self.log(f"iPhone record_stop ignored for episode {episode_index}: recording gate was not active")
            self.publish_recording_transition(
                "record_stop_ignored",
                episode_index=episode_index,
                event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
                event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                reason="recording gate was not active",
            )
            return False

        self.publish_recording_transition(
            "record_stop_requested",
            episode_index=episode_index,
            event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
            event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
        )
        self.patch_state(
            recording_control={
                **current_control,
                "armed": True,
                "active": False,
                "episode_index": episode_index,
                "stopped_at": aligned_ns,
                "stopped_at_unix_ns": aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
                "last_event": "record_stop",
                "blocked": False,
                "source": "iphone_recording_event",
            }
        )
        self.log(f"iPhone record_stop -> episode {episode_index}: closing disk writers")
        self.stop_d435_recording()
        self.refresh_preflight_state()
        self.publish_recording_transition(
            "record_stop_committed",
            episode_index=episode_index,
            event_aligned_monotonic_ns=aligned_ns if isinstance(aligned_ns, int) else None,
            event_aligned_unix_ns=aligned_unix_ns if isinstance(aligned_unix_ns, int) else None,
        )
        # Raw writers have been closed above. Post-processing is deliberately
        # queued in the background so a new recording can start immediately.
        if run_dir_text:
            self.start_postprocess(Path(str(run_dir_text)), episode_index)
        return True

    def handle_iphone_coinft_restart_request(self, event: dict[str, Any], normalized: dict[str, Any]) -> bool:
        """Handle a pre-record request without reserving an episode or changing capture state."""
        timestamp = normalized.get("timestamp", {}) if isinstance(normalized.get("timestamp"), dict) else {}
        result = self.restart_coinft()
        with self.lock:
            run_dir_text = self.state.get("run_dir")
        log_row = {
            "event": "coinft_restart_requested",
            "raw_event_index": event.get("index"),
            "reason": event.get("reason"),
            "event_aligned_monotonic_ns": timestamp.get("aligned_monotonic_ns"),
            "event_aligned_unix_ns": timestamp.get("aligned_unix_ns"),
            "restart": result,
        }
        if run_dir_text:
            append_sync_log(Path(str(run_dir_text)), log_row)
        status = "restarted" if result.get("ok") else "failed"
        self.log(f"iPhone CoinFT restart request {status}")
        return False

    def complete_iphone_recording_discard(
        self,
        *,
        run_dir: Path,
        episode_index: int,
        iphone_cleanup: dict[str, Any],
    ) -> bool:
        with self.lock:
            current_control = dict(self.state.get("recording_control", {}))
        if current_control.get("episode_index") != episode_index or not current_control.get("discard_pending"):
            return False
        try:
            removed_paths = discard_episode_artifacts(run_dir, episode_index)
            if not release_discarded_episode_index(run_dir, episode_index):
                raise RuntimeError("episode index is no longer the latest removable reservation")
        except Exception as exc:
            reason = f"discard cleanup failed: {exc}"
            self.patch_state(
                recording_control={
                    **current_control,
                    "discard_pending": False,
                    "last_event": "record_discard_failed",
                    "blocked": True,
                    "blocked_reason": reason,
                }
            )
            self.publish_recording_transition(
                "record_discard_failed",
                episode_index=episode_index,
                reason=reason,
            )
            self.log(reason)
            return False

        append_sync_log(
            run_dir,
            {
                "event": "record_discard_committed",
                "episode_index": episode_index,
                "removed_paths": removed_paths,
                "iphone_cleanup": iphone_cleanup,
            },
        )
        self.patch_state(
            recording_control={
                **current_control,
                "armed": True,
                "active": False,
                "episode_index": None,
                "last_event": "record_discard_committed",
                "discard_pending": False,
                "blocked": False,
                "blocked_reason": None,
                "source": "iphone_recording_event",
            }
        )
        self.publish_recording_transition(
            "record_discard_committed",
            episode_index=episode_index,
            metadata={"removed_paths": removed_paths, "iphone_cleanup": iphone_cleanup},
        )
        self.log(f"iPhone record_discard committed for episode {episode_index}; index released")
        return True

    def requested_episode_index(self, args: dict[str, Any] | None = None) -> int:
        args = args or {}
        value = args.get("episode_index")
        if isinstance(value, int):
            return value
        run_dir = self.ensure_active_run()
        indices: list[int] = []
        for path in (run_dir / "raw" / "global_camera").glob("episode_*/frame_timestamps.csv"):
            try:
                indices.append(int(path.parent.name.rsplit("_", 1)[-1]))
            except ValueError:
                continue
        if not indices:
            raise ValueError("No completed raw Episode is available")
        return max(indices)

    def normalize_current_run(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        run_dir = self.ensure_active_run()
        episode_index = self.requested_episode_index(args)
        self.log(f"normalizing episode {episode_index}")
        explicit_coinft_config = bool((self.config.coinft_config or "").strip()) or bool(self.coinft_calibration_error)
        coinft_config = self.coinft_calibration_config if explicit_coinft_config or self.coinft_calibration_config.calibrated else None
        report = normalize_episode_impl(run_dir, episode_index, coinft_config=coinft_config)
        quality = self.evaluate_normalization_quality(report)
        with self.lock:
            derived = {**self.state.get("derived", {}), "normalized": report}
            self.state["derived"] = derived
            self.state["sync_quality"] = quality
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        if quality.get("ok"):
            self.log("normalization quality ok")
        else:
            self.log("normalization quality blocked: " + "; ".join(quality.get("reasons", [])))
        self.log(f"episode {episode_index} normalization finished")
        return report

    def align_current_run(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}
        run_dir = self.ensure_active_run()
        episode_index = self.requested_episode_index(overrides)
        self.log(f"aligning episode {episode_index}")
        report = align_episode_impl(
            run_dir,
            episode_index,
            timeline_mode=str(overrides.get("timeline_mode", "iphone-frame")),
            target_fps=float(overrides.get("target_fps", PC_TARGET_FPS)),
            iphone_max_delta_ms=float(overrides.get("iphone_max_delta_ms", 50.0)),
            d435_max_delta_ms=float(overrides.get("d435_max_delta_ms", 80.0)),
            coinft_max_delta_ms=float(overrides.get("coinft_max_delta_ms", 25.0)),
        )
        quality = self.evaluate_alignment_quality(report)
        with self.lock:
            derived = {**self.state.get("derived", {}), "aligned": report}
            self.state["derived"] = derived
            self.state["sync_quality"] = quality
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        if quality.get("ok"):
            self.log("alignment quality ok")
        else:
            self.log("alignment quality blocked: " + "; ".join(quality.get("reasons", [])))
        self.log(f"episode {episode_index} alignment finished")
        return report

    def update_export_control_state(self, export_control: dict[str, Any]) -> None:
        with self.lock:
            self.state["export_control"] = export_control
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})

    def configure_export_control(self, args: dict[str, Any]) -> dict[str, Any]:
        format_name = args.get("format") or args.get("format_name")
        profile: str | None
        if "profile" in args:
            profile = str(args.get("profile") or "")
        elif "profile_path" in args:
            profile = str(args.get("profile_path") or "")
        else:
            profile = None
        export_control = self.export_controller.configure(
            format_name=str(format_name).strip() if format_name else None,
            profile=profile,
        )
        self.update_export_control_state(export_control)
        self.log(
            "export selection "
            f"format={export_control.get('selected_format')} "
            f"profile={export_control.get('selected_profile') or 'default'}"
        )
        return export_control

    def export_selected_current_run(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        run_dir = self.ensure_active_run()
        episode_index = self.requested_episode_index(args)
        format_name = args.get("format") or args.get("format_name")
        profile = args.get("profile") if "profile" in args else args.get("profile_path")
        config_overrides = args.get("config_overrides")
        if not isinstance(config_overrides, dict):
            config_overrides = {}
        try:
            self.export_controller.configure(
                format_name=str(format_name).strip() if format_name else None,
                profile=str(profile) if profile is not None else None,
            )
            exporting_state = self.export_controller.mark_exporting()
            self.update_export_control_state(exporting_state)
            self.log(
                f"exporting episode {episode_index} with selected format "
                f"{exporting_state.get('selected_format')} "
                f"profile={exporting_state.get('selected_profile') or 'default'}"
            )
            manifest = self.export_controller.export_episode(
                run_dir,
                episode_index,
                format_name=str(format_name).strip() if format_name else None,
                profile=str(profile) if profile is not None else None,
                config_overrides=config_overrides,
            )
            export_control = self.export_controller.snapshot()
            quality = {
                "ok": True,
                "status": "ready",
                "stage": "export",
                "reasons": [],
                "manifest": manifest.get("outputs", {}),
                "checked_at_unix_ns": time.time_ns(),
            }
            rows = manifest.get("exported_timeline_rows", manifest.get("row_count", "--"))
            self.log(f"export ok format={manifest.get('format')} rows={rows}")
        except (Exception, SystemExit) as exc:
            reason = str(exc) or "selected export failed"
            manifest = {"error": reason}
            export_control = self.export_controller.snapshot()
            quality = {
                "ok": False,
                "status": "blocked",
                "stage": "export",
                "reasons": [reason],
                "checked_at_unix_ns": time.time_ns(),
            }
            self.log("selected export blocked: " + reason)
        with self.lock:
            selected_format = export_control.get("selected_format") or "selected"
            derived_key = f"{str(selected_format).replace('-', '_')}_export"
            derived = {**self.state.get("derived", {}), derived_key: manifest}
            self.state["derived"] = derived
            self.state["export_control"] = export_control
            self.state["sync_quality"] = quality
            snapshot = dict(self.state)
        self.broadcast({"type": "state", "state": snapshot})
        return manifest



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a browser GUI for UMIFT receiver monitoring.")
    parser.add_argument("--install-d435-sudoers", action="store_true", help="One-time macOS setup for passwordless D435 control, then exit.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--d435-preview-dir", default=str(DEFAULT_D435_PREVIEW_DIR))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    parser.add_argument("--coinft-source", choices=("serial", "mock", "none"), default="serial", help="CoinFT collector source. Defaults to auto-detected Teensy USB serial.")
    parser.add_argument("--coinft-port", default=os.environ.get("UMIFT_COINFT_PORT", ""), help="Teensy serial port. Defaults to auto-detect or UMIFT_COINFT_PORT.")
    parser.add_argument("--coinft-baud", type=int, default=COINFT_BAUD_RATE)
    parser.add_argument("--coinft-simulate", action="store_true", help="Send Teensy mock-stream command 'm' instead of true CoinFT stream command 's'.")
    parser.add_argument("--coinft-tare", action="store_true", help="Send Teensy tare command 't' before streaming.")
    parser.add_argument("--coinft-config", default=os.environ.get("UMIFT_COINFT_CONFIG", ""), help="CoinFT calibration JSON for raw-to-wrench small models. Defaults to raw-only or UMIFT_COINFT_CONFIG.")
    parser.add_argument("--auto-start", action="store_true", default=True, help="Start all three live collectors when the GUI server starts. This is the default.")
    parser.add_argument("--no-auto-start", dest="auto_start", action="store_false", help="Do not start live collectors automatically.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.install_d435_sudoers:
        if sys.platform != "darwin":
            raise SystemExit("D435 sudoers installation requires macOS.")
        return subprocess.run(["bash", str(D435_SUDOERS_INSTALL_SCRIPT)], check=False).returncode
    config = ServerConfig(
        host=args.host,
        port=args.port,
        d435_preview_dir=Path(args.d435_preview_dir).expanduser().resolve(),
        output_root=Path(args.output_root).expanduser().resolve(),
        runs_root=Path(args.runs_root).expanduser().resolve(),
        auto_start=args.auto_start,
        coinft_source=args.coinft_source,
        coinft_port=args.coinft_port,
        coinft_baud=args.coinft_baud,
        coinft_simulate=args.coinft_simulate,
        coinft_tare=args.coinft_tare,
        coinft_config=args.coinft_config,
    )
    backend = ReceiverBackend(config)
    Handler.backend = backend
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    url = f"http://{config.host}:{config.port}/"
    print(f"receiver web GUI: {url}", flush=True)
    if config.auto_start:
        backend.arm_capture({"output_root": str(config.output_root)})
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop_receiver()
        backend.stop_teensy()
        backend.stop_d435_preview()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

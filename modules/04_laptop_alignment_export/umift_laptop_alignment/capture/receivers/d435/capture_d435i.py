#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

try:
    import pyrealsense2 as rs
except ImportError:  # pragma: no cover - exercised by real machine state
    rs = None


DEFAULT_RGB_WIDTH = 640
DEFAULT_RGB_HEIGHT = 480
DEFAULT_DEPTH_WIDTH = 640
DEFAULT_DEPTH_HEIGHT = 480
DEFAULT_FPS = 30
DEFAULT_STATUS_INTERVAL_S = 1.0
DEFAULT_PREVIEW_EVERY_N_FRAMES = 15
DEFAULT_RECORDING_PREROLL_S = 1.5
DEFAULT_RECORDING_POSTROLL_S = 0.5
DEFAULT_STREAM_BUFFER_S = 3.0
DEFAULT_FRAME_TIMEOUT_MS = 5000
DEFAULT_STARTUP_RECOVERY_ATTEMPTS = 2
DEFAULT_STARTUP_RESET_DELAY_S = 2.0
D435_EPISODE_RESERVATION_FILE = ".umift_episode_reservation.json"
STOP_REQUESTED = False
FRAME_TIMESTAMP_FIELDS = [
    "frame_index",
    "host_receive_time_s",
    "host_receive_monotonic_ns",
    "realsense_frame_timestamp_ms",
    "realsense_timestamp_domain",
    "color_sensor_timestamp_us",
    "depth_sensor_timestamp_us",
    "color_frame_number",
    "depth_frame_number",
]


def request_stop(signum: int, frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_realsense() -> None:
    if rs is not None:
        return
    raise SystemExit(
        "pyrealsense2 is not available.\n"
        "On this Mac you likely need to install librealsense first, then build/install the Python wrapper.\n"
        "Try: bash modules/01_global_camera/install_realsense_macos.sh\n"
        "Then verify with: rs-enumerate-devices"
    )


def run_command(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return result.stdout.strip()


def enumerate_devices() -> list[dict[str, Any]]:
    require_realsense()
    ctx = rs.context()
    devices = []
    for dev in ctx.query_devices():
        devices.append(
            {
                "name": dev.get_info(rs.camera_info.name) if dev.supports(rs.camera_info.name) else "",
                "serial": dev.get_info(rs.camera_info.serial_number) if dev.supports(rs.camera_info.serial_number) else "",
                "firmware_version": dev.get_info(rs.camera_info.firmware_version) if dev.supports(rs.camera_info.firmware_version) else "",
                "product_line": dev.get_info(rs.camera_info.product_line) if dev.supports(rs.camera_info.product_line) else "",
            }
        )
    return devices


def select_device(serial: str | None) -> dict[str, Any]:
    devices = enumerate_devices()
    if not devices:
        raise SystemExit("No RealSense devices found through librealsense.")
    if serial is None:
        return devices[0]
    for dev in devices:
        if dev["serial"] == serial:
            return dev
    found = ", ".join(d["serial"] or "<unknown>" for d in devices)
    raise SystemExit(f"Requested serial {serial} not found. Visible devices: {found}")


def build_metadata(args: argparse.Namespace, device: dict[str, Any], output_dir: Path | None) -> dict[str, Any]:
    return {
        "captureStartedAt": iso_now(),
        "device": {
            "model": device.get("name", "Intel RealSense D435i"),
            "serial": device.get("serial", ""),
            "firmwareVersion": device.get("firmware_version", ""),
            "productLine": device.get("product_line", ""),
            "imuEnabled": False,
        },
        "streams": {
            "rgb": {
                "enabled": True,
                "width": args.rgb_width,
                "height": args.rgb_height,
                "fps": args.rgb_fps,
                "pixelFormat": "bgr8",
            },
            "depth": {
                "enabled": True,
                "width": args.depth_width,
                "height": args.depth_height,
                "fps": args.depth_fps,
                "pixelFormat": "z16",
                "storage": "depth.raw",
                "shape": [args.depth_height, args.depth_width],
                "dtype": "uint16",
                "endianness": sys.byteorder,
                "depthScaleMeters": None,
            },
        },
        "artifacts": {
            "rgbVideo": "rgb.mp4",
            "depthRaw": "depth.raw",
            "frameTimestampsCsv": "frame_timestamps.csv",
            "cameraMetadataJson": "camera_metadata.json",
            "captureLog": "capture_log.json",
        },
        "runContext": {
            "runId": args.run_id,
            "outputDir": str(output_dir) if output_dir is not None else "",
            "hostTimeEpoch": "unix",
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "hostnameInfo": run_command(["hostname"]),
        },
        "notes": args.notes,
    }


def maybe_intrinsics(profile: Any, stream: Any) -> dict[str, Any] | None:
    try:
        video_profile = profile.get_stream(stream).as_video_stream_profile()
        intr = video_profile.get_intrinsics()
    except Exception:
        return None
    return {
        "width": intr.width,
        "height": intr.height,
        "ppx": intr.ppx,
        "ppy": intr.ppy,
        "fx": intr.fx,
        "fy": intr.fy,
        "distortionModel": str(intr.model),
        "coeffs": list(intr.coeffs),
    }


def create_video_writer(path: Path, width: int, height: int, fps: int) -> cv2.VideoWriter:
    candidates = (
        ("mp4v", ".mp4"),
        ("avc1", ".mp4"),
    )
    for fourcc_name, suffix in candidates:
        target = path if path.suffix == suffix else path.with_suffix(suffix)
        writer = cv2.VideoWriter(
            str(target),
            cv2.VideoWriter_fourcc(*fourcc_name),
            float(fps),
            (width, height),
        )
        if writer.isOpened():
            return writer
        writer.release()
    raise RuntimeError("Failed to open a video writer. OpenCV on this machine cannot encode mp4 rgb output.")


def metadata_value(frame: Any, key: Any) -> str:
    try:
        if frame.supports_frame_metadata(key):
            return str(frame.get_frame_metadata(key))
    except Exception:
        return ""
    return ""


def frame_timestamp_domain(frame: Any) -> str:
    try:
        return str(frame.get_frame_timestamp_domain())
    except Exception:
        return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture RGB/depth from an Intel RealSense D435 / D435i into one run directory.")
    parser.add_argument("--run-dir", help="Existing runs/<run_id> directory. If omitted, --output-dir must be provided.")
    parser.add_argument("--output-dir", help="Explicit output directory. Defaults to <run-dir>/global_camera.")
    parser.add_argument("--run-id", default="", help="Optional run id for metadata.")
    parser.add_argument("--serial", help="Target device serial. Defaults to the first visible device.")
    parser.add_argument("--duration-s", type=float, default=10.0, help="Capture duration in seconds.")
    parser.add_argument("--stream", action="store_true", help="Run a foreground live debug stream instead of recording rgb.mp4/depth.raw.")
    parser.add_argument("--preview-dir", help="If set, continuously refresh latest_rgb.jpg/latest_depth.png for live inspection.")
    parser.add_argument("--record-control-file", help="JSON command file for controlled stream recording start/stop.")
    parser.add_argument("--status-file", help="JSON status file refreshed by controlled stream mode.")
    parser.add_argument("--recording-preroll-s", type=float, default=DEFAULT_RECORDING_PREROLL_S, help="Seconds of buffered frames to prepend when controlled recording starts.")
    parser.add_argument("--recording-postroll-s", type=float, default=DEFAULT_RECORDING_POSTROLL_S, help="Seconds to keep writing after a controlled recording stop.")
    parser.add_argument("--stream-buffer-s", type=float, default=DEFAULT_STREAM_BUFFER_S, help="Seconds of live frames retained for controlled recording pre-roll.")
    parser.add_argument("--preview-every-n-frames", type=int, default=DEFAULT_PREVIEW_EVERY_N_FRAMES, help="How often to refresh preview images.")
    parser.add_argument("--status-interval-s", type=float, default=DEFAULT_STATUS_INTERVAL_S, help="How often to print live stream status JSON.")
    parser.add_argument("--frame-timeout-ms", type=int, default=DEFAULT_FRAME_TIMEOUT_MS, help="Maximum wait for one RGB/depth frameset before reporting a stream timeout.")
    parser.add_argument("--startup-recovery-attempts", type=int, default=DEFAULT_STARTUP_RECOVERY_ATTEMPTS, help="Hardware-reset retries allowed before the first stream frame arrives.")
    parser.add_argument("--startup-reset-delay-s", type=float, default=DEFAULT_STARTUP_RESET_DELAY_S, help="Delay after a startup hardware reset before reopening the pipeline.")
    parser.add_argument("--max-frames", type=int, default=0, help="Optional hard cap on frames to process. 0 means no cap.")
    parser.add_argument("--rgb-width", type=int, default=DEFAULT_RGB_WIDTH)
    parser.add_argument("--rgb-height", type=int, default=DEFAULT_RGB_HEIGHT)
    parser.add_argument("--rgb-fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--depth-width", type=int, default=DEFAULT_DEPTH_WIDTH)
    parser.add_argument("--depth-height", type=int, default=DEFAULT_DEPTH_HEIGHT)
    parser.add_argument("--depth-fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--warmup-frames", type=int, default=30, help="Frames to drop before writing files.")
    parser.add_argument("--notes", default="", help="Freeform notes stored in camera_metadata.json.")
    parser.add_argument("--list-devices", action="store_true", help="Print visible RealSense devices and exit.")
    return parser.parse_args()


def resolve_output_dir(args: argparse.Namespace) -> Path | None:
    if args.output_dir:
        return Path(args.output_dir).expanduser().resolve()
    if args.run_dir:
        return (Path(args.run_dir).expanduser().resolve() / "global_camera")
    if args.stream:
        return None
    raise SystemExit("Provide either --run-dir or --output-dir.")


def ensure_output_dir(path: Path | None) -> None:
    if path is None:
        return
    path.mkdir(parents=True, exist_ok=True)


def list_devices_main() -> None:
    devices = enumerate_devices()
    print(json.dumps({"devices": devices}, indent=2, ensure_ascii=False))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def cv2_imwrite_atomic(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.stem}.{time.monotonic_ns()}.tmp{path.suffix}")
    try:
        ok = cv2.imwrite(str(tmp_path), image)
    except cv2.error:
        ok = False
    if not ok:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return
    os.replace(tmp_path, path)


def read_control_request(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "command": "control_error",
            "request_id": f"decode_error:{stat.st_mtime_ns}",
            "error": str(exc),
            "_mtime_ns": stat.st_mtime_ns,
        }
    if not isinstance(payload, dict):
        return {
            "command": "control_error",
            "request_id": f"type_error:{stat.st_mtime_ns}",
            "error": "control file must contain a JSON object",
            "_mtime_ns": stat.st_mtime_ns,
        }
    payload["_mtime_ns"] = stat.st_mtime_ns
    return payload


def write_metadata_if_needed(args: argparse.Namespace, metadata: dict[str, Any], output_dir: Path | None) -> None:
    if output_dir is not None:
        write_json(output_dir / "camera_metadata.json", metadata)
    if args.run_dir and output_dir is not None:
        update_run_metadata(Path(args.run_dir).expanduser().resolve(), metadata, output_dir)


def normalize_depth_for_preview(depth_image: np.ndarray) -> np.ndarray:
    if depth_image.size == 0:
        return np.zeros((1, 1, 3), dtype=np.uint8)
    nonzero = depth_image[depth_image > 0]
    if nonzero.size == 0:
        depth_vis = np.zeros(depth_image.shape, dtype=np.uint8)
    else:
        near = float(np.percentile(nonzero, 5))
        far = float(np.percentile(nonzero, 95))
        if far <= near:
            far = near + 1.0
        clipped = np.clip(depth_image.astype(np.float32), near, far)
        scaled = ((clipped - near) / (far - near) * 255.0).astype(np.uint8)
        scaled[depth_image == 0] = 0
        depth_vis = scaled
    return cv2.applyColorMap(depth_vis, cv2.COLORMAP_TURBO)


def write_preview_images(preview_dir: Path, color_image: np.ndarray, depth_image: np.ndarray) -> None:
    cv2_imwrite_atomic(preview_dir / "latest_rgb.jpg", color_image)
    cv2_imwrite_atomic(preview_dir / "latest_depth.png", normalize_depth_for_preview(depth_image))


def start_stream_pipeline_with_recovery(
    *,
    start_attempt: Any,
    frame_timeout_ms: int,
    recovery_attempts: int,
    reset_delay_s: float,
    status_callback: Any | None = None,
    sleep_fn: Any = time.sleep,
) -> tuple[Any, Any, Any]:
    total_attempts = max(int(recovery_attempts), 0) + 1
    last_error: Exception | None = None
    for attempt_index in range(total_attempts):
        pipeline = None
        profile = None
        try:
            pipeline, profile = start_attempt()
            frames = pipeline.wait_for_frames(timeout_ms=max(int(frame_timeout_ms), 1))
            return pipeline, profile, frames
        except Exception as exc:  # noqa: BLE001 - librealsense raises runtime-specific exceptions
            last_error = exc
            if pipeline is not None:
                try:
                    pipeline.stop()
                except Exception:
                    pass
            if attempt_index + 1 >= total_attempts or STOP_REQUESTED:
                break

            reset_error = None
            if profile is not None:
                try:
                    profile.get_device().hardware_reset()
                except Exception as reset_exc:  # noqa: BLE001
                    reset_error = f"{type(reset_exc).__name__}: {reset_exc}"
            recovery_number = attempt_index + 1
            event = {
                "event": "stream_startup_recovery",
                "attempt": recovery_number,
                "maxRecoveryAttempts": max(total_attempts - 1, 0),
                "error": f"{type(exc).__name__}: {exc}",
                "hardwareResetError": reset_error,
            }
            print(json.dumps(event, ensure_ascii=False), flush=True)
            if status_callback is not None:
                status_callback(recovery_number, event["error"], reset_error)
            sleep_fn(max(float(reset_delay_s), 0.0))

    detail = f"{type(last_error).__name__}: {last_error}" if last_error is not None else "unknown startup error"
    raise RuntimeError(f"D435 stream startup failed after {total_attempts} attempt(s): {detail}") from last_error


def restart_idle_stream_pipeline(
    *,
    pipeline: Any,
    profile: Any,
    restart_stream: Any,
    reset_delay_s: float,
    sleep_fn: Any = time.sleep,
) -> tuple[Any, Any, Any]:
    try:
        pipeline.stop()
    except Exception:
        pass
    reset_error = None
    try:
        profile.get_device().hardware_reset()
    except Exception as exc:  # noqa: BLE001
        reset_error = f"{type(exc).__name__}: {exc}"
    print(
        json.dumps(
            {
                "event": "idle_stream_runtime_recovery",
                "hardwareResetError": reset_error,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    sleep_fn(max(float(reset_delay_s), 0.0))
    return restart_stream()


@dataclass(frozen=True)
class BufferedD435Frame:
    stream_frame_id: int
    host_receive_time_s: float
    host_receive_monotonic_ns: int
    color_image: np.ndarray
    depth_image: np.ndarray
    metadata: dict[str, Any]


class D435FrameRingBuffer:
    def __init__(self, *, max_age_s: float, max_items: int):
        self.max_age_s = max(0.0, float(max_age_s))
        self.max_items = max(0, int(max_items))
        self.frames: deque[BufferedD435Frame] = deque()
        self.total_appended = 0
        self.total_pruned = 0

    def append(self, frame: BufferedD435Frame) -> None:
        self.frames.append(frame)
        self.total_appended += 1
        min_host_time_s = frame.host_receive_time_s - self.max_age_s
        while self.frames and self.frames[0].host_receive_time_s < min_host_time_s:
            self.frames.popleft()
            self.total_pruned += 1
        while self.max_items > 0 and len(self.frames) > self.max_items:
            self.frames.popleft()
            self.total_pruned += 1

    def since_host_time_s(self, start_time_s: float) -> list[BufferedD435Frame]:
        return [frame for frame in self.frames if frame.host_receive_time_s >= start_time_s]

    def stats(self) -> dict[str, Any]:
        first = self.frames[0].host_receive_time_s if self.frames else None
        latest = self.frames[-1].host_receive_time_s if self.frames else None
        return {
            "frames": len(self.frames),
            "maxAgeS": self.max_age_s,
            "maxItems": self.max_items,
            "firstHostReceiveTimeS": first,
            "latestHostReceiveTimeS": latest,
            "spanMs": (latest - first) * 1000.0 if isinstance(first, float) and isinstance(latest, float) else None,
            "totalAppended": self.total_appended,
            "totalPruned": self.total_pruned,
        }


def build_frame_metadata(color_frame: Any, depth_frame: Any) -> dict[str, Any]:
    return {
        "realsense_frame_timestamp_ms": f"{color_frame.get_timestamp():.3f}",
        "realsense_timestamp_domain": frame_timestamp_domain(color_frame),
        "color_sensor_timestamp_us": metadata_value(color_frame, rs.frame_metadata_value.sensor_timestamp),
        "depth_sensor_timestamp_us": metadata_value(depth_frame, rs.frame_metadata_value.sensor_timestamp),
        "color_frame_number": color_frame.get_frame_number(),
        "depth_frame_number": depth_frame.get_frame_number(),
    }


def make_buffered_d435_frame(
    *,
    stream_frame_id: int,
    color_frame: Any,
    depth_frame: Any,
    color_image: np.ndarray,
    depth_image: np.ndarray,
    host_receive_monotonic_ns: int,
    host_receive_time_s: float,
) -> BufferedD435Frame:
    return BufferedD435Frame(
        stream_frame_id=stream_frame_id,
        host_receive_time_s=host_receive_time_s,
        host_receive_monotonic_ns=host_receive_monotonic_ns,
        color_image=color_image.copy(),
        depth_image=depth_image.copy(),
        metadata=build_frame_metadata(color_frame, depth_frame),
    )


class D435ContinuousFrameAcquirer:
    """Continuously drain the armed RealSense pipeline independently of I/O."""

    def __init__(
        self,
        *,
        pipeline: Any,
        frame_timeout_ms: int,
        queue_capacity: int,
        initial_frames: Any | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.frame_timeout_ms = max(int(frame_timeout_ms), 1)
        self.initial_frames = initial_frames
        self.frames: queue.Queue[BufferedD435Frame] = queue.Queue(
            maxsize=max(int(queue_capacity), 1)
        )
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.acquired_frame_count = 0
        self.max_queue_depth = 0

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.error = None
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="umift-d435-acquisition",
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        pending = self.initial_frames
        self.initial_frames = None
        try:
            while not self.stop_event.is_set() and not STOP_REQUESTED:
                if pending is not None:
                    frames = pending
                    pending = None
                else:
                    frames = self.pipeline.wait_for_frames(
                        timeout_ms=self.frame_timeout_ms
                    )
                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()
                if not color_frame or not depth_frame:
                    continue
                host_receive_monotonic_ns = time.monotonic_ns()
                host_receive_time_s = time.time()
                color_image = np.asanyarray(color_frame.get_data())
                depth_image = np.asanyarray(depth_frame.get_data())
                self.acquired_frame_count += 1
                buffered = make_buffered_d435_frame(
                    stream_frame_id=self.acquired_frame_count,
                    color_frame=color_frame,
                    depth_frame=depth_frame,
                    color_image=color_image,
                    depth_image=depth_image,
                    host_receive_monotonic_ns=host_receive_monotonic_ns,
                    host_receive_time_s=host_receive_time_s,
                )
                try:
                    self.frames.put(
                        buffered,
                        timeout=max(self.frame_timeout_ms / 1000.0, 0.1),
                    )
                except queue.Full as exc:
                    raise RuntimeError(
                        "D435 acquisition queue overflow; refusing silent frame loss"
                    ) from exc
                self.max_queue_depth = max(self.max_queue_depth, self.frames.qsize())
        except BaseException as exc:  # noqa: BLE001 - surfaced to control loop
            if not self.stop_event.is_set():
                self.error = exc

    def get(self, timeout_s: float) -> BufferedD435Frame:
        return self.frames.get(timeout=max(float(timeout_s), 0.1))

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.pipeline.stop()
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": bool(self.thread is not None and self.thread.is_alive()),
            "acquiredFrameCount": self.acquired_frame_count,
            "queueDepth": self.frames.qsize(),
            "queueCapacity": self.frames.maxsize,
            "maxQueueDepth": self.max_queue_depth,
            "error": f"{type(self.error).__name__}: {self.error}"
            if self.error is not None
            else None,
        }


def control_float(request: dict[str, Any], key: str, default: float) -> float:
    value = request.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return default
    return default


def preroll_start_host_time_s(request: dict[str, Any], args: argparse.Namespace) -> float:
    preroll_s = control_float(request, "recording_preroll_s", args.recording_preroll_s)
    event_unix_ns = request.get("event_aligned_unix_ns")
    if isinstance(event_unix_ns, int):
        return event_unix_ns / 1_000_000_000.0 - max(0.0, preroll_s)
    return time.time() - max(0.0, preroll_s)


def emit_stream_status(
    *,
    frame_index: int,
    started_monotonic: float,
    host_receive_time_s: float,
    frame_metadata: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    elapsed_s = max(time.monotonic() - started_monotonic, 1e-6)
    payload = {
        "event": "stream_status",
        "frameIndex": frame_index,
        "elapsedS": round(elapsed_s, 3),
        "approxFps": round(frame_index / elapsed_s, 2),
        "hostReceiveTimeS": round(host_receive_time_s, 6),
        "colorTimestampMs": float(frame_metadata.get("realsense_frame_timestamp_ms") or 0.0),
        "depthFrameNumber": int(frame_metadata.get("depth_frame_number") or 0),
        "colorFrameNumber": int(frame_metadata.get("color_frame_number") or 0),
        "rgb": {
            "width": args.rgb_width,
            "height": args.rgb_height,
            "fps": args.rgb_fps,
        },
        "depth": {
            "width": args.depth_width,
            "height": args.depth_height,
            "fps": args.depth_fps,
        },
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def build_session_metadata(
    *,
    args: argparse.Namespace,
    device: dict[str, Any],
    profile: Any,
    output_dir: Path,
    control_request: dict[str, Any],
) -> dict[str, Any]:
    metadata = build_metadata(args, device, output_dir)
    run_id = control_request.get("run_id")
    if isinstance(run_id, str) and run_id:
        metadata["runContext"]["runId"] = run_id
    metadata["runContext"]["recordControl"] = {
        "controlFile": args.record_control_file or "",
        "requestId": control_request.get("request_id"),
        "episodeIndex": control_request.get("episode_index"),
        "trigger": control_request.get("trigger", "control_file"),
        "eventAlignedMonotonicNs": control_request.get("event_aligned_monotonic_ns"),
        "eventAlignedUnixNs": control_request.get("event_aligned_unix_ns"),
        "recordingPrerollS": control_request.get("recording_preroll_s", args.recording_preroll_s),
    }
    depth_sensor = profile.get_device().first_depth_sensor()
    metadata["streams"]["depth"]["depthScaleMeters"] = depth_sensor.get_depth_scale()
    metadata["streams"]["rgb"]["intrinsics"] = maybe_intrinsics(profile, rs.stream.color)
    metadata["streams"]["depth"]["intrinsics"] = maybe_intrinsics(profile, rs.stream.depth)
    return metadata


def open_recording_session(
    *,
    args: argparse.Namespace,
    device: dict[str, Any],
    profile: Any,
    output_dir: Path,
    control_request: dict[str, Any],
) -> dict[str, Any]:
    if output_dir.exists():
        reservation_path = output_dir / D435_EPISODE_RESERVATION_FILE
        if not reservation_path.is_file():
            raise FileExistsError(f"D435 output directory already exists without reservation: {output_dir}")
        protected_outputs = ("rgb.mp4", "depth.raw", "frame_timestamps.csv")
        existing_outputs = [name for name in protected_outputs if (output_dir / name).exists()]
        if existing_outputs:
            raise FileExistsError(
                f"D435 reserved output directory already contains capture files: {existing_outputs}"
            )
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
    metadata = build_session_metadata(
        args=args,
        device=device,
        profile=profile,
        output_dir=output_dir,
        control_request=control_request,
    )
    write_metadata_if_needed(args, metadata, output_dir)

    rgb_writer = None
    depth_file = None
    csv_file = None
    try:
        rgb_writer = create_video_writer(output_dir / "rgb.mp4", args.rgb_width, args.rgb_height, args.rgb_fps)
        depth_file = (output_dir / "depth.raw").open("xb")
        csv_file = (output_dir / "frame_timestamps.csv").open("x", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(csv_file, fieldnames=FRAME_TIMESTAMP_FIELDS)
        csv_writer.writeheader()
    except Exception:
        if rgb_writer is not None:
            rgb_writer.release()
        if depth_file is not None:
            depth_file.close()
        if csv_file is not None:
            csv_file.close()
        raise

    capture_log: dict[str, Any] = {
        "startedAt": iso_now(),
        "serial": device.get("serial", ""),
        "status": "running",
        "requestedDurationS": args.duration_s,
        "framesDroppedWarmup": 0,
        "startedMonotonicNs": time.monotonic_ns(),
        "controlRequest": {
            "requestId": control_request.get("request_id"),
            "episodeIndex": control_request.get("episode_index"),
            "trigger": control_request.get("trigger", "control_file"),
            "eventAlignedMonotonicNs": control_request.get("event_aligned_monotonic_ns"),
            "eventAlignedUnixNs": control_request.get("event_aligned_unix_ns"),
            "recordingPrerollS": control_request.get("recording_preroll_s", args.recording_preroll_s),
        },
        "preroll": {
            "framesWritten": 0,
            "startHostReceiveTimeS": None,
            "bufferStatsAtStart": {},
        },
    }
    write_json(output_dir / "capture_log.json", capture_log)
    return {
        "output_dir": output_dir,
        "metadata": metadata,
        "capture_log": capture_log,
        "rgb_writer": rgb_writer,
        "depth_file": depth_file,
        "csv_file": csv_file,
        "csv_writer": csv_writer,
        "frame_index": 0,
    }


def write_recording_frame(
    *,
    args: argparse.Namespace,
    session: dict[str, Any],
    color_frame: Any | None,
    depth_frame: Any | None,
    color_image: np.ndarray,
    depth_image: np.ndarray,
    host_receive_monotonic_ns: int,
    host_receive_time_s: float,
    frame_metadata: dict[str, Any] | None = None,
) -> None:
    frame_index = int(session["frame_index"])
    metadata = frame_metadata or build_frame_metadata(color_frame, depth_frame)
    integrity = session.setdefault(
        "sensor_integrity",
        {
            "color_missing_frames": 0,
            "depth_missing_frames": 0,
            "color_out_of_order_frames": 0,
            "depth_out_of_order_frames": 0,
            "first_color_frame_number": None,
            "last_color_frame_number": None,
            "first_depth_frame_number": None,
            "last_depth_frame_number": None,
        },
    )
    for side in ("color", "depth"):
        value = metadata.get(f"{side}_frame_number")
        if not isinstance(value, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
        if value is None:
            continue
        first_key = f"first_{side}_frame_number"
        last_key = f"last_{side}_frame_number"
        missing_key = f"{side}_missing_frames"
        out_of_order_key = f"{side}_out_of_order_frames"
        previous = integrity.get(last_key)
        if integrity.get(first_key) is None:
            integrity[first_key] = value
        if isinstance(previous, int):
            if value > previous + 1:
                integrity[missing_key] = int(integrity.get(missing_key) or 0) + value - previous - 1
            elif value <= previous:
                integrity[out_of_order_key] = int(integrity.get(out_of_order_key) or 0) + 1
        integrity[last_key] = value
    session["rgb_writer"].write(color_image)
    depth_image.astype(np.uint16, copy=False).tofile(session["depth_file"])
    session["csv_writer"].writerow(
        {
            "frame_index": frame_index,
            "host_receive_time_s": f"{host_receive_time_s:.9f}",
            "host_receive_monotonic_ns": host_receive_monotonic_ns,
            "realsense_frame_timestamp_ms": metadata.get("realsense_frame_timestamp_ms", ""),
            "realsense_timestamp_domain": metadata.get("realsense_timestamp_domain", ""),
            "color_sensor_timestamp_us": metadata.get("color_sensor_timestamp_us", ""),
            "depth_sensor_timestamp_us": metadata.get("depth_sensor_timestamp_us", ""),
            "color_frame_number": metadata.get("color_frame_number", ""),
            "depth_frame_number": metadata.get("depth_frame_number", ""),
        }
    )
    session["frame_index"] = frame_index + 1


def close_recording_session(session: dict[str, Any], *, status: str) -> None:
    output_dir = session["output_dir"]
    try:
        session["rgb_writer"].release()
    finally:
        session["depth_file"].close()
        session["csv_file"].close()

    capture_log = dict(session["capture_log"])
    capture_log.update(
        {
            "status": status,
            "completedAt": iso_now(),
            "completedMonotonicNs": time.monotonic_ns(),
            "frameCount": int(session["frame_index"]),
            "depthFrameShape": [session["metadata"]["streams"]["depth"]["height"], session["metadata"]["streams"]["depth"]["width"]],
            "depthBytes": (output_dir / "depth.raw").stat().st_size if (output_dir / "depth.raw").exists() else 0,
            "sensorIntegrity": dict(session.get("sensor_integrity", {})),
        }
    )
    write_json(output_dir / "capture_log.json", capture_log)
    write_json(output_dir / "camera_metadata.json", session["metadata"])
    print(f"Wrote capture to {output_dir}", flush=True)
    print(f"Frames captured: {session['frame_index']}", flush=True)


def run_stream(
    *,
    pipeline: Any,
    args: argparse.Namespace,
    device: dict[str, Any],
    profile: Any,
    output_dir: Path | None,
    initial_frames: Any | None = None,
    restart_stream: Any | None = None,
) -> None:
    preview_dir = Path(args.preview_dir).expanduser().resolve() if args.preview_dir else output_dir
    control_file = Path(args.record_control_file).expanduser().resolve() if args.record_control_file else None
    status_file = Path(args.status_file).expanduser().resolve() if args.status_file else None
    warmup_remaining = max(args.warmup_frames, 0)
    started_monotonic = time.monotonic()
    deadline = None if args.duration_s <= 0 else started_monotonic + args.duration_s
    last_status_at = started_monotonic
    frame_index = 0
    preview_stride = max(args.preview_every_n_frames, 1)
    recording_session: dict[str, Any] | None = None
    pending_stop: dict[str, Any] | None = None
    buffer_max_items = max(1, int(max(args.rgb_fps, args.depth_fps, 1) * max(args.stream_buffer_s, 0.1) * 2))
    frame_buffer = D435FrameRingBuffer(max_age_s=args.stream_buffer_s, max_items=buffer_max_items)
    acquirer = D435ContinuousFrameAcquirer(
        pipeline=pipeline,
        frame_timeout_ms=args.frame_timeout_ms,
        queue_capacity=buffer_max_items,
        initial_frames=initial_frames,
    )
    last_recorded_stream_frame_id = -1
    last_request_marker: str | None = None
    latest_command = "idle"
    last_error: str | None = None
    runtime_recovery_count = 0

    def write_status(force: bool = False) -> None:
        if status_file is None:
            return
        now = time.monotonic()
        if not force and args.status_interval_s > 0 and now - last_status_at < args.status_interval_s:
            return
        elapsed_s = max(now - started_monotonic, 1e-6)
        payload = {
            "event": "controlled_stream_status",
            "updatedAt": iso_now(),
            "hostMonotonicNs": time.monotonic_ns(),
            "hostUnixNs": time.time_ns(),
            "running": True,
            "streamFrameCount": frame_index,
            "approxFps": round(frame_index / elapsed_s, 2),
            "previewDir": str(preview_dir) if preview_dir is not None else "",
            "controlFile": str(control_file) if control_file is not None else "",
            "latestCommand": latest_command,
            "lastRequestId": last_request_marker,
            "lastError": last_error,
            "runtimeRecoveryCount": runtime_recovery_count,
            "recordingActive": recording_session is not None,
            "recordingOutputDir": str(recording_session["output_dir"]) if recording_session is not None else "",
            "recordingFrameCount": int(recording_session["frame_index"]) if recording_session is not None else 0,
            "buffer": frame_buffer.stats(),
            "acquisition": acquirer.snapshot(),
        }
        write_json_atomic(status_file, payload)

    def write_buffered_frame_to_session(frame: BufferedD435Frame) -> bool:
        nonlocal last_recorded_stream_frame_id
        if recording_session is None or frame.stream_frame_id <= last_recorded_stream_frame_id:
            return False
        write_recording_frame(
            args=args,
            session=recording_session,
            color_frame=None,
            depth_frame=None,
            color_image=frame.color_image,
            depth_image=frame.depth_image,
            host_receive_monotonic_ns=frame.host_receive_monotonic_ns,
            host_receive_time_s=frame.host_receive_time_s,
            frame_metadata=frame.metadata,
        )
        last_recorded_stream_frame_id = frame.stream_frame_id
        return True

    def handle_control_request(request: dict[str, Any]) -> None:
        nonlocal recording_session, pending_stop, last_request_marker, latest_command, last_error, last_recorded_stream_frame_id
        marker_value = request.get("request_id")
        marker = str(marker_value) if marker_value not in (None, "") else str(request.get("_mtime_ns", ""))
        if not marker or marker == last_request_marker:
            return
        last_request_marker = marker
        command = str(request.get("command") or "").strip().lower()
        latest_command = command or "idle"
        last_error = None

        try:
            if command == "start":
                raw_output_dir = request.get("output_dir")
                if not isinstance(raw_output_dir, str) or not raw_output_dir:
                    raise ValueError("start command is missing output_dir")
                if recording_session is not None:
                    raise RuntimeError(
                        "D435 recording is already active; stop it before starting another episode"
                    )
                recording_session = open_recording_session(
                    args=args,
                    device=device,
                    profile=profile,
                    output_dir=Path(raw_output_dir).expanduser().resolve(),
                    control_request=request,
                )
                pending_stop = None
                last_recorded_stream_frame_id = -1
                start_host_time_s = preroll_start_host_time_s(request, args)
                buffered_frames = frame_buffer.since_host_time_s(start_host_time_s)
                preroll_written = 0
                for buffered_frame in buffered_frames:
                    if write_buffered_frame_to_session(buffered_frame):
                        preroll_written += 1
                if recording_session is not None:
                    recording_session["capture_log"]["preroll"] = {
                        "framesWritten": preroll_written,
                        "startHostReceiveTimeS": start_host_time_s,
                        "bufferStatsAtStart": frame_buffer.stats(),
                    }
                    write_json(recording_session["output_dir"] / "capture_log.json", recording_session["capture_log"])
                print(
                    json.dumps(
                        {
                            "event": "recording_started",
                            "requestId": marker,
                            "outputDir": str(recording_session["output_dir"]),
                            "prerollFramesWritten": preroll_written,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            elif command == "stop":
                if recording_session is not None:
                    postroll_s = max(
                        0.0,
                        control_float(request, "recording_postroll_s", args.recording_postroll_s),
                    )
                    pending_stop = {
                        "request_id": marker,
                        "deadline_monotonic": time.monotonic() + postroll_s,
                        "postroll_s": postroll_s,
                        "output_dir": str(recording_session["output_dir"]),
                        "frame_count_at_request": int(recording_session["frame_index"]),
                    }
                    print(
                        json.dumps(
                            {
                                "event": "recording_stop_scheduled",
                                "requestId": marker,
                                "outputDir": pending_stop["output_dir"],
                                "postrollS": postroll_s,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    print(json.dumps({"event": "recording_stop_ignored", "requestId": marker}, ensure_ascii=False), flush=True)
            elif command in {"", "idle", "noop"}:
                pass
            elif command == "control_error":
                last_error = str(request.get("error") or "invalid control file")
            else:
                last_error = f"unknown command: {command}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(json.dumps({"event": "recording_control_error", "requestId": marker, "error": last_error}, ensure_ascii=False), flush=True)
        write_status(force=True)

    try:
        write_status(force=True)
        acquirer.start()
        while True:
            if STOP_REQUESTED:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if args.max_frames > 0 and frame_index >= args.max_frames:
                break

            if control_file is not None:
                request = read_control_request(control_file)
                if request is not None:
                    handle_control_request(request)

            try:
                buffered_frame = acquirer.get(
                    max(float(args.frame_timeout_ms) / 1000.0, 0.1)
                )
            except queue.Empty as timeout_exc:
                exc = acquirer.error or RuntimeError("D435 acquisition queue timed out")
                if recording_session is not None or restart_stream is None or STOP_REQUESTED:
                    last_error = f"{type(exc).__name__}: {exc}"
                    write_status(force=True)
                    raise exc from timeout_exc
                runtime_recovery_count += 1
                last_error = f"{type(exc).__name__}: {exc}"
                write_status(force=True)
                print(
                    json.dumps(
                        {
                            "event": "idle_stream_timeout",
                            "recoveryCount": runtime_recovery_count,
                            "error": last_error,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                acquirer.stop()
                pipeline, profile, pending_frames = restart_idle_stream_pipeline(
                    pipeline=pipeline,
                    profile=profile,
                    restart_stream=restart_stream,
                    reset_delay_s=args.startup_reset_delay_s,
                )
                frame_buffer = D435FrameRingBuffer(max_age_s=args.stream_buffer_s, max_items=buffer_max_items)
                last_recorded_stream_frame_id = -1
                last_error = None
                acquirer = D435ContinuousFrameAcquirer(
                    pipeline=pipeline,
                    frame_timeout_ms=args.frame_timeout_ms,
                    queue_capacity=buffer_max_items,
                    initial_frames=pending_frames,
                )
                acquirer.start()
                write_status(force=True)
                continue

            if warmup_remaining > 0:
                warmup_remaining -= 1
                continue

            frame_index += 1
            frame_buffer.append(buffered_frame)
            color_image = buffered_frame.color_image
            depth_image = buffered_frame.depth_image
            host_receive_time_s = buffered_frame.host_receive_time_s

            if recording_session is not None:
                write_buffered_frame_to_session(buffered_frame)

            if recording_session is not None and pending_stop is not None:
                if time.monotonic() >= float(pending_stop["deadline_monotonic"]):
                    postroll_frames = int(recording_session["frame_index"]) - int(
                        pending_stop["frame_count_at_request"]
                    )
                    recording_session["capture_log"]["postroll"] = {
                        "seconds": pending_stop["postroll_s"],
                        "framesWritten": postroll_frames,
                        "requestId": pending_stop["request_id"],
                    }
                    output_text = str(recording_session["output_dir"])
                    request_id = str(pending_stop["request_id"])
                    recording_session["capture_log"]["acquisitionAtClose"] = acquirer.snapshot()
                    close_recording_session(recording_session, status="stopped")
                    recording_session = None
                    pending_stop = None
                    print(
                        json.dumps(
                            {
                                "event": "recording_stopped",
                                "requestId": request_id,
                                "outputDir": output_text,
                                "postrollFramesWritten": postroll_frames,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    write_status(force=True)

            if preview_dir is not None and frame_index % preview_stride == 0:
                write_preview_images(preview_dir, color_image, depth_image)

            now = time.monotonic()
            if frame_index == 1 or (args.status_interval_s > 0 and now - last_status_at >= args.status_interval_s):
                emit_stream_status(
                    frame_index=frame_index,
                    started_monotonic=started_monotonic,
                    host_receive_time_s=host_receive_time_s,
                    frame_metadata=buffered_frame.metadata,
                    args=args,
                )
                last_status_at = now
                write_status(force=True)
    finally:
        if recording_session is not None:
            recording_session["capture_log"]["acquisitionAtClose"] = acquirer.snapshot()
            close_recording_session(recording_session, status="interrupted")
            recording_session = None
            write_status(force=True)
        acquirer.stop()

    summary = {
        "event": "stream_completed",
        "frameCount": frame_index,
        "durationS": round(max(time.monotonic() - started_monotonic, 0.0), 3),
        "previewDir": str(preview_dir) if preview_dir is not None else "",
    }
    if status_file is not None:
        write_json_atomic(
            status_file,
            {
                **summary,
                "updatedAt": iso_now(),
                "hostMonotonicNs": time.monotonic_ns(),
                "hostUnixNs": time.time_ns(),
                "running": False,
                "recordingActive": False,
                "lastRequestId": last_request_marker,
                "lastError": last_error,
            },
        )
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def update_run_metadata(run_dir: Path, camera_metadata: dict[str, Any], output_dir: Path) -> None:
    metadata_path = run_dir / "RUN_METADATA.json"
    if not metadata_path.exists():
        return

    run_meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    hardware = run_meta.setdefault("hardware", {})
    outputs = run_meta.setdefault("outputs", {})
    hardware["global_camera"] = {
        "model": camera_metadata["device"]["model"],
        "serial": camera_metadata["device"]["serial"],
        "rgb_enabled": True,
        "depth_enabled": True,
        "imu_enabled": False,
        "rgb_width": camera_metadata["streams"]["rgb"]["width"],
        "rgb_height": camera_metadata["streams"]["rgb"]["height"],
        "rgb_fps": camera_metadata["streams"]["rgb"]["fps"],
        "depth_width": camera_metadata["streams"]["depth"]["width"],
        "depth_height": camera_metadata["streams"]["depth"]["height"],
        "depth_fps": camera_metadata["streams"]["depth"]["fps"],
    }
    outputs["global_camera_dir"] = str(output_dir)
    metadata_path.write_text(json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    args = parse_args()
    if args.list_devices:
        list_devices_main()
        return

    require_realsense()
    output_dir = resolve_output_dir(args)
    if args.stream:
        ensure_output_dir(output_dir)
    elif output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=False)

    device = select_device(args.serial)
    metadata = build_metadata(args, device, output_dir)
    write_metadata_if_needed(args, metadata, output_dir)

    def start_pipeline() -> tuple[Any, Any]:
        candidate_pipeline = rs.pipeline()
        candidate_config = rs.config()
        if device.get("serial"):
            candidate_config.enable_device(device["serial"])
        candidate_config.enable_stream(rs.stream.color, args.rgb_width, args.rgb_height, rs.format.bgr8, args.rgb_fps)
        candidate_config.enable_stream(rs.stream.depth, args.depth_width, args.depth_height, rs.format.z16, args.depth_fps)
        return candidate_pipeline, candidate_pipeline.start(candidate_config)

    initial_frames = None
    if args.stream:
        status_file = Path(args.status_file).expanduser().resolve() if args.status_file else None

        def write_startup_recovery_status(attempt: int, error: str, reset_error: str | None) -> None:
            if status_file is None:
                return
            write_json_atomic(
                status_file,
                {
                    "event": "controlled_stream_startup_recovery",
                    "updatedAt": iso_now(),
                    "hostMonotonicNs": time.monotonic_ns(),
                    "hostUnixNs": time.time_ns(),
                    "running": True,
                    "streamFrameCount": 0,
                    "approxFps": 0.0,
                    "previewDir": str(Path(args.preview_dir).expanduser().resolve()) if args.preview_dir else "",
                    "controlFile": str(Path(args.record_control_file).expanduser().resolve()) if args.record_control_file else "",
                    "latestCommand": "startup_recovery",
                    "lastRequestId": None,
                    "lastError": None,
                    "startupRecoveryAttempt": attempt,
                    "startupRecoveryError": error,
                    "hardwareResetError": reset_error,
                    "recordingActive": False,
                    "recordingOutputDir": "",
                    "recordingFrameCount": 0,
                    "buffer": {},
                },
            )

        try:
            pipeline, profile, initial_frames = start_stream_pipeline_with_recovery(
                start_attempt=start_pipeline,
                frame_timeout_ms=args.frame_timeout_ms,
                recovery_attempts=args.startup_recovery_attempts,
                reset_delay_s=args.startup_reset_delay_s,
                status_callback=write_startup_recovery_status,
            )
        except Exception as exc:
            if status_file is not None:
                write_json_atomic(
                    status_file,
                    {
                        "event": "controlled_stream_startup_failed",
                        "updatedAt": iso_now(),
                        "hostMonotonicNs": time.monotonic_ns(),
                        "hostUnixNs": time.time_ns(),
                        "running": False,
                        "streamFrameCount": 0,
                        "approxFps": 0.0,
                        "previewDir": str(Path(args.preview_dir).expanduser().resolve()) if args.preview_dir else "",
                        "controlFile": str(Path(args.record_control_file).expanduser().resolve()) if args.record_control_file else "",
                        "latestCommand": "startup_failed",
                        "lastRequestId": None,
                        "lastError": f"{type(exc).__name__}: {exc}",
                        "recordingActive": False,
                        "recordingOutputDir": "",
                        "recordingFrameCount": 0,
                        "buffer": {},
                    },
                )
            raise
    else:
        pipeline, profile = start_pipeline()
    depth_sensor = profile.get_device().first_depth_sensor()
    metadata["streams"]["depth"]["depthScaleMeters"] = depth_sensor.get_depth_scale()
    metadata["streams"]["rgb"]["intrinsics"] = maybe_intrinsics(profile, rs.stream.color)
    metadata["streams"]["depth"]["intrinsics"] = maybe_intrinsics(profile, rs.stream.depth)
    write_metadata_if_needed(args, metadata, output_dir)

    if args.stream:
        def restart_stream() -> tuple[Any, Any, Any]:
            return start_stream_pipeline_with_recovery(
                start_attempt=start_pipeline,
                frame_timeout_ms=args.frame_timeout_ms,
                recovery_attempts=args.startup_recovery_attempts,
                reset_delay_s=args.startup_reset_delay_s,
            )

        run_stream(
            pipeline=pipeline,
            args=args,
            device=device,
            profile=profile,
            output_dir=output_dir,
            initial_frames=initial_frames,
            restart_stream=restart_stream,
        )
        return

    rgb_path = output_dir / "rgb.mp4"
    rgb_writer = create_video_writer(rgb_path, args.rgb_width, args.rgb_height, args.rgb_fps)
    depth_file = (output_dir / "depth.raw").open("xb")
    csv_file = (output_dir / "frame_timestamps.csv").open("x", newline="", encoding="utf-8")
    writer = csv.DictWriter(
        csv_file,
        fieldnames=FRAME_TIMESTAMP_FIELDS,
    )
    writer.writeheader()

    capture_log: dict[str, Any] = {
        "startedAt": iso_now(),
        "serial": device.get("serial", ""),
        "status": "running",
        "requestedDurationS": args.duration_s,
        "framesDroppedWarmup": int(args.warmup_frames),
        "startedMonotonicNs": time.monotonic_ns(),
    }
    write_json(output_dir / "capture_log.json", capture_log)

    frame_index = 0
    warmup_remaining = max(args.warmup_frames, 0)
    deadline = time.monotonic() + max(args.duration_s, 0.0)
    preview_dir = Path(args.preview_dir).expanduser().resolve() if args.preview_dir else None
    preview_stride = max(args.preview_every_n_frames, 1)

    try:
        while not STOP_REQUESTED and time.monotonic() < deadline:
            frames = pipeline.wait_for_frames(timeout_ms=5000)
            color_frame = frames.get_color_frame()
            depth_frame = frames.get_depth_frame()
            if not color_frame or not depth_frame:
                continue

            if warmup_remaining > 0:
                warmup_remaining -= 1
                continue

            host_receive_monotonic_ns = time.monotonic_ns()
            host_receive_time_s = time.time()
            color_image = np.asanyarray(color_frame.get_data())
            depth_image = np.asanyarray(depth_frame.get_data())

            rgb_writer.write(color_image)
            depth_image.astype(np.uint16, copy=False).tofile(depth_file)
            if preview_dir is not None and frame_index % preview_stride == 0:
                write_preview_images(preview_dir, color_image, depth_image)
            writer.writerow(
                {
                    "frame_index": frame_index,
                    "host_receive_time_s": f"{host_receive_time_s:.9f}",
                    "host_receive_monotonic_ns": host_receive_monotonic_ns,
                    "realsense_frame_timestamp_ms": f"{color_frame.get_timestamp():.3f}",
                    "realsense_timestamp_domain": frame_timestamp_domain(color_frame),
                    "color_sensor_timestamp_us": metadata_value(color_frame, rs.frame_metadata_value.sensor_timestamp),
                    "depth_sensor_timestamp_us": metadata_value(depth_frame, rs.frame_metadata_value.sensor_timestamp),
                    "color_frame_number": color_frame.get_frame_number(),
                    "depth_frame_number": depth_frame.get_frame_number(),
                }
            )
            frame_index += 1
    finally:
        rgb_writer.release()
        depth_file.close()
        csv_file.close()
        pipeline.stop()

    capture_log.update(
        {
            "status": "stopped" if STOP_REQUESTED else "completed",
            "completedAt": iso_now(),
            "completedMonotonicNs": time.monotonic_ns(),
            "frameCount": frame_index,
            "depthFrameShape": [args.depth_height, args.depth_width],
            "depthBytes": (output_dir / "depth.raw").stat().st_size if (output_dir / "depth.raw").exists() else 0,
        }
    )
    write_json(output_dir / "capture_log.json", capture_log)
    write_json(output_dir / "camera_metadata.json", metadata)
    print(f"Wrote capture to {output_dir}")
    print(f"Frames captured: {frame_index}")


if __name__ == "__main__":
    main()

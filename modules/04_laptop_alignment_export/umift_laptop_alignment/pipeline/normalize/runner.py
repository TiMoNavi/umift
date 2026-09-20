#!/usr/bin/env python3
"""Normalize raw run artifacts into stream-specific JSONL files.

This script does not resample or align streams.  It only converts each source
into a common timestamped row shape so later alignment/export stages can be
format-agnostic.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Union

from umift_laptop_alignment.orchestration.run_layout import append_sync_log, ensure_run_layout, json_line, register_artifact, relpath, utc_now_iso, write_json
from umift_laptop_alignment.capture.receivers.coinft.calibration import (
    COINFT_CHANNELS,
    SIDES,
    WRENCH_AXES,
    CoinFTCalibrationConfig,
    CoinFTCalibrationRuntime,
    calibration_status_summary,
    load_coinft_calibration_config,
)
from umift_laptop_alignment.pipeline.transforms.iphone_v2 import (
    closed_recording_windows as closed_v2_recording_windows,
    discover_v2_sessions,
    normalize_v2_session,
    session_unix_minus_monotonic_offset_ns,
)


CoinFTConfigInput = Union[CoinFTCalibrationConfig, str, Path, None]
UINT32_MOD = 1 << 32
D435_RGB_DEPTH_MAX_DELTA_MS = 10.0


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                yield {
                    "schema_version": 1,
                    "row_type": "normalization_error",
                    "source_file": str(path),
                    "line_number": line_number,
                    "error": str(exc),
                }


def int_value(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def float_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def timestamp_ns(row: dict[str, Any]) -> int | None:
    timestamp = row.get("timestamp")
    if isinstance(timestamp, dict):
        value = timestamp.get("aligned_monotonic_ns") or timestamp.get("mac_receive_monotonic_ns")
        return int_value(value)
    return int_value(row.get("aligned_monotonic_ns") or row.get("host_receive_monotonic_ns"))


def unix_minus_monotonic_offset_ns(run_dir: Path, sources: Iterable[Path] | None = None) -> int | None:
    offsets: list[int] = []
    for source in sources or discover_v2_sessions(run_dir):
        if source.is_dir():
            offset = session_unix_minus_monotonic_offset_ns(source)
            if offset is not None:
                offsets.append(offset)
            continue
        for row in iter_jsonl(source):
            timestamp = row.get("timestamp") if isinstance(row.get("timestamp"), dict) else {}
            aligned_monotonic_ns = int_value(timestamp.get("aligned_monotonic_ns"))
            aligned_unix_ns = int_value(timestamp.get("aligned_unix_ns"))
            if aligned_monotonic_ns is not None and aligned_unix_ns is not None:
                offsets.append(aligned_unix_ns - aligned_monotonic_ns)
    if not offsets:
        return None
    offsets.sort()
    return offsets[len(offsets) // 2]


def unix_seconds_to_aligned_monotonic_ns(value: Any, unix_minus_monotonic_ns: int | None) -> int | None:
    seconds = float_value(value)
    if seconds is None or unix_minus_monotonic_ns is None:
        return None
    return int(seconds * 1_000_000_000) - unix_minus_monotonic_ns


def write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            for row in rows:
                handle.write(json_line(row))
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return count


def discover_iphone_aligned(run_dir: Path) -> list[Path]:
    candidates = sorted((run_dir / "raw" / "iphone_stream").glob("*/aligned_iphone_stream.jsonl"))
    if candidates:
        return candidates
    return sorted((run_dir / "iphone_stream").glob("*/aligned_iphone_stream.jsonl"))


def iphone_image_path(row: dict[str, Any], source_file: Path, run_dir: Path) -> str | None:
    rgb = row.get("rgb")
    if not isinstance(rgb, dict):
        return None
    relative = rgb.get("image_relative_path") or rgb.get("latest_image_relative_path")
    if not isinstance(relative, str) or not relative:
        return None
    return relpath(source_file.parent / relative, run_dir)


def iphone_depth_path(row: dict[str, Any], source_file: Path, run_dir: Path) -> str | None:
    depth = row.get("depth")
    if not isinstance(depth, dict):
        return None
    relative = depth.get("depth_relative_path") or depth.get("latest_depth_relative_path")
    if not isinstance(relative, str) or not relative:
        return None
    return relpath(source_file.parent / relative, run_dir)


def normalize_iphone(
    run_dir: Path,
    output_frames: Path,
    output_events: Path,
    *,
    sources: Iterable[Path] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
    source_episode_index: int | None = None,
) -> dict[str, Any]:
    selected_sources = list(sources or discover_v2_sessions(run_dir))
    if len(selected_sources) != 1 or not selected_sources[0].is_dir():
        raise SystemExit("Episode normalization requires exactly one iPhone Protocol V2 session directory.")
    if start_ns is None or end_ns is None or source_episode_index is None:
        raise SystemExit("Episode normalization requires a closed Protocol V2 recording window.")
    return normalize_v2_session(
        run_dir=run_dir,
        session_dir=selected_sources[0],
        output_frames=output_frames,
        output_events=output_events,
        start_ns=start_ns,
        end_ns=end_ns,
        source_episode_index=source_episode_index,
    )


def normalize_iphone_legacy_disabled(
    run_dir: Path,
    output_frames: Path,
    output_events: Path,
    *,
    sources: Iterable[Path] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
    source_episode_index: int | None = None,
) -> dict[str, Any]:
    """Retained only as dead reference code; the active pipeline is V2-only."""
    frame_count = 0
    event_count = 0
    selected_sources = list(sources or discover_iphone_aligned(run_dir))

    def selected_timestamp(value: int | None) -> bool:
        if value is None:
            return False
        if start_ns is not None and value < start_ns:
            return False
        return end_ns is None or value <= end_ns

    def frame_rows() -> Iterator[dict[str, Any]]:
        nonlocal frame_count
        for source in selected_sources:
            for row in iter_jsonl(source):
                if row.get("row_type") != "iphone_capture_frame":
                    continue
                ts = timestamp_ns(row)
                if not selected_timestamp(ts):
                    continue
                out = {
                    "schema_version": 1,
                    "row_type": "normalized_iphone_frame",
                    "run_id": run_dir.name,
                    "source_stream": "iphone",
                    "source_episode_index": source_episode_index,
                    "source_file": relpath(source, run_dir),
                    "session_id": row.get("session_id"),
                    "sequence": row.get("sequence"),
                    "aligned_monotonic_ns": ts,
                    "timestamp": row.get("timestamp", {}),
                    "recording": row.get("recording", {}),
                    "pose": row.get("pose", {}),
                    "gripper": row.get("gripper", {}),
                    "rgb": {
                        **(row.get("rgb", {}) if isinstance(row.get("rgb"), dict) else {}),
                        "image_path": iphone_image_path(row, source, run_dir),
                    },
                    "depth": {
                        **(row.get("depth", {}) if isinstance(row.get("depth"), dict) else {}),
                        "depth_path": iphone_depth_path(row, source, run_dir),
                    },
                    "quality": {
                        "time_alignment": row.get("time_alignment", {}),
                    },
                }
                frame_count += 1
                yield out

    def event_rows() -> Iterator[dict[str, Any]]:
        nonlocal event_count
        for source in selected_sources:
            for row in iter_jsonl(source):
                if row.get("row_type") != "iphone_recording_event":
                    continue
                ts = timestamp_ns(row)
                if not selected_timestamp(ts):
                    continue
                out = {
                    "schema_version": 1,
                    "row_type": "normalized_recording_event",
                    "run_id": run_dir.name,
                    "source_stream": "iphone",
                    "source_episode_index": source_episode_index,
                    "source_file": relpath(source, run_dir),
                    "session_id": row.get("session_id"),
                    "aligned_monotonic_ns": ts,
                    "timestamp": row.get("timestamp", {}),
                    "event": row.get("event", {}),
                    "quality": {
                        "time_alignment": row.get("time_alignment", {}),
                    },
                }
                event_count += 1
                yield out

    write_rows(output_frames, frame_rows())
    write_rows(output_events, event_rows())
    return {
        "sources": [relpath(path, run_dir) for path in selected_sources],
        "frame_count": frame_count,
        "event_count": event_count,
    }


def discover_d435_csvs(run_dir: Path) -> list[Path]:
    matches: list[Path] = []
    for root in (run_dir / "raw" / "global_camera", run_dir / "global_camera"):
        if root.exists():
            direct = root / "frame_timestamps.csv"
            if direct.exists():
                matches.append(direct)
            matches.extend(root.glob("**/frame_timestamps.csv"))
    return sorted(set(matches))


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_coinft_calibration_config(run_dir: Path, coinft_config: CoinFTConfigInput = None) -> CoinFTCalibrationConfig:
    if isinstance(coinft_config, CoinFTCalibrationConfig):
        return coinft_config
    if coinft_config is not None and str(coinft_config).strip():
        return load_coinft_calibration_config(coinft_config)

    manifest = load_json(run_dir / "RUN_MANIFEST.json")
    streams = manifest.get("streams", {}) if isinstance(manifest.get("streams"), dict) else {}
    coinft = streams.get("coinft", {}) if isinstance(streams.get("coinft"), dict) else {}
    calibration = coinft.get("calibration", {}) if isinstance(coinft.get("calibration"), dict) else {}
    config_path = calibration.get("config_path")
    if isinstance(config_path, str) and config_path.strip():
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = run_dir / path
        return load_coinft_calibration_config(path)
    return load_coinft_calibration_config(None)


def d435_unix_minus_monotonic_offset_ns(metadata: dict[str, Any], fallback: int | None) -> int | None:
    run_context = metadata.get("runContext", {}) if isinstance(metadata.get("runContext"), dict) else {}
    control = run_context.get("recordControl", {}) if isinstance(run_context.get("recordControl"), dict) else {}
    event_monotonic_ns = control.get("eventAlignedMonotonicNs")
    event_unix_ns = control.get("eventAlignedUnixNs")
    if isinstance(event_monotonic_ns, int) and isinstance(event_unix_ns, int):
        return event_unix_ns - event_monotonic_ns
    return fallback


def normalize_d435(
    run_dir: Path,
    output_path: Path,
    *,
    csv_paths: Iterable[Path] | None = None,
    iphone_sources: Iterable[Path] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
    source_episode_index: int | None = None,
) -> dict[str, Any]:
    selected_csv_paths = list(csv_paths or discover_d435_csvs(run_dir))
    if not selected_csv_paths:
        write_rows(output_path, [])
        return {"sources": [], "frame_count": 0, "missing": True}
    fallback_unix_minus_monotonic_ns = unix_minus_monotonic_offset_ns(run_dir, iphone_sources)
    source_offsets: dict[str, int | None] = {}
    source_integrity: dict[str, dict[str, Any]] = {}

    def rows() -> Iterator[dict[str, Any]]:
        for csv_path in selected_csv_paths:
            camera_dir = csv_path.parent
            source_key = relpath(csv_path, run_dir)
            integrity: dict[str, Any] = {
                "color_missing_frames": 0,
                "depth_missing_frames": 0,
                "color_out_of_order_frames": 0,
                "depth_out_of_order_frames": 0,
                "first_color_frame_number": None,
                "last_color_frame_number": None,
                "first_depth_frame_number": None,
                "last_depth_frame_number": None,
            }
            source_integrity[source_key] = integrity
            metadata = load_json(camera_dir / "camera_metadata.json")
            source_offset_ns = d435_unix_minus_monotonic_offset_ns(
                metadata,
                fallback_unix_minus_monotonic_ns,
            )
            source_offsets[relpath(csv_path, run_dir)] = source_offset_ns
            streams = metadata.get("streams", {}) if isinstance(metadata.get("streams"), dict) else {}
            depth_stream = streams.get("depth", {}) if isinstance(streams.get("depth"), dict) else {}
            depth_shape = depth_stream.get("shape") if isinstance(depth_stream.get("shape"), list) else None
            depth_bytes_per_frame = None
            if depth_shape and len(depth_shape) == 2:
                depth_bytes_per_frame = int(depth_shape[0]) * int(depth_shape[1]) * 2
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for raw in reader:
                    for side in ("color", "depth"):
                        value = int_value(raw.get(f"{side}_frame_number"))
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
                                integrity[missing_key] = int(integrity[missing_key]) + value - previous - 1
                            elif value <= previous:
                                integrity[out_of_order_key] = int(integrity[out_of_order_key]) + 1
                        integrity[last_key] = value
                    frame_index = int_value(raw.get("frame_index"))
                    raw_host_monotonic_ns = int_value(raw.get("host_receive_monotonic_ns"))
                    color_sensor_timestamp_us = int_value(raw.get("color_sensor_timestamp_us"))
                    depth_sensor_timestamp_us = int_value(raw.get("depth_sensor_timestamp_us"))
                    rgb_depth_delta_ms = (
                        abs(color_sensor_timestamp_us - depth_sensor_timestamp_us) / 1000.0
                        if color_sensor_timestamp_us is not None and depth_sensor_timestamp_us is not None
                        else None
                    )
                    aligned_ns = unix_seconds_to_aligned_monotonic_ns(
                        raw.get("host_receive_time_s"),
                        source_offset_ns,
                    )
                    if aligned_ns is None:
                        aligned_ns = raw_host_monotonic_ns
                    if aligned_ns is None:
                        continue
                    if start_ns is not None and aligned_ns < start_ns:
                        continue
                    if end_ns is not None and aligned_ns > end_ns:
                        continue
                    yield {
                        "schema_version": 1,
                        "row_type": "normalized_d435_frame",
                        "run_id": run_dir.name,
                        "source_stream": "d435",
                        "source_episode_index": source_episode_index,
                        "source_file": relpath(csv_path, run_dir),
                        "frame_index": frame_index,
                        "aligned_monotonic_ns": aligned_ns,
                        "timestamp": {
                            "mac_receive_monotonic_ns": aligned_ns,
                            "raw_process_monotonic_ns": raw_host_monotonic_ns,
                            "host_receive_time_s": float_value(raw.get("host_receive_time_s")),
                            "realsense_frame_timestamp_ms": float_value(raw.get("realsense_frame_timestamp_ms")),
                            "realsense_timestamp_domain": raw.get("realsense_timestamp_domain"),
                            "color_sensor_timestamp_us": color_sensor_timestamp_us,
                            "depth_sensor_timestamp_us": depth_sensor_timestamp_us,
                        },
                        "rgb": {
                            "video_path": relpath(camera_dir / "rgb.mp4", run_dir),
                            "frame_index": frame_index,
                        },
                        "depth": {
                            "raw_path": relpath(camera_dir / "depth.raw", run_dir),
                            "shape": depth_shape,
                            "dtype": "uint16",
                            "byte_offset": frame_index * depth_bytes_per_frame if frame_index is not None and depth_bytes_per_frame else None,
                        },
                        "quality": {
                            "timebase": "unix_wall_time_converted_to_receiver_monotonic"
                            if aligned_ns is not None and source_offset_ns is not None
                            else "raw_process_monotonic_fallback",
                            "unix_minus_monotonic_offset_ns": source_offset_ns,
                            "color_frame_number": int_value(raw.get("color_frame_number")),
                            "depth_frame_number": int_value(raw.get("depth_frame_number")),
                            "rgb_depth_delta_ms": rgb_depth_delta_ms,
                            "rgb_depth_max_delta_ms": D435_RGB_DEPTH_MAX_DELTA_MS,
                            "rgb_depth_pair_valid": (
                                rgb_depth_delta_ms is not None
                                and rgb_depth_delta_ms <= D435_RGB_DEPTH_MAX_DELTA_MS
                            ),
                        },
                    }

    count = write_rows(output_path, rows())
    missing_frames = sum(
        max(int(row["color_missing_frames"]), int(row["depth_missing_frames"]))
        for row in source_integrity.values()
    )
    out_of_order_frames = sum(
        max(int(row["color_out_of_order_frames"]), int(row["depth_out_of_order_frames"]))
        for row in source_integrity.values()
    )
    return {
        "sources": [relpath(path, run_dir) for path in selected_csv_paths],
        "frame_count": count,
        "missing": False,
        "sensor_integrity": {
            "ok": missing_frames == 0 and out_of_order_frames == 0,
            "missing_frames": missing_frames,
            "out_of_order_frames": out_of_order_frames,
            "sources": source_integrity,
        },
        "timebase": {
            "method": "per_episode_event_anchor_with_run_fallback",
            "fallback_unix_minus_monotonic_offset_ns": fallback_unix_minus_monotonic_ns,
            "source_offsets_ns": source_offsets,
            "ready": all(offset is not None for offset in source_offsets.values()),
        },
    }


def discover_coinft_csvs(run_dir: Path) -> list[Path]:
    roots = [run_dir / "raw" / "coinft", run_dir / "coinft"]
    matches: list[Path] = []
    for root in roots:
        if root.exists():
            matches.extend(root.glob("**/raw_coinft_stream.csv"))
            matches.extend(root.glob("**/teensy_mock_stream.csv"))
    return sorted(set(matches))


def coinft_channels(raw: dict[str, Any], prefix: str) -> list[int | None]:
    return [int_value(raw.get(f"{prefix}_c{i}")) for i in range(1, COINFT_CHANNELS + 1)]


def coinft_float_series(raw: dict[str, Any], names: Iterable[str]) -> list[float] | None:
    values = [float_value(raw.get(name)) for name in names]
    return [float(value) for value in values] if all(value is not None for value in values) else None


def csv_calibration_result(raw: dict[str, Any]) -> dict[str, Any] | None:
    status = string_value(raw.get("calibration_status"))
    error = string_value(raw.get("calibration_error"))
    result: dict[str, Any] = {
        "calibration_status": status,
        "calibration_error": error,
        "tare_count": int_value(raw.get("tare_count")),
        "tare_target": int_value(raw.get("tare_target")),
    }
    has_payload = status is not None or error is not None
    for side in SIDES:
        zeroed = coinft_float_series(raw, (f"{side}_zeroed_c{i}" for i in range(1, COINFT_CHANNELS + 1)))
        wrench = coinft_float_series(raw, (f"{side}_{axis}" for axis in WRENCH_AXES))
        if zeroed is not None:
            result[f"{side}_raw_zeroed"] = zeroed
            has_payload = True
        if wrench is not None:
            result[f"{side}_wrench"] = wrench
            has_payload = True
    return result if has_payload else None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def fit_affine_clock_model(pairs: list[tuple[int, int]]) -> dict[str, Any] | None:
    if len(pairs) < 8:
        return None
    device_span_ns = pairs[-1][0] - pairs[0][0]
    if device_span_ns < 20_000_000:
        return None
    mean_x = sum(float(x) for x, _ in pairs) / len(pairs)
    mean_y = sum(float(y) for _, y in pairs) / len(pairs)
    denominator = sum((float(x) - mean_x) ** 2 for x, _ in pairs)
    if denominator <= 0.0:
        return None
    scale = sum((float(x) - mean_x) * (float(y) - mean_y) for x, y in pairs) / denominator
    if not 0.999 <= scale <= 1.001:
        return None
    intercept = mean_y - scale * mean_x
    residuals = [float(y) - (scale * float(x) + intercept) for x, y in pairs]
    intercept += _percentile(residuals, 0.01)
    corrected = [float(y) - (scale * float(x) + intercept) for x, y in pairs]
    return {
        "method": "affine_lower_envelope",
        "sample_count": len(pairs),
        "device_span_s": device_span_ns / 1e9,
        "scale": scale,
        "rate_error_ppm": (scale - 1.0) * 1e6,
        "offset_ns": intercept,
        "residual_p50_ms": _percentile(corrected, 0.50) / 1e6,
        "residual_p95_ms": _percentile(corrected, 0.95) / 1e6,
        "residual_p99_ms": _percentile(corrected, 0.99) / 1e6,
        "residual_max_ms": max(corrected) / 1e6,
    }


def coinft_clock_model(source: Path) -> dict[str, Any] | None:
    pairs: list[tuple[int, int]] = []
    epoch_us = 0
    previous_raw: int | None = None
    with source.open("r", encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            teensy_time_us = int_value(raw.get("teensy_time_us"))
            host_ns = int_value(raw.get("host_receive_monotonic_ns"))
            if teensy_time_us is None or host_ns is None:
                continue
            if previous_raw is not None and teensy_time_us < previous_raw:
                if previous_raw - teensy_time_us > UINT32_MOD // 2:
                    epoch_us += UINT32_MOD
                else:
                    return None
            previous_raw = teensy_time_us
            pairs.append(((epoch_us + teensy_time_us) * 1000, host_ns))
    return fit_affine_clock_model(pairs)


def normalize_coinft(
    run_dir: Path,
    output_path: Path,
    coinft_config: CoinFTConfigInput = None,
    *,
    sources: Iterable[Path] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
    source_episode_index: int | None = None,
) -> dict[str, Any]:
    selected_sources = list(sources or discover_coinft_csvs(run_dir))
    calibration_config = resolve_coinft_calibration_config(run_dir, coinft_config)
    sample_count = 0
    calibrated_sample_count = 0
    calibration_status_counts: dict[str, int] = {}
    runtime_errors: list[dict[str, str]] = []
    clock_models = {source: coinft_clock_model(source) for source in selected_sources}

    def add_status(status: Any) -> None:
        key = str(status or "unknown")
        calibration_status_counts[key] = calibration_status_counts.get(key, 0) + 1

    def rows() -> Iterator[dict[str, Any]]:
        nonlocal sample_count, calibrated_sample_count
        for source in selected_sources:
            clock_model = clock_models[source]
            teensy_epoch_us = 0
            previous_teensy_raw: int | None = None
            runtime: CoinFTCalibrationRuntime | None = None
            runtime_error: str | None = None
            if calibration_config.calibrated:
                try:
                    runtime = CoinFTCalibrationRuntime(calibration_config)
                except Exception as exc:  # noqa: BLE001
                    runtime_error = str(exc)
                    runtime_errors.append({"source_file": relpath(source, run_dir), "error": runtime_error})
            with source.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for raw in reader:
                    host_receive_ns = int_value(raw.get("host_receive_monotonic_ns"))
                    teensy_time_us = int_value(raw.get("teensy_time_us"))
                    if host_receive_ns is None:
                        continue
                    teensy_unwrapped_us: int | None = None
                    clock_reset_detected = False
                    if teensy_time_us is not None:
                        if previous_teensy_raw is not None and teensy_time_us < previous_teensy_raw:
                            if previous_teensy_raw - teensy_time_us > UINT32_MOD // 2:
                                teensy_epoch_us += UINT32_MOD
                            else:
                                clock_reset_detected = True
                        previous_teensy_raw = teensy_time_us
                        teensy_unwrapped_us = teensy_epoch_us + teensy_time_us
                    if clock_model is not None and teensy_unwrapped_us is not None and not clock_reset_detected:
                        aligned_ns = int(round(clock_model["scale"] * teensy_unwrapped_us * 1000 + clock_model["offset_ns"]))
                        clock_alignment_method = "teensy_affine_lower_envelope"
                    else:
                        aligned_ns = host_receive_ns
                        clock_alignment_method = "mac_receive_fallback"
                    serial_arrival_delay_ms = (host_receive_ns - aligned_ns) / 1e6
                    if start_ns is not None and aligned_ns < start_ns:
                        continue
                    if end_ns is not None and aligned_ns > end_ns:
                        continue
                    left = coinft_channels(raw, "left")
                    right = coinft_channels(raw, "right")
                    csv_result = csv_calibration_result(raw)
                    csv_left_wrench = csv_result.get("left_wrench") if isinstance(csv_result, dict) else None
                    csv_right_wrench = csv_result.get("right_wrench") if isinstance(csv_result, dict) else None
                    csv_has_wrench = (
                        isinstance(csv_left_wrench, list)
                        and len(csv_left_wrench) == 6
                        and isinstance(csv_right_wrench, list)
                        and len(csv_right_wrench) == 6
                    )
                    calibration_result = csv_result if csv_result is not None else {"calibration_status": "raw_only"}
                    calibration_source = "raw_csv" if csv_result is not None else "raw_only"
                    if calibration_config.calibrated and not csv_has_wrench:
                        if runtime_error:
                            calibration_result = {
                                "calibration_status": "error",
                                "calibration_error": runtime_error,
                            }
                            calibration_source = "offline_model"
                        elif runtime is not None:
                            if all(isinstance(value, int) for value in left + right):
                                calibration_result = runtime.process_offline(left, right)
                                calibration_source = "offline_model"
                            else:
                                calibration_result = {
                                    "calibration_status": "error",
                                    "calibration_error": "raw CoinFT row has missing channels",
                                }
                                calibration_source = "offline_model"
                    status = calibration_result.get("calibration_status")
                    left_wrench = calibration_result.get("left_wrench")
                    right_wrench = calibration_result.get("right_wrench")
                    left_zeroed = calibration_result.get("left_raw_zeroed")
                    right_zeroed = calibration_result.get("right_raw_zeroed")
                    has_wrench = isinstance(left_wrench, list) and len(left_wrench) == 6 and isinstance(right_wrench, list) and len(right_wrench) == 6
                    dual_wrench = [left_wrench, right_wrench] if has_wrench else None
                    has_zeroed = isinstance(left_zeroed, list) and len(left_zeroed) == 12 and isinstance(right_zeroed, list) and len(right_zeroed) == 12
                    calibrated_sample_count += int(has_wrench)
                    add_status(status)
                    out = {
                        "schema_version": 1,
                        "row_type": "normalized_coinft_sample",
                        "run_id": run_dir.name,
                        "source_stream": "coinft",
                        "source_episode_index": source_episode_index,
                        "source_file": relpath(source, run_dir),
                        "packet_index": int_value(raw.get("packet_index")),
                        "sequence_id": int_value(raw.get("sequence_id")),
                        "aligned_monotonic_ns": aligned_ns,
                        "timestamp": {
                            "aligned_monotonic_ns": aligned_ns,
                            "alignment_method": clock_alignment_method,
                            "mac_receive_monotonic_ns": host_receive_ns,
                            "mac_receive_unix_ns": int_value(raw.get("host_receive_unix_ns")),
                            "teensy_time_us": teensy_time_us,
                            "teensy_unwrapped_time_us": teensy_unwrapped_us,
                        },
                        "left_raw": left,
                        "right_raw": right,
                        "side_order": list(SIDES),
                        "wrench_axes": list(WRENCH_AXES),
                        "raw_12ch": [left, right],
                        "zeroed_12ch": [left_zeroed, right_zeroed] if has_zeroed else None,
                        "wrench": dual_wrench,
                        "left_raw_zeroed": left_zeroed if isinstance(left_zeroed, list) else None,
                        "right_raw_zeroed": right_zeroed if isinstance(right_zeroed, list) else None,
                        "left_wrench": left_wrench if isinstance(left_wrench, list) else None,
                        "right_wrench": right_wrench if isinstance(right_wrench, list) else None,
                        "calibration": {
                            "mode": calibration_config.mode,
                            "status": status,
                            "error": calibration_result.get("calibration_error"),
                            "source": calibration_source,
                            "model_set_id": calibration_config.model_set_id,
                            "model_set_path": str(calibration_config.model_set_path) if calibration_config.model_set_path else None,
                            "tare_count": calibration_result.get("tare_count"),
                            "tare_target": calibration_result.get("tare_target"),
                        },
                        "quality": {
                            "valid_channels": sum(value is not None for value in left + right),
                            "has_calibrated_wrench": has_wrench,
                            "has_dual_calibrated_wrench": has_wrench,
                            "clock_model_valid": clock_model is not None,
                            "clock_reset_detected": clock_reset_detected,
                            "clock_alignment_method": clock_alignment_method,
                            "serial_arrival_delay_ms": serial_arrival_delay_ms,
                        },
                    }
                    sample_count += 1
                    yield out

    write_rows(output_path, rows())
    return {
        "sources": [relpath(path, run_dir) for path in selected_sources],
        "sample_count": sample_count,
        "calibrated_sample_count": calibrated_sample_count,
        "calibration": {
            **calibration_status_summary(calibration_config),
            "configured": calibration_config.to_manifest_dict(),
            "status_counts": calibration_status_counts,
            "runtime_errors": runtime_errors,
        },
        "clock_alignment": {
            "preferred_method": "teensy_affine_lower_envelope",
            "fallback_method": "mac_receive_monotonic_ns",
            "models": [
                {
                    "source_file": relpath(source, run_dir),
                    "valid": clock_models[source] is not None,
                    **(clock_models[source] or {}),
                }
                for source in selected_sources
            ],
        },
    }


@dataclass(frozen=True)
class EpisodeWindow:
    source_episode_index: int
    session_id: str
    start_ns: int
    end_ns: int
    iphone_source: Path
    d435_dir: Path
    d435_csv: Path
    coinft_sources: tuple[Path, ...]


def _episode_dir(root: Path, source_episode_index: int) -> Path:
    expected = root / f"episode_{source_episode_index:06d}"
    if expected.is_dir():
        return expected
    for candidate in root.glob("episode_*"):
        try:
            candidate_index = int(candidate.name.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if candidate_index == source_episode_index:
            return candidate
    return expected


def _closed_recording_windows(run_dir: Path) -> list[dict[str, Any]]:
    return [
        {
            "session_id": window.session_id,
            "source": window.session_dir,
            "start_ns": window.start_ns,
            "end_ns": window.end_ns,
            "start_event": window.start_event,
            "stop_event": window.stop_event,
        }
        for window in closed_v2_recording_windows(run_dir, strict=False)
    ]


def resolve_episode_window(
    run_dir: Path,
    source_episode_index: int,
    *,
    max_start_delta_ms: float = 2000.0,
) -> EpisodeWindow:
    run_dir = run_dir.expanduser().resolve()
    d435_dir = _episode_dir(run_dir / "raw" / "global_camera", source_episode_index)
    d435_csv = d435_dir / "frame_timestamps.csv"
    metadata_path = d435_dir / "camera_metadata.json"
    if not d435_csv.is_file():
        raise SystemExit(f"Episode {source_episode_index} is missing D435 frame_timestamps.csv.")
    metadata = load_json(metadata_path)
    control = metadata.get("runContext", {}).get("recordControl", {}) if isinstance(metadata, dict) else {}
    metadata_index = control.get("episodeIndex")
    if isinstance(metadata_index, int) and metadata_index != source_episode_index:
        raise SystemExit(
            f"D435 episode directory/index mismatch: requested {source_episode_index}, metadata says {metadata_index}."
        )
    anchor_ns = control.get("eventAlignedMonotonicNs")
    if not isinstance(anchor_ns, int):
        raise SystemExit(f"Episode {source_episode_index} D435 metadata has no recording start timestamp.")

    windows = _closed_recording_windows(run_dir)
    if not windows:
        raise SystemExit("No closed iPhone record_start/record_stop window is available.")
    matching = min(windows, key=lambda item: abs(int(item["start_ns"]) - anchor_ns))
    start_delta_ms = abs(int(matching["start_ns"]) - anchor_ns) / 1e6
    if start_delta_ms > max_start_delta_ms:
        raise SystemExit(
            f"Episode {source_episode_index} cannot be matched to an iPhone recording window; "
            f"nearest start differs by {start_delta_ms:.1f} ms."
        )

    coinft_dir = _episode_dir(run_dir / "raw" / "coinft", source_episode_index)
    coinft_sources = tuple(
        sorted(
            path
            for pattern in ("raw_coinft_stream.csv", "teensy_mock_stream.csv")
            for path in coinft_dir.glob(pattern)
        )
    )
    if not coinft_sources:
        raise SystemExit(f"Episode {source_episode_index} is missing CoinFT raw CSV.")
    return EpisodeWindow(
        source_episode_index=source_episode_index,
        session_id=str(matching["session_id"]),
        start_ns=int(matching["start_ns"]),
        end_ns=int(matching["end_ns"]),
        iphone_source=Path(matching["source"]),
        d435_dir=d435_dir,
        d435_csv=d435_csv,
        coinft_sources=coinft_sources,
    )


def normalize_episode(
    run_dir: Path,
    source_episode_index: int,
    coinft_config: CoinFTConfigInput = None,
) -> dict[str, Any]:
    paths = ensure_run_layout(run_dir)
    window = resolve_episode_window(paths.run_dir, source_episode_index)
    episode_name = f"episode_{source_episode_index:06d}"
    output_dir = paths.normalized_dir / "episodes" / episode_name
    iphone_frames = output_dir / "iphone_frames.jsonl"
    iphone_events = output_dir / "recording_events.jsonl"
    d435_frames = output_dir / "d435_frames.jsonl"
    coinft_samples = output_dir / "coinft_samples.jsonl"
    report_path = output_dir / "NORMALIZE_REPORT.json"

    report = {
        "schema_version": 2,
        "row_type": "episode_normalization_report",
        "run_id": paths.run_dir.name,
        "source_episode_index": source_episode_index,
        "created_at": utc_now_iso(),
        "recording_window": {
            "source_episode_index": source_episode_index,
            "iphone_session_id": window.session_id,
            "start_aligned_monotonic_ns": window.start_ns,
            "end_aligned_monotonic_ns": window.end_ns,
            "iphone_source": relpath(window.iphone_source, paths.run_dir),
            "d435_source": relpath(window.d435_dir, paths.run_dir),
            "coinft_sources": [relpath(path, paths.run_dir) for path in window.coinft_sources],
        },
        "outputs": {
            "iphone_frames": relpath(iphone_frames, paths.run_dir),
            "recording_events": relpath(iphone_events, paths.run_dir),
            "d435_frames": relpath(d435_frames, paths.run_dir),
            "coinft_samples": relpath(coinft_samples, paths.run_dir),
            "report": relpath(report_path, paths.run_dir),
        },
        "iphone": normalize_iphone(
            paths.run_dir,
            iphone_frames,
            iphone_events,
            sources=[window.iphone_source],
            start_ns=window.start_ns,
            end_ns=window.end_ns,
            source_episode_index=source_episode_index,
        ),
        "d435": normalize_d435(
            paths.run_dir,
            d435_frames,
            csv_paths=[window.d435_csv],
            iphone_sources=[window.iphone_source],
            start_ns=window.start_ns,
            end_ns=window.end_ns,
            source_episode_index=source_episode_index,
        ),
        "coinft": normalize_coinft(
            paths.run_dir,
            coinft_samples,
            coinft_config=coinft_config,
            sources=window.coinft_sources,
            start_ns=window.start_ns,
            end_ns=window.end_ns,
            source_episode_index=source_episode_index,
        ),
    }
    write_json(report_path, report)

    for stream, role, path in (
        ("iphone", f"normalized_frames_{episode_name}", iphone_frames),
        ("iphone", f"normalized_events_{episode_name}", iphone_events),
        ("d435", f"normalized_frames_{episode_name}", d435_frames),
        ("coinft", f"normalized_samples_{episode_name}", coinft_samples),
        ("normalization", f"report_{episode_name}", report_path),
    ):
        register_artifact(
            paths.run_dir,
            stream=stream,
            role=role,
            path=path,
            metadata={"kind": "file", "source_episode_index": source_episode_index},
        )
    append_sync_log(
        paths.run_dir,
        {
            "event": "normalized_episode",
            "source_episode_index": source_episode_index,
            "report": relpath(report_path, paths.run_dir),
        },
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize one raw UMIFT Episode into Episode-local JSONL.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("episode_index", type=int)
    parser.add_argument("--coinft-config", default="", help="CoinFT calibration JSON. Defaults to RUN_MANIFEST.json, then raw-only.")
    args = parser.parse_args()
    report = normalize_episode(
        args.run_dir.expanduser().resolve(),
        args.episode_index,
        coinft_config=args.coinft_config,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

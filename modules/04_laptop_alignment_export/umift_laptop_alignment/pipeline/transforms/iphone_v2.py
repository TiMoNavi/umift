"""Normalize the authoritative iPhone Protocol V2 archive."""

from __future__ import annotations

import json
import math
import os
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from umift_laptop_alignment.capture.receivers.iphone.video_segment_writer import (
    CHECKPOINT_MAGIC,
    CHECKPOINT_STRUCT,
    CHECKPOINT_VERSION,
)
from umift_laptop_alignment.orchestration.run_layout import json_line, relpath


EVENT_NAMES = {
    1: "coinft_restart_requested",
    2: "record_start",
    3: "record_stop",
    4: "record_discard",
}
TRACKING_NAMES = {0: "unknown", 1: "normal", 2: "limited", 3: "unavailable"}


@dataclass(frozen=True)
class V2RecordingWindow:
    session_id: str
    session_dir: Path
    start_ns: int
    end_ns: int
    start_event: dict[str, Any]
    stop_event: dict[str, Any]


def discover_v2_sessions(run_dir: Path) -> list[Path]:
    root = run_dir / "raw" / "iphone_stream"
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and (path / "SESSION.json").is_file()
        and (path / "frame_index.jsonl").is_file()
        and (path / "frame_metadata.jsonl").is_file()
        and (path / "recording_events.jsonl").is_file()
        and (path / "archive_commit.bin").is_file()
    )


def _checkpoint(session_dir: Path) -> dict[str, int]:
    payload = (session_dir / "archive_commit.bin").read_bytes()
    if len(payload) != CHECKPOINT_STRUCT.size:
        raise ValueError(f"invalid V2 checkpoint size in {session_dir}")
    values = CHECKPOINT_STRUCT.unpack(payload)
    if values[0] != CHECKPOINT_MAGIC or values[1] != CHECKPOINT_VERSION:
        raise ValueError(f"invalid V2 checkpoint header in {session_dir}")
    if zlib.crc32(payload[:-4]) & 0xFFFFFFFF != int(values[-1]):
        raise ValueError(f"invalid V2 checkpoint checksum in {session_dir}")
    return {
        "committed_sequence": int(values[2]),
        "frame_count": int(values[3]),
        "frame_index_size": int(values[8]),
        "frame_metadata_size": int(values[9]),
        "depth_data_size": int(values[10]),
        "depth_index_size": int(values[11]),
        "recording_events_size": int(values[12]),
    }


def _jsonl_prefix(path: Path, byte_count: int) -> Iterator[dict[str, Any]]:
    with path.open("rb") as handle:
        payload = handle.read(byte_count)
    if len(payload) != byte_count:
        raise ValueError(f"V2 archive file is shorter than checkpoint: {path}")
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}:{line_number}: {exc}") from exc


def _session_json(session_dir: Path) -> dict[str, Any]:
    return json.loads((session_dir / "SESSION.json").read_text(encoding="utf-8"))


def _event_rows(session_dir: Path, checkpoint: dict[str, int]) -> list[dict[str, Any]]:
    return list(
        _jsonl_prefix(
            session_dir / "recording_events.jsonl",
            checkpoint["recording_events_size"],
        )
    )


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


def phone_clock_model(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / "clock_sync.jsonl"
    if not path.is_file():
        return None
    samples: list[tuple[int, int, int]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                phone_ns = int(row["phone_midpoint_ns"])
                mac_ns = int(row["mac_midpoint_ns"])
                rtt_ns = int(row["round_trip_ns"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if phone_ns >= 0 and mac_ns >= 0 and rtt_ns >= 0:
                samples.append((phone_ns, mac_ns, rtt_ns))
    if not samples:
        return None

    samples.sort(key=lambda item: item[2])
    selected_count = min(len(samples), max(8, math.ceil(len(samples) * 0.20)))
    selected = sorted(samples[:selected_count], key=lambda item: item[0])
    scale = 1.0
    method = "bidirectional_offset"
    device_span_ns = selected[-1][0] - selected[0][0]
    if len(selected) >= 4 and device_span_ns >= 2_000_000_000:
        mean_phone = sum(float(phone_ns) for phone_ns, _, _ in selected) / len(selected)
        mean_mac = sum(float(mac_ns) for _, mac_ns, _ in selected) / len(selected)
        denominator = sum((float(phone_ns) - mean_phone) ** 2 for phone_ns, _, _ in selected)
        if denominator > 0.0:
            candidate_scale = sum(
                (float(phone_ns) - mean_phone) * (float(mac_ns) - mean_mac)
                for phone_ns, mac_ns, _ in selected
            ) / denominator
            if 0.999 <= candidate_scale <= 1.001:
                scale = candidate_scale
                method = "bidirectional_affine_low_rtt"
    offsets = [float(mac_ns) - scale * float(phone_ns) for phone_ns, mac_ns, _ in selected]
    offset_ns = _percentile(offsets, 0.50)
    residuals = [float(mac_ns) - (scale * float(phone_ns) + offset_ns) for phone_ns, mac_ns, _ in selected]
    all_rtts = [float(rtt_ns) for _, _, rtt_ns in samples]
    return {
        "method": method,
        "sample_count": len(samples),
        "selected_sample_count": len(selected),
        "selected_device_span_s": device_span_ns / 1e9,
        "scale": scale,
        "rate_error_ppm": (scale - 1.0) * 1e6,
        "offset_ns": offset_ns,
        "rtt_p50_ms": _percentile(all_rtts, 0.50) / 1e6,
        "rtt_p95_ms": _percentile(all_rtts, 0.95) / 1e6,
        "residual_p50_ms": _percentile([abs(value) for value in residuals], 0.50) / 1e6,
        "residual_p95_ms": _percentile([abs(value) for value in residuals], 0.95) / 1e6,
    }


def _one_way_minimum_offset_ns(session_dir: Path, checkpoint: dict[str, int]) -> int:
    offsets = [
        int(row["mac_receive_monotonic_ns"]) - int(row["capture_timestamp_ns"])
        for row in _jsonl_prefix(session_dir / "frame_index.jsonl", checkpoint["frame_index_size"])
    ]
    if not offsets:
        raise ValueError(f"V2 archive has no committed frame timing: {session_dir}")
    return min(offsets)


def _map_phone_timestamp_ns(
    phone_timestamp_ns: int,
    clock_model: dict[str, Any] | None,
    one_way_offset_ns: int,
) -> tuple[int, str, float, float]:
    if clock_model is not None:
        scale = float(clock_model["scale"])
        offset_ns = float(clock_model["offset_ns"])
        return (
            int(round(scale * phone_timestamp_ns + offset_ns)),
            str(clock_model["method"]),
            scale,
            offset_ns,
        )
    return phone_timestamp_ns + one_way_offset_ns, "one_way_min_delay", 1.0, float(one_way_offset_ns)


def _frame_timing_by_sequence(
    session_dir: Path,
    checkpoint: dict[str, int],
    selected_sequences: set[int] | None = None,
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    clock_model = phone_clock_model(session_dir)
    one_way_offset_ns = _one_way_minimum_offset_ns(session_dir, checkpoint)
    rows = _jsonl_prefix(session_dir / "frame_index.jsonl", checkpoint["frame_index_size"])
    for row in rows:
        sequence = int(row["sequence"])
        capture_ns = int(row["capture_timestamp_ns"])
        receive_ns = int(row["mac_receive_monotonic_ns"])
        receive_unix_ns = int(row["mac_receive_unix_ns"])
        if selected_sequences is not None and sequence not in selected_sequences:
            continue
        aligned_ns, method, scale, offset_ns = _map_phone_timestamp_ns(
            capture_ns, clock_model, one_way_offset_ns
        )
        latency_ns = max(0, receive_ns - aligned_ns)
        result[sequence] = {
            "phone_local_capture_ns": capture_ns,
            "mac_receive_monotonic_ns": receive_ns,
            "mac_receive_unix_ns": receive_unix_ns,
            "phone_to_mac_scale": scale,
            "phone_to_mac_offset_ns": offset_ns,
            "alignment_method": method,
            "estimated_receive_latency_ns": latency_ns,
            "aligned_monotonic_ns": aligned_ns,
            "aligned_unix_ns": receive_unix_ns - latency_ns,
        }
    return result


def session_unix_minus_monotonic_offset_ns(session_dir: Path) -> int | None:
    checkpoint = _checkpoint(session_dir)
    rows = _jsonl_prefix(session_dir / "frame_index.jsonl", checkpoint["frame_index_size"])
    for row in rows:
        return int(row["mac_receive_unix_ns"]) - int(row["mac_receive_monotonic_ns"])
    return None


def closed_recording_windows(
    run_dir: Path,
    *,
    strict: bool = True,
) -> list[V2RecordingWindow]:
    windows: list[V2RecordingWindow] = []
    for session_dir in discover_v2_sessions(run_dir):
        session_windows: list[V2RecordingWindow] = []
        try:
            checkpoint = _checkpoint(session_dir)
            session = _session_json(session_dir)
            session_id = str(session.get("session_id") or session_dir.name)
            events = [
                row
                for row in _event_rows(session_dir, checkpoint)
                if EVENT_NAMES.get(int(row.get("code", 0))) in {"record_start", "record_stop"}
            ]
            timing = _frame_timing_by_sequence(
                session_dir,
                checkpoint,
                {int(row["sequence"]) for row in events},
            )
            active: tuple[dict[str, Any], int] | None = None
            for row in sorted(events, key=lambda value: (int(value["sequence"]), int(value["event_index"]))):
                name = EVENT_NAMES[int(row["code"])]
                event_timing = timing.get(int(row["sequence"]))
                if event_timing is None:
                    raise ValueError(f"V2 event has no committed frame timing: {session_dir} seq={row['sequence']}")
                if name == "record_start":
                    if active is not None:
                        raise ValueError(f"overlapping V2 record_start events in {session_dir}")
                    active = (row, event_timing["aligned_monotonic_ns"])
                elif active is not None:
                    start_event, start_ns = active
                    end_ns = event_timing["aligned_monotonic_ns"]
                    if end_ns <= start_ns:
                        raise ValueError(f"invalid V2 recording window in {session_dir}")
                    session_windows.append(
                        V2RecordingWindow(session_id, session_dir, start_ns, end_ns, start_event, row)
                    )
                    active = None
            if active is not None and strict:
                raise ValueError(f"unclosed V2 record_start event in {session_dir}")
        except (OSError, KeyError, TypeError, ValueError):
            if strict:
                raise
            continue
        windows.extend(session_windows)
    return windows


def _matrix_rows(values: Any) -> list[list[float]] | None:
    if not isinstance(values, list) or len(values) != 16:
        return None
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values):
        return None
    flat = [float(value) for value in values]
    return [flat[index : index + 4] for index in range(0, 16, 4)]


def _write_rows(path: Path, rows: Iterator[dict[str, Any]]) -> int:
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


def normalize_v2_session(
    *,
    run_dir: Path,
    session_dir: Path,
    output_frames: Path,
    output_events: Path,
    start_ns: int,
    end_ns: int,
    source_episode_index: int,
) -> dict[str, Any]:
    checkpoint = _checkpoint(session_dir)
    session = _session_json(session_dir)
    session_id = str(session.get("session_id") or session_dir.name)
    capture = session.get("capture", {}) if isinstance(session.get("capture"), dict) else {}
    width = int(capture.get("width") or 1920)
    height = int(capture.get("height") or 1440)
    metadata_rows = _jsonl_prefix(
        session_dir / "frame_metadata.jsonl",
        checkpoint["frame_metadata_size"],
    )
    depth_by_sequence = {
        int(row["sequence"]): row
        for row in _jsonl_prefix(
            session_dir / "depth_index.jsonl",
            checkpoint["depth_index_size"],
        )
    }
    events = _event_rows(session_dir, checkpoint)
    events_by_sequence: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        events_by_sequence.setdefault(int(event["sequence"]), []).append(event)

    normalized_events: list[dict[str, Any]] = []
    segment_frame_counts: dict[int, int] = {}
    clock_model = phone_clock_model(session_dir)
    one_way_offset_ns = _one_way_minimum_offset_ns(session_dir, checkpoint)
    metadata_iterator = iter(metadata_rows)
    frame_count = 0

    def frame_rows() -> Iterator[dict[str, Any]]:
        nonlocal frame_count
        frame_source = _jsonl_prefix(session_dir / "frame_index.jsonl", checkpoint["frame_index_size"])
        for frame in frame_source:
            try:
                metadata = next(metadata_iterator)
            except StopIteration as exc:
                raise ValueError(f"V2 metadata ended before frame index in {session_dir}") from exc
            sequence = int(frame["sequence"])
            if int(metadata["sequence"]) != sequence:
                raise ValueError(
                    f"V2 frame/metadata sequence mismatch in {session_dir}: frame={sequence} metadata={metadata['sequence']}"
                )
            capture_ns = int(frame["capture_timestamp_ns"])
            if int(metadata["capture_timestamp_ns"]) != capture_ns:
                raise ValueError(f"V2 frame/metadata timestamp mismatch in {session_dir} seq={sequence}")
            receive_ns = int(frame["mac_receive_monotonic_ns"])
            receive_unix_ns = int(frame["mac_receive_unix_ns"])
            aligned_ns, alignment_method, phone_to_mac_scale, phone_to_mac_offset_ns = _map_phone_timestamp_ns(
                capture_ns, clock_model, one_way_offset_ns
            )
            latency_ns = max(0, receive_ns - aligned_ns)
            timestamp = {
                "phone_local_capture_ns": capture_ns,
                "mac_receive_monotonic_ns": receive_ns,
                "mac_receive_unix_ns": receive_unix_ns,
                "phone_to_mac_scale": phone_to_mac_scale,
                "phone_to_mac_offset_ns": phone_to_mac_offset_ns,
                "alignment_method": alignment_method,
                "estimated_receive_latency_ns": latency_ns,
                "aligned_monotonic_ns": aligned_ns,
                "aligned_unix_ns": receive_unix_ns - latency_ns,
            }

            segment_index = int(frame["video_segment"])
            segment_frame_index = segment_frame_counts.get(segment_index, 0)
            segment_frame_counts[segment_index] = segment_frame_index + 1
            if start_ns <= aligned_ns <= end_ns:
                transform = _matrix_rows(metadata.get("camera_in_user_world_transform"))
                pose_valid = bool(metadata.get("pose_valid")) and bool(metadata.get("world_calibrated")) and transform is not None
                depth_row = depth_by_sequence.get(sequence)
                depth_valid = bool(metadata.get("depth_valid")) and depth_row is not None
                out = {
                    "schema_version": 2,
                    "row_type": "normalized_iphone_frame",
                    "run_id": run_dir.name,
                    "source_stream": "iphone",
                    "source_episode_index": source_episode_index,
                    "source_file": relpath(session_dir / "frame_index.jsonl", run_dir),
                    "session_id": session_id,
                    "sequence": sequence,
                    "aligned_monotonic_ns": aligned_ns,
                    "timestamp": timestamp,
                    "recording": {"active": bool(metadata.get("recording_active"))},
                    "pose": {
                        "valid": pose_valid,
                        "frame": "user_world" if int(metadata.get("pose_frame") or 0) == 1 else "unavailable",
                        "world_calibrated": bool(metadata.get("world_calibrated")),
                        "world_origin_status": "calibrated" if metadata.get("world_calibrated") else "not_marked",
                        "tracking_state": TRACKING_NAMES.get(int(metadata.get("tracking_state") or 0), "unknown"),
                        "camera_in_user_world_transform": transform,
                    },
                    "gripper": {
                        "valid": bool(metadata.get("gripper_valid")),
                        "open_percent": metadata.get("gripper_open_percent"),
                        "phone_local_timestamp_ns": metadata.get("gripper_timestamp_ns"),
                        "sample_age_ms": (
                            float(metadata["gripper_age_ns"]) / 1e6
                            if isinstance(metadata.get("gripper_age_ns"), int)
                            and int(metadata["gripper_age_ns"]) > -(1 << 62)
                            else None
                        ),
                    },
                    "rgb": {
                        "valid": True,
                        "codec": "h264",
                        "width": width,
                        "height": height,
                        "video_path": relpath(session_dir / "video" / f"segment_{segment_index:06d}.h264", run_dir),
                        "frame_index": segment_frame_index,
                        "byte_offset": int(frame["video_byte_offset"]),
                        "byte_count": int(frame["video_annex_b_bytes"]),
                        "key_frame": bool(frame.get("key_frame")),
                    },
                    "depth": {
                        "valid": depth_valid,
                        "depth_path": relpath(session_dir / "depth_data.f16", run_dir) if depth_valid else None,
                        "byte_offset": int(depth_row["byte_offset"]) if depth_valid else None,
                        "byte_count": int(depth_row["byte_count"]) if depth_valid else None,
                        "width": int(depth_row["width"]) if depth_valid else int(metadata.get("depth_width") or 0),
                        "height": int(depth_row["height"]) if depth_valid else int(metadata.get("depth_height") or 0),
                        "dtype": "<f2",
                        "encoding": "float16_le",
                        "unit": "m",
                        "center_m": metadata.get("depth_center_m"),
                        "valid_ratio": metadata.get("depth_valid_ratio"),
                    },
                    "quality": {
                        "time_alignment": {
                            "method": alignment_method,
                            "clock_sync_model": clock_model,
                        }
                    },
                }
                frame_count += 1
                yield out

            for event in events_by_sequence.get(sequence, []):
                name = EVENT_NAMES.get(int(event.get("code", 0)))
                if name not in {"record_start", "record_stop"} or not start_ns <= aligned_ns <= end_ns:
                    continue
                normalized_events.append(
                    {
                        "schema_version": 2,
                        "row_type": "normalized_recording_event",
                        "run_id": run_dir.name,
                        "source_stream": "iphone",
                        "source_episode_index": source_episode_index,
                        "source_file": relpath(session_dir / "recording_events.jsonl", run_dir),
                        "session_id": session_id,
                        "sequence": sequence,
                        "aligned_monotonic_ns": aligned_ns,
                        "timestamp": timestamp,
                        "event": {
                            "code": name,
                            "index": int(event["event_index"]),
                            "reason": event.get("reason"),
                            "event_unix_time_ns": event.get("event_unix_time_ns"),
                            "app_uptime_ns": event.get("app_uptime_ns"),
                        },
                        "quality": {
                            "time_alignment": {
                                "method": alignment_method,
                                "clock_sync_model": clock_model,
                            }
                        },
                    }
                )

        try:
            extra = next(metadata_iterator)
        except StopIteration:
            extra = None
        if extra is not None:
            raise ValueError(f"V2 metadata has more rows than frame index in {session_dir}")

    _write_rows(output_frames, frame_rows())
    normalized_events.sort(key=lambda row: (int(row["aligned_monotonic_ns"]), int(row["event"]["index"])))
    _write_rows(output_events, iter(normalized_events))
    return {
        "sources": [relpath(session_dir, run_dir)],
        "session_id": session_id,
        "frame_count": frame_count,
        "event_count": len(normalized_events),
        "transport": "protocol_v2",
        "codec": "h264",
        "clock_alignment": clock_model or {
            "method": "one_way_min_delay",
            "scale": 1.0,
            "offset_ns": one_way_offset_ns,
        },
    }

#!/usr/bin/env python3
"""Build an aligned timeline from normalized UMIFT run streams."""

from __future__ import annotations

import argparse
import bisect
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from umift_laptop_alignment.orchestration.run_layout import append_sync_log, ensure_run_layout, json_line, read_json, register_artifact, relpath, utc_now_iso, write_json


EPISODE_DURATION_TOLERANCE_S = 1.0


@dataclass(frozen=True)
class TimedRows:
    rows: list[dict[str, Any]]
    timestamps: list[int]


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def row_ts(row: dict[str, Any]) -> int | None:
    value = row.get("aligned_monotonic_ns")
    return value if isinstance(value, int) else None


def load_timed(path: Path) -> TimedRows:
    rows = [row for row in iter_jsonl(path) if isinstance(row_ts(row), int)]
    rows.sort(key=lambda row: row_ts(row) or 0)
    return TimedRows(rows=rows, timestamps=[row_ts(row) or 0 for row in rows])


def nearest(timed: TimedRows, timestamp_ns: int) -> tuple[dict[str, Any] | None, int | None]:
    if not timed.timestamps:
        return None, None
    index = bisect.bisect_left(timed.timestamps, timestamp_ns)
    candidates: list[int] = []
    if index < len(timed.timestamps):
        candidates.append(index)
    if index > 0:
        candidates.append(index - 1)
    best_index = min(candidates, key=lambda i: abs(timed.timestamps[i] - timestamp_ns))
    row = timed.rows[best_index]
    return row, timed.timestamps[best_index] - timestamp_ns


def bracket(
    timed: TimedRows,
    timestamp_ns: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not timed.timestamps:
        return None, None
    index = bisect.bisect_left(timed.timestamps, timestamp_ns)
    if index < len(timed.timestamps) and timed.timestamps[index] == timestamp_ns:
        row = timed.rows[index]
        return row, row
    before = timed.rows[index - 1] if index > 0 else None
    after = timed.rows[index] if index < len(timed.rows) else None
    return before, after


def delta_ms(delta_ns: int | None) -> float | None:
    return None if delta_ns is None else delta_ns / 1e6


def in_range(row: dict[str, Any], start_ns: int, end_ns: int) -> bool:
    ts = row_ts(row)
    return ts is not None and start_ns <= ts <= end_ns


def event_code(row: dict[str, Any]) -> str:
    event = row.get("event")
    if isinstance(event, dict):
        code = event.get("code")
        if isinstance(code, str):
            return code
    return ""


def build_episodes(
    *,
    events: list[dict[str, Any]],
    iphone: TimedRows,
    d435: TimedRows,
    coinft: TimedRows,
    max_episode_duration_s: float = 600.0,
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    starts = [row for row in events if event_code(row) == "record_start" and row_ts(row) is not None]
    stops = [row for row in events if event_code(row) == "record_stop" and row_ts(row) is not None]
    if starts or stops:
        events_by_session: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for kind, rows in (("starts", starts), ("stops", stops)):
            for row in rows:
                session_id = row.get("session_id")
                key = session_id if isinstance(session_id, str) and session_id else "<unknown>"
                events_by_session.setdefault(key, {"starts": [], "stops": []})[kind].append(row)

        pairs: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
        for session_id, session_events in events_by_session.items():
            session_starts = sorted(session_events["starts"], key=lambda row: row_ts(row) or 0)
            session_stops = sorted(session_events["stops"], key=lambda row: row_ts(row) or 0)
            if len(session_starts) != len(session_stops):
                raise SystemExit(
                    f"Recording session {session_id} has unmatched recording events: "
                    f"starts={len(session_starts)}, stops={len(session_stops)}. Alignment refused."
                )
            for pair_index, (start, stop) in enumerate(zip(session_starts, session_stops)):
                start_ns = row_ts(start)
                end_ns = row_ts(stop)
                if start_ns is None or end_ns is None:
                    raise SystemExit(f"Recording session {session_id} has an event without aligned timestamp.")
                duration_s = (end_ns - start_ns) / 1e9
                if duration_s <= 0.0 or duration_s > max_episode_duration_s + EPISODE_DURATION_TOLERANCE_S:
                    raise SystemExit(
                        f"Recording session {session_id} has invalid duration {duration_s:.3f}s; "
                        f"expected 0 < duration <= {max_episode_duration_s:.3f}s "
                        f"(+{EPISODE_DURATION_TOLERANCE_S:.3f}s event tolerance)."
                    )
                if pair_index + 1 < len(session_starts):
                    next_start_ns = row_ts(session_starts[pair_index + 1])
                    if next_start_ns is not None and end_ns >= next_start_ns:
                        raise SystemExit(
                            f"Recording session {session_id} has overlapping or out-of-order recording events. "
                            "Alignment refused."
                        )
                pairs.append((start_ns, session_id, start, stop))

        for index, (start_ns, session_id, start, stop) in enumerate(sorted(pairs)):
            end_ns = row_ts(stop)
            episodes.append(
                {
                    "schema_version": 1,
                    "row_type": "aligned_episode",
                    "episode_index": index,
                    "session_id": session_id,
                    "start_aligned_monotonic_ns": start_ns,
                    "end_aligned_monotonic_ns": end_ns,
                    "source": "iphone_recording_event",
                    "start_event": start.get("event", {}),
                    "stop_event": stop.get("event", {}),
                }
            )

    if episodes:
        return episodes

    recorded = [
        row
        for row in iphone.rows
        if isinstance(row.get("recording"), dict) and bool(row.get("recording", {}).get("active"))
    ]
    if recorded:
        return [
            {
                "schema_version": 1,
                "row_type": "aligned_episode",
                "episode_index": 0,
                "start_aligned_monotonic_ns": row_ts(recorded[0]),
                "end_aligned_monotonic_ns": row_ts(recorded[-1]),
                "source": "iphone_recording_flag",
                "start_event": None,
                "stop_event": None,
            }
        ]

    all_ts = iphone.timestamps + d435.timestamps + coinft.timestamps
    if not all_ts:
        return []
    return [
        {
            "schema_version": 1,
            "row_type": "aligned_episode",
            "episode_index": 0,
            "start_aligned_monotonic_ns": min(all_ts),
            "end_aligned_monotonic_ns": max(all_ts),
            "source": "full_available_range",
            "start_event": None,
            "stop_event": None,
        }
    ]


def episode_target_rows(episode: dict[str, Any], iphone: TimedRows, d435: TimedRows) -> list[dict[str, Any]]:
    start_ns = episode.get("start_aligned_monotonic_ns")
    end_ns = episode.get("end_aligned_monotonic_ns")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int):
        return []
    if iphone.rows:
        return iphone.rows
    return [row for row in d435.rows if in_range(row, start_ns, end_ns)]


def compact_iphone(row: dict[str, Any] | None, delta_ns: int | None = 0, max_delta_ms: float | None = None) -> dict[str, Any]:
    valid = row is not None and delta_ns is not None
    if valid and max_delta_ms is not None:
        valid = abs(delta_ns) <= max_delta_ms * 1e6
    if row is None:
        return {"valid": False, "source_delta_ms": None, "dropped_reason": "missing_stream"}
    return {
        "valid": bool(valid),
        "source_delta_ms": delta_ms(delta_ns),
        "dropped_reason": None if valid else "nearest_sample_outside_threshold",
        "session_id": row.get("session_id"),
        "sequence": row.get("sequence"),
        "rgb": row.get("rgb", {}),
        "depth": row.get("depth", {}),
        "pose": row.get("pose", {}),
        "gripper": row.get("gripper", {}),
        "source_file": row.get("source_file"),
    }


def episode_target_timestamps(episode: dict[str, Any], target_fps: float) -> list[int]:
    start_ns = episode.get("start_aligned_monotonic_ns")
    end_ns = episode.get("end_aligned_monotonic_ns")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int) or end_ns <= start_ns:
        return []
    if target_fps <= 0:
        return []
    dt_ns = int(round(1_000_000_000 / target_fps))
    if dt_ns <= 0:
        return []
    count = max(0, int((end_ns - start_ns) // dt_ns))
    return [start_ns + index * dt_ns for index in range(count)]


def compact_d435(row: dict[str, Any] | None, delta_ns: int | None, max_delta_ms: float) -> dict[str, Any]:
    quality = row.get("quality", {}) if isinstance(row, dict) and isinstance(row.get("quality"), dict) else {}
    rgb_depth_pair_valid = bool(quality.get("rgb_depth_pair_valid", True))
    valid = (
        row is not None
        and delta_ns is not None
        and abs(delta_ns) <= max_delta_ms * 1e6
        and rgb_depth_pair_valid
    )
    if row is None:
        return {"valid": False, "source_delta_ms": None, "dropped_reason": "missing_stream"}
    if not rgb_depth_pair_valid:
        dropped_reason = "rgb_depth_timestamp_mismatch"
    elif not valid:
        dropped_reason = "nearest_sample_outside_threshold"
    else:
        dropped_reason = None
    return {
        "valid": bool(valid),
        "source_delta_ms": delta_ms(delta_ns),
        "dropped_reason": dropped_reason,
        "frame_index": row.get("frame_index"),
        "rgb": row.get("rgb", {}),
        "depth": row.get("depth", {}),
        "quality": quality,
        "source_file": row.get("source_file"),
    }


def compact_coinft(row: dict[str, Any] | None, delta_ns: int | None, max_delta_ms: float) -> dict[str, Any]:
    valid = row is not None and delta_ns is not None and abs(delta_ns) <= max_delta_ms * 1e6
    if row is None:
        return {"valid": False, "source_delta_ms": None, "dropped_reason": "missing_stream"}
    return {
        "valid": bool(valid),
        "source_delta_ms": delta_ms(delta_ns),
        "dropped_reason": None if valid else "nearest_sample_outside_threshold",
        "packet_index": row.get("packet_index"),
        "sequence_id": row.get("sequence_id"),
        "left_raw": row.get("left_raw"),
        "right_raw": row.get("right_raw"),
        "side_order": row.get("side_order"),
        "wrench_axes": row.get("wrench_axes"),
        "raw_12ch": row.get("raw_12ch"),
        "zeroed_12ch": row.get("zeroed_12ch"),
        "wrench": row.get("wrench"),
        "left_raw_zeroed": row.get("left_raw_zeroed"),
        "right_raw_zeroed": row.get("right_raw_zeroed"),
        "left_wrench": row.get("left_wrench"),
        "right_wrench": row.get("right_wrench"),
        "calibration": row.get("calibration", {}),
        "quality": row.get("quality", {}),
        "source_file": row.get("source_file"),
    }


def _interpolate_values(before: Any, after: Any, alpha: float) -> list[float] | None:
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        return None
    if any(not isinstance(value, (int, float)) for value in before + after):
        return None
    return [float(left) + (float(right) - float(left)) * alpha for left, right in zip(before, after)]


def compact_coinft_interpolated(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    target_ns: int,
    max_gap_ms: float,
) -> dict[str, Any]:
    if before is None or after is None:
        return {"valid": False, "source_delta_ms": None, "dropped_reason": "missing_bracketing_samples"}
    before_ns = row_ts(before)
    after_ns = row_ts(after)
    if before_ns is None or after_ns is None or before_ns > target_ns or after_ns < target_ns:
        return {"valid": False, "source_delta_ms": None, "dropped_reason": "invalid_bracketing_samples"}
    span_ns = after_ns - before_ns
    if span_ns < 0:
        return {"valid": False, "source_delta_ms": None, "dropped_reason": "invalid_bracketing_samples"}
    nearest_row, nearest_delta = (
        (before, before_ns - target_ns)
        if target_ns - before_ns <= after_ns - target_ns
        else (after, after_ns - target_ns)
    )
    if span_ns > max_gap_ms * 1e6:
        return {
            "valid": False,
            "source_delta_ms": delta_ms(nearest_delta),
            "dropped_reason": "bracketing_gap_outside_threshold",
            "interpolation_span_ms": span_ns / 1e6,
            "before_sequence_id": before.get("sequence_id"),
            "after_sequence_id": after.get("sequence_id"),
        }
    alpha = 0.0 if span_ns == 0 else (target_ns - before_ns) / span_ns
    left_wrench = _interpolate_values(before.get("left_wrench"), after.get("left_wrench"), alpha)
    right_wrench = _interpolate_values(before.get("right_wrench"), after.get("right_wrench"), alpha)
    if left_wrench is None or right_wrench is None:
        return {
            "valid": False,
            "source_delta_ms": delta_ms(nearest_delta),
            "dropped_reason": "missing_calibrated_wrench",
        }
    left_zeroed = _interpolate_values(before.get("left_raw_zeroed"), after.get("left_raw_zeroed"), alpha)
    right_zeroed = _interpolate_values(before.get("right_raw_zeroed"), after.get("right_raw_zeroed"), alpha)
    return {
        "valid": True,
        "aligned_monotonic_ns": target_ns,
        "source_delta_ms": delta_ms(nearest_delta),
        "dropped_reason": None,
        "packet_index": nearest_row.get("packet_index"),
        "sequence_id": nearest_row.get("sequence_id"),
        "before_packet_index": before.get("packet_index"),
        "after_packet_index": after.get("packet_index"),
        "before_sequence_id": before.get("sequence_id"),
        "after_sequence_id": after.get("sequence_id"),
        "interpolation_alpha": alpha,
        "interpolation_span_ms": span_ns / 1e6,
        "left_raw": nearest_row.get("left_raw"),
        "right_raw": nearest_row.get("right_raw"),
        "side_order": nearest_row.get("side_order"),
        "wrench_axes": nearest_row.get("wrench_axes"),
        "raw_12ch": nearest_row.get("raw_12ch"),
        "zeroed_12ch": [left_zeroed, right_zeroed] if left_zeroed is not None and right_zeroed is not None else None,
        "wrench": [left_wrench, right_wrench],
        "left_raw_zeroed": left_zeroed,
        "right_raw_zeroed": right_zeroed,
        "left_wrench": left_wrench,
        "right_wrench": right_wrench,
        "calibration": nearest_row.get("calibration", {}),
        "quality": {
            **(nearest_row.get("quality", {}) if isinstance(nearest_row.get("quality"), dict) else {}),
            "time_alignment": "linear_interpolation",
        },
        "source_file": nearest_row.get("source_file"),
    }


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
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


def align_episode(
    run_dir: Path,
    source_episode_index: int,
    *,
    d435_max_delta_ms: float,
    coinft_max_delta_ms: float,
    iphone_max_delta_ms: float,
    timeline_mode: str,
    target_fps: float,
    max_episode_duration_s: float = 600.0,
) -> dict[str, Any]:
    paths = ensure_run_layout(run_dir)
    episode_name = f"episode_{source_episode_index:06d}"
    normalized_dir = paths.normalized_dir / "episodes" / episode_name
    normalization_report = read_json(normalized_dir / "NORMALIZE_REPORT.json", {})
    if not normalization_report:
        raise SystemExit(f"Episode {source_episode_index} has not been normalized.")
    iphone = load_timed(normalized_dir / "iphone_frames.jsonl")
    events = list(iter_jsonl(normalized_dir / "recording_events.jsonl"))
    events.sort(key=lambda row: row_ts(row) or 0)
    d435 = load_timed(normalized_dir / "d435_frames.jsonl")
    coinft = load_timed(normalized_dir / "coinft_samples.jsonl")

    episodes = build_episodes(
        events=events,
        iphone=iphone,
        d435=d435,
        coinft=coinft,
        max_episode_duration_s=max_episode_duration_s,
    )
    if len(episodes) != 1:
        raise SystemExit(
            f"Episode {source_episode_index} normalization must contain exactly one closed recording window; "
            f"found {len(episodes)}."
        )
    episode = episodes[0]
    episode["episode_index"] = source_episode_index
    episode["source_episode_index"] = source_episode_index
    aligned_dir = paths.aligned_dir / "episodes" / episode_name
    episode_path = aligned_dir / "episode.json"
    timeline_path = aligned_dir / "timeline.jsonl"
    report_path = aligned_dir / "ALIGNMENT_REPORT.json"
    write_json(episode_path, episode)

    counts = {
        "timeline_rows": 0,
        "iphone_target_rows": 0,
        "iphone_valid": 0,
        "iphone_unique_sources": 0,
        "iphone_duplicate_targets": 0,
        "d435_valid": 0,
        "d435_unique_sources": 0,
        "d435_duplicate_targets": 0,
        "coinft_valid": 0,
        "coinft_unique_sources": 0,
        "coinft_duplicate_targets": 0,
        "all_valid": 0,
    }
    seen_sources: dict[str, set[tuple[Any, ...]]] = {"iphone": set(), "d435": set(), "coinft": set()}

    def count_source_use(stream: str, key: tuple[Any, ...] | None, valid: bool) -> None:
        if not valid or key is None:
            return
        if key in seen_sources[stream]:
            counts[f"{stream}_duplicate_targets"] += 1
        else:
            seen_sources[stream].add(key)
            counts[f"{stream}_unique_sources"] += 1

    def timeline_rows() -> Iterator[dict[str, Any]]:
        global_index = 0
        for current_episode in [episode]:
            if timeline_mode == "target-fps":
                targets: Iterable[tuple[int, dict[str, Any] | None, int | None, str]] = (
                    (
                        target_ns,
                        nearest(iphone, target_ns)[0],
                        nearest(iphone, target_ns)[1],
                        "target-fps",
                    )
                    for target_ns in episode_target_timestamps(current_episode, target_fps)
                )
            else:
                targets = (
                    (target_ns, target, 0, "iphone-frame")
                    for target in episode_target_rows(current_episode, iphone, d435)
                    for target_ns in [row_ts(target)]
                    if target_ns is not None
                )
            for sample_index, (target_ns, target, iphone_delta, target_stream) in enumerate(targets):
                d435_row, d435_delta = nearest(d435, target_ns)
                coinft_before, coinft_after = bracket(coinft, target_ns)
                iphone_payload = compact_iphone(
                    target,
                    iphone_delta,
                    iphone_max_delta_ms if timeline_mode == "target-fps" else None,
                )
                d435_payload = compact_d435(d435_row, d435_delta, d435_max_delta_ms)
                coinft_payload = compact_coinft_interpolated(
                    coinft_before,
                    coinft_after,
                    target_ns,
                    coinft_max_delta_ms,
                )
                valid_mask = {
                    "iphone": bool(iphone_payload.get("valid")),
                    "d435": bool(d435_payload.get("valid")),
                    "coinft": bool(coinft_payload.get("valid")),
                }
                counts["timeline_rows"] += 1
                counts["iphone_target_rows"] += 1
                counts["iphone_valid"] += int(valid_mask["iphone"])
                counts["d435_valid"] += int(valid_mask["d435"])
                counts["coinft_valid"] += int(valid_mask["coinft"])
                counts["all_valid"] += int(all(valid_mask.values()))
                count_source_use(
                    "iphone",
                    (iphone_payload.get("source_file"), iphone_payload.get("sequence")),
                    valid_mask["iphone"],
                )
                count_source_use(
                    "d435",
                    (d435_payload.get("source_file"), d435_payload.get("frame_index")),
                    valid_mask["d435"],
                )
                count_source_use(
                    "coinft",
                    (
                        coinft_payload.get("source_file"),
                        coinft_payload.get("before_packet_index"),
                        coinft_payload.get("after_packet_index"),
                    ),
                    valid_mask["coinft"],
                )
                yield {
                    "schema_version": 1,
                    "row_type": "aligned_sample",
                    "run_id": paths.run_dir.name,
                    "episode_index": source_episode_index,
                    "source_episode_index": source_episode_index,
                    "sample_index": sample_index,
                    "global_sample_index": global_index,
                    "aligned_monotonic_ns": target_ns,
                    "target_stream": target_stream,
                    "iphone": iphone_payload,
                    "d435": d435_payload,
                    "coinft": coinft_payload,
                    "valid_mask": valid_mask,
                    "quality": {
                        "timeline_mode": timeline_mode,
                        "target_fps": target_fps if timeline_mode == "target-fps" else None,
                        "iphone_max_delta_ms": iphone_max_delta_ms if timeline_mode == "target-fps" else None,
                        "d435_max_delta_ms": d435_max_delta_ms,
                        "coinft_max_delta_ms": coinft_max_delta_ms,
                    },
                }
                global_index += 1

    written_timeline_rows = write_jsonl(timeline_path, timeline_rows())
    if timeline_mode == "iphone-frame" and iphone.rows and written_timeline_rows != len(iphone.rows):
        raise RuntimeError(
            "authoritative iPhone timeline lost frames: "
            f"normalized={len(iphone.rows)} aligned={written_timeline_rows}"
        )
    report = {
        "schema_version": 1,
        "row_type": "alignment_report",
        "run_id": paths.run_dir.name,
        "source_episode_index": source_episode_index,
        "created_at": utc_now_iso(),
        "inputs": {
            "iphone_frames": len(iphone.rows),
            "recording_events": len(events),
            "d435_frames": len(d435.rows),
            "coinft_samples": len(coinft.rows),
        },
        "thresholds": {
            "iphone_max_delta_ms": iphone_max_delta_ms,
            "d435_max_delta_ms": d435_max_delta_ms,
            "coinft_max_delta_ms": coinft_max_delta_ms,
        },
        "timeline": {
            "mode": timeline_mode,
            "target_fps": target_fps if timeline_mode == "target-fps" else None,
            "target_dt_ms": (1000.0 / target_fps) if timeline_mode == "target-fps" and target_fps > 0 else None,
            "authoritative_iphone_frames_preserved": (
                written_timeline_rows == len(iphone.rows) if timeline_mode == "iphone-frame" else None
            ),
        },
        "outputs": {
            "timeline": relpath(timeline_path, paths.run_dir),
            "episode": relpath(episode_path, paths.run_dir),
            "report": relpath(report_path, paths.run_dir),
        },
        "episodes": 1,
        "counts": counts,
    }
    write_json(report_path, report)

    for stream, role, path in (
        ("alignment", f"timeline_{episode_name}", timeline_path),
        ("alignment", f"episode_{episode_name}", episode_path),
        ("alignment", f"report_{episode_name}", report_path),
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
            "event": "aligned_episode",
            "source_episode_index": source_episode_index,
            "report": relpath(report_path, paths.run_dir),
        },
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Align one normalized UMIFT Episode into an Episode-local timeline.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("episode_index", type=int)
    parser.add_argument("--timeline-mode", choices=["iphone-frame", "target-fps"], default="iphone-frame")
    parser.add_argument("--target-fps", type=float, default=30.0)
    parser.add_argument("--iphone-max-delta-ms", type=float, default=50.0)
    parser.add_argument("--d435-max-delta-ms", type=float, default=80.0)
    parser.add_argument("--coinft-max-delta-ms", type=float, default=25.0)
    parser.add_argument("--max-episode-duration-s", type=float, default=600.0)
    args = parser.parse_args()
    report = align_episode(
        args.run_dir.expanduser().resolve(),
        args.episode_index,
        d435_max_delta_ms=args.d435_max_delta_ms,
        coinft_max_delta_ms=args.coinft_max_delta_ms,
        iphone_max_delta_ms=args.iphone_max_delta_ms,
        timeline_mode=args.timeline_mode,
        target_fps=args.target_fps,
        max_episode_duration_s=args.max_episode_duration_s,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

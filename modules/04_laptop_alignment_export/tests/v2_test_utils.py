from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

from umift_laptop_alignment.capture.receivers.iphone.video_segment_writer import (
    CHECKPOINT_MAGIC,
    CHECKPOINT_VERSION,
)


def write_v2_session(
    run_dir: Path,
    *,
    session_id: str = "v2-session",
    aligned_timestamps: list[int],
    events: list[tuple[int, int, int]],
) -> Path:
    session_dir = run_dir / "raw" / "iphone_stream" / session_id
    video_dir = session_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "protocol_version": 2,
        "session_id": session_id,
        "first_sequence": 0,
        "capture": {"width": 1920, "height": 1440, "frames_per_second": 30, "codec": "h264"},
    }
    (session_dir / "SESSION.json").write_text(json.dumps(session), encoding="utf-8")

    frame_rows = []
    metadata_rows = []
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    for sequence, aligned_ns in enumerate(aligned_timestamps):
        capture_ns = aligned_ns - 1_000
        frame_rows.append(
            {
                "protocol_version": 2,
                "sequence": sequence,
                "capture_timestamp_ns": capture_ns,
                "mac_receive_monotonic_ns": aligned_ns,
                "mac_receive_unix_ns": 1_700_000_000_000_000_000 + aligned_ns,
                "video_segment": 0,
                "video_byte_offset": sequence,
                "video_annex_b_bytes": 1,
                "video_avcc_bytes": 1,
                "key_frame": sequence == 0,
            }
        )
        metadata_rows.append(
            {
                "protocol_version": 2,
                "sequence": sequence,
                "capture_timestamp_ns": capture_ns,
                "flags": 0x3B,
                "tracking_state": 1,
                "pose_frame": 1,
                "pose_valid": True,
                "world_calibrated": True,
                "recording_active": True,
                "camera_in_user_world_transform": identity,
                "rgb_camera_intrinsics": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                "gripper_valid": True,
                "gripper_timestamp_ns": capture_ns,
                "gripper_age_ns": 0,
                "gripper_open_percent": 50.0,
                "depth_valid": False,
                "depth_encoding": 0,
                "depth_width": 0,
                "depth_height": 0,
                "depth_pixel_format": 0,
                "depth_center_m": None,
                "depth_valid_ratio": 0.0,
            }
        )
    event_rows = [
        {
            "protocol_version": 2,
            "sequence": sequence,
            "capture_timestamp_ns": aligned_timestamps[sequence] - 1_000,
            "code": code,
            "event_index": event_index,
            "event_unix_time_ns": 1_700_000_000_000_000_000 + aligned_timestamps[sequence],
            "app_uptime_ns": aligned_timestamps[sequence],
            "reason": "test",
        }
        for sequence, code, event_index in events
    ]

    def write_jsonl(name: str, rows: list[dict]) -> int:
        payload = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode()
        (session_dir / name).write_bytes(payload)
        return len(payload)

    index_size = write_jsonl("frame_index.jsonl", frame_rows)
    metadata_size = write_jsonl("frame_metadata.jsonl", metadata_rows)
    depth_index_size = write_jsonl("depth_index.jsonl", [])
    events_size = write_jsonl("recording_events.jsonl", event_rows)
    (session_dir / "depth_data.f16").write_bytes(b"")
    segment = b"x" * max(1, len(frame_rows))
    (video_dir / "segment_000000.h264").write_bytes(segment)

    prefix = struct.pack(
        ">4sI11Q",
        CHECKPOINT_MAGIC,
        CHECKPOINT_VERSION,
        len(frame_rows) - 1,
        len(frame_rows),
        len(frame_rows),
        0,
        len(frame_rows),
        len(segment),
        index_size,
        metadata_size,
        0,
        depth_index_size,
        events_size,
    )
    (session_dir / "archive_commit.bin").write_bytes(
        prefix + struct.pack(">I", zlib.crc32(prefix) & 0xFFFFFFFF)
    )
    return session_dir

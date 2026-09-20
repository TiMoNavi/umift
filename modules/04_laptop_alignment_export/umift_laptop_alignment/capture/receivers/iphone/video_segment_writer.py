"""Durable H.264 Annex-B segment and frame-index writer for iPhone stream V2."""

from __future__ import annotations

import json
import math
import os
import struct
import zlib
from pathlib import Path
from typing import BinaryIO, Optional, TextIO

from umift_laptop_alignment.capture.receivers.iphone.protocol_v2 import (
    DepthFrame,
    FrameMetadata,
    RecordingEvent,
    VideoCodecConfig,
)


START_CODE = b"\x00\x00\x00\x01"
CHECKPOINT_MAGIC = b"UVC2"
CHECKPOINT_VERSION = 2
CHECKPOINT_STRUCT = struct.Struct(">4sI11QI")


class VideoArchiveError(RuntimeError):
    pass


def finite_or_none(value: float) -> Optional[float]:
    return value if math.isfinite(value) else None


def avcc_to_annex_b(payload: bytes, nal_unit_header_length: int) -> bytes:
    if nal_unit_header_length not in (1, 2, 3, 4):
        raise VideoArchiveError(f"invalid NAL length size {nal_unit_header_length}")
    output = bytearray()
    offset = 0
    while offset < len(payload):
        length_end = offset + nal_unit_header_length
        if length_end > len(payload):
            raise VideoArchiveError("truncated AVCC NAL length")
        nal_length = int.from_bytes(payload[offset:length_end], "big")
        offset = length_end
        nal_end = offset + nal_length
        if nal_length <= 0 or nal_end > len(payload):
            raise VideoArchiveError(
                f"invalid AVCC NAL length {nal_length} at offset {offset} payload={len(payload)}"
            )
        output.extend(START_CODE)
        output.extend(payload[offset:nal_end])
        offset = nal_end
    if not output:
        raise VideoArchiveError("empty H.264 access unit")
    return bytes(output)


class VideoSegmentWriter:
    def __init__(self, session_dir: Path, frames_per_segment: int = 300) -> None:
        self.session_dir = session_dir
        self.video_dir = session_dir / "video"
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.frame_index_path = session_dir / "frame_index.jsonl"
        self.frame_metadata_path = session_dir / "frame_metadata.jsonl"
        self.depth_data_path = session_dir / "depth_data.f16"
        self.depth_index_path = session_dir / "depth_index.jsonl"
        self.recording_events_path = session_dir / "recording_events.jsonl"
        self.checkpoint_path = session_dir / "archive_commit.bin"
        self.frames_per_segment = frames_per_segment
        self.codec_config: Optional[VideoCodecConfig] = None
        self.segment_index = -1
        self.segment_frame_count = 0
        self.segment_file: Optional[BinaryIO] = None
        self.pending_sequence: Optional[int] = None
        self.committed_sequence: Optional[int] = None
        self.frame_count = 0
        self.encoded_bytes = 0
        self._recover_checkpoint()
        self.index_file: TextIO = self.frame_index_path.open("a", encoding="utf-8")
        self.metadata_file: TextIO = self.frame_metadata_path.open("a", encoding="utf-8")
        self.depth_data_file: BinaryIO = self.depth_data_path.open("ab")
        self.depth_index_file: TextIO = self.depth_index_path.open("a", encoding="utf-8")
        self.events_file: TextIO = self.recording_events_path.open("a", encoding="utf-8")
        if self.segment_index >= 0:
            segment_path = self.video_dir / f"segment_{self.segment_index:06d}.h264"
            self.segment_file = segment_path.open("ab")

    def set_codec_config(self, config: VideoCodecConfig, raw_payload: bytes) -> None:
        if self.codec_config is not None and self.codec_config != config:
            raise VideoArchiveError("H.264 codec configuration changed during session")
        self.codec_config = config
        codec_path = self.video_dir / "codec_config.bin"
        if not codec_path.exists():
            codec_path.write_bytes(raw_payload)

    def append_access_unit(
        self,
        *,
        sequence: int,
        capture_timestamp_ns: int,
        is_key_frame: bool,
        avcc_payload: bytes,
        mac_receive_monotonic_ns: int,
        mac_receive_unix_ns: int,
    ) -> None:
        config = self.codec_config
        if config is None:
            raise VideoArchiveError("video frame received before codec config")
        annex_b = avcc_to_annex_b(avcc_payload, config.nal_unit_header_length)
        should_rotate = self.segment_file is None or (
            is_key_frame and self.segment_frame_count >= self.frames_per_segment
        )
        if should_rotate:
            if not is_key_frame:
                raise VideoArchiveError("first frame of an H.264 segment must be a keyframe")
            self._open_next_segment()

        assert self.segment_file is not None
        if self.segment_frame_count == 0:
            for parameter_set in config.parameter_sets:
                self.segment_file.write(START_CODE)
                self.segment_file.write(parameter_set)

        byte_offset = self.segment_file.tell()
        self.segment_file.write(annex_b)
        row = {
            "protocol_version": 2,
            "sequence": sequence,
            "capture_timestamp_ns": capture_timestamp_ns,
            "mac_receive_monotonic_ns": mac_receive_monotonic_ns,
            "mac_receive_unix_ns": mac_receive_unix_ns,
            "video_segment": self.segment_index,
            "video_byte_offset": byte_offset,
            "video_annex_b_bytes": len(annex_b),
            "video_avcc_bytes": len(avcc_payload),
            "key_frame": is_key_frame,
        }
        self.index_file.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.pending_sequence = sequence
        self.segment_frame_count += 1
        self.frame_count += 1
        self.encoded_bytes += len(avcc_payload)

    def append_frame_side_data(
        self,
        *,
        sequence: int,
        capture_timestamp_ns: int,
        metadata: FrameMetadata,
        depth: Optional[DepthFrame],
        events: list[RecordingEvent],
    ) -> None:
        metadata_row = {
            "protocol_version": 2,
            "sequence": sequence,
            "capture_timestamp_ns": capture_timestamp_ns,
            "flags": metadata.flags,
            "tracking_state": metadata.tracking_state,
            "pose_frame": metadata.pose_frame,
            "pose_valid": metadata.pose_valid,
            "world_calibrated": metadata.world_calibrated,
            "recording_active": metadata.recording_active,
            "camera_in_user_world_transform": [
                finite_or_none(value) for value in metadata.camera_in_user_world_transform
            ],
            "rgb_camera_intrinsics": [
                finite_or_none(value) for value in metadata.rgb_camera_intrinsics
            ],
            "gripper_valid": metadata.gripper_valid,
            "gripper_timestamp_ns": metadata.gripper_timestamp_ns,
            "gripper_age_ns": metadata.gripper_age_ns,
            "gripper_open_percent": finite_or_none(metadata.gripper_open_percent),
            "depth_valid": metadata.depth_valid,
            "depth_encoding": metadata.depth_encoding,
            "depth_width": metadata.depth_width,
            "depth_height": metadata.depth_height,
            "depth_pixel_format": metadata.depth_pixel_format,
            "depth_center_m": finite_or_none(metadata.depth_center_m),
            "depth_valid_ratio": finite_or_none(metadata.depth_valid_ratio),
        }
        self.metadata_file.write(
            json.dumps(metadata_row, separators=(",", ":"), allow_nan=False) + "\n"
        )
        if depth is not None:
            depth_offset = self.depth_data_file.tell()
            self.depth_data_file.write(depth.samples)
            depth_row = {
                "protocol_version": 2,
                "sequence": sequence,
                "capture_timestamp_ns": capture_timestamp_ns,
                "width": depth.width,
                "height": depth.height,
                "pixel_format": depth.pixel_format,
                "encoding": "float16_le",
                "byte_offset": depth_offset,
                "byte_count": len(depth.samples),
            }
            self.depth_index_file.write(json.dumps(depth_row, separators=(",", ":")) + "\n")
        for event in events:
            event_row = {
                "protocol_version": 2,
                "sequence": sequence,
                "capture_timestamp_ns": capture_timestamp_ns,
                "code": event.code,
                "event_index": event.event_index,
                "event_unix_time_ns": event.event_unix_time_ns,
                "app_uptime_ns": event.app_uptime_ns,
                "reason": event.reason,
            }
            self.events_file.write(json.dumps(event_row, separators=(",", ":")) + "\n")

    def commit(self) -> Optional[int]:
        if self.pending_sequence is None:
            return self.committed_sequence
        assert self.segment_file is not None
        self.segment_file.flush()
        self.index_file.flush()
        self.metadata_file.flush()
        self.depth_data_file.flush()
        self.depth_index_file.flush()
        self.events_file.flush()
        os.fsync(self.segment_file.fileno())
        os.fsync(self.index_file.fileno())
        os.fsync(self.metadata_file.fileno())
        os.fsync(self.depth_data_file.fileno())
        os.fsync(self.depth_index_file.fileno())
        os.fsync(self.events_file.fileno())
        self.committed_sequence = self.pending_sequence
        self._write_checkpoint()
        return self.committed_sequence

    def close(self) -> None:
        self.commit()
        if self.segment_file is not None:
            self.segment_file.close()
            self.segment_file = None
        self.index_file.close()
        self.metadata_file.close()
        self.depth_data_file.close()
        self.depth_index_file.close()
        self.events_file.close()

    def _open_next_segment(self) -> None:
        if self.segment_file is not None:
            self.segment_file.flush()
            self.segment_file.close()
        self.segment_index += 1
        self.segment_frame_count = 0
        segment_path = self.video_dir / f"segment_{self.segment_index:06d}.h264"
        self.segment_file = segment_path.open("ab")

    def _write_checkpoint(self) -> None:
        assert self.committed_sequence is not None
        assert self.segment_file is not None
        segment_size = os.fstat(self.segment_file.fileno()).st_size
        index_size = os.fstat(self.index_file.fileno()).st_size
        metadata_size = os.fstat(self.metadata_file.fileno()).st_size
        depth_data_size = os.fstat(self.depth_data_file.fileno()).st_size
        depth_index_size = os.fstat(self.depth_index_file.fileno()).st_size
        events_size = os.fstat(self.events_file.fileno()).st_size
        prefix = struct.pack(
            ">4sI11Q",
            CHECKPOINT_MAGIC,
            CHECKPOINT_VERSION,
            self.committed_sequence,
            self.frame_count,
            self.encoded_bytes,
            self.segment_index,
            self.segment_frame_count,
            segment_size,
            index_size,
            metadata_size,
            depth_data_size,
            depth_index_size,
            events_size,
        )
        payload = prefix + struct.pack(">I", zlib.crc32(prefix) & 0xFFFFFFFF)
        temporary_path = self.checkpoint_path.with_suffix(".tmp")
        with temporary_path.open("wb") as checkpoint_file:
            checkpoint_file.write(payload)
            checkpoint_file.flush()
            os.fsync(checkpoint_file.fileno())
        os.replace(temporary_path, self.checkpoint_path)

    def _recover_checkpoint(self) -> None:
        if not self.checkpoint_path.exists():
            if self.frame_index_path.exists() and self.frame_index_path.stat().st_size:
                raise VideoArchiveError(
                    "existing frame index has no durable V2 archive checkpoint"
                )
            return
        payload = self.checkpoint_path.read_bytes()
        if len(payload) != CHECKPOINT_STRUCT.size:
            raise VideoArchiveError("invalid V2 archive checkpoint size")
        (
            magic,
            version,
            committed_sequence,
            frame_count,
            encoded_bytes,
            segment_index,
            segment_frame_count,
            segment_size,
            index_size,
            metadata_size,
            depth_data_size,
            depth_index_size,
            events_size,
            checksum,
        ) = CHECKPOINT_STRUCT.unpack(payload)
        if magic != CHECKPOINT_MAGIC or version != CHECKPOINT_VERSION:
            raise VideoArchiveError("invalid V2 archive checkpoint header")
        if zlib.crc32(payload[:-4]) & 0xFFFFFFFF != checksum:
            raise VideoArchiveError("invalid V2 archive checkpoint checksum")
        segment_path = self.video_dir / f"segment_{segment_index:06d}.h264"
        required_paths = (
            self.frame_index_path,
            self.frame_metadata_path,
            self.depth_data_path,
            self.depth_index_path,
            self.recording_events_path,
            segment_path,
        )
        if any(not path.exists() for path in required_paths):
            raise VideoArchiveError("V2 archive checkpoint references missing files")
        expected_sizes = (
            (self.frame_index_path, index_size),
            (self.frame_metadata_path, metadata_size),
            (self.depth_data_path, depth_data_size),
            (self.depth_index_path, depth_index_size),
            (self.recording_events_path, events_size),
            (segment_path, segment_size),
        )
        if any(path.stat().st_size < size for path, size in expected_sizes):
            raise VideoArchiveError("V2 archive files are shorter than the durable checkpoint")
        for path, size in expected_sizes:
            with path.open("r+b") as archive_file:
                archive_file.truncate(size)
        for extra_segment in self.video_dir.glob("segment_*.h264"):
            try:
                extra_index = int(extra_segment.stem.rsplit("_", 1)[-1])
            except ValueError:
                continue
            if extra_index > segment_index:
                extra_segment.unlink()
        self.committed_sequence = committed_sequence
        self.pending_sequence = committed_sequence
        self.frame_count = frame_count
        self.encoded_bytes = encoded_bytes
        self.segment_index = segment_index
        self.segment_frame_count = segment_frame_count

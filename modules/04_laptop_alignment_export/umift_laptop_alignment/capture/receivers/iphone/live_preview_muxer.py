"""Fragmented MP4 muxing for browser preview without decoding or re-encoding H.264."""

from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from umift_laptop_alignment.capture.receivers.iphone.protocol_v2 import VideoCodecConfig


TIMESCALE = 90_000
TRACK_ID = 1


class LivePreviewMuxError(RuntimeError):
    pass


def _box(box_type: bytes, payload: bytes) -> bytes:
    if len(box_type) != 4:
        raise ValueError("MP4 box type must be four bytes")
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(box_type, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _matrix() -> bytes:
    return struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)


def _avcc_box(config: VideoCodecConfig) -> bytes:
    sequence_parameter_sets = [item for item in config.parameter_sets if item and item[0] & 0x1F == 7]
    picture_parameter_sets = [item for item in config.parameter_sets if item and item[0] & 0x1F == 8]
    if not sequence_parameter_sets or not picture_parameter_sets:
        raise LivePreviewMuxError("codec config must contain SPS and PPS")
    sps = sequence_parameter_sets[0]
    if len(sps) < 4:
        raise LivePreviewMuxError("SPS is too short")
    payload = bytearray(
        [
            1,
            sps[1],
            sps[2],
            sps[3],
            0xFC | (config.nal_unit_header_length - 1),
            0xE0 | len(sequence_parameter_sets),
        ]
    )
    for parameter_set in sequence_parameter_sets:
        payload.extend(struct.pack(">H", len(parameter_set)))
        payload.extend(parameter_set)
    payload.append(len(picture_parameter_sets))
    for parameter_set in picture_parameter_sets:
        payload.extend(struct.pack(">H", len(parameter_set)))
        payload.extend(parameter_set)
    return _box(b"avcC", bytes(payload))


def codec_string(config: VideoCodecConfig) -> str:
    sps = next(
        (item for item in config.parameter_sets if len(item) >= 4 and item[0] & 0x1F == 7),
        None,
    )
    if sps is None:
        raise LivePreviewMuxError("codec config does not contain a valid SPS")
    return f"avc1.{sps[1]:02x}{sps[2]:02x}{sps[3]:02x}"


def make_init_segment(config: VideoCodecConfig) -> bytes:
    ftyp = _box(b"ftyp", b"iso6" + struct.pack(">I", 1) + b"iso6isomavc1dash")
    mvhd = _full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, TIMESCALE, 0)
        + struct.pack(">IHH", 0x00010000, 0x0100, 0)
        + b"\x00" * 8
        + _matrix()
        + b"\x00" * 24
        + struct.pack(">I", 2),
    )
    tkhd = _full_box(
        b"tkhd",
        0,
        0x000007,
        struct.pack(">IIIII", 0, 0, TRACK_ID, 0, 0)
        + b"\x00" * 8
        + struct.pack(">HHHH", 0, 0, 0, 0)
        + _matrix()
        + struct.pack(">II", config.width << 16, config.height << 16),
    )
    mdhd = _full_box(
        b"mdhd",
        0,
        0,
        struct.pack(">IIIIHH", 0, 0, TIMESCALE, 0, 0x55C4, 0),
    )
    hdlr = _full_box(
        b"hdlr",
        0,
        0,
        struct.pack(">I4s", 0, b"vide") + b"\x00" * 12 + b"VideoHandler\x00",
    )
    vmhd = _full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    url = _full_box(b"url ", 0, 1, b"")
    dref = _full_box(b"dref", 0, 0, struct.pack(">I", 1) + url)
    dinf = _box(b"dinf", dref)

    compressor_name = b"\x00" + b"\x00" * 31
    visual_sample_entry = (
        b"\x00" * 6
        + struct.pack(">H", 1)
        + struct.pack(">HHIII", 0, 0, 0, 0, 0)
        + struct.pack(">HH", config.width, config.height)
        + struct.pack(">II", 0x00480000, 0x00480000)
        + struct.pack(">I", 0)
        + struct.pack(">H", 1)
        + compressor_name
        + struct.pack(">HH", 0x0018, 0xFFFF)
        + _avcc_box(config)
        + _box(b"btrt", struct.pack(">III", 0, config.average_bit_rate, config.average_bit_rate))
    )
    avc1 = _box(b"avc1", visual_sample_entry)
    stsd = _full_box(b"stsd", 0, 0, struct.pack(">I", 1) + avc1)
    stts = _full_box(b"stts", 0, 0, struct.pack(">I", 0))
    stsc = _full_box(b"stsc", 0, 0, struct.pack(">I", 0))
    stsz = _full_box(b"stsz", 0, 0, struct.pack(">II", 0, 0))
    stco = _full_box(b"stco", 0, 0, struct.pack(">I", 0))
    stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)
    minf = _box(b"minf", vmhd + dinf + stbl)
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    trak = _box(b"trak", tkhd + mdia)
    trex = _full_box(
        b"trex",
        0,
        0,
        struct.pack(">IIIII", TRACK_ID, 1, 0, 0, 0),
    )
    mvex = _box(b"mvex", trex)
    moov = _box(b"moov", mvhd + trak + mvex)
    return ftyp + moov


@dataclass(frozen=True)
class PreviewSample:
    sequence: int
    capture_timestamp_ns: int
    is_key_frame: bool
    data: bytes


def make_media_fragment(
    fragment_sequence: int,
    base_decode_time: int,
    sample_duration: int,
    samples: list[PreviewSample],
) -> bytes:
    if not samples:
        raise LivePreviewMuxError("cannot create an empty media fragment")
    mfhd = _full_box(b"mfhd", 0, 0, struct.pack(">I", fragment_sequence))
    tfhd = _full_box(b"tfhd", 0, 0x020000, struct.pack(">I", TRACK_ID))
    tfdt = _full_box(b"tfdt", 1, 0, struct.pack(">Q", base_decode_time))

    def make_trun(data_offset: int) -> bytes:
        entries = bytearray()
        for sample in samples:
            sample_flags = 0x02000000 if sample.is_key_frame else 0x01010000
            entries.extend(struct.pack(">III", sample_duration, len(sample.data), sample_flags))
        return _full_box(
            b"trun",
            0,
            0x000701,
            struct.pack(">Ii", len(samples), data_offset) + bytes(entries),
        )

    trun = make_trun(0)
    moof = _box(b"moof", mfhd + _box(b"traf", tfhd + tfdt + trun))
    trun = make_trun(len(moof) + 8)
    moof = _box(b"moof", mfhd + _box(b"traf", tfhd + tfdt + trun))
    mdat = _box(b"mdat", b"".join(sample.data for sample in samples))
    return moof + mdat


class LiveJpegExtractor:
    """Extract one JPEG per key frame fragment to avoid MSE stutter."""

    def __init__(self, preview_dir: Path, width: int = 1920, height: int = 1440) -> None:
        self.preview_dir = preview_dir
        self.width = width
        self.height = height
        self.queue: queue.Queue[tuple[Path, int]] = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None
        self.latest_jpeg_path = preview_dir / "latest.jpg"
        self.latest_sequence = 0
        self.extraction_error: Optional[str] = None

    def start(self) -> None:
        if self.worker_thread is not None:
            return
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.worker_thread is not None:
            self.worker_thread.join(timeout=2.0)
            self.worker_thread = None

    def enqueue_fragment(self, fragment_path: Path, sequence: int) -> None:
        """Non-blocking: enqueue a fragment for JPEG extraction."""
        try:
            self.queue.put_nowait((fragment_path, sequence))
        except queue.Full:
            pass  # Skip if extraction is lagging; preview stays on last good frame

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                fragment_path, sequence = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if not fragment_path.exists():
                continue

            try:
                # Extract first frame as JPEG at reduced resolution to keep preview responsive
                temp_path = self.preview_dir / f".latest_{time.monotonic_ns()}.jpg"
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-loglevel", "error",
                        "-i", str(fragment_path),
                        "-vframes", "1",
                        "-vf", f"scale={self.width//2}:{self.height//2}",
                        "-q:v", "3",
                        "-y",
                        str(temp_path),
                    ],
                    capture_output=True,
                    timeout=1.0,
                )
                if result.returncode == 0 and temp_path.exists():
                    os.replace(temp_path, self.latest_jpeg_path)
                    self.latest_sequence = sequence
                    self.extraction_error = None
                else:
                    self.extraction_error = result.stderr.decode("utf-8", errors="replace")[:200]
                    temp_path.unlink(missing_ok=True)
            except subprocess.TimeoutExpired:
                self.extraction_error = "ffmpeg timeout"
                temp_path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                self.extraction_error = str(exc)[:200]


class LivePreviewMuxer:
    def __init__(
        self,
        session_dir: Path,
        session_id: str,
        fragment_frames: int = 3,
        retained_fragments: int = 60,
        fetch_grace_fragments: int = 30,
    ) -> None:
        self.preview_dir = session_dir / "preview"
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        for stale_fragment in self.preview_dir.glob("fragment_*.m4s"):
            stale_fragment.unlink()
        (self.preview_dir / "manifest.json").unlink(missing_ok=True)
        self.session_id = session_id
        self.fragment_frames = max(1, fragment_frames)
        self.retained_fragments = max(5, retained_fragments)
        self.fetch_grace_fragments = max(1, fetch_grace_fragments)
        self.config: Optional[VideoCodecConfig] = None
        self.pending_samples: list[PreviewSample] = []
        self.fragment_index = 0
        self.total_sample_count = 0
        self.bootstrap_fragment_index: Optional[int] = None
        self.fragment_rows: list[dict[str, object]] = []
        self.jpeg_extractor: Optional[LiveJpegExtractor] = None

    def set_codec_config(self, config: VideoCodecConfig) -> None:
        if self.config is not None and self.config != config:
            raise LivePreviewMuxError("preview codec configuration changed")
        self.config = config
        self._write_atomic(self.preview_dir / "init.mp4", make_init_segment(config))
        self._write_manifest()
        if self.jpeg_extractor is None:
            self.jpeg_extractor = LiveJpegExtractor(self.preview_dir, config.width, config.height)
            self.jpeg_extractor.start()

    def append_access_unit(
        self,
        *,
        sequence: int,
        capture_timestamp_ns: int,
        is_key_frame: bool,
        avcc_payload: bytes,
    ) -> None:
        if self.config is None:
            raise LivePreviewMuxError("preview frame received before codec config")
        if self.total_sample_count == 0 and not self.pending_samples and not is_key_frame:
            return
        if is_key_frame and self.pending_samples:
            self._flush_fragment()
        self.pending_samples.append(
            PreviewSample(sequence, capture_timestamp_ns, is_key_frame, avcc_payload)
        )
        if len(self.pending_samples) >= self.fragment_frames:
            self._flush_fragment()

    def close(self) -> None:
        if self.pending_samples:
            self._flush_fragment()
        if self.jpeg_extractor is not None:
            self.jpeg_extractor.stop()

    def _flush_fragment(self) -> None:
        assert self.config is not None
        samples = self.pending_samples
        self.pending_samples = []
        self.fragment_index += 1
        sample_duration = TIMESCALE // self.config.frames_per_second
        base_decode_time = self.total_sample_count * sample_duration
        fragment_data = make_media_fragment(
            self.fragment_index,
            base_decode_time,
            sample_duration,
            samples,
        )
        filename = f"fragment_{self.fragment_index:08d}.m4s"
        fragment_path = self.preview_dir / filename
        self._write_atomic(fragment_path, fragment_data)
        starts_with_key_frame = samples[0].is_key_frame
        if starts_with_key_frame:
            self.bootstrap_fragment_index = self.fragment_index
            if self.jpeg_extractor is not None:
                self.jpeg_extractor.enqueue_fragment(fragment_path, samples[0].sequence)
        self.total_sample_count += len(samples)
        self.fragment_rows.append(
            {
                "index": self.fragment_index,
                "filename": filename,
                "first_sequence": samples[0].sequence,
                "last_sequence": samples[-1].sequence,
                "first_capture_timestamp_ns": samples[0].capture_timestamp_ns,
                "last_capture_timestamp_ns": samples[-1].capture_timestamp_ns,
                "starts_with_key_frame": starts_with_key_frame,
                "sample_count": len(samples),
            }
        )
        self._prune_fragments()
        self._write_manifest()

    def _prune_fragments(self) -> None:
        retention_floor = max(1, self.fragment_index - self.retained_fragments + 1)
        key_frame_indices = [
            int(row["index"])
            for row in self.fragment_rows
            if bool(row["starts_with_key_frame"]) and int(row["index"]) >= retention_floor
        ]
        # Keep the earliest decodable GOP in the retention window.  Using the
        # newest key frame here caused every preceding fragment to disappear
        # immediately and raced browser requests made from the last manifest.
        minimum_index = min(key_frame_indices) if key_frame_indices else (self.bootstrap_fragment_index or 1)
        retained = [
            row for row in self.fragment_rows if int(row["index"]) >= minimum_index
        ]
        self.fragment_rows = retained
        key_rows = [row for row in retained if bool(row["starts_with_key_frame"])]
        if key_rows:
            self.bootstrap_fragment_index = int(key_rows[0]["index"])

        # Stop advertising an expired GOP immediately, but keep its files for
        # a short grace period.  A browser may have read the previous atomic
        # manifest just before this prune and still be fetching one of those
        # fragment URLs.  The grace files are not part of the active manifest
        # and are bounded, so this removes that manifest-to-fetch 404 race
        # without growing preview storage indefinitely.
        delete_before = max(1, minimum_index - self.fetch_grace_fragments)
        for fragment_path in self.preview_dir.glob("fragment_*.m4s"):
            try:
                fragment_index = int(fragment_path.stem.rsplit("_", 1)[-1])
            except ValueError:
                continue
            if fragment_index < delete_before:
                fragment_path.unlink(missing_ok=True)

    def _write_manifest(self) -> None:
        config = self.config
        payload = {
            "protocol_version": 2,
            "session_id": self.session_id,
            "ready": config is not None and bool(self.fragment_rows),
            "codec": codec_string(config) if config is not None else None,
            "mime_type": f'video/mp4; codecs="{codec_string(config)}"' if config is not None else None,
            "width": config.width if config is not None else None,
            "height": config.height if config is not None else None,
            "frames_per_second": config.frames_per_second if config is not None else None,
            "timescale": TIMESCALE,
            "init": "init.mp4" if config is not None else None,
            "bootstrap_fragment_index": self.bootstrap_fragment_index,
            "latest_fragment_index": self.fragment_index,
            "updated_unix_ns": time.time_ns(),
            "fragments": self.fragment_rows,
        }
        self._write_atomic(
            self.preview_dir / "manifest.json",
            (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
        )

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        temporary_path = path.with_name(f".{path.name}.{time.monotonic_ns()}.tmp")
        temporary_path.write_bytes(payload)
        os.replace(temporary_path, path)

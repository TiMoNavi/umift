#!/usr/bin/env python3
"""Receive and durably archive the authoritative iPhone H.264 stream V2."""

from __future__ import annotations

import argparse
import json
import socket
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from umift_laptop_alignment.capture.receivers.iphone.live_preview_muxer import LivePreviewMuxer
from umift_laptop_alignment.capture.receivers.iphone.protocol_v2 import (
    Packet,
    PacketStreamParser,
    PacketType,
    ProtocolError,
    DepthFrame,
    FrameMetadata,
    RecordingEvent,
    SessionStart,
    VideoCodecConfig,
    make_cumulative_ack,
    make_clock_sync_request,
    parse_clock_sync_response,
    parse_session_start,
    parse_depth_frame,
    parse_frame_metadata,
    parse_recording_event,
    parse_video_codec_config,
)
from umift_laptop_alignment.capture.receivers.iphone.preview_server_v2 import PreviewServer
from umift_laptop_alignment.capture.receivers.iphone.usbmux import (
    DEFAULT_DEVICE_SERIAL,
    UsbmuxError,
    choose_device,
    connect_device_port,
)
from umift_laptop_alignment.capture.receivers.iphone.video_segment_writer import (
    CHECKPOINT_MAGIC,
    CHECKPOINT_STRUCT,
    CHECKPOINT_VERSION,
    VideoArchiveError,
    VideoSegmentWriter,
)


DEFAULT_DEVICE_PORT = 17381
DEFAULT_PREVIEW_HOST = "127.0.0.1"
DEFAULT_PREVIEW_PORT = 8765
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[3] / "captures" / "iphone_stream_v2"
CLOCK_SYNC_INTERVAL_NS = 2_000_000_000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def durable_sequence(session_dir: Path) -> Optional[int]:
    checkpoint_path = session_dir / "archive_commit.bin"
    if not checkpoint_path.exists():
        return None
    payload = checkpoint_path.read_bytes()
    if len(payload) != CHECKPOINT_STRUCT.size:
        raise VideoArchiveError(f"invalid V2 archive checkpoint size in {session_dir}")
    values = CHECKPOINT_STRUCT.unpack(payload)
    if values[0] != CHECKPOINT_MAGIC or values[1] != CHECKPOINT_VERSION:
        raise VideoArchiveError(f"invalid V2 archive checkpoint header in {session_dir}")
    if zlib.crc32(payload[:-4]) & 0xFFFFFFFF != int(values[-1]):
        raise VideoArchiveError(f"invalid V2 archive checkpoint checksum in {session_dir}")
    return int(values[2])


def session_first_sequence(session_dir: Path) -> Optional[int]:
    session_path = session_dir / "SESSION.json"
    if not session_path.exists():
        return None
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    value = payload.get("first_sequence")
    return int(value) if isinstance(value, int) else None


class ArchiveReceiverObserver(Protocol):
    def archive_session_started(
        self,
        receiver: "ArchiveReceiverV2",
        packet: Packet,
        session: SessionStart,
    ) -> None: ...

    def archive_codec_configured(
        self,
        receiver: "ArchiveReceiverV2",
        packet: Packet,
        config: VideoCodecConfig,
    ) -> None: ...

    def archive_video_frame(
        self,
        receiver: "ArchiveReceiverV2",
        packet: Packet,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None: ...

    def archive_frame_metadata(
        self,
        receiver: "ArchiveReceiverV2",
        packet: Packet,
        metadata: FrameMetadata,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None: ...

    def archive_recording_event(
        self,
        receiver: "ArchiveReceiverV2",
        packet: Packet,
        event: RecordingEvent,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None: ...

    def archive_clock_sync_sample(
        self,
        receiver: "ArchiveReceiverV2",
        sample: dict[str, object],
    ) -> None: ...


class ArchiveReceiverV2:
    def __init__(
        self,
        output_root: Path,
        ack_interval_frames: int = 3,
        observer: Optional[ArchiveReceiverObserver] = None,
    ) -> None:
        self.output_root = output_root
        self.ack_interval_frames = max(1, ack_interval_frames)
        self.observer = observer
        self.session_id: Optional[str] = None
        self.session_dir: Optional[Path] = None
        self.writer: Optional[VideoSegmentWriter] = None
        self.preview_muxer: Optional[LivePreviewMuxer] = None
        self.preview_failure_reason: Optional[str] = None
        self.expected_sequence: Optional[int] = None
        self.last_received_sequence: Optional[int] = None
        self.last_acked_sequence: Optional[int] = None
        self.frames_since_ack = 0
        self.started_monotonic = time.monotonic()
        self.initial_frame_count = 0
        self.initial_encoded_bytes = 0
        self.failure_reason: Optional[str] = None
        self.discontinuity: Optional[dict[str, object]] = None
        self.pending_metadata: dict[int, tuple[bytes, FrameMetadata]] = {}
        self.pending_depth: dict[int, tuple[bytes, DepthFrame]] = {}
        self.pending_events: dict[int, dict[int, tuple[bytes, RecordingEvent]]] = {}
        self.last_clock_sync_request_ns = 0
        self.pending_clock_sync_requests: dict[int, int] = {}
        self.clock_sync_sample_count = 0

    def run_socket(self, sock: socket.socket) -> None:
        parser = PacketStreamParser()
        self.last_clock_sync_request_ns = 0
        self.pending_clock_sync_requests.clear()
        while True:
            chunk = sock.recv(1024 * 1024)
            if not chunk:
                return
            receive_monotonic_ns = time.monotonic_ns()
            receive_unix_ns = time.time_ns()
            for packet in parser.feed(chunk):
                self.handle_packet(
                    sock,
                    packet,
                    receive_monotonic_ns=receive_monotonic_ns,
                    receive_unix_ns=receive_unix_ns,
                )
            self._maybe_send_clock_sync(sock)

    def handle_packet(
        self,
        sock: socket.socket,
        packet: Packet,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None:
        if packet.packet_type == PacketType.SESSION_START:
            self._handle_session_start(packet)
            return
        if self.session_id is None or self.writer is None:
            raise ProtocolError("packet received before session start")
        if packet.session_id != self.session_id:
            raise ProtocolError(
                f"session changed from {self.session_id} to {packet.session_id}"
            )
        if packet.packet_type == PacketType.VIDEO_CODEC_CONFIG:
            config = parse_video_codec_config(packet.payload)
            self.writer.set_codec_config(config, packet.payload)
            if self.preview_muxer is not None and self.preview_failure_reason is None:
                try:
                    self.preview_muxer.set_codec_config(config)
                except Exception as exc:  # noqa: BLE001 - preview is non-authoritative
                    self.preview_failure_reason = str(exc)
                    print(f"[iphone-v2-preview] disabled: {exc}")
            self._notify_observer("archive_codec_configured", self, packet, config)
            print(
                "[iphone-v2] codec "
                f"{config.width}x{config.height}@{config.frames_per_second} "
                f"nal={config.nal_unit_header_length} sets={len(config.parameter_sets)}"
            )
            return
        if packet.packet_type == PacketType.CLOCK_SYNC_RESPONSE:
            self._handle_clock_sync_response(
                packet,
                mac_receive_monotonic_ns=receive_monotonic_ns,
                mac_receive_unix_ns=receive_unix_ns,
            )
            return
        if packet.packet_type == PacketType.FRAME_METADATA:
            self._handle_frame_metadata(packet)
            return
        if packet.packet_type == PacketType.DEPTH_FRAME:
            self._handle_depth_frame(packet)
            return
        if packet.packet_type == PacketType.RECORDING_EVENT:
            self._handle_recording_event(packet)
            return
        if packet.packet_type == PacketType.H264_VIDEO_FRAME:
            self._handle_video_frame(
                sock,
                packet,
                receive_monotonic_ns=receive_monotonic_ns,
                receive_unix_ns=receive_unix_ns,
            )
            return
        if packet.packet_type == PacketType.HEARTBEAT:
            return
        if packet.packet_type == PacketType.CAPTURE_ERROR:
            raise ProtocolError(packet.payload.decode("utf-8", errors="replace"))
        raise ProtocolError(f"unexpected packet type {packet.packet_type.name}")

    def _maybe_send_clock_sync(self, sock: socket.socket) -> None:
        if self.session_id is None or self.session_dir is None:
            return
        now_ns = time.monotonic_ns()
        if now_ns - self.last_clock_sync_request_ns < CLOCK_SYNC_INTERVAL_NS:
            return
        request_id = now_ns
        sock.sendall(make_clock_sync_request(self.session_id, request_id, now_ns))
        self.pending_clock_sync_requests[request_id] = now_ns
        self.last_clock_sync_request_ns = now_ns
        if len(self.pending_clock_sync_requests) > 32:
            oldest = sorted(self.pending_clock_sync_requests)[:-32]
            for stale_request_id in oldest:
                self.pending_clock_sync_requests.pop(stale_request_id, None)

    def _handle_clock_sync_response(
        self,
        packet: Packet,
        *,
        mac_receive_monotonic_ns: int,
        mac_receive_unix_ns: int,
    ) -> None:
        assert self.session_dir is not None
        response = parse_clock_sync_response(packet.payload)
        if response.request_id != packet.sequence:
            raise ProtocolError(
                f"clock sync response ID mismatch header={packet.sequence} payload={response.request_id}"
            )
        expected_t1_ns = self.pending_clock_sync_requests.pop(response.request_id, None)
        if expected_t1_ns is None:
            raise ProtocolError(f"clock sync response has unknown request ID {response.request_id}")
        if response.mac_send_monotonic_ns != expected_t1_ns:
            raise ProtocolError(
                "clock sync response changed Mac send timestamp "
                f"expected={expected_t1_ns} got={response.mac_send_monotonic_ns}"
            )
        phone_processing_ns = response.phone_send_uptime_ns - response.phone_receive_uptime_ns
        round_trip_ns = mac_receive_monotonic_ns - expected_t1_ns - phone_processing_ns
        if phone_processing_ns < 0 or round_trip_ns < 0:
            raise ProtocolError(
                f"invalid clock sync timing processing={phone_processing_ns} rtt={round_trip_ns}"
            )
        phone_midpoint_ns = (
            response.phone_receive_uptime_ns + response.phone_send_uptime_ns
        ) // 2
        mac_midpoint_ns = (expected_t1_ns + mac_receive_monotonic_ns) // 2
        row = {
            "protocol_version": 2,
            "row_type": "iphone_clock_sync_sample",
            "session_id": self.session_id,
            "request_id": response.request_id,
            "t1_mac_send_monotonic_ns": expected_t1_ns,
            "t2_phone_receive_uptime_ns": response.phone_receive_uptime_ns,
            "t3_phone_send_uptime_ns": response.phone_send_uptime_ns,
            "t4_mac_receive_monotonic_ns": mac_receive_monotonic_ns,
            "t4_mac_receive_unix_ns": mac_receive_unix_ns,
            "phone_processing_ns": phone_processing_ns,
            "round_trip_ns": round_trip_ns,
            "phone_midpoint_ns": phone_midpoint_ns,
            "mac_midpoint_ns": mac_midpoint_ns,
            "estimated_mac_minus_phone_ns": mac_midpoint_ns - phone_midpoint_ns,
        }
        with (self.session_dir / "clock_sync.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.clock_sync_sample_count += 1
        self._notify_observer("archive_clock_sync_sample", self, row)

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            if self.preview_muxer is not None:
                try:
                    self.preview_muxer.close()
                except Exception as exc:  # noqa: BLE001 - preview is non-authoritative
                    self.preview_failure_reason = str(exc)
            self._write_summary()

    def mark_failed(self, reason: str) -> None:
        self.failure_reason = reason

    def _handle_session_start(self, packet: Packet) -> None:
        session = parse_session_start(packet.payload)
        if session.codec != 1:
            raise ProtocolError(f"unsupported video codec {session.codec}")
        if (session.width, session.height, session.frames_per_second) != (1920, 1440, 30):
            raise ProtocolError(
                "unexpected capture format "
                f"{session.width}x{session.height}@{session.frames_per_second}"
            )
        if self.session_id is None:
            safe_session_id = packet.session_id.replace("/", "_")
            self.session_id = packet.session_id
            self.session_dir, self.discontinuity = self._select_session_dir(
                safe_session_id,
                packet.sequence,
            )
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self.writer = VideoSegmentWriter(self.session_dir)
            self.initial_frame_count = self.writer.frame_count
            self.initial_encoded_bytes = self.writer.encoded_bytes
            self.started_monotonic = time.monotonic()
            preview_instance_id = (
                f"{packet.session_id}:{self.session_dir.name}:{time.time_ns()}"
            )
            self.preview_muxer = LivePreviewMuxer(self.session_dir, preview_instance_id)
            if self.writer.committed_sequence is not None:
                self.last_received_sequence = self.writer.committed_sequence
                self.last_acked_sequence = self.writer.committed_sequence
                self.expected_sequence = self.writer.committed_sequence + 1
            else:
                self.expected_sequence = packet.sequence
            session_payload = {
                "protocol_version": 2,
                "session_id": packet.session_id,
                "archive_generation_id": self.session_dir.name,
                "created_at": utc_now_iso(),
                "first_sequence": packet.sequence,
                "capture": {
                    "width": session.width,
                    "height": session.height,
                    "frames_per_second": session.frames_per_second,
                    "codec": "h264",
                    "average_bit_rate": session.average_bit_rate,
                    "timestamp_timescale": session.timestamp_timescale,
                },
            }
            session_path = self.session_dir / "SESSION.json"
            if not session_path.exists():
                session_path.write_text(
                    json.dumps(session_payload, indent=2) + "\n", encoding="utf-8"
                )
            if self.discontinuity is not None:
                (self.session_dir / "DISCONTINUITY.json").write_text(
                    json.dumps(self.discontinuity, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(
                f"[iphone-v2] session={packet.session_id} "
                f"first_sequence={packet.sequence} dir={self.session_dir}"
            )
            if self.discontinuity is not None:
                print(
                    "[iphone-v2] archive generation boundary "
                    f"expected={self.discontinuity['expected_sequence']} "
                    f"received={packet.sequence}"
                )
            self._notify_observer("archive_session_started", self, packet, session)
            return
        if packet.session_id != self.session_id:
            raise ProtocolError(
                f"receiver already owns session {self.session_id}, got {packet.session_id}"
            )

    def _select_session_dir(
        self,
        safe_session_id: str,
        first_sequence: int,
    ) -> tuple[Path, Optional[dict[str, object]]]:
        base_dir = self.output_root / safe_session_id
        candidate_dirs = [base_dir]
        candidate_dirs.extend(sorted(self.output_root.glob(f"{safe_session_id}__from_*")))
        resumable: list[tuple[int, Path]] = []
        committed_candidates: list[tuple[int, Path]] = []
        has_existing_session = False
        for candidate in candidate_dirs:
            candidate_first = session_first_sequence(candidate)
            if candidate_first is None:
                continue
            has_existing_session = True
            committed = durable_sequence(candidate)
            if committed is not None:
                committed_candidates.append((committed, candidate))
                if candidate_first <= first_sequence <= committed + 1:
                    resumable.append((candidate_first, candidate))
            elif first_sequence == candidate_first:
                resumable.append((candidate_first, candidate))
        if resumable:
            return max(resumable, key=lambda item: item[0])[1], None
        if not has_existing_session:
            return base_dir, None

        generation_dir = self.output_root / (
            f"{safe_session_id}__from_{first_sequence:020d}"
        )
        previous_committed: Optional[int] = None
        previous_dir: Optional[Path] = None
        if committed_candidates:
            previous_committed, previous_dir = max(
                committed_candidates,
                key=lambda item: item[0],
            )
        expected_sequence = (
            previous_committed + 1
            if previous_committed is not None
            else first_sequence
        )
        discontinuity: dict[str, object] = {
            "protocol_version": 2,
            "source_session_id": self.session_id,
            "created_at": utc_now_iso(),
            "reason": "source_sequence_advanced_before_receiver_rearm",
            "expected_sequence": expected_sequence,
            "received_sequence": first_sequence,
            "missing_sequence_count": max(0, first_sequence - expected_sequence),
            "previous_archive_dir": str(previous_dir) if previous_dir is not None else None,
            "previous_committed_sequence": previous_committed,
        }
        return generation_dir, discontinuity

    def _handle_video_frame(
        self,
        sock: socket.socket,
        packet: Packet,
        *,
        receive_monotonic_ns: int,
        receive_unix_ns: int,
    ) -> None:
        assert self.writer is not None
        if self.last_acked_sequence is not None and packet.sequence <= self.last_acked_sequence:
            self._discard_pending_sequence(packet.sequence)
            sock.sendall(make_cumulative_ack(self.session_id or "", self.last_acked_sequence))
            return
        if self.last_received_sequence is not None and packet.sequence <= self.last_received_sequence:
            self._discard_pending_sequence(packet.sequence)
            committed = self.writer.commit()
            if committed is not None:
                sock.sendall(make_cumulative_ack(self.session_id or "", committed))
                self.last_acked_sequence = committed
                self.frames_since_ack = 0
            return
        if self.expected_sequence is None:
            self.expected_sequence = packet.sequence
        if packet.sequence != self.expected_sequence:
            raise ProtocolError(
                f"video sequence gap expected={self.expected_sequence} got={packet.sequence}"
            )

        metadata_entry = self.pending_metadata.pop(packet.sequence, None)
        if metadata_entry is None:
            raise ProtocolError(f"video frame {packet.sequence} arrived without frame metadata")
        metadata = metadata_entry[1]
        depth_entry = self.pending_depth.pop(packet.sequence, None)
        depth = depth_entry[1] if depth_entry is not None else None
        if metadata.depth_valid and depth is None:
            raise ProtocolError(f"video frame {packet.sequence} is missing required depth payload")
        if not metadata.depth_valid and depth is not None:
            raise ProtocolError(f"video frame {packet.sequence} has unexpected depth payload")
        event_map = self.pending_events.pop(packet.sequence, {})
        events = [event_map[index][1] for index in sorted(event_map)]

        self.writer.append_frame_side_data(
            sequence=packet.sequence,
            capture_timestamp_ns=packet.capture_timestamp_ns,
            metadata=metadata,
            depth=depth,
            events=events,
        )
        self.writer.append_access_unit(
            sequence=packet.sequence,
            capture_timestamp_ns=packet.capture_timestamp_ns,
            is_key_frame=packet.is_key_frame,
            avcc_payload=packet.payload,
            mac_receive_monotonic_ns=receive_monotonic_ns,
            mac_receive_unix_ns=receive_unix_ns,
        )
        self.last_received_sequence = packet.sequence
        self.expected_sequence = packet.sequence + 1
        self._notify_observer(
            "archive_frame_metadata",
            self,
            packet,
            metadata,
            receive_monotonic_ns=receive_monotonic_ns,
            receive_unix_ns=receive_unix_ns,
        )
        for event in events:
            self._notify_observer(
                "archive_recording_event",
                self,
                packet,
                event,
                receive_monotonic_ns=receive_monotonic_ns,
                receive_unix_ns=receive_unix_ns,
            )
        self._notify_observer(
            "archive_video_frame",
            self,
            packet,
            receive_monotonic_ns=receive_monotonic_ns,
            receive_unix_ns=receive_unix_ns,
        )
        self.frames_since_ack += 1
        if self.frames_since_ack >= self.ack_interval_frames:
            committed = self.writer.commit()
            if committed is not None:
                sock.sendall(make_cumulative_ack(self.session_id or "", committed))
                self.last_acked_sequence = committed
                self.frames_since_ack = 0
        connection_frame_count = self.writer.frame_count - self.initial_frame_count
        if connection_frame_count > 0 and connection_frame_count % 30 == 0:
            elapsed = max(0.001, time.monotonic() - self.started_monotonic)
            fps = connection_frame_count / elapsed
            connection_encoded_bytes = self.writer.encoded_bytes - self.initial_encoded_bytes
            mbps = connection_encoded_bytes * 8 / elapsed / 1e6
            ack_lag = (
                packet.sequence - self.last_acked_sequence
                if self.last_acked_sequence is not None
                else self.writer.frame_count
            )
            print(
                f"[iphone-v2-perf] frames={self.writer.frame_count} fps={fps:.1f} "
                f"mbps={mbps:.1f} ack_lag={ack_lag} seq={packet.sequence}"
            )
        # Preview is best-effort and runs only after the durable archive path
        # accepted the frame and any due cumulative ACK has been sent.
        if self.preview_muxer is not None and self.preview_failure_reason is None:
            try:
                self.preview_muxer.append_access_unit(
                    sequence=packet.sequence,
                    capture_timestamp_ns=packet.capture_timestamp_ns,
                    is_key_frame=packet.is_key_frame,
                    avcc_payload=packet.payload,
                )
            except Exception as exc:  # noqa: BLE001 - preview is non-authoritative
                self.preview_failure_reason = str(exc)
                print(f"[iphone-v2-preview] disabled: {exc}")

    def _handle_frame_metadata(self, packet: Packet) -> None:
        if self._already_committed(packet.sequence):
            return
        metadata = parse_frame_metadata(packet.payload)
        current = self.pending_metadata.get(packet.sequence)
        if current is not None and current[0] != packet.payload:
            raise ProtocolError(f"conflicting frame metadata for sequence {packet.sequence}")
        self.pending_metadata[packet.sequence] = (packet.payload, metadata)

    def _handle_depth_frame(self, packet: Packet) -> None:
        if self._already_committed(packet.sequence):
            return
        depth = parse_depth_frame(packet.payload)
        current = self.pending_depth.get(packet.sequence)
        if current is not None and current[0] != packet.payload:
            raise ProtocolError(f"conflicting depth frame for sequence {packet.sequence}")
        self.pending_depth[packet.sequence] = (packet.payload, depth)

    def _handle_recording_event(self, packet: Packet) -> None:
        if self._already_committed(packet.sequence):
            return
        event = parse_recording_event(packet.payload)
        events = self.pending_events.setdefault(packet.sequence, {})
        current = events.get(event.event_index)
        if current is not None and current[0] != packet.payload:
            raise ProtocolError(
                f"conflicting recording event {event.event_index} for sequence {packet.sequence}"
            )
        events[event.event_index] = (packet.payload, event)

    def _already_committed(self, sequence: int) -> bool:
        return self.last_acked_sequence is not None and sequence <= self.last_acked_sequence

    def _discard_pending_sequence(self, sequence: int) -> None:
        self.pending_metadata.pop(sequence, None)
        self.pending_depth.pop(sequence, None)
        self.pending_events.pop(sequence, None)

    def _write_summary(self) -> None:
        assert self.writer is not None and self.session_dir is not None
        payload = {
            "protocol_version": 2,
            "session_id": self.session_id,
            "closed_at": utc_now_iso(),
            "frame_count": self.writer.frame_count,
            "encoded_bytes": self.writer.encoded_bytes,
            "depth_bytes": (
                self.writer.depth_data_path.stat().st_size
                if self.writer.depth_data_path.exists()
                else 0
            ),
            "last_received_sequence": self.last_received_sequence,
            "last_acked_sequence": self.last_acked_sequence,
            "sequence_integrity": (
                "failed"
                if self.failure_reason
                else "generation_complete_after_explicit_gap"
                if self.discontinuity is not None
                else "complete"
            ),
            "failure_reason": self.failure_reason,
            "discontinuity": self.discontinuity,
            "preview_status": "failed" if self.preview_failure_reason else "complete",
            "preview_failure_reason": self.preview_failure_reason,
            "clock_sync_sample_count": self.clock_sync_sample_count,
        }
        (self.session_dir / "SUMMARY.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def _notify_observer(self, method_name: str, *args: object, **kwargs: object) -> None:
        if self.observer is None:
            return
        try:
            getattr(self.observer, method_name)(*args, **kwargs)
        except Exception as exc:  # GUI/status observers must never block archive ACKs.
            print(f"[iphone-v2-observer] {method_name} failed: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE_SERIAL)
    parser.add_argument("--port", type=int, default=DEFAULT_DEVICE_PORT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--tcp-host", help="connect directly instead of using usbmuxd")
    parser.add_argument("--ack-interval-frames", type=int, default=3)
    parser.add_argument("--preview-host", default=DEFAULT_PREVIEW_HOST)
    parser.add_argument("--preview-port", type=int, default=DEFAULT_PREVIEW_PORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    receiver = ArchiveReceiverV2(args.output_root, args.ack_interval_frames)
    preview_server = PreviewServer(
        lambda: receiver.session_dir / "preview" if receiver.session_dir is not None else None,
        host=args.preview_host,
        port=args.preview_port,
    )
    try:
        preview_server.start()
        print(f"[iphone-v2-preview] http://{args.preview_host}:{preview_server.port}")
        if args.tcp_host:
            sock = socket.create_connection((args.tcp_host, args.port), timeout=10)
        else:
            device = choose_device(args.device)
            sock = connect_device_port(device, args.port)
        print(f"[iphone-v2] connected port={args.port}")
        with sock:
            sock.settimeout(None)
            receiver.run_socket(sock)
        return 0
    except KeyboardInterrupt:
        print("\n[iphone-v2] stopped")
        return 0
    except (OSError, ProtocolError, VideoArchiveError, UsbmuxError) as exc:
        receiver.mark_failed(str(exc))
        print(f"[iphone-v2] failed: {exc}")
        return 1
    finally:
        receiver.close()
        preview_server.stop()


if __name__ == "__main__":
    raise SystemExit(main())

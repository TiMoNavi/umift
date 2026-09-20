import json
import struct
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from umift_laptop_alignment.capture.receivers.iphone.archive_receiver_v2 import ArchiveReceiverV2
from umift_laptop_alignment.capture.receivers.iphone.live_preview_muxer import (
    LivePreviewMuxer,
    PreviewSample,
    make_init_segment,
    make_media_fragment,
)
from umift_laptop_alignment.capture.receivers.iphone.protocol_v2 import (
    FLAG_KEY_FRAME,
    GOLDEN_PACKET_HEX,
    Packet,
    PacketStreamParser,
    PacketType,
    VideoCodecConfig,
    encode_packet,
    make_clock_sync_request,
    parse_clock_sync_response,
    parse_depth_frame,
    parse_frame_metadata,
    parse_recording_event,
)
from umift_laptop_alignment.capture.receivers.iphone.preview_server_v2 import PreviewServer
from umift_laptop_alignment.capture.receivers.iphone.video_segment_writer import (
    START_CODE,
    avcc_to_annex_b,
)


class FakeSocket:
    def __init__(self) -> None:
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)


class FailingObserver:
    def archive_session_started(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("observer session failure")

    def archive_codec_configured(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("observer codec failure")

    def archive_video_frame(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("observer frame failure")

    def archive_frame_metadata(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("observer metadata failure")

    def archive_recording_event(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("observer event failure")


class IPhoneStreamProtocolV2Tests(unittest.TestCase):
    @staticmethod
    def codec_config() -> VideoCodecConfig:
        return VideoCodecConfig(
            width=1920,
            height=1440,
            frames_per_second=30,
            nal_unit_header_length=4,
            average_bit_rate=14_000_000,
            parameter_sets=(b"\x67\x64\x00\x32", b"\x68\xee\x3c\xb0"),
        )

    @staticmethod
    def metadata_payload(flags: int = 0) -> bytes:
        return struct.pack(
            ">HHI4BHHIqq3f16f9f",
            1,
            148,
            flags,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            -(1 << 63),
            -(1 << 63),
            float("nan"),
            float("nan"),
            0.0,
            *([float("nan")] * 16),
            *([0.0] * 9),
        )

    def send_metadata(
        self,
        receiver: ArchiveReceiverV2,
        sock: FakeSocket,
        session_id: str,
        sequence: int,
        timestamp: int,
        flags: int = 0,
    ) -> None:
        receiver.handle_packet(
            sock,
            Packet(
                PacketType.FRAME_METADATA,
                0,
                session_id,
                sequence,
                timestamp,
                self.metadata_payload(flags),
            ),
            receive_monotonic_ns=sequence,
            receive_unix_ns=sequence + 1000,
        )

    def test_golden_packet(self) -> None:
        packet = Packet(
            packet_type=PacketType.H264_VIDEO_FRAME,
            flags=FLAG_KEY_FRAME,
            session_id="s1",
            sequence=0x0102030405060708,
            capture_timestamp_ns=0x1112131415161718,
            payload=bytes.fromhex("0000000165"),
        )
        encoded = encode_packet(packet)
        self.assertEqual(encoded.hex(), GOLDEN_PACKET_HEX)

    def test_partial_and_coalesced_reads(self) -> None:
        first = bytes.fromhex(GOLDEN_PACKET_HEX)
        second = encode_packet(
            Packet(
                packet_type=PacketType.HEARTBEAT,
                flags=0,
                session_id="s1",
                sequence=9,
                capture_timestamp_ns=10,
                payload=b"ok",
            )
        )
        parser = PacketStreamParser()
        self.assertEqual(parser.feed(first[:17]), [])
        packets = parser.feed(first[17:] + second)
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].sequence, 0x0102030405060708)
        self.assertTrue(packets[0].is_key_frame)
        self.assertEqual(packets[1].payload, b"ok")
        self.assertEqual(parser.buffered_bytes, 0)

    def test_clock_sync_request_and_response_payloads(self) -> None:
        request = PacketStreamParser().feed(
            make_clock_sync_request("s1", request_id=77, mac_send_monotonic_ns=1234)
        )[0]
        self.assertEqual(request.packet_type, PacketType.CLOCK_SYNC_REQUEST)
        self.assertEqual(request.sequence, 77)
        self.assertEqual(struct.unpack(">HHQq", request.payload), (1, 0, 77, 1234))

        response = parse_clock_sync_response(
            struct.pack(">HHQqqq", 1, 0, 77, 1234, 2000, 2010)
        )
        self.assertEqual(response.request_id, 77)
        self.assertEqual(response.mac_send_monotonic_ns, 1234)
        self.assertEqual(response.phone_receive_uptime_ns, 2000)
        self.assertEqual(response.phone_send_uptime_ns, 2010)

    def test_archive_records_four_timestamp_clock_sync_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            receiver = ArchiveReceiverV2(Path(temporary_directory))
            sock = FakeSocket()
            session_id = "clock_session"
            session_payload = struct.pack(">HHHHII", 1920, 1440, 30, 1, 14_000_000, 1_000_000_000)
            receiver.handle_packet(
                sock,
                Packet(PacketType.SESSION_START, 0, session_id, 1, 100, session_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            receiver.pending_clock_sync_requests[1000] = 1000
            receiver.handle_packet(
                sock,
                Packet(
                    PacketType.CLOCK_SYNC_RESPONSE,
                    0,
                    session_id,
                    1000,
                    2010,
                    struct.pack(">HHQqqq", 1, 0, 1000, 1000, 2000, 2010),
                ),
                receive_monotonic_ns=3030,
                receive_unix_ns=4030,
            )
            receiver.close()
            row = json.loads(
                (Path(temporary_directory) / session_id / "clock_sync.jsonl")
                .read_text(encoding="utf-8")
                .strip()
            )

        self.assertEqual(row["t1_mac_send_monotonic_ns"], 1000)
        self.assertEqual(row["t2_phone_receive_uptime_ns"], 2000)
        self.assertEqual(row["t3_phone_send_uptime_ns"], 2010)
        self.assertEqual(row["t4_mac_receive_monotonic_ns"], 3030)
        self.assertEqual(row["round_trip_ns"], 2020)

    def test_avcc_to_annex_b(self) -> None:
        avcc = struct.pack(">I", 2) + b"\x65\x88" + struct.pack(">I", 1) + b"\x06"
        self.assertEqual(
            avcc_to_annex_b(avcc, 4),
            START_CODE + b"\x65\x88" + START_CODE + b"\x06",
        )

    def test_frame_metadata_depth_and_recording_event_payloads(self) -> None:
        transform = tuple(float(index) for index in range(16))
        intrinsics = tuple(float(index + 20) for index in range(9))
        metadata_payload = struct.pack(
            ">HHI4BHHIqq3f16f9f",
            1,
            148,
            0x3F,
            1,
            1,
            1,
            0,
            256,
            192,
            0x66646570,
            123,
            -45,
            67.5,
            0.42,
            0.9,
            *transform,
            *intrinsics,
        )
        metadata = parse_frame_metadata(metadata_payload)
        self.assertTrue(metadata.pose_valid)
        self.assertTrue(metadata.gripper_valid)
        self.assertTrue(metadata.depth_valid)
        self.assertTrue(metadata.world_calibrated)
        self.assertTrue(metadata.recording_active)
        self.assertEqual(metadata.depth_width, 256)
        self.assertEqual(metadata.camera_in_user_world_transform, transform)
        self.assertEqual(metadata.rgb_camera_intrinsics, intrinsics)

        depth_samples = bytes(range(32))
        depth_payload = struct.pack(">HHHHIHH", 1, 16, 4, 4, 0x66646570, 1, 0) + depth_samples
        depth = parse_depth_frame(depth_payload)
        self.assertEqual((depth.width, depth.height), (4, 4))
        self.assertEqual(depth.samples, depth_samples)

        reason = "phase_recording".encode("utf-8")
        event_payload = struct.pack(">HHIqqH", 1, 2, 7, 1000, 2000, len(reason)) + reason
        event = parse_recording_event(event_payload)
        self.assertEqual(event.code, 2)
        self.assertEqual(event.event_index, 7)
        self.assertEqual(event.reason, "phase_recording")

    def test_archive_commit_sends_cumulative_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            receiver = ArchiveReceiverV2(output_root, ack_interval_frames=3)
            sock = FakeSocket()
            session_id = "test_session"
            session_payload = struct.pack(">HHHHII", 1920, 1440, 30, 1, 14_000_000, 1_000_000_000)
            codec_payload = (
                struct.pack(">HHHBBII", 1920, 1440, 30, 4, 2, 14_000_000, 0)
                + struct.pack(">H", 4)
                + b"\x67\x64\x00\x32"
                + struct.pack(">H", 4)
                + b"\x68\xee\x3c\xb0"
            )
            receiver.handle_packet(
                sock,
                Packet(PacketType.SESSION_START, 0, session_id, 10, 100, session_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            receiver.handle_packet(
                sock,
                Packet(PacketType.VIDEO_CODEC_CONFIG, 0, session_id, 10, 100, codec_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            for sequence in range(10, 13):
                nal = b"\x65\x88" if sequence == 10 else b"\x41\x9a"
                self.send_metadata(
                    receiver,
                    sock,
                    session_id,
                    sequence,
                    100 + sequence,
                    flags=(1 << 2) if sequence == 10 else 0,
                )
                if sequence == 10:
                    depth_samples = b"\x00\x3c\x00\x40\x00\x42\x00\x44"
                    depth_payload = (
                        struct.pack(">HHHHIHH", 1, 16, 2, 2, 0x66646570, 1, 0)
                        + depth_samples
                    )
                    receiver.handle_packet(
                        sock,
                        Packet(
                            PacketType.DEPTH_FRAME,
                            0,
                            session_id,
                            sequence,
                            100 + sequence,
                            depth_payload,
                        ),
                        receive_monotonic_ns=sequence,
                        receive_unix_ns=sequence + 1000,
                    )
                    reason = b"phase_recording"
                    event_payload = (
                        struct.pack(">HHIqqH", 1, 2, 0, 1000, 2000, len(reason))
                        + reason
                    )
                    receiver.handle_packet(
                        sock,
                        Packet(
                            PacketType.RECORDING_EVENT,
                            0,
                            session_id,
                            sequence,
                            100 + sequence,
                            event_payload,
                        ),
                        receive_monotonic_ns=sequence,
                        receive_unix_ns=sequence + 1000,
                    )
                receiver.handle_packet(
                    sock,
                    Packet(
                        PacketType.H264_VIDEO_FRAME,
                        FLAG_KEY_FRAME if sequence == 10 else 0,
                        session_id,
                        sequence,
                        100 + sequence,
                        struct.pack(">I", len(nal)) + nal,
                    ),
                    receive_monotonic_ns=sequence,
                    receive_unix_ns=sequence + 1000,
                )
            ack_packets = PacketStreamParser().feed(bytes(sock.sent))
            self.assertEqual(len(ack_packets), 1)
            self.assertEqual(ack_packets[0].packet_type, PacketType.CUMULATIVE_ACK)
            self.assertEqual(ack_packets[0].sequence, 12)
            receiver.close()

            segment = output_root / session_id / "video" / "segment_000000.h264"
            segment_data = segment.read_bytes()
            self.assertTrue(segment_data.startswith(START_CODE + b"\x67\x64\x00\x32"))
            index_lines = (output_root / session_id / "frame_index.jsonl").read_text().splitlines()
            self.assertEqual(len(index_lines), 3)
            preview_manifest = output_root / session_id / "preview" / "manifest.json"
            self.assertTrue(preview_manifest.exists())
            metadata_lines = (output_root / session_id / "frame_metadata.jsonl").read_text().splitlines()
            self.assertEqual(len(metadata_lines), 3)
            depth_index_lines = (output_root / session_id / "depth_index.jsonl").read_text().splitlines()
            self.assertEqual(len(depth_index_lines), 1)
            self.assertEqual((output_root / session_id / "depth_data.f16").read_bytes(), depth_samples)
            event_lines = (output_root / session_id / "recording_events.jsonl").read_text().splitlines()
            self.assertEqual(len(event_lines), 1)
            self.assertEqual(json.loads(event_lines[0])["code"], 2)

    def test_observer_failure_does_not_block_archive_or_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            receiver = ArchiveReceiverV2(
                Path(temporary_directory),
                ack_interval_frames=1,
                observer=FailingObserver(),
            )
            sock = FakeSocket()
            session_id = "observer_failure"
            session_payload = struct.pack(">HHHHII", 1920, 1440, 30, 1, 14_000_000, 1_000_000_000)
            codec_payload = (
                struct.pack(">HHHBBII", 1920, 1440, 30, 4, 2, 14_000_000, 0)
                + struct.pack(">H", 4)
                + b"\x67\x64\x00\x32"
                + struct.pack(">H", 4)
                + b"\x68\xee\x3c\xb0"
            )
            receiver.handle_packet(
                sock,
                Packet(PacketType.SESSION_START, 0, session_id, 1, 100, session_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            receiver.handle_packet(
                sock,
                Packet(PacketType.VIDEO_CODEC_CONFIG, 0, session_id, 1, 100, codec_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            nal = b"\x65\x88"
            self.send_metadata(receiver, sock, session_id, 1, 101)
            receiver.handle_packet(
                sock,
                Packet(
                    PacketType.H264_VIDEO_FRAME,
                    FLAG_KEY_FRAME,
                    session_id,
                    1,
                    101,
                    struct.pack(">I", len(nal)) + nal,
                ),
                receive_monotonic_ns=3,
                receive_unix_ns=4,
            )
            ack_packets = PacketStreamParser().feed(bytes(sock.sent))
            self.assertEqual([packet.sequence for packet in ack_packets], [1])
            self.assertEqual(receiver.writer.frame_count if receiver.writer else 0, 1)
            receiver.close()

    def test_preview_failure_does_not_block_durable_archive_or_ack(self) -> None:
        class FailingPreviewMuxer:
            def append_access_unit(self, **_kwargs: object) -> None:
                raise OSError("simulated preview disk failure")

            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            receiver = ArchiveReceiverV2(output_root, ack_interval_frames=3)
            sock = FakeSocket()
            session_id = "preview_failure"
            session_payload = struct.pack(">HHHHII", 1920, 1440, 30, 1, 14_000_000, 1_000_000_000)
            codec_payload = (
                struct.pack(">HHHBBII", 1920, 1440, 30, 4, 2, 14_000_000, 0)
                + struct.pack(">H", 4)
                + b"\x67\x64\x00\x32"
                + struct.pack(">H", 4)
                + b"\x68\xee\x3c\xb0"
            )
            receiver.handle_packet(
                sock,
                Packet(PacketType.SESSION_START, 0, session_id, 0, 100, session_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            receiver.handle_packet(
                sock,
                Packet(PacketType.VIDEO_CODEC_CONFIG, 0, session_id, 0, 100, codec_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            receiver.preview_muxer = FailingPreviewMuxer()  # type: ignore[assignment]

            for sequence in range(3):
                nal = b"\x65\x88" if sequence == 0 else b"\x41\x9a"
                self.send_metadata(receiver, sock, session_id, sequence, 100 + sequence)
                receiver.handle_packet(
                    sock,
                    Packet(
                        PacketType.H264_VIDEO_FRAME,
                        FLAG_KEY_FRAME if sequence == 0 else 0,
                        session_id,
                        sequence,
                        100 + sequence,
                        struct.pack(">I", len(nal)) + nal,
                    ),
                    receive_monotonic_ns=sequence + 3,
                    receive_unix_ns=sequence + 1003,
                )

            index_lines = (output_root / session_id / "frame_index.jsonl").read_text().splitlines()
            ack_packets = PacketStreamParser().feed(bytes(sock.sent))
            self.assertEqual(len(index_lines), 3)
            self.assertEqual([packet.sequence for packet in ack_packets], [2])
            self.assertIn("simulated preview disk failure", receiver.preview_failure_reason or "")
            receiver.close()

    def test_archive_resumes_same_session_from_durable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            session_id = "resume_session"
            session_payload = struct.pack(">HHHHII", 1920, 1440, 30, 1, 14_000_000, 1_000_000_000)
            codec_payload = (
                struct.pack(">HHHBBII", 1920, 1440, 30, 4, 2, 14_000_000, 0)
                + struct.pack(">H", 4)
                + b"\x67\x64\x00\x32"
                + struct.pack(">H", 4)
                + b"\x68\xee\x3c\xb0"
            )

            first_receiver = ArchiveReceiverV2(output_root, ack_interval_frames=1)
            first_socket = FakeSocket()
            first_receiver.handle_packet(
                first_socket,
                Packet(PacketType.SESSION_START, 0, session_id, 10, 100, session_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            first_receiver.handle_packet(
                first_socket,
                Packet(PacketType.VIDEO_CODEC_CONFIG, 0, session_id, 10, 100, codec_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            for sequence in range(10, 13):
                nal = b"\x65\x88" if sequence == 10 else b"\x41\x9a"
                self.send_metadata(
                    first_receiver,
                    first_socket,
                    session_id,
                    sequence,
                    100 + sequence,
                )
                first_receiver.handle_packet(
                    first_socket,
                    Packet(
                        PacketType.H264_VIDEO_FRAME,
                        FLAG_KEY_FRAME if sequence == 10 else 0,
                        session_id,
                        sequence,
                        100 + sequence,
                        struct.pack(">I", len(nal)) + nal,
                    ),
                    receive_monotonic_ns=sequence,
                    receive_unix_ns=sequence + 1000,
                )
            first_receiver.close()
            first_manifest = json.loads(
                (output_root / session_id / "preview" / "manifest.json").read_text()
            )

            second_receiver = ArchiveReceiverV2(output_root, ack_interval_frames=1)
            second_socket = FakeSocket()
            second_receiver.handle_packet(
                second_socket,
                Packet(PacketType.SESSION_START, 0, session_id, 13, 113, session_payload),
                receive_monotonic_ns=20,
                receive_unix_ns=21,
            )
            second_receiver.handle_packet(
                second_socket,
                Packet(PacketType.VIDEO_CODEC_CONFIG, 0, session_id, 13, 113, codec_payload),
                receive_monotonic_ns=20,
                receive_unix_ns=21,
            )
            for sequence, key_frame in ((13, False), (14, True)):
                nal = b"\x65\x88" if key_frame else b"\x41\x9a"
                self.send_metadata(
                    second_receiver,
                    second_socket,
                    session_id,
                    sequence,
                    100 + sequence,
                )
                second_receiver.handle_packet(
                    second_socket,
                    Packet(
                        PacketType.H264_VIDEO_FRAME,
                        FLAG_KEY_FRAME if key_frame else 0,
                        session_id,
                        sequence,
                        100 + sequence,
                        struct.pack(">I", len(nal)) + nal,
                    ),
                    receive_monotonic_ns=sequence,
                    receive_unix_ns=sequence + 1000,
                )
            second_receiver.close()

            ack_packets = PacketStreamParser().feed(bytes(second_socket.sent))
            self.assertEqual([packet.sequence for packet in ack_packets], [13, 14])
            index_lines = (output_root / session_id / "frame_index.jsonl").read_text().splitlines()
            self.assertEqual(len(index_lines), 5)
            summary = json.loads((output_root / session_id / "SUMMARY.json").read_text())
            self.assertEqual(summary["frame_count"], 5)
            self.assertEqual(summary["last_received_sequence"], 14)
            manifest = json.loads((output_root / session_id / "preview" / "manifest.json").read_text())
            self.assertTrue(manifest["ready"])
            self.assertEqual(manifest["fragments"][0]["first_sequence"], 14)
            self.assertNotEqual(manifest["session_id"], first_manifest["session_id"])

    def test_archive_sequence_jump_starts_explicit_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            session_id = "generation_session"
            session_payload = struct.pack(">HHHHII", 1920, 1440, 30, 1, 14_000_000, 1_000_000_000)
            codec_payload = (
                struct.pack(">HHHBBII", 1920, 1440, 30, 4, 2, 14_000_000, 0)
                + struct.pack(">H", 4)
                + b"\x67\x64\x00\x32"
                + struct.pack(">H", 4)
                + b"\x68\xee\x3c\xb0"
            )

            first_receiver = ArchiveReceiverV2(output_root, ack_interval_frames=1)
            first_socket = FakeSocket()
            first_receiver.handle_packet(
                first_socket,
                Packet(PacketType.SESSION_START, 0, session_id, 10, 100, session_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            first_receiver.handle_packet(
                first_socket,
                Packet(PacketType.VIDEO_CODEC_CONFIG, 0, session_id, 10, 100, codec_payload),
                receive_monotonic_ns=1,
                receive_unix_ns=2,
            )
            for sequence in range(10, 13):
                nal = b"\x65\x88" if sequence == 10 else b"\x41\x9a"
                self.send_metadata(first_receiver, first_socket, session_id, sequence, 100 + sequence)
                first_receiver.handle_packet(
                    first_socket,
                    Packet(
                        PacketType.H264_VIDEO_FRAME,
                        FLAG_KEY_FRAME if sequence == 10 else 0,
                        session_id,
                        sequence,
                        100 + sequence,
                        struct.pack(">I", len(nal)) + nal,
                    ),
                    receive_monotonic_ns=sequence,
                    receive_unix_ns=sequence + 1000,
                )
            first_receiver.close()

            second_receiver = ArchiveReceiverV2(output_root, ack_interval_frames=1)
            second_socket = FakeSocket()
            second_receiver.handle_packet(
                second_socket,
                Packet(PacketType.SESSION_START, 0, session_id, 20, 200, session_payload),
                receive_monotonic_ns=20,
                receive_unix_ns=21,
            )
            second_receiver.handle_packet(
                second_socket,
                Packet(PacketType.VIDEO_CODEC_CONFIG, 0, session_id, 20, 200, codec_payload),
                receive_monotonic_ns=20,
                receive_unix_ns=21,
            )
            self.send_metadata(second_receiver, second_socket, session_id, 20, 200)
            nal = b"\x65\x88"
            second_receiver.handle_packet(
                second_socket,
                Packet(
                    PacketType.H264_VIDEO_FRAME,
                    FLAG_KEY_FRAME,
                    session_id,
                    20,
                    200,
                    struct.pack(">I", len(nal)) + nal,
                ),
                receive_monotonic_ns=20,
                receive_unix_ns=1020,
            )
            second_receiver.close()

            generation_dir = output_root / f"{session_id}__from_{20:020d}"
            self.assertEqual(len((output_root / session_id / "frame_index.jsonl").read_text().splitlines()), 3)
            self.assertEqual(len((generation_dir / "frame_index.jsonl").read_text().splitlines()), 1)
            discontinuity = json.loads((generation_dir / "DISCONTINUITY.json").read_text())
            self.assertEqual(discontinuity["expected_sequence"], 13)
            self.assertEqual(discontinuity["received_sequence"], 20)
            self.assertEqual(discontinuity["missing_sequence_count"], 7)
            summary = json.loads((generation_dir / "SUMMARY.json").read_text())
            self.assertEqual(summary["last_acked_sequence"], 20)
            self.assertEqual(
                summary["sequence_integrity"],
                "generation_complete_after_explicit_gap",
            )
            self.assertEqual(summary["discontinuity"], discontinuity)

    def test_fmp4_init_and_media_fragment(self) -> None:
        init_segment = make_init_segment(self.codec_config())
        self.assertEqual(init_segment[4:8], b"ftyp")
        first_box_size = struct.unpack_from(">I", init_segment)[0]
        self.assertEqual(init_segment[first_box_size + 4:first_box_size + 8], b"moov")
        self.assertIn(b"avcC", init_segment)

        sample = PreviewSample(
            sequence=1,
            capture_timestamp_ns=2,
            is_key_frame=True,
            data=struct.pack(">I", 2) + b"\x65\x88",
        )
        fragment = make_media_fragment(1, 0, 3000, [sample])
        self.assertEqual(fragment[4:8], b"moof")
        moof_size = struct.unpack_from(">I", fragment)[0]
        self.assertEqual(fragment[moof_size + 4:moof_size + 8], b"mdat")
        self.assertTrue(fragment.endswith(sample.data))

    def test_live_preview_manifest_starts_from_keyframe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory)
            muxer = LivePreviewMuxer(
                session_dir,
                "preview_session",
                fragment_frames=2,
                retained_fragments=5,
            )
            muxer.set_codec_config(self.codec_config())
            for sequence in range(4):
                nal = b"\x65\x88" if sequence == 0 else b"\x41\x9a"
                muxer.append_access_unit(
                    sequence=sequence,
                    capture_timestamp_ns=sequence * 33_333_333,
                    is_key_frame=sequence == 0,
                    avcc_payload=struct.pack(">I", len(nal)) + nal,
                )
            muxer.close()
            manifest = json.loads((session_dir / "preview" / "manifest.json").read_text())
            self.assertTrue(manifest["ready"])
            self.assertEqual(manifest["codec"], "avc1.640032")
            self.assertEqual(manifest["bootstrap_fragment_index"], 1)
            self.assertEqual(len(manifest["fragments"]), 2)

    def test_live_preview_retains_earliest_keyframe_in_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory)
            muxer = LivePreviewMuxer(
                session_dir,
                "preview_session",
                fragment_frames=1,
                retained_fragments=5,
                fetch_grace_fragments=2,
            )
            muxer.set_codec_config(self.codec_config())
            for sequence in range(10):
                is_key_frame = sequence % 3 == 0
                nal = b"\x65\x88" if is_key_frame else b"\x41\x9a"
                muxer.append_access_unit(
                    sequence=sequence,
                    capture_timestamp_ns=sequence * 33_333_333,
                    is_key_frame=is_key_frame,
                    avcc_payload=struct.pack(">I", len(nal)) + nal,
                )

            manifest = json.loads((session_dir / "preview" / "manifest.json").read_text())
            fragment_indices = [row["index"] for row in manifest["fragments"]]

            self.assertEqual(manifest["bootstrap_fragment_index"], 7)
            self.assertEqual(fragment_indices, [7, 8, 9, 10])
            self.assertTrue((session_dir / "preview" / "fragment_00000007.m4s").is_file())
            self.assertTrue((session_dir / "preview" / "fragment_00000005.m4s").is_file())
            self.assertFalse((session_dir / "preview" / "fragment_00000004.m4s").exists())

    def test_preview_server_waiting_and_media_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory)
            preview_dir = session_dir / "preview"
            current_preview_dir = None
            server = PreviewServer(lambda: current_preview_dir, port=0)
            server.start()
            base_url = f"http://127.0.0.1:{server.port}"
            try:
                with urllib.request.urlopen(f"{base_url}/") as response:
                    html = response.read().decode("utf-8")
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                self.assertIn("<video", html)
                self.assertIn("MediaSource", html)

                with urllib.request.urlopen(f"{base_url}/api/manifest") as response:
                    self.assertEqual(json.load(response), {"ready": False})

                preview_dir.mkdir()
                (preview_dir / "manifest.json").write_text(
                    json.dumps({"ready": True, "session_id": "test"}),
                    encoding="utf-8",
                )
                (preview_dir / "init.mp4").write_bytes(b"init")
                (preview_dir / "fragment_00000001.m4s").write_bytes(b"fragment")
                current_preview_dir = preview_dir

                with urllib.request.urlopen(f"{base_url}/api/manifest") as response:
                    self.assertTrue(json.load(response)["ready"])
                    self.assertEqual(response.headers.get_content_type(), "application/json")
                with urllib.request.urlopen(f"{base_url}/preview/init.mp4") as response:
                    self.assertEqual(response.read(), b"init")
                    self.assertEqual(response.headers.get_content_type(), "video/mp4")
                with urllib.request.urlopen(
                    f"{base_url}/preview/fragment_00000001.m4s"
                ) as response:
                    self.assertEqual(response.read(), b"fragment")
                    self.assertEqual(response.headers.get_content_type(), "video/mp4")
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(f"{base_url}/preview/missing.m4s")
                self.assertEqual(context.exception.code, 404)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()

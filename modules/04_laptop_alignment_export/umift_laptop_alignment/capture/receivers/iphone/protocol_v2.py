"""Binary protocol shared by the iPhone H.264 sender and Mac archive receiver."""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass


MAGIC = 0x554D5632  # UMV2
VERSION = 2
HEADER_LENGTH = 40
MAXIMUM_PAYLOAD_LENGTH = 8 * 1024 * 1024
HEADER_STRUCT = struct.Struct(">IHHIHHQqII")
GOLDEN_PACKET_HEX = (
    "554d56320002000300000001000200280102030405060708"
    "1112131415161718000000050000000073310000000165"
)


class PacketType(enum.IntEnum):
    SESSION_START = 1
    VIDEO_CODEC_CONFIG = 2
    H264_VIDEO_FRAME = 3
    SESSION_END = 4
    CAPTURE_ERROR = 5
    HEARTBEAT = 6
    FRAME_METADATA = 7
    DEPTH_FRAME = 8
    RECORDING_EVENT = 9
    CLOCK_SYNC_RESPONSE = 10
    CUMULATIVE_ACK = 0x8001
    CLOCK_SYNC_REQUEST = 0x8002


FLAG_KEY_FRAME = 1 << 0


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    packet_type: PacketType
    flags: int
    session_id: str
    sequence: int
    capture_timestamp_ns: int
    payload: bytes

    @property
    def is_key_frame(self) -> bool:
        return bool(self.flags & FLAG_KEY_FRAME)


@dataclass(frozen=True)
class SessionStart:
    width: int
    height: int
    frames_per_second: int
    codec: int
    average_bit_rate: int
    timestamp_timescale: int


@dataclass(frozen=True)
class VideoCodecConfig:
    width: int
    height: int
    frames_per_second: int
    nal_unit_header_length: int
    average_bit_rate: int
    parameter_sets: tuple[bytes, ...]


FRAME_METADATA_STRUCT = struct.Struct(">HHI4BHHIqq3f16f9f")
DEPTH_FRAME_HEADER_STRUCT = struct.Struct(">HHHHIHH")
RECORDING_EVENT_HEADER_STRUCT = struct.Struct(">HHIqqH")
CLOCK_SYNC_REQUEST_STRUCT = struct.Struct(">HHQq")
CLOCK_SYNC_RESPONSE_STRUCT = struct.Struct(">HHQqqq")


@dataclass(frozen=True)
class FrameMetadata:
    flags: int
    tracking_state: int
    pose_frame: int
    depth_encoding: int
    depth_width: int
    depth_height: int
    depth_pixel_format: int
    gripper_timestamp_ns: int
    gripper_age_ns: int
    gripper_open_percent: float
    depth_center_m: float
    depth_valid_ratio: float
    camera_in_user_world_transform: tuple[float, ...]
    rgb_camera_intrinsics: tuple[float, ...]

    @property
    def pose_valid(self) -> bool:
        return bool(self.flags & (1 << 0))

    @property
    def gripper_valid(self) -> bool:
        return bool(self.flags & (1 << 1))

    @property
    def depth_valid(self) -> bool:
        return bool(self.flags & (1 << 2))

    @property
    def world_calibrated(self) -> bool:
        return bool(self.flags & (1 << 3))

    @property
    def recording_active(self) -> bool:
        return bool(self.flags & (1 << 4))


@dataclass(frozen=True)
class DepthFrame:
    width: int
    height: int
    pixel_format: int
    encoding: int
    samples: bytes


@dataclass(frozen=True)
class RecordingEvent:
    code: int
    event_index: int
    event_unix_time_ns: int
    app_uptime_ns: int
    reason: str


@dataclass(frozen=True)
class ClockSyncResponse:
    request_id: int
    mac_send_monotonic_ns: int
    phone_receive_uptime_ns: int
    phone_send_uptime_ns: int


def encode_packet(packet: Packet) -> bytes:
    session_data = packet.session_id.encode("utf-8")
    if len(session_data) > 0xFFFF:
        raise ProtocolError("session ID is too long")
    if len(packet.payload) > MAXIMUM_PAYLOAD_LENGTH:
        raise ProtocolError(f"payload is too large: {len(packet.payload)}")
    header = HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        int(packet.packet_type),
        packet.flags,
        len(session_data),
        HEADER_LENGTH,
        packet.sequence,
        packet.capture_timestamp_ns,
        len(packet.payload),
        0,
    )
    return header + session_data + packet.payload


class PacketStreamParser:
    """Incrementally parses packets across partial and coalesced socket reads."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Packet]:
        self._buffer.extend(data)
        packets: list[Packet] = []
        while True:
            if len(self._buffer) < HEADER_LENGTH:
                break
            (
                magic,
                version,
                packet_type_raw,
                flags,
                session_id_length,
                header_length,
                sequence,
                capture_timestamp_ns,
                payload_length,
                _reserved,
            ) = HEADER_STRUCT.unpack_from(self._buffer)
            if magic != MAGIC:
                raise ProtocolError(f"invalid magic 0x{magic:08x}")
            if version != VERSION:
                raise ProtocolError(f"unsupported version {version}")
            if header_length != HEADER_LENGTH:
                raise ProtocolError(f"invalid header length {header_length}")
            if payload_length > MAXIMUM_PAYLOAD_LENGTH:
                raise ProtocolError(f"payload is too large: {payload_length}")
            try:
                packet_type = PacketType(packet_type_raw)
            except ValueError as exc:
                raise ProtocolError(f"unknown packet type {packet_type_raw}") from exc

            total_length = header_length + session_id_length + payload_length
            if len(self._buffer) < total_length:
                break
            session_start = header_length
            session_end = session_start + session_id_length
            try:
                session_id = bytes(self._buffer[session_start:session_end]).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("invalid UTF-8 session ID") from exc
            payload = bytes(self._buffer[session_end:total_length])
            del self._buffer[:total_length]
            packets.append(
                Packet(
                    packet_type=packet_type,
                    flags=flags,
                    session_id=session_id,
                    sequence=sequence,
                    capture_timestamp_ns=capture_timestamp_ns,
                    payload=payload,
                )
            )
        return packets


def parse_session_start(payload: bytes) -> SessionStart:
    if len(payload) != 16:
        raise ProtocolError(f"session start payload must be 16 bytes, got {len(payload)}")
    width, height, fps, codec, bit_rate, timescale = struct.unpack(">HHHHII", payload)
    return SessionStart(width, height, fps, codec, bit_rate, timescale)


def parse_video_codec_config(payload: bytes) -> VideoCodecConfig:
    if len(payload) < 16:
        raise ProtocolError("codec config payload is shorter than 16 bytes")
    width, height, fps, nal_length, parameter_set_count, bit_rate, _reserved = struct.unpack_from(
        ">HHHBBII", payload
    )
    if nal_length not in (1, 2, 3, 4):
        raise ProtocolError(f"invalid NAL length size {nal_length}")
    offset = 16
    parameter_sets: list[bytes] = []
    for _index in range(parameter_set_count):
        if offset + 2 > len(payload):
            raise ProtocolError("truncated parameter set length")
        parameter_set_length = struct.unpack_from(">H", payload, offset)[0]
        offset += 2
        end = offset + parameter_set_length
        if end > len(payload):
            raise ProtocolError("truncated parameter set")
        parameter_sets.append(payload[offset:end])
        offset = end
    if offset != len(payload):
        raise ProtocolError("unexpected trailing codec config bytes")
    return VideoCodecConfig(
        width=width,
        height=height,
        frames_per_second=fps,
        nal_unit_header_length=nal_length,
        average_bit_rate=bit_rate,
        parameter_sets=tuple(parameter_sets),
    )


def parse_frame_metadata(payload: bytes) -> FrameMetadata:
    if len(payload) != FRAME_METADATA_STRUCT.size:
        raise ProtocolError(
            f"frame metadata payload must be {FRAME_METADATA_STRUCT.size} bytes, got {len(payload)}"
        )
    values = FRAME_METADATA_STRUCT.unpack(payload)
    version, payload_length = values[:2]
    if version != 1 or payload_length != FRAME_METADATA_STRUCT.size:
        raise ProtocolError(
            f"unsupported frame metadata version={version} length={payload_length}"
        )
    return FrameMetadata(
        flags=values[2],
        tracking_state=values[3],
        pose_frame=values[4],
        depth_encoding=values[5],
        depth_width=values[7],
        depth_height=values[8],
        depth_pixel_format=values[9],
        gripper_timestamp_ns=values[10],
        gripper_age_ns=values[11],
        gripper_open_percent=values[12],
        depth_center_m=values[13],
        depth_valid_ratio=values[14],
        camera_in_user_world_transform=tuple(values[15:31]),
        rgb_camera_intrinsics=tuple(values[31:40]),
    )


def parse_depth_frame(payload: bytes) -> DepthFrame:
    if len(payload) < DEPTH_FRAME_HEADER_STRUCT.size:
        raise ProtocolError("depth frame payload is shorter than its header")
    version, header_length, width, height, pixel_format, encoding, _reserved = (
        DEPTH_FRAME_HEADER_STRUCT.unpack_from(payload)
    )
    if version != 1 or header_length != DEPTH_FRAME_HEADER_STRUCT.size:
        raise ProtocolError(
            f"unsupported depth payload version={version} header={header_length}"
        )
    if encoding != 1:
        raise ProtocolError(f"unsupported depth encoding {encoding}")
    samples = payload[header_length:]
    expected_length = width * height * 2
    if len(samples) != expected_length:
        raise ProtocolError(
            f"depth sample bytes mismatch expected={expected_length} got={len(samples)}"
        )
    return DepthFrame(width, height, pixel_format, encoding, samples)


def parse_recording_event(payload: bytes) -> RecordingEvent:
    if len(payload) < RECORDING_EVENT_HEADER_STRUCT.size:
        raise ProtocolError("recording event payload is shorter than its header")
    version, code, event_index, event_unix_ns, app_uptime_ns, reason_length = (
        RECORDING_EVENT_HEADER_STRUCT.unpack_from(payload)
    )
    if version != 1:
        raise ProtocolError(f"unsupported recording event version {version}")
    reason_bytes = payload[RECORDING_EVENT_HEADER_STRUCT.size:]
    if len(reason_bytes) != reason_length:
        raise ProtocolError(
            f"recording event reason length mismatch expected={reason_length} got={len(reason_bytes)}"
        )
    try:
        reason = reason_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("recording event reason is not UTF-8") from exc
    return RecordingEvent(code, event_index, event_unix_ns, app_uptime_ns, reason)


def parse_clock_sync_response(payload: bytes) -> ClockSyncResponse:
    if len(payload) != CLOCK_SYNC_RESPONSE_STRUCT.size:
        raise ProtocolError(
            f"clock sync response must be {CLOCK_SYNC_RESPONSE_STRUCT.size} bytes, got {len(payload)}"
        )
    version, _reserved, request_id, t1_ns, t2_ns, t3_ns = CLOCK_SYNC_RESPONSE_STRUCT.unpack(payload)
    if version != 1:
        raise ProtocolError(f"unsupported clock sync response version {version}")
    return ClockSyncResponse(request_id, t1_ns, t2_ns, t3_ns)


def make_clock_sync_request(session_id: str, request_id: int, mac_send_monotonic_ns: int) -> bytes:
    payload = CLOCK_SYNC_REQUEST_STRUCT.pack(1, 0, request_id, mac_send_monotonic_ns)
    return encode_packet(
        Packet(
            packet_type=PacketType.CLOCK_SYNC_REQUEST,
            flags=0,
            session_id=session_id,
            sequence=request_id,
            capture_timestamp_ns=mac_send_monotonic_ns,
            payload=payload,
        )
    )


def make_cumulative_ack(session_id: str, acked_through_sequence: int) -> bytes:
    return encode_packet(
        Packet(
            packet_type=PacketType.CUMULATIVE_ACK,
            flags=0,
            session_id=session_id,
            sequence=acked_through_sequence,
            capture_timestamp_ns=0,
            payload=b"",
        )
    )

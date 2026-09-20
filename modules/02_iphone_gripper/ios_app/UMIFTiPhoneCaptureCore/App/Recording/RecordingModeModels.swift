import Foundation

enum RecordingTransportStatus: Equatable {
    case notImplemented
    case disconnected
    case listening(UInt16)
    case connected
    case connectedClients(Int)
    case failed(String)

    var displayText: String {
        switch self {
        case .notImplemented:
            return "not implemented"
        case .disconnected:
            return "disconnected"
        case .listening(let port):
            return "listening \(port)"
        case .connected:
            return "connected"
        case .connectedClients(let count):
            return "connected \(count)"
        case .failed(let reason):
            return "failed \(reason)"
        }
    }
}

struct RecordingRuntimeState: Equatable {
    var isRecording = false
    var countdownValue: Int?
    var bufferDurationS: Double = 0
    var bufferedFrameCount = 0
    var syncValid = false
    var syncErrorMS: Double?
    var transportStatus: RecordingTransportStatus = .notImplemented
    var localFileStatus = RecordingLocalFileStatus.idle
    var latestPacketPreview = RecordingTransmitPacketPreview.empty
    var h264EncoderText = "h264 waiting"
    var h264SpoolText = "encoded spool empty"
    var captureIntegrityText = "ok"

    var phaseText: String {
        if let countdownValue {
            return "countdown \(countdownValue)"
        }
        return isRecording ? "recording" : "idle"
    }

    var syncText: String {
        let validText = syncValid ? "ok" : "hold"
        let errorText = syncErrorMS.map { String(format: "%.1fms", $0) } ?? "--"
        return "\(validText) \(errorText)"
    }

    var bufferText: String {
        String(format: "%.2fs / %d", bufferDurationS, bufferedFrameCount)
    }

    var localFileText: String {
        localFileStatus.displayText
    }

}

struct RecordingModeContract {
    var currentRecords: [String]
    var currentSends: [String]
    var internalCapture: [String]
    var transmitPacketV1: [String]
    var knownGaps: [String]

    static let currentImplementation = RecordingModeContract(
        currentRecords: [
            "record_countdown_started / recording_started / stop_reason events",
            "UI phase only: previewCalibrated -> startCountdown -> recording",
            "live wide timestamp and frame summary from ARKit",
            "live ultrawide timestamp and frame summary from private ARFrame stream",
            "live gripper opening/confidence/marker state after path calibration",
            "live ARKit pose, intrinsics, depth summary, and sync status"
        ],
        currentSends: [
            "local debug JSONL file is written during recording",
            "TCP JSONL stream listens on port 17381 during app runtime",
            "normal preview/startCountdown/recording frames stream continuously to the Mac; invalid fields are flagged before calibration",
            "record_start / record_stop events and recording flags define the valid demo segment",
            "TCP stream includes JPEG RGB payload when a receiver is connected"
        ],
        internalCapture: [
            "timestamp_ns on the wide ARKit timeline",
            "wide RGB frame metadata and optionally frame bytes for local buffer",
            "wide camera intrinsics and camera pose in user-world coordinates for local alignment",
            "ARKit depth map metadata and optional TCP depth payload for local alignment",
            "private ultrawide timestamp and frame metadata",
            "gripper open_percent, confidence, left/right marker states",
            "sync state: wide timestamp, ultrawide timestamp, gripper age, sync error",
            "events generated during capture"
        ],
        transmitPacketV1: RecordingTransmitPacketV1.fieldSummary,
        knownGaps: [
            "3-second aligned ring buffer is implemented for lightweight metadata",
            "full RGB/depth payload jobs use strict FIFO ordering and expose current/max encoder backlog",
            "fixed transmit frame is still JSONL debug transport, not compact binary transport",
            "RGB payload is JPEG base64 and depth payload is float16 base64 in JSONL for debug; compact chunked binary is still future work",
            "Mac receiver writes raw JSONL and receiver-aligned JSONL, not compact binary yet",
            "cross-device timebase uses one-way receiver alignment, not true handshake sync yet",
            "receiver acknowledgements and a persistent lossless payload spool are not wired into the app yet"
        ]
    )
}

enum RecordingTimebaseStatusV1: UInt8, Codable, Equatable {
    case unsynchronized = 0
    case estimating = 1
    case synchronized = 2

    var displayText: String {
        switch self {
        case .unsynchronized:
            return "local only"
        case .estimating:
            return "estimating"
        case .synchronized:
            return "synchronized"
        }
    }
}

enum RecordingEventCodeV1: String, Codable, Equatable {
    case coinFTRestartRequested = "coinft_restart_requested"
    case recordStart = "record_start"
    case recordStop = "record_stop"
    case recordDiscard = "record_discard"
}

enum RecordingRGBEncodingV1: UInt8, Codable, Equatable {
    case bgra8888 = 1
    case jpeg = 2
}

struct RecordingRGBPayloadV1 {
    var encoding: String
    var width: Int
    var height: Int
    var byteCount: Int
    var base64: String
}

struct RecordingWidePreviewPayloadV1 {
    var encoding: String
    var width: Int
    var height: Int
    var byteCount: Int
    var base64: String
}

struct RecordingDepthPayloadV1 {
    var encoding: String
    var byteCount: Int
    var base64: String
}

struct RecordingDepthPreviewPayloadV1 {
    var encoding: String
    var width: Int
    var height: Int
    var byteCount: Int
    var base64: String
}

struct RecordingDepthPreviewThumbnailPayloadV1 {
    var encoding: String
    var width: Int
    var height: Int
    var byteCount: Int
    var base64: String
}

struct RecordingUltraPreviewPayloadV1 {
    var encoding: String
    var width: Int
    var height: Int
    var byteCount: Int
    var base64: String
}

struct RecordingTransmitPacketV1: Codable, Equatable {
    static let schemaVersion: UInt16 = 1

    var schemaVersion = Self.schemaVersion
    var sequence: UInt32
    var alignedTimestampNS: Int64
    var localCaptureTimestampNS: Int64
    var timebaseStatus: RecordingTimebaseStatusV1

    // Pose is ARKit camera pose in the calibrated user-world frame.
    var poseX: Float
    var poseY: Float
    var poseZ: Float
    var rollDeg: Float
    var pitchDeg: Float
    var yawDeg: Float
    var poseFrame: String
    var worldCalibrated: Bool
    var worldOriginStatus: String

    var gripperOpenPercent: Float

    // Main wide-camera RGB image captured at the same aligned timestamp.
    var rgbWidth: UInt16
    var rgbHeight: UInt16
    var rgbEncoding: RecordingRGBEncodingV1
    var rgbPayload: Data

    var flags: UInt32

    static let fieldSummary = [
        "schema_version: UInt16 = 1",
        "sequence: UInt32",
        "aligned_timestamp_ns: Int64, cross-device aligned capture time",
        "local_capture_timestamp_ns: Int64, local monotonic capture time for diagnostics",
        "timebase_status: unsynchronized / estimating / synchronized",
        "pose: x/y/z meters + roll/pitch/yaw degrees in user-world",
        "pose_frame: user_world when world origin is marked, unavailable otherwise",
        "world_calibrated: Bool",
        "world_origin_status: calibrated / not_marked",
        "gripper_open_percent: Float",
        "main_rgb: width + height + encoding + payload bytes",
        "preview_payloads: schema fields reserved but disabled during capture to protect training throughput",
        "main_depth: optional ARKit depth metadata + float16_le_base64 payload in JSONL stream",
        "flags: validity bits for pose/gripper/rgb/timebase/recording"
    ]
}

struct RecordingTransmitFlags {
    static let poseValid: UInt32 = 1 << 0
    static let gripperValid: UInt32 = 1 << 1
    static let rgbValid: UInt32 = 1 << 2
    static let timebaseSynchronized: UInt32 = 1 << 3
    static let recording: UInt32 = 1 << 4
    static let depthValid: UInt32 = 1 << 5
}

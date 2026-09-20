import CoreVideo
import Foundation
import simd

struct RecordingPoseSample {
    var positionM: SIMD3<Float>
    var eulerDeg: SIMD3<Float>
}

struct RecordingWideFrameSample {
    var frameSequence: UInt64
    var timestampNS: Int64
    var trackingText: String
    var cameraInUserWorldTransform: simd_float4x4?
    var poseFrame: String
    var worldCalibrated: Bool
    var worldOriginStatus: String
    var rgbWidth: Int
    var rgbHeight: Int
    var rgbPixelFormat: OSType
    var rgbCameraIntrinsics: simd_float3x3?
    var rgbCaptureDeviceType: String
    var depthAvailable: Bool
    var depthSource: String
    var depthWidth: Int?
    var depthHeight: Int?
    var depthUnit: String
    var depthPixelFormat: OSType?
    var depthCenterM: Double?
    var depthValidRatio: Double
    var depthConfidenceText: String
    var previewSource: String

    init(
        frameSequence: UInt64,
        timestampNS: Int64,
        trackingText: String,
        cameraInUserWorldTransform: simd_float4x4?,
        poseFrame: String? = nil,
        worldCalibrated: Bool? = nil,
        worldOriginStatus: String? = nil,
        rgbPixelBuffer: CVPixelBuffer,
        rgbCameraIntrinsics: simd_float3x3? = nil,
        rgbCaptureDeviceType: String = "unknown",
        depthAvailable: Bool = false,
        depthSource: String = "arkit_depth_unavailable",
        depthWidth: Int? = nil,
        depthHeight: Int? = nil,
        depthUnit: String = "meter",
        depthPixelFormat: OSType? = nil,
        depthCenterM: Double? = nil,
        depthValidRatio: Double = 0,
        depthConfidenceText: String = "empty",
        previewSource: String = "Wide"
    ) {
        self.frameSequence = frameSequence
        self.timestampNS = timestampNS
        self.trackingText = trackingText
        self.cameraInUserWorldTransform = cameraInUserWorldTransform
        let hasUserWorldPose = cameraInUserWorldTransform != nil
        self.poseFrame = poseFrame ?? (hasUserWorldPose ? "user_world" : "unavailable")
        self.worldCalibrated = worldCalibrated ?? hasUserWorldPose
        self.worldOriginStatus = worldOriginStatus ?? (hasUserWorldPose ? "calibrated" : "not_marked")
        self.rgbWidth = CVPixelBufferGetWidth(rgbPixelBuffer)
        self.rgbHeight = CVPixelBufferGetHeight(rgbPixelBuffer)
        self.rgbPixelFormat = CVPixelBufferGetPixelFormatType(rgbPixelBuffer)
        self.rgbCameraIntrinsics = rgbCameraIntrinsics
        self.rgbCaptureDeviceType = rgbCaptureDeviceType
        self.depthAvailable = depthAvailable
        self.depthSource = depthSource
        self.depthWidth = depthWidth
        self.depthHeight = depthHeight
        self.depthUnit = depthUnit
        self.depthPixelFormat = depthPixelFormat
        self.depthCenterM = depthCenterM
        self.depthValidRatio = depthValidRatio
        self.depthConfidenceText = depthConfidenceText
        self.previewSource = previewSource
    }
}

struct RecordingGripperSample {
    var timestampNS: Int64
    var openPercent: Double?
}

struct RecordingAlignedFrameSample {
    var wide: RecordingWideFrameSample
    var gripper: RecordingGripperSample?
    var alignedTimestampNS: Int64
    var timebaseStatus: RecordingTimebaseStatusV1

    var localCaptureTimestampNS: Int64 {
        wide.timestampNS
    }

    var gripperAgeMS: Double? {
        guard let gripper else { return nil }
        return Double(wide.timestampNS - gripper.timestampNS) / 1_000_000.0
    }

    var pose: RecordingPoseSample? {
        guard let transform = wide.cameraInUserWorldTransform else { return nil }
        return RecordingPoseMath.pose(from: transform)
    }

    var poseQuaternionQWXYZ: [Float]? {
        guard let transform = wide.cameraInUserWorldTransform else { return nil }
        return RecordingPoseMath.quaternionQWXYZ(from: transform)
    }

    var cameraInUserWorldTransformRows: [[Float]]? {
        guard let transform = wide.cameraInUserWorldTransform else { return nil }
        return RecordingPoseMath.matrixRows(transform)
    }

    var rgbCameraIntrinsicsRows: [[Float]]? {
        guard let intrinsics = wide.rgbCameraIntrinsics else { return nil }
        return RecordingPoseMath.matrixRows(intrinsics)
    }

    var hasValidGripper: Bool {
        guard let openPercent = gripper?.openPercent,
              openPercent.isFinite,
              let gripperAgeMS
        else { return false }
        return abs(gripperAgeMS) <= 250.0
    }

    var flagsForBufferedFrame: UInt32 {
        var flags: UInt32 = 0
        if pose != nil {
            flags |= RecordingTransmitFlags.poseValid
        }
        if hasValidGripper {
            flags |= RecordingTransmitFlags.gripperValid
        }
        if wide.rgbWidth > 0, wide.rgbHeight > 0 {
            flags |= RecordingTransmitFlags.rgbValid
        }
        if wide.depthAvailable, (wide.depthWidth ?? 0) > 0, (wide.depthHeight ?? 0) > 0 {
            flags |= RecordingTransmitFlags.depthValid
        }
        if timebaseStatus == .synchronized {
            flags |= RecordingTransmitFlags.timebaseSynchronized
        }
        return flags
    }
}

struct RecordingTransmitPacketPreview: Equatable {
    var sequence: UInt32 = 0
    var alignedTimestampNS: Int64?
    var localCaptureTimestampNS: Int64?
    var timebaseStatus: RecordingTimebaseStatusV1 = .unsynchronized
    var rgbText = "rgb --"
    var poseText = "pose --"
    var gripperText = "grip --"
    var flags: UInt32 = 0
    var flagsText = "flags --"

    static let empty = RecordingTransmitPacketPreview()

    var summaryText: String {
        "seq \(sequence) \(rgbText) \(gripperText)"
    }

    var timestampText: String {
        guard let alignedTimestampNS else { return "aligned --" }
        return "aligned \(alignedTimestampNS)"
    }
}

struct RecordingTimebaseMapper {
    var status: RecordingTimebaseStatusV1 = .unsynchronized
    var offsetToAlignedNS: Int64?

    func alignedTimestampNS(forLocalTimestampNS localTimestampNS: Int64) -> Int64 {
        guard let offsetToAlignedNS else { return localTimestampNS }
        return localTimestampNS + offsetToAlignedNS
    }
}

struct RecordingFrameRingBuffer {
    private var frameTimestampsNS: [Int64] = []
    private(set) var latestFrame: RecordingAlignedFrameSample?
    private(set) var latestGripper: RecordingGripperSample?

    private let maxDurationNS: Int64 = 3_000_000_000
    private var timebase = RecordingTimebaseMapper()

    var frameCount: Int {
        frameTimestampsNS.count
    }

    var durationS: Double {
        guard let first = frameTimestampsNS.first, let last = frameTimestampsNS.last else { return 0 }
        return max(0, Double(last - first) / 1_000_000_000.0)
    }

    mutating func reset() {
        frameTimestampsNS.removeAll(keepingCapacity: true)
        latestFrame = nil
        latestGripper = nil
        timebase = RecordingTimebaseMapper()
    }

    mutating func updateGripper(_ sample: RecordingGripperSample) {
        latestGripper = sample
    }

    mutating func appendWideFrame(_ sample: RecordingWideFrameSample) -> RecordingAlignedFrameSample {
        let aligned = RecordingAlignedFrameSample(
            wide: sample,
            gripper: latestGripper,
            alignedTimestampNS: timebase.alignedTimestampNS(forLocalTimestampNS: sample.timestampNS),
            timebaseStatus: timebase.status
        )
        latestFrame = aligned
        frameTimestampsNS.append(sample.timestampNS)
        prune(keepingLatestTimestampNS: sample.timestampNS)
        return aligned
    }

    private mutating func prune(keepingLatestTimestampNS latestTimestampNS: Int64) {
        let oldestAllowed = latestTimestampNS - maxDurationNS
        guard let firstKeptIndex = frameTimestampsNS.firstIndex(where: { $0 >= oldestAllowed }) else {
            frameTimestampsNS.removeAll(keepingCapacity: true)
            return
        }
        if firstKeptIndex > 0 {
            frameTimestampsNS.removeFirst(firstKeptIndex)
        }
    }
}

struct RecordingPacketBuilder {
    private(set) var nextSequence: UInt32 = 0

    mutating func resetSequence() {
        nextSequence = 0
    }

    mutating func makePreview(from sample: RecordingAlignedFrameSample, isRecording: Bool) -> RecordingTransmitPacketPreview {
        let sequence = nextSequence
        nextSequence &+= 1

        var flags = sample.flagsForBufferedFrame
        if isRecording {
            flags |= RecordingTransmitFlags.recording
        }

        return RecordingTransmitPacketPreview(
            sequence: sequence,
            alignedTimestampNS: sample.alignedTimestampNS,
            localCaptureTimestampNS: sample.localCaptureTimestampNS,
            timebaseStatus: sample.timebaseStatus,
            rgbText: "\(sample.wide.rgbWidth)x\(sample.wide.rgbHeight)",
            poseText: Self.poseText(sample.pose),
            gripperText: Self.gripperText(sample.gripper?.openPercent, ageMS: sample.gripperAgeMS),
            flags: flags,
            flagsText: Self.flagsText(flags)
        )
    }

    func makePacket(
        from sample: RecordingAlignedFrameSample,
        sequence: UInt32,
        rgbEncoding: RecordingRGBEncodingV1,
        rgbPayload: Data,
        isRecording: Bool
    ) -> RecordingTransmitPacketV1 {
        let pose = sample.pose
        var flags = sample.flagsForBufferedFrame
        if rgbPayload.isEmpty {
            flags &= ~RecordingTransmitFlags.rgbValid
        }
        if isRecording {
            flags |= RecordingTransmitFlags.recording
        }

        return RecordingTransmitPacketV1(
            sequence: sequence,
            alignedTimestampNS: sample.alignedTimestampNS,
            localCaptureTimestampNS: sample.localCaptureTimestampNS,
            timebaseStatus: sample.timebaseStatus,
            poseX: pose?.positionM.x ?? .nan,
            poseY: pose?.positionM.y ?? .nan,
            poseZ: pose?.positionM.z ?? .nan,
            rollDeg: pose?.eulerDeg.x ?? .nan,
            pitchDeg: pose?.eulerDeg.y ?? .nan,
            yawDeg: pose?.eulerDeg.z ?? .nan,
            poseFrame: sample.wide.poseFrame,
            worldCalibrated: sample.wide.worldCalibrated,
            worldOriginStatus: sample.wide.worldOriginStatus,
            gripperOpenPercent: sample.hasValidGripper ? Float(sample.gripper?.openPercent ?? .nan) : .nan,
            rgbWidth: UInt16(clamping: sample.wide.rgbWidth),
            rgbHeight: UInt16(clamping: sample.wide.rgbHeight),
            rgbEncoding: rgbEncoding,
            rgbPayload: rgbPayload,
            flags: flags
        )
    }

    private static func poseText(_ pose: RecordingPoseSample?) -> String {
        guard let pose else { return "pose --" }
        return String(
            format: "x%.2f y%.2f z%.2f r%.0f p%.0f y%.0f",
            pose.positionM.x,
            pose.positionM.y,
            pose.positionM.z,
            pose.eulerDeg.x,
            pose.eulerDeg.y,
            pose.eulerDeg.z
        )
    }

    private static func gripperText(_ openPercent: Double?, ageMS: Double?) -> String {
        guard let openPercent else { return "grip --" }
        let ageText = ageMS.map { String(format: "%.0fms", $0) } ?? "--"
        return String(format: "grip %.1f%% %@", openPercent, ageText)
    }

    private static func flagsText(_ flags: UInt32) -> String {
        String(format: "0x%02X", flags)
    }
}

private enum RecordingPoseMath {
    static func pose(from transform: simd_float4x4) -> RecordingPoseSample {
        let position = SIMD3<Float>(
            transform.columns.3.x,
            transform.columns.3.y,
            transform.columns.3.z
        )
        let rotation = simd_float3x3(
            SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            SIMD3<Float>(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
        let quaternion = simd_normalize(simd_quatf(rotation))
        return RecordingPoseSample(positionM: position, eulerDeg: eulerDegrees(from: quaternion))
    }

    static func quaternionQWXYZ(from transform: simd_float4x4) -> [Float] {
        let rotation = simd_float3x3(
            SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            SIMD3<Float>(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
        let quaternion = simd_normalize(simd_quatf(rotation))
        return [
            quaternion.real,
            quaternion.imag.x,
            quaternion.imag.y,
            quaternion.imag.z,
        ]
    }

    static func matrixRows(_ matrix: simd_float4x4) -> [[Float]] {
        (0..<4).map { row in
            (0..<4).map { col in
                matrix[col][row]
            }
        }
    }

    static func matrixRows(_ matrix: simd_float3x3) -> [[Float]] {
        (0..<3).map { row in
            (0..<3).map { col in
                matrix[col][row]
            }
        }
    }

    private static func eulerDegrees(from quaternion: simd_quatf) -> SIMD3<Float> {
        let q = simd_normalize(quaternion)
        let x = q.imag.x
        let y = q.imag.y
        let z = q.imag.z
        let w = q.real

        let sinRoll = 2.0 * (w * x + y * z)
        let cosRoll = 1.0 - 2.0 * (x * x + y * y)
        let roll = atan2(sinRoll, cosRoll)

        let sinPitch = 2.0 * (w * y - z * x)
        let pitch: Float
        if abs(sinPitch) >= 1.0 {
            pitch = (sinPitch >= 0 ? 1 : -1) * .pi / 2.0
        } else {
            pitch = asin(sinPitch)
        }

        let sinYaw = 2.0 * (w * z + x * y)
        let cosYaw = 1.0 - 2.0 * (y * y + z * z)
        let yaw = atan2(sinYaw, cosYaw)

        return SIMD3<Float>(
            roll * 180.0 / .pi,
            pitch * 180.0 / .pi,
            yaw * 180.0 / .pi
        )
    }
}

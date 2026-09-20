import Foundation

struct RecordingLocalFileStatus: Equatable {
    var isOpen = false
    var sessionID: String?
    var relativePath = "--"
    var writtenFrameCount = 0
    var lastError: String?

    static let idle = RecordingLocalFileStatus()

    var displayText: String {
        if let lastError {
            return "failed \(lastError)"
        }
        if isOpen {
            return "writing \(writtenFrameCount)"
        }
        return writtenFrameCount > 0 ? "closed \(writtenFrameCount)" : "idle"
    }
}

struct RecordingJSONLRowV1: Codable {
    var schemaVersion: UInt16
    var rowType: String
    var sessionID: String
    var captureFrameSequence: UInt64
    var sequence: UInt32
    var alignedTimestampNS: Int64
    var localCaptureTimestampNS: Int64
    var timebaseStatus: String
    var tracking: String
    var previewSource: String?

    var poseValid: Bool
    var poseX: Float?
    var poseY: Float?
    var poseZ: Float?
    var rollDeg: Float?
    var pitchDeg: Float?
    var yawDeg: Float?
    var poseQuaternionQWXYZ: [Float]?
    var cameraInUserWorldTransform: [[Float]]?
    var poseFrame: String
    var worldCalibrated: Bool
    var worldOriginStatus: String

    var gripperValid: Bool
    var gripperOpenPercent: Double?
    var gripperTimestampNS: Int64?
    var gripperAgeMS: Double?

    var rgbValid: Bool
    var rgbWidth: Int
    var rgbHeight: Int
    var rgbPixelFormat: String
    var rgbCameraIntrinsics: [[Float]]?
    var rgbCaptureDeviceType: String?
    var rgbEncoding: String?
    var rgbPayloadWidth: Int?
    var rgbPayloadHeight: Int?
    var rgbPayloadBytes: Int?
    var rgbPayloadBase64: String?
    var widePreviewEncoding: String?
    var widePreviewWidth: Int?
    var widePreviewHeight: Int?
    var widePreviewPayloadBytes: Int?
    var widePreviewPayloadBase64: String?

    var depthValid: Bool
    var depthSource: String?
    var depthWidth: Int?
    var depthHeight: Int?
    var depthUnit: String?
    var depthPixelFormat: String?
    var depthCenterM: Double?
    var depthValidRatio: Double?
    var depthConfidence: String?
    var depthEncoding: String?
    var depthPayloadBytes: Int?
    var depthPayloadBase64: String?
    var depthPreviewEncoding: String?
    var depthPreviewWidth: Int?
    var depthPreviewHeight: Int?
    var depthPreviewPayloadBytes: Int?
    var depthPreviewPayloadBase64: String?
    var depthPreviewThumbnailEncoding: String?
    var depthPreviewThumbnailWidth: Int?
    var depthPreviewThumbnailHeight: Int?
    var depthPreviewThumbnailPayloadBytes: Int?
    var depthPreviewThumbnailPayloadBase64: String?
    var ultraPreviewEncoding: String?
    var ultraPreviewWidth: Int?
    var ultraPreviewHeight: Int?
    var ultraPreviewPayloadBytes: Int?
    var ultraPreviewPayloadBase64: String?
    var depthUnavailableReason: String?

    var flags: UInt32
    var recordingActive: Bool
}

struct RecordingEventJSONLRowV1: Codable {
    var schemaVersion: UInt16
    var rowType: String
    var sessionID: String
    var eventCode: RecordingEventCodeV1
    var eventIndex: UInt32
    var eventUnixTimeNS: Int64
    var appUptimeNS: Int64
    var reason: String
    var frameSequence: UInt32?
    var alignedTimestampNS: Int64?
    var localCaptureTimestampNS: Int64?
    var timebaseStatus: String
}

final class RecordingLocalJSONLWriter {
    private var fileHandle: FileHandle?
    private var currentFileURL: URL?
    private var currentSessionID: String?
    private var currentRelativePath = "--"
    private var writtenFrameCount = 0
    private var lastError: String?

    var status: RecordingLocalFileStatus {
        RecordingLocalFileStatus(
            isOpen: fileHandle != nil,
            sessionID: currentSessionID,
            relativePath: currentRelativePath,
            writtenFrameCount: writtenFrameCount,
            lastError: lastError
        )
    }

    func start() -> RecordingLocalFileStatus {
        _ = stop()
        lastError = nil
        writtenFrameCount = 0

        let sessionID = RecordingJSONLCodec.makeSessionID(prefix: "capture")
        let relativePath = "Recordings/\(sessionID).jsonl"

        do {
            let directory = try Self.recordingsDirectoryURL()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let fileURL = directory.appendingPathComponent("\(sessionID).jsonl")
            FileManager.default.createFile(atPath: fileURL.path, contents: nil)
            fileHandle = try FileHandle(forWritingTo: fileURL)
            currentFileURL = fileURL
            currentSessionID = sessionID
            currentRelativePath = relativePath
            writeHeader(sessionID: sessionID)
            print("[UMIFT][recording-file] start \(relativePath)")
        } catch {
            lastError = error.localizedDescription
            currentSessionID = nil
            currentRelativePath = "--"
            fileHandle = nil
            currentFileURL = nil
            print("[UMIFT][recording-file] start failed \(error.localizedDescription)")
        }

        return status
    }

    func append(sample: RecordingAlignedFrameSample, preview: RecordingTransmitPacketPreview) -> RecordingLocalFileStatus {
        guard fileHandle != nil, let sessionID = currentSessionID else { return status }
        let row = RecordingJSONLCodec.row(sessionID: sessionID, sample: sample, preview: preview)
        do {
            try write(row)
            writtenFrameCount += 1
        } catch {
            lastError = error.localizedDescription
            print("[UMIFT][recording-file] append failed \(error.localizedDescription)")
        }
        return status
    }

    func appendEvent(
        code: RecordingEventCodeV1,
        eventIndex: UInt32,
        reason: String,
        frameSequence: UInt32?,
        sample: RecordingAlignedFrameSample?
    ) -> RecordingLocalFileStatus {
        guard fileHandle != nil, let sessionID = currentSessionID else { return status }
        let row = RecordingJSONLCodec.eventRow(
            sessionID: sessionID,
            code: code,
            eventIndex: eventIndex,
            reason: reason,
            frameSequence: frameSequence,
            sample: sample
        )
        do {
            try write(row)
        } catch {
            lastError = error.localizedDescription
            print("[UMIFT][recording-file] event failed \(error.localizedDescription)")
        }
        return status
    }

    func stop() -> RecordingLocalFileStatus {
        guard let fileHandle else { return status }
        do {
            try fileHandle.synchronize()
            try fileHandle.close()
            print("[UMIFT][recording-file] stop \(currentRelativePath) frames=\(writtenFrameCount)")
        } catch {
            lastError = error.localizedDescription
            print("[UMIFT][recording-file] stop failed \(error.localizedDescription)")
        }
        self.fileHandle = nil
        self.currentFileURL = nil
        return status
    }

    func discard() -> RecordingLocalFileStatus {
        let fileURL = currentFileURL
        let relativePath = currentRelativePath
        if let fileHandle {
            do {
                try fileHandle.synchronize()
                try fileHandle.close()
            } catch {
                lastError = error.localizedDescription
                print("[UMIFT][recording-file] discard close failed \(error.localizedDescription)")
            }
        }
        self.fileHandle = nil
        self.currentFileURL = nil
        self.currentSessionID = nil
        self.writtenFrameCount = 0

        guard let fileURL else {
            currentRelativePath = "discarded"
            return status
        }
        do {
            try FileManager.default.removeItem(at: fileURL)
            currentRelativePath = "discarded"
            lastError = nil
            print("[UMIFT][recording-file] discarded \(relativePath)")
        } catch {
            lastError = error.localizedDescription
            print("[UMIFT][recording-file] discard failed \(error.localizedDescription)")
        }
        return status
    }

    private func writeHeader(sessionID: String) {
        do {
            let data = try RecordingJSONLCodec.headerData(sessionID: sessionID)
            try writeLine(data)
        } catch {
            lastError = error.localizedDescription
        }
    }

    private func write(_ row: RecordingJSONLRowV1) throws {
        let data = try RecordingJSONLCodec.encode(row)
        try writeLine(data)
    }

    private func write(_ row: RecordingEventJSONLRowV1) throws {
        let data = try RecordingJSONLCodec.encode(row)
        try writeLine(data)
    }

    private func writeLine(_ data: Data) throws {
        guard let fileHandle else { return }
        fileHandle.write(data)
        fileHandle.write(Data([0x0A]))
    }

    private static func recordingsDirectoryURL() throws -> URL {
        let documents = try FileManager.default.url(
            for: .documentDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        return documents.appendingPathComponent("Recordings", isDirectory: true)
    }

}

enum RecordingJSONLCodec {
    private static let encoder = JSONEncoder()

    static func headerData(sessionID: String) throws -> Data {
        let header: [String: String] = [
            "schema_version": "\(RecordingTransmitPacketV1.schemaVersion)",
            "row_type": "session_start",
            "session_id": sessionID,
            "note": "JSONL stream; TCP may include JPEG RGB payload bytes"
        ]
        return try JSONSerialization.data(withJSONObject: header, options: [.sortedKeys])
    }

    static func makeSessionID(prefix: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        return "\(prefix)_\(formatter.string(from: Date()))_\(UUID().uuidString.prefix(8))"
    }

    static func row(
        sessionID: String,
        sample: RecordingAlignedFrameSample,
        preview: RecordingTransmitPacketPreview,
        rgbPayload: RecordingRGBPayloadV1? = nil,
        widePreviewPayload: RecordingWidePreviewPayloadV1? = nil,
        depthPayload: RecordingDepthPayloadV1? = nil,
        depthPreviewPayload: RecordingDepthPreviewPayloadV1? = nil,
        depthPreviewThumbnailPayload: RecordingDepthPreviewThumbnailPayloadV1? = nil,
        ultraPreviewPayload: RecordingUltraPreviewPayloadV1? = nil
    ) -> RecordingJSONLRowV1 {
        let pose = sample.pose
        let gripperOpenPercent = sample.hasValidGripper ? sample.gripper?.openPercent : nil
        let recordingActive = (preview.flags & RecordingTransmitFlags.recording) != 0
        let depthValid = sample.wide.depthAvailable
            && (sample.wide.depthWidth ?? 0) > 0
            && (sample.wide.depthHeight ?? 0) > 0
        return RecordingJSONLRowV1(
            schemaVersion: RecordingTransmitPacketV1.schemaVersion,
            rowType: "frame",
            sessionID: sessionID,
            captureFrameSequence: sample.wide.frameSequence,
            sequence: preview.sequence,
            alignedTimestampNS: sample.alignedTimestampNS,
            localCaptureTimestampNS: sample.localCaptureTimestampNS,
            timebaseStatus: sample.timebaseStatus.displayText,
            tracking: sample.wide.trackingText,
            previewSource: sample.wide.previewSource,
            poseValid: pose != nil,
            poseX: pose?.positionM.x,
            poseY: pose?.positionM.y,
            poseZ: pose?.positionM.z,
            rollDeg: pose?.eulerDeg.x,
            pitchDeg: pose?.eulerDeg.y,
            yawDeg: pose?.eulerDeg.z,
            poseQuaternionQWXYZ: sample.poseQuaternionQWXYZ,
            cameraInUserWorldTransform: sample.cameraInUserWorldTransformRows,
            poseFrame: sample.wide.poseFrame,
            worldCalibrated: sample.wide.worldCalibrated,
            worldOriginStatus: sample.wide.worldOriginStatus,
            gripperValid: sample.hasValidGripper,
            gripperOpenPercent: gripperOpenPercent,
            gripperTimestampNS: sample.gripper?.timestampNS,
            gripperAgeMS: sample.gripperAgeMS,
            rgbValid: sample.wide.rgbWidth > 0 && sample.wide.rgbHeight > 0,
            rgbWidth: sample.wide.rgbWidth,
            rgbHeight: sample.wide.rgbHeight,
            rgbPixelFormat: pixelFormatText(sample.wide.rgbPixelFormat),
            rgbCameraIntrinsics: sample.rgbCameraIntrinsicsRows,
            rgbCaptureDeviceType: sample.wide.rgbCaptureDeviceType,
            rgbEncoding: rgbPayload?.encoding,
            rgbPayloadWidth: rgbPayload?.width,
            rgbPayloadHeight: rgbPayload?.height,
            rgbPayloadBytes: rgbPayload?.byteCount,
            rgbPayloadBase64: rgbPayload?.base64,
            widePreviewEncoding: widePreviewPayload?.encoding,
            widePreviewWidth: widePreviewPayload?.width,
            widePreviewHeight: widePreviewPayload?.height,
            widePreviewPayloadBytes: widePreviewPayload?.byteCount,
            widePreviewPayloadBase64: widePreviewPayload?.base64,
            depthValid: depthValid,
            depthSource: sample.wide.depthSource,
            depthWidth: sample.wide.depthWidth,
            depthHeight: sample.wide.depthHeight,
            depthUnit: sample.wide.depthUnit,
            depthPixelFormat: sample.wide.depthPixelFormat.map(pixelFormatText),
            depthCenterM: sample.wide.depthCenterM,
            depthValidRatio: sample.wide.depthValidRatio,
            depthConfidence: sample.wide.depthConfidenceText,
            depthEncoding: depthPayload?.encoding,
            depthPayloadBytes: depthPayload?.byteCount,
            depthPayloadBase64: depthPayload?.base64,
            depthPreviewEncoding: depthPreviewPayload?.encoding,
            depthPreviewWidth: depthPreviewPayload?.width,
            depthPreviewHeight: depthPreviewPayload?.height,
            depthPreviewPayloadBytes: depthPreviewPayload?.byteCount,
            depthPreviewPayloadBase64: depthPreviewPayload?.base64,
            depthPreviewThumbnailEncoding: depthPreviewThumbnailPayload?.encoding,
            depthPreviewThumbnailWidth: depthPreviewThumbnailPayload?.width,
            depthPreviewThumbnailHeight: depthPreviewThumbnailPayload?.height,
            depthPreviewThumbnailPayloadBytes: depthPreviewThumbnailPayload?.byteCount,
            depthPreviewThumbnailPayloadBase64: depthPreviewThumbnailPayload?.base64,
            ultraPreviewEncoding: ultraPreviewPayload?.encoding,
            ultraPreviewWidth: ultraPreviewPayload?.width,
            ultraPreviewHeight: ultraPreviewPayload?.height,
            ultraPreviewPayloadBytes: ultraPreviewPayload?.byteCount,
            ultraPreviewPayloadBase64: ultraPreviewPayload?.base64,
            depthUnavailableReason: depthValid ? nil : sample.wide.depthSource,
            flags: preview.flags,
            recordingActive: recordingActive
        )
    }

    static func eventRow(
        sessionID: String,
        code: RecordingEventCodeV1,
        eventIndex: UInt32,
        reason: String,
        frameSequence: UInt32?,
        sample: RecordingAlignedFrameSample?
    ) -> RecordingEventJSONLRowV1 {
        RecordingEventJSONLRowV1(
            schemaVersion: RecordingTransmitPacketV1.schemaVersion,
            rowType: "recording_event",
            sessionID: sessionID,
            eventCode: code,
            eventIndex: eventIndex,
            eventUnixTimeNS: unixTimeNS(),
            appUptimeNS: appUptimeNS(),
            reason: reason,
            frameSequence: frameSequence,
            alignedTimestampNS: sample?.alignedTimestampNS,
            localCaptureTimestampNS: sample?.localCaptureTimestampNS,
            timebaseStatus: sample?.timebaseStatus.displayText ?? RecordingTimebaseStatusV1.unsynchronized.displayText
        )
    }

    static func encode(_ row: RecordingJSONLRowV1) throws -> Data {
        try encoder.encode(row)
    }

    static func encode(_ row: RecordingEventJSONLRowV1) throws -> Data {
        try encoder.encode(row)
    }

    static func lineData(
        sessionID: String,
        sample: RecordingAlignedFrameSample,
        preview: RecordingTransmitPacketPreview,
        rgbPayload: RecordingRGBPayloadV1? = nil,
        widePreviewPayload: RecordingWidePreviewPayloadV1? = nil,
        depthPayload: RecordingDepthPayloadV1? = nil,
        depthPreviewPayload: RecordingDepthPreviewPayloadV1? = nil,
        depthPreviewThumbnailPayload: RecordingDepthPreviewThumbnailPayloadV1? = nil,
        ultraPreviewPayload: RecordingUltraPreviewPayloadV1? = nil
    ) throws -> Data {
        try encode(row(
            sessionID: sessionID,
            sample: sample,
            preview: preview,
            rgbPayload: rgbPayload,
            widePreviewPayload: widePreviewPayload,
            depthPayload: depthPayload,
            depthPreviewPayload: depthPreviewPayload,
            depthPreviewThumbnailPayload: depthPreviewThumbnailPayload,
            ultraPreviewPayload: ultraPreviewPayload
        ))
    }

    static func eventLineData(
        sessionID: String,
        code: RecordingEventCodeV1,
        eventIndex: UInt32,
        reason: String,
        frameSequence: UInt32?,
        sample: RecordingAlignedFrameSample?
    ) throws -> Data {
        try encode(eventRow(
            sessionID: sessionID,
            code: code,
            eventIndex: eventIndex,
            reason: reason,
            frameSequence: frameSequence,
            sample: sample
        ))
    }

    private static func pixelFormatText(_ value: OSType) -> String {
        let chars = [
            Character(UnicodeScalar((value >> 24) & 0xff) ?? " "),
            Character(UnicodeScalar((value >> 16) & 0xff) ?? " "),
            Character(UnicodeScalar((value >> 8) & 0xff) ?? " "),
            Character(UnicodeScalar(value & 0xff) ?? " "),
        ]
        let text = String(chars)
        return text.trimmingCharacters(in: .whitespaces).isEmpty ? "\(value)" : text
    }

    private static func unixTimeNS() -> Int64 {
        Int64(Date().timeIntervalSince1970 * 1_000_000_000.0)
    }

    private static func appUptimeNS() -> Int64 {
        Int64(ProcessInfo.processInfo.systemUptime * 1_000_000_000.0)
    }
}

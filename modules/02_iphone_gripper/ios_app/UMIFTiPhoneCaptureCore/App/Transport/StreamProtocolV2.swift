import Foundation

enum StreamPacketTypeV2: UInt16 {
    case sessionStart = 1
    case videoCodecConfig = 2
    case h264VideoFrame = 3
    case sessionEnd = 4
    case captureError = 5
    case heartbeat = 6
    case frameMetadata = 7
    case depthFrame = 8
    case recordingEvent = 9
    case clockSyncResponse = 10
    case cumulativeAck = 0x8001
    case clockSyncRequest = 0x8002
}

struct StreamPacketFlagsV2: OptionSet {
    let rawValue: UInt32

    static let keyFrame = StreamPacketFlagsV2(rawValue: 1 << 0)
}

struct StreamPacketV2 {
    var type: StreamPacketTypeV2
    var flags: StreamPacketFlagsV2 = []
    var sessionID: String
    var sequence: UInt64
    var captureTimestampNS: Int64
    var payload: Data
}

struct FrameMetadataV2 {
    static let payloadVersion: UInt16 = 1
    static let payloadLength: UInt16 = 148

    var flags: UInt32
    var trackingState: UInt8
    var poseFrame: UInt8
    var depthEncoding: UInt8
    var depthWidth: UInt16
    var depthHeight: UInt16
    var depthPixelFormat: UInt32
    var gripperTimestampNS: Int64
    var gripperAgeNS: Int64
    var gripperOpenPercent: Float
    var depthCenterM: Float
    var depthValidRatio: Float
    var cameraInUserWorldTransform: [Float]
    var rgbCameraIntrinsics: [Float]
}

struct FrameMetadataFlagsV2: OptionSet {
    let rawValue: UInt32

    static let poseValid = FrameMetadataFlagsV2(rawValue: 1 << 0)
    static let gripperValid = FrameMetadataFlagsV2(rawValue: 1 << 1)
    static let depthValid = FrameMetadataFlagsV2(rawValue: 1 << 2)
    static let worldCalibrated = FrameMetadataFlagsV2(rawValue: 1 << 3)
    static let recordingActive = FrameMetadataFlagsV2(rawValue: 1 << 4)
    static let intrinsicsValid = FrameMetadataFlagsV2(rawValue: 1 << 5)
}

struct DepthFrameV2 {
    static let payloadVersion: UInt16 = 1
    static let headerLength: UInt16 = 16
    static let float16LittleEndianEncoding: UInt16 = 1

    var width: UInt16
    var height: UInt16
    var pixelFormat: UInt32
    var samplesLittleEndian: Data
}

struct RecordingEventV2 {
    static let payloadVersion: UInt16 = 1

    var code: UInt16
    var eventIndex: UInt32
    var eventUnixTimeNS: Int64
    var appUptimeNS: Int64
    var reason: String
}

struct ClockSyncRequestV2 {
    static let payloadVersion: UInt16 = 1
    static let payloadLength = 20

    var requestID: UInt64
    var macSendMonotonicNS: Int64
}

enum StreamProtocolErrorV2: Error, CustomStringConvertible {
    case invalidMagic(UInt32)
    case unsupportedVersion(UInt16)
    case invalidHeaderLength(UInt16)
    case unknownPacketType(UInt16)
    case invalidSessionID
    case payloadTooLarge(Int)
    case malformedPayload(String)

    var description: String {
        switch self {
        case .invalidMagic(let magic):
            return String(format: "invalid stream magic 0x%08x", magic)
        case .unsupportedVersion(let version):
            return "unsupported stream version \(version)"
        case .invalidHeaderLength(let length):
            return "invalid stream header length \(length)"
        case .unknownPacketType(let rawValue):
            return "unknown stream packet type \(rawValue)"
        case .invalidSessionID:
            return "invalid UTF-8 stream session ID"
        case .payloadTooLarge(let size):
            return "stream payload too large \(size)"
        case .malformedPayload(let reason):
            return "malformed stream payload: \(reason)"
        }
    }
}

enum StreamProtocolV2 {
    static let magic: UInt32 = 0x554D5632 // UMV2
    static let version: UInt16 = 2
    static let headerLength = 40
    static let maximumPayloadLength = 8 * 1024 * 1024

    static let goldenPacketHex =
        "554d563200020003000000010002002801020304050607081112131415161718000000050000000073310000000165"

    static func encode(_ packet: StreamPacketV2) throws -> Data {
        let sessionData = Data(packet.sessionID.utf8)
        guard sessionData.count <= Int(UInt16.max) else {
            throw StreamProtocolErrorV2.invalidSessionID
        }
        guard packet.payload.count <= maximumPayloadLength else {
            throw StreamProtocolErrorV2.payloadTooLarge(packet.payload.count)
        }

        var data = Data(capacity: headerLength + sessionData.count + packet.payload.count)
        data.appendBigEndian(magic)
        data.appendBigEndian(version)
        data.appendBigEndian(packet.type.rawValue)
        data.appendBigEndian(packet.flags.rawValue)
        data.appendBigEndian(UInt16(sessionData.count))
        data.appendBigEndian(UInt16(headerLength))
        data.appendBigEndian(packet.sequence)
        data.appendBigEndian(UInt64(bitPattern: packet.captureTimestampNS))
        data.appendBigEndian(UInt32(packet.payload.count))
        data.appendBigEndian(UInt32(0))
        data.append(sessionData)
        data.append(packet.payload)
        return data
    }

    static func makeSessionStartPayload(
        width: Int,
        height: Int,
        framesPerSecond: Int,
        averageBitRate: Int
    ) -> Data {
        var data = Data(capacity: 16)
        data.appendBigEndian(UInt16(width))
        data.appendBigEndian(UInt16(height))
        data.appendBigEndian(UInt16(framesPerSecond))
        data.appendBigEndian(UInt16(1)) // H.264
        data.appendBigEndian(UInt32(averageBitRate))
        data.appendBigEndian(UInt32(1_000_000_000))
        return data
    }

    static func makeVideoCodecConfigPayload(
        width: Int,
        height: Int,
        framesPerSecond: Int,
        averageBitRate: Int,
        nalUnitHeaderLength: Int,
        parameterSets: [Data]
    ) throws -> Data {
        guard (1...4).contains(nalUnitHeaderLength) else {
            throw StreamProtocolErrorV2.malformedPayload("invalid NAL length size \(nalUnitHeaderLength)")
        }
        guard parameterSets.count <= Int(UInt8.max) else {
            throw StreamProtocolErrorV2.malformedPayload("too many parameter sets")
        }

        var data = Data()
        data.appendBigEndian(UInt16(width))
        data.appendBigEndian(UInt16(height))
        data.appendBigEndian(UInt16(framesPerSecond))
        data.append(UInt8(nalUnitHeaderLength))
        data.append(UInt8(parameterSets.count))
        data.appendBigEndian(UInt32(averageBitRate))
        data.appendBigEndian(UInt32(0))
        for parameterSet in parameterSets {
            guard parameterSet.count <= Int(UInt16.max) else {
                throw StreamProtocolErrorV2.malformedPayload("parameter set too large")
            }
            data.appendBigEndian(UInt16(parameterSet.count))
            data.append(parameterSet)
        }
        return data
    }

    static func makeFrameMetadataPayload(_ metadata: FrameMetadataV2) throws -> Data {
        guard metadata.cameraInUserWorldTransform.count == 16 else {
            throw StreamProtocolErrorV2.malformedPayload("camera transform must contain 16 floats")
        }
        guard metadata.rgbCameraIntrinsics.count == 9 else {
            throw StreamProtocolErrorV2.malformedPayload("camera intrinsics must contain 9 floats")
        }
        var data = Data(capacity: Int(FrameMetadataV2.payloadLength))
        data.appendBigEndian(FrameMetadataV2.payloadVersion)
        data.appendBigEndian(FrameMetadataV2.payloadLength)
        data.appendBigEndian(metadata.flags)
        data.append(metadata.trackingState)
        data.append(metadata.poseFrame)
        data.append(metadata.depthEncoding)
        data.append(UInt8(0))
        data.appendBigEndian(metadata.depthWidth)
        data.appendBigEndian(metadata.depthHeight)
        data.appendBigEndian(metadata.depthPixelFormat)
        data.appendBigEndian(UInt64(bitPattern: metadata.gripperTimestampNS))
        data.appendBigEndian(UInt64(bitPattern: metadata.gripperAgeNS))
        data.appendBigEndian(metadata.gripperOpenPercent.bitPattern)
        data.appendBigEndian(metadata.depthCenterM.bitPattern)
        data.appendBigEndian(metadata.depthValidRatio.bitPattern)
        for value in metadata.cameraInUserWorldTransform {
            data.appendBigEndian(value.bitPattern)
        }
        for value in metadata.rgbCameraIntrinsics {
            data.appendBigEndian(value.bitPattern)
        }
        guard data.count == Int(FrameMetadataV2.payloadLength) else {
            throw StreamProtocolErrorV2.malformedPayload("frame metadata length mismatch \(data.count)")
        }
        return data
    }

    static func makeDepthFramePayload(_ depth: DepthFrameV2) throws -> Data {
        let expectedBytes = Int(depth.width) * Int(depth.height) * MemoryLayout<UInt16>.stride
        guard depth.samplesLittleEndian.count == expectedBytes else {
            throw StreamProtocolErrorV2.malformedPayload(
                "depth sample bytes mismatch expected=\(expectedBytes) got=\(depth.samplesLittleEndian.count)"
            )
        }
        var data = Data(capacity: Int(DepthFrameV2.headerLength) + expectedBytes)
        data.appendBigEndian(DepthFrameV2.payloadVersion)
        data.appendBigEndian(DepthFrameV2.headerLength)
        data.appendBigEndian(depth.width)
        data.appendBigEndian(depth.height)
        data.appendBigEndian(depth.pixelFormat)
        data.appendBigEndian(DepthFrameV2.float16LittleEndianEncoding)
        data.appendBigEndian(UInt16(0))
        data.append(depth.samplesLittleEndian)
        return data
    }

    static func makeRecordingEventPayload(_ event: RecordingEventV2) throws -> Data {
        let reason = Data(event.reason.utf8)
        guard reason.count <= Int(UInt16.max) else {
            throw StreamProtocolErrorV2.malformedPayload("recording event reason is too long")
        }
        var data = Data(capacity: 26 + reason.count)
        data.appendBigEndian(RecordingEventV2.payloadVersion)
        data.appendBigEndian(event.code)
        data.appendBigEndian(event.eventIndex)
        data.appendBigEndian(UInt64(bitPattern: event.eventUnixTimeNS))
        data.appendBigEndian(UInt64(bitPattern: event.appUptimeNS))
        data.appendBigEndian(UInt16(reason.count))
        data.append(reason)
        return data
    }

    static func parseClockSyncRequestPayload(_ payload: Data) throws -> ClockSyncRequestV2 {
        guard payload.count == ClockSyncRequestV2.payloadLength else {
            throw StreamProtocolErrorV2.malformedPayload(
                "clock sync request length mismatch \(payload.count)"
            )
        }
        let version = payload.readUInt16BigEndian(at: 0)
        guard version == ClockSyncRequestV2.payloadVersion else {
            throw StreamProtocolErrorV2.malformedPayload(
                "unsupported clock sync request version \(version)"
            )
        }
        return ClockSyncRequestV2(
            requestID: payload.readUInt64BigEndian(at: 4),
            macSendMonotonicNS: Int64(bitPattern: payload.readUInt64BigEndian(at: 12))
        )
    }

    static func makeClockSyncResponsePayload(
        requestID: UInt64,
        macSendMonotonicNS: Int64,
        phoneReceiveUptimeNS: Int64,
        phoneSendUptimeNS: Int64
    ) -> Data {
        var data = Data(capacity: 36)
        data.appendBigEndian(UInt16(1))
        data.appendBigEndian(UInt16(0))
        data.appendBigEndian(requestID)
        data.appendBigEndian(UInt64(bitPattern: macSendMonotonicNS))
        data.appendBigEndian(UInt64(bitPattern: phoneReceiveUptimeNS))
        data.appendBigEndian(UInt64(bitPattern: phoneSendUptimeNS))
        return data
    }

    static func validateGoldenVector() throws {
        let packet = StreamPacketV2(
            type: .h264VideoFrame,
            flags: [.keyFrame],
            sessionID: "s1",
            sequence: 0x0102030405060708,
            captureTimestampNS: Int64(bitPattern: 0x1112131415161718),
            payload: Data([0, 0, 0, 1, 0x65])
        )
        let encoded = try encode(packet)
        guard encoded.hexString == goldenPacketHex else {
            throw StreamProtocolErrorV2.malformedPayload(
                "golden vector mismatch got=\(encoded.hexString)"
            )
        }
    }
}

final class StreamPacketParserV2 {
    private var buffer = Data()

    func append(_ data: Data) throws -> [StreamPacketV2] {
        buffer.append(data)
        var packets: [StreamPacketV2] = []
        while buffer.count >= StreamProtocolV2.headerLength {
            let magic = buffer.readUInt32BigEndian(at: 0)
            guard magic == StreamProtocolV2.magic else {
                throw StreamProtocolErrorV2.invalidMagic(magic)
            }
            let version = buffer.readUInt16BigEndian(at: 4)
            guard version == StreamProtocolV2.version else {
                throw StreamProtocolErrorV2.unsupportedVersion(version)
            }
            let typeRawValue = buffer.readUInt16BigEndian(at: 6)
            guard let type = StreamPacketTypeV2(rawValue: typeRawValue) else {
                throw StreamProtocolErrorV2.unknownPacketType(typeRawValue)
            }
            let flags = buffer.readUInt32BigEndian(at: 8)
            let sessionIDLength = Int(buffer.readUInt16BigEndian(at: 12))
            let headerLength = buffer.readUInt16BigEndian(at: 14)
            guard headerLength == UInt16(StreamProtocolV2.headerLength) else {
                throw StreamProtocolErrorV2.invalidHeaderLength(headerLength)
            }
            let sequence = buffer.readUInt64BigEndian(at: 16)
            let timestampBits = buffer.readUInt64BigEndian(at: 24)
            let payloadLength = Int(buffer.readUInt32BigEndian(at: 32))
            guard payloadLength <= StreamProtocolV2.maximumPayloadLength else {
                throw StreamProtocolErrorV2.payloadTooLarge(payloadLength)
            }
            let totalLength = Int(headerLength) + sessionIDLength + payloadLength
            guard buffer.count >= totalLength else { break }

            let sessionStart = Int(headerLength)
            let sessionEnd = sessionStart + sessionIDLength
            guard let sessionID = String(data: buffer[sessionStart..<sessionEnd], encoding: .utf8) else {
                throw StreamProtocolErrorV2.invalidSessionID
            }
            let payload = Data(buffer[sessionEnd..<totalLength])
            buffer.removeSubrange(0..<totalLength)
            packets.append(StreamPacketV2(
                type: type,
                flags: StreamPacketFlagsV2(rawValue: flags),
                sessionID: sessionID,
                sequence: sequence,
                captureTimestampNS: Int64(bitPattern: timestampBits),
                payload: payload
            ))
        }
        return packets
    }
}

private extension Data {
    mutating func appendBigEndian<T: FixedWidthInteger>(_ value: T) {
        var bigEndianValue = value.bigEndian
        Swift.withUnsafeBytes(of: &bigEndianValue) { bytes in
            append(contentsOf: bytes)
        }
    }

    var hexString: String {
        map { String(format: "%02x", $0) }.joined()
    }

    func readUInt16BigEndian(at offset: Int) -> UInt16 {
        (UInt16(self[offset]) << 8) | UInt16(self[offset + 1])
    }

    func readUInt32BigEndian(at offset: Int) -> UInt32 {
        (UInt32(self[offset]) << 24)
            | (UInt32(self[offset + 1]) << 16)
            | (UInt32(self[offset + 2]) << 8)
            | UInt32(self[offset + 3])
    }

    func readUInt64BigEndian(at offset: Int) -> UInt64 {
        var value: UInt64 = 0
        for index in 0..<8 {
            value = (value << 8) | UInt64(self[offset + index])
        }
        return value
    }
}

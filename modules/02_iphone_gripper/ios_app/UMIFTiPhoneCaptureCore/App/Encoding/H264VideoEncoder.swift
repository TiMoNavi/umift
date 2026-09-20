import CoreMedia
import CoreVideo
import Foundation
import VideoToolbox

struct H264VideoEncoderConfiguration {
    var width = 1920
    var height = 1440
    var framesPerSecond = 30
    var averageBitRate = 14_000_000
    var keyFrameInterval = 30
    // Covers short VideoToolbox scheduling stalls without allowing an unbounded
    // queue of retained ARKit CVPixelBuffers (12 frames is about 400 ms at 30 Hz).
    var maxInFlightFrames = 12

    static let arkitMain = H264VideoEncoderConfiguration()
}

struct H264EncodedAccessUnit {
    var sequence: UInt64
    var captureTimestampNS: Int64
    var isKeyFrame: Bool
    var data: Data
    var parameterSets: [Data]
    var nalUnitHeaderLength: Int
    var encodeDurationMS: Double
}

struct H264VideoEncoderSnapshot {
    var submittedFrames: UInt64
    var encodedFrames: UInt64
    var rejectedFrames: UInt64
    var droppedFrames: UInt64
    var inFlightFrames: Int
    var maxObservedInFlightFrames: Int
    var encodedBytes: UInt64
    var effectiveFPS: Double
    var averageBitRateMbps: Double
    var encodeP50MS: Double
    var encodeP95MS: Double

    static let idle = H264VideoEncoderSnapshot(
        submittedFrames: 0,
        encodedFrames: 0,
        rejectedFrames: 0,
        droppedFrames: 0,
        inFlightFrames: 0,
        maxObservedInFlightFrames: 0,
        encodedBytes: 0,
        effectiveFPS: 0,
        averageBitRateMbps: 0,
        encodeP50MS: 0,
        encodeP95MS: 0
    )

    var displayText: String {
        let thermalText: String
        switch ProcessInfo.processInfo.thermalState {
        case .nominal:
            thermalText = "nominal"
        case .fair:
            thermalText = "fair"
        case .serious:
            thermalText = "serious"
        case .critical:
            thermalText = "critical"
        @unknown default:
            thermalText = "unknown"
        }
        return String(
            format: "h264 %.1ffps %.1fMbps enc %.1f/%.1fms in-flight %d max %d",
            effectiveFPS,
            averageBitRateMbps,
            encodeP50MS,
            encodeP95MS,
            inFlightFrames,
            maxObservedInFlightFrames
        )
        + " reject \(rejectedFrames) drop \(droppedFrames) thermal \(thermalText)"
    }
}

private final class H264FrameContext {
    let sequence: UInt64
    let captureTimestampNS: Int64
    let submittedUptimeNS: UInt64

    init(sequence: UInt64, captureTimestampNS: Int64, submittedUptimeNS: UInt64) {
        self.sequence = sequence
        self.captureTimestampNS = captureTimestampNS
        self.submittedUptimeNS = submittedUptimeNS
    }
}

private let h264CompressionOutputCallback: VTCompressionOutputCallback = {
    outputCallbackRefCon,
    sourceFrameRefCon,
    status,
    infoFlags,
    sampleBuffer
in
    guard let outputCallbackRefCon, let sourceFrameRefCon else { return }
    let encoder = Unmanaged<H264VideoEncoder>.fromOpaque(outputCallbackRefCon).takeUnretainedValue()
    let context = Unmanaged<H264FrameContext>.fromOpaque(sourceFrameRefCon).takeRetainedValue()
    encoder.handleCompressionOutput(
        context: context,
        status: status,
        infoFlags: infoFlags,
        sampleBuffer: sampleBuffer
    )
}

final class H264VideoEncoder {
    var onAccessUnit: ((H264EncodedAccessUnit) -> Void)?
    var onSnapshot: ((H264VideoEncoderSnapshot) -> Void)?
    var onIntegrityFailure: ((String) -> Void)?

    let configuration: H264VideoEncoderConfiguration

    private let encoderQueue = DispatchQueue(label: "local.umift.capturecore.h264-encoder")
    private let metricsLock = NSLock()
    private var compressionSession: VTCompressionSession?
    private var submittedFrames: UInt64 = 0
    private var encodedFrames: UInt64 = 0
    private var rejectedFrames: UInt64 = 0
    private var droppedFrames: UInt64 = 0
    private var inFlightFrames = 0
    private var maxObservedInFlightFrames = 0
    private var encodedBytes: UInt64 = 0
    private var firstSubmitUptimeNS: UInt64?
    private var encodeDurationsMS: [Double] = []

    init(configuration: H264VideoEncoderConfiguration) {
        self.configuration = configuration
    }

    func encode(pixelBuffer: CVPixelBuffer, sequence: UInt64, captureTimestampNS: Int64) {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer)
        guard width == configuration.width, height == configuration.height else {
            reportRejected(
                "h264 source format mismatch seq=\(sequence) got=\(width)x\(height) expected=\(configuration.width)x\(configuration.height)"
            )
            return
        }
        guard pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
                || pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange else {
            reportRejected("h264 source pixel format mismatch seq=\(sequence) format=\(pixelFormat)")
            return
        }

        let submittedUptimeNS = DispatchTime.now().uptimeNanoseconds
        metricsLock.lock()
        guard inFlightFrames < configuration.maxInFlightFrames else {
            rejectedFrames &+= 1
            let inFlight = inFlightFrames
            let snapshot = makeSnapshot(nowUptimeNS: submittedUptimeNS)
            metricsLock.unlock()
            onSnapshot?(snapshot)
            onIntegrityFailure?(
                "h264 encoder admission overflow seq=\(sequence) inFlight=\(inFlight) limit=\(configuration.maxInFlightFrames)"
            )
            return
        }
        submittedFrames &+= 1
        inFlightFrames += 1
        maxObservedInFlightFrames = max(maxObservedInFlightFrames, inFlightFrames)
        if firstSubmitUptimeNS == nil {
            firstSubmitUptimeNS = submittedUptimeNS
        }
        metricsLock.unlock()

        encoderQueue.async { [weak self] in
            guard let self else { return }
            autoreleasepool {
                self.submit(
                    pixelBuffer: pixelBuffer,
                    sequence: sequence,
                    captureTimestampNS: captureTimestampNS,
                    submittedUptimeNS: submittedUptimeNS
                )
            }
        }
    }

    func invalidate() {
        encoderQueue.sync {
            guard let compressionSession else { return }
            VTCompressionSessionCompleteFrames(compressionSession, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(compressionSession)
            self.compressionSession = nil
        }
    }

    private func submit(
        pixelBuffer: CVPixelBuffer,
        sequence: UInt64,
        captureTimestampNS: Int64,
        submittedUptimeNS: UInt64
    ) {
        guard let session = compressionSession ?? makeCompressionSession() else {
            finishFailedFrame("h264 session unavailable seq=\(sequence)")
            return
        }

        let context = H264FrameContext(
            sequence: sequence,
            captureTimestampNS: captureTimestampNS,
            submittedUptimeNS: submittedUptimeNS
        )
        let contextPointer = Unmanaged.passRetained(context).toOpaque()
        let presentationTimeStamp = CMTime(value: captureTimestampNS, timescale: 1_000_000_000)
        let duration = CMTime(value: 1, timescale: CMTimeScale(configuration.framesPerSecond))
        var infoFlags = VTEncodeInfoFlags()
        let status = VTCompressionSessionEncodeFrame(
            session,
            imageBuffer: pixelBuffer,
            presentationTimeStamp: presentationTimeStamp,
            duration: duration,
            frameProperties: nil,
            sourceFrameRefcon: contextPointer,
            infoFlagsOut: &infoFlags
        )
        guard status == noErr else {
            Unmanaged<H264FrameContext>.fromOpaque(contextPointer).release()
            finishFailedFrame("h264 submit failed seq=\(sequence) status=\(status)")
            return
        }
        if infoFlags.contains(.frameDropped) {
            Unmanaged<H264FrameContext>.fromOpaque(contextPointer).release()
            reportDropped("h264 submit reported dropped seq=\(sequence)")
            finishInFlightFrame()
        }
    }

    private func makeCompressionSession() -> VTCompressionSession? {
        var session: VTCompressionSession?
        let encoderSpecification: CFDictionary?
        if #available(iOS 17.4, *) {
            encoderSpecification = [
                kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: kCFBooleanTrue,
            ] as CFDictionary
        } else {
            encoderSpecification = nil
        }
        let status = VTCompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            width: Int32(configuration.width),
            height: Int32(configuration.height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: encoderSpecification,
            imageBufferAttributes: nil,
            compressedDataAllocator: nil,
            outputCallback: h264CompressionOutputCallback,
            refcon: Unmanaged.passUnretained(self).toOpaque(),
            compressionSessionOut: &session
        )
        guard status == noErr, let session else {
            onIntegrityFailure?("h264 session create failed status=\(status)")
            return nil
        }

        let requiredProperties: [(CFString, CFTypeRef)] = [
            (kVTCompressionPropertyKey_RealTime, kCFBooleanTrue),
            (kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse),
            (kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel),
            (kVTCompressionPropertyKey_AverageBitRate, NSNumber(value: configuration.averageBitRate)),
            (kVTCompressionPropertyKey_ExpectedFrameRate, NSNumber(value: configuration.framesPerSecond)),
            (kVTCompressionPropertyKey_MaxKeyFrameInterval, NSNumber(value: configuration.keyFrameInterval)),
        ]
        for (key, value) in requiredProperties {
            let propertyStatus = VTSessionSetProperty(session, key: key, value: value)
            guard propertyStatus == noErr else {
                VTCompressionSessionInvalidate(session)
                onIntegrityFailure?("h264 property failed key=\(key) status=\(propertyStatus)")
                return nil
            }
        }

        // RealTime plus disabled frame reordering defines the required low-latency behavior.
        // MaxFrameDelayCount is not supported by every iPhone encoder implementation.
        let delayStatus = VTSessionSetProperty(
            session,
            key: kVTCompressionPropertyKey_MaxFrameDelayCount,
            value: NSNumber(value: 1)
        )
        if delayStatus != noErr {
            print("[UMIFT][h264] MaxFrameDelayCount unsupported status=\(delayStatus); continuing")
        }

        let prepareStatus = VTCompressionSessionPrepareToEncodeFrames(session)
        guard prepareStatus == noErr else {
            VTCompressionSessionInvalidate(session)
            onIntegrityFailure?("h264 prepare failed status=\(prepareStatus)")
            return nil
        }

        var hardwareText = "hardware=system-default"
        if #available(iOS 17.4, *) {
            var hardwareValue: CFTypeRef?
            let hardwareStatus = withUnsafeMutablePointer(to: &hardwareValue) { valuePointer in
                VTSessionCopyProperty(
                    session,
                    key: kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder,
                    allocator: kCFAllocatorDefault,
                    valueOut: UnsafeMutableRawPointer(valuePointer)
                )
            }
            let isHardware = hardwareStatus == noErr && (hardwareValue as? Bool == true)
            guard isHardware else {
                VTCompressionSessionInvalidate(session)
                onIntegrityFailure?("h264 hardware encoder unavailable status=\(hardwareStatus)")
                return nil
            }
            hardwareText = "hardware=true"
        }
        compressionSession = session
        print(
            "[UMIFT][h264] session ready \(configuration.width)x\(configuration.height) "
            + "@\(configuration.framesPerSecond) bitrate=\(configuration.averageBitRate) \(hardwareText)"
        )
        return session
    }

    fileprivate func handleCompressionOutput(
        context: H264FrameContext,
        status: OSStatus,
        infoFlags: VTEncodeInfoFlags,
        sampleBuffer: CMSampleBuffer?
    ) {
        guard status == noErr else {
            finishFailedFrame("h264 callback failed seq=\(context.sequence) status=\(status)")
            return
        }
        guard !infoFlags.contains(.frameDropped), let sampleBuffer, CMSampleBufferDataIsReady(sampleBuffer) else {
            reportDropped("h264 callback dropped seq=\(context.sequence)")
            finishInFlightFrame()
            return
        }
        guard let data = Self.encodedData(from: sampleBuffer) else {
            finishFailedFrame("h264 block buffer unavailable seq=\(context.sequence)")
            return
        }

        let isKeyFrame = Self.isKeyFrame(sampleBuffer)
        let codecConfiguration = isKeyFrame
            ? Self.codecConfiguration(from: sampleBuffer)
            : (parameterSets: [], nalUnitHeaderLength: 4)
        let nowUptimeNS = DispatchTime.now().uptimeNanoseconds
        let encodeDurationMS = Double(nowUptimeNS - context.submittedUptimeNS) / 1e6
        let accessUnit = H264EncodedAccessUnit(
            sequence: context.sequence,
            captureTimestampNS: context.captureTimestampNS,
            isKeyFrame: isKeyFrame,
            data: data,
            parameterSets: codecConfiguration.parameterSets,
            nalUnitHeaderLength: codecConfiguration.nalUnitHeaderLength,
            encodeDurationMS: encodeDurationMS
        )

        metricsLock.lock()
        encodedFrames &+= 1
        encodedBytes &+= UInt64(data.count)
        inFlightFrames = max(0, inFlightFrames - 1)
        encodeDurationsMS.append(encodeDurationMS)
        if encodeDurationsMS.count > 120 {
            encodeDurationsMS.removeFirst(encodeDurationsMS.count - 120)
        }
        let shouldPublish = encodedFrames % 30 == 0
        let snapshot = shouldPublish ? makeSnapshot(nowUptimeNS: nowUptimeNS) : nil
        metricsLock.unlock()

        onAccessUnit?(accessUnit)
        if let snapshot {
            print("[UMIFT][h264-perf] \(snapshot.displayText)")
            onSnapshot?(snapshot)
        }
    }

    private func reportRejected(_ message: String) {
        metricsLock.lock()
        rejectedFrames &+= 1
        let snapshot = makeSnapshot(nowUptimeNS: DispatchTime.now().uptimeNanoseconds)
        metricsLock.unlock()
        onSnapshot?(snapshot)
        onIntegrityFailure?(message)
    }

    private func reportDropped(_ message: String) {
        metricsLock.lock()
        droppedFrames &+= 1
        metricsLock.unlock()
        onIntegrityFailure?(message)
    }

    private func finishFailedFrame(_ message: String) {
        finishInFlightFrame()
        onIntegrityFailure?(message)
    }

    private func finishInFlightFrame() {
        metricsLock.lock()
        inFlightFrames = max(0, inFlightFrames - 1)
        let snapshot = makeSnapshot(nowUptimeNS: DispatchTime.now().uptimeNanoseconds)
        metricsLock.unlock()
        onSnapshot?(snapshot)
    }

    private func makeSnapshot(nowUptimeNS: UInt64) -> H264VideoEncoderSnapshot {
        let elapsedS: Double
        if let firstSubmitUptimeNS, nowUptimeNS > firstSubmitUptimeNS {
            elapsedS = Double(nowUptimeNS - firstSubmitUptimeNS) / 1e9
        } else {
            elapsedS = 0
        }
        let ordered = encodeDurationsMS.sorted()
        let p50 = ordered.isEmpty ? 0 : ordered[ordered.count / 2]
        let p95 = ordered.isEmpty ? 0 : ordered[Int(Double(ordered.count - 1) * 0.95)]
        return H264VideoEncoderSnapshot(
            submittedFrames: submittedFrames,
            encodedFrames: encodedFrames,
            rejectedFrames: rejectedFrames,
            droppedFrames: droppedFrames,
            inFlightFrames: inFlightFrames,
            maxObservedInFlightFrames: maxObservedInFlightFrames,
            encodedBytes: encodedBytes,
            effectiveFPS: elapsedS > 0 ? Double(encodedFrames) / elapsedS : 0,
            averageBitRateMbps: elapsedS > 0 ? Double(encodedBytes) * 8.0 / elapsedS / 1e6 : 0,
            encodeP50MS: p50,
            encodeP95MS: p95
        )
    }

    private static func encodedData(from sampleBuffer: CMSampleBuffer) -> Data? {
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return nil }
        let length = CMBlockBufferGetDataLength(blockBuffer)
        guard length > 0 else { return nil }
        var data = Data(count: length)
        let status = data.withUnsafeMutableBytes { rawBuffer -> OSStatus in
            guard let baseAddress = rawBuffer.baseAddress else { return kCMBlockBufferBadPointerParameterErr }
            return CMBlockBufferCopyDataBytes(
                blockBuffer,
                atOffset: 0,
                dataLength: length,
                destination: baseAddress
            )
        }
        return status == noErr ? data : nil
    }

    private static func isKeyFrame(_ sampleBuffer: CMSampleBuffer) -> Bool {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(
            sampleBuffer,
            createIfNecessary: false
        ) as? [[CFString: Any]], let first = attachments.first else {
            return false
        }
        return !(first[kCMSampleAttachmentKey_NotSync] as? Bool ?? false)
    }

    private static func codecConfiguration(
        from sampleBuffer: CMSampleBuffer
    ) -> (parameterSets: [Data], nalUnitHeaderLength: Int) {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            return ([], 4)
        }
        var parameterSetCount = 0
        var nalUnitHeaderLength: Int32 = 0
        let countStatus = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            formatDescription,
            parameterSetIndex: 0,
            parameterSetPointerOut: nil,
            parameterSetSizeOut: nil,
            parameterSetCountOut: &parameterSetCount,
            nalUnitHeaderLengthOut: &nalUnitHeaderLength
        )
        guard countStatus == noErr, parameterSetCount > 0 else { return ([], 4) }

        var result: [Data] = []
        for index in 0..<parameterSetCount {
            var pointer: UnsafePointer<UInt8>?
            var size = 0
            let status = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                formatDescription,
                parameterSetIndex: index,
                parameterSetPointerOut: &pointer,
                parameterSetSizeOut: &size,
                parameterSetCountOut: nil,
                nalUnitHeaderLengthOut: nil
            )
            if status == noErr, let pointer, size > 0 {
                result.append(Data(bytes: pointer, count: size))
            }
        }
        return (result, Int(nalUnitHeaderLength))
    }
}

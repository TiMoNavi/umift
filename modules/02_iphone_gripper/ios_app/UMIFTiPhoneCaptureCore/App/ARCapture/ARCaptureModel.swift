import AVFoundation
import Foundation
import UIKit

struct PrivateUltraWideFrameState {
    var pixelBuffer: CVPixelBuffer
    var timestampNS: Int64
    var frameText: String
    var cameraText: String
    var pixelFormatText: String
    var previewImage: UIImage?
}

private enum DepthPreviewRenderer {
    static func makeImage(from depthMap: CVPixelBuffer) -> UIImage? {
        guard let cgImage = makeCGImage(from: depthMap) else { return nil }
        return UIImage(cgImage: cgImage)
    }

    private static func makeCGImage(
        from depthMap: CVPixelBuffer,
        targetWidth: Int? = nil,
        targetHeight: Int? = nil
    ) -> CGImage? {
        let width = CVPixelBufferGetWidth(depthMap)
        let height = CVPixelBufferGetHeight(depthMap)
        guard width > 0, height > 0 else { return nil }
        let outputWidth = targetWidth ?? width
        let outputHeight = targetHeight ?? height
        guard outputWidth > 0, outputHeight > 0 else { return nil }

        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }

        guard let baseAddress = CVPixelBufferGetBaseAddress(depthMap) else {
            return nil
        }

        let values = baseAddress.assumingMemoryBound(to: Float32.self)
        let stride = CVPixelBufferGetBytesPerRow(depthMap) / MemoryLayout<Float32>.stride
        guard stride >= width else { return nil }

        var rgba = [UInt8](repeating: 0, count: outputWidth * outputHeight * 4)
        for y in 0..<outputHeight {
            let sourceY = min(height - 1, y * height / outputHeight)
            for x in 0..<outputWidth {
                let sourceX = min(width - 1, x * width / outputWidth)
                let depthM = values[sourceY * stride + sourceX]
                let color = pseudoColor(depthM: depthM)
                let offset = (y * outputWidth + x) * 4
                rgba[offset] = color.r
                rgba[offset + 1] = color.g
                rgba[offset + 2] = color.b
                rgba[offset + 3] = 255
            }
        }

        let data = Data(rgba)
        guard let provider = CGDataProvider(data: data as CFData) else { return nil }
        let bitmapInfo = CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue)
        return CGImage(
            width: outputWidth,
            height: outputHeight,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: outputWidth * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: bitmapInfo,
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
        )
    }

    private static func pseudoColor(depthM: Float32) -> (r: UInt8, g: UInt8, b: UInt8) {
        guard depthM.isFinite, depthM > 0.05, depthM < 8.0 else {
            return (0, 0, 0)
        }
        let t = clamp01((depthM - 0.15) / 3.35)
        let r = clamp01(1.5 - abs(4.0 * t - 3.0))
        let g = clamp01(1.5 - abs(4.0 * t - 2.0))
        let b = clamp01(1.5 - abs(4.0 * t - 1.0))
        return (
            UInt8(clamping: Int((r * 255.0).rounded())),
            UInt8(clamping: Int((g * 255.0).rounded())),
            UInt8(clamping: Int((b * 255.0).rounded()))
        )
    }

    private static func clamp01(_ value: Float32) -> Float32 {
        min(1.0, max(0.0, value))
    }
}

@MainActor
final class ARCaptureModel: ObservableObject {
    @Published var wideStatus = "arkit starting"
    @Published var ultraStatus = "private uw waiting"
    @Published var torchStatus = "off"
    @Published var receiverStatus = "disconnected"
    @Published var wideFrameText = "ar waiting"
    @Published var ultraFrameText = "uw waiting"
    @Published var wideTimestampNS: Int64?
    @Published var ultraTimestampNS: Int64?
    @Published var syncErrorMS: Double?
    @Published var syncValid = false
    @Published var bufferDurationS: Double = 0
    @Published var torchRequested = true
    @Published var capabilityText = "arkit private-ultrawide"
    @Published var gripperVisionState = GripperVisionState.empty
    @Published var ultraWidePreviewImage: UIImage?
    @Published var depthPreviewImage: UIImage?
    @Published var recordingRuntime = RecordingRuntimeState()

    let h264VideoEncoder = H264VideoEncoder(configuration: .arkitMain)

    private let processingQueue = DispatchQueue(label: "local.umift.dual-capture.ar-processing")
    private let torchQueue = DispatchQueue(label: "local.umift.dual-capture.ar-torch")
    private let archiveTransport = ArchiveTransport(configuration: .arkitMain)
    private let gripperVisionProcessor = GripperVisionProcessor()
    private var recordingBuffer = RecordingFrameRingBuffer()
    private var recordingPacketBuilder = RecordingPacketBuilder()
    private let localRecordingWriter = RecordingLocalJSONLWriter()
    private var recordingBufferEnabled = false
    private var activeStreamSessionID: String?
    private var activeRecordingFileSessionID: String?
    private var nextRecordingEventIndex: UInt32 = 0
    private var pendingArchiveEvents: [RecordingEventV2] = []
    private var ultraFrameCount = 0
    private var lastUltraTimestampNS: Int64?
    private var lastDepthPreviewTimestampNS: Int64 = 0
    private var lastGripperDebugLogNS: Int64 = 0
    private var torchRequestedOn = true
    private var torchKeepAliveTimer: DispatchSourceTimer?

    init() {
        let archiveTransport = archiveTransport
        h264VideoEncoder.onAccessUnit = { accessUnit in
            archiveTransport.enqueue(accessUnit)
        }
        h264VideoEncoder.onSnapshot = { [weak self] snapshot in
            DispatchQueue.main.async { [weak self] in
                self?.recordingRuntime.h264EncoderText = snapshot.displayText
            }
        }
        h264VideoEncoder.onIntegrityFailure = { [weak self] reason in
            print("[UMIFT][capture-integrity] \(reason)")
            DispatchQueue.main.async { [weak self] in
                self?.recordingRuntime.captureIntegrityText = reason
            }
        }
        archiveTransport.onStatusChange = { [weak self] status in
            guard let self else { return }
            self.recordingRuntime.transportStatus = status
            self.receiverStatus = status.displayText
        }
        archiveTransport.onSnapshot = { [weak self] snapshot in
            self?.recordingRuntime.h264SpoolText = snapshot.displayText
        }
        archiveTransport.onIntegrityFailure = { [weak self] reason in
            self?.recordingRuntime.captureIntegrityText = reason
        }
        archiveTransport.start()
    }

    deinit {
        h264VideoEncoder.invalidate()
        archiveTransport.stop()
        torchQueue.sync {
            stopTorchKeepAlive()
            _ = Self.applyTorch(enabled: false, force: true)
        }
    }

    func applyARStatus(_ status: WorldCalibrationARStatus) {
        wideStatus = status.statusText
    }

    func applyARWideFrame(_ state: WorldTrackingARFrameState, previewSource: PreviewSource = .wide) {
        wideTimestampNS = state.timestampNS
        wideFrameText = state.rgbText
        wideStatus = "arkit \(state.trackingText)"
        capabilityText = "arkit world + private uw"
        updateDepthPreviewIfNeeded(state, isPreviewVisible: previewSource == .depth)
        ingestRecordingWideFrame(state, previewSource: previewSource)
        updateSyncState()
    }

    private func updateDepthPreviewIfNeeded(_ state: WorldTrackingARFrameState, isPreviewVisible: Bool) {
        guard let depthMap = state.depthMapPixelBuffer, state.depthAvailable else {
            depthPreviewImage = nil
            return
        }
        guard isPreviewVisible else { return }
        guard state.timestampNS - lastDepthPreviewTimestampNS >= 100_000_000 else { return }
        guard let image = DepthPreviewRenderer.makeImage(from: depthMap) else { return }
        lastDepthPreviewTimestampNS = state.timestampNS
        depthPreviewImage = image
    }

    func consumePrivateUltraWideFrame(_ frame: PrivateUltraWideFrameState) {
        guard frame.timestampNS != lastUltraTimestampNS else { return }
        lastUltraTimestampNS = frame.timestampNS
        ultraTimestampNS = frame.timestampNS
        ultraFrameCount += 1
        ultraFrameText = "\(frame.frameText) #\(ultraFrameCount)"
        ultraStatus = "private uw \(frame.pixelFormatText) \(frame.cameraText)"
        if let previewImage = frame.previewImage {
            ultraWidePreviewImage = previewImage
        }
        updateSyncState()

        let pixelBuffer = frame.pixelBuffer
        let timestampNS = frame.timestampNS
        let intrinsicsText = frame.cameraText.isEmpty ? nil : "K \(frame.cameraText)"
        processingQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.process(
                pixelBuffer: pixelBuffer,
                timestampNS: timestampNS,
                intrinsicsText: intrinsicsText
            )
            DispatchQueue.main.async {
                self.gripperVisionState = nextState
                self.ingestRecordingGripperState(nextState, timestampNS: timestampNS)
                self.logGripperDebugIfNeeded(state: nextState, timestampNS: timestampNS)
            }
        }
    }

    func setRecordingActive(_ active: Bool) {
        guard recordingRuntime.isRecording != active else { return }
        if active {
            startStreamSessionIfNeeded()
        }
        recordingRuntime.isRecording = active
        if active {
            nextRecordingEventIndex = 0
            recordingRuntime.localFileStatus = localRecordingWriter.start()
            activeRecordingFileSessionID = recordingRuntime.localFileStatus.sessionID
            emitRecordingEvent(.recordStart, reason: "phase_recording")
        } else {
            emitRecordingEvent(.recordStop, reason: "phase_exit_recording")
            recordingRuntime.localFileStatus = localRecordingWriter.stop()
            activeRecordingFileSessionID = nil
        }
        publishRecordingBufferState()
    }

    func requestCoinFTRestart() {
        guard !recordingRuntime.isRecording else { return }
        emitRecordingEvent(.coinFTRestartRequested, reason: "record_countdown_requested")
    }

    func discardRecording() {
        guard recordingRuntime.isRecording else { return }
        emitRecordingEvent(.recordDiscard, reason: "operator_discard")
        recordingRuntime.isRecording = false
        recordingRuntime.localFileStatus = localRecordingWriter.discard()
        activeRecordingFileSessionID = nil
        print("[UMIFT][recording] discard requested")
    }

    func setRecordingBufferEnabled(_ enabled: Bool) {
        guard recordingBufferEnabled != enabled else { return }
        recordingBufferEnabled = enabled
        if enabled {
            startStreamSessionIfNeeded()
        } else {
            recordingBuffer.reset()
            recordingPacketBuilder.resetSequence()
            recordingRuntime.latestPacketPreview = .empty
            activeStreamSessionID = nil
        }
        publishRecordingBufferState()
    }

    func startGripperPathCalibration() {
        print("[UMIFT][gripper] start sweep calibration")
        processingQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.startSweepCalibration()
            DispatchQueue.main.async {
                self.gripperVisionState = nextState
            }
        }
    }

    func finishGripperPathCalibration() {
        print("[UMIFT][gripper] finish sweep calibration requested")
        processingQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.finishSweepCalibration()
            DispatchQueue.main.async {
                self.gripperVisionState = nextState
            }
        }
    }

    func cancelGripperPathCalibration() {
        print("[UMIFT][gripper] cancel sweep calibration")
        processingQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.cancelSweepCalibration()
            DispatchQueue.main.async {
                self.gripperVisionState = nextState
            }
        }
    }

    func resetGripperPathCalibration() {
        print("[UMIFT][gripper] reset path calibration")
        processingQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.resetCalibration()
            DispatchQueue.main.async {
                self.gripperVisionState = nextState
            }
        }
    }

    private func logGripperDebugIfNeeded(state: GripperVisionState, timestampNS: Int64) {
        guard timestampNS - lastGripperDebugLogNS > 1_000_000_000 else { return }
        lastGripperDebugLogNS = timestampNS
        print(
            "[UMIFT][gripper] \(state.statusText) ids=\(state.detectedIDsText) open=\(state.openingText) conf=\(state.confidenceText) pix=\(state.pixelDistanceText) cal=\(state.isPathCalibrated) sweep=\(state.sweepProgressText)"
        )
    }

    func setTorchEnabled(_ enabled: Bool) {
        torchRequested = enabled
        torchRequestedOn = enabled
        torchQueue.async { [weak self] in
            guard let self else { return }
            let status = Self.applyTorch(enabled: enabled, force: true)
            if enabled {
                self.startTorchKeepAlive()
            } else {
                self.stopTorchKeepAlive()
            }
            DispatchQueue.main.async {
                self.torchStatus = status
                self.torchRequested = enabled
            }
        }
    }

    private func updateSyncState() {
        guard let wideTimestampNS, let ultraTimestampNS else {
            syncValid = false
            syncErrorMS = nil
            recordingRuntime.syncValid = false
            recordingRuntime.syncErrorMS = nil
            return
        }
        let errorMS = abs(Double(wideTimestampNS - ultraTimestampNS)) / 1_000_000.0
        syncErrorMS = errorMS
        syncValid = errorMS <= 120.0
        bufferDurationS = min(3.0, bufferDurationS + 0.1)
        recordingRuntime.syncErrorMS = syncErrorMS
        recordingRuntime.syncValid = syncValid
    }

    private func ingestRecordingWideFrame(_ state: WorldTrackingARFrameState, previewSource: PreviewSource) {
        if recordingBufferEnabled {
            startStreamSessionIfNeeded()
        }
        let wideSample = RecordingWideFrameSample(
            frameSequence: state.frameSequence,
            timestampNS: state.timestampNS,
            trackingText: state.trackingText,
            cameraInUserWorldTransform: state.cameraInUserWorldTransform,
            rgbPixelBuffer: state.rgbPixelBuffer,
            rgbCameraIntrinsics: state.rgbCameraIntrinsics,
            rgbCaptureDeviceType: state.rgbCaptureDeviceType,
            depthAvailable: state.depthAvailable,
            depthSource: state.depthSource,
            depthWidth: state.depthWidth,
            depthHeight: state.depthHeight,
            depthUnit: state.depthUnit,
            depthPixelFormat: state.depthPixelFormat,
            depthCenterM: state.centerDepthM,
            depthValidRatio: state.depthValidRatio,
            depthConfidenceText: state.depthConfidenceText,
            previewSource: previewSource.rawValue
        )
        let alignedSample = recordingBuffer.appendWideFrame(wideSample)
        let preview = recordingPacketBuilder.makePreview(
            from: alignedSample,
            isRecording: recordingRuntime.isRecording
        )
        recordingRuntime.latestPacketPreview = preview
        enqueueArchiveSideData(sample: alignedSample, state: state)
        if recordingBufferEnabled, recordingRuntime.isRecording {
            appendLocalRecordingFrame(sample: alignedSample, preview: preview)
        }
        if recordingBufferEnabled {
            publishRecordingBufferState()
        }
    }

    private func ingestRecordingGripperState(_ state: GripperVisionState, timestampNS: Int64) {
        recordingBuffer.updateGripper(RecordingGripperSample(
            timestampNS: timestampNS,
            openPercent: state.openPercent
        ))
        if recordingBufferEnabled {
            publishRecordingBufferState()
        }
    }

    private func publishRecordingBufferState() {
        recordingRuntime.bufferDurationS = recordingBuffer.durationS
        recordingRuntime.bufferedFrameCount = recordingBuffer.frameCount
        bufferDurationS = recordingBuffer.durationS
    }

    private func emitRecordingEvent(_ code: RecordingEventCodeV1, reason: String) {
        startStreamSessionIfNeeded()
        let sample = recordingBuffer.latestFrame
        let eventIndex = nextRecordingEventIndex
        nextRecordingEventIndex &+= 1
        pendingArchiveEvents.append(RecordingEventV2(
            code: Self.archiveEventCode(code),
            eventIndex: eventIndex,
            eventUnixTimeNS: Int64(Date().timeIntervalSince1970 * 1_000_000_000.0),
            appUptimeNS: Int64(ProcessInfo.processInfo.systemUptime * 1_000_000_000.0),
            reason: reason
        ))
        if activeRecordingFileSessionID != nil {
            recordingRuntime.localFileStatus = localRecordingWriter.appendEvent(
                code: code,
                eventIndex: eventIndex,
                reason: reason,
                frameSequence: recordingPacketBuilder.nextSequence,
                sample: sample
            )
        }
        print(
            "[UMIFT][recording-event] local \(code.rawValue) stream=\(activeStreamSessionID ?? "--") "
            + "eventIndex=\(eventIndex) reason=\(reason)"
        )
    }

    private func enqueueArchiveSideData(
        sample: RecordingAlignedFrameSample,
        state: WorldTrackingARFrameState
    ) {
        do {
            let depthFrame = try Self.makeDepthFrame(from: state)
            var flags: FrameMetadataFlagsV2 = [.intrinsicsValid]
            if sample.pose != nil {
                flags.insert(.poseValid)
            }
            if sample.hasValidGripper {
                flags.insert(.gripperValid)
            }
            if depthFrame != nil {
                flags.insert(.depthValid)
            }
            if sample.wide.worldCalibrated {
                flags.insert(.worldCalibrated)
            }
            if recordingRuntime.isRecording {
                flags.insert(.recordingActive)
            }

            let transform = sample.cameraInUserWorldTransformRows?.flatMap { $0 }
                ?? [Float](repeating: .nan, count: 16)
            let intrinsics = sample.rgbCameraIntrinsicsRows?.flatMap { $0 }
                ?? [Float](repeating: .nan, count: 9)
            let metadata = FrameMetadataV2(
                flags: flags.rawValue,
                trackingState: Self.archiveTrackingState(sample.wide.trackingText),
                poseFrame: sample.wide.worldCalibrated ? 1 : 0,
                depthEncoding: depthFrame == nil ? 0 : 1,
                depthWidth: UInt16(clamping: state.depthWidth ?? 0),
                depthHeight: UInt16(clamping: state.depthHeight ?? 0),
                depthPixelFormat: UInt32(state.depthPixelFormat ?? 0),
                gripperTimestampNS: sample.gripper?.timestampNS ?? Int64.min,
                gripperAgeNS: sample.gripper.map { sample.localCaptureTimestampNS - $0.timestampNS } ?? Int64.min,
                gripperOpenPercent: sample.hasValidGripper
                    ? Float(sample.gripper?.openPercent ?? .nan)
                    : .nan,
                depthCenterM: Float(state.centerDepthM ?? .nan),
                depthValidRatio: Float(state.depthValidRatio),
                cameraInUserWorldTransform: transform,
                rgbCameraIntrinsics: intrinsics
            )
            let metadataPayload = try StreamProtocolV2.makeFrameMetadataPayload(metadata)
            let depthPayload = try depthFrame.map(StreamProtocolV2.makeDepthFramePayload)
            let eventPayloads = try pendingArchiveEvents.map(StreamProtocolV2.makeRecordingEventPayload)
            pendingArchiveEvents.removeAll(keepingCapacity: true)
            archiveTransport.enqueueSideData(ArchiveFrameSideDataV2(
                sequence: sample.wide.frameSequence,
                captureTimestampNS: sample.localCaptureTimestampNS,
                metadataPayload: metadataPayload,
                depthPayload: depthPayload,
                recordingEventPayloads: eventPayloads
            ))
        } catch {
            let reason = "frame side data encode failed seq=\(sample.wide.frameSequence): \(error)"
            recordingRuntime.captureIntegrityText = reason
            archiveTransport.failFrameIntegrity(reason)
        }
    }

    private static func makeDepthFrame(from state: WorldTrackingARFrameState) throws -> DepthFrameV2? {
        guard state.depthAvailable,
              let depthMap = state.depthMapPixelBuffer,
              let width = state.depthWidth,
              let height = state.depthHeight,
              width > 0,
              height > 0
        else { return nil }
        guard CVPixelBufferGetPixelFormatType(depthMap) == kCVPixelFormatType_DepthFloat32 else {
            throw StreamProtocolErrorV2.malformedPayload(
                "unsupported ARKit depth pixel format \(CVPixelBufferGetPixelFormatType(depthMap))"
            )
        }
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }
        guard let baseAddress = CVPixelBufferGetBaseAddress(depthMap) else {
            throw StreamProtocolErrorV2.malformedPayload("depth buffer has no base address")
        }
        let source = baseAddress.assumingMemoryBound(to: Float32.self)
        let sourceStride = CVPixelBufferGetBytesPerRow(depthMap) / MemoryLayout<Float32>.stride
        var samples = Data(count: width * height * MemoryLayout<UInt16>.stride)
        samples.withUnsafeMutableBytes { rawBuffer in
            let destination = rawBuffer.bindMemory(to: UInt16.self)
            for y in 0..<height {
                for x in 0..<width {
                    destination[y * width + x] = Float16(source[y * sourceStride + x]).bitPattern.littleEndian
                }
            }
        }
        return DepthFrameV2(
            width: UInt16(clamping: width),
            height: UInt16(clamping: height),
            pixelFormat: UInt32(CVPixelBufferGetPixelFormatType(depthMap)),
            samplesLittleEndian: samples
        )
    }

    private static func archiveTrackingState(_ text: String) -> UInt8 {
        if text == "normal" { return 1 }
        if text.hasPrefix("limited") { return 2 }
        if text.hasPrefix("unavailable") { return 3 }
        return 0
    }

    private static func archiveEventCode(_ code: RecordingEventCodeV1) -> UInt16 {
        switch code {
        case .coinFTRestartRequested: return 1
        case .recordStart: return 2
        case .recordStop: return 3
        case .recordDiscard: return 4
        }
    }

    private func appendLocalRecordingFrame(
        sample: RecordingAlignedFrameSample,
        preview: RecordingTransmitPacketPreview
    ) {
        recordingRuntime.localFileStatus = localRecordingWriter.append(
            sample: sample,
            preview: preview
        )
    }

    private func startStreamSessionIfNeeded() {
        guard activeStreamSessionID == nil else { return }
        let sessionID = RecordingJSONLCodec.makeSessionID(prefix: "stream")
        activeStreamSessionID = sessionID
        recordingPacketBuilder.resetSequence()
        print("[UMIFT][recording-local] start session=\(sessionID)")
    }

    private func startTorchKeepAlive() {
        guard torchKeepAliveTimer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: torchQueue)
        timer.schedule(deadline: .now() + 0.5, repeating: 1.0)
        timer.setEventHandler { [weak self] in
            guard let self, self.torchRequestedOn else { return }
            let status = Self.applyTorch(enabled: true, force: false)
            DispatchQueue.main.async {
                self.torchStatus = status
            }
        }
        torchKeepAliveTimer = timer
        timer.resume()
    }

    private func stopTorchKeepAlive() {
        torchKeepAliveTimer?.cancel()
        torchKeepAliveTimer = nil
    }

    private static func applyTorch(enabled: Bool, force: Bool) -> String {
        guard let device = preferredTorchDevice() else {
            return "torch unsupported"
        }
        if enabled, !force, device.isTorchActive, device.torchMode == .on {
            return "torch on keep \(shortName(for: device))"
        }

        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            if enabled {
                guard device.isTorchModeSupported(.on), device.isTorchAvailable else {
                    return "torch unavailable \(shortName(for: device))"
                }
                let level = min(1.0, AVCaptureDevice.maxAvailableTorchLevel)
                try device.setTorchModeOn(level: level)
                return String(format: "torch on %.2f %@", level, shortName(for: device))
            }
            if device.isTorchModeSupported(.off) {
                device.torchMode = .off
            }
            return "torch off \(shortName(for: device))"
        } catch {
            return "torch failed \(error.localizedDescription)"
        }
    }

    private static func preferredTorchDevice() -> AVCaptureDevice? {
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [
                .builtInWideAngleCamera,
                .builtInTripleCamera,
                .builtInDualWideCamera,
                .builtInDualCamera,
                .builtInUltraWideCamera
            ],
            mediaType: .video,
            position: .back
        )
        return discovery.devices.first { $0.hasTorch && $0.isTorchModeSupported(.on) }
    }

    private static func shortName(for device: AVCaptureDevice) -> String {
        switch device.deviceType {
        case .builtInTripleCamera:
            return "triple"
        case .builtInDualWideCamera:
            return "dualwide"
        case .builtInDualCamera:
            return "dual"
        case .builtInWideAngleCamera:
            return "wide"
        case .builtInUltraWideCamera:
            return "ultra"
        default:
            return "back"
        }
    }
}

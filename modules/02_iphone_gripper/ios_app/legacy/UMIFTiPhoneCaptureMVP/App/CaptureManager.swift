import ARKit
import AVFoundation
import CoreVideo
import Foundation
import CoreImage
import simd
import UIKit

enum CaptureSide: String, CaseIterable, Identifiable {
    case right
    case left

    var id: String { rawValue }
}

enum RecordingType: String, CaseIterable, Identifiable {
    case demonstration
    case grippercalibration
    case qrcalibration

    var id: String { rawValue }
}

@MainActor
final class CaptureManager: NSObject, ObservableObject {
    let session = ARSession()

    @Published var isRecording = false
    @Published var isOriginCalibrated = false
    @Published var frameCount = 0
    @Published var depthFrameCount = 0
    @Published var originStatusText = "origin not calibrated"
    @Published var calibrationGuideText = "place phone flat in landscape, long edge toward +X"
    @Published var calibrationMetricsText = "tilt --, snap --"
    @Published var statusText = "AR session idle"
    @Published var videoSourceText = "camera unknown"
    @Published var poseTranslationText = "x --  y --  z --"
    @Published var poseRotationText = "rx --  ry --  rz --"
    @Published var torchText = "torch --"
    @Published var isGripperPathCalibrated = false
    @Published var gripperCalibrationText = "gripper cal required"
    @Published var lastDemoDirectory: URL?

    private let recorderQueue = DispatchQueue(label: "local.umift.capture.recorder")
    private var activeRecorder: DemoRecorder?
    private var currentCaptureDeviceType = "unknown"
    private var currentImageResolution = "unknown"
    private var currentVideoFormatSummary = ARVideoFormatSummary.unknown
    private var currentSupportedVideoFormats: [ARVideoFormatSummary] = []
    private var sessionWorldTransformInARKitWorld: simd_float4x4?
    private var calibrationSnapshot: CalibrationSnapshot?
    private var lastTorchRefreshAt = Date.distantPast
    private static let gripperPathCalibrationDefaultsKey = "local.umift.gripper.pathCalibration.v1"

    override init() {
        super.init()
        session.delegate = self
    }

    func startARSession() {
        guard ARWorldTrackingConfiguration.isSupported else {
            statusText = "ARWorldTracking is not supported"
            return
        }
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        config.isLightEstimationEnabled = false
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.smoothedSceneDepth) {
            config.frameSemantics.insert(.smoothedSceneDepth)
        } else if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            config.frameSemantics.insert(.sceneDepth)
        }
        currentSupportedVideoFormats = Self.supportedVideoFormatSummaries()
        if let wideFormat = Self.preferredWideFormat() {
            config.videoFormat = wideFormat
        }
        let format = config.videoFormat
        currentCaptureDeviceType = format.captureDeviceType.rawValue
        currentImageResolution = "\(Int(format.imageResolution.width))x\(Int(format.imageResolution.height))"
        currentVideoFormatSummary = Self.videoFormatSummary(format)
        videoSourceText = "ARKit \(currentCaptureDeviceType) \(currentImageResolution) @\(format.framesPerSecond)"
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        torchText = TorchController.shared.setEnabled(true, reason: "ar-session-start")
        refreshGripperPathCalibrationStatus()
        resetCalibration(reason: "calibration required")
        statusText = "calibration stage"
    }

    func stopARSession() {
        session.pause()
        torchText = TorchController.shared.setEnabled(false, reason: "ar-session-stop")
        if !isRecording {
            UIApplication.shared.isIdleTimerDisabled = false
        }
        statusText = "AR session stopped"
    }

    func calibrateOrigin() {
        guard let current = session.currentFrame?.camera.transform else {
            originStatusText = "calibration failed: no current frame"
            return
        }
        guard let snapshot = DemoRecorder.makeCalibrationSnapshot(cameraTransform: current) else {
            originStatusText = "calibration failed: orientation unavailable"
            return
        }
        guard snapshot.canCalibrate else {
            originStatusText = String(
                format: "calibration blocked: tilt %.1f°, snap %.1f°",
                snapshot.tiltErrorDegrees,
                snapshot.headingSnapErrorDegrees
            )
            statusText = "calibration stage"
            return
        }
        sessionWorldTransformInARKitWorld = snapshot.arkitWorldFromSessionWorld
        calibrationSnapshot = snapshot
        isOriginCalibrated = true
        originStatusText = String(
            format: "origin calibrated: tilt %.1f°, snap %.1f°",
            snapshot.tiltErrorDegrees,
            snapshot.headingSnapErrorDegrees
        )
        calibrationGuideText = "session_world: +Z up, +X snapped from phone long edge"
        calibrationMetricsText = String(
            format: "tilt %.1f°, snap %.1f°, heading %.0f°",
            snapshot.tiltErrorDegrees,
            snapshot.headingSnapErrorDegrees,
            snapshot.snappedHeadingDegrees
        )
        statusText = "record stage ready"
    }

    func canStartRecording() -> Bool {
        isOriginCalibrated && isGripperPathCalibrated && !isRecording
    }

    func startRecording(sessionName: String, side: CaptureSide, recordingType: RecordingType) throws {
        refreshGripperPathCalibrationStatus()
        guard isOriginCalibrated else {
            statusText = "start blocked: calibrate origin first"
            return
        }
        guard isGripperPathCalibrated else {
            statusText = "start blocked: calibrate gripper path first"
            return
        }
        guard !isRecording else { return }
        let cleanSession = sanitize(sessionName.isEmpty ? "session" : sessionName)
        let recorder = try DemoRecorder(
            sessionName: cleanSession,
            side: side,
            recordingType: recordingType,
            rgbCaptureDeviceType: currentCaptureDeviceType,
            rgbImageResolution: currentImageResolution,
            selectedARVideoFormat: currentVideoFormatSummary,
            arkitSupportedVideoFormats: currentSupportedVideoFormats,
            sessionWorldTransformInARKitWorld: sessionWorldTransformInARKitWorld,
            calibrationSnapshot: calibrationSnapshot
        )
        activeRecorder = recorder
        frameCount = 0
        depthFrameCount = 0
        torchText = TorchController.shared.setEnabled(true, reason: "recording-start")
        isRecording = true
        UIApplication.shared.isIdleTimerDisabled = true
        statusText = "recording \(cleanSession)"
    }

    func stopRecording() async throws {
        guard let recorder = activeRecorder else { return }
        defer { UIApplication.shared.isIdleTimerDisabled = false }
        isRecording = false
        activeRecorder = nil
        statusText = "finalizing"

        let directory = try await recorder.finish()
        lastDemoDirectory = directory
        statusText = "saved \(directory.lastPathComponent)"
    }

    private func sanitize(_ value: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        return value.unicodeScalars.map { allowed.contains($0) ? Character($0) : "-" }.reduce("") { $0 + String($1) }
    }

    private static func preferredWideFormat() -> ARConfiguration.VideoFormat? {
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats
            .filter { $0.captureDevicePosition == .back }
            .filter { $0.captureDeviceType == .builtInWideAngleCamera }
            .sorted {
                if $0.framesPerSecond != $1.framesPerSecond {
                    return $0.framesPerSecond > $1.framesPerSecond
                }
                let lhsPixels = $0.imageResolution.width * $0.imageResolution.height
                let rhsPixels = $1.imageResolution.width * $1.imageResolution.height
                return lhsPixels > rhsPixels
            }
        return formats.first
    }

    private static func supportedVideoFormatSummaries() -> [ARVideoFormatSummary] {
        ARWorldTrackingConfiguration.supportedVideoFormats
            .map(videoFormatSummary)
            .sorted {
                if $0.captureDevicePosition != $1.captureDevicePosition {
                    return $0.captureDevicePosition < $1.captureDevicePosition
                }
                if $0.captureDeviceType != $1.captureDeviceType {
                    return $0.captureDeviceType < $1.captureDeviceType
                }
                if $0.framesPerSecond != $1.framesPerSecond {
                    return $0.framesPerSecond > $1.framesPerSecond
                }
                return ($0.width * $0.height) > ($1.width * $1.height)
            }
    }

    private static func videoFormatSummary(_ format: ARConfiguration.VideoFormat) -> ARVideoFormatSummary {
        ARVideoFormatSummary(
            captureDeviceType: format.captureDeviceType.rawValue,
            captureDevicePosition: Self.captureDevicePositionName(format.captureDevicePosition),
            width: Int(format.imageResolution.width),
            height: Int(format.imageResolution.height),
            framesPerSecond: format.framesPerSecond
        )
    }

    private static func captureDevicePositionName(_ position: AVCaptureDevice.Position) -> String {
        switch position {
        case .back:
            return "back"
        case .front:
            return "front"
        case .unspecified:
            return "unspecified"
        @unknown default:
            return "unknown"
        }
    }
}

extension CaptureManager: ARSessionDelegate {
    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        Task { @MainActor in
            updateCalibrationPreview(cameraTransform: frame.camera.transform)
            refreshTorchIfNeeded()
            guard let recorder = activeRecorder, isRecording else { return }
            let nextCount = frameCount + 1
            frameCount = nextCount
            if frame.smoothedSceneDepth != nil || frame.sceneDepth != nil {
                depthFrameCount += 1
            }
            if nextCount % 30 == 0 {
                statusText = "frames \(nextCount), depth \(depthFrameCount)"
            }
            recorder.append(frame: frame)
        }
    }

    nonisolated func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        Task { @MainActor in
            switch camera.trackingState {
            case .normal:
                statusText = isRecording ? "tracking normal" : "ready"
            case .notAvailable:
                statusText = "tracking unavailable"
            case .limited(let reason):
                statusText = "tracking limited: \(reason)"
            }
        }
    }
}

private extension CaptureManager {
    func resetCalibration(reason: String) {
        sessionWorldTransformInARKitWorld = nil
        calibrationSnapshot = nil
        isOriginCalibrated = false
        originStatusText = "origin not calibrated"
        calibrationGuideText = "place phone flat in landscape, long edge toward +X"
        calibrationMetricsText = "tilt --, snap --"
        statusText = reason
    }

    func updateCalibrationPreview(cameraTransform: simd_float4x4) {
        let sessionWorldCamera = DemoRecorder.sessionWorldTransform(
            from: cameraTransform,
            sessionWorldTransformInARKitWorld: sessionWorldTransformInARKitWorld
        )
        let pose6d = DemoRecorder.pose6d(from: sessionWorldCamera)
        poseTranslationText = String(
            format: "x %.3f  y %.3f  z %.3f m",
            pose6d[0], pose6d[1], pose6d[2]
        )
        poseRotationText = String(
            format: "rx %.1f  ry %.1f  rz %.1f deg",
            pose6d[3], pose6d[4], pose6d[5]
        )

        guard let snapshot = DemoRecorder.makeCalibrationSnapshot(cameraTransform: cameraTransform) else {
            calibrationMetricsText = "tilt --, snap --"
            if !isOriginCalibrated {
                calibrationGuideText = "waiting for a stable AR pose"
            }
            return
        }

        if !isOriginCalibrated {
            originStatusText = snapshot.canCalibrate ? "ready to calibrate" : "calibration stage"
            calibrationGuideText = snapshot.canCalibrate
                ? "tap Calibrate to lock session_world"
                : "keep phone flat; rotate toward nearest snapped heading"
        }
        calibrationMetricsText = String(
            format: "tilt %.1f°, snap %.1f°, heading %.0f°",
            snapshot.tiltErrorDegrees,
            snapshot.headingSnapErrorDegrees,
            snapshot.snappedHeadingDegrees
        )
    }

    func refreshTorchIfNeeded() {
        let now = Date()
        guard now.timeIntervalSince(lastTorchRefreshAt) >= 2.0 else { return }
        lastTorchRefreshAt = now
        torchText = TorchController.shared.ensureOnIfRequested(reason: "ar-frame-refresh")
    }

    func refreshGripperPathCalibrationStatus() {
        if UserDefaults.standard.data(forKey: Self.gripperPathCalibrationDefaultsKey) != nil {
            isGripperPathCalibrated = true
            gripperCalibrationText = "gripper path calibrated"
        } else {
            isGripperPathCalibrated = false
            gripperCalibrationText = "gripper cal required"
        }
    }
}

private final class DemoRecorder {
    private let sessionName: String
    private let side: CaptureSide
    private let recordingType: RecordingType
    private let demoDirectory: URL
    private let rgbURL: URL
    private let ultrawideURL: URL
    private let depthURL: URL
    private let depthConfidenceURL: URL
    private let jsonURL: URL
    private let queue = DispatchQueue(label: "local.umift.capture.demo-recorder")

    private var writer: AVAssetWriter?
    private var writerInput: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var firstFrameTimestamp: TimeInterval?
    private let recordingStartDate = Date()
    private var poseTimes: [String] = []
    private var poseTransforms: [[[Double]]] = []
    private var ultrawideRGBTimes: [String] = []
    private var depthTimes: [String] = []
    private var gripperWidthMeters: [Double] = []
    private var depthFrames = Data()
    private var depthConfidenceFrames = Data()
    private var realDepthFrameCount = 0
    private var missingDepthFrameCount = 0
    private var realConfidenceFrameCount = 0
    private var rgbCaptureDeviceType = "unknown"
    private var rgbImageResolution = ""
    private var selectedARVideoFormat: ARVideoFormatSummary
    private var arkitSupportedVideoFormats: [ARVideoFormatSummary]
    private let sessionWorldTransformInARKitWorld: simd_float4x4?
    private let calibrationSnapshot: CalibrationSnapshot?
    private var rgbImageSize: ImageSizeJSON?
    private var rgbCameraIntrinsics: simd_float3x3?
    private var depthSourceResolution: ImageSizeJSON?
    private var depthConfidenceResolution: ImageSizeJSON?
    private var depthSourceCameraIntrinsics: simd_float3x3?
    private var depthExportCameraIntrinsics: simd_float3x3?
    private var depthPixelCount = 0
    private var depthValidPixelCount = 0
    private var depthConfidenceHistogram = [Int](repeating: 0, count: 4)
    private var didStartWriter = false
    private var didFinish = false

    init(
        sessionName: String,
        side: CaptureSide,
        recordingType: RecordingType,
        rgbCaptureDeviceType: String,
        rgbImageResolution: String,
        selectedARVideoFormat: ARVideoFormatSummary,
        arkitSupportedVideoFormats: [ARVideoFormatSummary],
        sessionWorldTransformInARKitWorld: simd_float4x4?,
        calibrationSnapshot: CalibrationSnapshot?
    ) throws {
        self.sessionName = sessionName
        self.side = side
        self.recordingType = recordingType
        self.rgbCaptureDeviceType = rgbCaptureDeviceType
        self.rgbImageResolution = rgbImageResolution
        self.selectedARVideoFormat = selectedARVideoFormat
        self.arkitSupportedVideoFormats = arkitSupportedVideoFormats
        self.sessionWorldTransformInARKitWorld = sessionWorldTransformInARKitWorld
        self.calibrationSnapshot = calibrationSnapshot

        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let dateFolder = Self.dateFolderFormatter.string(from: recordingStartDate)
        let demoName = "\(Self.folderTimestampFormatter.string(from: recordingStartDate))_00001_\(sessionName)_demonstration"
        let demoDirectory = documents
            .appendingPathComponent("processed_data", isDirectory: true)
            .appendingPathComponent("iphone", isDirectory: true)
            .appendingPathComponent(dateFolder, isDirectory: true)
            .appendingPathComponent(demoName, isDirectory: true)

        try FileManager.default.createDirectory(at: demoDirectory, withIntermediateDirectories: true)
        self.demoDirectory = demoDirectory
        self.rgbURL = demoDirectory.appendingPathComponent("\(side.rawValue)_rgb.mp4")
        self.ultrawideURL = demoDirectory.appendingPathComponent("\(side.rawValue)_ultrawidergb.mp4")
        self.depthURL = demoDirectory.appendingPathComponent("\(side.rawValue)_depth.raw")
        self.depthConfidenceURL = demoDirectory.appendingPathComponent("\(side.rawValue)_depth_confidence.raw")
        self.jsonURL = demoDirectory.appendingPathComponent("\(side.rawValue).json")
    }

    func append(frame: ARFrame) {
        let pixelBuffer = frame.capturedImage
        let depthMap = frame.smoothedSceneDepth?.depthMap ?? frame.sceneDepth?.depthMap
        let confidenceMap = frame.smoothedSceneDepth?.confidenceMap ?? frame.sceneDepth?.confidenceMap
        let transform = Self.sessionWorldTransform(
            from: frame.camera.transform,
            sessionWorldTransformInARKitWorld: sessionWorldTransformInARKitWorld
        )
        let frameTimestamp = frame.timestamp

        queue.async {
            if self.didFinish { return }

            do {
                if self.writer == nil {
                    try self.configureWriter(pixelBuffer: pixelBuffer)
                }

                if self.firstFrameTimestamp == nil {
                    self.firstFrameTimestamp = frameTimestamp
                    self.writer?.startWriting()
                    self.writer?.startSession(atSourceTime: .zero)
                    self.didStartWriter = true
                }

                guard let firstFrameTimestamp = self.firstFrameTimestamp else { return }
                let relativeTime = frameTimestamp - firstFrameTimestamp
                let presentationTime = CMTime(seconds: relativeTime, preferredTimescale: 600)

                if self.writerInput?.isReadyForMoreMediaData == true {
                    self.adaptor?.append(pixelBuffer, withPresentationTime: presentationTime)
                    let utc = self.recordingStartDate.addingTimeInterval(relativeTime)
                    let timestamp = Self.isoFormatter.string(from: utc)
                    self.captureAlignmentMetadataIfNeeded(frame: frame, depthMap: depthMap, confidenceMap: confidenceMap)
                    self.poseTimes.append(timestamp)
                    self.poseTransforms.append(Self.matrixRows(transform))
                    self.ultrawideRGBTimes.append(timestamp)
                    self.depthTimes.append(timestamp)
                    self.gripperWidthMeters.append(0.04)
                    if let depthMap {
                        self.appendDepthFrame(depthMap, confidenceMap: confidenceMap)
                        self.realDepthFrameCount += 1
                        if confidenceMap != nil {
                            self.realConfidenceFrameCount += 1
                        }
                    } else {
                        self.appendZeroDepthFrameAndConfidence()
                        self.missingDepthFrameCount += 1
                    }
                }
            } catch {
                print("append failed: \(error)")
            }
        }
    }

    func finish() async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                self.didFinish = true
                guard let writer = self.writer, self.didStartWriter else {
                    continuation.resume(throwing: RecorderError.noFrames)
                    return
                }

                self.writerInput?.markAsFinished()
                writer.finishWriting {
                    do {
                        if writer.status == .failed {
                            throw writer.error ?? RecorderError.writerFailed
                        }
                        try FileManager.default.copyItem(at: self.rgbURL, to: self.ultrawideURL)
                        try self.writeDepth()
                        try self.writeDepthConfidence()
                        try self.writeCoinFTSimulation()
                        try self.writeJSON()
                        continuation.resume(returning: self.demoDirectory)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
        }
    }

    private func configureWriter(pixelBuffer: CVPixelBuffer) throws {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let writer = try AVAssetWriter(outputURL: rgbURL, fileType: .mp4)
        let input = AVAssetWriterInput(
            mediaType: .video,
            outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: width,
                AVVideoHeightKey: height,
                AVVideoCompressionPropertiesKey: [
                    AVVideoAverageBitRateKey: 12_000_000,
                    AVVideoMaxKeyFrameIntervalKey: 60
                ]
            ]
        )
        input.expectsMediaDataInRealTime = true

        let adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: input,
            sourcePixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: CVPixelBufferGetPixelFormatType(pixelBuffer),
                kCVPixelBufferWidthKey as String: width,
                kCVPixelBufferHeightKey as String: height
            ]
        )

        guard writer.canAdd(input) else { throw RecorderError.cannotAddWriterInput }
        writer.add(input)
        self.writer = writer
        self.writerInput = input
        self.adaptor = adaptor
    }

    private func appendZeroDepthFrameAndConfidence() {
        let zero = UInt16(0)
        for _ in 0..<(Self.depthWidth * Self.depthHeight) {
            var value = zero
            depthFrames.append(Data(bytes: &value, count: MemoryLayout<UInt16>.size))
            var confidence = UInt8(0)
            depthConfidenceFrames.append(Data(bytes: &confidence, count: MemoryLayout<UInt8>.size))
        }
        depthPixelCount += Self.depthWidth * Self.depthHeight
        depthConfidenceHistogram[0] += Self.depthWidth * Self.depthHeight
    }

    private func appendDepthFrame(_ depthMap: CVPixelBuffer, confidenceMap: CVPixelBuffer?) {
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }
        if let confidenceMap {
            CVPixelBufferLockBaseAddress(confidenceMap, .readOnly)
        }
        defer {
            if let confidenceMap {
                CVPixelBufferUnlockBaseAddress(confidenceMap, .readOnly)
            }
        }

        let sourceWidth = CVPixelBufferGetWidth(depthMap)
        let sourceHeight = CVPixelBufferGetHeight(depthMap)
        let pixelFormat = CVPixelBufferGetPixelFormatType(depthMap)
        let rowBytes = CVPixelBufferGetBytesPerRow(depthMap)
        guard let baseAddress = CVPixelBufferGetBaseAddress(depthMap) else {
            appendZeroDepthFrameAndConfidence()
            return
        }

        let confidenceBaseAddress = confidenceMap.flatMap { CVPixelBufferGetBaseAddress($0) }
        let confidenceWidth = confidenceMap.map(CVPixelBufferGetWidth) ?? 0
        let confidenceHeight = confidenceMap.map(CVPixelBufferGetHeight) ?? 0
        let confidenceRowBytes = confidenceMap.map(CVPixelBufferGetBytesPerRow) ?? 0
        let confidencePixelFormat = confidenceMap.map(CVPixelBufferGetPixelFormatType)

        for y in 0..<Self.depthHeight {
            let sourceY = min(sourceHeight - 1, Int((Double(y) + 0.5) * Double(sourceHeight) / Double(Self.depthHeight)))
            for x in 0..<Self.depthWidth {
                let sourceX = min(sourceWidth - 1, Int((Double(x) + 0.5) * Double(sourceWidth) / Double(Self.depthWidth)))
                let meters: Float
                if pixelFormat == kCVPixelFormatType_DepthFloat32 {
                    let row = baseAddress.advanced(by: sourceY * rowBytes)
                    meters = row.assumingMemoryBound(to: Float32.self)[sourceX]
                } else if pixelFormat == kCVPixelFormatType_DepthFloat16 {
                    let row = baseAddress.advanced(by: sourceY * rowBytes)
                    meters = Float(row.assumingMemoryBound(to: Float16.self)[sourceX])
                } else {
                    meters = 0.0
                }
                let clipped = meters.isFinite && meters > 0 ? min(meters, 10.0) : 0.0
                var bits = Float16(clipped).bitPattern
                depthFrames.append(Data(bytes: &bits, count: MemoryLayout<UInt16>.size))
                depthPixelCount += 1
                if clipped > 0 {
                    depthValidPixelCount += 1
                }

                let confidence: UInt8
                if
                    let confidenceBaseAddress,
                    confidencePixelFormat == kCVPixelFormatType_OneComponent8
                {
                    let confidenceY = min(confidenceHeight - 1, Int((Double(y) + 0.5) * Double(confidenceHeight) / Double(Self.depthHeight)))
                    let confidenceX = min(confidenceWidth - 1, Int((Double(x) + 0.5) * Double(confidenceWidth) / Double(Self.depthWidth)))
                    let row = confidenceBaseAddress.advanced(by: confidenceY * confidenceRowBytes)
                    confidence = row.assumingMemoryBound(to: UInt8.self)[confidenceX]
                } else {
                    confidence = 0
                }
                var confidenceBits = confidence
                depthConfidenceFrames.append(Data(bytes: &confidenceBits, count: MemoryLayout<UInt8>.size))
                depthConfidenceHistogram[min(depthConfidenceHistogram.count - 1, Int(confidence))] += 1
            }
        }
    }

    private func writeDepth() throws {
        let expectedBytes = poseTimes.count * Self.depthWidth * Self.depthHeight * MemoryLayout<UInt16>.size
        if depthFrames.count < expectedBytes {
            let missingFrames = (expectedBytes - depthFrames.count) / (Self.depthWidth * Self.depthHeight * MemoryLayout<UInt16>.size)
            for _ in 0..<missingFrames {
                appendZeroDepthFrameAndConfidence()
            }
        }
        try depthFrames.write(to: depthURL, options: .atomic)
    }

    private func writeDepthConfidence() throws {
        let expectedBytes = poseTimes.count * Self.depthWidth * Self.depthHeight * MemoryLayout<UInt8>.size
        if depthConfidenceFrames.count < expectedBytes {
            let missingPixels = expectedBytes - depthConfidenceFrames.count
            for _ in 0..<missingPixels {
                var value = UInt8(0)
                depthConfidenceFrames.append(Data(bytes: &value, count: MemoryLayout<UInt8>.size))
            }
            depthConfidenceHistogram[0] += missingPixels
        }
        try depthConfidenceFrames.write(to: depthConfidenceURL, options: .atomic)
    }

    private func writeCoinFTSimulation() throws {
        guard poseTimes.count >= 2 else { return }
        let outputDirectory = demoDirectory.appendingPathComponent("coinft_sim", isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        let streamURL = outputDirectory.appendingPathComponent("coinft_sim_stream.csv")
        let alignedURL = outputDirectory.appendingPathComponent("frame_aligned_coinft.csv")
        let metadataURL = outputDirectory.appendingPathComponent("coinft_sync_metadata.json")

        let poseSeconds = try poseTimes.map(Self.parseISOSeconds)
        let start = poseSeconds[0] - 0.5
        let end = poseSeconds[poseSeconds.count - 1] + 0.5
        let sampleHz = 120.0
        let fixedLatency = 0.045
        let jitterStd = 0.008
        let step = 1.0 / sampleHz
        var rng = DeterministicRNG(seed: 7)
        var samples: [CoinFTSample] = []
        var seq = 0
        var sampleTime = start
        while sampleTime <= end {
            if rng.nextUnit() >= 0.01 {
                let latency = max(0.0, fixedLatency + rng.nextGaussian() * jitterStd)
                let tRelative = sampleTime - poseSeconds[0]
                samples.append(CoinFTSample(
                    sequence: seq,
                    deviceTime: sampleTime,
                    receiveTime: sampleTime + latency,
                    latency: latency,
                    wrench: Self.syntheticCoinFTWrench(tRelative, rng: &rng)
                ))
            }
            seq += 1
            sampleTime += step
        }
        guard samples.count >= 2 else { return }

        try writeCoinFTStream(samples, to: streamURL)
        try writeUMIFTStyleCoinFTCSVs(samples)
        let medianLatency = Self.median(samples.map(\.latency))
        let correctedTimes = samples.map { $0.receiveTime - medianLatency }
        try writeCoinFTAligned(
            poseSeconds: poseSeconds,
            poseTimeStrings: poseTimes,
            correctedSampleTimes: correctedTimes,
            samples: samples,
            to: alignedURL
        )
        let metadata = CoinFTSyncMetadata(
            source: "iphone_side_simulation",
            sampleRateHz: sampleHz,
            fixedLatencySeconds: fixedLatency,
            jitterStdSeconds: jitterStd,
            estimatedLatencySeconds: medianLatency,
            sampleCount: samples.count,
            frameCount: poseTimes.count,
            timestampMode: "receive_minus_estimated_latency",
            streamCsv: "coinft_sim_stream.csv",
            alignedCsv: "frame_aligned_coinft.csv",
            note: "Simulates an external delayed force/torque device received by iPhone. Replace this stream with BLE/USB samples later."
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(metadata).write(to: metadataURL, options: .atomic)
    }

    private func writeCoinFTStream(_ samples: [CoinFTSample], to url: URL) throws {
        var csv = "seq,device_time_s,device_time_iso8601,iphone_receive_time_s,iphone_receive_time_iso8601,latency_s,left_fx,left_fy,left_fz,left_mx,left_my,left_mz,right_fx,right_fy,right_fz,right_mx,right_my,right_mz\n"
        for sample in samples {
            csv += "\(sample.sequence),\(Self.decimal(sample.deviceTime)),\(Self.isoFormatter.string(from: Date(timeIntervalSince1970: sample.deviceTime))),\(Self.decimal(sample.receiveTime)),\(Self.isoFormatter.string(from: Date(timeIntervalSince1970: sample.receiveTime))),\(Self.decimal(sample.latency)),\(sample.wrench.csv)\n"
        }
        try csv.write(to: url, atomically: true, encoding: .utf8)
    }

    private func writeUMIFTStyleCoinFTCSVs(_ samples: [CoinFTSample]) throws {
        let dateDirectoryName = Self.coinftDateFormatter.string(from: recordingStartDate)
        let stamp = Self.coinftTimestampFormatter.string(from: recordingStartDate)
        let cleanSession = sessionName.isEmpty ? "session" : sessionName
        let outputDirectory = demoDirectory
            .appendingPathComponent("coinft", isDirectory: true)
            .appendingPathComponent(dateDirectoryName, isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

        let leftURL = outputDirectory.appendingPathComponent("UMIFT_data_\(stamp)_\(cleanSession)_LF.csv")
        let rightURL = outputDirectory.appendingPathComponent("UMIFT_data_\(stamp)_\(cleanSession)_RF.csv")
        try writeUMIFTStyleCoinFTCSV(samples, side: .left, to: leftURL)
        try writeUMIFTStyleCoinFTCSV(samples, side: .right, to: rightURL)
    }

    private func writeUMIFTStyleCoinFTCSV(_ samples: [CoinFTSample], side: CoinFTSensorSide, to url: URL) throws {
        var csv = "Timestamp,Fx,Fy,Fz,Mx,My,Mz,C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12\n"
        for sample in samples {
            let wrench = sample.wrench.values(for: side)
            let rawChannels = sample.rawChannels(for: side)
            let values = wrench + rawChannels
            csv += "\(Self.isoFormatter.string(from: Date(timeIntervalSince1970: sample.deviceTime))),"
            csv += values.map { String(format: "%.6f", $0) }.joined(separator: ",")
            csv += "\n"
        }
        try csv.write(to: url, atomically: true, encoding: .utf8)
    }

    private func writeCoinFTAligned(
        poseSeconds: [TimeInterval],
        poseTimeStrings: [String],
        correctedSampleTimes: [TimeInterval],
        samples: [CoinFTSample],
        to url: URL
    ) throws {
        var csv = "frame_index,iphone_pose_time_s,iphone_pose_time_iso8601,nearest_sample_index,nearest_sample_age_s,left_fx,left_fy,left_fz,left_mx,left_my,left_mz,right_fx,right_fy,right_fz,right_mx,right_my,right_mz,grasp_force\n"
        for index in poseSeconds.indices {
            let target = poseSeconds[index]
            let interpolated = Self.interpolateWrench(sampleTimes: correctedSampleTimes, samples: samples, target: target)
            let nearest = Self.nearestSampleIndex(sampleTimes: correctedSampleTimes, target: target)
            let age = target - correctedSampleTimes[nearest]
            let graspForce = (-interpolated.leftFx + interpolated.rightFx) / 2.0
            csv += "\(index),\(Self.decimal(target)),\(poseTimeStrings[index]),\(nearest),\(Self.decimal(age)),\(interpolated.csv),\(Self.decimal(graspForce))\n"
        }
        try csv.write(to: url, atomically: true, encoding: .utf8)
    }

    private func writeJSON() throws {
        let payload = ProcessedDemoJSON(
            sessionName: sessionName,
                        side: side.rawValue,
                        type: recordingType.rawValue,
                        poseCoordinateFrame: "session_world",
                        calibration: calibrationSnapshot?.jsonValue,
                        recordingStartTime: poseTimes.first ?? Self.isoFormatter.string(from: recordingStartDate),
                        poseTimes: poseTimes,
                        poseTransforms: poseTransforms,
            ultrawideRGBTimes: ultrawideRGBTimes,
            depthTimes: depthTimes,
            rgbFrameCount: poseTimes.count,
            ultrawideFrameCount: poseTimes.count,
            depthFrameCount: poseTimes.count,
            gripperWidthMeters: gripperWidthMeters,
            gripperWidthSource: "constant_phase1_placeholder",
            depthSource: realDepthFrameCount > 0 ? "arkit_smoothed_or_scene_depth" : "zero_placeholder",
            realDepthFrameCount: realDepthFrameCount,
            missingDepthFrameCount: missingDepthFrameCount,
            rgbCaptureDeviceType: rgbCaptureDeviceType,
            rgbImageResolution: rgbImageResolution,
            rgbImageSize: rgbImageSize,
            rgbCameraIntrinsics: rgbCameraIntrinsics.map(Self.matrixRows),
            depthSourceResolution: depthSourceResolution,
            depthExportResolution: ImageSizeJSON(width: Self.depthWidth, height: Self.depthHeight),
            depthConfidenceResolution: depthConfidenceResolution,
            depthSourceCameraIntrinsics: depthSourceCameraIntrinsics.map(Self.matrixRows),
            depthExportCameraIntrinsics: depthExportCameraIntrinsics.map(Self.matrixRows),
            depthConfidencePath: depthConfidenceURL.lastPathComponent,
            depthConfidenceFrameCount: poseTimes.count,
            realDepthConfidenceFrameCount: realConfidenceFrameCount,
            depthConfidenceAvailable: realConfidenceFrameCount > 0,
            depthCoordinateSpace: "captured_image_normalized",
            depthAlignmentReference: "ARFrame.capturedImage",
            depthExportSampling: "nearest_from_arkit_depth_map",
            depthUnit: "meter",
            depthValueClampMeters: [0.0, 10.0],
            depthValidPixelRatio: depthPixelCount > 0 ? Double(depthValidPixelCount) / Double(depthPixelCount) : 0.0,
            depthConfidenceHistogram: depthConfidenceHistogram,
            selectedARVideoFormat: selectedARVideoFormat,
            arkitSupportedVideoFormats: arkitSupportedVideoFormats,
            ultrawideMode: rgbCaptureDeviceType.contains("UltraWide") ? "arkit_ultrawide_video_format" : "rgb_duplicate_placeholder",
            note: "Phase 2 MVP: ARFrame video, ARKit pose, and ARKit scene depth aligned to the same frame timeline. The stable capture path prefers the back wide-angle camera; ultrawide remains a compatibility placeholder file."
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(payload).write(to: jsonURL, options: .atomic)
    }

    private func captureAlignmentMetadataIfNeeded(frame: ARFrame, depthMap: CVPixelBuffer?, confidenceMap: CVPixelBuffer?) {
        if rgbImageSize == nil {
            let resolution = frame.camera.imageResolution
            rgbImageSize = ImageSizeJSON(width: Int(resolution.width), height: Int(resolution.height))
        }
        if rgbCameraIntrinsics == nil {
            rgbCameraIntrinsics = frame.camera.intrinsics
        }
        if depthExportCameraIntrinsics == nil, let intrinsics = rgbCameraIntrinsics {
            depthExportCameraIntrinsics = Self.scaledIntrinsics(
                intrinsics,
                from: rgbImageSize,
                to: ImageSizeJSON(width: Self.depthWidth, height: Self.depthHeight)
            )
        }
        if depthSourceResolution == nil, let depthMap {
            let resolution = ImageSizeJSON(
                width: CVPixelBufferGetWidth(depthMap),
                height: CVPixelBufferGetHeight(depthMap)
            )
            depthSourceResolution = resolution
            if let intrinsics = rgbCameraIntrinsics {
                depthSourceCameraIntrinsics = Self.scaledIntrinsics(
                    intrinsics,
                    from: rgbImageSize,
                    to: resolution
                )
            }
        }
        if depthConfidenceResolution == nil, let confidenceMap {
            depthConfidenceResolution = ImageSizeJSON(
                width: CVPixelBufferGetWidth(confidenceMap),
                height: CVPixelBufferGetHeight(confidenceMap)
            )
        }
    }

    static func matrixRows(_ matrix: simd_float4x4) -> [[Double]] {
        (0..<4).map { row in
            (0..<4).map { col in
                Double(matrix[col][row])
            }
        }
    }

    static func matrixRows(_ matrix: simd_float3x3) -> [[Double]] {
        (0..<3).map { row in
            (0..<3).map { col in
                Double(matrix[col][row])
            }
        }
    }

    static func sessionWorldTransform(
        from cameraTransform: simd_float4x4,
        sessionWorldTransformInARKitWorld: simd_float4x4?
    ) -> simd_float4x4 {
        guard let sessionWorldTransformInARKitWorld else { return cameraTransform }
        return sessionWorldTransformInARKitWorld.inverse * cameraTransform
    }

    static func makeCalibrationSnapshot(cameraTransform: simd_float4x4) -> CalibrationSnapshot? {
        let worldUp = simd_float3(0, 1, 0)
        let arkitX = simd_float3(1, 0, 0)
        let arkitZ = simd_float3(0, 0, 1)
        let cameraForward = normalized(-cameraTransform.columns.2.xyz)
        let rawForward = normalized(projectOntoPlane(normalized(cameraTransform.columns.0.xyz), normal: worldUp))
        guard simd_length(rawForward) > 0.0001 else { return nil }

        let rawHeading = atan2(simd_dot(rawForward, arkitZ), simd_dot(rawForward, arkitX))
        let snappedHeading = round(rawHeading / (.pi / 2.0)) * (.pi / 2.0)
        let snappedForward = normalized(cos(snappedHeading) * arkitX + sin(snappedHeading) * arkitZ)
        let sessionZ = worldUp
        let sessionX = snappedForward
        let sessionY = normalized(simd_cross(sessionZ, sessionX))
        let translation = cameraTransform.columns.3.xyz
        let arkitWorldFromSessionWorld = simd_float4x4(
            columns: (
                simd_float4(sessionX.x, sessionX.y, sessionX.z, 0),
                simd_float4(sessionY.x, sessionY.y, sessionY.z, 0),
                simd_float4(sessionZ.x, sessionZ.y, sessionZ.z, 0),
                simd_float4(translation.x, translation.y, translation.z, 1)
            )
        )

        let tiltError = radiansToDegrees(acos(clampedDot(cameraForward, worldUp)))
        let headingError = radiansToDegrees(abs(shortestAngle(rawHeading - snappedHeading)))
        let snappedHeadingDegrees = normalizeDegrees(radiansToDegrees(snappedHeading))
        return CalibrationSnapshot(
            tiltErrorDegrees: tiltError,
            headingSnapErrorDegrees: headingError,
            rawHeadingDegrees: normalizeDegrees(radiansToDegrees(rawHeading)),
            snappedHeadingDegrees: snappedHeadingDegrees,
            arkitWorldFromSessionWorld: arkitWorldFromSessionWorld
        )
    }

    private static func projectOntoPlane(_ vector: simd_float3, normal: simd_float3) -> simd_float3 {
        vector - simd_dot(vector, normal) * normal
    }

    static func pose6d(from transform: simd_float4x4) -> [Double] {
        let rotation = simd_float3x3(
            columns: (
                transform.columns.0.xyz,
                transform.columns.1.xyz,
                transform.columns.2.xyz
            )
        )
        let rotvecRadians = rotationMatrixToRotvec(rotation)
        return [
            Double(transform.columns.3.x),
            Double(transform.columns.3.y),
            Double(transform.columns.3.z),
            radiansToDegrees(rotvecRadians.x),
            radiansToDegrees(rotvecRadians.y),
            radiansToDegrees(rotvecRadians.z),
        ]
    }

    private static func normalized(_ vector: simd_float3) -> simd_float3 {
        let length = simd_length(vector)
        guard length > 0.0001 else { return .zero }
        return vector / length
    }

    private static func clampedDot(_ lhs: simd_float3, _ rhs: simd_float3) -> Float {
        min(1.0, max(-1.0, simd_dot(lhs, rhs)))
    }

    private static func shortestAngle(_ radians: Float) -> Float {
        atan2(sin(radians), cos(radians))
    }

    private static func rotationMatrixToRotvec(_ rmat: simd_float3x3) -> simd_float3 {
        let trace = rmat[0, 0] + rmat[1, 1] + rmat[2, 2]
        let cosTheta = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
        let theta = acos(cosTheta)
        if theta < 1e-8 {
            return .zero
        }
        let denom = 2.0 * sin(theta)
        let axis = simd_float3(
            (rmat[2, 1] - rmat[1, 2]) / denom,
            (rmat[0, 2] - rmat[2, 0]) / denom,
            (rmat[1, 0] - rmat[0, 1]) / denom
        )
        return axis * theta
    }

    private static func radiansToDegrees(_ radians: Float) -> Double {
        Double(radians) * 180.0 / .pi
    }

    private static func normalizeDegrees(_ degrees: Double) -> Double {
        var value = degrees.truncatingRemainder(dividingBy: 360.0)
        if value < 0 {
            value += 360.0
        }
        return value
    }

    private static func scaledIntrinsics(
        _ intrinsics: simd_float3x3,
        from source: ImageSizeJSON?,
        to target: ImageSizeJSON
    ) -> simd_float3x3? {
        guard let source, source.width > 0, source.height > 0 else { return nil }
        let scaleX = Float(target.width) / Float(source.width)
        let scaleY = Float(target.height) / Float(source.height)
        var scaled = intrinsics
        scaled[0, 0] *= scaleX
        scaled[1, 1] *= scaleY
        scaled[2, 0] *= scaleX
        scaled[2, 1] *= scaleY
        return scaled
    }

    private static let depthWidth = 256
    private static let depthHeight = 192

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let dateFolderFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    private static let folderTimestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH-mm-ss.SSS'Z'"
        return formatter
    }()

    private static let coinftDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyMMdd"
        return formatter
    }()

    private static let coinftTimestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyMMdd_HHmmss"
        return formatter
    }()

    private static func parseISOSeconds(_ value: String) throws -> TimeInterval {
        guard let date = isoFormatter.date(from: value) else {
            throw RecorderError.invalidTimestamp(value)
        }
        return date.timeIntervalSince1970
    }

    private static func syntheticCoinFTWrench(_ t: TimeInterval, rng: inout DeterministicRNG) -> CoinFTWrench {
        func oneSide(_ phase: Double) -> [Double] {
            let contact = 0.4 + 1.8 * max(0.0, sin(2.0 * .pi * 0.35 * t - 0.6))
            let centers = [1.2, 2.1, 3.0, 4.0, 5.2, 6.5, 8.0, 9.3]
            let tap = centers.reduce(0.0) { partial, center in
                partial + 1.2 * exp(-pow(t - center, 2.0) / (2.0 * pow(0.035, 2.0)))
            }
            return [
                contact + tap + 0.08 * sin(2.0 * .pi * 1.7 * t + phase),
                0.12 * sin(2.0 * .pi * 0.9 * t + phase),
                0.18 * cos(2.0 * .pi * 0.45 * t + phase),
                0.015 * sin(2.0 * .pi * 0.6 * t + phase),
                0.015 * cos(2.0 * .pi * 0.7 * t + phase),
                0.01 * sin(2.0 * .pi * 0.5 * t)
            ].map { $0 + rng.nextGaussian() * 0.015 }
        }
        let left = oneSide(0.0)
        let right = oneSide(0.8)
        return CoinFTWrench(
            leftFx: left[0], leftFy: left[1], leftFz: left[2],
            leftMx: left[3], leftMy: left[4], leftMz: left[5],
            rightFx: right[0], rightFy: right[1], rightFz: right[2],
            rightMx: right[3], rightMy: right[4], rightMz: right[5]
        )
    }

    private static func interpolateWrench(sampleTimes: [TimeInterval], samples: [CoinFTSample], target: TimeInterval) -> CoinFTWrench {
        let upper = sampleTimes.firstIndex { $0 >= target } ?? (sampleTimes.count - 1)
        let lower = max(0, upper - 1)
        if upper == lower {
            return samples[upper].wrench
        }
        let t0 = sampleTimes[lower]
        let t1 = sampleTimes[upper]
        let alpha = max(0.0, min(1.0, (target - t0) / (t1 - t0)))
        return CoinFTWrench.interpolate(samples[lower].wrench, samples[upper].wrench, alpha: alpha)
    }

    private static func nearestSampleIndex(sampleTimes: [TimeInterval], target: TimeInterval) -> Int {
        let upper = sampleTimes.firstIndex { $0 >= target } ?? (sampleTimes.count - 1)
        let lower = max(0, upper - 1)
        return abs(sampleTimes[upper] - target) < abs(sampleTimes[lower] - target) ? upper : lower
    }

    private static func median(_ values: [Double]) -> Double {
        let sorted = values.sorted()
        let mid = sorted.count / 2
        if sorted.count % 2 == 0 {
            return (sorted[mid - 1] + sorted[mid]) / 2.0
        }
        return sorted[mid]
    }

    private static func decimal(_ value: Double) -> String {
        String(format: "%.9f", value)
    }
}

private struct ProcessedDemoJSON: Encodable {
    let sessionName: String
    let side: String
    let type: String
    let poseCoordinateFrame: String
    let calibration: CalibrationJSON?
    let recordingStartTime: String
    let poseTimes: [String]
    let poseTransforms: [[[Double]]]
    let ultrawideRGBTimes: [String]
    let depthTimes: [String]
    let rgbFrameCount: Int
    let ultrawideFrameCount: Int
    let depthFrameCount: Int
    let gripperWidthMeters: [Double]
    let gripperWidthSource: String
    let depthSource: String
    let realDepthFrameCount: Int
    let missingDepthFrameCount: Int
    let rgbCaptureDeviceType: String
    let rgbImageResolution: String
    let rgbImageSize: ImageSizeJSON?
    let rgbCameraIntrinsics: [[Double]]?
    let depthSourceResolution: ImageSizeJSON?
    let depthExportResolution: ImageSizeJSON
    let depthConfidenceResolution: ImageSizeJSON?
    let depthSourceCameraIntrinsics: [[Double]]?
    let depthExportCameraIntrinsics: [[Double]]?
    let depthConfidencePath: String
    let depthConfidenceFrameCount: Int
    let realDepthConfidenceFrameCount: Int
    let depthConfidenceAvailable: Bool
    let depthCoordinateSpace: String
    let depthAlignmentReference: String
    let depthExportSampling: String
    let depthUnit: String
    let depthValueClampMeters: [Double]
    let depthValidPixelRatio: Double
    let depthConfidenceHistogram: [Int]
    let selectedARVideoFormat: ARVideoFormatSummary
    let arkitSupportedVideoFormats: [ARVideoFormatSummary]
    let ultrawideMode: String
    let note: String
}

private struct ImageSizeJSON: Encodable {
    let width: Int
    let height: Int
}

private struct CalibrationSnapshot {
    let tiltErrorDegrees: Double
    let headingSnapErrorDegrees: Double
    let rawHeadingDegrees: Double
    let snappedHeadingDegrees: Double
    let arkitWorldFromSessionWorld: simd_float4x4

    var canCalibrate: Bool {
        tiltErrorDegrees <= 5.0 && headingSnapErrorDegrees <= 20.0
    }

    var jsonValue: CalibrationJSON {
        CalibrationJSON(
            mode: "table_flat_landscape_snap",
            verticalAxisSource: "gravity",
            horizontalAxisSource: "camera_right_projected_to_table_then_snapped_to_90deg",
            positiveXAxisRule: "phone long edge direction while lying landscape on table",
            positiveZAxisRule: "up_from_table",
            tiltErrorDegrees: tiltErrorDegrees,
            headingSnapErrorDegrees: headingSnapErrorDegrees,
            rawHeadingDegrees: rawHeadingDegrees,
            snappedHeadingDegrees: snappedHeadingDegrees,
            arkitWorldFromSessionWorld: DemoRecorder.matrixRows(arkitWorldFromSessionWorld)
        )
    }
}

private struct CalibrationJSON: Encodable {
    let mode: String
    let verticalAxisSource: String
    let horizontalAxisSource: String
    let positiveXAxisRule: String
    let positiveZAxisRule: String
    let tiltErrorDegrees: Double
    let headingSnapErrorDegrees: Double
    let rawHeadingDegrees: Double
    let snappedHeadingDegrees: Double
    let arkitWorldFromSessionWorld: [[Double]]
}

private extension simd_float4 {
    var xyz: simd_float3 {
        simd_float3(x, y, z)
    }
}

private struct ARVideoFormatSummary: Encodable {
    let captureDeviceType: String
    let captureDevicePosition: String
    let width: Int
    let height: Int
    let framesPerSecond: Int

    static let unknown = ARVideoFormatSummary(
        captureDeviceType: "unknown",
        captureDevicePosition: "unknown",
        width: 0,
        height: 0,
        framesPerSecond: 0
    )
}

private enum RecorderError: LocalizedError {
    case cannotAddWriterInput
    case noFrames
    case writerFailed
    case invalidTimestamp(String)

    var errorDescription: String? {
        switch self {
        case .cannotAddWriterInput:
            return "Cannot add AVAssetWriter input."
        case .noFrames:
            return "No AR frames were recorded."
        case .writerFailed:
            return "Video writer failed."
        case .invalidTimestamp(let value):
            return "Invalid timestamp: \(value)"
        }
    }
}

private struct CoinFTSample {
    let sequence: Int
    let deviceTime: TimeInterval
    let receiveTime: TimeInterval
    let latency: TimeInterval
    let wrench: CoinFTWrench

    func rawChannels(for side: CoinFTSensorSide) -> [Double] {
        let sideOffset = side == .left ? 0.0 : 20.0
        return (0..<12).map { channel in
            1000.0 + sideOffset + Double(channel) + Double(sequence % 5) * 0.1
        }
    }
}

private enum CoinFTSensorSide {
    case left
    case right
}

private struct CoinFTWrench {
    let leftFx: Double
    let leftFy: Double
    let leftFz: Double
    let leftMx: Double
    let leftMy: Double
    let leftMz: Double
    let rightFx: Double
    let rightFy: Double
    let rightFz: Double
    let rightMx: Double
    let rightMy: Double
    let rightMz: Double

    var csv: String {
        [
            leftFx, leftFy, leftFz, leftMx, leftMy, leftMz,
            rightFx, rightFy, rightFz, rightMx, rightMy, rightMz
        ].map { String(format: "%.6f", $0) }.joined(separator: ",")
    }

    func values(for side: CoinFTSensorSide) -> [Double] {
        switch side {
        case .left:
            return [leftFx, leftFy, leftFz, leftMx, leftMy, leftMz]
        case .right:
            return [rightFx, rightFy, rightFz, rightMx, rightMy, rightMz]
        }
    }

    static func interpolate(_ lhs: CoinFTWrench, _ rhs: CoinFTWrench, alpha: Double) -> CoinFTWrench {
        func lerp(_ a: Double, _ b: Double) -> Double { a + (b - a) * alpha }
        return CoinFTWrench(
            leftFx: lerp(lhs.leftFx, rhs.leftFx),
            leftFy: lerp(lhs.leftFy, rhs.leftFy),
            leftFz: lerp(lhs.leftFz, rhs.leftFz),
            leftMx: lerp(lhs.leftMx, rhs.leftMx),
            leftMy: lerp(lhs.leftMy, rhs.leftMy),
            leftMz: lerp(lhs.leftMz, rhs.leftMz),
            rightFx: lerp(lhs.rightFx, rhs.rightFx),
            rightFy: lerp(lhs.rightFy, rhs.rightFy),
            rightFz: lerp(lhs.rightFz, rhs.rightFz),
            rightMx: lerp(lhs.rightMx, rhs.rightMx),
            rightMy: lerp(lhs.rightMy, rhs.rightMy),
            rightMz: lerp(lhs.rightMz, rhs.rightMz)
        )
    }
}

private struct CoinFTSyncMetadata: Encodable {
    let source: String
    let sampleRateHz: Double
    let fixedLatencySeconds: Double
    let jitterStdSeconds: Double
    let estimatedLatencySeconds: Double
    let sampleCount: Int
    let frameCount: Int
    let timestampMode: String
    let streamCsv: String
    let alignedCsv: String
    let note: String
}

private struct DeterministicRNG {
    private var state: UInt64

    init(seed: UInt64) {
        self.state = seed
    }

    mutating func nextUnit() -> Double {
        state = 2862933555777941757 &* state &+ 3037000493
        let bits = state >> 11
        return Double(bits) / Double(1 << 53)
    }

    mutating func nextGaussian() -> Double {
        let u1 = max(nextUnit(), 1e-12)
        let u2 = nextUnit()
        return sqrt(-2.0 * log(u1)) * cos(2.0 * .pi * u2)
    }
}

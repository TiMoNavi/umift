@preconcurrency import AVFoundation
import Foundation
import UIKit

final class DualCameraCaptureManager: NSObject, ObservableObject {
    let session = AVCaptureMultiCamSession()

    @Published var wideStatus = "idle"
    @Published var ultraStatus = "idle"
    @Published var torchStatus = "off"
    @Published var receiverStatus = "disconnected"
    @Published var wideFrameText = "no frame"
    @Published var ultraFrameText = "no frame"
    @Published var wideTimestampNS: Int64?
    @Published var ultraTimestampNS: Int64?
    @Published var syncErrorMS: Double?
    @Published var syncValid = false
    @Published var bufferDurationS: Double = 0
    @Published var torchRequested = false
    @Published var capabilityText = AVCaptureMultiCamSession.isMultiCamSupported ? "multicam supported" : "multicam unsupported"
    @Published var gripperVisionState = GripperVisionState.empty
    @Published var isSessionRunning = false

    private let sessionQueue = DispatchQueue(label: "local.umift.dual-capture.session")
    private let wideSampleQueue = DispatchQueue(label: "local.umift.dual-capture.wide-sample")
    private let ultraSampleQueue = DispatchQueue(label: "local.umift.dual-capture.ultra-sample")
    private let gripperVisionProcessor = GripperVisionProcessor()

    private enum CaptureGraphMode {
        case dualWideUltra
        case ultraOnlyForAR

        var debugName: String {
            switch self {
            case .dualWideUltra:
                return "dualWideUltra"
            case .ultraOnlyForAR:
                return "ultraOnlyForAR"
            }
        }
    }

    private var configured = false
    private var configuredMode: CaptureGraphMode?
    private var wideDevice: AVCaptureDevice?
    private var ultraDevice: AVCaptureDevice?
    private var wideInput: AVCaptureDeviceInput?
    private var ultraInput: AVCaptureDeviceInput?
    private var wideInputPort: AVCaptureInput.Port?
    private var ultraInputPort: AVCaptureInput.Port?
    private var wideOutput: AVCaptureVideoDataOutput?
    private var ultraOutput: AVCaptureVideoDataOutput?
    private weak var widePreviewLayer: AVCaptureVideoPreviewLayer?
    private weak var ultraPreviewLayer: AVCaptureVideoPreviewLayer?
    private var widePreviewConnection: AVCaptureConnection?
    private var ultraPreviewConnection: AVCaptureConnection?
    private var wideFrameCount = 0
    private var ultraFrameCount = 0
    private var lastWidePublishAt = Date.distantPast
    private var lastUltraPublishAt = Date.distantPast
    private var torchRequestedOn = false
    private var torchKeepAliveTimer: DispatchSourceTimer?

    deinit {
        sessionQueue.sync {
            stopTorchKeepAlive()
            if session.isRunning {
                session.stopRunning()
            }
        }
    }

    func start() {
        start(mode: .dualWideUltra)
    }

    func startUltraOnlyForAR() {
        start(mode: .ultraOnlyForAR)
    }

    private func start(mode: CaptureGraphMode) {
        print("[UMIFT][camera] start requested mode=\(mode.debugName)")
        AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
            guard let self else { return }
            self.sessionQueue.async {
                print("[UMIFT][camera] start on queue mode=\(mode.debugName) granted=\(granted) configured=\(self.configured) configuredMode=\(self.configuredMode?.debugName ?? "nil") running=\(self.session.isRunning)")
                guard granted else {
                    self.publish {
                        self.wideStatus = "camera denied"
                        self.ultraStatus = "camera denied"
                        self.capabilityText = "camera permission denied"
                        self.isSessionRunning = false
                    }
                    return
                }

                guard AVCaptureMultiCamSession.isMultiCamSupported else {
                    self.publish {
                        self.wideStatus = "multicam unsupported"
                        self.ultraStatus = "multicam unsupported"
                        self.capabilityText = "multicam unsupported"
                        self.isSessionRunning = false
                    }
                    return
                }

                if self.configuredMode != mode {
                    print("[UMIFT][camera] reconfigure graph \(self.configuredMode?.debugName ?? "nil")->\(mode.debugName)")
                    self.setTorch(enabled: false)
                    if self.session.isRunning {
                        self.session.stopRunning()
                    }
                    self.teardownSessionGraph()
                }

                if !self.configured {
                    print("[UMIFT][camera] configure graph mode=\(mode.debugName)")
                    self.configureSession(mode: mode)
                }

                guard self.configured else {
                    self.publish {
                        self.isSessionRunning = false
                    }
                    return
                }

                if !self.session.isRunning {
                    self.session.startRunning()
                }
                let sessionIsRunning = self.session.isRunning
                print("[UMIFT][camera] session started mode=\(mode.debugName) running=\(sessionIsRunning)")
                self.setTorch(enabled: true)
                self.publish {
                    UIApplication.shared.isIdleTimerDisabled = true
                    self.wideStatus = mode == .ultraOnlyForAR ? "arkit wide" : (self.wideDevice == nil ? "missing" : "running")
                    self.ultraStatus = self.ultraDevice == nil ? "missing" : "running"
                    self.capabilityText = mode == .ultraOnlyForAR ? "ultra-only running for arkit" : "multicam running"
                    self.isSessionRunning = sessionIsRunning
                }
            }
        }
    }

    func stop(completion: (() -> Void)? = nil) {
        print("[UMIFT][camera] stop requested")
        sessionQueue.async { [weak self] in
            guard let self else { return }
            print("[UMIFT][camera] stop on queue running=\(self.session.isRunning)")
            self.setTorch(enabled: false)
            if self.session.isRunning {
                self.session.stopRunning()
            }
            self.publish {
                UIApplication.shared.isIdleTimerDisabled = false
                self.wideStatus = "stopped"
                self.ultraStatus = "stopped"
                self.capabilityText = "multicam stopped"
                self.isSessionRunning = false
                completion?()
            }
        }
    }

    func restartAfterARRelease() {
        print("[UMIFT][camera] restartAfterARRelease requested")
        AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
            guard let self else { return }
            self.sessionQueue.async {
                print("[UMIFT][camera] restartAfterARRelease on queue granted=\(granted) running=\(self.session.isRunning)")
                guard granted else {
                    self.publish {
                        self.wideStatus = "camera denied"
                        self.ultraStatus = "camera denied"
                        self.capabilityText = "camera permission denied"
                        self.isSessionRunning = false
                    }
                    return
                }

                self.publish {
                    self.wideStatus = "rebuilding"
                    self.ultraStatus = "rebuilding"
                    self.capabilityText = "multicam rebuilding"
                    self.isSessionRunning = false
                }

                self.setTorch(enabled: false)
                if self.session.isRunning {
                    self.session.stopRunning()
                }
                self.teardownSessionGraph()
                self.configureSession(mode: .dualWideUltra)

                guard self.configured else {
                    self.publish {
                        self.wideStatus = "rebuild failed"
                        self.ultraStatus = "rebuild failed"
                        self.capabilityText = "multicam rebuild failed"
                        self.isSessionRunning = false
                    }
                    return
                }

                self.session.startRunning()
                let sessionIsRunning = self.session.isRunning
                print("[UMIFT][camera] restartAfterARRelease started running=\(sessionIsRunning)")
                self.setTorch(enabled: true)
                self.publish {
                    UIApplication.shared.isIdleTimerDisabled = true
                    self.wideStatus = self.wideDevice == nil ? "missing" : "running"
                    self.ultraStatus = self.ultraDevice == nil ? "missing" : "running"
                    self.capabilityText = "multicam running"
                    self.isSessionRunning = sessionIsRunning
                }
            }
        }
    }

    func attachPreviewLayers(wideLayer: AVCaptureVideoPreviewLayer, ultraLayer: AVCaptureVideoPreviewLayer) {
        sessionQueue.async { [weak self, weak wideLayer, weak ultraLayer] in
            guard let self, let wideLayer, let ultraLayer else { return }
            let sameLayers = self.widePreviewLayer === wideLayer && self.ultraPreviewLayer === ultraLayer
            if sameLayers,
               self.ultraPreviewConnection != nil,
               self.configuredMode == .ultraOnlyForAR || self.widePreviewConnection != nil {
                return
            }
            self.widePreviewLayer = wideLayer
            self.ultraPreviewLayer = ultraLayer
            self.connectPreviewLayersIfPossible()
        }
    }

    func startGripperPathCalibration() {
        ultraSampleQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.startSweepCalibration()
            self.publish {
                self.gripperVisionState = nextState
            }
        }
    }

    func finishGripperPathCalibration() {
        ultraSampleQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.finishSweepCalibration()
            self.publish {
                self.gripperVisionState = nextState
            }
        }
    }

    func cancelGripperPathCalibration() {
        ultraSampleQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.cancelSweepCalibration()
            self.publish {
                self.gripperVisionState = nextState
            }
        }
    }

    func resetGripperPathCalibration() {
        ultraSampleQueue.async { [weak self] in
            guard let self else { return }
            let nextState = self.gripperVisionProcessor.resetCalibration()
            self.publish {
                self.gripperVisionState = nextState
            }
        }
    }

    func setTorchEnabled(_ enabled: Bool) {
        sessionQueue.async { [weak self] in
            self?.setTorch(enabled: enabled)
        }
    }

    func applyARWideFrame(timestampNS: Int64?, frameText: String) {
        publish {
            self.wideTimestampNS = timestampNS
            self.wideFrameText = frameText
            self.updateSyncStateOnMain()
        }
    }

    private func configureSession(mode: CaptureGraphMode) {
        session.beginConfiguration()
        defer {
            session.commitConfiguration()
        }

        if session.canSetSessionPreset(.inputPriority) {
            session.sessionPreset = .inputPriority
        }

        guard let ultraDevice = Self.device(type: .builtInUltraWideCamera) else {
            publish {
                self.wideStatus = mode == .ultraOnlyForAR ? "arkit wide" : (Self.device(type: .builtInWideAngleCamera) == nil ? "wide missing" : "wide found")
                self.ultraStatus = "ultra missing"
                self.capabilityText = "camera missing"
            }
            return
        }

        self.ultraDevice = ultraDevice
        configureDevice(ultraDevice, preferredFPS: 60)

        do {
            if mode == .dualWideUltra {
                guard let wideDevice = Self.device(type: .builtInWideAngleCamera) else {
                    publish {
                        self.wideStatus = "wide missing"
                        self.ultraStatus = "ultra found"
                        self.capabilityText = "camera missing"
                    }
                    return
                }
                self.wideDevice = wideDevice
                configureDevice(wideDevice, preferredFPS: 30)
                try addCamera(device: wideDevice, source: .wide)
            } else {
                self.wideDevice = nil
                publish {
                    self.wideStatus = "arkit wide"
                    self.wideFrameText = "ar waiting"
                }
            }
            try addCamera(device: ultraDevice, source: .ultra)
            configured = true
            configuredMode = mode
            connectPreviewLayersIfPossible()
            publishActiveFormats()
        } catch {
            publish {
                self.capabilityText = "configure failed"
                self.wideStatus = "config failed"
                self.ultraStatus = error.localizedDescription
            }
        }
    }

    private func teardownSessionGraph() {
        wideOutput?.setSampleBufferDelegate(nil, queue: nil)
        ultraOutput?.setSampleBufferDelegate(nil, queue: nil)

        session.beginConfiguration()
        for connection in session.connections {
            session.removeConnection(connection)
        }
        for output in session.outputs {
            session.removeOutput(output)
        }
        for input in session.inputs {
            session.removeInput(input)
        }
        session.commitConfiguration()

        configured = false
        configuredMode = nil
        wideDevice = nil
        ultraDevice = nil
        wideInput = nil
        ultraInput = nil
        wideInputPort = nil
        ultraInputPort = nil
        wideOutput = nil
        ultraOutput = nil
        widePreviewConnection = nil
        ultraPreviewConnection = nil
        wideFrameCount = 0
        ultraFrameCount = 0
        lastWidePublishAt = Date.distantPast
        lastUltraPublishAt = Date.distantPast
        publish {
            self.wideFrameText = "no frame"
            self.ultraFrameText = "no frame"
            self.wideTimestampNS = nil
            self.ultraTimestampNS = nil
            self.syncErrorMS = nil
            self.syncValid = false
            self.bufferDurationS = 0
        }
    }

    private func addCamera(device: AVCaptureDevice, source: PreviewSource) throws {
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            throw CaptureSetupError.cannotAddInput(source.rawValue)
        }
        session.addInputWithNoConnections(input)

        guard let port = input.ports(
            for: .video,
            sourceDeviceType: device.deviceType,
            sourceDevicePosition: device.position
        ).first else {
            throw CaptureSetupError.missingInputPort(source.rawValue)
        }

        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA)
        ]
        output.setSampleBufferDelegate(self, queue: source == .wide ? wideSampleQueue : ultraSampleQueue)

        guard session.canAddOutput(output) else {
            throw CaptureSetupError.cannotAddOutput(source.rawValue)
        }
        session.addOutputWithNoConnections(output)

        let connection = AVCaptureConnection(inputPorts: [port], output: output)
        configure(connection: connection)
        guard session.canAddConnection(connection) else {
            throw CaptureSetupError.cannotAddConnection(source.rawValue)
        }
        session.addConnection(connection)

        if connection.isCameraIntrinsicMatrixDeliverySupported {
            connection.isCameraIntrinsicMatrixDeliveryEnabled = true
        }

        switch source {
        case .wide:
            wideInput = input
            wideInputPort = port
            wideOutput = output
        case .ultra:
            ultraInput = input
            ultraInputPort = port
            ultraOutput = output
        }
    }

    private func connectPreviewLayersIfPossible() {
        guard configured,
              let ultraInputPort,
              let ultraPreviewLayer else { return }

        session.beginConfiguration()
        defer {
            session.commitConfiguration()
        }

        removeConnectionIfAttached(widePreviewConnection)
        removeConnectionIfAttached(ultraPreviewConnection)
        widePreviewConnection = nil
        ultraPreviewConnection = nil

        if let wideInputPort, let widePreviewLayer {
            let wideConnection = AVCaptureConnection(inputPort: wideInputPort, videoPreviewLayer: widePreviewLayer)
            configure(connection: wideConnection)
            if session.canAddConnection(wideConnection) {
                session.addConnection(wideConnection)
                widePreviewConnection = wideConnection
            }
        }

        let ultraConnection = AVCaptureConnection(inputPort: ultraInputPort, videoPreviewLayer: ultraPreviewLayer)
        configure(connection: ultraConnection)
        if session.canAddConnection(ultraConnection) {
            session.addConnection(ultraConnection)
            ultraPreviewConnection = ultraConnection
        }
    }

    private func removeConnectionIfAttached(_ connection: AVCaptureConnection?) {
        guard let connection else { return }
        guard session.connections.contains(where: { $0 === connection }) else { return }
        session.removeConnection(connection)
    }

    private func configure(connection: AVCaptureConnection) {
        if connection.isVideoOrientationSupported {
            connection.videoOrientation = .landscapeRight
        } else if #available(iOS 17.0, *), connection.isVideoRotationAngleSupported(0) {
            connection.videoRotationAngle = 0
        }
    }

    private func configureDevice(_ device: AVCaptureDevice, preferredFPS: Double) {
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }

            if let format = Self.preferredFourByThreeFormat(for: device, preferredFPS: preferredFPS) {
                device.activeFormat = format
            }
            if device.isRampingVideoZoom {
                device.cancelVideoZoomRamp()
            }
            device.videoZoomFactor = device.minAvailableVideoZoomFactor

            if let fps = Self.bestFrameRate(for: device.activeFormat, preferredFPS: preferredFPS) {
                let timescale = CMTimeScale(max(1, Int32(fps.rounded())))
                device.activeVideoMinFrameDuration = CMTime(value: 1, timescale: timescale)
                device.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: timescale)
            }
        } catch {
            publish {
                self.capabilityText = "format config failed"
            }
        }
    }

    private func setTorch(enabled: Bool) {
        torchRequestedOn = enabled
        publish { self.torchRequested = enabled }
        applyTorch(enabled: enabled, force: true)
        if enabled {
            startTorchKeepAlive()
        } else {
            stopTorchKeepAlive()
        }
        guard enabled else { return }

        for delay in [0.25, 0.75, 1.5, 2.5] {
            sessionQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
                guard let self, self.torchRequestedOn else { return }
                self.applyTorch(enabled: true, force: delay <= 0.75)
            }
        }
    }

    private func startTorchKeepAlive() {
        guard torchKeepAliveTimer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: sessionQueue)
        timer.schedule(deadline: .now() + 1.0, repeating: 1.0)
        timer.setEventHandler { [weak self] in
            guard let self, self.torchRequestedOn else { return }
            self.applyTorch(enabled: true, force: false)
        }
        torchKeepAliveTimer = timer
        timer.resume()
    }

    private func stopTorchKeepAlive() {
        torchKeepAliveTimer?.cancel()
        torchKeepAliveTimer = nil
    }

    private func applyTorch(enabled: Bool, force: Bool) {
        let status = Self.applyTorch(enabled: enabled, captureDevice: wideDevice ?? ultraDevice, force: force)
        publish { self.torchStatus = status }
    }

    private func publishActiveFormats() {
        let wideText = configuredMode == .ultraOnlyForAR
            ? "arkit wide"
            : (wideDevice.map { Self.formatText(for: $0, prefix: "wide") } ?? "wide missing")
        let ultraText = ultraDevice.map { Self.formatText(for: $0, prefix: "ultra") } ?? "ultra missing"
        publish {
            self.wideStatus = wideText
            self.ultraStatus = ultraText
        }
    }

    private func publish(_ update: @escaping () -> Void) {
        DispatchQueue.main.async(execute: update)
    }
}

extension DualCameraCaptureManager: AVCaptureVideoDataOutputSampleBufferDelegate {
    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        let timestampNS = Self.timestampNS(for: sampleBuffer)
        let dimensions = Self.dimensionsText(for: sampleBuffer)
        let now = Date()

        if let wideOutput, output === wideOutput {
            wideFrameCount += 1
            guard now.timeIntervalSince(lastWidePublishAt) > 0.10 else { return }
            lastWidePublishAt = now
            publish {
                self.wideTimestampNS = timestampNS
                self.wideFrameText = "\(dimensions) #\(self.wideFrameCount)"
                self.updateSyncStateOnMain()
            }
            return
        }

        if let ultraOutput, output === ultraOutput {
            ultraFrameCount += 1
            let visionState = gripperVisionProcessor.process(sampleBuffer: sampleBuffer)
            let shouldPublish = now.timeIntervalSince(lastUltraPublishAt) > 0.033 || visionState != nil
            guard shouldPublish else { return }
            lastUltraPublishAt = now
            publish {
                self.ultraTimestampNS = timestampNS
                self.ultraFrameText = "\(dimensions) #\(self.ultraFrameCount)"
                if let visionState {
                    self.gripperVisionState = visionState
                }
                self.updateSyncStateOnMain()
            }
        }
    }

    private func updateSyncStateOnMain() {
        guard let wideTimestampNS, let ultraTimestampNS else {
            syncValid = false
            syncErrorMS = nil
            return
        }
        let errorMS = abs(Double(wideTimestampNS - ultraTimestampNS)) / 1_000_000.0
        syncErrorMS = errorMS
        syncValid = errorMS <= 20
        bufferDurationS = min(3.0, bufferDurationS + 0.1)
    }
}

private extension DualCameraCaptureManager {
    enum CaptureSetupError: LocalizedError {
        case cannotAddInput(String)
        case missingInputPort(String)
        case cannotAddOutput(String)
        case cannotAddConnection(String)

        var errorDescription: String? {
            switch self {
            case .cannotAddInput(let source):
                return "cannot add \(source) input"
            case .missingInputPort(let source):
                return "missing \(source) input port"
            case .cannotAddOutput(let source):
                return "cannot add \(source) output"
            case .cannotAddConnection(let source):
                return "cannot add \(source) connection"
            }
        }
    }

    static func device(type: AVCaptureDevice.DeviceType) -> AVCaptureDevice? {
        AVCaptureDevice.default(type, for: .video, position: .back)
    }

    static func applyTorch(enabled: Bool, captureDevice: AVCaptureDevice?, force: Bool) -> String {
        guard let captureDevice else { return "torch no device" }
        guard let device = preferredTorchDevice(captureDevice: captureDevice) else {
            return "torch unsupported \(shortName(for: captureDevice))"
        }

        let routeText = device === captureDevice
            ? shortName(for: device)
            : "\(shortName(for: device)) via \(shortName(for: captureDevice))"

        if enabled, !force, device.isTorchActive, device.torchMode == .on {
            return String(
                format: "torch on keep %@ avail%@ mode%@ active%@",
                routeText,
                device.isTorchAvailable ? "1" : "0",
                torchModeText(device.torchMode),
                device.isTorchActive ? "1" : "0"
            )
        }

        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            if enabled {
                guard device.isTorchModeSupported(.on), device.isTorchAvailable else {
                    return "torch unavailable \(routeText)"
                }
                let level = min(1.0, AVCaptureDevice.maxAvailableTorchLevel)
                try device.setTorchModeOn(level: level)
                if !device.isTorchActive, device.isTorchModeSupported(.on) {
                    device.torchMode = .on
                }
                usleep(60_000)
                return String(
                    format: "torch on %.2f %@ avail%@ mode%@ active%@",
                    level,
                    routeText,
                    device.isTorchAvailable ? "1" : "0",
                    torchModeText(device.torchMode),
                    device.isTorchActive ? "1" : "0"
                )
            } else {
                if device.isTorchModeSupported(.off) {
                    device.torchMode = .off
                }
                return "torch off \(routeText)"
            }
        } catch {
            return "torch failed \(error.localizedDescription)"
        }
    }

    static func preferredTorchDevice(captureDevice: AVCaptureDevice) -> AVCaptureDevice? {
        if captureDevice.hasTorch, captureDevice.isTorchModeSupported(.on) {
            return captureDevice
        }

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

        if let device = discovery.devices.first(where: { $0.hasTorch && $0.isTorchModeSupported(.on) }) {
            return device
        }
        let fallback = AVCaptureDevice.default(for: .video)
        if fallback?.position == .back,
           fallback?.hasTorch == true,
           fallback?.isTorchModeSupported(.on) == true {
            return fallback
        }
        return nil
    }

    static func shortName(for device: AVCaptureDevice) -> String {
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

    static func torchModeText(_ mode: AVCaptureDevice.TorchMode) -> String {
        switch mode {
        case .off:
            return "off"
        case .on:
            return "on"
        case .auto:
            return "auto"
        @unknown default:
            return "unknown"
        }
    }

    static func preferredFourByThreeFormat(for device: AVCaptureDevice, preferredFPS: Double) -> AVCaptureDevice.Format? {
        let formats = device.formats.filter {
            let dims = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
            guard dims.width > 0, dims.height > 0 else { return false }
            let ratio = Double(dims.width) / Double(dims.height)
            return abs(ratio - (4.0 / 3.0)) < 0.035
        }

        let fpsFormats = formats.filter { supportsFrameRate(preferredFPS, format: $0) }
        let pool = fpsFormats.isEmpty ? formats : fpsFormats
        return pool.sorted {
            let lhs = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
            let rhs = CMVideoFormatDescriptionGetDimensions($1.formatDescription)
            let lhsPixels = Int(lhs.width) * Int(lhs.height)
            let rhsPixels = Int(rhs.width) * Int(rhs.height)
            let targetPixels = 1920 * 1440
            let lhsDistance = abs(lhsPixels - targetPixels)
            let rhsDistance = abs(rhsPixels - targetPixels)
            if lhsDistance != rhsDistance {
                return lhsDistance < rhsDistance
            }
            return lhsPixels > rhsPixels
        }.first
    }

    static func supportsFrameRate(_ fps: Double, format: AVCaptureDevice.Format) -> Bool {
        format.videoSupportedFrameRateRanges.contains { range in
            range.minFrameRate <= fps && range.maxFrameRate >= fps
        }
    }

    static func bestFrameRate(for format: AVCaptureDevice.Format, preferredFPS: Double) -> Double? {
        if supportsFrameRate(preferredFPS, format: format) {
            return preferredFPS
        }
        if supportsFrameRate(60, format: format) {
            return 60
        }
        if supportsFrameRate(30, format: format) {
            return 30
        }
        return format.videoSupportedFrameRateRanges.map(\.maxFrameRate).filter { $0 > 0 }.max()
    }

    static func formatText(for device: AVCaptureDevice, prefix: String) -> String {
        let dims = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
        let fps = bestFrameRate(for: device.activeFormat, preferredFPS: 30).map { "\(Int($0.rounded()))fps" } ?? "fps?"
        return "\(prefix) \(dims.width)x\(dims.height) \(fps)"
    }

    static func timestampNS(for sampleBuffer: CMSampleBuffer) -> Int64 {
        let seconds = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        guard seconds.isFinite else { return 0 }
        return Int64(seconds * 1_000_000_000)
    }

    static func dimensionsText(for sampleBuffer: CMSampleBuffer) -> String {
        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            return "frame --"
        }
        return "\(CVPixelBufferGetWidth(imageBuffer))x\(CVPixelBufferGetHeight(imageBuffer))"
    }
}

import AVFoundation
import CoreImage
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

    private let processingQueue = DispatchQueue(label: "local.umift.dual-capture.ar-processing")
    private let torchQueue = DispatchQueue(label: "local.umift.dual-capture.ar-torch")
    private let gripperVisionProcessor = GripperVisionProcessor()
    private var ultraFrameCount = 0
    private var lastUltraTimestampNS: Int64?
    private var lastGripperDebugLogNS: Int64 = 0
    private var torchRequestedOn = true
    private var torchKeepAliveTimer: DispatchSourceTimer?

    deinit {
        torchQueue.sync {
            stopTorchKeepAlive()
            _ = Self.applyTorch(enabled: false, force: true)
        }
    }

    func applyARStatus(_ status: WorldCalibrationARStatus) {
        wideStatus = status.statusText
    }

    func applyARWideFrame(_ state: WorldTrackingARFrameState) {
        wideTimestampNS = state.timestampNS
        wideFrameText = state.rgbText
        wideStatus = "arkit \(state.trackingText)"
        capabilityText = "arkit world + private uw"
        updateSyncState()
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
                self.logGripperDebugIfNeeded(state: nextState, timestampNS: timestampNS)
            }
        }
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
            return
        }
        let errorMS = abs(Double(wideTimestampNS - ultraTimestampNS)) / 1_000_000.0
        syncErrorMS = errorMS
        syncValid = errorMS <= 120.0
        bufferDurationS = min(3.0, bufferDurationS + 0.1)
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

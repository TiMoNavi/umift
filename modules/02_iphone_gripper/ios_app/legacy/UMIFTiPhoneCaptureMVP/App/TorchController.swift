import AVFoundation
import Foundation

@MainActor
final class TorchController {
    static let shared = TorchController()

    private var currentDevice: AVCaptureDevice?
    private(set) var isRequestedOn = false
    private(set) var lastStatus = "torch --"

    private init() {}

    @discardableResult
    func setEnabled(_ enabled: Bool, reason: String = "", preferredDevice: AVCaptureDevice? = nil) -> String {
        isRequestedOn = enabled

        guard let device = selectTorchDevice(preferredDevice: preferredDevice) else {
            lastStatus = enabled ? "torch no device" : "torch off"
            return lastStatus
        }
        currentDevice = device

        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }

            if enabled {
                guard device.hasTorch, device.isTorchModeSupported(.on) else {
                    lastStatus = "torch unsupported"
                    return lastStatus
                }
                let level = min(1.0, AVCaptureDevice.maxAvailableTorchLevel)
                try device.setTorchModeOn(level: level)
                if !device.isTorchActive, device.isTorchModeSupported(.on) {
                    device.torchMode = .on
                }
                lastStatus = String(
                    format: "torch on %.2f %@ avail%@ mode%@ active%@",
                    level,
                    shortName(for: device),
                    device.isTorchAvailable ? "1" : "0",
                    Self.modeText(device.torchMode),
                    device.isTorchActive ? "1" : "0"
                )
            } else {
            if device.hasTorch, device.isTorchModeSupported(.off) {
                device.torchMode = .off
            }
            lastStatus = "torch off \(shortName(for: device))"
            }
        } catch {
            lastStatus = "torch failed"
        }

        return lastStatus
    }

    @discardableResult
    func ensureOnIfRequested(reason: String = "", preferredDevice: AVCaptureDevice? = nil) -> String {
        guard isRequestedOn else { return lastStatus }
        if let currentDevice, currentDevice.isTorchActive {
            lastStatus = lastStatus.hasPrefix("torch on") ? lastStatus : "torch on \(shortName(for: currentDevice))"
            return lastStatus
        }
        return setEnabled(true, reason: reason, preferredDevice: preferredDevice)
    }

    private func selectTorchDevice(preferredDevice: AVCaptureDevice?) -> AVCaptureDevice? {
        if let preferredDevice, preferredDevice.hasTorch, preferredDevice.isTorchModeSupported(.on) {
            return preferredDevice
        }

        if let currentDevice, currentDevice.hasTorch, currentDevice.isTorchModeSupported(.on) {
            return currentDevice
        }

        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [
                .builtInTripleCamera,
                .builtInDualWideCamera,
                .builtInDualCamera,
                .builtInWideAngleCamera,
                .builtInUltraWideCamera
            ],
            mediaType: .video,
            position: .back
        )

        if let device = discovery.devices.first(where: { $0.hasTorch && $0.isTorchModeSupported(.on) }) {
            return device
        }

        let defaultDevice = AVCaptureDevice.default(for: .video)
        if defaultDevice?.position == .back, defaultDevice?.hasTorch == true {
            return defaultDevice
        }
        return nil
    }

    private func shortName(for device: AVCaptureDevice) -> String {
        switch device.deviceType {
        case .builtInWideAngleCamera:
            return "wide"
        case .builtInUltraWideCamera:
            return "ultra"
        case .builtInTripleCamera:
            return "triple"
        case .builtInDualWideCamera:
            return "dualwide"
        case .builtInDualCamera:
            return "dual"
        default:
            return "back"
        }
    }

    private static func modeText(_ mode: AVCaptureDevice.TorchMode) -> String {
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
}

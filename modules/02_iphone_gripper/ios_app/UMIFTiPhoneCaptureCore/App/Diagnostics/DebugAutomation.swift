import Foundation

enum DebugAutomation {
    private static let environment = ProcessInfo.processInfo.environment

    static let autoStartCalibrationOnLaunch = boolEnvironment("UMIFT_DEBUG_AUTO_CALIBRATE")
    static let skipGripperCalibrationOnLaunch = boolEnvironment("UMIFT_DEBUG_SKIP_GRIPPER_CALIBRATION")
    static let forceCameraWorldOrigin = boolEnvironment("UMIFT_DEBUG_FORCE_CAMERA_WORLD_ORIGIN")
    static let autoMarkWorldOrigin = true
    static let keepARKitRunningAfterCalibration = true
    static let arFrameWatchdogEnabled = true
    static let arFrameWatchdogTimeout: TimeInterval = 3.0
    static let autoMarkRetryInterval: TimeInterval = 1.0
    static let launchCalibrationDelay: TimeInterval = 0.65
    static let skipUltraRestartAfterWorldCalibration = true
    static let autoRecordDurationS: TimeInterval? = {
        guard let text = environment["UMIFT_DEBUG_AUTO_RECORD_SECONDS"],
              let value = TimeInterval(text),
              value > 0
        else { return nil }
        return value
    }()

    static var statusText: String {
        let launchText: String
        if skipGripperCalibrationOnLaunch {
            launchText = "skip grip"
        } else {
            launchText = autoStartCalibrationOnLaunch ? "auto cal" : "manual"
        }
        let markText: String
        if forceCameraWorldOrigin {
            markText = "forced camera origin"
        } else {
            markText = autoMarkWorldOrigin ? "auto mark" : "manual mark"
        }
        let arText = keepARKitRunningAfterCalibration ? "ar continuous" : "ar release"
        let watchdogText = arFrameWatchdogEnabled ? "watchdog" : "no watchdog"
        return "\(launchText), \(markText), \(arText), \(watchdogText)"
    }

    private static func boolEnvironment(_ name: String) -> Bool {
        guard let value = environment[name]?.lowercased() else { return false }
        return value == "1" || value == "true" || value == "yes"
    }
}

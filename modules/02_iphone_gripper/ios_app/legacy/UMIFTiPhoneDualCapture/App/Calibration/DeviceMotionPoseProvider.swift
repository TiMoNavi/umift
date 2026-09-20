import CoreMotion
import Foundation
import simd

struct MotionPoseState: Equatable {
    var isAvailable: Bool
    var statusText: String
    var timestamp: TimeInterval?
    var rollDeg: Double?
    var pitchDeg: Double?
    var yawDeg: Double?
    var gravityX: Double?
    var gravityY: Double?
    var gravityZ: Double?
    var attitudeQX: Double?
    var attitudeQY: Double?
    var attitudeQZ: Double?
    var attitudeQW: Double?
    var userAccelerationXG: Double?
    var userAccelerationYG: Double?
    var userAccelerationZG: Double?
    var rotationRateX: Double?
    var rotationRateY: Double?
    var rotationRateZ: Double?

    static let idle = MotionPoseState(
        isAvailable: false,
        statusText: "motion idle",
        timestamp: nil,
        rollDeg: nil,
        pitchDeg: nil,
        yawDeg: nil,
        gravityX: nil,
        gravityY: nil,
        gravityZ: nil,
        attitudeQX: nil,
        attitudeQY: nil,
        attitudeQZ: nil,
        attitudeQW: nil,
        userAccelerationXG: nil,
        userAccelerationYG: nil,
        userAccelerationZG: nil,
        rotationRateX: nil,
        rotationRateY: nil,
        rotationRateZ: nil
    )

    var poseSummary: String {
        guard isAvailable,
              let rollDeg,
              let pitchDeg,
              let yawDeg
        else {
            return "att -- pos --"
        }
        return String(format: "att r%.1f p%.1f y%.1f pos --", rollDeg, pitchDeg, yawDeg)
    }

    var gravitySummary: String {
        guard let gravityX, let gravityY, let gravityZ else { return "g --" }
        return String(format: "g %.2f %.2f %.2f", gravityX, gravityY, gravityZ)
    }

    var attitudeQuaternion: simd_quatd? {
        guard let attitudeQX,
              let attitudeQY,
              let attitudeQZ,
              let attitudeQW
        else { return nil }
        return simd_normalize(simd_quatd(ix: attitudeQX, iy: attitudeQY, iz: attitudeQZ, r: attitudeQW))
    }

    var userAccelerationG: SIMD3<Double>? {
        guard let userAccelerationXG,
              let userAccelerationYG,
              let userAccelerationZG
        else { return nil }
        return SIMD3<Double>(userAccelerationXG, userAccelerationYG, userAccelerationZG)
    }

    var rotationRateMagnitude: Double? {
        guard let rotationRateX,
              let rotationRateY,
              let rotationRateZ
        else { return nil }
        return sqrt(rotationRateX * rotationRateX + rotationRateY * rotationRateY + rotationRateZ * rotationRateZ)
    }

    func yawDeviationDegrees(from referenceYawDeg: Double?) -> Double? {
        guard let referenceYawDeg, let yawDeg else { return nil }
        return Self.shortestAngleDegrees(yawDeg - referenceYawDeg)
    }

    static func from(deviceMotion: CMDeviceMotion) -> MotionPoseState {
        let quaternion = deviceMotion.attitude.quaternion
        return MotionPoseState(
            isAvailable: true,
            statusText: "motion running",
            timestamp: deviceMotion.timestamp,
            rollDeg: radiansToDegrees(deviceMotion.attitude.roll),
            pitchDeg: radiansToDegrees(deviceMotion.attitude.pitch),
            yawDeg: radiansToDegrees(deviceMotion.attitude.yaw),
            gravityX: deviceMotion.gravity.x,
            gravityY: deviceMotion.gravity.y,
            gravityZ: deviceMotion.gravity.z,
            attitudeQX: quaternion.x,
            attitudeQY: quaternion.y,
            attitudeQZ: quaternion.z,
            attitudeQW: quaternion.w,
            userAccelerationXG: deviceMotion.userAcceleration.x,
            userAccelerationYG: deviceMotion.userAcceleration.y,
            userAccelerationZG: deviceMotion.userAcceleration.z,
            rotationRateX: deviceMotion.rotationRate.x,
            rotationRateY: deviceMotion.rotationRate.y,
            rotationRateZ: deviceMotion.rotationRate.z
        )
    }

    static func unavailable(_ reason: String) -> MotionPoseState {
        return MotionPoseState(
            isAvailable: false,
            statusText: reason,
            timestamp: nil,
            rollDeg: nil,
            pitchDeg: nil,
            yawDeg: nil,
            gravityX: nil,
            gravityY: nil,
            gravityZ: nil,
            attitudeQX: nil,
            attitudeQY: nil,
            attitudeQZ: nil,
            attitudeQW: nil,
            userAccelerationXG: nil,
            userAccelerationYG: nil,
            userAccelerationZG: nil,
            rotationRateX: nil,
            rotationRateY: nil,
            rotationRateZ: nil
        )
    }

    private static func radiansToDegrees(_ radians: Double) -> Double {
        radians * 180.0 / .pi
    }

    private static func shortestAngleDegrees(_ angle: Double) -> Double {
        var value = angle.truncatingRemainder(dividingBy: 360.0)
        if value > 180.0 {
            value -= 360.0
        } else if value < -180.0 {
            value += 360.0
        }
        return value
    }
}

@MainActor
final class DeviceMotionPoseProvider: ObservableObject {
    @Published private(set) var state = MotionPoseState.idle

    private let motionManager = CMMotionManager()
    private let motionQueue: OperationQueue = {
        let queue = OperationQueue()
        queue.name = "local.umift.dual-capture.motion"
        queue.maxConcurrentOperationCount = 1
        return queue
    }()

    func start() {
        guard motionManager.isDeviceMotionAvailable else {
            state = .unavailable("motion unavailable")
            return
        }
        guard !motionManager.isDeviceMotionActive else { return }

        motionManager.deviceMotionUpdateInterval = 1.0 / 60.0
        let frame = Self.bestReferenceFrame()
        motionManager.startDeviceMotionUpdates(using: frame, to: motionQueue) { [weak self] deviceMotion, error in
            let nextState: MotionPoseState
            if let deviceMotion {
                nextState = .from(deviceMotion: deviceMotion)
            } else if let error {
                nextState = .unavailable("motion failed \(error.localizedDescription)")
            } else {
                nextState = .unavailable("motion waiting")
            }

            Task { @MainActor [weak self] in
                self?.state = nextState
            }
        }
    }

    func stop() {
        motionManager.stopDeviceMotionUpdates()
        state = .idle
    }

    private static func bestReferenceFrame() -> CMAttitudeReferenceFrame {
        let frames = CMMotionManager.availableAttitudeReferenceFrames()
        if frames.contains(.xArbitraryCorrectedZVertical) {
            return .xArbitraryCorrectedZVertical
        }
        return .xArbitraryZVertical
    }
}

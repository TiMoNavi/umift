import Foundation
import simd

enum CapturePhase: String, Equatable {
    case previewUncalibrated
    case calibrationWorld
    case calibrationGripper
    case previewCalibrated
    case startCountdown
    case recording

    var title: String {
        switch self {
        case .previewUncalibrated:
            return "Preview"
        case .calibrationWorld:
            return "World Cal"
        case .calibrationGripper:
            return "Gripper Cal"
        case .previewCalibrated:
            return "Ready"
        case .startCountdown:
            return "Countdown"
        case .recording:
            return "Recording"
        }
    }

    var detail: String {
        switch self {
        case .previewUncalibrated:
            return "Calibration required"
        case .calibrationWorld:
            return "Wide world origin"
        case .calibrationGripper:
            return "Ultra gripper path"
        case .previewCalibrated:
            return "Calibrated"
        case .startCountdown:
            return "Starting"
        case .recording:
            return "Sending aligned state"
        }
    }
}

enum PreviewSource: String, CaseIterable, Identifiable {
    case wide = "Wide"
    case ultra = "Ultra"
    case depth = "Depth"

    var id: String { rawValue }

    var shortLabel: String {
        switch self {
        case .wide:
            return "W"
        case .ultra:
            return "U"
        case .depth:
            return "D"
        }
    }

    var next: PreviewSource {
        switch self {
        case .wide:
            return .ultra
        case .ultra:
            return .depth
        case .depth:
            return .wide
        }
    }
}

struct DeviceRunState {
    var wide = "idle"
    var depth = "unavailable"
    var ultra = "idle"
    var torch = "off"
    var receiver = "disconnected"
}

struct MarkerDebugState {
    var markerID: Int
    var detected: Bool
    var trackingState: String
    var confidence: Double
    var centerPX: String
    var cornersPX: String
    var areaPX2: Double
    var angleDeg: Double
    var pathPosition: Double?
    var lastSeenTimestampNS: Int64?
}

struct GripperState {
    var confidence: Double
    var openPercent: Double
    var leftMarker: MarkerDebugState
    var rightMarker: MarkerDebugState
    var gripperDistanceM: Double?
}

struct WideCameraState {
    var intrinsics = "fx -- fy -- cx -- cy --"
    var extrinsics = "pending"
    var poseWorld = "x -- y -- z -- r -- p -- y --"
    var posePosition = "x -- y -- z --"
    var poseRotation = "r -- p -- y --"
    var poseRotationDelta = "drot --"
    var poseSource = "pose unavailable"
    var poseVelocity = "v --"
    var rgb = "no frame"
}

struct DepthState {
    var available = false
    var source = "unavailable"
    var alignedToRGB = false
    var centerDepthM: Double?
    var validRatio: Double = 0
    var confidence = "empty"
    var unavailableReason = "wide depth not connected"
}

struct SyncState {
    var wideTimestampNS: Int64?
    var ultraTimestampNS: Int64?
    var syncErrorMS: Double?
    var syncValid = false
    var bufferDurationS: Double = 0
}

struct GripperPressEvent: Identifiable, Equatable {
    let id: Int
    let timestamp: Date
    let openPercent: Double
}

struct WorldCalibrationState {
    var axisVisible = false
    var markedAt: Date?
    var holdUntil: Date?
    var directionDeviationDeg: Double?
    var finishRequestID = 0
    var releaseInProgress = false
    var triggerText = "waiting"
    var markRequestID = 0
    var arStatusText = "ar idle"
    var arTrackingText = "--"
    var centerHitText = "hit --"
    var centerDistanceM: Double?
    var originWorldText = "--"
    var arAnchorVisible = false
    var userWorldTransform: simd_float4x4?
}

@MainActor
final class CaptureAppState: ObservableObject {
    @Published var phase: CapturePhase = .previewUncalibrated
    @Published var previewSource: PreviewSource = .wide
    @Published var countdownValue: Int?
    @Published var device = DeviceRunState()
    @Published var wide = WideCameraState()
    @Published var depth = DepthState()
    @Published var sync = SyncState()
    @Published var gripper = GripperState.placeholder
    @Published var motion = MotionPoseState.idle
    @Published var lastGripperPressEvent: GripperPressEvent?
    @Published var worldCalibration = WorldCalibrationState()
    @Published var eventLog: [String] = ["app_boot"]

    var isWorldCalibrated = false
    var isGripperCalibrated = false
    private var countdownTask: Task<Void, Never>?
    private var worldCalibrationHoldTask: Task<Void, Never>?
    private var gripperPressArmed = true
    private var nextGripperPressEventID = 1
    private var worldYawReferenceDeg: Double?
    private var poseEstimateActive = false
    private var poseEstimatePositionM = SIMD3<Double>(repeating: 0)
    private var poseEstimateVelocityMPS = SIMD3<Double>(repeating: 0)
    private var poseUserFromMotionReference: simd_quatd?
    private var poseLastMotionTimestamp: TimeInterval?
    private var poseDriftSeconds: Double = 0
    private var arPoseActive = false
    private var lastARPoseTimestampNS: Int64?
    private var lastARPosePositionM: SIMD3<Double>?
    private var lastARPoseQuaternion: simd_quatd?
    private var lastARFrameDebugLogNS: Int64 = 0
    private var lastARPoseDebugLogNS: Int64 = 0

    private static let gripperPressThreshold = 10.0
    private static let gripperReleaseThreshold = 22.0
    private static let worldAxisHoldSeconds: TimeInterval = 10.0
    private static let keepARKitRunningAfterCalibration = true

    var canRecord: Bool {
        phase == .previewCalibrated
    }

    var isRecording: Bool {
        phase == .recording
    }

    var primaryActionTitle: String {
        switch phase {
        case .recording:
            return "Terminate"
        case .startCountdown:
            return "Starting"
        default:
            return "Record"
        }
    }

    init() {
        refreshPlaceholders()
    }

    func selectPreview(_ source: PreviewSource) {
        previewSource = source
        appendEvent("preview_\(source.rawValue.lowercased())")
    }

    func beginCalibration() {
        countdownTask?.cancel()
        worldCalibrationHoldTask?.cancel()
        isWorldCalibrated = false
        isGripperCalibrated = false
        worldYawReferenceDeg = nil
        resetPoseEstimate()
        worldCalibration = WorldCalibrationState()
        gripperPressArmed = true
        phase = .calibrationGripper
        previewSource = .ultra
        appendEvent("calibration_gripper_started")
    }

    func beginWorldCalibrationUsingSavedGripperDebugRoute() {
        countdownTask?.cancel()
        worldCalibrationHoldTask?.cancel()
        isWorldCalibrated = false
        isGripperCalibrated = true
        worldYawReferenceDeg = nil
        resetPoseEstimate()
        worldCalibration = WorldCalibrationState()
        gripperPressArmed = false
        gripper.openPercent = max(gripper.openPercent, 42)
        gripper.confidence = max(gripper.confidence, 0.82)
        phase = .calibrationWorld
        previewSource = .wide
        appendEvent("debug_skip_gripper_to_world")
    }

    func debugRequestWorldOriginMark() {
        guard phase == .calibrationWorld, !worldCalibration.axisVisible else { return }
        requestWorldOriginMark(trigger: "debug_auto")
    }

    func clearCalibrationResults() {
        countdownTask?.cancel()
        worldCalibrationHoldTask?.cancel()
        countdownValue = nil
        isWorldCalibrated = false
        isGripperCalibrated = false
        worldYawReferenceDeg = nil
        resetPoseEstimate()
        worldCalibration = WorldCalibrationState()
        gripper = .placeholder
        lastGripperPressEvent = nil
        gripperPressArmed = true
        phase = .previewUncalibrated
        previewSource = .wide
        depth.centerDepthM = nil
        appendEvent("calibration_cleared")
        refreshPlaceholders()
    }

    func advanceCalibration() {
        switch phase {
        case .calibrationGripper:
            if isGripperCalibrated {
                enterWorldCalibration(reason: "button")
            } else {
                appendEvent("gripper_finish_requested")
            }
        case .calibrationWorld:
            requestWorldOriginMark(trigger: "button")
        default:
            beginCalibration()
        }
        refreshPlaceholders()
    }

    func beginRecordCountdown() {
        guard canRecord else { return }
        countdownTask?.cancel()
        phase = .startCountdown
        appendEvent("record_countdown_started")

        countdownTask = Task { [weak self] in
            for value in [3, 2, 1] {
                await MainActor.run {
                    self?.countdownValue = value
                }
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
            await MainActor.run {
                self?.countdownValue = nil
                self?.phase = .recording
                self?.device.receiver = "disconnected"
                self?.appendEvent("recording_started")
            }
        }
    }

    func terminateRecording() {
        countdownTask?.cancel()
        countdownValue = nil
        guard phase == .recording || phase == .startCountdown else { return }
        phase = isWorldCalibrated && isGripperCalibrated ? .previewCalibrated : .previewUncalibrated
        appendEvent("stop_reason=screen_terminate_button")
    }

    func discardRecording() {
        guard phase == .recording else { return }
        phase = isWorldCalibrated && isGripperCalibrated ? .previewCalibrated : .previewUncalibrated
        appendEvent("recording_discarded")
    }

    func handlePrimaryAction() {
        if isRecording || phase == .startCountdown {
            terminateRecording()
        } else {
            beginRecordCountdown()
        }
    }

    func refreshPlaceholders() {
        device.wide = "running"
        device.depth = "unavailable"
        device.ultra = "running"
        device.torch = "pending"
        sync.bufferDurationS = isRecording ? 3.0 : 0.8
        sync.syncErrorMS = previewSource == .ultra ? 8.4 : 11.2
        sync.syncValid = true
        gripper.openPercent = isGripperCalibrated ? 42 : 0
        gripper.confidence = isGripperCalibrated ? 0.82 : 0.0
    }

    func apply(motion state: MotionPoseState) {
        motion = state
        if poseEstimateActive {
            updatePoseEstimate(with: state)
        } else if arPoseActive {
            updateExtrinsicsMode(isWorldCalibrated ? "user-world transform locked" : "arkit raw camera transform")
        } else {
            wide.poseWorld = state.poseSummary
            wide.poseSource = "coremotion attitude only"
            wide.poseVelocity = "v --"
            updateExtrinsicsMode("waiting for arkit frame")
        }

        guard phase == .calibrationWorld, worldCalibration.axisVisible else { return }
        worldCalibration.directionDeviationDeg = state.yawDeviationDegrees(from: worldYawReferenceDeg)
    }

    func apply(worldARStatus status: WorldCalibrationARStatus) {
        worldCalibration.arStatusText = status.statusText
        worldCalibration.arTrackingText = status.trackingText
        worldCalibration.centerHitText = status.centerHitText
        worldCalibration.centerDistanceM = status.centerDistanceM
        worldCalibration.originWorldText = status.originWorldText
        worldCalibration.arAnchorVisible = status.anchorVisible
        if phase == .calibrationWorld {
            depth.centerDepthM = status.centerDistanceM
        }
        print(
            "[UMIFT][ar-status] phase=\(phase.rawValue) status=\(status.statusText) tracking=\(status.trackingText) hit=\(status.centerHitText) dist=\(status.centerDistanceM.map { String(format: "%.3f", $0) } ?? "--") anchor=\(status.anchorVisible)"
        )
    }

    func apply(worldTrackingFrame state: WorldTrackingARFrameState) {
        worldCalibration.arTrackingText = state.trackingText
        wide.intrinsics = state.intrinsicsText
        depth.available = state.depthAvailable
        depth.source = state.depthSource
        depth.alignedToRGB = state.depthAvailable
        depth.centerDepthM = state.centerDepthM ?? depth.centerDepthM
        depth.validRatio = state.depthValidRatio
        depth.confidence = state.depthConfidenceText
        depth.unavailableReason = state.depthAvailable ? "none" : "arkit depth unavailable"
        if state.timestampNS - lastARFrameDebugLogNS > 1_000_000_000 {
            lastARFrameDebugLogNS = state.timestampNS
            print(
                "[UMIFT][ar-frame] phase=\(phase.rawValue) tracking=\(state.trackingText) userWorld=\(state.cameraInUserWorldTransform == nil ? "no" : "yes") depth=\(state.depthSource) centerDepth=\(state.centerDepthM.map { String(format: "%.3f", $0) } ?? "--") rgb=\(state.rgbText)"
            )
        }

        arPoseActive = true
        poseEstimateActive = false

        guard let cameraInUserWorldTransform = state.cameraInUserWorldTransform else {
            let position = Self.translation(from: state.cameraTransform)
            let quaternion = Self.quaternion(from: state.cameraTransform)
            let euler = Self.eulerDegrees(from: quaternion)
            updatePoseText(prefix: "raw", position: position, euler: euler, quaternion: quaternion)
            wide.poseVelocity = "v --"
            wide.poseSource = isWorldCalibrated ? "arkit waiting user world" : "arkit pre-origin"
            updateExtrinsicsMode("arkit raw camera transform")
            logPoseDebugIfNeeded(timestampNS: state.timestampNS, trackingText: state.trackingText)
            return
        }

        let position = Self.translation(from: cameraInUserWorldTransform)
        let quaternion = Self.quaternion(from: cameraInUserWorldTransform)
        let euler = Self.eulerDegrees(from: quaternion)

        if let lastARPoseTimestampNS, let lastARPosePositionM {
            let dt = Double(state.timestampNS - lastARPoseTimestampNS) / 1_000_000_000.0
            if dt > 0.001, dt < 0.5 {
                poseEstimateVelocityMPS = (position - lastARPosePositionM) / dt
            }
        }
        lastARPoseTimestampNS = state.timestampNS
        lastARPosePositionM = position
        poseEstimatePositionM = position

        let confidence = Self.trackingConfidence(for: state.trackingText)
        updatePoseText(position: position, euler: euler, quaternion: quaternion)
        wide.poseVelocity = String(
            format: "v %.3f %.3f %.3f m/s",
            poseEstimateVelocityMPS.x,
            poseEstimateVelocityMPS.y,
            poseEstimateVelocityMPS.z
        )
        wide.poseSource = String(format: "arkit-vio %@ conf %.0f%%", state.trackingText, confidence * 100)
        updateExtrinsicsMode("user-world transform locked")
        logPoseDebugIfNeeded(timestampNS: state.timestampNS, trackingText: state.trackingText)
    }

    func completeWorldOriginMark(_ result: WorldCalibrationARResult) {
        worldCalibrationHoldTask?.cancel()
        let now = result.timestamp
        worldYawReferenceDeg = motion.yawDeg
        worldCalibration.axisVisible = true
        worldCalibration.markedAt = now
        worldCalibration.holdUntil = now.addingTimeInterval(Self.worldAxisHoldSeconds)
        worldCalibration.directionDeviationDeg = motion.yawDeviationDegrees(from: worldYawReferenceDeg)
        worldCalibration.centerDistanceM = result.distanceM
        worldCalibration.originWorldText = result.originWorldText
        worldCalibration.arStatusText = "origin anchored"
        worldCalibration.arAnchorVisible = true
        worldCalibration.userWorldTransform = result.userWorldTransform
        arPoseActive = true
        poseEstimateActive = false
        poseLastMotionTimestamp = nil
        poseDriftSeconds = 0
        publishInitialARPose(from: result.cameraInUserWorldTransform)
        previewSource = .wide
        appendEvent("world_origin_anchored")

        worldCalibrationHoldTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.worldAxisHoldSeconds * 1_000_000_000))
            await MainActor.run {
                guard let self, self.phase == .calibrationWorld else { return }
                if Self.keepARKitRunningAfterCalibration {
                    self.worldCalibration.arStatusText = "ar tracking"
                    self.worldCalibration.centerHitText = "continuous pose"
                    self.appendEvent("world_hold_complete")
                    self.completeWorldCalibrationRelease()
                } else {
                    self.worldCalibration.releaseInProgress = true
                    self.worldCalibration.finishRequestID += 1
                    self.worldCalibration.arStatusText = "ar stopping"
                    self.worldCalibration.centerHitText = "releasing camera"
                    self.appendEvent("world_release_requested")
                }
            }
        }
    }

    func completeWorldCalibrationRelease() {
        guard phase == .calibrationWorld else { return }
        isWorldCalibrated = true
        worldCalibration.releaseInProgress = false
        worldCalibration.axisVisible = false
        worldCalibration.arAnchorVisible = false
        if Self.keepARKitRunningAfterCalibration {
            worldCalibration.arStatusText = "ar tracking"
            worldCalibration.centerHitText = "continuous pose"
        } else {
            worldCalibration.arStatusText = "ar released"
            worldCalibration.centerHitText = "camera rebuilding"
            wide.poseSource = "arkit snapshot, ar released"
            updateExtrinsicsMode("world origin snapshot")
        }
        phase = .previewCalibrated
        previewSource = .wide
        appendEvent("world_calibration_done")
    }

    func handleContinuousARWatchdogTimeout() {
        arPoseActive = false
        poseEstimateActive = false
        worldCalibration.arStatusText = "ar watchdog release"
        worldCalibration.arTrackingText = "--"
        worldCalibration.centerHitText = "ar frame stalled"
        wide.poseSource = "arkit stalled, camera rebuilt"
        updateExtrinsicsMode("arkit frame stalled")
        appendEvent("ar_watchdog_release")
    }

    private func resetPoseEstimate() {
        poseEstimateActive = false
        poseEstimatePositionM = SIMD3<Double>(repeating: 0)
        poseEstimateVelocityMPS = SIMD3<Double>(repeating: 0)
        poseUserFromMotionReference = nil
        poseLastMotionTimestamp = nil
        poseDriftSeconds = 0
        arPoseActive = false
        lastARPoseTimestampNS = nil
        lastARPosePositionM = nil
        lastARPoseQuaternion = nil
        lastARFrameDebugLogNS = 0
        lastARPoseDebugLogNS = 0
        wide.poseWorld = "x -- y -- z -- r -- p -- y --"
        wide.posePosition = "x -- y -- z --"
        wide.poseRotation = "r -- p -- y --"
        wide.poseRotationDelta = "drot --"
        wide.poseSource = "pose unavailable"
        wide.poseVelocity = "v --"
        updateExtrinsicsMode("pending")
    }

    private func publishInitialARPose(from transform: simd_float4x4) {
        let position = Self.translation(from: transform)
        let quaternion = Self.quaternion(from: transform)
        let euler = Self.eulerDegrees(from: quaternion)
        poseEstimatePositionM = position
        poseEstimateVelocityMPS = SIMD3<Double>(repeating: 0)
        lastARPosePositionM = position
        lastARPoseTimestampNS = nil
        lastARPoseQuaternion = nil
        updatePoseText(position: position, euler: euler, quaternion: quaternion)
        wide.poseVelocity = "v 0.000 0.000 0.000 m/s"
        wide.poseSource = "arkit-vio anchored conf 90%"
        updateExtrinsicsMode("user-world transform locked")
    }

    private func initializePoseEstimate(from result: WorldCalibrationARResult) {
        poseEstimatePositionM = Self.translation(from: result.cameraInUserWorldTransform)
        poseEstimateVelocityMPS = SIMD3<Double>(repeating: 0)
        poseLastMotionTimestamp = motion.timestamp
        poseDriftSeconds = 0

        let cameraUserOrientation = Self.quaternion(from: result.cameraInUserWorldTransform)
        if let motionQuaternion = motion.attitudeQuaternion {
            poseUserFromMotionReference = simd_normalize(cameraUserOrientation * motionQuaternion.inverse)
            poseEstimateActive = true
            updatePoseEstimate(with: motion)
        } else {
            poseUserFromMotionReference = nil
            poseEstimateActive = true
            let euler = Self.eulerDegrees(from: cameraUserOrientation)
            publishPoseEstimate(orientationEulerDeg: euler, confidence: 0.25, sourceNote: "ar origin, waiting imu")
        }
    }

    private func updatePoseEstimate(with state: MotionPoseState) {
        guard poseEstimateActive else { return }

        let timestamp = state.timestamp
        let motionQuaternion = state.attitudeQuaternion
        let orientation: simd_quatd
        if let poseUserFromMotionReference, let motionQuaternion {
            orientation = simd_normalize(poseUserFromMotionReference * motionQuaternion)
        } else {
            orientation = simd_quatd(angle: 0, axis: SIMD3<Double>(0, 0, 1))
        }

        if let timestamp {
            let rawDelta = poseLastMotionTimestamp.map { timestamp - $0 } ?? 0
            let dt = min(max(rawDelta, 0), 0.05)
            poseLastMotionTimestamp = timestamp
            if dt > 0, let accelerationG = state.userAccelerationG {
                let accelerationUserMPS2 = orientation.act(accelerationG * 9.80665)
                let accelerationMagnitude = simd_length(accelerationUserMPS2)
                let rotationMagnitude = state.rotationRateMagnitude ?? 0
                let stationary = accelerationMagnitude < 0.16 && rotationMagnitude < 0.05
                let effectiveAcceleration = stationary
                    ? SIMD3<Double>(repeating: 0)
                    : accelerationUserMPS2

                poseEstimateVelocityMPS += effectiveAcceleration * dt
                let dampingPerSecond = stationary ? 0.025 : 0.58
                poseEstimateVelocityMPS *= pow(dampingPerSecond, dt)
                if stationary && simd_length(poseEstimateVelocityMPS) < 0.006 {
                    poseEstimateVelocityMPS = SIMD3<Double>(repeating: 0)
                }
                poseEstimatePositionM += poseEstimateVelocityMPS * dt
                poseDriftSeconds += dt
            }
        }

        let euler = Self.eulerDegrees(from: orientation)
        let confidence = max(0.12, min(0.62, 0.62 - poseDriftSeconds * 0.012))
        publishPoseEstimate(
            orientationEulerDeg: euler,
            confidence: confidence,
            sourceNote: "imu-est"
        )
    }

    private func publishPoseEstimate(
        orientationEulerDeg: SIMD3<Double>,
        confidence: Double,
        sourceNote: String
    ) {
        updatePoseText(position: poseEstimatePositionM, euler: orientationEulerDeg, quaternion: nil)
        wide.poseRotationDelta = "drot imu"
        wide.poseVelocity = String(
            format: "v %.3f %.3f %.3f m/s",
            poseEstimateVelocityMPS.x,
            poseEstimateVelocityMPS.y,
            poseEstimateVelocityMPS.z
        )
        wide.poseSource = String(
            format: "%@ conf %.0f%% drift %.1fs",
            sourceNote,
            confidence * 100,
            poseDriftSeconds
        )
        updateExtrinsicsMode("user-world transform locked")
    }

    private func updateExtrinsicsMode(_ text: String) {
        guard wide.extrinsics != text else { return }
        wide.extrinsics = text
    }

    private func logPoseDebugIfNeeded(timestampNS: Int64, trackingText: String) {
        guard timestampNS - lastARPoseDebugLogNS > 1_000_000_000 else { return }
        lastARPoseDebugLogNS = timestampNS
        print(
            "[UMIFT][pose] phase=\(phase.rawValue) tracking=\(trackingText) mode=\(wide.poseSource) \(wide.posePosition) \(wide.poseRotation) \(wide.poseRotationDelta)"
        )
    }

    private func updatePoseText(
        prefix: String? = nil,
        position: SIMD3<Double>,
        euler: SIMD3<Double>,
        quaternion: simd_quatd?
    ) {
        let body = String(
            format: "x %.3f y %.3f z %.3f r %.1f p %.1f y %.1f",
            position.x,
            position.y,
            position.z,
            euler.x,
            euler.y,
            euler.z
        )
        wide.poseWorld = prefix.map { "\($0) \(body)" } ?? body
        wide.posePosition = String(format: "x %.3f y %.3f z %.3f", position.x, position.y, position.z)
        wide.poseRotation = String(format: "r %.1f p %.1f y %.1f", euler.x, euler.y, euler.z)
        if let quaternion {
            wide.poseRotationDelta = Self.rotationDeltaText(current: quaternion, previous: lastARPoseQuaternion)
            lastARPoseQuaternion = quaternion
        }
    }

    private static func rotationDeltaText(current: simd_quatd, previous: simd_quatd?) -> String {
        guard let previous else { return "drot 0.00 deg" }
        let delta = simd_normalize(current * previous.inverse)
        let shortestReal = min(1.0, max(-1.0, abs(delta.real)))
        let angleDeg = 2.0 * acos(shortestReal) * 180.0 / .pi
        return String(format: "drot %.2f deg/frame", angleDeg)
    }

    private static func translation(from transform: simd_float4x4) -> SIMD3<Double> {
        SIMD3<Double>(
            Double(transform.columns.3.x),
            Double(transform.columns.3.y),
            Double(transform.columns.3.z)
        )
    }

    private static func quaternion(from transform: simd_float4x4) -> simd_quatd {
        let rotation = simd_float3x3(
            SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            SIMD3<Float>(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
        let quaternion = simd_quatf(rotation)
        return simd_normalize(simd_quatd(
            ix: Double(quaternion.imag.x),
            iy: Double(quaternion.imag.y),
            iz: Double(quaternion.imag.z),
            r: Double(quaternion.real)
        ))
    }

    private static func eulerDegrees(from quaternion: simd_quatd) -> SIMD3<Double> {
        let q = simd_normalize(quaternion)
        let x = q.imag.x
        let y = q.imag.y
        let z = q.imag.z
        let w = q.real

        let sinRoll = 2.0 * (w * x + y * z)
        let cosRoll = 1.0 - 2.0 * (x * x + y * y)
        let roll = atan2(sinRoll, cosRoll)

        let sinPitch = 2.0 * (w * y - z * x)
        let pitch: Double
        if abs(sinPitch) >= 1.0 {
            pitch = (sinPitch >= 0 ? 1 : -1) * .pi / 2.0
        } else {
            pitch = asin(sinPitch)
        }

        let sinYaw = 2.0 * (w * z + x * y)
        let cosYaw = 1.0 - 2.0 * (y * y + z * z)
        let yaw = atan2(sinYaw, cosYaw)

        return SIMD3<Double>(
            roll * 180.0 / .pi,
            pitch * 180.0 / .pi,
            yaw * 180.0 / .pi
        )
    }

    private static func trackingConfidence(for trackingText: String) -> Double {
        if trackingText == "normal" {
            return 0.95
        }
        if trackingText.contains("initializing") || trackingText.contains("relocalizing") {
            return 0.45
        }
        if trackingText.contains("limited") {
            return 0.62
        }
        return 0.25
    }

    func apply(gripperVision state: GripperVisionState) {
        let nextOpenPercent = state.openPercent ?? gripper.openPercent
        if let openPercent = state.openPercent {
            gripper.openPercent = openPercent
        }
        gripper.confidence = state.confidence
        gripper.leftMarker = state.leftMarker
        gripper.rightMarker = state.rightMarker
        isGripperCalibrated = isGripperCalibrated || state.isPathCalibrated
        if phase == .calibrationGripper, state.isPathCalibrated {
            enterWorldCalibration(reason: "gripper_path")
        }

        if state.openPercent != nil, let event = consumeGripperPressIfNeeded(openPercent: nextOpenPercent) {
            lastGripperPressEvent = event
            appendEvent(String(format: "gripper_press open=%.0f", event.openPercent))
            if phase == .calibrationWorld, !worldCalibration.axisVisible {
                requestWorldOriginMark(trigger: "gripper")
            }
        }
    }

    private func appendEvent(_ event: String) {
        eventLog.insert(event, at: 0)
        eventLog = Array(eventLog.prefix(6))
        print(
            "[UMIFT][event] \(event) phase=\(phase.rawValue) preview=\(previewSource.rawValue) gripCal=\(isGripperCalibrated) worldCal=\(isWorldCalibrated) axis=\(worldCalibration.axisVisible) markID=\(worldCalibration.markRequestID) finishID=\(worldCalibration.finishRequestID) ar=\(worldCalibration.arStatusText) hit=\(worldCalibration.centerHitText)"
        )
    }

    private func enterWorldCalibration(reason: String) {
        guard phase != .calibrationWorld else { return }
        isGripperCalibrated = true
        worldCalibration = WorldCalibrationState()
        worldYawReferenceDeg = nil
        phase = .calibrationWorld
        previewSource = .wide
        appendEvent("calibration_world_started_\(reason)")
    }

    private func consumeGripperPressIfNeeded(openPercent: Double) -> GripperPressEvent? {
        if openPercent >= Self.gripperReleaseThreshold {
            gripperPressArmed = true
            return nil
        }
        guard gripperPressArmed, openPercent <= Self.gripperPressThreshold else { return nil }
        gripperPressArmed = false
        let event = GripperPressEvent(
            id: nextGripperPressEventID,
            timestamp: Date(),
            openPercent: openPercent
        )
        nextGripperPressEventID += 1
        return event
    }

    private func requestWorldOriginMark(trigger: String) {
        worldCalibration.triggerText = trigger
        worldCalibration.markRequestID += 1
        worldCalibration.arStatusText = "mark requested"
        previewSource = .wide
        appendEvent("world_origin_mark_requested_\(trigger)")
    }
}

extension GripperState {
    static var placeholder: GripperState {
        GripperState(
            confidence: 0,
            openPercent: 0,
            leftMarker: MarkerDebugState(
                markerID: 0,
                detected: false,
                trackingState: "lost",
                confidence: 0,
                centerPX: "--",
                cornersPX: "--",
                areaPX2: 0,
                angleDeg: 0,
                pathPosition: nil,
                lastSeenTimestampNS: nil
            ),
            rightMarker: MarkerDebugState(
                markerID: 1,
                detected: false,
                trackingState: "lost",
                confidence: 0,
                centerPX: "--",
                cornersPX: "--",
                areaPX2: 0,
                angleDeg: 0,
                pathPosition: nil,
                lastSeenTimestampNS: nil
            ),
            gripperDistanceM: nil
        )
    }
}

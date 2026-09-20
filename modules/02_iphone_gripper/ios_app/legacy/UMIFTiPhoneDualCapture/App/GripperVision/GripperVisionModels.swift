import CoreGraphics
import Foundation

enum MarkerOverlayKind {
    case detected
    case pathCandidate
    case rejected
    case held
}

struct MarkerOverlay: Identifiable {
    let stableID: String
    let markerID: Int?
    let kind: MarkerOverlayKind
    let corners: [CGPoint]
    let center: CGPoint
    let area: Double
    let age: TimeInterval

    var id: String { stableID }
}

struct GripperVisionState {
    var statusText = "vision idle"
    var detectedIDsText = "-"
    var rawWidthText = "pnp --"
    var pixelDistanceText = "pix --"
    var openingText = "-"
    var confidenceText = "0%"
    var calibrationText = "cal set 0/100"
    var depthHintText = "z --"
    var intrinsicsText = "K --"
    var frameText = "frame --"
    var pathText = "path --"
    var sweepCalibrationText = "cal idle"
    var sweepProgressText = "press Cal and move gripper"
    var sweepCycleEstimate: Double = 0
    var sweepPairedSampleCount: Int = 0
    var sweepRangePixels: Double?
    var sweepReadyToFinish = false
    var markerOverlays: [MarkerOverlay] = []
    var videoFrameSize = CGSize.zero
    var openPercent: Double?
    var confidence: Double = 0
    var leftMarker = MarkerDebugState.empty(markerID: 0)
    var rightMarker = MarkerDebugState.empty(markerID: 1)
    var isPathCalibrated = false

    static let empty = GripperVisionState()
}

extension MarkerDebugState {
    static func empty(markerID: Int) -> MarkerDebugState {
        MarkerDebugState(
            markerID: markerID,
            detected: false,
            trackingState: "lost",
            confidence: 0,
            centerPX: "--",
            cornersPX: "--",
            areaPX2: 0,
            angleDeg: 0,
            pathPosition: nil,
            lastSeenTimestampNS: nil
        )
    }
}

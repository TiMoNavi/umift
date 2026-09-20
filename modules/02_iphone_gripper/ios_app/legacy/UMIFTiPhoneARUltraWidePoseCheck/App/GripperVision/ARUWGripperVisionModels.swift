import CoreGraphics
import Foundation

enum ARUWMarkerOverlayKind {
    case detected
    case rejected
}

struct ARUWMarkerOverlay: Identifiable {
    let stableID: String
    let markerID: Int?
    let kind: ARUWMarkerOverlayKind
    let corners: [CGPoint]
    let center: CGPoint
    let area: Double

    var id: String { stableID }
}

struct ARUWMarkerDebugState {
    var detected: Bool
    var centerText: String
    var areaText: String

    static let empty = ARUWMarkerDebugState(
        detected: false,
        centerText: "--",
        areaText: "--"
    )
}

struct ARUWGripperVisionState {
    var statusText = "uw waiting"
    var frameText = "uw --"
    var idsText = "-"
    var roiText = "roi --"
    var inputText = "--"
    var openingText = "uncalibrated"
    var confidenceText = "0%"
    var timestampText = "--"
    var leftMarker = ARUWMarkerDebugState.empty
    var rightMarker = ARUWMarkerDebugState.empty
    var overlays: [ARUWMarkerOverlay] = []
    var frameSize = CGSize.zero

    static let empty = ARUWGripperVisionState()
}

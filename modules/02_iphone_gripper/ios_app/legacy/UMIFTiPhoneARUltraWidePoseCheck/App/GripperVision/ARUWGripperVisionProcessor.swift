import CoreGraphics
import CoreVideo
import Foundation

final class ARUWGripperVisionProcessor {
    private let bridge = ARUWGripperArucoBridge()
    private var lastAcceptedByID: [Int: ARUWMarkerOverlay] = [:]

    func process(pixelBuffer: CVPixelBuffer, timestamp: TimeInterval) -> ARUWGripperVisionState {
        let result = bridge.analyze(pixelBuffer)
        let frameWidth = (result["frameWidth"] as? NSNumber)?.doubleValue ?? Double(CVPixelBufferGetWidth(pixelBuffer))
        let frameHeight = (result["frameHeight"] as? NSNumber)?.doubleValue ?? Double(CVPixelBufferGetHeight(pixelBuffer))
        let roiTopY = (result["roiTopY"] as? NSNumber)?.doubleValue ?? frameHeight * 0.55
        let frameSize = CGSize(width: frameWidth, height: frameHeight)

        let decoded = Self.parseMarkers(from: result["markers"], kind: .detected, stablePrefix: "det")
        let accepted = Self.acceptedDecodedMarkers(decoded, frameSize: frameSize, roiTopY: roiTopY)
        for marker in accepted {
            if let markerID = marker.markerID {
                lastAcceptedByID[markerID] = marker
            }
        }

        let rejected = Self.parseMarkers(from: result["rejectedCandidates"], kind: .rejected, stablePrefix: "rej")
            .prefix(64)
        let overlays = Array(rejected) + accepted

        let targetIDs = accepted.compactMap(\.markerID).sorted()
        let rawIDs = (result["detectedIDs"] as? [NSNumber])?.map(\.intValue) ?? []
        let otherCount = rawIDs.filter { $0 != 0 && $0 != 1 }.count
        let rejectedCount = (result["keptRejectedCount"] as? NSNumber)?.intValue ?? overlays.filter { $0.kind == .rejected }.count

        let confidence: Double
        let status: String
        if targetIDs.count >= 2 {
            confidence = 0.92
            status = "ids 0+1 visible"
        } else if targetIDs.count == 1 {
            confidence = 0.55
            status = "single marker"
        } else if !rawIDs.isEmpty {
            confidence = 0.20
            status = "wrong ids visible"
        } else {
            confidence = 0.0
            status = "searching"
        }

        var idsParts = [targetIDs.isEmpty ? "-" : targetIDs.map(String.init).joined(separator: ",")]
        if otherCount > 0 {
            idsParts.append("o\(otherCount)")
        }
        if rejectedCount > 0 {
            idsParts.append("?\(rejectedCount)")
        }

        let left = accepted.first { $0.markerID == 0 }
        let right = accepted.first { $0.markerID == 1 }

        return ARUWGripperVisionState(
            statusText: status,
            frameText: String(format: "uw %.0fx%.0f", frameWidth, frameHeight),
            idsText: idsParts.joined(separator: " "),
            roiText: String(format: "bottom %.0f%%", max(0, min(100, (frameHeight - roiTopY) / max(1, frameHeight) * 100))),
            inputText: (result["inputMode"] as? String) ?? "--",
            openingText: "cal needed",
            confidenceText: String(format: "%.0f%%", confidence * 100),
            timestampText: String(format: "%.3f", timestamp),
            leftMarker: Self.debugState(for: left),
            rightMarker: Self.debugState(for: right),
            overlays: overlays,
            frameSize: frameSize
        )
    }

    private static func acceptedDecodedMarkers(
        _ markers: [ARUWMarkerOverlay],
        frameSize: CGSize,
        roiTopY: Double
    ) -> [ARUWMarkerOverlay] {
        markers.filter { marker in
            guard let markerID = marker.markerID, markerID == 0 || markerID == 1 else { return false }
            guard marker.center.y >= roiTopY else { return false }
            guard marker.area >= 36 else { return false }
            let xNorm = frameSize.width > 0 ? marker.center.x / frameSize.width : 0.5
            switch markerID {
            case 0:
                return xNorm <= 0.70
            case 1:
                return xNorm >= 0.38
            default:
                return false
            }
        }
    }

    private static func parseMarkers(
        from value: Any?,
        kind: ARUWMarkerOverlayKind,
        stablePrefix: String
    ) -> [ARUWMarkerOverlay] {
        guard let rows = value as? [[String: Any]] else { return [] }
        return rows.enumerated().compactMap { index, row in
            guard
                let id = (row["id"] as? NSNumber)?.intValue,
                let centerX = (row["centerX"] as? NSNumber)?.doubleValue,
                let centerY = (row["centerY"] as? NSNumber)?.doubleValue,
                let area = (row["area"] as? NSNumber)?.doubleValue,
                let cornerRows = row["corners"] as? [[String: Any]]
            else {
                return nil
            }

            let corners = cornerRows.compactMap { corner -> CGPoint? in
                guard
                    let x = (corner["x"] as? NSNumber)?.doubleValue,
                    let y = (corner["y"] as? NSNumber)?.doubleValue
                else {
                    return nil
                }
                return CGPoint(x: x, y: y)
            }
            guard corners.count == 4 else { return nil }

            let markerID = id >= 0 ? id : nil
            let stableID = markerID.map { "\(stablePrefix)-\($0)-\(index)" } ?? "\(stablePrefix)-\(index)"
            return ARUWMarkerOverlay(
                stableID: stableID,
                markerID: markerID,
                kind: kind,
                corners: corners,
                center: CGPoint(x: centerX, y: centerY),
                area: area
            )
        }
    }

    private static func debugState(for marker: ARUWMarkerOverlay?) -> ARUWMarkerDebugState {
        guard let marker else { return .empty }
        return ARUWMarkerDebugState(
            detected: true,
            centerText: String(format: "%.0f, %.0f", marker.center.x, marker.center.y),
            areaText: String(format: "%.0f px2", marker.area)
        )
    }
}

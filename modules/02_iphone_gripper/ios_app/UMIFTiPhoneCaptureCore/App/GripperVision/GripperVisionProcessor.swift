import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import simd

private struct SavedGripperPathCalibration: Codable {
    let version: Int
    let savedAt: Date
    let frameWidth: Double
    let frameHeight: Double
    let closedCenters: [String: SavedPoint]
    let openCenters: [String: SavedPoint]
}

private struct SavedPoint: Codable {
    let x: Double
    let y: Double

    init(_ point: CGPoint) {
        x = point.x
        y = point.y
    }

    var cgPoint: CGPoint {
        CGPoint(x: x, y: y)
    }
}

private struct SweepCalibrationSample {
    let time: TimeInterval
    let centers: [Int: CGPoint]
    let pairDistance: Double?
}

private struct PathCandidateTuning {
    let minArea: Double
    let maxSideRatio: Double
    let minCompactness: Double
    let minFillRatio: Double
    let maxCornerCosine: Double
    let minPathError: Double
    let pathErrorRate: Double
    let lastAnchorMinRadius: Double
    let lastAnchorPathRate: Double
    let lastAnchorAgeDrift: Double
    let referenceAnchorMinRadius: Double
    let referenceAnchorPathRate: Double
    let referencePenaltyDivisor: Double
    let maxPromotionScore: Double
    let trackingPathErrorRate: Double
}

final class GripperVisionProcessor {
    private let bridge = GripperArucoBridge()
    private var state = GripperVisionState.empty
    private var smoothedRawWidth: Double?
    private var smoothedPixelDistance: Double?
    private var smoothedZ: Double?
    private var consecutiveValidFrames = 0
    private var trackingConfidence = 0.0
    private var closedPixelDistance: Double?
    private var openPixelDistance: Double?
    private var closedCenters: [Int: CGPoint] = [:]
    private var openCenters: [Int: CGPoint] = [:]
    private var latestMarkerByID: [Int: MarkerOverlay] = [:]
    private var lastSeenMarkerByID: [Int: (marker: MarkerOverlay, seenAt: Date)] = [:]
    private var filteredOpenPercent: Double?
    private var lastValidPathAt: Date?
    private var didLoadSavedCalibration = false
    private var loadedCalibrationFrameSize: CGSize?
    private var sweepCalibrationStartedAt: Date?
    private var sweepCalibrationSamples: [SweepCalibrationSample] = []
    private var isSweepCalibrating = false

    private static let trackedMarkerIDs = [0, 1]
    private static let calibrationDefaultsKey = "local.umift.dual.gripper.pathCalibration.v1"
    private static let markerHoldSeconds: TimeInterval = 0.70
    private static let minCalibrationTravelPixels = 60.0
    private static let minSweepDistanceRangePixels = 80.0
    private static let maxDebugRejectedOverlays = 220
    private static let pathCandidateAnchorSeconds: TimeInterval = 0.55
    private static let leftMarkerMaxFrameX = 2.0 / 3.0
    private static let rightMarkerMinFrameX = 0.5
    private static let detectedJumpGuardSeconds: TimeInterval = 0.22
    private static let detectedMaxJumpPercentBase = 28.0
    private static let detectedMaxJumpPercentPerSecond = 520.0
    private static let leftDetectedMinPathError = 20.0
    private static let leftDetectedPathErrorRate = 0.16
    private static let rightDetectedMinPathError = 32.0
    private static let rightDetectedPathErrorRate = 0.24

    private static let leftCandidateTuning = PathCandidateTuning(
        minArea: 650.0,
        maxSideRatio: 3.0,
        minCompactness: 0.036,
        minFillRatio: 0.28,
        maxCornerCosine: 0.90,
        minPathError: 8.0,
        pathErrorRate: 0.065,
        lastAnchorMinRadius: 20.0,
        lastAnchorPathRate: 0.07,
        lastAnchorAgeDrift: 50.0,
        referenceAnchorMinRadius: 18.0,
        referenceAnchorPathRate: 0.055,
        referencePenaltyDivisor: 30.0,
        maxPromotionScore: 1.55,
        trackingPathErrorRate: 0.11
    )

    private static let rightCenterCandidateTuning = PathCandidateTuning(
        minArea: 620.0,
        maxSideRatio: 3.2,
        minCompactness: 0.034,
        minFillRatio: 0.26,
        maxCornerCosine: 0.91,
        minPathError: 8.0,
        pathErrorRate: 0.070,
        lastAnchorMinRadius: 22.0,
        lastAnchorPathRate: 0.070,
        lastAnchorAgeDrift: 52.0,
        referenceAnchorMinRadius: 20.0,
        referenceAnchorPathRate: 0.060,
        referencePenaltyDivisor: 28.0,
        maxPromotionScore: 1.45,
        trackingPathErrorRate: 0.13
    )

    private static let rightEdgeCandidateTuning = PathCandidateTuning(
        minArea: 360.0,
        maxSideRatio: 5.4,
        minCompactness: 0.020,
        minFillRatio: 0.16,
        maxCornerCosine: 0.965,
        minPathError: 11.0,
        pathErrorRate: 0.13,
        lastAnchorMinRadius: 34.0,
        lastAnchorPathRate: 0.12,
        lastAnchorAgeDrift: 80.0,
        referenceAnchorMinRadius: 30.0,
        referenceAnchorPathRate: 0.105,
        referencePenaltyDivisor: 34.0,
        maxPromotionScore: 1.75,
        trackingPathErrorRate: 0.22
    )

    func process(sampleBuffer: CMSampleBuffer) -> GripperVisionState? {
        loadSavedCalibrationIfNeeded()
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return nil }
        if let intrinsicsText = Self.intrinsicsText(sampleBuffer: sampleBuffer) {
            state.intrinsicsText = intrinsicsText
        }
        let analysis = bridge.analyze(pixelBuffer)
        consume(result: analysis, timestampNS: Self.timestampNS(for: sampleBuffer))
        return state
    }

    func process(
        pixelBuffer: CVPixelBuffer,
        timestampNS: Int64?,
        intrinsicsText: String?
    ) -> GripperVisionState {
        loadSavedCalibrationIfNeeded()
        if let intrinsicsText {
            state.intrinsicsText = intrinsicsText
        }
        let analysis = bridge.analyze(pixelBuffer)
        consume(result: analysis, timestampNS: timestampNS)
        return state
    }

    func startSweepCalibration() -> GripperVisionState {
        isSweepCalibrating = true
        closedCenters = [:]
        openCenters = [:]
        closedPixelDistance = nil
        openPixelDistance = nil
        latestMarkerByID = [:]
        lastSeenMarkerByID = [:]
        loadedCalibrationFrameSize = nil
        UserDefaults.standard.removeObject(forKey: Self.calibrationDefaultsKey)
        sweepCalibrationSamples = []
        sweepCalibrationStartedAt = Date()
        filteredOpenPercent = nil
        state.sweepCycleEstimate = 0
        state.sweepPairedSampleCount = 0
        state.sweepRangePixels = nil
        state.sweepReadyToFinish = false
        state.isPathCalibrated = false
        state.calibrationText = "cal recording"
        state.sweepCalibrationText = "cal recording"
        state.sweepProgressText = "move gripper open-close 3x"
        state.statusText = "calibrating sweep"
        return state
    }

    func finishSweepCalibration() -> GripperVisionState {
        guard isSweepCalibrating || !sweepCalibrationSamples.isEmpty else {
            state.sweepCalibrationText = "cal no samples"
            return state
        }
        isSweepCalibrating = false
        state.sweepReadyToFinish = false
        if applySweepCalibration() {
            saveCalibrationIfComplete()
            updateOpeningAndCalibrationText(allowLegacyPixelFallback: false)
            state.sweepCalibrationText = "cal done"
        }
        return state
    }

    func cancelSweepCalibration() -> GripperVisionState {
        isSweepCalibrating = false
        sweepCalibrationSamples = []
        sweepCalibrationStartedAt = nil
        state.sweepCalibrationText = "cal cancelled"
        state.sweepProgressText = "press Cal and move gripper"
        state.sweepCycleEstimate = 0
        state.sweepPairedSampleCount = 0
        state.sweepRangePixels = nil
        state.sweepReadyToFinish = false
        return state
    }

    func resetCalibration() -> GripperVisionState {
        isSweepCalibrating = false
        closedCenters = [:]
        openCenters = [:]
        closedPixelDistance = nil
        openPixelDistance = nil
        filteredOpenPercent = nil
        latestMarkerByID = [:]
        lastSeenMarkerByID = [:]
        loadedCalibrationFrameSize = nil
        sweepCalibrationSamples = []
        sweepCalibrationStartedAt = nil
        UserDefaults.standard.removeObject(forKey: Self.calibrationDefaultsKey)
        updateOpeningAndCalibrationText()
        state.sweepCalibrationText = "cal reset"
        state.sweepProgressText = "press Cal and move gripper"
        state.sweepCycleEstimate = 0
        state.sweepPairedSampleCount = 0
        state.sweepRangePixels = nil
        state.sweepReadyToFinish = false
        state.isPathCalibrated = false
        return state
    }

    private func consume(result: [String: Any], timestampNS: Int64?) {
        let now = Date()

        if let width = result["frameWidth"] as? NSNumber, let height = result["frameHeight"] as? NSNumber {
            if let roiTopY = result["roiTopY"] as? NSNumber {
                state.frameText = "frame \(width.intValue)x\(height.intValue) roi>\(roiTopY.intValue)"
            } else {
                state.frameText = "frame \(width.intValue)x\(height.intValue)"
            }
            state.videoFrameSize = CGSize(width: width.doubleValue, height: height.doubleValue)
            validateLoadedCalibrationForCurrentFrame()
        }

        let rawDetectedMarkers = Self.parseMarkers(from: result["markers"], kind: .detected, stablePrefix: "det")
        let detectedMarkers = Self.filterDetectedMarkersForTracking(
            rawDetectedMarkers,
            closedCenters: closedCenters,
            openCenters: openCenters,
            lastSeen: lastSeenMarkerByID,
            frameSize: state.videoFrameSize,
            now: now,
            usePathCalibration: !isSweepCalibrating
        )
        let rejectedMarkers = Self.parseMarkers(from: result["rejectedCandidates"], kind: .rejected, stablePrefix: "rej")
        var markerByID: [Int: MarkerOverlay] = [:]
        for marker in detectedMarkers {
            guard let markerID = marker.markerID, markerID == 0 || markerID == 1 else { continue }
            if marker.area > (markerByID[markerID]?.area ?? 0.0) {
                markerByID[markerID] = marker
            }
        }

        let pathCandidateByID = isSweepCalibrating ? [:] : Self.matchRejectedCandidatesToPaths(
            rejected: rejectedMarkers,
            existingMarkers: markerByID,
            closedCenters: closedCenters,
            openCenters: openCenters,
            lastSeen: lastSeenMarkerByID,
            frameSize: state.videoFrameSize,
            now: now
        )
        for (markerID, marker) in pathCandidateByID where markerByID[markerID] == nil {
            markerByID[markerID] = marker
        }

        latestMarkerByID = markerByID
        for (markerID, marker) in markerByID where marker.kind == .detected {
            lastSeenMarkerByID[markerID] = (marker, now)
        }
        state.markerOverlays = Self.displayOverlays(
            detected: detectedMarkers,
            pathCandidates: Array(pathCandidateByID.values),
            rejected: rejectedMarkers,
            lastSeen: lastSeenMarkerByID,
            now: now
        )

        let ids = (result["detectedIDs"] as? [NSNumber])?.map(\.intValue) ?? []
        let targetIDs = ids.filter { $0 == 0 || $0 == 1 }
        let otherIDCount = ids.filter { $0 != 0 && $0 != 1 }.count
        var idsParts = [targetIDs.isEmpty ? "-" : targetIDs.map(String.init).joined(separator: ",")]
        if otherIDCount > 0 {
            idsParts.append("o\(otherIDCount)")
        }
        if !rejectedMarkers.isEmpty {
            idsParts.append("?\(rejectedMarkers.count)")
        }
        state.detectedIDsText = idsParts.joined(separator: " ")

        let rawWidth = (result["widthRawM"] as? NSNumber)?.doubleValue
        let pixelDistance = (result["pixelDistance"] as? NSNumber)?.doubleValue ?? Self.centerDistance(
            latestMarkerByID[0]?.center,
            latestMarkerByID[1]?.center
        )
        let meanZ = (result["meanZ"] as? NSNumber)?.doubleValue

        if isSweepCalibrating {
            appendSweepCalibrationSample(now: now, pairDistance: pixelDistance)
        }

        if let rawWidth {
            smoothedRawWidth = blend(old: smoothedRawWidth, new: rawWidth, alpha: 0.18)
            state.rawWidthText = String(format: "%.4f m", smoothedRawWidth ?? rawWidth)
        } else {
            state.rawWidthText = "pnp --"
        }
        if let pixelDistance {
            smoothedPixelDistance = blend(old: smoothedPixelDistance, new: pixelDistance, alpha: 0.25)
            state.pixelDistanceText = String(format: "%.1f px", smoothedPixelDistance ?? pixelDistance)
        } else {
            state.pixelDistanceText = "pix --"
        }
        if let meanZ {
            smoothedZ = blend(old: smoothedZ, new: meanZ, alpha: 0.18)
            state.depthHintText = String(format: "z %.4f m", smoothedZ ?? meanZ)
        } else {
            state.depthHintText = "z --"
        }

        if isSweepCalibrating {
            state.statusText = "calibrating sweep"
            if let pixelDistance {
                state.openingText = String(format: "record %.0fpx", pixelDistance)
            } else {
                state.openingText = "recording"
            }
            state.pathText = "recording samples"
            trackingConfidence = latestMarkerByID[0] != nil && latestMarkerByID[1] != nil ? 0.8 : 0.35
        } else if latestMarkerByID.isEmpty {
            consecutiveValidFrames = 0
            trackingConfidence = max(0.0, trackingConfidence * 0.94 - 0.015)
            let holdAge = lastValidPathAt.map { now.timeIntervalSince($0) }
            if let holdAge, holdAge < 0.80, filteredOpenPercent != nil {
                state.statusText = "tracking hold"
            } else {
                state.statusText = "searching"
                if filteredOpenPercent == nil {
                    state.openingText = "-"
                    state.openPercent = nil
                }
            }
            state.pathText = "path no marker"
        } else if updatePathOpening(now: now) {
            consecutiveValidFrames += 1
        } else {
            consecutiveValidFrames += 1
            trackingConfidence = uncalibratedMarkerConfidence()
            state.statusText = latestMarkerByID.count >= 2 ? "markers visible" : "single marker"
            updateOpeningAndCalibrationText(allowLegacyPixelFallback: false)
            if hasCompletePathCalibration {
                if let filteredOpenPercent {
                    state.openingText = String(format: "hold %.0f%%", filteredOpenPercent)
                    state.openPercent = filteredOpenPercent
                } else if let pixelDistance {
                    state.openingText = String(format: "path miss raw %.0fpx", pixelDistance)
                    state.openPercent = nil
                } else {
                    state.openingText = "path miss"
                    state.openPercent = nil
                }
                state.pathText = "path calibrated, marker off path"
            } else {
                state.openPercent = nil
                state.openingText = pixelDistance.map {
                    String(format: "raw %.0fpx need cal", $0)
                } ?? "need path cal"
                state.pathText = "path uncalibrated"
            }
        }

        state.isPathCalibrated = hasCompletePathCalibration
        updateConfidenceText()
        updateMarkerStates(timestampNS: timestampNS)
    }

    private func uncalibratedMarkerConfidence() -> Double {
        let visibleMarkers = Self.trackedMarkerIDs.compactMap { latestMarkerByID[$0] }
        guard !visibleMarkers.isEmpty else { return 0.0 }
        let markerScores = visibleMarkers.map { marker in
            let kindScore = Self.confidence(for: marker.kind)
            let areaScore = min(1.0, max(0.35, marker.area / 900.0))
            return kindScore * areaScore
        }
        let meanScore = markerScores.reduce(0.0, +) / Double(markerScores.count)
        let pairBonus = visibleMarkers.count >= 2 ? 0.18 : 0.0
        return max(0.0, min(0.82, meanScore + pairBonus))
    }

    private func appendSweepCalibrationSample(now: Date, pairDistance: Double?) {
        let centers = latestMarkerByID.mapValues(\.center)
        guard !centers.isEmpty else {
            updateSweepProgressText()
            return
        }
        let startedAt = sweepCalibrationStartedAt ?? now
        sweepCalibrationStartedAt = startedAt
        sweepCalibrationSamples.append(SweepCalibrationSample(
            time: now.timeIntervalSince(startedAt),
            centers: centers,
            pairDistance: pairDistance
        ))
        if sweepCalibrationSamples.count > 2400 {
            sweepCalibrationSamples.removeFirst(sweepCalibrationSamples.count - 2400)
        }
        updateSweepProgressText()
    }

    private func updateSweepProgressText() {
        let pairedDistances = sweepCalibrationSamples.compactMap(\.pairDistance)
        let bothCount = sweepCalibrationSamples.filter { $0.centers[0] != nil && $0.centers[1] != nil }.count
        guard let minDistance = pairedDistances.min(), let maxDistance = pairedDistances.max() else {
            state.sweepProgressText = "samples \(sweepCalibrationSamples.count), both \(bothCount), need ids 0+1"
            state.sweepCycleEstimate = 0
            state.sweepPairedSampleCount = pairedDistances.count
            state.sweepRangePixels = nil
            state.sweepReadyToFinish = false
            return
        }
        let cycles = Self.estimatedCycleCount(from: pairedDistances)
        let range = maxDistance - minDistance
        state.sweepCycleEstimate = cycles
        state.sweepPairedSampleCount = pairedDistances.count
        state.sweepRangePixels = range
        state.sweepReadyToFinish = pairedDistances.count >= 20
            && range >= Self.minSweepDistanceRangePixels
            && cycles >= 3.0
        state.sweepProgressText = String(
            format: "samples %d both %d range %.0f-%.0f cycles %.1f/3",
            sweepCalibrationSamples.count,
            bothCount,
            minDistance,
            maxDistance,
            cycles
        )
    }

    private func applySweepCalibration() -> Bool {
        let paired = sweepCalibrationSamples
            .filter { $0.centers[0] != nil && $0.centers[1] != nil && $0.pairDistance != nil }
            .sorted { ($0.pairDistance ?? 0.0) < ($1.pairDistance ?? 0.0) }

        guard paired.count >= 20 else {
            state.sweepCalibrationText = "cal need both ids"
            state.sweepProgressText = "not enough paired samples: \(paired.count)"
            state.calibrationText = "cal failed need id0+id1"
            return false
        }
        guard
            let minDistance = paired.first?.pairDistance,
            let maxDistance = paired.last?.pairDistance,
            maxDistance - minDistance >= Self.minSweepDistanceRangePixels
        else {
            state.sweepCalibrationText = "cal range too small"
            state.sweepProgressText = "move gripper through full travel"
            state.calibrationText = "cal failed small range"
            return false
        }

        let bucketCount = min(max(10, paired.count / 6), max(10, paired.count / 3))
        let closedBucket = Array(paired.prefix(bucketCount))
        let openBucket = Array(paired.suffix(bucketCount))
        var nextClosedCenters: [Int: CGPoint] = [:]
        var nextOpenCenters: [Int: CGPoint] = [:]
        for markerID in Self.trackedMarkerIDs {
            guard
                let closed = Self.averageCenter(markerID: markerID, samples: closedBucket),
                let open = Self.averageCenter(markerID: markerID, samples: openBucket),
                Self.isUsablePath(closed: closed, open: open)
            else {
                continue
            }
            nextClosedCenters[markerID] = closed
            nextOpenCenters[markerID] = open
        }

        guard !nextClosedCenters.isEmpty else {
            state.sweepCalibrationText = "cal path too short"
            state.sweepProgressText = String(format: "need path >= %.0fpx, repeat full travel", Self.minCalibrationTravelPixels)
            state.calibrationText = "cal failed short path"
            return false
        }

        closedCenters = nextClosedCenters
        openCenters = nextOpenCenters
        closedPixelDistance = closedBucket.compactMap(\.pairDistance).reduce(0.0, +) / Double(closedBucket.count)
        openPixelDistance = openBucket.compactMap(\.pairDistance).reduce(0.0, +) / Double(openBucket.count)
        filteredOpenPercent = nil
        state.sweepProgressText = String(
            format: "saved 0 %.0fpx 100 %.0fpx from %d samples",
            closedPixelDistance ?? 0.0,
            openPixelDistance ?? 0.0,
            paired.count
        )
        return true
    }

    private func updatePathOpening(now: Date) -> Bool {
        var estimates: [(id: Int, percent: Double, error: Double, weight: Double, suffix: String)] = []
        for markerID in Self.trackedMarkerIDs {
            guard
                let marker = latestMarkerByID[markerID],
                let start = closedCenters[markerID],
                let end = openCenters[markerID]
            else {
                continue
            }
            let projection = Self.project(marker.center, ontoSegmentFrom: start, to: end)
            guard projection.pathLength >= Self.minCalibrationTravelPixels else { continue }
            let maxPathError = Self.trackingPathErrorLimit(markerID: markerID, pathLength: projection.pathLength)
            let pathScore = max(0.0, 1.0 - projection.error / maxPathError)
            let areaScore = min(1.0, max(0.25, marker.area / 900.0))
            let sourceScore: Double
            let suffix: String
            switch marker.kind {
            case .detected:
                sourceScore = 1.0
                suffix = ""
            case .pathCandidate:
                sourceScore = 0.45
                suffix = "?"
            case .held:
                sourceScore = 0.20
                suffix = "h"
            case .rejected:
                sourceScore = 0.0
                suffix = "r"
            }
            let weight = max(0.001, pathScore * areaScore * sourceScore)
            estimates.append((markerID, projection.t * 100.0, projection.error, weight, suffix))
        }

        guard !estimates.isEmpty else { return false }
        let totalWeight = estimates.reduce(0.0) { $0 + $1.weight }
        let rawPercent = estimates.reduce(0.0) { $0 + $1.percent * $1.weight } / totalWeight
        let clampedRaw = max(0.0, min(100.0, rawPercent))
        filteredOpenPercent = blend(old: filteredOpenPercent, new: clampedRaw, alpha: 0.35)
        let output = filteredOpenPercent ?? clampedRaw

        let disagreement = estimates.count >= 2 ? abs(estimates[0].percent - estimates[1].percent) : 0.0
        let agreementScore = estimates.count >= 2 ? max(0.0, 1.0 - disagreement / 30.0) : 0.62
        let pathScore = min(1.0, totalWeight / Double(estimates.count))
        trackingConfidence = max(0.0, min(1.0, pathScore * agreementScore))
        lastValidPathAt = now
        state.statusText = trackingConfidence >= 0.55 ? "tracking path" : "path low conf"
        state.openingText = String(format: "%.0f%%", output)
        state.openPercent = output
        state.confidence = trackingConfidence

        let parts = estimates.map {
            String(format: "id%d%@ %.0f%% e%.1f", $0.id, $0.suffix, $0.percent, $0.error)
        }
        state.pathText = "path " + parts.joined(separator: " ")
        updateOpeningAndCalibrationText(allowLegacyPixelFallback: false)
        return true
    }

    private func updateOpeningAndCalibrationText(allowLegacyPixelFallback: Bool = true) {
        if
            allowLegacyPixelFallback,
            let pixelDistance = smoothedPixelDistance,
            let closedPixelDistance,
            let openPixelDistance,
            abs(openPixelDistance - closedPixelDistance) > 2.0
        {
            let percent = Self.clampedPercent(value: pixelDistance, low: closedPixelDistance, high: openPixelDistance)
            state.openingText = String(format: "%.0f%%", percent)
            state.openPercent = percent
        } else if filteredOpenPercent == nil {
            state.openingText = "set 0/100"
        }

        let prefix = hasCompletePathCalibration ? "cal saved" : "cal"
        state.calibrationText = "\(prefix) 0=\(Self.centerSummary(closedCenters)) 100=\(Self.centerSummary(openCenters))"
        state.isPathCalibrated = hasCompletePathCalibration
    }

    private func updateConfidenceText() {
        state.confidence = trackingConfidence
        state.confidenceText = String(format: "%.0f%%", trackingConfidence * 100.0)
    }

    private func updateMarkerStates(timestampNS: Int64?) {
        state.leftMarker = Self.debugState(
            markerID: 0,
            marker: latestMarkerByID[0],
            closed: closedCenters[0],
            open: openCenters[0],
            timestampNS: timestampNS
        )
        state.rightMarker = Self.debugState(
            markerID: 1,
            marker: latestMarkerByID[1],
            closed: closedCenters[1],
            open: openCenters[1],
            timestampNS: timestampNS
        )
    }

    private var hasCompletePathCalibration: Bool {
        Self.hasUsableCalibration(closedCenters: closedCenters, openCenters: openCenters)
    }

    private func saveCalibrationIfComplete() {
        guard hasCompletePathCalibration else { return }
        let saved = SavedGripperPathCalibration(
            version: 1,
            savedAt: Date(),
            frameWidth: state.videoFrameSize.width,
            frameHeight: state.videoFrameSize.height,
            closedCenters: Self.encodeCenters(closedCenters),
            openCenters: Self.encodeCenters(openCenters)
        )
        do {
            let data = try JSONEncoder().encode(saved)
            UserDefaults.standard.set(data, forKey: Self.calibrationDefaultsKey)
            loadedCalibrationFrameSize = state.videoFrameSize
            updateOpeningAndCalibrationText()
        } catch {
            state.calibrationText = "cal save failed"
        }
    }

    private func loadSavedCalibrationIfNeeded() {
        guard !didLoadSavedCalibration else { return }
        didLoadSavedCalibration = true
        guard
            let data = UserDefaults.standard.data(forKey: Self.calibrationDefaultsKey),
            let saved = try? JSONDecoder().decode(SavedGripperPathCalibration.self, from: data)
        else {
            return
        }
        let decodedClosedCenters = Self.decodeCenters(saved.closedCenters)
        let decodedOpenCenters = Self.decodeCenters(saved.openCenters)
        guard Self.hasUsableCalibration(closedCenters: decodedClosedCenters, openCenters: decodedOpenCenters) else {
            UserDefaults.standard.removeObject(forKey: Self.calibrationDefaultsKey)
            state.calibrationText = "cal reset short saved path"
            state.sweepProgressText = "run gripper cal full travel 3x"
            return
        }
        closedCenters = decodedClosedCenters
        openCenters = decodedOpenCenters
        loadedCalibrationFrameSize = CGSize(width: saved.frameWidth, height: saved.frameHeight)
        filteredOpenPercent = nil
        updateOpeningAndCalibrationText()
    }

    private func validateLoadedCalibrationForCurrentFrame() {
        guard let loadedCalibrationFrameSize else { return }
        guard state.videoFrameSize.width > 0, state.videoFrameSize.height > 0 else { return }
        let widthMatches = abs(loadedCalibrationFrameSize.width - state.videoFrameSize.width) <= 2.0
        let heightMatches = abs(loadedCalibrationFrameSize.height - state.videoFrameSize.height) <= 2.0
        if !widthMatches || !heightMatches {
            closedCenters = [:]
            openCenters = [:]
            filteredOpenPercent = nil
            self.loadedCalibrationFrameSize = nil
            UserDefaults.standard.removeObject(forKey: Self.calibrationDefaultsKey)
            state.calibrationText = "cal reset frame changed"
            state.sweepProgressText = "frame changed, run gripper cal"
            state.isPathCalibrated = false
        }
    }

    private func blend(old: Double?, new: Double, alpha: Double) -> Double {
        guard let old else { return new }
        return old * (1.0 - alpha) + new * alpha
    }
}

private extension GripperVisionProcessor {
    static func parseMarkers(from value: Any?, kind: MarkerOverlayKind, stablePrefix: String) -> [MarkerOverlay] {
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
            return MarkerOverlay(
                stableID: stableID,
                markerID: markerID,
                kind: kind,
                corners: corners,
                center: CGPoint(x: centerX, y: centerY),
                area: area,
                age: 0
            )
        }
    }

    static func filterDetectedMarkersForTracking(
        _ markers: [MarkerOverlay],
        closedCenters: [Int: CGPoint],
        openCenters: [Int: CGPoint],
        lastSeen: [Int: (marker: MarkerOverlay, seenAt: Date)],
        frameSize: CGSize,
        now: Date,
        usePathCalibration: Bool
    ) -> [MarkerOverlay] {
        markers.filter { marker in
            guard let markerID = marker.markerID, markerID == 0 || markerID == 1 else { return true }
            guard isInMarkerCandidateRegion(markerID: markerID, center: marker.center, frameSize: frameSize) else { return false }
            guard usePathCalibration, let start = closedCenters[markerID], let end = openCenters[markerID] else {
                return true
            }

            let projection = project(marker.center, ontoSegmentFrom: start, to: end)
            guard projection.pathLength >= minCalibrationTravelPixels else { return true }
            guard projection.error <= detectedPathErrorLimit(markerID: markerID, pathLength: projection.pathLength) else { return false }

            if let last = lastSeen[markerID] {
                let age = now.timeIntervalSince(last.seenAt)
                if age <= detectedJumpGuardSeconds {
                    let lastProjection = project(last.marker.center, ontoSegmentFrom: start, to: end)
                    let percentDelta = abs(projection.t - lastProjection.t) * 100.0
                    let maxPercentDelta = min(100.0, detectedMaxJumpPercentBase + age * detectedMaxJumpPercentPerSecond)
                    if percentDelta > maxPercentDelta {
                        return false
                    }
                }
            }
            return true
        }
    }

    static func matchRejectedCandidatesToPaths(
        rejected: [MarkerOverlay],
        existingMarkers: [Int: MarkerOverlay],
        closedCenters: [Int: CGPoint],
        openCenters: [Int: CGPoint],
        lastSeen: [Int: (marker: MarkerOverlay, seenAt: Date)],
        frameSize: CGSize,
        now: Date
    ) -> [Int: MarkerOverlay] {
        guard !rejected.isEmpty else { return [:] }
        var referencePercent: Double?
        let referenceEstimates = trackedMarkerIDs.compactMap { markerID -> Double? in
            guard let marker = existingMarkers[markerID], let start = closedCenters[markerID], let end = openCenters[markerID] else {
                return nil
            }
            let projection = project(marker.center, ontoSegmentFrom: start, to: end)
            guard projection.pathLength >= minCalibrationTravelPixels else { return nil }
            return projection.t * 100.0
        }
        if !referenceEstimates.isEmpty {
            referencePercent = referenceEstimates.reduce(0.0, +) / Double(referenceEstimates.count)
        }

        var matches: [Int: MarkerOverlay] = [:]
        var usedRejectedIDs = Set<String>()
        for markerID in trackedMarkerIDs where existingMarkers[markerID] == nil {
            let anchorTuning = anchorCandidateTuning(for: markerID)
            guard let start = closedCenters[markerID], let end = openCenters[markerID] else { continue }
            let pathLength = pointDistance(start, end)
            guard pathLength >= minCalibrationTravelPixels else { continue }

            var anchors: [(center: CGPoint, radius: Double)] = []
            if let last = lastSeen[markerID] {
                let age = now.timeIntervalSince(last.seenAt)
                if age <= pathCandidateAnchorSeconds {
                    anchors.append((
                        center: last.marker.center,
                        radius: max(anchorTuning.lastAnchorMinRadius, pathLength * anchorTuning.lastAnchorPathRate) + age * anchorTuning.lastAnchorAgeDrift
                    ))
                }
            }
            if let referencePercent {
                anchors.append((
                    center: interpolate(from: start, to: end, t: referencePercent / 100.0),
                    radius: max(anchorTuning.referenceAnchorMinRadius, pathLength * anchorTuning.referenceAnchorPathRate)
                ))
            }
            guard !anchors.isEmpty else { continue }

            var best: (marker: MarkerOverlay, score: Double)?
            for candidate in rejected where !usedRejectedIDs.contains(candidate.stableID) {
                guard isInMarkerCandidateRegion(markerID: markerID, center: candidate.center, frameSize: frameSize) else { continue }
                let tuning = candidateTuning(for: markerID, center: candidate.center, frameSize: frameSize)
                guard isPromotableRejectedCandidate(candidate, tuning: tuning) else { continue }
                let projection = project(candidate.center, ontoSegmentFrom: start, to: end)
                guard projection.pathLength >= minCalibrationTravelPixels else { continue }
                let maxPathError = promotionPathErrorLimit(pathLength: projection.pathLength, tuning: tuning)
                guard projection.error <= maxPathError else { continue }
                let percent = projection.t * 100.0
                guard let anchorScore = anchors.map({ pointDistance(candidate.center, $0.center) / $0.radius }).min(), anchorScore <= 1.0 else {
                    continue
                }
                let referencePenalty = referencePercent.map { abs(percent - $0) / tuning.referencePenaltyDivisor } ?? 0.0
                let score = projection.error / maxPathError + anchorScore * 1.20 + referencePenalty
                if best == nil || score < best!.score {
                    best = (candidate, score)
                }
            }

            guard let best else { continue }
            let bestTuning = candidateTuning(for: markerID, center: best.marker.center, frameSize: frameSize)
            guard best.score <= bestTuning.maxPromotionScore else { continue }
            usedRejectedIDs.insert(best.marker.stableID)
            matches[markerID] = MarkerOverlay(
                stableID: "path-\(markerID)",
                markerID: markerID,
                kind: .pathCandidate,
                corners: best.marker.corners,
                center: best.marker.center,
                area: best.marker.area,
                age: 0
            )
        }
        return matches
    }

    static func displayOverlays(
        detected: [MarkerOverlay],
        pathCandidates: [MarkerOverlay],
        rejected: [MarkerOverlay],
        lastSeen: [Int: (marker: MarkerOverlay, seenAt: Date)],
        now: Date
    ) -> [MarkerOverlay] {
        let primaryMarkers = detected + pathCandidates
        var overlays = rejected
            .sorted { $0.area > $1.area }
            .prefix(maxDebugRejectedOverlays)
            .map { $0 }
        overlays.append(contentsOf: primaryMarkers)

        let visibleIDs = Set(primaryMarkers.compactMap(\.markerID))
        for markerID in trackedMarkerIDs where !visibleIDs.contains(markerID) {
            guard let last = lastSeen[markerID] else { continue }
            let age = now.timeIntervalSince(last.seenAt)
            guard age <= markerHoldSeconds else { continue }
            overlays.append(MarkerOverlay(
                stableID: "held-\(markerID)",
                markerID: markerID,
                kind: .held,
                corners: last.marker.corners,
                center: last.marker.center,
                area: last.marker.area,
                age: age
            ))
        }
        return overlays
    }

    static func debugState(
        markerID: Int,
        marker: MarkerOverlay?,
        closed: CGPoint?,
        open: CGPoint?,
        timestampNS: Int64?
    ) -> MarkerDebugState {
        guard let marker else {
            return .empty(markerID: markerID)
        }
        let projection = closed.flatMap { closedPoint in
            open.map { openPoint in project(marker.center, ontoSegmentFrom: closedPoint, to: openPoint) }
        }
        return MarkerDebugState(
            markerID: markerID,
            detected: marker.kind == .detected || marker.kind == .pathCandidate,
            trackingState: stateText(for: marker.kind),
            confidence: confidence(for: marker.kind),
            centerPX: String(format: "%.0f,%.0f", marker.center.x, marker.center.y),
            cornersPX: cornersText(marker.corners),
            areaPX2: marker.area,
            angleDeg: angleDeg(marker.corners),
            pathPosition: projection?.t,
            lastSeenTimestampNS: timestampNS
        )
    }

    static func candidateTuning(for markerID: Int, center: CGPoint, frameSize: CGSize) -> PathCandidateTuning {
        guard markerID == 1 else { return leftCandidateTuning }
        guard frameSize.width > 1.0 else { return rightCenterCandidateTuning }
        let halfWidth = max(1.0, Double(frameSize.width) * 0.5)
        let rawEdgeFactor = (Double(center.x) - halfWidth) / halfWidth
        let edgeFactor = smoothstep(max(0.0, min(1.0, rawEdgeFactor)))
        return blendedTuning(rightCenterCandidateTuning, rightEdgeCandidateTuning, t: edgeFactor)
    }

    static func anchorCandidateTuning(for markerID: Int) -> PathCandidateTuning {
        markerID == 1 ? rightEdgeCandidateTuning : leftCandidateTuning
    }

    static func trackingCandidateTuning(for markerID: Int) -> PathCandidateTuning {
        markerID == 1 ? rightEdgeCandidateTuning : leftCandidateTuning
    }

    static func promotionPathErrorLimit(pathLength: Double, tuning: PathCandidateTuning) -> Double {
        max(tuning.minPathError, pathLength * tuning.pathErrorRate)
    }

    static func trackingPathErrorLimit(markerID: Int, pathLength: Double) -> Double {
        let tuning = trackingCandidateTuning(for: markerID)
        return max(12.0, pathLength * tuning.trackingPathErrorRate)
    }

    static func detectedPathErrorLimit(markerID: Int, pathLength: Double) -> Double {
        if markerID == 1 {
            return max(rightDetectedMinPathError, pathLength * rightDetectedPathErrorRate)
        }
        return max(leftDetectedMinPathError, pathLength * leftDetectedPathErrorRate)
    }

    static func isInMarkerCandidateRegion(markerID: Int, center: CGPoint, frameSize: CGSize) -> Bool {
        guard frameSize.width > 1.0 else { return true }
        switch markerID {
        case 0:
            return Double(center.x) <= Double(frameSize.width) * leftMarkerMaxFrameX
        case 1:
            return Double(center.x) >= Double(frameSize.width) * rightMarkerMinFrameX
        default:
            return true
        }
    }

    static func isPromotableRejectedCandidate(_ marker: MarkerOverlay, tuning: PathCandidateTuning) -> Bool {
        let points = marker.corners
        guard points.count == 4, marker.area >= tuning.minArea, isConvexQuad(points) else { return false }
        let sideLengths = (0..<4).map { pointDistance(points[$0], points[($0 + 1) % 4]) }
        guard let minSide = sideLengths.min(), let maxSide = sideLengths.max() else { return false }
        guard minSide >= 10.0, maxSide / minSide <= tuning.maxSideRatio else { return false }

        let perimeter = sideLengths.reduce(0.0, +)
        guard perimeter > 1e-6 else { return false }
        let compactness = marker.area / (perimeter * perimeter)
        guard compactness >= tuning.minCompactness else { return false }

        let xs = points.map(\.x)
        let ys = points.map(\.y)
        guard let minX = xs.min(), let maxX = xs.max(), let minY = ys.min(), let maxY = ys.max() else {
            return false
        }
        let boundsArea = Double(max(CGFloat(1.0), maxX - minX) * max(CGFloat(1.0), maxY - minY))
        guard marker.area / boundsArea >= tuning.minFillRatio else { return false }

        for index in 0..<4 {
            let prev = points[(index + 3) % 4]
            let current = points[index]
            let next = points[(index + 1) % 4]
            let v1 = CGPoint(x: prev.x - current.x, y: prev.y - current.y)
            let v2 = CGPoint(x: next.x - current.x, y: next.y - current.y)
            let len1 = pointDistance(.zero, v1)
            let len2 = pointDistance(.zero, v2)
            guard len1 > 1e-6, len2 > 1e-6 else { return false }
            let cosine = Double(v1.x * v2.x + v1.y * v2.y) / (len1 * len2)
            if abs(cosine) > tuning.maxCornerCosine {
                return false
            }
        }
        return true
    }

    static func isConvexQuad(_ points: [CGPoint]) -> Bool {
        guard points.count == 4 else { return false }
        var sign = 0
        for index in 0..<4 {
            let a = points[index]
            let b = points[(index + 1) % 4]
            let c = points[(index + 2) % 4]
            let ab = CGPoint(x: b.x - a.x, y: b.y - a.y)
            let bc = CGPoint(x: c.x - b.x, y: c.y - b.y)
            let cross = Double(ab.x * bc.y - ab.y * bc.x)
            guard abs(cross) > 1e-3 else { return false }
            let nextSign = cross > 0 ? 1 : -1
            if sign == 0 {
                sign = nextSign
            } else if sign != nextSign {
                return false
            }
        }
        return true
    }

    static func blendedTuning(_ lhs: PathCandidateTuning, _ rhs: PathCandidateTuning, t: Double) -> PathCandidateTuning {
        let clampedT = max(0.0, min(1.0, t))
        func lerp(_ a: Double, _ b: Double) -> Double { a + (b - a) * clampedT }
        return PathCandidateTuning(
            minArea: lerp(lhs.minArea, rhs.minArea),
            maxSideRatio: lerp(lhs.maxSideRatio, rhs.maxSideRatio),
            minCompactness: lerp(lhs.minCompactness, rhs.minCompactness),
            minFillRatio: lerp(lhs.minFillRatio, rhs.minFillRatio),
            maxCornerCosine: lerp(lhs.maxCornerCosine, rhs.maxCornerCosine),
            minPathError: lerp(lhs.minPathError, rhs.minPathError),
            pathErrorRate: lerp(lhs.pathErrorRate, rhs.pathErrorRate),
            lastAnchorMinRadius: lerp(lhs.lastAnchorMinRadius, rhs.lastAnchorMinRadius),
            lastAnchorPathRate: lerp(lhs.lastAnchorPathRate, rhs.lastAnchorPathRate),
            lastAnchorAgeDrift: lerp(lhs.lastAnchorAgeDrift, rhs.lastAnchorAgeDrift),
            referenceAnchorMinRadius: lerp(lhs.referenceAnchorMinRadius, rhs.referenceAnchorMinRadius),
            referenceAnchorPathRate: lerp(lhs.referenceAnchorPathRate, rhs.referenceAnchorPathRate),
            referencePenaltyDivisor: lerp(lhs.referencePenaltyDivisor, rhs.referencePenaltyDivisor),
            maxPromotionScore: lerp(lhs.maxPromotionScore, rhs.maxPromotionScore),
            trackingPathErrorRate: lerp(lhs.trackingPathErrorRate, rhs.trackingPathErrorRate)
        )
    }

    static func hasUsableCalibration(closedCenters: [Int: CGPoint], openCenters: [Int: CGPoint]) -> Bool {
        trackedMarkerIDs.contains { markerID in
            guard let closed = closedCenters[markerID], let open = openCenters[markerID] else {
                return false
            }
            return isUsablePath(closed: closed, open: open)
        }
    }

    static func isUsablePath(closed: CGPoint, open: CGPoint) -> Bool {
        pointDistance(closed, open) >= minCalibrationTravelPixels
    }

    static func averageCenter(markerID: Int, samples: [SweepCalibrationSample]) -> CGPoint? {
        let points = samples.compactMap { $0.centers[markerID] }
        guard !points.isEmpty else { return nil }
        let sum = points.reduce(CGPoint.zero) { partial, point in
            CGPoint(x: partial.x + point.x, y: partial.y + point.y)
        }
        return CGPoint(x: sum.x / CGFloat(points.count), y: sum.y / CGFloat(points.count))
    }

    static func estimatedCycleCount(from distances: [Double]) -> Double {
        guard let minValue = distances.min(), let maxValue = distances.max() else { return 0.0 }
        let range = maxValue - minValue
        guard range >= 12.0 else { return 0.0 }
        let lowThreshold = minValue + range * 0.28
        let highThreshold = minValue + range * 0.72
        var state = 0
        var transitions = 0
        for value in distances {
            let nextState: Int
            if value <= lowThreshold {
                nextState = -1
            } else if value >= highThreshold {
                nextState = 1
            } else {
                continue
            }
            if state != 0, nextState != state {
                transitions += 1
            }
            state = nextState
        }
        return max(0.0, Double(transitions) / 2.0)
    }

    static func clampedPercent(value: Double, low: Double, high: Double) -> Double {
        let span = high - low
        guard abs(span) > 1e-9 else { return 0.0 }
        let percent = (value - low) / span * 100.0
        return max(0.0, min(100.0, percent))
    }

    static func centerSummary(_ centers: [Int: CGPoint]) -> String {
        let parts = trackedMarkerIDs.compactMap { id -> String? in
            guard let point = centers[id] else { return nil }
            return String(format: "%d(%.0f,%.0f)", id, point.x, point.y)
        }
        return parts.isEmpty ? "--" : parts.joined(separator: "/")
    }

    static func encodeCenters(_ centers: [Int: CGPoint]) -> [String: SavedPoint] {
        var encoded: [String: SavedPoint] = [:]
        for (id, point) in centers {
            encoded[String(id)] = SavedPoint(point)
        }
        return encoded
    }

    static func decodeCenters(_ centers: [String: SavedPoint]) -> [Int: CGPoint] {
        var decoded: [Int: CGPoint] = [:]
        for (idText, point) in centers {
            guard let id = Int(idText) else { continue }
            decoded[id] = point.cgPoint
        }
        return decoded
    }

    static func centerDistance(_ lhs: CGPoint?, _ rhs: CGPoint?) -> Double? {
        guard let lhs, let rhs else { return nil }
        return pointDistance(lhs, rhs)
    }

    static func pointDistance(_ lhs: CGPoint, _ rhs: CGPoint) -> Double {
        let dx = Double(rhs.x - lhs.x)
        let dy = Double(rhs.y - lhs.y)
        return sqrt(dx * dx + dy * dy)
    }

    static func interpolate(from start: CGPoint, to end: CGPoint, t: Double) -> CGPoint {
        let clampedT = max(0.0, min(1.0, t))
        return CGPoint(
            x: CGFloat(Double(start.x) + Double(end.x - start.x) * clampedT),
            y: CGFloat(Double(start.y) + Double(end.y - start.y) * clampedT)
        )
    }

    static func project(_ point: CGPoint, ontoSegmentFrom start: CGPoint, to end: CGPoint) -> (t: Double, error: Double, pathLength: Double) {
        let vx = Double(end.x - start.x)
        let vy = Double(end.y - start.y)
        let wx = Double(point.x - start.x)
        let wy = Double(point.y - start.y)
        let lengthSquared = vx * vx + vy * vy
        guard lengthSquared > 1e-9 else {
            let error = hypot(Double(point.x - start.x), Double(point.y - start.y))
            return (0.0, error, 0.0)
        }
        let rawT = (wx * vx + wy * vy) / lengthSquared
        let t = max(0.0, min(1.0, rawT))
        let projection = CGPoint(x: CGFloat(Double(start.x) + t * vx), y: CGFloat(Double(start.y) + t * vy))
        let error = hypot(Double(point.x - projection.x), Double(point.y - projection.y))
        return (t, error, sqrt(lengthSquared))
    }

    static func smoothstep(_ value: Double) -> Double {
        let t = max(0.0, min(1.0, value))
        return t * t * (3.0 - 2.0 * t)
    }

    static func stateText(for kind: MarkerOverlayKind) -> String {
        switch kind {
        case .detected:
            return "detected"
        case .pathCandidate:
            return "path_candidate"
        case .rejected:
            return "rejected"
        case .held:
            return "held"
        }
    }

    static func confidence(for kind: MarkerOverlayKind) -> Double {
        switch kind {
        case .detected:
            return 1.0
        case .pathCandidate:
            return 0.45
        case .held:
            return 0.20
        case .rejected:
            return 0.0
        }
    }

    static func cornersText(_ corners: [CGPoint]) -> String {
        corners
            .prefix(4)
            .map { String(format: "%.0f,%.0f", $0.x, $0.y) }
            .joined(separator: " ")
    }

    static func angleDeg(_ corners: [CGPoint]) -> Double {
        guard corners.count >= 2 else { return 0 }
        let dx = Double(corners[1].x - corners[0].x)
        let dy = Double(corners[1].y - corners[0].y)
        return atan2(dy, dx) * 180.0 / .pi
    }

    static func intrinsicsText(sampleBuffer: CMSampleBuffer) -> String? {
        if let text = intrinsicsText(from: CMGetAttachment(
            sampleBuffer,
            key: kCMSampleBufferAttachmentKey_CameraIntrinsicMatrix,
            attachmentModeOut: nil
        )) {
            return text
        }
        guard
            let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[CFString: Any]],
            let first = attachments.first,
            let text = intrinsicsText(from: first[kCMSampleBufferAttachmentKey_CameraIntrinsicMatrix])
        else {
            return nil
        }
        return text
    }

    static func intrinsicsText(from value: Any?) -> String? {
        let data: Data
        if let swiftData = value as? Data {
            data = swiftData
        } else if let nsData = value as? NSData {
            data = nsData as Data
        } else {
            return nil
        }
        guard data.count >= MemoryLayout<simd_float3x3>.size else {
            return "K data \(data.count)B"
        }
        let matrix = data.withUnsafeBytes { rawBuffer -> simd_float3x3 in
            rawBuffer.load(as: simd_float3x3.self)
        }
        return String(
            format: "K fx %.1f fy %.1f cx %.1f cy %.1f",
            matrix.columns.0.x,
            matrix.columns.1.y,
            matrix.columns.2.x,
            matrix.columns.2.y
        )
    }

    static func timestampNS(for sampleBuffer: CMSampleBuffer) -> Int64? {
        let seconds = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        guard seconds.isFinite else { return nil }
        return Int64(seconds * 1_000_000_000)
    }
}

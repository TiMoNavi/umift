import Foundation

struct ArchiveFrameSideDataV2 {
    var sequence: UInt64
    var captureTimestampNS: Int64
    var metadataPayload: Data
    var depthPayload: Data?
    var recordingEventPayloads: [Data]
}

struct ArchiveFrameBundleV2 {
    var accessUnit: H264EncodedAccessUnit
    var sideData: ArchiveFrameSideDataV2

    var sequence: UInt64 { accessUnit.sequence }
    var captureTimestampNS: Int64 { accessUnit.captureTimestampNS }
    var isKeyFrame: Bool { accessUnit.isKeyFrame }
}

struct ReliableEncodedVideoSpoolSnapshot {
    var frameCount: Int
    var byteCount: Int
    var durationS: Double
    var firstSequence: UInt64?
    var lastSequence: UInt64?
    var ackedThroughSequence: UInt64?

    var displayText: String {
        let ackText = ackedThroughSequence.map(String.init) ?? "--"
        return String(
            format: "h264 spool %.2fs %d frames %.1fMB ack %@",
            durationS,
            frameCount,
            Double(byteCount) / 1e6,
            ackText
        )
    }
}

enum ReliableEncodedVideoSpoolAppendResult {
    case accepted(ReliableEncodedVideoSpoolSnapshot)
    case overflow(ReliableEncodedVideoSpoolSnapshot)
}

final class ReliableEncodedVideoSpool {
    private let maxDurationNS: Int64
    private let maxBytes: Int
    private var frames: [ArchiveFrameBundleV2] = []
    private var byteCount = 0
    private var ackedThroughSequence: UInt64?

    init(maxDurationS: Double, maxBytes: Int) {
        maxDurationNS = Int64(maxDurationS * 1_000_000_000)
        self.maxBytes = maxBytes
    }

    func append(
        _ frame: ArchiveFrameBundleV2,
        reliableMode: Bool
    ) -> ReliableEncodedVideoSpoolAppendResult {
        if let last = frames.last, frame.sequence != last.sequence + 1 {
            if reliableMode {
                return .overflow(makeSnapshot())
            }
            removeFirst(frames.count)
        }
        if !reliableMode, frames.isEmpty, !frame.isKeyFrame {
            return .accepted(makeSnapshot())
        }
        frames.append(frame)
        byteCount += frameStorageBytes(frame)

        if !reliableMode {
            retainLatestDecodableGOP()
        }

        let snapshot = makeSnapshot()
        if reliableMode, exceedsLimit(snapshot) {
            let rejectedFrame = frames.removeLast()
            byteCount -= frameStorageBytes(rejectedFrame)
            return .overflow(snapshot)
        }
        return .accepted(snapshot)
    }

    func prepareForFirstConnection() -> ReliableEncodedVideoSpoolSnapshot {
        retainLatestDecodableGOP()
        if let firstSequence = frames.first?.sequence, firstSequence > 0 {
            ackedThroughSequence = firstSequence - 1
        }
        return makeSnapshot()
    }

    func acknowledge(through sequence: UInt64) -> ReliableEncodedVideoSpoolSnapshot {
        if let current = ackedThroughSequence, sequence <= current {
            return makeSnapshot()
        }
        ackedThroughSequence = sequence
        removePrefix(while: { $0.sequence <= sequence })
        return makeSnapshot()
    }

    func frames(after sequence: UInt64?) -> [ArchiveFrameBundleV2] {
        guard let sequence else { return frames }
        return frames.filter { $0.sequence > sequence }
    }

    func resetSentStateForReconnect() -> UInt64? {
        ackedThroughSequence
    }

    func snapshot() -> ReliableEncodedVideoSpoolSnapshot {
        makeSnapshot()
    }

    private func retainLatestDecodableGOP() {
        guard let latestKeyFrameIndex = frames.lastIndex(where: \.isKeyFrame), latestKeyFrameIndex > 0 else {
            return
        }
        removeFirst(latestKeyFrameIndex)
    }

    private func exceedsLimit(_ snapshot: ReliableEncodedVideoSpoolSnapshot) -> Bool {
        snapshot.byteCount > maxBytes || Int64(snapshot.durationS * 1_000_000_000) > maxDurationNS
    }

    private func removePrefix(while predicate: (ArchiveFrameBundleV2) -> Bool) {
        var count = 0
        while count < frames.count, predicate(frames[count]) {
            count += 1
        }
        removeFirst(count)
    }

    private func removeFirst(_ count: Int) {
        guard count > 0 else { return }
        for frame in frames.prefix(count) {
            byteCount -= frameStorageBytes(frame)
        }
        frames.removeFirst(count)
    }

    private func frameStorageBytes(_ frame: ArchiveFrameBundleV2) -> Int {
        let videoBytes = frame.accessUnit.data.count
        let parameterSetBytes = frame.accessUnit.parameterSets.reduce(0) { partial, data in
            partial + data.count
        }
        let metadataBytes = frame.sideData.metadataPayload.count
        let depthBytes = frame.sideData.depthPayload?.count ?? 0
        let eventBytes = frame.sideData.recordingEventPayloads.reduce(0) { partial, data in
            partial + data.count
        }
        return videoBytes + parameterSetBytes + metadataBytes + depthBytes + eventBytes
    }

    private func makeSnapshot() -> ReliableEncodedVideoSpoolSnapshot {
        let durationS: Double
        if let first = frames.first, let last = frames.last {
            durationS = max(0, Double(last.captureTimestampNS - first.captureTimestampNS) / 1e9)
        } else {
            durationS = 0
        }
        return ReliableEncodedVideoSpoolSnapshot(
            frameCount: frames.count,
            byteCount: max(0, byteCount),
            durationS: durationS,
            firstSequence: frames.first?.sequence,
            lastSequence: frames.last?.sequence,
            ackedThroughSequence: ackedThroughSequence
        )
    }
}

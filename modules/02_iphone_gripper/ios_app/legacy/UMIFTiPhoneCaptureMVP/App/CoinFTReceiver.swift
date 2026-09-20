import Foundation
import Network

enum CoinFTBinding: String, CaseIterable, Identifiable {
    case normal = "normal"
    case swapped = "swap"

    var id: String { rawValue }

    var leftLabel: String {
        switch self {
        case .normal:
            return "serial L -> LF"
        case .swapped:
            return "serial R -> LF"
        }
    }

    var rightLabel: String {
        switch self {
        case .normal:
            return "serial R -> RF"
        case .swapped:
            return "serial L -> RF"
        }
    }
}

@MainActor
final class CoinFTReceiver: ObservableObject {
    @Published var isListening = false
    @Published var statusText = "CoinFT idle"
    @Published var sourceID = "none"
    @Published var packetRateText = "0.0 Hz"
    @Published var lastPacketAgeText = "-"
    @Published var droppedPacketCount = 0
    @Published var latestLeftText = "L -"
    @Published var latestRightText = "R -"
    @Published var binding = CoinFTBinding.normal

    private let port: NWEndpoint.Port = 43001
    private var listener: NWListener?
    private var latestSequence: Int?
    private var packetWindow: [Date] = []
    private var lastPacketDate: Date?
    private var ageTimer: Timer?

    func start() {
        guard !isListening else { return }
        do {
            let listener = try NWListener(using: .udp, on: port)
            listener.stateUpdateHandler = { [weak self] state in
                Task { @MainActor in
                    self?.handleListenerState(state)
                }
            }
            listener.newConnectionHandler = { [weak self] connection in
                connection.start(queue: .global(qos: .userInitiated))
                self?.receiveNext(on: connection)
            }
            listener.start(queue: .global(qos: .userInitiated))
            self.listener = listener
            isListening = true
            statusText = "CoinFT UDP \(port.rawValue)"
            startAgeTimer()
        } catch {
            statusText = "CoinFT listen failed: \(error.localizedDescription)"
        }
    }

    func stop() {
        listener?.cancel()
        listener = nil
        isListening = false
        ageTimer?.invalidate()
        ageTimer = nil
        statusText = "CoinFT stopped"
    }

    private func handleListenerState(_ state: NWListener.State) {
        switch state {
        case .ready:
            isListening = true
            statusText = "CoinFT listening \(port.rawValue)"
        case .failed(let error):
            isListening = false
            statusText = "CoinFT failed: \(error.localizedDescription)"
        case .cancelled:
            isListening = false
            statusText = "CoinFT stopped"
        default:
            break
        }
    }

    nonisolated private func receiveNext(on connection: NWConnection) {
        connection.receiveMessage { [weak self] data, _, _, error in
            if let data {
                Task { @MainActor in
                    self?.handlePacketData(data)
                }
            }
            if error == nil {
                self?.receiveNext(on: connection)
            }
        }
    }

    private func handlePacketData(_ data: Data) {
        guard let packet = try? JSONDecoder().decode(CoinFTLANPacket.self, from: data) else {
            statusText = "CoinFT parse failed"
            return
        }

        let now = Date()
        if let latestSequence, packet.seq > latestSequence + 1 {
            droppedPacketCount += packet.seq - latestSequence - 1
        }
        latestSequence = packet.seq
        lastPacketDate = now
        sourceID = packet.sourceID
        latestLeftText = Self.formatSide("L", packet.left)
        latestRightText = Self.formatSide("R", packet.right)
        packetWindow.append(now)
        packetWindow = packetWindow.filter { now.timeIntervalSince($0) <= 2.0 }
        packetRateText = String(format: "%.1f Hz", Double(packetWindow.count) / 2.0)
        updatePacketAge(now: now)
    }

    private func startAgeTimer() {
        ageTimer?.invalidate()
        ageTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.updatePacketAge(now: Date())
            }
        }
    }

    private func updatePacketAge(now: Date) {
        guard let lastPacketDate else {
            lastPacketAgeText = "-"
            return
        }
        lastPacketAgeText = String(format: "%.0f ms", now.timeIntervalSince(lastPacketDate) * 1000.0)
    }

    private static func formatSide(_ prefix: String, _ side: CoinFTLANPacket.Side) -> String {
        if let wrench = side.wrench, wrench.count >= 6 {
            return String(
                format: "%@ Fx %.2f Fy %.2f Fz %.2f",
                prefix,
                wrench[0],
                wrench[1],
                wrench[2]
            )
        }
        guard let minRaw = side.raw.min(), let maxRaw = side.raw.max() else {
            return "\(prefix) raw -"
        }
        return "\(prefix) raw \(minRaw)...\(maxRaw)"
    }
}

private struct CoinFTLANPacket: Decodable {
    struct Side: Decodable {
        let raw: [Int]
        let wrench: [Double]?
    }

    let schema: String
    let seq: Int
    let sourceID: String
    let left: Side
    let right: Side

    enum CodingKeys: String, CodingKey {
        case schema
        case seq
        case sourceID = "source_id"
        case left
        case right
    }
}

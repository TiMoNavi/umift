import Foundation
import Network

struct ArchiveTransportSnapshot {
    var sentThroughSequence: UInt64?
    var ackedThroughSequence: UInt64?
    var contentProcessedPackets: UInt64
    var contentProcessedBytes: UInt64
    var spool: ReliableEncodedVideoSpoolSnapshot

    var displayText: String {
        let sentText = sentThroughSequence.map(String.init) ?? "--"
        let ackText = ackedThroughSequence.map(String.init) ?? "--"
        let lag: UInt64
        if let sentThroughSequence, let ackedThroughSequence, sentThroughSequence >= ackedThroughSequence {
            lag = sentThroughSequence - ackedThroughSequence
        } else {
            lag = 0
        }
        return "v2 sent \(sentText) ack \(ackText) lag \(lag) | \(spool.displayText)"
    }
}

final class ArchiveTransport {
    static let defaultPort: UInt16 = 17381

    var onStatusChange: ((RecordingTransportStatus) -> Void)?
    var onSnapshot: ((ArchiveTransportSnapshot) -> Void)?
    var onIntegrityFailure: ((String) -> Void)?

    private let configuration: H264VideoEncoderConfiguration
    private let queue = DispatchQueue(label: "local.umift.capturecore.archive-transport-v2")
    private let spool = ReliableEncodedVideoSpool(maxDurationS: 10, maxBytes: 64 * 1024 * 1024)
    private var sessionID: String
    private var listener: NWListener?
    private var connection: NWConnection?
    private var receiveParser = StreamPacketParserV2()
    private var currentPort = ArchiveTransport.defaultPort
    private var codecConfigurationPayload: Data?
    private var codecConfigurationSequence: UInt64?
    private var codecConfigurationTimestampNS: Int64 = 0
    private var connectionReady = false
    private var preambleSent = false
    private var hasEstablishedFirstConnection = false
    private var sendInProgress = false
    private var sentThroughSequence: UInt64?
    private var ackedThroughSequence: UInt64?
    private var contentProcessedPackets: UInt64 = 0
    private var contentProcessedBytes: UInt64 = 0
    private var overflowed = false
    private var requiresRecoveryKeyFrame = false
    private var pendingAccessUnits: [UInt64: H264EncodedAccessUnit] = [:]
    private var pendingSideData: [UInt64: ArchiveFrameSideDataV2] = [:]

    init(configuration: H264VideoEncoderConfiguration) {
        self.configuration = configuration
        sessionID = Self.makeSessionID()
    }

    func start(port: UInt16 = ArchiveTransport.defaultPort) {
        queue.async { [weak self] in
            self?.startOnQueue(port: port)
        }
    }

    func stop() {
        queue.async { [weak self] in
            guard let self else { return }
            self.connection?.cancel()
            self.connection = nil
            self.listener?.cancel()
            self.listener = nil
            self.connectionReady = false
            self.notifyStatus(.disconnected)
        }
    }

    func enqueue(_ accessUnit: H264EncodedAccessUnit) {
        queue.async { [weak self] in
            self?.enqueueOnQueue(accessUnit)
        }
    }

    func enqueueSideData(_ sideData: ArchiveFrameSideDataV2) {
        queue.async { [weak self] in
            self?.enqueueSideDataOnQueue(sideData)
        }
    }

    func failFrameIntegrity(_ reason: String) {
        queue.async { [weak self] in
            guard let self else { return }
            self.overflowed = true
            self.requiresRecoveryKeyFrame = true
            self.connection?.cancel()
            self.failIntegrity(reason)
        }
    }

    private func startOnQueue(port: UInt16) {
        guard listener == nil else {
            publishStatus()
            return
        }
        do {
            try StreamProtocolV2.validateGoldenVector()
            let parameters = NWParameters.tcp
            parameters.allowLocalEndpointReuse = true
            let listener = try NWListener(
                using: parameters,
                on: NWEndpoint.Port(rawValue: port) ?? NWEndpoint.Port(integerLiteral: Self.defaultPort)
            )
            currentPort = port
            self.listener = listener
            listener.newConnectionHandler = { [weak self] connection in
                self?.acceptOnQueue(connection)
            }
            listener.stateUpdateHandler = { [weak self] state in
                self?.handleListenerStateOnQueue(state)
            }
            listener.start(queue: queue)
            notifyStatus(.listening(port))
            print("[UMIFT][archive-v2] listening \(port) session=\(sessionID)")
        } catch {
            failTransport("archive v2 start failed: \(error)")
        }
    }

    private func enqueueOnQueue(_ accessUnit: H264EncodedAccessUnit) {
        if accessUnit.isKeyFrame, !accessUnit.parameterSets.isEmpty {
            do {
                codecConfigurationPayload = try StreamProtocolV2.makeVideoCodecConfigPayload(
                    width: configuration.width,
                    height: configuration.height,
                    framesPerSecond: configuration.framesPerSecond,
                    averageBitRate: configuration.averageBitRate,
                    nalUnitHeaderLength: accessUnit.nalUnitHeaderLength,
                    parameterSets: accessUnit.parameterSets
                )
                codecConfigurationSequence = accessUnit.sequence
                codecConfigurationTimestampNS = accessUnit.captureTimestampNS
            } catch {
                failIntegrity("codec config encode failed seq=\(accessUnit.sequence): \(error)")
                return
            }
        }

        guard pendingAccessUnits[accessUnit.sequence] == nil else {
            failIntegrity("duplicate encoded access unit seq=\(accessUnit.sequence)")
            return
        }
        pendingAccessUnits[accessUnit.sequence] = accessUnit
        assembleFrameIfReadyOnQueue(sequence: accessUnit.sequence)
        validatePendingAssemblyOnQueue()
    }

    private func enqueueSideDataOnQueue(_ sideData: ArchiveFrameSideDataV2) {
        guard pendingSideData[sideData.sequence] == nil else {
            failIntegrity("duplicate frame side data seq=\(sideData.sequence)")
            return
        }
        pendingSideData[sideData.sequence] = sideData
        assembleFrameIfReadyOnQueue(sequence: sideData.sequence)
        validatePendingAssemblyOnQueue()
    }

    private func assembleFrameIfReadyOnQueue(sequence: UInt64) {
        guard let accessUnit = pendingAccessUnits[sequence],
              let sideData = pendingSideData[sequence]
        else { return }
        guard accessUnit.captureTimestampNS == sideData.captureTimestampNS else {
            overflowed = true
            failIntegrity(
                "frame timestamp mismatch seq=\(sequence) video=\(accessUnit.captureTimestampNS) side=\(sideData.captureTimestampNS)"
            )
            return
        }
        pendingAccessUnits.removeValue(forKey: sequence)
        pendingSideData.removeValue(forKey: sequence)
        let bundle = ArchiveFrameBundleV2(accessUnit: accessUnit, sideData: sideData)
        let reliableMode = hasEstablishedFirstConnection
            && !overflowed
            && !requiresRecoveryKeyFrame
        let result = spool.append(bundle, reliableMode: reliableMode)
        switch result {
        case .accepted(let snapshot):
            if requiresRecoveryKeyFrame, snapshot.firstSequence != nil {
                requiresRecoveryKeyFrame = false
            }
            publishSnapshot(spoolSnapshot: snapshot)
        case .overflow(let snapshot):
            overflowed = true
            requiresRecoveryKeyFrame = true
            connection?.cancel()
            publishSnapshot(spoolSnapshot: snapshot)
            failIntegrity(
                "encoded spool overflow frames=\(snapshot.frameCount) bytes=\(snapshot.byteCount) duration=\(snapshot.durationS)s"
            )
            return
        }

        if connectionReady, !preambleSent {
            sendPreambleIfReadyOnQueue()
        } else {
            drainFramesOnQueue()
        }
    }

    private func validatePendingAssemblyOnQueue() {
        let pendingCount = pendingAccessUnits.count + pendingSideData.count
        guard pendingCount > 60 else { return }
        overflowed = true
        requiresRecoveryKeyFrame = true
        connection?.cancel()
        failIntegrity(
            "frame bundle assembly overflow video=\(pendingAccessUnits.count) side=\(pendingSideData.count)"
        )
    }

    private func acceptOnQueue(_ newConnection: NWConnection) {
        connection?.cancel()
        connection = newConnection
        receiveParser = StreamPacketParserV2()
        connectionReady = false
        preambleSent = false
        sendInProgress = false
        sentThroughSequence = ackedThroughSequence
        newConnection.stateUpdateHandler = { [weak self, weak newConnection] state in
            self?.handleConnectionStateOnQueue(state, connection: newConnection)
        }
        newConnection.start(queue: queue)
        print("[UMIFT][archive-v2] client accepted")
    }

    private func handleListenerStateOnQueue(_ state: NWListener.State) {
        switch state {
        case .ready:
            publishStatus()
        case .failed(let error):
            failTransport("archive listener failed: \(error.localizedDescription)")
        case .cancelled:
            notifyStatus(.disconnected)
        default:
            break
        }
    }

    private func handleConnectionStateOnQueue(_ state: NWConnection.State, connection: NWConnection?) {
        guard connection === self.connection else { return }
        switch state {
        case .ready:
            connectionReady = true
            if overflowed {
                beginRecoveryGenerationOnQueue()
            }
            if !hasEstablishedFirstConnection {
                let snapshot = spool.prepareForFirstConnection()
                ackedThroughSequence = snapshot.ackedThroughSequence
                sentThroughSequence = ackedThroughSequence
                hasEstablishedFirstConnection = true
                publishSnapshot(spoolSnapshot: snapshot)
            }
            notifyStatus(.connected)
            receiveNextOnQueue()
            sendPreambleIfReadyOnQueue()
        case .failed(let error):
            print("[UMIFT][archive-v2] connection failed \(error.localizedDescription)")
            disconnectOnQueue(connection)
        case .cancelled:
            disconnectOnQueue(connection)
        default:
            break
        }
    }

    private func disconnectOnQueue(_ disconnectedConnection: NWConnection?) {
        guard disconnectedConnection === connection else { return }
        connection = nil
        connectionReady = false
        preambleSent = false
        sendInProgress = false
        sentThroughSequence = spool.resetSentStateForReconnect()
        publishStatus()
    }

    private func sendPreambleIfReadyOnQueue() {
        guard connectionReady, !preambleSent, !sendInProgress else { return }
        guard let codecConfigurationPayload, let codecConfigurationSequence else { return }
        guard let firstSequence = spool.snapshot().firstSequence else { return }
        do {
            let sessionPacket = try StreamProtocolV2.encode(StreamPacketV2(
                type: .sessionStart,
                sessionID: sessionID,
                sequence: firstSequence,
                captureTimestampNS: codecConfigurationTimestampNS,
                payload: StreamProtocolV2.makeSessionStartPayload(
                    width: configuration.width,
                    height: configuration.height,
                    framesPerSecond: configuration.framesPerSecond,
                    averageBitRate: configuration.averageBitRate
                )
            ))
            let codecPacket = try StreamProtocolV2.encode(StreamPacketV2(
                type: .videoCodecConfig,
                sessionID: sessionID,
                sequence: codecConfigurationSequence,
                captureTimestampNS: codecConfigurationTimestampNS,
                payload: codecConfigurationPayload
            ))
            sendControlPacketOnQueue(sessionPacket) { [weak self] in
                self?.sendControlPacketOnQueue(codecPacket) { [weak self] in
                    guard let self else { return }
                    self.preambleSent = true
                    self.drainFramesOnQueue()
                }
            }
        } catch {
            failTransport("archive preamble encode failed: \(error)")
        }
    }

    private func sendControlPacketOnQueue(_ data: Data, completion: @escaping () -> Void) {
        guard let connection, connectionReady else { return }
        sendInProgress = true
        connection.send(content: data, completion: .contentProcessed { [weak self] error in
            guard let self else { return }
            self.sendInProgress = false
            if let error {
                self.failTransport("archive control send failed: \(error.localizedDescription)")
                return
            }
            self.contentProcessedPackets &+= 1
            self.contentProcessedBytes &+= UInt64(data.count)
            completion()
        })
    }

    private func drainFramesOnQueue() {
        guard connectionReady, preambleSent, !sendInProgress, let connection else { return }
        guard let bundle = spool.frames(after: sentThroughSequence).first else {
            publishSnapshot(spoolSnapshot: spool.snapshot())
            return
        }
        do {
            var packetData = Data()
            packetData.append(try StreamProtocolV2.encode(StreamPacketV2(
                type: .frameMetadata,
                sessionID: sessionID,
                sequence: bundle.sequence,
                captureTimestampNS: bundle.captureTimestampNS,
                payload: bundle.sideData.metadataPayload
            )))
            if let depthPayload = bundle.sideData.depthPayload {
                packetData.append(try StreamProtocolV2.encode(StreamPacketV2(
                    type: .depthFrame,
                    sessionID: sessionID,
                    sequence: bundle.sequence,
                    captureTimestampNS: bundle.captureTimestampNS,
                    payload: depthPayload
                )))
            }
            for eventPayload in bundle.sideData.recordingEventPayloads {
                packetData.append(try StreamProtocolV2.encode(StreamPacketV2(
                    type: .recordingEvent,
                    sessionID: sessionID,
                    sequence: bundle.sequence,
                    captureTimestampNS: bundle.captureTimestampNS,
                    payload: eventPayload
                )))
            }
            packetData.append(try StreamProtocolV2.encode(StreamPacketV2(
                type: .h264VideoFrame,
                flags: bundle.isKeyFrame ? [.keyFrame] : [],
                sessionID: sessionID,
                sequence: bundle.sequence,
                captureTimestampNS: bundle.captureTimestampNS,
                payload: bundle.accessUnit.data
            )))
            sendInProgress = true
            connection.send(content: packetData, completion: .contentProcessed { [weak self] error in
                guard let self else { return }
                self.sendInProgress = false
                if let error {
                    self.failTransport("archive frame send failed seq=\(bundle.sequence): \(error.localizedDescription)")
                    return
                }
                self.sentThroughSequence = bundle.sequence
                self.contentProcessedPackets &+= 1
                self.contentProcessedBytes &+= UInt64(packetData.count)
                self.publishSnapshot(spoolSnapshot: self.spool.snapshot())
                self.drainFramesOnQueue()
            })
        } catch {
            failTransport("archive frame encode failed seq=\(bundle.sequence): \(error)")
        }
    }

    private func receiveNextOnQueue() {
        guard let connection, connectionReady else { return }
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            if let data, !data.isEmpty {
                do {
                    for packet in try self.receiveParser.append(data) {
                        self.handleInboundPacketOnQueue(packet)
                    }
                } catch {
                    self.failTransport("archive inbound parse failed: \(error)")
                    connection.cancel()
                    return
                }
            }
            if let error {
                print("[UMIFT][archive-v2] receive failed \(error.localizedDescription)")
                connection.cancel()
                return
            }
            if isComplete {
                connection.cancel()
                return
            }
            self.receiveNextOnQueue()
        }
    }

    private func handleInboundPacketOnQueue(_ packet: StreamPacketV2) {
        let phoneReceiveUptimeNS = Self.phoneUptimeNS()
        guard packet.sessionID == sessionID else {
            failTransport("archive control session mismatch got=\(packet.sessionID) expected=\(sessionID)")
            return
        }
        if packet.type == .clockSyncRequest {
            handleClockSyncRequestOnQueue(packet, phoneReceiveUptimeNS: phoneReceiveUptimeNS)
            return
        }
        guard packet.type == .cumulativeAck else {
            failTransport("unexpected inbound packet type=\(packet.type.rawValue)")
            return
        }
        if let sentThroughSequence, packet.sequence > sentThroughSequence {
            failTransport("archive ACK beyond sent sequence ack=\(packet.sequence) sent=\(sentThroughSequence)")
            return
        }
        if let ackedThroughSequence, packet.sequence <= ackedThroughSequence {
            return
        }
        ackedThroughSequence = packet.sequence
        let snapshot = spool.acknowledge(through: packet.sequence)
        publishSnapshot(spoolSnapshot: snapshot)
        drainFramesOnQueue()
    }

    private func handleClockSyncRequestOnQueue(
        _ packet: StreamPacketV2,
        phoneReceiveUptimeNS: Int64
    ) {
        guard let connection, connectionReady else { return }
        do {
            let request = try StreamProtocolV2.parseClockSyncRequestPayload(packet.payload)
            guard request.requestID == packet.sequence else {
                throw StreamProtocolErrorV2.malformedPayload(
                    "clock sync request ID mismatch header=\(packet.sequence) payload=\(request.requestID)"
                )
            }
            let phoneSendUptimeNS = Self.phoneUptimeNS()
            let response = try StreamProtocolV2.encode(StreamPacketV2(
                type: .clockSyncResponse,
                sessionID: sessionID,
                sequence: request.requestID,
                captureTimestampNS: phoneSendUptimeNS,
                payload: StreamProtocolV2.makeClockSyncResponsePayload(
                    requestID: request.requestID,
                    macSendMonotonicNS: request.macSendMonotonicNS,
                    phoneReceiveUptimeNS: phoneReceiveUptimeNS,
                    phoneSendUptimeNS: phoneSendUptimeNS
                )
            ))
            connection.send(content: response, completion: .contentProcessed { [weak self] error in
                guard let self else { return }
                if let error {
                    self.failTransport("clock sync response failed: \(error.localizedDescription)")
                    return
                }
                self.contentProcessedPackets &+= 1
                self.contentProcessedBytes &+= UInt64(response.count)
            })
        } catch {
            failTransport("clock sync request failed: \(error)")
        }
    }

    private func beginRecoveryGenerationOnQueue() {
        sessionID = Self.makeSessionID()
        overflowed = false
        ackedThroughSequence = nil
        sentThroughSequence = nil
        hasEstablishedFirstConnection = false
        preambleSent = false
        sendInProgress = false
        let snapshot = spool.prepareForFirstConnection()
        requiresRecoveryKeyFrame = snapshot.firstSequence == nil
        print(
            "[UMIFT][archive-v2] recovery generation session=\(sessionID) "
            + "first=\(snapshot.firstSequence.map(String.init) ?? "--")"
        )
        publishSnapshot(spoolSnapshot: snapshot)
    }

    private static func makeSessionID() -> String {
        let timestamp = DateFormatter.streamSession.string(from: Date())
        return "stream_v2_\(timestamp)_\(UUID().uuidString.prefix(8))"
    }

    private static func phoneUptimeNS() -> Int64 {
        Int64(ProcessInfo.processInfo.systemUptime * 1_000_000_000.0)
    }

    private func publishStatus() {
        notifyStatus(connectionReady ? .connected : .listening(currentPort))
    }

    private func publishSnapshot(spoolSnapshot: ReliableEncodedVideoSpoolSnapshot) {
        let snapshot = ArchiveTransportSnapshot(
            sentThroughSequence: sentThroughSequence,
            ackedThroughSequence: ackedThroughSequence,
            contentProcessedPackets: contentProcessedPackets,
            contentProcessedBytes: contentProcessedBytes,
            spool: spoolSnapshot
        )
        DispatchQueue.main.async { [onSnapshot] in
            onSnapshot?(snapshot)
        }
        if let sequence = spoolSnapshot.lastSequence, sequence % 30 == 0 {
            print("[UMIFT][archive-v2-perf] \(snapshot.displayText)")
        }
    }

    private func failTransport(_ reason: String) {
        print("[UMIFT][archive-v2] \(reason)")
        notifyStatus(.failed(reason))
    }

    private func failIntegrity(_ reason: String) {
        print("[UMIFT][capture-integrity] \(reason)")
        DispatchQueue.main.async { [onIntegrityFailure] in
            onIntegrityFailure?(reason)
        }
    }

    private func notifyStatus(_ status: RecordingTransportStatus) {
        DispatchQueue.main.async { [onStatusChange] in
            onStatusChange?(status)
        }
    }
}

private extension DateFormatter {
    static let streamSession: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        return formatter
    }()
}

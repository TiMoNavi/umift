import SwiftUI
import Darwin

struct ContentView: View {
    @EnvironmentObject private var capture: CaptureManager
    @StateObject private var coinft = CoinFTReceiver()
    @State private var sessionName = "test"
    @State private var side = CaptureSide.right
    @State private var recordingType = RecordingType.demonstration
    @State private var shareURL: URL?
    @State private var didRunLaunchAutomation = false
    @State private var autoStopTask: Task<Void, Never>?
    @State private var isDetailsExpanded = false
    private let automation = LaunchAutomationConfig.current()

    var body: some View {
        ZStack {
            ARPreviewView(session: capture.session)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                HStack(alignment: .top, spacing: 12) {
                    compactStatusHUD
                    Spacer(minLength: 12)
                    trailingMiniHUD
                }
                .padding(.horizontal, 12)
                .padding(.top, 8)

                Spacer(minLength: 0)
                HStack(alignment: .bottom, spacing: 12) {
                    detailsDock
                    Spacer(minLength: 12)
                    controlDock
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 8)
            }
        }
        .sheet(item: $shareURL) { url in
            ShareSheet(items: [url])
        }
        .onAppear {
            applyAutomationDefaults()
            capture.startARSession()
            coinft.start()
            runLaunchAutomationIfNeeded()
        }
        .onDisappear {
            autoStopTask?.cancel()
            coinft.stop()
            capture.stopARSession()
        }
    }

    private var compactStatusHUD: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(capture.isRecording ? "REC" : (capture.isOriginCalibrated ? "READY" : "CAL"))
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .overlay(
                        Capsule()
                            .stroke(hudAccentColor, lineWidth: 1.2)
                    )
                Text(capture.statusText)
                    .font(.caption.monospacedDigit())
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }

            VStack(alignment: .leading, spacing: 3) {
                Text("6DoF")
                    .font(.caption.weight(.semibold))
                Text(capture.poseTranslationText)
                    .font(.caption.monospacedDigit())
                Text(capture.poseRotationText)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.82))
            }

            Text(capture.isOriginCalibrated ? capture.originStatusText : capture.calibrationMetricsText)
                .font(.caption2.monospacedDigit())
                .foregroundStyle(hudAccentColor)
            Text(capture.gripperCalibrationText)
                .font(.caption2.monospacedDigit())
                .foregroundStyle(capture.isGripperPathCalibrated ? .green : .orange)
        }
        .foregroundStyle(.white)
        .shadow(color: .black.opacity(0.55), radius: 2, x: 0, y: 1)
        .padding(10)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(.white.opacity(0.4), lineWidth: 1)
        )
        .frame(maxWidth: 330, alignment: .leading)
    }

    private var trailingMiniHUD: some View {
        VStack(alignment: .trailing, spacing: 8) {
            metricPill(title: "F", value: "\(capture.frameCount)")
            metricPill(title: "D", value: "\(capture.depthFrameCount)")
            metricPill(title: "T", value: compactTorchStatus)
            Button {
                withAnimation(.easeInOut(duration: 0.2)) {
                    isDetailsExpanded.toggle()
                }
            } label: {
                Image(systemName: isDetailsExpanded ? "slider.horizontal.3.circle.fill" : "slider.horizontal.3")
                    .font(.title3)
                    .frame(width: 42, height: 42)
            }
            .buttonStyle(OutlineIconButtonStyle())
        }
    }

    private var detailsDock: some View {
        VStack(alignment: .leading, spacing: 8) {
            if isDetailsExpanded {
                VStack(alignment: .leading, spacing: 10) {
                    detailSectionTitle("Capture")
                    outlineField(title: "session", text: $sessionName)
                        .accessibilityIdentifier("session_name_field")
                    HStack(spacing: 8) {
                        selectionMenu(
                            title: "side",
                            value: side.rawValue,
                            accessibilityID: "side_picker"
                        ) {
                            ForEach(CaptureSide.allCases) { item in
                                Button(item.rawValue) { side = item }
                            }
                        }
                        selectionMenu(
                            title: "type",
                            value: shortRecordingType(recordingType),
                            accessibilityID: "recording_type_picker"
                        ) {
                            ForEach(RecordingType.allCases) { item in
                                Button(shortRecordingType(item)) { recordingType = item }
                            }
                        }
                    }

                    detailSectionTitle("CoinFT")
                    HStack(spacing: 8) {
                        miniMetric(title: "src", value: coinft.sourceID)
                        miniMetric(title: "rate", value: coinft.packetRateText)
                        miniMetric(title: "age", value: coinft.lastPacketAgeText)
                    }
                    Text("\(coinft.latestLeftText)   \(coinft.latestRightText)")
                        .font(.caption2.monospacedDigit())
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        selectionMenu(
                            title: "bind",
                            value: coinft.binding.rawValue,
                            accessibilityID: "coinft_binding_menu"
                        ) {
                            ForEach(CoinFTBinding.allCases) { item in
                                Button(item.rawValue) { coinft.binding = item }
                            }
                        }
                        miniMetric(title: "drop", value: "\(coinft.droppedPacketCount)")
                    }
                    Text("\(coinft.binding.leftLabel), \(coinft.binding.rightLabel)")
                        .font(.caption2.monospaced())
                        .foregroundStyle(.white.opacity(0.82))
                        .fixedSize(horizontal: false, vertical: true)

                    detailSectionTitle("AR")
                    Text(capture.originStatusText)
                        .font(.caption2.monospaced())
                        .foregroundStyle(hudAccentColor)
                    Text(capture.calibrationGuideText)
                        .font(.caption2)
                        .foregroundStyle(.white.opacity(0.82))
                    Text(capture.videoSourceText)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.white.opacity(0.82))
                    Text(capture.torchText)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.white.opacity(0.82))
                    Text(capture.gripperCalibrationText)
                        .font(.caption2.monospaced())
                        .foregroundStyle(capture.isGripperPathCalibrated ? .green : .orange)
                }
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.55), radius: 2, x: 0, y: 1)
                .padding(10)
                .frame(width: 320, alignment: .leading)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(.white.opacity(0.38), lineWidth: 1)
                )
            }
        }
    }

    private var controlDock: some View {
        VStack(alignment: .trailing, spacing: 10) {
            Button {
                capture.calibrateOrigin()
            } label: {
                VStack(spacing: 4) {
                    Image(systemName: "scope")
                        .font(.title2)
                    Text("Cal")
                        .font(.caption2.weight(.semibold))
                }
                .frame(width: 68, height: 68)
            }
            .buttonStyle(OutlineIconButtonStyle(tint: hudAccentColor))
            .accessibilityIdentifier("calibrate_origin_button")

            Button {
                if capture.isRecording {
                    Task { await stopRecording() }
                } else {
                    startRecording()
                }
            } label: {
                VStack(spacing: 6) {
                    Image(systemName: capture.isRecording ? "stop.fill" : "record.circle")
                        .font(.system(size: 34, weight: .semibold))
                    Text(capture.isRecording ? "STOP" : "START")
                        .font(.caption.weight(.semibold))
                }
                .frame(width: 96, height: 96)
            }
            .buttonStyle(LargeRecordButtonStyle(isRecording: capture.isRecording))
            .accessibilityIdentifier("record_button")
            .disabled(!capture.canStartRecording() && !capture.isRecording)

            Button {
                shareURL = capture.lastDemoDirectory
            } label: {
                VStack(spacing: 4) {
                    Image(systemName: "square.and.arrow.up")
                        .font(.title2)
                    Text("Export")
                        .font(.caption2.weight(.semibold))
                }
                .frame(width: 68, height: 68)
            }
            .buttonStyle(OutlineIconButtonStyle())
            .disabled(capture.lastDemoDirectory == nil || capture.isRecording)
            .accessibilityIdentifier("export_button")
        }
    }

    private var hudAccentColor: Color {
        capture.isRecording ? .red : (capture.isOriginCalibrated ? .green : .orange)
    }

    private var compactTorchStatus: String {
        capture.torchText
            .replacingOccurrences(of: "torch ", with: "")
            .replacingOccurrences(of: "unsupported", with: "no")
    }

    private func metricPill(title: String, value: String) -> some View {
        HStack(spacing: 6) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.white.opacity(0.82))
            Text(value)
                .font(.caption.monospacedDigit())
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .shadow(color: .black.opacity(0.55), radius: 2, x: 0, y: 1)
        .overlay(
            Capsule()
                .stroke(.white.opacity(0.35), lineWidth: 1)
        )
    }

    private func miniMetric(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.white.opacity(0.7))
            Text(value)
                .font(.caption2.monospacedDigit())
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(.white.opacity(0.28), lineWidth: 1)
        )
    }

    private func detailSectionTitle(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.white.opacity(0.74))
    }

    private func outlineField(title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.white.opacity(0.7))
            TextField(title, text: text)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(.caption.monospaced())
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 7)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(.white.opacity(0.32), lineWidth: 1)
                )
        }
    }

    private func selectionMenu<Content: View>(
        title: String,
        value: String,
        accessibilityID: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        Menu {
            content()
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text(title.uppercased())
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.white.opacity(0.7))
                HStack(spacing: 6) {
                    Text(value)
                        .font(.caption.monospaced())
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.caption2)
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 7)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(.white.opacity(0.32), lineWidth: 1)
                )
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityIdentifier(accessibilityID)
    }

    private func shortRecordingType(_ type: RecordingType) -> String {
        switch type {
        case .demonstration:
            return "demo"
        case .grippercalibration:
            return "grip-cal"
        case .qrcalibration:
            return "qr-cal"
        }
    }

    private func startRecording() {
        guard capture.isOriginCalibrated else {
            capture.statusText = "start blocked: finish calibration first"
            return
        }
        guard capture.isGripperPathCalibrated else {
            capture.statusText = "start blocked: calibrate gripper path first"
            return
        }
        do {
            try capture.startRecording(
                sessionName: sessionName,
                side: side,
                recordingType: recordingType
            )
            scheduleAutoStopIfNeeded()
        } catch {
            capture.statusText = "start failed: \(error.localizedDescription)"
        }
    }

    private func stopRecording() async {
        autoStopTask?.cancel()
        autoStopTask = nil
        do {
            try await capture.stopRecording()
            if automation.exitAfterSave {
                capture.statusText = "saved; exiting"
                #if DEBUG
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.75) {
                    exit(0)
                }
                #endif
            }
        } catch {
            capture.statusText = "stop failed: \(error.localizedDescription)"
        }
    }

    private func applyAutomationDefaults() {
        if let value = automation.sessionName {
            sessionName = value
        }
        if let value = automation.side {
            side = value
        }
        if let value = automation.recordingType {
            recordingType = value
        }
        if automation.hasAutomationInputs {
            print("UMIFT launch automation config: \(automation.summary)")
        }
    }

    private func runLaunchAutomationIfNeeded() {
        guard (automation.autoCalibrate || automation.autoStart), !didRunLaunchAutomation else { return }
        didRunLaunchAutomation = true
        Task {
            try? await Task.sleep(for: .seconds(1.5))
            if automation.autoCalibrate {
                capture.calibrateOrigin()
            }
            try? await Task.sleep(for: .seconds(0.3))
            guard automation.autoStart else { return }
            guard capture.isOriginCalibrated else {
                capture.statusText = "auto-start blocked: finish calibration first"
                return
            }
            guard !capture.isRecording else { return }
            startRecording()
        }
    }

    private func scheduleAutoStopIfNeeded() {
        autoStopTask?.cancel()
        guard let autoStopAfter = automation.autoStopAfterSeconds, autoStopAfter > 0 else {
            autoStopTask = nil
            return
        }
        autoStopTask = Task {
            try? await Task.sleep(for: .seconds(autoStopAfter))
            guard !Task.isCancelled, capture.isRecording else { return }
            await stopRecording()
        }
    }
}

extension URL: @retroactive Identifiable {
    public var id: String { absoluteString }
}

private struct OutlineIconButtonStyle: ButtonStyle {
    var tint: Color = .white

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(tint)
            .shadow(color: .black.opacity(0.55), radius: 2, x: 0, y: 1)
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(tint.opacity(configuration.isPressed ? 0.9 : 0.45), lineWidth: 1.2)
            )
            .opacity(configuration.isPressed ? 0.82 : 1.0)
            .scaleEffect(configuration.isPressed ? 0.97 : 1.0)
    }
}

private struct LargeRecordButtonStyle: ButtonStyle {
    let isRecording: Bool

    func makeBody(configuration: Configuration) -> some View {
        let tint = isRecording ? Color.red : Color.green
        return configuration.label
            .foregroundStyle(tint)
            .shadow(color: .black.opacity(0.6), radius: 2, x: 0, y: 1)
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .stroke(tint.opacity(configuration.isPressed ? 0.95 : 0.55), lineWidth: 1.6)
            )
            .scaleEffect(configuration.isPressed ? 0.97 : 1.0)
            .opacity(configuration.isPressed ? 0.85 : 1.0)
    }
}

private struct LaunchAutomationConfig {
    let sessionName: String?
    let side: CaptureSide?
    let recordingType: RecordingType?
    let autoCalibrate: Bool
    let autoStart: Bool
    let autoStopAfterSeconds: Double?
    let exitAfterSave: Bool

    var hasAutomationInputs: Bool {
        sessionName != nil || side != nil || recordingType != nil || autoCalibrate || autoStart || autoStopAfterSeconds != nil || exitAfterSave
    }

    var summary: String {
        [
            "sessionName=\(sessionName ?? "nil")",
            "side=\(side?.rawValue ?? "nil")",
            "type=\(recordingType?.rawValue ?? "nil")",
            "autoCalibrate=\(autoCalibrate)",
            "autoStart=\(autoStart)",
            "autoStopAfter=\(autoStopAfterSeconds.map { String($0) } ?? "nil")",
            "exitAfterSave=\(exitAfterSave)",
        ].joined(separator: ", ")
    }

    static func current(processInfo: ProcessInfo = .processInfo) -> LaunchAutomationConfig {
        let environment = processInfo.environment
        let arguments = Array(processInfo.arguments.dropFirst())

        var sessionName = environment["UMIFT_SESSION_NAME"]
        var side = CaptureSide(rawValue: (environment["UMIFT_SIDE"] ?? "").lowercased())
        var recordingType = RecordingType(rawValue: (environment["UMIFT_RECORDING_TYPE"] ?? "").lowercased())
        var autoCalibrate = parseBool(environment["UMIFT_AUTO_CALIBRATE"]) ?? false
        var autoStart = parseBool(environment["UMIFT_AUTO_START"]) ?? false
        var autoStopAfterSeconds = parseDouble(environment["UMIFT_AUTO_STOP_AFTER"])
        var exitAfterSave = parseBool(environment["UMIFT_EXIT_AFTER_SAVE"]) ?? false

        var index = 0
        while index < arguments.count {
            let arg = arguments[index]
            switch arg {
            case "--session-name":
                if index + 1 < arguments.count {
                    sessionName = arguments[index + 1]
                    index += 1
                }
            case "--side":
                if index + 1 < arguments.count {
                    side = CaptureSide(rawValue: arguments[index + 1].lowercased())
                    index += 1
                }
            case "--type":
                if index + 1 < arguments.count {
                    recordingType = RecordingType(rawValue: arguments[index + 1].lowercased())
                    index += 1
                }
            case "--auto-calibrate":
                autoCalibrate = true
            case "--auto-start":
                autoStart = true
            case "--auto-stop-after":
                if index + 1 < arguments.count {
                    autoStopAfterSeconds = Double(arguments[index + 1])
                    index += 1
                }
            case "--exit-after-save":
                exitAfterSave = true
            default:
                break
            }
            index += 1
        }

        return LaunchAutomationConfig(
            sessionName: sessionName?.trimmingCharacters(in: .whitespacesAndNewlines).nonEmpty,
            side: side,
            recordingType: recordingType,
            autoCalibrate: autoCalibrate,
            autoStart: autoStart,
            autoStopAfterSeconds: autoStopAfterSeconds,
            exitAfterSave: exitAfterSave
        )
    }

    private static func parseBool(_ value: String?) -> Bool? {
        guard let normalized = value?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(), !normalized.isEmpty else {
            return nil
        }
        switch normalized {
        case "1", "true", "yes", "y", "on":
            return true
        case "0", "false", "no", "n", "off":
            return false
        default:
            return nil
        }
    }

    private static func parseDouble(_ value: String?) -> Double? {
        guard let value else { return nil }
        return Double(value.trimmingCharacters(in: .whitespacesAndNewlines))
    }
}

private extension String {
    var nonEmpty: String? {
        isEmpty ? nil : self
    }
}

#Preview {
    ContentView()
        .environmentObject(CaptureManager())
}

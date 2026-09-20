import SwiftUI

private enum DebugAutomation {
    static let autoStartCalibrationOnLaunch = false
    static let skipGripperCalibrationOnLaunch = false
    static let autoMarkWorldOrigin = true
    static let keepARKitRunningAfterCalibration = true
    static let arFrameWatchdogEnabled = true
    static let arFrameWatchdogTimeout: TimeInterval = 3.0
    static let autoMarkRetryInterval: TimeInterval = 1.0
    static let launchCalibrationDelay: TimeInterval = 0.65
    static let skipUltraRestartAfterWorldCalibration = true

    static var statusText: String {
        let launchText: String
        if skipGripperCalibrationOnLaunch {
            launchText = "skip grip"
        } else {
            launchText = autoStartCalibrationOnLaunch ? "auto cal" : "manual"
        }
        let markText = autoMarkWorldOrigin ? "auto mark" : "manual mark"
        let arText = keepARKitRunningAfterCalibration ? "ar continuous" : "ar release"
        let watchdogText = arFrameWatchdogEnabled ? "watchdog" : "no watchdog"
        return "\(launchText), \(markText), \(arText), \(watchdogText)"
    }
}

struct CaptureDashboardView: View {
    @EnvironmentObject private var appState: CaptureAppState
    @EnvironmentObject private var camera: DualCameraCaptureManager
    @EnvironmentObject private var arCapture: ARCaptureModel
    @EnvironmentObject private var motion: DeviceMotionPoseProvider
    @State private var autoFinishingGripperPath = false
    @State private var worldARActive = false
    @State private var cameraSuspendedForWorldAR = false
    @State private var worldARActivationID = 0
    @State private var didRunLaunchAutomation = false
    @State private var lastARFrameUpdate = Date.distantPast
    @State private var arWatchdogTriggered = false
    @State private var didAutoMarkWorldOrigin = false
    @State private var lastAutoWorldOriginMarkAttempt = Date.distantPast

    var body: some View {
        GeometryReader { geometry in
            HStack(spacing: 0) {
                PreviewPane(
                    worldARActive: worldARActive,
                    continuousARDisabled: arWatchdogTriggered,
                    onARFrameUpdate: {
                        lastARFrameUpdate = Date()
                        arWatchdogTriggered = false
                    }
                )
                    .frame(width: geometry.size.width * 0.5, height: geometry.size.height)
                    .background(AppPalette.previewBackground)

                ParameterPanel()
                    .frame(width: geometry.size.width * 0.5, height: geometry.size.height)
                    .background(AppPalette.panelBackground)
            }
            .background(AppPalette.appBackground)
            .ignoresSafeArea()
        }
        .onAppear {
            worldARActive = true
            arCapture.setTorchEnabled(true)
            motion.start()
            runLaunchAutomationIfNeeded()
        }
        .onDisappear {
            worldARActive = false
            arCapture.setTorchEnabled(false)
            motion.stop()
        }
        .onReceive(arCapture.$gripperVisionState) { state in
            appState.apply(gripperVision: state)
            if appState.phase == .calibrationGripper,
               state.sweepReadyToFinish,
               !autoFinishingGripperPath {
                autoFinishingGripperPath = true
                arCapture.finishGripperPathCalibration()
            }
        }
        .onReceive(motion.$state) { state in
            appState.apply(motion: state)
        }
        .onReceive(Timer.publish(every: 1.0, on: .main, in: .common).autoconnect()) { now in
            runAutoWorldOriginMarkIfReady(now: now)
            guard DebugAutomation.arFrameWatchdogEnabled,
                  DebugAutomation.keepARKitRunningAfterCalibration,
                  worldARActive,
                  !arWatchdogTriggered,
                  appState.phase == .previewCalibrated || appState.phase == .startCountdown || appState.phase == .recording
            else { return }
            guard now.timeIntervalSince(lastARFrameUpdate) > DebugAutomation.arFrameWatchdogTimeout else { return }
            print(
                "[UMIFT][watchdog] ar frame stalled phase=\(appState.phase.rawValue) lastFrameAge=\(String(format: "%.2f", now.timeIntervalSince(lastARFrameUpdate)))s"
            )
            arWatchdogTriggered = true
            appState.handleContinuousARWatchdogTimeout()
        }
        .onChange(of: appState.worldCalibration.finishRequestID) { _, requestID in
            guard requestID > 0, appState.phase == .calibrationWorld else { return }
            print("[UMIFT][ui] finish request id=\(requestID)")
            worldARActivationID += 1
            let activationID = worldARActivationID
            worldARActive = true
            appState.apply(worldARStatus: WorldCalibrationARStatus(
                statusText: "ar tracking",
                trackingText: appState.worldCalibration.arTrackingText,
                centerHitText: "continuous pose"
            ))
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.50) {
                guard appState.phase == .calibrationWorld,
                      appState.worldCalibration.finishRequestID == requestID,
                      activationID == worldARActivationID
                else { return }
                appState.completeWorldCalibrationRelease()
            }
        }
        .onChange(of: appState.worldCalibration.centerHitText) { _, _ in
            runAutoWorldOriginMarkIfReady()
        }
        .onChange(of: appState.worldCalibration.centerDistanceM) { _, _ in
            runAutoWorldOriginMarkIfReady()
        }
        .onChange(of: appState.worldCalibration.axisVisible) { _, visible in
            if visible {
                didAutoMarkWorldOrigin = true
            }
        }
        .onChange(of: worldARActive) { _, _ in
            runAutoWorldOriginMarkIfReady()
        }
        .onChange(of: appState.phase) { oldPhase, newPhase in
            print(
                "[UMIFT][ui] phase \(oldPhase.rawValue)->\(newPhase.rawValue) worldARActive=\(worldARActive) cameraSuspended=\(cameraSuspendedForWorldAR) activationID=\(worldARActivationID)"
            )
            if oldPhase == .calibrationGripper, newPhase != .calibrationGripper {
                if newPhase == .calibrationWorld || newPhase == .previewCalibrated {
                    if !arCapture.gripperVisionState.isPathCalibrated {
                        arCapture.finishGripperPathCalibration()
                    }
                } else {
                    arCapture.cancelGripperPathCalibration()
                }
            }

            if newPhase == .calibrationGripper {
                worldARActivationID += 1
                worldARActive = true
                cameraSuspendedForWorldAR = false
                autoFinishingGripperPath = false
                arWatchdogTriggered = false
                didAutoMarkWorldOrigin = false
                lastAutoWorldOriginMarkAttempt = .distantPast
                appState.selectPreview(.ultra)
                arCapture.setTorchEnabled(true)
                arCapture.startGripperPathCalibration()
            }

            if newPhase == .calibrationWorld {
                appState.selectPreview(.wide)
                worldARActivationID += 1
                worldARActive = true
                cameraSuspendedForWorldAR = false
                print("[UMIFT][ui] enter world calibration, keep single ar session")
                appState.apply(worldARStatus: WorldCalibrationARStatus(
                    statusText: "ar tracking",
                    trackingText: "--",
                    centerHitText: "find table"
                ))
                didAutoMarkWorldOrigin = false
                lastAutoWorldOriginMarkAttempt = .distantPast
                runAutoWorldOriginMarkIfReady()
            }

            if oldPhase == .calibrationWorld, newPhase != .calibrationWorld {
                if DebugAutomation.keepARKitRunningAfterCalibration, appState.isWorldCalibrated {
                    print("[UMIFT][ui] leaving world calibration, keep ar continuous")
                    worldARActive = true
                    cameraSuspendedForWorldAR = false
                    arWatchdogTriggered = false
                } else {
                    worldARActivationID += 1
                    worldARActive = true
                    cameraSuspendedForWorldAR = false
                    print("[UMIFT][ui] leaving world calibration, ar remains active")
                }
            }

            if newPhase == .previewUncalibrated {
                worldARActivationID += 1
                worldARActive = true
                cameraSuspendedForWorldAR = false
                arWatchdogTriggered = false
                didAutoMarkWorldOrigin = false
                lastAutoWorldOriginMarkAttempt = .distantPast
            }
        }
    }

    private func runLaunchAutomationIfNeeded() {
        guard !didRunLaunchAutomation else { return }
        guard DebugAutomation.skipGripperCalibrationOnLaunch || DebugAutomation.autoStartCalibrationOnLaunch else { return }
        didRunLaunchAutomation = true
        print("[UMIFT][automation] scheduling launch automation \(DebugAutomation.statusText)")
        DispatchQueue.main.asyncAfter(deadline: .now() + DebugAutomation.launchCalibrationDelay) {
            guard appState.phase == .previewUncalibrated else { return }
            if DebugAutomation.skipGripperCalibrationOnLaunch {
                print("[UMIFT][automation] skip gripper and enter world calibration")
                appState.beginWorldCalibrationUsingSavedGripperDebugRoute()
            } else {
                print("[UMIFT][automation] begin full calibration")
                appState.beginCalibration()
            }
        }
    }

    private func runAutoWorldOriginMarkIfReady(now: Date = Date()) {
        guard DebugAutomation.autoMarkWorldOrigin,
              !didAutoMarkWorldOrigin,
              worldARActive,
              appState.phase == .calibrationWorld,
              !appState.worldCalibration.axisVisible,
              autoMarkHasUsableCenterHit
        else { return }
        guard now.timeIntervalSince(lastAutoWorldOriginMarkAttempt) >= DebugAutomation.autoMarkRetryInterval else { return }
        lastAutoWorldOriginMarkAttempt = now
        print(
            "[UMIFT][automation] auto mark attempt hit=\(appState.worldCalibration.centerHitText) dist=\(appState.worldCalibration.centerDistanceM.map { String(format: "%.3f", $0) } ?? "--")"
        )
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            guard appState.phase == .calibrationWorld,
                  !appState.worldCalibration.axisVisible,
                  autoMarkHasUsableCenterHit
            else { return }
            appState.debugRequestWorldOriginMark()
        }
    }

    private var autoMarkHasUsableCenterHit: Bool {
        if appState.worldCalibration.centerDistanceM != nil {
            return true
        }
        let hitText = appState.worldCalibration.centerHitText.lowercased()
        return hitText.contains("hit") && (hitText.contains("center") || hitText.contains("horizontal"))
    }
}

private struct PreviewPane: View {
    @EnvironmentObject private var appState: CaptureAppState
    @EnvironmentObject private var camera: DualCameraCaptureManager
    @EnvironmentObject private var arCapture: ARCaptureModel
    let worldARActive: Bool
    let continuousARDisabled: Bool
    let onARFrameUpdate: () -> Void

    var body: some View {
        GeometryReader { proxy in
            let previewHeight = min(proxy.size.height, proxy.size.width * 3.0 / 4.0)
            let statusHeight = max(0, proxy.size.height - previewHeight)

            ZStack {
                Color.black

                VStack(spacing: 0) {
                    ZStack(alignment: .bottomTrailing) {
                        WorldCalibrationARView(
                            isActive: shouldKeepARSessionActive,
                            markRequestID: appState.worldCalibration.markRequestID,
                            userWorldTransform: appState.worldCalibration.userWorldTransform,
                            showWorldAxis: appState.phase == .calibrationWorld && appState.worldCalibration.axisVisible,
                            onStatusUpdate: { status in
                                arCapture.applyARStatus(status)
                                appState.apply(worldARStatus: status)
                            },
                            onOriginMarked: { result in
                                appState.completeWorldOriginMark(result)
                            },
                            onFrameUpdate: { frameState in
                                onARFrameUpdate()
                                appState.apply(worldTrackingFrame: frameState)
                                arCapture.applyARWideFrame(frameState)
                            },
                            onPrivateUltraWideFrame: { frameState in
                                arCapture.consumePrivateUltraWideFrame(frameState)
                            }
                        )
                        .opacity(arPreviewOpacity)
                        .allowsHitTesting(appState.previewSource == .wide)

                        if appState.previewSource == .ultra {
                            ZStack {
                                Rectangle()
                                    .fill(Color.black)
                                if let image = arCapture.ultraWidePreviewImage {
                                    Image(uiImage: image)
                                        .resizable()
                                        .scaledToFit()
                                } else {
                                    Text("waiting for private ultrawide")
                                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                                        .foregroundStyle(.white.opacity(0.58))
                                }
                            }
                        }

                        if appState.previewSource == .ultra && showGripperOverlay {
                            GripperMarkerOverlayLayer(
                                markers: arCapture.gripperVisionState.markerOverlays,
                                frameSize: arCapture.gripperVisionState.videoFrameSize
                            )
                            .allowsHitTesting(false)
                        }

                        if shouldShowWorldCalibrationOverlay {
                            WorldCalibrationOverlay(
                                state: appState.worldCalibration,
                                showAxes: appState.worldCalibration.axisVisible
                            )
                            .allowsHitTesting(false)
                        }

                        previewChrome
                    }
                    .aspectRatio(4.0 / 3.0, contentMode: .fit)
                    .frame(width: proxy.size.width, height: previewHeight, alignment: .top)

                    bottomStatusBar
                        .frame(width: proxy.size.width, height: statusHeight)
                }
                .frame(width: proxy.size.width, height: proxy.size.height, alignment: .top)
            }
        }
        .foregroundStyle(.white)
    }

    private var previewChrome: some View {
        ZStack {
            VStack {
                HStack {
                    Button {
                        appState.selectPreview(appState.previewSource == .wide ? .ultra : .wide)
                    } label: {
                        ZStack {
                            Circle()
                                .fill(Color.black.opacity(0.44))
                            Circle()
                                .stroke(.white.opacity(0.55), lineWidth: 1)
                            VStack(spacing: 0) {
                                Image(systemName: "camera.rotate")
                                    .font(.system(size: 15, weight: .semibold))
                                Text(appState.previewSource == .wide ? "W" : "U")
                                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                            }
                            .foregroundStyle(.white)
                        }
                        .frame(width: 42, height: 42)
                    }
                    .accessibilityLabel("Switch preview camera")

                    Spacer()
                }

                Spacer()
            }
            .padding(10)

            if let value = appState.countdownValue {
                CountdownOverlay(value: value)
            }
        }
    }

    private var arPreviewOpacity: Double {
        if shouldShowARWidePreview {
            return worldARActive ? 1 : 0.38
        }
        return 0.02
    }

    private var shouldKeepARSessionActive: Bool {
        return worldARActive
    }

    private var shouldShowARWidePreview: Bool {
        shouldKeepARSessionActive && appState.previewSource == .wide
    }

    private var bottomStatusBar: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                metricLine("pose", appState.wide.poseWorld)
                metricLine("grip", gripperSummary)
                metricLine("center", centerDistanceText)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.leading, 12)

            HStack(spacing: 8) {
                statusFlowButton
                    .frame(minWidth: 86)

                torchButton

                recordButton
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
            .padding(.trailing, 12)
        }
        .padding(.vertical, 6)
        .background(AppPalette.previewBackground)
        .overlay(alignment: .top) {
            Rectangle()
                .fill(.white.opacity(0.14))
                .frame(height: 1)
        }
    }

    private func metricLine(_ label: String, _ value: String) -> some View {
        HStack(spacing: 6) {
            Text(label)
                .foregroundStyle(.white.opacity(0.50))
                .frame(width: 42, alignment: .leading)
            Text(value)
                .foregroundStyle(.white.opacity(0.92))
        }
        .font(.system(size: 10, weight: .semibold, design: .monospaced))
        .lineLimit(1)
        .minimumScaleFactor(0.62)
    }

    private var statusFlowButton: some View {
        Button {
            handleFlowStatusTap()
        } label: {
            HStack(spacing: 6) {
                Image(systemName: flowStatusIcon)
                    .font(.system(size: 13, weight: .semibold))
                Text(flowStatusText)
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.66)
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 9)
            .frame(height: 34)
            .frame(maxWidth: .infinity)
            .background(flowStatusTint.opacity(flowControlDisabled ? 0.12 : 0.28))
            .overlay(
                RoundedRectangle(cornerRadius: 7)
                    .stroke(flowStatusTint.opacity(flowControlDisabled ? 0.32 : 0.86), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .disabled(flowControlDisabled)
        .opacity(flowControlDisabled ? 0.62 : 1)
    }

    private var torchButton: some View {
        Button {
            arCapture.setTorchEnabled(!arCapture.torchRequested)
        } label: {
            Image(systemName: arCapture.torchRequested ? "flashlight.on.fill" : "flashlight.off.fill")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 38, height: 34)
                .background(torchTint.opacity(0.24))
                .overlay(
                    RoundedRectangle(cornerRadius: 7)
                        .stroke(torchTint.opacity(0.84), lineWidth: 1)
                )
                .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Torch")
    }

    private var recordButton: some View {
        Button {
            appState.handlePrimaryAction()
        } label: {
            Image(systemName: primaryActionIcon)
                .font(.system(size: 18, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 42, height: 34)
                .background(primaryActionTint.opacity(recordDisabled ? 0.12 : 0.26))
                .overlay(
                    RoundedRectangle(cornerRadius: 7)
                        .stroke(primaryActionTint.opacity(recordDisabled ? 0.32 : 0.9), lineWidth: 1)
                )
                .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .disabled(recordDisabled)
        .opacity(recordDisabled ? 0.58 : 1)
        .accessibilityLabel(appState.primaryActionTitle)
    }

    private var gripperSummary: String {
        let openText = arCapture.gripperVisionState.openingText
        let confidenceText = arCapture.gripperVisionState.confidenceText
        return "\(openText) conf \(confidenceText)"
    }

    private var centerDistanceText: String {
        let distance = appState.phase == .calibrationWorld
            ? appState.worldCalibration.centerDistanceM
            : appState.depth.centerDepthM
        return distance.map { String(format: "%.3f m", $0) } ?? "-- m"
    }

    private var torchTint: Color {
        arCapture.torchRequested ? AppPalette.yellow : .white.opacity(0.45)
    }

    private var flowControlDisabled: Bool {
        hasHardwareError
            || appState.phase == .previewCalibrated
            || appState.phase == .startCountdown
            || appState.phase == .recording
            || appState.worldCalibration.releaseInProgress
            || appState.worldCalibration.axisVisible
    }

    private func compactStatus(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold, design: .monospaced))
            .foregroundStyle(.white.opacity(0.90))
            .lineLimit(1)
            .minimumScaleFactor(0.68)
            .padding(.horizontal, 7)
            .frame(height: 24)
            .background(color.opacity(0.18))
            .overlay(
                Capsule()
                    .stroke(color.opacity(0.62), lineWidth: 1)
            )
            .clipShape(Capsule())
    }

    private var showGripperOverlay: Bool {
        arCapture.gripperVisionState.videoFrameSize.width > 0
            && arCapture.gripperVisionState.videoFrameSize.height > 0
    }

    private var shouldShowWorldCalibrationOverlay: Bool {
        appState.previewSource == .wide
            && (appState.phase == .calibrationWorld || appState.worldCalibration.axisVisible)
    }

    private var syncText: String {
        guard let syncErrorMS = arCapture.syncErrorMS else { return "sync --" }
        return String(format: "sync %.0fms", syncErrorMS)
    }

    private func markerOverlay(in size: CGSize) -> some View {
        ZStack {
            MarkerBox(color: AppPalette.yellow)
                .frame(width: size.width * 0.18, height: size.width * 0.15)
                .rotationEffect(.degrees(-5))
                .position(x: size.width * 0.34, y: size.height * 0.76)

            MarkerBox(color: AppPalette.magenta)
                .frame(width: size.width * 0.20, height: size.width * 0.16)
                .rotationEffect(.degrees(9))
                .position(x: size.width * 0.68, y: size.height * 0.78)

            Path { path in
                path.move(to: CGPoint(x: size.width * 0.24, y: size.height * 0.83))
                path.addQuadCurve(
                    to: CGPoint(x: size.width * 0.78, y: size.height * 0.82),
                    control: CGPoint(x: size.width * 0.50, y: size.height * 0.72)
                )
            }
            .stroke(AppPalette.green.opacity(0.55), style: StrokeStyle(lineWidth: 2, dash: [7, 6]))
        }
    }

    private var hasHardwareError: Bool {
        let statuses = [arCapture.wideStatus, arCapture.ultraStatus, arCapture.torchStatus, arCapture.capabilityText]
            .joined(separator: " ")
            .lowercased()
        return statuses.contains("denied")
            || statuses.contains("unsupported")
            || statuses.contains("missing")
            || statuses.contains("failed")
    }

    private var flowStatusText: String {
        if hasHardwareError {
            return "硬件错误"
        }

        switch appState.phase {
        case .previewUncalibrated:
            return "开始校准"
        case .calibrationWorld:
            if appState.worldCalibration.releaseInProgress {
                return "释放相机"
            }
            return appState.worldCalibration.axisVisible ? "观察坐标系" : "标记原点"
        case .calibrationGripper:
            if arCapture.gripperVisionState.sweepReadyToFinish {
                return "保存夹爪"
            }
            return String(format: "开合 %.1f/3", min(3.0, arCapture.gripperVisionState.sweepCycleEstimate))
        case .previewCalibrated:
            return "准备就绪"
        case .startCountdown:
            return appState.countdownValue.map { "倒计时 \($0)" } ?? "倒计时"
        case .recording:
            return "正在录制"
        }
    }

    private var flowStatusIcon: String {
        if hasHardwareError {
            return "exclamationmark.triangle.fill"
        }

        switch appState.phase {
        case .previewUncalibrated, .calibrationWorld, .calibrationGripper:
            return "scope"
        case .previewCalibrated:
            return "checkmark.circle.fill"
        case .startCountdown:
            return "timer"
        case .recording:
            return "record.circle.fill"
        }
    }

    private var flowStatusTint: Color {
        if hasHardwareError {
            return AppPalette.red
        }

        switch appState.phase {
        case .previewCalibrated:
            return AppPalette.green
        case .recording:
            return AppPalette.red
        case .startCountdown, .calibrationWorld, .calibrationGripper:
            return AppPalette.orange
        case .previewUncalibrated:
            return AppPalette.blue
        }
    }

    private var primaryActionIcon: String {
        appState.isRecording || appState.phase == .startCountdown ? "stop.fill" : "record.circle"
    }

    private var primaryActionTint: Color {
        appState.isRecording || appState.phase == .startCountdown ? AppPalette.red : AppPalette.orange
    }

    private var recordDisabled: Bool {
        !appState.canRecord && !appState.isRecording && appState.phase != .startCountdown
    }

    private func handleFlowStatusTap() {
        switch appState.phase {
        case .previewUncalibrated:
            appState.beginCalibration()
        case .calibrationGripper:
            arCapture.finishGripperPathCalibration()
        case .calibrationWorld:
            appState.advanceCalibration()
        case .previewCalibrated:
            appState.beginRecordCountdown()
        case .startCountdown, .recording:
            appState.terminateRecording()
        }
    }
}

private struct WorldCalibrationOverlay: View {
    let state: WorldCalibrationState
    let showAxes: Bool

    var body: some View {
        GeometryReader { proxy in
            let size = proxy.size
            let center = CGPoint(x: size.width * 0.5, y: size.height * 0.5)

            ZStack(alignment: .topLeading) {
                guideLines(center: center, size: size)
                    .stroke(
                        AppPalette.green.opacity(0.72),
                        style: StrokeStyle(lineWidth: 1.5, lineCap: .round, dash: [7, 6])
                    )

                centerTarget(center: center)

                if showAxes {
                    angleBadge
                        .position(x: center.x, y: min(size.height - 22, center.y + 72))
                }
            }
        }
    }

    private func guideLines(center: CGPoint, size: CGSize) -> Path {
        var path = Path()
        let startY = center.y + 18
        let endY = min(size.height - 18, center.y + size.height * 0.34)
        path.move(to: CGPoint(x: center.x - 22, y: startY))
        path.addLine(to: CGPoint(x: center.x - 58, y: endY))
        path.move(to: CGPoint(x: center.x + 22, y: startY))
        path.addLine(to: CGPoint(x: center.x + 58, y: endY))
        return path
    }

    private func centerTarget(center: CGPoint) -> some View {
        ZStack {
            Circle()
                .stroke(.white.opacity(0.92), lineWidth: 1.4)
                .frame(width: 28, height: 28)
            Circle()
                .fill(AppPalette.green)
                .frame(width: 5, height: 5)
            Rectangle()
                .fill(.white.opacity(0.9))
                .frame(width: 42, height: 1)
            Rectangle()
                .fill(.white.opacity(0.9))
                .frame(width: 1, height: 42)
        }
        .position(center)
    }

    private var angleBadge: some View {
        let angleText = state.directionDeviationDeg.map { String(format: "%.1f deg", $0) } ?? "-- deg"
        return HStack(spacing: 8) {
            Image(systemName: "angle")
                .font(.system(size: 12, weight: .semibold))
            Text("方向偏差 \(angleText)")
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 10)
        .frame(height: 28)
        .background(Color.black.opacity(0.52))
        .overlay(
            Capsule()
                .stroke(AppPalette.green.opacity(0.78), lineWidth: 1)
        )
        .clipShape(Capsule())
    }
}

private struct ParameterPanel: View {
    @EnvironmentObject private var appState: CaptureAppState
    @EnvironmentObject private var camera: DualCameraCaptureManager
    @EnvironmentObject private var arCapture: ARCaptureModel
    @State private var showAdvanced = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("UMI-FT Dual Capture")
                            .font(.system(size: 18, weight: .semibold))
                        Text(appState.phase.title)
                            .font(.system(size: 13, weight: .medium, design: .monospaced))
                            .foregroundStyle(.white.opacity(0.68))
                    }
                    Spacer()
                    clearCalibrationButton
                    PhaseBadge(phase: appState.phase)
                }

                ParameterSection(title: "Key Status") {
                    StatusRow("phase", "\(appState.phase.title) / \(appState.phase.detail)")
                    StatusRow("preview", appState.previewSource.rawValue)
                    StatusRow("pose_mode", poseModeText)
                    StatusRow("pos_xyz", appState.wide.posePosition)
                    StatusRow("rot_euler_deg", appState.wide.poseRotation)
                    StatusRow("rot_step", appState.wide.poseRotationDelta)
                    StatusRow("pose_note", poseNoteText)
                    StatusRow("pose_src", appState.wide.poseSource)
                    StatusRow("open_0_100", liveOpenPercentText)
                    StatusRow("raw_pair", arCapture.gripperVisionState.pixelDistanceText)
                    StatusRow("grip_conf", liveConfidenceText)
                    StatusRow("gripper", gripperTrackingSummary)
                    StatusRow("ids", arCapture.gripperVisionState.detectedIDsText)
                    StatusRow("left", markerOverlayLine(markerID: 0))
                    StatusRow("right", markerOverlayLine(markerID: 1))
                    StatusRow("overlays", overlaySummaryText)
                    StatusRow("ultra", arCapture.ultraFrameText)
                    StatusRow("uw_tick", ultraTickText)
                    StatusRow("depth", centerDepthText)
                    StatusRow("sync", syncSummaryText)
                }

                ParameterSection(title: "Controls") {
                    StatusRow("automation", DebugAutomation.statusText)

                    Picker("Preview", selection: Binding(
                        get: { appState.previewSource },
                        set: { appState.selectPreview($0) }
                    )) {
                        ForEach(PreviewSource.allCases) { source in
                            Text(source.rawValue).tag(source)
                        }
                    }
                    .pickerStyle(.segmented)

                    HStack(spacing: 8) {
                        Button {
                            appState.beginCalibration()
                        } label: {
                            Label("校准", systemImage: "scope")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(ControlButtonStyle(tint: AppPalette.blue))

                        Button {
                            handleNextCalibrationTap()
                        } label: {
                            Label("下一步", systemImage: "arrow.right.circle")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(ControlButtonStyle(tint: AppPalette.green))

                        Button {
                            appState.handlePrimaryAction()
                        } label: {
                            Label(appState.primaryActionTitle, systemImage: primaryActionIcon)
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(ControlButtonStyle(tint: primaryActionTint))
                        .disabled(!appState.canRecord && !appState.isRecording && appState.phase != .startCountdown)
                    }
                }

                advancedToggle

                if showAdvanced {
                    ParameterSection(title: "Device") {
                        StatusRow("wide", arCapture.wideStatus)
                        StatusRow("depth", appState.depth.available ? "running" : appState.depth.source)
                        StatusRow("ultra", arCapture.ultraStatus)
                        StatusRow("gripper", gripperLiveSummary)
                        StatusRow("torch", arCapture.torchStatus)
                        StatusRow("receiver", arCapture.receiverStatus)
                    }

                    ParameterSection(title: "Calibration") {
                        StatusRow("stage", appState.phase.detail)
                        StatusRow("ar", appState.worldCalibration.arStatusText)
                        StatusRow("tracking", appState.worldCalibration.arTrackingText)
                        StatusRow("center_hit", appState.worldCalibration.centerHitText)
                        StatusRow("origin_world", appState.worldCalibration.originWorldText)
                        StatusRow("motion", appState.motion.statusText)
                        StatusRow("gravity", appState.motion.gravitySummary)
                        StatusRow("world_trigger", appState.worldCalibration.triggerText)
                        StatusRow("direction_delta", directionDeviationText)
                        StatusRow("gripper_cycles", String(format: "%.1f/3", min(3.0, arCapture.gripperVisionState.sweepCycleEstimate)))
                        StatusRow("gripper_ready", arCapture.gripperVisionState.sweepReadyToFinish ? "true" : "false")
                    }

                    ParameterSection(title: "Pose / Wide") {
                        StatusRow("pose_mode", poseModeText)
                        StatusRow("pos_xyz", appState.wide.posePosition)
                        StatusRow("rot_euler_deg", appState.wide.poseRotation)
                        StatusRow("rot_step", appState.wide.poseRotationDelta)
                        StatusRow("pose_note", poseNoteText)
                        StatusRow("pose_world", appState.wide.poseWorld)
                        StatusRow("pose_source", appState.wide.poseSource)
                        StatusRow("velocity", appState.wide.poseVelocity)
                        StatusRow("intrinsics", appState.wide.intrinsics)
                        StatusRow("extrinsics", appState.wide.extrinsics)
                        StatusRow("rgb", arCapture.wideFrameText)
                    }

                    ParameterSection(title: "Depth") {
                        StatusRow("available", appState.depth.available ? "true" : "false")
                        StatusRow("source", appState.depth.source)
                        StatusRow("aligned_rgb", appState.depth.alignedToRGB ? "true" : "false")
                        StatusRow("center_m", appState.depth.centerDepthM.map { String(format: "%.3f", $0) } ?? "--")
                        StatusRow("valid_ratio", String(format: "%.0f%%", appState.depth.validRatio * 100))
                        StatusRow("confidence", appState.depth.confidence)
                        StatusRow("reason", appState.depth.unavailableReason)
                    }

                    ParameterSection(title: "Gripper") {
                        StatusRow("vision", arCapture.gripperVisionState.statusText)
                        StatusRow("frame", arCapture.gripperVisionState.frameText)
                        StatusRow("ids", arCapture.gripperVisionState.detectedIDsText)
                        StatusRow("opening_live", arCapture.gripperVisionState.openingText)
                        StatusRow("open_percent", liveOpenPercentText)
                        StatusRow("confidence", liveConfidenceText)
                        StatusRow("overlay_count", overlaySummaryText)
                        StatusRow("overlay_left", markerOverlayLine(markerID: 0))
                        StatusRow("overlay_right", markerOverlayLine(markerID: 1))
                        StatusRow("pixel_dist", arCapture.gripperVisionState.pixelDistanceText)
                        StatusRow("path", arCapture.gripperVisionState.pathText)
                        StatusRow("cal", arCapture.gripperVisionState.calibrationText)
                        StatusRow("cal_sweep", arCapture.gripperVisionState.sweepProgressText)
                        StatusRow("distance_m", appState.gripper.gripperDistanceM.map { String(format: "%.4f", $0) } ?? "pending")
                        MarkerStateView(title: "left_marker_state", marker: arCapture.gripperVisionState.leftMarker)
                        MarkerStateView(title: "right_marker_state", marker: arCapture.gripperVisionState.rightMarker)
                    }

                    ParameterSection(title: "Sync / Buffer") {
                        StatusRow("wide_ts", arCapture.wideTimestampNS.map(String.init) ?? "--")
                        StatusRow("ultra_ts", arCapture.ultraTimestampNS.map(String.init) ?? "--")
                        StatusRow("ultra_rgb", arCapture.ultraFrameText)
                        StatusRow("sync_error", arCapture.syncErrorMS.map { String(format: "%.1f ms", $0) } ?? "--")
                        StatusRow("sync_valid", arCapture.syncValid ? "true" : "false")
                        StatusRow("buffer", String(format: "%.1f s", arCapture.bufferDurationS))
                    }

                    ParameterSection(title: "Events") {
                        ForEach(appState.eventLog, id: \.self) { event in
                            Text(event)
                                .font(.system(size: 12, weight: .medium, design: .monospaced))
                                .foregroundStyle(.white.opacity(0.78))
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            .padding(16)
        }
        .foregroundStyle(.white)
    }

    private var primaryActionIcon: String {
        appState.isRecording || appState.phase == .startCountdown ? "stop.fill" : "record.circle"
    }

    private var primaryActionTint: Color {
        appState.isRecording || appState.phase == .startCountdown ? AppPalette.red : AppPalette.orange
    }

    private var directionDeviationText: String {
        appState.worldCalibration.directionDeviationDeg.map { String(format: "%.1f deg", $0) } ?? "-- deg"
    }

    private var centerDepthText: String {
        appState.depth.centerDepthM.map { String(format: "%.3f m", $0) } ?? "-- m"
    }

    private var syncSummaryText: String {
        let validText = arCapture.syncValid ? "ok" : "hold"
        let errorText = arCapture.syncErrorMS.map { String(format: "%.0f ms", $0) } ?? "--"
        return "\(validText) \(errorText)"
    }

    private var ultraTickText: String {
        guard let timestamp = arCapture.ultraTimestampNS else { return "--" }
        return String(timestamp % 1_000_000_000)
    }

    private var poseModeText: String {
        if appState.wide.poseSource.contains("imu-est") {
            return "imu dead-reckon"
        }
        if appState.wide.poseWorld.hasPrefix("raw ") {
            return "arkit raw"
        }
        if appState.wide.poseSource.contains("arkit") {
            return appState.worldCalibration.userWorldTransform == nil ? "arkit pre-origin" : "arkit user-world"
        }
        return "unavailable"
    }

    private var poseNoteText: String {
        "rot is ARKit Euler deg; rot_step is per-frame change"
    }

    private var gripperLiveSummary: String {
        "\(arCapture.gripperVisionState.frameText) \(arCapture.gripperVisionState.detectedIDsText) \(arCapture.gripperVisionState.openingText)"
    }

    private var gripperTrackingSummary: String {
        let state = arCapture.gripperVisionState
        return "\(state.statusText) \(state.openingText) conf \(state.confidenceText)"
    }

    private var overlaySummaryText: String {
        let overlays = arCapture.gripperVisionState.markerOverlays
        let decoded = overlays.filter { $0.kind == .detected }.count
        let path = overlays.filter { $0.kind == .pathCandidate }.count
        let held = overlays.filter { $0.kind == .held }.count
        let rejected = overlays.filter { $0.kind == .rejected }.count
        return "det \(decoded) path \(path) hold \(held) cand \(rejected)"
    }

    private func markerOverlayLine(markerID: Int) -> String {
        let overlays = arCapture.gripperVisionState.markerOverlays
        let preferredKinds: [MarkerOverlayKind] = [.detected, .pathCandidate, .held]
        for kind in preferredKinds {
            if let marker = overlays.first(where: { $0.markerID == markerID && $0.kind == kind }) {
                return String(
                    format: "%@ %.0f,%.0f a%.0f",
                    overlayKindText(kind),
                    marker.center.x,
                    marker.center.y,
                    marker.area
                )
            }
        }
        return "--"
    }

    private func overlayKindText(_ kind: MarkerOverlayKind) -> String {
        switch kind {
        case .detected:
            return "det"
        case .pathCandidate:
            return "path"
        case .held:
            return "hold"
        case .rejected:
            return "cand"
        }
    }

    private var liveOpenPercentText: String {
        if let openPercent = arCapture.gripperVisionState.openPercent {
            return String(format: "%.1f", openPercent)
        }
        return "uncalibrated"
    }

    private var liveConfidenceText: String {
        String(
            format: "%.2f %@",
            arCapture.gripperVisionState.confidence,
            arCapture.gripperVisionState.confidenceText
        )
    }

    private var advancedToggle: some View {
        Button {
            showAdvanced.toggle()
        } label: {
            HStack(spacing: 8) {
                Image(systemName: showAdvanced ? "chevron.down.circle.fill" : "chevron.right.circle.fill")
                    .font(.system(size: 14, weight: .semibold))
                Text(showAdvanced ? "收起高级调试" : "展开高级调试")
                    .font(.system(size: 13, weight: .semibold))
                Spacer()
                Text(showAdvanced ? "all fields" : "summary only")
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.58))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 12)
            .frame(height: 36)
            .background(Color.white.opacity(0.055))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(AppPalette.line, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
    }

    private var clearCalibrationButton: some View {
        Button {
            appState.clearCalibrationResults()
            arCapture.resetGripperPathCalibration()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                arCapture.resetGripperPathCalibration()
            }
        } label: {
            Label("清除校准", systemImage: "trash")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
                .padding(.horizontal, 9)
                .frame(height: 30)
                .background(AppPalette.red.opacity(0.18))
                .overlay(
                    RoundedRectangle(cornerRadius: 7)
                        .stroke(AppPalette.red.opacity(0.78), lineWidth: 1)
                )
                .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Clear calibration")
    }

    private func handleNextCalibrationTap() {
        if appState.phase == .calibrationGripper {
            arCapture.finishGripperPathCalibration()
        } else {
            appState.advanceCalibration()
        }
    }
}

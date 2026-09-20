import ARKit
import AVFoundation
import CoreImage
import Foundation
import ObjectiveC.runtime
import SceneKit
import SwiftUI
import UIKit
import simd

struct UltraWidePoseFrame {
    var statusText: String
    var trackingText: String
    var frameText: String
    var fpsText: String
    var intrinsicsText: String
    var positionText: String
    var eulerText: String
    var velocityText: String
    var timestampText: String
    var compactPoseText: String
}

struct UltraWidePoseARView: UIViewRepresentable {
    @ObservedObject var model: PoseCheckModel

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.backgroundColor = .black
        view.scene = SCNScene()
        view.automaticallyUpdatesLighting = false
        view.autoenablesDefaultLighting = false
        view.delegate = context.coordinator
        view.session.delegate = context.coordinator
        context.coordinator.attach(view)
        context.coordinator.startSessionIfNeeded()
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        context.coordinator.model = model
        context.coordinator.startSessionIfNeeded()
    }

    static func dismantleUIView(_ uiView: ARSCNView, coordinator: Coordinator) {
        uiView.session.pause()
        uiView.delegate = nil
        uiView.session.delegate = nil
        coordinator.detach()
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(model: model)
    }

    final class Coordinator: NSObject, ARSCNViewDelegate, ARSessionDelegate {
        weak var model: PoseCheckModel?

        private weak var sceneView: ARSCNView?
        private var isRunning = false
        private var selectedFormat: ARConfiguration.VideoFormat?
        private var lastPublishTimestamp: TimeInterval = 0
        private var lastFileLogTimestamp: TimeInterval = 0
        private var lastFrameTimestamp: TimeInterval?
        private var lastPosition: SIMD3<Float>?
        private var runningStatusText = "starting"
        private var didRunRuntimeProbe = false
        private var lastPrivateUltraWideLogTimestamp: TimeInterval = 0
        private var lastPrivateUltraWideTimestamp: TimeInterval?
        private var privateUltraWideFrameCount = 0
        private var didLogPrivateUltraWideUnavailable = false
        private let gripperVisionProcessor = ARUWGripperVisionProcessor()
        private let ciContext = CIContext()

        init(model: PoseCheckModel) {
            self.model = model
        }

        func attach(_ view: ARSCNView) {
            sceneView = view
        }

        func detach() {
            sceneView = nil
            isRunning = false
            DispatchQueue.main.async {
                UIApplication.shared.isIdleTimerDisabled = false
            }
        }

        func startSessionIfNeeded() {
            guard !isRunning else { return }
            guard let sceneView else { return }
            guard ARWorldTrackingConfiguration.isSupported else {
                publishConfiguration(
                    status: "ARWorldTracking unsupported",
                    selectedFormat: "--",
                    availableFormats: "",
                    usesUltraWide: false
                )
                return
            }

            let formats = ARWorldTrackingConfiguration.supportedVideoFormats
            let availableText = formats
                .map(Self.formatSummary)
                .joined(separator: "\n")
            Self.resetLog()
            let diagnostics = Self.arkitCameraDiagnostics()
            Self.appendLog("arkit diagnostics:\n\(diagnostics)")
            Self.appendLog("available formats:\n\(availableText)")
            DispatchQueue.main.async { [weak self] in
                self?.model?.publishDiagnostics(diagnostics)
            }

            let selected = Self.pickWorldUltraWideFormat(from: formats)
                .map { (format: $0, mode: ARRunMode.worldTracking, isUltraWide: true, status: "running ARWorld ultrawide") }
                ?? Self.pickWideFallbackFormat(from: formats).map {
                    (format: $0, mode: ARRunMode.worldTracking, isUltraWide: false, status: "running ARWorld wide runtime probe")
                }
                ?? Self.pickPositionalUltraWideFormat().map {
                    (format: $0, mode: ARRunMode.positionalTracking, isUltraWide: true, status: "running ARPositional ultrawide 10fps")
                }

            guard let selected else {
                publishConfiguration(
                    status: "no usable ARKit videoFormat",
                    selectedFormat: "none",
                    availableFormats: availableText,
                    usesUltraWide: false
                )
                return
            }

            selectedFormat = selected.format
            runningStatusText = selected.status

            let configuration = Self.makeConfiguration(mode: selected.mode, format: selected.format)

            publishConfiguration(
                status: selected.status,
                selectedFormat: Self.formatSummary(selected.format),
                availableFormats: availableText,
                usesUltraWide: selected.isUltraWide
            )

            print("[pose-check][ar] selected format \(Self.formatSummary(selected.format)) ultraWide=\(selected.isUltraWide)")
            Self.appendLog("selected mode \(selected.mode.rawValue) format \(Self.formatSummary(selected.format)) ultraWide=\(selected.isUltraWide)")
            sceneView.session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
            UIApplication.shared.isIdleTimerDisabled = true
            isRunning = true
        }

        func session(_ session: ARSession, didUpdate frame: ARFrame) {
            guard isRunning else { return }
            if !didRunRuntimeProbe {
                didRunRuntimeProbe = true
                let report = Self.runtimeProbe(
                    session: session,
                    frame: frame,
                    format: selectedFormat
                )
                Self.appendLog(report)
                DispatchQueue.main.async { [weak self] in
                    self?.model?.publishDiagnostics("runtime probe written to pose_check_log.txt")
                }
            }
            logPrivateUltraWideIfAvailable(from: frame)
            guard frame.timestamp - lastPublishTimestamp >= 1.0 / 20.0 else { return }
            lastPublishTimestamp = frame.timestamp

            let poseFrame = makePoseFrame(from: frame)
            if frame.timestamp - lastFileLogTimestamp >= 1.0 {
                lastFileLogTimestamp = frame.timestamp
                Self.appendLog(
                    "\(poseFrame.statusText) | \(poseFrame.trackingText) | \(poseFrame.frameText) | \(poseFrame.positionText) | \(poseFrame.eulerText)"
                )
            }
            DispatchQueue.main.async { [weak self] in
                self?.model?.publishFrame(poseFrame)
            }
        }

        func session(_ session: ARSession, didFailWithError error: Error) {
            publishStatus("AR failed: \(error.localizedDescription)")
        }

        func sessionWasInterrupted(_ session: ARSession) {
            publishStatus("AR interrupted")
        }

        func sessionInterruptionEnded(_ session: ARSession) {
            isRunning = false
            lastFrameTimestamp = nil
            lastPosition = nil
            startSessionIfNeeded()
        }

        private func publishStatus(_ status: String) {
            DispatchQueue.main.async { [weak self] in
                self?.model?.statusText = status
            }
            print("[pose-check][ar] \(status)")
        }

        private func logPrivateUltraWideIfAvailable(from frame: ARFrame) {
            let snapshot = Self.privateUltraWideSnapshot(from: frame)
            if let snapshot {
                if lastPrivateUltraWideTimestamp.map({ abs($0 - snapshot.timestamp) > 0.000_1 }) ?? true {
                    privateUltraWideFrameCount += 1
                    lastPrivateUltraWideTimestamp = snapshot.timestamp
                    let gripperState = gripperVisionProcessor.process(
                        pixelBuffer: snapshot.pixelBuffer,
                        timestamp: snapshot.timestamp
                    )
                    let previewImage = Self.previewImage(from: snapshot.pixelBuffer, context: ciContext)
                    DispatchQueue.main.async { [weak self] in
                        self?.model?.publishPrivateUltraWide(
                            text: "privateUW \(snapshot.logText) count=\(self?.privateUltraWideFrameCount ?? 0)",
                            gripper: gripperState,
                            previewImage: previewImage
                        )
                    }
                }
            } else if !didLogPrivateUltraWideUnavailable {
                didLogPrivateUltraWideUnavailable = true
                Self.appendLog("privateUW unavailable: capturedUltraWideImage returned nil or selector missing")
            }

            guard frame.timestamp - lastPrivateUltraWideLogTimestamp >= 1.0 else { return }
            lastPrivateUltraWideLogTimestamp = frame.timestamp
            if let snapshot {
                Self.appendLog("privateUW count=\(privateUltraWideFrameCount) \(snapshot.logText)")
                if let state = model?.gripperVisionState {
                    Self.appendLog("gripperUW \(state.statusText) ids=\(state.idsText) \(state.frameText) \(state.roiText) input=\(state.inputText) left=\(state.leftMarker.centerText) right=\(state.rightMarker.centerText)")
                }
            } else {
                Self.appendLog("privateUW count=\(privateUltraWideFrameCount) unavailable")
            }
        }

        private func publishConfiguration(
            status: String,
            selectedFormat: String,
            availableFormats: String,
            usesUltraWide: Bool
        ) {
            DispatchQueue.main.async { [weak self] in
                self?.model?.publishConfiguration(
                    status: status,
                    selectedFormat: selectedFormat,
                    availableFormats: availableFormats,
                    usesUltraWide: usesUltraWide
                )
            }
            print("[pose-check][ar] \(status) selected=\(selectedFormat)")
            Self.appendLog("\(status) selected=\(selectedFormat)")
        }

        private func makePoseFrame(from frame: ARFrame) -> UltraWidePoseFrame {
            let transform = frame.camera.transform
            let position = SIMD3<Float>(
                transform.columns.3.x,
                transform.columns.3.y,
                transform.columns.3.z
            )
            let euler = Self.eulerDegrees(from: transform)
            let image = frame.capturedImage
            let width = CVPixelBufferGetWidth(image)
            let height = CVPixelBufferGetHeight(image)
            let intrinsics = frame.camera.intrinsics

            let dt = lastFrameTimestamp.map { max(0.000_1, frame.timestamp - $0) }
            let fps = dt.map { 1.0 / $0 }
            let velocity: SIMD3<Float>?
            if let lastPosition, let dt {
                velocity = (position - lastPosition) / Float(dt)
            } else {
                velocity = nil
            }
            lastFrameTimestamp = frame.timestamp
            lastPosition = position

            let tracking = Self.trackingText(frame.camera.trackingState)
            let positionText = String(
                format: "x %.3f  y %.3f  z %.3f m",
                position.x,
                position.y,
                position.z
            )
            let eulerText = String(
                format: "roll %.1f  pitch %.1f  yaw %.1f deg",
                euler.x,
                euler.y,
                euler.z
            )
            let velocityText = velocity.map {
                String(format: "vx %.3f  vy %.3f  vz %.3f m/s", $0.x, $0.y, $0.z)
            } ?? "v --"
            let compactPose = String(
                format: "p[%.2f %.2f %.2f] e[%.0f %.0f %.0f] %@",
                position.x,
                position.y,
                position.z,
                euler.x,
                euler.y,
                euler.z,
                tracking
            )

            return UltraWidePoseFrame(
                statusText: runningStatusText,
                trackingText: tracking,
                frameText: "\(width)x\(height)",
                fpsText: fps.map { String(format: "%.1f Hz", $0) } ?? "--",
                intrinsicsText: String(
                    format: "fx %.1f fy %.1f cx %.1f cy %.1f",
                    intrinsics.columns.0.x,
                    intrinsics.columns.1.y,
                    intrinsics.columns.2.x,
                    intrinsics.columns.2.y
                ),
                positionText: positionText,
                eulerText: eulerText,
                velocityText: velocityText,
                timestampText: String(format: "%.3f s", frame.timestamp),
                compactPoseText: compactPose
            )
        }

        private enum ARRunMode: String {
            case worldTracking
            case positionalTracking
        }

        private static func makeConfiguration(
            mode: ARRunMode,
            format: ARConfiguration.VideoFormat
        ) -> ARConfiguration {
            switch mode {
            case .worldTracking:
                let configuration = ARWorldTrackingConfiguration()
                configuration.worldAlignment = .gravity
                configuration.planeDetection = []
                configuration.environmentTexturing = .none
                configuration.providesAudioData = false
                configuration.isAutoFocusEnabled = true
                configuration.videoFormat = format
                return configuration
            case .positionalTracking:
                let configuration = ARPositionalTrackingConfiguration()
                configuration.worldAlignment = .gravity
                configuration.planeDetection = []
                configuration.providesAudioData = false
                configuration.videoFormat = format
                return configuration
            }
        }

        private static func pickWorldUltraWideFormat(
            from formats: [ARConfiguration.VideoFormat]
        ) -> ARConfiguration.VideoFormat? {
            formats
                .filter { $0.captureDeviceType == .builtInUltraWideCamera }
                .sorted { lhs, rhs in
                    if lhs.framesPerSecond != rhs.framesPerSecond {
                        return lhs.framesPerSecond > rhs.framesPerSecond
                    }
                    let lhsArea = lhs.imageResolution.width * lhs.imageResolution.height
                    let rhsArea = rhs.imageResolution.width * rhs.imageResolution.height
                    return lhsArea > rhsArea
                }
                .first
        }

        private static func pickPositionalUltraWideFormat() -> ARConfiguration.VideoFormat? {
            guard ARPositionalTrackingConfiguration.isSupported else { return nil }
            return ARPositionalTrackingConfiguration.supportedVideoFormats
                .filter { $0.captureDeviceType == .builtInUltraWideCamera }
                .sorted { lhs, rhs in
                    if lhs.framesPerSecond != rhs.framesPerSecond {
                        return lhs.framesPerSecond > rhs.framesPerSecond
                    }
                    let lhsArea = lhs.imageResolution.width * lhs.imageResolution.height
                    let rhsArea = rhs.imageResolution.width * rhs.imageResolution.height
                    return lhsArea > rhsArea
                }
                .first
        }

        private static func pickWideFallbackFormat(
            from formats: [ARConfiguration.VideoFormat]
        ) -> ARConfiguration.VideoFormat? {
            formats
                .filter { $0.captureDeviceType == .builtInWideAngleCamera }
                .sorted { lhs, rhs in
                    if lhs.framesPerSecond != rhs.framesPerSecond {
                        return lhs.framesPerSecond > rhs.framesPerSecond
                    }
                    let lhsArea = lhs.imageResolution.width * lhs.imageResolution.height
                    let rhsArea = rhs.imageResolution.width * rhs.imageResolution.height
                    return lhsArea > rhsArea
                }
                .first
        }

        private static func formatSummary(_ format: ARConfiguration.VideoFormat) -> String {
            let size = format.imageResolution
            let deviceType = format.captureDeviceType.rawValue
                .replacingOccurrences(of: "AVCaptureDeviceType", with: "")
            return String(
                format: "%@ %.0fx%.0f %ldfps",
                deviceType,
                size.width,
                size.height,
                format.framesPerSecond
            )
        }

        private static func arkitCameraDiagnostics() -> String {
            var lines: [String] = []

            lines.append("ARWorld configurable: \(captureDeviceSummary(ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera))")
            lines.append("ARWorld recommended4K: \(ARWorldTrackingConfiguration.recommendedVideoFormatFor4KResolution.map(formatSummary) ?? "nil")")
            lines.append("ARWorld recommendedHiRes: \(ARWorldTrackingConfiguration.recommendedVideoFormatForHighResolutionFrameCapturing.map(formatSummary) ?? "nil")")
            lines.append("")
            appendFormats("World", ARWorldTrackingConfiguration.isSupported, ARWorldTrackingConfiguration.supportedVideoFormats, to: &lines)
            appendFormats("Orientation", AROrientationTrackingConfiguration.isSupported, AROrientationTrackingConfiguration.supportedVideoFormats, to: &lines)
            if #available(iOS 12.0, *) {
                appendFormats("ImageTracking", ARImageTrackingConfiguration.isSupported, ARImageTrackingConfiguration.supportedVideoFormats, to: &lines)
                appendFormats("ObjectScanning", ARObjectScanningConfiguration.isSupported, ARObjectScanningConfiguration.supportedVideoFormats, to: &lines)
            }
            if #available(iOS 13.0, *) {
                appendFormats("Body", ARBodyTrackingConfiguration.isSupported, ARBodyTrackingConfiguration.supportedVideoFormats, to: &lines)
                appendFormats("Positional", ARPositionalTrackingConfiguration.isSupported, ARPositionalTrackingConfiguration.supportedVideoFormats, to: &lines)
            }
            if #available(iOS 14.0, *) {
                appendFormats("Geo", ARGeoTrackingConfiguration.isSupported, ARGeoTrackingConfiguration.supportedVideoFormats, to: &lines)
            }

            return lines.joined(separator: "\n")
        }

        private static func runtimeProbe(
            session: ARSession,
            frame: ARFrame,
            format: ARConfiguration.VideoFormat?
        ) -> String {
            var lines: [String] = []
            lines.append("runtime probe begin")
            lines.append("probe note: conservative class metadata scan only; not invoking private selectors")
            lines.append("current capturedImage: \(CVPixelBufferGetWidth(frame.capturedImage))x\(CVPixelBufferGetHeight(frame.capturedImage))")
            lines.append("current camera class: \(NSStringFromClass(type(of: frame.camera)))")
            lines.append("current frame class: \(NSStringFromClass(type(of: frame)))")
            lines.append("selected format: \(format.map(formatSummary) ?? "nil")")
            lines.append("")

            appendRuntimeClassTree("ARSession class", ARSession.self, to: &lines)
            appendRuntimeClassTree("ARFrame class", ARFrame.self, to: &lines)
            appendRuntimeClassTree("ARCamera class", ARCamera.self, to: &lines)
            appendRuntimeClassTree("ARVideoFormat class", ARConfiguration.VideoFormat.self, to: &lines)
            lines.append("")
            appendNamedARKitClasses(to: &lines)
            lines.append("runtime probe end")
            return lines.joined(separator: "\n")
        }

        private struct PrivateUltraWideSnapshot {
            var pixelBuffer: CVPixelBuffer
            var width: Int
            var height: Int
            var pixelFormat: OSType
            var timestamp: TimeInterval
            var cameraText: String
            var pointerText: String

            var logText: String {
                "\(width)x\(height) pixfmt=\(Self.pixelFormatText(pixelFormat)) ts=\(String(format: "%.6f", timestamp)) camera=\(cameraText) buffer=\(pointerText)"
            }

            private static func pixelFormatText(_ value: OSType) -> String {
                let chars = [
                    Character(UnicodeScalar((value >> 24) & 0xff) ?? " "),
                    Character(UnicodeScalar((value >> 16) & 0xff) ?? " "),
                    Character(UnicodeScalar((value >> 8) & 0xff) ?? " "),
                    Character(UnicodeScalar(value & 0xff) ?? " "),
                ]
                let text = String(chars)
                return text.trimmingCharacters(in: .whitespaces).isEmpty ? "\(value)" : "\(value)/\(text)"
            }
        }

        private static func privateUltraWideSnapshot(from frame: ARFrame) -> PrivateUltraWideSnapshot? {
            let imageSelector = NSSelectorFromString("capturedUltraWideImage")
            guard frame.responds(to: imageSelector),
                  let imageIMP = frame.method(for: imageSelector) else {
                return nil
            }

            typealias PixelBufferGetter = @convention(c) (AnyObject, Selector) -> CVPixelBuffer?
            let imageGetter = unsafeBitCast(imageIMP, to: PixelBufferGetter.self)
            guard let buffer = imageGetter(frame, imageSelector) else {
                return nil
            }

            let timestamp = privateUltraWideTimestamp(from: frame) ?? frame.timestamp
            let cameraText = privateUltraWideCameraText(from: frame)
            let pointer = Unmanaged.passUnretained(buffer).toOpaque()

            return PrivateUltraWideSnapshot(
                pixelBuffer: buffer,
                width: CVPixelBufferGetWidth(buffer),
                height: CVPixelBufferGetHeight(buffer),
                pixelFormat: CVPixelBufferGetPixelFormatType(buffer),
                timestamp: timestamp,
                cameraText: cameraText,
                pointerText: "\(pointer)"
            )
        }

        private static func previewImage(from pixelBuffer: CVPixelBuffer, context: CIContext) -> UIImage? {
            let image = CIImage(cvPixelBuffer: pixelBuffer)
            guard let cgImage = context.createCGImage(image, from: image.extent) else {
                return nil
            }
            return UIImage(cgImage: cgImage)
        }

        private static func privateUltraWideTimestamp(from frame: ARFrame) -> TimeInterval? {
            let selector = NSSelectorFromString("ultraWideImageTimestamp")
            guard frame.responds(to: selector),
                  let timestampIMP = frame.method(for: selector) else {
                return nil
            }
            typealias TimestampGetter = @convention(c) (AnyObject, Selector) -> Double
            let timestampGetter = unsafeBitCast(timestampIMP, to: TimestampGetter.self)
            return timestampGetter(frame, selector)
        }

        private static func privateUltraWideCameraText(from frame: ARFrame) -> String {
            let selector = NSSelectorFromString("ultraWideCamera")
            guard frame.responds(to: selector),
                  let cameraIMP = frame.method(for: selector) else {
                return "missing"
            }
            typealias CameraGetter = @convention(c) (AnyObject, Selector) -> AnyObject?
            let cameraGetter = unsafeBitCast(cameraIMP, to: CameraGetter.self)
            guard let camera = cameraGetter(frame, selector) as? ARCamera else {
                return "nil"
            }
            let resolution = camera.imageResolution
            let intrinsics = camera.intrinsics
            return String(
                format: "%.0fx%.0f fx=%.1f fy=%.1f cx=%.1f cy=%.1f",
                resolution.width,
                resolution.height,
                intrinsics.columns.0.x,
                intrinsics.columns.1.y,
                intrinsics.columns.2.x,
                intrinsics.columns.2.y
            )
        }

        private static func appendRuntimeClassTree(
            _ title: String,
            _ rootClass: AnyClass,
            to lines: inout [String]
        ) {
            lines.append("[\(title)]")
            var cls: AnyClass? = rootClass
            var depth = 0
            while let current = cls, depth < 8 {
                appendRuntimeClass(current, to: &lines)
                cls = class_getSuperclass(current)
                depth += 1
            }
            lines.append("")
        }

        private static func appendRuntimeClass(_ cls: AnyClass, to lines: inout [String]) {
            lines.append("class \(NSStringFromClass(cls))")

            let properties = runtimeProperties(for: cls)
            if properties.isEmpty {
                lines.append("  properties: none matching")
            } else {
                lines.append("  properties:")
                properties.prefix(40).forEach { lines.append("    \($0)") }
                if properties.count > 40 {
                    lines.append("    ... \(properties.count - 40) more")
                }
            }

            let ivars = runtimeIvars(for: cls)
            if ivars.isEmpty {
                lines.append("  ivars: none matching")
            } else {
                lines.append("  ivars:")
                ivars.prefix(40).forEach { lines.append("    \($0)") }
                if ivars.count > 40 {
                    lines.append("    ... \(ivars.count - 40) more")
                }
            }

            let methods = runtimeMethods(for: cls)
            if methods.isEmpty {
                lines.append("  methods: none matching")
            } else {
                lines.append("  methods:")
                methods.prefix(80).forEach { lines.append("    \($0)") }
                if methods.count > 80 {
                    lines.append("    ... \(methods.count - 80) more")
                }
            }
        }

        private static func appendNamedARKitClasses(to lines: inout [String]) {
            let names = [
                "ARBackFacingUltraWideImageSensor",
                "ARFixedIntrinsicsForBackUltraWideCamera640x480",
                "ARSyncedUltraWideForwardingStrategy",
                "ARWorldTrackingBackUltraWideCalibrationParameters",
                "ARWorldTrackingBackUltraWideCalibrationParametersUserDefaultsKey",
            ]

            lines.append("[known private ARKit class lookup]")
            for name in names {
                if let cls = NSClassFromString(name) {
                    lines.append("  \(name): present as \(NSStringFromClass(cls))")
                    appendRuntimeClass(cls, to: &lines)
                } else if let cls = NSClassFromString("ARKit.\(name)") {
                    lines.append("  ARKit.\(name): present as \(NSStringFromClass(cls))")
                    appendRuntimeClass(cls, to: &lines)
                } else {
                    lines.append("  \(name): not found")
                }
            }
        }

        private static func runtimeProperties(for cls: AnyClass) -> [String] {
            var count: UInt32 = 0
            guard let list = class_copyPropertyList(cls, &count) else { return [] }
            defer { free(list) }
            return (0..<Int(count))
                .compactMap { index -> String? in
                    let property = list[index]
                    let name = String(cString: property_getName(property))
                    let attrs = property_getAttributes(property).map { String(cString: $0) } ?? "?"
                    let text = "\(name) attrs=\(attrs)"
                    return runtimeMemberMatches(text) ? text : nil
                }
                .sorted()
        }

        private static func runtimeIvars(for cls: AnyClass) -> [String] {
            var count: UInt32 = 0
            guard let list = class_copyIvarList(cls, &count) else { return [] }
            defer { free(list) }
            return (0..<Int(count))
                .compactMap { index -> String? in
                    let ivar = list[index]
                    let name = ivar_getName(ivar).map { String(cString: $0) } ?? "?"
                    let type = ivar_getTypeEncoding(ivar).map { String(cString: $0) } ?? "?"
                    let offset = ivar_getOffset(ivar)
                    let text = "\(name) type=\(type) offset=\(offset)"
                    return runtimeMemberMatches(text) ? text : nil
                }
                .sorted()
        }

        private static func runtimeMethods(for cls: AnyClass) -> [String] {
            var count: UInt32 = 0
            guard let list = class_copyMethodList(cls, &count) else { return [] }
            defer { free(list) }
            return (0..<Int(count))
                .compactMap { index -> String? in
                    let method = list[index]
                    let name = NSStringFromSelector(method_getName(method))
                    let encoding = method_getTypeEncoding(method).map { String(cString: $0) } ?? "?"
                    let returnType = copyMethodReturnType(method)
                    let argCount = method_getNumberOfArguments(method)
                    let text = "\(name) args=\(argCount) ret=\(returnType) enc=\(encoding)"
                    return runtimeMemberMatches(text) ? text : nil
                }
                .sorted()
        }

        private static func copyMethodReturnType(_ method: Method) -> String {
            let copied = method_copyReturnType(method)
            defer { free(copied) }
            return String(cString: copied)
        }

        private static func runtimeMemberMatches(_ text: String) -> Bool {
            let lower = text.lowercased()
            return [
                "ultra",
                "secondary",
                "synced",
                "sync",
                "multicam",
                "captur",
                "camera",
                "image",
                "sensor",
                "stream",
                "video",
                "pixel",
            ].contains { lower.contains($0) }
        }

        private static func appendFormats(
            _ name: String,
            _ supported: Bool,
            _ formats: [ARConfiguration.VideoFormat],
            to lines: inout [String]
        ) {
            let ultraCount = formats.filter { $0.captureDeviceType == .builtInUltraWideCamera }.count
            let types = Set(formats.map { shortDeviceType($0.captureDeviceType) })
                .sorted()
                .joined(separator: ",")
            lines.append("\(name): supported=\(supported) formats=\(formats.count) ultra=\(ultraCount) types=[\(types)]")
            formats
                .filter { $0.captureDeviceType == .builtInUltraWideCamera }
                .forEach { lines.append("  ultra \(formatSummary($0))") }
        }

        private static func captureDeviceSummary(_ device: AVCaptureDevice?) -> String {
            guard let device else { return "nil" }
            let constituents = device.constituentDevices
                .map { shortDeviceType($0.deviceType) }
                .joined(separator: ",")
            let active = device.activePrimaryConstituent
                .map { shortDeviceType($0.deviceType) } ?? "nil"
            return "\(shortDeviceType(device.deviceType)) name=\(device.localizedName) constituents=[\(constituents)] active=\(active) zoom=\(String(format: "%.2f", device.videoZoomFactor))"
        }

        private static func shortDeviceType(_ type: AVCaptureDevice.DeviceType) -> String {
            type.rawValue
                .replacingOccurrences(of: "AVCaptureDeviceType", with: "")
                .replacingOccurrences(of: "BuiltIn", with: "")
                .replacingOccurrences(of: "Camera", with: "")
        }

        private static func resetLog() {
            guard let url = logURL else { return }
            guard let data = "UMIFT AR UltraWide Pose Check\n".data(using: .utf8) else { return }
            try? data.write(to: url, options: .atomic)
        }

        private static func appendLog(_ message: String) {
            guard let url = logURL else { return }
            let line = String(format: "[%.3f] %@\n", Date().timeIntervalSince1970, message)
            guard let data = line.data(using: .utf8) else { return }
            if FileManager.default.fileExists(atPath: url.path),
               let handle = try? FileHandle(forWritingTo: url) {
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
                try? handle.close()
            } else {
                try? data.write(to: url, options: .atomic)
            }
        }

        private static var logURL: URL? {
            FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
                .first?
                .appendingPathComponent("pose_check_log.txt")
        }

        private static func trackingText(_ trackingState: ARCamera.TrackingState) -> String {
            switch trackingState {
            case .normal:
                return "normal"
            case .notAvailable:
                return "not available"
            case .limited(let reason):
                switch reason {
                case .excessiveMotion:
                    return "limited: motion"
                case .insufficientFeatures:
                    return "limited: features"
                case .initializing:
                    return "initializing"
                case .relocalizing:
                    return "relocalizing"
                @unknown default:
                    return "limited"
                }
            }
        }

        private static func eulerDegrees(from transform: simd_float4x4) -> SIMD3<Float> {
            let q = simd_quatf(transform)
            let sinrCosp = 2 * (q.real * q.imag.x + q.imag.y * q.imag.z)
            let cosrCosp = 1 - 2 * (q.imag.x * q.imag.x + q.imag.y * q.imag.y)
            let roll = atan2(sinrCosp, cosrCosp)

            let sinp = 2 * (q.real * q.imag.y - q.imag.z * q.imag.x)
            let pitch: Float
            if abs(sinp) >= 1 {
                pitch = copysign(Float.pi / 2, sinp)
            } else {
                pitch = asin(sinp)
            }

            let sinyCosp = 2 * (q.real * q.imag.z + q.imag.x * q.imag.y)
            let cosyCosp = 1 - 2 * (q.imag.y * q.imag.y + q.imag.z * q.imag.z)
            let yaw = atan2(sinyCosp, cosyCosp)

            let scale = Float(180.0 / Double.pi)
            return SIMD3<Float>(roll * scale, pitch * scale, yaw * scale)
        }
    }
}

import ARKit
import CoreImage
import Foundation
import ObjectiveC.runtime
import QuartzCore
import SceneKit
import SwiftUI
import UIKit
import simd

struct WorldCalibrationARStatus: Equatable {
    var statusText = "ar idle"
    var trackingText = "--"
    var centerHitText = "hit --"
    var centerDistanceM: Double?
    var originWorldText = "--"
    var anchorVisible = false

    static let idle = WorldCalibrationARStatus()
}

struct WorldCalibrationARResult {
    var timestamp: Date
    var distanceM: Double?
    var originWorldText: String
    var userWorldTransform: simd_float4x4
    var cameraTransform: simd_float4x4
    var cameraInUserWorldTransform: simd_float4x4
}

struct WorldTrackingARFrameState {
    var frameSequence: UInt64
    var timestampNS: Int64
    var trackingText: String
    var cameraTransform: simd_float4x4
    var cameraInUserWorldTransform: simd_float4x4?
    var rgbCameraIntrinsics: simd_float3x3
    var rgbCaptureDeviceType: String
    var intrinsicsText: String
    var rgbText: String
    var rgbPixelBuffer: CVPixelBuffer
    var depthAvailable: Bool
    var depthSource: String
    var depthMapPixelBuffer: CVPixelBuffer?
    var depthWidth: Int?
    var depthHeight: Int?
    var depthUnit: String
    var depthPixelFormat: OSType?
    var centerDepthM: Double?
    var depthValidRatio: Double
    var depthConfidenceText: String
}

struct WorldCalibrationARView: UIViewRepresentable {
    // These settings affect only the on-screen SceneKit render target. ARKit
    // tracking, capturedImage, scene depth, and the recording payload stay native.
    private static let previewRenderFramesPerSecond = 30
    private static let previewRenderScale: CGFloat = 1.0

    let isActive: Bool
    let markRequestID: Int
    let userWorldTransform: simd_float4x4?
    let showWorldAxis: Bool
    let renderUltraWidePreview: Bool
    let videoEncoder: H264VideoEncoder
    let onStatusUpdate: (WorldCalibrationARStatus) -> Void
    let onOriginMarked: (WorldCalibrationARResult) -> Void
    let onFrameUpdate: (WorldTrackingARFrameState) -> Void
    let onPrivateUltraWideFrame: (PrivateUltraWideFrameState) -> Void

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.backgroundColor = .black
        view.preferredFramesPerSecond = Self.previewRenderFramesPerSecond
        view.contentScaleFactor = min(UIScreen.main.scale, Self.previewRenderScale)
        view.automaticallyUpdatesLighting = true
        view.autoenablesDefaultLighting = true
        view.scene = SCNScene()
        view.delegate = context.coordinator
        view.session.delegate = context.coordinator
        context.coordinator.attach(view)
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        context.coordinator.onStatusUpdate = onStatusUpdate
        context.coordinator.onOriginMarked = onOriginMarked
        context.coordinator.onFrameUpdate = onFrameUpdate
        context.coordinator.onPrivateUltraWideFrame = onPrivateUltraWideFrame
        context.coordinator.userWorldTransform = userWorldTransform
        context.coordinator.setShowsWorldAxis(showWorldAxis)
        context.coordinator.setUltraWidePreviewRendering(renderUltraWidePreview)
        if isActive {
            context.coordinator.startSessionIfNeeded()
            context.coordinator.consumeMarkRequest(markRequestID)
        } else {
            context.coordinator.pauseSession()
        }
    }

    static func dismantleUIView(_ uiView: ARSCNView, coordinator: Coordinator) {
        coordinator.detach()
        uiView.delegate = nil
        uiView.session.delegate = nil
        uiView.session.pause()
        uiView.scene.rootNode.childNodes.forEach { $0.removeFromParentNode() }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(
            videoEncoder: videoEncoder,
            onStatusUpdate: onStatusUpdate,
            onOriginMarked: onOriginMarked,
            onFrameUpdate: onFrameUpdate,
            onPrivateUltraWideFrame: onPrivateUltraWideFrame
        )
    }

    final class Coordinator: NSObject, ARSCNViewDelegate, ARSessionDelegate {
        var onStatusUpdate: (WorldCalibrationARStatus) -> Void
        var onOriginMarked: (WorldCalibrationARResult) -> Void
        var onFrameUpdate: (WorldTrackingARFrameState) -> Void
        var onPrivateUltraWideFrame: (PrivateUltraWideFrameState) -> Void
        var userWorldTransform: simd_float4x4?

        private let videoEncoder: H264VideoEncoder
        private weak var sceneView: ARSCNView?
        private var isSessionRunning = false
        private var lastMarkRequestID = 0
        private var lastStatusPublishTime: TimeInterval = 0
        private var lastFramePublishTime: TimeInterval = 0
        private let framePublishIntervalS: TimeInterval = 1.0 / 30.0
        private var nextMainFrameSequence: UInt64 = 0
        private var axisNode: SCNNode?
        private var lastStatus = WorldCalibrationARStatus.idle
        private var isActiveRequested = false
        private var shouldShowWorldAxis = false
        private var lastPrivateUltraWideTimestampNS: Int64?
        private var lastUltraWidePreviewTimestampNS: Int64 = 0
        private var pendingMarkRequestID: Int?
        private var pendingMarkAttemptCount = 0
        private var lastPendingMarkAttemptTime: TimeInterval = 0
        private var lastPendingMarkLogTime: TimeInterval = 0
        private let ciContext = CIContext()
        private var shouldRenderUltraWidePreview = false
        private let ultraWidePreviewIntervalNS: Int64 = 100_000_000
        private let ultraWidePreviewMaxDimension: CGFloat = 960

        init(
            videoEncoder: H264VideoEncoder,
            onStatusUpdate: @escaping (WorldCalibrationARStatus) -> Void,
            onOriginMarked: @escaping (WorldCalibrationARResult) -> Void,
            onFrameUpdate: @escaping (WorldTrackingARFrameState) -> Void,
            onPrivateUltraWideFrame: @escaping (PrivateUltraWideFrameState) -> Void
        ) {
            self.videoEncoder = videoEncoder
            self.onStatusUpdate = onStatusUpdate
            self.onOriginMarked = onOriginMarked
            self.onFrameUpdate = onFrameUpdate
            self.onPrivateUltraWideFrame = onPrivateUltraWideFrame
        }

        func attach(_ view: ARSCNView) {
            sceneView = view
        }

        func startSessionIfNeeded() {
            guard let sceneView else { return }
            guard ARWorldTrackingConfiguration.isSupported else {
                publishStatus(.init(statusText: "ar unsupported", trackingText: "--", centerHitText: "hit --"))
                return
            }
            isActiveRequested = true
            guard !isSessionRunning else { return }

            let configuration = ARWorldTrackingConfiguration()
            configuration.worldAlignment = .gravity
            configuration.planeDetection = [.horizontal]
            configuration.environmentTexturing = .none
            configuration.providesAudioData = false
            if let videoFormat = Self.preferredWideVideoFormat() {
                configuration.videoFormat = videoFormat
                print(
                    "[UMIFT][ar-view] selected video format "
                    + "\(Int(videoFormat.imageResolution.width))x\(Int(videoFormat.imageResolution.height)) "
                    + "@\(videoFormat.framesPerSecond)fps"
                )
            } else {
                let supported = ARWorldTrackingConfiguration.supportedVideoFormats.map {
                    "\(Int($0.imageResolution.width))x\(Int($0.imageResolution.height))@\($0.framesPerSecond)"
                }
                print("[UMIFT][ar-view] no 1920x1440@30 format; supported=\(supported.joined(separator: ","))")
            }
            if ARWorldTrackingConfiguration.supportsFrameSemantics(.smoothedSceneDepth) {
                configuration.frameSemantics.insert(.smoothedSceneDepth)
            } else if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
                configuration.frameSemantics.insert(.sceneDepth)
            }

            sceneView.scene.rootNode.childNodes.forEach { $0.removeFromParentNode() }
            axisNode = nil
            print("[UMIFT][ar-view] session run resetTracking removeAnchors")
            sceneView.session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
            isSessionRunning = true
            UIApplication.shared.isIdleTimerDisabled = true
            publishStatus(.init(statusText: "ar starting", trackingText: "--", centerHitText: "find table"))
        }

        func pauseSession() {
            stopSession(publishStatusUpdate: true)
        }

        func detach() {
            stopSession(publishStatusUpdate: false)
            sceneView?.delegate = nil
            sceneView?.session.delegate = nil
            sceneView = nil
        }

        private func stopSession(publishStatusUpdate: Bool) {
            isActiveRequested = false
            print("[UMIFT][ar-view] session pause publish=\(publishStatusUpdate)")
            sceneView?.session.pause()
            isSessionRunning = false
            axisNode = nil
            pendingMarkRequestID = nil
            pendingMarkAttemptCount = 0
            lastPendingMarkAttemptTime = 0
            UIApplication.shared.isIdleTimerDisabled = false
            if publishStatusUpdate {
                publishStatus(.idle)
            }
        }

        func setShowsWorldAxis(_ show: Bool) {
            shouldShowWorldAxis = show
            if show, let axisNode, axisNode.parent == nil {
                sceneView?.scene.rootNode.addChildNode(axisNode)
            } else if !show {
                axisNode?.removeFromParentNode()
            }
        }

        func setUltraWidePreviewRendering(_ render: Bool) {
            guard shouldRenderUltraWidePreview != render else { return }
            shouldRenderUltraWidePreview = render
            if render {
                lastUltraWidePreviewTimestampNS = 0
            }
        }

        func consumeMarkRequest(_ requestID: Int) {
            guard requestID > 0 else { return }
            if requestID == lastMarkRequestID {
                if pendingMarkRequestID != nil {
                    attemptPendingMarkRequest(reason: "view_update")
                }
                return
            }
            lastMarkRequestID = requestID
            pendingMarkRequestID = requestID
            pendingMarkAttemptCount = 0
            lastPendingMarkAttemptTime = 0
            print("[UMIFT][ar-view] queue mark request id=\(requestID)")
            publishStatus(WorldCalibrationARStatus(
                statusText: "mark pending",
                trackingText: lastStatus.trackingText,
                centerHitText: lastStatus.centerHitText,
                centerDistanceM: lastStatus.centerDistanceM,
                originWorldText: lastStatus.originWorldText,
                anchorVisible: axisNode?.parent != nil
            ))
            attemptPendingMarkRequest(reason: "request")
        }

        func renderer(_ renderer: SCNSceneRenderer, updateAtTime time: TimeInterval) {
            guard isSessionRunning, time - lastStatusPublishTime > 0.16 else { return }
            lastStatusPublishTime = time
            DispatchQueue.main.async { [weak self] in
                self?.updateLiveStatus()
            }
        }

        func session(_ session: ARSession, didFailWithError error: Error) {
            publishStatus(.init(statusText: "ar failed \(error.localizedDescription)", trackingText: "--", centerHitText: "hit --"))
        }

        func session(_ session: ARSession, didUpdate frame: ARFrame) {
            guard isSessionRunning else { return }
            publishPrivateUltraWideIfAvailable(from: frame)

            guard lastFramePublishTime == 0 || frame.timestamp - lastFramePublishTime >= framePublishIntervalS * 0.9 else { return }
            lastFramePublishTime = frame.timestamp
            let frameSequence = nextMainFrameSequence
            nextMainFrameSequence &+= 1
            let captureTimestampNS = Int64(frame.timestamp * 1_000_000_000)
            videoEncoder.encode(
                pixelBuffer: frame.capturedImage,
                sequence: frameSequence,
                captureTimestampNS: captureTimestampNS
            )
            let state = Self.frameState(
                from: frame,
                frameSequence: frameSequence,
                userWorldTransform: userWorldTransform
            )
            DispatchQueue.main.async { [onFrameUpdate] in
                onFrameUpdate(state)
            }
        }

        func sessionWasInterrupted(_ session: ARSession) {
            publishStatus(.init(statusText: "ar interrupted", trackingText: "--", centerHitText: "hit --"))
        }

        func sessionInterruptionEnded(_ session: ARSession) {
            isSessionRunning = false
            if isActiveRequested {
                startSessionIfNeeded()
            }
        }

        private func updateLiveStatus() {
            guard let sceneView, let frame = sceneView.session.currentFrame else {
                publishStatus(.init(statusText: "ar waiting", trackingText: "--", centerHitText: "find table"))
                return
            }

            let hit = centerRaycast(in: sceneView)
            var status = WorldCalibrationARStatus(
                statusText: "ar running",
                trackingText: Self.trackingText(frame.camera.trackingState),
                centerHitText: hit == nil ? "find horizontal surface" : "center horizontal hit",
                centerDistanceM: hit?.distanceM,
                originWorldText: lastStatus.originWorldText,
                anchorVisible: axisNode?.parent != nil
            )
            if axisNode?.parent == nil {
                status.originWorldText = "--"
            }
            publishStatus(status)
            if hit != nil {
                attemptPendingMarkRequest(reason: "center_hit")
            } else {
                logPendingMarkWait(reason: "no_center_hit")
            }
        }

        private func attemptPendingMarkRequest(reason: String) {
            guard let requestID = pendingMarkRequestID else { return }
            let now = CACurrentMediaTime()
            if reason != "request", now - lastPendingMarkAttemptTime < 0.20 {
                return
            }
            lastPendingMarkAttemptTime = now
            pendingMarkAttemptCount += 1
            print("[UMIFT][ar-view] attempt mark request id=\(requestID) reason=\(reason) attempt=\(pendingMarkAttemptCount)")
            if markCenterOrigin() {
                print("[UMIFT][ar-view] mark request completed id=\(requestID) attempts=\(pendingMarkAttemptCount)")
                pendingMarkRequestID = nil
                pendingMarkAttemptCount = 0
            }
        }

        private func logPendingMarkWait(reason: String) {
            guard let requestID = pendingMarkRequestID else { return }
            let now = CACurrentMediaTime()
            guard now - lastPendingMarkLogTime > 0.75 else { return }
            lastPendingMarkLogTime = now
            print("[UMIFT][ar-view] mark pending id=\(requestID) reason=\(reason)")
        }

        private func markCenterOrigin() -> Bool {
            guard let sceneView, let frame = sceneView.session.currentFrame else {
                print("[UMIFT][ar-view] mark failed no frame")
                publishStatus(.init(statusText: "ar waiting", trackingText: "--", centerHitText: "no frame"))
                return false
            }
            let hit: (origin: SIMD3<Float>, distanceM: Double)
            let forcedCameraOrigin: Bool
            if let centerHit = centerRaycast(in: sceneView) {
                hit = centerHit
                forcedCameraOrigin = false
            } else if DebugAutomation.forceCameraWorldOrigin {
                hit = (
                    SIMD3<Float>(
                        frame.camera.transform.columns.3.x,
                        frame.camera.transform.columns.3.y,
                        frame.camera.transform.columns.3.z
                    ),
                    0
                )
                forcedCameraOrigin = true
                print("[UMIFT][ar-view] DEBUG forcing world origin from current camera pose")
            } else {
                print("[UMIFT][ar-view] mark failed no center table hit tracking=\(Self.trackingText(frame.camera.trackingState))")
                publishStatus(WorldCalibrationARStatus(
                    statusText: "mark pending",
                    trackingText: Self.trackingText(frame.camera.trackingState),
                    centerHitText: "waiting center hit",
                    centerDistanceM: nil,
                    originWorldText: lastStatus.originWorldText,
                    anchorVisible: axisNode != nil
                ))
                return false
            }

            let userWorldTransform = Self.userWorldBasis(
                origin: hit.origin,
                cameraTransform: frame.camera.transform
            )
            let cameraInUserWorldTransform = simd_mul(
                simd_inverse(userWorldTransform),
                frame.camera.transform
            )
            let axis = makeWorldAxisNode(userWorldTransform: userWorldTransform)
            axisNode?.removeFromParentNode()
            axisNode = axis
            if shouldShowWorldAxis {
                sceneView.scene.rootNode.addChildNode(axis)
            }

            let originText = forcedCameraOrigin
                ? "DEBUG forced camera origin"
                : Self.originText(origin: hit.origin, cameraTransform: frame.camera.transform)
            print(
                "[UMIFT][ar-view] origin marked source=\(forcedCameraOrigin ? "debug_camera" : "horizontal_raycast") "
                + "dist=\(String(format: "%.3f", hit.distanceM)) \(originText)"
            )
            publishStatus(WorldCalibrationARStatus(
                statusText: forcedCameraOrigin ? "debug origin anchored" : "origin anchored",
                trackingText: Self.trackingText(frame.camera.trackingState),
                centerHitText: forcedCameraOrigin ? "debug forced camera origin" : "center horizontal hit",
                centerDistanceM: hit.distanceM,
                originWorldText: originText,
                anchorVisible: true
            ))
            onOriginMarked(WorldCalibrationARResult(
                timestamp: Date(),
                distanceM: hit.distanceM,
                originWorldText: originText,
                userWorldTransform: userWorldTransform,
                cameraTransform: frame.camera.transform,
                cameraInUserWorldTransform: cameraInUserWorldTransform
            ))
            return true
        }

        private func centerRaycast(in sceneView: ARSCNView) -> (origin: SIMD3<Float>, distanceM: Double)? {
            let center = CGPoint(x: sceneView.bounds.midX, y: sceneView.bounds.midY)
            guard let query = sceneView.raycastQuery(
                from: center,
                allowing: ARRaycastQuery.Target.estimatedPlane,
                alignment: ARRaycastQuery.TargetAlignment.horizontal
            ) else {
                return nil
            }
            let result = sceneView.session.raycast(query).first
            guard let result, let frame = sceneView.session.currentFrame else { return nil }

            let origin = SIMD3<Float>(
                result.worldTransform.columns.3.x,
                result.worldTransform.columns.3.y,
                result.worldTransform.columns.3.z
            )
            let camera = SIMD3<Float>(
                frame.camera.transform.columns.3.x,
                frame.camera.transform.columns.3.y,
                frame.camera.transform.columns.3.z
            )
            return (origin, Double(simd_length(origin - camera)))
        }

        private func makeWorldAxisNode(
            origin: SIMD3<Float>,
            cameraTransform: simd_float4x4
        ) -> SCNNode {
            return makeWorldAxisNode(userWorldTransform: Self.userWorldBasis(origin: origin, cameraTransform: cameraTransform))
        }

        private func makeWorldAxisNode(userWorldTransform: simd_float4x4) -> SCNNode {
            let root = SCNNode()
            root.simdTransform = userWorldTransform

            let xLength: Float = 0.090
            let yLength: Float = 0.075
            let zLength: Float = 0.065
            root.addChildNode(Self.axisNode(
                label: "X",
                from: .zero,
                to: SIMD3<Float>(xLength, 0, 0),
                color: UIColor(red: 0.95, green: 0.18, blue: 0.16, alpha: 1)
            ))
            root.addChildNode(Self.axisNode(
                label: "Y",
                from: .zero,
                to: SIMD3<Float>(0, yLength, 0),
                color: UIColor(red: 0.25, green: 0.85, blue: 0.40, alpha: 1)
            ))
            root.addChildNode(Self.axisNode(
                label: "Z",
                from: .zero,
                to: SIMD3<Float>(0, 0, zLength),
                color: UIColor(red: 0.18, green: 0.45, blue: 1.0, alpha: 1)
            ))

            let base = SCNSphere(radius: 0.006)
            base.firstMaterial?.diffuse.contents = UIColor.white
            let baseNode = SCNNode(geometry: base)
            root.addChildNode(baseNode)
            return root
        }

        private func publishStatus(_ status: WorldCalibrationARStatus) {
            let oldStatus = lastStatus
            lastStatus = status
            if oldStatus.statusText != status.statusText
                || oldStatus.trackingText != status.trackingText
                || oldStatus.centerHitText != status.centerHitText
                || oldStatus.anchorVisible != status.anchorVisible {
                print(
                    "[UMIFT][ar-view] status=\(status.statusText) tracking=\(status.trackingText) hit=\(status.centerHitText) anchor=\(status.anchorVisible) dist=\(status.centerDistanceM.map { String(format: "%.3f", $0) } ?? "--")"
                )
            }
            DispatchQueue.main.async { [onStatusUpdate] in
                onStatusUpdate(status)
            }
        }

        private func publishPrivateUltraWideIfAvailable(from frame: ARFrame) {
            guard let snapshot = Self.privateUltraWideSnapshot(from: frame) else { return }
            guard snapshot.timestampNS != lastPrivateUltraWideTimestampNS else { return }
            lastPrivateUltraWideTimestampNS = snapshot.timestampNS

            let shouldRenderPreview = shouldRenderUltraWidePreview
                && snapshot.timestampNS - lastUltraWidePreviewTimestampNS >= ultraWidePreviewIntervalNS
            if shouldRenderPreview {
                lastUltraWidePreviewTimestampNS = snapshot.timestampNS
            }
            let previewImage = shouldRenderPreview
                ? Self.previewImage(
                    from: snapshot.pixelBuffer,
                    context: ciContext,
                    maxDimension: ultraWidePreviewMaxDimension
                )
                : nil
            let frameState = PrivateUltraWideFrameState(
                pixelBuffer: snapshot.pixelBuffer,
                timestampNS: snapshot.timestampNS,
                frameText: "\(snapshot.width)x\(snapshot.height)",
                cameraText: snapshot.cameraText,
                pixelFormatText: snapshot.pixelFormatText,
                previewImage: previewImage
            )
            DispatchQueue.main.async { [onPrivateUltraWideFrame] in
                onPrivateUltraWideFrame(frameState)
            }
        }

        private struct PrivateUltraWideSnapshot {
            var pixelBuffer: CVPixelBuffer
            var width: Int
            var height: Int
            var pixelFormat: OSType
            var timestampNS: Int64
            var cameraText: String

            var pixelFormatText: String {
                Self.pixelFormatText(pixelFormat)
            }

            private static func pixelFormatText(_ value: OSType) -> String {
                let chars = [
                    Character(UnicodeScalar((value >> 24) & 0xff) ?? " "),
                    Character(UnicodeScalar((value >> 16) & 0xff) ?? " "),
                    Character(UnicodeScalar((value >> 8) & 0xff) ?? " "),
                    Character(UnicodeScalar(value & 0xff) ?? " "),
                ]
                let text = String(chars)
                return text.trimmingCharacters(in: .whitespaces).isEmpty ? "\(value)" : text
            }
        }

        private struct DepthSnapshot {
            var depthMap: CVPixelBuffer
            var source: String
            var width: Int
            var height: Int
            var pixelFormat: OSType
            var centerDepthM: Double?
            var validRatio: Double
        }

        private static func privateUltraWideSnapshot(from frame: ARFrame) -> PrivateUltraWideSnapshot? {
            let imageSelector = NSSelectorFromString("capturedUltraWideImage")
            guard frame.responds(to: imageSelector),
                  let imageIMP = frame.method(for: imageSelector)
            else {
                return nil
            }

            typealias PixelBufferGetter = @convention(c) (AnyObject, Selector) -> CVPixelBuffer?
            let imageGetter = unsafeBitCast(imageIMP, to: PixelBufferGetter.self)
            guard let buffer = imageGetter(frame, imageSelector) else {
                return nil
            }

            let timestamp = privateUltraWideTimestamp(from: frame) ?? frame.timestamp
            return PrivateUltraWideSnapshot(
                pixelBuffer: buffer,
                width: CVPixelBufferGetWidth(buffer),
                height: CVPixelBufferGetHeight(buffer),
                pixelFormat: CVPixelBufferGetPixelFormatType(buffer),
                timestampNS: Int64(timestamp * 1_000_000_000),
                cameraText: privateUltraWideCameraText(from: frame)
            )
        }

        private static func previewImage(
            from pixelBuffer: CVPixelBuffer,
            context: CIContext,
            maxDimension: CGFloat
        ) -> UIImage? {
            let image = CIImage(cvPixelBuffer: pixelBuffer)
            let extent = image.extent
            guard extent.width > 0, extent.height > 0 else { return nil }
            let scale = min(1, maxDimension / max(extent.width, extent.height))
            let output = scale < 1
                ? image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
                : image
            guard let cgImage = context.createCGImage(output, from: output.extent) else {
                return nil
            }
            return UIImage(cgImage: cgImage)
        }

        private static func privateUltraWideTimestamp(from frame: ARFrame) -> TimeInterval? {
            let selector = NSSelectorFromString("ultraWideImageTimestamp")
            guard frame.responds(to: selector),
                  let timestampIMP = frame.method(for: selector)
            else {
                return nil
            }
            typealias TimestampGetter = @convention(c) (AnyObject, Selector) -> Double
            let timestampGetter = unsafeBitCast(timestampIMP, to: TimestampGetter.self)
            return timestampGetter(frame, selector)
        }

        private static func privateUltraWideCameraText(from frame: ARFrame) -> String {
            let selector = NSSelectorFromString("ultraWideCamera")
            guard frame.responds(to: selector),
                  let cameraIMP = frame.method(for: selector)
            else {
                return "camera missing"
            }
            typealias CameraGetter = @convention(c) (AnyObject, Selector) -> AnyObject?
            let cameraGetter = unsafeBitCast(cameraIMP, to: CameraGetter.self)
            guard let camera = cameraGetter(frame, selector) as? ARCamera else {
                return "camera nil"
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

        private static func frameState(
            from frame: ARFrame,
            frameSequence: UInt64,
            userWorldTransform: simd_float4x4?
        ) -> WorldTrackingARFrameState {
            let timestampNS = Int64(frame.timestamp * 1_000_000_000)
            let imageBuffer = frame.capturedImage
            let rgbText = "ar \(CVPixelBufferGetWidth(imageBuffer))x\(CVPixelBufferGetHeight(imageBuffer))"
            let intrinsics = frame.camera.intrinsics
            let intrinsicsText = String(
                format: "fx %.1f fy %.1f cx %.1f cy %.1f",
                intrinsics.columns.0.x,
                intrinsics.columns.1.y,
                intrinsics.columns.2.x,
                intrinsics.columns.2.y
            )

            let cameraInUserWorldTransform = userWorldTransform.map {
                simd_mul(simd_inverse($0), frame.camera.transform)
            }

            let depth = depthSnapshot(from: frame)
            let depthSource = depth?.source ?? "arkit_depth_unavailable"
            let depthValidRatio = depth?.validRatio ?? 0
            let depthConfidenceText = depth.map { String(format: "valid %.0f%%", $0.validRatio * 100) } ?? "empty"
            return WorldTrackingARFrameState(
                frameSequence: frameSequence,
                timestampNS: timestampNS,
                trackingText: trackingText(frame.camera.trackingState),
                cameraTransform: frame.camera.transform,
                cameraInUserWorldTransform: cameraInUserWorldTransform,
                rgbCameraIntrinsics: intrinsics,
                rgbCaptureDeviceType: "builtInWideAngleCamera",
                intrinsicsText: intrinsicsText,
                rgbText: rgbText,
                rgbPixelBuffer: imageBuffer,
                depthAvailable: depth != nil,
                depthSource: depthSource,
                depthMapPixelBuffer: depth?.depthMap,
                depthWidth: depth?.width,
                depthHeight: depth?.height,
                depthUnit: "meter",
                depthPixelFormat: depth?.pixelFormat,
                centerDepthM: depth?.centerDepthM,
                depthValidRatio: depthValidRatio,
                depthConfidenceText: depthConfidenceText
            )
        }

        private static func depthSnapshot(from frame: ARFrame) -> DepthSnapshot? {
            let depthData: ARDepthData
            let source: String
            if let smoothedSceneDepth = frame.smoothedSceneDepth {
                depthData = smoothedSceneDepth
                source = "arkit_smoothed_scene_depth"
            } else if let sceneDepth = frame.sceneDepth {
                depthData = sceneDepth
                source = "arkit_scene_depth"
            } else {
                return nil
            }

            let depthMap = depthData.depthMap
            let stats = depthStats(depthMap: depthMap)
            return DepthSnapshot(
                depthMap: depthMap,
                source: source,
                width: CVPixelBufferGetWidth(depthMap),
                height: CVPixelBufferGetHeight(depthMap),
                pixelFormat: CVPixelBufferGetPixelFormatType(depthMap),
                centerDepthM: stats.centerDepthM,
                validRatio: stats.validRatio
            )
        }

        private static func preferredWideVideoFormat() -> ARConfiguration.VideoFormat? {
            ARWorldTrackingConfiguration.supportedVideoFormats.first {
                Int($0.imageResolution.width.rounded()) == 1920
                    && Int($0.imageResolution.height.rounded()) == 1440
                    && $0.framesPerSecond == 30
            }
        }

        private static func depthStats(depthMap: CVPixelBuffer) -> (centerDepthM: Double?, validRatio: Double) {
            CVPixelBufferLockBaseAddress(depthMap, .readOnly)
            defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }

            let width = CVPixelBufferGetWidth(depthMap)
            let height = CVPixelBufferGetHeight(depthMap)
            guard width > 0,
                  height > 0,
                  let baseAddress = CVPixelBufferGetBaseAddress(depthMap)
            else {
                return (nil, 0)
            }

            let values = baseAddress.assumingMemoryBound(to: Float32.self)
            let stride = CVPixelBufferGetBytesPerRow(depthMap) / MemoryLayout<Float32>.stride
            let centerX = width / 2
            let centerY = height / 2
            var centerValues: [Double] = []
            for y in max(0, centerY - 2)...min(height - 1, centerY + 2) {
                for x in max(0, centerX - 2)...min(width - 1, centerX + 2) {
                    let value = values[y * stride + x]
                    if value.isFinite, value > 0.05, value < 8.0 {
                        centerValues.append(Double(value))
                    }
                }
            }

            var valid = 0
            var total = 0
            let step = max(1, min(width, height) / 48)
            var y = 0
            while y < height {
                var x = 0
                while x < width {
                    let value = values[y * stride + x]
                    if value.isFinite, value > 0.05, value < 8.0 {
                        valid += 1
                    }
                    total += 1
                    x += step
                }
                y += step
            }

            let centerDepth = centerValues.isEmpty
                ? nil
                : centerValues.reduce(0, +) / Double(centerValues.count)
            return (centerDepth, total > 0 ? Double(valid) / Double(total) : 0)
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
                    return "limited motion"
                case .insufficientFeatures:
                    return "limited features"
                case .initializing:
                    return "initializing"
                case .relocalizing:
                    return "relocalizing"
                @unknown default:
                    return "limited"
                }
            }
        }

        private static func userWorldBasis(
            origin: SIMD3<Float>,
            cameraTransform: simd_float4x4
        ) -> simd_float4x4 {
            let up = SIMD3<Float>(0, 1, 0)
            var forward = -SIMD3<Float>(
                cameraTransform.columns.2.x,
                cameraTransform.columns.2.y,
                cameraTransform.columns.2.z
            )
            forward.y = 0
            if simd_length(forward) < 0.001 {
                forward = SIMD3<Float>(1, 0, 0)
            } else {
                forward = simd_normalize(forward)
            }

            var right = simd_cross(up, forward)
            if simd_length(right) < 0.001 {
                right = SIMD3<Float>(0, 0, 1)
            } else {
                right = simd_normalize(right)
            }

            var transform = matrix_identity_float4x4
            transform.columns.0 = SIMD4<Float>(forward.x, forward.y, forward.z, 0)
            transform.columns.1 = SIMD4<Float>(right.x, right.y, right.z, 0)
            transform.columns.2 = SIMD4<Float>(up.x, up.y, up.z, 0)
            transform.columns.3 = SIMD4<Float>(origin.x, origin.y, origin.z, 1)
            return transform
        }

        private static func axisNode(
            label: String,
            from start: SIMD3<Float>,
            to end: SIMD3<Float>,
            color: UIColor
        ) -> SCNNode {
            let root = SCNNode()
            let line = cylinderNode(from: start, to: end, radius: 0.0025, color: color)
            let head = coneNode(at: end, direction: end - start, color: color)
            let labelNode = labelNode(label, at: end, color: color)
            root.addChildNode(line)
            root.addChildNode(head)
            root.addChildNode(labelNode)
            return root
        }

        private static func cylinderNode(
            from start: SIMD3<Float>,
            to end: SIMD3<Float>,
            radius: CGFloat,
            color: UIColor
        ) -> SCNNode {
            let vector = end - start
            let length = max(0.001, simd_length(vector))
            let geometry = SCNCylinder(radius: radius, height: CGFloat(length))
            geometry.radialSegmentCount = 12
            geometry.firstMaterial?.diffuse.contents = color
            geometry.firstMaterial?.emission.contents = color.withAlphaComponent(0.24)
            let node = SCNNode(geometry: geometry)
            node.simdPosition = (start + end) * 0.5
            node.simdOrientation = simd_quatf(from: SIMD3<Float>(0, 1, 0), to: simd_normalize(vector))
            return node
        }

        private static func coneNode(
            at end: SIMD3<Float>,
            direction: SIMD3<Float>,
            color: UIColor
        ) -> SCNNode {
            let geometry = SCNCone(topRadius: 0, bottomRadius: 0.006, height: 0.018)
            geometry.radialSegmentCount = 16
            geometry.firstMaterial?.diffuse.contents = color
            geometry.firstMaterial?.emission.contents = color.withAlphaComponent(0.20)
            let node = SCNNode(geometry: geometry)
            let unit = simd_normalize(direction)
            node.simdPosition = end + unit * 0.008
            node.simdOrientation = simd_quatf(from: SIMD3<Float>(0, 1, 0), to: unit)
            return node
        }

        private static func labelNode(_ text: String, at position: SIMD3<Float>, color: UIColor) -> SCNNode {
            let geometry = SCNText(string: text, extrusionDepth: 0.0008)
            geometry.font = .boldSystemFont(ofSize: 0.055)
            geometry.flatness = 0.2
            geometry.firstMaterial?.diffuse.contents = color
            geometry.firstMaterial?.emission.contents = color.withAlphaComponent(0.35)

            let node = SCNNode(geometry: geometry)
            node.simdScale = SIMD3<Float>(0.22, 0.22, 0.22)
            node.simdPosition = position + simd_normalize(position == .zero ? SIMD3<Float>(0, 0, 1) : position) * 0.018
            node.constraints = [SCNBillboardConstraint()]
            return node
        }

        private static func originText(origin: SIMD3<Float>, cameraTransform: simd_float4x4) -> String {
            let basis = userWorldBasis(origin: origin, cameraTransform: cameraTransform)
            let x = SIMD3<Float>(basis.columns.0.x, basis.columns.0.y, basis.columns.0.z)
            return String(
                format: "o %.3f %.3f %.3f x %.2f %.2f %.2f",
                origin.x,
                origin.y,
                origin.z,
                x.x,
                x.y,
                x.z
            )
        }
    }
}

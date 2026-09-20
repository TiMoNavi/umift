import SwiftUI
import UIKit

struct PoseCheckView: View {
    @StateObject private var model = PoseCheckModel()

    var body: some View {
        ZStack(alignment: .topTrailing) {
            UltraWidePoseARView(model: model)
                .ignoresSafeArea()

            HStack(alignment: .top, spacing: 0) {
                ultraWidePanel
                    .frame(width: 390)
                    .padding(.top, 14)
                    .padding(.leading, 14)
                Spacer(minLength: 0)
                statusPanel
                    .frame(width: 380)
                    .padding(.top, 14)
                    .padding(.trailing, 14)
            }

            VStack {
                Spacer()
                bottomStrip
                    .padding(.horizontal, 18)
                    .padding(.bottom, 14)
            }
        }
        .background(Color.black)
    }

    private var statusPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Circle()
                    .fill(model.usesUltraWide ? Color.green : Color.red)
                    .frame(width: 10, height: 10)
                Text("ARKit UltraWide 6DoF")
                    .font(.system(size: 18, weight: .semibold, design: .rounded))
                Spacer()
            }

            Divider().background(Color.white.opacity(0.18))

            metric("status", model.statusText)
            metric("tracking", model.trackingText)
            metric("format", model.selectedFormatText)
            metric("frame", model.frameText)
            metric("fps", model.fpsText)
            metric("intrinsics", model.intrinsicsText)
            metric("position", model.positionText)
            metric("euler", model.eulerText)
            metric("velocity", model.velocityText)
            metric("timestamp", model.timestampText)
            metric("private uw", model.privateUltraWideText)
            metric("gripper", "\(model.gripperVisionState.statusText) \(model.gripperVisionState.idsText)")
            metric("opening", "\(model.gripperVisionState.openingText) conf \(model.gripperVisionState.confidenceText)")

            if !model.availableFormatsText.isEmpty {
                Divider().background(Color.white.opacity(0.18))
                Text("available AR formats")
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.72))
                Text(model.availableFormatsText)
                    .font(.system(size: 10.5, weight: .regular, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.62))
                    .lineLimit(10)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !model.arkitDiagnosticsText.isEmpty {
                Divider().background(Color.white.opacity(0.18))
                Text("arkit camera diagnostics")
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.72))
                Text(model.arkitDiagnosticsText)
                    .font(.system(size: 10.5, weight: .regular, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.62))
                    .lineLimit(12)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .background(.black.opacity(0.62))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(model.usesUltraWide ? Color.green.opacity(0.48) : Color.red.opacity(0.55), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var ultraWidePanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle()
                    .fill(model.ultraWidePreviewImage == nil ? Color.red : Color.green)
                    .frame(width: 9, height: 9)
                Text("private ultrawide")
                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)
                Spacer()
                Text(model.gripperVisionState.frameText)
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.70))
            }

            ZStack {
                Rectangle()
                    .fill(Color.black)
                if let image = model.ultraWidePreviewImage {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                } else {
                    Text("waiting for 640x480 side stream")
                        .font(.system(size: 12, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.55))
                }
                ARUWGripperOverlayLayer(
                    markers: model.gripperVisionState.overlays,
                    frameSize: model.gripperVisionState.frameSize
                )
            }
            .aspectRatio(4.0 / 3.0, contentMode: .fit)
            .overlay(
                Rectangle()
                    .stroke(Color.white.opacity(0.18), lineWidth: 1)
            )
            .clipped()

            VStack(alignment: .leading, spacing: 4) {
                metric("uw state", model.gripperVisionState.statusText)
                metric("ids", model.gripperVisionState.idsText)
                metric("roi/input", "\(model.gripperVisionState.roiText) \(model.gripperVisionState.inputText)")
                metric("left", model.gripperVisionState.leftMarker.detected ? model.gripperVisionState.leftMarker.centerText : "--")
                metric("right", model.gripperVisionState.rightMarker.detected ? model.gripperVisionState.rightMarker.centerText : "--")
            }
        }
        .padding(12)
        .background(.black.opacity(0.64))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.white.opacity(0.18), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var bottomStrip: some View {
        HStack(spacing: 14) {
            Label(model.usesUltraWide ? "ultrawide format selected" : "ultrawide format unavailable", systemImage: model.usesUltraWide ? "checkmark.circle.fill" : "xmark.octagon.fill")
                .foregroundStyle(model.usesUltraWide ? Color.green : Color.red)
            Text(model.compactPoseText)
                .font(.system(size: 14, weight: .medium, design: .monospaced))
                .foregroundStyle(.white.opacity(0.86))
            Spacer()
        }
        .font(.system(size: 14, weight: .semibold, design: .rounded))
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.black.opacity(0.56))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.white.opacity(0.16), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func metric(_ name: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(name)
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.46))
            Text(value)
                .font(.system(size: 12.5, weight: .medium, design: .monospaced))
                .foregroundStyle(.white.opacity(0.88))
                .lineLimit(2)
                .minimumScaleFactor(0.82)
        }
    }
}

final class PoseCheckModel: ObservableObject {
    @Published var statusText = "starting"
    @Published var trackingText = "--"
    @Published var selectedFormatText = "--"
    @Published var availableFormatsText = ""
    @Published var frameText = "--"
    @Published var fpsText = "--"
    @Published var intrinsicsText = "--"
    @Published var positionText = "x -- y -- z --"
    @Published var eulerText = "r -- p -- y --"
    @Published var velocityText = "v --"
    @Published var timestampText = "--"
    @Published var compactPoseText = "pose waiting"
    @Published var usesUltraWide = false
    @Published var arkitDiagnosticsText = ""
    @Published var privateUltraWideText = "not observed"
    @Published var gripperVisionState = ARUWGripperVisionState.empty
    @Published var ultraWidePreviewImage: UIImage?

    func publishConfiguration(
        status: String,
        selectedFormat: String,
        availableFormats: String,
        usesUltraWide: Bool
    ) {
        statusText = status
        selectedFormatText = selectedFormat
        availableFormatsText = availableFormats
        self.usesUltraWide = usesUltraWide
    }

    func publishDiagnostics(_ diagnostics: String) {
        arkitDiagnosticsText = diagnostics
    }

    func publishFrame(_ frame: UltraWidePoseFrame) {
        statusText = frame.statusText
        trackingText = frame.trackingText
        frameText = frame.frameText
        fpsText = frame.fpsText
        intrinsicsText = frame.intrinsicsText
        positionText = frame.positionText
        eulerText = frame.eulerText
        velocityText = frame.velocityText
        timestampText = frame.timestampText
        compactPoseText = frame.compactPoseText
    }

    func publishPrivateUltraWide(
        text: String,
        gripper: ARUWGripperVisionState,
        previewImage: UIImage?
    ) {
        privateUltraWideText = text
        gripperVisionState = gripper
        if let previewImage {
            ultraWidePreviewImage = previewImage
        }
    }
}

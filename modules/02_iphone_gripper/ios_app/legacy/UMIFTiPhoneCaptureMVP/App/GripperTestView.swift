import SwiftUI

struct GripperTestView: View {
    @StateObject private var manager = GripperTestManager()

    var body: some View {
        GeometryReader { geometry in
            let leftWidth = min(max(330, geometry.size.width * 0.44), 470)

            HStack(spacing: 0) {
                leftControlPane
                    .frame(width: leftWidth)
                    .frame(maxHeight: .infinity)

                previewPane
                    .frame(width: max(0, geometry.size.width - leftWidth))
                    .frame(maxHeight: .infinity)
            }
            .background(Color.black)
            .ignoresSafeArea()
        }
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .onAppear {
            manager.start()
        }
        .onDisappear {
            manager.stop()
        }
    }

    private var leftControlPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                diagnosticPanel
                calibrationPanel
            }
            .padding(12)
        }
        .background(Color.black.opacity(0.90))
    }

    private var previewPane: some View {
        GeometryReader { geometry in
            ZStack {
                Color.black

                ZStack(alignment: .bottomTrailing) {
                    UltraWidePreviewView(
                        session: manager.session,
                        videoOrientation: .landscapeRight
                    )

                    MarkerOverlayLayer(
                        markers: manager.markerOverlays,
                        frameSize: manager.videoFrameSize
                    )
                    .allowsHitTesting(false)

                    transparentRecordButton
                        .padding(18)
                }
                .aspectRatio(4.0 / 3.0, contentMode: .fit)
                .frame(width: geometry.size.width, height: geometry.size.height)
            }
        }
    }

    private var diagnosticPanel: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Circle()
                    .fill(manager.isRunning ? Color.green : Color.orange)
                    .frame(width: 9, height: 9)
                Text(manager.statusText)
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
            }

            HStack(spacing: 12) {
                metricRow("open", manager.openingText)
                metricRow("conf", manager.confidenceText)
                metricRow("ids", manager.detectedIDsText)
            }
            metricRow("id0", manager.leftMarkerText)
            metricRow("id1", manager.rightMarkerText)
            metricRow("path", manager.pathText)
            metricRow("torch", manager.torchText)
            metricRow("format", manager.formatText)
            metricRow("frame", manager.frameText)
        }
        .fontDesign(.monospaced)
        .foregroundStyle(.white)
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.black.opacity(0.66), in: RoundedRectangle(cornerRadius: 8))
    }

    private var calibrationPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(manager.isSweepCalibrating ? "GRIPPER CALIBRATION RECORDING" : "GRIPPER CALIBRATION")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(manager.isSweepCalibrating ? .yellow : .white)
                Text(manager.sweepProgressText)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.white)
                    .lineLimit(2)
                    .minimumScaleFactor(0.70)
                Text(manager.calibrationText)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.78))
                    .lineLimit(2)
                    .minimumScaleFactor(0.65)
            }

            LazyVGrid(
                columns: [
                    GridItem(.flexible(), spacing: 8),
                    GridItem(.flexible(), spacing: 8)
                ],
                spacing: 8
            ) {
                Button {
                    manager.startSweepCalibration()
                } label: {
                    Text("Start Cal")
                        .frame(maxWidth: .infinity, minHeight: 42)
                }
                .buttonStyle(.borderedProminent)
                .disabled(manager.isSweepCalibrating)

                Button {
                    manager.finishSweepCalibration()
                } label: {
                    Text("Finish")
                        .frame(maxWidth: .infinity, minHeight: 42)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!manager.isSweepCalibrating)

                Button {
                    manager.cancelSweepCalibration()
                } label: {
                    Text("Cancel")
                        .frame(maxWidth: .infinity, minHeight: 42)
                }
                .buttonStyle(.bordered)

                Button {
                    manager.resetRange()
                } label: {
                    Text("Reset")
                        .frame(maxWidth: .infinity, minHeight: 42)
                }
                .buttonStyle(.bordered)
            }
        }
        .fontDesign(.monospaced)
        .foregroundStyle(.white)
        .padding(10)
        .background(.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 8))
    }

    private var transparentRecordButton: some View {
        Button {
            if manager.isSweepCalibrating {
                manager.finishSweepCalibration()
            } else {
                manager.startSweepCalibration()
            }
        } label: {
            ZStack {
                Circle()
                    .stroke(manager.isSweepCalibrating ? Color.yellow : Color.white.opacity(0.92), lineWidth: 2)
                Circle()
                    .stroke(Color.black.opacity(0.35), lineWidth: 6)
                    .padding(6)
                Image(systemName: manager.isSweepCalibrating ? "checkmark" : "record.circle")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(manager.isSweepCalibrating ? Color.yellow : Color.white.opacity(0.92))
            }
            .frame(width: 86, height: 86)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("transparent_record_button")
    }

    private func metricRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .foregroundStyle(.white.opacity(0.72))
                .frame(width: 54, alignment: .leading)
            Text(value)
                .lineLimit(1)
                .minimumScaleFactor(0.65)
        }
        .font(.caption)
    }
}

private struct MarkerOverlayLayer: View {
    let markers: [MarkerOverlay]
    let frameSize: CGSize

    var body: some View {
        GeometryReader { geometry in
            let transform = PreviewCoordinateTransform(
                frameSize: frameSize,
                viewSize: geometry.size
            )

            ZStack(alignment: .topLeading) {
                ForEach(markers) { marker in
                    MarkerShape(points: marker.corners.map(transform.map))
                        .stroke(color(for: marker), style: strokeStyle(for: marker))
                        .opacity(opacity(for: marker))

                    if marker.kind != .rejected {
                        CenterCross(center: transform.map(marker.center))
                            .stroke(color(for: marker), lineWidth: marker.kind == .held ? 1.5 : 2)
                            .opacity(opacity(for: marker))
                    }

                    Text(label(for: marker))
                        .font(.caption2.monospacedDigit().weight(.bold))
                        .foregroundStyle(.black)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(color(for: marker).opacity(marker.kind == .held ? 0.72 : 1.0), in: RoundedRectangle(cornerRadius: 4))
                        .opacity(opacity(for: marker))
                        .position(labelPosition(for: marker, transform: transform))
                }
            }
        }
    }

    private func color(for marker: MarkerOverlay) -> Color {
        switch marker.kind {
        case .rejected:
            return .white.opacity(0.62)
        case .held:
            return marker.markerID == 1 ? .yellow : .cyan
        case .pathCandidate:
            return marker.markerID == 1 ? .yellow : .cyan
        case .detected:
            switch marker.markerID {
            case 0:
                return .cyan
            case 1:
                return .yellow
            default:
                return .orange
            }
        }
    }

    private func strokeStyle(for marker: MarkerOverlay) -> StrokeStyle {
        switch marker.kind {
        case .detected:
            return StrokeStyle(lineWidth: 3, lineJoin: .round)
        case .pathCandidate:
            return StrokeStyle(lineWidth: 3, lineJoin: .round, dash: [8, 5])
        case .rejected:
            return StrokeStyle(lineWidth: 1.5, lineJoin: .round, dash: [4, 5])
        case .held:
            return StrokeStyle(lineWidth: 2, lineJoin: .round, dash: [9, 6])
        }
    }

    private func opacity(for marker: MarkerOverlay) -> Double {
        switch marker.kind {
        case .detected:
            return 1.0
        case .pathCandidate:
            return 0.88
        case .rejected:
            return 0.58
        case .held:
            return max(0.25, 0.72 - marker.age * 0.55)
        }
    }

    private func label(for marker: MarkerOverlay) -> String {
        switch marker.kind {
        case .detected:
            return marker.markerID.map { "id \($0)" } ?? "id ?"
        case .pathCandidate:
            return marker.markerID.map { "id \($0) ?" } ?? "path ?"
        case .rejected:
            return "?"
        case .held:
            return marker.markerID.map { "id \($0) hold" } ?? "hold"
        }
    }

    private func labelPosition(for marker: MarkerOverlay, transform: PreviewCoordinateTransform) -> CGPoint {
        let points = marker.corners.map(transform.map)
        guard let first = points.first else { return transform.map(marker.center) }
        let topLeft = points.reduce(first) { best, point in
            (point.x + point.y) < (best.x + best.y) ? point : best
        }
        return CGPoint(x: topLeft.x, y: max(12, topLeft.y - 14))
    }
}

private struct MarkerShape: Shape {
    let points: [CGPoint]

    func path(in rect: CGRect) -> Path {
        var path = Path()
        guard let first = points.first else { return path }
        path.move(to: first)
        for point in points.dropFirst() {
            path.addLine(to: point)
        }
        path.closeSubpath()
        return path
    }
}

private struct CenterCross: Shape {
    let center: CGPoint

    func path(in rect: CGRect) -> Path {
        var path = Path()
        let radius: CGFloat = 7
        path.move(to: CGPoint(x: center.x - radius, y: center.y))
        path.addLine(to: CGPoint(x: center.x + radius, y: center.y))
        path.move(to: CGPoint(x: center.x, y: center.y - radius))
        path.addLine(to: CGPoint(x: center.x, y: center.y + radius))
        return path
    }
}

private struct PreviewCoordinateTransform {
    let frameSize: CGSize
    let viewSize: CGSize

    func map(_ point: CGPoint) -> CGPoint {
        guard frameSize.width > 0, frameSize.height > 0, viewSize.width > 0, viewSize.height > 0 else {
            return .zero
        }
        let scale = min(viewSize.width / frameSize.width, viewSize.height / frameSize.height)
        let fittedWidth = frameSize.width * scale
        let fittedHeight = frameSize.height * scale
        let xOffset = (viewSize.width - fittedWidth) * 0.5
        let yOffset = (viewSize.height - fittedHeight) * 0.5
        return CGPoint(
            x: xOffset + point.x * scale,
            y: yOffset + point.y * scale
        )
    }
}

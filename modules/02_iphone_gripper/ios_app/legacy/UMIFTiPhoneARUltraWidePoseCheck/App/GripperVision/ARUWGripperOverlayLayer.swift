import SwiftUI

struct ARUWGripperOverlayLayer: View {
    let markers: [ARUWMarkerOverlay]
    let frameSize: CGSize

    var body: some View {
        GeometryReader { geometry in
            let transform = ARUWPreviewCoordinateTransform(
                frameSize: frameSize,
                viewSize: geometry.size
            )

            ZStack(alignment: .topLeading) {
                ForEach(markers) { marker in
                    ARUWMarkerShape(points: marker.corners.map(transform.map))
                        .stroke(color(for: marker), style: strokeStyle(for: marker))
                        .opacity(opacity(for: marker))

                    if marker.kind == .detected {
                        ARUWCenterCross(center: transform.map(marker.center))
                            .stroke(color(for: marker), lineWidth: 2)
                    }

                    Text(label(for: marker))
                        .font(.caption2.monospacedDigit().weight(.bold))
                        .foregroundStyle(.black)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(color(for: marker).opacity(0.92), in: RoundedRectangle(cornerRadius: 4))
                        .position(labelPosition(for: marker, transform: transform))
                }
            }
        }
    }

    private func color(for marker: ARUWMarkerOverlay) -> Color {
        switch marker.kind {
        case .rejected:
            return .white.opacity(0.62)
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

    private func strokeStyle(for marker: ARUWMarkerOverlay) -> StrokeStyle {
        switch marker.kind {
        case .detected:
            return StrokeStyle(lineWidth: 3, lineJoin: .round)
        case .rejected:
            return StrokeStyle(lineWidth: 1.3, lineJoin: .round, dash: [4, 5])
        }
    }

    private func opacity(for marker: ARUWMarkerOverlay) -> Double {
        switch marker.kind {
        case .detected:
            return 1.0
        case .rejected:
            return 0.48
        }
    }

    private func label(for marker: ARUWMarkerOverlay) -> String {
        switch marker.kind {
        case .detected:
            return marker.markerID.map { "id \($0)" } ?? "id ?"
        case .rejected:
            return "?"
        }
    }

    private func labelPosition(for marker: ARUWMarkerOverlay, transform: ARUWPreviewCoordinateTransform) -> CGPoint {
        let points = marker.corners.map(transform.map)
        guard let first = points.first else { return transform.map(marker.center) }
        let topLeft = points.reduce(first) { best, point in
            (point.x + point.y) < (best.x + best.y) ? point : best
        }
        return CGPoint(x: topLeft.x, y: max(12, topLeft.y - 14))
    }
}

private struct ARUWMarkerShape: Shape {
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

private struct ARUWCenterCross: Shape {
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

private struct ARUWPreviewCoordinateTransform {
    let frameSize: CGSize
    let viewSize: CGSize

    func map(_ point: CGPoint) -> CGPoint {
        guard frameSize.width > 0, frameSize.height > 0, viewSize.width > 0, viewSize.height > 0 else {
            return .zero
        }
        let scale = min(viewSize.width / frameSize.width, viewSize.height / frameSize.height)
        let fittedWidth = frameSize.width * scale
        let fittedHeight = frameSize.height * scale
        return CGPoint(
            x: (viewSize.width - fittedWidth) * 0.5 + point.x * scale,
            y: (viewSize.height - fittedHeight) * 0.5 + point.y * scale
        )
    }
}

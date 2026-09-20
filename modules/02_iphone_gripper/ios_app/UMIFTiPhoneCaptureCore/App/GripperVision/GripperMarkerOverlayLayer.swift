import SwiftUI

struct GripperMarkerOverlayLayer: View {
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
        case .held, .pathCandidate:
            return marker.markerID == 1 ? AppPalette.yellow : .cyan
        case .detected:
            switch marker.markerID {
            case 0:
                return .cyan
            case 1:
                return AppPalette.yellow
            default:
                return AppPalette.orange
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
        return CGPoint(
            x: (viewSize.width - fittedWidth) * 0.5 + point.x * scale,
            y: (viewSize.height - fittedHeight) * 0.5 + point.y * scale
        )
    }
}

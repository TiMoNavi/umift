import SwiftUI

enum AppPalette {
    static let appBackground = Color(red: 0.04, green: 0.045, blue: 0.052)
    static let previewBackground = Color(red: 0.03, green: 0.035, blue: 0.04)
    static let panelBackground = Color(red: 0.08, green: 0.085, blue: 0.092)
    static let line = Color.white.opacity(0.15)
    static let green = Color(red: 0.24, green: 0.78, blue: 0.55)
    static let blue = Color(red: 0.28, green: 0.58, blue: 0.95)
    static let orange = Color(red: 0.96, green: 0.62, blue: 0.27)
    static let red = Color(red: 0.93, green: 0.32, blue: 0.32)
    static let yellow = Color(red: 0.98, green: 0.82, blue: 0.30)
    static let magenta = Color(red: 0.88, green: 0.46, blue: 0.88)
}

struct PhaseBadge: View {
    let phase: CapturePhase

    var body: some View {
        Text(phase.title.uppercased())
            .font(.system(size: 12, weight: .semibold, design: .monospaced))
            .foregroundStyle(.white)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(badgeColor.opacity(0.22))
            .overlay(
                Capsule()
                    .stroke(badgeColor.opacity(0.85), lineWidth: 1)
            )
            .clipShape(Capsule())
    }

    private var badgeColor: Color {
        switch phase {
        case .recording:
            return AppPalette.red
        case .previewCalibrated:
            return AppPalette.green
        case .calibrationWorld, .calibrationGripper, .startCountdown:
            return AppPalette.orange
        case .previewUncalibrated:
            return AppPalette.blue
        }
    }
}

struct ControlButtonStyle: ButtonStyle {
    var tint: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 14, weight: .semibold))
            .foregroundStyle(.white)
            .lineLimit(1)
            .minimumScaleFactor(0.82)
            .padding(.vertical, 12)
            .padding(.horizontal, 10)
            .background(tint.opacity(configuration.isPressed ? 0.38 : 0.22))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(tint.opacity(configuration.isPressed ? 1 : 0.82), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .opacity(configuration.isPressed ? 0.82 : 1)
    }
}

struct ParameterSection<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .foregroundStyle(AppPalette.green)

            VStack(alignment: .leading, spacing: 7) {
                content
            }
            .padding(10)
            .background(Color.white.opacity(0.045))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(AppPalette.line, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }
}

struct StatusRow: View {
    private let name: String
    private let value: String

    init(_ name: String, _ value: String) {
        self.name = name
        self.value = value
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(name)
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .foregroundStyle(.white.opacity(0.55))
                .frame(width: 112, alignment: .leading)
            Text(value)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.9))
                .lineLimit(2)
                .minimumScaleFactor(0.75)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct MarkerStateView: View {
    let title: String
    let marker: MarkerDebugState

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            StatusRow(title, "id\(marker.markerID) \(marker.trackingState)")
            StatusRow("  confidence", String(format: "%.2f", marker.confidence))
            StatusRow("  center_px", marker.centerPX)
            StatusRow("  corners_px", marker.cornersPX)
            StatusRow("  angle_deg", String(format: "%.1f", marker.angleDeg))
            StatusRow("  path_position", marker.pathPosition.map { String(format: "%.3f", $0) } ?? "--")
        }
    }
}

struct CountdownOverlay: View {
    let value: Int

    var body: some View {
        ZStack {
            Color.black.opacity(0.34)
            Text("\(value)")
                .font(.system(size: 88, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
                .monospacedDigit()
        }
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .padding(.horizontal, 14)
    }
}

struct MarkerBox: View {
    let color: Color

    var body: some View {
        Rectangle()
            .stroke(color, style: StrokeStyle(lineWidth: 3, lineJoin: .round))
            .overlay(alignment: .topLeading) {
                Circle()
                    .fill(color)
                    .frame(width: 7, height: 7)
                    .offset(x: -3, y: -3)
            }
    }
}

struct PreviewGrid: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        for index in 1..<3 {
            let x = rect.minX + rect.width * CGFloat(index) / 3
            path.move(to: CGPoint(x: x, y: rect.minY))
            path.addLine(to: CGPoint(x: x, y: rect.maxY))
        }
        for index in 1..<3 {
            let y = rect.minY + rect.height * CGFloat(index) / 3
            path.move(to: CGPoint(x: rect.minX, y: y))
            path.addLine(to: CGPoint(x: rect.maxX, y: y))
        }
        return path
    }
}

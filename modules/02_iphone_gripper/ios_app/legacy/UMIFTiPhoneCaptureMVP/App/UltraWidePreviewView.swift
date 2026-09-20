@preconcurrency import AVFoundation
import SwiftUI

final class PreviewHostView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }

    var previewLayer: AVCaptureVideoPreviewLayer {
        layer as! AVCaptureVideoPreviewLayer
    }

    var videoOrientation: AVCaptureVideoOrientation = .landscapeRight {
        didSet {
            applyVideoOrientation()
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        applyVideoOrientation()
    }

    func applyVideoOrientation() {
        guard let connection = previewLayer.connection else { return }
        if connection.isVideoOrientationSupported {
            connection.videoOrientation = videoOrientation
        }
    }
}

struct UltraWidePreviewView: UIViewRepresentable {
    let session: AVCaptureSession
    var videoOrientation: AVCaptureVideoOrientation = .landscapeRight

    func makeUIView(context: Context) -> PreviewHostView {
        let view = PreviewHostView(frame: .zero)
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspect
        view.videoOrientation = videoOrientation
        view.applyVideoOrientation()
        return view
    }

    func updateUIView(_ uiView: PreviewHostView, context: Context) {
        if uiView.previewLayer.session !== session {
            uiView.previewLayer.session = session
        }
        uiView.videoOrientation = videoOrientation
        uiView.applyVideoOrientation()
    }
}

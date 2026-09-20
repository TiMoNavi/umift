@preconcurrency import AVFoundation
import SwiftUI

final class DualCameraPreviewHostView: UIView {
    private(set) var wideLayer: AVCaptureVideoPreviewLayer?
    private(set) var ultraLayer: AVCaptureVideoPreviewLayer?

    var videoOrientation: AVCaptureVideoOrientation = .landscapeRight {
        didSet {
            applyVideoOrientation()
        }
    }

    func configure(session: AVCaptureMultiCamSession) {
        guard wideLayer == nil, ultraLayer == nil else { return }

        let wideLayer = AVCaptureVideoPreviewLayer(sessionWithNoConnection: session)
        let ultraLayer = AVCaptureVideoPreviewLayer(sessionWithNoConnection: session)
        [wideLayer, ultraLayer].forEach {
            $0.videoGravity = .resizeAspect
            layer.addSublayer($0)
        }
        self.wideLayer = wideLayer
        self.ultraLayer = ultraLayer
        setVisibleSource(.wide)
        applyVideoOrientation()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        layoutPreviewLayer(wideLayer)
        layoutPreviewLayer(ultraLayer)
        applyVideoOrientation()
    }

    func setVisibleSource(_ source: PreviewSource) {
        wideLayer?.isHidden = source != .wide
        ultraLayer?.isHidden = source != .ultra
    }

    private func layoutPreviewLayer(_ previewLayer: AVCaptureVideoPreviewLayer?) {
        guard let previewLayer else { return }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        previewLayer.frame = bounds
        previewLayer.setAffineTransform(.identity)
        CATransaction.commit()
    }

    func applyVideoOrientation() {
        for previewLayer in [wideLayer, ultraLayer] {
            guard let connection = previewLayer?.connection, connection.isVideoOrientationSupported else { continue }
            connection.videoOrientation = videoOrientation
        }
    }
}

struct DualCameraPreviewView: UIViewRepresentable {
    @EnvironmentObject private var camera: DualCameraCaptureManager
    let source: PreviewSource
    var videoOrientation: AVCaptureVideoOrientation = .landscapeRight

    func makeUIView(context: Context) -> DualCameraPreviewHostView {
        let view = DualCameraPreviewHostView()
        view.backgroundColor = .black
        view.configure(session: camera.session)
        if let wideLayer = view.wideLayer, let ultraLayer = view.ultraLayer {
            camera.attachPreviewLayers(wideLayer: wideLayer, ultraLayer: ultraLayer)
        }
        view.setVisibleSource(source)
        view.videoOrientation = videoOrientation
        view.applyVideoOrientation()
        return view
    }

    func updateUIView(_ uiView: DualCameraPreviewHostView, context: Context) {
        uiView.setVisibleSource(source)
        uiView.videoOrientation = videoOrientation
        if let wideLayer = uiView.wideLayer, let ultraLayer = uiView.ultraLayer {
            camera.attachPreviewLayers(wideLayer: wideLayer, ultraLayer: ultraLayer)
        }
        uiView.applyVideoOrientation()
    }
}

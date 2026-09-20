import SwiftUI
import UIKit

final class LandscapeOnlyAppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        supportedInterfaceOrientationsFor window: UIWindow?
    ) -> UIInterfaceOrientationMask {
        .landscapeRight
    }
}

enum LandscapeOrientationLock {
    @MainActor
    static func apply() {
        guard let windowScene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first(where: { $0.activationState == .foregroundActive || $0.activationState == .foregroundInactive })
        else { return }

        windowScene.requestGeometryUpdate(.iOS(interfaceOrientations: .landscapeRight)) { error in
            print("[orientation] landscape request failed: \(error.localizedDescription)")
        }
        UIViewController.attemptRotationToDeviceOrientation()
    }
}

@main
struct UMIFTiPhoneCaptureCoreApp: App {
    @UIApplicationDelegateAdaptor(LandscapeOnlyAppDelegate.self) private var appDelegate
    @StateObject private var appState = CaptureAppState()
    @StateObject private var camera = DualCameraCaptureManager()
    @StateObject private var arCapture = ARCaptureModel()
    @StateObject private var motion = DeviceMotionPoseProvider()

    var body: some Scene {
        WindowGroup {
            CaptureDashboardView()
                .environmentObject(appState)
                .environmentObject(camera)
                .environmentObject(arCapture)
                .environmentObject(motion)
                .preferredColorScheme(.dark)
                .statusBarHidden(true)
                .persistentSystemOverlays(.hidden)
                .onAppear {
                    LandscapeOrientationLock.apply()
                }
        }
    }
}

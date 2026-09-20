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
            print("[pose-check][orientation] landscape request failed: \(error.localizedDescription)")
        }
        UIViewController.attemptRotationToDeviceOrientation()
    }
}

@main
struct UMIFTiPhoneARUltraWidePoseCheckApp: App {
    @UIApplicationDelegateAdaptor(LandscapeOnlyAppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            PoseCheckView()
                .preferredColorScheme(.dark)
                .statusBarHidden(true)
                .persistentSystemOverlays(.hidden)
                .onAppear {
                    LandscapeOrientationLock.apply()
                }
        }
    }
}

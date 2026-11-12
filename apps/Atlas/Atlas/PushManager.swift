// Asks for notification permission, registers with APNs, and hands the device token to the server.
import Foundation
import UserNotifications
#if os(macOS)
import AppKit
#else
import UIKit
#endif

@MainActor
final class PushManager {
    static let shared = PushManager()

    func requestAndRegister() {
        Task {
            let center = UNUserNotificationCenter.current()
            do {
                let granted = try await center.requestAuthorization(options: [.alert, .sound, .badge])
                let st = await center.notificationSettings().authorizationStatus
                Diag.log("notifications: granted=\(granted) status=\(st.rawValue)")
                ChatStore.shared.pushStatus = granted ? "notifications allowed; registering for push…" : "notifications denied: allow Atlas in System Settings → Notifications"
                guard granted else { return }
            } catch {
                Diag.log("notifications: request failed: \(error)")
                ChatStore.shared.pushStatus = "notifications unavailable (\(error.localizedDescription)); the app's signature must be stable for macOS to allow them"
                return
            }
            #if os(macOS)
            NSApplication.shared.registerForRemoteNotifications()
            #else
            UIApplication.shared.registerForRemoteNotifications()
            #endif
        }
    }

    func gotToken(_ data: Data) {
        let hex = data.map { String(format: "%02x", $0) }.joined()
        AppConfig.shared.deviceToken = hex
        ChatStore.shared.pushStatus = "APNs token received; sending to server…"
        Task { await ChatStore.shared.registerDevice() }
    }

    func failed(_ error: Error) {
        // Common causes: no paid developer team selected in Xcode, an unsigned build, or the Simulator.
        // Not fatal: while the app is running, the live connection delivers notifications itself.
        ChatStore.shared.pushStatus = "Apple push unavailable (\(error.localizedDescription)). Notifications still arrive over the live connection while the app runs."
    }
}

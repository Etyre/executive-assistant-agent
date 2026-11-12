// Push registration and notification handling, the one place the two platforms differ.
import Foundation
import UserNotifications
#if os(macOS)
import AppKit
typealias PlatformAppDelegate = NSObject & NSApplicationDelegate
#else
import UIKit
typealias PlatformAppDelegate = NSObject & UIApplicationDelegate
#endif

final class AppDelegate: PlatformAppDelegate, UNUserNotificationCenterDelegate {

    #if os(macOS)
    func applicationWillFinishLaunching(_ notification: Notification) {
        // One copy at a time: if Atlas is already running (another build, Xcode's Run, a second click), bring
        // that one forward and quit this one, instead of two menu-bar icons and double notifications (Eli, 2026-11-06).
        let me = NSRunningApplication.current
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "com.elityre.atlas")
            .filter { $0.processIdentifier != me.processIdentifier && !$0.isTerminated }
        if let other = others.first {
            other.activate()
            NSApp.terminate(nil)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        PushManager.shared.requestAndRegister()
        // Diagnostics: `defaults write com.elityre.atlas snapshotAfterLaunch 8` saves a picture of the window to
        // ~/Library/Logs/Atlas/window.png that many seconds after launch (the app can draw its own window; no
        // screen-recording permission needed). One-shot: the key is removed after use.
        let delay = UserDefaults.standard.double(forKey: "snapshotAfterLaunch")
        if delay > 0 {
            UserDefaults.standard.removeObject(forKey: "snapshotAfterLaunch")
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { Self.snapshotWindow() }
        }
        // ...and `defaults write com.elityre.atlas snapshotNow 1` at any time (checked every 3 s).
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
            if UserDefaults.standard.bool(forKey: "snapshotNow") {
                UserDefaults.standard.removeObject(forKey: "snapshotNow")
                Self.snapshotWindow()
            }
        }
    }

    static func snapshotWindow() {
        let logs = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0].appendingPathComponent("Logs/Atlas")
        var n = 0
        for w in NSApp.windows where w.isVisible {
            guard let v = w.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) else { continue }
            v.cacheDisplay(in: v.bounds, to: rep)
            let name = w is NSPanel ? "panel.png" : "window.png"
            if let png = rep.representation(using: .png, properties: [:]) { try? png.write(to: logs.appendingPathComponent(name)); n += 1 }
        }
        let names = NSApp.windows.filter { $0.isVisible }.map { "\(type(of: $0)):\($0.title.isEmpty ? "(untitled)" : $0.title) \(Int($0.frame.width))x\(Int($0.frame.height))" }
        Diag.log("snapshot: saved \(n) window(s): \(names.joined(separator: ", "))")
    }

    func application(_ application: NSApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushManager.shared.gotToken(deviceToken)
    }

    func application(_ application: NSApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PushManager.shared.failed(error)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { for w in sender.windows { w.makeKeyAndOrderFront(nil) } }
        return true
    }

    /// Bring the chat to the front, creating the window if it was closed: SwiftUI opens a WindowGroup window for
    /// a URL it handles (`atlas://open`), which is the one reliable way in from outside a view.
    @MainActor static func openChat() {
        NSApp.activate(ignoringOtherApps: true)
        if let w = NSApp.windows.first(where: { $0.canBecomeMain && $0.contentView != nil && !($0 is NSPanel) && $0.title != "Settings" }) {
            w.makeKeyAndOrderFront(nil)
        } else if let url = URL(string: "atlas://open") {
            NSWorkspace.shared.open(url)
        }
        ChatStore.shared.unread = 0
        NoticePanel.shared.clear()
    }
    #else
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        PushManager.shared.requestAndRegister()
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushManager.shared.gotToken(deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PushManager.shared.failed(error)
    }

    // Silent/background push: refresh the thread so it is current when the app is opened.
    func application(_ application: UIApplication, didReceiveRemoteNotification userInfo: [AnyHashable: Any]) async -> UIBackgroundFetchResult {
        await ChatStore.shared.loadHistory()
        return .newData
    }
    #endif

    // MARK: UNUserNotificationCenterDelegate

    /// A notification arrives while the app is running. If the chat is on screen the live event feed already
    /// shows the text, so no banner; otherwise show it as usual.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        let store = ChatStore.shared
        let chatVisible = await MainActor.run { store.isChatVisible }
        Task { await store.loadHistory() }
        return chatVisible ? [] : [.banner, .list, .sound]
    }

    /// The user tapped a notification: bring the chat forward and make sure it is current.
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        await ChatStore.shared.loadHistory()
        await MainActor.run {
            ChatStore.shared.unread = 0
            #if os(macOS)
            Self.openChat()
            #endif
        }
    }
}

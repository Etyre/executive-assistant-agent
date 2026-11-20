// Atlas: the dedicated iPhone + Mac client for the executive-assistant agent.
// One SwiftUI codebase; the agent's proactive messages arrive as push notifications (APNs), the chat view
// streams replies and listens to the server's live event feed so every open copy stays in sync.
import SwiftUI

@main
struct AtlasApp: App {
    #if os(macOS)
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    #else
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    #endif
    @State private var store = ChatStore.shared

    var body: some Scene {
        #if os(macOS)
        WindowGroup {
            MainView()
                .environment(store)
                .task { await store.start() }
                .onOpenURL { url in
                    NSApp.activate(ignoringOtherApps: true)
                    if AppConfig.shared.apply(setup: url) { Task { await store.restart() } }      // start() does nothing once started: the event stream stayed on the old server
                }
        }
        .handlesExternalEvents(matching: ["open"])          // atlas://open creates the window if none is open
        .defaultSize(width: 540 * CGFloat(1 + ((UserDefaults.standard.object(forKey: "showActivity") as? Bool ?? true) ? 1 : 0)
                                          + ((UserDefaults.standard.object(forKey: "showWorkspace") as? Bool ?? false) ? 1 : 0)), height: 760)
        .commands {
            CommandGroup(replacing: .newItem) { }
            CommandMenu("View") {
                Button(store.showActivity ? "Hide Thinking" : "Show Thinking") { store.showActivity.toggle() }
                    .keyboardShortcut("t", modifiers: [.command, .shift])
                Button(store.showWorkspace ? "Hide Workspace" : "Show Workspace") { store.showWorkspace.toggle() }
                    .keyboardShortcut("w", modifiers: [.command, .shift])
                Button("Refresh") { Task { await store.loadHistory(); await store.loadActivity() } }
                    .keyboardShortcut("r", modifiers: .command)
                Divider()
                Button("Copy Conversation (today)") {
                    let day = store.messages.last?.day ?? ""
                    Clipboard.set(store.messages.filter { $0.day == day }.map { "\($0.hhmm) \($0.who == .me ? "me" : store.agentName): \($0.text)" }.joined(separator: "\n\n"))
                }
                .keyboardShortcut("c", modifiers: [.command, .shift])
            }
        }
        Settings {
            SettingsView().environment(store).frame(minWidth: 460)
        }
        MenuBarExtra {
            MenuBarView().environment(store)
        } label: {
            Image(systemName: store.unread > 0 ? "sparkles.rectangle.stack.fill" : "sparkles")
        }
        .menuBarExtraStyle(.window)
        #else
        WindowGroup {
            ChatView()
                .environment(store)
                .task { await store.start() }
                .onOpenURL { url in
                    if AppConfig.shared.apply(setup: url) { Task { await store.restart() } }      // start() does nothing once started: the event stream stayed on the old server
                }
        }
        #endif
    }
}

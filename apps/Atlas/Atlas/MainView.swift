// macOS: the chat, and beside it the optional shared workspace (⌘⇧W) and chain-of-thought pane (⌘⇧T).
// The two panes are always the same width: showing the pane doubles the window and hiding it halves it, so the
// chat keeps the width it had. The pane sits to the right of the chat at full width and the animated window
// edge reveals or covers it, so the slide and the resize are one motion (about a fifth of a second).
#if os(macOS)
import SwiftUI
import AppKit

struct MainView: View {
    @Environment(ChatStore.self) private var store
    @State private var activityMounted = UserDefaults.standard.object(forKey: "showActivity") as? Bool ?? true
    @State private var workspaceMounted = UserDefaults.standard.object(forKey: "showWorkspace") as? Bool ?? false
    @State private var lockedChatWidth: CGFloat? = nil        // fixed while the window animates, flexible otherwise
    @State private var contentWidth: CGFloat = 540
    @State private var animating = false
    private let duration = 0.22

    private var mountedPanes: Int { 1 + (workspaceMounted ? 1 : 0) + (activityMounted ? 1 : 0) }

    var body: some View {
        @Bindable var store = store
        GeometryReader { geo in
            let chatWidth = lockedChatWidth ?? geo.size.width / CGFloat(mountedPanes)
            HStack(spacing: 0) {
                ChatView()
                    .frame(width: chatWidth, height: geo.size.height)
                if workspaceMounted {
                    Divider()
                    WorkspaceView()
                        .frame(width: chatWidth, height: geo.size.height)
                }
                if activityMounted {
                    Divider()
                    ActivityView()
                        .frame(width: chatWidth, height: geo.size.height)
                }
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .leading)
            .clipped()
            .onAppear { contentWidth = geo.size.width }
            .onChange(of: geo.size.width) { _, w in if !animating { contentWidth = w } }
        }
        .frame(minWidth: 380)
        // An open window takes the atlas://open event itself, so it activates instead of a second window appearing.
        .handlesExternalEvents(preferring: ["open"], allowing: ["open"])
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Toggle(isOn: $store.showWorkspace) { Label("Workspace", systemImage: "rectangle.and.pencil.and.ellipsis") }
                    .toggleStyle(.button)
                    .help(store.showWorkspace ? "Hide the shared workspace (⌘⇧W)" : "Show the shared workspace (⌘⇧W)")
                Toggle(isOn: $store.showActivity) { Label("Thinking", systemImage: "brain") }
                    .toggleStyle(.button)
                    .help(store.showActivity ? "Hide the agent's thinking (⌘⇧T)" : "Show the agent's thinking (⌘⇧T)")
            }
        }
        .onChange(of: store.showActivity) { _, show in
            slide(show: show, immediate: { activityMounted = show }, mount: { activityMounted = true }, after: { activityMounted = false })
        }
        .onChange(of: store.showWorkspace) { _, show in
            slide(show: show, immediate: { workspaceMounted = show }, mount: { workspaceMounted = true }, after: { workspaceMounted = false })
        }
    }

    private var window: NSWindow? {
        NSApp.keyWindow ?? NSApp.mainWindow ?? NSApp.windows.first(where: { $0.isVisible && $0.canBecomeMain })
    }

    /// Lock the chat at its current width, mount the pane beside it, and animate only the window frame: the
    /// right edge sweeps across the pane. On hide, unmount the pane once the edge has covered it.
    private func slide(show: Bool, immediate: () -> Void, mount: () -> Void, after: @escaping () -> Void) {
        guard !animating, let w = window else { immediate(); return }
        let chatWidth = contentWidth / CGFloat(mountedPanes)
        lockedChatWidth = chatWidth
        animating = true
        if show { mount() }                                         // laid out at once, hidden beyond the edge
        let panesAfter = mountedPanes + (show ? 0 : -1)
        var f = w.frame
        let chrome = f.width - contentWidth
        f.size.width = chatWidth * CGFloat(panesAfter) + chrome
        if let screen = w.screen?.visibleFrame ?? NSScreen.main?.visibleFrame {
            f.size.width = min(f.width, screen.width)
            if f.maxX > screen.maxX { f.origin.x = screen.maxX - f.width }
            if f.minX < screen.minX { f.origin.x = screen.minX }
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = duration
            ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            w.animator().setFrame(f, display: true)
        }, completionHandler: {
            if !show { after() }
            contentWidth = w.contentView?.bounds.width ?? f.width
            lockedChatWidth = nil
            animating = false
        })
    }
}
#endif

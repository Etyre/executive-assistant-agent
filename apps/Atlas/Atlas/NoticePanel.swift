// A persistent notice: a small floating panel at the top-right of the screen listing the agent's unread messages.
// It stays until you click it (which opens or activates the chat), close it with its x (which just dismisses it,
// Eli 2026-11-12), or until the chat comes to the front. This is the app's own, so it does not depend on System
// Settings' banner style (Eli, 2026-11-07: "notifications should stay until I click on them").
#if os(macOS)
import SwiftUI
import AppKit

@MainActor
final class NoticePanel {
    static let shared = NoticePanel()
    private var panel: NSPanel?
    private(set) var items: [Message] = []

    func add(_ m: Message) {
        items.append(m)
        if items.count > 6 { items.removeFirst(items.count - 6) }
        show()
    }

    func clear() {
        if !items.isEmpty { Diag.log("notice: cleared") }
        items = []
        panel?.orderOut(nil)
    }

    private func show() {
        let p = panel ?? makePanel()
        p.contentView = NSHostingView(rootView: NoticeView(items: items, onOpen: { [weak self] in
            self?.clear()
            AppDelegate.openChat()
        }, onDismiss: { [weak self] in
            Diag.log("notice: dismissed")
            self?.clear()
        }))
        p.setContentSize(p.contentView!.fittingSize)
        // On the screen the mouse is on (where the user is looking), not whichever holds the key window.
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { $0.frame.contains(mouse) }) ?? NSScreen.main ?? NSScreen.screens.first
        if let f = screen?.visibleFrame {
            p.setFrameTopLeftPoint(NSPoint(x: f.maxX - p.frame.width - 16, y: f.maxY - 12))
        }
        p.orderFrontRegardless()
        Diag.log("notice: shown (\(items.count)) at \(Int(p.frame.minX)),\(Int(p.frame.minY))")
    }

    private func makePanel() -> NSPanel {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 360, height: 80),
                        styleMask: [.nonactivatingPanel, .borderless, .utilityWindow], backing: .buffered, defer: false)
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.hidesOnDeactivate = false
        p.isMovableByWindowBackground = true
        panel = p
        return p
    }
}

struct NoticeView: View {
    let items: [Message]
    let onOpen: () -> Void
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "sparkles").foregroundStyle(Color.accentColor)
                Text("Atlas").font(.headline)
                Spacer()
                Text("\(items.count)").font(.caption).foregroundStyle(.secondary)
                Button(action: onDismiss) {
                    Image(systemName: "xmark.circle.fill").font(.system(size: 15)).foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Dismiss (the messages stay in the chat)")
            }
            ForEach(items.suffix(3)) { m in
                Text(m.text).font(.callout).lineLimit(4).fixedSize(horizontal: false, vertical: true)
            }
            if items.count > 3 { Text("and \(items.count - 3) more").font(.caption).foregroundStyle(.secondary) }
            Text("Click to open").font(.caption2).foregroundStyle(.tertiary)
        }
        .padding(14)
        .frame(width: 360, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.accentColor.opacity(0.5), lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture(perform: onOpen)
    }
}
#endif

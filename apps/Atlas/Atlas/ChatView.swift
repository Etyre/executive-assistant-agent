// The thread and the composer.
import SwiftUI
import UniformTypeIdentifiers
#if os(macOS)
import AppKit
#else
import UIKit
#endif

struct ChatView: View {
    @Environment(ChatStore.self) private var store
    @FocusState private var composing: Bool
    @Environment(\.scenePhase) private var phase

    var body: some View {
        @Bindable var store = store
        VStack(spacing: 0) {
            thread
            if !store.attachments.isEmpty { attachmentStrip }
            if !store.workers.isEmpty {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("Working in the background: " + store.workers.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Spacer()
                }
                .padding(.horizontal, 14).padding(.vertical, 4)
            }
            Divider()
            composer
        }
        .navigationTitle(store.agentName)
        .toolbar { toolbar }
        .sheet(isPresented: $store.showSettings) {
            SettingsView().environment(store)
                #if os(macOS)
                .frame(minWidth: 460, minHeight: 420)
                #endif
        }
        #if os(macOS)
        .onAppear { store.windowShowing = true }
        .onDisappear { store.windowShowing = false }
        .onChange(of: phase) { _, p in if p == .active { Task { await store.loadHistory(); store.flushOutbox() } } }
        #else
        .onAppear { store.isChatVisible = true }
        .onDisappear { store.isChatVisible = false }
        .onChange(of: phase) { _, p in
            store.isChatVisible = (p == .active)
            if p == .active { Task { await store.loadHistory() } }
        }
        #endif
        #if os(macOS)
        // Images: paste (⌘V) anywhere in the chat, or drop files / images onto it (Eli, 2026-11-07).
        .onPasteCommand(of: [.image, .png, .jpeg, .tiff, .fileURL]) { providers in loadImages(from: providers) }
        .onDrop(of: [.image, .png, .jpeg, .tiff, .fileURL], isTargeted: nil) { providers in loadImages(from: providers); return true }
        #endif
        #if os(iOS)
        .modifier(NavWrap())
        #endif
    }

    private var thread: some View {
        GeometryReader { geo in
        ScrollViewReader { proxy in
            ScrollView {
                // A plain stack, not a lazy one: a lazy list only lays out what it thinks is visible and, after a
                // reload changed rows near the bottom, showed nothing at all until the window was resized
                // (Eli, 2026-11-12). A few hundred bubbles lay out instantly; the cache holds at most 300.
                VStack(alignment: .leading, spacing: 8) {
                    if store.messages.isEmpty { emptyState }
                    ForEach(Array(store.messages.enumerated()), id: \.element.id) { i, m in
                        if i == 0 || store.messages[i - 1].day != m.day { DayStamp(day: m.day) }
                        if m.isProactive { Stamp("sent by \(store.agentName) at \(m.hhmm)") }
                        MessageBubble(message: m, maxWidth: max(160, min(560, geo.size.width - 70)))
                    }
                    if !store.activity.isEmpty { Stamp(store.activity).transition(.opacity) }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .frame(width: min(720, geo.size.width))          // never wider than the column: text wraps instead of being clipped
                .frame(maxWidth: .infinity)
            }
            .defaultScrollAnchor(.bottom)
            .onChange(of: store.messages.last?.text) { _, _ in withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
            .onChange(of: store.messages.count) { _, _ in settleAtBottom(proxy) }
            // Resizing the window keeps the latest message in view: the chat is anchored at its bottom, so a shorter
            // window hides older messages at the top, not the newest at the bottom (Eli, 2026-11-06).
            .onChange(of: geo.size.height) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
            .onAppear { Diag.log("chat: thread appeared, column \(Int(geo.size.width))x\(Int(geo.size.height)), \(store.messages.count) messages"); settleAtBottom(proxy) }
            .onChange(of: store.messages.count) { _, n in Diag.log("chat: \(n) messages, column \(Int(geo.size.width))x\(Int(geo.size.height))") }
        }
        }
    }

    private var attachmentStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(store.attachments.enumerated()), id: \.offset) { i, a in
                    ZStack(alignment: .topTrailing) {
                        #if os(macOS)
                        if let img = NSImage(data: a.data) {
                            Image(nsImage: img).resizable().aspectRatio(contentMode: .fill).frame(width: 72, height: 72).clipShape(RoundedRectangle(cornerRadius: 8))
                        }
                        #else
                        if let img = UIImage(data: a.data) {
                            Image(uiImage: img).resizable().aspectRatio(contentMode: .fill).frame(width: 72, height: 72).clipShape(RoundedRectangle(cornerRadius: 8))
                        }
                        #endif
                        Button { store.removeAttachment(i) } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.white, .black.opacity(0.6)) }
                            .buttonStyle(.plain).padding(3)
                    }
                }
                Text("\(store.attachments.count) image\(store.attachments.count == 1 ? "" : "s") to send").font(.caption).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 14).padding(.vertical, 6)
        }
    }

    #if os(macOS)
    private func loadImages(from providers: [NSItemProvider]) {
        for p in providers {
            if p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                p.loadItem(forTypeIdentifier: UTType.fileURL.identifier) { item, _ in
                    guard let data = item as? Data, let url = URL(dataRepresentation: data, relativeTo: nil),
                          let type = UTType(filenameExtension: url.pathExtension), type.conforms(to: .image),
                          let bytes = try? Data(contentsOf: url) else { return }
                    DispatchQueue.main.async { store.attach(imageData: bytes) }
                }
            } else if let t = [UTType.png, .jpeg, .tiff, .image].first(where: { p.hasItemConformingToTypeIdentifier($0.identifier) }) {
                p.loadDataRepresentation(forTypeIdentifier: t.identifier) { data, _ in
                    guard let data else { return }
                    DispatchQueue.main.async { store.attach(imageData: data) }
                }
            }
        }
    }

    #endif

    /// The chat always opens and stays at the latest message. A lazy list only knows the heights of the rows it has
    /// laid out, so one scrollTo right after a bulk load lands short (seen 2026-11-06); scroll again once layout settles.
    private func settleAtBottom(_ proxy: ScrollViewProxy) {
        proxy.scrollTo("bottom", anchor: .bottom)
        for delay in [0.05, 0.25, 0.6] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { proxy.scrollTo("bottom", anchor: .bottom) }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "sparkles").font(.largeTitle).foregroundStyle(.secondary)
            if store.config.isConfigured {
                Text("No messages yet. \(store.agentName) will write here when something needs you.").multilineTextAlignment(.center)
            } else {
                Text("Point the app at the agent's server in Settings.").multilineTextAlignment(.center)
                Button("Open Settings") { store.showSettings = true }
            }
            if let e = store.lastError { Text(e).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center) }
        }
        .frame(maxWidth: .infinity).padding(.top, 80).foregroundStyle(.secondary)
    }

    @ViewBuilder private var composer: some View {
        @Bindable var store = store
        HStack(alignment: .bottom, spacing: 8) {
            #if os(macOS)
            // A TextEditor, not a vertical TextField: the field would not wrap at narrow widths and clipped the text
            // (Eli, 2026-11-06). It grows from one to eight lines with the text, then scrolls.
            GeometryReader { g in
                TextEditor(text: $store.draft)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .scrollDisabled(estimatedLines(width: g.size.width) <= 8)
                    .padding(.horizontal, 6).padding(.vertical, 4)
                    .focused($composing)
                    .overlay(alignment: .topLeading) {
                        if store.draft.isEmpty {
                            Text("Message \(store.agentName)…").foregroundStyle(.tertiary).padding(.horizontal, 11).padding(.vertical, 4).allowsHitTesting(false)
                        }
                    }
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) { store.draft += "\n"; return .handled }
                        store.send(); return .handled
                    }
            }
            .frame(height: composerHeight)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.08)))
            #else
            TextField("Message \(store.agentName)…", text: $store.draft, axis: .vertical)
                .lineLimit(1...8)
                .textFieldStyle(.plain)
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.08)))
                .focused($composing)
                .onSubmit { store.send() }
                .submitLabel(.send)
            #endif
            Button(action: store.send) {
                Image(systemName: store.isSending ? "ellipsis.circle" : "arrow.up.circle.fill").font(.system(size: 28))
            }
            .buttonStyle(.plain)
            .disabled(store.draft.trimmingCharacters(in: .whitespaces).isEmpty)
            .keyboardShortcut(.return, modifiers: .command)
        }
        .padding(12)
        .onAppear { composing = true }
    }

    #if os(macOS)
    @State private var composerWidth: CGFloat = 400
    private var composerHeight: CGFloat {
        let lines = min(8, max(1, estimatedLines(width: composerWidth)))
        return CGFloat(lines) * 20 + 14
    }
    /// Rough wrapped-line count for sizing: newlines plus overflow at ~7.2 pt per character.
    private func estimatedLines(width: CGFloat) -> Int {
        let perLine = max(10, Int((width - 24) / 7.2))
        DispatchQueue.main.async { if abs(composerWidth - width) > 1 { composerWidth = width } }
        return store.draft.split(separator: "\n", omittingEmptySubsequences: false).reduce(0) { $0 + max(1, ($1.count + perLine - 1) / perLine) }
    }
    #endif

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .automatic) {
            HStack(spacing: 10) {
                ConnectionDot(state: store.connection)
                Button { store.showSettings = true } label: { Image(systemName: "gearshape") }
            }
        }
    }
}

#if os(iOS)
private struct NavWrap: ViewModifier {
    func body(content: Content) -> some View {
        NavigationStack { content.navigationBarTitleDisplayMode(.inline) }
    }
}
#endif

/// Message text with its web addresses made tappable (2026-11-19: the agent sends a link that opens a restaurant in
/// its app, and a plain `Text(String)` shows a URL as dead text). The text itself is shown exactly as written; only
/// http(s) links are marked. Kept per text, since bubbles are redrawn often.
enum Linkified {
    private static let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
    private static let cache: NSCache<NSString, NSAttributedString> = { let c = NSCache<NSString, NSAttributedString>(); c.countLimit = 400; return c }()

    static func text(_ s: String) -> AttributedString {
        if let hit = cache.object(forKey: s as NSString) { return AttributedString(hit) }
        let out = NSMutableAttributedString(string: s)
        if s.contains("http"), let detector {
            for m in detector.matches(in: s, range: NSRange(location: 0, length: (s as NSString).length)) {
                guard let url = m.url, url.scheme == "https" || url.scheme == "http" else { continue }
                out.addAttribute(.link, value: url, range: m.range)
            }
        }
        cache.setObject(out, forKey: s as NSString)
        return AttributedString(out)
    }
}

struct MessageBubble: View {
    let message: Message
    var maxWidth: CGFloat = 560          // from the column width: selectable Text on macOS only wraps inside an explicit bound
    var body: some View {
        HStack {
            if message.who == .me { Spacer(minLength: 20) }
            Group {
                if message.pending && message.text.isEmpty {
                    ProgressView().controlSize(.small).padding(.horizontal, 6)
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        if !message.localImages.isEmpty || !message.images.isEmpty {
                            HStack(spacing: 6) {
                                ForEach(Array(message.localImages.enumerated()), id: \.offset) { _, d in LocalImage(data: d) }
                                if message.localImages.isEmpty { ForEach(message.images, id: \.self) { n in RemoteImage(name: n) } }
                            }
                        }
                        if !message.text.isEmpty {
                            Text(Linkified.text(message.text))
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                        }
                        if message.queued {
                            Label("waiting for a connection", systemImage: "clock").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: maxWidth - 28, alignment: .leading)
                }
            }
            .padding(.horizontal, 14).padding(.vertical, 9)
            .background(RoundedRectangle(cornerRadius: 14).fill(message.who == .me ? Color.accentColor.opacity(0.18) : Color.secondary.opacity(0.12)))
            .frame(maxWidth: maxWidth, alignment: message.who == .me ? .trailing : .leading)
            // Drag inside a bubble selects text; right-click copies the whole message (SwiftUI cannot select across bubbles).
            .contextMenu {
                Button("Copy message") { Clipboard.set(message.text) }
                Button("Copy with time and sender") { Clipboard.set("\(message.hhmm) \(message.who == .me ? "me" : "agent"): \(message.text)") }
            }
            if message.who == .it { Spacer(minLength: 20) }
        }
        .frame(maxWidth: .infinity, alignment: message.who == .me ? .trailing : .leading)
    }
}

struct Stamp: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading).padding(.leading, 4) }
}

struct DayStamp: View {
    let day: String
    var body: some View {
        Text(pretty).font(.caption.weight(.medium)).foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.vertical, 6)
    }
    private var pretty: String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: day) else { return day }
        if Calendar.current.isDateInToday(d) { return "Today" }
        if Calendar.current.isDateInYesterday(d) { return "Yesterday" }
        return d.formatted(.dateTime.weekday(.wide).month(.abbreviated).day())
    }
}

struct ConnectionDot: View {
    let state: ChatStore.Connection
    var body: some View {
        Circle().fill(color).frame(width: 9, height: 9).help(label)
    }
    private var color: Color {
        switch state { case .live: .green; case .connecting: .yellow; case .retrying: .orange; case .off: .gray }
    }
    private var label: String {
        switch state {
        case .live: "live: new messages appear instantly"
        case .connecting: "connecting…"
        case .retrying(let why): "reconnecting: \(why)"
        case .off: "not connected"
        }
    }
}


enum Clipboard {
    static func set(_ text: String) {
        #if os(macOS)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #else
        UIPasteboard.general.string = text
        #endif
    }
}


/// An image the user attached, shown from the bytes we have.
struct LocalImage: View {
    let data: Data
    var body: some View {
        #if os(macOS)
        if let img = NSImage(data: data) {
            Image(nsImage: img).resizable().aspectRatio(contentMode: .fit).frame(maxWidth: 260, maxHeight: 220).clipShape(RoundedRectangle(cornerRadius: 8))
        }
        #else
        if let img = UIImage(data: data) {
            Image(uiImage: img).resizable().aspectRatio(contentMode: .fit).frame(maxWidth: 260, maxHeight: 220).clipShape(RoundedRectangle(cornerRadius: 8))
        }
        #endif
    }
}

/// An image from the history, fetched with the access token (AsyncImage cannot send headers).
struct RemoteImage: View {
    let name: String
    @Environment(ChatStore.self) private var store
    @State private var data: Data?
    static var cache: [String: Data] = [:]
    var body: some View {
        Group {
            if let d = data ?? Self.cache[name] { LocalImage(data: d) }
            else { RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.15)).frame(width: 120, height: 90).overlay(ProgressView().controlSize(.small)) }
        }
        .task {
            if Self.cache[name] == nil, let d = try? await API(config: store.config).imageData(name) { Self.cache[name] = d; data = d }
        }
    }
}

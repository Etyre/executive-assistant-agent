// The app's state: the thread, the turn in progress, the live connection, push status.
import Foundation
import Observation
import SwiftUI
import UserNotifications
import os
#if os(macOS)
import AppKit
#endif

@MainActor
@Observable
final class ChatStore {
    static let shared = ChatStore()

    let config = AppConfig.shared
    /// Every change is written to a local cache (debounced), so the chat opens with its history offline and a
    /// message typed without a connection survives a relaunch until it is sent (Eli, 2026-11-11).
    var messages: [Message] = [] { didSet { cacheDirty() } }
    /// The unsent message survives a restart (Eli, 2026-11-06).
    var attachments: [API.Attachment] = []    // images pasted or dropped, waiting to be sent with the next message
    var draft: String = UserDefaults.standard.string(forKey: "draft") ?? "" {
        didSet { if draft != oldValue { UserDefaults.standard.set(draft, forKey: "draft") } }
    }
    var isSending = false
    private var flushTimer: Task<Void, Never>?
    private var cacheSave: Task<Void, Never>?
    var activity = ""                 // "looking at the Project Tracker…" while the agent works
    var lastError: String?
    var connection: Connection = .off
    var agentName = "Atlas"
    var pushStatus = "not registered"
    var unread = 0
    var isChatVisible = false { didSet { if oldValue != isChatVisible { presenceChanged() } } }
    var showSettings = false
    // Shared workspace: the whiteboard and any other shared document, one shown at a time
    var documents: [API.DocInfo] = []
    var currentDoc = "workspace"
    var wsGeneration = 0                  // bumped to force the editor to take the store's text (Load theirs)
    var wsText = ""                       // what the editor shows
    var wsVersion = ""                    // the server version the editor's text is based on
    var wsUpdatedBy = ""
    var wsUpdatedAt = ""
    var wsDirty = false                   // local edits not saved yet
    var wsSaving = false
    var wsRemoteChange: String? = nil     // "agent" / "another device": the server moved on while we had unsaved edits
    var wsConflict: API.WorkspaceDoc? = nil
    var wsError: String? = nil
    var showWorkspace: Bool = UserDefaults.standard.object(forKey: "showWorkspace") as? Bool ?? false {
        didSet { UserDefaults.standard.set(showWorkspace, forKey: "showWorkspace"); if showWorkspace && wsVersion.isEmpty { Task { await loadWorkspace() } } }
    }
    private var wsSaveTask: Task<Void, Never>?
    var thoughts: [ActivityItem] = []
    var workers: [String] = []            // names of background workers running right now
    /// Where the thinking pane was: the id of the row at the top of the viewport, or nil = at the bottom. Saved on
    /// every change, restored after the next launch's first load (Eli, 2026-11-06).
    var thoughtsAnchor: String? = UserDefaults.standard.string(forKey: "thoughtsAnchor") {
        didSet { UserDefaults.standard.set(thoughtsAnchor, forKey: "thoughtsAnchor") }
    }
    var pendingScrollRestore = true
    /// The chain-of-thought pane (macOS): an option, remembered across launches.
    var showActivity: Bool = UserDefaults.standard.object(forKey: "showActivity") as? Bool ?? true {
        didSet { UserDefaults.standard.set(showActivity, forKey: "showActivity"); if showActivity && thoughts.isEmpty { Task { await loadActivity() } } }
    }
    private var presenceTask: Task<Void, Never>?
    private var activationObservers: [Any] = []

    enum Connection: Equatable { case off, connecting, live, retrying(String) }

    private var eventsTask: Task<Void, Never>?
    private var lastEventAt = Date()
    private var started = false
    private var api: API { API(config: config) }

    // MARK: lifecycle

    func start() async {
        guard !started else { return }
        if !config.isConfigured { showSettings = true; return }    // not started: a setup link can call again
        started = true
        showSettings = false
        loadCache()
        await loadHistory()
        flushOutbox()
        connectEvents()
        await registerDevice()
        observeActivation()
        if showActivity { await loadActivity() }
        if showWorkspace { await loadWorkspace() }
    }

    // MARK: workspace

    func loadWorkspace() async {
        guard config.isConfigured else { return }
        do {
            async let list = api.documents()
            let d = try await api.workspace(currentDoc)
            apply(d)
            wsRemoteChange = nil
            documents = (try? await list) ?? documents
        } catch { wsError = error.localizedDescription }
    }

    func loadDocumentList() async {
        documents = (try? await api.documents()) ?? documents
    }

    func switchDocument(_ name: String) {
        guard name != currentDoc else { return }
        wsSaveTask?.cancel()
        currentDoc = name
        wsText = ""; wsVersion = ""; wsDirty = false; wsConflict = nil; wsRemoteChange = nil
        Task { await loadWorkspace() }
    }

    func createDocument(_ name: String) {
        let n = name.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        Task {
            _ = try? await api.saveWorkspace("# \(n)\n\n", baseVersion: "", name: n)
            await loadDocumentList()
            switchDocument(n)
        }
    }

    func deleteCurrentDocument() {
        guard currentDoc != "workspace" else { return }
        let n = currentDoc
        Task {
            try? await api.deleteDocument(n)
            switchDocument("workspace")
            await loadDocumentList()
        }
    }

    /// The editor saved (0.8 s after typing stopped).
    func editorSaved(_ name: String, _ text: String) {
        guard name == currentDoc else { return }
        wsText = text
        wsDirty = true
        Task { await saveWorkspace() }
    }

    private func apply(_ d: API.WorkspaceDoc) {
        wsText = d.text; wsVersion = d.version; wsUpdatedBy = d.updatedBy; wsUpdatedAt = d.updatedAt
        wsDirty = false; wsConflict = nil; wsError = nil
    }

    /// Called on every keystroke: mark dirty and save 0.8 s after typing stops.
    func workspaceEdited() {
        wsDirty = true
        wsSaveTask?.cancel()
        wsSaveTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 800_000_000)
            guard !Task.isCancelled, let self else { return }
            await self.saveWorkspace()
        }
    }

    func saveWorkspace(overwrite: Bool = false) async {
        guard wsDirty || overwrite, !wsSaving else { return }
        wsSaving = true
        defer { wsSaving = false }
        do {
            let d = try await api.saveWorkspace(wsText, baseVersion: overwrite ? "" : wsVersion, name: currentDoc)
            wsVersion = d.version; wsUpdatedBy = d.updatedBy; wsUpdatedAt = d.updatedAt
            wsDirty = false; wsConflict = nil; wsRemoteChange = nil; wsError = nil
        } catch let c as API.WorkspaceConflict {
            wsConflict = c.current              // the user decides: take theirs, or overwrite
        } catch {
            wsError = error.localizedDescription
        }
    }

    func takeTheirs() {
        if let c = wsConflict { apply(c) } else { Task { await loadWorkspace(); wsGeneration += 1 } }
        wsGeneration += 1
    }

    func loadActivity() async {
        guard config.isConfigured else { return }
        do { thoughts = try await api.activity() } catch { lastError = error.localizedDescription }
    }

    /// macOS: "in the chat" = the window is showing and the app is the active one. (iOS uses scenePhase.)
    private func observeActivation() {
        #if os(macOS)
        guard activationObservers.isEmpty else { return }
        let nc = NotificationCenter.default
        for (name, active) in [(NSApplication.didBecomeActiveNotification, true), (NSApplication.didResignActiveNotification, false)] {
            activationObservers.append(nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.appActive = active }
            })
        }
        appActive = NSApp.isActive
        #endif
    }

    #if os(macOS)
    var windowShowing = false { didSet { isChatVisible = windowShowing && appActive } }
    var appActive = false { didSet { isChatVisible = windowShowing && appActive } }
    #endif

    /// Report to the server now, and every 30 s while the chat stays in front.
    private func presenceChanged() {
        presenceTask?.cancel()
        let active = isChatVisible
        if active {
            unread = 0
            #if os(macOS)
            NoticePanel.shared.clear()                // the chat is in front: the notice has done its job
            #endif
        }
        presenceTask = Task { [weak self] in
            while let self, self.config.isConfigured {
                try? await self.api.reportPresence(active: active)
                guard active else { return }
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                if Task.isCancelled { return }
            }
        }
    }

    /// Settings changed: reconnect with the new URL/token.
    func restart() async {
        started = false
        eventsTask?.cancel()
        eventsTask = nil
        connection = .off
        messages = []
        lastError = nil
        await start()
    }

    func loadHistory() async {
        guard config.isConfigured else { return }
        do {
            let fresh = try await api.history()
            let before = messages.count
            messages = merge(fresh)
            let ids = messages.map(\.id)
            Diag.log("history: \(fresh.count) loaded, \(before) on screen -> \(messages.count), \(ids.count - Set(ids).count) duplicate ids")
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// Fold a history load into what is on screen instead of replacing it. The server's rows are the truth for
    /// the span they cover: they keep their ids (so nothing is re-laid-out), and they replace the rows the app
    /// made itself for that span (streamed replies, live-feed rows, our own sent messages), which otherwise
    /// showed up twice next to their stored copies (Eli, 2026-11-12). Rows older than the load keeps (the cache
    /// beyond the server's limit) stay in front; queued and streaming rows stay at the end.
    private func merge(_ fresh: [Message]) -> [Message] {
        let waiting = messages.filter { $0.pending || $0.queued }
        guard let first = fresh.first else { return waiting }
        let older = messages.filter { !$0.pending && !$0.queued && $0.id.hasPrefix("db-") && $0.ts < first.ts }
        return older + fresh + waiting
    }

    // MARK: sending

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let images = attachments
        guard !text.isEmpty || !images.isEmpty else { return }
        draft = ""                                   // cleared on submit; given back below if the send fails
        #if os(iOS)
        // The vertical-axis TextField writes its old text back into the binding right after onSubmit, so the
        // sent message stayed in the field (Eli, 2026-11-12). Clear it once more after that write-back.
        DispatchQueue.main.async { [weak self] in
            if let self, self.draft.trimmingCharacters(in: .whitespacesAndNewlines) == text { self.draft = "" }
        }
        #endif
        attachments = []
        lastError = nil
        let now = Self.nowISO()
        let meID = "local-me-\(UUID().uuidString)"
        messages.append(Message(id: meID, who: .me, text: text, ts: now, channel: "app", localImages: images.map(\.data)))
        if isSending {
            // The agent is mid-turn: hand the message over now; the running turn folds it in (or it starts the
            // next turn) and the reply arrives on the live feed (Eli, 2026-11-06).
            Task {
                do {
                    let r = try await api.sendNow(text, images: images, messageID: meID)
                    activity = r.queued ? "the agent will see your message in its current turn" : activity
                } catch {
                    if Self.isOffline(error) { markQueued(meID) } else { lastError = error.localizedDescription; if draft.isEmpty { draft = text } }
                }
            }
            return
        }
        transmit(meID: meID, text: text, images: images)
    }

    /// One request/reply exchange for a message already on screen. Unreachable server: the message is kept as
    /// `queued` and goes out from `flushOutbox()` when the connection is back; nothing is lost and nothing is retyped.
    /// A request cut after the server took the message (the app was left, the signal dropped) looks the same from
    /// here, so every send carries `meID` as its message id and the server answers a resend with the first one's
    /// reply instead of running the message again (the agent got each message twice, Eli 2026-11-15).
    private func transmit(meID: String, text: String, images: [API.Attachment]) {
        isSending = true
        let replyID = "local-it-\(UUID().uuidString)"
        messages.append(Message(id: replyID, who: .it, text: "", ts: Self.nowISO(), channel: "app", pending: true))
        Task {
            var got = ""
            var offline = false
            do {
                for try await ev in api.send(text, images: images, messageID: meID) {
                    switch ev {
                    case .text(let t): got += t; update(replyID, text: got)
                    case .tool(let name): activity = Self.describe(tool: name)
                    case .done(let reply):
                        let final = got.isEmpty ? reply : got
                        // the same reply may already be on screen via the live feed; keep one copy
                        if messages.contains(where: { $0.id != replyID && $0.who == .it && !$0.pending && $0.text == final }) {
                            messages.removeAll { $0.id == replyID }
                        } else {
                            update(replyID, text: final, pending: false)
                        }
                    case .error(let e): update(replyID, text: (got.isEmpty ? "" : got + "\n\n") + "error: \(e)", pending: false)
                    }
                }
            } catch {
                if got.isEmpty && Self.isOffline(error) {
                    offline = true
                    messages.removeAll { $0.id == replyID }
                    markQueued(meID)
                } else {
                    update(replyID, text: (got.isEmpty ? "" : got + "\n\n") + "error: \(error.localizedDescription)", pending: false)
                    lastError = error.localizedDescription
                }
            }
            activity = ""
            isSending = false
            if offline {
                lastError = "no connection to the server: your message is saved and will be sent when it is back"
            } else {
                await loadHistory()               // swap the local ids for the server's, so later events dedupe
                flushOutbox()
            }
        }
    }

    // MARK: offline outbox and local cache

    private static func isOffline(_ error: Error) -> Bool {
        (error as NSError).domain == NSURLErrorDomain
    }

    private func markQueued(_ id: String) {
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].queued = true }
        Diag.log("outbox: queued \(messages.filter(\.queued).count)")
        if flushTimer == nil {
            flushTimer = Task { [weak self] in
                while let self, !Task.isCancelled, self.messages.contains(where: \.queued) {
                    try? await Task.sleep(nanoseconds: 30_000_000_000)
                    self.flushOutbox()
                }
                self?.flushTimer = nil
            }
        }
    }

    /// Send the oldest queued message; the exchange's completion calls back here for the next one.
    func flushOutbox() {
        guard !isSending, config.isConfigured, let m = messages.first(where: \.queued) else { return }
        guard let i = messages.firstIndex(where: { $0.id == m.id }) else { return }
        messages[i].queued = false
        Diag.log("outbox: sending \(m.id)")
        let images = m.localImages.map { API.Attachment(data: $0, mediaType: $0.starts(with: [0xFF, 0xD8]) ? "image/jpeg" : "image/png") }
        transmit(meID: m.id, text: m.text, images: images)
    }

    private static var cacheURL: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Atlas")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("messages.json")
    }

    private func cacheDirty() {
        cacheSave?.cancel()
        cacheSave = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 800_000_000)
            guard let self, !Task.isCancelled else { return }
            let keep = Array(self.messages.filter { !$0.pending }.suffix(300))
            if let data = try? JSONEncoder().encode(keep) { try? data.write(to: Self.cacheURL, options: .atomic) }
        }
    }

    private func loadCache() {
        guard messages.isEmpty, let data = try? Data(contentsOf: Self.cacheURL),
              let cached = try? JSONDecoder().decode([Message].self, from: data) else { return }
        messages = cached
        Diag.log("cache: \(cached.count) messages, \(cached.filter(\.queued).count) queued")
    }

    private func update(_ id: String, text: String, pending: Bool? = nil) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[i].text = text
        if let pending { messages[i].pending = pending }
    }

    // MARK: attachments (paste / drop)

    /// Add an image to send with the next message: downscaled to 1568 px on the long side and re-encoded, so a
    /// screenshot does not cost megabytes.
    func attach(imageData: Data) {
        #if os(macOS)
        guard let img = NSImage(data: imageData), let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) else { return }
        let w = CGFloat(rep.pixelsWide), h = CGFloat(rep.pixelsHigh)
        let scale = min(1, 1568 / max(w, h))
        var out = rep
        if scale < 1, let scaled = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(w * scale), pixelsHigh: Int(h * scale), bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) {
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: scaled)
            img.draw(in: NSRect(x: 0, y: 0, width: w * scale, height: h * scale), from: .zero, operation: .copy, fraction: 1)
            NSGraphicsContext.restoreGraphicsState()
            out = scaled
        }
        // PNG keeps screenshots crisp; JPEG for photos (large, no alpha)
        let photo = out.pixelsWide * out.pixelsHigh > 800_000 && !rep.hasAlpha
        if let data = out.representation(using: photo ? .jpeg : .png, properties: photo ? [.compressionFactor: 0.85] : [:]) {
            attachments.append(API.Attachment(data: data, mediaType: photo ? "image/jpeg" : "image/png"))
        }
        #endif
    }

    func removeAttachment(_ i: Int) { if attachments.indices.contains(i) { attachments.remove(at: i) } }

    // MARK: live events

    func connectEvents() {
        eventsTask?.cancel()
        eventsTask = Task { [weak self] in
            var backoff: UInt64 = 1
            while !Task.isCancelled {
                guard let self else { return }
                self.connection = .connecting
                Diag.log("events: connecting")
                self.lastEventAt = Date()
                // Watchdog: the server pings every 25 s; a minute of silence means a dead socket (e.g. the server
                // was restarted and the close never reached us), so drop the stream and reconnect.
                let stream = self.api.events()
                let watchdog = Task { [weak self] in
                    while !Task.isCancelled {
                        try? await Task.sleep(nanoseconds: 15_000_000_000)
                        guard let self, !Task.isCancelled else { return }
                        if Date().timeIntervalSince(self.lastEventAt) > 60 {
                            Diag.log("events: no ping for 60 s; reconnecting")
                            self.connectEvents()          // cancels this task and the stream's consumer
                            return
                        }
                    }
                }
                do {
                    for try await e in stream {
                        backoff = 1
                        self.lastEventAt = Date()
                        self.handle(e)
                    }
                    Diag.log("events: stream closed by server")
                    self.connection = .retrying("stream closed")
                } catch is CancellationError {
                    Diag.log("events: consumer cancelled (task cancelled=\(Task.isCancelled))")
                    watchdog.cancel()
                    if Task.isCancelled { return }
                    self.connection = .retrying("cancelled")
                } catch {
                    Diag.log("events: \(error.localizedDescription)")
                    self.connection = .retrying(error.localizedDescription)
                }
                watchdog.cancel()
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 15)
            }
        }
    }

    private func handle(_ e: LiveEvent) {
        switch e["type"] as? String {
        case "hello":
            connection = .live
            Diag.log("events: live")
            if let a = e["agent"] as? String { agentName = a }
            workers = (e["workers"] as? [[String: Any]] ?? []).compactMap { $0["name"] as? String }
            Task { await loadHistory(); flushOutbox(); if showActivity { await loadActivity() } }   // catch up, then send what waited
        case "message":
            guard let text = e["text"] as? String, let who = (e["who"] as? String).flatMap(Message.Who.init) else { return }
            if who == .me && (e["client_id"] as? String) == config.clientID { return }     // typed here; already on screen
            if messages.contains(where: { $0.who == who && $0.text == text && !$0.pending }) { return }
            if who == .it, let i = messages.lastIndex(where: { $0.who == .it && $0.pending }), messages[i].text == text {
                messages[i].pending = false; return                    // the streamed bubble just finished with this text
            }
            let m = Message(id: "live-\(UUID().uuidString)", who: who, text: text, ts: e["ts"] as? String ?? Self.nowISO(), channel: e["channel"] as? String ?? "",
                            images: e["images"] as? [String] ?? [])
            messages.append(m)
            if who == .it {
                if !isChatVisible { unread += 1 }
                if (e["quiet"] as? Bool) != true { notifyLocally(m) }        // quiet: the user is in the chat somewhere
            }
        case "bye":                                  // server restarting: drop this stream, reconnect at once
            Diag.log("events: server said bye")
            connection = .retrying("server restarting")
            connectEvents()
        case "workers":
            workers = (e["running"] as? [[String: Any]] ?? []).compactMap { $0["name"] as? String }
        case "ui":
            if (e["action"] as? String) == "show_workspace" {
                if !showWorkspace { showWorkspace = true }
                if let d = e["doc"] as? String, d != currentDoc { switchDocument(d) } else { Task { await loadWorkspace() } }
            } else if (e["action"] as? String) == "hide_workspace" {
                // The agent closed the pane. Unsaved typing is safe: the debounced save lives in this store, not the pane.
                if showWorkspace { showWorkspace = false }
            } else if (e["action"] as? String) == "show_activity" {
                if !showActivity { showActivity = true }
            } else if (e["action"] as? String) == "hide_activity" {
                if showActivity { showActivity = false }
            }
        case "workspace":
            if (e["client_id"] as? String) == config.clientID { return }      // our own save
            let doc = e["doc"] as? String ?? "workspace"
            if (e["deleted"] as? Bool) == true { Task { await loadDocumentList() }; if doc == currentDoc { switchDocument("workspace") }; return }
            if doc != currentDoc { Task { await loadDocumentList() }; return }
            let who = (e["by"] as? String) == "agent" ? "the agent" : "another device"
            if wsDirty { wsRemoteChange = who } else { Task { await loadWorkspace() } }
        case "activity":
            if let item = ActivityItem(json: e) {
                thoughts.append(item)
                if thoughts.count > 2000 { thoughts.removeFirst(thoughts.count - 1500) }
            }
        default:
            break
        }
    }

    /// A message from the agent arrived over the live connection while the chat was not in front: show it as a
    /// system notification. This is what makes notifications work with no Apple push setup at all, as long as
    /// the app is running (always the case on the Mac, where it sits in the menu bar).
    private func notifyLocally(_ m: Message) {
        if isChatVisible { return }
        #if os(macOS)
        NoticePanel.shared.add(m)                     // stays on screen until clicked
        #endif
        let c = UNMutableNotificationContent()
        c.title = agentName
        c.body = m.text.count > 400 ? String(m.text.prefix(399)) + "…" : m.text
        c.sound = .default
        c.threadIdentifier = "chat"
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: m.id, content: c, trigger: nil)) { err in
            Diag.log(err.map { "notification: failed: \($0)" } ?? "notification: posted (\(m.text.prefix(40)))")
        }
    }

    // MARK: push

    func registerDevice() async {
        let tok = config.deviceToken
        guard !tok.isEmpty, config.isConfigured else { return }
        do {
            let status = try await api.registerDevice(token: tok)
            config.registeredToken = tok
            pushStatus = "registered (\(config.pushEnvironment)); server: \(status)"
        } catch {
            pushStatus = "registration failed: \(error.localizedDescription)"
        }
    }

    func testPush() async -> String {
        do { return try await api.testPush() } catch { return error.localizedDescription }
    }

    func checkServer() async -> String {
        do {
            let h = try await api.health()
            return "\(h["agent"] ?? "?") · \(h["model"] ?? "") · push: \(h["push"] ?? "?")"
        } catch { return error.localizedDescription }
    }

    // MARK: helpers

    nonisolated static func nowISO() -> String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"; return f.string(from: Date())
    }

    static let niceSources: [String: String] = ["tracker": "the Project Tracker", "focus": "the Focus Monitor", "sleep": "Sleep Bandit",
        "oura": "Oura", "toggl": "Toggl", "calendar": "the calendar", "memory": "memory", "exercise": "the exercise log",
        "otter": "Otter transcripts", "read": "a document", "list": "documents"]

    static func describe(tool: String) -> String {
        let k = tool.split(separator: "_").first.map(String.init) ?? tool
        return "looking at " + (niceSources[k] ?? tool.replacingOccurrences(of: "_", with: " ")) + "…"
    }
}

// The server's HTTP surface (ea/channels/web.py), as the app uses it.
import Foundation
import os

struct Message: Identifiable, Hashable, Codable {
    enum Who: String, Codable { case me, it }
    var id: String
    var who: Who
    var text: String
    var ts: String           // ISO-8601 local, as the server sends it
    var channel: String
    var pending = false      // being streamed right now
    var queued = false       // typed while the server was unreachable; sent automatically once it is back
    var images: [String] = []        // names served by /api/images/<name>
    var localImages: [Data] = []     // what we attached ourselves, before the server names come back

    var day: String { String(ts.prefix(10)) }
    var hhmm: String { ts.count >= 16 ? String(ts[ts.index(ts.startIndex, offsetBy: 11)..<ts.index(ts.startIndex, offsetBy: 16)]) : "" }
    var isProactive: Bool { channel == "proactive" }
}

enum StreamEvent {
    case text(String), tool(String), done(String), error(String)
}

/// One line of the live feed, as a dictionary: {"type":"message",...} or {"type":"activity","kind":...}.
typealias LiveEvent = [String: Any]

/// One step of the agent's chain of thought (see the server's conversations.activity()).
struct ActivityItem: Identifiable, Hashable {
    enum Kind: String { case turn, thinking, text, tool_call, tool_result }
    var id: String
    var kind: Kind
    var ts: String
    var label = ""          // turn: what started it (user text, or "[heartbeat: ...]")
    var channel = ""        // turn/text: app, web, scheduler, ...
    var text = ""           // thinking / text
    var final = false       // text: the reply that ended the turn (shown in the chat too)
    var name = ""           // tool_call / tool_result
    var input = ""          // tool_call: compact JSON
    var output = ""         // tool_result
    var error = false
    var callID = ""

    init?(json e: [String: Any]) {
        guard let k = (e["kind"] as? String).flatMap(Kind.init) else { return nil }
        kind = k
        ts = e["ts"] as? String ?? ChatStore.nowISO()
        label = e["label"] as? String ?? ""
        channel = e["channel"] as? String ?? ""
        text = e["text"] as? String ?? ""
        final = e["final"] as? Bool ?? false
        name = e["name"] as? String ?? ""
        output = e["output"] as? String ?? ""
        error = e["error"] as? Bool ?? false
        callID = e["id"] as? String ?? ""
        if let inp = e["input"], JSONSerialization.isValidJSONObject(inp),
           let d = try? JSONSerialization.data(withJSONObject: inp, options: [.sortedKeys, .withoutEscapingSlashes]) {
            input = String(data: d, encoding: .utf8) ?? ""
        }
        id = "\(kind.rawValue)-\(ts)-\(callID)-\(label.hashValue)-\(text.hashValue)-\(output.hashValue)"
    }

    var hhmm: String { ts.count >= 16 ? String(ts[ts.index(ts.startIndex, offsetBy: 11)..<ts.index(ts.startIndex, offsetBy: 16)]) : "" }
    var isScheduled: Bool { channel == "scheduler" }
}

struct APIError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

struct API {
    let config: AppConfig

    /// One session for the app's lifetime. A session created per request can be released while the event
    /// stream is still being read, which silently ends the stream (seen 2026-11-06).
    private static let shared: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 90          // idle timeout; the event stream pings every 25 s
        c.timeoutIntervalForResource = 60 * 60 * 24 * 7
        // Never wait for connectivity: with it on, a refused connection while the server restarts made the event
        // stream's connect hang forever instead of failing into the retry loop (seen 2026-11-06).
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()
    private var session: URLSession { Self.shared }

    private func request(_ path: String, method: String = "GET", json: [String: Any]? = nil) throws -> URLRequest {
        guard let base = config.url else { throw APIError(message: "Set the server URL in Settings.") }
        guard !config.token.isEmpty else { throw APIError(message: "Set the access token (EA_WEB_TOKEN) in Settings.") }
        // `path` may carry a query ("api/history?limit=120"); appendingPathComponent would percent-encode the "?".
        let parts = path.split(separator: "?", maxSplits: 1).map(String.init)
        var comps = URLComponents(url: base.appendingPathComponent(parts[0]), resolvingAgainstBaseURL: false)!
        if parts.count == 2 { comps.percentEncodedQuery = parts[1] }
        guard let url = comps.url else { throw APIError(message: "bad URL for \(path)") }
        var r = URLRequest(url: url)
        r.httpMethod = method
        r.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        if !config.deviceToken.isEmpty { r.setValue(config.deviceToken, forHTTPHeaderField: "X-Device-Token") }
        if let json {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        return r
    }

    private func check(_ resp: URLResponse, _ data: Data? = nil) throws {
        guard let h = resp as? HTTPURLResponse else { return }
        guard (200..<300).contains(h.statusCode) else {
            let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
            let hint = h.statusCode == 401 ? "wrong access token" : h.statusCode == 503 ? "server not fully configured" : ""
            throw APIError(message: "HTTP \(h.statusCode) \(hint) \(body.prefix(200))".trimmingCharacters(in: .whitespaces))
        }
    }

    // MARK: endpoints

    func health() async throws -> [String: Any] {
        guard let base = config.url else { throw APIError(message: "Set the server URL in Settings.") }
        let (data, resp) = try await session.data(from: base.appendingPathComponent("health"))
        try check(resp, data)
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    func history(limit: Int = 120) async throws -> [Message] {
        let (data, resp) = try await session.data(for: request("api/history?limit=\(limit)"))
        try check(resp, data)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let rows = obj?["messages"] as? [[String: Any]] ?? []
        return rows.compactMap { r in
            guard let text = r["text"] as? String, let who = Message.Who(rawValue: r["who"] as? String ?? "") else { return nil }
            let id = (r["id"] as? Int).map { "db-\($0)" } ?? "h-\(r["ts"] as? String ?? "")-\(text.hashValue)"
            return Message(id: id, who: who, text: text, ts: r["ts"] as? String ?? "", channel: r["channel"] as? String ?? "",
                           images: r["images"] as? [String] ?? [])
        }
    }

    struct Attachment { var data: Data; var mediaType: String }

    private func imagePayload(_ images: [Attachment]) -> [[String: String]] {
        images.map { ["media_type": $0.mediaType, "data": $0.data.base64EncodedString()] }
    }

    func imageData(_ name: String) async throws -> Data {
        let (data, resp) = try await session.data(for: request("api/images/\(name)"))
        try check(resp, data)
        return data
    }

    struct SendResult { var reply: String; var queued: Bool }

    /// A message without streaming: when the agent is mid-turn the server queues it (queued = true) and the reply
    /// comes over the live feed; otherwise this waits for the reply.
    /// `messageID`: this message's own id, the same on every resend, so the server never runs one message twice.
    func sendNow(_ text: String, images: [Attachment] = [], messageID: String = "") async throws -> SendResult {
        let req = try request("api/message", method: "POST", json: ["text": text, "channel": "app", "client_id": config.clientID, "message_id": messageID, "images": imagePayload(images)])
        let (data, resp) = try await session.data(for: req)
        try check(resp, data)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return SendResult(reply: obj?["reply"] as? String ?? "", queued: obj?["queued"] as? Bool ?? false)
    }

    /// One chat turn, streamed: text deltas as the model writes, tool names as it looks things up, then done/error.
    func send(_ text: String, images: [Attachment] = [], messageID: String = "") -> AsyncThrowingStream<StreamEvent, Error> {
        AsyncThrowingStream { cont in
            let task = Task {
                do {
                    let req = try request("api/message/stream", method: "POST",
                                          json: ["text": text, "channel": "app", "client_id": config.clientID, "message_id": messageID, "images": imagePayload(images)])
                    let (bytes, resp) = try await session.bytes(for: req)
                    try check(resp)
                    for try await line in bytes.lines {
                        guard let d = line.data(using: .utf8),
                              let e = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                              let type = e["type"] as? String else { continue }
                        switch type {
                        case "text": cont.yield(.text(e["text"] as? String ?? ""))
                        case "tool": cont.yield(.tool(e["name"] as? String ?? ""))
                        case "done": cont.yield(.done(e["reply"] as? String ?? "")); cont.finish(); return
                        case "error": cont.yield(.error(e["error"] as? String ?? "error")); cont.finish(); return
                        default: break
                        }
                    }
                    cont.finish()
                } catch {
                    cont.finish(throwing: error)
                }
            }
            cont.onTermination = { _ in task.cancel() }
        }
    }

    /// The live feed: everything that passes through the agent, from any device, plus the agent's own messages.
    func events() -> AsyncThrowingStream<LiveEvent, Error> {
        AsyncThrowingStream { cont in
            let task = Task {
                do {
                    let (bytes, resp) = try await session.bytes(for: request("api/events"))
                    try check(resp)
                    for try await line in bytes.lines {
                        if let d = line.data(using: .utf8), let e = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] { cont.yield(e) }
                    }
                    Diag.log("events: server closed the stream")
                    cont.finish()
                } catch {
                    Diag.log("events: read failed: \(String(describing: error)) cancelled=\(Task.isCancelled)")
                    cont.finish(throwing: error)
                }
            }
            cont.onTermination = { reason in
                Diag.log("events: stream terminated (\(String(describing: reason)))")
                task.cancel()
            }
        }
    }

    struct WorkspaceDoc { var text: String; var version: String; var updatedBy: String; var updatedAt: String }
    struct WorkspaceConflict: Error { var current: WorkspaceDoc }

    private func doc(_ obj: [String: Any]?) -> WorkspaceDoc {
        WorkspaceDoc(text: obj?["text"] as? String ?? "", version: obj?["version"] as? String ?? "",
                     updatedBy: obj?["updated_by"] as? String ?? "", updatedAt: obj?["updated_at"] as? String ?? "")
    }

    struct DocInfo: Identifiable, Hashable { var name: String; var title: String; var whiteboard: Bool; var updatedBy: String; var id: String { name } }

    func documents() async throws -> [DocInfo] {
        let (data, resp) = try await session.data(for: request("api/docs"))
        try check(resp, data)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return (obj?["documents"] as? [[String: Any]] ?? []).compactMap { d in
            guard let n = d["name"] as? String else { return nil }
            return DocInfo(name: n, title: d["title"] as? String ?? n, whiteboard: d["whiteboard"] as? Bool ?? false, updatedBy: d["updated_by"] as? String ?? "")
        }
    }

    /// A document's name as a path piece. Not percent-encoded here: `request` builds the URL with
    /// appendingPathComponent, which encodes it. Encoding it here as well sent "Locked-in%2520day" for
    /// "Locked-in day", and the server refused every document with a space in its name (HTTP 400; Eli, 2026-11-16).
    private func encoded(_ name: String) -> String { name }

    func workspace(_ name: String = "workspace") async throws -> WorkspaceDoc {
        let (data, resp) = try await session.data(for: request("api/docs/\(encoded(name))"))
        try check(resp, data)
        return doc(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func deleteDocument(_ name: String) async throws {
        let (data, resp) = try await session.data(for: request("api/docs/\(encoded(name))", method: "DELETE"))
        try check(resp, data)
    }

    /// Save the user's text. Throws WorkspaceConflict (with the current document) if it moved on since baseVersion.
    func saveWorkspace(_ text: String, baseVersion: String, name: String = "workspace") async throws -> WorkspaceDoc {
        let req = try request("api/docs/\(encoded(name))", method: "PUT", json: ["text": text, "base_version": baseVersion, "client_id": config.clientID])
        let (data, resp) = try await session.data(for: req)
        if let h = resp as? HTTPURLResponse, h.statusCode == 409 {
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw WorkspaceConflict(current: doc(obj?["detail"] as? [String: Any]))
        }
        try check(resp, data)
        return doc(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func activity(limit: Int = 300) async throws -> [ActivityItem] {
        let (data, resp) = try await session.data(for: request("api/activity?limit=\(limit)"))
        try check(resp, data)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return (obj?["events"] as? [[String: Any]] ?? []).compactMap(ActivityItem.init(json:))
    }

    /// Tell the server whether this device's chat is in front. While any device's is, the agent's messages are
    /// quiet everywhere (no banner, no phone push): the user is already looking at the thread.
    func reportPresence(active: Bool) async throws {
        let (data, resp) = try await session.data(for: request("api/presence", method: "POST", json: ["client_id": config.clientID, "active": active]))
        try check(resp, data)
    }

    func registerDevice(token: String) async throws -> String {
        let body: [String: Any] = ["token": token, "platform": AppConfig.platform, "name": config.deviceName, "env": config.pushEnvironment]
        let (data, resp) = try await session.data(for: request("api/devices", method: "POST", json: body))
        try check(resp, data)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return obj?["push"] as? String ?? "registered"
    }

    func testPush() async throws -> String {
        let body: [String: Any] = config.deviceToken.isEmpty ? [:] : ["token": config.deviceToken]
        let (data, resp) = try await session.data(for: request("api/push/test", method: "POST", json: body))
        try check(resp, data)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let results = obj?["results"] as? [[String: Any]] ?? []
        let sent = obj?["sent"] as? Int ?? 0
        let reasons = results.filter { ($0["status"] as? Int) != 200 }.map { "\($0["reason"] ?? "?")" }
        return sent > 0 ? "Apple accepted \(sent) notification(s)" : "not delivered: \(reasons.joined(separator: ", "))"
    }
}

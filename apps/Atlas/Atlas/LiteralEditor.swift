// Literal's editor (Eli's own Markdown editor: rendered page, markup hidden, fully editable) inside a web
// view, for the workspace pane. Text in and out goes over a small bridge; saving happens 0.8 s after typing stops.
#if os(macOS)
import SwiftUI
import WebKit

struct LiteralEditor: NSViewRepresentable {
    let docName: String
    let text: String                 // the server's text; pushed into the editor when it changes and the editor is clean
    let generation: Int              // bump to force a reload of the text into the editor (e.g. "Load theirs")
    var onSave: (String, String) -> Void          // (docName, text)
    var onDirty: (Bool) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(context.coordinator, name: "atlas")
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = context.coordinator
        web.setValue(false, forKey: "drawsBackground")
        if let url = Bundle.main.url(forResource: "embed", withExtension: "html", subdirectory: "literal") {
            web.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent().deletingLastPathComponent())
        }
        context.coordinator.web = web
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        context.coordinator.parent = self
        context.coordinator.push()
    }

    final class Coordinator: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
        var parent: LiteralEditor
        weak var web: WKWebView?
        private var ready = false
        private var shown: (String, String, Int)? = nil        // (doc, text, generation) last pushed
        private var saveTimer: Timer?

        init(_ p: LiteralEditor) { parent = p }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { }

        /// Push the document into the editor when it is new, or its text changed while the editor is clean.
        func push() {
            guard ready, let web else { return }
            let key = (parent.docName, parent.text, parent.generation)
            if let s = shown, s == key { return }
            let force = (shown?.2 ?? -1) != parent.generation
            shown = key
            let js = "window.__atlasSetDocument(\(Self.quote("/atlas/\(parent.docName).md")), \(Self.quote(parent.text)), \(force))"
            web.evaluateJavaScript(js)
        }

        func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
            guard let d = m.body as? [String: Any], let type = d["type"] as? String else { return }
            switch type {
            case "ready":
                ready = true
                Diag.log("literal: editor ready")
                push()
            case "dirty":
                let dirty = d["dirty"] as? Bool ?? false
                parent.onDirty(dirty)
                saveTimer?.invalidate()
                if dirty {
                    saveTimer = Timer.scheduledTimer(withTimeInterval: 0.8, repeats: false) { [weak self] _ in
                        self?.web?.evaluateJavaScript("window.__atlasMenu('save')")
                    }
                }
            case "save":
                if let path = d["path"] as? String, let text = d["text"] as? String {
                    let name = path.hasPrefix("/atlas/") ? String(path.dropFirst(7).dropLast(3)) : path
                    shown = (name, text, parent.generation)          // the editor and the store now agree
                    Diag.log("literal: saved \(name) (\(text.count) chars)")
                    parent.onSave(name, text)
                }
            case "open":
                if let u = (d["url"] as? String).flatMap(URL.init(string:)) { NSWorkspace.shared.open(u) }
            default: break
            }
        }

        static func quote(_ s: String) -> String {
            let data = try? JSONSerialization.data(withJSONObject: [s])
            let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
            return String(arr.dropFirst().dropLast())
        }
    }
}
#endif

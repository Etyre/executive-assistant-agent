// The shared workspace: the whiteboard and any other shared document, edited in Literal's editor (rendered
// Markdown, markup hidden, fully editable; ```mermaid fences drawn as diagrams below). Saves itself shortly after
// typing stops; the agent's edits arrive live. Conflicts are the user's call, never silent.
#if os(macOS)
import SwiftUI

struct WorkspaceView: View {
    @Environment(ChatStore.self) private var store
    @AppStorage("draft.workspace.newDocumentName") private var newName = ""      // a draft is never lost
    @State private var askingName = false

    var body: some View {
        @Bindable var store = store
        VStack(spacing: 0) {
            header
            Divider()
            if let who = store.wsRemoteChange {
                banner("\(who.capitalized) changed this page while you were editing.", primary: ("Load theirs", { store.takeTheirs() }), secondary: ("Keep mine", { store.wsRemoteChange = nil; Task { await store.saveWorkspace(overwrite: true) } }))
            }
            if store.wsConflict != nil {
                banner("Your save was refused: the page changed first.", primary: ("Load theirs (drop mine)", { store.takeTheirs() }), secondary: ("Overwrite with mine", { Task { await store.saveWorkspace(overwrite: true) } }))
            }
            LiteralEditor(docName: store.currentDoc, text: store.wsText, generation: store.wsGeneration,
                          onSave: { name, text in store.editorSaved(name, text) },
                          onDirty: { d in if d { store.wsDirty = true } })
        }
        .background(Color.secondary.opacity(0.03))
        .onAppear { if store.wsVersion.isEmpty { Task { await store.loadWorkspace() } } }
        .alert("New document", isPresented: $askingName) {
            TextField("Name", text: $newName)
            Button("Create") { store.createDocument(newName); newName = "" }
            Button("Cancel", role: .cancel) { newName = "" }
        } message: { Text("A shared Markdown page the agent can also edit.") }
    }

    @ViewBuilder private var header: some View {
        @Bindable var store = store
        HStack(spacing: 8) {
            Image(systemName: "rectangle.and.pencil.and.ellipsis").foregroundStyle(.secondary)
            Picker("", selection: Binding(get: { store.currentDoc }, set: { store.switchDocument($0) })) {
                Text("Whiteboard").tag("workspace")
                ForEach(store.documents.filter { !$0.whiteboard }) { d in Text(d.title).tag(d.name) }
            }
            .labelsHidden().frame(maxWidth: 260)
            Spacer()
            Text(status).font(.caption).foregroundStyle(.secondary)
            Menu {
                Button("New document…") { askingName = true }
                Button("Reload from server") { Task { await store.loadWorkspace(); store.wsGeneration += 1 } }
                if store.currentDoc != "workspace" {
                    Divider()
                    Button("Delete this document", role: .destructive) { store.deleteCurrentDocument() }
                }
            } label: { Image(systemName: "ellipsis.circle") }
            .menuStyle(.borderlessButton).frame(width: 28)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    private var status: String {
        if let e = store.wsError { return "error: \(e)" }
        if store.wsSaving { return "saving…" }
        if store.wsDirty { return "unsaved" }
        let when = store.wsUpdatedAt.count >= 16 ? String(store.wsUpdatedAt.dropFirst(11).prefix(5)) : store.wsUpdatedAt
        return store.wsUpdatedBy.isEmpty ? "" : "last edit: \(store.wsUpdatedBy) \(when)"
    }

    private func banner(_ text: String, primary: (String, () -> Void), secondary: (String, () -> Void)) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
            Text(text).font(.callout)
            Spacer()
            Button(primary.0, action: primary.1).controlSize(.small)
            Button(secondary.0, action: secondary.1).controlSize(.small)
        }
        .padding(10).background(Color.orange.opacity(0.12))
    }
}
#endif

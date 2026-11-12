// macOS only: the last few messages and a composer, one click from the menu bar.
#if os(macOS)
import SwiftUI

struct MenuBarView: View {
    @Environment(ChatStore.self) private var store
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        @Bindable var store = store
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(store.agentName).font(.headline)
                ConnectionDot(state: store.connection)
                Spacer()
                Button("Open") { AppDelegate.openChat() }
                .controlSize(.small)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(store.messages.suffix(6)) { m in
                        if m.isProactive { Stamp("sent at \(m.hhmm)") }
                        MessageBubble(message: m).font(.callout)
                    }
                }
            }
            .frame(height: 260)
            HStack {
                TextField("Message \(store.agentName)…", text: $store.draft).textFieldStyle(.roundedBorder).onSubmit { store.send() }
                Button("Send", action: store.send).disabled(store.isSending || store.draft.isEmpty)
            }
        }
        .padding(12)
        .frame(width: 380)
        .onAppear { store.unread = 0 }
    }
}
#endif

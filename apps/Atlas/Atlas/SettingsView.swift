// Server address, access token, push registration and a test button.
import SwiftUI

struct SettingsView: View {
    @Environment(ChatStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var serverStatus = ""
    @State private var pushResult = ""
    @State private var engine: [String: Any] = [:]

    var body: some View {
        @Bindable var cfg = store.config
        Form {
            Section("Server") {
                TextField("URL", text: $cfg.baseURL, prompt: Text("https://your-mac.tailnet.ts.net  or  http://127.0.0.1:8800"))
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .keyboardType(.URL).textInputAutocapitalization(.never)
                    #endif
                SecureField("Access token (EA_WEB_TOKEN)", text: $cfg.token)
                HStack {
                    Button("Check") { Task { serverStatus = await store.checkServer() } }
                    Text(serverStatus).font(.footnote).foregroundStyle(.secondary).lineLimit(3)
                }
            }
            Section("Engine") {
                LabeledContent("Runs on") { Text(engine["billing"] as? String ?? "…") }
                if let w = engine["subscription_window"] as? String {
                    LabeledContent("Subscription window") {
                        Text("\(w) · \(engine["subscription_status"] as? String ?? "")" + ((engine["subscription_utilization"] as? Double).map { String(format: " · %.0f%% used", $0 * 100) } ?? "")
                             + ((engine["subscription_resets"] as? String).map { " · resets \($0.dropFirst(5).prefix(11))" } ?? ""))
                            .font(.footnote).multilineTextAlignment(.trailing)
                    }
                }
                if let until = engine["api_fallback_until"] as? String {
                    LabeledContent("API fallback until") { Text(until).font(.footnote) }
                }
            }
            Section("Notifications") {
                TextField("This device's name", text: $cfg.deviceName)
                Picker("Push environment", selection: $cfg.pushEnvironment) {
                    Text("Sandbox (built from Xcode)").tag("sandbox")
                    Text("Production (TestFlight / App Store)").tag("production")
                }
                LabeledContent("Status") { Text(store.pushStatus).font(.footnote).multilineTextAlignment(.trailing) }
                if !cfg.deviceToken.isEmpty {
                    LabeledContent("Device token") { Text(cfg.deviceToken.prefix(12) + "…").font(.footnote.monospaced()) }
                }
                HStack {
                    Button("Register again") { Task { await store.registerDevice() } }
                    Button("Send test notification") { Task { pushResult = await store.testPush() } }
                }
                if !pushResult.isEmpty { Text(pushResult).font(.footnote).foregroundStyle(.secondary) }
            }
            Section {
                Text("While the app is running (on the Mac it stays in the menu bar), messages the agent sends arrive as notifications over its live connection; Apple push adds delivery when the app is closed or the phone is locked. Phone calls come from its Twilio number.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .task { engine = (try? await API(config: store.config).health())?["engine"] as? [String: Any] ?? [:] }
        .navigationTitle("Settings")
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss(); Task { await store.restart() } }
            }
        }
        #if os(iOS)
        .modifier(SettingsNav())
        #else
        .padding()
        #endif
    }
}

#if os(iOS)
private struct SettingsNav: ViewModifier {
    func body(content: Content) -> some View { NavigationStack { content } }
}
#endif

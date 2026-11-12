// Where the server is and how to prove who we are. The token lives in the Keychain, the rest in UserDefaults.
import Foundation
import Observation
import Security

@Observable
final class AppConfig {
    static let shared = AppConfig()

    private let d = UserDefaults.standard

    var baseURL: String { didSet { d.set(baseURL, forKey: "baseURL") } }
    var token: String { didSet { TokenStore.set(token) } }
    var deviceName: String { didSet { d.set(deviceName, forKey: "deviceName") } }
    /// production | sandbox. Debug builds from Xcode get sandbox tokens; TestFlight/App Store builds production.
    var pushEnvironment: String { didSet { d.set(pushEnvironment, forKey: "pushEnv") } }
    var deviceToken: String { didSet { d.set(deviceToken, forKey: "deviceToken") } }
    var registeredToken: String { didSet { d.set(registeredToken, forKey: "registeredToken") } }
    let clientID: String

    private init() {
        baseURL = d.string(forKey: "baseURL") ?? AppConfig.defaultBaseURL
        // `defaults write com.elityre.atlas token <EA_WEB_TOKEN>` is a scriptable way to configure the app; the value
        // is moved into the token store on first launch and removed from defaults.
        if let imported = d.string(forKey: "token"), !imported.isEmpty {
            TokenStore.set(imported)
            d.removeObject(forKey: "token")
        }
        token = TokenStore.get() ?? ""
        deviceName = d.string(forKey: "deviceName") ?? AppConfig.defaultDeviceName
        #if DEBUG
        pushEnvironment = d.string(forKey: "pushEnv") ?? "sandbox"
        #else
        pushEnvironment = d.string(forKey: "pushEnv") ?? "production"
        #endif
        deviceToken = d.string(forKey: "deviceToken") ?? ""
        registeredToken = d.string(forKey: "registeredToken") ?? ""
        if let id = d.string(forKey: "clientID") {
            clientID = id
        } else {
            clientID = UUID().uuidString
            d.set(clientID, forKey: "clientID")
        }
    }

    var isConfigured: Bool { !token.isEmpty && url != nil }

    /// `atlas://setup?url=<server>&token=<EA_WEB_TOKEN>`: one QR scan (or tap) configures a fresh install,
    /// so the token is never typed on the phone. Returns false for any other URL.
    @discardableResult
    func apply(setup incoming: URL) -> Bool {
        guard incoming.scheme == "atlas", incoming.host == "setup",
              let items = URLComponents(url: incoming, resolvingAgainstBaseURL: false)?.queryItems else { return false }
        var changed = false
        for item in items {
            guard let v = item.value, !v.isEmpty else { continue }
            if item.name == "url" { baseURL = v; changed = true }
            if item.name == "token" { token = v; changed = true }
        }
        return changed
    }

    var url: URL? {
        var s = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        guard !s.isEmpty, let u = URL(string: s), u.scheme != nil, u.host != nil else { return nil }
        return u
    }

    static var platform: String {
        #if os(macOS)
        return "macos"
        #else
        return "ios"
        #endif
    }

    private static var defaultBaseURL: String {
        #if os(macOS)
        return "http://127.0.0.1:8800"       // the server usually runs on this very Mac
        #else
        return ""
        #endif
    }

    private static var defaultDeviceName: String {
        #if os(macOS)
        return Host.current().localizedName ?? "Mac"
        #else
        return "iPhone"
        #endif
    }
}

/// Where the access token lives. iOS: the Keychain. macOS: a 0600 file in Application Support, because a build
/// signed ad hoc gets a new identity every time and the Keychain then asks for the login password on each launch
/// (Eli, 2026-11-06); the same token sits in a plain file on this Mac for the server anyway.
enum TokenStore {
    #if os(macOS)
    private static let url: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Atlas")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        return dir.appendingPathComponent("token")
    }()
    static func get() -> String? {
        (try? String(contentsOf: url, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    static func set(_ value: String) {
        if value.isEmpty { try? FileManager.default.removeItem(at: url); return }
        try? Data(value.utf8).write(to: url, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
    #else
    static func get() -> String? { Keychain.get("ea_web_token") }
    static func set(_ value: String) { Keychain.set(value, for: "ea_web_token") }
    #endif
}

enum Keychain {
    private static func query(_ key: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "com.elityre.atlas", kSecAttrAccount as String: key]
    }

    static func get(_ key: String) -> String? {
        var q = query(key)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func set(_ value: String, for key: String) {
        let q = query(key)
        SecItemDelete(q as CFDictionary)
        guard !value.isEmpty else { return }
        var add = q
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }
}

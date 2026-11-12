// A plain append-only log file (~/Library/Logs/Atlas/atlas.log) for the connection machinery, because the
// unified log drops info-level entries from an unsigned build. Small, rotated at 2 MB.
import Foundation

enum Diag {
    private static let url: URL = {
        let dir = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0].appendingPathComponent("Logs/Atlas")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("atlas.log")
    }()
    private static let q = DispatchQueue(label: "atlas.diag")
    private static let fmt: DateFormatter = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"; return f }()

    static func log(_ message: String) {
        let line = "\(fmt.string(from: Date())) \(message)\n"
        q.async {
            if let size = try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int, size > 2_000_000 {
                try? FileManager.default.removeItem(at: url)
            }
            if let h = try? FileHandle(forWritingTo: url) {
                h.seekToEndOfFile(); h.write(Data(line.utf8)); try? h.close()
            } else {
                try? Data(line.utf8).write(to: url)
            }
        }
    }
}

// Keeps the share extension on the same authenticated server as the main app.
import Foundation
import Security

struct ShareConnection: Codable {
    let serverURL: String
    let token: String

    var uploadURL: URL? {
        let base = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: base + "/api/audio/upload"),
              let scheme = url.scheme?.lowercased(), ["https", "http"].contains(scheme),
              url.host != nil, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil else { return nil }
        return url
    }
}

enum ShareConnectionStore {
    private static func query() throws -> [String: Any] {
        var request: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "io.pdf2podcast.share-connection",
            kSecAttrAccount as String: "server"
        ]
        if let group = Bundle.main.object(forInfoDictionaryKey: "Pdf2PodcastSharedKeychainGroup") as? String,
           !group.isEmpty, !group.contains("$(") {
            request[kSecAttrAccessGroup as String] = group
        }
        return request
    }

    static func load() throws -> ShareConnection? {
        var request = try query()
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw failure(status)
        }
        return try JSONDecoder().decode(ShareConnection.self, from: data)
    }

    static func save(_ connection: ShareConnection) throws {
        let request = try query()
        let attributes: [String: Any] = [
            kSecValueData as String: try JSONEncoder().encode(connection),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        var status = SecItemUpdate(request as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(request.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw failure(status) }
    }

    private static func failure(_ status: OSStatus) -> StoreError {
        StoreError(message: SecCopyErrorMessageString(status, nil) as String?
            ?? "Secure connection storage failed (\(status)).")
    }

    private struct StoreError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
}

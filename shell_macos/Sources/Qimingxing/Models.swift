import Foundation

enum AppConnectionPhase: String, Sendable {
    case starting
    case connected
    case offline
    case error
}

struct ProfileMeta: Identifiable, Hashable, Sendable {
    let name: String
    let label: String
    let provider: String
    let model: String
    let supportsVision: Bool

    var id: String { name }
}

struct BackgroundTaskItem: Identifiable, Equatable, Sendable {
    let taskId: String
    var title: String
    var status: String
    var progressPct: Int
    var currentStep: String
    var resultSummary: String
    var artifactPath: String
    var errorMessage: String

    var id: String { taskId }

    var isActive: Bool {
        status == "queued" || status == "running" || status == "waiting_approval"
    }
}

struct PendingApprovalItem: Identifiable, Equatable, Sendable {
    let approvalId: String
    let turnId: String
    let toolName: String
    let riskLevel: String
    let summary: String
    let details: [String: String]
    let options: [String]
    let isClarification: Bool
    let allowRemember: Bool
    let timeoutSec: Int

    var id: String { approvalId }
}

struct ArtifactCapsuleItem: Identifiable, Equatable, Sendable {
    let id: String
    let turnId: String
    let title: String
    let path: String
    let mediaType: String
    let actions: [String]
}

struct ChatEntry: Identifiable, Equatable, Sendable {
    let id: String
    let turnId: String
    let isUser: Bool
    var meta: String
    var text: String
    var isStreaming: Bool = false
}

struct StagedAttachment: Identifiable, Equatable, Sendable {
    let artifactId: String
    let filename: String
    let kind: String

    var id: String { artifactId }
}

struct ProviderViewModel: Identifiable, Equatable, Sendable {
    var name: String
    var label: String
    var protocolName: String
    var baseURL: String
    var authRequired: Bool
    var hasKey: Bool
    var keyMasked: String
    var models: [String]
    var draftApiKey: String = ""

    var id: String { name }
}

struct SlotRouteViewModel: Equatable, Sendable {
    var provider: String
    var model: String
    var voice: String = ""
}

struct MCPServerViewModel: Identifiable, Equatable, Sendable {
    var name: String
    var enabled: Bool
    var transport: String
    var command: String
    var args: [String]
    var url: String
    var authMode: String
    var safeTools: [String]
    var status: String
    var error: String
    var hasOAuthToken: Bool

    var id: String { name }
}

enum JSONHelper {
    static func dict(from data: Data) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    static func stringified(_ value: Any?) -> String {
        guard let value else { return "" }
        if let s = value as? String { return s }
        if let n = value as? NSNumber { return n.stringValue }
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
           let s = String(data: data, encoding: .utf8) {
            return s
        }
        return String(describing: value)
    }
}

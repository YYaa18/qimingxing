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

struct ModelCapabilityViewModel: Equatable, Sendable {
    var contextWindow: Int = 0
    var maxOutputTokens: Int = 0
    var supportsTools: Bool = true
    var supportsVision: Bool = false
    var supportsCache: Bool = false
    var supportsReasoning: Bool = false
    var isPreview: Bool = false
    var source: String = "manual"

    var payload: [String: Any] {
        [
            "context_window": contextWindow,
            "max_output_tokens": maxOutputTokens,
            "supports_tools": supportsTools,
            "supports_vision": supportsVision,
            "supports_cache": supportsCache,
            "supports_reasoning": supportsReasoning,
            "is_preview": isPreview,
            "source": source,
        ]
    }
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
    var modelCapabilities: [String: ModelCapabilityViewModel] = [:]
    var draftApiKey: String = ""
    var isPersisted: Bool = true

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

extension Notification.Name {
    static let qmxOpenWorkspace = Notification.Name("com.qimingxing.desktop.openWorkspace")
    static let qmxDetachWorkspace = Notification.Name("com.qimingxing.desktop.detachWorkspace")
    static let qmxCloseWorkspaceWing = Notification.Name("com.qimingxing.desktop.closeWorkspaceWing")
    static let qmxHideQuickEntry = Notification.Name("com.qimingxing.desktop.hideQuickEntry")
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


/// Removes control tags that the model uses internally but that should never
/// appear in the rendered answer (e.g. a leading `[EMOTION:happy]`).
func qmxCleanAssistantText(_ raw: String) -> String {
    var text = raw
    // Models occasionally emit zero-width characters inside the control tag,
    // so strip by delimiters instead of a strict character-class regex.
    while let start = text.range(of: "[EMOTION"),
          let end = text.range(of: "]", range: start.upperBound..<text.endIndex) {
        text.removeSubrange(start.lowerBound..<end.upperBound)
    }
    text = text.replacingOccurrences(of: "\u{200B}", with: "")
    text = text.replacingOccurrences(of: "\u{FEFF}", with: "")
    return text.trimmingCharacters(in: .whitespacesAndNewlines)
}

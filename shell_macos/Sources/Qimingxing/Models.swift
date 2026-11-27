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
    var blockReason: String = ""
    var watchdogReason: String = ""
    var followupAck: String = ""
    var resultRefs: [String] = []
    var bundlePath: String = ""
    var resultMedia: [MediaItem] = []

    var id: String { taskId }

    var isActive: Bool {
        status == "queued" || status == "planning" || status == "running"
            || status == "waiting_approval" || status == "waiting_input"
            || status == "verifying"
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

struct SourceItem: Codable, Identifiable, Equatable, Sendable {
    let title: String
    let url: String
    let domain: String

    var id: String { url }

    enum CodingKeys: String, CodingKey {
        case title, url, domain
    }

    init(title: String, url: String, domain: String = "") {
        self.title = title
        self.url = url
        self.domain = domain
    }

    init?(dict: [String: Any]) {
        guard let title = dict["title"] as? String,
              let url = dict["url"] as? String,
              let parsed = URL(string: url),
              parsed.scheme == "http" || parsed.scheme == "https" else { return nil }
        self.title = title
        self.url = url
        self.domain = (dict["domain"] as? String) ?? parsed.host ?? ""
    }
}

struct SheetPreviewItem: Codable, Equatable, Sendable {
    let name: String
    let columns: [String]
    let rows: [[String]]

    enum CodingKeys: String, CodingKey { case name, columns, rows }

    init(name: String, columns: [String] = [], rows: [[String]] = []) {
        self.name = name
        self.columns = columns
        self.rows = rows
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        columns = try c.decodeIfPresent([String].self, forKey: .columns) ?? []
        rows = try c.decodeIfPresent([[String]].self, forKey: .rows) ?? []
    }
}

struct MediaItem: Codable, Identifiable, Equatable, Sendable {
    let mediaId: String
    let kind: String
    let title: String
    let localPath: String
    let mimeType: String
    let width: Int
    let height: Int
    let durationSec: Double
    let sizeBytes: Int
    let sourceURL: String
    let sourceTitle: String
    let license: String
    let attribution: String
    let provider: String
    let fileExt: String
    let summaryBadge: String
    let previewColumns: [String]
    let previewRows: [[String]]
    let sheetNames: [String]
    let sheetPreviews: [SheetPreviewItem]

    var id: String { mediaId }
    var fileURL: URL? {
        localPath.isEmpty ? nil : URL(fileURLWithPath: localPath)
    }

    enum CodingKeys: String, CodingKey {
        case mediaId = "media_id"
        case kind, title
        case localPath = "local_path"
        case mimeType = "mime_type"
        case width, height
        case durationSec = "duration_sec"
        case sizeBytes = "size_bytes"
        case sourceURL = "source_url"
        case sourceTitle = "source_title"
        case license, attribution, provider
        case fileExt = "file_ext"
        case summaryBadge = "summary_badge"
        case previewColumns = "preview_columns"
        case previewRows = "preview_rows"
        case sheetNames = "sheet_names"
        case sheetPreviews = "sheet_previews"
    }

    init(mediaId: String, kind: String, title: String = "", localPath: String = "",
         mimeType: String = "", width: Int = 0, height: Int = 0,
         durationSec: Double = 0, sizeBytes: Int = 0,
         sourceURL: String = "", sourceTitle: String = "", license: String = "",
         attribution: String = "", provider: String = "", fileExt: String = "",
         summaryBadge: String = "", previewColumns: [String] = [],
         previewRows: [[String]] = [], sheetNames: [String] = [],
         sheetPreviews: [SheetPreviewItem] = []) {
        self.mediaId = mediaId
        self.kind = kind
        self.title = title
        self.localPath = localPath
        self.mimeType = mimeType
        self.width = width
        self.height = height
        self.durationSec = durationSec
        self.sizeBytes = sizeBytes
        self.sourceURL = sourceURL
        self.sourceTitle = sourceTitle
        self.license = license
        self.attribution = attribution
        self.provider = provider
        self.fileExt = fileExt
        self.summaryBadge = summaryBadge
        self.previewColumns = previewColumns
        self.previewRows = previewRows
        self.sheetNames = sheetNames
        self.sheetPreviews = sheetPreviews
    }

    init?(dict: [String: Any]) {
        guard let mediaId = dict["media_id"] as? String,
              let kind = dict["kind"] as? String else { return nil }
        self.mediaId = mediaId
        self.kind = kind
        self.title = (dict["title"] as? String) ?? ""
        self.localPath = (dict["local_path"] as? String) ?? ""
        self.mimeType = (dict["mime_type"] as? String) ?? ""
        self.width = (dict["width"] as? Int) ?? 0
        self.height = (dict["height"] as? Int) ?? 0
        self.durationSec = (dict["duration_sec"] as? Double) ?? 0
        self.sizeBytes = (dict["size_bytes"] as? Int) ?? 0
        self.sourceURL = (dict["source_url"] as? String) ?? ""
        self.sourceTitle = (dict["source_title"] as? String) ?? ""
        self.license = (dict["license"] as? String) ?? ""
        self.attribution = (dict["attribution"] as? String) ?? ""
        self.provider = (dict["provider"] as? String) ?? ""
        self.fileExt = (dict["file_ext"] as? String) ?? ""
        self.summaryBadge = (dict["summary_badge"] as? String) ?? ""
        self.previewColumns = (dict["preview_columns"] as? [String]) ?? []
        self.previewRows = (dict["preview_rows"] as? [[String]]) ?? []
        self.sheetNames = (dict["sheet_names"] as? [String]) ?? []
        self.sheetPreviews = (dict["sheet_previews"] as? [[String: Any]])?.compactMap { raw in
            guard let name = raw["name"] as? String else { return nil }
            return SheetPreviewItem(
                name: name,
                columns: (raw["columns"] as? [String]) ?? [],
                rows: (raw["rows"] as? [[String]]) ?? []
            )
        } ?? []
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mediaId = try c.decode(String.self, forKey: .mediaId)
        kind = try c.decode(String.self, forKey: .kind)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        localPath = try c.decodeIfPresent(String.self, forKey: .localPath) ?? ""
        mimeType = try c.decodeIfPresent(String.self, forKey: .mimeType) ?? ""
        width = try c.decodeIfPresent(Int.self, forKey: .width) ?? 0
        height = try c.decodeIfPresent(Int.self, forKey: .height) ?? 0
        durationSec = try c.decodeIfPresent(Double.self, forKey: .durationSec) ?? 0
        sizeBytes = try c.decodeIfPresent(Int.self, forKey: .sizeBytes) ?? 0
        sourceURL = try c.decodeIfPresent(String.self, forKey: .sourceURL) ?? ""
        sourceTitle = try c.decodeIfPresent(String.self, forKey: .sourceTitle) ?? ""
        license = try c.decodeIfPresent(String.self, forKey: .license) ?? ""
        attribution = try c.decodeIfPresent(String.self, forKey: .attribution) ?? ""
        provider = try c.decodeIfPresent(String.self, forKey: .provider) ?? ""
        fileExt = try c.decodeIfPresent(String.self, forKey: .fileExt) ?? ""
        summaryBadge = try c.decodeIfPresent(String.self, forKey: .summaryBadge) ?? ""
        previewColumns = try c.decodeIfPresent([String].self, forKey: .previewColumns) ?? []
        previewRows = try c.decodeIfPresent([[String]].self, forKey: .previewRows) ?? []
        sheetNames = try c.decodeIfPresent([String].self, forKey: .sheetNames) ?? []
        sheetPreviews = try c.decodeIfPresent([SheetPreviewItem].self, forKey: .sheetPreviews) ?? []
    }
}

struct ChatEntry: Identifiable, Equatable, Sendable {
    let id: String
    let turnId: String
    let isUser: Bool
    var meta: String
    var text: String
    var isStreaming: Bool = false
    var sources: [SourceItem] = []
    var media: [MediaItem] = []
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
    static let qmxStopMediaPlayback = Notification.Name("com.qimingxing.desktop.stopMediaPlayback")
    static let qmxToggleMediaPlayback = Notification.Name("com.qimingxing.desktop.toggleMediaPlayback")
    static let qmxMediaViewerClosed = Notification.Name("com.qimingxing.desktop.mediaViewerClosed")
    static let qmxFocusComposer = Notification.Name("com.qimingxing.desktop.focusComposer")
    static let qmxToggleWalkieTalkie = Notification.Name("com.qimingxing.desktop.toggleWalkieTalkie")
    static let qmxStartSpatialLasso = Notification.Name("com.qimingxing.desktop.startSpatialLasso")
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

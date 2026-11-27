import AppKit
import Combine
import SwiftUI

// MARK: - Model

enum WorkspaceKind: String, Codable, CaseIterable {
    case answer
    case task
    case artifact

    var label: String {
        switch self {
        case .answer: return "回答"
        case .task: return "任务"
        case .artifact: return "产物"
        }
    }

    var icon: String {
        switch self {
        case .answer: return "bubble.left"
        case .task: return "clock"
        case .artifact: return "doc.richtext"
        }
    }
}

struct WorkspaceItem: Identifiable, Codable, Equatable, Sendable {
    var id: String
    var kind: WorkspaceKind
    var title: String
    var prompt: String
    var body: String
    var status: String
    var progress: Int
    var path: String
    var createdAt: Date
    var updatedAt: Date
    var sources: [SourceItem]? = nil
    var media: [MediaItem]? = nil
    var cards: [OutputCardItem]? = nil
    var bundlePath: String? = nil
}

// MARK: - Store

@MainActor
final class WorkspaceStore: ObservableObject {
    @Published private(set) var items: [WorkspaceItem] = []

    private let brainClient: BrainClient?
    private let fileURL: URL?
    private var cancellables = Set<AnyCancellable>()
    private var persistTask: Task<Void, Never>?
    private var messageSignatures: [String: MessageSignature] = [:]
    private let persistQueue = DispatchQueue(
        label: "com.qimingxing.workspace.persist",
        qos: .utility
    )

    init(brainClient: BrainClient) {
        self.brainClient = brainClient
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("Qimingxing", isDirectory: true)
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        self.fileURL = support.appendingPathComponent("workspace.json")
        load()
        observe()
    }

    init(previewItems: [WorkspaceItem]) {
        self.brainClient = nil
        self.fileURL = nil
        self.items = previewItems.sorted { $0.updatedAt > $1.updatedAt }
    }

    private func observe() {
        guard let brainClient else { return }
        brainClient.$messages
            .sink { [weak self] messages in self?.ingestMessages(messages) }
            .store(in: &cancellables)
        brainClient.$backgroundTasks
            .sink { [weak self] tasks in self?.ingestTasks(tasks) }
            .store(in: &cancellables)
        brainClient.$artifactCapsules
            .sink { [weak self] artifacts in self?.ingestArtifacts(artifacts) }
            .store(in: &cancellables)
    }

    private struct TurnSnapshot {
        let id: String
        var prompt: String = ""
        var answer: String = ""
        var streaming: Bool = false
        var sources: [SourceItem] = []
        var media: [MediaItem] = []
        var cards: [OutputCardItem] = []
    }

    private struct MessageSignature: Equatable {
        let textCount: Int
        let streaming: Bool
        let sources: [SourceItem]
        let media: [MediaItem]
        let cards: [OutputCardItem]

        init(_ message: ChatEntry) {
            textCount = message.text.count
            streaming = message.isStreaming
            sources = message.sources
            media = message.media
            cards = message.cards
        }
    }

    private func ingestMessages(_ messages: [ChatEntry]) {
        guard !messages.isEmpty else {
            messageSignatures = [:]
            return
        }
        // Turn 1 may finish while Turn 2 is already streaming, so "last turn"
        // is not enough: detect every message whose text/status/media changed.
        var dirtyTurnIds: Set<String> = []
        var nextSignatures: [String: MessageSignature] = [:]
        nextSignatures.reserveCapacity(messages.count)
        for message in messages {
            let signature = MessageSignature(message)
            nextSignatures[message.id] = signature
            if messageSignatures[message.id] != signature {
                dirtyTurnIds.insert(message.turnId)
            }
        }
        messageSignatures = nextSignatures
        guard !dirtyTurnIds.isEmpty else { return }

        var order: [String] = []
        var map: [String: TurnSnapshot] = [:]
        for message in messages where dirtyTurnIds.contains(message.turnId) {
            if map[message.turnId] == nil {
                order.append(message.turnId)
                map[message.turnId] = TurnSnapshot(id: message.turnId)
            }
            if message.isUser {
                map[message.turnId]?.prompt = message.text
            } else {
                map[message.turnId]?.answer = qmxCleanAssistantText(message.text)
                map[message.turnId]?.streaming = message.isStreaming
                map[message.turnId]?.sources = message.sources
                map[message.turnId]?.media = message.media
                map[message.turnId]?.cards = message.cards
            }
        }

        for id in order {
            guard let turn = map[id] else { continue }
            guard !turn.prompt.isEmpty || !turn.answer.isEmpty || !turn.cards.isEmpty else { continue }
            let itemID = "turn_\(id)"
            let existing = items.first(where: { $0.id == itemID })
            let titleSource = turn.prompt.trimmingCharacters(in: .whitespacesAndNewlines)
            let title = titleSource.isEmpty ? "对话" : String(titleSource.prefix(80))
            let hasContent = !turn.answer.isEmpty || !turn.cards.isEmpty
            let status = hasContent ? (turn.streaming ? "streaming" : "completed") : "waiting"
            if let existing,
               existing.prompt == turn.prompt,
               existing.body == turn.answer,
               existing.status == status,
               existing.sources == turn.sources,
               existing.media == turn.media,
               (existing.cards ?? []) == turn.cards {
                continue
            }
            upsert(WorkspaceItem(
                id: itemID,
                kind: .answer,
                title: title,
                prompt: turn.prompt,
                body: turn.answer,
                status: status,
                progress: 0,
                path: "",
                createdAt: existing?.createdAt ?? Date(),
                updatedAt: Date(),
                sources: turn.sources.isEmpty ? nil : turn.sources,
                media: turn.media.isEmpty ? nil : turn.media,
                cards: turn.cards.isEmpty ? nil : turn.cards
            ))
        }
    }

    func sendCardAction(_ card: OutputCardItem, action: CardActionItem, turnItemID: String) {
        guard let brainClient else { return }
        let turnId = turnItemID.hasPrefix("turn_")
            ? String(turnItemID.dropFirst("turn_".count))
            : turnItemID
        brainClient.sendCardAction(cardId: card.cardId, action: action.id, turnId: turnId)
    }

    /// Card tables are deduped out of the media strip; the notice inside the
    /// card opens the full viewer, preferring the brain media twin.
    func openCardTable(
        _ table: QMXMarkdownTable,
        media: [MediaItem]?,
        contextTitle: String
    ) {
        let signature = table.dedupeSignature
        if let match = (media ?? []).first(where: {
            $0.isMarkdownTable
                && QMXTableDedupe.signature(
                    columns: $0.previewColumns,
                    rows: $0.previewRows
                ) == signature
        }) {
            MediaViewerLauncher.open(
                items: [match],
                selectedID: match.mediaId,
                title: match.title.isEmpty ? "表格" : match.title,
                contextLabel: contextTitle,
                prefersLargeWindow: true
            )
            return
        }
        let fallback = table.asMediaItem(
            id: "card_table_\(table.id)",
            contextTitle: contextTitle
        )
        MediaViewerLauncher.open(
            items: [fallback],
            selectedID: fallback.mediaId,
            title: fallback.title,
            contextLabel: contextTitle,
            prefersLargeWindow: true
        )
    }

    private func ingestTasks(_ tasks: [BackgroundTaskItem]) {
        for task in tasks {
            let itemID = "task_\(task.taskId)"
            let existing = items.first(where: { $0.id == itemID })
            var parts: [String] = []
            if !task.resultSummary.isEmpty {
                parts.append(task.resultSummary)
            } else if !task.currentStep.isEmpty {
                parts.append(task.currentStep)
            }
            if !task.blockReason.isEmpty {
                parts.append(task.blockReason)
            }
            if !task.followupAck.isEmpty {
                parts.append(task.followupAck)
            }
            if !task.watchdogReason.isEmpty && task.blockReason != task.watchdogReason {
                parts.append(task.watchdogReason)
            }
            let body = parts.joined(separator: " · ")
            let media = task.resultMedia.isEmpty ? nil : task.resultMedia
            let bundle = task.bundlePath.isEmpty ? nil : task.bundlePath
            if let existing,
               existing.title == task.title,
               existing.body == body,
               existing.status == task.status,
               existing.progress == task.progressPct,
               existing.path == task.artifactPath,
               existing.media == media,
               existing.bundlePath == bundle {
                continue
            }
            upsert(WorkspaceItem(
                id: itemID,
                kind: .task,
                title: task.title,
                prompt: "",
                body: body,
                status: task.status,
                progress: task.progressPct,
                path: task.artifactPath,
                createdAt: existing?.createdAt ?? Date(),
                updatedAt: Date(),
                media: media,
                bundlePath: bundle
            ))
        }
    }

    private func ingestArtifacts(_ artifacts: [ArtifactCapsuleItem]) {
        for artifact in artifacts {
            let itemID = "artifact_\(artifact.id)"
            let existing = items.first(where: { $0.id == itemID })
            if let existing,
               existing.title == artifact.title,
               existing.path == artifact.path {
                continue
            }
            upsert(WorkspaceItem(
                id: itemID,
                kind: .artifact,
                title: artifact.title,
                prompt: "",
                body: artifact.path,
                status: "ready",
                progress: 100,
                path: artifact.path,
                createdAt: existing?.createdAt ?? Date(),
                updatedAt: Date()
            ))
        }
    }

    private func upsert(_ item: WorkspaceItem) {
        if let index = items.firstIndex(where: { $0.id == item.id }) {
            var merged = item
            merged.createdAt = items[index].createdAt
            items[index] = merged
        } else {
            items.insert(item, at: 0)
        }
        items.sort { $0.updatedAt > $1.updatedAt }
        if items.count > 500 {
            items = Array(items.prefix(500))
        }
        schedulePersist()
    }

    private func schedulePersist() {
        guard fileURL != nil else { return }
        persistTask?.cancel()
        persistTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard !Task.isCancelled, let self else { return }
            self.persistNow()
        }
    }

    private func persistNow() {
        guard let fileURL else { return }
        let snapshot = items
        // Encoding ~500 items (hundreds of KB) on the main actor caused a
        // visible hitch at the end of long turns; a serial queue keeps writes
        // ordered without blocking UI.
        persistQueue.async {
            Self.write(snapshot, to: fileURL)
        }
    }

    /// Flush pending debounced/queued writes before the app terminates.
    func flushOnTerminate() {
        guard let fileURL else { return }
        persistTask?.cancel()
        persistTask = nil
        let snapshot = items
        persistQueue.sync {
            Self.write(snapshot, to: fileURL)
        }
    }

    nonisolated private static func write(_ snapshot: [WorkspaceItem], to fileURL: URL) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(snapshot) else { return }
        try? data.write(to: fileURL, options: .atomic)
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: fileURL.path
        )
    }

    private func load() {
        guard let fileURL, let data = try? Data(contentsOf: fileURL) else { return }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        items = ((try? decoder.decode([WorkspaceItem].self, from: data)) ?? []).map { item in
            var cleaned = item
            if cleaned.kind == .answer {
                cleaned.body = qmxCleanAssistantText(cleaned.body)
                cleaned.prompt = qmxCleanAssistantText(cleaned.prompt)
            }
            return cleaned
        }
        items.sort { $0.updatedAt > $1.updatedAt }
    }
}

// MARK: - Conversation threads

/// Answers are stored one row per turn, but the workspace presents them the way
/// people read them: one continuous conversation per day, with tasks and
/// artifacts kept as separate cards.
struct WorkspaceThread: Identifiable {
    let id: String
    let day: Date
    var turns: [WorkspaceItem]

    var startedAt: Date { turns.first?.createdAt ?? day }
    var updatedAt: Date { turns.map(\.updatedAt).max() ?? day }
    var turnCount: Int { turns.count }

    var lastPrompt: String {
        turns.last(where: { !$0.prompt.isEmpty })?.prompt ?? "对话"
    }

    var preview: String {
        guard let body = turns.last(where: { !$0.body.isEmpty })?.body else {
            return turns.last?.status == "waiting" ? "等待回复…" : ""
        }
        let flat = body
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first(where: { !$0.isEmpty }) ?? ""
        return flat
    }

    var isStreaming: Bool {
        turns.last?.status == "streaming"
    }

    static func group(_ answers: [WorkspaceItem]) -> [WorkspaceThread] {
        let calendar = Calendar.current
        let ordered = answers.sorted { $0.createdAt < $1.createdAt }
        var buckets: [Date: [WorkspaceItem]] = [:]
        for item in ordered {
            let day = calendar.startOfDay(for: item.createdAt)
            buckets[day, default: []].append(item)
        }
        return buckets.keys.sorted(by: >).map { day in
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyyMMdd"
            return WorkspaceThread(
                id: "thread_\(formatter.string(from: day))",
                day: day,
                turns: buckets[day, default: []]
            )
        }
    }
}

enum WorkspaceEntry: Identifiable {
    case thread(WorkspaceThread)
    case item(WorkspaceItem)

    var id: String {
        switch self {
        case .thread(let thread): return thread.id
        case .item(let item): return item.id
        }
    }

    var updatedAt: Date {
        switch self {
        case .thread(let thread): return thread.updatedAt
        case .item(let item): return item.updatedAt
        }
    }
}

// MARK: - Window

@MainActor
final class WorkspaceWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private var windowLease: WindowLease?

    var isVisible: Bool { window?.isVisible ?? false }
    private let store: WorkspaceStore
    private let brainClient: BrainClient
    private let windowCoordinator: WindowCoordinator?

    init(
        store: WorkspaceStore,
        brainClient: BrainClient,
        windowCoordinator: WindowCoordinator? = nil
    ) {
        self.store = store
        self.brainClient = brainClient
        self.windowCoordinator = windowCoordinator
        super.init()
    }

    func show(anchoredTo anchor: NSRect? = nil, movePanelBy: ((CGFloat) -> Void)? = nil) {
        ensureWindow()
        positionWindow(anchoredTo: anchor, movePanelBy: movePanelBy)
        // Decision: the full workspace docks beside QuickEntry instead of
        // suspending it; it only takes the key.
        windowLease = windowCoordinator?.present(
            WindowRequest(role: .workspaceWindow, activation: .required)
        )
        if windowCoordinator == nil {
            NSApp.activate(ignoringOtherApps: true)
        }
        window?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        if let lease = windowLease, let windowCoordinator {
            windowCoordinator.dismiss(lease)
        }
        windowLease = nil
    }

    func orderOutForSuspend() {
        window?.orderOut(nil)
    }

    func restoreFromSuspend() {
        if windowCoordinator?.keyOwner == .workspaceWindow {
            window?.makeKeyAndOrderFront(nil)
        } else {
            window?.orderFront(nil)
        }
    }

    /// Opens the workspace beside the Quick Entry panel when there is room,
    /// otherwise below/above it, and only falls back to centering when the
    /// panel is not visible.
    private func positionWindow(anchoredTo anchor: NSRect?, movePanelBy: ((CGFloat) -> Void)?) {
        guard let window else { return }
        let size = window.frame.size
        guard let anchor, anchor.width > 0, anchor.height > 0 else {
            window.center()
            return
        }

        let screen = NSScreen.screens.first {
            $0.frame.contains(NSPoint(x: anchor.midX, y: anchor.midY))
        } ?? NSScreen.main
        guard let visible = screen?.visibleFrame else {
            window.center()
            return
        }

        let gap: CGFloat = 14
        let candidates = [
            // Right of the dialog, top edges aligned.
            NSRect(x: anchor.maxX + gap, y: anchor.maxY - size.height,
                   width: size.width, height: size.height),
            // Left of the dialog.
            NSRect(x: anchor.minX - gap - size.width, y: anchor.maxY - size.height,
                   width: size.width, height: size.height),
            // Below, horizontally centered.
            NSRect(x: anchor.midX - size.width / 2, y: anchor.minY - gap - size.height,
                   width: size.width, height: size.height),
            // Above, horizontally centered.
            NSRect(x: anchor.midX - size.width / 2, y: anchor.maxY + gap,
                   width: size.width, height: size.height),
        ]

        if let fit = candidates.first(where: { visible.contains($0) }) {
            window.setFrame(fit, display: true)
            return
        }

        // No room next to the centered dialog: slide the dialog aside and
        // dock the workspace on the freed side. This keeps both visible on
        // screens that cannot host 760pt + 920pt side by side while centered.
        let total = anchor.width + gap + size.width
        if total <= visible.width {
            let panelX = visible.maxX - total
            let dx = panelX - anchor.minX
            let shiftedRight = NSRect(
                x: panelX + anchor.width + gap,
                y: anchor.maxY - size.height,
                width: size.width,
                height: size.height
            )
            var frame = shiftedRight
            frame.origin.y = min(max(frame.origin.y, visible.minY), visible.maxY - size.height)
            movePanelBy?(dx)
            window.setFrame(frame, display: true)
            return
        }

        var frame = candidates[0]
        frame.origin.x = min(max(frame.origin.x, visible.minX), visible.maxX - size.width)
        frame.origin.y = min(max(frame.origin.y, visible.minY), visible.maxY - size.height)
        window.setFrame(frame, display: true)
    }

    private func ensureWindow() {
        if window != nil { return }
        let root = WorkspaceRootView(store: store, brainClient: brainClient)
        let hosting = NSHostingController(rootView: root)
        let win = NSWindow(contentViewController: hosting)
        win.title = "启明星工作区"
        win.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        win.titlebarAppearsTransparent = true
        win.titleVisibility = .hidden
        win.isMovableByWindowBackground = true
        win.setContentSize(NSSize(width: 920, height: 620))
        win.minSize = NSSize(width: 820, height: 540)
        win.center()
        win.isReleasedWhenClosed = false
        win.delegate = self
        self.window = win
    }
}

// MARK: - View

enum WorkspaceFilter: String, CaseIterable, Identifiable {
    case today = "今天"
    case all = "全部"
    case conversations = "对话"
    case tasks = "任务"
    case artifacts = "产物"

    var id: Self { self }

    var icon: String {
        switch self {
        case .today: return "sun.max"
        case .all: return "tray.full"
        case .conversations: return "bubble.left.and.bubble.right"
        case .tasks: return "clock"
        case .artifacts: return "doc.richtext"
        }
    }
}

struct WorkspaceRootView: View {
    @ObservedObject var store: WorkspaceStore
    @ObservedObject var brainClient: BrainClient

    @State private var filter: WorkspaceFilter = .today
    @State private var search = ""
    @State private var selectedID: String?

    var body: some View {
        HStack(spacing: 0) {
            filterSidebar
            Divider().opacity(0.5)
            timeline
            Divider().opacity(0.5)
            detail
        }
        .frame(minWidth: 820, minHeight: 540)
        .background(QMXTheme.window)
        .onAppear {
            if selectedID == nil {
                selectedID = filteredEntries.first?.id
            }
        }
        .onChange(of: filter) { _ in
            selectedID = filteredEntries.first?.id
        }
        .onChange(of: search) { _ in
            selectedID = filteredEntries.first?.id
        }
    }

    // MARK: Sidebar

    private var filterSidebar: some View {
        ZStack {
            VisualEffectBlur(material: .sidebar, blendingMode: .behindWindow)
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 8) {
                    MorningStarMark(size: 15, color: QMXTheme.textPrimary)
                    Text("工作区")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(QMXTheme.textPrimary)
                    Spacer()
                }
                .padding(.horizontal, 18)
                .padding(.top, 38)
                .padding(.bottom, 16)

                ForEach(WorkspaceFilter.allCases) { item in
                    filterRow(item)
                }
                Spacer(minLength: 0)
            }
        }
        .frame(width: 190)
    }

    private func filterRow(_ item: WorkspaceFilter) -> some View {
        let selected = filter == item
        let count = entries(for: item).count
        return Button {
            filter = item
        } label: {
            HStack(spacing: 9) {
                Image(systemName: item.icon)
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(selected ? QMXTheme.textPrimary : QMXTheme.textSecondary)
                    .frame(width: 18)
                Text(item.rawValue)
                    .font(.system(size: 13))
                    .foregroundStyle(selected ? QMXTheme.textPrimary : QMXTheme.textSecondary)
                Spacer()
                Text("\(count)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .monospacedDigit()
            }
            .padding(.horizontal, 10)
            .frame(height: 36)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(selected ? QMXTheme.surfaceRaised : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 8)
    }

    // MARK: Timeline

    private var timeline: some View {
        VStack(spacing: 0) {
            TextField("搜索工作区", text: $search)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12))
                .padding(12)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(groupedEntries, id: \.day) { group in
                        Text(dayLabel(group.day))
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(QMXTheme.textTertiary)
                            .padding(.horizontal, 12)
                            .padding(.top, 10)
                            .padding(.bottom, 4)

                        ForEach(group.entries) { entry in
                            timelineRow(entry)
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 12)
            }
            .scrollIndicators(.hidden)
        }
        .frame(width: 300)
        .background(QMXTheme.surface.opacity(0.25))
    }

    private func timelineRow(_ entry: WorkspaceEntry) -> some View {
        let selected = selectedID == entry.id
        return Button {
            selectedID = entry.id
        } label: {
            HStack(alignment: .top, spacing: 9) {
                Text(timeLabel(entry.updatedAt))
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .frame(width: 38, alignment: .leading)
                    .padding(.top, 2)

                Image(systemName: entryIcon(entry))
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(selected ? QMXTheme.accent : QMXTheme.textSecondary)
                    .frame(width: 18)
                    .padding(.top, 1)

                VStack(alignment: .leading, spacing: 3) {
                    Text(entryTitle(entry))
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(QMXTheme.textPrimary)
                        .lineLimit(1)
                    Text(entrySubtitle(entry))
                        .font(.system(size: 11))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8)
            .frame(height: 54)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(selected ? QMXTheme.surfaceRaised : .clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .strokeBorder(selected ? QMXTheme.hairline : .clear, lineWidth: 0.5)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func entryIcon(_ entry: WorkspaceEntry) -> String {
        switch entry {
        case .thread: return "bubble.left.and.bubble.right"
        case .item(let item): return item.kind.icon
        }
    }

    private func entryTitle(_ entry: WorkspaceEntry) -> String {
        switch entry {
        case .thread(let thread):
            return thread.lastPrompt.isEmpty ? "对话" : thread.lastPrompt
        case .item(let item):
            return item.title
        }
    }

    private func entrySubtitle(_ entry: WorkspaceEntry) -> String {
        switch entry {
        case .thread(let thread):
            let head = "\(thread.turnCount) 轮"
            if thread.isStreaming { return head + " · 生成中…" }
            let preview = thread.preview
            return preview.isEmpty ? head : head + " · " + preview
        case .item(let item):
            return item.body.isEmpty ? statusLabel(item.status) : item.body
        }
    }

    // MARK: Detail

    @ViewBuilder
    private var detail: some View {
        if let entry = filteredEntries.first(where: { $0.id == selectedID }) ?? filteredEntries.first {
            switch entry {
            case .thread(let thread):
                threadDetail(thread)
            case .item(let item):
                itemDetail(item)
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("工作区")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                Text("对话、后台任务和产物会自动出现在这里。")
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textSecondary)
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }

    private func scrollThreadToBottom(_ proxy: ScrollViewProxy) {
        // LazyVStack may not have realized the last turn on first appearance.
        // Scroll a sentinel after the next layout pass instead of only to the
        // last lazy row (which can silently no-op).
        DispatchQueue.main.async {
            proxy.scrollTo("workspace-thread-bottom", anchor: .bottom)
        }
    }

    private func threadDetail(_ thread: WorkspaceThread) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(spacing: 6) {
                        Image(systemName: "bubble.left.and.bubble.right")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(QMXTheme.textSecondary)
                        Text("对话")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(QMXTheme.textSecondary)
                        Text(dayLabel(thread.day))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(QMXTheme.textTertiary)
                        Text("· \(thread.turnCount) 轮")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(QMXTheme.textTertiary)
                        Spacer()
                    }

                    Text("\(dayLabel(thread.day))的对话")
                        .font(.system(size: 22, weight: .semibold))
                        .foregroundStyle(QMXTheme.textPrimary)

                    LazyVStack(alignment: .leading, spacing: 26) {
                        ForEach(thread.turns) { turn in
                            turnView(turn)
                                .id(turn.id)
                        }
                    }
                    .padding(.top, 4)

                    Color.clear
                        .frame(height: 1)
                        .id("workspace-thread-bottom")
                }
                .padding(24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollIndicators(.hidden)
            .onAppear {
                scrollThreadToBottom(proxy)
            }
            .onChange(of: thread.id) { _ in
                scrollThreadToBottom(proxy)
            }
            .onChange(of: thread.turns.last?.body) { _ in
                scrollThreadToBottom(proxy)
            }
            .onChange(of: thread.turns.last?.media?.count) { _ in
                scrollThreadToBottom(proxy)
            }
            .onChange(of: thread.updatedAt) { _ in
                scrollThreadToBottom(proxy)
            }
        }
    }

    private func turnView(_ turn: WorkspaceItem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(timeLabel(turn.createdAt))
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                if !turn.prompt.isEmpty {
                    Text(turn.prompt)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }

            if turn.body.isEmpty && (turn.cards?.isEmpty ?? true) {
                Text(turn.status == "waiting" ? "等待回复…" : statusLabel(turn.status))
                    .font(.system(size: 13))
                    .foregroundStyle(QMXTheme.textTertiary)
            } else {
                if !turn.body.isEmpty {
                    MarkdownMessageView(text: turn.body)
                }
                if let cards = turn.cards, !cards.isEmpty {
                    ForEach(cards) { card in
                        QMXOutputCardView(
                            card: card,
                            onOpenTable: { table in
                                store.openCardTable(
                                    table,
                                    media: turn.media,
                                    contextTitle: "工作区"
                                )
                            }
                        ) { action in
                            store.sendCardAction(card, action: action, turnItemID: turn.id)
                        }
                    }
                }
                if let sources = turn.sources, !sources.isEmpty {
                    SourceChipsView(sources: sources)
                }
                if let media = turn.media {
                    let stripMedia = qmxMediaWithoutInlineTables(
                        media, answer: turn.body, cards: turn.cards ?? []
                    )
                    if !stripMedia.isEmpty {
                        MediaStripView(media: stripMedia, mode: .expanded, contextTitle: "工作区")
                    }
                }
            }
        }
        .padding(.leading, 12)
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 1, style: .continuous)
                .fill(QMXTheme.hairline)
                .frame(width: 2)
        }
    }

    private func itemDetail(_ item: WorkspaceItem) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 6) {
                    Image(systemName: item.kind.icon)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                    Text(item.kind.label)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                    Text(dateLabel(item.updatedAt))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(QMXTheme.textTertiary)
                    Spacer()
                }

                Text(item.title)
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)

                itemBody(item)
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollIndicators(.hidden)
    }

    @ViewBuilder
    private func itemBody(_ item: WorkspaceItem) -> some View {
        switch item.kind {
        case .answer:
            if !item.body.isEmpty {
                MarkdownMessageView(text: item.body)
            }
            if let cards = item.cards, !cards.isEmpty {
                ForEach(cards) { card in
                    QMXOutputCardView(
                        card: card,
                        onOpenTable: { table in
                            store.openCardTable(
                                table,
                                media: item.media,
                                contextTitle: "工作区"
                            )
                        }
                    ) { action in
                        store.sendCardAction(card, action: action, turnItemID: item.id)
                    }
                }
            }
            if let sources = item.sources, !sources.isEmpty {
                SourceChipsView(sources: sources)
            }
            if let media = item.media, !media.isEmpty {
                MediaStripView(media: media, mode: .expanded)
            }

        case .task:
            HStack(spacing: 10) {
                Text(statusLabel(item.status))
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                Text("\(item.progress)%")
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .monospacedDigit()
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(QMXTheme.surfaceMuted).frame(height: 2)
                    Capsule()
                        .fill(QMXTheme.accent)
                        .frame(width: max(2, geo.size.width * CGFloat(item.progress) / 100.0), height: 2)
                }
            }
            .frame(height: 2)

            if !item.body.isEmpty {
                Text(item.body)
                    .font(.system(size: 13))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .qmxCard(fill: QMXTheme.surfaceRaised)
            }
            if let media = item.media, !media.isEmpty {
                MediaStripView(media: media, mode: .expanded, contextTitle: "任务产物")
            }
            artifactActions(item)

        case .artifact:
            Text(item.path)
                .font(.system(size: 12, design: .monospaced))
                .foregroundStyle(QMXTheme.textSecondary)
                .textSelection(.enabled)
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .qmxCard(fill: QMXTheme.surfaceRaised)
            artifactActions(item)
        }
    }

    @ViewBuilder
    private func artifactActions(_ item: WorkspaceItem) -> some View {
        let bundlePath = item.bundlePath ?? ""
        if !item.path.isEmpty || !bundlePath.isEmpty {
            HStack(spacing: 8) {
                if !bundlePath.isEmpty {
                    Button("打开产物文件夹") { _ = DesktopActions().revealPath(bundlePath) }
                        .buttonStyle(.qmxSecondary)
                }
                if !item.path.isEmpty {
                    Button("快速预览") { _ = DesktopActions().quickLook(item.path) }
                        .buttonStyle(.qmxSecondary)
                    Button("复制文件") { _ = DesktopActions().copyFile(item.path) }
                        .buttonStyle(.qmxSecondary)
                    Button("在访达中显示") { _ = DesktopActions().revealPath(item.path) }
                        .buttonStyle(.qmxSecondary)
                }
            }
        }
    }

    // MARK: Filtering

    private var allThreads: [WorkspaceThread] {
        WorkspaceThread.group(store.items.filter { $0.kind == .answer })
    }

    private var allEntries: [WorkspaceEntry] {
        let threads = allThreads.map(WorkspaceEntry.thread)
        let others = store.items
            .filter { $0.kind != .answer }
            .map(WorkspaceEntry.item)
        return (threads + others).sorted { $0.updatedAt > $1.updatedAt }
    }

    private func entries(for filter: WorkspaceFilter) -> [WorkspaceEntry] {
        switch filter {
        case .today:
            return allEntries.filter { Calendar.current.isDateInToday($0.updatedAt) }
        case .all:
            return allEntries
        case .conversations:
            return allThreads.map(WorkspaceEntry.thread)
        case .tasks:
            return store.items
                .filter { $0.kind == .task }
                .sorted { $0.updatedAt > $1.updatedAt }
                .map(WorkspaceEntry.item)
        case .artifacts:
            return store.items
                .filter { $0.kind == .artifact }
                .sorted { $0.updatedAt > $1.updatedAt }
                .map(WorkspaceEntry.item)
        }
    }

    private var filteredEntries: [WorkspaceEntry] {
        let needle = search.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else { return entries(for: filter) }
        return entries(for: filter).filter { entry in
            switch entry {
            case .thread(let thread):
                return thread.turns.contains { turn in
                    turn.title.lowercased().contains(needle)
                        || turn.prompt.lowercased().contains(needle)
                        || turn.body.lowercased().contains(needle)
                }
            case .item(let item):
                return item.title.lowercased().contains(needle)
                    || item.prompt.lowercased().contains(needle)
                    || item.body.lowercased().contains(needle)
                    || item.path.lowercased().contains(needle)
            }
        }
    }

    private struct TimelineDay: Identifiable {
        let day: Date
        let entries: [WorkspaceEntry]
        var id: Date { day }
    }

    private var groupedEntries: [TimelineDay] {
        let calendar = Calendar.current
        let grouped = Dictionary(grouping: filteredEntries) {
            calendar.startOfDay(for: $0.updatedAt)
        }
        return grouped.keys.sorted(by: >).map { day in
            TimelineDay(
                day: day,
                entries: grouped[day, default: []].sorted { $0.updatedAt > $1.updatedAt }
            )
        }
    }

    private func dayLabel(_ day: Date) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(day) { return "今天" }
        if calendar.isDateInYesterday(day) { return "昨天" }
        let formatter = DateFormatter()
        formatter.dateFormat = "MM月dd日"
        return formatter.string(from: day)
    }

    private func timeLabel(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }

    private func dateLabel(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "MM-dd HH:mm"
        return formatter.string(from: date)
    }

    private func statusLabel(_ status: String) -> String {
        switch status {
        case "queued": return "排队中"
        case "running": return "运行中"
        case "waiting_approval": return "等待审批"
        case "completed": return "已完成"
        case "failed": return "失败"
        case "cancelled": return "已取消"
        case "interrupted": return "已中断"
        case "waiting": return "等待回复"
        case "streaming": return "生成中"
        case "ready": return "已就绪"
        default: return status
        }
    }
}

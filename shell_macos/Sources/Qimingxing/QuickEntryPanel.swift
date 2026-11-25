import AppKit
import Combine
import SwiftUI
import UniformTypeIdentifiers

@MainActor
final class FloatingQuickEntryPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    var onEscapePressed: (() -> Void)?

    override func cancelOperation(_ sender: Any?) {
        onEscapePressed?()
    }
}

@MainActor
final class QuickEntryController: NSObject, NSWindowDelegate {
    private static let commandWidth: CGFloat = 760
    static let wingWidth: CGFloat = 380
    private static let idleHeight: CGFloat = 112
    private static let thinkingHeight: CGFloat = 236
    private static let answeringHeight: CGFloat = 520
    private static let wingMinHeight: CGFloat = 430

    private var panel: FloatingQuickEntryPanel?
    private weak var previousApp: NSRunningApplication?
    private let brainClient: BrainClient
    private let desktopActions: DesktopActions
    private let workspaceStore: WorkspaceStore
    private let inputState = QuickEntryInputState()
    private var cancellables = Set<AnyCancellable>()
    private var preWingOriginX: CGFloat?
    /// Stable chat height reported by the view. The wing is a display-layer
    /// modifier and must never overwrite this value.
    private var lastChatHeight: CGFloat?

    init(
        brainClient: BrainClient,
        desktopActions: DesktopActions,
        workspaceStore: WorkspaceStore
    ) {
        self.brainClient = brainClient
        self.desktopActions = desktopActions
        self.workspaceStore = workspaceStore
        super.init()
        wireExpansion()
        inputState.$isWorkspaceWingOpen
            .removeDuplicates()
            .sink { [weak self] open in self?.layoutWing(open: open) }
            .store(in: &cancellables)
    }

    private var panelWidth: CGFloat {
        Self.commandWidth + (inputState.isWorkspaceWingOpen ? Self.wingWidth : 0)
    }

    func showWorkspaceWing() {
        if !isVisible { show() }
        inputState.isWorkspaceWingOpen = true
    }

    func hideWorkspaceWing() {
        inputState.isWorkspaceWingOpen = false
    }

    func detachWorkspace() {
        inputState.isWorkspaceWingOpen = false
        NotificationCenter.default.post(name: .qmxDetachWorkspace, object: nil)
    }


    private func layoutWing(open: Bool) {
        guard let panel, panel.isVisible else { return }
        let screen = panel.screen ?? NSScreen.main
        let visible = screen?.visibleFrame ?? panel.frame
        let width = Self.commandWidth + (open ? Self.wingWidth : 0)
        var frame = panel.frame
        let topEdge = frame.maxY
        let chatHeight = lastChatHeight ?? frame.height
        let height = open ? max(chatHeight, Self.wingMinHeight) : chatHeight

        if open, preWingOriginX == nil {
            preWingOriginX = frame.origin.x
            if frame.origin.x + width > visible.maxX - 8 {
                frame.origin.x = max(visible.minX + 8, visible.maxX - width - 8)
            }
        } else if !open, let restoreX = preWingOriginX {
            frame.origin.x = restoreX
            preWingOriginX = nil
        }

        frame.size.width = width
        frame.size.height = height
        frame.origin.y = topEdge - height
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            panel.setFrame(frame, display: true)
        } else {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.24
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }
        }
    }

    var isVisible: Bool {
        panel?.isVisible ?? false
    }

    var visiblePanelFrame: NSRect? {
        guard let panel, panel.isVisible else { return nil }
        return panel.frame
    }

    private func wireExpansion() {
        inputState.$forceExpanded
            .sink { [weak self] _ in self?.refreshExpansion() }
            .store(in: &cancellables)
        brainClient.$isGenerating
            .sink { [weak self] _ in self?.refreshExpansion() }
            .store(in: &cancellables)
        brainClient.$messages
            .sink { [weak self] messages in
                guard let self else { return }
                if !messages.isEmpty { self.inputState.forceExpanded = false }
                self.refreshExpansion()
            }
            .store(in: &cancellables)
    }

    private func refreshExpansion() {
        let expanded = inputState.forceExpanded
            || brainClient.isGenerating
            || !brainClient.messages.isEmpty

        inputState.isExpanded = expanded
    }

    /// The last turn already has visible text -> answering layout; otherwise
    /// we are still waiting on the first token.
    private var lastTurnHasAnswer: Bool {
        guard let turnID = brainClient.messages.last?.turnId else { return false }
        return brainClient.messages.contains {
            $0.turnId == turnID && !$0.isUser && !$0.text.isEmpty
        }
    }

    private var desiredPanelHeight: CGFloat {
        guard inputState.isExpanded else { return Self.idleHeight }
        return lastTurnHasAnswer ? Self.answeringHeight : Self.thinkingHeight
    }

    func toggle() {
        if isVisible {
            hideAndRestoreFocus()
        } else {
            show()
        }
    }

    func show() {
        if let front = NSWorkspace.shared.frontmostApplication,
           front.bundleIdentifier != Bundle.main.bundleIdentifier {
            previousApp = front
        }
        ensurePanel()
        guard let panel else { return }
        refreshExpansion()
        resizePanel(animated: false)
        positionPanel()
        panel.makeKeyAndOrderFront(nil)
        panel.makeKey()
        inputState.focusRequestToken = UUID()
        inputState.layoutToken = UUID()

        DispatchQueue.main.async { [weak panel] in
            panel?.makeKey()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak panel] in
            guard let panel, panel.isVisible, !panel.isKeyWindow else { return }
            NSApp.activate(ignoringOtherApps: true)
            panel.makeKey()
        }
    }

    func hideAndRestoreFocus() {
        inputState.isWorkspaceWingOpen = false
        panel?.orderOut(nil)
        if let prev = previousApp, !prev.isTerminated {
            prev.activate(options: [])
        }
        previousApp = nil
    }

    private func ensurePanel() {
        if panel != nil { return }
        let p = FloatingQuickEntryPanel(
            contentRect: NSRect(x: 0, y: 0, width: Self.commandWidth, height: Self.idleHeight),
            styleMask: [.nonactivatingPanel, .titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        p.title = "启明星"
        p.titleVisibility = .hidden
        p.titlebarAppearsTransparent = true
        // Spotlight-style panel: Esc is the only close affordance. Hide the
        // standard traffic lights so no window chrome appears at top-left.
        p.standardWindowButton(.closeButton)?.isHidden = true
        p.standardWindowButton(.miniaturizeButton)?.isHidden = true
        p.standardWindowButton(.zoomButton)?.isHidden = true
        p.isFloatingPanel = true
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.delegate = self
        p.onEscapePressed = { [weak self] in self?.hideAndRestoreFocus() }

        let rootView = QuickEntryRootView(
            brainClient: brainClient,
            desktopActions: desktopActions,
            workspaceStore: workspaceStore,
            inputState: inputState,
            onClose: { [weak self] in self?.hideAndRestoreFocus() },
            onCloseWing: { [weak self] in self?.hideWorkspaceWing() },
            onSplitWing: { [weak self] in self?.detachWorkspace() },
            onDesiredHeightChange: { [weak self] height in
                self?.resize(toContentHeight: height)
            }
        )
        p.contentViewController = NSHostingController(rootView: rootView)
        self.panel = p
    }

    func shiftHorizontally(by dx: CGFloat) {
        guard let panel, abs(dx) > 0.5 else { return }
        var frame = panel.frame
        frame.origin.x += dx
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            panel.setFrame(frame, display: true)
        } else {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.24
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }
        }
    }

    func resize(toContentHeight height: CGFloat) {
        // Always cache the chat height, even while the panel is hidden.
        let chatHeight = min(Self.answeringHeight, max(112, height))
        lastChatHeight = chatHeight
        guard let panel, panel.isVisible else { return }
        let target = inputState.isWorkspaceWingOpen
            ? max(chatHeight, Self.wingMinHeight)
            : chatHeight
        let delta = abs(panel.frame.height - target)
        guard delta > 3 else { return }
        var frame = panel.frame
        let topEdge = frame.maxY
        frame.size = NSSize(width: panelWidth, height: target)
        frame.origin.y = topEdge - target

        // Large jumps happen on phase changes and are animated; small
        // line-by-line growth is applied directly so overlapping window
        // animations cannot stutter.
        if delta > 80 && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.20
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }
        } else {
            panel.setFrame(frame, display: true)
        }
    }

    private func resizePanel(animated: Bool) {
        guard let panel else { return }
        let chatHeight = lastChatHeight ?? desiredPanelHeight
        let height = inputState.isWorkspaceWingOpen
            ? max(chatHeight, Self.wingMinHeight)
            : chatHeight
        guard abs(panel.frame.height - height) > 0.5 else { return }
        var frame = panel.frame
        let topEdge = frame.maxY
        frame.size = NSSize(width: panelWidth, height: height)
        frame.origin.y = topEdge - height

        if animated && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.24
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }
        } else {
            panel.setFrame(frame, display: true)
        }
    }

    private func positionPanel() {
        guard let panel, let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let visible = screen.visibleFrame
        let height = panel.frame.height
        let x = visible.midX - panelWidth / 2
        let y = visible.maxY - height - 120
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        hideAndRestoreFocus()
        return false
    }
}

@MainActor
final class QuickEntryInputState: ObservableObject {
    @Published var text: String = ""
    @Published var includeScreen: Bool = false
    @Published var stagedAttachments: [StagedAttachment] = []
    @Published var focusRequestToken: UUID = UUID()
    @Published var isExpanded: Bool = false
    @Published var forceExpanded: Bool = false
    @Published var isWorkspaceWingOpen: Bool = false
    @Published var layoutToken: UUID = UUID()
}

struct QuickEntryRootView: View {
    @ObservedObject var brainClient: BrainClient
    let desktopActions: DesktopActions
    @ObservedObject var workspaceStore: WorkspaceStore
    @ObservedObject var inputState: QuickEntryInputState
    let onClose: () -> Void
    var onCloseWing: () -> Void = {}
    var onSplitWing: () -> Void = {}
    var onDesiredHeightChange: (CGFloat) -> Void = { _ in }

    @AppStorage("qmx.answerFontSize") private var answerFontSize: Double = 16
    @State private var appeared = QMXTheme.isHeadlessDump
    @State private var answerContentHeight: CGFloat = 0
    @State private var finalAnswerHeight: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let chatWidth: CGFloat = 760
    private static let wingWidth: CGFloat = 380
    private static let answerWidth: CGFloat = 696

    var body: some View {
        ZStack {
            VisualEffectBlur(material: .popover, blendingMode: .behindWindow)

            HStack(spacing: 0) {
                chatColumn

                if inputState.isWorkspaceWingOpen {
                    Divider().opacity(0.5)
                    WorkspaceWingView(
                        store: workspaceStore,
                        desktopActions: desktopActions,
                        onSplit: onSplitWing,
                        onClose: onCloseWing
                    )
                    .frame(width: Self.wingWidth)
                    .transition(.move(edge: .trailing).combined(with: .opacity))
                }
            }
            .animation(.easeOut(duration: 0.20), value: inputState.isWorkspaceWingOpen)
        }
        .frame(
            width: Self.chatWidth + (inputState.isWorkspaceWingOpen ? Self.wingWidth : 0)
        )
        .clipShape(RoundedRectangle(cornerRadius: QMXTheme.commandRadius, style: .continuous))
        .overlay(HairlineBorderOverlay(cornerRadius: QMXTheme.commandRadius))
        .overlay(
            AmbientRimLight(
                isActive: brainClient.isGenerating,
                cornerRadius: QMXTheme.commandRadius
            )
        )
        .scaleEffect(appeared ? 1.0 : 0.96)
        .opacity(appeared ? 1.0 : 0.0)
        .onAppear {
            if reduceMotion {
                appeared = true
            } else {
                withAnimation(.spring(response: 0.32, dampingFraction: 0.84)) {
                    appeared = true
                }
            }
            onDesiredHeightChange(chatHeight)
        }
        .onChange(of: chatHeight) { height in
            onDesiredHeightChange(height)
        }
        .onChange(of: inputState.layoutToken) { _ in
            onDesiredHeightChange(chatHeight)
        }
        .onChange(of: latestTurn?.id) { _ in
            answerContentHeight = 0
            finalAnswerHeight = 0
        }
        .onChange(of: answerFontSize) { _ in
            answerContentHeight = 0
            finalAnswerHeight = 0
        }
    }

    private var chatColumn: some View {
        VStack(spacing: 0) {
            inputRow

            if inputState.isExpanded {
                Divider().opacity(0.5)
                liveStage
                    .transition(.opacity.combined(with: .offset(y: 4)))
                Divider().opacity(0.5)
            } else {
                Spacer(minLength: 0)
            }

            footer
        }
        .frame(width: Self.chatWidth)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(alignment: .topLeading) {
            if let turn = latestTurn, !turn.answer.isEmpty {
                TypewriterText(
                    text: turn.answer,
                    fontSize: CGFloat(answerFontSize),
                    turnID: "measure-\(turn.id)-\(Int(answerFontSize))",
                    fullText: true
                )
                .frame(width: Self.answerWidth, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .background(
                    GeometryReader { geo in
                        Color.clear.preference(
                            key: ChatAnswerHeightKey.self,
                            value: geo.size.height
                        )
                    }
                )
                .opacity(0)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
            }
        }
        .onPreferenceChange(ChatAnswerHeightKey.self) { answerContentHeight = $0 }
    }

    // MARK: Input

    private var inputRow: some View {
        HStack(spacing: 14) {
            MorningStarMark(size: 20, color: QMXTheme.textPrimary)
                .padding(.leading, 2)

            ChineseIMETextView(
                text: $inputState.text,
                focusToken: inputState.focusRequestToken,
                fontSize: 22,
                placeholder: "问启明星，或搜索工作区…",
                onSubmit: { submitMessage() },
                onEscape: { onClose() },
                onPasteImage: { data in
                    Task { await uploadPastedImage(data) }
                }
            )
            .frame(maxWidth: .infinity, minHeight: 68, maxHeight: 68)

            HStack(spacing: 10) {
                Image(systemName: "mic")
                    .font(.system(size: 16, weight: .regular))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .help("按住右 Option 说话")

                Button(action: onClose) {
                    Text("esc")
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundStyle(QMXTheme.textTertiary)
                }
                .buttonStyle(.plain)
                .help("关闭 (Esc)")
            }
        }
        .padding(.horizontal, 20)
        .frame(height: 68)
    }

    // MARK: Live stage

    private var liveStage: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let turn = latestTurn, !turn.prompt.isEmpty {
                Text(turn.prompt)
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .lineLimit(1)
            }

            if isThinking {
                HStack(spacing: 10) {
                    GlobeSpinner(size: 20, color: QMXTheme.textSecondary)
                    Text(brainClient.toolStatusText.isEmpty ? "正在思考与调度…" : brainClient.toolStatusText)
                        .font(.system(size: 22, weight: .regular))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
            } else if let turn = latestTurn, !turn.answer.isEmpty {
                answerStream(turn)
                workspaceLink
            } else {
                Text("…")
                    .font(.system(size: 22))
                    .foregroundStyle(QMXTheme.textTertiary)
                Spacer(minLength: 0)
            }
        }
        .padding(.horizontal, 32)
        .padding(.vertical, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    /// Full answer, never truncated. Scrolling is owned by a child view so the
    /// per-character reveal does not re-evaluate the whole panel.
    private func answerStream(_ turn: LiveTurn) -> some View {
        AnswerStreamView(
            text: turn.answer,
            fontSize: CGFloat(answerFontSize),
            turnID: turn.id,
            width: Self.answerWidth,
            isStreaming: turn.isStreaming,
            onFinalHeight: { finalAnswerHeight = $0 }
        )
    }

    private var workspaceLink: some View {
        Button {
            NotificationCenter.default.post(name: .qmxOpenWorkspace, object: nil)
        } label: {
            HStack(spacing: 4) {
                Text("完整记录已放入工作区")
                Image(systemName: "arrow.right")
            }
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(QMXTheme.textSecondary)
        }
        .buttonStyle(.plain)
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 10) {
            Menu {
                ForEach(brainClient.profiles) { profile in
                    Button {
                        brainClient.setActiveProfile(profile.name)
                    } label: {
                        if profile.name == brainClient.activeProfile {
                            Label(profile.label.isEmpty ? profile.name : profile.label, systemImage: "checkmark")
                        } else {
                            Text(profile.label.isEmpty ? profile.name : profile.label)
                        }
                    }
                }
            } label: {
                HStack(spacing: 4) {
                    Text(activeModelLabel)
                        .font(.system(size: 12))
                        .foregroundStyle(QMXTheme.textSecondary)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(QMXTheme.textTertiary)
                }
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()

            EmotionBadge(emotion: brainClient.emotion)

            Spacer()

            Button {
                pickAttachmentFile()
            } label: {
                Image(systemName: "paperclip")
                    .font(.system(size: 13, weight: .medium))
            }
            .buttonStyle(.qmxIcon)
            .help("添加文件")

            Button {
                inputState.includeScreen.toggle()
            } label: {
                Image(systemName: inputState.includeScreen ? "display.and.arrow.down" : "display")
                    .font(.system(size: 13, weight: .medium))
            }
            .buttonStyle(.qmxIcon)
            .foregroundStyle(inputState.includeScreen ? QMXTheme.accent : QMXTheme.textSecondary)
            .help("看屏")

            Button {
                brainClient.clearHistory()
                inputState.forceExpanded = false
            } label: {
                Image(systemName: "square.and.pencil")
                    .font(.system(size: 13, weight: .medium))
            }
            .buttonStyle(.qmxIcon)
            .help("新对话")

            if brainClient.isGenerating {
                Button {
                    brainClient.cancelActiveTurn()
                } label: {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 11, weight: .bold))
                        .frame(width: 28, height: 28)
                        .background(Circle().fill(QMXTheme.danger))
                        .foregroundStyle(.white)
                }
                .buttonStyle(TactileButtonStyle(haptic: .generic))
                .help("停止")
            } else {
                Button(action: submitMessage) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 12, weight: .bold))
                        .frame(width: 28, height: 28)
                        .background(Circle().fill(canSend ? QMXTheme.primaryFill : QMXTheme.surfaceMuted))
                        .foregroundStyle(canSend ? QMXTheme.primaryText : QMXTheme.textTertiary)
                }
                .buttonStyle(TactileButtonStyle(haptic: .generic))
                .disabled(!canSend)
                .help("发送 (⏎)")
            }
        }
        .padding(.horizontal, 20)
        .frame(height: 36)
    }

    // MARK: Data

    private struct LiveTurn {
        let id: String
        var prompt: String = ""
        var answer: String = ""
        var isStreaming: Bool = false
    }

    /// Chrome above/below the answer stage: 68 input + 2 dividers + 36 footer.
    private static let panelChrome: CGFloat = 106
    private static let minExpandedHeight: CGFloat = 236
    private static let maxExpandedHeight: CGFloat = 520

    /// Pure chat-column height. The wing floor is applied by the window
    /// controller and never written back into this value.
    private var chatHeight: CGFloat {
        if !inputState.isExpanded {
            return 112
        }
        if isThinking || latestTurn?.answer.isEmpty != false {
            return Self.minExpandedHeight
        }
        let measured = max(answerContentHeight, finalAnswerHeight)
        guard measured > 0 else {
            return Self.minExpandedHeight
        }
        let promptBlock: CGFloat = latestTurn?.prompt.isEmpty == false ? 28 : 0
        let stage = 40 + promptBlock + measured + 26
        return min(Self.maxExpandedHeight, max(200, Self.panelChrome + stage))
    }

    private var latestTurn: LiveTurn? {
        var order: [String] = []
        var map: [String: LiveTurn] = [:]
        for message in brainClient.messages {
            if map[message.turnId] == nil {
                order.append(message.turnId)
                map[message.turnId] = LiveTurn(id: message.turnId)
            }
            if message.isUser {
                map[message.turnId]?.prompt = message.text
            } else {
                map[message.turnId]?.answer = qmxCleanAssistantText(message.text)
                map[message.turnId]?.isStreaming = message.isStreaming
            }
        }
        return order.last.flatMap { map[$0] }
    }

    private var hasAnswer: Bool {
        !(latestTurn?.answer.isEmpty ?? true)
    }

    private var isThinking: Bool {
        brainClient.isGenerating && !hasAnswer
    }

    private var activeModelLabel: String {
        if let profile = brainClient.profiles.first(where: { $0.name == brainClient.activeProfile }) {
            return profile.label.isEmpty ? profile.name : profile.label
        }
        return brainClient.activeProfile.isEmpty ? "未选择模型" : brainClient.activeProfile
    }

    private var canSend: Bool {
        brainClient.phase == .connected &&
        (!inputState.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
         !inputState.stagedAttachments.isEmpty)
    }

    private func submitMessage() {
        let text = inputState.text
        let attachments = inputState.stagedAttachments
        let withScreen = inputState.includeScreen
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty else {
            return
        }
        inputState.forceExpanded = true
        inputState.text = ""
        inputState.stagedAttachments = []
        brainClient.sendText(text, attachments: attachments, includeScreen: withScreen)
    }

    private func pickAttachmentFile() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        if panel.runModal() == .OK {
            for url in panel.urls {
                Task {
                    if let attachment = await brainClient.uploadArtifact(fileURL: url) {
                        inputState.stagedAttachments.append(attachment)
                    }
                }
            }
        }
    }

    private func uploadPastedImage(_ data: Data) async {
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("qmx_paste_\(UUID().uuidString.prefix(8)).png")
        try? data.write(to: tmp)
        if let attachment = await brainClient.uploadArtifact(fileURL: tmp) {
            inputState.stagedAttachments.append(attachment)
        }
        try? FileManager.default.removeItem(at: tmp)
    }
}

private struct ChatAnswerHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

private struct StreamHeightBox {
    var value: CGFloat = 0
}

private struct StreamFinalHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

private struct AnswerStreamView: View {
    let text: String
    let fontSize: CGFloat
    let turnID: String
    let width: CGFloat
    let isStreaming: Bool
    let onFinalHeight: (CGFloat) -> Void

    @State private var finished = false
    @State private var didReportFinal = false
    @State private var heightBox = StreamHeightBox()

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    TypewriterText(
                        text: text,
                        fontSize: fontSize,
                        turnID: turnID,
                        onFinish: { finished = true }
                    )
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .id("answerTop")

                    Color.clear
                        .frame(height: 1)
                        .id("answerBottom")
                }
                .frame(width: width, alignment: .leading)
                .background(
                    GeometryReader { geo in
                        Color.clear.preference(
                            key: StreamFinalHeightKey.self,
                            value: geo.size.height
                        )
                    }
                )
            }
            .scrollIndicators(.hidden)
            .onPreferenceChange(StreamFinalHeightKey.self) { height in
                heightBox.value = height
                if finished && !didReportFinal {
                    didReportFinal = true
                    onFinalHeight(height)
                }
            }
            .onAppear {
                proxy.scrollTo("answerTop", anchor: .top)
            }
            .onChange(of: turnID) { _ in
                finished = false
                didReportFinal = false
                heightBox.value = 0
                proxy.scrollTo("answerTop", anchor: .top)
            }
            .onChange(of: isStreaming) { streaming in
                if !streaming {
                    proxy.scrollTo("answerTop", anchor: .top)
                }
            }
            .onChange(of: text) { _ in
                finished = false
                didReportFinal = false
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct TypewriterText: View {
    let text: String
    let fontSize: CGFloat
    let turnID: String
    var onTick: () -> Void = {}
    var onFinish: () -> Void = {}
    var fullText: Bool = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var visibleCount = 0
    @State private var revealTask: Task<Void, Never>?
    @State private var blocks: [StageBlock]

    init(
        text: String,
        fontSize: CGFloat,
        turnID: String,
        onTick: @escaping () -> Void = {},
        onFinish: @escaping () -> Void = {},
        fullText: Bool = false
    ) {
        self.text = text
        self.fontSize = fontSize
        self.turnID = turnID
        self.onTick = onTick
        self.onFinish = onFinish
        self.fullText = fullText
        let parsed = Self.parse(text)
        _blocks = State(initialValue: parsed)
        let total = parsed.reduce(0) { $0 + $1.text.count }
        _visibleCount = State(initialValue: (QMXTheme.isHeadlessDump || fullText) ? total : 0)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(blocks) { block in
                let shown = min(max(0, visibleCount - block.start), block.text.count)
                if shown > 0 {
                    blockView(block, String(block.text.prefix(shown)))
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear { start() }
        .onChange(of: text) { _ in
            blocks = Self.parse(text)
            start()
        }
        .onChange(of: turnID) { _ in
            blocks = Self.parse(text)
            visibleCount = 0
            start()
        }
        .onDisappear { revealTask?.cancel() }
    }

    private var codeFontSize: CGFloat {
        max(12, (fontSize * 0.72).rounded())
    }

    @ViewBuilder
    private func blockView(_ block: StageBlock, _ visible: String) -> some View {
        if block.isCode {
            Text(visible)
                .font(.system(size: codeFontSize, design: .monospaced))
                .lineSpacing(2)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(Color.primary.opacity(0.05))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                )
        } else {
            Text(visible)
                .font(.system(size: fontSize, weight: .regular))
                .lineSpacing(3)
                .foregroundStyle(QMXTheme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var totalCount: Int {
        blocks.reduce(0) { $0 + $1.text.count }
    }

    private func start() {
        if fullText || reduceMotion {
            visibleCount = totalCount
            return
        }
        if visibleCount > totalCount {
            visibleCount = 0
        }
        revealTask?.cancel()
        // Reveal a few characters per display frame (~16ms) instead of
        // relaying out the text hundreds of times per second.
        let step = max(1, (totalCount + 179) / 180)
        revealTask = Task { @MainActor in
            while visibleCount < totalCount {
                try? await Task.sleep(nanoseconds: 16_000_000)
                if Task.isCancelled { return }
                visibleCount = min(totalCount, visibleCount + step)
                onTick()
            }
            onFinish()
        }
    }

    private static func parse(_ raw: String) -> [StageBlock] {
        var parsed: [StageBlock] = []
        var proseLines: [String] = []
        var codeLines: [String] = []
        var inCode = false
        var language = ""

        func flushProse() {
            let joined = proseLines
                .joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty {
                parsed.append(StageBlock(
                    id: parsed.count,
                    isCode: false,
                    language: "",
                    text: joined,
                    start: 0
                ))
            }
            proseLines.removeAll()
        }

        for line in raw.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if inCode {
                    let code = codeLines.joined(separator: "\n")
                    if !code.isEmpty {
                        parsed.append(StageBlock(
                            id: parsed.count,
                            isCode: true,
                            language: language,
                            text: code,
                            start: 0
                        ))
                    }
                    codeLines.removeAll()
                    language = ""
                    inCode = false
                } else {
                    flushProse()
                    inCode = true
                    language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                }
                continue
            }
            if inCode {
                codeLines.append(line)
            } else if trimmed.isEmpty {
                flushProse()
            } else {
                proseLines.append(
                    trimmed
                        .replacingOccurrences(of: "**", with: "")
                        .replacingOccurrences(of: "`", with: "")
                )
            }
        }

        if inCode {
            let code = codeLines.joined(separator: "\n")
            if !code.isEmpty {
                parsed.append(StageBlock(
                    id: parsed.count,
                    isCode: true,
                    language: language,
                    text: code,
                    start: 0
                ))
            }
        } else {
            flushProse()
        }

        if parsed.isEmpty, !raw.isEmpty {
            parsed.append(StageBlock(id: 0, isCode: false, language: "", text: raw, start: 0))
        }

        var offset = 0
        return parsed.map { block in
            var block = block
            block.start = offset
            offset += block.text.count
            return block
        }
    }
}

private struct StageBlock: Identifiable {
    let id: Int
    let isCode: Bool
    let language: String
    let text: String
    var start: Int
}

// MARK: - Native composer (IME-safe)

private struct ChineseIMETextView: NSViewRepresentable {
    @Binding var text: String
    let focusToken: UUID
    let fontSize: CGFloat
    let placeholder: String
    let onSubmit: () -> Void
    let onEscape: () -> Void
    let onPasteImage: (Data) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = false
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.verticalScrollElasticity = .none

        let tv = QMXTextView()
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.allowsUndo = true
        tv.font = NSFont.systemFont(ofSize: fontSize)
        tv.textColor = .labelColor
        tv.insertionPointColor = .labelColor
        tv.drawsBackground = false
        tv.isEditable = true
        tv.isSelectable = true
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainerInset = NSSize(width: 0, height: 20)
        tv.textContainer?.lineFragmentPadding = 0
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        tv.minSize = NSSize(width: 0, height: 68)
        tv.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        tv.frame = NSRect(x: 0, y: 0, width: 560, height: 68)
        tv.placeholder = placeholder
        tv.onSubmit = onSubmit
        tv.onEscape = onEscape
        tv.onPasteImage = onPasteImage

        scroll.documentView = tv
        context.coordinator.textView = tv
        return scroll
    }

    func updateNSView(_ nsView: NSScrollView, context: Context) {
        guard let tv = context.coordinator.textView else { return }

        if tv.font?.pointSize != fontSize {
            tv.font = NSFont.systemFont(ofSize: fontSize)
            tv.needsDisplay = true
        }
        if !tv.hasMarkedText() && tv.string != text {
            tv.string = text
            tv.needsDisplay = true
        }
        tv.placeholder = placeholder
        tv.onSubmit = onSubmit
        tv.onEscape = onEscape
        tv.onPasteImage = onPasteImage

        let width = nsView.contentSize.width
        if width > 0, abs(tv.frame.width - width) > 0.5 {
            tv.frame = NSRect(x: 0, y: 0, width: width, height: max(68, tv.frame.height))
            tv.textContainer?.containerSize = NSSize(
                width: width,
                height: CGFloat.greatestFiniteMagnitude
            )
        }

        if context.coordinator.lastFocusToken != focusToken {
            context.coordinator.lastFocusToken = focusToken
            DispatchQueue.main.async { [weak tv] in
                guard let tv, let window = tv.window else { return }
                window.makeFirstResponder(tv)
            }
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ChineseIMETextView
        weak var textView: QMXTextView?
        var lastFocusToken: UUID = UUID()

        init(_ parent: ChineseIMETextView) {
            self.parent = parent
        }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            tv.needsDisplay = true
            guard !tv.hasMarkedText() else { return }
            parent.text = tv.string
        }
    }
}

private final class QMXTextView: NSTextView {
    var onSubmit: (() -> Void)?
    var onEscape: (() -> Void)?
    var onPasteImage: ((Data) -> Void)?
    var placeholder: String = "" {
        didSet { needsDisplay = true }
    }

    override func keyDown(with event: NSEvent) {
        if hasMarkedText() {
            super.keyDown(with: event)
            return
        }
        if event.keyCode == 53 { // Esc
            onEscape?()
            return
        }
        if (event.keyCode == 36 || event.keyCode == 76) && !event.modifierFlags.contains(.shift) {
            onSubmit?()
            return
        }
        super.keyDown(with: event)
        needsDisplay = true
    }

    override func didChangeText() {
        super.didChangeText()
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty else { return }
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font ?? NSFont.systemFont(ofSize: 22),
            .foregroundColor: NSColor.tertiaryLabelColor
        ]
        (placeholder as NSString).draw(at: textContainerOrigin, withAttributes: attrs)
    }

    override func paste(_ sender: Any?) {
        let pb = NSPasteboard.general
        if let img = NSImage(pasteboard: pb),
           let tiff = img.tiffRepresentation,
           let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            onPasteImage?(png)
            return
        }
        super.paste(sender)
    }
}

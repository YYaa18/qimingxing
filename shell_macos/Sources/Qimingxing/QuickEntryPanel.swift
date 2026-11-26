import AppKit
import Combine
import SwiftUI
import UniformTypeIdentifiers

enum QuickEntryLayout {
    static let chatWidth: CGFloat = 760
    static let wingWidth: CGFloat = 380
    static let answerWidth: CGFloat = 696
    static let inputHeight: CGFloat = 68
    static let footerHeight: CGFloat = 48
    static let idleHeight: CGFloat = 120       // 68 input + 4 breathing + 48 footer
    static let thinkingHeight: CGFloat = 248
    static let answeringHeight: CGFloat = 520
    static let wingMinHeight: CGFloat = 430
    static let panelChrome: CGFloat = inputHeight + 2 + footerHeight // 118
}

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
    static let wingWidth: CGFloat = QuickEntryLayout.wingWidth

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
    private var targetChatHeight: CGFloat = QuickEntryLayout.idleHeight
    private var currentAnimatedHeight: CGFloat = QuickEntryLayout.idleHeight
    private var heightVelocity: CGFloat = 0
    private var heightDriverTask: Task<Void, Never>?
    private var focusObservers: [NSObjectProtocol] = []
    private var hiddenForMediaViewer = false

    var debugPanel: FloatingQuickEntryPanel? { panel }
    var debugInputState: QuickEntryInputState { inputState }

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
        installFocusRecovery()
        inputState.$isWorkspaceWingOpen
            .removeDuplicates()
            .sink { [weak self] open in self?.layoutWing(open: open) }
            .store(in: &cancellables)
    }

    /// Auxiliary windows (settings, workspace, media viewer) can take key
    /// status and never hand it back. The panel then looks focused but swallows
    /// no keystrokes. Re-key it when the app becomes active or another app
    /// window closes.
    private func installFocusRecovery() {
        let center = NotificationCenter.default
        focusObservers.append(
            center.addObserver(
                forName: NSWindow.willCloseNotification,
                object: nil,
                queue: .main
            ) { [weak self] note in
                guard let self,
                      let closing = note.object as? NSWindow,
                      closing !== self.panel
                else { return }
                Task { @MainActor [weak self] in
                    try? await Task.sleep(nanoseconds: 120_000_000)
                    guard let self,
                          let panel = self.panel,
                          panel.isVisible
                    else { return }
                    guard NSApp.keyWindow == nil || NSApp.keyWindow === panel else { return }
                    panel.makeKeyAndOrderFront(nil)
                    self.inputState.focusRequestToken = UUID()
                    NotificationCenter.default.post(name: .qmxFocusComposer, object: nil)
                }
            }
        )
        focusObservers.append(
            center.addObserver(
                forName: NSApplication.didBecomeActiveNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in
                    guard let self,
                          let panel = self.panel,
                          panel.isVisible,
                          !panel.isKeyWindow
                    else { return }
                    panel.makeKey()
                    self.inputState.focusRequestToken = UUID()
                    NotificationCenter.default.post(name: .qmxFocusComposer, object: nil)
                }
            }
        )
    }

    private var panelWidth: CGFloat {
        QuickEntryLayout.chatWidth + (inputState.isWorkspaceWingOpen ? QuickEntryLayout.wingWidth : 0)
    }

    func showWorkspaceWing() {
        if !isVisible { show() }
        inputState.auxiliaryTab = .workspace
        inputState.isWorkspaceWingOpen = true
    }

    func showMediaViewer() {
        if !isVisible { show() }
        inputState.auxiliaryTab = .media
        inputState.isWorkspaceWingOpen = true
    }

    /// A large table viewer needs the whole screen. Hide the floating chat
    /// instead of letting it sit on top of the table.
    func hideForMediaViewer() {
        guard isVisible else { return }
        hiddenForMediaViewer = true
        stopHeightDriver()
        inputState.isWorkspaceWingOpen = false
        panel?.orderOut(nil)
    }

    func restoreAfterMediaViewer() {
        guard hiddenForMediaViewer else { return }
        hiddenForMediaViewer = false
        show()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            NotificationCenter.default.post(name: .qmxFocusComposer, object: nil)
        }
    }

    func hideWorkspaceWing() {
        inputState.isWorkspaceWingOpen = false
        NotificationCenter.default.post(name: .qmxStopMediaPlayback, object: nil)
    }

    func detachWorkspace() {
        inputState.isWorkspaceWingOpen = false
        NotificationCenter.default.post(name: .qmxDetachWorkspace, object: nil)
    }

    func detachMediaViewer() {
        inputState.isWorkspaceWingOpen = false
        NotificationCenter.default.post(name: .qmxStopMediaPlayback, object: nil)
        MediaViewerWindowController.shared.show()
    }


    private func layoutWing(open: Bool) {
        guard let panel, panel.isVisible else { return }
        stopHeightDriver()
        let screen = panel.screen ?? NSScreen.main
        let visible = screen?.visibleFrame ?? panel.frame
        let width = QuickEntryLayout.chatWidth + (open ? QuickEntryLayout.wingWidth : 0)
        var frame = panel.frame
        let topEdge = frame.maxY
        let chatHeight = lastChatHeight ?? frame.height
        let height = open ? max(chatHeight, QuickEntryLayout.wingMinHeight) : chatHeight
        currentAnimatedHeight = height

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
            panel.invalidateShadow()
        } else {
            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.24
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }, completionHandler: { [weak self] in
                Task { @MainActor [weak self] in
                    self?.panel?.invalidateShadow()
                }
            })
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
        brainClient.$messages
            .sink { [weak self] messages in
                guard let self, !messages.isEmpty, self.inputState.forceExpanded else { return }
                self.inputState.forceExpanded = false
            }
            .store(in: &cancellables)

        Publishers.CombineLatest3(
            inputState.$forceExpanded,
            brainClient.$isGenerating,
            brainClient.$messages.map { !$0.isEmpty }
        )
        .map { forceExpanded, isGenerating, hasMessages in
            forceExpanded || isGenerating || hasMessages
        }
        .removeDuplicates()
        .sink { [weak self] expanded in
            guard let self else { return }
            self.inputState.isExpanded = expanded
            if !expanded {
                self.resize(toContentHeight: QuickEntryLayout.idleHeight)
            }
        }
        .store(in: &cancellables)
    }

    private func refreshExpansion() {
        let expanded = inputState.forceExpanded
            || brainClient.isGenerating
            || !brainClient.messages.isEmpty

        inputState.isExpanded = expanded
        if !expanded {
            lastChatHeight = QuickEntryLayout.idleHeight
            targetChatHeight = QuickEntryLayout.idleHeight
        }
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
        guard inputState.isExpanded else { return QuickEntryLayout.idleHeight }
        return lastTurnHasAnswer ? QuickEntryLayout.answeringHeight : QuickEntryLayout.thinkingHeight
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
            guard let panel, panel.isVisible else { return }
            // A panel can be the key window while the app is inactive; that
            // still swallows keystrokes. Activate in that case too.
            guard !NSApp.isActive || !panel.isKeyWindow else { return }
            NSApp.activate(ignoringOtherApps: true)
            panel.makeKey()
        }
    }

    func hideAndRestoreFocus() {
        stopHeightDriver()
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
            contentRect: NSRect(
                x: 0,
                y: 0,
                width: QuickEntryLayout.chatWidth,
                height: QuickEntryLayout.idleHeight
            ),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        p.title = "启明星"
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
            onSplitMedia: { [weak self] in self?.detachMediaViewer() },
            onDesiredHeightChange: { [weak self] height in
                self?.resize(toContentHeight: height)
            }
        )
        let hosting = NSHostingController(rootView: rootView)
        // The window frame is owned by QuickEntryController. Without this,
        // NSHostingController feeds the SwiftUI intrinsic height back into the
        // window while text streams, producing a continuous zoom.
        hosting.sizingOptions = []
        if #available(macOS 13.3, *) {
            hosting.safeAreaRegions = []
        }
        hosting.view.wantsLayer = true
        hosting.view.layer?.backgroundColor = NSColor.clear.cgColor
        hosting.view.frame = NSRect(
            x: 0,
            y: 0,
            width: QuickEntryLayout.chatWidth,
            height: QuickEntryLayout.idleHeight
        )
        p.contentViewController = hosting
        p.setContentSize(NSSize(
            width: QuickEntryLayout.chatWidth,
            height: QuickEntryLayout.idleHeight
        ))
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

    private func stopHeightDriver() {
        heightDriverTask?.cancel()
        heightDriverTask = nil
        heightVelocity = 0
    }

    private func resolvedPanelHeight(forChatHeight chatHeight: CGFloat) -> CGFloat {
        inputState.isWorkspaceWingOpen
            ? max(chatHeight, QuickEntryLayout.wingMinHeight)
            : chatHeight
    }

    private func applyPanelHeight(_ height: CGFloat, to panel: FloatingQuickEntryPanel) {
        let scale = max(1.0, panel.backingScaleFactor)
        let snapped = (height * scale).rounded() / scale
        var frame = panel.frame
        var topEdge = frame.maxY
        if let screen = panel.screen ?? NSScreen.main {
            let visible = screen.visibleFrame
            let minTop = visible.minY + 8 + snapped
            let maxTop = visible.maxY - 16
            topEdge = min(max(topEdge, minTop), maxTop)
        }
        let nextSize = NSSize(width: panelWidth, height: snapped)
        let nextOriginY = topEdge - snapped
        guard abs(frame.width - nextSize.width) >= 0.25 ||
              abs(frame.height - nextSize.height) >= 0.25 ||
              abs(frame.origin.y - nextOriginY) >= 0.25 else {
            return
        }
        frame.size = nextSize
        frame.origin.y = nextOriginY
        panel.setFrame(frame, display: true)
    }

    private func startHeightDriverIfNeeded() {
        guard heightDriverTask == nil, let panel else { return }
        currentAnimatedHeight = panel.frame.height
        heightDriverTask = Task { @MainActor [weak self] in
            let omega: CGFloat = 20.0
            var lastTime = ProcessInfo.processInfo.systemUptime
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 16_000_000)
                guard !Task.isCancelled, let self, let panel = self.panel, panel.isVisible else { break }

                let now = ProcessInfo.processInfo.systemUptime
                let dt = CGFloat(min(max(now - lastTime, 0.004), 0.033))
                lastTime = now

                let target = self.resolvedPanelHeight(forChatHeight: self.targetChatHeight)
                let delta = self.currentAnimatedHeight - target
                let decay = exp(-omega * dt)
                let nextHeight = target + (delta + (self.heightVelocity + omega * delta) * dt) * decay
                let nextVelocity = (self.heightVelocity - omega * (self.heightVelocity + omega * delta) * dt) * decay

                if abs(nextHeight - target) < 0.5 && abs(nextVelocity) < 4.0 {
                    self.currentAnimatedHeight = target
                    self.heightVelocity = 0
                    self.applyPanelHeight(target, to: panel)
                    panel.invalidateShadow()
                    break
                }

                self.currentAnimatedHeight = nextHeight
                self.heightVelocity = nextVelocity
                self.applyPanelHeight(nextHeight, to: panel)
            }
            self?.heightVelocity = 0
            self?.heightDriverTask = nil
        }
    }

    func resize(toContentHeight height: CGFloat) {
        // Always cache the chat height, even while the panel is hidden.
        let chatHeight = max(QuickEntryLayout.idleHeight, height)
        lastChatHeight = chatHeight
        targetChatHeight = chatHeight
        guard let panel, panel.isVisible else { return }
        let target = resolvedPanelHeight(forChatHeight: chatHeight)

        if QMXTheme.isHeadlessDump || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            stopHeightDriver()
            currentAnimatedHeight = target
            applyPanelHeight(target, to: panel)
            panel.invalidateShadow()
            return
        }

        if heightDriverTask == nil &&
           abs(panel.frame.height - target) <= 1.0 &&
           abs(panel.frame.width - panelWidth) <= 0.5 {
            currentAnimatedHeight = target
            return
        }

        startHeightDriverIfNeeded()
    }

    private func resizePanel(animated: Bool) {
        guard let panel else { return }
        stopHeightDriver()
        let chatHeight = lastChatHeight ?? desiredPanelHeight
        targetChatHeight = chatHeight
        let height = resolvedPanelHeight(forChatHeight: chatHeight)
        currentAnimatedHeight = height
        guard abs(panel.frame.height - height) > 0.5 || abs(panel.frame.width - panelWidth) > 0.5 else { return }
        var frame = panel.frame
        let topEdge = frame.maxY
        frame.size = NSSize(width: panelWidth, height: height)
        frame.origin.y = topEdge - height

        if animated && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.24
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }, completionHandler: { [weak self] in
                Task { @MainActor [weak self] in
                    self?.panel?.invalidateShadow()
                }
            })
        } else {
            panel.setFrame(frame, display: true)
            panel.invalidateShadow()
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

enum AuxiliaryWingTab: String, CaseIterable, Identifiable {
    case workspace
    case media

    var id: String { rawValue }
    var label: String { self == .workspace ? "工作区" : "媒体" }
}

@MainActor
final class QuickEntryInputState: ObservableObject {
    @Published var text: String = ""
    @Published var includeScreen: Bool = false
    @Published var stagedAttachments: [StagedAttachment] = []
    @Published var uploadingNames: [String] = []
    @Published var focusRequestToken: UUID = UUID()
    @Published var isExpanded: Bool = false
    @Published var forceExpanded: Bool = false
    @Published var isWorkspaceWingOpen: Bool = false
    @Published var auxiliaryTab: AuxiliaryWingTab = .workspace
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
    var onSplitMedia: () -> Void = {}
    var onDesiredHeightChange: (CGFloat) -> Void = { _ in }

    @AppStorage("qmx.answerFontSize") private var answerFontSize: Double = 16
    @AppStorage("qmx.answerMaxLines") private var answerMaxLines: Int = 30
    @State private var appeared = QMXTheme.isHeadlessDump
    @State private var answerContentHeight: CGFloat = 0
    @State private var turnPeakHeight: CGFloat = 0
    @State private var answerRevealFinished = QMXTheme.isHeadlessDump
    @State private var caughtUpAnswerCount = -1
    @State private var copiedAnswer = false
    @State private var answerRating: String?
    @State private var isDropTargeted = false
    @State private var cachedLatestTurn: LiveTurn?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            VisualEffectBlur(
                material: .popover,
                blendingMode: .behindWindow,
                cornerRadius: QMXTheme.commandRadius
            )

            HStack(spacing: 0) {
                chatColumn

                if inputState.isWorkspaceWingOpen {
                    Divider().opacity(0.5)
                    AuxiliaryWingView(
                        store: workspaceStore,
                        desktopActions: desktopActions,
                        inputState: inputState,
                        onSplitWorkspace: onSplitWing,
                        onSplitMedia: onSplitMedia,
                        onClose: onCloseWing
                    )
                    .frame(width: QuickEntryLayout.wingWidth)
                    .transition(.move(edge: .trailing).combined(with: .opacity))
                }
            }
            .animation(.easeOut(duration: 0.20), value: inputState.isWorkspaceWingOpen)
        }
        .frame(
            width: QuickEntryLayout.chatWidth + (inputState.isWorkspaceWingOpen ? QuickEntryLayout.wingWidth : 0)
        )
        .frame(maxHeight: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: QMXTheme.commandRadius, style: .continuous))
        .overlay(HairlineBorderOverlay(cornerRadius: QMXTheme.commandRadius))
        .overlay(
            AmbientRimLight(
                isActive: brainClient.isGenerating,
                cornerRadius: QMXTheme.commandRadius
            )
        )
        .overlay {
            if isDropTargeted {
                ZStack {
                    RoundedRectangle(cornerRadius: QMXTheme.commandRadius, style: .continuous)
                        .strokeBorder(QMXTheme.accent.opacity(0.75), lineWidth: 1.5)
                    VStack(spacing: 6) {
                        Image(systemName: "arrow.down.doc")
                            .font(.system(size: 22, weight: .light))
                        Text("松开以添加 PDF / 表格 / 文档")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .foregroundStyle(QMXTheme.accent)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 10).fill(QMXTheme.surfaceRaised.opacity(0.94)))
                }
                .allowsHitTesting(false)
            }
        }
        .onDrop(of: [UTType.fileURL], isTargeted: $isDropTargeted) { providers in
            for provider in providers {
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                    var url: URL?
                    if let data = item as? Data {
                        url = URL(dataRepresentation: data, relativeTo: nil)
                    } else if let itemURL = item as? URL {
                        url = itemURL
                    }
                    guard let url else { return }
                    Task { @MainActor in
                        if let attachment = await brainClient.uploadArtifact(fileURL: url) {
                            inputState.stagedAttachments.append(attachment)
                        }
                    }
                }
            }
            return true
        }
        .ignoresSafeArea()
        .scaleEffect(appeared ? 1.0 : 0.96)
        .opacity(appeared ? 1.0 : 0.0)
        .onAppear {
            cachedLatestTurn = Self.makeLatestTurn(from: brainClient.messages)
            if reduceMotion {
                appeared = true
            } else {
                withAnimation(.spring(response: 0.32, dampingFraction: 0.84)) {
                    appeared = true
                }
            }
            if reduceMotion || QMXTheme.isHeadlessDump {
                answerRevealFinished = true
            }
            onDesiredHeightChange(chatHeight)
        }
        .onReceive(brainClient.$messages) { messages in
            cachedLatestTurn = Self.makeLatestTurn(from: messages)
        }
        .onChange(of: chatHeight) { height in
            onDesiredHeightChange(height)
        }
        .onChange(of: brainClient.isGenerating) { generating in
            if generating {
                answerRevealFinished = QMXTheme.isHeadlessDump
                caughtUpAnswerCount = -1
                turnPeakHeight = 0
                onDesiredHeightChange(chatHeight)
            } else {
                // The answer is done: keep the caret in the composer so the
                // next question can be typed immediately.
                inputState.focusRequestToken = UUID()
            }
            if !inputState.isExpanded {
                onDesiredHeightChange(QuickEntryLayout.idleHeight)
            } else if reduceMotion || QMXTheme.isHeadlessDump {
                answerRevealFinished = true
                onDesiredHeightChange(chatHeight)
            } else {
                completeRevealIfReady()
            }
        }
        .onChange(of: inputState.isExpanded) { expanded in
            if !expanded {
                turnPeakHeight = 0
                onDesiredHeightChange(QuickEntryLayout.idleHeight)
            }
        }
        .onChange(of: inputState.layoutToken) { _ in
            onDesiredHeightChange(chatHeight)
        }
        .onChange(of: latestTurn?.id) { _ in
            answerContentHeight = 0
            turnPeakHeight = 0
            answerRevealFinished = reduceMotion || QMXTheme.isHeadlessDump
            caughtUpAnswerCount = -1
            copiedAnswer = false
            answerRating = nil
            onDesiredHeightChange(chatHeight)
        }
        .onChange(of: answerFontSize) { _ in
            answerContentHeight = 0
            turnPeakHeight = 0
        }
        .onChange(of: inputState.text) { _ in
            onDesiredHeightChange(chatHeight)
        }
        .onChange(of: inputState.stagedAttachments.count) { _ in
            onDesiredHeightChange(chatHeight)
        }
        .onChange(of: inputState.uploadingNames.count) { _ in
            onDesiredHeightChange(chatHeight)
        }
        .onChange(of: latestTurn?.media.map(\.mediaId)) { _ in
            guard !MediaViewerWindowController.shared.isVisible else { return }
            guard inputState.isWorkspaceWingOpen else { return }
            guard let turn = latestTurn, !turn.media.isEmpty else { return }
            let viewer = MediaViewerModel.shared
            guard viewer.contextLabel == "本轮回答", !viewer.items.isEmpty else { return }
            viewer.offerPending(items: turn.media, title: turn.prompt)
        }
    }

    private var chatColumn: some View {
        VStack(spacing: 0) {
            inputRow

            if attachmentBarHeight > 0 {
                attachmentBar
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }

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
        .frame(width: QuickEntryLayout.chatWidth)
        .frame(maxHeight: .infinity, alignment: .top)
        .onPreferenceChange(ChatAnswerHeightKey.self) { height in
            answerContentHeight = height
            if brainClient.isGenerating || !answerRevealFinished {
                turnPeakHeight = max(turnPeakHeight, naturalAnswerChatHeight)
            }
        }
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
            .frame(
                maxWidth: .infinity,
                minHeight: QuickEntryLayout.inputHeight + inputExtraHeight,
                maxHeight: QuickEntryLayout.inputHeight + inputExtraHeight
            )

            HStack(spacing: 10) {
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
        .frame(height: QuickEntryLayout.inputHeight + inputExtraHeight)
    }

    private var attachmentBar: some View {
        HStack(spacing: 7) {
            ForEach(inputState.stagedAttachments) { attachment in
                HStack(spacing: 6) {
                    Image(systemName: attachmentSymbol(attachment.kind))
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(QMXTheme.accent)
                    Text(attachment.filename)
                        .font(.system(size: 11.5))
                        .foregroundStyle(QMXTheme.textPrimary)
                        .lineLimit(1)
                    Button {
                        inputState.stagedAttachments.removeAll { $0.id == attachment.id }
                        onDesiredHeightChange(chatHeight)
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 8.5, weight: .semibold))
                            .foregroundStyle(QMXTheme.textTertiary)
                            .frame(width: 14, height: 14)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 9)
                .frame(height: 26)
                .background(Capsule().fill(QMXTheme.surfaceRaised))
                .overlay(Capsule().strokeBorder(QMXTheme.hairline, lineWidth: 0.5))
            }

            ForEach(inputState.uploadingNames, id: \.self) { name in
                HStack(spacing: 6) {
                    ProgressView()
                        .controlSize(.mini)
                        .scaleEffect(0.65)
                        .frame(width: 12, height: 12)
                    Text(name)
                        .font(.system(size: 11.5))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .lineLimit(1)
                }
                .padding(.horizontal, 9)
                .frame(height: 26)
                .background(Capsule().fill(QMXTheme.surfaceMuted))
            }

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 20)
        .frame(height: attachmentBarHeight)
    }

    private func attachmentSymbol(_ kind: String) -> String {
        switch kind.lowercased() {
        case "image": return "photo"
        case "audio": return "waveform"
        case "video": return "film"
        case "pdf": return "doc.richtext"
        default: return "doc"
        }
    }

    // MARK: Live stage

    private var liveStage: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let turn = latestTurn, !turn.prompt.isEmpty {
                Text(turn.prompt)
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .lineLimit(8)
                    .fixedSize(horizontal: false, vertical: true)
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
                answerView(turn)
                if !turn.sources.isEmpty {
                    SourceChipsView(sources: turn.sources)
                }
                let stripMedia = qmxMediaWithoutInlineTables(turn.media, answer: turn.answer)
                if !stripMedia.isEmpty {
                    MediaStripView(media: stripMedia, contextTitle: "本轮回答")
                }
                workspaceLink
            } else if let turn = latestTurn,
                      !turn.media.isEmpty || !turn.sources.isEmpty {
                // Card-only answers (e.g. a table emitted inside <<<CARD>>>)
                // have no spoken text, but their media must still be visible.
                if !turn.sources.isEmpty {
                    SourceChipsView(sources: turn.sources)
                }
                let stripMedia = qmxMediaWithoutInlineTables(turn.media, answer: turn.answer)
                if !stripMedia.isEmpty {
                    MediaStripView(media: stripMedia, contextTitle: "本轮回答")
                }
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

    /// Short answers render as a plain stack so no ScrollView (and therefore
    /// no scroll indicator) can appear. Long answers switch to a fixed-height
    /// scroll view.
    private func answerView(_ turn: LiveTurn) -> some View {
        let scrollable = answerContentHeight > maxLinesContentHeight + 0.5
        let shouldAutoScroll = (turn.isStreaming || !answerRevealFinished) && scrollable
        // Use one stable view for the whole turn. Switching between a plain
        // stack and a ScrollView recreates TypewriterText and restarts the
        // reveal, which looks like a large empty answer area.
        return AnswerScrollView(
            text: turn.answer,
            fontSize: CGFloat(answerFontSize),
            turnID: turn.id,
            width: QuickEntryLayout.answerWidth,
            shouldAutoScroll: shouldAutoScroll,
            tableMedia: turn.media.filter(\.isMarkdownTable),
            onOpenTable: { index in
                let items = turn.media.filter(\.isMarkdownTable)
                guard items.indices.contains(index) else { return }
                MediaViewerLauncher.open(
                    items: items,
                    selectedID: items[index].mediaId,
                    title: items[index].title.isEmpty ? "表格" : items[index].title,
                    contextLabel: "本轮回答",
                    prefersLargeWindow: true
                )
            },
            onFinish: {
                caughtUpAnswerCount = latestTurn?.answer.count ?? -1
                completeRevealIfReady()
            }
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
            if brainClient.profiles.isEmpty {
                Button {
                    NotificationCenter.default.post(name: .qmxOpenSettings, object: "routing")
                } label: {
                    Text("配置模型")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(QMXTheme.accent)
                }
                .buttonStyle(.plain)
                .help("先连接 DeepSeek 或 Gemini，并选择对话模型")
            } else {
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
            }

            EmotionBadge(emotion: brainClient.emotion)

            if let turn = latestTurn, !turn.answer.isEmpty, !turn.isStreaming {
                HStack(spacing: 2) {
                    Button {
                        copyAnswer(turn.answer)
                    } label: {
                        Image(systemName: copiedAnswer ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 12.5, weight: .medium))
                            .foregroundStyle(copiedAnswer ? QMXTheme.success : QMXTheme.textSecondary)
                            .frame(width: 26, height: 26)
                    }
                    .buttonStyle(.qmxIcon)
                    .qmxTooltip(copiedAnswer ? "已复制" : "复制回答")

                    feedbackButton(
                        symbol: "hand.thumbsup",
                        active: answerRating == "up",
                        help: "有帮助",
                        action: { rateAnswer("up", turn: turn) }
                    )
                    feedbackButton(
                        symbol: "hand.thumbsdown",
                        active: answerRating == "down",
                        help: "没帮助",
                        action: { rateAnswer("down", turn: turn) }
                    )
                }
            }

            Spacer()

            Button {
                // Voice input is not wired to a microphone pipeline yet.
                // Keep the microphone target in place for the upcoming PTT work.
            } label: {
                Image(systemName: "mic")
                    .font(.system(size: 13, weight: .medium))
            }
            .buttonStyle(.qmxIcon)
            .help("语音输入（即将支持）")
            .accessibilityLabel("语音输入")

            Button {
                brainClient.setTTSEnabled(!brainClient.ttsEnabled)
            } label: {
                Image(systemName: brainClient.ttsEnabled ? "speaker.wave.2.fill" : "speaker.slash")
                    .font(.system(size: 13, weight: .medium))
            }
            .buttonStyle(QMXButtonStyle(variant: .icon, isActive: brainClient.ttsEnabled))
            .help(brainClient.ttsEnabled ? "关闭朗读回答" : "朗读回答")
            .accessibilityLabel(brainClient.ttsEnabled ? "关闭朗读回答" : "朗读回答")

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

            Button(action: startNewConversation) {
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
        .padding(.vertical, 10)
        .frame(height: QuickEntryLayout.footerHeight)
    }

    // MARK: Data

    private struct LiveTurn {
        let id: String
        var prompt: String = ""
        var answer: String = ""
        var isStreaming: Bool = false
        var sources: [SourceItem] = []
        var media: [MediaItem] = []
    }

    private var answerLineHeight: CGFloat {
        ceil(CGFloat(answerFontSize) * 1.4) + 3
    }

    /// Content height allowed before the panel switches to a fixed height.
    /// The screen height is a hard upper bound so a tall setting can never
    /// push the window off the display.
    private var sourceBarHeight: CGFloat {
        (latestTurn?.sources.isEmpty == false) ? 36 : 0
    }

    private var mediaBarHeight: CGFloat {
        (latestTurn?.media.isEmpty == false) ? 94 : 0
    }

    private var attachmentBarHeight: CGFloat {
        (inputState.stagedAttachments.isEmpty && inputState.uploadingNames.isEmpty) ? 0 : 38
    }

    /// Let the composer grow up to three extra lines so a pasted multi-line
    /// instruction stays readable instead of hiding behind a one-line frame.
    private var inputExtraHeight: CGFloat {
        let text = inputState.text
        guard !text.isEmpty else { return 0 }
        let charsPerLine = 28
        var lines = 0
        for line in text.components(separatedBy: "\n") {
            lines += max(1, Int(ceil(Double(line.count) / Double(charsPerLine))))
        }
        return CGFloat(min(max(lines - 1, 0), 8)) * 24
    }

    private var promptBlockHeight: CGFloat {
        guard let prompt = latestTurn?.prompt, !prompt.isEmpty else { return 0 }
        let charsPerLine = 56
        var lines = 0
        for line in prompt.components(separatedBy: "\n") {
            lines += max(1, Int(ceil(Double(line.count) / Double(charsPerLine))))
        }
        return CGFloat(min(max(lines, 1), 8)) * 17 + 8
    }

    private var maxLinesContentHeight: CGFloat {
        let visibleHeight = NSScreen.main?.visibleFrame.height ?? 960
        let prompt = promptBlockHeight
        let screenCap = max(
            160,
            visibleHeight - 24 - QuickEntryLayout.panelChrome - inputExtraHeight - 40 - prompt - 26 - 12
                - sourceBarHeight - mediaBarHeight - attachmentBarHeight
        )
        return min(CGFloat(answerMaxLines) * answerLineHeight, screenCap)
    }

    private var naturalAnswerChatHeight: CGFloat {
        let measured = answerContentHeight
        guard measured > 0 else {
            return QuickEntryLayout.thinkingHeight + attachmentBarHeight + inputExtraHeight
        }
        let promptBlock = promptBlockHeight
        let capped = min(measured, maxLinesContentHeight)
        let stage = 40 + promptBlock + capped + sourceBarHeight + mediaBarHeight
            + attachmentBarHeight + 26 + 12
        return max(
            QuickEntryLayout.thinkingHeight + attachmentBarHeight + inputExtraHeight,
            QuickEntryLayout.panelChrome + inputExtraHeight + stage
        )
    }

    /// Pure chat-column height. The wing floor is applied by the window
    /// controller and never written back into this value.
    private var chatHeight: CGFloat {
        if !inputState.isExpanded {
            return QuickEntryLayout.idleHeight + attachmentBarHeight + inputExtraHeight
        }
        if isThinking || latestTurn?.answer.isEmpty != false {
            return QuickEntryLayout.thinkingHeight + attachmentBarHeight + inputExtraHeight
        }
        let natural = naturalAnswerChatHeight
        if brainClient.isGenerating || !answerRevealFinished {
            return max(
                QuickEntryLayout.thinkingHeight + attachmentBarHeight + inputExtraHeight,
                turnPeakHeight,
                natural
            )
        }
        return natural
    }

    private func completeRevealIfReady() {
        guard !brainClient.isGenerating else { return }
        let count = latestTurn?.answer.count ?? -1
        guard count >= 0, caughtUpAnswerCount == count else { return }
        answerRevealFinished = true
        onDesiredHeightChange(chatHeight)
    }

    /// Cached derived state: recomputed once per messages publish (and only for
    /// the last turn) instead of on every body evaluation. The live panel is
    /// 60Hz; recomputing this from the whole history per frame was the main
    /// streaming jank source in long sessions.
    private var latestTurn: LiveTurn? {
        cachedLatestTurn
    }

    private static func makeLatestTurn(from messages: [ChatEntry]) -> LiveTurn? {
        guard let last = messages.last else { return nil }
        let turnId = last.turnId
        var start = messages.count - 1
        while start > 0 && messages[start - 1].turnId == turnId {
            start -= 1
        }
        var turn = LiveTurn(id: turnId)
        for message in messages[start...] where message.turnId == turnId {
            if message.isUser {
                turn.prompt = message.text
            } else {
                turn.answer = qmxCleanAssistantText(message.text)
                turn.isStreaming = message.isStreaming
                turn.sources = message.sources
                turn.media = message.media
            }
        }
        return turn
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
        !brainClient.profiles.isEmpty &&
        inputState.uploadingNames.isEmpty &&
        (!inputState.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
         !inputState.stagedAttachments.isEmpty)
    }

    private func copyAnswer(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(QMXMarkdownTables.strippingTables(from: text), forType: .string)
        copiedAnswer = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
            copiedAnswer = false
        }
    }

    private func rateAnswer(_ rating: String, turn: LiveTurn) {
        answerRating = rating
        Task {
            await brainClient.sendFeedback(
                turnId: turn.id,
                rating: rating,
                prompt: turn.prompt,
                answer: turn.answer
            )
        }
    }

    private func feedbackButton(
        symbol: String,
        active: Bool,
        help: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: active ? "\(symbol).fill" : symbol)
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(active ? QMXTheme.accent : QMXTheme.textSecondary)
                .frame(width: 26, height: 26)
        }
        .buttonStyle(.qmxIcon)
        .qmxTooltip(help)
    }

    private func startNewConversation() {
        answerContentHeight = 0
        turnPeakHeight = 0
        answerRevealFinished = reduceMotion || QMXTheme.isHeadlessDump
        caughtUpAnswerCount = -1
        copiedAnswer = false
        answerRating = nil
        inputState.forceExpanded = false
        inputState.stagedAttachments = []
        inputState.uploadingNames = []
        brainClient.clearHistory()
        inputState.focusRequestToken = UUID()
        onDesiredHeightChange(QuickEntryLayout.idleHeight)
    }

    private func submitMessage() {
        let text = inputState.text
        let attachments = inputState.stagedAttachments
        let withScreen = inputState.includeScreen
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty else {
            return
        }
        // The send button honors canSend, but Enter must too: without a
        // connected Brain / configured model the turn would be rejected and
        // leave the panel in a generating state.
        guard brainClient.phase == .connected, !brainClient.profiles.isEmpty else {
            brainClient.errorBannerText = brainClient.phase == .connected
                ? "还没有配置可用模型，请先打开设置完成配置。"
                : "Brain 尚未连接，请稍候或检查后端日志。"
            return
        }
        inputState.forceExpanded = true
        inputState.text = ""
        inputState.stagedAttachments = []
        brainClient.sendText(text, attachments: attachments, includeScreen: withScreen)
    }

    private func pickAttachmentFile() {
        let chooser = NSOpenPanel()
        chooser.allowsMultipleSelection = true
        chooser.canChooseDirectories = false
        chooser.canChooseFiles = true
        chooser.level = .floating
        // The quick-entry panel is a floating non-activating panel; left in
        // place it keeps key status and the file chooser cannot be clicked.
        let host = NSApp.windows.first(where: { $0 is FloatingQuickEntryPanel })
        host?.orderOut(nil)
        NSApp.activate(ignoringOtherApps: true)
        let response = chooser.runModal()
        host?.makeKeyAndOrderFront(nil)
        host?.makeKey()
        NSApp.activate(ignoringOtherApps: true)
        if response == .OK {
            let urls = chooser.urls
            for url in urls {
                inputState.uploadingNames.append(url.lastPathComponent)
            }
            onDesiredHeightChange(chatHeight)
            Task {
                for url in urls {
                    let attachment = await brainClient.uploadArtifact(fileURL: url)
                    inputState.uploadingNames.removeAll { $0 == url.lastPathComponent }
                    if let attachment {
                        inputState.stagedAttachments.append(attachment)
                    }
                    onDesiredHeightChange(chatHeight)
                }
            }
        }
        // The modal panel steals key status; put the caret back in the input.
        inputState.focusRequestToken = UUID()
    }

    private func uploadPastedImage(_ data: Data) async {
        let name = "粘贴图片-\(UUID().uuidString.prefix(4)).png"
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("qmx_paste_\(UUID().uuidString.prefix(8)).png")
        inputState.uploadingNames.append(name)
        onDesiredHeightChange(chatHeight)
        try? data.write(to: tmp)
        if let attachment = await brainClient.uploadArtifact(fileURL: tmp) {
            inputState.stagedAttachments.append(attachment)
        }
        inputState.uploadingNames.removeAll { $0 == name }
        try? FileManager.default.removeItem(at: tmp)
        onDesiredHeightChange(chatHeight)
    }
}

private struct ChatAnswerHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

private struct AnswerScrollView: View {
    let text: String
    let fontSize: CGFloat
    let turnID: String
    let width: CGFloat
    let shouldAutoScroll: Bool
    var tableMedia: [MediaItem] = []
    var onOpenTable: (Int) -> Void = { _ in }
    var onFinish: () -> Void = {}
    @State private var lastAutoScrollAt = Date.distantPast

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    TypewriterText(
                        text: text,
                        fontSize: fontSize,
                        turnID: turnID,
                        tableMedia: tableMedia,
                        allowsSelection: !shouldAutoScroll,
                        onOpenTable: onOpenTable,
                        onTick: {
                            guard shouldAutoScroll else { return }
                            // The reveal runs at 60Hz; scrolling the whole
                            // ScrollView at that rate is pure layout churn.
                            let now = Date()
                            guard now.timeIntervalSince(lastAutoScrollAt) >= 0.033 else { return }
                            lastAutoScrollAt = now
                            proxy.scrollTo("answerBottom", anchor: .bottom)
                        },
                        onFinish: onFinish
                    )
                    .id(turnID)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        GeometryReader { geo in
                            Color.clear.preference(
                                key: ChatAnswerHeightKey.self,
                                value: geo.size.height
                            )
                        }
                    )
                    .id("answerTop")

                    Color.clear
                        .frame(height: 1)
                        .id("answerBottom")
                }
                .frame(width: width, alignment: .leading)
            }
            .scrollIndicators(.visible)
            .onAppear {
                proxy.scrollTo(shouldAutoScroll ? "answerBottom" : "answerTop", anchor: shouldAutoScroll ? .bottom : .top)
            }
            .onChange(of: turnID) { _ in
                proxy.scrollTo("answerTop", anchor: .top)
            }
            .onChange(of: shouldAutoScroll) { autoScroll in
                if autoScroll {
                    proxy.scrollTo("answerBottom", anchor: .bottom)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct TypewriterText: View {
    let text: String
    let fontSize: CGFloat
    let turnID: String
    var tableMedia: [MediaItem] = []
    var allowsSelection: Bool = true
    var onOpenTable: (Int) -> Void = { _ in }
    var onTick: () -> Void = {}
    var onFinish: () -> Void = {}

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var visibleCount = 0
    @State private var revealTask: Task<Void, Never>?
    @State private var blocks: [StageBlock] = []
    @State private var pendingParseText = ""
    @State private var parseTask: Task<Void, Never>?
    @State private var lastParseAt = Date.distantPast

    init(
        text: String,
        fontSize: CGFloat,
        turnID: String,
        tableMedia: [MediaItem] = [],
        allowsSelection: Bool = true,
        onOpenTable: @escaping (Int) -> Void = { _ in },
        onTick: @escaping () -> Void = {},
        onFinish: @escaping () -> Void = {}
    ) {
        self.text = text
        self.fontSize = fontSize
        self.turnID = turnID
        self.tableMedia = tableMedia
        self.allowsSelection = allowsSelection
        self.onOpenTable = onOpenTable
        self.onTick = onTick
        self.onFinish = onFinish
        // Parsing in init would run on every parent body evaluation, even when
        // State discards the value. Parse lazily in onAppear/onChange instead.
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
        .onAppear {
            rebuildBlocks(text, force: true)
            start()
        }
        .onChange(of: text) { newText in
            scheduleParse(newText)
        }
        .onChange(of: turnID) { _ in
            revealTask?.cancel()
            revealTask = nil
            parseTask?.cancel()
            parseTask = nil
            rebuildBlocks(text, force: true)
            visibleCount = (reduceMotion || QMXTheme.isHeadlessDump) ? totalCount : 0
            start()
        }
        .onDisappear {
            revealTask?.cancel()
            revealTask = nil
            parseTask?.cancel()
            parseTask = nil
        }
    }

    /// Parse at most every 80ms during a stream. Continuous deltas used to
    /// re-split and re-scan the entire answer for every chunk.
    private func scheduleParse(_ newText: String) {
        pendingParseText = newText
        if reduceMotion || QMXTheme.isHeadlessDump {
            parseTask?.cancel()
            parseTask = nil
            rebuildBlocks(newText, force: true)
            start()
            return
        }
        let wait = 0.08 - Date().timeIntervalSince(lastParseAt)
        if wait <= 0 {
            parseTask?.cancel()
            parseTask = nil
            rebuildBlocks(newText, force: true)
            start()
            return
        }
        guard parseTask == nil else { return }
        parseTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(max(0.01, wait) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            parseTask = nil
            rebuildBlocks(pendingParseText, force: true)
            start()
        }
    }

    private func rebuildBlocks(_ raw: String, force: Bool) {
        guard force else { return }
        lastParseAt = Date()
        pendingParseText = raw
        blocks = Self.parse(raw)
    }

    private var codeFontSize: CGFloat {
        max(12, (fontSize * 0.72).rounded())
    }

    @ViewBuilder
    private func blockView(_ block: StageBlock, _ visible: String) -> some View {
        if let table = block.table {
            let media = tableMedia.indices.contains(table.id) ? tableMedia[table.id] : nil
            QMXTableNotice(
                table: table,
                media: media,
                onOpen: media == nil ? nil : { onOpenTable(table.id) }
            )
        } else if block.isCode {
            Text(visible)
                .font(.system(size: codeFontSize, design: .monospaced))
                .lineSpacing(2)
                .modifier(QMXTextSelectable(enabled: allowsSelection))
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
            InlineMarkdownText(
                text: visible,
                fontSize: fontSize,
                lineSpacing: 3,
                selectable: allowsSelection
            )
                .foregroundStyle(QMXTheme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var totalCount: Int {
        blocks.reduce(0) { $0 + $1.text.count }
    }

    private func start() {
        if reduceMotion || QMXTheme.isHeadlessDump {
            revealTask?.cancel()
            revealTask = nil
            visibleCount = totalCount
            onFinish()
            return
        }
        if visibleCount > totalCount {
            visibleCount = 0
        }
        if visibleCount >= totalCount {
            onFinish()
            return
        }
        guard revealTask == nil else { return }
        // Keep one steady 60Hz loop across incoming deltas so fast streams
        // never cancel and starve the typewriter reveal.
        revealTask = Task { @MainActor in
            while visibleCount < totalCount {
                try? await Task.sleep(nanoseconds: 16_000_000)
                if Task.isCancelled {
                    revealTask = nil
                    return
                }
                let remaining = max(1, totalCount - visibleCount)
                let step = max(1, (remaining + 29) / 30)
                visibleCount = min(totalCount, visibleCount + step)
                onTick()
            }
            revealTask = nil
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
                    table: nil,
                    start: 0
                ))
            }
            proseLines.removeAll()
        }

        let normalized = raw.replacingOccurrences(of: "\r\n", with: "\n")
        let lines = normalized.components(separatedBy: "\n")
        let tables = QMXMarkdownTables.scan(normalized)
        var tableByStart: [Int: QMXMarkdownTable] = [:]
        for table in tables {
            tableByStart[table.startLine] = table
        }

        var index = 0
        while index < lines.count {
            if let table = tableByStart[index] {
                flushProse()
                parsed.append(StageBlock(
                    id: parsed.count,
                    isCode: false,
                    language: "",
                    text: "表格",
                    table: table,
                    start: 0
                ))
                index = table.endLine + 1
                continue
            }

            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                if inCode {
                    let code = codeLines.joined(separator: "\n")
                    if !code.isEmpty {
                        parsed.append(StageBlock(
                            id: parsed.count,
                            isCode: true,
                            language: language,
                            text: code,
                            table: nil,
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
                index += 1
                continue
            }
            if inCode {
                codeLines.append(line)
            } else if trimmed.isEmpty {
                flushProse()
            } else {
                proseLines.append(trimmed)
            }
            index += 1
        }

        if inCode {
            let code = codeLines.joined(separator: "\n")
            if !code.isEmpty {
                parsed.append(StageBlock(
                    id: parsed.count,
                    isCode: true,
                    language: language,
                    text: code,
                    table: nil,
                    start: 0
                ))
            }
        } else {
            flushProse()
        }

        if parsed.isEmpty, !raw.isEmpty {
            parsed.append(StageBlock(
                id: 0,
                isCode: false,
                language: "",
                text: raw,
                table: nil,
                start: 0
            ))
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
    let table: QMXMarkdownTable?
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
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.verticalScrollElasticity = .none

        let tv = QMXTextView()
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.allowsUndo = true
        tv.font = NSFont.systemFont(ofSize: fontSize)
        tv.textColor = .labelColor
        tv.insertionPointColor = .clear
        tv.drawsBackground = false
        tv.isEditable = true
        tv.isSelectable = true
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        // Keep one stable optical inset. Recomputing it from the live height
        // made the whole text block move on every keystroke and panel resize.
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
        context.coordinator.focusObserver = NotificationCenter.default.addObserver(
            forName: .qmxFocusComposer,
            object: nil,
            queue: .main
        ) { [weak tv] _ in
            guard let tv, let window = tv.window else { return }
            NSApp.activate(ignoringOtherApps: true)
            window.makeKey()
            window.makeFirstResponder(tv)
        }
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
        let height = max(68, nsView.contentSize.height)
        if width > 0,
           abs(tv.frame.width - width) > 0.5 || abs(tv.frame.height - height) > 0.5 {
            tv.frame = NSRect(x: 0, y: 0, width: width, height: height)
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

    static func dismantleNSView(_ nsView: NSScrollView, coordinator: Coordinator) {
        if let observer = coordinator.focusObserver {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ChineseIMETextView
        weak var textView: QMXTextView?
        var lastFocusToken: UUID = UUID()
        var focusObserver: NSObjectProtocol?

        init(_ parent: ChineseIMETextView) {
            self.parent = parent
        }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            tv.needsDisplay = true
            tv.scrollRangeToVisible(tv.selectedRange())
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

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard flags.contains(.command),
              let key = event.charactersIgnoringModifiers?.lowercased()
        else {
            return super.performKeyEquivalent(with: event)
        }
        switch key {
        case "c":
            copy(nil)
            return true
        case "v":
            paste(nil)
            return true
        case "x":
            cut(nil)
            return true
        case "a":
            selectAll(nil)
            return true
        case "z":
            if flags.contains(.shift) {
                undoManager?.redo()
            } else {
                undoManager?.undo()
            }
            return true
        default:
            return super.performKeyEquivalent(with: event)
        }
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
        startCaretBlink()
    }

    override func layout() {
        super.layout()
        invalidateCaret()
    }

    private var caretTimer: Timer?
    private var caretVisible = false

    private var caretLineHeight: CGFloat {
        ceil((font ?? NSFont.systemFont(ofSize: 22)).boundingRectForFont.height)
    }

    /// AppKit's insertion rect for a multi-line composer can point at the
    /// first line (or above it). Locate the glyph at the caret and derive the
    /// line rect from the layout manager instead.
    private func caretRectInView() -> NSRect? {
        let origin = textContainerOrigin
        let lineHeight = caretLineHeight
        let length = textStorage?.length ?? 0
        let location = selectedRange().location
        var x = origin.x
        var y = origin.y
        var height = lineHeight

        if length > 0, location >= 0,
           let manager = layoutManager, let container = textContainer {
            if location < length {
                let glyph = manager.glyphIndexForCharacter(at: location)
                let rect = manager.boundingRect(
                    forGlyphRange: NSRange(location: glyph, length: 1),
                    in: container
                )
                x = origin.x + rect.minX
                y = origin.y + rect.minY
                height = max(rect.height, lineHeight)
            } else {
                let text = string as NSString
                if location > 0, text.character(at: location - 1) == 10 {
                    let extra = manager.extraLineFragmentRect
                    if extra.height > 0 {
                        y = origin.y + extra.minY
                        height = max(extra.height, lineHeight)
                    } else {
                        y = origin.y + manager.usedRect(for: container).maxY
                    }
                } else if location > 0 {
                    let glyph = manager.glyphIndexForCharacter(at: location - 1)
                    let rect = manager.boundingRect(
                        forGlyphRange: NSRange(location: glyph, length: 1),
                        in: container
                    )
                    x = origin.x + rect.maxX
                    y = origin.y + rect.minY
                    height = max(rect.height, lineHeight)
                }
            }
        }

        return NSRect(x: x - 1, y: y, width: 3, height: height)
    }

    private func invalidateCaret() {
        guard let rect = caretRectInView() else { return }
        setNeedsDisplay(rect.insetBy(dx: -4, dy: -1))
    }

    private func startCaretBlink() {
        caretTimer?.invalidate()
        caretVisible = true
        invalidateCaret()
        let timer = Timer(
            timeInterval: 0.5,
            target: self,
            selector: #selector(blinkCaret),
            userInfo: nil,
            repeats: true
        )
        RunLoop.main.add(timer, forMode: .common)
        caretTimer = timer
    }

    private func stopCaretBlink() {
        caretTimer?.invalidate()
        caretTimer = nil
        caretVisible = false
        invalidateCaret()
    }

    @objc private func blinkCaret() {
        caretVisible.toggle()
        invalidateCaret()
    }

    private var shouldDrawCaret: Bool {
        caretVisible
            && window?.isKeyWindow == true
            && selectedRange().length == 0
    }

    override func drawInsertionPoint(in rect: NSRect, color: NSColor, turnedOn flag: Bool) {
        // Custom caret is drawn in draw(_:) so it can be widened, snapped to
        // the text baseline and blinked independently of AppKit's 1pt bar.
    }

    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted { startCaretBlink() }
        return accepted
    }

    override func resignFirstResponder() -> Bool {
        stopCaretBlink()
        return super.resignFirstResponder()
    }

    override func mouseDown(with event: NSEvent) {
        super.mouseDown(with: event)
        startCaretBlink()
    }

    override func setSelectedRange(
        _ charRange: NSRange,
        affinity: NSSelectionAffinity,
        stillSelecting stillSelectingFlag: Bool
    ) {
        super.setSelectedRange(charRange, affinity: affinity, stillSelecting: stillSelectingFlag)
        startCaretBlink()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window == nil {
            stopCaretBlink()
        } else {
            invalidateCaret()
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        if string.isEmpty, !placeholder.isEmpty {
            let attrs: [NSAttributedString.Key: Any] = [
                .font: font ?? NSFont.systemFont(ofSize: 22),
                .foregroundColor: NSColor.tertiaryLabelColor
            ]
            // The text container inset is what centers an empty single-line
            // composer, so the placeholder shares the caret's baseline.
            (placeholder as NSString).draw(at: textContainerOrigin, withAttributes: attrs)
        }
        if shouldDrawCaret, let caret = caretRectInView() {
            NSColor.labelColor.setFill()
            NSBezierPath(roundedRect: caret, xRadius: 1, yRadius: 1).fill()
        }
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
        scrollRangeToVisible(selectedRange())
    }
}

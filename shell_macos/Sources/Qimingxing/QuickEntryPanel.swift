import AppKit
import Luminare
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
    private var panel: FloatingQuickEntryPanel?
    private weak var previousApp: NSRunningApplication?
    private let brainClient: BrainClient
    private let desktopActions: DesktopActions
    private let inputState = QuickEntryInputState()

    init(brainClient: BrainClient, desktopActions: DesktopActions) {
        self.brainClient = brainClient
        self.desktopActions = desktopActions
        super.init()
    }

    var isVisible: Bool {
        panel?.isVisible ?? false
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
        panel.center()
        panel.makeKeyAndOrderFront(nil)
        inputState.focusRequestToken = UUID()
    }

    func hideAndRestoreFocus() {
        panel?.orderOut(nil)
        if let prev = previousApp, !prev.isTerminated {
            prev.activate(options: [])
        }
        previousApp = nil
    }

    private func ensurePanel() {
        if panel != nil { return }
        let p = FloatingQuickEntryPanel(
            contentRect: NSRect(x: 0, y: 0, width: 700, height: 540),
            styleMask: [.nonactivatingPanel, .borderless, .resizable],
            backing: .buffered,
            defer: false
        )
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.isFloatingPanel = true
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        p.minSize = NSSize(width: 560, height: 420)
        p.delegate = self
        p.onEscapePressed = { [weak self] in
            self?.hideAndRestoreFocus()
        }

        let rootView = QuickEntryRootView(
            brainClient: brainClient,
            desktopActions: desktopActions,
            inputState: inputState,
            onClose: { [weak self] in
                self?.hideAndRestoreFocus()
            }
        )
        .luminareTint(overridingWith: QMXTheme.accent)

        let hosting = NSHostingController(rootView: rootView)
        hosting.view.wantsLayer = true
        hosting.view.layer?.backgroundColor = NSColor.clear.cgColor
        p.contentViewController = hosting
        self.panel = p
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
}

private struct QuickEntryRootView: View {
    @ObservedObject var brainClient: BrainClient
    let desktopActions: DesktopActions
    @ObservedObject var inputState: QuickEntryInputState
    let onClose: () -> Void

    @State private var isDropTargeted = false

    var body: some View {
        ZStack {
            VisualEffectBlur(material: .hudWindow, blendingMode: .behindWindow)

            VStack(spacing: 0) {
                // Spotlight-style Top Header Bar
                HStack(spacing: 10) {
                    HStack(spacing: 6) {
                        Image(systemName: "sparkles")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(QMXTheme.accent)
                        Text("启明星")
                            .font(.system(size: 13, weight: .semibold))
                        PulsingDotView(
                            color: statusColor,
                            isPulsing: brainClient.isGenerating || brainClient.phase == .starting
                        )
                    }

                    if !brainClient.profiles.isEmpty {
                        Picker("", selection: Binding(
                            get: { brainClient.activeProfile },
                            set: { brainClient.setActiveProfile($0) }
                        )) {
                            ForEach(brainClient.profiles) { p in
                                Text(p.label.isEmpty ? p.name : p.label).tag(p.name)
                            }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 210)
                    }

                    Spacer()

                    if brainClient.activeBgTaskCount > 0 {
                        HStack(spacing: 4) {
                            ProgressView().scaleEffect(0.5)
                            Text("后台 \(brainClient.activeBgTaskCount)")
                                .font(.caption.weight(.medium))
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(QMXTheme.accent.opacity(0.16)))
                        .foregroundStyle(QMXTheme.accent)
                    }

                    Button {
                        inputState.includeScreen.toggle()
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: inputState.includeScreen ? "display.and.arrow.down" : "display")
                            Text("看屏")
                        }
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 9)
                        .padding(.vertical, 5)
                    }
                    .buttonStyle(.luminare(tinted: inputState.includeScreen))

                    Button {
                        brainClient.clearHistory()
                    } label: {
                        Image(systemName: "trash")
                            .font(.caption)
                            .padding(6)
                    }
                    .buttonStyle(.luminare)
                    .help("清空当前对话")

                    Button(action: onClose) {
                        HStack(spacing: 4) {
                            KeyCapBadge("Esc")
                        }
                        .padding(.horizontal, 4)
                        .padding(.vertical, 4)
                    }
                    .buttonStyle(.plain)
                    .help("关闭并归还焦点")
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 11)

                Divider().opacity(0.5)

                if !brainClient.errorBannerText.isEmpty {
                    HStack(spacing: 8) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(QMXTheme.warning)
                        Text(brainClient.errorBannerText)
                            .font(.caption)
                            .lineLimit(2)
                        Spacer()
                        Button("关闭") { brainClient.errorBannerText = "" }
                            .font(.caption)
                            .buttonStyle(.borderless)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(QMXTheme.warning.opacity(0.14))
                }

                // Conversation Stream
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 14) {
                            if brainClient.messages.isEmpty {
                                emptyStateHero
                            } else {
                                ForEach(brainClient.messages) { msg in
                                    chatBubble(msg)
                                        .id(msg.id)
                                }
                            }

                            if !brainClient.artifactCapsules.isEmpty {
                                VStack(spacing: 8) {
                                    ForEach(brainClient.artifactCapsules.prefix(3)) { cap in
                                        artifactCapsuleRow(cap)
                                    }
                                }
                            }
                        }
                        .padding(16)
                    }
                    .onChange(of: brainClient.messages.last?.text) { _ in
                        if let last = brainClient.messages.last {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }

                if !brainClient.toolStatusText.isEmpty {
                    HStack(spacing: 8) {
                        ProgressView().scaleEffect(0.55)
                        Text(brainClient.toolStatusText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 6)
                    .background(Color.primary.opacity(0.04))
                }

                if !inputState.stagedAttachments.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(inputState.stagedAttachments) { att in
                                HStack(spacing: 6) {
                                    Image(systemName: att.kind == "image" ? "photo" : "doc.text")
                                        .font(.caption)
                                        .foregroundStyle(QMXTheme.accent)
                                    Text(att.filename)
                                        .font(.caption)
                                        .lineLimit(1)
                                    Button {
                                        inputState.stagedAttachments.removeAll { $0.artifactId == att.artifactId }
                                    } label: {
                                        Image(systemName: "xmark.circle.fill")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                    .buttonStyle(.plain)
                                }
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(
                                    Capsule().fill(QMXTheme.accent.opacity(0.14))
                                )
                                .overlay(
                                    Capsule().strokeBorder(QMXTheme.accent.opacity(0.3), lineWidth: 0.75)
                                )
                            }
                        }
                        .padding(.horizontal, 16)
                        .padding(.vertical, 6)
                    }
                }

                Divider().opacity(0.5)

                // Floating Glass Composer Dock
                HStack(alignment: .bottom, spacing: 10) {
                    Button {
                        pickAttachmentFile()
                    } label: {
                        Image(systemName: "paperclip")
                            .font(.system(size: 14, weight: .medium))
                            .frame(width: 30, height: 30)
                    }
                    .buttonStyle(.luminare)
                    .help("添加文件/截图附件（支持拖拽与 Cmd+V 粘贴）")

                    ChineseIMETextView(
                        text: $inputState.text,
                        focusToken: inputState.focusRequestToken,
                        onSubmit: { submitMessage() },
                        onEscape: { onClose() },
                        onPasteImage: { imageData in
                            Task { await uploadPastedImage(imageData) }
                        }
                    )
                    .frame(minHeight: 40, maxHeight: 115)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(Color.black.opacity(0.22))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.12), lineWidth: 1)
                    )

                    if brainClient.isGenerating {
                        Button {
                            brainClient.cancelActiveTurn()
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: "stop.fill")
                                    .foregroundStyle(QMXTheme.danger)
                                Text("停止")
                            }
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                        }
                        .buttonStyle(.luminare)
                    }

                    Button(action: submitMessage) {
                        HStack(spacing: 5) {
                            Text("发送")
                                .font(.system(size: 12.5, weight: .semibold))
                            KeyCapBadge("⏎")
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                    }
                    .buttonStyle(.luminare(tinted: true))
                    .disabled(
                        brainClient.phase != .connected ||
                        (inputState.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
                         inputState.stagedAttachments.isEmpty)
                    )
                }
                .padding(14)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: QMXTheme.panelRadius, style: .continuous))
        .overlay(
            HairlineBorderOverlay(cornerRadius: QMXTheme.panelRadius)
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.panelRadius, style: .continuous)
                .strokeBorder(isDropTargeted ? QMXTheme.accent : Color.clear, lineWidth: 2)
        )
        .onDrop(of: [UTType.fileURL], isTargeted: $isDropTargeted) { providers in
            handleDroppedProviders(providers)
        }
    }

    private var emptyStateHero: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "sparkles")
                    .font(.title2)
                    .foregroundStyle(QMXTheme.accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("有什么可以帮你的？")
                        .font(.system(size: 16, weight: .semibold))
                    Text("原生 macOS 调度 · 后台异步长任务 · 本地 3 层记忆")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 8) {
                suggestionChip("整理当前浏览器标签页并写到备忘录")
                suggestionChip("派发后台任务汇总今天的工作进展")
            }
            HStack(spacing: 8) {
                suggestionChip("查一下我未来 3 天的日历和待办事项")
                suggestionChip("总结刚才拖拽或粘贴的文件内容")
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: QMXTheme.cardRadius, style: .continuous)
                .fill(Color.primary.opacity(0.04))
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.cardRadius, style: .continuous)
                .strokeBorder(Color.white.opacity(0.08), lineWidth: 1)
        )
    }

    private func suggestionChip(_ prompt: String) -> some View {
        Button {
            inputState.text = prompt
            inputState.focusRequestToken = UUID()
        } label: {
            Text(prompt)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.luminare)
    }

    @ViewBuilder
    private func chatBubble(_ msg: ChatEntry) -> some View {
        VStack(alignment: msg.isUser ? .trailing : .leading, spacing: 4) {
            Text(msg.meta)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 4)

            Group {
                if msg.isUser {
                    Text(msg.text)
                        .font(.system(size: 13.5))
                        .textSelection(.enabled)
                        .padding(.horizontal, 13)
                        .padding(.vertical, 9)
                        .background(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(QMXTheme.accent.opacity(0.22))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .strokeBorder(QMXTheme.accent.opacity(0.35), lineWidth: 0.8)
                        )
                } else if msg.text.isEmpty && msg.isStreaming {
                    HStack(spacing: 6) {
                        ProgressView().scaleEffect(0.55)
                        Text("思考与调度中…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 13)
                    .padding(.vertical, 9)
                    .background(
                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .fill(Color.primary.opacity(0.06))
                    )
                } else {
                    MarkdownMessageView(text: msg.text)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(Color.primary.opacity(0.06))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .strokeBorder(Color.white.opacity(0.08), lineWidth: 0.8)
                        )
                }
            }
            .frame(maxWidth: 560, alignment: msg.isUser ? .trailing : .leading)
        }
        .frame(maxWidth: .infinity, alignment: msg.isUser ? .trailing : .leading)
    }

    private func artifactCapsuleRow(_ cap: ArtifactCapsuleItem) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "doc.richtext.fill")
                .foregroundStyle(QMXTheme.accent)
            VStack(alignment: .leading, spacing: 1) {
                Text(cap.title)
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
                Text(cap.path)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            Button("快速预览") { _ = desktopActions.quickLook(cap.path) }
                .font(.caption)
                .buttonStyle(.luminare(tinted: true))
            Button("复制") { _ = desktopActions.copyFile(cap.path) }
                .font(.caption)
                .buttonStyle(.luminare)
            Button("访达") { _ = desktopActions.revealPath(cap.path) }
                .font(.caption)
                .buttonStyle(.luminare)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: QMXTheme.cardRadius, style: .continuous)
                .fill(QMXTheme.accent.opacity(0.09))
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.cardRadius, style: .continuous)
                .strokeBorder(QMXTheme.accent.opacity(0.28), lineWidth: 0.8)
        )
    }

    private var statusColor: Color {
        switch brainClient.phase {
        case .connected: return QMXTheme.success
        case .starting: return QMXTheme.warning
        case .offline, .error: return QMXTheme.danger
        }
    }

    private func submitMessage() {
        let msg = inputState.text
        let atts = inputState.stagedAttachments
        let withScreen = inputState.includeScreen
        guard !msg.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !atts.isEmpty else { return }
        inputState.text = ""
        inputState.stagedAttachments = []
        brainClient.sendText(msg, attachments: atts, includeScreen: withScreen)
    }

    private func pickAttachmentFile() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        if panel.runModal() == .OK {
            for url in panel.urls {
                Task {
                    if let att = await brainClient.uploadArtifact(fileURL: url) {
                        inputState.stagedAttachments.append(att)
                    }
                }
            }
        }
    }

    private func uploadPastedImage(_ data: Data) async {
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("qmx_paste_\(UUID().uuidString.prefix(8)).png")
        try? data.write(to: tmp)
        if let att = await brainClient.uploadArtifact(fileURL: tmp) {
            inputState.stagedAttachments.append(att)
        }
        try? FileManager.default.removeItem(at: tmp)
    }

    private func handleDroppedProviders(_ providers: [NSItemProvider]) -> Bool {
        var accepted = false
        for prov in providers where prov.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            accepted = true
            prov.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                if let data = item as? Data,
                   let url = URL(dataRepresentation: data, relativeTo: nil) {
                    Task { @MainActor in
                        if let att = await brainClient.uploadArtifact(fileURL: url) {
                            inputState.stagedAttachments.append(att)
                        }
                    }
                }
            }
        }
        return accepted
    }
}

private struct ChineseIMETextView: NSViewRepresentable {
    @Binding var text: String
    let focusToken: UUID
    let onSubmit: () -> Void
    let onEscape: () -> Void
    let onPasteImage: (Data) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false

        let tv = QMXTextView()
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.allowsUndo = true
        tv.font = NSFont.systemFont(ofSize: 14)
        tv.drawsBackground = false
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.textContainer?.widthTracksTextView = true
        tv.onSubmit = onSubmit
        tv.onEscape = onEscape
        tv.onPasteImage = onPasteImage

        scroll.documentView = tv
        context.coordinator.textView = tv
        return scroll
    }

    func updateNSView(_ nsView: NSScrollView, context: Context) {
        guard let tv = context.coordinator.textView else { return }
        // Never overwrite while IME marked text is active
        if !tv.hasMarkedText() && tv.string != text {
            tv.string = text
        }
        tv.onSubmit = onSubmit
        tv.onEscape = onEscape
        tv.onPasteImage = onPasteImage
        if context.coordinator.lastFocusToken != focusToken {
            context.coordinator.lastFocusToken = focusToken
            DispatchQueue.main.async {
                tv.window?.makeFirstResponder(tv)
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
            guard let tv = textView, !tv.hasMarkedText() else { return }
            parent.text = tv.string
        }
    }
}

private final class QMXTextView: NSTextView {
    var onSubmit: (() -> Void)?
    var onEscape: (() -> Void)?
    var onPasteImage: ((Data) -> Void)?

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

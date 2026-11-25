import AppKit
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
            contentRect: NSRect(x: 0, y: 0, width: 680, height: 500),
            styleMask: [.nonactivatingPanel, .titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        p.title = "启明星 · Quick Entry"
        p.titleVisibility = .hidden
        p.titlebarAppearsTransparent = true
        p.isFloatingPanel = true
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
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
        let hosting = NSHostingController(rootView: rootView)
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
        VStack(spacing: 0) {
            // Top bar: connection, model picker, screen toggle, close
            HStack(spacing: 10) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                Text(statusText)
                    .font(.caption)
                    .foregroundColor(.secondary)

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
                    Label("后台任务 \(brainClient.activeBgTaskCount)", systemImage: "gearshape.2.fill")
                        .font(.caption)
                        .foregroundColor(.cyan)
                }

                Toggle(isOn: $inputState.includeScreen) {
                    Label("看屏", systemImage: "display")
                        .font(.caption)
                }
                .toggleStyle(.button)

                Button {
                    brainClient.clearHistory()
                } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .help("清空当前对话")

                Button(action: onClose) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.borderless)
                .help("关闭 (Esc)")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)

            Divider()

            if !brainClient.errorBannerText.isEmpty {
                HStack {
                    Text(brainClient.errorBannerText)
                        .font(.caption)
                        .foregroundColor(.orange)
                    Spacer()
                    Button("关闭") { brainClient.errorBannerText = "" }
                        .font(.caption)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Color.orange.opacity(0.12))
            }

            // Conversation stream
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        if brainClient.messages.isEmpty {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("有什么可以帮你的？")
                                    .font(.headline)
                                Text("支持直接对话、拖入/粘贴文件截图、调用 macOS 备忘录/日历/提醒事项/浏览器，或派发后台长任务。")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            .padding(.top, 20)
                        } else {
                            ForEach(brainClient.messages) { msg in
                                VStack(alignment: msg.isUser ? .trailing : .leading, spacing: 4) {
                                    Text(msg.meta)
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                    Text(msg.text.isEmpty && msg.isStreaming ? "思考中…" : msg.text)
                                        .textSelection(.enabled)
                                        .padding(10)
                                        .background(msg.isUser ? Color.accentColor.opacity(0.22) : Color(NSColor.controlBackgroundColor))
                                        .cornerRadius(10)
                                }
                                .frame(maxWidth: .infinity, alignment: msg.isUser ? .trailing : .leading)
                                .id(msg.id)
                            }
                        }

                        if !brainClient.artifactCapsules.isEmpty {
                            ForEach(brainClient.artifactCapsules.prefix(3)) { cap in
                                HStack {
                                    Image(systemName: "doc.richtext")
                                        .foregroundColor(.accentColor)
                                    Text(cap.title)
                                        .font(.caption)
                                        .lineLimit(1)
                                    Spacer()
                                    Button("快速预览") { _ = desktopActions.quickLook(cap.path) }
                                        .font(.caption)
                                    Button("复制文件") { _ = desktopActions.copyFile(cap.path) }
                                        .font(.caption)
                                    Button("访达定位") { _ = desktopActions.revealPath(cap.path) }
                                        .font(.caption)
                                }
                                .padding(8)
                                .background(Color.accentColor.opacity(0.08))
                                .cornerRadius(8)
                            }
                        }
                    }
                    .padding(14)
                }
                .onChange(of: brainClient.messages.last?.text) { _ in
                    if let last = brainClient.messages.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }

            if !brainClient.toolStatusText.isEmpty {
                HStack(spacing: 6) {
                    ProgressView().scaleEffect(0.6)
                    Text(brainClient.toolStatusText)
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Spacer()
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 4)
            }

            if !inputState.stagedAttachments.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(inputState.stagedAttachments) { att in
                            HStack(spacing: 4) {
                                Text(att.filename)
                                    .font(.caption)
                                Button {
                                    inputState.stagedAttachments.removeAll { $0.artifactId == att.artifactId }
                                } label: {
                                    Image(systemName: "xmark")
                                        .font(.caption2)
                                }
                                .buttonStyle(.borderless)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(Color.accentColor.opacity(0.15))
                            .cornerRadius(12)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 6)
                }
            }

            Divider()

            // Native IME-friendly multiline composer
            HStack(alignment: .bottom, spacing: 8) {
                Button {
                    pickAttachmentFile()
                } label: {
                    Image(systemName: "paperclip")
                }
                .buttonStyle(.borderless)
                .help("添加文件/图片附件")

                ChineseIMETextView(
                    text: $inputState.text,
                    focusToken: inputState.focusRequestToken,
                    onSubmit: { submitMessage() },
                    onEscape: { onClose() },
                    onPasteImage: { imageData in
                        Task { await uploadPastedImage(imageData) }
                    }
                )
                .frame(minHeight: 42, maxHeight: 110)
                .padding(6)
                .background(Color(NSColor.textBackgroundColor))
                .cornerRadius(8)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.secondary.opacity(0.25), lineWidth: 1)
                )

                if brainClient.isGenerating {
                    Button("停止") {
                        brainClient.cancelActiveTurn()
                    }
                    .buttonStyle(.bordered)
                    .tint(.red)
                }

                Button("发送") {
                    submitMessage()
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    brainClient.phase != .connected ||
                    (inputState.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
                     inputState.stagedAttachments.isEmpty)
                )
            }
            .padding(12)
        }
        .background(.ultraThinMaterial)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(isDropTargeted ? Color.accentColor : Color.clear, lineWidth: 2)
        )
        .onDrop(of: [UTType.fileURL], isTargeted: $isDropTargeted) { providers in
            handleDroppedProviders(providers)
        }
    }

    private var statusColor: Color {
        switch brainClient.phase {
        case .connected: return .green
        case .starting: return .yellow
        case .offline, .error: return .red
        }
    }

    private var statusText: String {
        switch brainClient.phase {
        case .connected: return "已连接"
        case .starting: return "启动中…"
        case .offline: return "已离线（重连中）"
        case .error: return "异常"
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

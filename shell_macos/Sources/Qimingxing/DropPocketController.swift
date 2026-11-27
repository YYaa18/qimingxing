import AppKit
import Combine
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Geometry & Data Models

enum DropPocketGeometry {
    static let width: CGFloat = 220
    static let emptyHeight: CGFloat = 200
    static let maxHeight: CGFloat = 280
    static let cornerRadius: CGFloat = 18

    static func panelHeight(forItemCount count: Int) -> CGFloat {
        guard count > 0 else { return emptyHeight }
        return min(maxHeight, 146 + CGFloat(count) * 30)
    }

    static func clampedOrigin(
        mouseLocation: NSPoint,
        panelSize: NSSize,
        visibleFrame: NSRect,
        offset: CGFloat = 24,
        margin: CGFloat = 12
    ) -> NSPoint {
        let minX = visibleFrame.minX + margin
        let maxX = visibleFrame.maxX - panelSize.width - margin
        let x: CGFloat
        if maxX < minX {
            x = visibleFrame.midX - panelSize.width / 2
        } else {
            x = min(max(mouseLocation.x + offset, minX), maxX)
        }

        let minY = visibleFrame.minY + margin
        let maxY = visibleFrame.maxY - panelSize.height - margin
        let y: CGFloat
        if maxY < minY {
            y = visibleFrame.midY - panelSize.height / 2
        } else {
            y = min(max(mouseLocation.y + offset, minY), maxY)
        }

        return NSPoint(x: x, y: y)
    }
}

enum DropPocketQuickSlot: Int, CaseIterable, Identifiable {
    case summary = 0
    case extract = 1

    var id: Int { rawValue }

    var title: String {
        switch self {
        case .summary: return "速览摘要"
        case .extract: return "提取要点"
        }
    }

    var symbol: String {
        switch self {
        case .summary: return "doc.text.magnifyingglass"
        case .extract: return "list.bullet.rectangle"
        }
    }

    var promptPrefix: String {
        switch self {
        case .summary:
            return "请对以下素材做一份简洁清晰的速览摘要，提炼核心结论与关键信息："
        case .extract:
            return "请从以下素材中结构化提取关键要点、数据或待办事项："
        }
    }
}

enum DropPocketHoverTarget: Equatable {
    case none
    case mainZone
    case quickSlot(DropPocketQuickSlot)
}

struct DropPocketItem: Identifiable, Equatable {
    let id: UUID
    let filename: String
    let kind: String
    var isUploading: Bool
    var attachment: StagedAttachment?
    var textSnippet: String?
    var sourceURL: URL?
}

// MARK: - Non-Activating Panel & Native NSDraggingDestination Host

@MainActor
final class DropPocketPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private struct ExtractedDragPayload {
    var fileURLs: [URL] = []
    var imageDataList: [Data] = []
    var textSnippets: [String] = []

    var isEmpty: Bool {
        fileURLs.isEmpty && imageDataList.isEmpty && textSnippets.isEmpty
    }
}

private final class DropPocketHostView: NSView {
    var onHoverChanged: ((DropPocketHoverTarget) -> Void)?
    var onDropped: ((DropPocketHoverTarget, ExtractedDragPayload) -> Void)?

    private var currentHover: DropPocketHoverTarget = .none

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        registerForDraggedTypes([
            .fileURL,
            .png,
            .tiff,
            .URL,
            .string,
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draggingEntered(_ sender: any NSDraggingInfo) -> NSDragOperation {
        let target = resolveHoverTarget(for: sender)
        updateHover(target)
        return .copy
    }

    override func draggingUpdated(_ sender: any NSDraggingInfo) -> NSDragOperation {
        let target = resolveHoverTarget(for: sender)
        updateHover(target)
        return .copy
    }

    override func draggingExited(_ sender: (any NSDraggingInfo)?) {
        updateHover(.none)
    }

    override func draggingEnded(_ sender: any NSDraggingInfo) {
        updateHover(.none)
    }

    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        let target = resolveHoverTarget(for: sender)
        let payload = extractPayload(from: sender.draggingPasteboard)
        updateHover(.none)
        guard !payload.isEmpty else { return false }
        NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
        onDropped?(target, payload)
        return true
    }

    private func updateHover(_ next: DropPocketHoverTarget) {
        guard next != currentHover else { return }
        if next != .none {
            NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
        }
        currentHover = next
        onHoverChanged?(next)
    }

    private func resolveHoverTarget(for sender: any NSDraggingInfo) -> DropPocketHoverTarget {
        let pt = convert(sender.draggingLocation, from: nil)
        // Quick-action slots sit in the bottom 54pt strip (side-by-side)
        if pt.y <= 54 {
            let slot: DropPocketQuickSlot = pt.x < bounds.width / 2 ? .summary : .extract
            return .quickSlot(slot)
        }
        return .mainZone
    }

    private func extractPayload(from pb: NSPasteboard) -> ExtractedDragPayload {
        var result = ExtractedDragPayload()
        if let urls = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] {
            result.fileURLs.append(contentsOf: urls)
        }
        if result.fileURLs.isEmpty {
            if let png = pb.data(forType: .png) {
                result.imageDataList.append(png)
            } else if let tiff = pb.data(forType: .tiff),
                      let rep = NSBitmapImageRep(data: tiff),
                      let png = rep.representation(using: .png, properties: [:]) {
                result.imageDataList.append(png)
            }
        }
        if result.fileURLs.isEmpty && result.imageDataList.isEmpty {
            if let urlString = pb.string(forType: .URL), !urlString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                result.textSnippets.append(urlString.trimmingCharacters(in: .whitespacesAndNewlines))
            } else if let str = pb.string(forType: .string), !str.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                result.textSnippets.append(str.trimmingCharacters(in: .whitespacesAndNewlines))
            }
        }
        return result
    }
}

// MARK: - Controller

@MainActor
final class DropPocketController: ObservableObject {
    @Published private(set) var items: [DropPocketItem] = []
    @Published private(set) var isVisible: Bool = false
    @Published var hoverTarget: DropPocketHoverTarget = .none

    private var panel: DropPocketPanel?
    private let brainClient: BrainClient

    var onDeliverToQuickEntry: (([StagedAttachment], [String]) -> Void)?
    var onPendingCountChanged: ((Int) -> Void)?

    init(brainClient: BrainClient) {
        self.brainClient = brainClient
    }

    var pendingCount: Int {
        items.count
    }

    var isUploadingAny: Bool {
        items.contains(where: \.isUploading)
    }

    func toggleAtCursor() {
        if isVisible {
            if items.isEmpty {
                hide()
            } else {
                repositionNearCursor()
                panel?.orderFrontRegardless()
            }
        } else {
            showAtCursor()
        }
    }

    func showAtCursor() {
        ensurePanel()
        guard let panel else { return }
        updatePanelSize(animated: false)
        repositionNearCursor()
        panel.orderFrontRegardless()
        isVisible = true
    }

    func hide() {
        hoverTarget = .none
        panel?.orderOut(nil)
        isVisible = false
    }

    func clear() {
        items.removeAll()
        onPendingCountChanged?(0)
        updatePanelSize(animated: true)
    }

    func removeItem(id: UUID) {
        items.removeAll { $0.id == id }
        onPendingCountChanged?(items.count)
        updatePanelSize(animated: true)
    }

    func deliverToQuickEntry() {
        let attachments = items.compactMap(\.attachment)
        let snippets = items.compactMap(\.textSnippet)
        guard !attachments.isEmpty || !snippets.isEmpty else { return }
        clear()
        hide()
        onDeliverToQuickEntry?(attachments, snippets)
    }

    private func ensurePanel() {
        guard panel == nil else { return }
        let size = NSSize(width: DropPocketGeometry.width, height: DropPocketGeometry.emptyHeight)
        let p = DropPocketPanel(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        p.title = "投喂口袋"
        p.isFloatingPanel = true
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true

        let hostView = DropPocketHostView(frame: NSRect(origin: .zero, size: size))
        hostView.wantsLayer = true
        hostView.layer?.backgroundColor = NSColor.clear.cgColor
        hostView.onHoverChanged = { [weak self] target in
            self?.hoverTarget = target
        }
        hostView.onDropped = { [weak self] target, payload in
            self?.handleDrop(target: target, payload: payload)
        }

        let swiftUIView = DropPocketView(
            controller: self,
            onClose: { [weak self] in self?.hide() }
        )
        let hosting = NSHostingView(rootView: swiftUIView)
        hosting.translatesAutoresizingMaskIntoConstraints = false
        if #available(macOS 13.3, *) {
            hosting.safeAreaRegions = []
        }
        hostView.addSubview(hosting)
        NSLayoutConstraint.activate([
            hosting.topAnchor.constraint(equalTo: hostView.topAnchor),
            hosting.leadingAnchor.constraint(equalTo: hostView.leadingAnchor),
            hosting.trailingAnchor.constraint(equalTo: hostView.trailingAnchor),
            hosting.bottomAnchor.constraint(equalTo: hostView.bottomAnchor),
        ])

        p.contentView = hostView
        p.setContentSize(size)
        self.panel = p
    }

    private func repositionNearCursor() {
        guard let panel else { return }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let origin = DropPocketGeometry.clampedOrigin(
            mouseLocation: mouse,
            panelSize: panel.frame.size,
            visibleFrame: visible
        )
        panel.setFrameOrigin(origin)
    }

    private func updatePanelSize(animated: Bool) {
        guard let panel else { return }
        let targetHeight = DropPocketGeometry.panelHeight(forItemCount: items.count)
        var frame = panel.frame
        let topEdge = frame.maxY
        frame.size = NSSize(width: DropPocketGeometry.width, height: targetHeight)
        frame.origin.y = topEdge - targetHeight

        if !animated || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            panel.setFrame(frame, display: true)
            panel.invalidateShadow()
        } else {
            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.18
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }, completionHandler: { [weak self] in
                Task { @MainActor [weak self] in
                    self?.panel?.invalidateShadow()
                }
            })
        }
    }

    private func handleDrop(target: DropPocketHoverTarget, payload: ExtractedDragPayload) {
        switch target {
        case .quickSlot(let slot):
            executeQuickSlot(slot, payload: payload)
        case .mainZone, .none:
            ingestDropped(payload: payload)
        }
    }

    private func ingestDropped(payload: ExtractedDragPayload) {
        for url in payload.fileURLs {
            let itemId = UUID()
            let ext = url.pathExtension.lowercased()
            let kind = inferKind(ext: ext)
            items.append(
                DropPocketItem(
                    id: itemId,
                    filename: url.lastPathComponent,
                    kind: kind,
                    isUploading: true,
                    sourceURL: url
                )
            )
            onPendingCountChanged?(items.count)
            updatePanelSize(animated: true)
            Task { [weak self] in
                guard let self else { return }
                let uploaded = await self.brainClient.uploadArtifact(fileURL: url)
                if let idx = self.items.firstIndex(where: { $0.id == itemId }) {
                    self.items[idx].isUploading = false
                    self.items[idx].attachment = uploaded
                    if uploaded == nil {
                        self.items.remove(at: idx)
                        self.onPendingCountChanged?(self.items.count)
                        self.updatePanelSize(animated: true)
                    }
                }
            }
        }

        for (index, png) in payload.imageDataList.enumerated() {
            let itemId = UUID()
            let name = "拖入图片-\(index + 1).png"
            items.append(
                DropPocketItem(
                    id: itemId,
                    filename: name,
                    kind: "image",
                    isUploading: true
                )
            )
            onPendingCountChanged?(items.count)
            updatePanelSize(animated: true)
            Task { [weak self] in
                guard let self else { return }
                let tmp = FileManager.default.temporaryDirectory
                    .appendingPathComponent("qmx_pocket_\(UUID().uuidString.prefix(8)).png")
                try? png.write(to: tmp)
                let uploaded = await self.brainClient.uploadArtifact(fileURL: tmp)
                try? FileManager.default.removeItem(at: tmp)
                if let idx = self.items.firstIndex(where: { $0.id == itemId }) {
                    self.items[idx].isUploading = false
                    self.items[idx].attachment = uploaded
                    if uploaded == nil {
                        self.items.remove(at: idx)
                        self.onPendingCountChanged?(self.items.count)
                        self.updatePanelSize(animated: true)
                    }
                }
            }
        }

        for snippet in payload.textSnippets {
            let itemId = UUID()
            let label = String(snippet.prefix(40))
            items.append(
                DropPocketItem(
                    id: itemId,
                    filename: label,
                    kind: "text",
                    isUploading: false,
                    textSnippet: snippet
                )
            )
            onPendingCountChanged?(items.count)
            updatePanelSize(animated: true)
        }
    }

    private func executeQuickSlot(_ slot: DropPocketQuickSlot, payload: ExtractedDragPayload) {
        let existingAttachments = items.compactMap(\.attachment)
        let existingSnippets = items.compactMap(\.textSnippet)
        clear()
        hide()

        Task { [weak self] in
            guard let self else { return }
            var uploadedAttachments = existingAttachments
            for url in payload.fileURLs {
                if let att = await self.brainClient.uploadArtifact(fileURL: url) {
                    uploadedAttachments.append(att)
                }
            }
            for png in payload.imageDataList {
                let tmp = FileManager.default.temporaryDirectory
                    .appendingPathComponent("qmx_pocket_slot_\(UUID().uuidString.prefix(8)).png")
                try? png.write(to: tmp)
                if let att = await self.brainClient.uploadArtifact(fileURL: tmp) {
                    uploadedAttachments.append(att)
                }
                try? FileManager.default.removeItem(at: tmp)
            }

            let allSnippets = existingSnippets + payload.textSnippets
            guard !uploadedAttachments.isEmpty || !allSnippets.isEmpty else { return }

            var prompt = slot.promptPrefix
            if !allSnippets.isEmpty {
                prompt += "\n\n" + allSnippets.joined(separator: "\n---\n")
            }
            self.brainClient.sendText(prompt, attachments: uploadedAttachments, includeScreen: false)
            TaskToastController.shared.show(
                title: "已派发 · \(slot.title)",
                message: "正在后台处理素材，完成后将在工作区与通知提示",
                isError: false
            ) {
                NotificationCenter.default.post(name: .qmxOpenWorkspace, object: nil)
            }
        }
    }

    private func inferKind(ext: String) -> String {
        switch ext {
        case "png", "jpg", "jpeg", "webp", "gif", "heic": return "image"
        case "mp3", "wav", "m4a", "aac": return "audio"
        case "mp4", "mov", "m4v": return "video"
        case "pdf": return "pdf"
        default: return "file"
        }
    }
}

// MARK: - SwiftUI View

struct DropPocketView: View {
    @ObservedObject var controller: DropPocketController
    let onClose: () -> Void

    var body: some View {
        ZStack {
            VisualEffectBlur(
                material: .popover,
                blendingMode: .behindWindow,
                cornerRadius: DropPocketGeometry.cornerRadius
            )

            VStack(spacing: 8) {
                header
                mainZone
                quickSlotsRow
            }
            .padding(10)
        }
        .frame(width: DropPocketGeometry.width)
        .frame(maxHeight: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: DropPocketGeometry.cornerRadius, style: .continuous))
        .overlay(HairlineBorderOverlay(cornerRadius: DropPocketGeometry.cornerRadius))
    }

    private var header: some View {
        HStack(spacing: 6) {
            MorningStarMark(size: 13, color: QMXTheme.textSecondary)
            Text("投喂口袋")
                .font(.system(size: 11.5, weight: .semibold))
                .foregroundStyle(QMXTheme.textPrimary)
            if controller.pendingCount > 0 {
                Text("\(controller.pendingCount)")
                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(QMXTheme.accent)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(QMXTheme.accentSoft))
            }
            Spacer(minLength: 0)
            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 9.5, weight: .semibold))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .frame(width: 18, height: 18)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("收起投喂口袋 (⌥D)")
        }
        .padding(.horizontal, 2)
    }

    private var mainZone: some View {
        let isHovered = controller.hoverTarget == .mainZone
        return VStack(spacing: 6) {
            if controller.items.isEmpty {
                Spacer(minLength: 0)
                Image(systemName: "tray.and.arrow.down")
                    .font(.system(size: 20, weight: .light))
                    .foregroundStyle(isHovered ? QMXTheme.accent : QMXTheme.textTertiary)
                Text("拖入文件、图片或文本")
                    .font(.system(size: 11))
                    .foregroundStyle(isHovered ? QMXTheme.textPrimary : QMXTheme.textSecondary)
                KeyCapBadge("⌥D")
                Spacer(minLength: 0)
            } else {
                ScrollView {
                    VStack(spacing: 4) {
                        ForEach(controller.items) { item in
                            itemRow(item)
                        }
                    }
                }
                .scrollIndicators(.hidden)

                HStack(spacing: 6) {
                    Button("清空") {
                        controller.clear()
                    }
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .buttonStyle(.plain)
                    .padding(.horizontal, 8)
                    .frame(height: 24)
                    .background(Capsule().fill(QMXTheme.surfaceMuted.opacity(0.6)))

                    Spacer(minLength: 0)

                    Button {
                        controller.deliverToQuickEntry()
                    } label: {
                        Text("带入对话")
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(QMXTheme.primaryText)
                            .padding(.horizontal, 10)
                            .frame(height: 24)
                            .background(Capsule().fill(QMXTheme.primaryFill))
                    }
                    .buttonStyle(.plain)
                    .disabled(controller.isUploadingAny)
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(QMXTheme.surfaceMuted.opacity(isHovered ? 0.45 : 0.25))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(
                    isHovered ? QMXTheme.accent : QMXTheme.hairline,
                    lineWidth: isHovered ? 1.5 : 0.5
                )
        )
    }

    private func itemRow(_ item: DropPocketItem) -> some View {
        HStack(spacing: 6) {
            Image(systemName: symbol(for: item.kind))
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(QMXTheme.accent)
                .frame(width: 14)

            Text(item.filename)
                .font(.system(size: 10.5))
                .foregroundStyle(QMXTheme.textPrimary)
                .lineLimit(1)

            Spacer(minLength: 0)

            if item.isUploading {
                ProgressView()
                    .controlSize(.mini)
                    .scaleEffect(0.6)
                    .frame(width: 12, height: 12)
            } else {
                Button {
                    controller.removeItem(id: item.id)
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 8.5, weight: .semibold))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .frame(width: 14, height: 14)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 7)
        .frame(height: 24)
        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(QMXTheme.surfaceRaised.opacity(0.7)))
    }

    private var quickSlotsRow: some View {
        HStack(spacing: 6) {
            ForEach(DropPocketQuickSlot.allCases) { slot in
                let isHovered = controller.hoverTarget == .quickSlot(slot)
                VStack(spacing: 2) {
                    Image(systemName: slot.symbol)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(isHovered ? QMXTheme.accent : QMXTheme.textSecondary)
                    Text(slot.title)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(isHovered ? QMXTheme.textPrimary : QMXTheme.textSecondary)
                }
                .frame(maxWidth: .infinity)
                .frame(height: 44)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(isHovered ? QMXTheme.accentSoft : QMXTheme.surfaceRaised.opacity(0.85))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(isHovered ? QMXTheme.accent : QMXTheme.hairline, lineWidth: isHovered ? 1.5 : 0.5)
                )
            }
        }
    }

    private func symbol(for kind: String) -> String {
        switch kind {
        case "image": return "photo"
        case "audio": return "waveform"
        case "video": return "film"
        case "pdf": return "doc.richtext"
        case "text": return "text.quote"
        default: return "doc"
        }
    }
}

import AppKit
import AVKit
import PDFKit
import SwiftUI

enum MediaViewerMode {
    case attached
    case detached
}

@MainActor
final class MediaViewerModel: ObservableObject {
    static let shared = MediaViewerModel()

    @Published private(set) var items: [MediaItem] = []
    @Published private(set) var selectedID: String?
    @Published private(set) var title: String = ""
    @Published private(set) var contextLabel: String = ""
    @Published private(set) var pendingItems: [MediaItem] = []
    @Published private(set) var pendingTitle: String = ""

    var selectedItem: MediaItem? {
        guard let selectedID else { return nil }
        return items.first { $0.mediaId == selectedID }
    }

    func load(items: [MediaItem], selectedID: String?, title: String, contextLabel: String) {
        self.items = items
        self.title = title
        self.contextLabel = contextLabel
        pendingItems = []
        pendingTitle = ""
        if let selectedID, items.contains(where: { $0.mediaId == selectedID }) {
            self.selectedID = selectedID
        } else {
            self.selectedID = items.first?.mediaId
        }
    }

    func select(_ id: String) {
        guard items.contains(where: { $0.mediaId == id }) else { return }
        selectedID = id
    }

    func selectOffset(_ offset: Int) {
        guard !items.isEmpty else { return }
        let current = items.firstIndex { $0.mediaId == selectedID } ?? 0
        let next = min(items.count - 1, max(0, current + offset))
        selectedID = items[next].mediaId
    }

    func offerPending(items: [MediaItem], title: String) {
        guard !items.isEmpty else { return }
        guard items.map(\.mediaId) != self.items.map(\.mediaId) else {
            pendingItems = []
            pendingTitle = ""
            return
        }
        pendingItems = items
        pendingTitle = title
    }

    func acceptPending() {
        guard !pendingItems.isEmpty else { return }
        let next = pendingItems
        let nextTitle = pendingTitle.isEmpty ? title : pendingTitle
        pendingItems = []
        pendingTitle = ""
        load(
            items: next,
            selectedID: next.first?.mediaId,
            title: nextTitle,
            contextLabel: "本轮回答"
        )
    }

    func dismissPending() {
        pendingItems = []
        pendingTitle = ""
    }
}

@MainActor
enum MediaViewerLauncher {
    /// Installed by AppDelegate. It decides between the attached wing and the
    /// detached window based on whether Quick Entry is currently visible.
    static var openHandler: ((Bool) -> Void)?
    static var attachHandler: (() -> Void)?

    static func open(
        items: [MediaItem],
        selectedID: String?,
        title: String,
        contextLabel: String,
        prefersLargeWindow: Bool = false
    ) {
        guard !items.isEmpty else { return }
        MediaViewerModel.shared.load(
            items: items,
            selectedID: selectedID,
            title: title,
            contextLabel: contextLabel
        )
        if let openHandler {
            openHandler(prefersLargeWindow)
        } else {
            MediaViewerWindowController.shared.show(large: prefersLargeWindow)
        }
    }

    static func dock() {
        MediaViewerWindowController.shared.close()
        attachHandler?()
    }
}

@MainActor
final class MediaViewerWindow: NSWindow {
    var onKeyDown: ((NSEvent) -> Bool)?

    override func sendEvent(_ event: NSEvent) {
        if event.type == .keyDown, onKeyDown?(event) == true {
            return
        }
        super.sendEvent(event)
    }
}

@MainActor
final class MediaViewerWindowController: NSObject, NSWindowDelegate {
    static let shared = MediaViewerWindowController()

    private var window: MediaViewerWindow?
    private var windowLease: WindowLease?
    /// Injected by AppDelegate; nil in previews so they keep the old behavior.
    var windowCoordinator: WindowCoordinator?

    var isVisible: Bool { window?.isVisible ?? false }

    func show(large: Bool = false) {
        let wantsLarge = large || (MediaViewerModel.shared.selectedItem?.isMarkdownTable ?? false)
        windowLease = windowCoordinator?.present(
            WindowRequest(role: .mediaDetached, activation: .required)
        )
        if let window {
            if wantsLarge {
                expandForTable(window)
            }
            window.makeKeyAndOrderFront(nil)
            if windowCoordinator == nil {
                NSApp.activate(ignoringOtherApps: true)
            }
            return
        }
        let host = NSHostingController(
            rootView: MediaViewerContent(model: MediaViewerModel.shared, mode: .detached)
        )
        host.sizingOptions = []
        let window = MediaViewerWindow(
            contentRect: NSRect(x: 0, y: 0, width: 860, height: 620),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "媒体预览"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 640, height: 480)
        window.contentViewController = host
        window.delegate = self
        window.setContentSize(NSSize(width: 860, height: 620))
        window.setFrameAutosaveName("QMXMediaViewerWindow")
        if wantsLarge {
            expandForTable(window)
        } else if !window.setFrameUsingName("QMXMediaViewerWindow") {
            window.center()
        }
        window.onKeyDown = { [weak self] event in
            self?.handleKey(event) ?? false
        }
        self.window = window
        window.makeKeyAndOrderFront(nil)
        if windowCoordinator == nil {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func expandForTable(_ window: NSWindow) {
        let screen = window.screen ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let targetWidth = min(1180, max(960, visible.width - 120))
        let targetHeight = min(820, max(680, visible.height - 140))
        let target = NSRect(
            x: visible.midX - targetWidth / 2,
            y: visible.midY - targetHeight / 2,
            width: targetWidth,
            height: targetHeight
        )
        window.setFrame(target, display: true)
    }

    private func handleKey(_ event: NSEvent) -> Bool {
        switch event.keyCode {
        case 123: // left
            MediaViewerModel.shared.selectOffset(-1)
            return true
        case 124: // right
            MediaViewerModel.shared.selectOffset(1)
            return true
        case 49: // space
            NotificationCenter.default.post(name: .qmxToggleMediaPlayback, object: nil)
            return true
        case 53: // escape
            close()
            return true
        default:
            return false
        }
    }

    func close() {
        window?.close()
        window = nil
    }

    func windowWillClose(_ notification: Notification) {
        NotificationCenter.default.post(name: .qmxStopMediaPlayback, object: nil)
        NotificationCenter.default.post(name: .qmxMediaViewerClosed, object: nil)
        if let lease = windowLease, let windowCoordinator {
            windowCoordinator.dismiss(lease)
        }
        windowLease = nil
        window = nil
    }
}

struct MediaViewerContent: View {
    @ObservedObject private var model: MediaViewerModel
    let mode: MediaViewerMode

    init(model: MediaViewerModel = .shared, mode: MediaViewerMode) {
        self.model = model
        self.mode = mode
    }

    var body: some View {
        VStack(spacing: 0) {
            if mode == .detached {
                detachedHeader
                Divider().opacity(0.45)
            }

            if mode == .attached, !model.pendingItems.isEmpty {
                pendingBanner
            }

            stage
                .frame(maxWidth: .infinity, maxHeight: .infinity)

            Divider().opacity(0.35)
            filmstrip

            if mode == .detached {
                Divider().opacity(0.35)
                detachedMetadata
            } else {
                compactMetadata
            }
        }
        .background(mode == .detached ? QMXTheme.surface : QMXTheme.surface.opacity(0.24))
    }

    private var pendingBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkles")
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(QMXTheme.accent)
            Text("新回答包含 \(model.pendingItems.count) 项媒体")
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(QMXTheme.textSecondary)
            Spacer(minLength: 0)
            Button("切换") { model.acceptPending() }
                .buttonStyle(.plain)
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(QMXTheme.accent)
            Button {
                model.dismissPending()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16)
        .frame(height: 28)
        .background(QMXTheme.accent.opacity(0.06))
    }

    private var detachedHeader: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.title.isEmpty ? "媒体预览" : model.title)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                HStack(spacing: 6) {
                    Text("\(model.items.count) 项 · \(model.contextLabel)")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .monospacedDigit()
                    Text("已锁定")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(QMXTheme.surfaceMuted))
                }
            }

            Spacer(minLength: 0)

            if let item = model.selectedItem, !item.isMarkdownTable {
                Button("快速预览") { quickLook(item) }
                    .buttonStyle(.qmxSecondary)
                    .help("打开系统 Quick Look")
            }
            Button("停靠") { MediaViewerLauncher.dock() }
                .buttonStyle(.qmxSecondary)
                .help("停靠回启明星侧翼")

        }
        .padding(.horizontal, 20)
        .frame(height: 58)
    }

    @ViewBuilder
    private var stage: some View {
        if let item = model.selectedItem {
            Group {
                switch item.kind {
                case "image":
                    MediaViewerImageStage(item: item)
                case "video":
                    MediaViewerVideoStage(item: item, stopOnQuickEntryHide: mode == .attached)
                        .id(item.mediaId)
                case "audio":
                    MediaViewerAudioStage(item: item, stopOnQuickEntryHide: mode == .attached)
                        .id(item.mediaId)
                default:
                    MediaViewerFileStage(item: item)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            VStack(spacing: 8) {
                Image(systemName: "photo.on.rectangle.angled")
                    .font(.system(size: 24, weight: .light))
                Text("没有可预览的媒体")
                    .font(.system(size: 12))
            }
            .foregroundStyle(QMXTheme.textTertiary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var filmstrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(model.items) { item in
                    Button {
                        model.select(item.mediaId)
                    } label: {
                        MediaViewerThumb(item: item, selected: item.mediaId == model.selectedID)
                    }
                    .buttonStyle(.plain)
                    .help(item.title)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
        }
        .frame(height: 82)
    }

    private var compactMetadata: some View {
        HStack(spacing: 6) {
            if let item = model.selectedItem {
                Text(sourceLabel(item))
                    .lineLimit(1)
                if item.width > 0, item.height > 0 {
                    Text("· \(item.width)×\(item.height)")
                        .monospacedDigit()
                }
                Spacer(minLength: 0)
                if let source = safeSourceURL(item) {
                    Button {
                        NSWorkspace.shared.open(source)
                    } label: {
                        Image(systemName: "arrow.up.right")
                    }
                    .buttonStyle(.plain)
                    .help("打开来源页面")
                }
                Button {
                    infoItem = item
                } label: {
                    Image(systemName: "info.circle")
                }
                .buttonStyle(.plain)
                .help("媒体信息")
                .popover(item: $infoItem, arrowEdge: .bottom) { info in
                    MediaViewerInfoPopover(item: info)
                }
            }
        }
        .font(.system(size: 10))
        .foregroundStyle(QMXTheme.textTertiary)
        .padding(.horizontal, 16)
        .frame(height: 28)
    }

    @State private var infoItem: MediaItem?

    private var detachedMetadata: some View {
        Group {
            if let item = model.selectedItem {
                HStack(alignment: .top, spacing: 18) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.title.isEmpty ? "未命名媒体" : item.title)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(QMXTheme.textPrimary)
                            .lineLimit(2)
                        HStack(spacing: 8) {
                            Text(sourceLabel(item))
                            if !item.license.isEmpty { Text("· \(item.license)") }
                            if item.width > 0 { Text("· \(item.width)×\(item.height)") }
                            if item.sizeBytes > 0 { Text("· \(sizeLabel(item.sizeBytes))") }
                            if !item.mimeType.isEmpty { Text("· \(item.mimeType)") }
                        }
                        .font(.system(size: 10))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .lineLimit(1)
                    }

                    Spacer(minLength: 0)

                    HStack(spacing: 8) {
                        if item.isMarkdownTable {
                            Button("复制表格") {
                                _ = qmxCopyTableToPasteboard(
                                    columns: item.previewColumns,
                                    rows: item.previewRows
                                )
                            }
                            .buttonStyle(.qmxSecondary)
                        } else if let path = item.fileURL?.path, FileManager.default.fileExists(atPath: path) {
                            Button("打开文件") { quickLook(item) }
                                .buttonStyle(.qmxSecondary)
                            Button("在访达中显示") { _ = DesktopActions().revealPath(path) }
                                .buttonStyle(.qmxSecondary)
                        } else {
                            if let source = safeSourceURL(item) {
                                Button("打开来源页") { NSWorkspace.shared.open(source) }
                                    .buttonStyle(.qmxSecondary)
                            }
                            Button("打开缓存目录") { openCacheDirectory(item) }
                                .buttonStyle(.qmxSecondary)
                        }
                        if let source = safeSourceURL(item) {
                            Button("来源") { NSWorkspace.shared.open(source) }
                                .buttonStyle(.qmxSecondary)
                        }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
        }
    }

    private func sourceLabel(_ item: MediaItem) -> String {
        if !item.sourceTitle.isEmpty { return item.sourceTitle }
        if !item.sourceURL.isEmpty, let host = URL(string: item.sourceURL)?.host {
            return host
        }
        return item.provider.isEmpty ? "本地媒体" : item.provider
    }

    private func safeSourceURL(_ item: MediaItem) -> URL? {
        guard !item.sourceURL.isEmpty,
              let url = URL(string: item.sourceURL),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else { return nil }
        return url
    }

    private func quickLook(_ item: MediaItem) {
        guard let path = item.fileURL?.path, FileManager.default.fileExists(atPath: path) else { return }
        _ = DesktopActions().quickLook(path)
    }

    private func openCacheDirectory(_ item: MediaItem) {
        openMediaCacheDirectory(item)
    }

    private func sizeLabel(_ bytes: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}

private struct MediaViewerImageStage: View {
    let item: MediaItem
    @State private var image: NSImage?
    @State private var failed = false
    @State private var loadedID = ""

    var body: some View {
        ZStack {
            Color.black.opacity(0.10)
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .padding(14)
                    .clipped()
            } else if failed {
                missing
            } else {
                ProgressView()
                    .controlSize(.small)
            }
        }
        .task(id: item.mediaId) {
            image = nil
            failed = false
            loadedID = item.mediaId
            guard let path = item.fileURL?.path, FileManager.default.fileExists(atPath: path) else {
                failed = true
                return
            }
            let loaded = await MediaThumbnailCache.shared.thumbnail(for: item, maxPixel: 2048)
            guard !Task.isCancelled, loadedID == item.mediaId else { return }
            image = loaded
            failed = loaded == nil
        }
        .onTapGesture(count: 2) {
            quickLook()
        }
        .onReceive(NotificationCenter.default.publisher(for: .qmxToggleMediaPlayback)) { _ in
            quickLook()
        }
    }

    private func quickLook() {
        if let path = item.fileURL?.path, FileManager.default.fileExists(atPath: path) {
            _ = DesktopActions().quickLook(path)
        }
    }

    private var missing: some View {
        VStack(spacing: 8) {
            Image(systemName: "photo.badge.exclamationmark")
                .font(.system(size: 24, weight: .light))
            Text("图片文件已不存在")
                .font(.system(size: 12))
            HStack(spacing: 8) {
                if let source = safeURL(item.sourceURL) {
                    Button("打开来源页面") { NSWorkspace.shared.open(source) }
                        .buttonStyle(.qmxSecondary)
                }
                Button("打开缓存目录") { openMediaCacheDirectory(item) }
                    .buttonStyle(.qmxSecondary)
            }
        }
        .foregroundStyle(QMXTheme.textTertiary)
    }
}

private struct MediaViewerVideoStage: View {
    let item: MediaItem
    let stopOnQuickEntryHide: Bool
    @State private var player: AVPlayer?

    var body: some View {
        Group {
            if let player {
                VideoPlayer(player: player)
                    .padding(8)
            } else {
                MediaViewerMissingStage(item: item, symbol: "film")
            }
        }
        .onAppear(perform: setup)
        .onDisappear(perform: teardown)
        .onReceive(NotificationCenter.default.publisher(for: .qmxStopMediaPlayback)) { _ in
            if stopOnQuickEntryHide { teardown() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .qmxToggleMediaPlayback)) { _ in
            guard let player else { return }
            if player.rate == 0 { player.play() } else { player.pause() }
        }
    }

    private func setup() {
        guard player == nil, let url = item.fileURL,
              FileManager.default.fileExists(atPath: url.path) else { return }
        player = AVPlayer(url: url)
    }

    private func teardown() {
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        player = nil
    }
}

private struct MediaViewerAudioStage: View {
    let item: MediaItem
    let stopOnQuickEntryHide: Bool
    @StateObject private var audio = MediaAudioPlayer()
    @State private var rate: Float = 1

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 64, weight: .ultraLight))
                .foregroundStyle(QMXTheme.accent.opacity(0.8))
            Text(item.title.isEmpty ? "音频" : item.title)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(QMXTheme.textPrimary)
                .lineLimit(2)

            Slider(value: Binding(
                get: { audio.duration > 0 ? min(1, max(0, audio.progress / audio.duration)) : 0 },
                set: { audio.seek(fraction: $0) }
            ), in: 0...1)
            .tint(QMXTheme.accent)
            .frame(maxWidth: 420)

            HStack(spacing: 12) {
                Button {
                    audio.toggle(item)
                } label: {
                    Image(systemName: audio.isPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 12, weight: .bold))
                        .frame(width: 30, height: 30)
                        .background(Circle().fill(QMXTheme.surfaceMuted))
                }
                .buttonStyle(.plain)
                Text("\(durationLabel(audio.progress)) / \(durationLabel(max(audio.duration, item.durationSec)))")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                Menu {
                    ForEach([1.0, 1.25, 1.5, 2.0], id: \.self) { value in
                        Button(String(format: "%gx", value)) {
                            rate = Float(value)
                            audio.setRate(rate)
                        }
                    }
                } label: {
                    Text(String(format: "%gx", rate))
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { audio.load(item) }
        .onDisappear { audio.stop() }
        .onReceive(NotificationCenter.default.publisher(for: .qmxStopMediaPlayback)) { _ in
            if stopOnQuickEntryHide { audio.stop() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .qmxToggleMediaPlayback)) { _ in
            audio.toggle(item)
        }
    }

    private func durationLabel(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "0:00" }
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

private struct MediaViewerFileStage: View {
    let item: MediaItem

    private var path: String? {
        guard let path = item.fileURL?.path,
              FileManager.default.fileExists(atPath: path) else { return nil }
        return path
    }

    private var ext: String {
        if !item.fileExt.isEmpty { return item.fileExt.lowercased() }
        return (path as NSString?)?.pathExtension.lowercased() ?? ""
    }

    var body: some View {
        if ext == "md-table" {
            TableMediaStage(item: item)
        } else if path == nil {
            missingFile
        } else if ext == "pdf" {
            PDFFileStage(item: item, path: path!)
        } else if ["xlsx", "xlsm", "csv"].contains(ext) {
            SpreadsheetFileStage(item: item, path: path!)
        } else if ["md", "markdown"].contains(ext) {
            MarkdownFileStage(item: item, path: path!)
        } else {
            genericFile
        }
    }

    private var missingFile: some View {
        VStack(spacing: 10) {
            Image(systemName: "doc.questionmark")
                .font(.system(size: 44, weight: .ultraLight))
                .foregroundStyle(QMXTheme.textSecondary)
            Text(item.title.isEmpty ? "文件" : item.title)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(QMXTheme.textPrimary)
                .lineLimit(2)
            Text("文件已被移动或删除")
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textTertiary)
            Button("打开所在目录") { openMediaCacheDirectory(item) }
                .buttonStyle(.qmxSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var genericFile: some View {
        VStack(spacing: 10) {
            if let path {
                Image(nsImage: NSWorkspace.shared.icon(forFile: path))
                    .resizable()
                    .frame(width: 54, height: 54)
            } else {
                Image(systemName: "doc.fill")
                    .font(.system(size: 54, weight: .ultraLight))
                    .foregroundStyle(QMXTheme.textSecondary)
            }
            Text(item.title.isEmpty ? "文件" : item.title)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(QMXTheme.textPrimary)
                .lineLimit(2)
            if let badge = badgeText {
                Text(badge)
                    .font(.system(size: 10))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
            if let path {
                Button("快速预览") { _ = DesktopActions().quickLook(path) }
                    .buttonStyle(.qmxSecondary)
                Button("用默认应用打开") { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                    .buttonStyle(.qmxSecondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var badgeText: String? {
        if !item.summaryBadge.isEmpty { return item.summaryBadge }
        if item.sizeBytes > 0 {
            return ByteCountFormatter.string(fromByteCount: Int64(item.sizeBytes), countStyle: .file)
        }
        return nil
    }
}

private struct PDFFileStage: View {
    let item: MediaItem
    let path: String

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            PDFKitView(url: URL(fileURLWithPath: path))
                .background(QMXTheme.surface)
            if !item.summaryBadge.isEmpty {
                Text(item.summaryBadge)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Capsule().fill(QMXTheme.surfaceRaised.opacity(0.92)))
                    .padding(12)
            }
        }
    }
}

private struct PDFKitView: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        view.displayMode = .singlePageContinuous
        view.displayDirection = .vertical
        view.backgroundColor = .clear
        view.document = PDFDocument(url: url)
        return view
    }

    func updateNSView(_ view: PDFView, context: Context) {
        if view.document?.documentURL != url {
            view.document = PDFDocument(url: url)
        }
    }
}

private struct SpreadsheetFileStage: View {
    let item: MediaItem
    let path: String
    @State private var selectedSheet: String = ""

    private var activePreview: SheetPreviewItem? {
        if let selected = item.sheetPreviews.first(where: { $0.name == selectedSheet }) {
            return selected
        }
        return item.sheetPreviews.first
    }
    private var columns: [String] { activePreview?.columns ?? item.previewColumns }
    private var rows: [[String]] { activePreview?.rows ?? item.previewRows }
    private var firstSheet: String { item.sheetNames.first ?? "Sheet1" }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(nsImage: NSWorkspace.shared.icon(forFile: path))
                    .resizable()
                    .frame(width: 20, height: 20)
                Text(item.title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                if !item.summaryBadge.isEmpty {
                    Text(item.summaryBadge)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(QMXTheme.textTertiary)
                }
                Spacer(minLength: 0)
                Button("快速预览") { _ = DesktopActions().quickLook(path) }
                    .buttonStyle(.qmxSecondary)
            }
            .padding(.horizontal, 14)
            .frame(height: 42)

            if item.sheetNames.count > 1 {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(item.sheetNames, id: \.self) { sheet in
                            Button {
                                selectedSheet = sheet
                            } label: {
                                Text(sheet)
                                    .font(.system(size: 10.5, weight: selectedSheet == sheet ? .medium : .regular))
                                    .foregroundStyle(selectedSheet == sheet ? QMXTheme.textPrimary : QMXTheme.textSecondary)
                                    .padding(.horizontal, 8)
                                    .frame(height: 22)
                                    .background(Capsule().fill(selectedSheet == sheet ? QMXTheme.surfaceRaised : Color.clear))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 14)
                }
                .frame(height: 30)
            }

            Divider().opacity(0.4)
            if columns.isEmpty || rows.isEmpty {
                emptyPreview
            } else {
                table
            }
        }
        .onAppear { if selectedSheet.isEmpty { selectedSheet = firstSheet } }
    }

    private var emptyPreview: some View {
        VStack(spacing: 8) {
            Image(systemName: "tablecells")
                .font(.system(size: 26, weight: .light))
            Text("没有可用的表格预览")
                .font(.system(size: 11))
            Button("用快速预览查看") { _ = DesktopActions().quickLook(path) }
                .buttonStyle(.qmxSecondary)
        }
        .foregroundStyle(QMXTheme.textTertiary)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var table: some View {
        NativeTableGrid(columns: columns, rows: rows, monospaced: true)
    }
}

private struct TableMediaStage: View {
    let item: MediaItem
    @StateObject private var scrollProxy = TableScrollProxy()

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "tablecells")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                Text(item.title.isEmpty ? "表格" : item.title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                if !item.summaryBadge.isEmpty {
                    Text(item.summaryBadge)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(QMXTheme.textTertiary)
                }
                Spacer(minLength: 0)
                Button {
                    scrollProxy.scroll(by: -300)
                } label: {
                    Image(systemName: "chevron.left")
                }
                .buttonStyle(.qmxIcon)
                .help("向左滚动")
                .accessibilityLabel("向左滚动")
                Button {
                    scrollProxy.scroll(by: 300)
                } label: {
                    Image(systemName: "chevron.right")
                }
                .buttonStyle(.qmxIcon)
                .help("向右滚动")
                .accessibilityLabel("向右滚动")
                Button("大窗口") {
                    MediaViewerWindowController.shared.show(large: true)
                }
                .buttonStyle(.qmxSecondary)
                .help("在可调整大小的独立窗口中查看完整表格")
                Button("复制表格") {
                    _ = qmxCopyTableToPasteboard(
                        columns: item.previewColumns,
                        rows: item.previewRows
                    )
                }
                .buttonStyle(.qmxSecondary)
            }
            .padding(.horizontal, 14)
            .frame(height: 42)

            Divider().opacity(0.4)

            if item.previewColumns.isEmpty || item.previewRows.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "tablecells")
                        .font(.system(size: 26, weight: .light))
                    Text("没有可用的表格预览")
                        .font(.system(size: 11))
                }
                .foregroundStyle(QMXTheme.textTertiary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                VStack(spacing: 0) {
                    HStack(spacing: 6) {
                        Image(systemName: "arrow.left.and.right")
                            .font(.system(size: 9.5, weight: .medium))
                        Text(item.previewColumns.count > 6
                             ? "双指左右滑动或点上方 ‹ › 查看全部 \(item.previewColumns.count) 列，可拖动列宽"
                             : "可拖动列宽")
                            .font(.system(size: 10))
                        Spacer(minLength: 0)
                    }
                    .foregroundStyle(QMXTheme.textTertiary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 4)
                    NativeTableGrid(
                        columns: item.previewColumns,
                        rows: item.previewRows,
                        scrollProxy: scrollProxy
                    )
                    .id(item.mediaId)
                }
            }
        }
    }
}

private struct MarkdownFileStage: View {
    let item: MediaItem
    let path: String

    private var content: AttributedString? {
        guard let raw = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        return try? AttributedString(markdown: raw)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(nsImage: NSWorkspace.shared.icon(forFile: path))
                    .resizable()
                    .frame(width: 20, height: 20)
                Text(item.title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                Spacer(minLength: 0)
                Button("快速预览") { _ = DesktopActions().quickLook(path) }
                    .buttonStyle(.qmxSecondary)
            }
            .padding(.horizontal, 14)
            .frame(height: 42)
            Divider().opacity(0.4)
            ScrollView {
                if let content {
                    Text(content)
                        .font(.system(size: 13))
                        .lineSpacing(4)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(18)
                } else {
                    Text("Markdown 文件无法读取")
                        .font(.system(size: 11))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .padding(18)
                }
            }
        }
    }
}

private struct MediaViewerMissingStage: View {
    let item: MediaItem
    let symbol: String

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: symbol)
                .font(.system(size: 34, weight: .light))
            Text("媒体文件已不存在")
                .font(.system(size: 12))
            if !item.sourceURL.isEmpty,
               let url = URL(string: item.sourceURL),
               let scheme = url.scheme?.lowercased(),
               scheme == "http" || scheme == "https" {
                Button("打开来源页面") { NSWorkspace.shared.open(url) }
                    .buttonStyle(.qmxSecondary)
            }
            Button("打开缓存目录") { openMediaCacheDirectory(item) }
                .buttonStyle(.qmxSecondary)
        }
        .foregroundStyle(QMXTheme.textTertiary)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct MediaViewerThumb: View {
    let item: MediaItem
    let selected: Bool
    @State private var image: NSImage?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(QMXTheme.surfaceMuted)
            if item.kind == "file", let path = item.fileURL?.path,
               FileManager.default.fileExists(atPath: path) {
                Image(nsImage: NSWorkspace.shared.icon(forFile: path))
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .frame(width: 32, height: 32)
            } else if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .clipped()
            } else {
                Image(systemName: symbol)
                    .font(.system(size: 14, weight: .light))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
        }
        .frame(width: 62, height: 62)
        .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .strokeBorder(selected ? QMXTheme.accent : QMXTheme.hairline,
                              lineWidth: selected ? 1.5 : 0.5)
        )
        .task(id: item.mediaId) {
            image = await MediaThumbnailCache.shared.thumbnail(for: item, maxPixel: 160)
        }
    }

    private var symbol: String {
        if item.isMarkdownTable { return "tablecells" }
        switch item.kind {
        case "video": return "film"
        case "audio": return "waveform"
        case "file": return "doc"
        default: return "photo"
        }
    }
}

private struct MediaViewerInfoPopover: View {
    let item: MediaItem

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(item.title.isEmpty ? "媒体信息" : item.title)
                .font(.system(size: 12, weight: .medium))
                .lineLimit(2)
            if !item.sourceTitle.isEmpty {
                Text("来源：\(item.sourceTitle)")
            }
            if !item.sourceURL.isEmpty, let url = URL(string: item.sourceURL) {
                Text(url.host ?? item.sourceURL)
            }
            if !item.license.isEmpty { Text("许可：\(item.license)") }
            if !item.attribution.isEmpty { Text("署名：\(item.attribution)") }
            if item.width > 0 { Text("尺寸：\(item.width)×\(item.height)") }
            if !item.mimeType.isEmpty { Text("类型：\(item.mimeType)") }
            if item.sizeBytes > 0 {
                Text("大小：\(ByteCountFormatter.string(fromByteCount: Int64(item.sizeBytes), countStyle: .file))")
            }
        }
        .font(.system(size: 11))
        .foregroundStyle(QMXTheme.textSecondary)
        .padding(12)
        .frame(width: 240, alignment: .leading)
    }
}

struct AuxiliaryWingView: View {
    @ObservedObject var store: WorkspaceStore
    let desktopActions: DesktopActions
    @ObservedObject var inputState: QuickEntryInputState
    let onSplitWorkspace: () -> Void
    let onSplitMedia: () -> Void
    let onClose: () -> Void

    @ObservedObject private var viewer = MediaViewerModel.shared

    var body: some View {
        VStack(spacing: 0) {
            topBar
            Divider().opacity(0.45)

            if inputState.auxiliaryTab == .workspace {
                WorkspaceWingView(
                    store: store,
                    desktopActions: desktopActions,
                    onSplit: onSplitWorkspace,
                    onClose: onClose,
                    showsHeader: false
                )
            } else {
                MediaViewerContent(model: viewer, mode: .attached)
            }
        }
        .background(QMXTheme.surface.opacity(0.26))
    }

    private var topBar: some View {
        HStack(spacing: 6) {
            tabButton(.workspace)
            tabButton(.media)
            Spacer(minLength: 0)

            Button {
                if inputState.auxiliaryTab == .workspace {
                    onSplitWorkspace()
                } else {
                    onSplitMedia()
                }
            } label: {
                Image(systemName: "rectangle.split.2x1")
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .frame(width: 26, height: 26)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.qmxIcon)
            .help(inputState.auxiliaryTab == .workspace ? "拆分工作区" : "拆分媒体预览")

            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .frame(width: 26, height: 26)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.qmxIcon)
            .help("收起辅助区")
        }
        .padding(.horizontal, 14)
        .frame(height: 50)
    }

    private func tabButton(_ tab: AuxiliaryWingTab) -> some View {
        let selected = inputState.auxiliaryTab == tab
        return Button {
            inputState.auxiliaryTab = tab
        } label: {
            HStack(spacing: 4) {
                Text(tab.label)
                if tab == .media, !viewer.pendingItems.isEmpty {
                    Circle()
                        .fill(QMXTheme.accent)
                        .frame(width: 5, height: 5)
                }
            }
            .font(.system(size: 11.5, weight: selected ? .medium : .regular))
            .foregroundStyle(selected ? QMXTheme.textPrimary : QMXTheme.textSecondary)
            .padding(.horizontal, 10)
            .frame(height: 26)
            .background(
                Capsule().fill(selected ? QMXTheme.surfaceRaised : Color.clear)
            )
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

private func safeURL(_ raw: String) -> URL? {
    guard let url = URL(string: raw),
          let scheme = url.scheme?.lowercased(),
          scheme == "http" || scheme == "https" else { return nil }
    return url
}

private func openMediaCacheDirectory(_ item: MediaItem) {
    if let path = item.fileURL?.path {
        let parent = URL(fileURLWithPath: path).deletingLastPathComponent()
        if FileManager.default.fileExists(atPath: parent.path) {
            NSWorkspace.shared.open(parent)
            return
        }
    }
    let fallback = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
        .first?
        .appendingPathComponent("Qimingxing/data/media/image_search", isDirectory: true)
    if let fallback, FileManager.default.fileExists(atPath: fallback.path) {
        NSWorkspace.shared.open(fallback)
    }
}

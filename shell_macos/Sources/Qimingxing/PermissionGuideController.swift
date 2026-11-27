import AppKit
import ApplicationServices
import AVFoundation
import CoreGraphics
import Speech
import SwiftUI

enum SystemPermissionKind: String, CaseIterable, Identifiable, Sendable {
    case accessibility
    case screenRecording
    case microphone
    case speechRecognition

    var id: String { rawValue }

    var title: String {
        switch self {
        case .accessibility: return "辅助功能"
        case .screenRecording: return "屏幕录制"
        case .microphone: return "麦克风"
        case .speechRecognition: return "语音识别"
        }
    }

    var subtitle: String {
        switch self {
        case .accessibility: return "用于原位替换写回、划词感知与全局快捷键"
        case .screenRecording: return "用于屏幕圈选看屏与截屏问答"
        case .microphone: return "用于按住说话实时语音输入"
        case .speechRecognition: return "用于本地实时语音转文字"
        }
    }

    var symbolName: String {
        switch self {
        case .accessibility: return "accessibility"
        case .screenRecording: return "rectangle.dashed.badge.record"
        case .microphone: return "mic.fill"
        case .speechRecognition: return "waveform"
        }
    }

    var supportsDragDrop: Bool {
        switch self {
        case .accessibility, .screenRecording: return true
        case .microphone, .speechRecognition: return false
        }
    }

    var settingsURLString: String {
        switch self {
        case .accessibility:
            return "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        case .screenRecording:
            return "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        case .microphone:
            return "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        case .speechRecognition:
            return "x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition"
        }
    }
}

enum PermissionGuideHelper {
    static func resolvedAppBundleURL() -> URL {
        let mainURL = Bundle.main.bundleURL
        if mainURL.pathExtension == "app" {
            return mainURL
        }
        let installed = URL(fileURLWithPath: "/Applications/Qimingxing.app")
        if FileManager.default.fileExists(atPath: installed.path) {
            return installed
        }
        return mainURL
    }

    @MainActor
    static func appDisplayIcon(for url: URL = resolvedAppBundleURL()) -> NSImage {
        if FileManager.default.fileExists(atPath: url.path) {
            return NSWorkspace.shared.icon(forFile: url.path)
        }
        if let appIcon = NSApp.applicationIconImage {
            return appIcon
        }
        return NSWorkspace.shared.icon(for: .applicationBundle)
    }

    @MainActor
    static func isGranted(_ kind: SystemPermissionKind) -> Bool {
        switch kind {
        case .accessibility:
            return AXIsProcessTrusted()
        case .screenRecording:
            return CGPreflightScreenCaptureAccess()
        case .microphone:
            return AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        case .speechRecognition:
            return SFSpeechRecognizer.authorizationStatus() == .authorized
        }
    }

    @MainActor
    static func openSystemSettings(for kind: SystemPermissionKind) {
        guard let url = URL(string: kind.settingsURLString) else { return }
        NSWorkspace.shared.open(url)
    }
}

// MARK: - Draggable Qimingxing.app Icon (NSURL Drop Source for macOS TCC)

struct DraggableAppBundleIcon: NSViewRepresentable {
    var size: CGFloat = 46
    var onDragStarted: (() -> Void)? = nil

    func makeNSView(context: Context) -> DraggableAppBundleIconView {
        let view = DraggableAppBundleIconView(iconSize: size)
        view.onDragStarted = onDragStarted
        return view
    }

    func updateNSView(_ nsView: DraggableAppBundleIconView, context: Context) {
        nsView.iconSize = size
        nsView.onDragStarted = onDragStarted
    }
}

final class DraggableAppBundleIconView: NSView, NSDraggingSource {
    var iconSize: CGFloat {
        didSet { needsDisplay = true }
    }
    var onDragStarted: (() -> Void)?

    private var mouseDownPoint: NSPoint?
    private var mouseDownEvent: NSEvent?
    private var isHovered = false
    private var trackingArea: NSTrackingArea?

    init(iconSize: CGFloat = 46) {
        self.iconSize = iconSize
        super.init(frame: NSRect(x: 0, y: 0, width: iconSize, height: iconSize))
        toolTip = "按住并拖放「启明星.app」到系统设置的权限列表中即可完成授权"
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var intrinsicContentSize: NSSize {
        NSSize(width: iconSize, height: iconSize)
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingArea {
            removeTrackingArea(trackingArea)
        }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .cursorUpdate, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
        trackingArea = area
    }

    override func cursorUpdate(with event: NSEvent) {
        NSCursor.openHand.set()
    }

    override func mouseEntered(with event: NSEvent) {
        isHovered = true
        needsDisplay = true
        NSCursor.openHand.set()
    }

    override func mouseExited(with event: NSEvent) {
        isHovered = false
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let bundleURL = PermissionGuideHelper.resolvedAppBundleURL()
        let icon = PermissionGuideHelper.appDisplayIcon(for: bundleURL)
        let inset: CGFloat = isHovered ? 0 : 1.5
        let targetRect = bounds.insetBy(dx: inset, dy: inset)
        icon.draw(in: targetRect, from: .zero, operation: .sourceOver, fraction: isHovered ? 1.0 : 0.96)
    }

    override func mouseDown(with event: NSEvent) {
        mouseDownPoint = convert(event.locationInWindow, from: nil)
        mouseDownEvent = event
        NSCursor.closedHand.push()
    }

    override func mouseUp(with event: NSEvent) {
        NSCursor.pop()
        mouseDownPoint = nil
        mouseDownEvent = nil
    }

    override func mouseDragged(with event: NSEvent) {
        guard let downPt = mouseDownPoint, let downEvent = mouseDownEvent else { return }
        let cur = convert(event.locationInWindow, from: nil)
        guard hypot(cur.x - downPt.x, cur.y - downPt.y) >= 3.0 else { return }

        mouseDownPoint = nil
        mouseDownEvent = nil
        NSCursor.pop()

        let appURL = PermissionGuideHelper.resolvedAppBundleURL()
        let dragItem = NSDraggingItem(pasteboardWriter: appURL as NSURL)
        let preview = PermissionGuideHelper.appDisplayIcon(for: appURL)
        preview.size = NSSize(width: iconSize, height: iconSize)
        dragItem.setDraggingFrame(
            NSRect(x: 0, y: 0, width: iconSize, height: iconSize),
            contents: preview
        )

        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
        onDragStarted?()
        beginDraggingSession(with: [dragItem], event: downEvent, source: self)
    }

    static let supportedDragMask: NSDragOperation = [.copy, .link]

    func draggingSession(
        _ session: NSDraggingSession,
        sourceOperationMaskFor context: NSDraggingContext
    ) -> NSDragOperation {
        Self.supportedDragMask
    }

    func draggingSession(
        _ session: NSDraggingSession,
        endedAt screenPoint: NSPoint,
        operation: NSDragOperation
    ) {
        if operation != [] {
            NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
        }
    }
}

// MARK: - Floating Permission Guide Card UI

struct PermissionGuideCardView: View {
    let kind: SystemPermissionKind
    let isGranted: Bool
    var onDragStarted: (() -> Void)? = nil
    let onOpenSettings: () -> Void
    let onClose: () -> Void

    @State private var hasStartedDrag = false

    var body: some View {
        ZStack {
            VisualEffectBlur(
                material: .popover,
                blendingMode: .behindWindow,
                state: .active,
                cornerRadius: 20
            )

            VStack(alignment: .leading, spacing: 14) {
                headerRow
                dragTrackStage
                captionText
                footerRow
            }
            .padding(16)
        }
        .frame(width: PermissionGuideController.cardSize.width, height: PermissionGuideController.cardSize.height)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.75)
        )
    }

    private var headerRow: some View {
        HStack(spacing: 8) {
            Image(systemName: kind.symbolName)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(isGranted ? QMXTheme.success : QMXTheme.accent)

            Text("拖放授权 · \(kind.title)")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(QMXTheme.textPrimary)

            Spacer()

            HStack(spacing: 5) {
                StatusDot(color: isGranted ? QMXTheme.success : QMXTheme.warning, size: 6)
                Text(isGranted ? "已授权" : "等待拖入")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(isGranted ? QMXTheme.success : QMXTheme.textSecondary)
            }
            .padding(.horizontal, 8)
            .frame(height: 22)
            .background(
                Capsule(style: .continuous)
                    .fill(isGranted ? QMXTheme.success.opacity(0.14) : QMXTheme.surfaceMuted.opacity(0.8))
            )

            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .frame(width: 22, height: 22)
            }
            .buttonStyle(.plain)
        }
    }

    private var dragTrackStage: some View {
        HStack(spacing: 12) {
            VStack(spacing: 6) {
                DraggableAppBundleIcon(size: 46) {
                    hasStartedDrag = true
                    onDragStarted?()
                }
                .frame(width: 46, height: 46)

                Text(hasStartedDrag ? "拖放中…" : "按住拖我")
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(QMXTheme.accent)
            }
            .frame(width: 84, height: 82)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(QMXTheme.surfaceRaised.opacity(0.9))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(QMXTheme.accent.opacity(0.45), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            )

            VStack(spacing: 4) {
                Image(systemName: "arrow.right")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(isGranted ? QMXTheme.success : QMXTheme.textSecondary)
                Text("拖入列表")
                    .font(.system(size: 10))
                    .foregroundStyle(QMXTheme.textTertiary)
            }

            VStack(spacing: 5) {
                ZStack {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(QMXTheme.surfaceMuted)
                        .frame(width: 36, height: 36)
                    Image(systemName: isGranted ? "checkmark.shield.fill" : "gearshape.fill")
                        .font(.system(size: 17, weight: .medium))
                        .foregroundStyle(isGranted ? QMXTheme.success : QMXTheme.textPrimary)
                }
                Text("系统设置 · \(kind.title)")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity)
            .frame(height: 82)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(QMXTheme.surfaceRaised.opacity(0.65))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(isGranted ? QMXTheme.success.opacity(0.5) : QMXTheme.hairline, lineWidth: 0.75)
            )
        }
    }

    private var captionText: some View {
        Text(
            isGranted
                ? "太好了，系统已授予「\(kind.title)」权限，现在可以关闭此卡片。"
                : "将左侧「启明星」图标直接拖入系统设置的「\(kind.title)」列表中，并确认右侧开关已打开。"
        )
        .font(.system(size: 11.5))
        .foregroundStyle(QMXTheme.textSecondary)
        .fixedSize(horizontal: false, vertical: true)
        .lineSpacing(2)
    }

    private var footerRow: some View {
        HStack(spacing: 8) {
            Spacer()
            Button("打开「\(kind.title)」设置", action: onOpenSettings)
                .buttonStyle(.qmxSecondary)
            Button(isGranted ? "完成" : "稍后再说", action: onClose)
                .buttonStyle(isGranted ? .qmxProminent : .qmxSecondary)
        }
    }
}

// MARK: - Floating Permission Guide Controller

@MainActor
final class PermissionGuideController: ObservableObject {
    static let shared = PermissionGuideController()
    static let cardSize = NSSize(width: 364, height: 236)

    @Published private(set) var activeKind: SystemPermissionKind = .accessibility
    @Published private(set) var isGranted: Bool = false

    private var panel: NSPanel?
    private var pollTask: Task<Void, Never>?
    private var windowLease: WindowLease?
    /// Injected by AppDelegate; nil in previews.
    var windowCoordinator: WindowCoordinator?

    func show(for kind: SystemPermissionKind, openSettings: Bool = true) {
        activeKind = kind
        isGranted = PermissionGuideHelper.isGranted(kind)

        if openSettings {
            PermissionGuideHelper.openSystemSettings(for: kind)
        }

        // Security surface: highest priority in the model, but it never takes
        // the key and never calls NSApp.activate.
        windowLease = windowCoordinator?.present(
            WindowRequest(role: .permissionGuide, activation: .none)
        )
        ensurePanel()
        refreshContentView()
        positionPanelOnScreen()
        panel?.orderFrontRegardless()
        startPolling()
    }

    func close() {
        pollTask?.cancel()
        pollTask = nil
        if let lease = windowLease, let windowCoordinator {
            windowCoordinator.dismiss(lease)
        }
        windowLease = nil
        panel?.orderOut(nil)
    }

    private func ensurePanel() {
        if panel != nil { return }
        let p = NSPanel(
            contentRect: NSRect(origin: .zero, size: Self.cardSize),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        p.level = .floating
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        p.contentMinSize = Self.cardSize
        p.contentMaxSize = Self.cardSize
        panel = p
    }

    private func refreshContentView() {
        guard let panel else { return }
        let root = PermissionGuideHostView(
            controller: self,
            onOpenSettings: { [weak self] in
                guard let self else { return }
                PermissionGuideHelper.openSystemSettings(for: self.activeKind)
            },
            onClose: { [weak self] in
                self?.close()
            }
        )
        let hosting = NSHostingView(rootView: root)
        hosting.frame = NSRect(origin: .zero, size: Self.cardSize)
        hosting.wantsLayer = true
        hosting.layer?.backgroundColor = NSColor.clear.cgColor
        panel.contentView = hosting
    }

    private func positionPanelOnScreen() {
        guard let panel else { return }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) })
            ?? NSScreen.main
            ?? NSScreen.screens.first
        guard let screen else {
            panel.center()
            return
        }
        let vf = screen.visibleFrame
        let x = vf.maxX - Self.cardSize.width - 28
        let y = vf.maxY - Self.cardSize.height - 64
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let nowGranted = PermissionGuideHelper.isGranted(self.activeKind)
                if nowGranted != self.isGranted {
                    self.isGranted = nowGranted
                    if nowGranted {
                        NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
                        try? await Task.sleep(nanoseconds: 1_500_000_000)
                        if !Task.isCancelled {
                            self.close()
                        }
                        return
                    }
                }
                try? await Task.sleep(nanoseconds: 900_000_000)
            }
        }
    }
}

private struct PermissionGuideHostView: View {
    @ObservedObject var controller: PermissionGuideController
    let onOpenSettings: () -> Void
    let onClose: () -> Void

    var body: some View {
        PermissionGuideCardView(
            kind: controller.activeKind,
            isGranted: controller.isGranted,
            onOpenSettings: onOpenSettings,
            onClose: onClose
        )
    }
}

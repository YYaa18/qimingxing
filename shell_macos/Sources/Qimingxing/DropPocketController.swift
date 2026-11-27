import AppKit
import Combine
import QuickLookThumbnailing
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Scheme C Geometry, Multi-Screen Resolver & State Models

enum ShelfDockEdge: Equatable {
    case left
    case right
}

enum ShelfDockMode: String, Equatable {
    case left
    case right
    case floating

    var edge: ShelfDockEdge {
        self == .left ? .left : .right
    }
}

struct SavedShelfPosition: Equatable {
    static let hasKey = "qmx.dropPocket.hasSavedPosition"
    static let xKey = "qmx.dropPocket.savedX"
    static let yKey = "qmx.dropPocket.savedY"
    static let modeKey = "qmx.dropPocket.savedMode"

    let x: CGFloat
    let y: CGFloat
    let mode: ShelfDockMode

    static func load(defaults: UserDefaults = .standard) -> SavedShelfPosition? {
        guard defaults.bool(forKey: hasKey) else { return nil }
        let x = CGFloat(defaults.double(forKey: xKey))
        let y = CGFloat(defaults.double(forKey: yKey))
        let rawMode = defaults.string(forKey: modeKey) ?? ShelfDockMode.right.rawValue
        let mode = ShelfDockMode(rawValue: rawMode) ?? .right
        return SavedShelfPosition(x: x, y: y, mode: mode)
    }

    func save(defaults: UserDefaults = .standard) {
        defaults.set(true, forKey: Self.hasKey)
        defaults.set(Double(x), forKey: Self.xKey)
        defaults.set(Double(y), forKey: Self.yKey)
        defaults.set(mode.rawValue, forKey: Self.modeKey)
    }

    static func clear(defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: hasKey)
        defaults.removeObject(forKey: xKey)
        defaults.removeObject(forKey: yKey)
        defaults.removeObject(forKey: modeKey)
    }
}

struct ShelfScreenBounds: Equatable {
    let frame: CGRect
    let visibleFrame: CGRect
}

struct ShelfPlacement: Equatable {
    let mode: ShelfDockMode
    let visibleFrame: CGRect
    let anchorPoint: CGPoint

    var edge: ShelfDockEdge { mode.edge }
    var anchorY: CGFloat { anchorPoint.y }

    init(edge: ShelfDockEdge, visibleFrame: CGRect, anchorY: CGFloat) {
        self.mode = edge == .left ? .left : .right
        self.visibleFrame = visibleFrame
        self.anchorPoint = CGPoint(x: edge == .left ? visibleFrame.minX : visibleFrame.maxX, y: anchorY)
    }

    init(mode: ShelfDockMode, visibleFrame: CGRect, anchorPoint: CGPoint) {
        self.mode = mode
        self.visibleFrame = visibleFrame
        self.anchorPoint = anchorPoint
    }
}

enum ShelfQuickSlot: String, CaseIterable, Identifiable {
    case summary = "速览"
    case keyPoints = "提要点"

    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .summary: return "doc.text.magnifyingglass"
        case .keyPoints: return "list.bullet.rectangle"
        }
    }

    var promptPrefix: String {
        switch self {
        case .summary:
            return "请对以下素材做一份简洁清晰的速览摘要，提炼核心结论与关键信息："
        case .keyPoints:
            return "请从以下素材中结构化提取关键要点、数据或待办事项："
        }
    }
}

enum ShelfPhase: Equatable {
    case idleEar
    case dragExpanded(hoveredSlot: ShelfQuickSlot?)
    case stashedStack
}

enum ScreenEdgeResolver {
    static let idleEarSize = CGSize(width: 48, height: 220)
    static let dragExpandedSize = CGSize(width: 224, height: 220)
    static let stashedWidth: CGFloat = 112
    static let stashedMaxHeight: CGFloat = 292
    static let stashedMinHeight: CGFloat = 176
    static let cornerRadius: CGFloat = 20

    static func resolve(for mouse: CGPoint, screens: [ShelfScreenBounds]) -> ShelfPlacement {
        let fallback = ShelfScreenBounds(
            frame: CGRect(x: 0, y: 0, width: 1440, height: 900),
            visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 900)
        )
        let current = screens.first(where: { $0.frame.contains(mouse) })
            ?? screens.first
            ?? fallback

        var leftHasSeam = false
        var rightHasSeam = false

        for other in screens where other != current {
            let overlapY = min(other.frame.maxY, current.frame.maxY) - max(other.frame.minY, current.frame.minY)
            guard overlapY > 20 else { continue }
            if abs(other.frame.maxX - current.frame.minX) <= 4 {
                leftHasSeam = true
            }
            if abs(other.frame.minX - current.frame.maxX) <= 4 {
                rightHasSeam = true
            }
        }

        let edge: ShelfDockEdge
        if !leftHasSeam && rightHasSeam {
            edge = .left
        } else if leftHasSeam && !rightHasSeam {
            edge = .right
        } else {
            let distToLeft = abs(mouse.x - current.frame.minX)
            let distToRight = abs(current.frame.maxX - mouse.x)
            edge = distToLeft <= distToRight ? .left : .right
        }

        let mode: ShelfDockMode = edge == .left ? .left : .right
        let anchorX = mode == .left ? current.visibleFrame.minX : current.visibleFrame.maxX
        return ShelfPlacement(
            mode: mode,
            visibleFrame: current.visibleFrame,
            anchorPoint: CGPoint(x: anchorX, y: mouse.y)
        )
    }

    static func resolveFromSaved(
        _ saved: SavedShelfPosition,
        screens: [ShelfScreenBounds]
    ) -> ShelfPlacement {
        let fallback = ShelfScreenBounds(
            frame: CGRect(x: 0, y: 0, width: 1440, height: 900),
            visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 900)
        )
        let savedPt = CGPoint(x: saved.x, y: saved.y)
        let matched = screens.first(where: { $0.frame.contains(savedPt) })
            ?? screens.min(by: { a, b in
                hypot(a.frame.midX - savedPt.x, a.frame.midY - savedPt.y)
                    < hypot(b.frame.midX - savedPt.x, b.frame.midY - savedPt.y)
            })
            ?? fallback

        return ShelfPlacement(
            mode: saved.mode,
            visibleFrame: matched.visibleFrame,
            anchorPoint: savedPt
        )
    }

    @MainActor
    static func resolveFromSaved(
        _ saved: SavedShelfPosition,
        nsScreens: [NSScreen] = NSScreen.screens
    ) -> ShelfPlacement {
        let bounds = nsScreens.map { ShelfScreenBounds(frame: $0.frame, visibleFrame: $0.visibleFrame) }
        return resolveFromSaved(saved, screens: bounds)
    }

    static func classifyDraggedFrame(
        _ frame: CGRect,
        in visibleFrame: CGRect,
        snapThreshold: CGFloat = 24
    ) -> SavedShelfPosition {
        if abs(frame.minX - visibleFrame.minX) <= snapThreshold {
            return SavedShelfPosition(x: visibleFrame.minX, y: frame.midY, mode: .left)
        }
        if abs(visibleFrame.maxX - frame.maxX) <= snapThreshold {
            return SavedShelfPosition(x: visibleFrame.maxX, y: frame.midY, mode: .right)
        }
        return SavedShelfPosition(x: frame.midX, y: frame.midY, mode: .floating)
    }

    @MainActor
    static func resolve(for mouse: CGPoint, nsScreens: [NSScreen] = NSScreen.screens) -> ShelfPlacement {
        let bounds = nsScreens.map { ShelfScreenBounds(frame: $0.frame, visibleFrame: $0.visibleFrame) }
        return resolve(for: mouse, screens: bounds)
    }

    static func targetSize(for phase: ShelfPhase, itemCount: Int) -> CGSize {
        switch phase {
        case .idleEar:
            return idleEarSize
        case .dragExpanded:
            return dragExpandedSize
        case .stashedStack:
            let visibleCount = min(max(itemCount, 1), 3)
            let height = min(stashedMaxHeight, max(stashedMinHeight, 96 + CGFloat(visibleCount) * 64))
            return CGSize(width: stashedWidth, height: height)
        }
    }

    static func frame(for size: CGSize, placement: ShelfPlacement, verticalMargin: CGFloat = 24) -> CGRect {
        switch placement.mode {
        case .left, .right:
            let x: CGFloat = placement.mode == .left
                ? placement.visibleFrame.minX
                : placement.visibleFrame.maxX - size.width

            let minY = placement.visibleFrame.minY + verticalMargin
            let maxY = placement.visibleFrame.maxY - size.height - verticalMargin
            let y: CGFloat
            if maxY < minY {
                y = placement.visibleFrame.midY - size.height / 2
            } else {
                y = min(max(placement.anchorPoint.y - size.height / 2, minY), maxY)
            }
            return CGRect(origin: CGPoint(x: x, y: y), size: size)

        case .floating:
            let margin: CGFloat = 12
            let minX = placement.visibleFrame.minX + margin
            let maxX = max(minX, placement.visibleFrame.maxX - size.width - margin)
            let minY = placement.visibleFrame.minY + margin
            let maxY = max(minY, placement.visibleFrame.maxY - size.height - margin)

            let rawX = placement.anchorPoint.x - size.width / 2
            let rawY = placement.anchorPoint.y - size.height / 2
            let x = min(max(rawX, minX), maxX)
            let y = min(max(rawY, minY), maxY)
            return CGRect(origin: CGPoint(x: x, y: y), size: size)
        }
    }
}

struct DropPocketItem: Identifiable, Equatable {
    let id: UUID
    let filename: String
    let kind: String
    var isUploading: Bool
    var attachment: StagedAttachment?
    var textSnippet: String?
    var sourceURL: URL?
    var thumbnail: NSImage?
}

struct DragShakeDetector {
    static let windowDuration: TimeInterval = 0.40
    static let minSegmentDeltaX: CGFloat = 12
    static let requiredReversals: Int = 3
    static let cooldown: TimeInterval = 1.2

    private var windowStartAt: TimeInterval?
    private var anchorX: CGFloat = 0
    private var lastDirection: Int = 0
    private var reversals: Int = 0
    private var lastTriggeredAt: TimeInterval = -100

    mutating func record(x: CGFloat, at timestamp: TimeInterval) -> Bool {
        if timestamp - lastTriggeredAt < Self.cooldown {
            windowStartAt = nil
            lastDirection = 0
            reversals = 0
            anchorX = x
            return false
        }
        guard let start = windowStartAt, timestamp - start <= Self.windowDuration else {
            windowStartAt = timestamp
            anchorX = x
            lastDirection = 0
            reversals = 0
            return false
        }
        let dx = x - anchorX
        guard abs(dx) >= Self.minSegmentDeltaX else { return false }
        let dir = dx > 0 ? 1 : -1
        anchorX = x
        if lastDirection != 0 && dir != lastDirection {
            reversals += 1
            if reversals >= Self.requiredReversals {
                lastTriggeredAt = timestamp
                windowStartAt = nil
                lastDirection = 0
                reversals = 0
                return true
            }
        }
        lastDirection = dir
        return false
    }
}

// MARK: - Non-Activating Dark Obsidian Panel & Drag Destination Host

@MainActor
final class DropShelfPanel: NSPanel {
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

private final class EdgeDropHostView: NSView {
    var onDragEntered: (() -> Void)?
    var onHoverSlotChanged: ((ShelfQuickSlot?) -> Void)?
    var onDragExited: (() -> Void)?
    var onDropped: ((ShelfQuickSlot?, ExtractedDragPayload) -> Void)?

    private var hoveredSlot: ShelfQuickSlot?

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
        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
        onDragEntered?()
        updateHoveredSlot(resolveSlot(for: sender))
        return .copy
    }

    override func draggingUpdated(_ sender: any NSDraggingInfo) -> NSDragOperation {
        updateHoveredSlot(resolveSlot(for: sender))
        return .copy
    }

    override func draggingExited(_ sender: (any NSDraggingInfo)?) {
        hoveredSlot = nil
        onHoverSlotChanged?(nil)
        onDragExited?()
    }

    override func draggingEnded(_ sender: any NSDraggingInfo) {
        hoveredSlot = nil
        onHoverSlotChanged?(nil)
        onDragExited?()
    }

    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        let slot = resolveSlot(for: sender)
        let payload = extractPayload(from: sender.draggingPasteboard)
        hoveredSlot = nil
        onHoverSlotChanged?(nil)
        guard !payload.isEmpty else {
            onDragExited?()
            return false
        }
        NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
        onDropped?(slot, payload)
        return true
    }

    private func updateHoveredSlot(_ next: ShelfQuickSlot?) {
        guard next != hoveredSlot else { return }
        hoveredSlot = next
        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
        onHoverSlotChanged?(next)
    }

    private func resolveSlot(for sender: any NSDraggingInfo) -> ShelfQuickSlot? {
        guard bounds.width >= 160 else { return nil }
        let pt = convert(sender.draggingLocation, from: nil)
        guard pt.y <= 72 else { return nil }
        return pt.x < bounds.width / 2 ? .summary : .keyPoints
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
            if let urlString = pb.string(forType: .URL),
               !urlString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                result.textSnippets.append(urlString.trimmingCharacters(in: .whitespacesAndNewlines))
            } else if let str = pb.string(forType: .string),
                      !str.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                result.textSnippets.append(str.trimmingCharacters(in: .whitespacesAndNewlines))
            }
        }
        return result
    }
}

// MARK: - Controller

@MainActor
final class DropPocketController: NSObject, ObservableObject, NSWindowDelegate {
    static let dragShakeDefaultsKey = "qmx.dropPocket.dragShakeEnabled"

    @Published private(set) var items: [DropPocketItem] = []
    @Published private(set) var isVisible: Bool = false
    @Published private(set) var phase: ShelfPhase = .idleEar
    @Published private(set) var dockEdge: ShelfDockEdge = .right
    @Published private(set) var dockMode: ShelfDockMode = .right

    private var panel: DropShelfPanel?
    private var currentPlacement: ShelfPlacement?
    private let brainClient: BrainClient
    private var shakeDetector = DragShakeDetector()
    private var lastDragChangeCount: Int = -1
    private var programmaticMoveCount: Int = 0
    private nonisolated(unsafe) var dragShakeMonitor: Any?

    var onDeliverToQuickEntry: (([StagedAttachment], [String]) -> Void)?
    var onPendingCountChanged: ((Int) -> Void)?

    init(brainClient: BrainClient) {
        self.brainClient = brainClient
        super.init()
        if !QMXTheme.isHeadlessDump, UserDefaults.standard.bool(forKey: Self.dragShakeDefaultsKey) {
            setDragShakeEnabled(true)
        }
    }

    deinit {
        if let dragShakeMonitor {
            NSEvent.removeMonitor(dragShakeMonitor)
        }
    }

    func setDragShakeEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: Self.dragShakeDefaultsKey)
        if let dragShakeMonitor {
            NSEvent.removeMonitor(dragShakeMonitor)
            self.dragShakeMonitor = nil
        }
        guard enabled else { return }
        dragShakeMonitor = NSEvent.addGlobalMonitorForEvents(matching: .leftMouseDragged) { [weak self] _ in
            let mouseX = NSEvent.mouseLocation.x
            let uptime = ProcessInfo.processInfo.systemUptime
            Task { @MainActor [weak self] in
                self?.processGlobalDragSample(mouseX: mouseX, timestamp: uptime)
            }
        }
    }

    private func processGlobalDragSample(mouseX: CGFloat, timestamp: TimeInterval) {
        guard !isVisible else { return }
        let dragPB = NSPasteboard(name: .drag)
        let currentCount = dragPB.changeCount
        if currentCount != lastDragChangeCount {
            lastDragChangeCount = currentCount
            shakeDetector = DragShakeDetector()
        }
        let supportedTypes: [NSPasteboard.PasteboardType] = [.fileURL, .png, .tiff, .URL, .string]
        guard dragPB.availableType(from: supportedTypes) != nil else { return }
        if shakeDetector.record(x: mouseX, at: timestamp) {
            NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
            showAtCursorEdge()
        }
    }

    var pendingCount: Int {
        items.count
    }

    var isUploadingAny: Bool {
        items.contains(where: \.isUploading)
    }

    func toggleAtCursor() {
        if isVisible && items.isEmpty {
            hide()
            return
        }
        showAtCursorEdge()
    }

    func showAtCursorEdge() {
        ensurePanel()
        guard let panel else { return }

        let resolved: ShelfPlacement
        if let saved = SavedShelfPosition.load() {
            resolved = ScreenEdgeResolver.resolveFromSaved(saved)
        } else {
            resolved = ScreenEdgeResolver.resolve(for: NSEvent.mouseLocation)
        }
        currentPlacement = resolved
        dockMode = resolved.mode
        dockEdge = resolved.edge
        phase = items.isEmpty ? .idleEar : .stashedStack

        let size = ScreenEdgeResolver.targetSize(for: phase, itemCount: items.count)
        let targetFrame = ScreenEdgeResolver.frame(for: size, placement: resolved)
        programmaticMoveCount += 1
        panel.setFrame(targetFrame, display: true)
        programmaticMoveCount = max(0, programmaticMoveCount - 1)
        panel.invalidateShadow()
        panel.orderFrontRegardless()
        isVisible = true
    }

    func resetCustomPosition() {
        SavedShelfPosition.clear()
        guard isVisible else { return }
        ensurePanel()
        guard let panel else { return }
        let resolved = ScreenEdgeResolver.resolve(for: NSEvent.mouseLocation)
        currentPlacement = resolved
        dockMode = resolved.mode
        dockEdge = resolved.edge
        let size = ScreenEdgeResolver.targetSize(for: phase, itemCount: items.count)
        let targetFrame = ScreenEdgeResolver.frame(for: size, placement: resolved)
        programmaticMoveCount += 1
        panel.setFrame(targetFrame, display: true)
        programmaticMoveCount = max(0, programmaticMoveCount - 1)
        panel.invalidateShadow()
    }

    func windowDidMove(_ notification: Notification) {
        guard programmaticMoveCount == 0, isVisible, let panel else { return }
        let frame = panel.frame
        let center = CGPoint(x: frame.midX, y: frame.midY)
        let screen = NSScreen.screens.first(where: { $0.frame.contains(center) })
            ?? panel.screen
            ?? NSScreen.main
        guard let screen else { return }

        let saved = ScreenEdgeResolver.classifyDraggedFrame(frame, in: screen.visibleFrame)
        saved.save()
        dockMode = saved.mode
        dockEdge = saved.mode.edge
        currentPlacement = ShelfPlacement(
            mode: saved.mode,
            visibleFrame: screen.visibleFrame,
            anchorPoint: CGPoint(x: saved.x, y: saved.y)
        )
    }

    func hide() {
        panel?.orderOut(nil)
        isVisible = false
        phase = items.isEmpty ? .idleEar : .stashedStack
    }

    func clear() {
        items.removeAll()
        onPendingCountChanged?(0)
        if isVisible {
            morph(to: .idleEar, animated: true)
        } else {
            phase = .idleEar
        }
    }

    func removeItem(id: UUID) {
        items.removeAll { $0.id == id }
        onPendingCountChanged?(items.count)
        if items.isEmpty {
            morph(to: .idleEar, animated: true)
        } else {
            morph(to: .stashedStack, animated: true)
        }
    }

    func deliverToQuickEntry() {
        let attachments = items.compactMap(\.attachment)
        let snippets = items.compactMap(\.textSnippet)
        guard !attachments.isEmpty || !snippets.isEmpty else { return }
        items.removeAll()
        onPendingCountChanged?(0)
        hide()
        onDeliverToQuickEntry?(attachments, snippets)
    }

    func summarizeStashedItems() {
        let attachments = items.compactMap(\.attachment)
        let snippets = items.compactMap(\.textSnippet)
        guard !attachments.isEmpty || !snippets.isEmpty else { return }
        items.removeAll()
        onPendingCountChanged?(0)
        hide()

        var prompt = ShelfQuickSlot.summary.promptPrefix
        if !snippets.isEmpty {
            prompt += "\n\n" + snippets.joined(separator: "\n---\n")
        }
        brainClient.sendText(prompt, attachments: attachments, includeScreen: false)
        TaskToastController.shared.show(
            title: "已派发 · \(ShelfQuickSlot.summary.rawValue)",
            message: "正在后台处理暂存素材，完成后将在工作区与通知提示",
            isError: false
        ) {
            NotificationCenter.default.post(name: .qmxOpenWorkspace, object: nil)
        }
    }

    func configureForPreview(edge: ShelfDockEdge, phase: ShelfPhase, items: [DropPocketItem]) {
        self.dockMode = edge == .left ? .left : .right
        self.dockEdge = edge
        self.phase = phase
        self.items = items
    }

    // MARK: - Private Setup & Morphing

    private func ensurePanel() {
        guard panel == nil else { return }
        let initialSize = ScreenEdgeResolver.idleEarSize
        let p = DropShelfPanel(
            contentRect: NSRect(origin: .zero, size: initialSize),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        p.title = "投喂托盘"
        p.isFloatingPanel = true
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.appearance = NSAppearance(named: .darkAqua)
        p.delegate = self

        let hostView = EdgeDropHostView(frame: NSRect(origin: .zero, size: initialSize))
        hostView.wantsLayer = true
        hostView.layer?.backgroundColor = NSColor.clear.cgColor
        hostView.onDragEntered = { [weak self] in
            self?.morph(to: .dragExpanded(hoveredSlot: nil), animated: true)
        }
        hostView.onHoverSlotChanged = { [weak self] slot in
            guard let self else { return }
            if case .dragExpanded = self.phase {
                self.phase = .dragExpanded(hoveredSlot: slot)
            }
        }
        hostView.onDragExited = { [weak self] in
            guard let self else { return }
            let restPhase: ShelfPhase = self.items.isEmpty ? .idleEar : .stashedStack
            self.morph(to: restPhase, animated: true)
        }
        hostView.onDropped = { [weak self] slot, payload in
            self?.handleDrop(slot: slot, payload: payload)
        }

        let swiftUIView = EdgeShelfRootView(
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
        p.setContentSize(initialSize)
        self.panel = p
    }

    private func morph(to nextPhase: ShelfPhase, animated: Bool) {
        phase = nextPhase
        guard let panel, let placement = currentPlacement else { return }
        let targetSize = ScreenEdgeResolver.targetSize(for: nextPhase, itemCount: items.count)
        let nextFrame = ScreenEdgeResolver.frame(for: targetSize, placement: placement)

        guard abs(panel.frame.width - nextFrame.width) > 0.5
            || abs(panel.frame.height - nextFrame.height) > 0.5
            || abs(panel.frame.minX - nextFrame.minX) > 0.5
            || abs(panel.frame.minY - nextFrame.minY) > 0.5
        else { return }

        if !animated || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            programmaticMoveCount += 1
            panel.setFrame(nextFrame, display: true)
            programmaticMoveCount = max(0, programmaticMoveCount - 1)
            panel.invalidateShadow()
        } else {
            programmaticMoveCount += 1
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = 0.24
                ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)
                panel.animator().setFrame(nextFrame, display: true)
            }, completionHandler: { [weak self] in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.programmaticMoveCount = max(0, self.programmaticMoveCount - 1)
                    self.panel?.invalidateShadow()
                }
            })
        }
    }

    // MARK: - Drag Ingestion, QuickLook Thumbnails & Quick Slots

    private func handleDrop(slot: ShelfQuickSlot?, payload: ExtractedDragPayload) {
        if let slot {
            executeQuickSlot(slot, payload: payload)
        } else {
            ingestDropped(payload: payload)
        }
    }

    private func ingestDropped(payload: ExtractedDragPayload) {
        for url in payload.fileURLs {
            let itemId = UUID()
            let ext = url.pathExtension.lowercased()
            let kind = inferKind(ext: ext)
            let fallbackIcon = NSWorkspace.shared.icon(forFile: url.path)
            items.append(
                DropPocketItem(
                    id: itemId,
                    filename: url.lastPathComponent,
                    kind: kind,
                    isUploading: true,
                    sourceURL: url,
                    thumbnail: kind == "image" ? NSImage(contentsOf: url) ?? fallbackIcon : fallbackIcon
                )
            )
            onPendingCountChanged?(items.count)
            morph(to: .stashedStack, animated: true)
            loadThumbnail(for: url, itemId: itemId)

            Task { [weak self] in
                guard let self else { return }
                let uploaded = await self.brainClient.uploadArtifact(fileURL: url)
                if let idx = self.items.firstIndex(where: { $0.id == itemId }) {
                    self.items[idx].isUploading = false
                    self.items[idx].attachment = uploaded
                    if uploaded == nil {
                        self.items.remove(at: idx)
                        self.onPendingCountChanged?(self.items.count)
                        let nextPhase: ShelfPhase = self.items.isEmpty ? .idleEar : .stashedStack
                        self.morph(to: nextPhase, animated: true)
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
                    isUploading: true,
                    thumbnail: NSImage(data: png)
                )
            )
            onPendingCountChanged?(items.count)
            morph(to: .stashedStack, animated: true)

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
                        let nextPhase: ShelfPhase = self.items.isEmpty ? .idleEar : .stashedStack
                        self.morph(to: nextPhase, animated: true)
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
            morph(to: .stashedStack, animated: true)
        }
    }

    private func loadThumbnail(for url: URL, itemId: UUID) {
        let scale = NSScreen.main?.backingScaleFactor ?? 2.0
        let request = QLThumbnailGenerator.Request(
            fileAt: url,
            size: CGSize(width: 120, height: 100),
            scale: scale,
            representationTypes: .thumbnail
        )
        let path = url.path
        QLThumbnailGenerator.shared.generateBestRepresentation(for: request) { [weak self] rep, _ in
            let cgImage = rep?.cgImage
            Task { @MainActor [weak self] in
                guard let self else { return }
                guard let idx = self.items.firstIndex(where: { $0.id == itemId }) else { return }
                if let cgImage {
                    self.items[idx].thumbnail = NSImage(
                        cgImage: cgImage,
                        size: NSSize(width: cgImage.width, height: cgImage.height)
                    )
                } else if self.items[idx].thumbnail == nil {
                    self.items[idx].thumbnail = NSWorkspace.shared.icon(forFile: path)
                }
            }
        }
    }

    private func executeQuickSlot(_ slot: ShelfQuickSlot, payload: ExtractedDragPayload) {
        let existingAttachments = items.compactMap(\.attachment)
        let existingSnippets = items.compactMap(\.textSnippet)
        items.removeAll()
        onPendingCountChanged?(0)
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
                title: "已派发 · \(slot.rawValue)",
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

// MARK: - Asymmetric Corner Shape (macOS 13+)

struct AsymmetricRoundedRect: InsettableShape {
    var topLeading: CGFloat = 0
    var bottomLeading: CGFloat = 0
    var bottomTrailing: CGFloat = 0
    var topTrailing: CGFloat = 0
    var insetAmount: CGFloat = 0

    func inset(by amount: CGFloat) -> Self {
        var copy = self
        copy.insetAmount += amount
        return copy
    }

    func path(in rect: CGRect) -> Path {
        let r = rect.insetBy(dx: insetAmount, dy: insetAmount)
        guard r.width > 0, r.height > 0 else { return Path() }

        let maxR = min(r.width, r.height) / 2
        let tl = min(max(0, topLeading - insetAmount), maxR)
        let tr = min(max(0, topTrailing - insetAmount), maxR)
        let br = min(max(0, bottomTrailing - insetAmount), maxR)
        let bl = min(max(0, bottomLeading - insetAmount), maxR)

        var path = Path()
        path.move(to: CGPoint(x: r.minX + tl, y: r.minY))
        path.addLine(to: CGPoint(x: r.maxX - tr, y: r.minY))
        if tr > 0 {
            path.addArc(
                center: CGPoint(x: r.maxX - tr, y: r.minY + tr),
                radius: tr,
                startAngle: .degrees(-90),
                endAngle: .degrees(0),
                clockwise: false
            )
        }
        path.addLine(to: CGPoint(x: r.maxX, y: r.maxY - br))
        if br > 0 {
            path.addArc(
                center: CGPoint(x: r.maxX - br, y: r.maxY - br),
                radius: br,
                startAngle: .degrees(0),
                endAngle: .degrees(90),
                clockwise: false
            )
        }
        path.addLine(to: CGPoint(x: r.minX + bl, y: r.maxY))
        if bl > 0 {
            path.addArc(
                center: CGPoint(x: r.minX + bl, y: r.maxY - bl),
                radius: bl,
                startAngle: .degrees(90),
                endAngle: .degrees(180),
                clockwise: false
            )
        }
        path.addLine(to: CGPoint(x: r.minX, y: r.minY + tl))
        if tl > 0 {
            path.addArc(
                center: CGPoint(x: r.minX + tl, y: r.minY + tl),
                radius: tl,
                startAngle: .degrees(180),
                endAngle: .degrees(270),
                clockwise: false
            )
        }
        path.closeSubpath()
        return path
    }
}

// MARK: - SwiftUI View (Dark Obsidian Edge Shelf)

struct EdgeShelfRootView: View {
    @ObservedObject var controller: DropPocketController
    let onClose: () -> Void
    @State private var isHovering = false
    @State private var hoveredItemId: UUID?

    private var shelfShape: AsymmetricRoundedRect {
        let r = ScreenEdgeResolver.cornerRadius
        switch controller.dockMode {
        case .right:
            return AsymmetricRoundedRect(topLeading: r, bottomLeading: r, bottomTrailing: 0, topTrailing: 0)
        case .left:
            return AsymmetricRoundedRect(topLeading: 0, bottomLeading: 0, bottomTrailing: r, topTrailing: r)
        case .floating:
            return AsymmetricRoundedRect(topLeading: r, bottomLeading: r, bottomTrailing: r, topTrailing: r)
        }
    }

    private var isDragActive: Bool {
        if case .dragExpanded = controller.phase { return true }
        return false
    }

    private var cyanGlowColor: Color {
        Color(red: 0.36, green: 0.88, blue: 1.0)
    }

    var body: some View {
        ZStack {
            if !QMXTheme.isHeadlessDump {
                VisualEffectBlur(
                    material: .hudWindow,
                    blendingMode: .behindWindow,
                    state: .active,
                    cornerRadius: 0
                )
            }
            // Dark smoky obsidian frosted glass regardless of system theme
            LinearGradient(
                colors: [
                    Color(red: 0.095, green: 0.110, blue: 0.138).opacity(QMXTheme.isHeadlessDump ? 0.94 : 0.80),
                    Color(red: 0.058, green: 0.068, blue: 0.088).opacity(QMXTheme.isHeadlessDump ? 0.96 : 0.84),
                ],
                startPoint: .top,
                endPoint: .bottom
            )

            if isDragActive {
                shelfShape
                    .strokeBorder(cyanGlowColor.opacity(0.28), lineWidth: 5)
                    .blur(radius: 4)
            }

            Group {
                switch controller.phase {
                case .idleEar:
                    idleEarContent
                case .dragExpanded(let hoveredSlot):
                    dragExpandedContent(hoveredSlot: hoveredSlot)
                case .stashedStack:
                    stashedStackContent
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .clipShape(shelfShape)
        .overlay(borderOverlay)
        .environment(\.colorScheme, .dark)
        .onHover { isHovering = $0 }
        .contextMenu {
            Button("重置到屏幕边缘") {
                controller.resetCustomPosition()
            }
            Button("收起投喂托盘", action: onClose)
        }
    }

    private var borderOverlay: some View {
        let strokeGradient = LinearGradient(
            colors: isDragActive
                ? [cyanGlowColor.opacity(0.95), cyanGlowColor.opacity(0.75)]
                : [Color.white.opacity(0.28), Color.white.opacity(0.10)],
            startPoint: .top,
            endPoint: .bottom
        )
        return shelfShape.strokeBorder(strokeGradient, lineWidth: isDragActive ? 1.5 : 0.75)
    }

    private var idleEarContent: some View {
        ZStack {
            // Centered morning star (with subtle centered cyan halo when free-floating)
            if controller.dockMode == .floating {
                Circle()
                    .fill(cyanGlowColor.opacity(0.24))
                    .frame(width: 28, height: 28)
                    .blur(radius: 6)
            }
            MorningStarMark(size: 18, color: .white)

            // Docked screen-edge cyan vertical light streak (hidden when floating)
            if controller.dockMode != .floating {
                HStack {
                    if controller.dockEdge == .right { Spacer() }
                    Capsule()
                        .fill(
                            LinearGradient(
                                colors: [.clear, cyanGlowColor.opacity(0.92), .clear],
                                startPoint: .top,
                                endPoint: .bottom
                            )
                        )
                        .frame(width: 2.5, height: 68)
                        .shadow(color: cyanGlowColor.opacity(0.85), radius: 5)
                    if controller.dockEdge == .left { Spacer() }
                }
                .padding(controller.dockEdge == .right ? .trailing : .leading, 1)
            }

            if isHovering {
                VStack {
                    Button(action: onClose) {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(Color.white.opacity(0.68))
                            .frame(width: 18, height: 18)
                            .background(Color.white.opacity(0.12), in: Circle())
                    }
                    .buttonStyle(.plain)
                    .padding(.top, 12)
                    .help("收起投喂托盘")
                    Spacer()
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func dragExpandedContent(hoveredSlot: ShelfQuickSlot?) -> some View {
        let dropZoneFill = Color.white.opacity(hoveredSlot == nil ? 0.065 : 0.03)
        let dropZoneStroke = Color.white.opacity(hoveredSlot == nil ? 0.16 : 0.08)
        let iconColor = Color.white.opacity(hoveredSlot == nil ? 0.50 : 0.35)
        let titleColor = Color.white.opacity(hoveredSlot == nil ? 0.88 : 0.56)

        return VStack(spacing: 9) {
            VStack(spacing: 7) {
                Image(systemName: "tray.and.arrow.down")
                    .font(.system(size: 24, weight: .light))
                    .foregroundStyle(iconColor)
                Text("拖入文件、图片或文本")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(titleColor)
                Text("⌥D")
                    .font(.system(size: 10, weight: .medium, design: .rounded))
                    .foregroundStyle(Color.white.opacity(0.56))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(
                        RoundedRectangle(cornerRadius: 5, style: .continuous)
                            .fill(Color.white.opacity(0.08))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 5, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5)
                    )
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(
                RoundedRectangle(cornerRadius: 13, style: .continuous)
                    .fill(dropZoneFill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 13, style: .continuous)
                    .strokeBorder(dropZoneStroke, lineWidth: 0.75)
            )

            HStack(spacing: 8) {
                ForEach(ShelfQuickSlot.allCases) { slot in
                    quickSlotCard(slot, active: hoveredSlot == slot)
                }
            }
        }
        .padding(12)
    }

    private func quickSlotCard(_ slot: ShelfQuickSlot, active: Bool) -> some View {
        let fgColor: Color = active ? .white : Color.white.opacity(0.84)
        let bgColor: Color = active
            ? Color(red: 0.25, green: 0.65, blue: 0.95).opacity(0.42)
            : Color.white.opacity(0.07)
        let strokeColor: Color = active ? cyanGlowColor.opacity(0.85) : Color.white.opacity(0.15)

        return VStack(spacing: 3) {
            Image(systemName: slot.symbol)
                .font(.system(size: 13, weight: .medium))
            Text(slot.rawValue)
                .font(.system(size: 11, weight: .medium))
        }
        .foregroundStyle(fgColor)
        .frame(maxWidth: .infinity)
        .frame(height: 48)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(bgColor)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(strokeColor, lineWidth: active ? 1 : 0.75)
        )
    }

    private var stashedStackContent: some View {
        let canAct = controller.items.contains { $0.attachment != nil || $0.textSnippet != nil }

        return ZStack(alignment: .topTrailing) {
            VStack(spacing: 0) {
                VStack(spacing: 10) {
                    ForEach(Array(controller.items.prefix(3))) { item in
                        stashedItemTile(item)
                    }

                    if controller.items.count > 3 {
                        Text("+\(controller.items.count - 3)")
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(Color.white.opacity(0.65))
                    }
                }
                .padding(.top, 14)

                Spacer(minLength: 6)

                Rectangle()
                    .fill(Color.white.opacity(0.12))
                    .frame(height: 0.5)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)

                HStack(spacing: 12) {
                    Button {
                        controller.deliverToQuickEntry()
                    } label: {
                        Image(systemName: "arrow.up.right")
                            .font(.system(size: 13, weight: .bold))
                            .foregroundStyle(Color.black)
                            .frame(width: 34, height: 34)
                            .background(Color.white.opacity(canAct ? 1.0 : 0.45), in: Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!canAct)
                    .help("带入对话")

                    Button {
                        controller.summarizeStashedItems()
                    } label: {
                        Image(systemName: "text.bubble")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(Color.white.opacity(canAct ? 0.90 : 0.40))
                            .frame(width: 34, height: 34)
                            .background(Color.white.opacity(0.10), in: Circle())
                            .overlay(Circle().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5))
                    }
                    .buttonStyle(.plain)
                    .disabled(!canAct)
                    .help("立即速览")
                }
                .padding(.bottom, 12)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            if isHovering {
                Button {
                    controller.clear()
                } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(Color.white.opacity(0.70))
                        .frame(width: 20, height: 20)
                        .background(Color.black.opacity(0.55), in: Circle())
                        .overlay(Circle().strokeBorder(Color.white.opacity(0.20), lineWidth: 0.5))
                }
                .buttonStyle(.plain)
                .padding(.top, 8)
                .padding(.trailing, 8)
                .help("清空全部暂存")
            }
        }
    }

    private func stashedItemTile(_ item: DropPocketItem) -> some View {
        let isHoveredTile = hoveredItemId == item.id
        return ZStack(alignment: .topTrailing) {
            stashedItemPreview(item)
                .overlay {
                    if item.isUploading {
                        ZStack {
                            Color.black.opacity(0.45)
                            ProgressView()
                                .controlSize(.small)
                        }
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    }
                }

            if isHoveredTile {
                Button {
                    controller.removeItem(id: item.id)
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 15, height: 15)
                        .background(Color.black.opacity(0.82), in: Circle())
                        .overlay(Circle().strokeBorder(Color.white.opacity(0.30), lineWidth: 0.5))
                }
                .buttonStyle(.plain)
                .offset(x: 5, y: -5)
                .help("移除 \(item.filename)")
            }
        }
        .onHover { hovering in
            hoveredItemId = hovering ? item.id : (hoveredItemId == item.id ? nil : hoveredItemId)
        }
        .help(item.filename)
    }

    @ViewBuilder
    private func stashedItemPreview(_ item: DropPocketItem) -> some View {
        if let thumb = item.thumbnail {
            if item.kind == "image" {
                Image(nsImage: thumb)
                    .resizable()
                    .scaledToFill()
                    .frame(width: 72, height: 46)
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.22), lineWidth: 0.5)
                    )
                    .shadow(color: .black.opacity(0.38), radius: 4, y: 2)
            } else {
                Image(nsImage: thumb)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 60, height: 54)
                    .shadow(color: .black.opacity(0.38), radius: 4, y: 2)
            }
        } else {
            textDocThumbnailCard(label: item.filename)
                .shadow(color: .black.opacity(0.38), radius: 4, y: 2)
        }
    }

    private func textDocThumbnailCard(label: String) -> some View {
        ZStack(alignment: .topTrailing) {
            VStack(alignment: .leading, spacing: 3) {
                Text(label)
                    .font(.system(size: 6.5, weight: .semibold))
                    .foregroundStyle(Color.black.opacity(0.78))
                    .lineLimit(1)
                Capsule().fill(Color.black.opacity(0.22)).frame(width: 34, height: 1.5)
                Capsule().fill(Color.black.opacity(0.16)).frame(width: 38, height: 1.5)
                Capsule().fill(Color.black.opacity(0.16)).frame(width: 28, height: 1.5)
                Capsule().fill(Color.black.opacity(0.14)).frame(width: 34, height: 1.5)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 6)
            .padding(.top, 9)
            .padding(.bottom, 6)
            .frame(width: 48, height: 54, alignment: .topLeading)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(Color(white: 0.94))
            )

            // Subtle folded corner accent
            Path { path in
                path.move(to: CGPoint(x: 37, y: 0))
                path.addLine(to: CGPoint(x: 48, y: 11))
                path.addLine(to: CGPoint(x: 37, y: 11))
                path.closeSubpath()
            }
            .fill(Color(white: 0.80))
            .frame(width: 48, height: 54)
        }
    }
}

import AppKit
import Combine
import ScreenCaptureKit
import SwiftUI

// MARK: - Pure Testable Spatial Geometry

enum SpatialLassoGeometry {
    static let popoverWidth: CGFloat = 340
    static let inputHeight: CGFloat = 152
    static let answeredHeight: CGFloat = 340

    static func normalizedRect(from start: CGPoint, to end: CGPoint) -> CGRect {
        CGRect(
            x: min(start.x, end.x),
            y: min(start.y, end.y),
            width: abs(end.x - start.x),
            height: abs(end.y - start.y)
        )
    }

    /// Converts an AppKit global bottom-left selection rectangle into display-local
    /// top-left point coordinates expected by ScreenCaptureKit / CGDisplayCreateImage.
    static func displayLocalSourceRect(selection: CGRect, screenFrame: CGRect) -> CGRect {
        let clipped = selection.intersection(screenFrame)
        guard !clipped.isNull else { return .zero }
        return CGRect(
            x: clipped.minX - screenFrame.minX,
            y: screenFrame.maxY - clipped.maxY,
            width: clipped.width,
            height: clipped.height
        )
    }

    /// Places the compact Q&A popover adjacent to the selected region without jumping
    /// to screen center, clamping strictly inside `visibleFrame`.
    static func popoverFrame(
        for selection: CGRect,
        popoverSize: CGSize,
        visibleFrame: CGRect,
        gap: CGFloat = 10,
        margin: CGFloat = 12
    ) -> CGRect {
        let safe = visibleFrame.insetBy(dx: margin, dy: margin)
        guard safe.width > 0, safe.height > 0 else {
            return CGRect(origin: selection.origin, size: popoverSize)
        }

        let belowY = selection.minY - gap - popoverSize.height
        let aboveY = selection.maxY + gap
        let rightX = selection.maxX + gap
        let leftX = selection.minX - gap - popoverSize.width

        let candidateOrigin: CGPoint
        if belowY >= safe.minY {
            candidateOrigin = CGPoint(x: selection.minX, y: belowY)
        } else if aboveY + popoverSize.height <= safe.maxY {
            candidateOrigin = CGPoint(x: selection.minX, y: aboveY)
        } else if rightX + popoverSize.width <= safe.maxX {
            candidateOrigin = CGPoint(x: rightX, y: selection.maxY - popoverSize.height)
        } else if leftX >= safe.minX {
            candidateOrigin = CGPoint(x: leftX, y: selection.maxY - popoverSize.height)
        } else {
            candidateOrigin = CGPoint(x: selection.minX, y: belowY)
        }

        let maxOriginX = max(safe.minX, safe.maxX - popoverSize.width)
        let maxOriginY = max(safe.minY, safe.maxY - popoverSize.height)
        let clampedX = min(max(candidateOrigin.x, safe.minX), maxOriginX)
        let clampedY = min(max(candidateOrigin.y, safe.minY), maxOriginY)
        return CGRect(origin: CGPoint(x: clampedX, y: clampedY), size: popoverSize)
    }

    static func extractInlineArtifactText(
        media: [MediaItem],
        capsules: [ArtifactCapsuleItem]
    ) -> String {
        var sections: [String] = []
        var seenPaths = Set<String>()
        let textExts: Set<String> = [
            "md", "markdown", "txt", "json", "csv",
            "py", "swift", "ts", "js", "html", "xml", "yaml", "yml"
        ]

        func appendFileIfReadable(title: String, path: String) {
            let trimmedPath = path.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmedPath.isEmpty, !seenPaths.contains(trimmedPath) else { return }
            seenPaths.insert(trimmedPath)

            let url = URL(fileURLWithPath: trimmedPath)
            let ext = url.pathExtension.lowercased()
            guard ext.isEmpty || textExts.contains(ext) else { return }
            guard let attrs = try? FileManager.default.attributesOfItem(atPath: trimmedPath),
                  let size = attrs[.size] as? NSNumber,
                  size.intValue > 0,
                  size.intValue <= 65_536,
                  let raw = try? String(contentsOf: url, encoding: .utf8)
            else { return }

            let clean = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !clean.isEmpty else { return }
            if !title.isEmpty && !clean.hasPrefix("#") {
                sections.append("[\(title)]\n\(clean)")
            } else {
                sections.append(clean)
            }
        }

        for item in media {
            appendFileIfReadable(title: item.title, path: item.localPath)
        }
        for cap in capsules {
            appendFileIfReadable(title: cap.title, path: cap.path)
        }

        if sections.isEmpty {
            for item in media where !item.previewColumns.isEmpty {
                let header = "| " + item.previewColumns.joined(separator: " | ") + " |"
                let sep = "| " + item.previewColumns.map { _ in "---" }.joined(separator: " | ") + " |"
                let body = item.previewRows.map { "| " + $0.joined(separator: " | ") + " |" }.joined(separator: "\n")
                let titleLine = item.title.isEmpty ? "" : "[\(item.title)]\n"
                sections.append(titleLine + header + "\n" + sep + (body.isEmpty ? "" : "\n" + body))
            }
        }

        return sections.joined(separator: "\n\n")
    }
}

struct LassoArtifactRef: Identifiable, Equatable {
    let id: String
    let title: String
    let path: String
}

// MARK: - Overlay, Popover & Pinned Panels

@MainActor
private final class LassoOverlayPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    var onCancel: (() -> Void)?

    override func cancelOperation(_ sender: Any?) {
        onCancel?()
    }
}

@MainActor
final class LassoPopoverPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    var onCancel: (() -> Void)?

    override func cancelOperation(_ sender: Any?) {
        onCancel?()
    }
}

@MainActor
final class PinnedNotePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    var onCancel: (() -> Void)?

    override func cancelOperation(_ sender: Any?) {
        onCancel?()
    }
}

struct PinnedNoteItem: Identifiable, Equatable {
    let id: UUID
    let thumbnail: NSImage?
    let question: String
    let answer: String
    var artifactFiles: [LassoArtifactRef] = []
}

// MARK: - Full-Screen Crosshair Selection View

private final class LassoSelectionView: NSView {
    var screenFrame: CGRect = .zero
    var windowResolver: ((CGPoint) -> CGRect?)?
    var onSelectionCompleted: ((CGRect) -> Void)?
    var onCancelled: (() -> Void)?

    private var dragStart: CGPoint?
    private var dragCurrent: CGPoint?
    private var hoveredWindowRect: CGRect?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        let area = NSTrackingArea(
            rect: .zero,
            options: [.mouseMoved, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var acceptsFirstResponder: Bool { true }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .crosshair)
    }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 {
            onCancelled?()
        } else {
            super.keyDown(with: event)
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        onCancelled?()
    }

    override func mouseMoved(with event: NSEvent) {
        guard dragStart == nil else { return }
        let localPt = convert(event.locationInWindow, from: nil)
        let globalPt = CGPoint(x: screenFrame.minX + localPt.x, y: screenFrame.minY + localPt.y)
        if let globalWin = windowResolver?(globalPt) {
            hoveredWindowRect = CGRect(
                x: globalWin.minX - screenFrame.minX,
                y: globalWin.minY - screenFrame.minY,
                width: globalWin.width,
                height: globalWin.height
            )
        } else {
            hoveredWindowRect = nil
        }
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        let pt = convert(event.locationInWindow, from: nil)
        dragStart = pt
        dragCurrent = pt
        hoveredWindowRect = nil
        needsDisplay = true
    }

    override func mouseDragged(with event: NSEvent) {
        dragCurrent = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseUp(with event: NSEvent) {
        guard let start = dragStart else { return }
        let end = convert(event.locationInWindow, from: nil)
        dragStart = nil
        dragCurrent = nil

        let localRect = SpatialLassoGeometry.normalizedRect(from: start, to: end)
        if localRect.width < 8 && localRect.height < 8 {
            let globalPt = CGPoint(x: screenFrame.minX + end.x, y: screenFrame.minY + end.y)
            if let winRect = windowResolver?(globalPt), winRect.width > 20, winRect.height > 20 {
                onSelectionCompleted?(winRect)
            } else {
                onCancelled?()
            }
            return
        }

        let clamped = localRect.intersection(bounds)
        guard clamped.width >= 8, clamped.height >= 8 else {
            onCancelled?()
            return
        }
        let globalRect = CGRect(
            x: screenFrame.minX + clamped.minX,
            y: screenFrame.minY + clamped.minY,
            width: clamped.width,
            height: clamped.height
        )
        onSelectionCompleted?(globalRect)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.withAlphaComponent(0.28).setFill()
        bounds.fill()

        let activeRect: CGRect?
        if let s = dragStart, let c = dragCurrent {
            activeRect = SpatialLassoGeometry.normalizedRect(from: s, to: c)
        } else {
            activeRect = hoveredWindowRect
        }

        if let rect = activeRect, rect.width >= 4, rect.height >= 4 {
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current?.compositingOperation = .clear
            NSColor.clear.setFill()
            rect.fill()
            NSGraphicsContext.restoreGraphicsState()

            let border = NSBezierPath(rect: rect.insetBy(dx: 0.75, dy: 0.75))
            border.lineWidth = 1.5
            NSColor(calibratedRed: 0.36, green: 0.88, blue: 1.0, alpha: 0.95).setStroke()
            border.stroke()

            let sizeText = "\(Int(rect.width)) × \(Int(rect.height))"
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium),
                .foregroundColor: NSColor.white,
            ]
            let attrStr = NSAttributedString(string: sizeText, attributes: attrs)
            let strSize = attrStr.size()
            let pillSize = NSSize(width: strSize.width + 12, height: 20)
            let pillX = min(max(rect.minX, 8), bounds.maxX - pillSize.width - 8)
            let pillY = rect.minY >= pillSize.height + 10 ? rect.minY - pillSize.height - 6 : rect.maxY + 6
            let pillRect = NSRect(origin: NSPoint(x: pillX, y: pillY), size: pillSize)
            NSColor(calibratedRed: 0.067, green: 0.078, blue: 0.102, alpha: 0.88).setFill()
            NSBezierPath(roundedRect: pillRect, xRadius: 6, yRadius: 6).fill()
            attrStr.draw(at: NSPoint(x: pillRect.minX + 6, y: pillRect.minY + 3))
        }
    }
}

// MARK: - Spatial Lasso State & Controller

@MainActor
final class LassoPopoverState: ObservableObject {
    @Published var thumbnail: NSImage?
    @Published var attachment: StagedAttachment?
    @Published var isUploading: Bool = false
    @Published var questionText: String = ""
    @Published var submittedQuestion: String = ""
    @Published var answerText: String = ""
    @Published var artifactFiles: [LassoArtifactRef] = []
    @Published var isStreamingAnswer: Bool = false
    @Published var statusNote: String = ""
    @Published var boundTurnId: String?

    var hasAnswerUI: Bool {
        isStreamingAnswer || !answerText.isEmpty || !statusNote.isEmpty || !artifactFiles.isEmpty
    }
}

@MainActor
final class SpatialLassoController: NSObject {
    private let brainClient: BrainClient
    private var overlayPanel: LassoOverlayPanel?
    private var cursorPushed = false
    private var popoverPanel: LassoPopoverPanel?
    private let popoverState = LassoPopoverState()
    private var activeSelectionRect: CGRect = .zero
    private var activeVisibleFrame: CGRect = .zero
    private var cancellables = Set<AnyCancellable>()
    private var pinnedPanels: [UUID: PinnedNotePanel] = [:]

    init(brainClient: BrainClient) {
        self.brainClient = brainClient
        super.init()
        bindBrainMessages()
    }

    func startSelection() {
        if overlayPanel != nil {
            cancelSelection()
            return
        }
        closePopover()

        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) ?? NSScreen.main
        guard let screen else { return }

        activeVisibleFrame = screen.visibleFrame

        if !CGPreflightScreenCaptureAccess() {
            CGRequestScreenCaptureAccess()
            TaskToastController.shared.show(
                title: "需要屏幕录制权限",
                message: "请在系统设置 → 隐私与安全性 → 录屏与系统录音 中授权启明星",
                isError: true
            ) {
                if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                    NSWorkspace.shared.open(url)
                }
            }
            return
        }

        let panel = LassoOverlayPanel(
            contentRect: screen.frame,
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        panel.level = .screenSaver
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = false
        panel.onCancel = { [weak self] in self?.cancelSelection() }

        let view = LassoSelectionView(frame: NSRect(origin: .zero, size: screen.frame.size))
        view.screenFrame = screen.frame
        view.windowResolver = { [weak self] pt in
            self?.topWindowBounds(at: pt, on: screen)
        }
        view.onSelectionCompleted = { [weak self] globalRect in
            self?.finishSelection(globalRect, on: screen)
        }
        view.onCancelled = { [weak self] in
            self?.cancelSelection()
        }

        panel.contentView = view
        overlayPanel = panel
        if !cursorPushed {
            NSCursor.crosshair.push()
            cursorPushed = true
        }
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(view)
    }

    func cancelSelection() {
        if cursorPushed {
            NSCursor.pop()
            cursorPushed = false
        }
        overlayPanel?.orderOut(nil)
        overlayPanel = nil
    }

    func closePopover() {
        popoverPanel?.orderOut(nil)
        popoverPanel = nil
        popoverState.boundTurnId = nil
        popoverState.isStreamingAnswer = false
    }

    // MARK: - Capture & In-Place Popover

    private func finishSelection(_ globalRect: CGRect, on screen: NSScreen) {
        cancelSelection()
        activeSelectionRect = globalRect

        Task { [weak self] in
            guard let self else { return }
            // Wait briefly for the overlay window server surface to disappear cleanly
            try? await Task.sleep(nanoseconds: 50_000_000)
            guard let cgImage = await self.captureRegion(globalRect, on: screen) else {
                TaskToastController.shared.show(
                    title: "截屏失败",
                    message: "请检查 macOS 屏幕录制权限是否有效",
                    isError: true
                ) {
                    if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                        NSWorkspace.shared.open(url)
                    }
                }
                return
            }

            let nsImage = NSImage(
                cgImage: cgImage,
                size: NSSize(width: globalRect.width, height: globalRect.height)
            )
            self.resetPopoverState(thumbnail: nsImage)
            self.showPopover(for: globalRect, visibleFrame: screen.visibleFrame)
            await self.uploadCapturedImage(cgImage)
        }
    }

    private func captureRegion(_ selection: CGRect, on screen: NSScreen) async -> CGImage? {
        let localRect = SpatialLassoGeometry.displayLocalSourceRect(selection: selection, screenFrame: screen.frame)
        guard localRect.width >= 4, localRect.height >= 4 else { return nil }
        let displayID = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
            ?? CGMainDisplayID()

        if #available(macOS 14.0, *) {
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                if let display = content.displays.first(where: { $0.displayID == displayID }) ?? content.displays.first {
                    let filter = SCContentFilter(display: display, excludingWindows: [])
                    let config = SCStreamConfiguration()
                    config.sourceRect = localRect
                    let scale = screen.backingScaleFactor
                    config.width = max(1, Int(localRect.width * scale))
                    config.height = max(1, Int(localRect.height * scale))
                    config.showsCursor = false
                    return try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
                }
            } catch {
                return nil
            }
        }
        return CGDisplayCreateImage(displayID, rect: localRect)
    }

    private func uploadCapturedImage(_ cgImage: CGImage) async {
        let rep = NSBitmapImageRep(cgImage: cgImage)
        guard let pngData = rep.representation(using: .png, properties: [:]) else {
            popoverState.isUploading = false
            popoverState.statusNote = "截图编码失败"
            return
        }
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("qmx_lasso_\(UUID().uuidString.prefix(8)).png")
        do {
            try pngData.write(to: tmp)
            defer { try? FileManager.default.removeItem(at: tmp) }
            let uploaded = await brainClient.uploadArtifact(fileURL: tmp)
            popoverState.isUploading = false
            popoverState.attachment = uploaded
            if uploaded == nil {
                popoverState.statusNote = "截图上传失败，请检查 Brain 连接"
            }
        } catch {
            popoverState.isUploading = false
            popoverState.statusNote = "写入临时截图失败"
        }
    }

    private func resetPopoverState(thumbnail: NSImage) {
        popoverState.thumbnail = thumbnail
        popoverState.attachment = nil
        popoverState.isUploading = true
        popoverState.questionText = ""
        popoverState.submittedQuestion = ""
        popoverState.answerText = ""
        popoverState.artifactFiles = []
        popoverState.isStreamingAnswer = false
        popoverState.statusNote = ""
        popoverState.boundTurnId = nil
    }

    private func showPopover(for selection: CGRect, visibleFrame: CGRect) {
        let size = CGSize(width: SpatialLassoGeometry.popoverWidth, height: SpatialLassoGeometry.inputHeight)
        let frame = SpatialLassoGeometry.popoverFrame(
            for: selection,
            popoverSize: size,
            visibleFrame: visibleFrame
        )

        let p = LassoPopoverPanel(
            contentRect: frame,
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        p.title = "屏幕圈选问答"
        p.isFloatingPanel = true
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.appearance = NSAppearance(named: .darkAqua)
        p.onCancel = { [weak self] in self?.closePopover() }

        let view = LassoPopoverView(
            state: popoverState,
            onSubmit: { [weak self] prompt, title in
                self?.submitPopoverQuestion(prompt, displayTitle: title)
            },
            onPin: { [weak self] in self?.pinCurrentAnswer() },
            onClose: { [weak self] in self?.closePopover() }
        )
        let hosting = NSHostingController(rootView: view)
        hosting.sizingOptions = []
        if #available(macOS 13.3, *) {
            hosting.safeAreaRegions = []
        }
        hosting.view.wantsLayer = true
        hosting.view.layer?.backgroundColor = NSColor.clear.cgColor
        hosting.view.frame = NSRect(origin: .zero, size: size)
        p.contentView = hosting.view
        p.contentViewController = hosting

        popoverPanel = p
        NSApp.activate(ignoringOtherApps: true)
        p.makeKeyAndOrderFront(nil)
    }

    private func submitPopoverQuestion(_ rawPrompt: String, displayTitle: String? = nil) {
        let prompt = rawPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }
        guard brainClient.phase == .connected else {
            popoverState.statusNote = "Brain 尚未连接"
            return
        }
        guard !popoverState.isUploading, let att = popoverState.attachment else {
            popoverState.statusNote = popoverState.isUploading ? "截图还在上传，请稍候…" : "缺少截图附件"
            return
        }

        popoverState.statusNote = ""
        popoverState.submittedQuestion = displayTitle ?? prompt
        popoverState.questionText = ""
        popoverState.answerText = ""
        popoverState.artifactFiles = []
        popoverState.isStreamingAnswer = true
        resizePopover(expanded: true)

        brainClient.sendText(prompt, attachments: [att], includeScreen: false)
        popoverState.boundTurnId = brainClient.currentTurnId
    }

    private func resizePopover(expanded: Bool) {
        guard let panel = popoverPanel else { return }
        let targetHeight = expanded ? SpatialLassoGeometry.answeredHeight : SpatialLassoGeometry.inputHeight
        let targetSize = CGSize(width: SpatialLassoGeometry.popoverWidth, height: targetHeight)
        let nextFrame = SpatialLassoGeometry.popoverFrame(
            for: activeSelectionRect,
            popoverSize: targetSize,
            visibleFrame: activeVisibleFrame
        )
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            panel.setFrame(nextFrame, display: true)
        } else {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.20
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(nextFrame, display: true)
            }
        }
    }

    private func bindBrainMessages() {
        Publishers.CombineLatest3(
            brainClient.$messages,
            brainClient.$artifactCapsules,
            brainClient.$isGenerating
        )
        .sink { [weak self] messages, capsules, isGenerating in
            guard let self, let turnId = self.popoverState.boundTurnId else { return }
            let turnAssistantEntries = messages.filter { $0.turnId == turnId && !$0.isUser }
            let assistantTexts = turnAssistantEntries
                .map { qmxCleanAssistantText($0.text) }
                .filter { !$0.isEmpty }
            let combined = assistantTexts.joined(separator: "\n\n")

            let turnMedia = turnAssistantEntries.flatMap(\.media)
            let turnCapsules = capsules.filter { $0.turnId == turnId }

            var refs: [LassoArtifactRef] = []
            var seenRefPaths = Set<String>()
            for m in turnMedia where !m.localPath.isEmpty && !seenRefPaths.contains(m.localPath) {
                seenRefPaths.insert(m.localPath)
                let label = m.title.isEmpty ? URL(fileURLWithPath: m.localPath).lastPathComponent : m.title
                refs.append(LassoArtifactRef(id: m.localPath, title: label, path: m.localPath))
            }
            for c in turnCapsules where !c.path.isEmpty && !seenRefPaths.contains(c.path) {
                seenRefPaths.insert(c.path)
                let label = c.title.isEmpty ? URL(fileURLWithPath: c.path).lastPathComponent : c.title
                refs.append(LassoArtifactRef(id: c.path, title: label, path: c.path))
            }
            self.popoverState.artifactFiles = refs

            let inlineExtra = SpatialLassoGeometry.extractInlineArtifactText(
                media: turnMedia,
                capsules: turnCapsules
            )
            let fullAnswer: String
            if inlineExtra.isEmpty {
                fullAnswer = combined
            } else if combined.isEmpty {
                fullAnswer = inlineExtra
            } else if combined.contains(inlineExtra) {
                fullAnswer = combined
            } else {
                fullAnswer = combined + "\n\n---\n\n" + inlineExtra
            }

            if !fullAnswer.isEmpty {
                self.popoverState.answerText = fullAnswer
                self.resizePopover(expanded: true)
            }
            let stillActive = isGenerating && self.brainClient.currentTurnId == turnId
            self.popoverState.isStreamingAnswer = stillActive
            if !stillActive && !self.brainClient.errorBannerText.isEmpty && fullAnswer.isEmpty {
                self.popoverState.statusNote = self.brainClient.errorBannerText
            }
        }
        .store(in: &cancellables)
    }

    // MARK: - Pinned Floating Note

    private func pinCurrentAnswer() {
        let note = PinnedNoteItem(
            id: UUID(),
            thumbnail: popoverState.thumbnail,
            question: popoverState.submittedQuestion.isEmpty ? "屏幕圈选问答" : popoverState.submittedQuestion,
            answer: popoverState.answerText,
            artifactFiles: popoverState.artifactFiles
        )
        let origin = popoverPanel?.frame.origin ?? activeSelectionRect.origin
        closePopover()
        openPinnedNote(note, near: origin)
    }

    private func openPinnedNote(_ note: PinnedNoteItem, near origin: CGPoint) {
        let size = NSSize(width: 320, height: 260)
        let rect = NSRect(origin: origin, size: size)
        let noteId = note.id

        let p = PinnedNotePanel(
            contentRect: rect,
            styleMask: [.nonactivatingPanel, .borderless, .resizable],
            backing: .buffered,
            defer: false
        )
        p.title = "桌面便签"
        p.isFloatingPanel = true
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.alphaValue = 0.94
        p.hasShadow = true
        p.appearance = NSAppearance(named: .darkAqua)
        p.minSize = NSSize(width: 240, height: 180)
        p.onCancel = { [weak self] in self?.closePinnedNote(id: noteId) }

        let view = PinnedNoteView(note: note) { [weak self] in
            self?.closePinnedNote(id: noteId)
        }
        let hosting = NSHostingController(rootView: view)
        hosting.sizingOptions = []
        if #available(macOS 13.3, *) {
            hosting.safeAreaRegions = []
        }
        hosting.view.wantsLayer = true
        hosting.view.layer?.backgroundColor = NSColor.clear.cgColor
        hosting.view.frame = NSRect(origin: .zero, size: size)
        p.contentView = hosting.view
        p.contentViewController = hosting

        pinnedPanels[noteId] = p
        p.makeKeyAndOrderFront(nil)
    }

    private func closePinnedNote(id: UUID) {
        pinnedPanels[id]?.orderOut(nil)
        pinnedPanels.removeValue(forKey: id)
    }

    private func topWindowBounds(at appKitPoint: CGPoint, on screen: NSScreen) -> CGRect? {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        let primaryHeight = NSScreen.screens.first?.frame.maxY ?? screen.frame.maxY
        let quartzPt = CGPoint(x: appKitPoint.x, y: primaryHeight - appKitPoint.y)
        let ownPid = Int(ProcessInfo.processInfo.processIdentifier)

        for info in list {
            if let pid = info[kCGWindowOwnerPID as String] as? Int, pid == ownPid { continue }
            if let layer = info[kCGWindowLayer as String] as? Int, layer != 0 { continue }
            guard let boundsDict = info[kCGWindowBounds as String] as? [String: Any],
                  let x = boundsDict["X"] as? CGFloat,
                  let y = boundsDict["Y"] as? CGFloat,
                  let w = boundsDict["Width"] as? CGFloat,
                  let h = boundsDict["Height"] as? CGFloat
            else { continue }
            let quartzRect = CGRect(x: x, y: y, width: w, height: h)
            guard quartzRect.contains(quartzPt) else { continue }
            let appKitRect = CGRect(x: x, y: primaryHeight - y - h, width: w, height: h)
            let clipped = appKitRect.intersection(screen.frame)
            if !clipped.isNull, clipped.width > 20, clipped.height > 20 {
                return clipped
            }
        }
        return nil
    }
}

// MARK: - In-Place Popover & Pinned Note Views

private struct LassoQuickChip: Identifiable {
    let id: String
    let title: String
    let prompt: String
}

struct LassoPopoverView: View {
    @ObservedObject var state: LassoPopoverState
    let onSubmit: (String, String?) -> Void
    let onPin: () -> Void
    let onClose: () -> Void

    private let quickChips: [LassoQuickChip] = [
        LassoQuickChip(
            id: "explain",
            title: "解释一下",
            prompt: "请直接用清晰简洁的文字解释选中截图里的核心内容，直接输出正文，不要放到外部文件卡片中。"
        ),
        LassoQuickChip(
            id: "translate",
            title: "翻译",
            prompt: "请将选中截图中的文字准确翻译（中文译为英文，外文译为中文），直接输出完整译文正文，不要放到外部文件卡片中。"
        ),
        LassoQuickChip(
            id: "debug",
            title: "排查报错",
            prompt: "请直接分析选中截图里的报错原因并给出具体修复步骤，直接输出正文，不要放到外部文件卡片中。"
        ),
    ]

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 16, style: .continuous)
        VStack(alignment: .leading, spacing: 10) {
            headerRow

            if !state.hasAnswerUI {
                quickChipRow
            }

            if state.hasAnswerUI {
                answerSection
            } else if !state.statusNote.isEmpty {
                Text(state.statusNote)
                    .font(.system(size: 11))
                    .foregroundStyle(QMXTheme.danger)
            }

            inputRow
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            ZStack {
                if !QMXTheme.isHeadlessDump {
                    VisualEffectBlur(material: .hudWindow, blendingMode: .behindWindow, state: .active, cornerRadius: 0)
                }
                Color(red: 0.067, green: 0.078, blue: 0.102)
                    .opacity(QMXTheme.isHeadlessDump ? 0.94 : 0.86)
            }
        )
        .clipShape(shape)
        .overlay(
            shape.strokeBorder(Color.white.opacity(0.14), lineWidth: 0.75)
        )
        .environment(\.colorScheme, .dark)
    }

    private var headerRow: some View {
        HStack(spacing: 8) {
            if let thumb = state.thumbnail {
                Image(nsImage: thumb)
                    .resizable()
                    .scaledToFill()
                    .frame(width: 44, height: 30)
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5)
                    )
            }

            VStack(alignment: .leading, spacing: 2) {
                Text(state.submittedQuestion.isEmpty ? "圈选区域" : state.submittedQuestion)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.white.opacity(0.92))
                    .lineLimit(1)
                if state.isUploading {
                    Text("正在准备截图…")
                        .font(.system(size: 10))
                        .foregroundStyle(Color.white.opacity(0.55))
                } else if state.isStreamingAnswer {
                    Text("思考中…")
                        .font(.system(size: 10))
                        .foregroundStyle(Color(red: 0.36, green: 0.88, blue: 1.0))
                }
            }

            Spacer(minLength: 4)

            if !state.answerText.isEmpty {
                Button(action: onPin) {
                    Image(systemName: "pin")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Color.white.opacity(0.85))
                        .frame(width: 24, height: 24)
                        .background(Color.white.opacity(0.10), in: Circle())
                }
                .buttonStyle(.plain)
                .help("钉在桌面")
            }

            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.white.opacity(0.65))
                    .frame(width: 24, height: 24)
                    .background(Color.white.opacity(0.08), in: Circle())
            }
            .buttonStyle(.plain)
            .help("关闭 (Esc)")
        }
    }

    private var quickChipRow: some View {
        HStack(spacing: 6) {
            ForEach(quickChips) { chip in
                Button {
                    onSubmit(chip.prompt, chip.title)
                } label: {
                    Text(chip.title)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Color.white.opacity(0.86))
                        .padding(.horizontal, 9)
                        .padding(.vertical, 4)
                        .background(Color.white.opacity(0.08), in: Capsule())
                        .overlay(Capsule().strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5))
                }
                .buttonStyle(.plain)
                .disabled(state.isUploading)
            }
        }
    }

    private var answerSection: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if !state.answerText.isEmpty {
                    Text(state.answerText)
                        .font(.system(size: 12.5))
                        .foregroundStyle(Color.white.opacity(0.92))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else if !state.statusNote.isEmpty {
                    Text(state.statusNote)
                        .font(.system(size: 12))
                        .foregroundStyle(QMXTheme.danger)
                } else {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("正在识别并分析选区内容…")
                            .font(.system(size: 12))
                            .foregroundStyle(Color.white.opacity(0.62))
                    }
                }

                if !state.artifactFiles.isEmpty {
                    artifactChipList(state.artifactFiles)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxHeight: .infinity)
    }

    private func artifactChipList(_ files: [LassoArtifactRef]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(files) { ref in
                Button {
                    NSWorkspace.shared.open(URL(fileURLWithPath: ref.path))
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "doc.text")
                            .font(.system(size: 10, weight: .medium))
                        Text(ref.title)
                            .font(.system(size: 11, weight: .medium))
                            .lineLimit(1)
                        Image(systemName: "arrow.up.forward")
                            .font(.system(size: 9))
                            .opacity(0.7)
                    }
                    .foregroundStyle(Color(red: 0.36, green: 0.88, blue: 1.0))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5)
                    )
                }
                .buttonStyle(.plain)
                .help("打开 \(ref.path)")
            }
        }
    }

    private var inputRow: some View {
        HStack(spacing: 8) {
            TextField("问问选中区域…", text: $state.questionText)
                .textFieldStyle(.plain)
                .font(.system(size: 12.5))
                .foregroundStyle(Color.white)
                .onSubmit {
                    onSubmit(state.questionText, nil)
                }

            Button {
                onSubmit(state.questionText, nil)
            } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.black)
                    .frame(width: 26, height: 26)
                    .background(
                        Color.white.opacity(
                            state.isUploading || state.questionText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                ? 0.35 : 1.0
                        ),
                        in: Circle()
                    )
            }
            .buttonStyle(.plain)
            .disabled(
                state.isUploading
                    || state.questionText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.white.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5)
        )
    }
}

struct PinnedNoteView: View {
    let note: PinnedNoteItem
    let onClose: () -> Void
    @State private var copied = false

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 16, style: .continuous)
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                if let thumb = note.thumbnail {
                    Image(nsImage: thumb)
                        .resizable()
                        .scaledToFill()
                        .frame(width: 38, height: 26)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                }
                Text(note.question)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.white.opacity(0.92))
                    .lineLimit(1)
                Spacer(minLength: 4)
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(note.answer, forType: .string)
                    copied = true
                } label: {
                    Image(systemName: copied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(copied ? QMXTheme.success : Color.white.opacity(0.75))
                        .frame(width: 22, height: 22)
                        .background(Color.white.opacity(0.08), in: Circle())
                }
                .buttonStyle(.plain)
                .help("复制内容")

                Button(action: onClose) {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.white.opacity(0.70))
                        .frame(width: 22, height: 22)
                        .background(Color.white.opacity(0.08), in: Circle())
                }
                .buttonStyle(.plain)
                .help("关闭便签 (Esc)")
            }

            Rectangle()
                .fill(Color.white.opacity(0.10))
                .frame(height: 0.5)

            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    Text(note.answer)
                        .font(.system(size: 12.5))
                        .foregroundStyle(Color.white.opacity(0.90))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    if !note.artifactFiles.isEmpty {
                        ForEach(note.artifactFiles) { ref in
                            Button {
                                NSWorkspace.shared.open(URL(fileURLWithPath: ref.path))
                            } label: {
                                HStack(spacing: 5) {
                                    Image(systemName: "doc.text")
                                        .font(.system(size: 10, weight: .medium))
                                    Text(ref.title)
                                        .font(.system(size: 11, weight: .medium))
                                        .lineLimit(1)
                                }
                                .foregroundStyle(Color(red: 0.36, green: 0.88, blue: 1.0))
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                            }
                            .buttonStyle(.plain)
                            .help("打开 \(ref.path)")
                        }
                    }
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            ZStack {
                if !QMXTheme.isHeadlessDump {
                    VisualEffectBlur(material: .hudWindow, blendingMode: .behindWindow, state: .active, cornerRadius: 0)
                }
                Color(red: 0.067, green: 0.078, blue: 0.102)
                    .opacity(QMXTheme.isHeadlessDump ? 0.94 : 0.88)
            }
        )
        .clipShape(shape)
        .overlay(shape.strokeBorder(Color.white.opacity(0.18), lineWidth: 0.75))
        .environment(\.colorScheme, .dark)
    }
}

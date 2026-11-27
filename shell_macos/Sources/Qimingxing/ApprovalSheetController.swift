import AppKit
import SwiftUI

struct HoldToApproveState: Equatable {
    static let requiredHoldSeconds: TimeInterval = 0.45
    static let reduceMotionCooldownSeconds: TimeInterval = 0.40

    private(set) var progress: Double = 0
    private(set) var isHolding: Bool = false
    private(set) var didTrigger: Bool = false
    var appearedAt: Date = Date()

    mutating func beginHold() {
        guard !didTrigger else { return }
        isHolding = true
    }

    mutating func cancelHold() {
        isHolding = false
        if !didTrigger {
            progress = 0
        }
    }

    mutating func reset(appearedAt: Date = Date()) {
        progress = 0
        isHolding = false
        didTrigger = false
        self.appearedAt = appearedAt
    }

    mutating func advance(dt: TimeInterval) -> Bool {
        guard isHolding, !didTrigger else { return false }
        let clampedDt = min(max(dt, 0.001), 0.1)
        progress = min(1.0, progress + clampedDt / Self.requiredHoldSeconds)
        if progress >= 1.0 {
            didTrigger = true
            isHolding = false
            return true
        }
        return false
    }

    func canClickInReduceMotion(now: Date = Date()) -> Bool {
        !didTrigger && now.timeIntervalSince(appearedAt) >= Self.reduceMotionCooldownSeconds
    }
}

@MainActor
final class HoldToApproveDriver: ObservableObject {
    @Published private(set) var progress: Double = 0
    @Published private(set) var isHolding: Bool = false
    @Published private(set) var cooldownReady: Bool = false

    private var state = HoldToApproveState()
    private let displayLink = DisplayLinkDriver()
    nonisolated(unsafe) private var cooldownTimer: Timer?
    var onApprove: (() -> Void)?

    func reset() {
        displayLink.stop()
        cooldownTimer?.invalidate()
        cooldownTimer = nil
        state.reset(appearedAt: Date())
        progress = 0
        isHolding = false
        cooldownReady = false

        let timer = Timer(timeInterval: HoldToApproveState.reduceMotionCooldownSeconds, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.cooldownReady = true
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        cooldownTimer = timer
    }

    func beginHold() {
        guard !state.isHolding, !state.didTrigger else { return }
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            if state.canClickInReduceMotion() {
                triggerApprove()
            }
            return
        }
        state.beginHold()
        isHolding = true
        displayLink.start { [weak self] dt in
            self?.tick(dt: dt)
        }
    }

    func endHold() {
        guard state.isHolding || state.progress > 0 else { return }
        displayLink.stop()
        state.cancelHold()
        isHolding = false
        if !state.didTrigger {
            withAnimation(.spring(response: 0.22, dampingFraction: 0.78)) {
                progress = 0
            }
        }
    }

    func triggerInReduceMotion() {
        guard state.canClickInReduceMotion() else { return }
        triggerApprove()
    }

    private func tick(dt: TimeInterval) {
        let completed = state.advance(dt: dt)
        progress = state.progress
        if completed {
            displayLink.stop()
            isHolding = false
            triggerApprove()
        }
    }

    private func triggerApprove() {
        NSHapticFeedbackManager.defaultPerformer.perform(.levelChange, performanceTime: .now)
        let action = onApprove
        onApprove = nil
        action?()
    }

    deinit {
        cooldownTimer?.invalidate()
    }
}

@MainActor
final class ApprovalQueueModel: ObservableObject {
    @Published private(set) var items: [PendingApprovalItem] = []
    @Published var selectedIndex: Int = 0

    var currentItem: PendingApprovalItem? {
        guard items.indices.contains(selectedIndex) else { return items.first }
        return items[selectedIndex]
    }

    func sync(_ next: [PendingApprovalItem]) {
        let currentId = currentItem?.approvalId
        items = next
        if let currentId, let idx = next.firstIndex(where: { $0.approvalId == currentId }) {
            selectedIndex = idx
        } else {
            selectedIndex = min(selectedIndex, max(0, next.count - 1))
        }
    }

    func remove(approvalId: String) {
        items.removeAll { $0.approvalId == approvalId }
        selectedIndex = min(selectedIndex, max(0, items.count - 1))
    }

    func step(by delta: Int) {
        guard !items.isEmpty else { return }
        selectedIndex = (selectedIndex + delta + items.count) % items.count
    }
}

/// Borderless windows cannot become key by default; the approval surface must
/// own the keyboard so typing never leaks into QuickEntry behind it.
private final class ApprovalWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class ApprovalSheetController {    private var window: NSWindow?
    private var windowLease: WindowLease?
    private weak var brainClient: BrainClient?
    private let windowCoordinator: WindowCoordinator?
    private let queueModel = ApprovalQueueModel()
    private let holdDriver = HoldToApproveDriver()
    private var keyMonitor: Any?

    init(brainClient: BrainClient, windowCoordinator: WindowCoordinator? = nil) {
        self.brainClient = brainClient
        self.windowCoordinator = windowCoordinator
    }

    func syncPending(_ items: [PendingApprovalItem]) {
        queueModel.sync(items)
        if items.isEmpty {
            closeAll()
        } else {
            showOrUpdateWindow()
        }
    }

    func closeWindow(approvalId: String) {
        queueModel.remove(approvalId: approvalId)
        if queueModel.items.isEmpty {
            closeAll()
        } else {
            holdDriver.reset()
        }
    }

    private func closeAll() {
        holdDriver.endHold()
        removeKeyMonitor()
        if let lease = windowLease, let windowCoordinator {
            windowCoordinator.dismiss(lease)
        }
        windowLease = nil
        window?.close()
        window = nil
    }

    private func respond(_ item: PendingApprovalItem, approved: Bool, remember: Bool, answerText: String) {
        brainClient?.respondToApproval(
            approvalId: item.approvalId,
            turnId: item.turnId,
            approved: approved,
            rememberForSession: remember,
            answerText: answerText
        )
        closeWindow(approvalId: item.approvalId)
    }

    private func showOrUpdateWindow() {
        if let window {
            window.makeKeyAndOrderFront(nil)
            if windowCoordinator == nil {
                NSApp.activate(ignoringOtherApps: true)
            }
            return
        }

        windowLease = windowCoordinator?.present(
            WindowRequest(role: .approval, activation: .required)
        )
        holdDriver.reset()
        let rootView = ApprovalQueueView(
            model: queueModel,
            holdDriver: holdDriver,
            onRespond: { [weak self] item, approved, remember, answerText in
                self?.respond(item, approved: approved, remember: remember, answerText: answerText)
            }
        )

        let hosting = NSHostingController(rootView: rootView)
        hosting.view.wantsLayer = true
        hosting.view.layer?.backgroundColor = NSColor.clear.cgColor

        let win = ApprovalWindow(contentViewController: hosting)
        win.title = "启明星 · 操作审批"
        win.styleMask = [.borderless]
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = true
        win.level = .floating
        win.isMovableByWindowBackground = true
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        win.setContentSize(NSSize(width: 440, height: 372))
        win.center()
        if windowCoordinator == nil {
            NSApp.activate(ignoringOtherApps: true)
        }
        win.makeKeyAndOrderFront(nil)
        self.window = win
        installKeyMonitor()
    }

    func focusIfVisible() {
        guard let window, window.isVisible, !window.isKeyWindow else { return }
        window.makeKeyAndOrderFront(nil)
    }

    private func installKeyMonitor() {
        removeKeyMonitor()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp, .flagsChanged]) { [weak self] event in
            guard let self, let win = self.window, win.isKeyWindow,
                  let item = self.queueModel.currentItem
            else { return event }

            if event.type == .flagsChanged {
                if !event.modifierFlags.contains(.command), self.holdDriver.isHolding {
                    self.holdDriver.endHold()
                }
                return event
            }

            if event.keyCode == 53, event.type == .keyDown {
                self.respond(item, approved: false, remember: false, answerText: "")
                return nil
            }

            // Never intercept Return in clarification text fields, except swallowing
            // accidental bare Return for high-risk approval modals.
            if !item.isClarification && (event.keyCode == 36 || event.keyCode == 76) {
                let hasCommand = event.modifierFlags.contains(.command)
                if event.type == .keyDown {
                    if hasCommand {
                        if !event.isARepeat {
                            self.holdDriver.beginHold()
                        }
                    }
                    // Swallow both bare Return and Cmd+Return so bare Enter
                    // can never accidentally approve a pending action.
                    return nil
                } else if event.type == .keyUp {
                    self.holdDriver.endHold()
                    return nil
                }
            }

            return event
        }
    }

    private func removeKeyMonitor() {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
    }
}

private struct ApprovalQueueView: View {
    @ObservedObject var model: ApprovalQueueModel
    @ObservedObject var holdDriver: HoldToApproveDriver
    let onRespond: (PendingApprovalItem, Bool, Bool, String) -> Void

    var body: some View {
        if let item = model.currentItem {
            ApprovalModalView(
                item: item,
                queueIndex: model.selectedIndex,
                queueTotal: model.items.count,
                externalHoldDriver: holdDriver,
                onNavigate: { delta in
                    model.step(by: delta)
                    holdDriver.reset()
                },
                onRespond: { approved, remember, answerText in
                    onRespond(item, approved, remember, answerText)
                }
            )
            .id(item.approvalId)
        }
    }
}

struct ApprovalModalView: View {
    let item: PendingApprovalItem
    var queueIndex: Int = 0
    var queueTotal: Int = 1
    var externalHoldDriver: HoldToApproveDriver? = nil
    var onNavigate: ((Int) -> Void)? = nil
    let onRespond: (Bool, Bool, String) -> Void

    @State private var customReply: String = ""
    @State private var rememberChoice: Bool = false
    @StateObject private var fallbackHoldDriver = HoldToApproveDriver()

    private var activeHoldDriver: HoldToApproveDriver {
        externalHoldDriver ?? fallbackHoldDriver
    }

    init(
        item: PendingApprovalItem,
        queueIndex: Int = 0,
        queueTotal: Int = 1,
        externalHoldDriver: HoldToApproveDriver? = nil,
        onNavigate: ((Int) -> Void)? = nil,
        onRespond: @escaping (Bool, Bool, String) -> Void
    ) {
        self.item = item
        self.queueIndex = queueIndex
        self.queueTotal = queueTotal
        self.externalHoldDriver = externalHoldDriver
        self.onNavigate = onNavigate
        self.onRespond = onRespond
    }

    var body: some View {
        ZStack {
            VisualEffectBlur(material: .popover, blendingMode: .behindWindow)

            VStack(alignment: .leading, spacing: 16) {
                header

                Text(item.summary)
                    .font(.system(size: 14))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)

                if item.isClarification {
                    clarificationBody
                } else {
                    detailsCard
                    rememberRow
                    actionButtons
                }
            }
            .padding(20)
        }
        .frame(width: 440)
        .clipShape(RoundedRectangle(cornerRadius: QMXTheme.approvalRadius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.approvalRadius, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
        .onAppear {
            if externalHoldDriver == nil {
                fallbackHoldDriver.reset()
            }
            activeHoldDriver.onApprove = {
                onRespond(true, rememberChoice, "")
            }
        }
        .onChange(of: rememberChoice) { _ in
            activeHoldDriver.onApprove = {
                onRespond(true, rememberChoice, "")
            }
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: item.isClarification ? "questionmark.circle.fill" : "shield.lefthalf.filled")
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(item.isClarification ? QMXTheme.accent : QMXTheme.warning)

            VStack(alignment: .leading, spacing: 3) {
                Text(humanTitle)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                Text(sourceText)
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textSecondary)
            }

            Spacer()

            if queueTotal > 1 {
                HStack(spacing: 4) {
                    Button {
                        onNavigate?(-1)
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 10, weight: .semibold))
                            .frame(width: 18, height: 20)
                    }
                    .buttonStyle(.plain)

                    Text("\(queueIndex + 1) / \(queueTotal)")
                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .monospacedDigit()

                    Button {
                        onNavigate?(1)
                    } label: {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 10, weight: .semibold))
                            .frame(width: 18, height: 20)
                    }
                    .buttonStyle(.plain)
                }
                .foregroundStyle(QMXTheme.textSecondary)
                .padding(.horizontal, 6)
                .frame(height: 22)
                .background(Capsule().fill(QMXTheme.surfaceRaised))
                .overlay(Capsule().strokeBorder(QMXTheme.hairline, lineWidth: 0.5))
            }
        }
    }

    private var detailsCard: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 7) {
                if let rawPath = item.details["preview_image_path"],
                   !rawPath.isEmpty,
                   let image = NSImage(contentsOfFile: rawPath) {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(maxHeight: 120)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                        )
                }
                ForEach(item.details.keys.sorted().filter { $0 != "preview_image_path" }, id: \.self) { key in
                    HStack(alignment: .top, spacing: 10) {
                        Text(humanKey(key))
                            .font(.system(size: 11, weight: .medium, design: .monospaced))
                            .foregroundStyle(QMXTheme.textTertiary)
                            .frame(width: 92, alignment: .leading)
                        Text(displayValue(key, item.details[key] ?? ""))
                            .font(.system(size: 11.5, design: .monospaced))
                            .foregroundStyle(QMXTheme.textPrimary)
                            .textSelection(.enabled)
                        Spacer(minLength: 0)
                    }
                }
            }
            .padding(10)
        }
        .frame(maxHeight: 140)
        .background(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .fill(QMXTheme.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }

    @ViewBuilder
    private var rememberRow: some View {
        if item.allowRemember {
            Toggle("记住本次选择", isOn: $rememberChoice)
                .font(.system(size: 12))
                .toggleStyle(.checkbox)
                .foregroundStyle(QMXTheme.textSecondary)
        }
    }

    private var actionButtons: some View {
        HStack(spacing: 10) {
            ApprovalButton(title: "拒绝 (Esc)", isPrimary: false, haptic: .generic) {
                onRespond(false, false, "")
            }

            Spacer()

            HoldToApproveButton(driver: activeHoldDriver)
        }
    }

    @ViewBuilder
    private var clarificationBody: some View {
        if !item.options.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(item.options, id: \.self) { option in
                    Button {
                        onRespond(true, false, option)
                    } label: {
                        Text(option)
                            .font(.system(size: 13))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 8)
                            .background(
                                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                                    .fill(QMXTheme.surfaceRaised)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                                    .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                            )
                    }
                    .buttonStyle(.plain)
                }
            }
        }

        HStack(spacing: 8) {
            TextField("输入答复", text: $customReply)
                .textFieldStyle(.roundedBorder)

            ApprovalButton(title: "发送", isPrimary: true, haptic: .generic) {
                let answer = customReply.trimmingCharacters(in: .whitespacesAndNewlines)
                if !answer.isEmpty {
                    onRespond(true, false, answer)
                }
            }
            .disabled(customReply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

            ApprovalButton(title: "取消", isPrimary: false, haptic: .generic) {
                onRespond(false, false, "")
            }
        }
    }

    private var humanTitle: String {
        if item.isClarification { return "需要你的确认" }
        switch item.toolName {
        case "macos_notes_create": return "允许写入 Apple 备忘录？"
        case "macos_notes_search": return "允许读取 Apple 备忘录？"
        case "macos_calendar_create": return "允许创建日历日程？"
        case "macos_calendar_list": return "允许读取日历日程？"
        case "macos_reminders_create": return "允许创建提醒事项？"
        case "macos_reminders_list": return "允许读取提醒事项？"
        case "macos_mail_search": return "允许读取邮件？"
        case "macos_mail_draft": return "允许创建邮件草稿？"
        case "macos_browser_tabs": return "允许读取浏览器标签页？"
        case "browser_open": return "允许接入浏览器？"
        case "browser_act": return "允许执行网页高危操作？"
        default: return "允许执行这项操作？"
        }
    }

    private var sourceText: String {
        if item.isClarification { return "启明星需要补充一点信息" }
        if item.toolName.hasPrefix("macos_") { return "来自 macOS 应用联动" }
        if item.toolName.hasPrefix("mcp") { return "来自 MCP 工具" }
        return "来自启明星"
    }

    private func displayValue(_ key: String, _ value: String) -> String {
        if key == "browser_mode" {
            if value == "live" { return "复用主浏览器（live）" }
            if value == "isolated" { return "隔离浏览器（isolated）" }
        }
        return value
    }

    private func humanKey(_ key: String) -> String {
        switch key {
        case "app": return "应用"
        case "folder": return "文件夹"
        case "title": return "标题"
        case "body_preview": return "正文预览"
        case "to": return "收件人"
        case "cc": return "抄送"
        case "subject": return "主题"
        case "start": return "开始"
        case "end": return "结束"
        case "calendar_name": return "日历"
        case "list_name": return "列表"
        case "browser_mode": return "浏览器模式"
        case "allowed_domains": return "授权域名"
        case "target_url": return "目标网址"
        case "target": return "目标操作"
        case "tab_hint": return "标签页"
        case "preview_image_path": return "页面截图"
        case "reason": return "原因"
        case "instruction": return "需要你做什么"
        default: return key.replacingOccurrences(of: "_", with: " ")
        }
    }
}

private struct HoldToApproveButton: View {
    @ObservedObject var driver: HoldToApproveDriver
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pressed = false

    var body: some View {
        let fillFraction = reduceMotion ? (driver.cooldownReady ? 1.0 : 0.35) : driver.progress
        let label = reduceMotion ? "允许 ⌘↩" : "长按允许 ⌘↩"

        ZStack {
            Capsule(style: .continuous)
                .fill(QMXTheme.surfaceRaised)

            GeometryReader { geo in
                Capsule(style: .continuous)
                    .fill(QMXTheme.primaryFill)
                    .frame(width: max(0, geo.size.width * fillFraction))
            }
            .clipShape(Capsule(style: .continuous))

            HStack(spacing: 6) {
                Text(label)
                    .font(.system(size: 13, weight: .medium))
            }
            .foregroundStyle(fillFraction > 0.52 ? QMXTheme.primaryText : QMXTheme.textPrimary)
            .padding(.horizontal, 18)
        }
        .frame(minWidth: 146)
        .frame(height: 36)
        .fixedSize(horizontal: true, vertical: true)
        .overlay(
            Capsule(style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
        .contentShape(Capsule(style: .continuous))
        .scaleEffect(reduceMotion ? 1.0 : (driver.isHolding ? 0.97 : 1.0))
        .animation(.easeOut(duration: 0.12), value: driver.isHolding)
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    guard !pressed else { return }
                    pressed = true
                    if reduceMotion {
                        driver.triggerInReduceMotion()
                    } else {
                        driver.beginHold()
                    }
                }
                .onEnded { _ in
                    pressed = false
                    if !reduceMotion {
                        driver.endHold()
                    }
                }
        )
        .opacity(reduceMotion && !driver.cooldownReady ? 0.65 : 1.0)
        .help("长按 0.45 秒或按住 ⌘↩ 以确认高危操作")
    }
}

private struct ApprovalButton: View {
    let title: String
    let isPrimary: Bool
    let haptic: NSHapticFeedbackManager.FeedbackPattern
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(isPrimary ? QMXTheme.primaryText : QMXTheme.textPrimary)
                .frame(minWidth: 104)
                .padding(.vertical, 9)
                .background(
                    Capsule(style: .continuous)
                        .fill(isPrimary ? QMXTheme.primaryFill : QMXTheme.surfaceRaised)
                )
                .overlay(
                    Capsule(style: .continuous)
                        .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                )
        }
        .buttonStyle(TactileButtonStyle(haptic: haptic))
    }
}

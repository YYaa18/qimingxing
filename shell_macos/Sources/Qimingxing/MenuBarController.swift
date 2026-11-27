import AppKit
import SwiftUI

@MainActor
final class MenuBarController: NSObject {
    private let statusItem: NSStatusItem
    private let popover = NSPopover()
    private let contextMenu = NSMenu()

    private let brainClient: BrainClient
    private let supervisor: ProcessSupervisor
    private let desktopActions: DesktopActions
    private let onOpenQuickEntry: () -> Void
    private let onOpenSettings: () -> Void
    private let onOpenWorkspace: () -> Void
    private let onOpenDropPocket: (() -> Void)?
    private let onSpatialLasso: (() -> Void)?
    private var dropPocketBadgeCount: Int = 0

    init(
        brainClient: BrainClient,
        supervisor: ProcessSupervisor,
        desktopActions: DesktopActions,
        onOpenQuickEntry: @escaping () -> Void,
        onOpenSettings: @escaping () -> Void,
        onOpenWorkspace: @escaping () -> Void,
        onOpenDropPocket: (() -> Void)? = nil,
        onSpatialLasso: (() -> Void)? = nil
    ) {
        self.brainClient = brainClient
        self.supervisor = supervisor
        self.desktopActions = desktopActions
        self.onOpenQuickEntry = onOpenQuickEntry
        self.onOpenSettings = onOpenSettings
        self.onOpenWorkspace = onOpenWorkspace
        self.onOpenDropPocket = onOpenDropPocket
        self.onSpatialLasso = onSpatialLasso
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()

        configureButton()
        configurePopover()
        configureContextMenu()
    }

    func setDropPocketBadgeCount(_ count: Int) {
        dropPocketBadgeCount = max(0, count)
        statusItem.button?.image = Self.makeStarImage(size: 16, hasBadge: dropPocketBadgeCount > 0)
        statusItem.button?.toolTip = dropPocketBadgeCount > 0
            ? "启明星 Qimingxing（投喂口袋中有 \(dropPocketBadgeCount) 项素材）"
            : "启明星 Qimingxing"
    }

    private func configureButton() {
        guard let btn = statusItem.button else { return }
        btn.image = Self.makeStarImage(size: 16, hasBadge: dropPocketBadgeCount > 0)
        btn.toolTip = "启明星 Qimingxing"
        btn.target = self
        btn.action = #selector(handleStatusButtonClick(_:))
        btn.sendAction(on: [.leftMouseUp, .rightMouseUp])
    }

    private static func makeStarImage(size: CGFloat, hasBadge: Bool = false) -> NSImage {
        let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            let center = CGPoint(x: rect.midX, y: rect.midY)
            let outer = min(rect.width, rect.height) / 2 - 1
            let inner = outer * 0.26
            let path = NSBezierPath()
            for i in 0..<8 {
                let angle = -Double.pi / 2 + Double(i) * Double.pi / 4
                let radius = i.isMultiple(of: 2) ? outer : inner
                let point = CGPoint(
                    x: center.x + CGFloat(cos(angle)) * radius,
                    y: center.y + CGFloat(sin(angle)) * radius
                )
                if i == 0 { path.move(to: point) } else { path.line(to: point) }
            }
            path.close()
            NSColor.black.setFill()
            path.fill()

            if hasBadge {
                let badgeRect = NSRect(x: rect.maxX - 4.5, y: rect.maxY - 4.5, width: 4, height: 4)
                NSColor.black.setFill()
                NSBezierPath(ovalIn: badgeRect).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }

    private func configurePopover() {
        let root = MenuBarPopoverView(
            brainClient: brainClient,
            supervisor: supervisor,
            desktopActions: desktopActions,
            onOpenQuickEntry: { [weak self] in
                self?.popover.performClose(nil)
                self?.onOpenQuickEntry()
            },
            onOpenDropPocket: { [weak self] in
                self?.popover.performClose(nil)
                self?.onOpenDropPocket?()
            },
            onOpenSettings: { [weak self] in
                self?.popover.performClose(nil)
                self?.onOpenSettings()
            },
            onOpenWorkspace: { [weak self] in
                self?.popover.performClose(nil)
                self?.onOpenWorkspace()
            },
            onQuit: { NSApp.terminate(nil) }
        )
        popover.contentViewController = NSHostingController(rootView: root)
        popover.behavior = .transient
        updatePopoverSize()
    }

    private func updatePopoverSize() {
        let taskRows = min(2, brainClient.backgroundTasks.filter(\.isActive).count)
        let height: CGFloat = 392 + (taskRows == 0 ? 0 : 22 + CGFloat(taskRows) * 44)
        popover.contentSize = NSSize(width: 280, height: height)
    }

    private func configureContextMenu() {
        let items: [(String, Selector)] = [
            ("打开对话 (⌥Space)", #selector(menuQuickEntry)),
            ("投喂口袋 (⌥D)", #selector(menuDropPocket)),
            ("屏幕圈选问答 (⌥⇧Space)", #selector(menuSpatialLasso)),
            ("打开工作区", #selector(menuWorkspace)),
            ("设置与 API Key", #selector(menuSettings)),
            ("打开日志目录", #selector(menuOpenLogs)),
            ("紧急停止", #selector(menuEmergencyStop)),
            ("退出启明星", #selector(menuQuit))
        ]
        for (title, sel) in items {
            let mi = NSMenuItem(title: title, action: sel, keyEquivalent: "")
            mi.target = self
            contextMenu.addItem(mi)
        }
    }

    @objc private func handleStatusButtonClick(_ sender: Any?) {
        guard let event = NSApp.currentEvent else {
            togglePopover()
            return
        }
        if event.type == .rightMouseUp {
            statusItem.menu = contextMenu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            togglePopover()
        }
    }

    private func togglePopover() {
        guard let btn = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            updatePopoverSize()
            popover.show(relativeTo: btn.bounds, of: btn, preferredEdge: .minY)
            Task { [weak self] in
                guard let self else { return }
                await brainClient.refreshTasks()
                updatePopoverSize()
            }
        }
    }

    @objc private func menuQuickEntry() { onOpenQuickEntry() }
    @objc private func menuDropPocket() { onOpenDropPocket?() }
    @objc private func menuSpatialLasso() { onSpatialLasso?() }
    @objc private func menuSettings() { onOpenSettings() }
    @objc private func menuWorkspace() { onOpenWorkspace() }
    @objc private func menuOpenLogs() { desktopActions.openLogsDirectory() }
    @objc private func menuEmergencyStop() { brainClient.cancelActiveTurn(reason: "menu_stop") }
    @objc private func menuQuit() { NSApp.terminate(nil) }
}

struct MenuBarPopoverView: View {
    @ObservedObject var brainClient: BrainClient
    let supervisor: ProcessSupervisor
    let desktopActions: DesktopActions
    let onOpenQuickEntry: () -> Void
    var onOpenDropPocket: (() -> Void)? = nil
    let onOpenSettings: () -> Void
    let onOpenWorkspace: () -> Void
    let onQuit: () -> Void

    var body: some View {
        ZStack {
            VisualEffectBlur(material: .popover, blendingMode: .behindWindow)

            VStack(alignment: .leading, spacing: 6) {
                MenuActionRow(
                    icon: "bubble.left.and.bubble.right",
                    title: "呼出对话",
                    keycap: "⌥Space",
                    action: onOpenQuickEntry
                )
                if let onOpenDropPocket {
                    MenuActionRow(
                        icon: "tray.and.arrow.down",
                        title: "投喂口袋",
                        keycap: "⌥D",
                        action: onOpenDropPocket
                    )
                }
                MenuActionRow(
                    icon: "gearshape",
                    title: "设置与 API Key",
                    keycap: "⌘,",
                    action: onOpenSettings
                )
                MenuActionRow(
                    icon: "square.grid.2x2",
                    title: "打开工作区",
                    keycap: "",
                    action: onOpenWorkspace
                )

                if !activeTasks.isEmpty {
                    Divider().opacity(0.5)
                    Text("后台任务")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .padding(.horizontal, 8)
                        .padding(.top, 2)

                    ForEach(activeTasks.prefix(2)) { task in
                        TaskRow(task: task) {
                            Task { await brainClient.cancelBackgroundTask(taskId: task.taskId) }
                        }
                    }
                }

                Divider().opacity(0.5)

                MenuActionRow(
                    icon: "eye",
                    title: "隐私模式",
                    keycap: "",
                    trailing: AnyView(
                        Menu {
                            Button("关闭") { brainClient.setPrivacyMode("off") }
                            Button("仅窗口") { brainClient.setPrivacyMode("meta_only") }
                            Button("按需看屏") { brainClient.setPrivacyMode("full_on_demand") }
                        } label: {
                            HStack(spacing: 3) {
                                Text(privacyShortLabel(brainClient.privacyMode))
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
                    ),
                    action: {}
                )

                ToggleRow(
                    icon: "speaker.wave.2",
                    title: "状态提示音",
                    isOn: Binding(
                        get: { brainClient.earconsEnabled },
                        set: { brainClient.setUIStatePatch(["earcons": $0]) }
                    )
                )
                ToggleRow(
                    icon: "speaker.wave.2.fill",
                    title: "朗读回答",
                    isOn: Binding(
                        get: { brainClient.ttsEnabled },
                        set: { brainClient.setTTSEnabled($0) }
                    )
                )
                ToggleRow(
                    icon: "music.note",
                    title: "背景氛围音",
                    isOn: Binding(
                        get: { brainClient.ambientEnabled },
                        set: { brainClient.setUIStatePatch(["ambient": $0]) }
                    )
                )

                Divider().opacity(0.5)

                MenuActionRow(
                    icon: "power",
                    title: "退出启明星",
                    keycap: "⌘Q",
                    action: onQuit
                )
            }
            .padding(10)
        }
        .frame(width: 280)
        .clipShape(RoundedRectangle(cornerRadius: QMXTheme.popoverRadius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.popoverRadius, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }

    private var activeTasks: [BackgroundTaskItem] {
        brainClient.backgroundTasks.filter(\.isActive)
    }


    private func privacyShortLabel(_ mode: String) -> String {
        switch mode {
        case "off": return "关闭"
        case "meta_only": return "仅窗口"
        default: return "按需看屏"
        }
    }
}

private struct MenuActionRow: View {
    let icon: String
    let title: String
    let keycap: String
    var trailing: AnyView? = nil
    let action: () -> Void
    @State private var isHovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 9) {
                Image(systemName: icon)
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .frame(width: 18, alignment: .center)
                Text(title)
                    .font(.system(size: 13))
                    .foregroundStyle(QMXTheme.textPrimary)
                Spacer()
                if let trailing {
                    trailing
                } else if !keycap.isEmpty {
                    KeyCapBadge(keycap)
                }
            }
            .padding(.horizontal, 8)
            .frame(height: 32)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(isHovering ? QMXTheme.surfaceRaised : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { isHovering = $0 }
    }
}

private struct ToggleRow: View {
    let icon: String
    let title: String
    @Binding var isOn: Bool

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .regular))
                .foregroundStyle(QMXTheme.textSecondary)
                .frame(width: 18, alignment: .center)
            Text(title)
                .font(.system(size: 13))
                .foregroundStyle(QMXTheme.textPrimary)
            Spacer()
            Toggle("", isOn: $isOn)
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.mini)
        }
        .padding(.horizontal, 8)
        .frame(height: 32)
    }
}

private struct TaskRow: View {
    let task: BackgroundTaskItem
    let onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(task.title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                Spacer()
                Text("\(task.progressPct)%")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .monospacedDigit()
                Button(action: onCancel) {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(QMXTheme.textTertiary)
                }
                .buttonStyle(.plain)
                .help("取消任务")
            }

            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(QMXTheme.surfaceMuted)
                        .frame(height: 2)
                    Capsule()
                        .fill(QMXTheme.accent)
                        .frame(width: max(2, geo.size.width * CGFloat(task.progressPct) / 100.0), height: 2)
                }
            }
            .frame(height: 2)

            Text(task.currentStep.isEmpty ? task.status : task.currentStep)
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textTertiary)
                .lineLimit(1)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
    }
}

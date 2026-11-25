import AppKit
import Luminare
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

    init(
        brainClient: BrainClient,
        supervisor: ProcessSupervisor,
        desktopActions: DesktopActions,
        onOpenQuickEntry: @escaping () -> Void,
        onOpenSettings: @escaping () -> Void
    ) {
        self.brainClient = brainClient
        self.supervisor = supervisor
        self.desktopActions = desktopActions
        self.onOpenQuickEntry = onOpenQuickEntry
        self.onOpenSettings = onOpenSettings
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()

        configureButton()
        configurePopover()
        configureContextMenu()
    }

    private func configureButton() {
        guard let btn = statusItem.button else { return }
        if let img = NSImage(systemSymbolName: "sparkles", accessibilityDescription: "启明星") {
            img.isTemplate = true
            btn.image = img
        } else {
            btn.title = "✦"
        }
        btn.toolTip = "启明星 Qimingxing"
        btn.target = self
        btn.action = #selector(handleStatusButtonClick(_:))
        btn.sendAction(on: [.leftMouseUp, .rightMouseUp])
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
            onOpenSettings: { [weak self] in
                self?.popover.performClose(nil)
                self?.onOpenSettings()
            },
            onQuit: { NSApp.terminate(nil) }
        )

        popover.contentViewController = NSHostingController(rootView: root)
        popover.behavior = .transient
        popover.contentSize = NSSize(width: 356, height: 470)
    }

    private func configureContextMenu() {
        let items: [(String, Selector)] = [
            ("打开 Quick Entry (⌥Space)", #selector(menuQuickEntry)),
            ("设置中心 (⌥K)", #selector(menuSettings)),
            ("打开日志目录", #selector(menuOpenLogs)),
            ("紧急停止当前任务 (⌥Esc)", #selector(menuEmergencyStop)),
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
            Task { await brainClient.refreshTasks() }
            popover.show(relativeTo: btn.bounds, of: btn, preferredEdge: .minY)
        }
    }

    @objc private func menuQuickEntry() { onOpenQuickEntry() }
    @objc private func menuSettings() { onOpenSettings() }
    @objc private func menuOpenLogs() { desktopActions.openLogsDirectory() }
    @objc private func menuEmergencyStop() { brainClient.cancelActiveTurn(reason: "menu_stop") }
    @objc private func menuQuit() { NSApp.terminate(nil) }
}

struct MenuBarPopoverView: View {
    @ObservedObject var brainClient: BrainClient
    @ObservedObject var supervisor: ProcessSupervisor
    let desktopActions: DesktopActions
    let onOpenQuickEntry: () -> Void
    let onOpenSettings: () -> Void
    let onQuit: () -> Void

    var body: some View {
        ZStack {
            VisualEffectBlur(material: .popover, blendingMode: .behindWindow)

            VStack(alignment: .leading, spacing: 12) {
                // Header Status Card
                HStack(spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(QMXTheme.accent.opacity(0.16))
                            .frame(width: 36, height: 36)
                        Image(systemName: "sparkles")
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(QMXTheme.accent)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text("启明星")
                                .font(.system(size: 14, weight: .semibold))
                            Text("v\(supervisor.appVersion)")
                                .font(.system(size: 10, weight: .medium, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.primary.opacity(0.08)))
                        }
                        HStack(spacing: 4) {
                            PulsingDotView(
                                color: statusColor,
                                isPulsing: brainClient.isGenerating || brainClient.phase == .starting
                            )
                            Text(statusLabel)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }

                    Spacer()

                    Button(action: onOpenQuickEntry) {
                        HStack(spacing: 5) {
                            Text("对话")
                            KeyCapBadge("⌥Space", onAccent: true)
                        }
                    }
                    .buttonStyle(.qmxProminent)
                }

                // Model & Senses Info Card
                LuminareSection {
                    LuminareCompose("当前模型") {
                        Text(activeModelLabel)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                    }

                    LuminareCompose("感知隐私") {
                        LuminarePicker(
                            compactElements: ["off", "meta_only", "full_on_demand"],
                            selection: Binding(
                                get: { brainClient.privacyMode },
                                set: { brainClient.setPrivacyMode($0) }
                            )
                        ) { mode in
                            Text(privacyShortLabel(mode))
                                .font(.system(size: 11, weight: .medium))
                                .padding(.vertical, 4)
                        }
                        .frame(width: 192, height: 28)
                    }

                    LuminareToggle(
                        "提示音效 (Earcons)",
                        isOn: Binding(
                            get: { brainClient.earconsEnabled },
                            set: { brainClient.setUIStatePatch(["earcons": $0]) }
                        )
                    )
                }
                .luminareTint(overridingWith: QMXTheme.accent)

                // Background Tasks Section
                LuminareSection {
                    if brainClient.backgroundTasks.isEmpty {
                        LuminareCompose("后台长任务") {
                            Text("暂无运行中任务")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        VStack(spacing: 0) {
                            ForEach(brainClient.backgroundTasks.prefix(4)) { task in
                                VStack(alignment: .leading, spacing: 5) {
                                    HStack {
                                        Text(task.title)
                                            .font(.system(size: 12, weight: .medium))
                                            .lineLimit(1)
                                        Spacer()
                                        Text("\(task.progressPct)%")
                                            .font(.system(size: 11, design: .monospaced))
                                            .foregroundStyle(task.isActive ? QMXTheme.accent : .secondary)
                                    }

                                    ProgressView(value: Double(task.progressPct), total: 100.0)
                                        .tint(task.isActive ? QMXTheme.accent : QMXTheme.success)

                                    HStack {
                                        Text(task.currentStep.isEmpty ? task.status : task.currentStep)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                        Spacer()
                                        if task.isActive {
                                            Button("取消") {
                                                Task { await brainClient.cancelBackgroundTask(taskId: task.taskId) }
                                            }
                                            .font(.caption2)
                                            .buttonStyle(.borderless)
                                            .foregroundStyle(QMXTheme.danger)
                                        } else if !task.artifactPath.isEmpty {
                                            Button("预览报告") {
                                                _ = desktopActions.quickLook(task.artifactPath)
                                            }
                                            .font(.caption2)
                                            .buttonStyle(.borderless)
                                            .foregroundStyle(QMXTheme.accent)
                                        }
                                    }
                                }
                                .padding(.horizontal, 10)
                                .padding(.vertical, 8)
                            }
                        }
                    }
                } header: {
                    HStack {
                        Text("后台任务 (\(brainClient.activeBgTaskCount) 运行中)")
                            .font(.caption.weight(.semibold))
                        Spacer()
                        Button {
                            Task { await brainClient.refreshTasks() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                                .font(.caption)
                        }
                        .buttonStyle(.borderless)
                    }
                }
                .luminareTint(overridingWith: QMXTheme.accent)

                Spacer(minLength: 0)

                // Compact Footer Control Bar
                HStack(spacing: 8) {
                    Button(action: onOpenSettings) {
                        HStack(spacing: 4) {
                            Image(systemName: "gearshape")
                            Text("设置")
                            KeyCapBadge("⌥K")
                        }
                    }
                    .buttonStyle(.qmxSecondary)

                    Button {
                        brainClient.cancelActiveTurn(reason: "popover_stop")
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "stop.circle.fill")
                            Text("急停")
                        }
                    }
                    .buttonStyle(.qmxDestructive)

                    Spacer()

                    Button(action: onQuit) {
                        Text("退出")
                    }
                    .buttonStyle(.qmxSecondary)
                }
            }
            .padding(14)
        }
        .frame(width: 356)
    }

    private var activeModelLabel: String {
        if let p = brainClient.profiles.first(where: { $0.name == brainClient.activeProfile }) {
            return p.label.isEmpty ? p.name : p.label
        }
        return brainClient.activeProfile.isEmpty ? "未就绪" : brainClient.activeProfile
    }

    private var statusColor: Color {
        switch brainClient.phase {
        case .connected: return QMXTheme.success
        case .starting: return QMXTheme.warning
        case .offline, .error: return QMXTheme.danger
        }
    }

    private var statusLabel: String {
        switch brainClient.phase {
        case .connected:
            return brainClient.sensesConnected ? "已连接 · 感官在线" : "已连接 Brain"
        case .starting:
            return "正在启动引擎…"
        case .offline:
            return "已离线（自动重连中）"
        case .error:
            return "运行异常"
        }
    }

    private func privacyShortLabel(_ mode: String) -> String {
        switch mode {
        case "off": return "关闭"
        case "meta_only": return "仅窗口"
        default: return "看屏"
        }
    }
}

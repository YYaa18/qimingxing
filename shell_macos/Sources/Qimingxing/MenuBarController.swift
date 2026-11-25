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
        popover.contentSize = NSSize(width: 320, height: 420)
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

private struct MenuBarPopoverView: View {
    @ObservedObject var brainClient: BrainClient
    @ObservedObject var supervisor: ProcessSupervisor
    let desktopActions: DesktopActions
    let onOpenQuickEntry: () -> Void
    let onOpenSettings: () -> Void
    let onQuit: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("启明星 Qimingxing")
                        .font(.headline)
                    Text("v\(supervisor.appVersion) · \(statusLabel)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Button("对话") { onOpenQuickEntry() }
                    .buttonStyle(.borderedProminent)
            }

            Divider()

            HStack {
                Text("当前模型")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Spacer()
                Text(brainClient.activeProfile.isEmpty ? "未就绪" : brainClient.activeProfile)
                    .font(.caption.bold())
                    .lineLimit(1)
            }

            HStack {
                Text("感知隐私")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Spacer()
                Picker("", selection: Binding(
                    get: { brainClient.privacyMode },
                    set: { brainClient.setPrivacyMode($0) }
                )) {
                    Text("关闭").tag("off")
                    Text("仅窗口").tag("meta_only")
                    Text("按需看屏").tag("full_on_demand")
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .frame(width: 190)
            }

            Toggle("提示音效 (Earcons)", isOn: Binding(
                get: { brainClient.earconsEnabled },
                set: { brainClient.setUIStatePatch(["earcons": $0]) }
            ))
            .font(.caption)

            Divider()

            HStack {
                Text("后台长任务 (\(brainClient.activeBgTaskCount) 运行中)")
                    .font(.caption.bold())
                Spacer()
                Button("刷新") {
                    Task { await brainClient.refreshTasks() }
                }
                .font(.caption)
                .buttonStyle(.borderless)
            }

            if brainClient.backgroundTasks.isEmpty {
                Text("暂无后台任务")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .padding(.vertical, 6)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(brainClient.backgroundTasks.prefix(5)) { task in
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(task.title)
                                        .font(.caption.bold())
                                        .lineLimit(1)
                                    Spacer()
                                    Text("\(task.progressPct)%")
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                }
                                Text("\(task.status) · \(task.currentStep)")
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                                    .lineLimit(1)
                                HStack {
                                    if task.isActive {
                                        Button("取消") {
                                            Task { await brainClient.cancelBackgroundTask(taskId: task.taskId) }
                                        }
                                        .font(.caption2)
                                    } else if !task.artifactPath.isEmpty {
                                        Button("预览报告") {
                                            _ = desktopActions.quickLook(task.artifactPath)
                                        }
                                        .font(.caption2)
                                    }
                                }
                            }
                            .padding(6)
                            .background(Color(NSColor.controlBackgroundColor))
                            .cornerRadius(6)
                        }
                    }
                }
                .frame(maxHeight: 150)
            }

            Divider()

            HStack {
                Button("设置…") { onOpenSettings() }
                Button("急停") { brainClient.cancelActiveTurn(reason: "popover_stop") }
                    .tint(.red)
                Spacer()
                Button("退出") { onQuit() }
            }
        }
        .padding(14)
        .frame(width: 320)
    }

    private var statusLabel: String {
        switch brainClient.phase {
        case .connected: return "已连接"
        case .starting: return "启动中"
        case .offline: return "已离线"
        case .error: return "异常"
        }
    }
}

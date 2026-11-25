import AppKit
import Luminare
import SwiftUI

@MainActor
enum UIPreviewDumper {
    static func dumpAll(to outputDir: URL) {
        try? FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        NSApp.appearance = NSAppearance(named: .darkAqua)

        let supervisor = ProcessSupervisor()
        let desktop = DesktopActions()
        let audio = AudioStreamPlayer()
        let client = BrainClient(
            httpBase: "http://127.0.0.1:18790",
            wsURLString: "ws://127.0.0.1:18790/ws",
            appVersion: "1.0.0",
            desktopActions: desktop,
            audioPlayer: audio
        )
        client.phase = .connected
        client.sensesConnected = true
        client.activeProfile = "deepseek_flash"
        client.profiles = [
            ProfileMeta(name: "deepseek_flash", label: "DeepSeek Flash · 视觉", provider: "openai_compatible", model: "deepseek-flash", supportsVision: true),
            ProfileMeta(name: "fast_deepseek", label: "DeepSeek Chat", provider: "openai_compatible", model: "deepseek-chat", supportsVision: false),
        ]
        client.backgroundTasks = [
            BackgroundTaskItem(
                taskId: "bg_demo_1",
                title: "整理浏览器标签并生成周报",
                status: "running",
                progressPct: 65,
                currentStep: "正在汇总 12 个技术文档标签页…",
                resultSummary: "",
                artifactPath: "",
                errorMessage: ""
            )
        ]

        // 1. Quick Entry (empty hero state)
        let inputState1 = QuickEntryInputState()
        let qeEmpty = QuickEntryRootView(
            brainClient: client,
            desktopActions: desktop,
            inputState: inputState1,
            onClose: {}
        )
        render(qeEmpty, size: CGSize(width: 700, height: 540), to: outputDir.appendingPathComponent("quick_entry_empty.png"))

        // 2. Quick Entry (conversation + markdown code block + capsule)
        client.messages = [
            ChatEntry(id: "u1", turnId: "t1", isUser: true, meta: "我", text: "帮我写一个 Python 快速检查端口占用的脚本"),
            ChatEntry(
                id: "a1",
                turnId: "t1",
                isUser: false,
                meta: "启明星 · DeepSeek Flash",
                text: "可以使用标准库 `socket` 在 10 行内完成：\n\n```python\nimport socket\nwith socket.socket() as s:\n    print(s.connect_ex(('127.0.0.1', 18790)) == 0)\n```\n\n如果返回 `True` 说明端口已被监听。"
            )
        ]
        client.artifactCapsules = [
            ArtifactCapsuleItem(id: "c1", turnId: "t1", title: "port_check_report.md", path: "/tmp/port_check_report.md", mediaType: "text", actions: ["quick_look", "copy_file", "reveal"])
        ]
        let inputState2 = QuickEntryInputState()
        let qeChat = QuickEntryRootView(
            brainClient: client,
            desktopActions: desktop,
            inputState: inputState2,
            onClose: {}
        )
        render(qeChat, size: CGSize(width: 700, height: 540), to: outputDir.appendingPathComponent("quick_entry_chat.png"))

        // 3. Menu Bar Popover
        let popover = MenuBarPopoverView(
            brainClient: client,
            supervisor: supervisor,
            desktopActions: desktop,
            onOpenQuickEntry: {},
            onOpenSettings: {},
            onQuit: {}
        )
        render(popover, size: CGSize(width: 356, height: 470), to: outputDir.appendingPathComponent("menubar_popover.png"))

        // 4. Settings Window (General & Routing)
        let store = SettingsViewStore()
        store.providers = [
            ProviderViewModel(name: "deepseek", label: "DeepSeek", protocolName: "openai_compatible", baseURL: "https://api.deepseek.com/v1", authRequired: true, hasKey: true, keyMasked: "sk-a****9f21", models: ["deepseek-chat", "deepseek-flash"]),
            ProviderViewModel(name: "local_ollama", label: "本地 Ollama", protocolName: "openai_compatible", baseURL: "http://127.0.0.1:11434/v1", authRequired: false, hasKey: false, keyMasked: "", models: ["qwen2.5:7b"]),
        ]
        store.selectedSection = .general
        let settingsGen = ZStack {
            Color(red: 0.10, green: 0.12, blue: 0.17)
            LuminareView {
                SettingsRootView(store: store, brainClient: client, supervisor: supervisor, desktopActions: desktop)
            }
            .environment(\.luminareTintColor, QMXTheme.accent)
            .environment(\.luminareTitleBarHeight, 44)
        }
        render(settingsGen, size: CGSize(width: 830, height: 560), to: outputDir.appendingPathComponent("settings_general.png"))

        store.selectedSection = .routing
        let settingsRouting = ZStack {
            Color(red: 0.10, green: 0.12, blue: 0.17)
            LuminareView {
                SettingsRootView(store: store, brainClient: client, supervisor: supervisor, desktopActions: desktop)
            }
            .environment(\.luminareTintColor, QMXTheme.accent)
            .environment(\.luminareTitleBarHeight, 44)
        }
        render(settingsRouting, size: CGSize(width: 830, height: 560), to: outputDir.appendingPathComponent("settings_routing.png"))

        // 5. Approval Modal
        let aprItem = PendingApprovalItem(
            approvalId: "apr_demo",
            turnId: "t_demo",
            toolName: "macos_notes_create",
            riskLevel: "confirm",
            summary: "新建 macOS 备忘录「今日技术标签总结」",
            details: ["app": "Notes", "folder": "工作", "title": "今日技术标签总结", "body_preview": "1. Luminare macOS 设计系统...\n2. Swift 6 并发模型..."],
            options: [],
            isClarification: false,
            allowRemember: true,
            timeoutSec: 48
        )
        let aprView = ApprovalModalView(item: aprItem) { _, _, _ in }
        render(aprView, size: CGSize(width: 480, height: 390), to: outputDir.appendingPathComponent("approval_modal.png"))
    }

    private static func render<V: View>(_ view: V, size: CGSize, to url: URL) {
        let hosting = NSHostingView(rootView: view.frame(width: size.width, height: size.height))
        hosting.frame = NSRect(origin: .zero, size: size)
        hosting.wantsLayer = true
        hosting.appearance = NSAppearance(named: .darkAqua)
        hosting.layoutSubtreeIfNeeded()

        guard let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else { return }
        hosting.cacheDisplay(in: hosting.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: url)
    }
}

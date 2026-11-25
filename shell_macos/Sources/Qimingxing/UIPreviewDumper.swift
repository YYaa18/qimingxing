import AppKit
import SwiftUI

@MainActor
enum UIPreviewDumper {
    static func dumpAll(to outputDir: URL, appearance: NSAppearance.Name = .darkAqua) {
        try? FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        NSApp.appearance = NSAppearance(named: appearance)

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
            ProfileMeta(name: "deepseek_flash", label: "DeepSeek Flash", provider: "openai_compatible", model: "deepseek-flash", supportsVision: true),
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

        // Workspace preview data shared with the wing and the full window.
        let now = Date()
        let workspaceItems = [
            WorkspaceItem(
                id: "turn_demo",
                kind: .answer,
                title: "帮我写一个 Python 快速检查端口占用的脚本",
                prompt: "帮我写一个 Python 快速检查端口占用的脚本",
                body: "可以使用标准库 `socket` 在 10 行内完成：\n\n```python\nimport socket\nwith socket.socket() as s:\n    print(s.connect_ex(('127.0.0.1', 18790)) == 0)\n```\n\n如果返回 `True` 说明端口已被监听。",
                status: "completed",
                progress: 0,
                path: "",
                createdAt: now.addingTimeInterval(-3600),
                updatedAt: now.addingTimeInterval(-3500)
            ),
            WorkspaceItem(
                id: "turn_demo2",
                kind: .answer,
                title: "再解释一下 connect_ex 的返回值",
                prompt: "再解释一下 connect_ex 的返回值",
                body: "`connect_ex` 返回的是错误码而不是布尔值：`0` 表示连接成功，其余值是 `errno`。\n\n所以在脚本里判断 `== 0` 才是「端口已监听」。",
                status: "completed",
                progress: 0,
                path: "",
                createdAt: now.addingTimeInterval(-120),
                updatedAt: now.addingTimeInterval(-100)
            ),
            WorkspaceItem(
                id: "task_demo",
                kind: .task,
                title: "整理浏览器标签并生成周报",
                prompt: "",
                body: "正在汇总 12 个技术文档标签页…",
                status: "running",
                progress: 65,
                path: "",
                createdAt: now.addingTimeInterval(-2400),
                updatedAt: now.addingTimeInterval(-120)
            ),
            WorkspaceItem(
                id: "artifact_demo",
                kind: .artifact,
                title: "port_check_report.md",
                prompt: "",
                body: "/tmp/port_check_report.md",
                status: "ready",
                progress: 100,
                path: "/tmp/port_check_report.md",
                createdAt: now.addingTimeInterval(-3300),
                updatedAt: now.addingTimeInterval(-3300)
            )
        ]
        let workspaceStore = WorkspaceStore(previewItems: workspaceItems)

        // 1. Quick Entry (idle Grand Spotlight)
        let inputState1 = QuickEntryInputState()
        inputState1.isExpanded = false
        let qeEmpty = QuickEntryRootView(
            brainClient: client,
            desktopActions: desktop,
            workspaceStore: workspaceStore,
            inputState: inputState1,
            onClose: {}
        )
        render(qeEmpty, size: CGSize(width: 760, height: 112), to: outputDir.appendingPathComponent("quick_entry_empty.png"))

        // 2. Quick Entry (expanded two-column Spotlight)
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
        let inputState2 = QuickEntryInputState()
        inputState2.isExpanded = true
        client.isGenerating = true
        var measuredChatHeight: CGFloat = 0
        func makeChat() -> QuickEntryRootView {
            QuickEntryRootView(
                brainClient: client,
                desktopActions: desktop,
                workspaceStore: workspaceStore,
                inputState: inputState2,
                onClose: {},
                onDesiredHeightChange: { measuredChatHeight = $0 }
            )
        }
        // First pass measures the natural content height; second pass renders
        // at exactly that height, matching the running panel.
        render(makeChat(), size: CGSize(width: 760, height: 520), to: outputDir.appendingPathComponent("quick_entry_chat.png"))
        let chatHeight = measuredChatHeight > 0 ? measuredChatHeight : 430
        render(makeChat(), size: CGSize(width: 760, height: chatHeight), to: outputDir.appendingPathComponent("quick_entry_chat.png"))

        // 2c. Quick Entry with the attached workspace wing
        let inputStateWing = QuickEntryInputState()
        inputStateWing.isExpanded = true
        inputStateWing.isWorkspaceWingOpen = true
        let qeWing = QuickEntryRootView(
            brainClient: client,
            desktopActions: desktop,
            workspaceStore: workspaceStore,
            inputState: inputStateWing,
            onClose: {}
        )
        render(qeWing, size: CGSize(width: 1140, height: 430), to: outputDir.appendingPathComponent("quick_entry_wing.png"))

        // 2b. Quick Entry (thinking: compact, no answer yet)
        client.messages = []
        client.isGenerating = true
        let inputStateThinking = QuickEntryInputState()
        inputStateThinking.isExpanded = true
        let qeThinking = QuickEntryRootView(
            brainClient: client,
            desktopActions: desktop,
            workspaceStore: workspaceStore,
            inputState: inputStateThinking,
            onClose: {}
        )
        render(qeThinking, size: CGSize(width: 760, height: 236), to: outputDir.appendingPathComponent("quick_entry_thinking.png"))

        // 3. Menu Bar Popover
        let popover = MenuBarPopoverView(
            brainClient: client,
            supervisor: supervisor,
            desktopActions: desktop,
            onOpenQuickEntry: {},
            onOpenSettings: {},
            onOpenWorkspace: {},
            onQuit: {}
        )
        render(popover, size: CGSize(width: 280, height: 438), to: outputDir.appendingPathComponent("menubar_popover.png"))

        // 4. Settings Window (General & Routing)
        let store = SettingsViewStore()
        store.providers = [
            ProviderViewModel(name: "deepseek", label: "DeepSeek", protocolName: "openai_compatible", baseURL: "https://api.deepseek.com/v1", authRequired: true, hasKey: true, keyMasked: "sk-a****9f21", models: ["deepseek-chat", "deepseek-flash"]),
            ProviderViewModel(name: "local_ollama", label: "本地 Ollama", protocolName: "openai_compatible", baseURL: "http://127.0.0.1:11434/v1", authRequired: false, hasKey: false, keyMasked: "", models: ["qwen2.5:7b"]),
        ]
        store.selectedSection = .general
        let settingsGen = SettingsRootView(
            store: store,
            brainClient: client,
            supervisor: supervisor,
            desktopActions: desktop,
            onClose: {}
        )
        render(settingsGen, size: CGSize(width: 860, height: 580), to: outputDir.appendingPathComponent("settings_general.png"))

        store.selectedSection = .voicePrivacy
        let settingsVoice = SettingsRootView(
            store: store,
            brainClient: client,
            supervisor: supervisor,
            desktopActions: desktop,
            onClose: {}
        )
        render(settingsVoice, size: CGSize(width: 860, height: 580), to: outputDir.appendingPathComponent("settings_voice.png"))

        store.selectedSection = .routing
        let settingsRouting = SettingsRootView(
            store: store,
            brainClient: client,
            supervisor: supervisor,
            desktopActions: desktop,
            onClose: {}
        )
        render(settingsRouting, size: CGSize(width: 860, height: 580), to: outputDir.appendingPathComponent("settings_routing.png"))

        // 5. Approval Modal
        let aprItem = PendingApprovalItem(
            approvalId: "apr_demo",
            turnId: "t_demo",
            toolName: "macos_notes_create",
            riskLevel: "confirm",
            summary: "新建备忘录「今日技术标签总结」",
            details: ["app": "Notes", "folder": "工作", "title": "今日技术标签总结", "body_preview": "1. 原生 macOS 设计系统\n2. Swift 6 并发模型"],
            options: [],
            isClarification: false,
            allowRemember: true,
            timeoutSec: 48
        )
        let aprView = ApprovalModalView(item: aprItem) { _, _, _ in }
        render(aprView, size: CGSize(width: 440, height: 360), to: outputDir.appendingPathComponent("approval_modal.png"))

        // 6. Workspace
        let workspaceView = WorkspaceRootView(store: workspaceStore, brainClient: client)
        render(workspaceView, size: CGSize(width: 920, height: 620), to: outputDir.appendingPathComponent("workspace.png"))
    }

    private static func render<V: View>(_ view: V, size: CGSize, to url: URL) {
        let appearance = NSApp.appearance ?? NSAppearance(named: .darkAqua)!

        let hosting = NSHostingView(
            rootView: view.frame(width: size.width, height: size.height, alignment: .top)
        )
        hosting.frame = NSRect(origin: .zero, size: size)
        hosting.wantsLayer = true
        hosting.appearance = appearance

        appearance.performAsCurrentDrawingAppearance {
            hosting.layoutSubtreeIfNeeded()
            guard let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else { return }
            hosting.cacheDisplay(in: hosting.bounds, to: rep)
            guard let png = rep.representation(using: .png, properties: [:]) else { return }
            try? png.write(to: url)
        }
    }
}

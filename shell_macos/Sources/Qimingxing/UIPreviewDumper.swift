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
            appVersion: "1.0.3",
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
        let repoPath = FileManager.default.currentDirectoryPath
        let now = Date()
        let demoSources = [
            SourceItem(title: "中央气象台", url: "https://www.weather.com.cn/", domain: "weather.com.cn"),
            SourceItem(title: "Google 官方博客", url: "https://blog.google/technology/ai/", domain: "blog.google"),
            SourceItem(title: "DeepSeek API Docs", url: "https://api-docs.deepseek.com/", domain: "api-docs.deepseek.com"),
        ]
        let demoTable = MediaItem(
            mediaId: "media_demo_table",
            kind: "file",
            title: "端口检查结果",
            mimeType: "text/markdown",
            sourceTitle: "本轮回答",
            provider: "qimingxing",
            fileExt: "md-table",
            summaryBadge: "2 行 × 2 列",
            previewColumns: ["检查项", "结果"],
            previewRows: [["18790", "已监听"], ["返回值", "0"]]
        )
        let demoMedia = [
            demoTable,
            MediaItem(
                mediaId: "media_demo_image",
                kind: "image",
                title: "live-line-preview.png",
                localPath: repoPath + "/body_godot/assets/sprites/happy.png",
                mimeType: "image/png",
                width: 256, height: 256, sizeBytes: 49_207,
                sourceURL: "https://www.weather.com.cn/",
                sourceTitle: "中央气象台",
                license: "示例许可",
                provider: "tavily"
            ),
            MediaItem(
                mediaId: "media_demo_audio",
                kind: "audio",
                title: "周报摘要.m4a",
                localPath: "/tmp/qmx_demo_weekly.m4a",
                mimeType: "audio/mp4",
                durationSec: 42, sizeBytes: 680_000
            ),
            MediaItem(
                mediaId: "media_demo_video",
                kind: "video",
                title: "排障回放.mp4",
                localPath: "/tmp/qmx_demo_debug.mp4",
                mimeType: "video/mp4",
                width: 1920, height: 1080, durationSec: 18, sizeBytes: 4_200_000
            ),
        ]
        let workspaceItems = [
            WorkspaceItem(
                id: "turn_demo",
                kind: .answer,
                title: "帮我写一个 Python 快速检查端口占用的脚本",
                prompt: "帮我写一个 Python 快速检查端口占用的脚本",
                body: "可以使用标准库 `socket` 在 10 行内完成：\n\n```python\nimport socket\nwith socket.socket() as s:\n    print(s.connect_ex(('127.0.0.1', 18790)) == 0)\n```\n\n如果返回 `True` 说明端口已被监听。\n\n**端口检查结果**\n\n| 检查项 | 结果 |\n| --- | --- |\n| 18790 | 已监听 |\n| 返回值 | 0 |",
                status: "completed",
                progress: 0,
                path: "",
                createdAt: now.addingTimeInterval(-3600),
                updatedAt: now.addingTimeInterval(-3500),
                sources: demoSources,
                media: demoMedia
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
        render(qeEmpty, size: CGSize(width: QuickEntryLayout.chatWidth, height: QuickEntryLayout.idleHeight), to: outputDir.appendingPathComponent("quick_entry_empty.png"))

        // 2. Quick Entry (expanded two-column Spotlight)
        client.messages = [
            ChatEntry(id: "u1", turnId: "t1", isUser: true, meta: "我", text: "帮我写一个 Python 快速检查端口占用的脚本"),
            ChatEntry(
                id: "a1",
                turnId: "t1",
                isUser: false,
                meta: "启明星 · DeepSeek Flash",
                text: "可以使用标准库 `socket` 在 10 行内完成：\n\n```python\nimport socket\nwith socket.socket() as s:\n    print(s.connect_ex(('127.0.0.1', 18790)) == 0)\n```\n\n如果返回 `True` 说明端口已被监听。\n\n**端口检查结果**\n\n| 检查项 | 结果 |\n| --- | --- |\n| 18790 | 已监听 |\n| 返回值 | 0 |",
                sources: demoSources,
                media: demoMedia
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
        render(makeChat(), size: CGSize(width: QuickEntryLayout.chatWidth, height: 520), to: outputDir.appendingPathComponent("quick_entry_chat.png"))
        let chatHeight = measuredChatHeight > 0 ? measuredChatHeight : 430
        render(makeChat(), size: CGSize(width: QuickEntryLayout.chatWidth, height: chatHeight), to: outputDir.appendingPathComponent("quick_entry_chat.png"))

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
        render(qeWing, size: CGSize(width: QuickEntryLayout.chatWidth + QuickEntryLayout.wingWidth, height: 430), to: outputDir.appendingPathComponent("quick_entry_wing.png"))

        // 2d. Attached media wing with a virtual markdown table
        MediaViewerModel.shared.load(
            items: [demoTable],
            selectedID: demoTable.mediaId,
            title: demoTable.title,
            contextLabel: "本轮回答"
        )
        let inputStateMediaWing = QuickEntryInputState()
        inputStateMediaWing.isExpanded = true
        inputStateMediaWing.isWorkspaceWingOpen = true
        inputStateMediaWing.auxiliaryTab = .media
        let qeMediaWing = QuickEntryRootView(
            brainClient: client,
            desktopActions: desktop,
            workspaceStore: workspaceStore,
            inputState: inputStateMediaWing,
            onClose: {}
        )
        render(
            qeMediaWing,
            size: CGSize(width: QuickEntryLayout.chatWidth + QuickEntryLayout.wingWidth, height: 430),
            to: outputDir.appendingPathComponent("quick_entry_wing_media.png")
        )

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
        render(qeThinking, size: CGSize(width: QuickEntryLayout.chatWidth, height: QuickEntryLayout.thinkingHeight), to: outputDir.appendingPathComponent("quick_entry_thinking.png"))

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
            ProviderViewModel(
                name: "deepseek",
                label: "DeepSeek",
                protocolName: "openai_compatible",
                baseURL: "https://api.deepseek.com/v1",
                authRequired: true,
                hasKey: true,
                keyMasked: "sk-a****9f21",
                models: ["deepseek-flash", "deepseek-v4-pro"],
                modelCapabilities: [
                    "deepseek-flash": ModelCapabilityViewModel(contextWindow: 1048576, maxOutputTokens: 393216, supportsTools: true, supportsVision: false, supportsCache: true, supportsReasoning: true, source: "provider_api"),
                    "deepseek-v4-pro": ModelCapabilityViewModel(contextWindow: 1048576, maxOutputTokens: 393216, supportsTools: true, supportsVision: false, supportsCache: true, supportsReasoning: true, source: "provider_api"),
                ]
            ),
            ProviderViewModel(
                name: "gemini",
                label: "Google Gemini",
                protocolName: "openai_compatible",
                baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
                authRequired: true,
                hasKey: false,
                keyMasked: "",
                models: ["gemini-4-carbon", "gemini-4-neon", "gemini-4-hydrogen", "gemini-4-argon"],
                modelCapabilities: [
                    "gemini-4-carbon": ModelCapabilityViewModel(contextWindow: 1048576, maxOutputTokens: 65536, supportsTools: true, supportsVision: true, supportsCache: true, supportsReasoning: true, source: "catalog"),
                    "gemini-4-neon": ModelCapabilityViewModel(contextWindow: 1048576, maxOutputTokens: 65536, supportsTools: true, supportsVision: true, supportsCache: true, supportsReasoning: true, source: "catalog"),
                    "gemini-4-hydrogen": ModelCapabilityViewModel(contextWindow: 1048576, maxOutputTokens: 65536, supportsTools: true, supportsVision: true, supportsCache: true, supportsReasoning: true, source: "catalog"),
                    "gemini-4-argon": ModelCapabilityViewModel(contextWindow: 1048576, maxOutputTokens: 65536, supportsTools: true, supportsVision: true, supportsCache: true, supportsReasoning: true, source: "catalog"),
                ],
                draftApiKey: "AIzaSyPreviewKey"
            ),
            ProviderViewModel(
                name: "openai",
                label: "OpenAI",
                protocolName: "openai_compatible",
                baseURL: "https://api.openai.com/v1",
                authRequired: true,
                hasKey: false,
                keyMasked: "",
                models: ["gpt-4o", "gpt-4o-mini"],
                modelCapabilities: [
                    "gpt-4o": ModelCapabilityViewModel(contextWindow: 128000, maxOutputTokens: 16384, supportsTools: true, supportsVision: true, supportsCache: true, source: "catalog"),
                    "gpt-4o-mini": ModelCapabilityViewModel(contextWindow: 128000, maxOutputTokens: 16384, supportsTools: true, supportsVision: true, supportsCache: true, source: "catalog"),
                ]
            ),
            ProviderViewModel(
                name: "custom_ab12cd34",
                label: "我的中转站",
                protocolName: "openai_compatible",
                baseURL: "https://relay.example.com/v1",
                authRequired: true,
                hasKey: false,
                keyMasked: "",
                models: ["gpt-4o"],
                modelCapabilities: [
                    "gpt-4o": ModelCapabilityViewModel(contextWindow: 32768, maxOutputTokens: 4096, supportsTools: true, supportsVision: false, source: "manual")
                ]
            ),
        ]
        store.chatSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-flash")
        store.visionSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-flash")
        store.fastSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-flash")
        store.asrSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-asr")
        store.ttsSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-tts", voice: "stella")
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
        render(settingsRouting, size: CGSize(width: 860, height: 1400), to: outputDir.appendingPathComponent("settings_routing_full.png"))
        render(settingsRouting, size: CGSize(width: 760, height: 760), to: outputDir.appendingPathComponent("settings_routing_narrow.png"))
        let customSheet = CustomProviderSheet { _ in }
        render(customSheet, size: CGSize(width: 480, height: 520), to: outputDir.appendingPathComponent("custom_provider_sheet.png"))

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

        // 5b. Detached media viewer with a virtual markdown table
        let tableView = MediaViewerContent(model: MediaViewerModel.shared, mode: .detached)
        render(tableView, size: CGSize(width: 860, height: 620), to: outputDir.appendingPathComponent("media_viewer_table.png"))

        // 6. Workspace
        let workspaceView = WorkspaceRootView(store: workspaceStore, brainClient: client)
        render(workspaceView, size: CGSize(width: 920, height: 620), to: outputDir.appendingPathComponent("workspace.png"))
        render(workspaceView, size: CGSize(width: 920, height: 900), to: outputDir.appendingPathComponent("workspace_rich.png"))

        // 7. Real NSPanel lifecycle verification (idle -> Q&A -> New Chat)
        verifyRealPanelLifecycle(
            outputDir: outputDir,
            desktop: desktop,
            audio: audio
        )
    }

    private static func verifyRealPanelLifecycle(
        outputDir: URL,
        desktop: DesktopActions,
        audio: AudioStreamPlayer
    ) {
        let panelClient = BrainClient(
            httpBase: "http://127.0.0.1:18790",
            wsURLString: "ws://127.0.0.1:18790/ws",
            appVersion: "1.0.3",
            desktopActions: desktop,
            audioPlayer: audio
        )
        panelClient.phase = .connected
        panelClient.activeProfile = "deepseek_flash"
        panelClient.profiles = [
            ProfileMeta(name: "deepseek_flash", label: "DeepSeek Flash", provider: "openai_compatible", model: "deepseek-flash", supportsVision: true),
        ]
        let panelWorkspaceStore = WorkspaceStore(brainClient: panelClient)
        let controller = QuickEntryController(
            brainClient: panelClient,
            desktopActions: desktop,
            workspaceStore: panelWorkspaceStore
        )

        func pump(_ seconds: TimeInterval) {
            RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        }

        func capturePanel(_ window: NSWindow, to url: URL) {
            guard let root = window.contentView?.superview ?? window.contentView else { return }
            let appearance = NSApp.appearance ?? NSAppearance(named: .darkAqua)!
            appearance.performAsCurrentDrawingAppearance {
                root.layoutSubtreeIfNeeded()
                let bounds = root.bounds
                guard let rep = root.bitmapImageRepForCachingDisplay(in: bounds) else { return }
                root.cacheDisplay(in: bounds, to: rep)
                guard let png = rep.representation(using: .png, properties: [:]) else { return }
                try? png.write(to: url)
            }
        }

        func assertZeroSafeArea(_ panel: NSWindow, phase: String) {
            let insets = panel.contentView?.safeAreaInsets ?? NSEdgeInsets()
            if insets.top != 0 || insets.left != 0 || insets.bottom != 0 || insets.right != 0 {
                fatalError("PANEL_VERIFY FAIL (\(phase)): non-zero safeAreaInsets=\(insets)")
            }
        }

        controller.show()
        pump(0.25)
        guard let panel = controller.debugPanel else {
            fatalError("PANEL_VERIFY FAIL: panel was not created")
        }
        let idleFrame = panel.frame
        assertZeroSafeArea(panel, phase: "initial_idle")
        if abs(idleFrame.width - QuickEntryLayout.chatWidth) > 0.5 ||
            abs(idleFrame.height - QuickEntryLayout.idleHeight) > 0.5 {
            fatalError("PANEL_VERIFY FAIL (initial_idle): unexpected frame \(idleFrame)")
        }
        if !panel.canBecomeKey {
            fatalError("PANEL_VERIFY FAIL (initial_idle): panel.canBecomeKey is false")
        }
        capturePanel(panel, to: outputDir.appendingPathComponent("panel_1_initial_idle.png"))

        // Thinking phase
        panelClient.isGenerating = true
        panelClient.messages = [
            ChatEntry(id: "pv_u1", turnId: "pv_t1", isUser: true, meta: "我", text: "帮我详细说明一下工作区与实时流式面板的作用")
        ]
        pump(0.25)
        let thinkingFrame = panel.frame
        assertZeroSafeArea(panel, phase: "thinking")
        if abs(thinkingFrame.height - QuickEntryLayout.thinkingHeight) > 0.5 {
            fatalError("PANEL_VERIFY FAIL (thinking): expected height \(QuickEntryLayout.thinkingHeight), got \(thinkingFrame.height)")
        }

        // Streaming mid phase (~5 lines)
        let streamMidText = (1...5).map { "第 \($0) 条：工作区会自动归档每一轮问答的上下文与产物，方便你随时回看与检索。" }.joined(separator: "\n")
        panelClient.messages.append(
            ChatEntry(
                id: "pv_a1",
                turnId: "pv_t1",
                isUser: false,
                meta: "启明星 · DeepSeek Flash",
                text: streamMidText,
                isStreaming: true
            )
        )
        pump(0.25)
        let streamMidFrame = panel.frame
        assertZeroSafeArea(panel, phase: "streaming_mid")
        if streamMidFrame.height <= 260 {
            fatalError("PANEL_VERIFY FAIL (streaming_mid): expected height > 260 during streaming, got \(streamMidFrame.height)")
        }
        capturePanel(panel, to: outputDir.appendingPathComponent("panel_2a_streaming_mid.png"))

        // Streaming long phase (~12 lines)
        let streamLongText = (1...12).map { "第 \($0) 条：流式输出过程中，窗口高度会随已渲染的内容行数平滑地自适应增高，而不会先静止再突然跳变。" }.joined(separator: "\n")
        if let idx = panelClient.messages.lastIndex(where: { !$0.isUser && $0.turnId == "pv_t1" }) {
            panelClient.messages[idx].text = streamLongText
        }
        pump(0.25)
        let streamLongFrame = panel.frame
        assertZeroSafeArea(panel, phase: "streaming_long")
        if streamLongFrame.height <= streamMidFrame.height + 60 {
            fatalError("PANEL_VERIFY FAIL (streaming_long): expected progressive growth > \(streamMidFrame.height + 60), got \(streamLongFrame.height)")
        }

        // Streaming finished -> should not abruptly jump
        panelClient.isGenerating = false
        if let idx = panelClient.messages.lastIndex(where: { !$0.isUser && $0.turnId == "pv_t1" }) {
            panelClient.messages[idx].isStreaming = false
        }
        pump(0.25)
        let finalFrame = panel.frame
        assertZeroSafeArea(panel, phase: "after_qa")
        if abs(finalFrame.height - streamLongFrame.height) >= 2.0 {
            fatalError("PANEL_VERIFY FAIL (after_qa): unexpected jump from \(streamLongFrame.height) to \(finalFrame.height)")
        }
        capturePanel(panel, to: outputDir.appendingPathComponent("panel_2_after_qa.png"))

        panelClient.clearHistory()
        pump(0.25)
        let collapsedFrame = panel.frame
        assertZeroSafeArea(panel, phase: "after_new_chat")
        if controller.debugInputState.isExpanded {
            fatalError("PANEL_VERIFY FAIL (after_new_chat): isExpanded stayed true")
        }
        if abs(collapsedFrame.width - QuickEntryLayout.chatWidth) > 0.5 ||
            abs(collapsedFrame.height - QuickEntryLayout.idleHeight) > 0.5 {
            fatalError("PANEL_VERIFY FAIL (after_new_chat): unexpected frame \(collapsedFrame)")
        }
        capturePanel(panel, to: outputDir.appendingPathComponent("panel_3_after_new_chat.png"))

        let safeArea = panel.contentView?.safeAreaInsets ?? NSEdgeInsets()
        print("PANEL_VERIFY PASS idle=\(idleFrame.size) thinking=\(thinkingFrame.size) streamMid=\(streamMidFrame.size) streamLong=\(streamLongFrame.size) final=\(finalFrame.size) after_new_chat=\(collapsedFrame.size) safeArea=(\(safeArea.top),\(safeArea.left),\(safeArea.bottom),\(safeArea.right)) isExpanded=\(controller.debugInputState.isExpanded) canBecomeKey=\(panel.canBecomeKey)")
        controller.hideAndRestoreFocus()
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

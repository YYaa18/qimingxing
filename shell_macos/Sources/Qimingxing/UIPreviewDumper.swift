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
            appVersion: "1.0.4",
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
        render(
            qeThinking,
            size: CGSize(
                width: QuickEntryLayout.chatWidth,
                height: QuickEntryLayout.compactThinkingHeight(
                    promptHeight: 0,
                    attachmentBarHeight: 0,
                    inputExtraHeight: 0
                )
            ),
            to: outputDir.appendingPathComponent("quick_entry_thinking.png")
        )

        // 2c. Quick Entry (Phase 3 Ghost Write captured selection capsule)
        client.isGenerating = false
        let inputStateGhost = QuickEntryInputState()
        let sampleLines = (1...18).map { "line \($0)" }.joined(separator: "\n")
        inputStateGhost.capturedSelection = CapturedSelectionContext.make(
            text: sampleLines,
            appName: "Xcode",
            bundleId: "com.apple.dt.Xcode"
        )
        let qeGhost = QuickEntryRootView(
            brainClient: client,
            desktopActions: desktop,
            workspaceStore: workspaceStore,
            inputState: inputStateGhost,
            onClose: {}
        )
        render(
            qeGhost,
            size: CGSize(width: QuickEntryLayout.chatWidth, height: QuickEntryLayout.idleHeight + 34),
            to: outputDir.appendingPathComponent("quick_entry_ghost_selection.png")
        )

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
        let aprView = ApprovalModalView(
            item: aprItem,
            queueIndex: 0,
            queueTotal: 2
        ) { _, _, _ in }
        render(aprView, size: CGSize(width: 440, height: 372), to: outputDir.appendingPathComponent("approval_modal.png"))

        // 5a. Drop Shelf (Scheme C: 3 states)
        let pocketController = DropPocketController(brainClient: client)
        let shelfEmpty = EdgeShelfRootView(controller: pocketController, onClose: {})
        render(
            shelfEmpty,
            size: ScreenEdgeResolver.targetSize(for: .idleEar, itemCount: 0),
            to: outputDir.appendingPathComponent("drop_pocket_empty.png")
        )

        let expandedController = DropPocketController(brainClient: client)
        expandedController.configureForPreview(edge: .right, phase: .dragExpanded(hoveredSlot: .summary), items: [])
        render(
            EdgeShelfRootView(controller: expandedController, onClose: {}),
            size: ScreenEdgeResolver.targetSize(for: .dragExpanded(hoveredSlot: .summary), itemCount: 0),
            to: outputDir.appendingPathComponent("drop_pocket_expanded.png")
        )

        let stashedController = DropPocketController(brainClient: client)
        let sampleImage = NSImage(contentsOfFile: repoPath + "/body_godot/assets/sprites/happy.png")
        let stashedDemoItems = [
            DropPocketItem(
                id: UUID(),
                filename: "happy.png",
                kind: "image",
                isUploading: false,
                attachment: StagedAttachment(artifactId: "a1", filename: "happy.png", kind: "image"),
                thumbnail: sampleImage
            ),
            DropPocketItem(
                id: UUID(),
                filename: "架构方案.pdf",
                kind: "pdf",
                isUploading: false,
                attachment: StagedAttachment(artifactId: "a2", filename: "架构方案.pdf", kind: "pdf"),
                thumbnail: NSWorkspace.shared.icon(forFile: "/tmp/sample.pdf")
            ),
            DropPocketItem(
                id: UUID(),
                filename: "接口调试日志片段",
                kind: "text",
                isUploading: false,
                textSnippet: "curl -s http://127.0.0.1:18790/health"
            ),
            DropPocketItem(
                id: UUID(),
                filename: "extra.md",
                kind: "file",
                isUploading: false,
                textSnippet: "more"
            ),
        ]
        stashedController.configureForPreview(edge: .right, phase: .stashedStack, items: stashedDemoItems)
        render(
            EdgeShelfRootView(controller: stashedController, onClose: {}),
            size: ScreenEdgeResolver.targetSize(for: .stashedStack, edge: .right, itemCount: stashedDemoItems.count),
            to: outputDir.appendingPathComponent("drop_pocket_stashed.png")
        )

        let topEmptyController = DropPocketController(brainClient: client)
        topEmptyController.configureForPreview(edge: .top, phase: .idleEar, items: [])
        render(
            EdgeShelfRootView(controller: topEmptyController, onClose: {}),
            size: ScreenEdgeResolver.targetSize(for: .idleEar, edge: .top, itemCount: 0),
            to: outputDir.appendingPathComponent("drop_pocket_top_empty.png")
        )

        let topStashedController = DropPocketController(brainClient: client)
        topStashedController.configureForPreview(edge: .top, phase: .stashedStack, items: stashedDemoItems)
        render(
            EdgeShelfRootView(controller: topStashedController, onClose: {}),
            size: ScreenEdgeResolver.targetSize(for: .stashedStack, edge: .top, itemCount: stashedDemoItems.count),
            to: outputDir.appendingPathComponent("drop_pocket_top_stashed.png")
        )

        // 5b. Phase 2: Walkie-Talkie Capsule, Spatial Lasso Popover & Pinned Note
        let walkieController = WalkieTalkieVoiceController()
        walkieController.configureForPreview(transcript: "帮我检查一下 18790 端口现在是否正常", level: 0.72)
        render(
            WalkieTalkieCapsuleView(controller: walkieController, onCancel: {}, onDone: {}),
            size: CGSize(width: 360, height: 58),
            to: outputDir.appendingPathComponent("walkie_talkie_capsule.png")
        )

        let lassoState = LassoPopoverState()
        lassoState.thumbnail = sampleImage
        lassoState.submittedQuestion = "这是什么报错？怎么修"
        lassoState.answerText = "这是 Swift 6 跨隔离域传递非 Sendable 值引发的编译错误。可以在回调中先提取 `CGImage` 或基础标量字段后再切换回 `@MainActor`。"
        render(
            LassoPopoverView(state: lassoState, onSubmit: { _, _ in }, onPin: {}, onClose: {}),
            size: CGSize(width: SpatialLassoGeometry.popoverWidth, height: SpatialLassoGeometry.answeredHeight),
            to: outputDir.appendingPathComponent("lasso_popover.png")
        )

        let pinnedDemo = PinnedNoteItem(
            id: UUID(),
            thumbnail: sampleImage,
            question: "端口排查要点",
            answer: "1. 使用 lsof -i :18790 确认监听进程\n2. 若旧实例残留，先 pkill 后重新启动"
        )
        render(
            PinnedNoteView(note: pinnedDemo, onClose: {}),
            size: CGSize(width: 320, height: 260),
            to: outputDir.appendingPathComponent("pinned_note.png")
        )

        // 5c. Detached media viewer with a virtual markdown table
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
            appVersion: "1.0.4",
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

        // Thinking phase (compact height based on prompt + 28pt status bar, not fixed 248pt)
        panelClient.isGenerating = true
        panelClient.messages = [
            ChatEntry(id: "pv_u1", turnId: "pv_t1", isUser: true, meta: "我", text: "帮我详细说明一下工作区与实时流式面板的作用")
        ]
        pump(0.25)
        let thinkingFrame = panel.frame
        assertZeroSafeArea(panel, phase: "thinking")
        if thinkingFrame.height <= QuickEntryLayout.idleHeight || thinkingFrame.height >= 248 {
            fatalError("PANEL_VERIFY FAIL (thinking): expected compact height in (120, 248), got \(thinkingFrame.height)")
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
        if streamMidFrame.height <= thinkingFrame.height {
            fatalError("PANEL_VERIFY FAIL (streaming_mid): expected height > thinking (\(thinkingFrame.height)), got \(streamMidFrame.height)")
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

@MainActor
enum StreamingEngineVerifier {
    private static func check(_ condition: Bool, _ message: String) {
        if !condition {
            fatalError("STREAMING_VERIFY FAIL: \(message)")
        }
    }

    static func runAll() {
        verifySanitizer()
        verifyBlockParserAndTables()
        verifyAdaptiveRevealer()
        verifyMonotonicHeightEngine()
        verifyMediaTableDedupe()
        print("STREAMING_VERIFY PASS")
    }

    private static func verifySanitizer() {
        check(
            StreamingMarkdownSanitizer.sanitizeInline("关键是 **高度锁定") == "关键是 **高度锁定**",
            "unclosed bold should be auto-closed"
        )
        check(
            StreamingMarkdownSanitizer.sanitizeInline("关键是 **") == "关键是 ",
            "dangling ** without content should be stripped"
        )
        check(
            StreamingMarkdownSanitizer.sanitizeInline("执行 `lsof -i") == "执行 `lsof -i`",
            "unclosed inline code should be auto-closed"
        )
        check(
            StreamingMarkdownSanitizer.sanitizeInline("执行 `") == "执行 ",
            "dangling backtick should be stripped"
        )
        check(
            StreamingMarkdownSanitizer.sanitizeInline("详见 [官方手册](https://example.com/do") == "详见 官方手册",
            "unclosed link should display readable label"
        )
        check(
            StreamingMarkdownSanitizer.sanitizeInline("图 ![预览](https://example.com/a.p") == "图 ",
            "unclosed image should be hidden"
        )
        check(
            StreamingMarkdownSanitizer.sanitizeInline("价格为 \\*100") == "价格为 \\*100",
            "escaped asterisk should not trigger closing asterisk"
        )
        check(
            StreamingMarkdownSanitizer.sanitizeInline("* 列表项") == "* 列表项",
            "list bullet should not trigger closing asterisk"
        )
    }

    private static func verifyBlockParserAndTables() {
        // 1. Settled vs activeTail split
        let split = StreamingBlockParser.parse(
            "第一段已闭合。\n\n第二段正在 **生成",
            turnId: "t1",
            isFinished: false
        )
        check(split.settled.count == 1, "expected 1 settled block, got \(split.settled.count)")
        check(split.settled.first?.kind == .paragraph("第一段已闭合。"), "settled paragraph mismatch")
        check(split.activeTail?.kind == .paragraph("第二段正在 **生成"), "activeTail mismatch")

        // 2. Unclosed fenced code block
        let unclosedCode = StreamingBlockParser.parse(
            "示例：\n\n```swift\nlet x = 1",
            turnId: "t1",
            isFinished: false
        )
        check(unclosedCode.settled.count == 1, "prose before code fence should settle")
        if case .codeBlock(let lang, let code, let isClosed)? = unclosedCode.activeTail?.kind {
            check(lang == "swift" && code == "let x = 1" && !isClosed, "unclosed code block fields mismatch")
        } else {
            check(false, "expected unclosed codeBlock as activeTail")
        }

        // 3. Wide table (>3 cols) pre-placeholder at header + separator prefix
        let wideHeaderOnly = StreamingBlockParser.parse("| A | B | C | D |\n|---", turnId: "t2", isFinished: false)
        check(wideHeaderOnly.hasAnyTable, "wide table should trigger immediately on |---")
        if case .tableNotice(_, _, let cols, let rows, let streaming, _)? = wideHeaderOnly.activeTail?.kind {
            check(cols == 4 && rows == 0 && streaming, "wide table notice mismatch: cols=\(cols) rows=\(rows)")
        } else {
            check(false, "expected wide tableNotice on activeTail")
        }

        // 4. Active pipe header buffered without flashing raw pipes
        let partialPipeHeader = StreamingBlockParser.parse("| 端口 | 进程", turnId: "t2", isFinished: false)
        check(partialPipeHeader.settled.isEmpty && partialPipeHeader.activeTail == nil, "partial pipe header should be buffered")

        // 5. False positive pipe header reverts to prose
        let falsePositive = StreamingBlockParser.parse("A | B\n普通正文继续", turnId: "t2", isFinished: false)
        if case .paragraph(let body)? = falsePositive.activeTail?.kind {
            check(body.contains("A | B"), "false-positive pipe line should revert to prose")
        } else {
            check(false, "expected reverted prose paragraph")
        }

        // 6. Mini table (<=3 cols, <=6 rows) renders inline
        let miniTableMd = """
        **端口检查结果**

        | 端口 | 进程 | 状态 |
        | --- | --- | --- |
        | 18790 | brain | 监听中 |
        | 18791 | senses | 监听中 |
        """
        let miniParsed = StreamingBlockParser.parse(miniTableMd, turnId: "t3", isFinished: true)
        check(miniParsed.activeTail == nil && miniParsed.settled.count == 2, "finished mini table should settle")
        if case .inlineTable(_, let title, let headers, let rows, let total, let streaming, let truncated, _)? = miniParsed.settled.last?.kind {
            check(title == "端口检查结果" && headers.count == 3 && rows.count == 2 && total == 2 && !streaming && !truncated, "mini inline table mismatch")
        } else {
            check(false, "expected inlineTable block")
        }

        // 7. Narrow table (>6 rows) locks to 6 rows and marks truncated
        let longNarrowRows = (1...9).map { "| \($0) | item-\($0) |" }.joined(separator: "\n")
        let longNarrowMd = "| ID | Name |\n| --- | --- |\n" + longNarrowRows
        let longNarrowParsed = StreamingBlockParser.parse(longNarrowMd, turnId: "t4", isFinished: true)
        if case .inlineTable(_, _, let headers, let rows, let total, _, let truncated, _)? = longNarrowParsed.settled.first?.kind {
            check(headers.count == 2 && rows.count == 6 && total == 9 && truncated, "long narrow table should lock at 6 rows and mark truncated")
        } else {
            check(false, "expected truncated inlineTable block")
        }

        // 8. Incomplete data row debounce in inline table
        let partialRowMd = "| 端口 | 状态 |\n| --- | --- |\n| 18790 | 监听 |\n| 8080"
        let partialRowParsed = StreamingBlockParser.parse(partialRowMd, turnId: "t5", isFinished: false)
        if case .inlineTable(_, _, _, let rows, _, _, _, _)? = partialRowParsed.activeTail?.kind {
            check(rows.count == 1, "incomplete streaming data row should be debounced")
        } else {
            check(false, "expected inlineTable with debounced row")
        }
    }

    private static func verifyAdaptiveRevealer() {
        let revealer = AdaptiveStreamRevealer()
        var finished = false
        revealer.reset(turnId: "rev_1") {
            finished = true
        }
        // Grapheme cluster safety with CJK + ZWJ emoji
        let sample = "你好👨‍👩‍👧‍👦世界🚀测试自适应追赶流输出效果是否平滑无乱码！"
        revealer.appendDelta(sample, turnId: "rev_1")
        // Ignore wrong turnId
        revealer.appendDelta("污染文本", turnId: "other_turn")

        check(revealer.currentTargetCPS(backlog: 10) == 55.0, "base CPS should be 55 when backlog <= 15")
        check(revealer.currentTargetCPS(backlog: 40) > 55.0, "catchUp CPS should accelerate when backlog > 15")

        revealer.tick(dt: 0.016)
        check(!revealer.revealedText.isEmpty, "tick should reveal characters")
        check(!revealer.revealedText.contains("污染"), "wrong turnId delta must be ignored")

        revealer.markServerTurnEnded(turnId: "rev_1")
        check(revealer.currentTargetCPS(backlog: 10) >= 120.0, "finish sprint CPS should be >= 120")
        for _ in 0..<20 {
            revealer.tick(dt: 0.016)
            if finished { break }
        }
        check(finished && revealer.revealedText == sample, "revealer should finish cleanly with exact grapheme output")
    }

    private static func verifyMonotonicHeightEngine() {
        var engine = MonotonicPanelHeightEngine(baseHeight: 120)
        let step1 = engine.evaluate(measured: 210, maxAllowed: 700, currentHeight: 120, isExpanded: true)
        check(step1.target == 210 && step1.shouldUpdate, "first expansion should update to 210")

        // Small jitter < 4pt should not trigger resize
        let jitter = engine.evaluate(measured: 212, maxAllowed: 700, currentHeight: 210, isExpanded: true)
        check(!jitter.shouldUpdate, "delta < 4pt should be suppressed")

        // Shrink attempt within turn must be rejected
        let shrink = engine.evaluate(measured: 180, maxAllowed: 700, currentHeight: 210, isExpanded: true)
        check(shrink.target >= 210 && !shrink.shouldUpdate, "monotonic height must never shrink within a turn")

        // Explicit collapse resets back to 120 (or idle height with staged attachments)
        let idleWithAttachment = engine.evaluate(measured: 158, maxAllowed: 700, currentHeight: 120, isExpanded: false)
        check(idleWithAttachment.target == 158 && idleWithAttachment.shouldUpdate, "idle with staged attachment must grow to 158")

        let reset = engine.evaluate(measured: 120, maxAllowed: 700, currentHeight: 212, isExpanded: false)
        check(reset.target == 120 && reset.shouldUpdate, "collapse must reset back to 120")
    }

    private static func verifyMediaTableDedupe() {
        let tableMedia = MediaItem(
            mediaId: "t_1",
            kind: "file",
            title: "表",
            fileExt: "md-table"
        )
        let imgMedia = MediaItem(
            mediaId: "img_1",
            kind: "image",
            title: "图",
            fileExt: "png"
        )
        let streamingAnswer = "结果如下：\n\n| A | B | C | D |\n|---"
        let deduped = qmxMediaWithoutInlineTables([tableMedia, imgMedia], answer: streamingAnswer)
        check(deduped.count == 1 && deduped.first?.mediaId == "img_1", "streaming table should dedupe virtual md-table")

        let noTable = qmxMediaWithoutInlineTables([tableMedia, imgMedia], answer: "仅卡片表格")
        check(noTable.count == 2, "standalone card table without inline markdown should remain in media strip")
    }
}

@MainActor
enum Phase1Verifier {
    private static func check(_ condition: Bool, _ message: String) {
        if !condition {
            fatalError("PHASE1_VERIFY FAIL: \(message)")
        }
    }

    static func runAll() {
        verifyDropPocketGeometry()
        verifyHoldToApproveAndQueue()
        verifyFilePromiseSerialization()
        print("PHASE1_VERIFY PASS")
    }

    private static func verifyDropPocketGeometry() {
        let primary = ShelfScreenBounds(
            frame: CGRect(x: 0, y: 0, width: 1440, height: 900),
            visibleFrame: CGRect(x: 0, y: 36, width: 1440, height: 839)
        )
        let secondary = ShelfScreenBounds(
            frame: CGRect(x: 1440, y: 0, width: 1920, height: 1080),
            visibleFrame: CGRect(x: 1440, y: 0, width: 1920, height: 1055)
        )

        // 1. Single screen: cursor near left docks .left, cursor near right docks .right
        let leftPlace = ScreenEdgeResolver.resolve(for: CGPoint(x: 200, y: 400), screens: [primary])
        check(leftPlace.edge == .left, "single screen left cursor should dock left")
        let leftIdleFrame = ScreenEdgeResolver.frame(for: ScreenEdgeResolver.idleEarSize, placement: leftPlace)
        check(abs(leftIdleFrame.minX - primary.visibleFrame.minX) < 0.5, "left dock must sit flush at minX")
        check(leftIdleFrame.size == CGSize(width: 48, height: 330), "idle ear size must be 48x330")

        let rightPlace = ScreenEdgeResolver.resolve(for: CGPoint(x: 1300, y: 500), screens: [primary])
        check(rightPlace.edge == .right, "single screen right cursor should dock right")

        // 2. Dual screen seam avoidance: cursor near inner seam on left monitor avoids seam and docks .left
        let dualLeftInner = ScreenEdgeResolver.resolve(for: CGPoint(x: 1410, y: 450), screens: [primary, secondary])
        check(dualLeftInner.edge == .left, "left screen with seam on right must dock to outer left edge")

        // Cursor near inner seam on right monitor avoids seam and docks .right
        let dualRightInner = ScreenEdgeResolver.resolve(for: CGPoint(x: 1460, y: 450), screens: [primary, secondary])
        check(dualRightInner.edge == .right, "right screen with seam on left must dock to outer right edge")

        // 3. Vertical clamping with 24pt top/bottom margin
        let topNear = ScreenEdgeResolver.resolve(for: CGPoint(x: 3300, y: 1075), screens: [primary, secondary])
        let topFrame = ScreenEdgeResolver.frame(for: ScreenEdgeResolver.dragExpandedSize, placement: topNear)
        check(topFrame.maxY <= secondary.visibleFrame.maxY - 24 + 0.5, "expanded shelf must clamp top edge by 24pt")
        check(abs(topFrame.maxX - secondary.visibleFrame.maxX) < 0.5, "right expanded shelf must stay anchored flush on right edge")

        let botNear = ScreenEdgeResolver.resolve(for: CGPoint(x: 3300, y: 5), screens: [primary, secondary])
        let botFrame = ScreenEdgeResolver.frame(for: ScreenEdgeResolver.dragExpandedSize, placement: botNear)
        check(botFrame.minY >= secondary.visibleFrame.minY + 24 - 0.5, "expanded shelf must clamp bottom edge by 24pt")

        // 4. Target sizes across all 3 phases + edge-anchored resize invariant
        let expSize = ScreenEdgeResolver.targetSize(for: .dragExpanded(hoveredSlot: .summary), itemCount: 0)
        check(expSize == CGSize(width: 224, height: 330), "dragExpanded size must be 224x330")
        let stashed1 = ScreenEdgeResolver.targetSize(for: .stashedStack, itemCount: 1)
        let stashed3 = ScreenEdgeResolver.targetSize(for: .stashedStack, itemCount: 5)
        check(stashed1.width == 112 && stashed1.height >= 264, "stashed 1-item size mismatch")
        check(stashed3 == CGSize(width: 112, height: 432), "stashed 3+-item size mismatch: \(stashed3)")

        let earRightFrame = ScreenEdgeResolver.frame(for: ScreenEdgeResolver.idleEarSize, placement: rightPlace)
        let expRightFrame = ScreenEdgeResolver.frame(for: expSize, placement: rightPlace)
        check(abs(earRightFrame.maxX - expRightFrame.maxX) < 0.5, "right-docked expansion must keep right edge fixed")

        // 5. Non-activating panel check
        let shelfPanel = DropShelfPanel(
            contentRect: NSRect(origin: .zero, size: ScreenEdgeResolver.idleEarSize),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        check(!shelfPanel.canBecomeKey && !shelfPanel.canBecomeMain, "DropShelfPanel must never steal key/main focus")

        // 6. 3-Edge magnetic snap classification, top horizontal layout & multi-monitor fallback
        let leftSnap = ScreenEdgeResolver.snapToNearestEdge(
            CGRect(x: 40, y: 400, width: 48, height: 220),
            in: primary.visibleFrame
        )
        check(leftSnap.edge == .left && leftSnap.x == primary.visibleFrame.minX, "drag near left edge must snap .left")

        let rightSnap = ScreenEdgeResolver.snapToNearestEdge(
            CGRect(x: 1360, y: 400, width: 48, height: 220),
            in: primary.visibleFrame
        )
        check(rightSnap.edge == .right && rightSnap.x == primary.visibleFrame.maxX, "drag near right edge must snap .right")

        let topSnap = ScreenEdgeResolver.snapToNearestEdge(
            CGRect(x: 570, y: 810, width: 300, height: 44),
            in: primary.visibleFrame
        )
        check(topSnap.edge == .top && topSnap.y == primary.visibleFrame.maxY && abs(topSnap.x - 720) < 0.5, "drag near top edge must snap .top")

        let topIdle = ScreenEdgeResolver.targetSize(for: .idleEar, edge: .top, itemCount: 0)
        let topExp = ScreenEdgeResolver.targetSize(for: .dragExpanded(hoveredSlot: nil), edge: .top, itemCount: 0)
        let topStash = ScreenEdgeResolver.targetSize(for: .stashedStack, edge: .top, itemCount: 4)
        check(topIdle == CGSize(width: 300, height: 44), "top idle size must be 300x44")
        check(topExp == CGSize(width: 450, height: 184), "top expanded size must be 450x184")
        check(topStash == CGSize(width: 519, height: 88), "top stashed 3+ size mismatch: \(topStash)")

        let restoredTop = ScreenEdgeResolver.resolveFromSaved(topSnap, screens: [primary, secondary])
        let topDockFrame = ScreenEdgeResolver.frame(for: topIdle, placement: restoredTop)
        check(abs(topDockFrame.maxY - primary.visibleFrame.maxY) < 0.5 && abs(topDockFrame.midX - topSnap.x) < 0.5, "top dock must sit flush at top visibleFrame and keep horizontal anchor")

        // Disconnected monitor fallback: saved position on secondary falls back cleanly onto primary
        let offlineFallback = ScreenEdgeResolver.resolveFromSaved(
            SavedShelfPosition(x: 2400, y: 800, edge: .right),
            screens: [primary]
        )
        let offlineFrame = ScreenEdgeResolver.frame(for: ScreenEdgeResolver.idleEarSize, placement: offlineFallback)
        check(primary.visibleFrame.contains(offlineFrame), "disconnected monitor saved coords must clamp onto active screen")
    }

    private static func verifyHoldToApproveAndQueue() {
        let now = Date()
        var hold = HoldToApproveState(appearedAt: now)
        check(!hold.canClickInReduceMotion(now: now.addingTimeInterval(0.20)), "reduce motion must wait 400ms cooldown")
        check(hold.canClickInReduceMotion(now: now.addingTimeInterval(0.45)), "reduce motion should unlock after 400ms")

        hold.beginHold()
        // Early release at 0.25s must cancel and reset progress
        for _ in 0..<5 {
            let triggered = hold.advance(dt: 0.05)
            check(!triggered, "0.25s hold must not trigger approval")
        }
        check(hold.progress > 0.4 && hold.progress < 0.7, "progress should be ~0.55 at 0.25s")
        hold.cancelHold()
        check(hold.progress == 0 && !hold.didTrigger, "early release must reset progress to 0")

        // Full 0.45s hold triggers once
        hold.beginHold()
        var triggeredCount = 0
        for _ in 0..<10 {
            if hold.advance(dt: 0.05) {
                triggeredCount += 1
            }
        }
        check(triggeredCount == 1 && hold.didTrigger, "0.45s hold must trigger exactly once")

        // Stacked queue model
        let queue = ApprovalQueueModel()
        let item1 = PendingApprovalItem(approvalId: "a1", turnId: "t1", toolName: "browser_act", riskLevel: "confirm", summary: "1", details: [:], options: [], isClarification: false, allowRemember: true, timeoutSec: 60)
        let item2 = PendingApprovalItem(approvalId: "a2", turnId: "t1", toolName: "macos_mail_draft", riskLevel: "confirm", summary: "2", details: [:], options: [], isClarification: false, allowRemember: true, timeoutSec: 60)
        queue.sync([item1, item2])
        check(queue.items.count == 2 && queue.currentItem?.approvalId == "a1", "queue should start on first item")
        queue.step(by: 1)
        check(queue.currentItem?.approvalId == "a2", "queue step should advance to second item")
        queue.remove(approvalId: "a2")
        check(queue.currentItem?.approvalId == "a1", "removing resolved item should keep valid selection")
    }

    private static func verifyFilePromiseSerialization() {
        let csv = QMXFilePromiseSerializer.csvData(
            columns: ["名称", "备注"],
            rows: [["启明星", "支持 \"引号\", 逗号"]]
        )
        let csvStr = String(data: csv, encoding: .utf8) ?? ""
        check(csv.starts(with: [0xEF, 0xBB, 0xBF]), "CSV must include UTF-8 BOM for Excel/Numbers")
        check(csvStr.contains("\"支持 \"\"引号\"\", 逗号\""), "CSV must RFC-4180 escape quotes and commas")

        check(QMXFilePromiseSerializer.fileExtension(forLanguage: "python") == "py", "python -> .py")
        check(QMXFilePromiseSerializer.fileExtension(forLanguage: "swift") == "swift", "swift -> .swift")
        check(
            QMXFilePromiseSerializer.suggestedFilename(for: .table(title: "端口/状态:检查", columns: ["A"], rows: [["1"]])) == "端口_状态_检查.csv",
            "table filename must sanitize invalid path chars and append .csv"
        )

        // Lazy write check
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent("qmx_promise_test_\(UUID().uuidString.prefix(6))", isDirectory: true)
        try? FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let targetURL = tmpDir.appendingPathComponent("out.csv")
        try? QMXFilePromiseSerializer.write(
            payload: .table(title: "Test", columns: ["Col"], rows: [["Val"]]),
            to: targetURL
        )
        check(FileManager.default.fileExists(atPath: targetURL.path), "lazy write must produce target CSV file")
    }
}

@MainActor
enum Phase2Verifier {
    private static func check(_ condition: Bool, _ message: String) {
        if !condition {
            fatalError("PHASE2_VERIFY FAIL: \(message)")
        }
    }

    static func runAll() {
        verifyDualStateHotkey()
        verifySpatialLassoGeometry()
        verifyWalkieTalkiePanel()
        print("PHASE2_VERIFY PASS")
    }

    private static func verifyDualStateHotkey() {
        // 1. Tap < 220ms
        var tapState = DualStateHotkeyState()
        tapState.handlePress(at: 10.00)
        check(tapState.phase == .pendingTap, "press must enter pendingTap")
        check(tapState.handleThresholdTick(at: 10.15) == .none, "<220ms tick must not trigger hold")
        check(tapState.handleRelease(at: 10.18) == .tap, "release before 220ms must trigger tap")
        check(tapState.phase == .idle, "tap release must return to idle")

        // 2. Hold >= 220ms
        var holdState = DualStateHotkeyState()
        holdState.handlePress(at: 20.00)
        check(holdState.handleThresholdTick(at: 20.22) == .holdStart, ">=220ms tick must start hold")
        check(holdState.phase == .holding, "phase must be holding")
        check(holdState.handleRelease(at: 21.50) == .holdEnd, "release after hold must trigger holdEnd")
        check(holdState.phase == .idle, "holdEnd must reset to idle")

        // 3. Cancel during hold (Esc)
        var cancelState = DualStateHotkeyState()
        cancelState.handlePress(at: 30.00)
        check(cancelState.handleThresholdTick(at: 30.25) == .holdStart, "expected holdStart")
        check(cancelState.cancel() == .holdCancel, "cancel during hold must emit holdCancel")
        check(cancelState.handleRelease(at: 30.40) == .none, "release after cancel must be no-op")
    }

    private static func verifySpatialLassoGeometry() {
        // Normalized rect from reverse drag
        let norm = SpatialLassoGeometry.normalizedRect(
            from: CGPoint(x: 400, y: 600),
            to: CGPoint(x: 200, y: 450)
        )
        check(norm == CGRect(x: 200, y: 450, width: 200, height: 150), "normalizedRect mismatch")

        // AppKit bottom-left -> display-local top-left
        let screenFrame = CGRect(x: 1440, y: 0, width: 1920, height: 1080)
        let appKitSelection = CGRect(x: 1640, y: 680, width: 300, height: 200)
        let localRect = SpatialLassoGeometry.displayLocalSourceRect(
            selection: appKitSelection,
            screenFrame: screenFrame
        )
        check(
            localRect == CGRect(x: 200, y: 200, width: 300, height: 200),
            "displayLocalSourceRect coordinate flip mismatch: \(localRect)"
        )

        // Popover placement below selection + clamping inside visibleFrame
        let visible = CGRect(x: 0, y: 0, width: 1440, height: 900)
        let size = CGSize(width: 340, height: 152)
        let below = SpatialLassoGeometry.popoverFrame(
            for: CGRect(x: 500, y: 500, width: 240, height: 160),
            popoverSize: size,
            visibleFrame: visible
        )
        check(abs(below.maxY - 490) < 0.5 && abs(below.minX - 500) < 0.5, "popover should sit 10pt below selection")

        // Selection near bottom edge flips above selection
        let nearBottom = SpatialLassoGeometry.popoverFrame(
            for: CGRect(x: 1200, y: 20, width: 200, height: 120),
            popoverSize: size,
            visibleFrame: visible
        )
        check(nearBottom.minY >= 150 - 0.5 && nearBottom.maxX <= visible.maxX - 12 + 0.5, "bottom selection must flip above and clamp right edge")

        // Inline artifact extraction for markdown card files
        let tmpFile = FileManager.default.temporaryDirectory
            .appendingPathComponent("qmx_lasso_artifact_\(UUID().uuidString.prefix(6)).md")
        try? "Translated English output".write(to: tmpFile, atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: tmpFile) }
        let cap = ArtifactCapsuleItem(id: "c1", turnId: "t1", title: "译文卡片", path: tmpFile.path, mediaType: "text", actions: [])
        let extracted = SpatialLassoGeometry.extractInlineArtifactText(media: [], capsules: [cap])
        check(extracted.contains("Translated English output"), "extractInlineArtifactText must inline .md artifact content")
    }

    private static func verifyWalkieTalkiePanel() {
        let capsulePanel = WalkieTalkieCapsulePanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 58),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        check(!capsulePanel.canBecomeKey && !capsulePanel.canBecomeMain, "WalkieTalkieCapsulePanel must never steal key/main focus")
    }
}

@MainActor
enum Phase3Verifier {
    private static func check(_ condition: Bool, _ message: String) {
        if !condition {
            fatalError("PHASE3_VERIFY FAIL: \(message)")
        }
    }

    static func runAll() {
        verifyCapturedSelectionAndPrompt()
        verifyPasteboardSnapshotRoundTrip()
        verifyDragShakeDetector()
        print("PHASE3_VERIFY PASS")
    }

    private static func verifyCapturedSelectionAndPrompt() {
        check(
            CapturedSelectionContext.make(text: "   \n\t ", appName: "Notes", bundleId: "com.apple.Notes") == nil,
            "whitespace-only selection must be ignored"
        )
        let lines = (1...18).map { "let line\($0) = \($0)" }.joined(separator: "\n")
        guard let ctx = CapturedSelectionContext.make(text: lines, appName: "Xcode", bundleId: "com.apple.dt.Xcode") else {
            check(false, "valid multiline selection must succeed")
            return
        }
        check(ctx.lineCount == 18 && ctx.summaryLabel == "已捕获 · Xcode 选区 · 18 行", "summaryLabel mismatch: \(ctx.summaryLabel)")

        let prompt = GhostWriteEngine.formatPromptWithSelection(userPrompt: "优化这段代码", selection: ctx)
        check(prompt.contains("Xcode") && prompt.contains("let line18 = 18") && prompt.contains("优化这段代码"), "formatted ghost write prompt missing fields")
    }

    private static func verifyPasteboardSnapshotRoundTrip() {
        let items = [
            PasteboardSnapshotItem(entries: [
                .init(typeRawValue: NSPasteboard.PasteboardType.string.rawValue, data: Data("原始剪贴板内容".utf8)),
                .init(typeRawValue: "com.example.custom-type", data: Data([0xDE, 0xAD, 0xBE, 0xEF])),
            ])
        ]
        check(PasteboardSnapshot.roundTripVerify(items: items), "pasteboard snapshot must losslessly preserve multi-type items")
    }

    private static func verifyDragShakeDetector() {
        var detector = DragShakeDetector()

        // Linear drag in one direction must not trigger
        check(!detector.record(x: 100, at: 1.00), "initial sample should not trigger")
        check(!detector.record(x: 120, at: 1.05), "linear drag step 1 should not trigger")
        check(!detector.record(x: 140, at: 1.10), "linear drag step 2 should not trigger")
        check(!detector.record(x: 165, at: 1.15), "linear drag step 3 should not trigger")

        // Micro-jitter < 12pt must not trigger
        check(!detector.record(x: 158, at: 1.18), "micro jitter should be ignored")

        // 3 rapid reversals >= 12pt within 400ms must trigger
        var fast = DragShakeDetector()
        check(!fast.record(x: 500, at: 2.00), "start")
        check(!fast.record(x: 524, at: 2.07), "right move")
        check(!fast.record(x: 496, at: 2.14), "reversal 1")
        check(!fast.record(x: 530, at: 2.21), "reversal 2")
        check(fast.record(x: 498, at: 2.28), "reversal 3 within 400ms must trigger")

        // Immediate follow-up inside 1.2s cooldown must be suppressed
        check(!fast.record(x: 530, at: 2.35), "cooldown must suppress immediate repeat")
        check(!fast.record(x: 495, at: 2.42), "cooldown must suppress reversal 1")

        // Slow reversals > 400ms must reset and not trigger
        var slow = DragShakeDetector()
        check(!slow.record(x: 500, at: 5.00), "slow start")
        check(!slow.record(x: 524, at: 5.20), "slow right")
        check(!slow.record(x: 496, at: 5.45), "window expired -> reset")
        check(!slow.record(x: 530, at: 5.65), "only reversal 1 after reset")
    }
}

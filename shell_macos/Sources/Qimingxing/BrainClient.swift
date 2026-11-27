import AVFoundation
import Foundation

@MainActor
final class BrainClient: ObservableObject {
    @Published var phase: AppConnectionPhase = .starting
    @Published var activeProfile: String = ""
    @Published var profiles: [ProfileMeta] = []
    @Published var privacyMode: String = "meta_only"
    @Published var sensesConnected: Bool = false
    @Published var activeWindowSummary: String = ""
    @Published var permissionHint: String = ""

    @Published var avatarEnabled: Bool = false
    @Published var earconsEnabled: Bool = true
    @Published var roamEnabled: Bool = true
    @Published var ambientEnabled: Bool = false

    @Published var messages: [ChatEntry] = []
    @Published var isGenerating: Bool = false
    @Published var emotion: String = "neutral"
    @Published var ttsEnabled: Bool = (UserDefaults.standard.object(forKey: "qmx.ttsEnabled") as? Bool) ?? true
    @Published var currentTurnId: String?
    @Published var toolStatusText: String = ""
    @Published var errorBannerText: String = ""

    @Published var backgroundTasks: [BackgroundTaskItem] = []
    @Published var taskFollowupAcks: [String: String] = [:]
    @Published var taskWatchdogAlerts: [String: String] = [:]
    @Published var pendingApprovals: [PendingApprovalItem] = []
    @Published var artifactCapsules: [ArtifactCapsuleItem] = []
    private var terminalTaskStatuses: [String: String] = [:]

    var onAvatarEnabledChanged: ((Bool) -> Void)?
    var onShortcutsSynced: ((String, String, String) -> Void)?
    var onPendingApprovalsUpdated: (([PendingApprovalItem]) -> Void)?
    var onApprovalResolved: ((String) -> Void)?

    private let httpBase: String
    private let wsURL: URL
    private let appVersion: String
    private let desktopActions: DesktopActions
    private let audioPlayer: AudioStreamPlayer
    private var audioStreamActive = false

    private var webSocketTask: URLSessionWebSocketTask?
    private var reconnectAttempt = 0
    private var reconnectTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var turnAckTask: Task<Void, Never>?
    private var acknowledgedTurnId: String?
    private var shouldRun = false
    private let clientInstanceId: String

    init(
        httpBase: String,
        wsURLString: String,
        appVersion: String,
        desktopActions: DesktopActions,
        audioPlayer: AudioStreamPlayer
    ) {
        self.httpBase = httpBase
        self.wsURL = URL(string: wsURLString) ?? URL(string: "ws://127.0.0.1:18790/ws")!
        self.appVersion = appVersion
        self.desktopActions = desktopActions
        self.audioPlayer = audioPlayer
        let key = "qmx.clientInstanceId"
        if let existing = UserDefaults.standard.string(forKey: key), !existing.isEmpty {
            self.clientInstanceId = existing
        } else {
            let generated = UUID().uuidString
            UserDefaults.standard.set(generated, forKey: key)
            self.clientInstanceId = generated
        }
    }

    var activeBgTaskCount: Int {
        backgroundTasks.filter(\.isActive).count
    }

    func start() {
        shouldRun = true
        connect()
    }

    func stop() {
        shouldRun = false
        reconnectTask?.cancel()
        turnAckTask?.cancel()
        turnAckTask = nil
        pingTask?.cancel()
        webSocketTask?.cancel(with: .goingAway, reason: nil)
        webSocketTask = nil
    }

    private func connect() {
        guard shouldRun else { return }
        reconnectTask?.cancel()
        pingTask?.cancel()
        webSocketTask?.cancel(with: .goingAway, reason: nil)

        let session = URLSession(configuration: .default)
        let task = session.webSocketTask(with: wsURL)
        self.webSocketTask = task
        task.resume()

        sendEnvelope(type: "system.hello", payload: [
            "client_name": "qimingxing-macos",
            "role": "body",
            "client_type": "body_macos",
            "platform": "macos",
            "app_version": appVersion,
            "client_instance_id": clientInstanceId,
            "capabilities": [
                "mirror_turns",
                "native_approvals",
                "native_desktop",
                "audio_output"
            ]
        ])

        startPingLoop()
        receiveLoop(for: task)
    }

    private func startPingLoop() {
        pingTask?.cancel()
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard let self, !Task.isCancelled else { break }
                self.sendEnvelope(type: "system.ping", payload: [
                    "client_ts": Int(Date().timeIntervalSince1970 * 1000)
                ])
            }
        }
    }

    private func scheduleReconnect() {
        guard shouldRun else { return }
        phase = .offline
        isGenerating = false
        turnAckTask?.cancel()
        turnAckTask = nil
        reconnectTask?.cancel()
        reconnectAttempt += 1
        let delay = min(5.0, pow(2.0, Double(reconnectAttempt - 1)))
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard let self, !Task.isCancelled, self.shouldRun else { return }
            self.connect()
        }
    }

    private func receiveLoop(for task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor [weak self] in
                guard let self, self.webSocketTask === task else { return }
                switch result {
                case .success(let message):
                    if case .string(let text) = message, let data = text.data(using: .utf8) {
                        self.handleIncoming(data)
                    } else if case .data(let data) = message {
                        self.handleIncoming(data)
                    }
                    self.receiveLoop(for: task)
                case .failure:
                    self.scheduleReconnect()
                }
            }
        }
    }

    func sendEnvelope(type: String, payload: [String: Any], turnId: String? = nil) {
        guard let task = webSocketTask else { return }
        var env: [String: Any] = [
            "v": "1.0",
            "id": "evt_\(UUID().uuidString.prefix(12).lowercased())",
            "ts": Int(Date().timeIntervalSince1970 * 1000),
            "source": "body",
            "type": type,
            "payload": payload
        ]
        if let turnId {
            env["turn_id"] = turnId
        }
        guard let data = try? JSONSerialization.data(withJSONObject: env),
              let text = String(data: data, encoding: .utf8)
        else { return }
        task.send(.string(text)) { [weak self] err in
            if err != nil {
                Task { @MainActor [weak self] in
                    self?.scheduleReconnect()
                }
            }
        }
    }

    // MARK: - Outbound User Actions

    func sendText(_ text: String, attachments: [StagedAttachment] = [], includeScreen: Bool = false) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty || !attachments.isEmpty else { return }
        if isGenerating {
            cancelActiveTurn(reason: "superseded")
        }
        let effectiveText = trimmed.isEmpty ? "请分析并总结我发送的附件内容。" : trimmed
        let turnId = "turn_\(UUID().uuidString.prefix(12).lowercased())"
        currentTurnId = turnId
        isGenerating = true
        emotion = "thinking"
        errorBannerText = ""

        let prefix = attachments.isEmpty
            ? (includeScreen ? "[附带屏幕] " : "")
            : attachments.map { "[附件:\($0.filename)]" }.joined(separator: " ") + " "
        messages.append(
            ChatEntry(id: UUID().uuidString, turnId: turnId, isUser: true, meta: "我", text: prefix + effectiveText)
        )
        trimMessagesIfNeeded()

        if includeScreen && attachments.isEmpty {
            sendEnvelope(
                type: "input.action",
                payload: ["action": "ask_with_screen", "data": ["text": effectiveText]],
                turnId: turnId
            )
        } else {
            var p: [String: Any] = ["text": effectiveText]
            if !attachments.isEmpty {
                p["attachments"] = attachments.map(\.artifactId)
            }
            sendEnvelope(type: "input.text", payload: p, turnId: turnId)
        }
        scheduleTurnAckWatchdog(turnId)
    }

    /// If Brain never acknowledges a turn (e.g. it crashed before sending
    /// turn_start), clear the generating state instead of spinning forever.
    private func scheduleTurnAckWatchdog(_ turnId: String) {
        turnAckTask?.cancel()
        turnAckTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            guard let self, !Task.isCancelled else { return }
            guard self.isGenerating,
                  self.currentTurnId == turnId,
                  self.acknowledgedTurnId != turnId
            else { return }
            self.isGenerating = false
            self.toolStatusText = ""
            self.errorBannerText = "后端没有确认这一轮请求，请检查 Brain 状态后重试。"
        }
    }

    func cancelActiveTurn(reason: String = "user_cancelled") {
        audioStreamActive = false
        audioPlayer.stop()
        turnAckTask?.cancel()
        turnAckTask = nil
        let tid = currentTurnId ?? ""
        sendEnvelope(type: "input.cancel", payload: ["reason": reason], turnId: tid.isEmpty ? nil : tid)
        isGenerating = false
        toolStatusText = ""
    }

    func clearHistory() {
        if isGenerating {
            cancelActiveTurn(reason: "clear_history")
        }
        currentTurnId = nil
        emotion = "neutral"
        toolStatusText = ""
        errorBannerText = ""
        messages.removeAll()
        artifactCapsules.removeAll()
        sendEnvelope(type: "input.action", payload: ["action": "clear_history"])
    }

    func setTTSEnabled(_ enabled: Bool) {
        ttsEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: "qmx.ttsEnabled")
        if !enabled {
            audioStreamActive = false
            audioPlayer.stop()
        }
        Task {
            _ = await saveSettings(["audio": ["tts": ["enabled": enabled]]])
        }
    }

    func setPrivacyMode(_ mode: String) {
        sendEnvelope(type: "system.privacy_set", payload: ["mode": mode])
    }

    func setActiveProfile(_ profileName: String) {
        sendEnvelope(type: "system.config_set", payload: ["profile": profileName])
    }

    func setUIStatePatch(_ patch: [String: Any]) {
        sendEnvelope(type: "ui.set_state", payload: ["origin": "shell_macos", "patch": patch])
    }

    func respondToApproval(
        approvalId: String,
        turnId: String,
        approved: Bool,
        rememberForSession: Bool = false,
        answerText: String = ""
    ) {
        pendingApprovals.removeAll { $0.approvalId == approvalId }
        onApprovalResolved?(approvalId)
        sendEnvelope(
            type: "tool.approval_response",
            payload: [
                "approval_id": approvalId,
                "turn_id": turnId,
                "approved": approved,
                "remember_for_session": rememberForSession,
                "answer_text": answerText
            ],
            turnId: turnId
        )
    }

    // MARK: - Inbound Event Dispatch

    private func handleIncoming(_ data: Data) {
        guard let env = JSONHelper.dict(from: data),
              let type = env["type"] as? String
        else { return }
        let payload = (env["payload"] as? [String: Any]) ?? [:]
        let turnId = (env["turn_id"] as? String) ?? ""

        switch type {
        case "system.welcome":
            phase = .connected
            reconnectAttempt = 0
            activeProfile = (payload["active_profile"] as? String) ?? activeProfile
            if let rawProfs = payload["profiles"] as? [[String: Any]] {
                profiles = rawProfs.map {
                    ProfileMeta(
                        name: ($0["name"] as? String) ?? "",
                        label: ($0["label"] as? String) ?? "",
                        provider: ($0["provider"] as? String) ?? "",
                        model: ($0["model"] as? String) ?? "",
                        supportsVision: ($0["supports_vision"] as? Bool) ?? false
                    )
                }
            }
            if let sc = payload["shortcuts"] as? [String: Any] {
                onShortcutsSynced?(
                    (sc["chat_input_hotkey"] as? String) ?? "option+space",
                    (sc["settings_hotkey"] as? String) ?? "option+k",
                    (sc["cancel_hotkey"] as? String) ?? "option+esc"
                )
            }
            Task { await refreshTasks() }

        case "system.shortcuts_sync":
            onShortcutsSynced?(
                (payload["chat_input_hotkey"] as? String) ?? "option+space",
                (payload["settings_hotkey"] as? String) ?? "option+k",
                (payload["cancel_hotkey"] as? String) ?? "option+esc"
            )

        case "system.privacy_state":
            privacyMode = (payload["mode"] as? String) ?? privacyMode
            sensesConnected = (payload["senses_connected"] as? Bool) ?? false
            activeWindowSummary = (payload["active_window_summary"] as? String) ?? ""
            permissionHint = (payload["permission_hint"] as? String) ?? ""

        case "ui.state":
            let nextAvatar = (payload["avatar_enabled"] as? Bool) ?? false
            if nextAvatar != avatarEnabled {
                avatarEnabled = nextAvatar
                onAvatarEnabledChanged?(nextAvatar)
            }
            earconsEnabled = (payload["earcons"] as? Bool) ?? earconsEnabled
            roamEnabled = (payload["roam"] as? Bool) ?? roamEnabled
            ambientEnabled = (payload["ambient"] as? Bool) ?? ambientEnabled
            if let mode = payload["privacy_mode"] as? String {
                privacyMode = mode
            }
            if let rawPending = payload["pending_approvals"] as? [[String: Any]] {
                for reqDict in rawPending {
                    if let item = parseApprovalRequest(reqDict, fallbackTurnId: turnId) {
                        upsertPendingApproval(item)
                    }
                }
                onPendingApprovalsUpdated?(pendingApprovals)
            }

        case "task.status":
            if let taskId = payload["task_id"] as? String, !taskId.isEmpty {
                let existing = backgroundTasks.first(where: { $0.taskId == taskId })
                let item = BackgroundTaskItem(
                    taskId: taskId,
                    title: (payload["title"] as? String) ?? existing?.title ?? taskId,
                    status: (payload["status"] as? String) ?? "queued",
                    progressPct: (payload["progress_pct"] as? Int) ?? 0,
                    currentStep: (payload["current_step"] as? String) ?? "",
                    resultSummary: (payload["result_summary"] as? String) ?? existing?.resultSummary ?? "",
                    artifactPath: (payload["artifact_path"] as? String) ?? existing?.artifactPath ?? "",
                    errorMessage: (payload["error_message"] as? String) ?? existing?.errorMessage ?? "",
                    blockReason: existing?.blockReason ?? "",
                    watchdogReason: existing?.watchdogReason ?? taskWatchdogAlerts[taskId] ?? "",
                    followupAck: existing?.followupAck ?? taskFollowupAcks[taskId] ?? "",
                    resultRefs: existing?.resultRefs ?? [],
                    bundlePath: (payload["bundle_path"] as? String) ?? existing?.bundlePath ?? "",
                    resultMedia: Self.mediaItems(from: payload["result_media"])
                        ?? existing?.resultMedia ?? []
                )
                upsertBackgroundTask(item)
                synthesizeTaskResultIfNeeded(taskId: taskId)
            }

        case "task.state_changed":
            if let taskId = payload["task_id"] as? String, !taskId.isEmpty {
                let existing = backgroundTasks.first(where: { $0.taskId == taskId })
                let item = BackgroundTaskItem(
                    taskId: taskId,
                    title: (payload["title"] as? String) ?? existing?.title ?? taskId,
                    status: (payload["status"] as? String) ?? existing?.status ?? "queued",
                    progressPct: (payload["progress_pct"] as? Int) ?? existing?.progressPct ?? 0,
                    currentStep: (payload["current_step"] as? String) ?? existing?.currentStep ?? "",
                    resultSummary: existing?.resultSummary ?? "",
                    artifactPath: existing?.artifactPath ?? "",
                    errorMessage: existing?.errorMessage ?? "",
                    blockReason: (payload["block_reason"] as? String) ?? "",
                    watchdogReason: existing?.watchdogReason ?? "",
                    followupAck: existing?.followupAck ?? "",
                    resultRefs: (payload["result_refs"] as? [String]) ?? existing?.resultRefs ?? [],
                    bundlePath: (payload["bundle_path"] as? String) ?? existing?.bundlePath ?? "",
                    resultMedia: Self.mediaItems(from: payload["result_media"])
                        ?? existing?.resultMedia ?? []
                )
                upsertBackgroundTask(item)
                let previous = terminalTaskStatuses[taskId]
                terminalTaskStatuses[taskId] = item.status
                if ["completed", "failed"].contains(item.status), previous != item.status {
                    let message = friendlyTaskMessage(item)
                    let isFailure = (item.status == "failed")
                    let toastTitle = isFailure
                        ? "任务失败 · \(item.title)"
                        : "任务完成 · \(item.title)"
                    TaskToastController.shared.show(
                        title: toastTitle,
                        message: message,
                        style: isFailure ? .error : .success,
                        actionTitle: "查看结果",
                        route: .workspace(taskId: item.taskId),
                        mirrorToSystem: true
                    ) {
                        NotificationCenter.default.post(name: .qmxOpenWorkspace, object: nil)
                    }
                }
                synthesizeTaskResultIfNeeded(taskId: taskId)
            }

        case "task.followup_acked":
            if let taskId = payload["task_id"] as? String, !taskId.isEmpty,
               let message = payload["message"] as? String, !message.isEmpty {
                taskFollowupAcks[taskId] = message
                if let idx = backgroundTasks.firstIndex(where: { $0.taskId == taskId }) {
                    backgroundTasks[idx].followupAck = message
                }
            }

        case "task.watchdog_alert":
            if let taskId = payload["task_id"] as? String, !taskId.isEmpty {
                let action = (payload["action"] as? String) ?? "nudge"
                let reason = (payload["reason"] as? String) ?? ""
                taskWatchdogAlerts[taskId] = reason
                if let idx = backgroundTasks.firstIndex(where: { $0.taskId == taskId }) {
                    backgroundTasks[idx].watchdogReason = reason
                    backgroundTasks[idx].blockReason = reason
                    if action == "abort" {
                        backgroundTasks[idx].status = "failed"
                    } else if action == "escalate" {
                        backgroundTasks[idx].status = "waiting_input"
                    }
                }
            }

        case "output.turn_start":
            acknowledgedTurnId = turnId
            turnAckTask?.cancel()
            turnAckTask = nil
            if let originType = payload["origin_client_type"] as? String,
               originType != "body_macos" && currentTurnId != turnId {
                // Mirrored turn from another client
                currentTurnId = turnId
            }
            isGenerating = true
            emotion = "thinking"
            if let userText = payload["user_text"] as? String,
               !userText.isEmpty,
               !messages.contains(where: { $0.turnId == turnId && $0.isUser }) {
                messages.append(
                    ChatEntry(
                        id: UUID().uuidString,
                        turnId: turnId,
                        isUser: true,
                        meta: "我",
                        text: userText
                    )
                )
            }
            let prof = (payload["profile"] as? String) ?? ""
            let mod = (payload["model"] as? String) ?? ""
            let meta = prof.isEmpty ? "启明星" : "启明星 · \(prof) (\(mod))"
            ensureAssistantMessage(turnId: turnId, meta: meta)

        case "output.text_delta":
            let delta = (payload["delta"] as? String) ?? ""
            if !delta.isEmpty {
                appendDelta(turnId: turnId, delta: delta)
            }

        case "output.turn_end":
            let isCurrentTurn = turnId.isEmpty || turnId == currentTurnId
            if let idx = messages.lastIndex(where: { !$0.isUser && $0.turnId == turnId }) {
                let wasStreaming = messages[idx].isStreaming
                messages[idx].isStreaming = false
                if let rawSources = payload["sources"] as? [[String: Any]] {
                    messages[idx].sources = rawSources.compactMap(SourceItem.init(dict:))
                }
                if let rawMedia = payload["media"] as? [[String: Any]] {
                    messages[idx].media = rawMedia.compactMap(MediaItem.init(dict:))
                }
                if (payload["finish_reason"] as? String) == "cancelled", wasStreaming {
                    messages[idx].meta += " · [已中断]"
                }
            }
            // A late turn_end for a previous turn (e.g. its TTS was still
            // playing when the next turn started) must not unlock the current
            // turn or cancel its ack watchdog.
            if isCurrentTurn {
                isGenerating = false
                toolStatusText = ""
                turnAckTask?.cancel()
                turnAckTask = nil
            }

        case "output.tool_status":
            let status = (payload["status"] as? String) ?? ""
            let label = (payload["label"] as? String) ?? ""
            let tool = (payload["tool"] as? String) ?? ""
            toolStatusText = status == "running" ? (label.isEmpty ? "执行 \(tool)…" : label) : ""

        case "output.proactive_notice":
            let title = (payload["title"] as? String) ?? "启明星通知"
            let content = (payload["content"] as? String) ?? ""
            let taskId = (payload["task_id"] as? String) ?? ""
            desktopActions.showProactiveNotification(title: title, body: content, taskId: taskId)

        case "output.artifact_capsule":
            let path = (payload["path"] as? String) ?? ""
            if !path.isEmpty {
                let cap = ArtifactCapsuleItem(
                    id: UUID().uuidString,
                    turnId: (payload["turn_id"] as? String) ?? turnId,
                    title: (payload["title"] as? String) ?? URL(fileURLWithPath: path).lastPathComponent,
                    path: path,
                    mediaType: (payload["media_type"] as? String) ?? "file",
                    actions: (payload["actions"] as? [String]) ?? ["quick_look", "copy_file", "reveal"]
                )
                artifactCapsules.insert(cap, at: 0)
            }

        case "tool.approval_request":
            if let item = parseApprovalRequest(payload, fallbackTurnId: turnId) {
                upsertPendingApproval(item)
                onPendingApprovalsUpdated?(pendingApprovals)
            }

        case "tool.approval_resolved":
            if let aid = payload["approval_id"] as? String {
                pendingApprovals.removeAll { $0.approvalId == aid }
                onApprovalResolved?(aid)
            }

        case "audio.output_start":
            guard ttsEnabled else { break }
            audioStreamActive = true
            let sr = (payload["sample_rate"] as? Double) ?? Double((payload["sample_rate"] as? Int) ?? 24000)
            let ch = AVAudioChannelCount((payload["channels"] as? Int) ?? 1)
            audioPlayer.startStream(sampleRate: sr, channels: ch)

        case "audio.output_chunk":
            guard ttsEnabled && audioStreamActive else { break }
            if let b64 = payload["data_b64"] as? String,
               let pcm = Data(base64Encoded: b64) {
                audioPlayer.enqueuePCM16LE(pcm)
            }

        case "audio.output_end":
            audioStreamActive = false
            guard ttsEnabled else { break }
            let reason = (payload["finish_reason"] as? String) ?? "completed"
            if reason == "cancelled" {
                audioPlayer.stop()
            } else {
                audioPlayer.finishStream()
            }

        case "avatar.state":
            if let value = payload["emotion"] as? String, !value.isEmpty {
                let activity = (payload["activity"] as? String) ?? ""
                // The turn-end idle/neutral reset should not erase the emotion
                // of the answer the user is still reading.
                if !(value == "neutral" && activity == "idle") {
                    emotion = value
                }
            }

        case "ui.command":
            handleUICommand(payload)

        case "system.error":
            let msg = (payload["message"] as? String) ?? "发生未知错误"
            errorBannerText = msg
            if (payload["code"] as? String) != "USAGE_SOFT_ALERT" {
                isGenerating = false
            }

        default:
            break
        }
    }

    private func handleUICommand(_ payload: [String: Any]) {
        let cmdId = (payload["command_id"] as? String) ?? ""
        let action = (payload["action"] as? String) ?? ""
        let args = (payload["args"] as? [String: Any]) ?? [:]
        let path = (args["path"] as? String) ?? ""

        var ok = false
        var err = ""
        switch action {
        case "reveal_path":
            (ok, err) = desktopActions.revealPath(path)
        case "open_path":
            (ok, err) = desktopActions.openPath(path)
        case "copy_file":
            (ok, err) = desktopActions.copyFile(path)
        case "quick_look":
            (ok, err) = desktopActions.quickLook(path)
        case "show_pin_image":
            let title = (args["title"] as? String) ?? "启明星钉图"
            (ok, err) = desktopActions.showPinImage(path, title: title)
        case "show_notification":
            let title = (args["title"] as? String) ?? "启明星"
            let body = (args["body"] as? String) ?? ""
            desktopActions.showProactiveNotification(title: title, body: body)
            ok = true
        case "open_quick_entry":
            NotificationCenter.default.post(name: .qmxOpenQuickEntry, object: nil)
            ok = true
        case "open_settings":
            NotificationCenter.default.post(name: .qmxOpenSettings, object: args["tab"])
            ok = true
        case "open_workspace":
            NotificationCenter.default.post(name: .qmxOpenWorkspace, object: nil)
            ok = true
        case "detach_workspace":
            NotificationCenter.default.post(name: .qmxDetachWorkspace, object: nil)
            ok = true
        case "close_workspace":
            NotificationCenter.default.post(name: .qmxCloseWorkspaceWing, object: nil)
            ok = true
        case "hide_quick_entry":
            NotificationCenter.default.post(name: .qmxHideQuickEntry, object: nil)
            ok = true
        default:
            err = "unsupported_action"
        }

        if !cmdId.isEmpty {
            sendEnvelope(type: "ui.command_result", payload: [
                "command_id": cmdId,
                "ok": ok,
                "error": err,
                "data": [:]
            ])
        }
    }

    private func parseApprovalRequest(_ payload: [String: Any], fallbackTurnId: String) -> PendingApprovalItem? {
        guard let aid = payload["approval_id"] as? String, !aid.isEmpty else { return nil }
        let detailsRaw = (payload["details"] as? [String: Any]) ?? [:]
        var detailStrings: [String: String] = [:]
        for (k, v) in detailsRaw {
            detailStrings[k] = JSONHelper.stringified(v)
        }
        let isClarify = ((payload["tool_name"] as? String) == "ask_user") || ((detailsRaw["kind"] as? String) == "clarification")
        let opts = (detailsRaw["options"] as? [String]) ?? []
        let allowRemember = (detailsRaw["allow_remember"] as? Bool) ?? true
        return PendingApprovalItem(
            approvalId: aid,
            turnId: (payload["turn_id"] as? String) ?? fallbackTurnId,
            toolName: (payload["tool_name"] as? String) ?? "tool",
            riskLevel: (payload["risk_level"] as? String) ?? "confirm",
            summary: (payload["summary"] as? String) ?? "请确认是否允许执行",
            details: detailStrings,
            options: opts,
            isClarification: isClarify,
            allowRemember: allowRemember,
            timeoutSec: (payload["timeout_sec"] as? Int) ?? 60
        )
    }

    private func upsertPendingApproval(_ item: PendingApprovalItem) {
        if !pendingApprovals.contains(where: { $0.approvalId == item.approvalId }) {
            pendingApprovals.append(item)
        }
    }

    private func ensureAssistantMessage(turnId: String, meta: String) {
        if let idx = messages.lastIndex(where: { !$0.isUser && $0.turnId == turnId }) {
            messages[idx].isStreaming = true
            return
        }
        messages.append(
            ChatEntry(id: UUID().uuidString, turnId: turnId, isUser: false, meta: meta, text: "", isStreaming: true)
        )
        trimMessagesIfNeeded()
    }

    private func appendDelta(turnId: String, delta: String) {
        if let idx = messages.lastIndex(where: { !$0.isUser && $0.turnId == turnId }) {
            messages[idx].text += delta
        } else {
            messages.append(
                ChatEntry(id: UUID().uuidString, turnId: turnId, isUser: false, meta: "启明星", text: delta, isStreaming: true)
            )
        }
        trimMessagesIfNeeded()
    }

    /// The live panel only needs the recent tail; workspaces keep the full
    /// history. Bounding this array prevents per-update cost from growing
    /// without limit across multi-day sessions.
    private func trimMessagesIfNeeded() {
        let maxInMemoryMessages = 400
        if messages.count > maxInMemoryMessages {
            messages.removeFirst(messages.count - maxInMemoryMessages)
        }
    }

    private func upsertBackgroundTask(_ item: BackgroundTaskItem) {
        if let idx = backgroundTasks.firstIndex(where: { $0.taskId == item.taskId }) {
            backgroundTasks[idx] = item
        } else {
            backgroundTasks.insert(item, at: 0)
        }
    }

    static func mediaItems(from value: Any?) -> [MediaItem]? {
        guard let raw = value as? [[String: Any]] else { return nil }
        return raw.compactMap(MediaItem.init(dict:))
    }

    /// Short human-facing line for the completion banner. It never falls back
    /// to error_message: users should not see HTTP codes or stack traces here.
    private func friendlyTaskMessage(_ item: BackgroundTaskItem) -> String {
        var raw = item.resultSummary.trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.isEmpty {
            return item.status == "completed"
                ? "任务已完成，产物见下方附件。"
                : "任务没有完成，详情可在工作区查看。"
        }
        raw = raw
            .replacingOccurrences(of: "**", with: "")
            .replacingOccurrences(of: "`", with: "")
            .replacingOccurrences(of: "^#{1,4}\\s*", with: "", options: .regularExpression)
        let lines = raw
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let headings: Set<String> = ["关键数字", "结论", "结果", "摘要", "交付结果", "完成"]
        var first = lines.first ?? raw
        if headings.contains(first), lines.count > 1 {
            first = lines.dropFirst().prefix(2).joined(separator: " · ")
        }
        var text = first.hasPrefix("- ") ? String(first.dropFirst(2)) : first
        text = text.replacingOccurrences(of: " · - ", with: " · ")
        if text.count > 96 {
            let cut = text.index(text.startIndex, offsetBy: 96)
            text = String(text[..<cut]) + "…"
        }
        return text
    }

    /// A finished background task becomes a real conversation turn so its
    /// produced files appear as attachments, not as a bare path. The entry is
    /// keyed by task id, so repeated status events update it instead of
    /// stacking duplicates.
    private func synthesizeTaskResultIfNeeded(taskId: String) {
        guard let item = backgroundTasks.first(where: { $0.taskId == taskId }) else { return }
        guard item.status == "completed" || item.status == "failed" else { return }
        let text: String
        if item.status == "completed" {
            if !item.resultSummary.isEmpty {
                text = item.resultSummary
            } else {
                text = item.resultMedia.isEmpty ? "任务已完成。" : "任务已完成，产物见下方附件。"
            }
        } else {
            text = item.resultSummary.isEmpty
                ? "任务没有完成，详情可在工作区查看。"
                : item.resultSummary
        }
        let turnId = "task_\(taskId)"
        if let idx = messages.firstIndex(where: { $0.turnId == turnId && !$0.isUser }) {
            guard messages[idx].text != text || messages[idx].media != item.resultMedia else {
                return
            }
            messages[idx].text = text
            messages[idx].media = item.resultMedia
            return
        }
        guard !text.isEmpty || !item.resultMedia.isEmpty else { return }
        messages.append(
            ChatEntry(
                id: "task_\(taskId)_result",
                turnId: turnId,
                isUser: false,
                meta: item.status == "completed" ? "启明星 · 后台任务已完成" : "启明星 · 后台任务失败",
                text: text,
                isStreaming: false,
                media: item.resultMedia
            )
        )
    }

    // MARK: - HTTP REST APIs

    func refreshTasks() async {
        guard let url = URL(string: "\(httpBase)/api/tasks") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let obj = JSONHelper.dict(from: data), let list = obj["tasks"] as? [[String: Any]] {
                backgroundTasks = list.compactMap { t in
                    guard let tid = t["task_id"] as? String else { return nil }
                    return BackgroundTaskItem(
                        taskId: tid,
                        title: (t["title"] as? String) ?? tid,
                        status: (t["status"] as? String) ?? "queued",
                        progressPct: (t["progress_pct"] as? Int) ?? 0,
                        currentStep: (t["current_step"] as? String) ?? "",
                        resultSummary: (t["result_summary"] as? String) ?? "",
                        artifactPath: (t["artifact_path"] as? String) ?? "",
                        errorMessage: (t["error_message"] as? String) ?? "",
                        bundlePath: (t["bundle_path"] as? String) ?? "",
                        resultMedia: Self.mediaItems(from: t["result_media"]) ?? []
                    )
                }
            }
        } catch {}
    }

    func cancelBackgroundTask(taskId: String) async {
        guard let url = URL(string: "\(httpBase)/api/tasks/\(taskId)/cancel") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: req)
        await refreshTasks()
    }

    func uploadArtifact(fileURL: URL) async -> StagedAttachment? {
        guard let url = URL(string: "\(httpBase)/api/artifacts"),
              let fileData = try? Data(contentsOf: fileURL)
        else { return nil }
        let boundary = "Boundary-\(UUID().uuidString)"
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()
        let filename = fileURL.lastPathComponent
        let partContentType = Self.mimeType(for: fileURL)
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: \(partContentType)\r\n\r\n".data(using: .utf8)!)
        body.append(fileData)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body

        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200,
                  let obj = JSONHelper.dict(from: data),
                  let aid = obj["artifact_id"] as? String
            else { return nil }
            return StagedAttachment(
                artifactId: aid,
                filename: (obj["filename"] as? String) ?? filename,
                kind: (obj["kind"] as? String) ?? "file"
            )
        } catch {
            return nil
        }
    }

    private static func mimeType(for fileURL: URL) -> String {
        switch fileURL.pathExtension.lowercased() {
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "webp": return "image/webp"
        case "gif": return "image/gif"
        case "heic": return "image/heic"
        case "pdf": return "application/pdf"
        case "txt", "md", "markdown": return "text/plain"
        case "json": return "application/json"
        default: return "application/octet-stream"
        }
    }

    func fetchSettings() async -> [String: Any]? {
        guard let url = URL(string: "\(httpBase)/api/config/settings") else { return nil }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            return JSONHelper.dict(from: data)
        } catch {
            return nil
        }
    }

    func fetchBrowserStatus() async -> [String: Any]? {
        guard let url = URL(string: "\(httpBase)/api/browser/status") else { return nil }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            return JSONHelper.dict(from: data)
        } catch {
            return nil
        }
    }

    func testBrowserCDP(endpoint: String) async -> [String: Any]? {
        guard let url = URL(string: "\(httpBase)/api/browser/test_cdp"),
              let body = try? JSONSerialization.data(withJSONObject: ["endpoint": endpoint])
        else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            return JSONHelper.dict(from: data)
        } catch {
            return nil
        }
    }

    func clearBrowserProfile() async -> [String: Any]? {
        guard let url = URL(string: "\(httpBase)/api/browser/clear_profile") else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            return JSONHelper.dict(from: data)
        } catch {
            return nil
        }
    }

    func saveSettings(_ updates: [String: Any]) async -> (Bool, String) {
        guard let url = URL(string: "\(httpBase)/api/config/settings"),
              let body = try? JSONSerialization.data(withJSONObject: updates)
        else { return (false, "参数序列化失败") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let obj = JSONHelper.dict(from: data)
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                return (true, "设置已保存并热生效")
            }
            return (false, (obj?["detail"] as? String) ?? "保存失败")
        } catch {
            return (false, error.localizedDescription)
        }
    }

    func sendFeedback(turnId: String, rating: String, prompt: String, answer: String) async {
        guard let url = URL(string: "\(httpBase)/api/feedback") else { return }
        let payload: [String: Any] = [
            "turn_id": turnId,
            "rating": rating,
            "profile": activeProfile,
            "prompt": prompt,
            "answer": answer,
            "source": "shell_macos"
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        _ = try? await URLSession.shared.data(for: req)
    }

    func discoverModels(providerId: String, apiKey: String = "") async -> (Bool, String, Int) {
        guard let url = URL(string: "\(httpBase)/api/models/discover") else {
            return (false, "无效的 Brain 地址", 0)
        }
        var payload: [String: Any] = ["provider_id": providerId]
        if !apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            payload["api_key"] = apiKey
        }
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            return (false, "参数序列化失败", 0)
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let obj = JSONHelper.dict(from: data)
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                let count = (obj?["count"] as? Int) ?? 0
                return (true, "已同步 \(count) 个模型", count)
            }
            return (false, (obj?["detail"] as? String) ?? "刷新模型失败", 0)
        } catch {
            return (false, error.localizedDescription, 0)
        }
    }

    func testProvider(_ payload: [String: Any]) async -> (Bool, String) {
        guard let url = URL(string: "\(httpBase)/api/config/test_provider"),
              let body = try? JSONSerialization.data(withJSONObject: payload)
        else { return (false, "参数错误") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            if let obj = JSONHelper.dict(from: data) {
                let ok = (obj["ok"] as? Bool) ?? false
                let msg = (obj["message"] as? String) ?? (ok ? "连通正常" : "连通失败")
                return (ok, msg)
            }
            return (false, "响应解析失败")
        } catch {
            return (false, error.localizedDescription)
        }
    }

    func fetchMCPServers() async -> [MCPServerViewModel] {
        guard let url = URL(string: "\(httpBase)/api/mcp/servers") else { return [] }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            guard let obj = JSONHelper.dict(from: data),
                  let rawList = obj["servers"] as? [[String: Any]]
            else { return [] }
            return rawList.map { s in
                MCPServerViewModel(
                    name: (s["name"] as? String) ?? "",
                    enabled: (s["enabled"] as? Bool) ?? true,
                    transport: (s["transport"] as? String) ?? "stdio",
                    command: (s["command"] as? String) ?? "",
                    args: (s["args"] as? [String]) ?? [],
                    url: (s["url"] as? String) ?? "",
                    authMode: (s["auth_mode"] as? String) ?? "none",
                    safeTools: (s["safe_tools"] as? [String]) ?? [],
                    status: (s["status"] as? String) ?? "offline",
                    error: (s["error"] as? String) ?? "",
                    hasOAuthToken: (s["has_oauth_token"] as? Bool) ?? false
                )
            }
        } catch {
            return []
        }
    }

    func upsertMCPServer(_ bodyDict: [String: Any]) async -> (Bool, String) {
        guard let url = URL(string: "\(httpBase)/api/mcp/servers"),
              let body = try? JSONSerialization.data(withJSONObject: bodyDict)
        else { return (false, "参数序列化失败") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let obj = JSONHelper.dict(from: data)
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                return (true, "MCP 服务已保存并重载")
            }
            return (false, (obj?["detail"] as? String) ?? "保存失败")
        } catch {
            return (false, error.localizedDescription)
        }
    }

    func deleteMCPServer(name: String) async {
        guard let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "\(httpBase)/api/mcp/servers/\(encoded)")
        else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "DELETE"
        _ = try? await URLSession.shared.data(for: req)
    }

    func authorizeMCPServer(name: String) async -> (Bool, String, String?) {
        guard let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "\(httpBase)/api/mcp/servers/\(encoded)/authorize")
        else { return (false, "无效服务名", nil) }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            let obj = JSONHelper.dict(from: data) ?? [:]
            let ok = (obj["ok"] as? Bool) ?? false
            let msg = (obj["message"] as? String) ?? (ok ? "请在浏览器完成授权" : "授权启动失败")
            let authURL = obj["authorize_url"] as? String
            return (ok, msg, authURL)
        } catch {
            return (false, error.localizedDescription, nil)
        }
    }
}

extension Notification.Name {
    static let qmxOpenQuickEntry = Notification.Name("com.qimingxing.openQuickEntry")
    static let qmxOpenSettings = Notification.Name("com.qimingxing.openSettings")
}

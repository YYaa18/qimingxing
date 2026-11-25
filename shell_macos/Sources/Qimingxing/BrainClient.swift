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
    @Published var currentTurnId: String?
    @Published var toolStatusText: String = ""
    @Published var errorBannerText: String = ""

    @Published var backgroundTasks: [BackgroundTaskItem] = []
    @Published var pendingApprovals: [PendingApprovalItem] = []
    @Published var artifactCapsules: [ArtifactCapsuleItem] = []

    var onAvatarEnabledChanged: ((Bool) -> Void)?
    var onShortcutsSynced: ((String, String, String) -> Void)?
    var onPendingApprovalsUpdated: (([PendingApprovalItem]) -> Void)?
    var onApprovalResolved: ((String) -> Void)?

    private let httpBase: String
    private let wsURL: URL
    private let appVersion: String
    private let desktopActions: DesktopActions
    private let audioPlayer: AudioStreamPlayer

    private var webSocketTask: URLSessionWebSocketTask?
    private var reconnectAttempt = 0
    private var reconnectTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var shouldRun = false

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
            "capabilities": [
                "mirror_turns",
                "native_approvals",
                "native_desktop",
                "audio_output"
            ]
        ])

        reconnectAttempt = 0
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
        errorBannerText = ""

        let prefix = attachments.isEmpty
            ? (includeScreen ? "[附带屏幕] " : "")
            : attachments.map { "[附件:\($0.filename)]" }.joined(separator: " ") + " "
        messages.append(
            ChatEntry(id: UUID().uuidString, turnId: turnId, isUser: true, meta: "我", text: prefix + effectiveText)
        )

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
    }

    func cancelActiveTurn(reason: String = "user_cancelled") {
        audioPlayer.stop()
        let tid = currentTurnId ?? ""
        sendEnvelope(type: "input.cancel", payload: ["reason": reason], turnId: tid.isEmpty ? nil : tid)
        isGenerating = false
        toolStatusText = ""
    }

    func clearHistory() {
        if isGenerating {
            cancelActiveTurn(reason: "clear_history")
        }
        messages.removeAll()
        artifactCapsules.removeAll()
        sendEnvelope(type: "input.action", payload: ["action": "clear_history"])
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
                let item = BackgroundTaskItem(
                    taskId: taskId,
                    title: (payload["title"] as? String) ?? taskId,
                    status: (payload["status"] as? String) ?? "queued",
                    progressPct: (payload["progress_pct"] as? Int) ?? 0,
                    currentStep: (payload["current_step"] as? String) ?? "",
                    resultSummary: (payload["result_summary"] as? String) ?? "",
                    artifactPath: (payload["artifact_path"] as? String) ?? "",
                    errorMessage: (payload["error_message"] as? String) ?? ""
                )
                if let idx = backgroundTasks.firstIndex(where: { $0.taskId == taskId }) {
                    backgroundTasks[idx] = item
                } else {
                    backgroundTasks.insert(item, at: 0)
                }
            }

        case "output.turn_start":
            if let originType = payload["origin_client_type"] as? String,
               originType != "body_macos" && currentTurnId != turnId {
                // Mirrored turn from another client
                currentTurnId = turnId
            }
            isGenerating = true
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
            isGenerating = false
            toolStatusText = ""
            if let idx = messages.lastIndex(where: { !$0.isUser && $0.turnId == turnId }) {
                messages[idx].isStreaming = false
                if (payload["finish_reason"] as? String) == "cancelled" {
                    messages[idx].meta += " · [已中断]"
                }
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
            let sr = (payload["sample_rate"] as? Double) ?? Double((payload["sample_rate"] as? Int) ?? 24000)
            let ch = AVAudioChannelCount((payload["channels"] as? Int) ?? 1)
            audioPlayer.startStream(sampleRate: sr, channels: ch)

        case "audio.output_chunk":
            if let b64 = payload["data_b64"] as? String,
               let pcm = Data(base64Encoded: b64) {
                audioPlayer.enqueuePCM16LE(pcm)
            }

        case "audio.output_end":
            let reason = (payload["finish_reason"] as? String) ?? "completed"
            if reason == "cancelled" {
                audioPlayer.stop()
            } else {
                audioPlayer.finishStream()
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
    }

    private func appendDelta(turnId: String, delta: String) {
        if let idx = messages.lastIndex(where: { !$0.isUser && $0.turnId == turnId }) {
            messages[idx].text += delta
        } else {
            messages.append(
                ChatEntry(id: UUID().uuidString, turnId: turnId, isUser: false, meta: "启明星", text: delta, isStreaming: true)
            )
        }
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
                        errorMessage: (t["error_message"] as? String) ?? ""
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
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: application/octet-stream\r\n\r\n".data(using: .utf8)!)
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

    func fetchSettings() async -> [String: Any]? {
        guard let url = URL(string: "\(httpBase)/api/config/settings") else { return nil }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
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

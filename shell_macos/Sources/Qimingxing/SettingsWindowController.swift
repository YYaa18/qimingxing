import AppKit
import ServiceManagement
import SwiftUI

enum SettingsSection: String, CaseIterable, Identifiable {
    case general = "通用与外观"
    case routing = "模型与路由"
    case voicePrivacy = "语音与隐私"
    case shortcuts = "快捷键"
    case mcp = "MCP 服务"

    var id: String { rawValue }
    var icon: String {
        switch self {
        case .general: return "gearshape"
        case .routing: return "cpu"
        case .voicePrivacy: return "waveform.and.mic"
        case .shortcuts: return "command"
        case .mcp: return "puzzlepiece.extension"
        }
    }
}

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let brainClient: BrainClient
    private let supervisor: ProcessSupervisor
    private let desktopActions: DesktopActions
    private let store = SettingsViewStore()

    init(brainClient: BrainClient, supervisor: ProcessSupervisor, desktopActions: DesktopActions) {
        self.brainClient = brainClient
        self.supervisor = supervisor
        self.desktopActions = desktopActions
        super.init()
    }

    func show(tab: String? = nil) {
        if let tab, let sec = Self.section(for: tab) {
            store.selectedSection = sec
        }
        ensureWindow()
        Task { await store.reload(brainClient: brainClient) }
        NSApp.activate(ignoringOtherApps: true)
        window?.center()
        window?.makeKeyAndOrderFront(nil)
    }

    private static func section(for tab: String) -> SettingsSection? {
        switch tab.lowercased() {
        case "general": return .general
        case "routing", "models": return .routing
        case "voice", "privacy": return .voicePrivacy
        case "shortcuts": return .shortcuts
        case "mcp": return .mcp
        default: return nil
        }
    }

    private func ensureWindow() {
        if window != nil { return }
        let root = SettingsRootView(
            store: store,
            brainClient: brainClient,
            supervisor: supervisor,
            desktopActions: desktopActions
        )
        let hosting = NSHostingController(rootView: root)
        let win = NSWindow(contentViewController: hosting)
        win.title = "启明星设置中心"
        win.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        win.setContentSize(NSSize(width: 820, height: 580))
        win.minSize = NSSize(width: 720, height: 500)
        win.delegate = self
        self.window = win
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
}

@MainActor
final class SettingsViewStore: ObservableObject {
    @Published var selectedSection: SettingsSection = .general
    @Published var statusMessage: String = ""

    @Published var avatarEnabled: Bool = false
    @Published var earconsEnabled: Bool = true
    @Published var roamEnabled: Bool = true
    @Published var ambientEnabled: Bool = false
    @Published var launchAtLogin: Bool = false

    @Published var providers: [ProviderViewModel] = []
    @Published var chatSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-chat")
    @Published var visionSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-flash")
    @Published var fastSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-chat")
    @Published var asrSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-asr")
    @Published var ttsSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-tts", voice: "stella")
    @Published var testResultMessage: String = ""

    @Published var privacyMode: String = "meta_only"
    @Published var audioEnabled: Bool = true
    @Published var pttHotkey: String = "right_option"
    @Published var chatInputHotkey: String = "option+space"
    @Published var cancelHotkey: String = "option+esc"
    @Published var settingsHotkey: String = "option+k"
    @Published var toggleDockHotkey: String = "option+h"

    @Published var mcpServers: [MCPServerViewModel] = []

    func reload(brainClient: BrainClient) async {
        if #available(macOS 13.0, *), Bundle.main.bundleURL.pathExtension == "app" {
            launchAtLogin = SMAppService.mainApp.status == .enabled
        }
        if let settings = await brainClient.fetchSettings() {
            if let ui = settings["ui"] as? [String: Any] {
                avatarEnabled = (ui["avatar_enabled"] as? Bool) ?? false
                earconsEnabled = (ui["earcons"] as? Bool) ?? true
                roamEnabled = (ui["roam"] as? Bool) ?? true
                ambientEnabled = (ui["ambient"] as? Bool) ?? false
            }
            if let priv = settings["privacy"] as? [String: Any] {
                privacyMode = (priv["default_mode"] as? String) ?? "meta_only"
            }
            if let audio = settings["audio"] as? [String: Any] {
                audioEnabled = (audio["enabled"] as? Bool) ?? true
            }
            if let sc = settings["shortcuts"] as? [String: Any] {
                pttHotkey = (sc["ptt_hotkey"] as? String) ?? pttHotkey
                chatInputHotkey = (sc["chat_input_hotkey"] as? String) ?? chatInputHotkey
                cancelHotkey = (sc["cancel_hotkey"] as? String) ?? cancelHotkey
                settingsHotkey = (sc["settings_hotkey"] as? String) ?? settingsHotkey
                toggleDockHotkey = (sc["toggle_dock_hotkey"] as? String) ?? toggleDockHotkey
            }
            if let provMap = settings["providers"] as? [String: [String: Any]] {
                providers = provMap.keys.sorted().compactMap { key in
                    guard let p = provMap[key] else { return nil }
                    return ProviderViewModel(
                        name: (p["name"] as? String) ?? key,
                        label: (p["label"] as? String) ?? key,
                        protocolName: (p["protocol"] as? String) ?? "openai_compatible",
                        baseURL: (p["base_url"] as? String) ?? "",
                        authRequired: (p["auth_required"] as? Bool) ?? true,
                        hasKey: (p["has_key"] as? Bool) ?? false,
                        keyMasked: (p["key_masked"] as? String) ?? "",
                        models: (p["models"] as? [String]) ?? []
                    )
                }
            }
            if let mr = settings["model_routing"] as? [String: [String: Any]] {
                if let c = mr["chat"] {
                    chatSlot = SlotRouteViewModel(provider: (c["provider"] as? String) ?? "deepseek", model: (c["model"] as? String) ?? "")
                }
                if let v = mr["vision_ocr"] {
                    visionSlot = SlotRouteViewModel(provider: (v["provider"] as? String) ?? "deepseek", model: (v["model"] as? String) ?? "")
                }
                if let f = mr["fast_utility"] {
                    fastSlot = SlotRouteViewModel(provider: (f["provider"] as? String) ?? "deepseek", model: (f["model"] as? String) ?? "")
                }
                if let a = mr["asr"] {
                    asrSlot = SlotRouteViewModel(provider: (a["provider"] as? String) ?? "deepseek", model: (a["model"] as? String) ?? "")
                }
                if let t = mr["tts"] {
                    ttsSlot = SlotRouteViewModel(provider: (t["provider"] as? String) ?? "deepseek", model: (t["model"] as? String) ?? "", voice: (t["voice"] as? String) ?? "stella")
                }
            }
        }
        mcpServers = await brainClient.fetchMCPServers()
    }
}

private struct SettingsRootView: View {
    @ObservedObject var store: SettingsViewStore
    @ObservedObject var brainClient: BrainClient
    let supervisor: ProcessSupervisor
    let desktopActions: DesktopActions

    var body: some View {
        HStack(spacing: 0) {
            List(SettingsSection.allCases, selection: $store.selectedSection) { sec in
                Label(sec.rawValue, systemImage: sec.icon)
                    .tag(sec)
            }
            .listStyle(.sidebar)
            .frame(width: 190)

            Divider()

            VStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        switch store.selectedSection {
                        case .general:
                            generalPane
                        case .routing:
                            routingPane
                        case .voicePrivacy:
                            voicePrivacyPane
                        case .shortcuts:
                            shortcutsPane
                        case .mcp:
                            mcpPane
                        }
                    }
                    .padding(20)
                }

                Divider()
                HStack {
                    Text(store.statusMessage)
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Spacer()
                    Button("保存设置") {
                        Task { await saveAll() }
                    }
                    .buttonStyle(.borderedProminent)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
        }
    }

    private var generalPane: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("通用与外观").font(.title2.bold())
            GroupBox("运行与自启") {
                VStack(alignment: .leading, spacing: 10) {
                    Toggle("开机自动启动启明星", isOn: $store.launchAtLogin)
                        .onChange(of: store.launchAtLogin) { enabled in
                            if #available(macOS 13.0, *), Bundle.main.bundleURL.pathExtension == "app" {
                                if enabled {
                                    try? SMAppService.mainApp.register()
                                } else {
                                    try? SMAppService.mainApp.unregister()
                                }
                            }
                        }
                    Toggle("启用提示音效 (Earcons)", isOn: $store.earconsEnabled)
                    Toggle("启用可选桌面虚拟形象 (Godot 插件，默认关闭)", isOn: $store.avatarEnabled)
                    Text("提示：关闭虚拟形象时由原生 macOS 壳独立完成全部对话、审批、语音与通知。")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .padding(8)
            }

            GroupBox("macOS 隐私与系统权限 (TCC 归属 Qimingxing.app)") {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("屏幕录制 / 辅助功能 / 自动化")
                        Spacer()
                        Button("打开隐私设置") {
                            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy") {
                                NSWorkspace.shared.open(url)
                            }
                        }
                        Button("打开日志目录") {
                            desktopActions.openLogsDirectory()
                        }
                    }
                    if !brainClient.permissionHint.isEmpty {
                        Text(brainClient.permissionHint)
                            .font(.caption)
                            .foregroundColor(.orange)
                    }
                }
                .padding(8)
            }
        }
    }

    private var routingPane: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("模型与 5 槽位路由").font(.title2.bold())
            GroupBox("能力槽位映射") {
                VStack(spacing: 10) {
                    slotRow("主对话 (Chat)", slot: $store.chatSlot, showVoice: false)
                    slotRow("视觉理解 (Vision/OCR)", slot: $store.visionSlot, showVoice: false)
                    slotRow("后台快模型 (Fast/Utility)", slot: $store.fastSlot, showVoice: false)
                    slotRow("语音识别 (ASR)", slot: $store.asrSlot, showVoice: false)
                    slotRow("语音合成 (TTS)", slot: $store.ttsSlot, showVoice: true)
                }
                .padding(8)
            }

            GroupBox("服务商密钥池（仅保存脱敏占位保护，明文永不落盘壳本地）") {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach($store.providers) { $prov in
                        HStack(spacing: 8) {
                            Text(prov.label)
                                .frame(width: 90, alignment: .leading)
                            TextField("Base URL", text: $prov.baseURL)
                                .textFieldStyle(.roundedBorder)
                            if prov.authRequired {
                                SecureField(
                                    prov.hasKey ? "已配置 (\(prov.keyMasked))" : "输入新 API Key",
                                    text: $prov.draftApiKey
                                )
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 190)
                            }
                            Button("测连通") {
                                Task {
                                    let (_, msg) = await brainClient.testProvider([
                                        "provider_id": prov.name,
                                        "base_url": prov.baseURL,
                                        "api_key": prov.draftApiKey,
                                        "protocol": prov.protocolName
                                    ])
                                    store.testResultMessage = "\(prov.label): \(msg)"
                                }
                            }
                        }
                    }
                    if !store.testResultMessage.isEmpty {
                        Text(store.testResultMessage)
                            .font(.caption)
                            .foregroundColor(.accentColor)
                    }
                }
                .padding(8)
            }
        }
    }

    private func slotRow(_ title: String, slot: Binding<SlotRouteViewModel>, showVoice: Bool) -> some View {
        HStack {
            Text(title).frame(width: 165, alignment: .leading)
            Picker("", selection: slot.provider) {
                ForEach(store.providers) { p in
                    Text(p.label).tag(p.name)
                }
            }
            .labelsHidden()
            .frame(width: 130)
            TextField("Model ID", text: slot.model)
                .textFieldStyle(.roundedBorder)
            if showVoice {
                TextField("Voice", text: slot.voice)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 90)
            }
        }
    }

    private var voicePrivacyPane: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("语音与隐私守卫").font(.title2.bold())
            GroupBox("桌面隐私级别") {
                Picker("默认感知模式", selection: $store.privacyMode) {
                    Text("关闭桌面感知 (off)").tag("off")
                    Text("仅窗口元数据 (meta_only)").tag("meta_only")
                    Text("按需看屏 (full_on_demand)").tag("full_on_demand")
                }
                .pickerStyle(.radioGroup)
                .padding(8)
            }
            GroupBox("流式语音闭环") {
                Toggle("启用 ASR 语音输入与 TTS 语音播报", isOn: $store.audioEnabled)
                    .padding(8)
            }
        }
    }

    private var shortcutsPane: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("快捷键配置").font(.title2.bold())
            GroupBox("壳内直达快捷键（Brain 重启期依然秒开）与 PTT") {
                VStack(spacing: 10) {
                    shortcutField("呼出 Quick Entry", text: $store.chatInputHotkey)
                    shortcutField("打开设置中心", text: $store.settingsHotkey)
                    shortcutField("紧急停止当前任务", text: $store.cancelHotkey)
                    shortcutField("按住说话 (PTT，由 Senses 监听)", text: $store.pttHotkey)
                }
                .padding(8)
            }
        }
    }

    private func shortcutField(_ label: String, text: Binding<String>) -> some View {
        HStack {
            Text(label).frame(width: 220, alignment: .leading)
            TextField("", text: text).textFieldStyle(.roundedBorder)
        }
    }

    private var mcpPane: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("MCP 扩展服务").font(.title2.bold())
                Spacer()
                Button("刷新") {
                    Task { store.mcpServers = await brainClient.fetchMCPServers() }
                }
            }
            if store.mcpServers.isEmpty {
                Text("当前未配置 MCP Server。可通过 config.yaml 或 POST /api/mcp/servers 添加。")
                    .font(.caption)
                    .foregroundColor(.secondary)
            } else {
                ForEach(store.mcpServers) { srv in
                    GroupBox {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("\(srv.name) · \(srv.transport)")
                                    .font(.headline)
                                Text("状态: \(srv.status) \(srv.error)")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            Spacer()
                            if srv.authMode == "oauth" {
                                Button(srv.hasOAuthToken ? "重新 OAuth 授权" : "OAuth 授权") {
                                    Task {
                                        let (_, msg, authURL) = await brainClient.authorizeMCPServer(name: srv.name)
                                        store.statusMessage = msg
                                        if let authURL, let u = URL(string: authURL) {
                                            NSWorkspace.shared.open(u)
                                        }
                                        store.mcpServers = await brainClient.fetchMCPServers()
                                    }
                                }
                            }
                            Button("删除") {
                                Task {
                                    await brainClient.deleteMCPServer(name: srv.name)
                                    store.mcpServers = await brainClient.fetchMCPServers()
                                }
                            }
                        }
                        .padding(6)
                    }
                }
            }
        }
    }

    private func saveAll() async {
        var provPatch: [String: Any] = [:]
        for p in store.providers {
            var entry: [String: Any] = [
                "label": p.label,
                "protocol": p.protocolName,
                "base_url": p.baseURL,
                "auth_required": p.authRequired,
                "models": p.models
            ]
            if !p.draftApiKey.isEmpty {
                entry["api_key"] = p.draftApiKey
            }
            provPatch[p.name] = entry
        }

        let payload: [String: Any] = [
            "ui": [
                "avatar_enabled": store.avatarEnabled,
                "earcons": store.earconsEnabled,
                "roam": store.roamEnabled,
                "ambient": store.ambientEnabled
            ],
            "privacy": ["default_mode": store.privacyMode],
            "audio": ["enabled": store.audioEnabled],
            "shortcuts": [
                "ptt_hotkey": store.pttHotkey,
                "chat_input_hotkey": store.chatInputHotkey,
                "cancel_hotkey": store.cancelHotkey,
                "settings_hotkey": store.settingsHotkey,
                "toggle_dock_hotkey": store.toggleDockHotkey
            ],
            "providers": provPatch,
            "model_routing": [
                "chat": ["provider": store.chatSlot.provider, "model": store.chatSlot.model],
                "vision_ocr": ["provider": store.visionSlot.provider, "model": store.visionSlot.model],
                "fast_utility": ["provider": store.fastSlot.provider, "model": store.fastSlot.model],
                "asr": ["provider": store.asrSlot.provider, "model": store.asrSlot.model],
                "tts": ["provider": store.ttsSlot.provider, "model": store.ttsSlot.model, "voice": store.ttsSlot.voice]
            ]
        ]

        let (ok, msg) = await brainClient.saveSettings(payload)
        store.statusMessage = msg
        if ok {
            brainClient.setUIStatePatch(["avatar_enabled": store.avatarEnabled, "earcons": store.earconsEnabled])
            await store.reload(brainClient: brainClient)
        }
    }
}

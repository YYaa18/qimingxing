import AppKit
import Luminare
import ServiceManagement
import SwiftUI

enum SettingsSection: String, LuminareTabItem, CaseIterable, Identifiable, Hashable {
    case general = "通用与外观"
    case routing = "模型与路由"
    case voicePrivacy = "语音与隐私"
    case shortcuts = "快捷键"
    case mcp = "MCP 服务"

    var id: Self { self }
    var title: String { rawValue }

    var icon: some View {
        Image(systemName: systemIconName)
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(QMXTheme.accent)
    }

    private var systemIconName: String {
        switch self {
        case .general: return "gearshape.fill"
        case .routing: return "cpu.fill"
        case .voicePrivacy: return "waveform.and.mic"
        case .shortcuts: return "command"
        case .mcp: return "puzzlepiece.extension.fill"
        }
    }
}

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private var window: LuminareWindow?
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
        let win = LuminareWindow(titleBarButtonConfiguration: .default) {
            LuminareView {
                SettingsRootView(
                    store: self.store,
                    brainClient: self.brainClient,
                    supervisor: self.supervisor,
                    desktopActions: self.desktopActions
                )
            }
            .environment(\.luminareTintColor, QMXTheme.accent)
            .environment(\.luminareTitleBarHeight, 44)
        }
        win.title = "启明星设置中心"
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
    @Published var isSaving: Bool = false

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
        LuminareDividedStack {
            // Left Luminare Sidebar
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    Image(systemName: "sparkles")
                        .foregroundStyle(QMXTheme.accent)
                    Text("启明星")
                        .font(.system(size: 14, weight: .semibold))
                    Spacer()
                    Text("v\(supervisor.appVersion)")
                        .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.primary.opacity(0.07)))
                }
                .padding(.horizontal, 16)
                .padding(.top, 38)
                .padding(.bottom, 10)

                LuminareSidebar {
                    LuminareSidebarSection(
                        "核心设置",
                        selection: $store.selectedSection,
                        items: [.general, .routing, .voicePrivacy]
                    )
                    LuminareSidebarSection(
                        "效率与扩展",
                        selection: $store.selectedSection,
                        items: [.shortcuts, .mcp]
                    )
                }
            }
            .frame(width: 215)

            // Right Luminare Pane
            LuminarePane {
                VStack(spacing: 0) {
                    LuminareForm {
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
                    .luminareFormLayout(.stacked)

                    Divider()

                    HStack(spacing: 12) {
                        if !store.statusMessage.isEmpty {
                            Label(store.statusMessage, systemImage: "checkmark.circle.fill")
                                .font(.caption)
                                .foregroundStyle(QMXTheme.accent)
                        } else {
                            Text("配置以 Brain 为唯一状态源（0600 安全落盘）")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button {
                            Task { await saveAll() }
                        } label: {
                            HStack(spacing: 6) {
                                if store.isSaving {
                                    ProgressView().scaleEffect(0.6)
                                }
                                Text(store.isSaving ? "保存中…" : "保存设置")
                            }
                            .padding(.horizontal, 14)
                            .padding(.vertical, 6)
                        }
                        .buttonStyle(.luminare(tinted: true))
                        .disabled(store.isSaving)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                }
            } header: {
                HStack(spacing: 8) {
                    store.selectedSection.icon
                    Text(store.selectedSection.title)
                        .font(.system(size: 14, weight: .semibold))
                    Spacer()
                    HStack(spacing: 6) {
                        Circle()
                            .fill(brainClient.phase == .connected ? QMXTheme.success : QMXTheme.warning)
                            .frame(width: 7, height: 7)
                        Text(brainClient.phase == .connected ? "Brain 已连接" : "离线重连中")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(width: 615)
        }
        .frame(height: 560)
    }

    // MARK: - Section Panes

    private var generalPane: some View {
        Group {
            LuminareSection("运行与外观", "关闭桌面虚拟形象时，由 Swift 原生壳独立完成对话、审批、语音与通知。") {
                LuminareToggle("开机自动启动启明星", isOn: $store.launchAtLogin)
                    .onChange(of: store.launchAtLogin) { enabled in
                        if #available(macOS 13.0, *), Bundle.main.bundleURL.pathExtension == "app" {
                            if enabled {
                                try? SMAppService.mainApp.register()
                            } else {
                                try? SMAppService.mainApp.unregister()
                            }
                        }
                    }

                LuminareToggle("启用提示音效 (Earcons)", isOn: $store.earconsEnabled)
                LuminareToggle("启用可选桌面虚拟形象 (Godot 插件)", isOn: $store.avatarEnabled)
            }

            LuminareSection("macOS 隐私与系统权限 (TCC)", "屏幕录制、麦克风与自动化权限统一归属 Qimingxing.app。") {
                LuminareButton("系统隐私设置", "打开权限面板") {
                    if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy") {
                        NSWorkspace.shared.open(url)
                    }
                }
                LuminareButton("运行日志", "在访达中打开") {
                    desktopActions.openLogsDirectory()
                }
                if !brainClient.permissionHint.isEmpty {
                    LuminareCompose("权限提示") {
                        Text(brainClient.permissionHint)
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
            }
        }
    }

    private var routingPane: some View {
        Group {
            LuminareSection("5 槽位能力路由", "可为对话、看图 OCR、后台快任务、ASR 与 TTS 分别指定最适合的模型。") {
                slotRow("主对话 (Chat)", slot: $store.chatSlot, showVoice: false)
                slotRow("视觉理解 (Vision/OCR)", slot: $store.visionSlot, showVoice: false)
                slotRow("后台与快模型 (Fast)", slot: $store.fastSlot, showVoice: false)
                slotRow("语音识别 (ASR)", slot: $store.asrSlot, showVoice: false)
                slotRow("语音合成 (TTS)", slot: $store.ttsSlot, showVoice: true)
            }

            LuminareSection("服务商密钥池", "仅显示脱敏占位，未修改密钥时保存不会覆盖原有密钥。") {
                ForEach($store.providers) { $prov in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(spacing: 8) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(prov.label)
                                    .font(.system(size: 13, weight: .semibold))
                                Text(prov.protocolName)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            .frame(width: 96, alignment: .leading)

                            TextField("Base URL", text: $prov.baseURL)
                                .textFieldStyle(.roundedBorder)

                            if prov.authRequired {
                                SecureField(
                                    prov.hasKey ? "已配置 (\(prov.keyMasked))" : "输入 API Key",
                                    text: $prov.draftApiKey
                                )
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 175)
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
                            .buttonStyle(.luminare)
                        }
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                }

                if !store.testResultMessage.isEmpty {
                    LuminareCompose("连通探测结果") {
                        Text(store.testResultMessage)
                            .font(.caption)
                            .foregroundStyle(QMXTheme.accent)
                    }
                }
            }
        }
    }

    private func slotRow(_ title: String, slot: Binding<SlotRouteViewModel>, showVoice: Bool) -> some View {
        let providerIds = store.providers.map(\.name)
        return LuminareCompose(title) {
            HStack(spacing: 8) {
                if !providerIds.isEmpty {
                    Picker("", selection: slot.provider) {
                        ForEach(store.providers) { p in
                            Text(p.label).tag(p.name)
                        }
                    }
                    .labelsHidden()
                    .frame(width: 120)
                }
                TextField("Model ID", text: slot.model)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: showVoice ? 150 : 210)
                if showVoice {
                    TextField("Voice", text: slot.voice)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 75)
                }
            }
        }
    }

    private var voicePrivacyPane: some View {
        Group {
            LuminareSection("桌面感知隐私守卫", "敏感密码与网银窗口在 Senses 侧强制脱敏且拦截截屏。") {
                LuminarePicker(
                    compactElements: ["off", "meta_only", "full_on_demand"],
                    selection: $store.privacyMode
                ) { mode in
                    VStack(spacing: 4) {
                        Image(systemName: privacyIcon(mode))
                            .font(.system(size: 14))
                        Text(privacyTitle(mode))
                            .font(.caption.bold())
                    }
                    .padding(.vertical, 8)
                }
                .luminareRoundingBehavior(top: true, bottom: true)
            }

            LuminareSection("流式语音闭环") {
                LuminareToggle("启用 ASR 语音输入与流式 TTS 语音播报", isOn: $store.audioEnabled)
            }
        }
    }

    private var shortcutsPane: some View {
        LuminareSection("全局与壳内直达快捷键", "Quick Entry、设置中心与急停由 Swift 壳本地注册，Brain 重启期间依然可用。") {
            shortcutRow("呼出 Quick Entry", value: $store.chatInputHotkey)
            shortcutRow("打开设置中心", value: $store.settingsHotkey)
            shortcutRow("紧急停止当前任务", value: $store.cancelHotkey)
            shortcutRow("按住说话 (PTT · Senses)", value: $store.pttHotkey)
        }
    }

    private func shortcutRow(_ title: String, value: Binding<String>) -> some View {
        LuminareCompose(title) {
            TextField("", text: value)
                .textFieldStyle(.roundedBorder)
                .frame(width: 180)
                .multilineTextAlignment(.trailing)
                .font(.system(.body, design: .monospaced))
        }
    }

    private var mcpPane: some View {
        LuminareSection("已挂载 MCP Server", "所有第三方 MCP 写入或未知工具默认触发原生审批弹窗。") {
            LuminareButton("服务列表", "立即刷新") {
                Task { store.mcpServers = await brainClient.fetchMCPServers() }
            }

            if store.mcpServers.isEmpty {
                LuminareCompose("状态") {
                    Text("当前未配置 MCP Server")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else {
                ForEach(store.mcpServers) { srv in
                    LuminareCompose {
                        HStack(spacing: 8) {
                            if srv.authMode == "oauth" {
                                Button(srv.hasOAuthToken ? "重新授权" : "OAuth 授权") {
                                    Task {
                                        let (_, msg, authURL) = await brainClient.authorizeMCPServer(name: srv.name)
                                        store.statusMessage = msg
                                        if let authURL, let u = URL(string: authURL) {
                                            NSWorkspace.shared.open(u)
                                        }
                                        store.mcpServers = await brainClient.fetchMCPServers()
                                    }
                                }
                                .buttonStyle(.luminare)
                            }
                            Button("移除", role: .destructive) {
                                Task {
                                    await brainClient.deleteMCPServer(name: srv.name)
                                    store.mcpServers = await brainClient.fetchMCPServers()
                                }
                            }
                            .buttonStyle(.luminare)
                        }
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(srv.name) · \(srv.transport)")
                                .font(.system(size: 13, weight: .semibold))
                            Text("\(srv.status) \(srv.error)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private func privacyIcon(_ mode: String) -> String {
        switch mode {
        case "off": return "eye.slash"
        case "meta_only": return "macwindow"
        default: return "display"
        }
    }

    private func privacyTitle(_ mode: String) -> String {
        switch mode {
        case "off": return "关闭感知"
        case "meta_only": return "仅窗口信息"
        default: return "按需看屏"
        }
    }

    private func saveAll() async {
        store.isSaving = true
        defer { store.isSaving = false }

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

import AppKit
import ServiceManagement
import SwiftUI

enum SettingsSection: String, CaseIterable, Identifiable, Hashable {
    case general = "通用与外观"
    case routing = "模型与路由"
    case voicePrivacy = "语音与隐私"
    case shortcuts = "快捷键"
    case mcp = "MCP 服务"

    var id: Self { self }
    var title: String { rawValue }

    var iconName: String {
        switch self {
        case .general: return "gearshape"
        case .routing: return "cpu"
        case .voicePrivacy: return "waveform.and.mic"
        case .shortcuts: return "command"
        case .mcp: return "puzzlepiece.extension"
        }
    }

    var subtitle: String {
        switch self {
        case .general: return "启动、外观与系统权限"
        case .routing: return "按能力分配模型与服务商"
        case .voicePrivacy: return "隐私模式与语音闭环"
        case .shortcuts: return "全局与壳内快捷键"
        case .mcp: return "第三方 MCP Server"
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
            desktopActions: desktopActions,
            onClose: { [weak self] in self?.window?.orderOut(nil) }
        )

        let hosting = NSHostingController(rootView: root)
        let win = NSWindow(contentViewController: hosting)
        win.title = "启明星设置"
        win.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        win.titlebarAppearsTransparent = true
        win.titleVisibility = .hidden
        win.isMovableByWindowBackground = true
        win.setContentSize(NSSize(width: 860, height: 580))
        win.minSize = NSSize(width: 760, height: 520)
        win.center()
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

struct SettingsRootView: View {
    @AppStorage("qmx.answerFontSize") private var answerFontSize: Double = 16
    @AppStorage("qmx.answerMaxLines") private var answerMaxLines: Int = 30
    @ObservedObject var store: SettingsViewStore
    @ObservedObject var brainClient: BrainClient
    let supervisor: ProcessSupervisor
    let desktopActions: DesktopActions
    let onClose: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider().opacity(0.5)
            detail
        }
        .frame(minWidth: 760, minHeight: 520)
        .background(QMXTheme.window)
    }

    private var sidebar: some View {
        ZStack {
            VisualEffectBlur(material: .sidebar, blendingMode: .behindWindow)
            VStack(alignment: .leading, spacing: 0) {
                brand
                navSection("核心设置", items: [.general, .routing, .voicePrivacy])
                navSection("效率与扩展", items: [.shortcuts, .mcp])
                Spacer(minLength: 0)
            }
        }
        .frame(width: 210)
    }

    private var detail: some View {
        ZStack {
            VisualEffectBlur(material: .underWindowBackground, blendingMode: .behindWindow)
            VStack(spacing: 0) {
                detailHeader
                Form {
                    switch store.selectedSection {
                    case .general: generalPane
                    case .routing: routingPane
                    case .voicePrivacy: voicePrivacyPane
                    case .shortcuts: shortcutsPane
                    case .mcp: mcpPane
                    }
                }
                .formStyle(.grouped)
                .scrollContentBackground(.hidden)
                .environment(\.defaultMinListRowHeight, 38)

                Divider().opacity(0.5)
                footer
            }
        }
    }

    private var brand: some View {
        HStack(spacing: 8) {
            MorningStarMark(size: 16, color: QMXTheme.textPrimary)
            Text("启明星")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(QMXTheme.textPrimary)
            Text("设置")
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textTertiary)
            Spacer()
        }
        .padding(.horizontal, 18)
        .padding(.top, 38)
        .padding(.bottom, 14)
        .help("启明星 v\(supervisor.appVersion)")
    }

    private func navSection(_ title: String, items: [SettingsSection]) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(QMXTheme.textTertiary)
                .padding(.horizontal, 18)
                .padding(.top, 14)
                .padding(.bottom, 4)

            ForEach(items) { item in
                navButton(item)
            }
        }
        .padding(.horizontal, 8)
    }

    private func navButton(_ item: SettingsSection) -> some View {
        let selected = store.selectedSection == item
        return Button {
            store.selectedSection = item
        } label: {
            HStack(spacing: 10) {
                Image(systemName: item.iconName)
                    .font(.system(size: 14, weight: .regular))
                    .foregroundStyle(selected ? QMXTheme.textPrimary : QMXTheme.textSecondary)
                    .frame(width: 18, alignment: .center)
                Text(item.title)
                    .font(.system(size: 13))
                    .foregroundStyle(selected ? QMXTheme.textPrimary : QMXTheme.textSecondary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .frame(height: 40)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(selected ? QMXTheme.surfaceRaised : .clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .strokeBorder(selected ? QMXTheme.hairline : .clear, lineWidth: 0.5)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var detailHeader: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(store.selectedSection.title)
                    .font(.system(size: 24, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                Text(store.selectedSection.subtitle)
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textSecondary)
            }
            Spacer()
            HStack(spacing: 6) {
                StatusDot(color: brainClient.phase == .connected ? QMXTheme.success : QMXTheme.warning, size: 6)
                Text(brainClient.phase == .connected ? "已连接" : "重连中")
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textSecondary)
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 18)
        .padding(.bottom, 10)
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Image(systemName: "lock")
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textTertiary)
            Text(store.statusMessage.isEmpty ? "本地 0600 安全存储（密钥已脱敏）" : store.statusMessage)
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textSecondary)
                .lineLimit(1)
            Spacer()
            Button("关闭", action: onClose)
                .buttonStyle(.qmxSecondary)
            Button {
                Task { await saveAll() }
            } label: {
                HStack(spacing: 6) {
                    if store.isSaving {
                        ProgressView().scaleEffect(0.55)
                    }
                    Text(store.isSaving ? "保存中" : "保存")
                }
            }
            .buttonStyle(.qmxProminent)
            .disabled(store.isSaving)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
    }

    // MARK: - Panes

    private var generalPane: some View {
        Group {
            Section("运行与外观") {
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
                    .frame(minHeight: 40)

                Toggle("状态提示音", isOn: $store.earconsEnabled)
                    .frame(minHeight: 40)
                Toggle("可选桌面头像插件", isOn: $store.avatarEnabled)
                    .frame(minHeight: 40)
            }

            Section("回答显示") {
                Picker("回答字体大小", selection: $answerFontSize) {
                    Text("小 · 14").tag(14.0)
                    Text("标准 · 16").tag(16.0)
                    Text("大 · 18").tag(18.0)
                    Text("特大 · 20").tag(20.0)
                }
                .pickerStyle(.segmented)
                .frame(minHeight: 40)

                Text("工作区已开好了，你现在把窗口放在前台。")
                    .font(.system(size: answerFontSize))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 6)
                    .help("回答在 Quick Entry 中的显示字号")

                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("回答超过 \(answerMaxLines) 行后固定高度")
                            .font(.system(size: 13))
                        Spacer()
                        Text("滚动查看")
                            .font(.system(size: 11))
                            .foregroundStyle(QMXTheme.textTertiary)
                    }
                    Slider(
                        value: Binding(
                            get: { Double(answerMaxLines) },
                            set: { answerMaxLines = Int($0.rounded()) }
                        ),
                        in: 5...80,
                        step: 1
                    )
                }
                .padding(.vertical, 4)
                .help("不超过该行数时完整展示；超过后固定高度并允许滚动。")
            }

            Section("系统权限") {
                HStack {
                    Text("屏幕录制、麦克风与自动化权限")
                        .font(.system(size: 13))
                    Spacer()
                    Button("打开系统设置") {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                    .buttonStyle(.qmxSecondary)
                }
                .frame(minHeight: 40)

                HStack {
                    Text("运行日志")
                        .font(.system(size: 13))
                    Spacer()
                    Button("在访达中打开") {
                        desktopActions.openLogsDirectory()
                    }
                    .buttonStyle(.qmxSecondary)
                }
                .frame(minHeight: 40)

                if !brainClient.permissionHint.isEmpty {
                    Text(brainClient.permissionHint)
                        .font(.system(size: 12))
                        .foregroundStyle(QMXTheme.warning)
                }
            }

        }
    }

    private var routingPane: some View {
        Group {
            Section("能力路由") {
                slotRow("对话", slot: $store.chatSlot, showVoice: false)
                slotRow("看屏 / OCR", slot: $store.visionSlot, showVoice: false)
                slotRow("后台快任务", slot: $store.fastSlot, showVoice: false)
                slotRow("语音识别", slot: $store.asrSlot, showVoice: false)
                slotRow("语音合成", slot: $store.ttsSlot, showVoice: true)
            }

            Section("服务商") {
                ForEach($store.providers) { $prov in
                    HStack(spacing: 8) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 5) {
                                StatusDot(
                                    color: prov.hasKey
                                        ? QMXTheme.success
                                        : (prov.authRequired ? QMXTheme.warning : QMXTheme.textTertiary),
                                    size: 5
                                )
                                Text(prov.label)
                                    .font(.system(size: 13, weight: .medium))
                            }
                            Text(prov.protocolName)
                                .font(.system(size: 11))
                                .foregroundStyle(QMXTheme.textTertiary)
                                .lineLimit(1)
                        }
                        .frame(width: 92, alignment: .leading)

                        TextField("", text: $prov.baseURL)
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder)

                        if prov.authRequired {
                            SecureField(
                                "",
                                text: $prov.draftApiKey,
                                prompt: Text(prov.hasKey ? "已配置 \(prov.keyMasked)" : "API Key")
                            )
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 160)
                        }

                        Button("测连通") {
                            Task {
                                let (_, msg) = await brainClient.testProvider([
                                    "provider_id": prov.name,
                                    "base_url": prov.baseURL,
                                    "api_key": prov.draftApiKey,
                                    "protocol": prov.protocolName
                                ])
                                store.testResultMessage = "\(prov.label)：\(msg)"
                            }
                        }
                        .buttonStyle(.qmxSecondary)
                    }
                    .frame(minHeight: 40)
                }

                if !store.testResultMessage.isEmpty {
                    Text(store.testResultMessage)
                        .font(.system(size: 12))
                        .foregroundStyle(QMXTheme.textSecondary)
                }
            }
        }
    }

    private func slotRow(_ title: String, slot: Binding<SlotRouteViewModel>, showVoice: Bool) -> some View {
        HStack(spacing: 8) {
            Text(title)
                .font(.system(size: 13))
                .frame(width: 100, alignment: .leading)

            Picker("", selection: slot.provider) {
                ForEach(store.providers) { p in
                    Text(p.label).tag(p.name)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(width: 140)

            TextField("", text: slot.model)
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .frame(width: showVoice ? 140 : 200)

            if showVoice {
                TextField("", text: slot.voice)
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 80)
            }
            Spacer(minLength: 0)
        }
        .frame(minHeight: 40)
    }

    private var voicePrivacyPane: some View {
        Group {
            Section("隐私") {
                Picker("隐私模式", selection: $store.privacyMode) {
                    Text("关闭").tag("off")
                    Text("仅窗口信息").tag("meta_only")
                    Text("按需看屏").tag("full_on_demand")
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 320)
                .frame(minHeight: 40)
            }

            Section("语音") {
                Toggle("启用语音输入与播报", isOn: $store.audioEnabled)
                    .frame(minHeight: 40)
                Toggle("朗读回答", isOn: Binding(
                    get: { brainClient.ttsEnabled },
                    set: { brainClient.setTTSEnabled($0) }
                ))
                .frame(minHeight: 40)
                .help("关闭后会停止当前播报，并静音后续回答；语音输入不受影响。")
            }
        }
    }

    private var shortcutsPane: some View {
        Section("快捷键") {
            shortcutRow("呼出对话", value: $store.chatInputHotkey)
            shortcutRow("打开设置", value: $store.settingsHotkey)
            shortcutRow("紧急停止", value: $store.cancelHotkey)
            shortcutRow("按住说话（PTT）", value: $store.pttHotkey)
            shortcutRow("贴边 / 取消贴边", value: $store.toggleDockHotkey)
        }
    }

    private func shortcutRow(_ title: String, value: Binding<String>) -> some View {
        HStack {
            Text(title)
                .font(.system(size: 13))
            Spacer()
            TextField("", text: value)
                .textFieldStyle(.roundedBorder)
                .frame(width: 180)
                .font(.system(size: 12, design: .monospaced))
        }
        .frame(minHeight: 40)
    }

    private var mcpPane: some View {
        Section("MCP Server") {
            HStack {
                Text("已挂载 \(store.mcpServers.count) 个服务")
                    .font(.system(size: 13))
                Spacer()
                Button("刷新") {
                    Task { store.mcpServers = await brainClient.fetchMCPServers() }
                }
                .buttonStyle(.qmxSecondary)
            }
            .frame(minHeight: 40)

            if store.mcpServers.isEmpty {
                Text("当前未配置 MCP Server")
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textSecondary)
            } else {
                ForEach(store.mcpServers) { srv in
                    HStack(spacing: 8) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(srv.name) · \(srv.transport)")
                                .font(.system(size: 13, weight: .medium))
                            Text(srv.error.isEmpty ? srv.status : "\(srv.status) \(srv.error)")
                                .font(.system(size: 11))
                                .foregroundStyle(QMXTheme.textTertiary)
                                .lineLimit(1)
                        }
                        Spacer()
                        if srv.authMode == "oauth" {
                            Button(srv.hasOAuthToken ? "重新授权" : "授权") {
                                Task {
                                    let (_, msg, authURL) = await brainClient.authorizeMCPServer(name: srv.name)
                                    store.statusMessage = msg
                                    if let authURL, let u = URL(string: authURL) {
                                        NSWorkspace.shared.open(u)
                                    }
                                    store.mcpServers = await brainClient.fetchMCPServers()
                                }
                            }
                            .buttonStyle(.qmxSecondary)
                        }
                        Button("移除") {
                            Task {
                                await brainClient.deleteMCPServer(name: srv.name)
                                store.mcpServers = await brainClient.fetchMCPServers()
                            }
                        }
                        .buttonStyle(.qmxDestructive)
                    }
                    .frame(minHeight: 40)
                }
            }
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

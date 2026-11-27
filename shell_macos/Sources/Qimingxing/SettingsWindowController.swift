import AppKit
import ServiceManagement
import SwiftUI

enum SettingsSection: String, CaseIterable, Identifiable, Hashable {
    case general = "通用与外观"
    case routing = "模型与路由"
    case voicePrivacy = "语音与隐私"
    case shortcuts = "快捷键"
    case browser = "浏览器自动化"
    case mcp = "MCP 服务"

    var id: Self { self }
    var title: String { rawValue }

    var iconName: String {
        switch self {
        case .general: return "gearshape"
        case .routing: return "cpu"
        case .voicePrivacy: return "waveform.and.mic"
        case .shortcuts: return "command"
        case .browser: return "safari"
        case .mcp: return "puzzlepiece.extension"
        }
    }

    var subtitle: String {
        switch self {
        case .general: return "启动、外观与系统权限"
        case .routing: return "按能力分配模型与服务商"
        case .voicePrivacy: return "隐私模式与语音闭环"
        case .shortcuts: return "全局与壳内快捷键"
        case .browser: return "隔离浏览器与 live 接管"
        case .mcp: return "第三方 MCP Server"
        }
    }
}

private enum RouteSlotKind {
    case chat
    case vision
    case fast
    case asr
    case tts
}

private let chatExcludedTokens = [
    "ocr", "asr", "tts", "embedding", "whisper", "transcribe",
    "sensevoice", "cosyvoice", "audio", "image", "live",
    "computer-use", "deep-research", "antigravity", "translate"
]
private let audioExcludedTokens = [
    "asr", "tts", "whisper", "transcribe", "sensevoice", "cosyvoice",
    "audio", "embedding", "live"
]
private let asrModelTokens = ["asr", "whisper", "transcribe", "sensevoice"]
private let ttsModelTokens = ["tts", "speech", "voice", "cosyvoice"]

private func modelMatches(_ model: String, anyOf tokens: [String]) -> Bool {
    let lower = model.lowercased()
    return tokens.contains { lower.contains($0) }
}

private func isChatCapableModel(_ model: String) -> Bool {
    !modelMatches(model, anyOf: chatExcludedTokens)
}

private func compatibleModels(_ models: [String], kind: RouteSlotKind) -> [String] {
    models.filter { model in
        switch kind {
        case .chat, .fast:
            return isChatCapableModel(model)
        case .vision:
            return !modelMatches(model, anyOf: audioExcludedTokens)
        case .asr:
            return modelMatches(model, anyOf: asrModelTokens)
        case .tts:
            return modelMatches(model, anyOf: ttsModelTokens)
        }
    }
}

private func modelSortRank(_ model: String) -> Int {
    let lower = model.lowercased()
    if lower.hasPrefix("gemini-") || lower.hasPrefix("gpt-") {
        let prefix = lower.hasPrefix("gemini-") ? "gemini-" : "gpt-"
        let rest = lower.dropFirst(prefix.count)
        let numberPart = rest.prefix { $0.isNumber || $0 == "." }
        let version = Double(numberPart) ?? 0
        var rank = Int((10.0 - version) * 100)
        if rest.contains("carbon") {
            rank += 0
        } else if rest.contains("neon") {
            rank += 1
        } else if rest.contains("hydrogen") {
            rank += 2
        } else if rest.contains("argon") {
            rank += 3
        } else if rest.contains("mini") || rest.contains("flash") {
            rank += 10
        } else {
            rank += 5
        }
        return max(0, rank)
    }
    if lower.contains("deepseek-flash") { return 0 }
    if lower.contains("deepseek-v4-pro") { return 1 }
    if lower.hasPrefix("deepseek-") { return 5 }
    return 1000
}

private func orderedModels(_ models: [String]) -> [String] {
    models.sorted { lhs, rhs in
        let l = modelSortRank(lhs)
        let r = modelSortRank(rhs)
        if l != r { return l < r }
        return lhs.localizedStandardCompare(rhs) == .orderedAscending
    }
}

private func preferredDefaultModel(_ models: [String], kind: RouteSlotKind) -> String {
    guard !models.isEmpty else { return "" }
    let preference: [String]
    switch kind {
    case .chat, .fast:
        // Gemini 4 Hydrogen is the vendor's default general-purpose model;
        // DeepSeek flash is preferred for cost/latency.
        preference = ["hydrogen", "carbon", "neon", "flash", "pro", "gpt-4o"]
    case .vision:
        preference = ["carbon", "neon", "hydrogen", "flash", "pro", "ocr"]
    case .asr:
        preference = ["asr", "whisper", "transcribe", "sensevoice"]
    case .tts:
        preference = ["tts", "speech", "voice", "cosyvoice", "audio"]
    }
    for needle in preference {
        if let match = models.first(where: { $0.lowercased().contains(needle) }) {
            return match
        }
    }
    return models[0]
}

private func normalizedSlot(
    _ slot: SlotRouteViewModel,
    kind: RouteSlotKind,
    providers: [ProviderViewModel]
) -> SlotRouteViewModel {
    guard let provider = providers.first(where: { $0.name == slot.provider }) else {
        return SlotRouteViewModel(provider: "", model: "", voice: slot.voice)
    }
    let options = compatibleModels(provider.models, kind: kind)
    if options.contains(slot.model) {
        return slot
    }
    return SlotRouteViewModel(
        provider: provider.name,
        model: preferredDefaultModel(options, kind: kind),
        voice: slot.voice
    )
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
        case "browser": return .browser
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
    @Published var workerSlot = SlotRouteViewModel(provider: "", model: "")
    @Published var workerFollowsChat = true
    @Published var autoResumeTasks = true
    @Published var asrSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-asr")
    @Published var ttsSlot = SlotRouteViewModel(provider: "deepseek", model: "deepseek-tts", voice: "stella")
    @Published var privacyMode: String = "meta_only"
    @Published var audioEnabled: Bool = true
    @Published var deepseekThinking: String = "auto"
    @Published var deepseekEffort: String = ""
    @Published var deepseekNativeWeb: Bool = true
    @Published var deepseekSafety: Bool = false
    @Published var geminiThinkingLevel: String = "auto"
    @Published var geminiGoogleSearch: Bool = false
    @Published var geminiURLContext: Bool = false
    @Published var imageSearchEnabled: Bool = true
    @Published var imageSearchKey: String = ""
    @Published var imageSearchHasKey: Bool = false
    @Published var imageSearchMasked: String = ""
    @Published var pttHotkey: String = "right_option"
    @Published var chatInputHotkey: String = "option+space"
    @Published var cancelHotkey: String = "option+esc"
    @Published var settingsHotkey: String = "option+k"
    @Published var toggleDockHotkey: String = "option+h"

    @Published var mcpServers: [MCPServerViewModel] = []

    private static func parseModelCapabilities(
        _ raw: [String: [String: Any]]
    ) -> [String: ModelCapabilityViewModel] {
        raw.reduce(into: [:]) { result, entry in
            let value = entry.value
            result[entry.key] = ModelCapabilityViewModel(
                contextWindow: (value["context_window"] as? Int) ?? 0,
                maxOutputTokens: (value["max_output_tokens"] as? Int) ?? 0,
                supportsTools: (value["supports_tools"] as? Bool) ?? true,
                supportsVision: (value["supports_vision"] as? Bool) ?? false,
                supportsCache: (value["supports_cache"] as? Bool) ?? false,
                supportsReasoning: (value["supports_reasoning"] as? Bool) ?? false,
                isPreview: (value["is_preview"] as? Bool) ?? false,
                source: (value["source"] as? String) ?? "manual"
            )
        }
    }

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
            if let ds = settings["deepseek"] as? [String: Any] {
                deepseekThinking = (ds["thinking"] as? String) ?? "auto"
                deepseekEffort = (ds["reasoning_effort"] as? String) ?? ""
                deepseekNativeWeb = (ds["native_web_tools"] as? Bool) ?? true
                deepseekSafety = (ds["safety_check"] as? Bool) ?? false
            }
            if let gem = settings["gemini"] as? [String: Any] {
                geminiThinkingLevel = (gem["thinking_level"] as? String) ?? "auto"
                geminiGoogleSearch = (gem["google_search"] as? Bool) ?? false
                geminiURLContext = (gem["url_context"] as? Bool) ?? false
            }
            if let img = settings["image_search"] as? [String: Any] {
                imageSearchEnabled = (img["enabled"] as? Bool) ?? true
                imageSearchHasKey = (img["has_key"] as? Bool) ?? false
                imageSearchMasked = (img["key_masked"] as? String) ?? ""
            }
            if let bg = settings["background_tasks"] as? [String: Any] {
                autoResumeTasks = (bg["auto_resume_on_restart"] as? Bool) ?? true
            }
            if let sc = settings["shortcuts"] as? [String: Any] {
                pttHotkey = (sc["ptt_hotkey"] as? String) ?? pttHotkey
                chatInputHotkey = (sc["chat_input_hotkey"] as? String) ?? chatInputHotkey
                cancelHotkey = (sc["cancel_hotkey"] as? String) ?? cancelHotkey
                settingsHotkey = (sc["settings_hotkey"] as? String) ?? settingsHotkey
                toggleDockHotkey = (sc["toggle_dock_hotkey"] as? String) ?? toggleDockHotkey
            }
            if let provMap = settings["providers"] as? [String: [String: Any]] {
                let builtinOrder = ["deepseek": 0, "gemini": 1, "openai": 2]
                let existingDrafts = Dictionary(
                    uniqueKeysWithValues: providers.compactMap { prov -> (String, String)? in
                        guard !prov.draftApiKey.isEmpty else { return nil }
                        return (prov.name, prov.draftApiKey)
                    }
                )
                providers = provMap.keys.sorted { a, b in
                    let ra = builtinOrder[a] ?? 99
                    let rb = builtinOrder[b] ?? 99
                    return ra == rb ? a < b : ra < rb
                }.compactMap { key -> ProviderViewModel? in
                    guard let p = provMap[key] else { return nil }
                    var prov = ProviderViewModel(
                        name: (p["name"] as? String) ?? key,
                        label: (p["label"] as? String) ?? key,
                        protocolName: (p["protocol"] as? String) ?? "openai_compatible",
                        baseURL: (p["base_url"] as? String) ?? "",
                        authRequired: (p["auth_required"] as? Bool) ?? true,
                        hasKey: (p["has_key"] as? Bool) ?? false,
                        keyMasked: (p["key_masked"] as? String) ?? "",
                        models: (p["models"] as? [String]) ?? [],
                        modelCapabilities: Self.parseModelCapabilities(
                            (p["model_capabilities"] as? [String: [String: Any]]) ?? [:]
                        )
                    )
                    prov.draftApiKey = existingDrafts[prov.name] ?? ""
                    return prov
                }
            }
            if let firstWithModels = providers.first(where: {
                $0.name == "deepseek" && !compatibleModels($0.models, kind: .chat).isEmpty
            }) ?? providers.first(where: {
                !compatibleModels($0.models, kind: .chat).isEmpty
            }), chatSlot.provider.isEmpty {
                chatSlot = SlotRouteViewModel(provider: firstWithModels.name, model: "")
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
                if let w = mr["worker"],
                   let wp = w["provider"] as? String, !wp.isEmpty,
                   let wm = w["model"] as? String, !wm.isEmpty {
                    workerSlot = SlotRouteViewModel(provider: wp, model: wm)
                    workerFollowsChat = false
                } else {
                    workerSlot = SlotRouteViewModel(provider: "", model: "")
                    workerFollowsChat = true
                }
                if let a = mr["asr"] {
                    asrSlot = SlotRouteViewModel(provider: (a["provider"] as? String) ?? "deepseek", model: (a["model"] as? String) ?? "")
                }
                if let t = mr["tts"] {
                    ttsSlot = SlotRouteViewModel(provider: (t["provider"] as? String) ?? "deepseek", model: (t["model"] as? String) ?? "", voice: (t["voice"] as? String) ?? "stella")
                }
            }
            chatSlot = normalizedSlot(chatSlot, kind: .chat, providers: providers)
            visionSlot = normalizedSlot(visionSlot, kind: .vision, providers: providers)
            fastSlot = normalizedSlot(fastSlot, kind: .fast, providers: providers)
            if !workerFollowsChat {
                workerSlot = normalizedSlot(workerSlot, kind: .fast, providers: providers)
            }
            asrSlot = normalizedSlot(asrSlot, kind: .asr, providers: providers)
            ttsSlot = normalizedSlot(ttsSlot, kind: .tts, providers: providers)
        }
        mcpServers = await brainClient.fetchMCPServers()
    }

    func clearDraftKeys() {
        for index in providers.indices {
            providers[index].draftApiKey = ""
        }
        imageSearchKey = ""
    }
}

private struct ProviderFeedback: Equatable {
    var text: String
    var isError: Bool
}

private struct RouteModelMenu: View {
    @Binding var selection: String
    let options: [String]
    @State private var isPresented = false

    var body: some View {
        Button {
            isPresented.toggle()
        } label: {
            HStack(spacing: 6) {
                Text(selection.isEmpty ? "选择模型" : selection)
                    .font(.system(size: 12))
                    .foregroundStyle(selection.isEmpty ? QMXTheme.textTertiary : QMXTheme.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 0)
                Image(systemName: "chevron.down")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
            .padding(.horizontal, 7)
            .frame(height: 22)
            .frame(maxWidth: .infinity)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(QMXTheme.surfaceMuted)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("选择模型")
        .accessibilityValue(selection)
        .popover(isPresented: $isPresented, arrowEdge: .top) {
            let listHeight = min(CGFloat(options.count) * 27 + 8, 320)
            ScrollView {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(options, id: \.self) { model in
                        Button {
                            selection = model
                            isPresented = false
                        } label: {
                            HStack(spacing: 8) {
                                Text(model)
                                    .font(.system(size: 12))
                                    .lineLimit(1)
                                Spacer(minLength: 0)
                                if selection == model {
                                    Image(systemName: "checkmark")
                                        .font(.system(size: 10, weight: .semibold))
                                        .foregroundStyle(QMXTheme.accent)
                                }
                            }
                            .padding(.horizontal, 8)
                            .frame(height: 26)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(4)
            }
            .frame(width: 280, height: listHeight)
        }
    }
}

struct SettingsRootView: View {
    @AppStorage("qmx.answerFontSize") private var answerFontSize: Double = 16
    @AppStorage("qmx.answerMaxLines") private var answerMaxLines: Int = 30
    @AppStorage("qmx.dropPocket.dragShakeEnabled") private var dragShakeEnabled: Bool = false
    @ObservedObject var store: SettingsViewStore
    @ObservedObject var brainClient: BrainClient
    let supervisor: ProcessSupervisor
    let desktopActions: DesktopActions
    let onClose: () -> Void

    @State private var showingCustomProvider = false
    @State private var providerPendingRemoval: String?
    @State private var removedProviderIDs: Set<String> = []
    @State private var providerFeedback: [String: ProviderFeedback] = [:]
    @State private var busyProviders: Set<String> = []

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider().opacity(0.5)
            detail
        }
        .frame(minWidth: 760, minHeight: 520)
        .background(QMXTheme.window)
        .sheet(isPresented: $showingCustomProvider) {
            CustomProviderSheet { provider in
                store.providers.append(provider)
                showingCustomProvider = false
            }
        }
        .alert("移除自定义厂商？", isPresented: removalAlertBinding) {
            Button("移除", role: .destructive) {
                if let pid = providerPendingRemoval {
                    removeCustomProvider(pid)
                }
            }
            Button("取消", role: .cancel) {
                providerPendingRemoval = nil
            }
        } message: {
            Text("会同时清除该厂商的 Key 与关联路由，点击设置页的「保存」后生效。")
        }
    }

    private var removalAlertBinding: Binding<Bool> {
        Binding(
            get: { providerPendingRemoval != nil },
            set: { if !$0 { providerPendingRemoval = nil } }
        )
    }

    private func removeCustomProvider(_ pid: String) {
        store.providers.removeAll { $0.name == pid }
        removedProviderIDs.insert(pid)
        if store.chatSlot.provider == pid { store.chatSlot = SlotRouteViewModel(provider: "", model: "") }
        if store.visionSlot.provider == pid { store.visionSlot = SlotRouteViewModel(provider: "", model: "") }
        if store.fastSlot.provider == pid { store.fastSlot = SlotRouteViewModel(provider: "", model: "") }
        if store.workerSlot.provider == pid { store.workerSlot = SlotRouteViewModel(provider: "", model: "") }
        if store.asrSlot.provider == pid { store.asrSlot = SlotRouteViewModel(provider: "", model: "") }
        if store.ttsSlot.provider == pid { store.ttsSlot = SlotRouteViewModel(provider: "", model: "", voice: "stella") }
        providerPendingRemoval = nil
    }

    private var sidebar: some View {
        ZStack {
            VisualEffectBlur(material: .sidebar, blendingMode: .behindWindow)
            VStack(alignment: .leading, spacing: 0) {
                brand
                navSection("核心设置", items: [.general, .routing, .voicePrivacy])
                navSection("效率与扩展", items: [.shortcuts, .browser, .mcp])
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
                    case .browser: BrowserSettingsPane(brainClient: brainClient)
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
                .foregroundStyle(
                    store.statusMessage.contains("失败") || store.statusMessage.contains("请先切换")
                        ? QMXTheme.danger
                        : QMXTheme.textSecondary
                )
                .lineLimit(2)
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
                Toggle(isOn: $dragShakeEnabled) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("拖拽文件时晃动鼠标唤出托盘")
                        Text("开启后，在 Finder 等应用中拖起文件并左右快速晃动鼠标，将在屏边自动弹出投喂托盘")
                            .font(.system(size: 11))
                            .foregroundStyle(QMXTheme.textTertiary)
                    }
                }
                .onChange(of: dragShakeEnabled) { enabled in
                    NotificationCenter.default.post(name: .qmxDragShakeSettingChanged, object: enabled)
                }
                .padding(.vertical, 2)
                if supervisor.isGodotPluginAvailable {
                    Toggle("可选桌面头像插件", isOn: $store.avatarEnabled)
                        .frame(minHeight: 40)
                } else {
                    Text("可选桌面头像插件未随此安装包提供")
                        .font(.system(size: 11))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .frame(minHeight: 32)
                }
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
            if store.providers.allSatisfy({ !$0.hasKey }) || store.chatSlot.model.isEmpty {
                Section("开始使用") {
                    Text("先填写 DeepSeek 或 Google Gemini 的 API Key，点击「刷新模型」，再在下方选择对话模型。模型上下文会自动从厂商 API 获取，也可手动修改。")
                        .font(.system(size: 12))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.vertical, 6)
                }
            }

            Section("能力路由") {
                slotRow("对话", slot: $store.chatSlot, kind: .chat, showVoice: false)
                slotRow("识图 / 看屏 / OCR", slot: $store.visionSlot, kind: .vision, showVoice: false)
                slotRow("后台快任务", slot: $store.fastSlot, kind: .fast, showVoice: false)
                HStack {
                    Toggle("后台 Worker 跟随对话模型", isOn: $store.workerFollowsChat)
                        .onChange(of: store.workerFollowsChat) { follows in
                            if !follows && store.workerSlot.provider.isEmpty {
                                store.workerSlot = store.chatSlot
                            }
                        }
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 40)
                if !store.workerFollowsChat {
                    slotRow("后台执行（Worker）", slot: $store.workerSlot, kind: .fast, showVoice: false)
                }
                slotRow("语音识别", slot: $store.asrSlot, kind: .asr, showVoice: false)
                slotRow("语音合成", slot: $store.ttsSlot, kind: .tts, showVoice: true)
            }

            Section("后台任务") {
                Toggle("重启后自动恢复中断的后台任务", isOn: $store.autoResumeTasks)
                    .frame(minHeight: 40)
                    .help("开启时只读/排队任务从检查点续跑；写操作中断后仍会转为等待审批，绝不静默重放。关闭时所有中断任务保持挂起。")
            }

            Section("DeepSeek 能力") {
                Picker("深度思考", selection: $store.deepseekThinking) {
                    Text("自动").tag("auto")
                    Text("开启").tag("enabled")
                    Text("关闭").tag("disabled")
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 300)
                .frame(minHeight: 40)

                Picker("思考强度", selection: $store.deepseekEffort) {
                    Text("默认").tag("")
                    Text("低").tag("low")
                    Text("高").tag("high")
                    Text("最高").tag("max")
                }
                .pickerStyle(.menu)
                .frame(maxWidth: 180)
                .frame(minHeight: 40)
                .disabled(store.deepseekThinking == "disabled")
                .help("DeepSeek 思考模式下生效；关闭思考后此设置不可用。")

                Toggle("原生联网搜索", isOn: $store.deepseekNativeWeb)
                    .frame(minHeight: 40)
                    .help("DeepSeek Flash 使用服务端 deepseek_web_search / deepseek_read_url，回答可带来源引用；需在工具设置中开启联网搜索。")

                Toggle("内容安全校验", isOn: $store.deepseekSafety)
                    .frame(minHeight: 40)
                    .help("按 DeepSeek high 级别审核输入输出；会额外消耗少量安全 token，并可能拒答风险内容。")
            }

            Section("图片搜索") {
                Toggle("启用 Tavily 图片搜索", isOn: $store.imageSearchEnabled)
                    .frame(minHeight: 40)
                    .help("使用 Tavily /search 的 include_images 搜索图片，下载后放入回答下方媒体条；图片会保留来源页面。")

                HStack(spacing: 8) {
                    Text("Tavily Key")
                        .font(.system(size: 11))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .frame(width: 72, alignment: .leading)
                    SecureField(
                        "",
                        text: $store.imageSearchKey,
                        prompt: Text(store.imageSearchHasKey ? "已配置 \(store.imageSearchMasked)" : "粘贴 Tavily Key，例如 tvly-…")
                    )
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12))
                }
                .labelsHidden()
                .frame(minHeight: 34)

                Text("只需要 Tavily API Key；留空会保留已有 Key。图片搜索与 DeepSeek/Gemini 的文本联网是独立开关。")
                    .font(.system(size: 11))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Section("Gemini 能力") {
                Picker("思考强度", selection: $store.geminiThinkingLevel) {
                    Text("自动").tag("auto")
                    Text("低").tag("low")
                    Text("中").tag("medium")
                    Text("高").tag("high")
                    Text("最高").tag("xhigh")
                }
                .pickerStyle(.menu)
                .frame(maxWidth: 180)
                .frame(minHeight: 40)
                .help("Gemini 4 无法完全关闭思考；低/中/高控制推理深度，最高仅 Neon / Hydrogen 支持。")

                Toggle("Google 搜索", isOn: $store.geminiGoogleSearch)
                    .frame(minHeight: 40)
                    .help("使用 Gemini 原生 googleSearch grounding，回答附带来源；按检索次数计费（每月 5000 次免费，之后 $14/1000 次）。")

                Toggle("网页上下文", isOn: $store.geminiURLContext)
                    .frame(minHeight: 40)
                    .help("使用 Gemini urlContext 读取消息中的链接；与 Google 搜索一起开启时会合并为服务端工具。")
            }

            Section {
                ForEach($store.providers) { $prov in
                    providerCard($prov)
                        .padding(.vertical, 4)
                }

                HStack(spacing: 8) {
                    Button {
                        showingCustomProvider = true
                    } label: {
                        Label("添加自定义厂商", systemImage: "plus")
                            .font(.system(size: 12, weight: .medium))
                    }
                    .buttonStyle(.qmxSecondary)

                    Spacer()
                }
                .frame(minHeight: 40)
            } header: {
                HStack(spacing: 8) {
                    Text("服务商")
                    Spacer()
                    Button {
                        showingCustomProvider = true
                    } label: {
                        Label("自定义", systemImage: "plus")
                            .font(.system(size: 10.5, weight: .medium))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(QMXTheme.accent)
                }
            }
        }
    }

    private func providerCard(_ prov: Binding<ProviderViewModel>) -> some View {
        let value = prov.wrappedValue
        let statusText = !value.draftApiKey.isEmpty
            ? "待保存"
            : (value.hasKey ? "已配置" : "待配置 Key")
        let statusColor = (value.hasKey && value.draftApiKey.isEmpty)
            ? QMXTheme.success
            : QMXTheme.warning
        let isBusy = busyProviders.contains(value.name)
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                StatusDot(
                    color: value.hasKey
                        ? QMXTheme.success
                        : (value.authRequired ? QMXTheme.warning : QMXTheme.textTertiary),
                    size: 6
                )
                Text(value.label)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Text(value.protocolName == "anthropic" ? "Anthropic" : "OpenAI 兼容")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(QMXTheme.surfaceMuted))
                Spacer(minLength: 0)
                Text(statusText)
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(statusColor)
                if value.name.hasPrefix("custom_") {
                    Button {
                        providerPendingRemoval = value.name
                    } label: {
                        Image(systemName: "trash")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(QMXTheme.textTertiary)
                            .frame(width: 18, height: 18)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("移除自定义厂商")
                }
            }

            HStack(spacing: 8) {
                Text("Base URL")
                    .font(.system(size: 11))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .frame(width: 64, alignment: .leading)
                TextField("https://…", text: prov.baseURL)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12))
            }
            .labelsHidden()

            HStack(spacing: 8) {
                Text("API Key")
                    .font(.system(size: 11))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .frame(width: 64, alignment: .leading)
                SecureField(
                    "",
                    text: prov.draftApiKey,
                    prompt: Text(value.hasKey ? "已配置 \(value.keyMasked)" : "输入 API Key")
                )
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12))
            }
            .labelsHidden()

            HStack(spacing: 8) {
                if !value.models.isEmpty {
                    Text("\(value.models.count) 个模型")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(QMXTheme.textTertiary)
                }
                Spacer(minLength: 0)
                Button(isBusy ? "刷新中…" : "刷新模型") {
                    refreshModels(for: value)
                }
                .buttonStyle(.qmxSecondary)
                .disabled(isBusy || !value.isPersisted)
                .help(value.isPersisted ? "从厂商 API 同步最新模型" : "先点击设置页「保存」，再刷新模型")
                Button(isBusy ? "测试中…" : "测连通") {
                    testProvider(for: value)
                }
                .buttonStyle(.qmxSecondary)
                .disabled(isBusy)
            }

            if let feedback = providerFeedback[value.name] {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if isBusy {
                        ProgressView()
                            .controlSize(.small)
                    }
                    Text(feedback.text)
                        .font(.system(size: 10.5))
                        .foregroundStyle(feedback.isError ? QMXTheme.danger : QMXTheme.success)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }

            if !value.models.isEmpty {
                DisclosureGroup {
                    VStack(spacing: 8) {
                        ForEach(orderedModels(value.models), id: \.self) { model in
                            modelCapabilityRow(provider: prov, model: model)
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("模型能力 · \(value.models.count)")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                }
            }
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(QMXTheme.surfaceRaised)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }

    private func modelCapabilityRow(
        provider: Binding<ProviderViewModel>,
        model: String
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(model)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .lineLimit(1)
                if provider.wrappedValue.modelCapabilities[model]?.isPreview == true {
                    Text("预览")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(QMXTheme.warning)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(QMXTheme.warning.opacity(0.12)))
                }
                Spacer(minLength: 0)
                Text(provider.wrappedValue.modelCapabilities[model]?.source ?? "manual")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
            }

            HStack(spacing: 6) {
                Text("上下文")
                    .font(.system(size: 10.5))
                    .foregroundStyle(QMXTheme.textTertiary)
                TextField("上下文", value: capabilityIntBinding(provider, model, \.contextWindow), format: .number)
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 88)
                Text("输出")
                    .font(.system(size: 10.5))
                    .foregroundStyle(QMXTheme.textTertiary)
                TextField("输出", value: capabilityIntBinding(provider, model, \.maxOutputTokens), format: .number)
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 72)
                Toggle("工具", isOn: capabilityBoolBinding(provider, model, \.supportsTools))
                    .toggleStyle(.checkbox)
                    .font(.system(size: 10.5))
                Toggle("视觉", isOn: capabilityBoolBinding(provider, model, \.supportsVision))
                    .toggleStyle(.checkbox)
                    .font(.system(size: 10.5))
                Spacer(minLength: 0)
            }
        }
        .padding(.vertical, 4)
    }

    private func capabilityIntBinding(
        _ provider: Binding<ProviderViewModel>,
        _ model: String,
        _ keyPath: WritableKeyPath<ModelCapabilityViewModel, Int>
    ) -> Binding<Int> {
        Binding(
            get: { provider.wrappedValue.modelCapabilities[model]?[keyPath: keyPath] ?? 0 },
            set: { value in
                var cap = provider.wrappedValue.modelCapabilities[model] ?? ModelCapabilityViewModel()
                cap[keyPath: keyPath] = value
                cap.source = "manual"
                provider.wrappedValue.modelCapabilities[model] = cap
            }
        )
    }

    private func capabilityBoolBinding(
        _ provider: Binding<ProviderViewModel>,
        _ model: String,
        _ keyPath: WritableKeyPath<ModelCapabilityViewModel, Bool>
    ) -> Binding<Bool> {
        Binding(
            get: { provider.wrappedValue.modelCapabilities[model]?[keyPath: keyPath] ?? false },
            set: { value in
                var cap = provider.wrappedValue.modelCapabilities[model] ?? ModelCapabilityViewModel()
                cap[keyPath: keyPath] = value
                cap.source = "manual"
                provider.wrappedValue.modelCapabilities[model] = cap
            }
        )
    }

    private func slotRow(
        _ title: String,
        slot: Binding<SlotRouteViewModel>,
        kind: RouteSlotKind,
        showVoice: Bool
    ) -> some View {
        let current = slot.wrappedValue
        var options = modelsForSlot(kind, provider: current.provider)
        if !current.model.isEmpty && !options.contains(current.model) {
            options.append(current.model)
        }
        return HStack(spacing: 8) {
            Text(title)
                .font(.system(size: 13))
                .frame(width: 100, alignment: .leading)

            Picker("", selection: providerBinding(slot, kind: kind)) {
                ForEach(store.providers) { p in
                    Text(p.label).tag(p.name)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(width: 140)

            if options.isEmpty {
                Text("暂无可用模型")
                    .font(.system(size: 11))
                    .foregroundStyle(QMXTheme.warning)
                    .frame(minWidth: 120, maxWidth: .infinity, alignment: .leading)
            } else {
                RouteModelMenu(selection: slot.model, options: options)
                    .frame(minWidth: 120, maxWidth: .infinity, alignment: .leading)
            }

            if showVoice {
                TextField("", text: slot.voice)
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 80)
            }
            Spacer(minLength: 0)
        }
        .labelsHidden()
        .frame(minHeight: 40)
    }

    private func refreshModels(for provider: ProviderViewModel) {
        guard provider.isPersisted, !busyProviders.contains(provider.name) else { return }
        busyProviders.insert(provider.name)
        providerFeedback[provider.name] = ProviderFeedback(
            text: "正在从 \(provider.label) 刷新模型…",
            isError: false
        )
        Task { @MainActor in
            let (ok, message, _) = await brainClient.discoverModels(
                providerId: provider.name,
                apiKey: provider.draftApiKey
            )
            busyProviders.remove(provider.name)
            providerFeedback[provider.name] = ProviderFeedback(text: message, isError: !ok)
            store.statusMessage = "\(provider.label)：\(message)"
            if ok {
                await store.reload(brainClient: brainClient)
            }
        }
    }

    private func testProvider(for provider: ProviderViewModel) {
        guard !busyProviders.contains(provider.name) else { return }
        busyProviders.insert(provider.name)
        providerFeedback[provider.name] = ProviderFeedback(
            text: "正在测试 \(provider.label) 连通性…",
            isError: false
        )
        Task { @MainActor in
            let (ok, message) = await brainClient.testProvider([
                "provider_id": provider.name,
                "base_url": provider.baseURL,
                "api_key": provider.draftApiKey,
                "protocol": provider.protocolName
            ])
            busyProviders.remove(provider.name)
            providerFeedback[provider.name] = ProviderFeedback(
                text: "\(provider.label)：\(message)",
                isError: !ok
            )
        }
    }

    private func modelsForSlot(_ kind: RouteSlotKind, provider: String) -> [String] {
        guard let prov = store.providers.first(where: { $0.name == provider }) else {
            return []
        }
        return orderedModels(compatibleModels(prov.models, kind: kind))
    }

    private func providerBinding(
        _ slot: Binding<SlotRouteViewModel>,
        kind: RouteSlotKind
    ) -> Binding<String> {
        Binding(
            get: { slot.wrappedValue.provider },
            set: { newProvider in
                slot.wrappedValue.provider = newProvider
                let options = modelsForSlot(kind, provider: newProvider)
                if !options.contains(slot.wrappedValue.model) {
                    slot.wrappedValue.model = preferredDefaultModel(options, kind: kind)
                }
            }
        )
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
                "models": p.models,
                "model_capabilities": p.modelCapabilities.mapValues { $0.payload }
            ]
            if !p.draftApiKey.isEmpty {
                entry["api_key"] = p.draftApiKey
            }
            provPatch[p.name] = entry
        }
        for pid in removedProviderIDs {
            provPatch[pid] = ["delete": true]
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
            "deepseek": [
                "thinking": store.deepseekThinking,
                "reasoning_effort": store.deepseekEffort,
                "native_web_tools": store.deepseekNativeWeb,
                "safety_check": store.deepseekSafety
            ],
            "gemini": [
                "thinking_level": store.geminiThinkingLevel,
                "google_search": store.geminiGoogleSearch,
                "url_context": store.geminiURLContext
            ],
            "image_search": [
                "enabled": store.imageSearchEnabled,
                "provider": "tavily",
                "api_key": store.imageSearchKey
            ],
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
                "worker": store.workerFollowsChat
                    ? ["provider": "", "model": ""]
                    : ["provider": store.workerSlot.provider, "model": store.workerSlot.model],
                "asr": ["provider": store.asrSlot.provider, "model": store.asrSlot.model],
                "tts": ["provider": store.ttsSlot.provider, "model": store.ttsSlot.model, "voice": store.ttsSlot.voice]
            ],
            "background_tasks": [
                "auto_resume_on_restart": store.autoResumeTasks
            ]
        ]

        let (ok, msg) = await brainClient.saveSettings(payload)
        store.statusMessage = msg
        if ok {
            removedProviderIDs.removeAll()
            brainClient.setUIStatePatch(["avatar_enabled": store.avatarEnabled, "earcons": store.earconsEnabled])
            await store.reload(brainClient: brainClient)
            store.clearDraftKeys()
        }
    }
}

// MARK: - Custom provider sheet

struct CustomProviderSheet: View {
    @Environment(\.dismiss) private var dismiss
    let onAdd: (ProviderViewModel) -> Void

    @State private var label = ""
    @State private var protocolName = "openai_compatible"
    @State private var baseURL = ""
    @State private var apiKey = ""
    @State private var modelsText = ""

    private var canAdd: Bool {
        !label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
        !baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                Text("添加自定义厂商")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                }
                .buttonStyle(.qmxIcon)
            }

            VStack(alignment: .leading, spacing: 12) {
                fieldRow("显示名称") {
                    TextField("例如 我的中转站", text: $label)
                        .textFieldStyle(.roundedBorder)
                }

                fieldRow("协议") {
                    Picker("", selection: $protocolName) {
                        Text("OpenAI 兼容").tag("openai_compatible")
                        Text("Anthropic").tag("anthropic")
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                }

                fieldRow("Base URL") {
                    TextField("https://example.com/v1", text: $baseURL)
                        .textFieldStyle(.roundedBorder)
                }

                fieldRow("API Key") {
                    SecureField("sk-…", text: $apiKey)
                        .textFieldStyle(.roundedBorder)
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text("模型 ID")
                        .font(.system(size: 11))
                        .foregroundStyle(QMXTheme.textTertiary)
                    TextField(
                        "每行一个，或逗号分隔",
                        text: $modelsText,
                        axis: .vertical
                    )
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(3...5)
                }

                Text("添加并点击设置页的「保存」后，可点「刷新模型」自动发现；上下文窗口和最大输出也能在「模型能力」里调整（默认 32K / 4K）。")
                    .font(.system(size: 10.5))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 0)

            HStack(spacing: 8) {
                Text("Key 仅保存在本机 0600 数据库")
                    .font(.system(size: 10.5))
                    .foregroundStyle(QMXTheme.textTertiary)
                Spacer()
                Button("取消") { dismiss() }
                    .buttonStyle(.qmxSecondary)
                Button("添加") { add() }
                    .buttonStyle(.qmxProminent)
                    .disabled(!canAdd)
            }
        }
        .padding(24)
        .frame(width: 480, height: 440)
        .background(QMXTheme.window)
    }

    private func fieldRow<Content: View>(
        _ label: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textTertiary)
                .frame(width: 72, alignment: .leading)
            content()
        }
    }

    private func add() {
        let cleanLabel = label.trimmingCharacters(in: .whitespacesAndNewlines)
        let models = modelsText
            .split(whereSeparator: { $0 == "," || $0 == "\n" })
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let caps = Dictionary(uniqueKeysWithValues: models.map { model in
            (model, ModelCapabilityViewModel(
                contextWindow: 32768,
                maxOutputTokens: 4096,
                supportsTools: true,
                supportsVision: false,
                supportsCache: false,
                supportsReasoning: false,
                source: "manual"
            ))
        })
        let provider = ProviderViewModel(
            name: "custom_\(UUID().uuidString.prefix(8).lowercased())",
            label: cleanLabel,
            protocolName: protocolName,
            baseURL: baseURL.trimmingCharacters(in: .whitespacesAndNewlines),
            authRequired: true,
            hasKey: !apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            keyMasked: "",
            models: models,
            modelCapabilities: caps,
            draftApiKey: apiKey.trimmingCharacters(in: .whitespacesAndNewlines),
            isPersisted: false
        )
        onAdd(provider)
        dismiss()
    }
}

private struct BrowserSettingsPane: View {
    @ObservedObject var brainClient: BrainClient
    @State private var chromeInstalled = false
    @State private var playwrightInstalled = false
    @State private var profileSizeBytes = 0
    @State private var cdpEndpoint = "http://127.0.0.1:9222"
    @State private var cdpReady = false
    @State private var cdpMessage = ""
    @State private var busy = false
    @State private var message = ""
    @State private var confirmClear = false

    var body: some View {
        Section("运行时检测") {
            LabeledContent("Google Chrome") {
                Text(chromeInstalled ? "已安装" : "未找到")
                    .foregroundStyle(chromeInstalled ? QMXTheme.success : QMXTheme.warning)
            }
            LabeledContent("Playwright") {
                Text(playwrightInstalled ? "已就绪" : "未安装")
                    .foregroundStyle(playwrightInstalled ? QMXTheme.success : QMXTheme.warning)
            }
            LabeledContent("隔离缓存") {
                Text(profileSizeText)
                    .monospacedDigit()
                    .foregroundStyle(QMXTheme.textSecondary)
            }
            Button("重新检测") {
                Task { await reload() }
            }
            .disabled(busy)
        }

        Section("复用主浏览器（live）") {
            TextField("CDP 地址", text: $cdpEndpoint)
                .textFieldStyle(.roundedBorder)
            HStack(spacing: 10) {
                Button("保存地址") {
                    Task { await saveEndpoint() }
                }
                .disabled(busy)
                Button(cdpReady ? "已连接" : "测试连接") {
                    Task { await testCDP() }
                }
                .disabled(busy)
                if !cdpMessage.isEmpty {
                    Text(cdpMessage)
                        .font(.system(size: 11))
                        .foregroundStyle(cdpReady ? QMXTheme.success : QMXTheme.warning)
                        .lineLimit(2)
                }
            }
            Text("live 模式只连接本机 127.0.0.1 调试端口。Chrome 136+ 必须使用独立的 --user-data-dir 启动协作窗口；不要对个人默认 Chrome 目录开启调试端口。")
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }

        Section("安全边界") {
            Text("默认使用隔离浏览器 browser_profile/，与个人浏览器物理隔离。live 模式必须在任务派发审批中绑定 allowed_domains；跨域跳转会由代码层阻断，任务结束只断开连接，不关闭你的浏览器窗口。")
                .font(.system(size: 11.5))
                .foregroundStyle(QMXTheme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }

        Section("缓存") {
            Button("清理隔离浏览器缓存…", role: .destructive) {
                confirmClear = true
            }
            .disabled(busy)
            if !message.isEmpty {
                Text(message)
                    .font(.system(size: 11))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
        }
        .task { await reload() }
        .alert("清理隔离浏览器缓存？", isPresented: $confirmClear) {
            Button("清理", role: .destructive) {
                Task { await clearProfile() }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("会删除 browser_profile/ 中的登录态与缓存，不影响个人浏览器和 live 协作窗口。")
        }
    }

    private var profileSizeText: String {
        if profileSizeBytes <= 0 { return "空" }
        let mb = Double(profileSizeBytes) / 1024.0 / 1024.0
        if mb >= 1024 { return String(format: "%.1f GB", mb / 1024.0) }
        return String(format: "%.1f MB", mb)
    }

    private func reload() async {
        busy = true
        defer { busy = false }
        guard let status = await brainClient.fetchBrowserStatus() else {
            message = "无法读取浏览器状态，Brain 可能未连接。"
            return
        }
        chromeInstalled = (status["chrome_installed"] as? Bool) ?? false
        playwrightInstalled = (status["playwright_installed"] as? Bool) ?? false
        profileSizeBytes = (status["profile_size_bytes"] as? Int) ?? 0
        if let endpoint = status["cdp_endpoint"] as? String, !endpoint.isEmpty {
            cdpEndpoint = endpoint
        }
        cdpReady = (status["cdp_ok"] as? Bool) ?? false
    }

    private func saveEndpoint() async {
        busy = true
        defer { busy = false }
        let (ok, messageText) = await brainClient.saveSettings([
            "browser": ["cdp_endpoint": cdpEndpoint]
        ])
        message = ok ? "CDP 地址已保存" : messageText
        if ok {
            await reload()
        }
    }

    private func testCDP() async {
        busy = true
        defer { busy = false }
        guard let result = await brainClient.testBrowserCDP(endpoint: cdpEndpoint) else {
            cdpReady = false
            cdpMessage = "测试请求失败"
            return
        }
        cdpReady = (result["ok"] as? Bool) ?? false
        if cdpReady {
            let browser = (result["browser"] as? String) ?? ""
            let tabs = (result["tabs"] as? [[String: Any]])?.count ?? 0
            cdpMessage = "\(browser) · \(tabs) 个标签页"
        } else {
            cdpMessage = (result["error"] as? String) ?? "连接失败"
        }
    }

    private func clearProfile() async {
        busy = true
        defer { busy = false }
        guard let result = await brainClient.clearBrowserProfile() else {
            message = "清理失败"
            return
        }
        let freed = (result["freed_bytes"] as? Int) ?? 0
        message = "已清理 \(ByteCountFormatter.string(fromByteCount: Int64(freed), countStyle: .file))"
        await reload()
    }
}

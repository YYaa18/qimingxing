import AppKit
import SwiftUI

@MainActor
final class ApprovalSheetController {
    private var windows: [String: NSWindow] = [:]
    private weak var brainClient: BrainClient?

    init(brainClient: BrainClient) {
        self.brainClient = brainClient
    }

    func syncPending(_ items: [PendingApprovalItem]) {
        let activeIds = Set(items.map(\.approvalId))
        for (aid, win) in windows where !activeIds.contains(aid) {
            win.close()
            windows.removeValue(forKey: aid)
        }
        for item in items where windows[item.approvalId] == nil {
            showWindow(for: item)
        }
    }

    func closeWindow(approvalId: String) {
        if let win = windows.removeValue(forKey: approvalId) {
            win.close()
        }
    }

    private func showWindow(for item: PendingApprovalItem) {
        guard let brainClient else { return }
        let view = ApprovalModalView(item: item) { [weak brainClient, weak self] approved, remember, answerText in
            brainClient?.respondToApproval(
                approvalId: item.approvalId,
                turnId: item.turnId,
                approved: approved,
                rememberForSession: remember,
                answerText: answerText
            )
            self?.closeWindow(approvalId: item.approvalId)
        }

        let hosting = NSHostingController(rootView: view)
        hosting.view.wantsLayer = true
        hosting.view.layer?.backgroundColor = NSColor.clear.cgColor

        let win = NSWindow(contentViewController: hosting)
        win.title = item.isClarification ? "启明星 · 请确认" : "启明星 · 操作审批"
        win.styleMask = [.borderless]
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = true
        win.level = .floating
        win.isMovableByWindowBackground = true
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        win.setContentSize(NSSize(width: 440, height: item.isClarification ? 300 : 360))
        win.center()
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
        windows[item.approvalId] = win
    }
}

struct ApprovalModalView: View {
    let item: PendingApprovalItem
    let onRespond: (Bool, Bool, String) -> Void

    @State private var customReply: String = ""
    @State private var rememberChoice: Bool = false

    init(item: PendingApprovalItem, onRespond: @escaping (Bool, Bool, String) -> Void) {
        self.item = item
        self.onRespond = onRespond
    }

    var body: some View {
        ZStack {
            VisualEffectBlur(material: .popover, blendingMode: .behindWindow)

            VStack(alignment: .leading, spacing: 16) {
                header

                Text(item.summary)
                    .font(.system(size: 14))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)

                if item.isClarification {
                    clarificationBody
                } else {
                    detailsCard
                    rememberRow
                    actionButtons
                }
            }
            .padding(20)
        }
        .frame(width: 440)
        .clipShape(RoundedRectangle(cornerRadius: QMXTheme.approvalRadius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.approvalRadius, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: item.isClarification ? "questionmark.circle.fill" : "shield.lefthalf.filled")
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(item.isClarification ? QMXTheme.accent : QMXTheme.warning)

            VStack(alignment: .leading, spacing: 3) {
                Text(humanTitle)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                Text(sourceText)
                    .font(.system(size: 12))
                    .foregroundStyle(QMXTheme.textSecondary)
            }

            Spacer()
        }
    }

    private var detailsCard: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 7) {
                if let rawPath = item.details["preview_image_path"],
                   !rawPath.isEmpty,
                   let image = NSImage(contentsOfFile: rawPath) {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(maxHeight: 120)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                        )
                }
                ForEach(item.details.keys.sorted().filter { $0 != "preview_image_path" }, id: \.self) { key in
                    HStack(alignment: .top, spacing: 10) {
                        Text(humanKey(key))
                            .font(.system(size: 11, weight: .medium, design: .monospaced))
                            .foregroundStyle(QMXTheme.textTertiary)
                            .frame(width: 92, alignment: .leading)
                        Text(displayValue(key, item.details[key] ?? ""))
                            .font(.system(size: 11.5, design: .monospaced))
                            .foregroundStyle(QMXTheme.textPrimary)
                            .textSelection(.enabled)
                        Spacer(minLength: 0)
                    }
                }
            }
            .padding(10)
        }
        .frame(maxHeight: 140)
        .background(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .fill(QMXTheme.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }

    @ViewBuilder
    private var rememberRow: some View {
        if item.allowRemember {
            Toggle("记住本次选择", isOn: $rememberChoice)
                .font(.system(size: 12))
                .toggleStyle(.checkbox)
                .foregroundStyle(QMXTheme.textSecondary)
        }
    }

    private var actionButtons: some View {
        HStack(spacing: 10) {
            ApprovalButton(title: "拒绝", isPrimary: false, haptic: .generic) {
                onRespond(false, false, "")
            }
            .keyboardShortcut(.cancelAction)

            Spacer()

            ApprovalButton(title: "允许", isPrimary: true, haptic: .generic) {
                onRespond(true, rememberChoice, "")
            }
            .keyboardShortcut(.defaultAction)
        }
    }

    @ViewBuilder
    private var clarificationBody: some View {
        if !item.options.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(item.options, id: \.self) { option in
                    Button {
                        onRespond(true, false, option)
                    } label: {
                        Text(option)
                            .font(.system(size: 13))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 8)
                            .background(
                                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                                    .fill(QMXTheme.surfaceRaised)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                                    .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                            )
                    }
                    .buttonStyle(.plain)
                }
            }
        }

        HStack(spacing: 8) {
            TextField("输入答复", text: $customReply)
                .textFieldStyle(.roundedBorder)

            ApprovalButton(title: "发送", isPrimary: true, haptic: .generic) {
                let answer = customReply.trimmingCharacters(in: .whitespacesAndNewlines)
                if !answer.isEmpty {
                    onRespond(true, false, answer)
                }
            }
            .disabled(customReply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

            ApprovalButton(title: "取消", isPrimary: false, haptic: .generic) {
                onRespond(false, false, "")
            }
        }
    }

    private var humanTitle: String {
        if item.isClarification { return "需要你的确认" }
        switch item.toolName {
        case "macos_notes_create": return "允许写入 Apple 备忘录？"
        case "macos_notes_search": return "允许读取 Apple 备忘录？"
        case "macos_calendar_create": return "允许创建日历日程？"
        case "macos_calendar_list": return "允许读取日历日程？"
        case "macos_reminders_create": return "允许创建提醒事项？"
        case "macos_reminders_list": return "允许读取提醒事项？"
        case "macos_mail_search": return "允许读取邮件？"
        case "macos_mail_draft": return "允许创建邮件草稿？"
        case "macos_browser_tabs": return "允许读取浏览器标签页？"
        case "browser_open": return "允许接入浏览器？"
        case "browser_act": return "允许执行网页高危操作？"
        default: return "允许执行这项操作？"
        }
    }

    private var sourceText: String {
        if item.isClarification { return "启明星需要补充一点信息" }
        if item.toolName.hasPrefix("macos_") { return "来自 macOS 应用联动" }
        if item.toolName.hasPrefix("mcp") { return "来自 MCP 工具" }
        return "来自启明星"
    }

    private func displayValue(_ key: String, _ value: String) -> String {
        if key == "browser_mode" {
            if value == "live" { return "复用主浏览器（live）" }
            if value == "isolated" { return "隔离浏览器（isolated）" }
        }
        return value
    }

    private func humanKey(_ key: String) -> String {
        switch key {
        case "app": return "应用"
        case "folder": return "文件夹"
        case "title": return "标题"
        case "body_preview": return "正文预览"
        case "to": return "收件人"
        case "cc": return "抄送"
        case "subject": return "主题"
        case "start": return "开始"
        case "end": return "结束"
        case "calendar_name": return "日历"
        case "list_name": return "列表"
        case "browser_mode": return "浏览器模式"
        case "allowed_domains": return "授权域名"
        case "target_url": return "目标网址"
        case "target": return "目标操作"
        case "tab_hint": return "标签页"
        case "preview_image_path": return "页面截图"
        case "reason": return "原因"
        case "instruction": return "需要你做什么"
        default: return key.replacingOccurrences(of: "_", with: " ")
        }
    }
}

private struct ApprovalButton: View {
    let title: String
    let isPrimary: Bool
    let haptic: NSHapticFeedbackManager.FeedbackPattern
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(isPrimary ? QMXTheme.primaryText : QMXTheme.textPrimary)
                .frame(minWidth: 104)
                .padding(.vertical, 9)
                .background(
                    Capsule(style: .continuous)
                        .fill(isPrimary ? QMXTheme.primaryFill : QMXTheme.surfaceRaised)
                )
                .overlay(
                    Capsule(style: .continuous)
                        .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                )
        }
        .buttonStyle(TactileButtonStyle(haptic: haptic))
    }
}

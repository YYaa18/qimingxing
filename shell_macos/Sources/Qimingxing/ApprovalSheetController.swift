import AppKit
import Luminare
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
        .luminareTint(overridingWith: QMXTheme.accent)

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
        win.setContentSize(NSSize(width: 480, height: item.isClarification ? 320 : 390))
        win.center()
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
        windows[item.approvalId] = win
    }
}

private struct ApprovalModalView: View {
    let item: PendingApprovalItem
    let onRespond: (Bool, Bool, String) -> Void

    @State private var customReply: String = ""
    @State private var remaining: Int
    @State private var timer: Timer?

    init(item: PendingApprovalItem, onRespond: @escaping (Bool, Bool, String) -> Void) {
        self.item = item
        self.onRespond = onRespond
        self._remaining = State(initialValue: max(1, item.timeoutSec))
    }

    var body: some View {
        ZStack {
            VisualEffectBlur(material: .hudWindow, blendingMode: .behindWindow)

            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill((item.isClarification ? QMXTheme.accent : QMXTheme.warning).opacity(0.16))
                            .frame(width: 36, height: 36)
                        Image(systemName: item.isClarification ? "questionmark.circle.fill" : "exclamationmark.shield.fill")
                            .font(.system(size: 18))
                            .foregroundStyle(item.isClarification ? QMXTheme.accent : QMXTheme.warning)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.isClarification ? "需要您的澄清确认" : "需要审批：\(item.toolName)")
                            .font(.system(size: 14, weight: .semibold))
                        Text("风险级别 \(item.riskLevel.uppercased()) · 剩余 \(remaining)s")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    Spacer()

                    Text("\(remaining)s")
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(remaining <= 10 ? QMXTheme.danger : .secondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(Color.primary.opacity(0.08)))
                }

                ProgressView(
                    value: Double(max(0, remaining)),
                    total: Double(max(1, item.timeoutSec))
                )
                .tint(remaining <= 10 ? QMXTheme.danger : QMXTheme.accent)

                Text(item.summary)
                    .font(.system(size: 13.5, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)

                if item.isClarification {
                    if !item.options.isEmpty {
                        LuminareSection("快捷选项") {
                            HStack(spacing: 8) {
                                ForEach(item.options, id: \.self) { opt in
                                    Button(opt) {
                                        onRespond(true, false, opt)
                                    }
                                    .buttonStyle(.luminare(tinted: true))
                                }
                            }
                            .padding(8)
                        }
                    }

                    HStack(spacing: 8) {
                        TextField("或输入自定义答复…", text: $customReply)
                            .textFieldStyle(.roundedBorder)
                        Button("发送") {
                            let ans = customReply.trimmingCharacters(in: .whitespacesAndNewlines)
                            if !ans.isEmpty {
                                onRespond(true, false, ans)
                            }
                        }
                        .buttonStyle(.luminare(tinted: true))
                        .disabled(customReply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                        Button("取消") {
                            onRespond(false, false, "")
                        }
                        .buttonStyle(.luminare)
                    }
                } else {
                    LuminareSection("参数详情") {
                        ScrollView {
                            VStack(alignment: .leading, spacing: 6) {
                                ForEach(item.details.keys.sorted(), id: \.self) { k in
                                    HStack(alignment: .top, spacing: 8) {
                                        Text(k)
                                            .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                                            .foregroundStyle(.secondary)
                                            .frame(width: 96, alignment: .leading)
                                        Text(item.details[k] ?? "")
                                            .font(.system(size: 11.5, design: .monospaced))
                                            .textSelection(.enabled)
                                        Spacer()
                                    }
                                }
                            }
                            .padding(10)
                        }
                        .frame(maxHeight: 150)
                    }

                    Spacer(minLength: 0)

                    HStack(spacing: 8) {
                        Button("拒绝") {
                            onRespond(false, false, "")
                        }
                        .buttonStyle(.luminare)
                        .keyboardShortcut(.cancelAction)

                        Spacer()

                        if item.allowRemember {
                            Button("本会话记住并允许") {
                                onRespond(true, true, "")
                            }
                            .buttonStyle(.luminare)
                        }

                        Button("允许执行") {
                            onRespond(true, false, "")
                        }
                        .buttonStyle(.luminare(tinted: true))
                        .keyboardShortcut(.defaultAction)
                    }
                }
            }
            .padding(18)
        }
        .frame(width: 480)
        .clipShape(RoundedRectangle(cornerRadius: QMXTheme.panelRadius, style: .continuous))
        .overlay(HairlineBorderOverlay(cornerRadius: QMXTheme.panelRadius))
        .onAppear {
            timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
                Task { @MainActor in
                    if remaining > 0 {
                        remaining -= 1
                    }
                }
            }
        }
        .onDisappear {
            timer?.invalidate()
        }
    }
}

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
        let win = NSWindow(contentViewController: hosting)
        win.title = item.isClarification ? "启明星 · 请确认" : "启明星 · 操作审批"
        win.styleMask = [.titled, .closable]
        win.level = .floating
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        win.setContentSize(NSSize(width: 460, height: item.isClarification ? 300 : 360))
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
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Image(systemName: item.isClarification ? "questionmark.circle.fill" : "exclamationmark.shield.fill")
                    .font(.title2)
                    .foregroundColor(item.isClarification ? .accentColor : .orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.isClarification ? "需要您的澄清确认" : "需要审批执行：\(item.toolName)")
                        .font(.headline)
                    Text("风险等级: \(item.riskLevel.uppercased()) · 剩余 \(remaining)s")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
            }

            Text(item.summary)
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)

            if item.isClarification {
                if !item.options.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("快捷选项")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        HStack {
                            ForEach(item.options, id: \.self) { opt in
                                Button(opt) {
                                    onRespond(true, false, opt)
                                }
                                .buttonStyle(.borderedProminent)
                            }
                        }
                    }
                }
                HStack {
                    TextField("或输入自定义答复…", text: $customReply)
                        .textFieldStyle(.roundedBorder)
                    Button("发送") {
                        let ans = customReply.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !ans.isEmpty {
                            onRespond(true, false, ans)
                        }
                    }
                    .disabled(customReply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button("取消") {
                        onRespond(false, false, "")
                    }
                }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(item.details.keys.sorted(), id: \.self) { k in
                            HStack(alignment: .top) {
                                Text("\(k):")
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundColor(.secondary)
                                    .frame(width: 96, alignment: .leading)
                                Text(item.details[k] ?? "")
                                    .font(.system(.caption, design: .monospaced))
                                    .textSelection(.enabled)
                                Spacer()
                            }
                        }
                    }
                    .padding(10)
                }
                .background(Color(NSColor.textBackgroundColor).opacity(0.6))
                .cornerRadius(8)

                HStack {
                    Button("拒绝") {
                        onRespond(false, false, "")
                    }
                    .keyboardShortcut(.cancelAction)

                    Spacer()

                    if item.allowRemember {
                        Button("本会话记住并允许") {
                            onRespond(true, true, "")
                        }
                    }

                    Button("允许执行") {
                        onRespond(true, false, "")
                    }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(18)
        .frame(minWidth: 440)
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

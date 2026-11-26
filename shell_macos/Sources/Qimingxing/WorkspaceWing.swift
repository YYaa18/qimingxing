import AppKit
import SwiftUI

// MARK: - Attached workspace wing

/// The compact workspace that grows out of the chat panel. It is deliberately
/// quieter than the standalone workspace: a short conversation recap, active
/// tasks and recent artifacts, with a split button to detach the full window.
struct WorkspaceWingView: View {
    @ObservedObject var store: WorkspaceStore
    let desktopActions: DesktopActions
    let onSplit: () -> Void
    let onClose: () -> Void

    @State private var tab: WingTab = .conversation

    private var threads: [WorkspaceThread] {
        WorkspaceThread.group(store.items.filter { $0.kind == .answer })
    }

    private var thread: WorkspaceThread? { threads.first }

    private var tasks: [WorkspaceItem] {
        store.items
            .filter { $0.kind == .task }
            .sorted { $0.updatedAt > $1.updatedAt }
    }

    private var artifacts: [WorkspaceItem] {
        store.items
            .filter { $0.kind == .artifact }
            .sorted { $0.updatedAt > $1.updatedAt }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.45)
            tabRow
            Divider().opacity(0.45)

            ScrollView {
                content
                    .padding(.horizontal, 18)
                    .padding(.vertical, 16)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollIndicators(.hidden)

            Divider().opacity(0.45)
            footer
        }
        .background(QMXTheme.surface.opacity(0.26))
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 8) {
            Text("工作区")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(QMXTheme.textPrimary)
            Text("今天 · \(thread?.turnCount ?? 0) 轮")
                .font(.system(size: 11))
                .foregroundStyle(QMXTheme.textTertiary)
                .monospacedDigit()

            Spacer(minLength: 0)

            iconButton("rectangle.split.2x1", help: "拆分到独立窗口", action: onSplit)
            iconButton("xmark", help: "收起工作区", action: onClose)
        }
        .padding(.horizontal, 18)
        .frame(height: 52)
    }

    private func iconButton(_ symbol: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(QMXTheme.textSecondary)
                .frame(width: 26, height: 26)
                .contentShape(Rectangle())
        }
        .buttonStyle(.qmxIcon)
        .help(help)
    }

    // MARK: Tabs

    private var tabRow: some View {
        HStack(spacing: 6) {
            ForEach(WingTab.allCases) { item in
                tabButton(item)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    private func tabButton(_ item: WingTab) -> some View {
        let selected = tab == item
        return Button {
            tab = item
        } label: {
            HStack(spacing: 4) {
                Text(item.label)
                Text("\(count(for: item))")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .monospacedDigit()
            }
            .font(.system(size: 11.5, weight: selected ? .medium : .regular))
            .foregroundStyle(selected ? QMXTheme.textPrimary : QMXTheme.textSecondary)
            .padding(.horizontal, 9)
            .frame(height: 26)
            .background(
                Capsule()
                    .fill(selected ? QMXTheme.surfaceRaised : Color.clear)
            )
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private func count(for item: WingTab) -> Int {
        switch item {
        case .conversation: return thread?.turnCount ?? 0
        case .tasks: return tasks.count
        case .artifacts: return artifacts.count
        }
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        switch tab {
        case .conversation:
            conversationContent
        case .tasks:
            tasksContent
        case .artifacts:
            artifactsContent
        }
    }

    @ViewBuilder
    private var conversationContent: some View {
        if let thread, !thread.turns.isEmpty {
            VStack(alignment: .leading, spacing: 18) {
                ForEach(thread.turns.suffix(3).reversed()) { turn in
                    turnRow(turn)
                }

                if thread.turnCount > 3 {
                    Text("另有 \(thread.turnCount - 3) 轮，拆分后可看完整记录。")
                        .font(.system(size: 10.5))
                        .foregroundStyle(QMXTheme.textTertiary)
                }
            }
        } else {
            empty("今天还没有对话。")
        }
    }

    private func turnRow(_ turn: WorkspaceItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(timeLabel(turn.createdAt))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                Spacer(minLength: 0)
            }

            if !turn.prompt.isEmpty {
                Text(turn.prompt)
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if turn.body.isEmpty {
                Text(wingStatusLabel(turn.status))
                    .font(.system(size: 11))
                    .foregroundStyle(QMXTheme.textTertiary)
            } else {
                Text(compactText(turn.body))
                    .font(.system(size: 12.5))
                    .lineSpacing(3)
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let media = turn.media, !media.isEmpty {
                MediaStripView(media: media, mode: .compact)
                    .padding(.top, 2)
            }
        }
    }

    @ViewBuilder
    private var tasksContent: some View {
        if tasks.isEmpty {
            empty("没有进行中的任务。")
        } else {
            VStack(alignment: .leading, spacing: 16) {
                ForEach(tasks.prefix(6)) { task in
                    taskRow(task)
                }
            }
        }
    }

    private func taskRow(_ task: WorkspaceItem) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                if task.status == "running" {
                    GlobeSpinner(size: 11, color: QMXTheme.accent)
                } else {
                    Circle()
                        .fill(taskColor(task.status))
                        .frame(width: 5, height: 5)
                }
                Text(task.title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(2)
                Spacer(minLength: 0)
                Text("\(task.progress)%")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .monospacedDigit()
            }

            HStack(spacing: 6) {
                Text(wingStatusLabel(task.status))
                if !task.body.isEmpty {
                    Text("·")
                    Text(task.body)
                        .lineLimit(1)
                }
            }
            .font(.system(size: 10.5))
            .foregroundStyle(QMXTheme.textTertiary)

            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(QMXTheme.surfaceMuted).frame(height: 2)
                    Capsule()
                        .fill(QMXTheme.accent)
                        .frame(
                            width: max(2, geo.size.width * CGFloat(task.progress) / 100.0),
                            height: 2
                        )
                }
            }
            .frame(height: 2)
        }
    }

    @ViewBuilder
    private var artifactsContent: some View {
        if artifacts.isEmpty {
            empty("还没有产物。")
        } else {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(artifacts.prefix(8)) { artifact in
                    Button {
                        _ = desktopActions.quickLook(artifact.path)
                    } label: {
                        HStack(spacing: 9) {
                            Image(systemName: "doc.richtext")
                                .font(.system(size: 12))
                                .foregroundStyle(QMXTheme.textSecondary)
                                .frame(width: 16)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(artifact.title)
                                    .font(.system(size: 12))
                                    .foregroundStyle(QMXTheme.textPrimary)
                                    .lineLimit(1)
                                if !artifact.path.isEmpty {
                                    Text(artifact.path)
                                        .font(.system(size: 10, design: .monospaced))
                                        .foregroundStyle(QMXTheme.textTertiary)
                                        .lineLimit(1)
                                }
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, 8)
                        .frame(height: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func empty(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(QMXTheme.textTertiary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 4)
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 6) {
            Text("\(thread?.turnCount ?? 0) 轮 · \(tasks.count) 个任务 · \(artifacts.count) 个产物")
                .font(.system(size: 10.5))
                .foregroundStyle(QMXTheme.textTertiary)
                .monospacedDigit()

            Spacer(minLength: 0)

            Button(action: onSplit) {
                HStack(spacing: 3) {
                    Text("独立窗口")
                    Image(systemName: "arrow.up.forward")
                }
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(QMXTheme.textSecondary)
            }
            .buttonStyle(.plain)
            .help("拆分到独立窗口")
        }
        .padding(.horizontal, 18)
        .frame(height: QuickEntryLayout.footerHeight)
    }

    // MARK: Helpers

    private func compactText(_ raw: String) -> String {
        var text = qmxCleanAssistantText(raw)
        for token in ["**", "`"] {
            text = text.replacingOccurrences(of: token, with: "")
        }
        let lines = text
            .components(separatedBy: .newlines)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("```") }
        return lines.joined(separator: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func timeLabel(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }

    private func taskColor(_ status: String) -> Color {
        switch status {
        case "running": return QMXTheme.accent
        case "waiting_approval": return QMXTheme.warning
        case "completed", "ready": return QMXTheme.success
        case "failed", "cancelled", "interrupted": return QMXTheme.danger
        default: return QMXTheme.textTertiary
        }
    }

    private func wingStatusLabel(_ status: String) -> String {
        switch status {
        case "queued": return "排队中"
        case "running": return "运行中"
        case "waiting_approval": return "等待审批"
        case "completed": return "已完成"
        case "failed": return "失败"
        case "cancelled": return "已取消"
        case "interrupted": return "已中断"
        case "waiting": return "等待回复"
        case "streaming": return "生成中"
        case "ready": return "已就绪"
        default: return status
        }
    }
}

private enum WingTab: String, CaseIterable, Identifiable {
    case conversation
    case tasks
    case artifacts

    var id: Self { self }

    var label: String {
        switch self {
        case .conversation: return "对话"
        case .tasks: return "任务"
        case .artifacts: return "产物"
        }
    }
}

import SwiftUI

/// Native renderer for `output.card` (title, type badge, markdown body,
/// server-issued actions). `dismiss` is fold/unfold and never leaves the app.
struct QMXOutputCardView: View {
    let card: OutputCardItem
    var isBusy: Bool = false
    var onAction: (CardActionItem) -> Void = { _ in }

    @State private var isCollapsed = false
    @State private var pendingActionId: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header

            if !isCollapsed, !card.contentMarkdown.isEmpty {
                MarkdownMessageView(text: QMXCardMarkers.strip(card.contentMarkdown))
            }

            if !card.actions.isEmpty {
                actionRow
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: QMXTheme.cardRadius, style: .continuous)
                .fill(QMXTheme.surfaceRaised)
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.cardRadius, style: .continuous)
                .stroke(QMXTheme.hairline, lineWidth: 1)
        )
        .accessibilityElement(children: .contain)
        .onChange(of: isBusy) { busy in
            if !busy { pendingActionId = nil }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text(card.title.isEmpty ? "提示" : card.title)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(QMXTheme.textPrimary)
                .lineLimit(2)

            Text(badgeLabel)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(QMXTheme.textSecondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(QMXTheme.surfaceMuted, in: Capsule())

            Spacer(minLength: 0)

            if isCollapsed {
                Image(systemName: "chevron.down")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
        }
    }

    private var actionRow: some View {
        HStack(spacing: 8) {
            ForEach(card.actions) { action in
                Button(action.label) { handle(action) }
                    .buttonStyle(.plain)
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(
                        isActionDisabled(action) ? QMXTheme.textTertiary : QMXTheme.textPrimary
                    )
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .background(QMXTheme.surfaceMuted.opacity(0.7), in: Capsule())
                    .disabled(isActionDisabled(action))
            }
            Spacer(minLength: 0)
        }
    }

    private var badgeLabel: String {
        switch card.cardType {
        case "spoiler_steps": return "防剧透"
        case "markdown": return "卡片"
        default: return card.cardType.isEmpty ? "卡片" : card.cardType
        }
    }

    private func isActionDisabled(_ action: CardActionItem) -> Bool {
        guard action.id != "dismiss" else { return false }
        return isBusy || pendingActionId != nil
    }

    private func handle(_ action: CardActionItem) {
        if action.id == "dismiss" {
            withAnimation(.easeOut(duration: 0.16)) { isCollapsed.toggle() }
            return
        }
        guard pendingActionId == nil, !isBusy else { return }
        pendingActionId = action.id
        onAction(action)
    }
}

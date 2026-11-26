import AppKit
import SwiftUI

struct SourceChipsView: View {
    let sources: [SourceItem]

    var body: some View {
        if !sources.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(sources) { source in
                        Button {
                            open(source)
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: "globe")
                                    .font(.system(size: 11, weight: .medium))
                                Text(trimmed(source.title))
                                    .font(.system(size: 11, weight: .medium))
                                    .lineLimit(1)
                                Image(systemName: "arrow.up.right")
                                    .font(.system(size: 9, weight: .semibold))
                                    .opacity(0.7)
                            }
                            .foregroundStyle(QMXTheme.textSecondary)
                            .padding(.horizontal, 8)
                            .frame(height: 24)
                            .background(
                                Capsule(style: .continuous)
                                    .fill(QMXTheme.surfaceMuted.opacity(0.62))
                            )
                            .overlay(
                                Capsule(style: .continuous)
                                    .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                            )
                        }
                        .buttonStyle(TactileButtonStyle(haptic: .alignment))
                        .help(source.domain.isEmpty ? source.title : "\(source.title)\n\(source.domain)")
                    }
                }
                .padding(.vertical, 1)
            }
            .frame(height: 26)
        }
    }

    private func trimmed(_ title: String) -> String {
        title.count > 18 ? String(title.prefix(18)) + "…" : title
    }

    private func open(_ source: SourceItem) {
        guard let url = URL(string: source.url),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else { return }
        NSWorkspace.shared.open(url)
    }
}

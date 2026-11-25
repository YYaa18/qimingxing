import AppKit
import SwiftUI

enum QMXTheme {
    static let accent = Color(red: 0.22, green: 0.74, blue: 0.97) // #38BDF8 sky cyan
    static let success = Color(red: 0.20, green: 0.83, blue: 0.60)
    static let warning = Color(red: 0.98, green: 0.75, blue: 0.14)
    static let danger = Color(red: 0.97, green: 0.44, blue: 0.44)

    static let panelRadius: CGFloat = 16
    static let cardRadius: CGFloat = 12
    static let controlRadius: CGFloat = 8
}

struct VisualEffectBlur: NSViewRepresentable {
    let material: NSVisualEffectView.Material
    let blendingMode: NSVisualEffectView.BlendingMode
    let state: NSVisualEffectView.State

    init(
        material: NSVisualEffectView.Material = .hudWindow,
        blendingMode: NSVisualEffectView.BlendingMode = .behindWindow,
        state: NSVisualEffectView.State = .active
    ) {
        self.material = material
        self.blendingMode = blendingMode
        self.state = state
    }

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = state
        view.isEmphasized = true
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
        nsView.state = state
    }
}

struct HairlineBorderOverlay: View {
    let cornerRadius: CGFloat

    init(cornerRadius: CGFloat = QMXTheme.panelRadius) {
        self.cornerRadius = cornerRadius
    }

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
            .strokeBorder(
                LinearGradient(
                    colors: [
                        Color.white.opacity(0.28),
                        Color.white.opacity(0.08),
                        Color.white.opacity(0.04)
                    ],
                    startPoint: .top,
                    endPoint: .bottom
                ),
                lineWidth: 1
            )
    }
}

struct KeyCapBadge: View {
    let keys: String

    init(_ keys: String) {
        self.keys = keys
    }

    var body: some View {
        Text(keys)
            .font(.system(size: 10, weight: .medium, design: .rounded))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(Color.primary.opacity(0.08))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5)
            )
    }
}

struct PulsingDotView: View {
    let color: Color
    let isPulsing: Bool
    @State private var animate = false

    var body: some View {
        ZStack {
            if isPulsing {
                Circle()
                    .fill(color.opacity(0.45))
                    .frame(width: 14, height: 14)
                    .scaleEffect(animate ? 1.35 : 0.75)
                    .opacity(animate ? 0.15 : 0.65)
                    .animation(.easeInOut(duration: 1.0).repeatForever(autoreverses: true), value: animate)
            }
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
                .shadow(color: color.opacity(0.6), radius: 3)
        }
        .frame(width: 14, height: 14)
        .onAppear {
            if isPulsing { animate = true }
        }
        .onChange(of: isPulsing) { next in
            animate = next
        }
    }
}

struct MarkdownMessageView: View {
    let text: String

    var body: some View {
        let blocks = Self.parseBlocks(text)
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .code(let lang, let code):
                    CodeBlockCard(language: lang, code: code)
                case .prose(let proseText):
                    ProseMarkdownText(proseText)
                }
            }
        }
    }

    private enum Block {
        case prose(String)
        case code(language: String, content: String)
    }

    private static func parseBlocks(_ raw: String) -> [Block] {
        let normalized = raw.replacingOccurrences(of: "\r\n", with: "\n")
        let lines = normalized.components(separatedBy: "\n")
        var result: [Block] = []
        var proseLines: [String] = []
        var codeLines: [String] = []
        var inCode = false
        var codeLang = ""

        func flushProse() {
            let joined = proseLines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty {
                result.append(.prose(joined))
            }
            proseLines.removeAll()
        }

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if !inCode {
                    flushProse()
                    inCode = true
                    codeLang = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                } else {
                    result.append(.code(language: codeLang, content: codeLines.joined(separator: "\n")))
                    codeLines.removeAll()
                    codeLang = ""
                    inCode = false
                }
                continue
            }
            if inCode {
                codeLines.append(line)
            } else {
                proseLines.append(line)
            }
        }

        if inCode {
            result.append(.code(language: codeLang, content: codeLines.joined(separator: "\n")))
        } else {
            flushProse()
        }

        if result.isEmpty && !raw.isEmpty {
            result.append(.prose(raw))
        }
        return result
    }
}

private struct ProseMarkdownText: View {
    let text: String

    init(_ text: String) {
        self.text = text
    }

    var body: some View {
        if let attr = try? AttributedString(
            markdown: text,
            options: AttributedString.MarkdownParsingOptions(
                interpretedSyntax: .inlineOnlyPreservingWhitespace
            )
        ) {
            Text(attr)
                .font(.system(size: 13.5))
                .lineSpacing(3)
                .textSelection(.enabled)
        } else {
            Text(text)
                .font(.system(size: 13.5))
                .lineSpacing(3)
                .textSelection(.enabled)
        }
    }
}

private struct CodeBlockCard: View {
    let language: String
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language)
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    let pb = NSPasteboard.general
                    pb.clearContents()
                    pb.setString(code, forType: .string)
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                        copied = false
                    }
                } label: {
                    Label(copied ? "已复制" : "复制", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 10.5, weight: .medium))
                }
                .buttonStyle(.borderless)
                .foregroundStyle(copied ? QMXTheme.success : .secondary)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color.primary.opacity(0.06))

            Divider().opacity(0.4)

            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(10)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .fill(Color.black.opacity(0.26))
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .strokeBorder(Color.white.opacity(0.1), lineWidth: 1)
        )
    }
}

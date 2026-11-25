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

    static let isHeadlessDump: Bool = ProcessInfo.processInfo.arguments.contains("--dump-ui")
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

    func makeNSView(context: Context) -> NSView {
        if QMXTheme.isHeadlessDump {
            let v = NSView()
            v.wantsLayer = true
            v.layer?.backgroundColor = NSColor(red: 0.09, green: 0.11, blue: 0.16, alpha: 0.98).cgColor
            return v
        }
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = state
        view.isEmphasized = true
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        guard let ve = nsView as? NSVisualEffectView else { return }
        ve.material = material
        ve.blendingMode = blendingMode
        ve.state = state
    }
}

enum QMXButtonVariant {
    case prominent
    case secondary
    case destructive
    case icon
}

struct QMXButtonStyle: ButtonStyle {
    let variant: QMXButtonVariant
    let isActive: Bool
    @State private var isHovering = false

    init(variant: QMXButtonVariant = .secondary, isActive: Bool = false) {
        self.variant = variant
        self.isActive = isActive
    }

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        configuration.label
            .fixedSize(horizontal: true, vertical: false)
            .font(.system(size: 12.5, weight: variant == .prominent ? .semibold : .medium))
            .foregroundStyle(foregroundColor)
            .padding(.horizontal, horizontalPad)
            .padding(.vertical, verticalPad)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(backgroundColor(pressed: pressed))
            )
            .overlay(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .strokeBorder(borderColor, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous))
            .scaleEffect(pressed ? 0.98 : 1.0)
            .onHover { isHovering = $0 }
    }

    private var horizontalPad: CGFloat {
        variant == .icon ? 7 : 11
    }

    private var verticalPad: CGFloat {
        variant == .icon ? 6 : 6
    }

    private var foregroundColor: Color {
        switch variant {
        case .prominent:
            return .white
        case .destructive:
            return QMXTheme.danger
        case .secondary, .icon:
            return isActive ? QMXTheme.accent : .primary
        }
    }

    private func backgroundColor(pressed: Bool) -> Color {
        switch variant {
        case .prominent:
            if pressed { return QMXTheme.accent.opacity(0.75) }
            if isHovering { return QMXTheme.accent.opacity(0.95) }
            return QMXTheme.accent.opacity(0.85)
        case .destructive:
            if pressed { return QMXTheme.danger.opacity(0.28) }
            if isHovering { return QMXTheme.danger.opacity(0.20) }
            return QMXTheme.danger.opacity(0.12)
        case .secondary, .icon:
            if isActive {
                return QMXTheme.accent.opacity(pressed ? 0.28 : 0.18)
            }
            if pressed { return Color.primary.opacity(0.16) }
            if isHovering { return Color.primary.opacity(0.11) }
            return Color.primary.opacity(0.07)
        }
    }

    private var borderColor: Color {
        switch variant {
        case .prominent:
            return Color.white.opacity(0.24)
        case .destructive:
            return QMXTheme.danger.opacity(0.35)
        case .secondary, .icon:
            return isActive ? QMXTheme.accent.opacity(0.45) : Color.primary.opacity(0.12)
        }
    }
}

struct QMXChipButtonStyle: ButtonStyle {
    @State private var isHovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(Color.primary.opacity(configuration.isPressed ? 0.14 : (isHovering ? 0.09 : 0.05)))
            )
            .overlay(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .strokeBorder(Color.primary.opacity(isHovering ? 0.18 : 0.10), lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous))
            .onHover { isHovering = $0 }
    }
}

extension ButtonStyle where Self == QMXButtonStyle {
    static var qmxProminent: QMXButtonStyle { QMXButtonStyle(variant: .prominent) }
    static var qmxSecondary: QMXButtonStyle { QMXButtonStyle(variant: .secondary) }
    static var qmxDestructive: QMXButtonStyle { QMXButtonStyle(variant: .destructive) }
    static var qmxIcon: QMXButtonStyle { QMXButtonStyle(variant: .icon) }
    static func qmxSecondary(active: Bool) -> QMXButtonStyle {
        QMXButtonStyle(variant: .secondary, isActive: active)
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
    let onAccent: Bool

    init(_ keys: String, onAccent: Bool = false) {
        self.keys = keys
        self.onAccent = onAccent
    }

    var body: some View {
        Text(keys)
            .font(.system(size: 10, weight: .semibold, design: .rounded))
            .foregroundStyle(onAccent ? Color.white.opacity(0.92) : Color.secondary)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(onAccent ? Color.white.opacity(0.20) : Color.primary.opacity(0.08))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .strokeBorder(onAccent ? Color.white.opacity(0.25) : Color.primary.opacity(0.12), lineWidth: 0.5)
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

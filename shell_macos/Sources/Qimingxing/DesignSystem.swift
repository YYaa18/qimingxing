import AppKit
import SwiftUI

// Quiet Native design tokens.
// Chrome stays neutral; the only strong contrast is the primary action
// (white on dark, black on light). Color is reserved for state and content.
enum QMXTheme {
    // MARK: Neutral surfaces (light / dark)

    private static func dynamic(light: NSColor, dark: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return isDark ? dark : light
        })
    }

    static let window = dynamic(
        light: NSColor(calibratedWhite: 0.96, alpha: 1.0),
        dark: NSColor(red: 0.039, green: 0.047, blue: 0.063, alpha: 1.0) // #0A0C10
    )
    static let surface = dynamic(
        light: NSColor.white,
        dark: NSColor(red: 0.067, green: 0.078, blue: 0.102, alpha: 1.0) // #11141A
    )
    static let surfaceRaised = dynamic(
        light: NSColor(calibratedWhite: 0.95, alpha: 1.0),
        dark: NSColor(red: 0.102, green: 0.118, blue: 0.149, alpha: 1.0) // #1A1E26
    )
    static let surfaceMuted = dynamic(
        light: NSColor(calibratedWhite: 0.91, alpha: 1.0),
        dark: NSColor(red: 0.137, green: 0.157, blue: 0.200, alpha: 1.0) // #232833
    )
    static let field = dynamic(
        light: NSColor.white,
        dark: NSColor(red: 0.075, green: 0.086, blue: 0.110, alpha: 1.0) // #13161C
    )

    static let hairline = Color.primary.opacity(0.10)
    static let textPrimary = Color.primary
    static let textSecondary = Color.secondary
    static let textTertiary = Color(nsColor: .tertiaryLabelColor)

    // MARK: Primary action inversion

    // Semantic inversion avoids appearance-resolution mismatches: primary is
    // white on dark and black on light, so the fill is Color.primary and the
    // label is the window background.
    static let primaryFill = Color.primary
    static let primaryText = Color(nsColor: .windowBackgroundColor)

    // MARK: System accent + semantic state

    static let accent = Color.accentColor
    static let accentSoft = Color.accentColor.opacity(0.12)
    static let success = Color(red: 0.20, green: 0.83, blue: 0.60) // #34D399
    static let warning = Color(red: 0.98, green: 0.75, blue: 0.14) // #FBBF24
    static let danger = Color(red: 0.97, green: 0.44, blue: 0.44) // #F87171

    // MARK: Radii

    static let commandRadius: CGFloat = 24
    static let settingsRadius: CGFloat = 22
    static let approvalRadius: CGFloat = 20
    static let popoverRadius: CGFloat = 16
    static let cardRadius: CGFloat = 14
    static let controlRadius: CGFloat = 9
    static let keycapRadius: CGFloat = 5

    static let isHeadlessDump: Bool = ProcessInfo.processInfo.arguments.contains("--dump-ui")
}

extension View {
    func qmxCard(
        radius: CGFloat = QMXTheme.cardRadius,
        fill: Color = QMXTheme.surfaceRaised
    ) -> some View {
        self
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(fill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
            )
    }

    func qmxField(radius: CGFloat = QMXTheme.controlRadius) -> some View {
        self
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(QMXTheme.field)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
            )
    }
}

// MARK: - Window material

struct VisualEffectBlur: NSViewRepresentable {
    let material: NSVisualEffectView.Material
    let blendingMode: NSVisualEffectView.BlendingMode
    let state: NSVisualEffectView.State
    let cornerRadius: CGFloat

    init(
        material: NSVisualEffectView.Material = .popover,
        blendingMode: NSVisualEffectView.BlendingMode = .behindWindow,
        state: NSVisualEffectView.State = .active,
        cornerRadius: CGFloat = 0
    ) {
        self.material = material
        self.blendingMode = blendingMode
        self.state = state
        self.cornerRadius = cornerRadius
    }

    func makeNSView(context: Context) -> NSView {
        if QMXTheme.isHeadlessDump {
            let v = NSView()
            v.wantsLayer = true
            let isLight = ProcessInfo.processInfo.arguments.contains("--light")
            let color = isLight
                ? NSColor(calibratedWhite: 0.96, alpha: 1.0)
                : NSColor(red: 0.039, green: 0.047, blue: 0.063, alpha: 1.0)
            v.layer?.backgroundColor = color.cgColor
            applyCornerRadius(to: v)
            return v
        }
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = state
        view.isEmphasized = true
        applyCornerRadius(to: view)
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        applyCornerRadius(to: nsView)
        guard let ve = nsView as? NSVisualEffectView else { return }
        ve.material = material
        ve.blendingMode = blendingMode
        ve.state = state
    }

    private func applyCornerRadius(to view: NSView) {
        guard cornerRadius > 0 else { return }
        view.wantsLayer = true
        view.layer?.cornerRadius = cornerRadius
        view.layer?.cornerCurve = .continuous
        view.layer?.masksToBounds = true
    }
}

// MARK: - Brand mark

/// A flat four-point morning star. No tile, no glow, no gradient.
struct MorningStarShape: Shape {
    func path(in rect: CGRect) -> Path {
        let center = CGPoint(x: rect.midX, y: rect.midY)
        let outer = min(rect.width, rect.height) / 2
        let inner = outer * 0.26
        var path = Path()
        for i in 0..<8 {
            let angle = -Double.pi / 2 + Double(i) * Double.pi / 4
            let radius = i.isMultiple(of: 2) ? outer : inner
            let point = CGPoint(
                x: center.x + CGFloat(cos(angle)) * radius,
                y: center.y + CGFloat(sin(angle)) * radius
            )
            if i == 0 {
                path.move(to: point)
            } else {
                path.addLine(to: point)
            }
        }
        path.closeSubpath()
        return path
    }
}

struct MorningStarMark: View {
    var size: CGFloat = 20
    var color: Color = QMXTheme.textPrimary

    var body: some View {
        MorningStarShape()
            .fill(color)
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

// MARK: - Status

struct StatusDot: View {
    let color: Color
    var size: CGFloat = 6

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

// MARK: - Ambient rim light

/// A slow pearl/ice rim that appears only while the assistant is thinking
/// or listening. Static hairline when idle; degrades to a static fade when
/// Reduce Motion is enabled.
struct AmbientRimLight: View {
    let isActive: Bool
    let cornerRadius: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var spinning = false

    private var gradient: AngularGradient {
        AngularGradient(
            gradient: Gradient(stops: [
                .init(color: .clear, location: 0.00),
                .init(color: QMXTheme.accent.opacity(0.72), location: 0.30),
                .init(color: Color.primary.opacity(0.95), location: 0.50),
                .init(color: QMXTheme.accent.opacity(0.40), location: 0.70),
                .init(color: .clear, location: 1.00),
            ]),
            center: .center
        )
    }

    var body: some View {
        GeometryReader { geo in
            let side = hypot(geo.size.width, geo.size.height) + 8

            gradient
                .frame(width: side, height: side)
                .rotationEffect(.degrees(spinning ? 360 : 0))
                .frame(width: geo.size.width, height: geo.size.height)
                .mask(
                    RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                        .strokeBorder(lineWidth: 1.8)
                )
                .opacity(isActive ? 1.0 : 0.0)
                .animation(.easeInOut(duration: 0.25), value: isActive)
        }
        .allowsHitTesting(false)
        .onAppear { startIfNeeded() }
        .onChange(of: isActive) { _ in startIfNeeded() }
        .onChange(of: reduceMotion) { _ in startIfNeeded() }
    }

    /// Core Animation rotates the gradient layer; no per-frame SwiftUI body
    /// rebuilds, which keeps the rim smooth and cheap.
    private func startIfNeeded() {
        if isActive && !reduceMotion {
            spinning = false
            withAnimation(.linear(duration: 2.8).repeatForever(autoreverses: false)) {
                spinning = true
            }
        } else {
            withAnimation(nil) {
                spinning = false
            }
        }
    }
}

// MARK: - Globe spinner

/// A small dotted sphere that rotates like a loading globe. Used while the
/// assistant is thinking and for running background tasks.
struct GlobeSpinner: View {
    var size: CGFloat = 18
    var color: Color = QMXTheme.textSecondary
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 24.0, paused: reduceMotion)) { context in
            Canvas { graphics, canvasSize in
                let elapsed = context.date.timeIntervalSinceReferenceDate
                let angle = reduceMotion
                    ? 0.7
                    : elapsed.truncatingRemainder(dividingBy: 2.6) / 2.6 * .pi * 2
                draw(graphics, size: canvasSize, angle: angle)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private func draw(_ graphics: GraphicsContext, size: CGSize, angle: Double) {
        let radius = Double(min(size.width, size.height)) / 2 - 1
        guard radius > 1 else { return }
        let center = CGPoint(x: size.width / 2, y: size.height / 2)
        let tilt = 0.42
        let unit = Double(size.width) / 18

        for latitude in stride(from: -60.0, through: 60.0, by: 30.0) {
            let phi = latitude * .pi / 180
            let ring = cos(phi) * radius
            let y0 = sin(phi) * radius
            let count = max(5, Int(ring / radius * 10))
            guard count > 0 else { continue }

            for index in 0..<count {
                let theta = 2 * .pi * Double(index) / Double(count) + angle
                let x = cos(theta) * ring
                let z = sin(theta) * ring
                let y = y0 * cos(tilt) - z * sin(tilt)
                let depthZ = y0 * sin(tilt) + z * cos(tilt)
                let depth = (depthZ / radius + 1) / 2
                let alpha = 0.14 + 0.86 * depth
                let dot = (0.70 + 0.80 * depth) * unit
                let point = CGPoint(x: center.x + CGFloat(x), y: center.y + CGFloat(y))
                graphics.fill(
                    Path(ellipseIn: CGRect(
                        x: point.x - dot,
                        y: point.y - dot,
                        width: dot * 2,
                        height: dot * 2
                    )),
                    with: .color(color.opacity(alpha))
                )
            }
        }
    }
}

// MARK: - Fast tooltips

/// A lightweight tooltip that appears after a short hover delay, instead of
/// waiting for the system HelpTag (which can take seconds).
private struct QMXFastTooltip: ViewModifier {
    let text: String
    @State private var visible = false
    @State private var hoverTask: Task<Void, Never>?

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .top) {
                if visible {
                    Text(text)
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 4)
                        .background(
                            Capsule()
                                .fill(QMXTheme.surfaceRaised)
                        )
                        .overlay(
                            Capsule()
                                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
                        )
                        .fixedSize()
                        .offset(y: -26)
                        .transition(.opacity)
                        .allowsHitTesting(false)
                }
            }
            .onHover { hovering in
                hoverTask?.cancel()
                if hovering {
                    hoverTask = Task { @MainActor in
                        try? await Task.sleep(nanoseconds: 160_000_000)
                        if Task.isCancelled { return }
                        withAnimation(.easeOut(duration: 0.10)) {
                            visible = true
                        }
                    }
                } else {
                    withAnimation(.easeOut(duration: 0.08)) {
                        visible = false
                    }
                }
            }
            .onDisappear {
                hoverTask?.cancel()
                visible = false
            }
    }
}

extension View {
    func qmxTooltip(_ text: String) -> some View {
        modifier(QMXFastTooltip(text: text))
    }
}

// MARK: - Emotion badge

/// Native expression indicator for the latest answer. The model's emotion
/// event is rendered here instead of leaking into the spoken text.
struct EmotionBadge: View {
    let emotion: String

    private var style: (symbol: String, label: String, color: Color) {
        switch emotion {
        case "happy":
            return ("face.smiling", "开心", QMXTheme.success)
        case "thinking":
            return ("brain.head.profile", "思考", QMXTheme.accent)
        case "surprised":
            return ("exclamationmark.bubble", "惊讶", QMXTheme.warning)
        case "confused":
            return ("questionmark.circle", "困惑", QMXTheme.warning)
        case "warning":
            return ("exclamationmark.triangle", "关切", QMXTheme.danger)
        default:
            return ("circle.dotted", "平静", QMXTheme.textTertiary)
        }
    }

    var body: some View {
        let style = style
        HStack(spacing: 4) {
            Image(systemName: style.symbol)
                .font(.system(size: 10, weight: .medium))
            Text(style.label)
                .font(.system(size: 10.5, weight: .medium))
        }
        .foregroundStyle(style.color)
        .padding(.horizontal, 7)
        .frame(height: 20)
        .background(
            Capsule()
                .fill(style.color.opacity(0.12))
        )
        .animation(.easeInOut(duration: 0.2), value: emotion)
        .help("启明星此刻的情绪")
        .accessibilityLabel("情绪：\(style.label)")
    }
}

// MARK: - Tactile buttons

struct TactileButtonStyle: ButtonStyle {
    var haptic: NSHapticFeedbackManager.FeedbackPattern? = .alignment
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(reduceMotion ? 1.0 : (configuration.isPressed ? 0.97 : 1.0))
            .opacity(configuration.isPressed ? 0.88 : 1.0)
            .animation(.spring(response: 0.22, dampingFraction: 0.72), value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { pressed in
                if pressed, let pattern = haptic {
                    NSHapticFeedbackManager.defaultPerformer.perform(pattern, performanceTime: .now)
                }
            }
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(variant: QMXButtonVariant = .secondary, isActive: Bool = false) {
        self.variant = variant
        self.isActive = isActive
    }

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        configuration.label
            .fixedSize(horizontal: true, vertical: false)
            .font(.system(size: 13, weight: variant == .prominent ? .medium : .regular))
            .foregroundStyle(foregroundColor)
            .padding(.horizontal, horizontalPad)
            .padding(.vertical, verticalPad)
            .frame(
                width: variant == .icon ? 28 : nil,
                height: variant == .icon ? 28 : nil
            )
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(backgroundColor(pressed: pressed))
            )
            .overlay(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .strokeBorder(borderColor, lineWidth: 0.5)
            )
            .contentShape(RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous))
            .scaleEffect(reduceMotion ? 1.0 : (pressed ? 0.98 : 1.0))
            .animation(.easeOut(duration: 0.12), value: isHovering)
            .animation(.easeOut(duration: 0.08), value: pressed)
            .onHover { isHovering = $0 }
            .onChange(of: pressed) { isPressed in
                if isPressed, variant == .prominent || variant == .destructive {
                    NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
                }
            }
    }

    private var horizontalPad: CGFloat {
        variant == .icon ? 0 : 12
    }

    private var verticalPad: CGFloat {
        variant == .icon ? 0 : 7
    }

    private var foregroundColor: Color {
        switch variant {
        case .prominent:
            return QMXTheme.primaryText
        case .destructive:
            return QMXTheme.danger
        case .secondary, .icon:
            return isActive ? QMXTheme.accent : QMXTheme.textPrimary
        }
    }

    private func backgroundColor(pressed: Bool) -> Color {
        switch variant {
        case .prominent:
            return pressed ? QMXTheme.primaryFill.opacity(0.82) : QMXTheme.primaryFill
        case .destructive:
            if pressed { return QMXTheme.danger.opacity(0.20) }
            if isHovering { return QMXTheme.danger.opacity(0.14) }
            return QMXTheme.danger.opacity(0.08)
        case .secondary, .icon:
            if isActive { return QMXTheme.accent.opacity(pressed ? 0.18 : 0.10) }
            if pressed { return QMXTheme.surfaceMuted }
            if isHovering { return QMXTheme.surfaceMuted.opacity(0.72) }
            return variant == .icon ? .clear : QMXTheme.surfaceRaised
        }
    }

    private var borderColor: Color {
        switch variant {
        case .prominent:
            return QMXTheme.hairline
        case .destructive:
            return QMXTheme.danger.opacity(0.24)
        case .secondary, .icon:
            return isActive ? QMXTheme.accent.opacity(0.28) : (variant == .icon ? .clear : QMXTheme.hairline)
        }
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

struct QMXChipButtonStyle: ButtonStyle {
    @State private var isHovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13))
            .foregroundStyle(isHovering ? QMXTheme.textPrimary : QMXTheme.textSecondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .fill(configuration.isPressed ? QMXTheme.surfaceMuted : QMXTheme.surfaceRaised)
            )
            .overlay(
                RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                    .strokeBorder(isHovering ? QMXTheme.accent.opacity(0.22) : QMXTheme.hairline, lineWidth: 0.5)
            )
            .contentShape(RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous))
            .animation(.easeOut(duration: 0.12), value: isHovering)
            .onHover { isHovering = $0 }
    }
}

// MARK: - Small details

struct KeyCapBadge: View {
    let keys: String
    let onAccent: Bool

    init(_ keys: String, onAccent: Bool = false) {
        self.keys = keys
        self.onAccent = onAccent
    }

    var body: some View {
        Text(keys)
            .font(.system(size: 10, weight: .medium, design: .monospaced))
            .foregroundStyle(onAccent ? QMXTheme.primaryText.opacity(0.7) : QMXTheme.textTertiary)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(
                RoundedRectangle(cornerRadius: QMXTheme.keycapRadius, style: .continuous)
                    .fill(onAccent ? QMXTheme.primaryText.opacity(0.10) : QMXTheme.surfaceMuted)
            )
            .overlay(
                RoundedRectangle(cornerRadius: QMXTheme.keycapRadius, style: .continuous)
                    .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
            )
    }
}

struct HairlineBorderOverlay: View {
    let cornerRadius: CGFloat

    init(cornerRadius: CGFloat = QMXTheme.commandRadius) {
        self.cornerRadius = cornerRadius
    }

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
            .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
    }
}

// MARK: - Markdown

@discardableResult
func qmxOpenSafeURL(_ url: URL) -> OpenURLAction.Result {
    guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
        return .discarded
    }
    NSWorkspace.shared.open(url)
    return .handled
}

/// Inline markdown parsing is pure and hot: the streaming answer re-renders
/// several blocks per frame. Cache by text so completed blocks are free and
/// only the currently growing block pays a parse.
@MainActor
private enum QMXInlineMarkdownCache {
    final class Box: NSObject {
        let value: AttributedString
        init(_ value: AttributedString) { self.value = value }
    }

    static let cache: NSCache<NSString, Box> = {
        let cache = NSCache<NSString, Box>()
        cache.countLimit = 48
        return cache
    }()

    static func attributed(for text: String) -> AttributedString? {
        let key = text as NSString
        if let hit = cache.object(forKey: key) {
            return hit.value
        }
        guard let parsed = try? AttributedString(
            markdown: text,
            options: AttributedString.MarkdownParsingOptions(
                interpretedSyntax: .inlineOnlyPreservingWhitespace
            )
        ) else {
            return nil
        }
        cache.setObject(Box(parsed), forKey: key)
        return parsed
    }
}

struct QMXTextSelectable: ViewModifier {
    let enabled: Bool

    @ViewBuilder
    func body(content: Content) -> some View {
        if enabled {
            content.textSelection(.enabled)
        } else {
            content
        }
    }
}

struct InlineMarkdownText: View {
    let text: String
    var fontSize: CGFloat = 14
    var lineSpacing: CGFloat = 5
    var weight: Font.Weight = .regular
    var selectable: Bool = true

    var body: some View {
        Group {
            if let attr = QMXInlineMarkdownCache.attributed(for: text) {
                Text(attr)
            } else {
                Text(text)
            }
        }
        .font(.system(size: fontSize, weight: weight))
        .lineSpacing(lineSpacing)
        .modifier(QMXTextSelectable(enabled: selectable))
        .environment(\.openURL, OpenURLAction { qmxOpenSafeURL($0) })
    }
}

struct MarkdownMessageView: View {
    let text: String

    var body: some View {
        let blocks = Self.parseBlocks(text)
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .code(let lang, let code):
                    CodeBlockCard(language: lang, code: code)
                case .prose(let proseText):
                    ProseMarkdownText(proseText)
                case .table(let table):
                    QMXTableNotice(table: table)
                }
            }
        }
    }

    private enum Block {
        case prose(String)
        case code(language: String, content: String)
        case table(QMXMarkdownTable)
    }

    private static func parseBlocks(_ raw: String) -> [Block] {
        let normalized = raw.replacingOccurrences(of: "\r\n", with: "\n")
        let lines = normalized.components(separatedBy: "\n")
        let tables = QMXMarkdownTables.scan(normalized)
        var tableByStart: [Int: QMXMarkdownTable] = [:]
        for table in tables {
            tableByStart[table.startLine] = table
        }
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

        var index = 0
        while index < lines.count {
            if let table = tableByStart[index] {
                flushProse()
                result.append(.table(table))
                index = table.endLine + 1
                continue
            }
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
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
                index += 1
                continue
            }
            if inCode {
                codeLines.append(line)
            } else {
                proseLines.append(line)
            }
            index += 1
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
        InlineMarkdownText(text: text, fontSize: 14, lineSpacing: 5)
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
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundStyle(QMXTheme.textTertiary)
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
                        .font(.system(size: 11))
                }
                .buttonStyle(.plain)
                .foregroundStyle(copied ? QMXTheme.success : QMXTheme.textTertiary)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(Color.primary.opacity(0.04))

            Divider().opacity(0.5)

            Text(code)
                .font(.system(size: 12, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
        }
        .background(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .fill(Color.primary.opacity(0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: QMXTheme.controlRadius, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }
}

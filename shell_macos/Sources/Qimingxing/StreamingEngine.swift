import AppKit
import CoreVideo
import Foundation
import SwiftUI

// MARK: - DisplayLink Driver

/// Bridges CVDisplayLink (or Reduce-Motion-aware timer fallback) onto the main
/// actor so both AdaptiveStreamRevealer and MonotonicPanelHeightEngine tick in
/// lockstep with the display refresh rate instead of fixed 30Hz/60Hz sleeps.
@MainActor
final class DisplayLinkDriver {
    private final class CallbackBox: @unchecked Sendable {
        weak var driver: DisplayLinkDriver?
    }

    nonisolated(unsafe) private var displayLink: CVDisplayLink?
    private var callbackBox: CallbackBox?
    private var lastTickUptime: TimeInterval = 0
    private var onTick: (@MainActor (TimeInterval) -> Void)?
    nonisolated(unsafe) private var fallbackTimer: Timer?

    var isRunning: Bool {
        displayLink != nil || fallbackTimer != nil
    }

    func start(onTick: @escaping @MainActor (TimeInterval) -> Void) {
        self.onTick = onTick
        guard !isRunning else { return }
        lastTickUptime = ProcessInfo.processInfo.systemUptime

        var link: CVDisplayLink?
        let status = CVDisplayLinkCreateWithActiveCGDisplays(&link)
        guard status == kCVReturnSuccess, let link else {
            startFallbackTimer()
            return
        }

        let box = CallbackBox()
        box.driver = self
        self.callbackBox = box

        let userInfo = Unmanaged.passUnretained(box).toOpaque()
        let setResult = CVDisplayLinkSetOutputCallback(
            link,
            { _, _, _, _, _, userInfo in
                guard let userInfo else { return kCVReturnSuccess }
                let box = Unmanaged<CallbackBox>.fromOpaque(userInfo).takeUnretainedValue()
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        box.driver?.handleFrameTick()
                    }
                }
                return kCVReturnSuccess
            },
            userInfo
        )

        if setResult == kCVReturnSuccess, CVDisplayLinkStart(link) == kCVReturnSuccess {
            self.displayLink = link
        } else {
            self.callbackBox = nil
            startFallbackTimer()
        }
    }

    func stop() {
        if let link = displayLink {
            CVDisplayLinkStop(link)
            displayLink = nil
        }
        callbackBox?.driver = nil
        callbackBox = nil
        fallbackTimer?.invalidate()
        fallbackTimer = nil
        onTick = nil
    }

    private func startFallbackTimer() {
        let timer = Timer(timeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.handleFrameTick()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        fallbackTimer = timer
    }

    private func handleFrameTick() {
        guard isRunning, let onTick else { return }
        let now = ProcessInfo.processInfo.systemUptime
        let rawDt = now - lastTickUptime
        lastTickUptime = now
        let dt = min(max(rawDt, 1.0 / 240.0), 0.05)
        onTick(dt)
    }

    deinit {
        if let link = displayLink {
            CVDisplayLinkStop(link)
        }
        fallbackTimer?.invalidate()
    }
}

// MARK: - Adaptive Stream Revealer

/// Display-refresh-synchronized adaptive character revealer.
/// Uses `[Character]` (Unicode grapheme clusters) so CJK and ZWJ emoji never
/// split mid-codepoint, smoothly catches up on network bursts, and sprints to
/// completion within ~150-220ms after `output.turn_end`.
@MainActor
final class AdaptiveStreamRevealer: ObservableObject {
    @Published private(set) var revealedText: String = ""
    @Published private(set) var isRevealing: Bool = false

    private(set) var activeTurnId: String = ""
    private var fullCharacters: [Character] = []
    private(set) var revealedCount: Int = 0
    private var fractionalCarry: Double = 0.0
    private var serverTurnEnded: Bool = false
    private var onFinishCallback: (() -> Void)?
    private var didFireFinish: Bool = false

    private let driver = DisplayLinkDriver()

    // Velocity tuning parameters
    private let baseCPS: Double = 55.0
    private let catchUpTau: Double = 0.25
    private let finishTau: Double = 0.14
    private let maxCharsPerFrame: Int = 28

    var backlogCount: Int {
        max(0, fullCharacters.count - revealedCount)
    }

    var totalCharacterCount: Int {
        fullCharacters.count
    }

    func reset(turnId: String, onFinish: (() -> Void)? = nil) {
        driver.stop()
        activeTurnId = turnId
        fullCharacters = []
        revealedCount = 0
        fractionalCarry = 0.0
        serverTurnEnded = false
        didFireFinish = false
        revealedText = ""
        isRevealing = false
        onFinishCallback = onFinish
    }

    func updateOnFinish(_ onFinish: @escaping () -> Void) {
        onFinishCallback = onFinish
    }

    /// Appends an incremental delta for `turnId`.
    func appendDelta(_ delta: String, turnId: String) {
        guard turnId == activeTurnId, !delta.isEmpty else { return }
        fullCharacters.append(contentsOf: delta)
        handleBufferUpdated()
    }

    /// Synchronizes full accumulated answer text from `BrainClient` without
    /// discarding already-revealed progress when the string only appends.
    func updateFullText(_ fullText: String, turnId: String) {
        guard turnId == activeTurnId else { return }
        let nextChars = Array(fullText)
        if nextChars.count < revealedCount || !startsWithRevealed(nextChars) {
            revealedCount = 0
            revealedText = ""
            fractionalCarry = 0.0
        }
        fullCharacters = nextChars
        handleBufferUpdated()
    }

    /// Marks the server stream as ended (`output.turn_end` / `isStreaming == false`).
    func markServerTurnEnded(turnId: String) {
        guard turnId == activeTurnId else { return }
        serverTurnEnded = true

        if shouldBypassAnimation || revealedCount >= fullCharacters.count {
            flushImmediately()
        } else if !driver.isRunning {
            isRevealing = true
            startDriver()
        }
    }

    func stop() {
        driver.stop()
        isRevealing = false
    }

    /// Advances revealed characters for frame interval `dt` (seconds).
    /// Exposed internally for deterministic testing.
    func tick(dt: TimeInterval) {
        guard isRevealing else { return }
        let clampedDt = min(max(dt, 1.0 / 240.0), 0.05)
        let backlog = fullCharacters.count - revealedCount

        guard backlog > 0 else {
            if serverTurnEnded {
                completeReveal()
            } else {
                driver.stop()
                isRevealing = false
            }
            return
        }

        let targetCPS = currentTargetCPS(backlog: backlog)
        fractionalCarry += targetCPS * clampedDt
        let step = min(backlog, min(maxCharsPerFrame, Int(floor(fractionalCarry))))

        if step > 0 {
            fractionalCarry -= Double(step)
            revealedCount += step
            revealedText = String(fullCharacters.prefix(revealedCount))
        }

        if serverTurnEnded && revealedCount >= fullCharacters.count {
            completeReveal()
        }
    }

    /// Computes target reveal speed in characters/second for a given backlog.
    func currentTargetCPS(backlog: Int) -> Double {
        if serverTurnEnded {
            return max(120.0, Double(backlog) / finishTau)
        } else if backlog <= 15 {
            return baseCPS
        } else {
            return max(baseCPS, Double(backlog) / catchUpTau)
        }
    }

    private var shouldBypassAnimation: Bool {
        QMXTheme.isHeadlessDump || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    private func startsWithRevealed(_ candidate: [Character]) -> Bool {
        guard revealedCount > 0 else { return true }
        guard candidate.count >= revealedCount else { return false }
        for i in 0..<revealedCount where candidate[i] != fullCharacters[i] {
            return false
        }
        return true
    }

    private func handleBufferUpdated() {
        if shouldBypassAnimation {
            flushImmediately()
            return
        }
        if revealedCount < fullCharacters.count {
            isRevealing = true
            startDriver()
        } else if serverTurnEnded {
            completeReveal()
        }
    }

    private func startDriver() {
        guard !driver.isRunning else { return }
        driver.start { [weak self] dt in
            self?.tick(dt: dt)
        }
    }

    private func flushImmediately() {
        driver.stop()
        revealedCount = fullCharacters.count
        revealedText = String(fullCharacters)
        if serverTurnEnded {
            completeReveal()
        } else {
            isRevealing = false
        }
    }

    private func completeReveal() {
        driver.stop()
        isRevealing = false
        fractionalCarry = 0.0
        guard !didFireFinish else { return }
        didFireFinish = true
        onFinishCallback?()
    }
}

// MARK: - Streaming Markdown Sanitizer

/// Non-destructive O(n) pre-completion for an active streaming paragraph so
/// partial `**bold`, `` `code ``, `~~strike~~`, and `[link](url` never flash
/// raw markdown punctuation mid-stream.
enum StreamingMarkdownSanitizer {
    static func sanitizeInline(_ raw: String) -> String {
        guard !raw.isEmpty else { return raw }

        // 1. Strip/resolve incomplete trailing link or image syntax
        var text = stripIncompleteTrailingLink(raw)

        // 2. Single-pass scan for inline code backticks and emphasis tokens
        var inInlineCode = false
        var codeTickCount = 0
        var lastCodeOpenIndex: Int?
        var styleStack: [String] = []

        let chars = Array(text)
        var i = 0
        while i < chars.count {
            let ch = chars[i]

            if ch == "\\" && i + 1 < chars.count {
                i += 2
                continue
            }

            if ch == "`" {
                var count = 0
                while i + count < chars.count && chars[i + count] == "`" {
                    count += 1
                }
                if !inInlineCode {
                    inInlineCode = true
                    codeTickCount = count
                    lastCodeOpenIndex = i
                } else if count == codeTickCount {
                    inInlineCode = false
                    codeTickCount = 0
                    lastCodeOpenIndex = nil
                }
                i += count
                continue
            }

            if inInlineCode {
                i += 1
                continue
            }

            if let token = matchStyleToken(chars: chars, at: i) {
                if styleStack.last == token {
                    styleStack.removeLast()
                } else {
                    styleStack.append(token)
                }
                i += token.count
                continue
            }

            i += 1
        }

        // 3. Hide dangling just-opened backticks at the very end (e.g. "执行 `" with no code body yet)
        if inInlineCode, let openIdx = lastCodeOpenIndex, openIdx + codeTickCount == chars.count {
            text.removeLast(codeTickCount)
            inInlineCode = false
            codeTickCount = 0
        }

        // 4. Hide dangling just-opened emphasis marker at the very end (e.g. "关键是 **")
        if !inInlineCode, let lastToken = styleStack.last, text.hasSuffix(lastToken) {
            text.removeLast(lastToken.count)
            styleStack.removeLast()
        }

        // Also trim a trailing lone line-start "-" or "*" that hasn't become a list item yet
        if let lastNewline = text.lastIndex(of: "\n") {
            let tail = text[text.index(after: lastNewline)...]
            if tail == "-" || tail == "*" {
                text = String(text[...lastNewline])
            }
        } else if text == "-" || text == "*" {
            text = ""
        }

        // 5. Close open inline code span
        if inInlineCode, codeTickCount > 0 {
            text += String(repeating: "`", count: codeTickCount)
        }

        // 6. Close remaining emphasis markers in LIFO order
        for token in styleStack.reversed() {
            text += token
        }

        return text
    }

    private static func matchStyleToken(chars: [Character], at i: Int) -> String? {
        let ch = chars[i]
        // Do not treat list bullet "* " at line start as italic
        if ch == "*" && (i == 0 || chars[i - 1] == "\n") && (i + 1 < chars.count && chars[i + 1] == " ") {
            return nil
        }
        // Do not treat lone "*" at line start with no following character as italic
        if ch == "*" && (i == 0 || chars[i - 1] == "\n") && i + 1 == chars.count {
            return nil
        }
        if i + 1 < chars.count {
            let pair = String([ch, chars[i + 1]])
            if pair == "**" || pair == "__" || pair == "~~" {
                return pair
            }
        }
        if ch == "*" {
            return "*"
        }
        return nil
    }

    private static func stripIncompleteTrailingLink(_ input: String) -> String {
        // Incomplete ![alt](url... or [label](url...
        let urlPattern = #"!?\[([^\]]*)\]\([^)\n]*$"#
        if let range = input.range(of: urlPattern, options: .regularExpression) {
            let matched = String(input[range])
            if matched.hasPrefix("!") {
                return String(input[..<range.lowerBound])
            }
            if let closeBracket = matched.firstIndex(of: "]") {
                let labelStart = matched.index(after: matched.startIndex)
                let label = String(matched[labelStart..<closeBracket])
                return String(input[..<range.lowerBound]) + label
            }
        }
        // Incomplete ![alt or [label before "](" arrives
        let bracketPattern = #"!?\[([^\]\n]*)$"#
        if let range = input.range(of: bracketPattern, options: .regularExpression) {
            let matched = String(input[range])
            if matched.hasPrefix("!") {
                return String(input[..<range.lowerBound])
            }
            let label = String(matched.dropFirst())
            return String(input[..<range.lowerBound]) + label
        }
        return input
    }
}

// MARK: - Incremental Block Parser & Streaming Table Detector

enum MarkdownBlockKind: Equatable {
    case paragraph(String)
    case heading(level: Int, text: String)
    case listItem(prefix: String, text: String)
    case blockquote(String)
    case codeBlock(language: String, code: String, isClosed: Bool)
    case inlineTable(
        tableIndex: Int,
        title: String,
        headers: [String],
        rows: [[String]],
        totalRowCount: Int,
        isStreaming: Bool,
        isTruncated: Bool,
        rawMarkdown: String
    )
    case tableNotice(
        tableIndex: Int,
        title: String,
        colCount: Int,
        rowCount: Int,
        isStreaming: Bool,
        rawMarkdown: String
    )
}

struct MarkdownBlock: Identifiable, Equatable {
    let id: String
    let kind: MarkdownBlockKind
    let isSettled: Bool
}

enum StreamingBlockParser {
    static let inlineMaxColumns = 3
    static let inlineMaxRows = 6

    struct ParseResult: Equatable {
        let settled: [MarkdownBlock]
        let activeTail: MarkdownBlock?
        let hasAnyTable: Bool
    }

    static func parse(_ raw: String, turnId: String, isFinished: Bool) -> ParseResult {
        guard !raw.isEmpty else {
            return ParseResult(settled: [], activeTail: nil, hasAnyTable: false)
        }

        let normalized = raw.replacingOccurrences(of: "\r\n", with: "\n")
        let hasTrailingNewline = normalized.hasSuffix("\n")
        let lines = normalized.components(separatedBy: "\n")

        var blocks: [MarkdownBlock] = []
        var blockCounter = 0
        var tableCounter = 0
        var hasAnyTable = false

        func nextId() -> String {
            let id = "\(turnId)-b\(blockCounter)"
            blockCounter += 1
            return id
        }

        var proseLines: [String] = []
        var quoteLines: [String] = []
        var inCard = false

        var inCode = false
        var codeFenceMarker = "```"
        var codeLanguage = ""
        var codeLines: [String] = []

        var candidateHeaderLine: String?
        var candidateHeaderLineIndex: Int = 0

        var inTable = false
        var tableTitle = ""
        var tableHeaders: [String] = []
        var tableRows: [[String]] = []
        var tableRawLines: [String] = []

        func flushProse(settled: Bool) {
            let joined = proseLines
                .joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            proseLines.removeAll()
            guard !joined.isEmpty else { return }
            blocks.append(MarkdownBlock(id: nextId(), kind: .paragraph(joined), isSettled: settled))
        }

        func flushQuote(settled: Bool) {
            let joined = quoteLines
                .joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            quoteLines.removeAll()
            guard !joined.isEmpty else { return }
            blocks.append(MarkdownBlock(id: nextId(), kind: .blockquote(joined), isSettled: settled))
        }

        func flushTable(settled: Bool) {
            guard inTable else { return }
            hasAnyTable = true
            let idx = tableCounter
            tableCounter += 1
            let title = tableTitle.isEmpty ? "表格 \(idx + 1)" : tableTitle
            let rawMd = tableRawLines.joined(separator: "\n")
            let colCount = tableHeaders.count
            let totalRows = tableRows.count

            if colCount > inlineMaxColumns {
                blocks.append(
                    MarkdownBlock(
                        id: nextId(),
                        kind: .tableNotice(
                            tableIndex: idx,
                            title: title,
                            colCount: colCount,
                            rowCount: totalRows,
                            isStreaming: !settled,
                            rawMarkdown: rawMd
                        ),
                        isSettled: settled
                    )
                )
            } else {
                let cappedRows = Array(tableRows.prefix(inlineMaxRows)).map { row in
                    (0..<colCount).map { col in col < row.count ? row[col] : "" }
                }
                blocks.append(
                    MarkdownBlock(
                        id: nextId(),
                        kind: .inlineTable(
                            tableIndex: idx,
                            title: title,
                            headers: tableHeaders,
                            rows: cappedRows,
                            totalRowCount: totalRows,
                            isStreaming: !settled,
                            isTruncated: totalRows > inlineMaxRows,
                            rawMarkdown: rawMd
                        ),
                        isSettled: settled
                    )
                )
            }

            inTable = false
            tableTitle = ""
            tableHeaders = []
            tableRows = []
            tableRawLines = []
        }

        for index in 0..<lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let isActiveLastLine = !isFinished && !hasTrailingNewline && index == lines.count - 1

            // Skip card markers (same validity rule as the brain parser:
            // `<<<CARD` must be followed by whitespace or `>>>`). A malformed
            // tag stays visible instead of swallowing the rest of the answer.
            let upper = trimmed.uppercased()
            if let cardRange = upper.range(of: "<<<CARD") {
                let rest = upper[cardRange.upperBound...]
                if let first = rest.first, first.isWhitespace || first == ">" {
                    inCard = true
                    continue
                }
            }
            if upper.contains("<<<END_CARD>>>") {
                inCard = false
                continue
            }
            if inCard {
                continue
            }

            // Inside fenced code block
            if inCode {
                if !isActiveLastLine, isClosingFence(trimmed, marker: codeFenceMarker) {
                    let code = codeLines.joined(separator: "\n")
                    blocks.append(
                        MarkdownBlock(
                            id: nextId(),
                            kind: .codeBlock(language: codeLanguage, code: code, isClosed: true),
                            isSettled: true
                        )
                    )
                    codeLines.removeAll()
                    codeLanguage = ""
                    inCode = false
                } else {
                    codeLines.append(line)
                }
                continue
            }

            // Check candidate header resolution
            if let headerLine = candidateHeaderLine {
                if isActiveLastLine {
                    if isStreamingSeparatorMatch(trimmed, expectedCols: QMXMarkdownTables.splitRow(headerLine).count) {
                        flushProse(settled: true)
                        flushQuote(settled: true)
                        inTable = true
                        tableHeaders = Array(QMXMarkdownTables.splitRow(headerLine).prefix(15)).map { String($0.prefix(160)) }
                        tableTitle = nearestTitle(lines: lines, before: candidateHeaderLineIndex) ?? ""
                        tableRows = []
                        tableRawLines = [headerLine, line]
                        candidateHeaderLine = nil
                        continue
                    } else if couldStillBecomeSeparator(trimmed) {
                        // Hold buffered until more characters arrive
                        continue
                    } else {
                        // False positive: revert headerLine back to prose
                        proseLines.append(headerLine)
                        candidateHeaderLine = nil
                    }
                } else {
                    if QMXMarkdownTables.isSeparator(trimmed),
                       QMXMarkdownTables.splitRow(headerLine).count == QMXMarkdownTables.splitRow(trimmed).count {
                        flushProse(settled: true)
                        flushQuote(settled: true)
                        inTable = true
                        tableHeaders = Array(QMXMarkdownTables.splitRow(headerLine).prefix(15)).map { String($0.prefix(160)) }
                        tableTitle = nearestTitle(lines: lines, before: candidateHeaderLineIndex) ?? ""
                        tableRows = []
                        tableRawLines = [headerLine, line]
                        candidateHeaderLine = nil
                        continue
                    } else {
                        proseLines.append(headerLine)
                        candidateHeaderLine = nil
                    }
                }
            }

            // Inside active streaming table
            if inTable {
                if trimmed.isEmpty || !trimmed.contains("|") || trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                    flushTable(settled: true)
                    // Fall through to process this line normally
                } else {
                    if QMXMarkdownTables.isSeparator(trimmed) {
                        tableRawLines.append(line)
                        continue
                    }
                    let parsedRow = QMXMarkdownTables.splitRow(trimmed)
                    // Data row debounce: wait until active last line has expected column count or newline
                    if isActiveLastLine && tableHeaders.count <= inlineMaxColumns && parsedRow.count < tableHeaders.count {
                        continue
                    }
                    tableRows.append(parsedRow.map { String($0.prefix(160)) })
                    tableRawLines.append(line)
                    continue
                }
            }

            // Partial backticks at start of active line (e.g. "`" or "``" before third "`")
            if isActiveLastLine && (trimmed == "`" || trimmed == "``" || trimmed == "~" || trimmed == "~~") {
                continue
            }

            // Code block opening fence
            if let (marker, lang) = openingFence(trimmed) {
                flushProse(settled: true)
                flushQuote(settled: true)
                inCode = true
                codeFenceMarker = marker
                codeLanguage = lang
                codeLines.removeAll()
                continue
            }

            // Potential table header line
            if trimmed.contains("|"),
               !trimmed.hasPrefix("#"),
               !trimmed.hasPrefix(">") {
                // If active line starts with "|", buffer immediately so bare pipes never flash
                if isActiveLastLine {
                    if trimmed.hasPrefix("|") {
                        candidateHeaderLine = line
                        candidateHeaderLineIndex = index
                        continue
                    }
                } else {
                    candidateHeaderLine = line
                    candidateHeaderLineIndex = index
                    continue
                }
            }

            // Blank line settles current paragraph / quote
            if trimmed.isEmpty {
                flushProse(settled: true)
                flushQuote(settled: true)
                continue
            }

            // Heading line
            if !isActiveLastLine || hasTrailingNewline, let (level, headingText) = parseHeading(trimmed) {
                flushProse(settled: true)
                flushQuote(settled: true)
                blocks.append(
                    MarkdownBlock(
                        id: nextId(),
                        kind: .heading(level: level, text: headingText),
                        isSettled: true
                    )
                )
                continue
            }

            // Blockquote line
            if trimmed.hasPrefix(">") {
                flushProse(settled: true)
                var body = String(trimmed.dropFirst())
                if body.hasPrefix(" ") { body.removeFirst() }
                quoteLines.append(body)
                continue
            } else {
                flushQuote(settled: true)
            }

            // List item line (settle completed line immediately so each item caches independently)
            if !isActiveLastLine, let (prefix, itemText) = parseListItem(trimmed) {
                flushProse(settled: true)
                blocks.append(
                    MarkdownBlock(
                        id: nextId(),
                        kind: .listItem(prefix: prefix, text: itemText),
                        isSettled: true
                    )
                )
                continue
            }

            proseLines.append(trimmed)
        }

        // Flush remaining open structures
        if let headerLine = candidateHeaderLine {
            if isFinished {
                proseLines.append(headerLine)
            } else if !headerLine.trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                proseLines.append(headerLine)
            }
        }

        if inTable {
            flushTable(settled: isFinished)
        } else if inCode {
            let code = codeLines.joined(separator: "\n")
            blocks.append(
                MarkdownBlock(
                    id: nextId(),
                    kind: .codeBlock(language: codeLanguage, code: code, isClosed: isFinished),
                    isSettled: isFinished
                )
            )
        } else {
            flushQuote(settled: isFinished)
            flushProse(settled: isFinished)
        }

        if isFinished {
            return ParseResult(settled: blocks, activeTail: nil, hasAnyTable: hasAnyTable)
        }

        if let last = blocks.last, !last.isSettled {
            return ParseResult(
                settled: Array(blocks.dropLast()),
                activeTail: last,
                hasAnyTable: hasAnyTable
            )
        }
        return ParseResult(settled: blocks, activeTail: nil, hasAnyTable: hasAnyTable)
    }

    private static func openingFence(_ trimmed: String) -> (marker: String, language: String)? {
        if trimmed.hasPrefix("```") {
            let rest = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
            // Inline single-line code like ```foo``` is not a fenced block
            if rest.contains("```") { return nil }
            return ("```", rest)
        }
        if trimmed.hasPrefix("~~~") {
            let rest = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
            if rest.contains("~~~") { return nil }
            return ("~~~", rest)
        }
        return nil
    }

    private static func isClosingFence(_ trimmed: String, marker: String) -> Bool {
        guard trimmed.hasPrefix(marker) else { return false }
        let fenceChar = marker.first!
        let rest = trimmed.drop(while: { $0 == fenceChar })
        return rest.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private static func isStreamingSeparatorMatch(_ trimmed: String, expectedCols: Int) -> Bool {
        let colCount = QMXMarkdownTables.splitRow(trimmed).count
        if QMXMarkdownTables.isSeparator(trimmed) && colCount == expectedCols {
            return true
        }
        guard trimmed.count >= 3, colCount <= expectedCols else { return false }
        // Early match when streaming separator line starts like "|---" or "|:--" or "---"
        let noSpaces = trimmed.replacingOccurrences(of: " ", with: "")
        let validChars = CharacterSet(charactersIn: "|:-")
        guard noSpaces.unicodeScalars.allSatisfy({ validChars.contains($0) }) else { return false }
        return noSpaces.contains("--") && (noSpaces.contains("|") || expectedCols == 1)
    }

    private static func couldStillBecomeSeparator(_ trimmed: String) -> Bool {
        guard trimmed.count < 4 else { return false }
        let noSpaces = trimmed.replacingOccurrences(of: " ", with: "")
        guard !noSpaces.isEmpty else { return false }
        let validChars = CharacterSet(charactersIn: "|:-")
        return noSpaces.unicodeScalars.allSatisfy { validChars.contains($0) }
    }

    private static func parseHeading(_ trimmed: String) -> (Int, String)? {
        guard trimmed.hasPrefix("#") else { return nil }
        var level = 0
        for ch in trimmed {
            guard ch == "#" else { break }
            level += 1
        }
        guard (1...6).contains(level) else { return nil }
        let after = trimmed.dropFirst(level)
        guard after.first == " " else { return nil }
        let title = String(after.dropFirst()).trimmingCharacters(in: .whitespaces)
        guard !title.isEmpty else { return nil }
        return (level, title)
    }

    private static func parseListItem(_ trimmed: String) -> (String, String)? {
        if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") {
            let body = String(trimmed.dropFirst(2)).trimmingCharacters(in: .whitespaces)
            guard !body.isEmpty else { return nil }
            return ("•", body)
        }
        // Ordered list: "1. text"
        if let dotIdx = trimmed.firstIndex(of: "."), dotIdx > trimmed.startIndex {
            let numPart = trimmed[..<dotIdx]
            if numPart.count <= 3, numPart.allSatisfy({ $0.isNumber }) {
                let afterDot = trimmed.index(after: dotIdx)
                if afterDot < trimmed.endIndex, trimmed[afterDot] == " " {
                    let body = String(trimmed[trimmed.index(after: afterDot)...])
                        .trimmingCharacters(in: .whitespaces)
                    if !body.isEmpty {
                        return ("\(numPart).", body)
                    }
                }
            }
        }
        return nil
    }

    private static func nearestTitle(lines: [String], before index: Int) -> String? {
        let lowerBound = max(0, index - 12)
        guard index > lowerBound else { return nil }
        for cursor in stride(from: index - 1, through: lowerBound, by: -1) {
            let trimmed = lines[cursor].trimmingCharacters(in: .whitespaces)
            if let (_, title) = parseHeading(trimmed) {
                return String(title.prefix(80))
            }
            if trimmed.hasPrefix("**"), trimmed.hasSuffix("**"), trimmed.count <= 42 {
                let title = String(trimmed.dropFirst(2).dropLast(2))
                    .trimmingCharacters(in: .whitespaces)
                if !title.isEmpty, !["。", "！", "？", ".", "!", "?"].contains(String(title.suffix(1))) {
                    return String(title.prefix(80))
                }
            }
        }
        return nil
    }
}

// MARK: - Monotonic Panel Height Engine

/// Guarantees panel height grows monotonically downward within a turn, only
/// fires resizes when height changes by >= 4pt (or aligns at screen/max cap),
/// never shrinks on turn completion, and resets only on explicit collapse.
struct MonotonicPanelHeightEngine {
    private(set) var monotonicHeight: CGFloat
    private let baseHeight: CGFloat
    private let minStepThreshold: CGFloat = 4.0

    init(baseHeight: CGFloat = 120) {
        self.baseHeight = baseHeight
        self.monotonicHeight = baseHeight
    }

    mutating func reset() {
        monotonicHeight = baseHeight
    }

    mutating func evaluate(
        measured: CGFloat,
        maxAllowed: CGFloat,
        currentHeight: CGFloat,
        isExpanded: Bool
    ) -> (target: CGFloat, shouldUpdate: Bool) {
        let boundedMax = max(baseHeight, maxAllowed)
        guard isExpanded else {
            monotonicHeight = baseHeight
            let idleTarget = min(boundedMax, max(baseHeight, measured))
            return (idleTarget, abs(idleTarget - currentHeight) >= 0.5)
        }

        let candidate = max(baseHeight, measured)
        let nextMonotonic = min(boundedMax, max(monotonicHeight, candidate))
        monotonicHeight = nextMonotonic

        let diff = nextMonotonic - currentHeight
        let atCapNeedsAlign = nextMonotonic >= boundedMax - 0.5 && abs(diff) >= 0.5
        let shouldUpdate = diff >= minStepThreshold || atCapNeedsAlign
        return (nextMonotonic, shouldUpdate)
    }
}

// MARK: - Smart Auto-Scroll Controller & Native QMXScrollView

final class QMXFlippedDocumentView: NSView {
    override var isFlipped: Bool { true }
}

final class QMXScrollView: NSScrollView {
    var onUserScrollUp: (() -> Void)?

    override func scrollWheel(with event: NSEvent) {
        if event.scrollingDeltaY > 0, let doc = documentView {
            let scrollable = doc.bounds.height > contentView.bounds.height + 1.0
            if scrollable {
                onUserScrollUp?()
            }
        }
        super.scrollWheel(with: event)
    }
}

@MainActor
final class SmartAutoScrollController: ObservableObject {
    @Published private(set) var isPinnedToBottom: Bool = true
    @Published private(set) var hasUnreadContentBelow: Bool = false

    weak var scrollView: QMXScrollView?
    private var isProgrammaticScroll = false
    private let bottomThreshold: CGFloat = 24.0
    nonisolated(unsafe) private var observers: [NSObjectProtocol] = []

    func attach(to scrollView: QMXScrollView) {
        guard self.scrollView !== scrollView else { return }
        detach()
        self.scrollView = scrollView
        scrollView.contentView.postsBoundsChangedNotifications = true
        scrollView.onUserScrollUp = { [weak self] in
            MainActor.assumeIsolated {
                self?.handleUserScrollUp()
            }
        }

        let center = NotificationCenter.default
        observers.append(
            center.addObserver(
                forName: NSScrollView.didLiveScrollNotification,
                object: scrollView,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.handleViewportChanged()
                }
            }
        )
        observers.append(
            center.addObserver(
                forName: NSView.boundsDidChangeNotification,
                object: scrollView.contentView,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.handleViewportChanged()
                }
            }
        )
    }

    func detach() {
        let center = NotificationCenter.default
        for observer in observers {
            center.removeObserver(observer)
        }
        observers.removeAll()
        scrollView?.onUserScrollUp = nil
        scrollView = nil
    }

    func resetForNewTurn() {
        isPinnedToBottom = true
        hasUnreadContentBelow = false
        guard let scrollView else { return }
        isProgrammaticScroll = true
        scrollView.contentView.scroll(to: .zero)
        scrollView.reflectScrolledClipView(scrollView.contentView)
        isProgrammaticScroll = false
    }

    func syncToBottomIfPinned() {
        guard let scrollView, let doc = scrollView.documentView else { return }
        let docHeight = doc.bounds.height
        let clipHeight = scrollView.contentView.bounds.height
        guard docHeight > clipHeight + 0.5 else {
            isPinnedToBottom = true
            hasUnreadContentBelow = false
            return
        }
        guard isPinnedToBottom else {
            hasUnreadContentBelow = true
            return
        }

        isProgrammaticScroll = true
        let targetY = max(0, docHeight - clipHeight)
        scrollView.contentView.scroll(to: NSPoint(x: 0, y: targetY))
        scrollView.reflectScrolledClipView(scrollView.contentView)
        isProgrammaticScroll = false
        hasUnreadContentBelow = false
    }

    func scrollToBottomAnimated() {
        guard let scrollView, let doc = scrollView.documentView else { return }
        let targetY = max(0, doc.bounds.height - scrollView.contentView.bounds.height)
        isPinnedToBottom = true
        hasUnreadContentBelow = false
        isProgrammaticScroll = true

        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            scrollView.contentView.scroll(to: NSPoint(x: 0, y: targetY))
            scrollView.reflectScrolledClipView(scrollView.contentView)
            isProgrammaticScroll = false
            return
        }

        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.18
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            scrollView.contentView.animator().setBoundsOrigin(NSPoint(x: 0, y: targetY))
        }, completionHandler: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self, let scrollView = self.scrollView else { return }
                scrollView.reflectScrolledClipView(scrollView.contentView)
                self.isProgrammaticScroll = false
            }
        })
    }

    private func handleUserScrollUp() {
        if isPinnedToBottom {
            isPinnedToBottom = false
        }
    }

    private func handleViewportChanged() {
        guard !isProgrammaticScroll, let scrollView, let doc = scrollView.documentView else { return }
        let docHeight = doc.bounds.height
        let clipBounds = scrollView.contentView.bounds
        guard docHeight > clipBounds.height + 0.5 else {
            isPinnedToBottom = true
            hasUnreadContentBelow = false
            return
        }
        let distanceToBottom = max(0, docHeight - clipBounds.maxY)
        if distanceToBottom <= bottomThreshold {
            isPinnedToBottom = true
            hasUnreadContentBelow = false
        }
    }

    deinit {
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
        }
    }
}

/// Bridges a SwiftUI answer content view into a flipped NSScrollView managed by
/// `SmartAutoScrollController`, keeping vertical scrollbars hidden for short
/// answers and giving zero-lag stick-to-bottom tracking for long streams.
struct QMXNativeAnswerScrollView<Content: View>: NSViewRepresentable {
    let width: CGFloat
    let maxHeight: CGFloat
    let turnID: String
    let isScrollable: Bool
    let controller: SmartAutoScrollController
    let onContentHeightChange: (CGFloat) -> Void
    @ViewBuilder let content: () -> Content

    func makeCoordinator() -> Coordinator {
        Coordinator(
            width: width,
            turnID: turnID,
            controller: controller,
            onContentHeightChange: onContentHeightChange
        )
    }

    func makeNSView(context: Context) -> QMXScrollView {
        let scroll = QMXScrollView()
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasHorizontalScroller = false
        scroll.hasVerticalScroller = isScrollable
        scroll.autohidesScrollers = false
        scroll.scrollerStyle = .overlay
        scroll.verticalScrollElasticity = .allowed

        let doc = QMXFlippedDocumentView(frame: NSRect(x: 0, y: 0, width: width, height: 1))
        let hosting = NSHostingView(rootView: content())
        hosting.translatesAutoresizingMaskIntoConstraints = false
        hosting.postsFrameChangedNotifications = true
        if #available(macOS 13.3, *) {
            hosting.safeAreaRegions = []
        }
        doc.addSubview(hosting)
        let widthConstraint = hosting.widthAnchor.constraint(equalToConstant: width)
        NSLayoutConstraint.activate([
            hosting.topAnchor.constraint(equalTo: doc.topAnchor),
            hosting.leadingAnchor.constraint(equalTo: doc.leadingAnchor),
            widthConstraint
        ])

        scroll.documentView = doc
        context.coordinator.scrollView = scroll
        context.coordinator.documentView = doc
        context.coordinator.hostingView = hosting
        context.coordinator.widthConstraint = widthConstraint
        context.coordinator.observeFrameChanges(of: hosting)
        controller.attach(to: scroll)
        return scroll
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: QMXScrollView, context: Context) -> CGSize? {
        let targetWidth = proposal.width ?? width
        let measured = context.coordinator.lastReportedHeight
        let fallback = min(maxHeight, proposal.height ?? maxHeight)
        let resolvedHeight = measured > 0 ? min(measured, maxHeight) : max(1, fallback)
        return CGSize(width: targetWidth, height: resolvedHeight)
    }

    func updateNSView(_ nsView: QMXScrollView, context: Context) {
        if nsView.hasVerticalScroller != isScrollable {
            nsView.hasVerticalScroller = isScrollable
        }
        if let widthConstraint = context.coordinator.widthConstraint,
           abs(widthConstraint.constant - width) > 0.5 {
            widthConstraint.constant = width
        }
        context.coordinator.onContentHeightChange = onContentHeightChange
        context.coordinator.hostingView?.rootView = content()

        if context.coordinator.lastTurnID != turnID {
            context.coordinator.lastTurnID = turnID
            controller.resetForNewTurn()
        }

        DispatchQueue.main.async { [weak coordinator = context.coordinator] in
            coordinator?.syncDocumentHeight(syncScroll: true)
        }
    }

    static func dismantleNSView(_ nsView: QMXScrollView, coordinator: Coordinator) {
        coordinator.stopObserving()
        coordinator.controller.detach()
    }

    @MainActor
    final class Coordinator {
        let width: CGFloat
        var lastTurnID: String
        let controller: SmartAutoScrollController
        var onContentHeightChange: (CGFloat) -> Void

        weak var scrollView: QMXScrollView?
        weak var documentView: QMXFlippedDocumentView?
        weak var hostingView: NSHostingView<Content>?
        var widthConstraint: NSLayoutConstraint?
        private(set) var lastReportedHeight: CGFloat = 0
        nonisolated(unsafe) private var frameObserver: NSObjectProtocol?

        init(
            width: CGFloat,
            turnID: String,
            controller: SmartAutoScrollController,
            onContentHeightChange: @escaping (CGFloat) -> Void
        ) {
            self.width = width
            self.lastTurnID = turnID
            self.controller = controller
            self.onContentHeightChange = onContentHeightChange
        }

        func observeFrameChanges(of hosting: NSHostingView<Content>) {
            stopObserving()
            frameObserver = NotificationCenter.default.addObserver(
                forName: NSView.frameDidChangeNotification,
                object: hosting,
                queue: .main
            ) { [weak self] _ in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        self?.syncDocumentHeight(syncScroll: true)
                    }
                }
            }
        }

        func stopObserving() {
            if let frameObserver {
                NotificationCenter.default.removeObserver(frameObserver)
                self.frameObserver = nil
            }
        }

        func syncDocumentHeight(syncScroll: Bool) {
            guard let doc = documentView, let hosting = hostingView else { return }
            let fittingHeight = ceil(hosting.fittingSize.height)
            let frameHeight = ceil(hosting.frame.height)
            let targetHeight = max(1, max(fittingHeight, frameHeight))
            if abs(doc.frame.height - targetHeight) > 0.5 || abs(doc.frame.width - width) > 0.5 {
                doc.frame = NSRect(x: 0, y: 0, width: width, height: targetHeight)
            }
            if abs(lastReportedHeight - targetHeight) > 0.5 {
                lastReportedHeight = targetHeight
                onContentHeightChange(targetHeight)
            }
            if syncScroll {
                controller.syncToBottomIfPinned()
            }
        }

        deinit {
            if let frameObserver {
                NotificationCenter.default.removeObserver(frameObserver)
            }
        }
    }
}

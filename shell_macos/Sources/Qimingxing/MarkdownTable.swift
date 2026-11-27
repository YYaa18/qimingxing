import AppKit
import Foundation
import SwiftUI

struct QMXMarkdownTable: Identifiable, Equatable {
    let id: Int
    let startLine: Int
    let endLine: Int
    let raw: String
    let title: String
    let columns: [String]
    let rows: [[String]]
    let totalRows: Int
    let totalColumns: Int

    var summary: String {
        let rowsLabel = totalRows > rows.count ? "\(rows.count)+" : "\(totalRows)"
        let columnsLabel = totalColumns > columns.count ? "\(columns.count)+" : "\(totalColumns)"
        return "\(rowsLabel) 行 × \(columnsLabel) 列"
    }
}

enum QMXMarkdownTables {
    static func scan(_ raw: String) -> [QMXMarkdownTable] {
        let normalized = raw.replacingOccurrences(of: "\r\n", with: "\n")
        let lines = normalized.components(separatedBy: "\n")
        var tables: [QMXMarkdownTable] = []
        var fenceMarker: String?
        var inCard = false
        var index = 0

        while index < lines.count {
            let trimmed = lines[index].trimmingCharacters(in: .whitespaces)
            if let marker = fenceMarker {
                if trimmed.hasPrefix(marker) {
                    fenceMarker = nil
                }
                index += 1
                continue
            }
            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                fenceMarker = String(trimmed.prefix(3))
                index += 1
                continue
            }
            let upper = trimmed.uppercased()
            if upper.hasPrefix("<<<CARD") {
                inCard = true
                index += 1
                continue
            }
            if upper.contains("<<<END_CARD>>>") {
                inCard = false
                index += 1
                continue
            }
            if inCard {
                index += 1
                continue
            }

            guard let header = tableHeader(at: index, lines: lines) else {
                index += 1
                continue
            }

            let start = index
            var cursor = index + 2
            var rows: [[String]] = []
            while cursor < lines.count {
                let candidate = lines[cursor]
                let candidateTrimmed = candidate.trimmingCharacters(in: .whitespaces)
                if candidateTrimmed.isEmpty || !candidateTrimmed.contains("|") {
                    break
                }
                if candidateTrimmed.hasPrefix("```") || candidateTrimmed.hasPrefix("~~~") {
                    break
                }
                if isSeparator(candidateTrimmed) {
                    cursor += 1
                    continue
                }
                rows.append(splitRow(candidateTrimmed))
                cursor += 1
            }

            guard !rows.isEmpty else {
                index = max(index + 1, cursor)
                continue
            }

            let end = cursor - 1
            let columns = Array(header.prefix(15)).map { String($0.prefix(160)) }
            let previewRows = rows.prefix(100).map { row in
                (0..<columns.count).map { column in
                    column < row.count ? String(row[column].prefix(160)) : ""
                }
            }
            tables.append(
                QMXMarkdownTable(
                    id: tables.count,
                    startLine: start,
                    endLine: end,
                    raw: lines[start...end].joined(separator: "\n"),
                    title: nearestTitle(lines: lines, before: start) ?? "表格 \(tables.count + 1)",
                    columns: columns,
                    rows: Array(previewRows),
                    totalRows: rows.count,
                    totalColumns: header.count
                )
            )
            index = max(index + 1, cursor)
        }
        return tables
    }

    static func replacingTables(
        in raw: String,
        with replacement: (QMXMarkdownTable) -> String
    ) -> String {
        let normalized = raw.replacingOccurrences(of: "\r\n", with: "\n")
        let lines = normalized.components(separatedBy: "\n")
        let tables = scan(normalized)
        guard !tables.isEmpty else { return raw }
        var output: [String] = []
        var cursor = 0
        for table in tables {
            if cursor < table.startLine {
                output.append(contentsOf: lines[cursor..<table.startLine])
            }
            output.append(replacement(table))
            cursor = table.endLine + 1
        }
        if cursor < lines.count {
            output.append(contentsOf: lines[cursor...])
        }
        var joined = output.joined(separator: "\n")
        while joined.contains("\n\n\n") {
            joined = joined.replacingOccurrences(of: "\n\n\n", with: "\n\n")
        }
        return joined.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func strippingTables(from raw: String) -> String {
        replacingTables(in: raw) { _ in "" }
    }

    private static func tableHeader(at index: Int, lines: [String]) -> [String]? {
        guard index + 1 < lines.count else { return nil }
        let headerLine = lines[index].trimmingCharacters(in: .whitespaces)
        let separatorLine = lines[index + 1].trimmingCharacters(in: .whitespaces)
        guard headerLine.contains("|"), separatorLine.contains("|"),
              isSeparator(separatorLine) else { return nil }
        let header = splitRow(headerLine)
        let separator = splitRow(separatorLine)
        guard !header.isEmpty, header.count == separator.count else { return nil }
        return header
    }

    static func splitRow(_ line: String) -> [String] {
        var value = line.trimmingCharacters(in: .whitespaces)
        if value.hasPrefix("|") {
            value.removeFirst()
        }
        if value.hasSuffix("|") && !value.hasSuffix("\\|") {
            value.removeLast()
        }

        var cells: [String] = []
        var buffer = ""
        var inCode = false
        let characters = Array(value)
        var index = 0
        while index < characters.count {
            let character = characters[index]
            if character == "\\", index + 1 < characters.count, characters[index + 1] == "|" {
                buffer.append("|")
                index += 2
                continue
            }
            if character == "`" {
                inCode.toggle()
                buffer.append(character)
                index += 1
                continue
            }
            if character == "|", !inCode {
                cells.append(buffer.trimmingCharacters(in: .whitespaces))
                buffer = ""
                index += 1
                continue
            }
            buffer.append(character)
            index += 1
        }
        cells.append(buffer.trimmingCharacters(in: .whitespaces))
        return cells
    }

    static func isSeparator(_ line: String) -> Bool {
        guard line.contains("|") else { return false }
        let cells = splitRow(line)
        guard !cells.isEmpty else { return false }
        return cells.allSatisfy { cell in
            var value = cell.trimmingCharacters(in: .whitespaces)
            if value.hasPrefix(":") { value.removeFirst() }
            if value.hasSuffix(":") { value.removeLast() }
            return value.count >= 2 && value.allSatisfy { $0 == "-" }
        }
    }

    private static func nearestTitle(lines: [String], before index: Int) -> String? {
        let lowerBound = max(0, index - 12)
        guard index > lowerBound else { return nil }
        for cursor in stride(from: index - 1, through: lowerBound, by: -1) {
            let trimmed = lines[cursor].trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("#") {
                var level = 0
                for character in trimmed {
                    guard character == "#" else { break }
                    level += 1
                }
                let title = String(trimmed.dropFirst(level))
                    .trimmingCharacters(in: .whitespaces)
                if !title.isEmpty {
                    return String(title.prefix(80))
                }
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

extension MediaItem {
    var isMarkdownTable: Bool {
        fileExt.lowercased() == "md-table"
    }
}

/// Removes leaked card markers. Valid card blocks are consumed by the brain
/// parser, so anything left in stored text is a malformed fragment; dropping
/// only the marker tokens keeps the inline text visible.
enum QMXCardMarkers {
    static func strip(_ raw: String) -> String {
        guard raw.contains("<<<") else { return raw }
        return raw
            .replacingOccurrences(
                of: #"<<<CARD[^>\n]*>>>"#,
                with: "",
                options: [.regularExpression, .caseInsensitive]
            )
            .replacingOccurrences(
                of: "<<<END_CARD>>>",
                with: "",
                options: .caseInsensitive
            )
    }
}

/// Stable signature for a table, used to drop the md-table media twin when the
/// same table is already visible inline or inside a card. Only the header and
/// the first three rows are compared so brain-side truncation (15x100) and the
/// card's full markdown still match.
enum QMXTableDedupe {
    static func signature(columns: [String], rows: [[String]]) -> String {
        func norm(_ value: String) -> String {
            value.trimmingCharacters(in: .whitespaces)
        }
        let header = columns.map(norm).joined(separator: "\u{1F}")
        let sample = rows.prefix(3).map { $0.map(norm).joined(separator: "\u{1F}") }
        return ([header] + sample).joined(separator: "\u{1E}")
    }
}

extension QMXMarkdownTable {
    var dedupeSignature: String {
        QMXTableDedupe.signature(columns: columns, rows: rows)
    }

    /// Synthetic media item for tables that only exist inside a card body.
    func asMediaItem(id: String, contextTitle: String) -> MediaItem {
        MediaItem(
            mediaId: id,
            kind: "file",
            title: title,
            mimeType: "text/markdown",
            sourceTitle: contextTitle,
            provider: "qimingxing",
            fileExt: "md-table",
            summaryBadge: summary,
            previewColumns: columns,
            previewRows: rows
        )
    }
}

func qmxTableTSV(columns: [String], rows: [[String]]) -> String {
    func clean(_ value: String) -> String {
        value.replacingOccurrences(of: "\t", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
    }
    return ([columns] + rows)
        .map { $0.map(clean).joined(separator: "\t") }
        .joined(separator: "\n")
}

@discardableResult
func qmxCopyTableToPasteboard(columns: [String], rows: [[String]]) -> Bool {
    guard !columns.isEmpty else { return false }
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    return pasteboard.setString(qmxTableTSV(columns: columns, rows: rows), forType: .string)
}

struct QMXTableNotice: View {
    let table: QMXMarkdownTable
    var media: MediaItem?
    var onOpen: (() -> Void)?
    var isStreaming: Bool = false

    init(
        table: QMXMarkdownTable,
        media: MediaItem? = nil,
        onOpen: (() -> Void)? = nil,
        isStreaming: Bool = false
    ) {
        self.table = table
        self.media = media
        self.onOpen = onOpen
        self.isStreaming = isStreaming
    }

    init(
        title: String,
        colCount: Int,
        rowCount: Int,
        isStreaming: Bool,
        rawMarkdown: String = "",
        media: MediaItem? = nil,
        onOpen: (() -> Void)? = nil
    ) {
        self.table = QMXMarkdownTable(
            id: 0,
            startLine: 0,
            endLine: 0,
            raw: rawMarkdown,
            title: title,
            columns: [],
            rows: [],
            totalRows: rowCount,
            totalColumns: colCount
        )
        self.media = media
        self.onOpen = onOpen
        self.isStreaming = isStreaming
    }

    private var canOpen: Bool {
        onOpen != nil && (media != nil || !isStreaming)
    }

    var body: some View {
        if let onOpen, canOpen {
            Button(action: onOpen) {
                content(showsAction: true)
            }
            .buttonStyle(.plain)
            .help("在媒体区查看完整表格")
        } else {
            content(showsAction: false)
        }
    }

    private func content(showsAction: Bool) -> some View {
        HStack(spacing: 10) {
            Group {
                if isStreaming {
                    GlobeSpinner(size: 14, color: QMXTheme.textSecondary)
                } else {
                    Image(systemName: "tablecells")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                }
            }
            .frame(width: 22)

            VStack(alignment: .leading, spacing: 2) {
                Text(table.title)
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .lineLimit(1)
                Text(subtitle(showsAction: showsAction))
                    .font(.system(size: 10.5))
                    .foregroundStyle(QMXTheme.textTertiary)
                    .lineLimit(1)
            }

            Spacer(minLength: 0)

            if !isStreaming, !QMXTheme.isHeadlessDump {
                let parsedRaw = (table.columns.isEmpty && !table.raw.isEmpty) ? QMXMarkdownTables.scan(table.raw).first : nil
                let dragCols = !table.columns.isEmpty ? table.columns : (parsedRaw?.columns ?? media?.previewColumns ?? [])
                let dragRows = !table.rows.isEmpty ? table.rows : (parsedRaw?.rows ?? media?.previewRows ?? [])
                if !dragCols.isEmpty {
                    QMXDragOutHandle(payload: .table(title: table.title, columns: dragCols, rows: dragRows))
                        .frame(width: 22, height: 22)
                }
            }

            if showsAction {
                Text("媒体区")
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                Image(systemName: "arrow.right")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(QMXTheme.surfaceMuted.opacity(0.35))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
        .contentShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private func subtitle(showsAction: Bool) -> String {
        if isStreaming {
            return table.totalRows > 0
                ? "正在整理表格 · 已生成 \(table.totalRows) 行…"
                : "正在整理表格…"
        }
        if showsAction {
            return "\(table.summary) · 在媒体区查看"
        }
        return "\(table.summary) · 表格"
    }
}

/// Compact Quiet-Native inline table for narrow Markdown tables (<= 3 columns).
/// Keeps up to 6 rows visible inline with copy-TSV and expand-in-media actions;
/// if streaming exceeds 6 rows, locks height and offers an inline overflow strip.
struct InlineMarkdownTableView: View {
    let title: String
    let headers: [String]
    let rows: [[String]]
    let totalRowCount: Int
    let isStreaming: Bool
    let isTruncated: Bool
    var fontSize: CGFloat = 14
    var selectable: Bool = true
    var onOpen: (() -> Void)? = nil

    @State private var copied = false

    private var cellFontSize: CGFloat {
        max(11.5, min(13.5, fontSize - 2.5))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            headerBar

            Divider().opacity(0.45)

            ZStack(alignment: .bottom) {
                Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                    GridRow {
                        ForEach(Array(headers.enumerated()), id: \.offset) { colIndex, header in
                            Text(header)
                                .font(.system(size: cellFontSize, weight: .semibold))
                                .foregroundStyle(QMXTheme.textPrimary)
                                .lineLimit(2)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(QMXTheme.surfaceRaised.opacity(0.85))
                                .overlay(alignment: .trailing) {
                                    if colIndex + 1 < headers.count {
                                        Rectangle()
                                            .fill(QMXTheme.hairline)
                                            .frame(width: 0.5)
                                    }
                                }
                        }
                    }

                    ForEach(Array(rows.enumerated()), id: \.offset) { rowIndex, row in
                        GridRow {
                            ForEach(Array(headers.indices), id: \.self) { colIndex in
                                let value = colIndex < row.count ? row[colIndex] : ""
                                Text(value)
                                    .font(.system(size: cellFontSize))
                                    .foregroundStyle(QMXTheme.textPrimary)
                                    .lineLimit(3)
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 5.5)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .background(
                                        rowIndex % 2 == 1
                                            ? Color.primary.opacity(0.025)
                                            : Color.clear
                                    )
                                    .overlay(alignment: .top) {
                                        Rectangle()
                                            .fill(QMXTheme.hairline)
                                            .frame(height: 0.5)
                                    }
                                    .overlay(alignment: .trailing) {
                                        if colIndex + 1 < headers.count {
                                            Rectangle()
                                                .fill(QMXTheme.hairline)
                                                .frame(width: 0.5)
                                        }
                                    }
                            }
                        }
                    }
                }
                .modifier(QMXTextSelectable(enabled: selectable))

                if isTruncated {
                    LinearGradient(
                        colors: [.clear, QMXTheme.surface.opacity(0.82)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                    .frame(height: 22)
                    .allowsHitTesting(false)
                }
            }

            if isTruncated {
                Divider().opacity(0.45)
                truncationFooter
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(QMXTheme.surfaceMuted.opacity(0.22))
        )
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }

    private var headerBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "tablecells")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(QMXTheme.textTertiary)
            Text(title.isEmpty ? "表格" : title)
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(QMXTheme.textSecondary)
                .lineLimit(1)
            Spacer(minLength: 0)

            Button {
                if qmxCopyTableToPasteboard(columns: headers, rows: rows) {
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                        copied = false
                    }
                }
            } label: {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(copied ? QMXTheme.success : QMXTheme.textTertiary)
                    .frame(width: 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(copied ? "已复制 TSV" : "复制 TSV")

            if !isStreaming, !QMXTheme.isHeadlessDump, !headers.isEmpty {
                QMXDragOutHandle(payload: .table(title: title, columns: headers, rows: rows))
                    .frame(width: 22, height: 22)
            }

            if let onOpen {
                Button(action: onOpen) {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(QMXTheme.textTertiary)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("在媒体区放大")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(QMXTheme.surfaceRaised.opacity(0.6))
    }

    @ViewBuilder
    private var truncationFooter: some View {
        let labelText = isStreaming
            ? "已生成 \(totalRowCount) 行 · 在媒体区查看全部"
            : "共 \(totalRowCount) 行 · 在媒体区查看全部"
        if let onOpen {
            Button(action: onOpen) {
                HStack(spacing: 5) {
                    if isStreaming {
                        GlobeSpinner(size: 11, color: QMXTheme.textTertiary)
                    }
                    Text(labelText)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                    Image(systemName: "arrow.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(QMXTheme.textTertiary)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(QMXTheme.surfaceRaised.opacity(0.55))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        } else {
            HStack(spacing: 5) {
                Text(labelText)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(QMXTheme.textSecondary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(QMXTheme.surfaceRaised.opacity(0.55))
        }
    }
}

/// Tables render as an inline single-line entry inside the answer body, so
/// the media strip should not repeat the same virtual table card. Card bodies
/// are included because the brain also emits card tables as `turn.media`.
func qmxMediaWithoutInlineTables(
    _ media: [MediaItem],
    answer: String,
    cards: [OutputCardItem] = []
) -> [MediaItem] {
    guard !media.isEmpty else { return media }
    var signatures = Set(QMXMarkdownTables.scan(answer).map(\.dedupeSignature))
    for card in cards {
        signatures.formUnion(QMXMarkdownTables.scan(card.contentMarkdown).map(\.dedupeSignature))
    }
    guard !signatures.isEmpty else { return media }
    return media.filter { item in
        guard item.isMarkdownTable else { return true }
        let signature = QMXTableDedupe.signature(
            columns: item.previewColumns,
            rows: item.previewRows
        )
        return !signatures.contains(signature)
    }
}

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
            let columns = Array(header.prefix(10)).map { String($0.prefix(160)) }
            let previewRows = rows.prefix(20).map { row in
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

    var body: some View {
        if let onOpen, media != nil {
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
            Image(systemName: "tablecells")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(QMXTheme.textSecondary)
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
        if showsAction {
            return "\(table.summary) · 在媒体区查看"
        }
        return "\(table.summary) · 表格"
    }
}

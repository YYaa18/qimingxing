import AppKit
import SwiftUI

@MainActor
final class TableScrollProxy: ObservableObject {
    weak var scrollView: NSScrollView?
    var contentWidth: CGFloat = 0

    func scroll(by dx: CGFloat) {
        guard let scrollView, let document = scrollView.documentView else { return }
        let viewWidth = scrollView.contentSize.width
        let targetWidth = max(contentWidth, document.frame.width, viewWidth)
        if abs(document.frame.width - targetWidth) > 0.5 {
            document.frame.size.width = targetWidth
        }
        let maxX = max(0, targetWidth - viewWidth)
        var origin = scrollView.contentView.bounds.origin
        origin.x = min(max(0, origin.x + dx), maxX)
        scrollView.contentView.scroll(to: origin)
        scrollView.reflectScrolledClipView(scrollView.contentView)
        // NSTableView does not always move its header when the clip view is
        // scrolled programmatically, which leaves titles over the wrong
        // columns. Scroll the header's own clip view in step.
        if let table = document as? NSTableView,
           let header = table.headerView,
           let headerClip = header.superview as? NSClipView {
            var headerOrigin = headerClip.bounds.origin
            headerOrigin.x = origin.x
            headerClip.scroll(to: headerOrigin)
            scrollView.reflectScrolledClipView(headerClip)
        }
    }
}

private final class WideTableScrollView: NSScrollView {
    var desiredContentWidth: CGFloat = 0
    var desiredContentHeight: CGFloat = 0

    override func layout() {
        super.layout()
        guard let documentView else { return }
        let target = NSSize(
            width: max(desiredContentWidth, contentSize.width),
            height: max(desiredContentHeight, contentSize.height)
        )
        if abs(documentView.frame.width - target.width) > 0.5
            || abs(documentView.frame.height - target.height) > 0.5 {
            documentView.frame = NSRect(origin: .zero, size: target)
        }
    }
}

private final class QMXTableRowView: NSTableRowView {
    var alternate = false

    override func drawBackground(in dirtyRect: NSRect) {
        guard alternate else { return }
        NSColor.labelColor.withAlphaComponent(0.035).setFill()
        dirtyRect.fill()
    }
}

private final class WideTableView: NSTableView {
    var minimumContentWidth: CGFloat = 0
    override func setFrameSize(_ newSize: NSSize) {
        var size = newSize
        if minimumContentWidth > 0 {
            size.width = max(minimumContentWidth, size.width)
        }
        super.setFrameSize(size)
    }
}

/// Native NSTableView wrapper for data previews. Unlike the SwiftUI Grid it
/// has real horizontal/vertical scrollers, a sticky header, resizable columns
/// and per-cell tooltips, which is what wide/long tables need.
struct NativeTableGrid: NSViewRepresentable {
    let columns: [String]
    let rows: [[String]]
    var monospaced: Bool = false
    var scrollProxy: TableScrollProxy? = nil

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = WideTableScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = false
        scroll.borderType = .noBorder
        scroll.drawsBackground = false
        scroll.horizontalScrollElasticity = .allowed
        scroll.verticalScrollElasticity = .allowed

        let table = WideTableView()
        table.headerView = NSTableHeaderView()
        table.usesAlternatingRowBackgroundColors = false
        table.backgroundColor = .clear
        table.gridStyleMask = [.solidHorizontalGridLineMask]
        table.gridColor = NSColor.separatorColor.withAlphaComponent(0.28)
        table.rowHeight = 24
        table.intercellSpacing = NSSize(width: 8, height: 0)
        table.columnAutoresizingStyle = .noColumnAutoresizing
        table.autoresizingMask = []
        table.allowsColumnReordering = false
        table.allowsMultipleSelection = true
        table.dataSource = context.coordinator
        table.delegate = context.coordinator
        scroll.documentView = table
        scrollProxy?.scrollView = scroll
        context.coordinator.table = table
        context.coordinator.update(
            columns: columns,
            rows: rows,
            monospaced: monospaced,
            scrollProxy: scrollProxy
        )
        return scroll
    }

    func updateNSView(_ nsView: NSScrollView, context: Context) {
        context.coordinator.update(
            columns: columns,
            rows: rows,
            monospaced: monospaced,
            scrollProxy: scrollProxy
        )
        nsView.layoutSubtreeIfNeeded()
        nsView.reflectScrolledClipView(nsView.contentView)
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, NSTableViewDataSource, NSTableViewDelegate {
        weak var table: NSTableView?
        private var columns: [String] = []
        private var rows: [[String]] = []
        private var monospaced = false
        private var signature = ""

        @MainActor
        func update(
            columns: [String],
            rows: [[String]],
            monospaced: Bool,
            scrollProxy: TableScrollProxy?
        ) {
            let signature = columns.joined(separator: "\u{1f}")
                + "\u{1e}"
                + rows.map { $0.joined(separator: "\u{1f}") }.joined(separator: "\u{1e}")
                + "\u{1e}\(monospaced)"
            guard signature != self.signature, let table else { return }
            self.signature = signature
            self.columns = columns
            self.rows = rows
            self.monospaced = monospaced

            for column in table.tableColumns {
                table.removeTableColumn(column)
            }
            let font = cellFont(bold: false)
            let headerFont = NSFont.systemFont(ofSize: 11, weight: .semibold)
            for (index, title) in columns.enumerated() {
                let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("c\(index)"))
                column.title = title
                column.headerCell.font = headerFont
                column.minWidth = 80
                column.maxWidth = 520
                column.width = measuredWidth(title: title, index: index, headerFont: headerFont, cellFont: font)
                table.addTableColumn(column)
            }
            table.reloadData()
            table.layoutSubtreeIfNeeded()

            // NSTableView keeps its frame equal to the clip view, so the
            // scroll view concludes there is nothing to scroll horizontally
            // and clips any columns past the right edge. A container scroll
            // view owns the real content size instead.
            let intercell = CGFloat(max(0, columns.count - 1)) * table.intercellSpacing.width
            let totalWidth = table.tableColumns.reduce(0) { $0 + $1.width } + intercell
            let headerHeight = table.headerView?.frame.height ?? 0
            let totalHeight = CGFloat(rows.count) * (table.rowHeight + table.intercellSpacing.height) + headerHeight
            if let scroll = table.enclosingScrollView as? WideTableScrollView {
                scroll.desiredContentWidth = totalWidth
                scroll.desiredContentHeight = totalHeight
                scroll.needsLayout = true
                scroll.layoutSubtreeIfNeeded()
                scroll.reflectScrolledClipView(scroll.contentView)
            }
            (table as? WideTableView)?.minimumContentWidth = totalWidth
            scrollProxy?.contentWidth = totalWidth
        }

        private func measuredWidth(
            title: String,
            index: Int,
            headerFont: NSFont,
            cellFont: NSFont
        ) -> CGFloat {
            let headerWidth = (title as NSString).size(withAttributes: [.font: headerFont]).width
            var maxWidth = headerWidth
            for row in rows.prefix(120) {
                let value = index < row.count ? row[index] : ""
                let width = (value as NSString).size(withAttributes: [.font: cellFont]).width
                maxWidth = max(maxWidth, width)
            }
            return min(360, max(110, ceil(maxWidth) + 34))
        }

        private func cellFont(bold: Bool) -> NSFont {
            if monospaced {
                return NSFont.monospacedSystemFont(ofSize: 11, weight: bold ? .semibold : .regular)
            }
            return NSFont.systemFont(ofSize: 11.5, weight: bold ? .semibold : .regular)
        }

        func numberOfRows(in tableView: NSTableView) -> Int {
            rows.count
        }

        func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
            let identifier = NSUserInterfaceItemIdentifier("qmx-row")
            let rowView = tableView.makeView(withIdentifier: identifier, owner: self) as? QMXTableRowView
                ?? QMXTableRowView()
            rowView.identifier = identifier
            rowView.alternate = row % 2 == 1
            return rowView
        }

        func tableView(
            _ tableView: NSTableView,
            viewFor tableColumn: NSTableColumn?,
            row: Int
        ) -> NSView? {
            guard let tableColumn,
                  row < rows.count,
                  let index = Int(tableColumn.identifier.rawValue.dropFirst())
            else { return nil }
            let identifier = tableColumn.identifier
            let cell: NSTableCellView
            if let reused = tableView.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView {
                cell = reused
            } else {
                cell = NSTableCellView()
                cell.identifier = identifier
                let text = NSTextField(labelWithString: "")
                text.translatesAutoresizingMaskIntoConstraints = false
                text.lineBreakMode = .byTruncatingTail
                text.maximumNumberOfLines = 1
                text.font = cellFont(bold: false)
                cell.addSubview(text)
                cell.textField = text
                NSLayoutConstraint.activate([
                    text.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                    text.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                    text.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                ])
            }
            let value = index < rows[row].count ? rows[row][index] : ""
            cell.textField?.stringValue = value
            cell.textField?.toolTip = value
            cell.textField?.font = cellFont(bold: false)
            return cell
        }
    }
}

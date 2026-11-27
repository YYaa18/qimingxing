import AppKit
import XCTest
@testable import Qimingxing

@MainActor
final class ApprovalQueueModelTests: XCTestCase {
    private func item(_ id: String) -> PendingApprovalItem {
        PendingApprovalItem(
            approvalId: id,
            turnId: "turn",
            toolName: "shell_exec",
            riskLevel: "confirm",
            summary: id,
            details: [:],
            options: [],
            isClarification: false,
            allowRemember: true,
            timeoutSec: 30
        )
    }

    func testSyncSelectsFirstItem() {
        let model = ApprovalQueueModel()
        model.sync([item("a"), item("b")])
        XCTAssertEqual(model.items.map(\.approvalId), ["a", "b"])
        XCTAssertEqual(model.currentItem?.approvalId, "a")
    }

    func testSyncPreservesCurrentSelection() {
        let model = ApprovalQueueModel()
        model.sync([item("a"), item("b"), item("c")])
        model.selectedIndex = 2
        model.sync([item("b"), item("c")])
        XCTAssertEqual(model.currentItem?.approvalId, "c")
        XCTAssertEqual(model.selectedIndex, 1)
    }

    func testSyncClampsSelectionWhenItemsShrink() {
        let model = ApprovalQueueModel()
        model.sync([item("a"), item("b"), item("c")])
        model.selectedIndex = 2
        model.sync([item("x")])
        XCTAssertEqual(model.selectedIndex, 0)
        XCTAssertEqual(model.currentItem?.approvalId, "x")
    }

    func testRemoveAdjustsSelection() {
        let model = ApprovalQueueModel()
        model.sync([item("a"), item("b")])
        model.selectedIndex = 1
        model.remove(approvalId: "b")
        XCTAssertEqual(model.items.map(\.approvalId), ["a"])
        XCTAssertEqual(model.selectedIndex, 0)
    }

    func testStepWrapsAround() {
        let model = ApprovalQueueModel()
        model.sync([item("a"), item("b")])
        model.step(by: -1)
        XCTAssertEqual(model.currentItem?.approvalId, "b")
        model.step(by: 1)
        XCTAssertEqual(model.currentItem?.approvalId, "a")
    }

    func testEmptyQueueHasNoCurrentItem() {
        let model = ApprovalQueueModel()
        model.sync([])
        XCTAssertNil(model.currentItem)
        model.step(by: 1)
        XCTAssertEqual(model.selectedIndex, 0)
    }
}

@MainActor
final class MediaViewerModelTests: XCTestCase {
    private func media(_ id: String) -> MediaItem {
        MediaItem(mediaId: id, kind: "image", title: id)
    }

    func testLoadSelectsRequestedItem() {
        let model = MediaViewerModel()
        model.load(
            items: [media("a"), media("b")],
            selectedID: "b",
            title: "t",
            contextLabel: "ctx"
        )
        XCTAssertEqual(model.selectedID, "b")
        XCTAssertEqual(model.title, "t")
        XCTAssertEqual(model.contextLabel, "ctx")
    }

    func testLoadFallsBackToFirstForUnknownID() {
        let model = MediaViewerModel()
        model.load(items: [media("a")], selectedID: "missing", title: "", contextLabel: "")
        XCTAssertEqual(model.selectedID, "a")
    }

    func testSelectIgnoresUnknownID() {
        let model = MediaViewerModel()
        model.load(items: [media("a")], selectedID: "a", title: "", contextLabel: "")
        model.select("zzz")
        XCTAssertEqual(model.selectedID, "a")
    }

    func testSelectOffsetClampsAtEnds() {
        let model = MediaViewerModel()
        model.load(
            items: [media("a"), media("b")],
            selectedID: "a",
            title: "",
            contextLabel: ""
        )
        model.selectOffset(-5)
        XCTAssertEqual(model.selectedID, "a")
        model.selectOffset(5)
        XCTAssertEqual(model.selectedID, "b")
    }

    func testOfferPendingIgnoresIdenticalItems() {
        let model = MediaViewerModel()
        model.load(items: [media("a")], selectedID: "a", title: "", contextLabel: "")
        model.offerPending(items: [media("a")], title: "next")
        XCTAssertTrue(model.pendingItems.isEmpty)
        XCTAssertTrue(model.pendingTitle.isEmpty)
    }

    func testAcceptPendingLoadsThem() {
        let model = MediaViewerModel()
        model.load(items: [media("a")], selectedID: "a", title: "", contextLabel: "")
        model.offerPending(items: [media("b")], title: "next")
        XCTAssertEqual(model.pendingItems.map(\.mediaId), ["b"])
        model.acceptPending()
        XCTAssertEqual(model.items.map(\.mediaId), ["b"])
        XCTAssertEqual(model.selectedID, "b")
        XCTAssertEqual(model.title, "next")
        XCTAssertTrue(model.pendingItems.isEmpty)
    }

    func testDismissPendingClears() {
        let model = MediaViewerModel()
        model.load(items: [media("a")], selectedID: "a", title: "", contextLabel: "")
        model.offerPending(items: [media("b")], title: "next")
        model.dismissPending()
        XCTAssertTrue(model.pendingItems.isEmpty)
        XCTAssertEqual(model.items.map(\.mediaId), ["a"])
    }
}

final class MarkdownTableParsingTests: XCTestCase {
    func testScanBasicTable() {
        let tables = QMXMarkdownTables.scan(
            "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
        )
        XCTAssertEqual(tables.count, 1)
        XCTAssertEqual(tables[0].columns, ["A", "B"])
        XCTAssertEqual(tables[0].rows, [["1", "2"], ["3", "4"]])
    }

    func testScanSkipsFencedCode() {
        let raw = "```\n| A |\n| --- |\n| 1 |\n```"
        XCTAssertTrue(QMXMarkdownTables.scan(raw).isEmpty)
    }

    func testScanSkipsCardRegions() {
        let raw = "<<<CARD title=\"t\">>>\n| A |\n| --- |\n| 1 |\n<<<END_CARD>>>"
        XCTAssertTrue(QMXMarkdownTables.scan(raw).isEmpty)
    }

    func testScanCapsColumnsAndReportsTotal() {
        let header = (0..<20).map { "H\($0)" }
        let row = (0..<20).map { "\($0)" }
        let raw = "| " + header.joined(separator: " | ") + " |\n"
            + "| " + header.map { _ in "---" }.joined(separator: " | ") + " |\n"
            + "| " + row.joined(separator: " | ") + " |"
        let table = QMXMarkdownTables.scan(raw).first
        XCTAssertEqual(table?.columns.count, 15)
        XCTAssertEqual(table?.totalColumns, 20)
    }

    func testScanCapsRowsAndReportsTotal() {
        var lines = ["| A |", "| --- |"]
        for index in 0..<105 { lines.append("| \(index) |") }
        let table = QMXMarkdownTables.scan(lines.joined(separator: "\n")).first
        XCTAssertEqual(table?.rows.count, 100)
        XCTAssertEqual(table?.totalRows, 105)
    }

    func testTSVReplacesTabsAndNewlines() {
        let tsv = qmxTableTSV(columns: ["A\tB"], rows: [["1\n2", "3"]])
        XCTAssertTrue(tsv.contains("A B"))
        XCTAssertTrue(tsv.contains("1 2"))
        XCTAssertFalse(tsv.contains("1\n2"))
    }

    func testTableWithoutSeparatorIsNotATable() {
        XCTAssertTrue(QMXMarkdownTables.scan("| A | B |\n| 1 | 2 |").isEmpty)
    }

    func testSignatureUsesHeaderAndFirstRows() {
        let table = QMXMarkdownTables.scan("| A |\n| --- |\n| 1 |\n| 2 |").first
        XCTAssertEqual(table?.dedupeSignature, "A\u{1E}1\u{1E}2")
    }
}

final class StreamingBlockParserTests: XCTestCase {
    private func text(_ result: StreamingBlockParser.ParseResult) -> String {
        let blocks = result.settled + (result.activeTail.map { [$0] } ?? [])
        return blocks.map { block in
            switch block.kind {
            case .paragraph(let text): return text
            case .heading(_, let text): return text
            case .listItem(let prefix, let text): return prefix + text
            case .blockquote(let text): return text
            case .codeBlock(_, let code, _): return code
            case .inlineTable(_, _, _, _, _, _, _, let raw): return raw
            case .tableNotice(_, _, _, _, _, let raw): return raw
            }
        }.joined(separator: "\n")
    }

    func testClosedCodeFence() {
        let result = StreamingBlockParser.parse(
            "```python\nprint(1)\n```",
            turnId: "t",
            isFinished: true
        )
        let codeBlocks = result.settled.compactMap { block -> String? in
            if case .codeBlock(_, let code, let closed) = block.kind, closed { return code }
            return nil
        }
        XCTAssertEqual(codeBlocks.first, "print(1)")
    }

    func testUnclosedCodeFenceAtFinishStaysCode() {
        let result = StreamingBlockParser.parse(
            "```\nlet x = 1",
            turnId: "t",
            isFinished: true
        )
        XCTAssertTrue(text(result).contains("let x = 1"))
    }

    func testHeadingAndParagraph() {
        let result = StreamingBlockParser.parse(
            "## 标题\n正文内容",
            turnId: "t",
            isFinished: true
        )
        let rendered = text(result)
        XCTAssertTrue(rendered.contains("标题"))
        XCTAssertTrue(rendered.contains("正文内容"))
    }

    func testTableDetection() {
        let result = StreamingBlockParser.parse(
            "| A | B |\n| --- | --- |\n| 1 | 2 |",
            turnId: "t",
            isFinished: true
        )
        XCTAssertTrue(result.hasAnyTable)
    }

    func testCJKTextSurvives() {
        let result = StreamingBlockParser.parse(
            "这是中文段落，包含标点。",
            turnId: "t",
            isFinished: true
        )
        XCTAssertTrue(text(result).contains("这是中文段落"))
    }

    func testUnclosedEmphasisDoesNotDropText() {
        let result = StreamingBlockParser.parse(
            "**未闭合的强调",
            turnId: "t",
            isFinished: true
        )
        XCTAssertTrue(text(result).contains("未闭合的强调"))
    }

    func testListItemPrefix() {
        let result = StreamingBlockParser.parse(
            "- 第一项\n- 第二项",
            turnId: "t",
            isFinished: false
        )
        XCTAssertTrue(text(result).contains("第一项"))
    }

    func testEmptyInput() {
        let result = StreamingBlockParser.parse("", turnId: "t", isFinished: true)
        XCTAssertTrue(result.settled.isEmpty)
        XCTAssertNil(result.activeTail)
        XCTAssertFalse(result.hasAnyTable)
    }

    func testFinishedInputHasNoActiveTail() {
        let result = StreamingBlockParser.parse("普通段落", turnId: "t", isFinished: true)
        XCTAssertNil(result.activeTail)
        XCTAssertFalse(result.settled.isEmpty)
    }
}

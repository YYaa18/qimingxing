import XCTest
@testable import Qimingxing

final class CardsTests: XCTestCase {
    // MARK: - Payload decoding

    func testCardDecodesFullPayload() {
        let card = OutputCardItem(dict: [
            "card_id": "card_abc",
            "card_type": "spoiler_steps",
            "title": "提示",
            "content_markdown": "- 第一步",
            "actions": [
                ["id": "more_hint", "label": "再来一条"],
                ["id": "dismiss", "label": "收起"],
            ],
        ])
        XCTAssertEqual(card?.cardId, "card_abc")
        XCTAssertEqual(card?.cardType, "spoiler_steps")
        XCTAssertEqual(card?.title, "提示")
        XCTAssertEqual(card?.contentMarkdown, "- 第一步")
        XCTAssertEqual(card?.actions.map(\.id), ["more_hint", "dismiss"])
    }

    func testCardWithoutIdIsRejected() {
        XCTAssertNil(OutputCardItem(dict: ["title": "无 id"]))
        XCTAssertNil(OutputCardItem(dict: ["card_id": "", "title": "空 id"]))
    }

    func testCardDefaultsTypeAndTitle() {
        let card = OutputCardItem(dict: ["card_id": "c1"])
        XCTAssertEqual(card?.cardType, "markdown")
        XCTAssertEqual(card?.title, "")
        XCTAssertEqual(card?.contentMarkdown, "")
        XCTAssertEqual(card?.actions, [])
    }

    func testActionWithoutIdIsDropped() {
        let card = OutputCardItem(dict: [
            "card_id": "c1",
            "actions": [
                ["label": "无 id"],
                ["id": "dismiss", "label": "收起"],
            ],
        ])
        XCTAssertEqual(card?.actions.map(\.id), ["dismiss"])
    }

    func testCardCodableRoundTrip() throws {
        let card = OutputCardItem(
            cardId: "card_1",
            cardType: "markdown",
            title: "T",
            contentMarkdown: "body",
            actions: [CardActionItem(id: "dismiss", label: "收起")]
        )
        let data = try JSONEncoder().encode(card)
        let decoded = try JSONDecoder().decode(OutputCardItem.self, from: data)
        XCTAssertEqual(decoded, card)
    }

    func testChatEntryCardsDefaultEmpty() {
        let entry = ChatEntry(id: "e", turnId: "t", isUser: false, meta: "", text: "")
        XCTAssertEqual(entry.cards, [])
    }

    // MARK: - Marker stripping

    func testMarkerStripLeavesPlainText() {
        XCTAssertEqual(QMXCardMarkers.strip("普通回答"), "普通回答")
    }

    func testMarkerStripRemovesInlineMarker() {
        XCTAssertEqual(
            QMXCardMarkers.strip("前文 <<<CARD title=\"x\">>> 后文"),
            "前文  后文"
        )
    }

    func testMarkerStripRemovesEndMarkerCaseInsensitive() {
        XCTAssertEqual(
            QMXCardMarkers.strip("abc <<<end_card>>> def"),
            "abc  def"
        )
    }

    func testMarkerStripKeepsBodyText() {
        // Malformed fragments: keep the inline text, drop only the tokens.
        XCTAssertEqual(
            QMXCardMarkers.strip("<<<CARD title=broken>>>正文仍在"),
            "正文仍在"
        )
    }

    func testMarkerStripHandlesBothMarkers() {
        XCTAssertEqual(
            QMXCardMarkers.strip("<<<CARD title=\"t\">>>\n- 提示\n<<<END_CARD>>>"),
            "\n- 提示\n"
        )
    }

    // MARK: - Table signatures

    func testSignatureIgnoresWhitespace() {
        let a = QMXTableDedupe.signature(columns: ["A", "B"], rows: [[" 1 ", "2"]])
        let b = QMXTableDedupe.signature(columns: [" A", "B "], rows: [["1", " 2"]])
        XCTAssertEqual(a, b)
    }

    func testSignatureDiffersForDifferentHeader() {
        XCTAssertNotEqual(
            QMXTableDedupe.signature(columns: ["A"], rows: [["1"]]),
            QMXTableDedupe.signature(columns: ["B"], rows: [["1"]])
        )
    }

    func testSignatureDiffersForDifferentFirstRows() {
        XCTAssertNotEqual(
            QMXTableDedupe.signature(columns: ["A"], rows: [["1"], ["2"]]),
            QMXTableDedupe.signature(columns: ["A"], rows: [["1"], ["3"]])
        )
    }

    func testSignatureToleratesTruncationAfterSample() {
        let full = QMXTableDedupe.signature(
            columns: ["A"], rows: [["1"], ["2"], ["3"], ["4"], ["5"]]
        )
        let truncated = QMXTableDedupe.signature(
            columns: ["A"], rows: [["1"], ["2"], ["3"]]
        )
        XCTAssertEqual(full, truncated)
    }

    // MARK: - Media dedupe

    func testMediaWithoutInlineTablesReturnsUnchangedWhenNoTables() {
        let media = [tableMedia(columns: ["A"], rows: [["1"]])]
        XCTAssertEqual(qmxMediaWithoutInlineTables(media, answer: "纯文字"), media)
    }

    func testMediaMatchingAnswerTableIsDropped() {
        let media = [tableMedia(columns: ["A", "B"], rows: [["1", "2"]])]
        let answer = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        XCTAssertTrue(qmxMediaWithoutInlineTables(media, answer: answer).isEmpty)
    }

    func testMediaMatchingCardTableIsDropped() {
        let media = [tableMedia(columns: ["A", "B"], rows: [["1", "2"]])]
        let card = OutputCardItem(
            cardId: "c1",
            cardType: "markdown",
            title: "表",
            contentMarkdown: "| A | B |\n| --- | --- |\n| 1 | 2 |",
            actions: []
        )
        XCTAssertTrue(
            qmxMediaWithoutInlineTables(media, answer: "", cards: [card]).isEmpty
        )
    }

    func testUnrelatedTableMediaIsKept() {
        let media = [tableMedia(columns: ["X"], rows: [["9"]])]
        let answer = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        XCTAssertEqual(qmxMediaWithoutInlineTables(media, answer: answer).count, 1)
    }

    func testNonTableMediaIsKept() {
        let media = [
            MediaItem(mediaId: "img_1", kind: "image", title: "图", mimeType: "image/png")
        ]
        let answer = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        XCTAssertEqual(qmxMediaWithoutInlineTables(media, answer: answer).count, 1)
    }

    func testEmptyMediaReturnsEmpty() {
        XCTAssertTrue(qmxMediaWithoutInlineTables([], answer: "x").isEmpty)
    }

    // MARK: - Streaming parser

    func testValidCardBodyIsHidden() {
        let result = StreamingBlockParser.parse(
            "前文\n<<<CARD title=\"t\">>>\n秘密\n<<<END_CARD>>>\n后文",
            turnId: "t",
            isFinished: true
        )
        let text = renderText(result)
        XCTAssertTrue(text.contains("前文"))
        XCTAssertTrue(text.contains("后文"))
        XCTAssertFalse(text.contains("秘密"))
    }

    func testInvalidCardTagStaysVisible() {
        // No whitespace or `>>>` after `<<<CARD`: same rule as the brain parser.
        let result = StreamingBlockParser.parse(
            "<<<CARD\n正文",
            turnId: "t",
            isFinished: true
        )
        let text = renderText(result)
        XCTAssertTrue(text.contains("<<<CARD"))
        XCTAssertTrue(text.contains("正文"))
    }

    func testUnclosedCardBodyIsHidden() {
        let result = StreamingBlockParser.parse(
            "前文\n<<<CARD title=\"t\">>>\n还在流式输出",
            turnId: "t",
            isFinished: false
        )
        let text = renderText(result)
        XCTAssertTrue(text.contains("前文"))
        XCTAssertFalse(text.contains("还在流式输出"))
    }

    // MARK: - Workspace persistence

    func testWorkspaceItemDecodesLegacyJSONWithoutCards() throws {
        let item = WorkspaceItem(
            id: "turn_t1",
            kind: .answer,
            title: "t",
            prompt: "p",
            body: "b",
            status: "completed",
            progress: 0,
            path: "",
            createdAt: Date(timeIntervalSince1970: 1000),
            updatedAt: Date(timeIntervalSince1970: 1001)
        )
        let data = try JSONEncoder().encode(item)
        var object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        object.removeValue(forKey: "cards")
        let legacy = try JSONSerialization.data(withJSONObject: object)
        let decoded = try JSONDecoder().decode(WorkspaceItem.self, from: legacy)
        XCTAssertNil(decoded.cards)
    }

    func testWorkspaceItemRoundTripsCards() throws {
        let card = OutputCardItem(
            cardId: "card_1",
            cardType: "spoiler_steps",
            title: "T",
            contentMarkdown: "body",
            actions: [CardActionItem(id: "dismiss", label: "收起")]
        )
        var item = WorkspaceItem(
            id: "turn_t1",
            kind: .answer,
            title: "t",
            prompt: "p",
            body: "",
            status: "completed",
            progress: 0,
            path: "",
            createdAt: Date(timeIntervalSince1970: 1000),
            updatedAt: Date(timeIntervalSince1970: 1001)
        )
        item.cards = [card]
        let data = try JSONEncoder().encode(item)
        let decoded = try JSONDecoder().decode(WorkspaceItem.self, from: data)
        XCTAssertEqual(decoded.cards, [card])
    }

    // MARK: - Helpers

    private func tableMedia(columns: [String], rows: [[String]]) -> MediaItem {
        MediaItem(
            mediaId: "tbl_1",
            kind: "file",
            title: "表",
            mimeType: "text/markdown",
            provider: "qimingxing",
            fileExt: "md-table",
            previewColumns: columns,
            previewRows: rows
        )
    }

    private func renderText(_ result: StreamingBlockParser.ParseResult) -> String {
        let blocks = result.settled + (result.activeTail.map { [$0] } ?? [])
        return blocks.map { block in
            switch block.kind {
            case .paragraph(let text):
                return text
            case .heading(_, let text):
                return text
            case .listItem(let prefix, let text):
                return prefix + text
            case .blockquote(let text):
                return text
            case .codeBlock(_, let code, _):
                return code
            case .inlineTable(_, _, _, let rows, _, _, _, let raw):
                return raw + rows.flatMap { $0 }.joined()
            case .tableNotice(_, _, _, _, _, let raw):
                return raw
            }
        }.joined(separator: "\n")
    }
}

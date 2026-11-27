import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

enum QMXDragPayload: Equatable, Sendable {
    case table(title: String, columns: [String], rows: [[String]])
    case code(language: String, code: String)
    case file(path: String, suggestedName: String)
}

enum QMXFilePromiseSerializer {
    static func csvData(columns: [String], rows: [[String]]) -> Data {
        func escape(_ cell: String) -> String {
            let cleaned = cell.replacingOccurrences(of: "\r\n", with: "\n")
            if cleaned.contains(",") || cleaned.contains("\"") || cleaned.contains("\n") || cleaned.contains("\r") {
                let doubled = cleaned.replacingOccurrences(of: "\"", with: "\"\"")
                return "\"\(doubled)\""
            }
            return cleaned
        }

        var lines: [String] = []
        if !columns.isEmpty {
            lines.append(columns.map(escape).joined(separator: ","))
        }
        for row in rows {
            let normalized = columns.isEmpty
                ? row
                : (0..<columns.count).map { idx in idx < row.count ? row[idx] : "" }
            lines.append(normalized.map(escape).joined(separator: ","))
        }
        let csvString = "\u{FEFF}" + lines.joined(separator: "\r\n") + "\r\n"
        return csvString.data(using: .utf8) ?? Data()
    }

    static func fileExtension(forLanguage language: String) -> String {
        switch language.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "swift": return "swift"
        case "python", "py": return "py"
        case "javascript", "js": return "js"
        case "typescript", "ts": return "ts"
        case "tsx": return "tsx"
        case "jsx": return "jsx"
        case "json": return "json"
        case "yaml", "yml": return "yml"
        case "bash", "sh", "zsh", "shell": return "sh"
        case "sql": return "sql"
        case "html", "htm": return "html"
        case "css": return "css"
        case "markdown", "md": return "md"
        case "go", "golang": return "go"
        case "rust", "rs": return "rs"
        case "c": return "c"
        case "cpp", "c++", "cxx": return "cpp"
        default: return "txt"
        }
    }

    static func sanitizedFilename(_ raw: String, ext: String, fallback: String) -> String {
        let invalid = CharacterSet(charactersIn: "/:\\?*\"<>|\n\r\t")
        let stripped = raw
            .components(separatedBy: invalid)
            .joined(separator: "_")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let base = stripped.isEmpty ? fallback : String(stripped.prefix(48))
        let cleanExt = ext.trimmingCharacters(in: CharacterSet(charactersIn: "."))
        if cleanExt.isEmpty {
            return base
        }
        if base.lowercased().hasSuffix(".\(cleanExt.lowercased())") {
            return base
        }
        return "\(base).\(cleanExt)"
    }

    static func suggestedFilename(for payload: QMXDragPayload) -> String {
        switch payload {
        case .table(let title, _, _):
            return sanitizedFilename(title, ext: "csv", fallback: "qimingxing-table")
        case .code(let language, _):
            let ext = fileExtension(forLanguage: language)
            let stem = language.isEmpty ? "snippet" : "snippet-\(language.lowercased())"
            return sanitizedFilename(stem, ext: ext, fallback: "snippet")
        case .file(let path, let suggestedName):
            let url = URL(fileURLWithPath: path)
            let ext = url.pathExtension
            let rawStem = suggestedName.isEmpty ? url.deletingPathExtension().lastPathComponent : suggestedName
            return sanitizedFilename(rawStem, ext: ext, fallback: "qimingxing-file")
        }
    }

    static func utTypeIdentifier(for payload: QMXDragPayload) -> String {
        switch payload {
        case .table:
            return UTType.commaSeparatedText.identifier
        case .code:
            return UTType.plainText.identifier
        case .file(let path, _):
            let ext = URL(fileURLWithPath: path).pathExtension
            if !ext.isEmpty, let inferred = UTType(filenameExtension: ext) {
                return inferred.identifier
            }
            return UTType.data.identifier
        }
    }

    static func write(payload: QMXDragPayload, to destinationURL: URL) throws {
        switch payload {
        case .table(_, let columns, let rows):
            let data = csvData(columns: columns, rows: rows)
            try data.write(to: destinationURL, options: .atomic)
        case .code(_, let code):
            let data = (code + "\n").data(using: .utf8) ?? Data()
            try data.write(to: destinationURL, options: .atomic)
        case .file(let path, _):
            let sourceURL = URL(fileURLWithPath: path)
            let fm = FileManager.default
            if fm.fileExists(atPath: destinationURL.path) {
                try fm.removeItem(at: destinationURL)
            }
            try fm.copyItem(at: sourceURL, to: destinationURL)
        }
    }
}

final class QMXFilePromiseDelegate: NSObject, NSFilePromiseProviderDelegate, @unchecked Sendable {
    private static let promiseQueue: OperationQueue = {
        let queue = OperationQueue()
        queue.name = "com.qimingxing.filePromiseQueue"
        queue.qualityOfService = .userInitiated
        queue.maxConcurrentOperationCount = 2
        return queue
    }()

    let payload: QMXDragPayload

    init(payload: QMXDragPayload) {
        self.payload = payload
    }

    func filePromiseProvider(
        _ filePromiseProvider: NSFilePromiseProvider,
        fileNameForType fileType: String
    ) -> String {
        QMXFilePromiseSerializer.suggestedFilename(for: payload)
    }

    func operationQueue(for filePromiseProvider: NSFilePromiseProvider) -> OperationQueue {
        Self.promiseQueue
    }

    func filePromiseProvider(
        _ filePromiseProvider: NSFilePromiseProvider,
        writePromiseTo url: URL,
        completionHandler: @escaping (Error?) -> Void
    ) {
        do {
            try QMXFilePromiseSerializer.write(payload: payload, to: url)
            completionHandler(nil)
        } catch {
            completionHandler(error)
        }
    }
}

final class QMXDragSourceView: NSView, NSDraggingSource {
    var payload: QMXDragPayload {
        didSet { needsDisplay = true }
    }
    var symbolName: String = "arrow.up.forward.square"
    var onDragBegan: (() -> Void)?

    private var mouseDownLocation: NSPoint?
    private var mouseDownEvent: NSEvent?
    private var activeDelegate: QMXFilePromiseDelegate?
    private var isHovering = false
    private var trackingArea: NSTrackingArea?

    init(payload: QMXDragPayload) {
        self.payload = payload
        super.init(frame: NSRect(x: 0, y: 0, width: 22, height: 22))
        toolTip = "拖出保存为 \(QMXFilePromiseSerializer.suggestedFilename(for: payload))"
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingArea {
            removeTrackingArea(trackingArea)
        }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
        trackingArea = area
    }

    override func mouseEntered(with event: NSEvent) {
        isHovering = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        isHovering = false
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        if isHovering {
            NSColor.labelColor.withAlphaComponent(0.08).setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 5, yRadius: 5).fill()
        }
        let config = NSImage.SymbolConfiguration(pointSize: 11, weight: .medium)
        if let icon = NSImage(systemSymbolName: symbolName, accessibilityDescription: "拖出文件")?
            .withSymbolConfiguration(config) {
            let color = isHovering ? NSColor.labelColor : NSColor.tertiaryLabelColor
            let tinted = NSImage(size: icon.size, flipped: false) { rect in
                icon.draw(in: rect)
                color.set()
                rect.fill(using: .sourceAtop)
                return true
            }
            let x = (bounds.width - icon.size.width) / 2
            let y = (bounds.height - icon.size.height) / 2
            tinted.draw(at: NSPoint(x: x, y: y), from: .zero, operation: .sourceOver, fraction: 1.0)
        }
    }

    override func mouseDown(with event: NSEvent) {
        mouseDownLocation = convert(event.locationInWindow, from: nil)
        mouseDownEvent = event
    }

    override func mouseDragged(with event: NSEvent) {
        guard let downLoc = mouseDownLocation, let downEvent = mouseDownEvent else { return }
        let current = convert(event.locationInWindow, from: nil)
        let dist = hypot(current.x - downLoc.x, current.y - downLoc.y)
        guard dist >= 4.0 else { return }

        mouseDownLocation = nil
        mouseDownEvent = nil

        let delegate = QMXFilePromiseDelegate(payload: payload)
        activeDelegate = delegate
        let utType = QMXFilePromiseSerializer.utTypeIdentifier(for: payload)
        let provider = NSFilePromiseProvider(fileType: utType, delegate: delegate)

        let dragItem = NSDraggingItem(pasteboardWriter: provider)
        let preview = dragPreviewImage()
        dragItem.setDraggingFrame(
            NSRect(x: 0, y: 0, width: preview.size.width, height: preview.size.height),
            contents: preview
        )

        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
        onDragBegan?()
        beginDraggingSession(with: [dragItem], event: downEvent, source: self)
    }

    func draggingSession(
        _ session: NSDraggingSession,
        sourceOperationMaskFor context: NSDraggingContext
    ) -> NSDragOperation {
        .copy
    }

    func draggingSession(
        _ session: NSDraggingSession,
        endedAt screenPoint: NSPoint,
        operation: NSDragOperation
    ) {
        if operation != [] {
            NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
        }
        activeDelegate = nil
    }

    private func dragPreviewImage() -> NSImage {
        let label = QMXFilePromiseSerializer.suggestedFilename(for: payload) as NSString
        let font = NSFont.systemFont(ofSize: 11, weight: .medium)
        let textSize = label.size(withAttributes: [.font: font])
        let size = NSSize(width: min(220, ceil(textSize.width) + 28), height: 26)
        return NSImage(size: size, flipped: false) { rect in
            NSColor.windowBackgroundColor.withAlphaComponent(0.92).setFill()
            let path = NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8)
            path.fill()
            NSColor.separatorColor.withAlphaComponent(0.4).setStroke()
            path.lineWidth = 0.5
            path.stroke()
            label.draw(
                in: NSRect(x: 10, y: (rect.height - textSize.height) / 2, width: rect.width - 20, height: textSize.height),
                withAttributes: [.font: font, .foregroundColor: NSColor.labelColor]
            )
            return true
        }
    }
}

struct QMXDragOutHandle: NSViewRepresentable {
    let payload: QMXDragPayload

    func makeNSView(context: Context) -> QMXDragSourceView {
        QMXDragSourceView(payload: payload)
    }

    func updateNSView(_ nsView: QMXDragSourceView, context: Context) {
        if nsView.payload != payload {
            nsView.payload = payload
            nsView.toolTip = "拖出保存为 \(QMXFilePromiseSerializer.suggestedFilename(for: payload))"
        }
    }
}

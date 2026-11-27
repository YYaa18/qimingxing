import AppKit
import ApplicationServices
import Carbon
import Foundation

// MARK: - Selection Model & Prompt Formatting

struct CapturedSelectionContext: Equatable, Sendable {
    let text: String
    let appName: String
    let appBundleId: String
    let lineCount: Int

    var summaryLabel: String {
        let target = appName.isEmpty ? "当前应用" : appName
        if lineCount > 1 {
            return "已捕获 · \(target) 选区 · \(lineCount) 行"
        }
        return "已捕获 · \(target) 选区 · \(text.count) 字"
    }

    static func make(text: String, appName: String, bundleId: String) -> CapturedSelectionContext? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let capped = text.count > 16_000 ? String(text.prefix(16_000)) : text
        let lines = capped.components(separatedBy: .newlines).count
        return CapturedSelectionContext(
            text: capped,
            appName: appName,
            appBundleId: bundleId,
            lineCount: max(1, lines)
        )
    }
}

// MARK: - Lossless Multi-Type Pasteboard Snapshot

struct PasteboardSnapshotItem: Equatable {
    struct Entry: Equatable {
        let typeRawValue: String
        let data: Data
    }

    let entries: [Entry]
}

enum PasteboardSnapshot {
    static func capture(from pasteboard: NSPasteboard = .general) -> [PasteboardSnapshotItem] {
        guard let pbItems = pasteboard.pasteboardItems else { return [] }
        return pbItems.compactMap { item in
            let entries: [PasteboardSnapshotItem.Entry] = item.types.compactMap { type in
                guard let data = item.data(forType: type) else { return nil }
                return PasteboardSnapshotItem.Entry(typeRawValue: type.rawValue, data: data)
            }
            return entries.isEmpty ? nil : PasteboardSnapshotItem(entries: entries)
        }
    }

    static func restore(_ snapshot: [PasteboardSnapshotItem], to pasteboard: NSPasteboard = .general) {
        pasteboard.clearContents()
        guard !snapshot.isEmpty else { return }
        let restored: [NSPasteboardItem] = snapshot.map { snap in
            let pbItem = NSPasteboardItem()
            for entry in snap.entries {
                pbItem.setData(entry.data, forType: NSPasteboard.PasteboardType(entry.typeRawValue))
            }
            return pbItem
        }
        pasteboard.writeObjects(restored)
    }

    static func roundTripVerify(items: [PasteboardSnapshotItem]) -> Bool {
        let name = NSPasteboard.Name("com.qimingxing.verify.pb.\(UUID().uuidString.prefix(8))")
        let pb = NSPasteboard(name: name)
        defer { pb.releaseGlobally() }

        restore(items, to: pb)
        let original = capture(from: pb)

        pb.clearContents()
        pb.setString("temporary-overwrite-payload", forType: .string)

        restore(original, to: pb)
        let restored = capture(from: pb)
        return restored == items
    }
}

// MARK: - Ghost Write Engine (AX Primary + Silent Clipboard Transaction Fallback)

@MainActor
enum GhostWriteEngine {
    static func captureSelection(from frontApp: NSRunningApplication?) -> CapturedSelectionContext? {
        guard let frontApp else { return nil }
        if frontApp.bundleIdentifier == Bundle.main.bundleIdentifier {
            return nil
        }
        let appName = frontApp.localizedName ?? "前台应用"
        let bundleId = frontApp.bundleIdentifier ?? ""

        // Primary path: Accessibility API (zero clipboard pollution)
        if AXIsProcessTrusted() {
            if let axText = readSelectedTextViaAX(pid: frontApp.processIdentifier),
               let ctx = CapturedSelectionContext.make(text: axText, appName: appName, bundleId: bundleId) {
                return ctx
            }
            // Fallback path for Electron / Chrome / webviews: silent Cmd+C + 60ms restore
            if let clipText = readSelectedTextViaSilentCopy(),
               let ctx = CapturedSelectionContext.make(text: clipText, appName: appName, bundleId: bundleId) {
                return ctx
            }
        }
        return nil
    }

    static func replaceSelection(
        with text: String,
        targetApp: NSRunningApplication?,
        onComplete: @escaping @MainActor (Bool) -> Void
    ) {
        let clean = qmxCleanAssistantText(text)
        guard !clean.isEmpty else {
            onComplete(false)
            return
        }

        if let targetApp, !targetApp.isTerminated {
            targetApp.activate(options: [])
        }

        let targetPid = targetApp?.processIdentifier
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 120_000_000)

            // Primary path: AX direct attribute replacement
            if AXIsProcessTrusted(), let pid = targetPid, writeSelectedTextViaAX(clean, pid: pid) {
                NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
                onComplete(true)
                return
            }

            // Fallback path: lossless pasteboard snapshot + Cmd+V + 60ms restore
            guard AXIsProcessTrusted() else {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(clean, forType: .string)
                let prefsURL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
                TaskToastController.shared.show(
                    title: "已复制到剪贴板",
                    message: "拖放图标到系统「辅助功能」即可直接原位覆盖写回",
                    style: .info,
                    actionTitle: "去拖放授权",
                    route: .systemURL(prefsURL)
                ) {
                    PermissionGuideController.shared.show(for: .accessibility, openSettings: true)
                }
                onComplete(false)
                return
            }

            let pb = NSPasteboard.general
            let backup = PasteboardSnapshot.capture(from: pb)
            pb.clearContents()
            pb.setString(clean, forType: .string)
            postCommandKey(virtualKey: CGKeyCode(kVK_ANSI_V))

            try? await Task.sleep(nanoseconds: 65_000_000)
            PasteboardSnapshot.restore(backup, to: pb)
            NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
            onComplete(true)
        }
    }

    static func formatPromptWithSelection(
        userPrompt: String,
        selection: CapturedSelectionContext
    ) -> String {
        let appLabel = selection.appName.isEmpty ? "当前应用" : selection.appName
        let trimmedPrompt = userPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return """
        以下是我在「\(appLabel)」中选中的内容：
        ```
        \(selection.text)
        ```

        请根据我的要求直接输出处理后的最终结果（不要添加多余寒暄，以便我一键原位替换）：
        \(trimmedPrompt)
        """
    }

    // MARK: - Private AX & CGEvent Helpers

    private static func focusedAXElement(pid: pid_t) -> AXUIElement? {
        let appElement = AXUIElementCreateApplication(pid)
        var focusedRef: CFTypeRef?
        let appStatus = AXUIElementCopyAttributeValue(
            appElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        )
        if appStatus == .success, let focusedRef {
            return (focusedRef as! AXUIElement)
        }
        let sysElement = AXUIElementCreateSystemWide()
        var sysFocused: CFTypeRef?
        let sysStatus = AXUIElementCopyAttributeValue(
            sysElement,
            kAXFocusedUIElementAttribute as CFString,
            &sysFocused
        )
        if sysStatus == .success, let sysFocused {
            return (sysFocused as! AXUIElement)
        }
        return nil
    }

    private static func readSelectedTextViaAX(pid: pid_t) -> String? {
        guard let element = focusedAXElement(pid: pid) else { return nil }
        var selectedRef: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(
            element,
            kAXSelectedTextAttribute as CFString,
            &selectedRef
        )
        guard status == .success, let str = selectedRef as? String else { return nil }
        return str
    }

    private static func writeSelectedTextViaAX(_ text: String, pid: pid_t) -> Bool {
        guard let element = focusedAXElement(pid: pid) else { return false }
        var isSettable = DarwinBoolean(false)
        let settableErr = AXUIElementIsAttributeSettable(
            element,
            kAXSelectedTextAttribute as CFString,
            &isSettable
        )
        guard settableErr == .success, isSettable.boolValue else { return false }
        return AXUIElementSetAttributeValue(
            element,
            kAXSelectedTextAttribute as CFString,
            text as CFTypeRef
        ) == .success
    }

    private static func readSelectedTextViaSilentCopy() -> String? {
        let pb = NSPasteboard.general
        let backup = PasteboardSnapshot.capture(from: pb)
        let beforeCount = pb.changeCount
        defer {
            PasteboardSnapshot.restore(backup, to: pb)
        }

        postCommandKey(virtualKey: CGKeyCode(kVK_ANSI_C))
        for _ in 0..<4 {
            usleep(15_000)
            if pb.changeCount != beforeCount {
                return pb.string(forType: .string)
            }
        }
        return nil
    }

    private static func postCommandKey(virtualKey: CGKeyCode) {
        guard let source = CGEventSource(stateID: .combinedSessionState),
              let down = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false)
        else { return }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
    }
}

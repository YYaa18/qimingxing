import AppKit
import AVFoundation
import Foundation
import QuickLookUI
@preconcurrency import UserNotifications

@MainActor
final class DesktopActions: NSObject, QLPreviewPanelDataSource, QLPreviewPanelDelegate, UNUserNotificationCenterDelegate {
    private var previewURLs: [URL] = []
    private var pinPanel: NSPanel?
    private var notificationsAuthorized = false
    private let speechSynthesizer = AVSpeechSynthesizer()
    var onNotificationOpenRequested: ((String) -> Void)?

    private static let blockedExtensions: Set<String> = [
        "sh", "command", "app", "pkg", "dmg", "scpt", "applescript",
        "py", "rb", "pl", "exe", "bat", "ps1", "workflow"
    ]

    override init() {
        super.init()
        if Bundle.main.bundleURL.pathExtension == "app" {
            let center = UNUserNotificationCenter.current()
            center.delegate = self
            center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
                Task { @MainActor in
                    self.notificationsAuthorized = granted
                }
            }
        }
    }

    func revealPath(_ rawPath: String) -> (Bool, String) {
        let url = resolveURL(rawPath)
        guard FileManager.default.fileExists(atPath: url.path) else {
            return (false, "file_not_found")
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
        return (true, "")
    }

    func openPath(_ rawPath: String) -> (Bool, String) {
        let url = resolveURL(rawPath)
        let ext = url.pathExtension.lowercased()
        if Self.blockedExtensions.contains(ext) {
            return (false, "dangerous_executable_blocked")
        }
        guard FileManager.default.fileExists(atPath: url.path) else {
            return (false, "file_not_found")
        }
        let ok = NSWorkspace.shared.open(url)
        return (ok, ok ? "" : "open_failed")
    }

    func copyFile(_ rawPath: String) -> (Bool, String) {
        let url = resolveURL(rawPath)
        guard FileManager.default.fileExists(atPath: url.path) else {
            return (false, "file_not_found")
        }
        let pb = NSPasteboard.general
        pb.clearContents()
        let ok = pb.writeObjects([url as NSURL])
        return (ok, ok ? "" : "pasteboard_failed")
    }

    func quickLook(_ rawPath: String) -> (Bool, String) {
        let url = resolveURL(rawPath)
        guard FileManager.default.fileExists(atPath: url.path) else {
            return (false, "file_not_found")
        }
        previewURLs = [url]
        if let panel = QLPreviewPanel.shared() {
            panel.dataSource = self
            panel.delegate = self
            panel.reloadData()
            NSApp.activate(ignoringOtherApps: true)
            panel.makeKeyAndOrderFront(nil)
            return (true, "")
        }
        return (false, "quicklook_unavailable")
    }

    func showPinImage(_ rawPath: String, title: String = "启明星钉图") -> (Bool, String) {
        let url = resolveURL(rawPath)
        guard let image = NSImage(contentsOf: url) else {
            return (false, "invalid_image")
        }
        pinPanel?.close()

        let maxDim: CGFloat = 440
        let imgSize = image.size
        let scale = min(1.0, min(maxDim / max(1, imgSize.width), maxDim / max(1, imgSize.height)))
        let w = max(160, imgSize.width * scale)
        let h = max(120, imgSize.height * scale)

        let panel = NSPanel(
            contentRect: NSRect(x: 120, y: 120, width: w, height: h + 28),
            styleMask: [.nonactivatingPanel, .titled, .closable, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        panel.title = title
        panel.level = .floating
        panel.isMovableByWindowBackground = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let iv = NSImageView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        iv.image = image
        iv.imageScaling = .scaleProportionallyUpOrDown
        iv.autoresizingMask = [.width, .height]
        panel.contentView = iv
        panel.center()
        panel.orderFrontRegardless()
        pinPanel = panel
        return (true, "")
    }

    func showProactiveNotification(title: String, body: String, taskId: String = "") {
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        let content = UNMutableNotificationContent()
        content.title = title.isEmpty ? "启明星" : title
        content.body = body
        content.sound = .default
        if !taskId.isEmpty {
            content.userInfo = ["task_id": taskId]
        }
        let req = UNNotificationRequest(
            identifier: "qmx_\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(req, withCompletionHandler: nil)

        let spoken = [title, body]
            .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            .joined(separator: "。")
        if !spoken.isEmpty {
            speak(spoken)
        }
    }

    private func speak(_ text: String) {
        speechSynthesizer.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: String(text.prefix(160)))
        utterance.voice = AVSpeechSynthesisVoice(language: "zh-CN")
        utterance.rate = 0.48
        utterance.pitchMultiplier = 1.0
        speechSynthesizer.speak(utterance)
    }

    func openLogsDirectory() {
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let logsURL = appSupport.appendingPathComponent("Qimingxing/logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logsURL, withIntermediateDirectories: true)
        NSWorkspace.shared.open(logsURL)
    }

    private func resolveURL(_ rawPath: String) -> URL {
        let expanded = (rawPath as NSString).expandingTildeInPath
        return URL(fileURLWithPath: expanded)
    }

    // MARK: - QLPreviewPanelDataSource

    nonisolated func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int {
        MainActor.assumeIsolated { previewURLs.count }
    }

    nonisolated func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> (any QLPreviewItem)! {
        MainActor.assumeIsolated { previewURLs[index] as NSURL }
    }

    // MARK: - UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let taskId = (response.notification.request.content.userInfo["task_id"] as? String) ?? ""
        Task { @MainActor in
            self.onNotificationOpenRequested?(taskId)
        }
        completionHandler()
    }
}

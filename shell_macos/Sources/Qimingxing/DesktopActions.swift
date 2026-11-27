import AppKit
import AVFoundation
import Combine
import Foundation
import QuickLookUI
@preconcurrency import UserNotifications

enum NotificationRoute: Equatable, Sendable {
    case quickEntry
    case workspace(taskId: String)
    case settings(tab: String?)
    case systemURL(String)
    case none

    private enum Keys {
        static let routeKind = "route_kind"
        static let taskId = "task_id"
        static let settingsTab = "settings_tab"
        static let systemURL = "system_url"
    }

    var categoryIdentifier: String {
        switch self {
        case .quickEntry, .workspace:
            return DesktopActions.notificationCategoryResult
        case .settings, .systemURL:
            return DesktopActions.notificationCategorySettings
        case .none:
            return DesktopActions.notificationCategoryInfo
        }
    }

    func toUserInfo() -> [AnyHashable: Any] {
        switch self {
        case .quickEntry:
            return [Keys.routeKind: "quick_entry"]
        case .workspace(let taskId):
            return [Keys.routeKind: "workspace", Keys.taskId: taskId]
        case .settings(let tab):
            var dict: [AnyHashable: Any] = [Keys.routeKind: "settings"]
            if let tab, !tab.isEmpty {
                dict[Keys.settingsTab] = tab
            }
            return dict
        case .systemURL(let urlString):
            return [Keys.routeKind: "system_url", Keys.systemURL: urlString]
        case .none:
            return [Keys.routeKind: "none"]
        }
    }

    static func from(userInfo: [AnyHashable: Any]) -> NotificationRoute {
        if let kind = userInfo[Keys.routeKind] as? String {
            switch kind {
            case "quick_entry":
                return .quickEntry
            case "workspace":
                let taskId = (userInfo[Keys.taskId] as? String) ?? ""
                return .workspace(taskId: taskId)
            case "settings":
                let tab = userInfo[Keys.settingsTab] as? String
                return .settings(tab: tab)
            case "system_url":
                if let urlStr = userInfo[Keys.systemURL] as? String, !urlStr.isEmpty {
                    return .systemURL(urlStr)
                }
                return .none
            case "none":
                return .none
            default:
                break
            }
        }
        if let legacyTaskId = userInfo[Keys.taskId] as? String, !legacyTaskId.isEmpty {
            return .workspace(taskId: legacyTaskId)
        }
        return .quickEntry
    }
}

enum NativeNotificationAuthState: String, Sendable {
    case authorized
    case provisional
    case denied
    case notDetermined
    case unavailable

    var displayLabel: String {
        switch self {
        case .authorized: return "已授权系统横幅与通知中心"
        case .provisional: return "静默通知已启用"
        case .denied: return "已在系统设置中关闭"
        case .notDetermined: return "尚未请求系统通知权限"
        case .unavailable: return "非 .app 运行环境（系统通知不可用）"
        }
    }

    var isReady: Bool {
        self == .authorized || self == .provisional
    }
}

@MainActor
final class DesktopActions: NSObject, ObservableObject, QLPreviewPanelDataSource, QLPreviewPanelDelegate, UNUserNotificationCenterDelegate {
    nonisolated static let nativeNotificationsEnabledKey = "qmx.nativeNotificationsEnabled"
    nonisolated static let inAppToastEnabledKey = "qmx.inAppToastEnabled"

    nonisolated static let notificationCategoryResult = "qmx.result"
    nonisolated static let notificationCategorySettings = "qmx.settings"
    nonisolated static let notificationCategoryInfo = "qmx.info"

    private static let actionOpenResult = "OPEN_RESULT"
    private static let actionOpenSettings = "OPEN_SETTINGS"

    static var nativeNotificationsEnabled: Bool {
        (UserDefaults.standard.object(forKey: nativeNotificationsEnabledKey) as? Bool) ?? true
    }

    static var inAppToastEnabled: Bool {
        (UserDefaults.standard.object(forKey: inAppToastEnabledKey) as? Bool) ?? true
    }

    private var previewURLs: [URL] = []
    private var pinPanel: NSPanel?
    @Published private(set) var notificationAuthState: NativeNotificationAuthState = .notDetermined
    private let speechSynthesizer = AVSpeechSynthesizer()
    var onNotificationOpenRequested: ((String) -> Void)?
    var onNotificationRouteRequested: ((NotificationRoute) -> Void)?

    private static let blockedExtensions: Set<String> = [
        "sh", "command", "app", "pkg", "dmg", "scpt", "applescript",
        "py", "rb", "pl", "exe", "bat", "ps1", "workflow"
    ]

    override init() {
        super.init()
        if Bundle.main.bundleURL.pathExtension == "app" {
            let center = UNUserNotificationCenter.current()
            center.delegate = self
            registerNotificationCategories(center: center)
            refreshNotificationAuthorizationStatus()
            center.getNotificationSettings { [weak self] settings in
                let status = settings.authorizationStatus
                if status == .notDetermined {
                    UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { [weak self] _, _ in
                        Task { @MainActor [weak self] in
                            self?.refreshNotificationAuthorizationStatus()
                        }
                    }
                }
            }
        } else {
            notificationAuthState = .unavailable
        }
    }

    private func registerNotificationCategories(center: UNUserNotificationCenter) {
        let openResultAction = UNNotificationAction(
            identifier: Self.actionOpenResult,
            title: "查看结果",
            options: [.foreground]
        )
        let openSettingsAction = UNNotificationAction(
            identifier: Self.actionOpenSettings,
            title: "打开设置",
            options: [.foreground]
        )
        let resultCategory = UNNotificationCategory(
            identifier: Self.notificationCategoryResult,
            actions: [openResultAction],
            intentIdentifiers: [],
            options: [.customDismissAction]
        )
        let settingsCategory = UNNotificationCategory(
            identifier: Self.notificationCategorySettings,
            actions: [openSettingsAction],
            intentIdentifiers: [],
            options: [.customDismissAction]
        )
        let infoCategory = UNNotificationCategory(
            identifier: Self.notificationCategoryInfo,
            actions: [],
            intentIdentifiers: [],
            options: []
        )
        center.setNotificationCategories([resultCategory, settingsCategory, infoCategory])
    }

    func refreshNotificationAuthorizationStatus() {
        guard Bundle.main.bundleURL.pathExtension == "app" else {
            notificationAuthState = .unavailable
            return
        }
        UNUserNotificationCenter.current().getNotificationSettings { [weak self] settings in
            let mapped = Self.mapAuthStatus(settings.authorizationStatus)
            Task { @MainActor [weak self] in
                self?.notificationAuthState = mapped
            }
        }
    }

    func requestNotificationAuthorization(completion: (@Sendable (Bool) -> Void)? = nil) {
        guard Bundle.main.bundleURL.pathExtension == "app" else {
            notificationAuthState = .unavailable
            completion?(false)
            return
        }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound, .badge]) { [weak self] granted, _ in
            Task { @MainActor [weak self] in
                self?.refreshNotificationAuthorizationStatus()
                completion?(granted)
            }
        }
    }

    func openSystemNotificationSettings() {
        let candidates = [
            "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=com.qimingxing.desktop",
            "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
            "x-apple.systempreferences:com.apple.preference.notifications"
        ]
        for raw in candidates {
            if let url = URL(string: raw), NSWorkspace.shared.open(url) {
                return
            }
        }
    }

    private nonisolated static func mapAuthStatus(_ status: UNAuthorizationStatus) -> NativeNotificationAuthState {
        switch status {
        case .authorized:
            return .authorized
        case .provisional:
            return .provisional
        case .denied:
            return .denied
        case .notDetermined:
            return .notDetermined
        @unknown default:
            return .notDetermined
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

    func deliverNativeNotification(
        title: String,
        body: String,
        route: NotificationRoute = .quickEntry,
        categoryIdentifier: String? = nil,
        playSound: Bool = true
    ) {
        guard Self.nativeNotificationsEnabled else { return }
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }

        let center = UNUserNotificationCenter.current()
        let cleanTitle = title.isEmpty ? "启明星" : title
        let resolvedCategory = categoryIdentifier ?? route.categoryIdentifier

        let postBlock: @Sendable () -> Void = {
            let content = UNMutableNotificationContent()
            content.title = cleanTitle
            content.body = body
            if playSound {
                content.sound = .default
            }
            content.categoryIdentifier = resolvedCategory
            content.userInfo = route.toUserInfo()
            let req = UNNotificationRequest(
                identifier: "qmx_\(UUID().uuidString)",
                content: content,
                trigger: nil
            )
            center.add(req, withCompletionHandler: nil)
        }

        center.getNotificationSettings { [weak self] settings in
            let authStatus = settings.authorizationStatus
            Task { @MainActor [weak self] in
                self?.notificationAuthState = Self.mapAuthStatus(authStatus)
            }
            switch authStatus {
            case .authorized, .provisional:
                postBlock()
            case .notDetermined:
                center.requestAuthorization(options: [.alert, .sound, .badge]) { [weak self] granted, _ in
                    Task { @MainActor [weak self] in
                        self?.refreshNotificationAuthorizationStatus()
                    }
                    if granted {
                        postBlock()
                    }
                }
            default:
                break
            }
        }
    }

    func showProactiveNotification(title: String, body: String, taskId: String = "", announce: Bool = true) {
        let route: NotificationRoute = taskId.isEmpty ? .quickEntry : .workspace(taskId: taskId)
        deliverNativeNotification(
            title: title,
            body: body,
            route: route,
            categoryIdentifier: Self.notificationCategoryResult,
            playSound: true
        )

        if Self.inAppToastEnabled {
            TaskToastController.shared.show(
                title: title.isEmpty ? "启明星通知" : title,
                message: body,
                style: .info,
                actionTitle: "查看结果",
                route: route,
                mirrorToSystem: false
            ) { [weak self] in
                self?.dispatchRoute(route)
            }
        }

        let spoken = [title, body]
            .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            .joined(separator: "。")
        if announce && !spoken.isEmpty {
            speak(spoken)
        }
    }

    private func dispatchRoute(_ route: NotificationRoute) {
        if let onNotificationRouteRequested {
            onNotificationRouteRequested(route)
            return
        }
        switch route {
        case .workspace(let taskId):
            onNotificationOpenRequested?(taskId)
        case .quickEntry:
            onNotificationOpenRequested?("")
        case .settings(let tab):
            NotificationCenter.default.post(name: .qmxOpenSettings, object: tab)
        case .systemURL(let urlString):
            if let url = URL(string: urlString) {
                NSWorkspace.shared.open(url)
            }
        case .none:
            break
        }
    }

    private func speak(_ text: String) {
        let enabled = (UserDefaults.standard.object(forKey: "qmx.ttsEnabled") as? Bool) ?? true
        guard enabled else { return }
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
        completionHandler([.banner, .list, .sound])
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if response.actionIdentifier == UNNotificationDismissActionIdentifier {
            completionHandler()
            return
        }
        let userInfo = response.notification.request.content.userInfo
        let route = NotificationRoute.from(userInfo: userInfo)
        Task { @MainActor [weak self] in
            self?.dispatchRoute(route)
        }
        completionHandler()
    }
}

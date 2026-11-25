import AppKit
import Combine

@main
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var supervisor: ProcessSupervisor!
    private var desktopActions: DesktopActions!
    private var audioPlayer: AudioStreamPlayer!
    private var brainClient: BrainClient!
    private var menuBarController: MenuBarController!
    private var quickEntryController: QuickEntryController!
    private var settingsController: SettingsWindowController!
    private var approvalController: ApprovalSheetController!
    private let hotkeyManager = HotkeyManager()
    private var cancellables = Set<AnyCancellable>()

    private static let singleInstanceNotice = Notification.Name("com.qimingxing.desktop.activateExisting")

    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if ensureSingleInstance() {
            NSApp.terminate(nil)
            return
        }

        NSApp.setActivationPolicy(.accessory)

        supervisor = ProcessSupervisor()
        desktopActions = DesktopActions()
        audioPlayer = AudioStreamPlayer()
        brainClient = BrainClient(
            httpBase: supervisor.httpBase,
            wsURLString: supervisor.wsURL,
            appVersion: supervisor.appVersion,
            desktopActions: desktopActions,
            audioPlayer: audioPlayer
        )

        quickEntryController = QuickEntryController(
            brainClient: brainClient,
            desktopActions: desktopActions
        )
        settingsController = SettingsWindowController(
            brainClient: brainClient,
            supervisor: supervisor,
            desktopActions: desktopActions
        )
        approvalController = ApprovalSheetController(brainClient: brainClient)

        menuBarController = MenuBarController(
            brainClient: brainClient,
            supervisor: supervisor,
            desktopActions: desktopActions,
            onOpenQuickEntry: { [weak self] in self?.quickEntryController.toggle() },
            onOpenSettings: { [weak self] in self?.settingsController.show() }
        )

        wireEvents()
        registerHotkeys()

        Task { [weak self] in
            guard let self else { return }
            await self.supervisor.startAll { [weak self] in
                self?.brainClient.start()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        hotkeyManager.unregisterAll()
        audioPlayer?.stop()
        brainClient?.stop()
        supervisor?.terminateAll()
    }

    private func ensureSingleInstance() -> Bool {
        if let myBundleId = Bundle.main.bundleIdentifier {
            let running = NSRunningApplication.runningApplications(withBundleIdentifier: myBundleId)
                .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
            if !running.isEmpty {
                DistributedNotificationCenter.default().postNotificationName(
                    Self.singleInstanceNotice,
                    object: nil,
                    userInfo: nil,
                    deliverImmediately: true
                )
                return true
            }
        }
        DistributedNotificationCenter.default().addObserver(
            forName: Self.singleInstanceNotice,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.quickEntryController.show()
            }
        }
        return false
    }

    private func wireEvents() {
        brainClient.onAvatarEnabledChanged = { [weak self] enabled in
            self?.supervisor.syncAvatarEnabled(enabled)
        }
        brainClient.onPendingApprovalsUpdated = { [weak self] items in
            self?.approvalController.syncPending(items)
        }
        brainClient.onApprovalResolved = { [weak self] aid in
            self?.approvalController.closeWindow(approvalId: aid)
        }
        brainClient.onShortcutsSynced = { [weak self] chatHk, settingsHk, cancelHk in
            guard let self else { return }
            self.hotkeyManager.updateFromStrings(
                chatInput: chatHk,
                settings: settingsHk,
                cancel: cancelHk,
                onQuickEntry: { [weak self] in self?.quickEntryController.toggle() },
                onSettings: { [weak self] in self?.settingsController.show() },
                onEmergencyStop: { [weak self] in self?.brainClient.cancelActiveTurn(reason: "hotkey_stop") }
            )
        }
        desktopActions.onNotificationOpenRequested = { [weak self] _ in
            self?.quickEntryController.show()
        }

        NotificationCenter.default.publisher(for: .qmxOpenQuickEntry)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.quickEntryController.show()
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: .qmxOpenSettings)
            .sink { [weak self] note in
                let tab = note.object as? String
                Task { @MainActor [weak self] in
                    self?.settingsController.show(tab: tab)
                }
            }
            .store(in: &cancellables)
    }

    private func registerHotkeys() {
        hotkeyManager.registerDefaults(
            onQuickEntry: { [weak self] in self?.quickEntryController.toggle() },
            onSettings: { [weak self] in self?.settingsController.show() },
            onEmergencyStop: { [weak self] in self?.brainClient.cancelActiveTurn(reason: "hotkey_stop") }
        )
    }
}

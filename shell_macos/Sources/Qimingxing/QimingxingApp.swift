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
    private var workspaceStore: WorkspaceStore!
    private var workspaceController: WorkspaceWindowController!
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
        let args = ProcessInfo.processInfo.arguments
        if let idx = args.firstIndex(of: "--dump-ui"), idx + 1 < args.count {
            let dirURL = URL(fileURLWithPath: args[idx + 1], isDirectory: true)
            let appearance: NSAppearance.Name = args.contains("--light") ? .aqua : .darkAqua
            UIPreviewDumper.dumpAll(to: dirURL, appearance: appearance)
            NSApp.terminate(nil)
            return
        }

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

        workspaceStore = WorkspaceStore(brainClient: brainClient)
        quickEntryController = QuickEntryController(
            brainClient: brainClient,
            desktopActions: desktopActions,
            workspaceStore: workspaceStore
        )
        settingsController = SettingsWindowController(
            brainClient: brainClient,
            supervisor: supervisor,
            desktopActions: desktopActions
        )
        approvalController = ApprovalSheetController(brainClient: brainClient)
        workspaceController = WorkspaceWindowController(store: workspaceStore, brainClient: brainClient)

        menuBarController = MenuBarController(
            brainClient: brainClient,
            supervisor: supervisor,
            desktopActions: desktopActions,
            onOpenQuickEntry: { [weak self] in self?.quickEntryController.toggle() },
            onOpenSettings: { [weak self] in self?.settingsController.show() },
            onOpenWorkspace: { [weak self] in
                self?.workspaceController.show(
                    anchoredTo: self?.quickEntryController.visiblePanelFrame,
                    movePanelBy: { [weak self] dx in
                        self?.quickEntryController.shiftHorizontally(by: dx)
                    }
                )
            }
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

        NotificationCenter.default.publisher(for: .qmxOpenWorkspace)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if self.quickEntryController.isVisible,
                       !self.brainClient.messages.isEmpty {
                        self.quickEntryController.showWorkspaceWing()
                    } else {
                        self.workspaceController.show()
                    }
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: .qmxHideQuickEntry)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.quickEntryController.hideAndRestoreFocus()
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: .qmxCloseWorkspaceWing)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.quickEntryController.hideWorkspaceWing()
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: .qmxDetachWorkspace)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.quickEntryController.hideWorkspaceWing()
                    // Let the wing collapse first, then dock the full window
                    // beside the (now narrower) chat panel.
                    try? await Task.sleep(nanoseconds: 280_000_000)
                    self.workspaceController.show(
                        anchoredTo: self.quickEntryController.visiblePanelFrame,
                        movePanelBy: { [weak self] dx in
                            self?.quickEntryController.shiftHorizontally(by: dx)
                        }
                    )
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

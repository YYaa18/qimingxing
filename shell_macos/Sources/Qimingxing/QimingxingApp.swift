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
    private var dropPocketController: DropPocketController!
    private var walkieTalkieController: WalkieTalkieVoiceController!
    private var spatialLassoController: SpatialLassoController!
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
        if args.contains("--verify-streaming") {
            StreamingEngineVerifier.runAll()
            NSApp.terminate(nil)
            return
        }
        if args.contains("--verify-phase1") {
            Phase1Verifier.runAll()
            NSApp.terminate(nil)
            return
        }
        if args.contains("--verify-phase2") {
            Phase2Verifier.runAll()
            NSApp.terminate(nil)
            return
        }
        if args.contains("--verify-phase3") {
            Phase3Verifier.runAll()
            NSApp.terminate(nil)
            return
        }
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
        installMainMenu()

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
        dropPocketController = DropPocketController(brainClient: brainClient)
        dropPocketController.onDeliverToQuickEntry = { [weak self] attachments, snippets in
            self?.quickEntryController.openWithStagedItems(attachments: attachments, textSnippets: snippets)
        }
        walkieTalkieController = WalkieTalkieVoiceController()
        walkieTalkieController.onTranscriptReady = { [weak self] text in
            self?.quickEntryController.submitPromptText(text)
        }
        walkieTalkieController.onCancelled = { [weak self] in
            self?.hotkeyManager.cancelQuickEntryHold()
        }
        spatialLassoController = SpatialLassoController(brainClient: brainClient)
        settingsController = SettingsWindowController(
            brainClient: brainClient,
            supervisor: supervisor,
            desktopActions: desktopActions
        )
        approvalController = ApprovalSheetController(brainClient: brainClient)
        workspaceController = WorkspaceWindowController(store: workspaceStore, brainClient: brainClient)

        MediaViewerLauncher.openHandler = { [weak self] prefersLargeWindow in
            guard let self else { return }
            if prefersLargeWindow || self.workspaceController.isVisible {
                if prefersLargeWindow, self.quickEntryController.isVisible {
                    self.quickEntryController.hideForMediaViewer()
                }
                MediaViewerWindowController.shared.show(large: prefersLargeWindow)
            } else if self.quickEntryController.isVisible {
                self.quickEntryController.showMediaViewer()
            } else {
                MediaViewerWindowController.shared.show()
            }
        }
        MediaViewerLauncher.attachHandler = { [weak self] in
            self?.quickEntryController.showMediaViewer()
        }

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
            },
            onOpenDropPocket: { [weak self] in self?.dropPocketController.toggleAtCursor() },
            onSpatialLasso: { [weak self] in self?.spatialLassoController.startSelection() }
        )
        dropPocketController.onPendingCountChanged = { [weak self] count in
            self?.menuBarController.setDropPocketBadgeCount(count)
        }

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
        workspaceStore?.flushOnTerminate()
        audioPlayer?.stop()
        brainClient?.stop()
        supervisor?.terminateAll()
    }

    /// Accessory apps have no visible menu bar, but standard editing keyboard
    /// shortcuts are routed through the main menu. Without this, Cmd+C/V/X
    /// silently do nothing in the Quick Entry composer.
    private func installMainMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(
            withTitle: "关于启明星",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "隐藏启明星",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"
        )
        appMenu.addItem(
            withTitle: "退出启明星",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        NSApp.mainMenu = mainMenu
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
                onQuickEntryHoldStart: { [weak self] in self?.walkieTalkieController.startHoldRecording() },
                onQuickEntryHoldEnd: { [weak self] in self?.walkieTalkieController.endHoldAndSubmit() },
                onQuickEntryHoldCancel: { [weak self] in self?.walkieTalkieController.cancelRecording() },
                onSettings: { [weak self] in self?.settingsController.show() },
                onEmergencyStop: { [weak self] in self?.brainClient.cancelActiveTurn(reason: "hotkey_stop") },
                onDropPocket: { [weak self] in self?.dropPocketController.toggleAtCursor() },
                onSpatialLasso: { [weak self] in self?.spatialLassoController.startSelection() }
            )
        }
        TaskToastController.shared.nativeNotificationHandler = { [weak self] title, message, route, _ in
            self?.desktopActions.deliverNativeNotification(
                title: title,
                body: message,
                route: route
            )
        }
        desktopActions.onNotificationRouteRequested = { [weak self] route in
            guard let self else { return }
            TaskToastController.shared.close()
            switch route {
            case .quickEntry:
                self.quickEntryController.show()
            case .workspace:
                if self.quickEntryController.isVisible, !self.brainClient.messages.isEmpty {
                    self.quickEntryController.showWorkspaceWing()
                } else {
                    self.workspaceController.show()
                }
            case .settings(let tab):
                self.settingsController.show(tab: tab)
            case .systemURL(let urlString):
                if let url = URL(string: urlString) {
                    NSWorkspace.shared.open(url)
                }
            case .none:
                break
            }
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

        NotificationCenter.default.publisher(for: .qmxMediaViewerClosed)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    try? await Task.sleep(nanoseconds: 250_000_000)
                    self?.quickEntryController.restoreAfterMediaViewer()
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

        NotificationCenter.default.publisher(for: .qmxToggleWalkieTalkie)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.walkieTalkieController.toggleClickRecording()
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: .qmxStartSpatialLasso)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.spatialLassoController.startSelection()
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: .qmxDragShakeSettingChanged)
            .sink { [weak self] note in
                let enabled = (note.object as? Bool)
                    ?? UserDefaults.standard.bool(forKey: DropPocketController.dragShakeDefaultsKey)
                Task { @MainActor [weak self] in
                    self?.dropPocketController.setDragShakeEnabled(enabled)
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
            onQuickEntryHoldStart: { [weak self] in self?.walkieTalkieController.startHoldRecording() },
            onQuickEntryHoldEnd: { [weak self] in self?.walkieTalkieController.endHoldAndSubmit() },
            onQuickEntryHoldCancel: { [weak self] in self?.walkieTalkieController.cancelRecording() },
            onSettings: { [weak self] in self?.settingsController.show() },
            onEmergencyStop: { [weak self] in self?.brainClient.cancelActiveTurn(reason: "hotkey_stop") },
            onDropPocket: { [weak self] in self?.dropPocketController.toggleAtCursor() },
            onSpatialLasso: { [weak self] in self?.spatialLassoController.startSelection() }
        )
    }
}

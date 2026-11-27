import AppKit
import XCTest
@testable import Qimingxing

@MainActor
final class WindowCoordinatorTests: XCTestCase {
    private func makeCoordinator() -> WindowCoordinator {
        WindowCoordinator()
    }

    func testPresentBecomesKeyOwner() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .quickEntry))
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
        XCTAssertTrue(coordinator.isActive(.quickEntry))
    }

    func testHigherPriorityTakesKey() {
        let coordinator = makeCoordinator()
        let quickEntry = coordinator.present(WindowRequest(role: .quickEntry))
        let approval = coordinator.present(WindowRequest(role: .approval, activation: .none))
        XCTAssertEqual(coordinator.keyOwner, .approval)

        coordinator.dismiss(approval)
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
        XCTAssertTrue(coordinator.isActive(.quickEntry))
        _ = quickEntry
    }

    func testTransientDoesNotStealKey() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .quickEntry))
        coordinator.present(WindowRequest(role: .dropPocket, activation: .none))
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
        XCTAssertTrue(coordinator.isActive(.dropPocket))
    }

    func testPassiveDoesNotStealKey() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .settings))
        coordinator.present(WindowRequest(role: .taskToast, activation: .none))
        XCTAssertEqual(coordinator.keyOwner, .settings)
    }

    func testSettingsSuspendsQuickEntry() {
        let coordinator = makeCoordinator()
        let quickEntry = coordinator.present(WindowRequest(role: .quickEntry))

        coordinator.present(WindowRequest(role: .settings))
        XCTAssertTrue(coordinator.isSuspended(.quickEntry))
        XCTAssertFalse(coordinator.isActive(.quickEntry))
        XCTAssertEqual(coordinator.keyOwner, .settings)
        XCTAssertEqual(
            coordinator.suspensionReason(for: .quickEntry),
            "presented:settings"
        )

        coordinator.dismiss(WindowLease(role: .settings, generation: 2))
        XCTAssertFalse(coordinator.isSuspended(.quickEntry))
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
        _ = quickEntry
    }

    func testQuickEntryOverSettingsSuspendsSettings() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .settings))
        coordinator.present(WindowRequest(role: .quickEntry))

        XCTAssertTrue(coordinator.isSuspended(.settings))
        XCTAssertFalse(coordinator.isActive(.settings))
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
        XCTAssertEqual(
            coordinator.suspensionReason(for: .settings),
            "quick_entry_over_settings"
        )
    }

    func testDismissQuickEntryResumesSettings() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .settings))
        let quickEntry = coordinator.present(WindowRequest(role: .quickEntry))
        coordinator.dismiss(quickEntry)

        XCTAssertFalse(coordinator.isSuspended(.settings))
        XCTAssertTrue(coordinator.isActive(.settings))
        XCTAssertEqual(coordinator.keyOwner, .settings)
    }

    func testApprovalDoesNotSuspendQuickEntry() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .quickEntry))
        coordinator.present(WindowRequest(role: .approval, activation: .none))
        XCTAssertFalse(coordinator.isSuspended(.quickEntry))
        XCTAssertTrue(coordinator.isActive(.quickEntry))
        XCTAssertEqual(coordinator.keyOwner, .approval)
    }

    func testDismissRestoresPreviousKeyOwner() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .quickEntry))
        let settings = coordinator.present(WindowRequest(role: .settings))
        coordinator.dismiss(settings)
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
    }

    func testStaleLeaseDismissIsIgnored() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .settings))
        let first = coordinator.present(WindowRequest(role: .settings))
        // A delayed close callback from the first presentation must not close
        // the replacement window.
        coordinator.dismiss(WindowLease(role: .settings, generation: 1))
        XCTAssertTrue(coordinator.isActive(.settings))
        XCTAssertEqual(coordinator.keyOwner, .settings)
        coordinator.dismiss(first)
        XCTAssertFalse(coordinator.isActive(.settings))
    }

    func testResumeRestoresRoleAndKey() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .quickEntry))
        coordinator.suspend(.quickEntry, reason: "test")
        XCTAssertNil(coordinator.keyOwner)

        coordinator.resume(.quickEntry)
        XCTAssertTrue(coordinator.isActive(.quickEntry))
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
    }

    func testPinnedNotesAllowMultipleInstances() {
        let coordinator = makeCoordinator()
        let first = coordinator.present(WindowRequest(role: .pinnedNote))
        let second = coordinator.present(WindowRequest(role: .pinnedNote))
        XCTAssertNotEqual(first.generation, second.generation)
        XCTAssertEqual(coordinator.activeRoles.filter { $0 == .pinnedNote }.count, 2)

        coordinator.dismiss(first)
        XCTAssertEqual(coordinator.activeRoles.filter { $0 == .pinnedNote }.count, 1)
    }

    func testSingleInstanceReplacementInvalidatesOldLease() {
        let coordinator = makeCoordinator()
        let first = coordinator.present(WindowRequest(role: .settings))
        let second = coordinator.present(WindowRequest(role: .settings))
        coordinator.dismiss(first)
        XCTAssertTrue(coordinator.isActive(.settings))
        coordinator.dismiss(second)
        XCTAssertFalse(coordinator.isActive(.settings))
    }

    func testExclusiveGroupDeduplicates() {
        let coordinator = makeCoordinator()
        let first = coordinator.present(
            WindowRequest(role: .workspaceWindow, exclusiveGroup: "turn_1")
        )
        let second = coordinator.present(
            WindowRequest(role: .workspaceWindow, exclusiveGroup: "turn_1")
        )
        XCTAssertEqual(first, second)
        XCTAssertEqual(coordinator.activeRoles.filter { $0 == .workspaceWindow }.count, 1)
    }

    func testKeyOwnerChangedFiresOnlyOnChange() {
        let coordinator = makeCoordinator()
        var changes: [WindowRole?] = []
        coordinator.onKeyOwnerChanged = { changes.append($0) }

        coordinator.present(WindowRequest(role: .quickEntry))
        coordinator.present(WindowRequest(role: .dropPocket, activation: .none))
        coordinator.present(WindowRequest(role: .quickEntry))

        XCTAssertEqual(changes, [.quickEntry])
    }

    func testWingDoesNotOwnKey() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .workspaceWing))
        XCTAssertNil(coordinator.keyOwner)
        XCTAssertTrue(coordinator.isActive(.workspaceWing))
    }

    func testWorkspaceTakesKeyWithoutSuspendingQuickEntry() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .quickEntry))
        let workspace = coordinator.present(WindowRequest(role: .workspaceWindow))

        XCTAssertFalse(coordinator.isSuspended(.quickEntry))
        XCTAssertTrue(coordinator.isActive(.quickEntry))
        XCTAssertEqual(coordinator.keyOwner, .workspaceWindow)

        coordinator.dismiss(workspace)
        XCTAssertEqual(coordinator.keyOwner, .quickEntry)
    }

    func testResumeDoesNotJumpAheadOfExistingContent() {
        let coordinator = makeCoordinator()
        coordinator.present(WindowRequest(role: .quickEntry))
        coordinator.present(WindowRequest(role: .workspaceWindow))
        let media = coordinator.present(WindowRequest(role: .mediaDetached))
        XCTAssertTrue(coordinator.isSuspended(.quickEntry))
        XCTAssertEqual(coordinator.keyOwner, .mediaDetached)

        coordinator.dismiss(media)
        XCTAssertFalse(coordinator.isSuspended(.quickEntry))
        // QuickEntry comes back, but the workspace keeps the key.
        XCTAssertEqual(coordinator.keyOwner, .workspaceWindow)
    }

    func testWorkspaceCloseDoesNotReopenClosedQuickEntry() {
        let coordinator = makeCoordinator()
        let quickEntry = coordinator.present(WindowRequest(role: .quickEntry))
        coordinator.dismiss(quickEntry)
        XCTAssertFalse(coordinator.isActive(.quickEntry))

        let workspace = coordinator.present(WindowRequest(role: .workspaceWindow))
        coordinator.dismiss(workspace)

        XCTAssertFalse(coordinator.isActive(.quickEntry))
        XCTAssertNil(coordinator.keyOwner)
    }

    func testPresentCallbacksReceiveActivationPolicy() {
        let coordinator = makeCoordinator()
        var seen: [(WindowRole, ActivationPolicy)] = []
        coordinator.onPresent = { role, policy in seen.append((role, policy)) }
        coordinator.present(WindowRequest(role: .quickEntry, activation: .restorePrevious))
        XCTAssertEqual(seen.count, 1)
        XCTAssertEqual(seen.first?.0, .quickEntry)
        if case .restorePrevious = seen.first!.1 {} else {
            XCTFail("expected restorePrevious")
        }
    }
}

@MainActor
final class FocusArbitratorTests: XCTestCase {
    private final class Recorder {
        var activations = 0
        var externalActivations = 0
        var appIsActive = true
    }

    private func makeArbitrator(_ recorder: Recorder) -> FocusArbitrator {
        let env = FocusArbitrator.Environment(
            frontmostApp: { NSRunningApplication.current },
            // Pretend the current app is external so the previous-app path can
            // be exercised without launching a real second app.
            isCurrentApp: { _ in false },
            appIsActive: { recorder.appIsActive },
            activateApp: { recorder.activations += 1 },
            activateExternalApp: { _ in recorder.externalActivations += 1 }
        )
        return FocusArbitrator(environment: env)
    }

    func testNonePolicyDoesNotActivate() {
        let recorder = Recorder()
        let arbitrator = makeArbitrator(recorder)
        arbitrator.activate(for: .taskToast, policy: .none)
        XCTAssertEqual(recorder.activations, 0)
    }

    func testRequiredPolicyActivatesAndRemembersExternalApp() {
        let recorder = Recorder()
        let arbitrator = makeArbitrator(recorder)
        arbitrator.activate(for: .quickEntry, policy: .required)
        XCTAssertEqual(recorder.activations, 1)
        XCTAssertNotNil(arbitrator.rememberedApp)
    }

    func testFrontmostCurrentAppIsNotRemembered() {
        let recorder = Recorder()
        var env = FocusArbitrator.Environment(
            frontmostApp: { NSRunningApplication.current },
            isCurrentApp: { _ in true },
            appIsActive: { true },
            activateApp: { recorder.activations += 1 },
            activateExternalApp: { _ in }
        )
        env.activateExternalApp = { _ in }
        let arbitrator = FocusArbitrator(environment: env)

        arbitrator.activate(for: .quickEntry, policy: .required)
        XCTAssertNil(arbitrator.rememberedApp)
    }

    func testRestoreOnlyWhenAppIsActive() {
        let recorder = Recorder()
        let arbitrator = makeArbitrator(recorder)
        arbitrator.activate(for: .quickEntry, policy: .restorePrevious)
        XCTAssertTrue(arbitrator.restoreIfAppropriate())
        XCTAssertEqual(recorder.externalActivations, 1)
        XCTAssertNil(arbitrator.rememberedApp)
    }

    func testRestoreSkippedWhenUserSwitchedAway() {
        let recorder = Recorder()
        let arbitrator = makeArbitrator(recorder)
        arbitrator.activate(for: .quickEntry, policy: .restorePrevious)
        recorder.appIsActive = false

        XCTAssertFalse(arbitrator.restoreIfAppropriate())
        XCTAssertEqual(recorder.externalActivations, 0)
    }

    func testRepeatedActivationKeepsFirstPreviousApp() {
        let recorder = Recorder()
        let arbitrator = makeArbitrator(recorder)
        arbitrator.activate(for: .quickEntry, policy: .required)
        let remembered = arbitrator.rememberedApp
        arbitrator.activate(for: .settings, policy: .required)
        XCTAssertEqual(arbitrator.rememberedApp, remembered)
        XCTAssertEqual(recorder.activations, 2)
    }
}

final class PanelConfigurationTests: XCTestCase {
    func testQuickEntryFloatsAndDoesNotBecomeKeyOnlyIfNeeded() {
        let config = PanelConfiguration.forRole(.quickEntry)
        XCTAssertEqual(config.level, .floating)
        XCTAssertTrue(config.styleMask.contains(.nonactivatingPanel))
        XCTAssertFalse(config.becomesKeyOnlyIfNeeded)
    }

    func testPassiveUsesStatusBar() {
        XCTAssertEqual(PanelConfiguration.forRole(.taskToast).level, .statusBar)
    }

    func testTransientIsNonActivating() {
        let config = PanelConfiguration.forRole(.dropPocket)
        XCTAssertEqual(config.level, .floating)
        XCTAssertTrue(config.styleMask.contains(.nonactivatingPanel))
        XCTAssertTrue(config.becomesKeyOnlyIfNeeded)
    }

    func testContentUsesNormalLevel() {
        let config = PanelConfiguration.forRole(.settings)
        XCTAssertEqual(config.level, .normal)
        XCTAssertTrue(config.styleMask.contains(.titled))
    }

    func testApprovalFloats() {
        let config = PanelConfiguration.forRole(.approval)
        XCTAssertEqual(config.level, .floating)
        XCTAssertFalse(config.becomesKeyOnlyIfNeeded)
    }

    func testSecurityFloats() {
        let config = PanelConfiguration.forRole(.permissionGuide)
        XCTAssertEqual(config.level, .floating)
        XCTAssertTrue(config.styleMask.contains(.nonactivatingPanel))
    }
}
